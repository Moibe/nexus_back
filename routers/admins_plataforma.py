"""Dominio: administradores de plataforma — crearlos, verlos, regenerarles la contraseña y retirarlos.

Lo usa Nexus Monitor (el proyecto hermano, la herramienta del operador), no la plataforma: un administrador de plataforma
no se da de alta desde NexusDoc sino desde fuera, por quien opera el sistema. Hasta ahora eso era `bootstrap_admin.py`,
`reenviar_invitacion.py` y `retirar_cuenta.py` en una terminal del servidor; estas rutas hacen lo MISMO con los mismos
servicios (`usuarios`, `invitaciones`, `tokens`, `auditoria`) para poder hacerlo desde una pantalla y llevar la cuenta de los
que existen. Los scripts siguen sirviendo.

## ⚠️ Qué NO tiene (decisión del usuario, 2026-10-20: «sin protección extra por ahora»)

Estas rutas piden SOLO la llave de servicio, como `/esquema` o `/procesos`. No hay un JWT de persona (Monitor no tiene
login) ni una llave de administración aparte. Quiere decir que **quien tenga la llave de servicio y llegue al puerto del back
puede crear un administrador de plataforma**, la cuenta con más poder del sistema; y que quien abra Monitor desde la VPN, también.
Antes de esto, crear uno exigía tener acceso al servidor. Cuando se quiera cerrar: una `ADMIN_PLATAFORMA_LLAVE` en el `.env`
que estas rutas exijan en un encabezado (y que Monitor pida tecleada), o apagar las rutas si no está puesta.

## Qué se anota (auditoría)

Crear (`USER_CREATED`), regenerar la contraseña (`INVITATION_REISSUED`) y retirar (`USER_DELETED`) dejan rastro con `actor:
"monitor"` y qué pasó con el correo. Nunca la contraseña.

## Qué se cuida

- Solo se toca a quien ES administrador de plataforma: regenerar la contraseña o retirar a un usuario de una organización por
  aquí es un 404, igual que uno que no existe. Monitor no es una puerta para administrar usuarios ajenos.
- No se retira al último administrador de plataforma activo ni al administrador de una organización (`usuarios` lo niega).
- La contraseña temporal se devuelve SIEMPRE a quien la pidió (como la imprimía el script): es el respaldo si el correo no
  salió, y quien opera Monitor es quien debe entregarla.
"""

import logging

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from routers.errores import almacen_no_disponible
from servicios import auditoria, auth, invitaciones, tenants_registro, tokens, usuarios
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

router = APIRouter()

#: Quién queda como autor en la bitácora: el operador, desde Monitor (no hay una persona con sesión).
ACTOR = "monitor"

_NO_EXISTE = "Ese administrador de plataforma no existe."


class NuevoAdmin(BaseModel):
    email: str = Field(..., max_length=200)
    nombre: str = Field(..., max_length=100)
    apellidos: str = Field(..., max_length=100)
    enviarCorreo: bool = Field(True, description="Mandar la bienvenida con los accesos. `false` equivale a `--sin-correo`.")


def _estado(u: dict) -> str:
    """`inactivo` (se desactivó), `pendiente` (todavía no ha hecho su primer acceso) o `activo`."""
    if not u["activo"]:
        return "inactivo"
    return "pendiente" if u["debeCambiarContrasena"] else "activo"


def _resumen(u: dict) -> dict:
    return {
        "guid": u["guid"],
        "email": u["email"],
        "nombre": u["nombre"],
        "apellidos": u["apellidos"],
        "estado": _estado(u),
        "creadoEn": u["creadoEn"],
        "ultimoLoginEn": u["ultimoLoginEn"],
        "temporalEmitidaEn": u["temporalEmitidaEn"] if u["debeCambiarContrasena"] else None,
        "sesionActiva": usuarios.tiene_sesion(u["guid"]),
    }


def _creados_por_actor() -> dict[str, list[dict]]:
    """Por cada quien dio altas, las cuentas que creó, en el orden de su alta.

    Sale de la bitácora (`USER_CREATED` lleva `actor`, el guid de quien la dio de alta), porque la cuenta misma no guarda
    quién la creó. Se lee el registro UNA vez para todos los administradores. Las cuentas que ya se retiraron siguen
    saliendo, con estado `retirado` y el correo que dejó su `USER_DELETED`: haber creado una cuenta no se borra al retirarla."""
    altas: list[dict] = []
    retiros: dict[str, dict] = {}
    for e in auditoria.eventos({auditoria.USER_CREATED, auditoria.USER_DELETED}):
        if e.get("accion") == auditoria.USER_CREATED and e.get("actor") and e.get("usuarioGuid"):
            altas.append(e)
        elif e.get("accion") == auditoria.USER_DELETED and e.get("usuarioGuid"):
            retiros[e["usuarioGuid"]] = e

    salida: dict[str, list[dict]] = {}
    for e in altas:
        guid = e["usuarioGuid"]
        organizacion = None
        if e.get("tenantGuid"):
            try:
                t = tenants_registro.por_guid(e["tenantGuid"])
                organizacion = {"guid": t["guid"], "nombre": t["nombre"]}
            except tenants_registro.NoEncontrado:
                organizacion = {"guid": e["tenantGuid"], "nombre": None}
        try:
            u = usuarios.por_guid(guid)
            cuenta = {"email": u["email"], "nombre": u["nombre"], "apellidos": u["apellidos"], "estado": _estado(u)}
        except usuarios.NoEncontrado:
            r = retiros.get(guid, {})
            cuenta = {"email": r.get("email") or r.get("correo"), "nombre": None, "apellidos": None, "estado": "retirado"}
        salida.setdefault(e["actor"], []).append(
            {"guid": guid, **cuenta, "rol": e.get("rol"), "organizacion": organizacion, "creadoEn": e.get("en")}
        )
    return salida


