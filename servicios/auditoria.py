"""Bitácora de auditoría — registro PROVISIONAL.

Los criterios de aceptación de varias historias piden "registrar el evento X
en el audit trail" (`PASSWORD_RESET_REQUESTED`, `PASSWORD_RESET_COMPLETED`, y
los de HU01, HU06...). Esa tabla no existe todavía: está en el
diccionario de datos (`audit_event`, sección 6.1, de solo agregar), pero ni
existe en la base (verificado el 2026-10-12 contra el catálogo de IA_Nexus) ni
se le ha pedido a Charlie. Mientras tanto, los eventos caen
aquí, en un registro de solo agregar como los demás, con la misma forma que
tendrá el renglón de la tabla:

    registrar         ↔ security.uspWriteAuditEvent(@action, @userGuid, @dataJson)
    conteo_reciente   ↔ (consulta sobre esa misma tabla, por acción y ventana)

No sustituye al audit trail: es lo que hay para no dejar el criterio sin nada
y para tener los datos listos el día que la tabla exista.

## Dos usos

1. **Rastro**: quién pidió qué y cuándo. Nunca se guarda un token en claro ni
   una contraseña: a lo más el `guid` del token, que no sirve para nada solo.
2. **Freno al abuso**: `conteo_reciente` cuenta eventos en una ventana de
   tiempo. Lo usa la recuperación de contraseña para limitar cuántos correos se
   pueden provocar. Cuenta TODAS las solicitudes, existan o no las cuentas, y
   eso es a propósito: si solo contara las de cuentas reales, el límite sería
   un oráculo que delata cuáles existen.
"""

from datetime import datetime, timedelta, timezone

from servicios import registro

ARCHIVO = "auditoria.jsonl"

PASSWORD_RESET_REQUESTED = "PASSWORD_RESET_REQUESTED"
PASSWORD_RESET_COMPLETED = "PASSWORD_RESET_COMPLETED"
# HU13 CA-4: alguien cambió SU contraseña desde una sesión abierta (con la actual). Lleva la persona, la sesión desde
# la que lo hizo y cuántas OTRAS sesiones se cerraron; nunca la contraseña ni su hash.
PASSWORD_CHANGED = "PASSWORD_CHANGED"
# HU04 CA-5: la persona puso su contraseña definitiva en el primer acceso (la temporal dejó de servir). Lleva la persona, la
# sesión desde la que lo hizo y cuántas sesiones se cerraron; nunca la contraseña ni su hash.
FIRST_LOGIN_PASSWORD_CHANGED = "FIRST_LOGIN_PASSWORD_CHANGED"
# HU12 CA-6: la persona guardó cambios en SU perfil. Lleva la persona, la sesión desde la que lo hizo y los NOMBRES de los
# campos que cambiaron (`campos`); nunca los valores, que son datos personales y ya viven en el registro de usuarios.
PROFILE_UPDATED = "PROFILE_UPDATED"
# HU11 CA-6: alguien con sesión de una organización intentó llegar a algo de OTRA (un tenant que no es el suyo, un archivo
# bajo el prefijo de otra, un webhook o una llave ajenos). Lleva quién (`usuarioGuid`), a qué organizaciones pertenece
# (`tenantsDelActor`), qué pidió (`tenantPedido`, `recurso`, `recursoId`), por qué camino lo detectó (`via`: `tenant`, `id` o
# `ruta`) y la sesión. Lo que se le contesta a quien lo intentó es siempre lo mismo que si no existiera.
CROSS_TENANT_ACCESS = "CROSS_TENANT_ACCESS"
# NEX-472: alguien con sesión de una organización, SIN el rol que pide la ruta, intentó administrar algo (una API Key, un webhook, un
# proceso, la biblioteca de tipos documentales). Lleva quién (`usuarioGuid`, `sesionGuid`), de qué organización (`tenant`), qué
# intentó (`recurso`) y con qué roles cuenta en ella (`roles`). La respuesta es un 403 `sin_permiso`.
ROLE_DENIED = "ROLE_DENIED"
# HU05 (cerrar sesión). Hasta el 2026-10-12 estos dos no tenían nombre propio:
# el cierre voluntario solo dejaba un `sesion_revocada` en `usuarios.jsonl` y el
# forzado un `cierre_forzado`. Los datos siempre se guardaron; faltaba el
# evento con el nombre que pide el criterio, que además es el que se mudará a la
# tabla de auditoría cuando exista.
LOGOUT = "LOGOUT"
FORCED_LOGOUT = "FORCED_LOGOUT"

