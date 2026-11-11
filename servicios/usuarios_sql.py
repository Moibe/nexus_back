"""Usuarios, credenciales, intentos de inicio de sesión y sesiones, guardados en SQL Server (migración `0014`).

Hacen lo mismo que el JSONL de `usuarios.py` —mismas reglas, misma forma de salida—, y es ese módulo el que elige: SQL si el `.env` trae
`SQLSERVER_HOST`, el almacén si no (las pruebas sin base). Antes de leer o escribir se asegura que `usuarios.jsonl` ya se haya importado una
vez en esta base (`servicios/importacion_json.py`).

La validación de negocio se queda en `usuarios.py` (que el correo sea válido, el motivo de una contraseña, cómo se separa un nombre, por qué
no se puede retirar a alguien, la política de intentos); aquí se guarda y se lee. Lo que sí vive aquí son las garantías que el candado del
archivo daba a UN proceso y que ahora da la base a todos (los tres ambientes, varios workers):

- Lo que lee y luego escribe la cuenta de una persona (abrir una sesión, renovarla, cambiar la contraseña, desactivar, cerrar sus sesiones,
  anotar un intento…) toma un candado de aplicación con su guid (`nexus-usuario-<guid>`) dentro de su transacción. Esas operaciones se hacen
  en fila: una renovación y un «cerrar todas las sesiones» no pueden cruzarse y dejar viva a la sucesora, y un login cuya contraseña se
  verificó fuera del candado no abre sesión si mientras tanto se restableció.
- El contador de un navegador lleva su propio candado (`nexus-origen-<id>`), siempre DESPUÉS del de la persona: así dos operaciones nunca
  se esperan en círculo.
- Retirar una cuenta toma además un candado general (`nexus-usuarios-retiro`), ANTES del de la persona: dos retiros a la vez no pueden dejar
  a la plataforma sin administradores.
- El correo único lo cuida la base (`UX_users_email`) y un candado por correo: dos altas con el mismo correo, una gana.

Cualquier fallo de la base se levanta como `ErrorAlmacen`, el 503 que los routers ya contestan. Las fechas se guardan en UTC sin zona y a
segundos, como las escribía el JSONL.
"""

import json
from datetime import datetime

import pyodbc

from db import tablas
from servicios import importacion_json, usuarios
from servicios.integraciones_sql import _candado, _como_almacen, _iso, _naive

_COLUMNAS = (
    "u.guid, u.email, u.firstName, u.lastName, u.secondLastName, u.phone, u.recoveryJson, u.isPlatformAdmin, u.passwordHash, "
    "u.mustChangePassword, u.passwordChangedAt, u.temporaryIssuedAt, u.failedAttempts, u.lockedUntil, u.lastLoginAt, u.isActive, u.createdAt"
)
_COLUMNAS_SESION = "guid, userGuid, refreshHash, expiresAt, createdAt, ip, userAgent, revokedAt, revocationReason"


def _a_usuario(f: dict, membresias: list[dict]) -> dict:
    """La cuenta con la MISMA forma que el índice del JSONL (`usuarios._construir_indice`): así `usuarios._publico` y las reglas de
    `usuarios.py` sirven igual para los dos almacenamientos."""
    paterno, materno = f["lastName"], f["secondLastName"]
    try:
        recuperacion = json.loads(f["recoveryJson"] or "{}")
    except ValueError:
        recuperacion = {}
    return {
        "guid": f["guid"],
        "email": f["email"],
        "nombre": f["firstName"],
        "apellidos": " ".join(x for x in (paterno, materno) if x),
        "apellidoPaterno": paterno,
        "apellidoMaterno": materno,
        "recuperacion": recuperacion if isinstance(recuperacion, dict) else {},
        "esAdminPlataforma": bool(f["isPlatformAdmin"]),
        "telefono": f["phone"],
        "hash": f["passwordHash"],
        "debeCambiar": bool(f["mustChangePassword"]),
        "contrasenaCambiadaEn": _iso(f["passwordChangedAt"]),
        "fallidos": f["failedAttempts"],
        "bloqueadoHasta": _iso(f["lockedUntil"]),
        "ultimoLoginEn": _iso(f["lastLoginAt"]),
        "activo": bool(f["isActive"]),
        "creadoEn": _iso(f["createdAt"]),
        "temporalEmitidaEn": _iso(f["temporaryIssuedAt"]),
        "membresias": membresias,
    }


