"""Aislamiento entre organizaciones: la SEGUNDA RED (HU11).

## Por qué hace falta

Las rutas de bandeja, llaves, webhooks y archivos van detrás de la llave de servicio, que tiene el front entero, y reciben
el `tenant` como un dato más de la petición. Hasta el 2026-10-14 este back se creía lo que el front le dijera: el
aislamiento dependía de que el BFF calculara bien el tenant desde la sesión (y `/api/archivos` no lo hacía: tomaba el
`tenant` del formulario del navegador). Un solo olvido en el front y una organización leía o escribía en otra.

Ahora esas rutas exigen TAMBIÉN el JWT de la persona (el BFF lo reenvía) y comprueban aquí, contra el registro y no contra
lo que el JWT dijo hace minutos, que esa persona puede operar en ese tenant:

- Si pertenece a una organización, solo en la suya (`membresias`).
- Si es administrador de plataforma, que no pertenece a ninguna, solo en el espacio de plataforma: cualquier tenant que NO
  sea el guid de una organización. Nunca en el de una organización: para eso están las rutas de `/organizaciones`.
- Sin Bearer, sesión cerrada, cuenta desactivada: 401 o 403, como en el resto de la API.

## Qué se contesta (CA-4 y CA-5)

Un tenant que no es el tuyo: 403 `sin_acceso`, con un mensaje que no nombra nada de la otra organización. Un recurso (un
webhook, una llave, un archivo) que existe pero es de otra: lo MISMO que si no existiera, un 404. Quien lo intentó no
aprende si existe.

## Qué se anota (CA-6)

Cada intento de este tipo deja `CROSS_TENANT_ACCESS` en la bitácora de auditoría (`intento_cruzado`): quién, a qué
organizaciones pertenece, qué pidió y por dónde se detectó. La anotación nunca cambia la respuesta: si el registro de
auditoría falla, se avisa en el log y el rechazo se da igual.

## Lo heredado

Los ejemplos documentales de la configuración se guardaban TODOS bajo el prefijo fijo `csi`, para cualquiera. Ese prefijo
sigue existiendo en el almacén (`PREFIJOS_HEREDADOS`): lo lee el administrador de plataforma, y quien pertenece a una
organización recibe el 404 de siempre, SIN que cuente como intento cruzado (es su propio apuntador viejo, no un ataque).
"""

import logging
from typing import NamedTuple

from fastapi import HTTPException, status

from servicios import auditoria, auth, roles, tenants_registro, usuarios
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

#: Prefijos del almacén que no son de ninguna organización: los ejemplos que se guardaban antes de que existieran. Solo el
#: administrador de plataforma los lee; a los demás se les dice que no existe, sin anotarlo como intento.
PREFIJOS_HEREDADOS = frozenset({"csi"})

MENSAJE_SIN_ACCESO = "No tienes acceso a esa organización."


class Actor(NamedTuple):
    """Quién pide, ya verificado."""

    guid: str
    #: Los guids de las organizaciones a las que pertenece (vacío para el administrador de plataforma).
    tenants: tuple[str, ...]
    es_admin_plataforma: bool
    #: El guid de la sesión desde la que pide; `None` si quien anota el intento no lo tiene a la mano.
    sesion: str | None


def _error(codigo_http: int, codigo: str, mensaje: str) -> HTTPException:
    return HTTPException(status_code=codigo_http, detail={"codigo": codigo, "mensaje": mensaje})


def actor_de(authorization: str | None) -> Actor:
    """La persona del Bearer, con su sesión viva y su cuenta activa; 401 o 403 si no."""
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
    except ErrorAlmacen as exc:
        logger.exception("No se pudo leer el registro de usuarios para comprobar el aislamiento")
        raise _error(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "no_disponible",
            "El servicio no está disponible en este momento. Intenta de nuevo en unos minutos.",
        ) from exc
    if not u["activo"]:
        raise _error(status.HTTP_403_FORBIDDEN, "desactivada", "La cuenta está desactivada.")
    if not viva:
        raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_cerrada", "La sesión ya no es válida. Inicia sesión de nuevo.")
    return Actor(
        guid=u["guid"],
        tenants=tuple(m["tenantGuid"] for m in u["membresias"]),
        es_admin_plataforma=bool(u["esAdminPlataforma"]),
        sesion=claims["sid"],
    )


