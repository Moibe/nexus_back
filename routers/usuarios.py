"""HU06 · Crear usuarios dentro de una organización. HU07 · Su rol.

Quién puede: el **administrador de la organización** (membresía con rol
`ADMIN`). Opera siempre sobre SU organización, la del JWT: no recibe el tenant
por parámetro, justamente para que nadie pueda tocar la de al lado cambiando un
id en la petición.

Sin correo en este sprint: al crear un usuario se devuelve su contraseña
temporal UNA vez y la pantalla se la muestra al administrador. El correo de
bienvenida del diseño, y su enlace que caduca a las 48 horas, quedan para
cuando haya envío de correo.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

from servicios import auth, roles, tenants_registro, usuarios
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)
router = APIRouter()


class NuevoUsuario(BaseModel):
    nombre: str = Field(max_length=200)
    apellidos: str = Field(default="", max_length=200)
    email: str = Field(max_length=254)
    telefono: str | None = Field(default=None, max_length=25)
    rol: str = Field(max_length=40)


class CambioUsuario(BaseModel):
    nombre: str = Field(max_length=200)
    apellidos: str = Field(default="", max_length=200)
    telefono: str | None = Field(default=None, max_length=25)
    rol: str = Field(max_length=40)


def _error(codigo_http: int, codigo: str, mensaje: str, **extra) -> HTTPException:
    return HTTPException(status_code=codigo_http, detail={"codigo": codigo, "mensaje": mensaje, **extra})


def _admin_de_organizacion(authorization: str | None) -> tuple[dict, str]:
    """(usuario, guid de su organización) si es administrador de una. Se relee
    del registro: un JWT emitido hace minutos puede estar desactualizado."""
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
    propia = next((m for m in u["membresias"] if m["rol"] == roles.ADMIN), None)
    if propia is None:
        raise _error(
            status.HTTP_403_FORBIDDEN,
            "sin_organizacion",
            "Solo el administrador de una organización puede gestionar a sus usuarios.",
        )
    return u, propia["tenantGuid"]


def _publico(u: dict, tenant_guid: str, posicion: int) -> dict:
    """El usuario como lo pinta el listado: su rol EN ESTA organización, el
    número de orden que el diseño muestra como ID, y el nombre del rol ya
    resuelto (el front no debería traducir códigos)."""
    rol = next((m["rol"] for m in u["membresias"] if m["tenantGuid"] == tenant_guid), None)
    return {
        "guid": u["guid"],
        "numero": f"{posicion:03d}",
        "nombre": f"{u['nombre']} {u['apellidos']}".strip(),
        "email": u["email"],
        "telefono": u.get("telefono"),
        "rol": rol,
        "rolNombre": roles.nombre_de(rol) if rol else "—",
        "activo": u["activo"],
        "creadoEn": u["creadoEn"],
        "debeCambiarContrasena": u["debeCambiarContrasena"],
    }


@router.get("/roles", summary="Los roles que se pueden asignar")
def catalogo_de_roles(authorization: str | None = Header(default=None)) -> dict:
    _admin_de_organizacion(authorization)
    return {"roles": roles.CATALOGO}


@router.get("/", summary="Los usuarios de mi organización")
def listar(authorization: str | None = Header(default=None)) -> dict:
    _, tenant = _admin_de_organizacion(authorization)
    try:
        lista = usuarios.de_tenant(tenant)
        organizacion = tenants_registro.por_guid(tenant)
    except tenants_registro.NoEncontrado as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "no_existe", "Tu organización ya no existe.") from exc
    except ErrorAlmacen as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return {
        "organizacion": {"guid": organizacion["guid"], "nombre": organizacion["nombre"]},
        "usuarios": [_publico(u, tenant, i + 1) for i, u in enumerate(lista)],
    }


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    summary="Crear un usuario en mi organización",
    description=(
        "Lo crea con contraseña temporal, que se devuelve UNA vez (`contrasenaTemporal`): no hay "
        "correo en este sprint. 409 `correo_registrado` si el correo ya existe."
    ),
)
def crear(datos: NuevoUsuario, response: Response, authorization: str | None = Header(default=None)) -> dict:
    _, tenant = _admin_de_organizacion(authorization)
    response.headers["Cache-Control"] = "no-store"
    if datos.rol not in roles.ASIGNABLES:
        raise _error(status.HTTP_400_BAD_REQUEST, "rol_invalido", "Elige un rol de la lista.")
    if not usuarios.correo_valido(datos.email.strip().lower()):
        raise _error(status.HTTP_400_BAD_REQUEST, "correo_invalido", "Por favor, ingresa un correo electrónico válido.")
    try:
        temporal = auth.contrasena_temporal()
        creado = usuarios.crear_usuario(
            email=datos.email,
            nombre=datos.nombre,
            apellidos=datos.apellidos,
            hash_contrasena=auth.hash_de(temporal),
            telefono=datos.telefono,
            tenant_guid=tenant,
            rol=datos.rol,
        )
        logger.info("Usuario creado (guid=%s, tenant=%s, rol=%s)", creado["guid"], tenant, datos.rol)
        lista = usuarios.de_tenant(tenant)
        posicion = next((i + 1 for i, u in enumerate(lista) if u["guid"] == creado["guid"]), len(lista))
        return {"usuario": _publico(creado, tenant, posicion), "contrasenaTemporal": temporal}
    except usuarios.YaExiste as exc:
        raise _error(status.HTTP_409_CONFLICT, "correo_registrado", str(exc)) from exc
    except ValueError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, "datos_invalidos", str(exc)) from exc
    except ErrorAlmacen as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


@router.post(
    "/{guid}",
    summary="Actualizar un usuario de mi organización (HU07)",
    description="Cambia sus datos y su rol. El correo no se toca: es con el que entra.",
)
def actualizar(guid: str, datos: CambioUsuario, authorization: str | None = Header(default=None)) -> dict:
    yo, tenant = _admin_de_organizacion(authorization)
    if datos.rol not in roles.VALIDOS:
        raise _error(status.HTTP_400_BAD_REQUEST, "rol_invalido", "Elige un rol de la lista.")
    try:
        objetivo = usuarios.por_guid(guid)
    except usuarios.NoEncontrado as exc:
        raise _error(status.HTTP_404_NOT_FOUND, "no_existe", "Ese usuario no existe.") from exc
    if not any(m["tenantGuid"] == tenant for m in objetivo["membresias"]):
        # Ni siquiera se admite que exista: un administrador no tiene por qué
        # saber quién hay en otra organización.
        raise _error(status.HTTP_404_NOT_FOUND, "no_existe", "Ese usuario no existe.")
    if objetivo["guid"] == yo["guid"] and datos.rol != roles.ADMIN:
        raise _error(
            status.HTTP_409_CONFLICT,
            "no_puedes_degradarte",
            "No puedes quitarte a ti mismo el rol de administrador.",
        )
    try:
        usuarios.actualizar_datos(guid, datos.nombre, datos.apellidos, datos.telefono)
        actualizado = usuarios.agregar_membresia(guid, tenant, datos.rol)
        lista = usuarios.de_tenant(tenant)
        posicion = next((i + 1 for i, u in enumerate(lista) if u["guid"] == guid), len(lista))
        logger.info("Usuario actualizado (guid=%s, rol=%s)", guid, datos.rol)
        return {"usuario": _publico(actualizado, tenant, posicion)}
    except ValueError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, "datos_invalidos", str(exc)) from exc
    except ErrorAlmacen as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