def _a_sesion(f: dict) -> dict:
    """La sesión con la forma del índice del JSONL."""
    return {
        "sesionGuid": f["guid"],
        "guid": f["userGuid"],
        "refreshHash": f["refreshHash"],
        "expira": _iso(f["expiresAt"]),
        "creadaEn": _iso(f["createdAt"]),
        "ip": f["ip"],
        "userAgent": f["userAgent"],
        "revocadaEn": _iso(f["revokedAt"]),
        "revocadaPor": f["revocationReason"],
    }


def _leer(cur, filtro: str, *parametros) -> list[dict]:
    """Las cuentas que existen (no retiradas) que cumplen `filtro` (SQL sobre el alias `u`), en el orden de su alta, cada una con sus
    membresías en su orden."""
    cur.execute(
        f"SELECT {_COLUMNAS} FROM nexus.users u WHERE u.deletedAt IS NULL AND ({filtro}) ORDER BY u.createdAt, u.userId", *parametros
    )
    filas = tablas.filas(cur)
    if not filas:
        return []
    cur.execute(
        "SELECT m.userGuid, m.tenant, m.role FROM nexus.userMemberships m JOIN nexus.users u ON u.guid = m.userGuid "
        f"WHERE u.deletedAt IS NULL AND ({filtro}) ORDER BY m.membershipId",
        *parametros,
    )
    membresias: dict[str, list[dict]] = {}
    for m in tablas.filas(cur):
        membresias.setdefault(m["userGuid"], []).append({"tenantGuid": m["tenant"], "rol": m["role"]})
    return [_a_usuario(f, membresias.get(f["guid"], [])) for f in filas]


def _uno(cur, guid: str) -> dict | None:
    lista = _leer(cur, "u.guid = ?", guid)
    return lista[0] if lista else None


def _existe(cur, guid: str) -> bool:
    cur.execute("SELECT 1 FROM nexus.users WHERE guid = ? AND deletedAt IS NULL", guid)
    return cur.fetchone() is not None


def _candado_de(cur, guid: str) -> None:
    _candado(cur, f"nexus-usuario-{guid}")


def _cerrar_todas(cur, guid: str, motivo: str, en: datetime, salvo: str | None = None) -> int:
    """Revoca las sesiones abiertas de esa persona (vencidas o no, como el JSONL), salvo `salvo`. Devuelve cuántas."""
    sql = "UPDATE nexus.userSessions SET revokedAt = ?, revocationReason = ? WHERE userGuid = ? AND revokedAt IS NULL"
    parametros = [en, motivo[:40], guid]
    if salvo is not None:
        sql += " AND guid <> ?"
        parametros.append(salvo)
    cur.execute(sql, *parametros)
    return cur.rowcount


def _anotar(cur, guid: str, tipo: str, motivo: str | None, por: str | None, en: datetime) -> None:
    """Lo que un administrador le hizo a la cuenta, con su motivo (`nexus.userAdminEvents`)."""
    cur.execute(
        "INSERT INTO nexus.userAdminEvents (userGuid, kind, reason, byWhom, occurredAt) VALUES (?, ?, ?, ?, ?)",
        guid, tipo, (motivo or None) and str(motivo)[:1000], (por or None) and str(por)[:63], en,
    )


# ── Lecturas ────────────────────────────────────────────────────────────────────────────────────────────────────────────


@_como_almacen
def por_guid(guid: str) -> dict | None:
    importacion_json.asegurar("usuarios")
    with tablas.lectura() as cur:
        return _uno(cur, guid)


@_como_almacen
def por_correo(email: str) -> dict | None:
    importacion_json.asegurar("usuarios")
    with tablas.lectura() as cur:
        lista = _leer(cur, "u.email = ?", email)
    return lista[0] if lista else None