def es_organizacion(tenant: str) -> bool:
    """¿Este tenant es el guid de una organización registrada?

    Se compara SIN espacios en los extremos y sin distinguir mayúsculas: el almacén recorta el tenant antes de usarlo, y
    un montaje que no distingue mayúsculas llevaría `ABC…` al mismo directorio que `abc…`. Si aquí no se igualara, al
    administrador de plataforma le bastaría escribir el guid de una organización con un espacio o en mayúsculas para que
    no lo reconociera como organización."""
    normal = (tenant or "").strip().lower()
    return any(t["guid"].lower() == normal for t in tenants_registro.listar())


def puede_operar_en(actor: Actor, tenant: str) -> bool:
    """Las dos únicas formas de poder: pertenecer a esa organización (con su guid EXACTO), o ser administrador de plataforma
    y que el tenant no sea el de ninguna organización (el espacio de plataforma)."""
    if tenant in actor.tenants:
        return True
    return actor.es_admin_plataforma and not es_organizacion(tenant)


def puede_descartar(actor: Actor, tenant: str) -> bool:
    """¿Su rol en ESA organización permite descartar documentos? El administrador de plataforma sí (en su espacio, que es lo único
    donde `exigir` lo deja operar). Se lee del registro, no del JWT."""
    if actor.es_admin_plataforma:
        return True
    try:
        u = usuarios.por_guid(actor.guid)
    except usuarios.NoEncontrado:
        return False
    return any(m["tenantGuid"] == tenant and m["rol"] in roles.PUEDEN_DESCARTAR for m in u["membresias"])


def _roles_en(actor: Actor, tenant: str) -> list[str]:
    """Los roles que la persona tiene en ESA organización, leídos del registro (no del JWT, que dura 15 minutos)."""
    try:
        u = usuarios.por_guid(actor.guid)
    except usuarios.NoEncontrado:
        return []
    return [m["rol"] for m in u["membresias"] if m["tenantGuid"] == tenant]


def puede_administrar(actor: Actor, tenant: str) -> bool:
    """¿Su rol en ESA organización permite administrar integraciones y configuración? (`roles.PUEDEN_ADMINISTRAR`). El
    administrador de plataforma sí, en su espacio (que es lo único donde `exigir` lo deja operar)."""
    if actor.es_admin_plataforma:
        return True
    return any(r in roles.PUEDEN_ADMINISTRAR for r in _roles_en(actor, tenant))


def exigir_administrar(authorization: str | None, tenant: str, recurso: str) -> Actor:
    """`exigir` y, además, que el rol de la persona pueda administrar (NEX-472). Primero el aislamiento: quien no es de la
    organización recibe lo de siempre (403 `sin_acceso`, con su CROSS_TENANT_ACCESS); quien sí lo es pero no tiene el rol recibe
    403 `sin_permiso` y queda `ROLE_DENIED` en la bitácora. No dice qué rol sí puede."""
    actor = exigir(authorization, tenant, recurso)
    if not puede_administrar(actor, tenant):
        try:
            auditoria.registrar(
                auditoria.ROLE_DENIED,
                usuario_guid=actor.guid,
                sesionGuid=actor.sesion,
                tenant=tenant,
                recurso=recurso,
                roles=_roles_en(actor, tenant),
            )
        except (ErrorAlmacen, OSError) as exc:
            logger.warning("No se pudo registrar el ROLE_DENIED en la bitácora de auditoría: %s", exc)
        raise _error(status.HTTP_403_FORBIDDEN, "sin_permiso", "Tu rol no puede administrar esto.")
    return actor


