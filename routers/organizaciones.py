"""HU02 · Registrar organizaciones (tenants) y su administrador.

Quién puede: SOLO el administrador de plataforma (el `adm` del JWT). Es la
primera ruta con permiso por ROL, no solo por llave de servicio: la llave la
tiene el front entero, así que sin esto cualquier usuario con sesión podría
crear organizaciones.

Además de crear organizaciones, el administrador de plataforma ve los usuarios
de cada una y puede CERRARLES LA SESIÓN (HU05 CA-4): la misma acción que tiene el
administrador de la organización, pero sobre cualquiera de ellas.

Al crear una organización se crea TAMBIÉN a su administrador, como pide el
diseño (el modal captura sus datos), y se le manda por correo la bienvenida con
sus accesos temporales (`servicios/invitaciones.py`). Si el correo salió, la
contraseña NO se devuelve; si no salió (este ambiente no manda correo, o el
servicio falló), se devuelve UNA vez y la pantalla se la muestra al super admin
para que la entregue por donde pueda. Por eso la respuesta va con `no-store`.

Lo que vive en `servicios/tenants_registro.py` (y mañana en los SPs): el nombre
puede repetirse y el **slug** no. Ahí está el aviso de coincidencias del
diseño, con su "ID encontrado" y su "ID asignado".
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from routers.errores import almacen_no_disponible
from servicios import auth, invitaciones, tenants_registro, usuarios
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)
router = APIRouter()


class Recuperacion(BaseModel):
    telefono: str | None = Field(default=None, max_length=25)
    email: str | None = Field(default=None, max_length=254)


class CierreSesion(BaseModel):
    motivo: str = Field(max_length=500)


class NuevaOrganizacion(BaseModel):
    nombre: str = Field(max_length=200)
    adminNombre: str = Field(max_length=200)
    adminApellidos: str = Field(default="", max_length=200)
    # Los formularios los piden por separado; si vienen, mandan sobre `adminApellidos`.
    adminApellidoPaterno: str = Field(default="", max_length=100)
    adminApellidoMaterno: str = Field(default="", max_length=100)
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
        u, viva = usuarios.por_guid_y_sesion(claims["sub"], claims["sid"])
    except usuarios.NoEncontrado as exc:
        raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_invalida", "El usuario ya no existe.") from exc
    if not u["activo"]:
        raise _error(status.HTTP_403_FORBIDDEN, "desactivada", "La cuenta está desactivada.")
    if not viva:
        raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_cerrada", "La sesión ya no es válida. Inicia sesión de nuevo.")
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
        raise almacen_no_disponible(exc) from exc


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
        raise almacen_no_disponible(exc) from exc


@router.get(
    "/{guid}/usuarios",
    summary="Los usuarios de una organización",
    description=(
        "Con la misma forma que el listado que ve su administrador (`GET /usuarios`): número de orden, nombre, "
        "rol EN esa organización, estado y fecha de alta."
    ),
)
def usuarios_de(guid: str, authorization: str | None = Header(default=None)) -> dict:
    _exigir_admin_plataforma(authorization)
    try:
        tenants_registro.por_guid(guid)
        lista = usuarios.de_tenant(guid)
    except tenants_registro.NoEncontrado as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "no_existe", "Esa organización no existe.") from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    return {"usuarios": [usuarios.para_listado(u, guid, i + 1) for i, u in enumerate(lista)]}


@router.post(
    "/{guid}/usuarios/{usuario}/reenviar-invitacion",
    summary="Reenviar la invitación de un usuario de cualquier organización (HU06 CA-6)",
    description=(
        "Lo mismo que `POST /usuarios/{guid}/reenviar-invitacion`, pero ejecutado por el administrador de plataforma "
        "sobre un usuario de la organización `guid` (es quien le reenvía el acceso al ADMINISTRADOR de una "
        "organización, que no tiene a nadie por encima en su organización). 404 si la organización no existe o el "
        "usuario no es de ella; 409 `ya_activa` o `desactivada`."
    ),
)
def reenviar_invitacion_de(guid: str, usuario: str, response: Response, authorization: str | None = Header(default=None)) -> dict:
    yo = _exigir_admin_plataforma(authorization)
    response.headers["Cache-Control"] = "no-store"
    try:
        tenants_registro.por_guid(guid)
        objetivo = usuarios.por_guid(usuario)
    except tenants_registro.NoEncontrado as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "no_existe", "Esa organización no existe.") from exc
    except usuarios.NoEncontrado as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "no_existe", "Ese usuario no existe.") from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    if not any(m["tenantGuid"] == guid for m in objetivo["membresias"]):
        raise _error(status.HTTP_404_NOT_FOUND, "no_existe", "Ese usuario no existe.")
    negativa = invitaciones.por_que_no_reenviar(objetivo)
    if negativa:
        raise _error(status.HTTP_409_CONFLICT, *negativa)
    try:
        resultado, temporal = invitaciones.reenviar(usuario, actor_guid=yo["guid"])
        lista = usuarios.de_tenant(guid)
        posicion = next((i + 1 for i, u in enumerate(lista) if u["guid"] == usuario), len(lista))
        logger.info("Invitación reenviada por el administrador de plataforma (guid=%s, resultado=%s)", usuario, resultado)
        return {"usuario": usuarios.para_listado(usuarios.por_guid(usuario), guid, posicion), **invitaciones.para_respuesta(resultado, temporal)}
    except usuarios.NoPendiente as exc:
        raise _error(status.HTTP_409_CONFLICT, "ya_activa", "Este usuario ya hizo su primer acceso: no hay invitación que reenviar.") from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc


@router.post(
    "/{guid}/usuarios/{usuario}/cerrar-sesion",
    summary="Cerrar la sesión de un usuario de cualquier organización (HU05)",
    description=(
        "Lo mismo que `POST /usuarios/{guid}/cerrar-sesion`, pero ejecutado por el administrador de plataforma "
        "sobre un usuario de la organización `guid`: revoca TODAS sus sesiones, sin desactivarlo. Motivo "
        "obligatorio; queda como `FORCED_LOGOUT` con `actor: ADMIN_PLATAFORMA`. 404 si la organización no "
        "existe o el usuario no es de ella. Un administrador de plataforma no puede cerrarse la sesión a sí mismo."
    ),
)
def cerrar_sesion_de(
    guid: str, usuario: str, datos: CierreSesion, authorization: str | None = Header(default=None)
) -> dict:
    yo = _exigir_admin_plataforma(authorization)
    motivo = datos.motivo.strip()
    if not motivo:
        raise _error(status.HTTP_400_BAD_REQUEST, "motivo_requerido", "Escribe el motivo.")
    try:
        tenants_registro.por_guid(guid)
        objetivo = usuarios.por_guid(usuario)
    except tenants_registro.NoEncontrado as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "no_existe", "Esa organización no existe.") from exc
    except usuarios.NoEncontrado as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "no_existe", "Ese usuario no existe.") from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    # Se cierra la sesión de un MIEMBRO de esa organización, no de cualquiera que
    # se nombre en la URL: la ruta cuelga de la organización justamente para que el
    # permiso y el listado que se ve en pantalla digan lo mismo que lo que se ejecuta.
    if not any(m["tenantGuid"] == guid for m in objetivo["membresias"]):
        raise _error(status.HTTP_404_NOT_FOUND, "no_existe", "Ese usuario no existe.")
    if objetivo["guid"] == yo["guid"]:
        raise _error(
            status.HTTP_409_CONFLICT,
            "no_a_ti_mismo",
            "Para cerrar tu propia sesión usa el menú de tu cuenta.",
        )
    try:
        cerradas = usuarios.forzar_cierre(usuario, motivo, yo["guid"], guid, usuarios.ACTOR_ADMIN_PLATAFORMA)
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    logger.info("Sesiones cerradas por el administrador de plataforma (guid=%s, cuantas=%s)", usuario, cerradas)
    return {"cerradas": cerradas}


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    summary="Crear una organización con su administrador",
    description=(
        "Crea la organización y la cuenta de su administrador, con contraseña temporal, y le manda por "
        "correo su bienvenida con los accesos (`correoEnviado`). La contraseña se devuelve UNA vez "
        "(`contrasenaTemporal`) SOLO si el correo no salió (`correoMotivo`: `sin_canal` o `fallo`). "
        "409 `correo_registrado` si el correo del administrador ya existe."
    ),
)
def crear(datos: NuevaOrganizacion, response: Response, authorization: str | None = Header(default=None)) -> dict:
    yo = _exigir_admin_plataforma(authorization)
    response.headers["Cache-Control"] = "no-store"
    nombre = " ".join(datos.nombre.split())
    if not nombre:
        raise _error(status.HTTP_400_BAD_REQUEST, "nombre_requerido", "El nombre de la organización es obligatorio.")
    if not usuarios.correo_valido(datos.adminEmail.strip().lower()):
        raise _error(status.HTTP_400_BAD_REQUEST, "correo_invalido", "Por favor, ingresa un correo electrónico válido.")
    try:
        recuperacion = usuarios.normalizar_recuperacion(
            datos.recuperacion.model_dump() if datos.recuperacion else {}
        )
    except ValueError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, "correo_recuperacion_invalido", str(exc)) from exc
    try:
        temporal = auth.contrasena_temporal()
        # Primero el usuario: si el correo ya existe, no se crea la organización
        # a medias. (En la base será una transacción; aquí, el orden.)
        admin = usuarios.crear_usuario(
            email=datos.adminEmail,
            nombre=datos.adminNombre,
            apellidos=datos.adminApellidos,
            apellido_paterno=datos.adminApellidoPaterno,
            apellido_materno=datos.adminApellidoMaterno,
            hash_contrasena=auth.hash_de(temporal),
            telefono=datos.adminTelefono,
            # Es la recuperación de la cuenta de SU administrador: también va en su perfil, no solo en la organización.
            recuperacion=recuperacion,
        )
        organizacion = tenants_registro.crear(
            nombre,
            admin_guid=admin["guid"],
            recuperacion=recuperacion,
        )
        usuarios.agregar_membresia(admin["guid"], organizacion["guid"], "ADMIN")
        logger.info("Organización creada (slug=%s, admin=%s)", organizacion["slug"], admin["guid"])
        resultado = invitaciones.enviar_bienvenida(
            admin["email"], admin["nombre"], temporal, "admin_organizacion", organizacion["nombre"]
        )
        invitaciones.auditar_alta(yo["guid"], admin["guid"], "ADMIN", organizacion["guid"], resultado)
        return {
            "organizacion": _con_admin(organizacion),
            "admin": admin,
            **invitaciones.para_respuesta(resultado, temporal),
        }
    except usuarios.YaExiste as exc:
        raise _error(status.HTTP_409_CONFLICT, "correo_registrado", str(exc)) from exc
    except ValueError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, "datos_invalidos", str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