@_como_almacen
def de_tenant(tenant: str) -> list[dict]:
    importacion_json.asegurar("usuarios")
    with tablas.lectura() as cur:
        return _leer(cur, "EXISTS (SELECT 1 FROM nexus.userMemberships x WHERE x.userGuid = u.guid AND x.tenant = ?)", tenant)


@_como_almacen
def administradores() -> list[dict]:
    importacion_json.asegurar("usuarios")
    with tablas.lectura() as cur:
        return _leer(cur, "u.isPlatformAdmin = 1")


@_como_almacen
def tiene_sesion(guid: str, ahora: datetime) -> bool:
    importacion_json.asegurar("usuarios")
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT TOP 1 1 FROM nexus.userSessions WHERE userGuid = ? AND revokedAt IS NULL AND expiresAt > ?", guid, _naive(ahora)
        )
        return cur.fetchone() is not None


@_como_almacen
def por_guid_y_sesion(guid: str, sesion_guid: str) -> tuple[dict | None, bool]:
    """`(cuenta o None, ¿la sesión está viva?)` en UNA conexión. Viva = existe, es de ESA persona y no está cerrada (sea cual sea el
    motivo); su vencimiento no cuenta aquí, igual que en el JSONL."""
    importacion_json.asegurar("usuarios")
    with tablas.lectura() as cur:
        u = _uno(cur, guid)
        if u is None:
            return None, False
        cur.execute("SELECT userGuid, revokedAt FROM nexus.userSessions WHERE guid = ?", sesion_guid)
        s = tablas.una(cur)
    return u, s is not None and s["userGuid"] == guid and s["revokedAt"] is None


@_como_almacen
def sesion_por_hash(refresh_hash: str) -> tuple[dict, dict | None] | None:
    """`(sesión, su cuenta o None si ya no existe)`, o `None` si ese refresh no es de ninguna sesión."""
    importacion_json.asegurar("usuarios")
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS_SESION} FROM nexus.userSessions WHERE refreshHash = ?", refresh_hash)
        s = tablas.una(cur)
        if s is None:
            return None
        return _a_sesion(s), _uno(cur, s["userGuid"])


@_como_almacen
def estado_origen(origen: str) -> tuple[int, datetime | None]:
    """`(errores seguidos, bloqueado hasta)` de ese navegador, tal como quedaron (el bloqueo puede estar ya vencido: lo decide quien llama)."""
    importacion_json.asegurar("usuarios")
    with tablas.lectura() as cur:
        cur.execute("SELECT failedAttempts, lockedUntil FROM nexus.loginOrigins WHERE originId = ?", origen)
        f = tablas.una(cur)
    return (f["failedAttempts"], f["lockedUntil"]) if f else (0, None)


@_como_almacen
def para_retiro(guid: str) -> dict[str, dict]:
    """La cuenta y TODOS los administradores de plataforma que existen: lo que `usuarios._negativa_de_eliminar` necesita para decidir."""
    importacion_json.asegurar("usuarios")
    with tablas.lectura() as cur:
        return {u["guid"]: u for u in _leer(cur, "u.guid = ? OR u.isPlatformAdmin = 1", guid)}


# ── Altas y datos ───────────────────────────────────────────────────────────────────────────────────────────────────────


