"""HU02 · Registrar organizaciones (tenants) y su administrador.

Quién puede: SOLO el administrador de plataforma (el `adm` del JWT). Es la
primera ruta con permiso por ROL, no solo por llave de servicio: la llave la
tiene el front entero, así que sin esto cualquier usuario con sesión podría
crear organizaciones.

Al crear una organización se crea TAMBIÉN a su administrador, como pide el
diseño (el modal captura sus datos). Sin correo en este sprint, la contraseña
temporal se devuelve UNA vez y la pantalla se la muestra al super admin para
que la entregue por donde pueda. Por eso la respuesta va con `no-store`.

Lo que vive en `servicios/tenants_registro.py` (y mañana en los SPs): el nombre
puede repetirse y el **slug** no. Ahí está el aviso de coincidencias del
diseño, con su "ID encontrado" y su "ID asignado".
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from servicios import auth, tenants_registro, usuarios
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)
router = APIRouter()


class Recuperacion(BaseModel):
    telefono: str | None = Field(default=None, max_length=25)
    email: str | None = Field(default=None, max_length=254)


class NuevaOrganizacion(BaseModel):
    nombre: str = Field(max_length=200)
    adminNombre: str = Field(max_length=200)
    adminApellidos: str = Field(default="", max_length=200)
    adminTelefono: str | None = Field(default=None, max_length=25)
    adminEmail: str = Field(max_length=254)
    recuperacion: Recuperacion | None = None


def _error(codigo_http: int, codigo: str, mensaje: str, **extra) -> HTTPException:
    return HTTPException(status_code=codigo_http, detail={"codigo": codigo, "mensaje": mensaje, **extra})


def _exigir_admin_plataforma(authorization: str | None) -> dict:
    """El usuario del Bearer, si es administrador de plataforma. Se vuelve a
    leer del registro (no basta el claim): una cuenta desactivada hace un rato
    todavía tiene un JWT válido unos minutos."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _error(status.HTTP_401_UNAUTHORIZED, "sin_sesion", "Falta el token de acceso.")
    try:
        claims = auth.verificar_acceso(authorization[7:].strip())
    except auth.SinConfigurar as exc:
        raise _error(status.HTTP_503_SERVICE_UNAVAILABLE, "sin_configurar", "El acceso no está configurado.") from exc
    if not claims:
        raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_invalida", "El token de acceso no es válido o venció.")
    try:
        u = usuarios.por_guid(claims["sub"])
    except usuarios.NoEncontrado as exc:
        raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_invalida", "El usuario ya no existe.") from exc
    if not u["activo"]:
        raise _error(status.HTTP_403_FORBIDDEN, "desactivada", "La cuenta está desactivada.")
    if not u["esAdminPlataforma"]:
        raise _error(status.HTTP_403_FORBIDDEN, "sin_permiso", "Solo un administrador de plataforma puede hacer esto.")
    return u


def _con_admin(t: dict) -> dict:
    """La organización como la muestra el listado: con los datos de su
    administrador, que es lo que pinta la tarjeta del diseño."""
    admin = None
    if t.get("adminGuid"):
        try:
            a = usuarios.por_guid(t["adminGuid"])
            admin = {
                "guid": a["guid"],
                "nombre": f"{a['nombre']} {a['apellidos']}".strip(),
                "email": a["email"],
                "telefono": a.get("telefono"),
            }
        except usuarios.NoEncontrado:
            admin = None
    return {**t, "admin": admin}


@router.get("/", summary="Listar las organizaciones")
def listar(authorization: str | None = Header(default=None)) -> dict:
    _exigir_admin_plataforma(authorization)
    try:
        return {"organizaciones": [_con_admin(t) for t in tenants_registro.listar()]}
    except ErrorAlmacen as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get(
    "/coincidencias",
    summary="¿Ya hay una organización con ese nombre?",
    description=(
        "Para el aviso del alta: devuelve las que coinciden (`ID encontrado`) y el slug que se "
        "asignaría (`ID asignado`). No crea nada."
    ),
)
def buscar_coincidencias(nombre: str = Query(max_length=200), authorization: str | None = Header(default=None)) -> dict:
    _exigir_admin_plataforma(authorization)
    try:
        return {
            "coincidencias": tenants_registro.coincidencias(nombre),
            "slugPropuesto": tenants_registro.slug_propuesto(nombre),
        }
    except ErrorAlmacen as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.get("/{guid}/usuarios", summary="Los usuarios de una organización")
def usuarios_de(guid: str, authorization: str | None = Header(default=None)) -> dict:
    _exigir_admin_plataforma(authorization)
    try:
        tenants_registro.por_guid(guid)
        return {"usuarios": usuarios.de_tenant(guid)}
    except tenants_registro.NoEncontrado as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "no_existe", "Esa organización no existe.") from exc
    except ErrorAlmacen as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    summary="Crear una organización con su administrador",
    description=(
        "Crea la organización y la cuenta de su administrador, con contraseña temporal. "
        "La contraseña se devuelve UNA vez (`contrasenaTemporal`): no hay correo en este sprint. "
        "409 `correo_registrado` si el correo del administrador ya existe."
    ),
)
def crear(datos: NuevaOrganizacion, response: Response, authorization: str | None = Header(default=None)) -> dict:
    _exigir_admin_plataforma(authorization)
    response.headers["Cache-Control"] = "no-store"
    nombre = " ".join(datos.nombre.split())
    if not nombre:
        raise _error(status.HTTP_400_BAD_REQUEST, "nombre_requerido", "El nombre de la organización es obligatorio.")
    if not usuarios.correo_valido(datos.adminEmail.strip().lower()):
        raise _error(status.HTTP_400_BAD_REQUEST, "correo_invalido", "Por favor, ingresa un correo electrónico válido.")
    try:
        temporal = auth.contrasena_temporal()
        # Primero el usuario: si el correo ya existe, no se crea la organización
        # a medias. (En la base será una transacción; aquí, el orden.)
        admin = usuarios.crear_usuario(
            email=datos.adminEmail,
            nombre=datos.adminNombre,
            apellidos=datos.adminApellidos,
            hash_contrasena=auth.hash_de(temporal),
            telefono=datos.adminTelefono,
        )
        organizacion = tenants_registro.crear(
            nombre,
            admin_guid=admin["guid"],
            recuperacion=datos.recuperacion.model_dump() if datos.recuperacion else {},
        )
        usuarios.agregar_membresia(admin["guid"], organizacion["guid"], "ADMIN")
        logger.info("Organización creada (slug=%s, admin=%s)", organizacion["slug"], admin["guid"])
        return {
            "organizacion": _con_admin(organizacion),
            "admin": admin,
            "contrasenaTemporal": temporal,
        }
    except usuarios.YaExiste as exc:
        raise _error(status.HTTP_409_CONFLICT, "correo_registrado", str(exc)) from exc
    except ValueError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, "datos_invalidos", str(exc)) from exc
    except ErrorAlmacen as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