def puede_organizar(actor: Actor, tenant: str) -> bool:
    """¿Su rol en ESA organización permite organizar documentos en expedientes? (`roles.PUEDEN_ORGANIZAR`). El administrador de
    plataforma sí, en su espacio (que es lo único donde `exigir` lo deja operar)."""
    if actor.es_admin_plataforma:
        return True
    return any(r in roles.PUEDEN_ORGANIZAR for r in _roles_en(actor, tenant))


def exigir_organizar(authorization: str | None, tenant: str, recurso: str) -> Actor:
    """`exigir` y, además, que el rol de la persona pueda organizar documentos en expedientes. Igual que `exigir_administrar`: quien
    no es de la organización recibe 403 `sin_acceso`; quien sí lo es pero tiene un rol de solo consulta recibe 403 `sin_permiso` y
    queda `ROLE_DENIED` en la bitácora, sin decir qué rol sí puede."""
    actor = exigir(authorization, tenant, recurso)
    if not puede_organizar(actor, tenant):
        try:
            auditoria.registrar(
                auditoria.ROLE_DENIED,
                usuario_guid=actor.guid,
                sesionGuid=actor.sesion,
                tenant=tenant,
                recurso=recurso,
                roles=_roles_en(actor, tenant),
            )
        except (ErrorAlmacen, OSError) as exc:
            logger.warning("No se pudo registrar el ROLE_DENIED en la bitácora de auditoría: %s", exc)
        raise _error(status.HTTP_403_FORBIDDEN, "sin_permiso", "Tu rol no puede organizar documentos en expedientes.")
    return actor


def intento_cruzado(actor: Actor, recurso: str, recurso_id: str | None = None, tenant_pedido: str | None = None, via: str = "id") -> None:
    """Anota `CROSS_TENANT_ACCESS`. No levanta nada: es un rastro, no una decisión (ver el docstring del módulo)."""
    try:
        auditoria.registrar(
            auditoria.CROSS_TENANT_ACCESS,
            usuario_guid=actor.guid,
            sesionGuid=actor.sesion,
            tenantsDelActor=list(actor.tenants),
            tenantPedido=tenant_pedido,
            recurso=recurso,
            recursoId=recurso_id,
            via=via,
        )
    except (ErrorAlmacen, OSError) as exc:
        logger.warning("No se pudo registrar el CROSS_TENANT_ACCESS en la bitácora de auditoría: %s", exc)


def exigir(authorization: str | None, tenant: str, recurso: str) -> Actor:
    """Deja pasar si quien pide puede operar en `tenant`; si no, anota el intento y responde 403 `sin_acceso`.

    Es para las rutas que RECIBEN el tenant (el cuerpo, el formulario o la consulta). `recurso` dice de qué ruta se trata
    (`webhook`, `llave`, `bandeja`, `archivo`): va a la bitácora."""
    actor = actor_de(authorization)
    if not puede_operar_en(actor, tenant):
        intento_cruzado(actor, recurso, tenant_pedido=tenant, via="tenant")
        raise _error(status.HTTP_403_FORBIDDEN, "sin_acceso", MENSAJE_SIN_ACCESO)
    return actor


def exigir_ruta(authorization: str | None, ruta: str) -> Actor:
    """Lo mismo para leer un archivo por su ruta (`{tenant}/aa/bb/sha256`): el tenant es el primer segmento.

    Aquí un archivo ajeno responde 404, igual que uno que no existe: la ruta ya nombra una organización, y un 403 le
    confirmaría que existe. El prefijo heredado (`csi`) responde igual pero no se anota."""
    actor = actor_de(authorization)
    prefijo = ruta.split("/", 1)[0]
    if puede_operar_en(actor, prefijo):
        return actor
    if prefijo not in PREFIJOS_HEREDADOS:
        intento_cruzado(actor, "archivo", recurso_id=ruta, tenant_pedido=prefijo, via="ruta")
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ese archivo no está en el almacén.")