@_como_almacen
def crear(
    guid: str,
    email: str,
    nombres: str,
    paterno: str,
    materno: str,
    telefono: str | None,
    recuperacion: dict,
    es_admin_plataforma: bool,
    hash_contrasena: str,
    en: datetime,
    tenant: str | None = None,
    rol: str | None = None,
) -> bool:
    """Da de alta la cuenta (y su membresía, si trae organización) en UNA transacción. `False` si el correo ya lo tiene otra cuenta."""
    importacion_json.asegurar("usuarios")
    with tablas.transaccion() as cur:
        _candado(cur, f"nexus-correo-{email}")
        cur.execute("SELECT 1 FROM nexus.users WHERE email = ? AND deletedAt IS NULL", email)
        if cur.fetchone() is not None:
            return False
        try:
            cur.execute(
                "INSERT INTO nexus.users (guid, email, firstName, lastName, secondLastName, phone, recoveryJson, isPlatformAdmin, passwordHash, "
                "mustChangePassword, temporaryIssuedAt, createdAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)",
                guid, email, nombres, paterno, materno, telefono, json.dumps(recuperacion or {}, ensure_ascii=False),
                es_admin_plataforma, hash_contrasena, _naive(en), _naive(en),
            )
        except pyodbc.IntegrityError as exc:
            # Lo que el candado no cubre (otra forma de escribir el mismo correo que la base considera igual): lo decide el índice único.
            if "UX_users_email" in str(exc):
                return False
            raise
        if tenant:
            cur.execute(
                "INSERT INTO nexus.userMemberships (userGuid, tenant, role, assignedAt) VALUES (?, ?, ?, ?)", guid, tenant, rol, _naive(en)
            )
    return True


@_como_almacen
def agregar_membresia(guid: str, tenant: str, rol: str, en: datetime) -> bool:
    """Le da (o le cambia) el rol en esa organización. Como en el JSONL, la membresía se va al final de la lista. `False` si no existe."""
    importacion_json.asegurar("usuarios")
    with tablas.transaccion() as cur:
        _candado_de(cur, guid)
        if not _existe(cur, guid):
            return False
        cur.execute("DELETE FROM nexus.userMemberships WHERE userGuid = ? AND tenant = ?", guid, tenant)
        cur.execute("INSERT INTO nexus.userMemberships (userGuid, tenant, role, assignedAt) VALUES (?, ?, ?, ?)", guid, tenant, rol, _naive(en))
    return True


@_como_almacen
def actualizar_datos(guid: str, decidir, telefono: str | None) -> bool:
    """Nombre y teléfono que cambia quien administra. `decidir(cuenta)` devuelve `(nombres, paterno, materno)` a partir de cómo está la
    cuenta AHORA (bajo el candado). `False` si no existe."""
    importacion_json.asegurar("usuarios")
    with tablas.transaccion() as cur:
        _candado_de(cur, guid)
        actual = _uno(cur, guid)
        if actual is None:
            return False
        nombres, paterno, materno = decidir(actual)
        cur.execute(
            "UPDATE nexus.users SET firstName = ?, lastName = ?, secondLastName = ?, phone = ? WHERE guid = ?",
            nombres, paterno, materno, telefono, guid,
        )
    return True


@_como_almacen
def actualizar_perfil(guid: str, nombre: str, paterno: str, materno: str, telefono: str | None, recuperacion: dict) -> bool:
    """Lo que la persona cambia de sí misma (HU12). `False` si no existe."""
    importacion_json.asegurar("usuarios")
    with tablas.transaccion() as cur:
        _candado_de(cur, guid)
        cur.execute(
            "UPDATE nexus.users SET firstName = ?, lastName = ?, secondLastName = ?, phone = ?, recoveryJson = ? WHERE guid = ? AND deletedAt IS NULL",
            nombre, paterno, materno, telefono, json.dumps(recuperacion or {}, ensure_ascii=False), guid,
        )
        return cur.rowcount > 0


# ── Intentos de inicio de sesión ────────────────────────────────────────────────────────────────────────────────────────