def _admin_o_404(guid: str) -> dict:
    """El administrador de plataforma `guid`. Cualquier otra cuenta —de una organización, o que no existe— es un 404."""
    try:
        u = usuarios.por_guid(guid)
    except usuarios.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    if not u["esAdminPlataforma"]:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE)
    return u


def _anotar(accion: str, usuario_guid: str, **datos) -> None:
    """El rastro nunca cambia la respuesta: si la bitácora falla, se avisa en el log y la operación sigue."""
    try:
        auditoria.registrar(accion, usuario_guid=usuario_guid, actor=ACTOR, **datos)
    except (ErrorAlmacen, OSError) as exc:
        logger.warning("No se pudo anotar %s en la bitácora de auditoría: %s", accion, exc)


@router.get(
    "/",
    tags=["Administradores de plataforma"],
    summary="Listar los administradores de plataforma",
    description=(
        "Los que existen, en el orden de su alta, con su estado (`pendiente`, `activo` o `inactivo`). Los retirados ya no salen. "
        "Cada uno trae `creados`: las cuentas que dio de alta (según la bitácora), con su rol, su organización y su estado."
    ),
)
def listar():
    try:
        admins = usuarios.administradores_de_plataforma()
        creados = _creados_por_actor()
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    return {
        "total": len(admins),
        "administradores": [{**_resumen(u), "creados": creados.get(u["guid"], [])} for u in admins],
    }


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    tags=["Administradores de plataforma"],
    summary="Crear un administrador de plataforma",
    description=(
        "Lo da de alta con una contraseña temporal (que cumple las reglas y se le pedirá cambiar al entrar) y le manda la bienvenida "
        "si este ambiente manda correo de verdad. Devuelve la contraseña temporal SIEMPRE: es el respaldo si el correo no salió. "
        "Un correo ya registrado es un 409 y no crea nada."
    ),
)
def crear(entrada: NuevoAdmin):
    temporal = auth.contrasena_temporal()
    try:
        u = usuarios.bootstrap_admin(entrada.email, entrada.nombre, entrada.apellidos, auth.hash_de(temporal))
    except usuarios.YaExiste as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"codigo": "ya_existe", "mensaje": str(exc)}) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc

    correo = invitaciones.enviar_bienvenida(u["email"], u["nombre"], temporal, "super_admin") if entrada.enviarCorreo else "omitido"
    invitaciones.auditar_alta(ACTOR, u["guid"], "ADMIN_PLATAFORMA", None, correo)
    return {"administrador": _resumen(u), "correo": correo, "contrasenaTemporal": temporal}


@router.post(
    "/{guid}/reenviar-invitacion",
    tags=["Administradores de plataforma"],
    summary="Regenerar la contraseña temporal de quien no ha hecho su primer acceso",
    description=(
        "La anterior deja de servir. Solo vale para quien sigue en su primer acceso: quien ya entró tiene «Olvidé mi contraseña» "
        "(409 `ya_hizo_su_primer_acceso`). Devuelve la contraseña nueva SIEMPRE."
    ),
)
def reenviar_invitacion(guid: str):
    _admin_o_404(guid)
    try:
        correo, temporal = invitaciones.reenviar(guid, actor_guid=ACTOR)
    except usuarios.NoPendiente as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"codigo": "ya_hizo_su_primer_acceso", "mensaje": "Esa cuenta ya hizo su primer acceso: para recuperarla, que use «Olvidé mi contraseña»."},
        ) from exc
    except usuarios.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    return {"administrador": _resumen(usuarios.por_guid(guid)), "correo": correo, "contrasenaTemporal": temporal}


@router.delete(
    "/{guid}",
    tags=["Administradores de plataforma"],
    summary="Retirar un administrador de plataforma",
    description=(
        "Borrado LÓGICO: se le cierran las sesiones, deja de existir para el login y su correo queda libre para darse de alta otra "
        "vez; el rastro se queda en la bitácora. El `motivo` es obligatorio. No se retira al último administrador de plataforma "
        "activo (409 `ultimo_superadmin`)."
    ),
)
def retirar(guid: str, motivo: str = Query(..., min_length=1, max_length=500, description="Por qué se retira; queda en la bitácora")):
    cuenta = _admin_o_404(guid)
    motivo = motivo.strip()
    if not motivo:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="El motivo es obligatorio.")
    try:
        usuarios.eliminar_usuario(guid, motivo, por=ACTOR)
    except usuarios.NoSePuedeEliminar as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"codigo": exc.motivo, "mensaje": f"No se puede retirar: {exc}."}) from exc
    except usuarios.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    tokens.invalidar_de(guid, motivo="CUENTA_RETIRADA")
    _anotar(auditoria.USER_DELETED, guid, email=cuenta["email"], motivo=motivo, esAdminPlataforma=True, membresias=len(cuenta["membresias"]))
    return {"retirado": True, "email": cuenta["email"]}