# HU06 CA-1 y CA-7: el alta de un usuario (también la del administrador de una organización, HU02). Lleva el
# usuario creado, su rol, quién lo dio de alta y qué pasó con su correo de bienvenida; nunca la contraseña.
USER_CREATED = "USER_CREATED"
# HU06 CA-5/CA-6: a una cuenta que sigue en su primer acceso se le generó una contraseña temporal NUEVA (la
# anterior dejó de servir y las 48 horas empiezan otra vez). Quién la pidió y qué pasó con su correo; nunca la contraseña.
INVITATION_REISSUED = "INVITATION_REISSUED"
# Una cuenta se RETIRÓ (borrado lógico: `retirar_cuenta.py`). Lleva el correo, el motivo y quién lo hizo; nunca la contraseña.
USER_DELETED = "USER_DELETED"

# Alguien DESCARTÓ un documento de la bandeja de preparación: sale de la bandeja y el archivo se queda en el almacén. Lleva quién
# (`usuarioGuid`, `sesionGuid`), de qué organización (`tenant`), qué documento (`entradaId`, `nombreOriginal`, `sha256`,
# `canal`) y el motivo que escribió la persona (`detalle`, opcional).
DOCUMENT_DISCARDED = "DOCUMENT_DISCARDED"

# Tipos documentales de una organización (la biblioteca de Configuración). Crear, borrar, cambiar de estado y calibrar dejan
# rastro; guardar a secas no. Llevan quién (`usuarioGuid`, `sesionGuid`), de qué organización (`tenant`), qué tipo (`tipoId`,
# `nombre`) y, según el caso, `estado` / `de` y `a` / `version`.
DOCUMENT_TYPE_CREATED = "DOCUMENT_TYPE_CREATED"
DOCUMENT_TYPE_DELETED = "DOCUMENT_TYPE_DELETED"
DOCUMENT_TYPE_STATE_CHANGED = "DOCUMENT_TYPE_STATE_CHANGED"
DOCUMENT_TYPE_CALIBRATED = "DOCUMENT_TYPE_CALIBRATED"

# Procesos de una organización (el motor de reglas, `servicios/procesos.py`). Crear y borrar dejan rastro; guardar a secas no.
# Llevan quién (`usuarioGuid`, `sesionGuid`), de qué organización (`tenant`) y qué proceso (`procesoId`, `nombre`).
PROCESS_CREATED = "PROCESS_CREATED"
PROCESS_DELETED = "PROCESS_DELETED"

ACCIONES = frozenset({ROLE_DENIED, PROCESS_CREATED, PROCESS_DELETED, DOCUMENT_TYPE_CREATED, DOCUMENT_TYPE_DELETED, DOCUMENT_TYPE_STATE_CHANGED, DOCUMENT_TYPE_CALIBRATED, DOCUMENT_DISCARDED, PASSWORD_RESET_REQUESTED, PASSWORD_RESET_COMPLETED, PASSWORD_CHANGED, FIRST_LOGIN_PASSWORD_CHANGED, PROFILE_UPDATED, CROSS_TENANT_ACCESS, LOGOUT, FORCED_LOGOUT, USER_CREATED, INVITATION_REISSUED, USER_DELETED})


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def registrar(accion: str, usuario_guid: str | None = None, **datos) -> None:
    """Anota un evento. `datos` va tal cual al renglón: quien llama es
    responsable de no meter secretos."""
    if accion not in ACCIONES:
        raise ValueError(f"Acción de auditoría desconocida: {accion}")
    registro.agregar(
        ARCHIVO,
        {
            "evento": "auditoria",
            "accion": accion,
            "usuarioGuid": usuario_guid,
            "en": _ahora().isoformat(timespec="seconds"),
            **datos,
        },
    )


def cuando(evento: dict) -> datetime | None:
    """La hora del evento, o `None` si no se puede leer."""
    try:
        return datetime.fromisoformat(evento.get("en", ""))
    except (TypeError, ValueError):
        return None


def recientes(accion: str, segundos: int = 3600) -> list[tuple[datetime, dict]]:
    """Los eventos de esa acción de los últimos `segundos`, con su hora.

    Se lee el registro UNA vez: quien necesita varios conteos (la recuperación
    de contraseña calcula cinco límites) los saca de esta lista en memoria en
    vez de repasar el archivo cinco veces. La unidad es SEGUNDOS, la misma que
    usan las variables de configuración: mezclar minutos y segundos fue justo
    el error que el plan de esta pieza traía."""
    desde = _ahora() - timedelta(seconds=segundos)
    salida = []
    for e in registro.eventos(ARCHIVO):
        if e.get("accion") != accion:
            continue
        hora = cuando(e)
        if hora is not None and hora >= desde:
            salida.append((hora, e))
    return salida


def conteo_reciente(accion: str, campo: str | None = None, valor: str | None = None, segundos: int = 3600) -> int:
    """Cuántos eventos de esa acción hubo en los últimos `segundos`. Con
    `campo` y `valor` cuenta solo los que lo traen igual (por ejemplo
    `campo="email"`); sin ellos, los de toda la plataforma."""
    return sum(1 for _, e in recientes(accion, segundos) if campo is None or e.get(campo) == valor)