@_como_almacen
def registrar_intento(
    guid: str | None,
    email: str,
    exitoso: bool,
    motivo: str | None,
    ip: str | None,
    user_agent: str | None,
    origen: str | None,
    en: datetime,
) -> tuple[int, datetime | None]:
    """Anota el intento y aplica, en la MISMA transacción, la política de la cuenta y la del navegador (`usuarios.plegar_intento_*`).
    Devuelve `(fallidos, bloqueado hasta)` de la cuenta tal como quedaron; `(0, None)` si no hay cuenta."""
    importacion_json.asegurar("usuarios")
    en = _naive(en)
    with tablas.transaccion() as cur:
        cuenta = None
        if guid:
            _candado_de(cur, guid)
            cur.execute("SELECT failedAttempts, lockedUntil FROM nexus.users WHERE guid = ? AND deletedAt IS NULL", guid)
            cuenta = tablas.una(cur)
        if origen:
            _candado(cur, f"nexus-origen-{origen}")
        cur.execute(
            "INSERT INTO nexus.loginAttempts (userGuid, email, succeeded, reason, ip, userAgent, originId, occurredAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            guid if cuenta is not None else None, email[:320], exitoso, None if exitoso else (motivo or None) and motivo[:20],
            (ip or None) and ip[:45], user_agent, origen, en,
        )
        resultado: tuple[int, datetime | None] = (0, None)
        if cuenta is not None:
            fallidos, hasta = usuarios.plegar_intento_cuenta(cuenta["failedAttempts"], cuenta["lockedUntil"], exitoso, motivo, en)
            cur.execute(
                "UPDATE nexus.users SET failedAttempts = ?, lockedUntil = ?, lastLoginAt = CASE WHEN ? = 1 THEN ? ELSE lastLoginAt END WHERE guid = ?",
                fallidos, hasta, exitoso, en, guid,
            )
            resultado = (fallidos, hasta)
        if origen:
            cur.execute("SELECT failedAttempts, lockedUntil FROM nexus.loginOrigins WHERE originId = ?", origen)
            antes = tablas.una(cur)
            fallidos_o, hasta_o = usuarios.plegar_intento_origen(
                antes["failedAttempts"] if antes else 0, antes["lockedUntil"] if antes else None, exitoso, motivo, en
            )
            if antes is not None:
                cur.execute(
                    "UPDATE nexus.loginOrigins SET failedAttempts = ?, lockedUntil = ?, updatedAt = ? WHERE originId = ?", fallidos_o, hasta_o, en, origen
                )
            elif fallidos_o or hasta_o:
                cur.execute(
                    "INSERT INTO nexus.loginOrigins (originId, failedAttempts, lockedUntil, updatedAt) VALUES (?, ?, ?, ?)", origen, fallidos_o, hasta_o, en
                )
    return resultado


@_como_almacen
def reiniciar_origen(origen: str, en: datetime) -> None:
    """Contador y bloqueo de ese navegador en cero (restablecer la contraseña desde él)."""
    importacion_json.asegurar("usuarios")
    with tablas.transaccion() as cur:
        _candado(cur, f"nexus-origen-{origen}")
        cur.execute("UPDATE nexus.loginOrigins SET failedAttempts = 0, lockedUntil = NULL, updatedAt = ? WHERE originId = ?", _naive(en), origen)


# ── Contraseñas ─────────────────────────────────────────────────────────────────────────────────────────────────────────


@_como_almacen
def fijar_contrasena(
    guid: str, hash_contrasena: str, cierra_todas: bool, sesion_guid: str | None, hash_verificado: str | None, en: datetime
) -> int:
    """La contraseña nueva y las sesiones que eso cierra, en UNA transacción; devuelve cuántas cerró. Bajo el candado de la persona comprueba lo
    que `usuarios.fijar_contrasena_y_cerrar` documenta: que la sesión que lo pide siga viva y que la contraseña que se verificó siga siendo la
    vigente (si no, `SesionNoCanjeable` y no escribe nada)."""
    importacion_json.asegurar("usuarios")
    en = _naive(en)
    with tablas.transaccion() as cur:
        _candado_de(cur, guid)
        cur.execute("SELECT passwordHash FROM nexus.users WHERE guid = ? AND deletedAt IS NULL", guid)
        cuenta = tablas.una(cur)
        if cuenta is None:
            raise usuarios.NoEncontrado(guid)
        if sesion_guid is not None:
            cur.execute("SELECT userGuid, revokedAt FROM nexus.userSessions WHERE guid = ?", sesion_guid)
            s = tablas.una(cur)
            if s is None or s["userGuid"] != guid or s["revokedAt"] is not None:
                raise usuarios.SesionNoCanjeable("cerrada")
        if hash_verificado is not None and cuenta["passwordHash"] != hash_verificado:
            raise usuarios.SesionNoCanjeable("contrasena")
        cerradas = _cerrar_todas(cur, guid, "PASSWORD_CHANGED", en, salvo=None if cierra_todas else sesion_guid)
        cur.execute(
            "UPDATE nexus.users SET passwordHash = ?, mustChangePassword = 0, passwordChangedAt = ?, failedAttempts = 0, lockedUntil = NULL WHERE guid = ?",
            hash_contrasena, en, guid,
        )
    return cerradas


@_como_almacen
def emitir_temporal(guid: str, hash_contrasena: str, en: datetime) -> None:
    """Contraseña temporal nueva para quien sigue en su primer acceso; cierra las sesiones abiertas con la anterior."""
    importacion_json.asegurar("usuarios")
    en = _naive(en)
    with tablas.transaccion() as cur:
        _candado_de(cur, guid)
        cur.execute("SELECT mustChangePassword FROM nexus.users WHERE guid = ? AND deletedAt IS NULL", guid)
        cuenta = tablas.una(cur)
        if cuenta is None:
            raise usuarios.NoEncontrado(guid)
        if not cuenta["mustChangePassword"]:
            raise usuarios.NoPendiente(guid)
        cur.execute(
            "UPDATE nexus.users SET passwordHash = ?, mustChangePassword = 1, temporaryIssuedAt = ?, failedAttempts = 0, lockedUntil = NULL WHERE guid = ?",
            hash_contrasena, en, guid,
        )
        _cerrar_todas(cur, guid, "REINVITED", en)


# ── Sesiones ────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _insertar_sesion(cur, sesion_guid: str, guid: str, refresh_hash: str, expira: datetime, ip, user_agent, en: datetime) -> None:
    cur.execute(
        "INSERT INTO nexus.userSessions (guid, userGuid, refreshHash, expiresAt, createdAt, ip, userAgent) VALUES (?, ?, ?, ?, ?, ?, ?)",
        sesion_guid, guid, refresh_hash, _naive(expira), en, (ip or None) and ip[:45], user_agent,
    )


@_como_almacen
def crear_sesion(
    guid: str, sesion_guid: str, refresh_hash: str, expira: datetime, ip, user_agent, hash_verificado: str | None, en: datetime
) -> None:
    """Abre la sesión. Con `hash_verificado` (el login) comprueba bajo el candado que la cuenta siga activa y con esa contraseña."""
    importacion_json.asegurar("usuarios")
    en = _naive(en)
    with tablas.transaccion() as cur:
        _candado_de(cur, guid)
        cur.execute("SELECT passwordHash, isActive FROM nexus.users WHERE guid = ? AND deletedAt IS NULL", guid)
        cuenta = tablas.una(cur)
        if cuenta is None:
            raise usuarios.NoEncontrado(guid)
        if hash_verificado is not None:
            if not cuenta["isActive"]:
                raise usuarios.SesionNoCanjeable("desactivada")
            if cuenta["passwordHash"] != hash_verificado:
                raise usuarios.SesionNoCanjeable("invalida")
        _insertar_sesion(cur, sesion_guid, guid, refresh_hash, expira, ip, user_agent, en)


@_como_almacen
def rotar_sesion(
    refresh_hash: str, nuevo_refresh_hash: str, sesion_guid: str, expira: datetime, ip, user_agent, ahora: datetime
) -> dict:
    """Canjea el refresh por una sesión nueva en UNA transacción, bajo el candado de la persona: comprobar, revocar (`ROTATED`) y crear la
    sucesora. Devuelve la cuenta (como estaba); levanta `SesionNoCanjeable` (`invalida` o `vencida`) sin escribir nada."""
    importacion_json.asegurar("usuarios")
    en = _naive(ahora)
    with tablas.transaccion() as cur:
        cur.execute("SELECT userGuid FROM nexus.userSessions WHERE refreshHash = ?", refresh_hash)
        dueno = tablas.una(cur)
        if dueno is None:
            raise usuarios.SesionNoCanjeable("invalida")
        _candado_de(cur, dueno["userGuid"])
        cur.execute("SELECT sessionId, userGuid, expiresAt, revokedAt FROM nexus.userSessions WITH (UPDLOCK) WHERE refreshHash = ?", refresh_hash)
        s = tablas.una(cur)
        u = _uno(cur, s["userGuid"])
        if s["revokedAt"] is not None or u is None or not u["activo"]:
            raise usuarios.SesionNoCanjeable("invalida")
        if s["expiresAt"] <= en:
            raise usuarios.SesionNoCanjeable("vencida")
        cur.execute("UPDATE nexus.userSessions SET revokedAt = ?, revocationReason = 'ROTATED' WHERE sessionId = ?", en, s["sessionId"])
        _insertar_sesion(cur, sesion_guid, s["userGuid"], nuevo_refresh_hash, expira, ip, user_agent, en)
    return u


@_como_almacen
def revocar_sesiones(guid: str, sesion_guid: str | None, motivo: str, en: datetime) -> int:
    """Una sesión de esa persona o, sin `sesion_guid`, todas las que tenga abiertas. Devuelve cuántas cerró."""
    importacion_json.asegurar("usuarios")
    with tablas.transaccion() as cur:
        _candado_de(cur, guid)
        if sesion_guid is None:
            return _cerrar_todas(cur, guid, motivo, _naive(en))
        cur.execute(
            "UPDATE nexus.userSessions SET revokedAt = ?, revocationReason = ? WHERE userGuid = ? AND guid = ? AND revokedAt IS NULL",
            _naive(en), motivo[:40], guid, sesion_guid,
        )
        return cur.rowcount


# ── Lo que hace un administrador ────────────────────────────────────────────────────────────────────────────────────────


@_como_almacen
def cambiar_estado(guid: str, activo: bool, motivo: str | None, por: str | None, en: datetime) -> None:
    """Activa o desactiva; al desactivar, primero se cierran sus sesiones (QA, HU08). Queda en la bitácora con su motivo."""
    importacion_json.asegurar("usuarios")
    en = _naive(en)
    with tablas.transaccion() as cur:
        _candado_de(cur, guid)
        if not _existe(cur, guid):
            raise usuarios.NoEncontrado(guid)
        if not activo:
            _cerrar_todas(cur, guid, "DEACTIVATED", en)
        cur.execute("UPDATE nexus.users SET isActive = ? WHERE guid = ?", activo, guid)
        _anotar(cur, guid, "REACTIVATED" if activo else "DEACTIVATED", motivo, por, en)


@_como_almacen
def cerrar_sesiones(guid: str, motivo: str, por: str | None, en: datetime) -> int:
    """Cierre forzado (HU05): primero el rastro y después las sesiones, en la misma transacción. Devuelve cuántas cerró."""
    importacion_json.asegurar("usuarios")
    en = _naive(en)
    with tablas.transaccion() as cur:
        _candado_de(cur, guid)
        if not _existe(cur, guid):
            raise usuarios.NoEncontrado(guid)
        _anotar(cur, guid, "SESSIONS_CLOSED", motivo, por, en)
        return _cerrar_todas(cur, guid, "FORCED_LOGOUT", en)


@_como_almacen
def eliminar(guid: str, motivo: str, por: str | None, forzar: bool, en: datetime) -> dict:
    """Retiro (borrado lógico): decide con `usuarios._negativa_de_eliminar` bajo el candado general, cierra sus sesiones, marca la cuenta y lo
    anota. Devuelve la cuenta como era."""
    importacion_json.asegurar("usuarios")
    en = _naive(en)
    with tablas.transaccion() as cur:
        _candado(cur, "nexus-usuarios-retiro")
        _candado_de(cur, guid)
        cuentas = {u["guid"]: u for u in _leer(cur, "u.guid = ? OR u.isPlatformAdmin = 1", guid)}
        if guid not in cuentas:
            raise usuarios.NoEncontrado(guid)
        negativa = usuarios._negativa_de_eliminar(cuentas, guid, forzar)
        if negativa is not None:
            raise negativa
        _cerrar_todas(cur, guid, "DELETED", en)
        cur.execute("UPDATE nexus.users SET deletedAt = ? WHERE guid = ?", en, guid)
        _anotar(cur, guid, "DELETED", motivo, por, en)
    return cuentas[guid]
