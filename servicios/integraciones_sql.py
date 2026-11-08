"""Enlaces de un solo uso, API keys de cliente, su uso, webhooks y sus entregas, guardados en SQL Server (migración `0008`).

Hacen lo mismo que el JSONL de `tokens.py`, `llaves_cliente.py`, `uso_llaves.py`, `webhooks_cliente.py` y `entregas_webhooks.py` —mismas
reglas, misma forma de salida—, y son esos módulos los que eligen: SQL si el `.env` trae `SQLSERVER_HOST`, el almacén si no (la laptop y las
pruebas sin base). Antes de leer o escribir cada registro se asegura que su JSONL ya se haya importado una vez en esta base
(`servicios/importacion_json.py`).

Aquí NO hay validación de negocio: eso se queda en los módulos de arriba, que la hacen igual con cualquiera de los dos almacenamientos.
Lo que sí hay son las garantías que el candado del archivo daba a un solo proceso y que ahora da la base a todos: leer y escribir
bajo `UPDLOCK, HOLDLOCK` (un enlace se gasta una vez; dos altas con la misma URL no se cuelan; un aviso se programa una vez), con las
llaves únicas como último recurso.

Cualquier fallo de la base se levanta como `ErrorAlmacen`, que es lo que los routers ya contestan con su 503 genérico: así no hubo que tocar
ninguno, y quien verifica una llave de cliente sigue recibiendo «no se pudo verificar» y no un 500.

Las fechas se guardan en UTC sin zona y a segundos (como las escribía el JSONL), salvo el uso de las llaves, que va a milisegundos.
"""

import functools
import json
import logging
import secrets
from datetime import datetime, timezone

import pyodbc

from db import tablas
from servicios import importacion_json
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)


def _iso(valor) -> str | None:
    return valor.replace(tzinfo=timezone.utc).isoformat(timespec="seconds") if valor else None


def _aware(valor) -> datetime | None:
    return valor.replace(tzinfo=timezone.utc) if valor else None


def _naive(momento: datetime | None, milisegundos: bool = False) -> datetime | None:
    """Un instante con zona (o ya en UTC) como lo guarda la base: UTC sin zona, a segundos (o a milisegundos)."""
    if momento is None:
        return None
    utc = (momento.astimezone(timezone.utc) if momento.tzinfo else momento).replace(tzinfo=None)
    return utc.replace(microsecond=(utc.microsecond // 1000) * 1000) if milisegundos else utc.replace(microsecond=0)


def _fecha_iso(texto) -> datetime:
    """Una fecha ISO de un evento (`...+00:00`) como UTC sin zona, a segundos."""
    return _naive(datetime.fromisoformat(str(texto)))


_INTENTOS_POR_DEADLOCK = 3


def _es_deadlock(exc: pyodbc.Error) -> bool:
    return bool(exc.args) and exc.args[0] == "40001"


def _como_almacen(funcion):
    """Un fallo de la base se levanta como `ErrorAlmacen` (el 503 que los routers ya contestan).

    Si la base escoge a esta transacción como víctima de un deadlock, ya la deshizo entera: no quedó nada a medias, y repetirla es seguro."""

    @functools.wraps(funcion)
    def envuelta(*args, **kwargs):
        for intento in range(1, _INTENTOS_POR_DEADLOCK + 1):
            try:
                return funcion(*args, **kwargs)
            except pyodbc.Error as exc:
                if _es_deadlock(exc) and intento < _INTENTOS_POR_DEADLOCK:
                    logger.warning("Deadlock en %s (intento %s); se repite.", funcion.__name__, intento)
                    continue
                raise ErrorAlmacen(f"Falló la base de datos en {funcion.__name__}: {exc}") from exc

    return envuelta


def _candado(cur, recurso: str) -> None:
    """Un candado de aplicación, exclusivo y hasta que termine la transacción: lo que hacía `registro.candado` para un solo proceso, ahora para
    todos. Se toma ANTES de leer y escribir lo que protege, con el nombre del ámbito (la persona, la organización, la entrada): dos operaciones
    de ámbitos distintos no se esperan, y las del mismo se hacen en fila sin deadlocks (los bloqueos de rango solos no alcanzaban para eso)."""
    cur.execute(
        "DECLARE @r int; EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', @LockOwner = 'Transaction', @LockTimeout = 30000; SELECT @r AS r",
        recurso[:250],
    )
    if tablas.una(cur)["r"] < 0:
        raise ErrorAlmacen(f"No se pudo tomar el candado {recurso[:60]}.")


# ── Enlaces de un solo uso ──────────────────────────────────────────────────────────────────────────────────────────────

_COLUMNAS_TOKEN = "tokenId, guid, tokenHash, userGuid, kind, expiresAt, createdAt, usedAt, invalidatedAt, invalidationReason"


def _a_token(f: dict) -> dict:
    return {
        "guid": f["guid"],
        "hash": f["tokenHash"],
        "usuarioGuid": f["userGuid"],
        "tipo": f["kind"],
        "expira": _iso(f["expiresAt"]),
        "creadoEn": _iso(f["createdAt"]),
        "usadoEn": _iso(f["usedAt"]),
        "invalidadoEn": _iso(f["invalidatedAt"]),
        "motivo": f["invalidationReason"],
    }


@_como_almacen
def tokens_crear(usuario_guid: str, tipo: str, clave: str, motivo_anteriores: str, guid: str, creado: datetime, expira: datetime) -> None:
    """Apaga los pendientes del mismo tipo de esa persona y guarda el nuevo, en UNA transacción: dos emisiones a la vez no dejan dos vigentes."""
    importacion_json.asegurar("tokens")
    ahora = _naive(creado)
    with tablas.transaccion() as cur:
        _candado(cur, f"nexus-tokens-{usuario_guid}")
        cur.execute(
            "UPDATE nexus.accessTokens WITH (UPDLOCK, HOLDLOCK) SET invalidatedAt = ?, invalidationReason = ? "
            "WHERE userGuid = ? AND kind = ? AND usedAt IS NULL AND invalidatedAt IS NULL AND expiresAt > ?",
            ahora, motivo_anteriores, usuario_guid, tipo, ahora,
        )
        cur.execute(
            "INSERT INTO nexus.accessTokens (guid, tokenHash, userGuid, kind, expiresAt, createdAt) VALUES (?, ?, ?, ?, ?, ?)",
            guid, clave, usuario_guid, tipo, _naive(expira), ahora,
        )


@_como_almacen
def tokens_leer(clave: str) -> dict | None:
    importacion_json.asegurar("tokens")
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS_TOKEN} FROM nexus.accessTokens WHERE tokenHash = ?", clave)
        f = tablas.una(cur)
    return _a_token(f) if f else None


@_como_almacen
def tokens_consumir(clave: str, comprobar, ahora: datetime) -> dict:
    """Lo gasta si sirve. `comprobar(token | None)` levanta lo que corresponda (inválido, ya usado, vencido) y, al hacerlo, la transacción se
    deshace sin escribir nada. Con el renglón BLOQUEADO: dos peticiones con el mismo enlace, una gana y la otra lo ve ya usado."""
    importacion_json.asegurar("tokens")
    with tablas.transaccion() as cur:
        cur.execute(f"SELECT {_COLUMNAS_TOKEN} FROM nexus.accessTokens WITH (UPDLOCK, HOLDLOCK) WHERE tokenHash = ?", clave)
        f = tablas.una(cur)
        token = _a_token(f) if f else None
        comprobar(token)
        cur.execute("UPDATE nexus.accessTokens SET usedAt = ? WHERE tokenId = ?", _naive(ahora), f["tokenId"])
    return token


@_como_almacen
def tokens_invalidar_de(usuario_guid: str, tipo: str | None, motivo: str, ahora: datetime) -> int:
    importacion_json.asegurar("tokens")
    sql = (
        "UPDATE nexus.accessTokens WITH (UPDLOCK, HOLDLOCK) SET invalidatedAt = ?, invalidationReason = ? "
        "WHERE userGuid = ? AND usedAt IS NULL AND invalidatedAt IS NULL AND expiresAt > ?"
    )
    parametros = [_naive(ahora), motivo, usuario_guid, _naive(ahora)]
    if tipo is not None:
        sql += " AND kind = ?"
        parametros.append(tipo)
    with tablas.transaccion() as cur:
        _candado(cur, f"nexus-tokens-{usuario_guid}")
        cur.execute(sql, *parametros)
        return cur.rowcount


@_como_almacen
def tokens_pendientes_de(usuario_guid: str, tipo: str | None, ahora: datetime) -> list[dict]:
    importacion_json.asegurar("tokens")
    sql = (
        f"SELECT {_COLUMNAS_TOKEN} FROM nexus.accessTokens "
        "WHERE userGuid = ? AND usedAt IS NULL AND invalidatedAt IS NULL AND expiresAt > ?"
    )
    parametros = [usuario_guid, _naive(ahora)]
    if tipo is not None:
        sql += " AND kind = ?"
        parametros.append(tipo)
    with tablas.lectura() as cur:
        cur.execute(sql + " ORDER BY tokenId", *parametros)
        return [_a_token(f) for f in tablas.filas(cur)]


# ── API keys de cliente ─────────────────────────────────────────────────────────────────────────────────────────────────

_COLUMNAS_LLAVE = "keyId, publicId, tenant, secretHash, name, description, createdAt, expiresAt, revokedAt"


def _a_llave_publica(f: dict) -> dict:
    """La llave como la ve el listado: SIN el hash del secret."""
    return {
        "id": f["publicId"],
        "tenant": f["tenant"],
        "nombre": f["name"],
        "descripcion": f["description"],
        "creadaEn": _iso(f["createdAt"]),
        "expiraEn": _iso(f["expiresAt"]),
        "revocadaEn": _iso(f["revokedAt"]),
    }


@_como_almacen
def llaves_emitir(tenant: str, nombre: str, descripcion: str, secret_hash_de, creada: datetime, expira: datetime, generar) -> tuple[str, str]:
    """Sortea un identificador libre y guarda la llave bajo el MISMO bloqueo: dos emisiones a la vez no sacan el mismo. Devuelve
    `(identificador, secret)`; `generar(ocupado)` es `seguridad_llaves.generar` y `secret_hash_de` su `hash_de`."""
    importacion_json.asegurar("llaves")
    with tablas.transaccion() as cur:
        _candado(cur, "nexus-llaves-emitir")

        def ocupado(identificador: str) -> bool:
            cur.execute("SELECT 1 FROM nexus.apiKeys WITH (UPDLOCK, HOLDLOCK) WHERE publicId = ?", identificador)
            return cur.fetchone() is not None

        identificador, secret = generar(ocupado)
        cur.execute(
            "INSERT INTO nexus.apiKeys (publicId, tenant, secretHash, name, description, createdAt, expiresAt) VALUES (?, ?, ?, ?, ?, ?, ?)",
            identificador, tenant, secret_hash_de(secret), nombre, descripcion, _naive(creada), _naive(expira),
        )
    return identificador, secret


@_como_almacen
def llaves_listar(tenant: str) -> list[dict]:
    """De la más nueva a la más vieja; dos del mismo instante salen en el orden en que se emitieron (igual que el almacén)."""
    importacion_json.asegurar("llaves")
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS_LLAVE} FROM nexus.apiKeys WHERE tenant = ? ORDER BY createdAt DESC, keyId ASC", tenant)
        return [_a_llave_publica(f) for f in tablas.filas(cur)]


@_como_almacen
def llaves_revocar(tenant: str, identificador: str, ahora: datetime) -> str | None:
    """Cuándo quedó revocada, o `None` si no existe o es de otro tenant. Idempotente: revocar otra vez devuelve la fecha de la primera."""
    importacion_json.asegurar("llaves")
    with tablas.transaccion() as cur:
        cur.execute("SELECT keyId, tenant, revokedAt FROM nexus.apiKeys WITH (UPDLOCK, HOLDLOCK) WHERE publicId = ?", identificador)
        f = tablas.una(cur)
        if f is None or f["tenant"].lower() != tenant.lower():
            return None
        if f["revokedAt"] is not None:
            return _iso(f["revokedAt"])
        cur.execute("UPDATE nexus.apiKeys SET revokedAt = ? WHERE keyId = ?", _naive(ahora), f["keyId"])
    return _iso(_naive(ahora))


@_como_almacen
def llaves_existe_en_otro_tenant(tenant: str, identificador: str) -> bool:
    importacion_json.asegurar("llaves")
    with tablas.lectura() as cur:
        cur.execute("SELECT 1 AS hay FROM nexus.apiKeys WHERE publicId = ? AND tenant <> ?", identificador, tenant)
        return tablas.una(cur) is not None


@_como_almacen
def llaves_buscar_por_id(identificador: str) -> dict | None:
    """La fila que necesita `seguridad_llaves.verificar`. Se llama en CADA petición con llave de cliente, así que es una sola lectura por índice."""
    importacion_json.asegurar("llaves")
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS_LLAVE} FROM nexus.apiKeys WHERE publicId = ?", identificador)
        f = tablas.una(cur)
    if f is None:
        return None
    return {
        "id": f["publicId"],
        "tenant": f["tenant"],
        "secret_hash": f["secretHash"],
        "revocada_en": _aware(f["revokedAt"]),
        "expira_en": _aware(f["expiresAt"]),
    }


# ── Uso de las llaves ───────────────────────────────────────────────────────────────────────────────────────────────────


@_como_almacen
def uso_cargar() -> dict[str, list[tuple]]:
    """Todo el uso, por llave, como lo guarda el índice en memoria: `(fecha, status, ms, bytes)`."""
    importacion_json.asegurar("uso-llaves")
    indice: dict[str, list[tuple]] = {}
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT k.publicId, u.occurredAt, u.status, u.durationMs, u.bytes FROM nexus.apiKeyUsage u "
            "JOIN nexus.apiKeys k ON k.keyId = u.keyId ORDER BY u.usageId"
        )
        for llave, cuando, status, ms, bytes_ in cur.fetchall():
            indice.setdefault(llave, []).append((_aware(cuando), int(status), int(ms), int(bytes_)))
    return indice


@_como_almacen
def uso_registrar(llave_id: str, ruta: str, metodo: str, status: int, ms: int, bytes_: int, cuando: datetime) -> None:
    importacion_json.asegurar("uso-llaves")
    with tablas.transaccion() as cur:
        cur.execute(
            "INSERT INTO nexus.apiKeyUsage (keyId, occurredAt, route, method, status, durationMs, bytes) "
            "SELECT keyId, ?, ?, ?, ?, ?, ? FROM nexus.apiKeys WHERE publicId = ?",
            _naive(cuando, milisegundos=True), ruta[:400], metodo[:10], status, ms, bytes_, llave_id,
        )


# ── Webhooks ────────────────────────────────────────────────────────────────────────────────────────────────────────────

_COLUMNAS_WEBHOOK = "webhookId, publicId, tenant, url, encryptedSecret, status, createdAt, validatedAt, validationFailedAt, deletedAt"


def _eventos_de(cur, ids: list[int]) -> dict[int, list[str]]:
    if not ids:
        return {}
    cur.execute(f"SELECT webhookId, eventType FROM nexus.webhookSubscriptions WHERE webhookId IN ({', '.join('?' * len(ids))}) ORDER BY webhookId", *ids)
    por_webhook: dict[int, list[str]] = {}
    for webhook, evento in cur.fetchall():
        por_webhook.setdefault(webhook, []).append(evento)
    return por_webhook


def _a_vista(f: dict, eventos: dict[int, list[str]]) -> tuple[dict, str, str | None, str | None]:
    """`(webhook, estado, validadoEn, fallidaEn)`: lo que `webhooks_cliente._publico` espera."""
    webhook = {
        "id": f["publicId"],
        "tenant": f["tenant"],
        "url": f["url"],
        "eventos": eventos.get(f["webhookId"], []),
        "secret_cifrado": f["encryptedSecret"],
        "creadoEn": _iso(f["createdAt"]),
    }
    return webhook, f["status"], _iso(f["validatedAt"]), _iso(f["validationFailedAt"])


def _vigente_de(cur, tenant: str, identificador: str, bloquear: bool = False) -> dict | None:
    cur.execute(
        f"SELECT {_COLUMNAS_WEBHOOK} FROM nexus.webhooks{' WITH (UPDLOCK, HOLDLOCK)' if bloquear else ''} "
        "WHERE publicId = ? AND tenant = ? AND deletedAt IS NULL",
        identificador, tenant,
    )
    return tablas.una(cur)


def _vista_de(cur, f: dict) -> tuple[dict, str, str | None, str | None]:
    return _a_vista(f, _eventos_de(cur, [f["webhookId"]]))


_HASH_URL = "HASHBYTES('SHA2_256', CAST(? AS nvarchar(2048)))"


@_como_almacen
def wh_existe_en_otro_tenant(tenant: str, identificador: str) -> bool:
    importacion_json.asegurar("webhooks")
    with tablas.lectura() as cur:
        cur.execute("SELECT 1 AS hay FROM nexus.webhooks WHERE publicId = ? AND tenant <> ? AND deletedAt IS NULL", identificador, tenant)
        return tablas.una(cur) is not None


@_como_almacen
def wh_registrar(tenant: str, url: str, eventos: list[str], secret_cifrado: str, creado: datetime) -> str | None:
    """Guarda un webhook ACTIVO y devuelve su id; `None` si la organización ya tiene uno vigente con esa URL."""
    importacion_json.asegurar("webhooks")
    try:
        with tablas.transaccion() as cur:
            _candado(cur, f"nexus-webhooks-{tenant.lower()}")
            cur.execute(
                f"SELECT 1 FROM nexus.webhooks WITH (UPDLOCK, HOLDLOCK) WHERE tenant = ? AND urlHash = {_HASH_URL} AND deletedAt IS NULL", tenant, url
            )
            if cur.fetchone() is not None:
                return None
            while True:
                identificador = "wh_" + secrets.token_hex(6)
                cur.execute("SELECT 1 FROM nexus.webhooks WITH (UPDLOCK, HOLDLOCK) WHERE publicId = ?", identificador)
                if cur.fetchone() is None:
                    break
            cur.execute(
                "INSERT INTO nexus.webhooks (publicId, tenant, url, encryptedSecret, createdAt) OUTPUT INSERTED.webhookId VALUES (?, ?, ?, ?, ?)",
                identificador, tenant, url, secret_cifrado, _naive(creado),
            )
            nuevo = cur.fetchone()[0]
            cur.executemany("INSERT INTO nexus.webhookSubscriptions (webhookId, eventType) VALUES (?, ?)", [(nuevo, e) for e in eventos])
    except pyodbc.IntegrityError as exc:
        if "UX_webhooks_tenantUrl" in str(exc):
            return None  # otra alta con la misma URL ganó la carrera
        raise
    return identificador


@_como_almacen
def wh_listar(tenant: str) -> list[tuple]:
    """Los vigentes del tenant, del más nuevo al más viejo (dos del mismo segundo: el de después primero)."""
    importacion_json.asegurar("webhooks")
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS_WEBHOOK} FROM nexus.webhooks WHERE tenant = ? AND deletedAt IS NULL ORDER BY createdAt DESC, webhookId DESC", tenant)
        filas = tablas.filas(cur)
        eventos = _eventos_de(cur, [f["webhookId"] for f in filas])
    return [_a_vista(f, eventos) for f in filas]


@_como_almacen
def wh_cambiar_estado(tenant: str, identificador: str, estado: str) -> tuple | None:
    importacion_json.asegurar("webhooks")
    with tablas.transaccion() as cur:
        f = _vigente_de(cur, tenant, identificador, bloquear=True)
        if f is None:
            return None
        if f["status"] != estado:
            cur.execute("UPDATE nexus.webhooks SET status = ? WHERE webhookId = ?", estado, f["webhookId"])
            f["status"] = estado
        return _vista_de(cur, f)


@_como_almacen
def wh_eliminar(tenant: str, identificador: str, ahora: datetime) -> bool:
    """`True` si era del tenant (ya eliminado o no: idempotente). Al eliminarlo se BORRA su secreto cifrado."""
    importacion_json.asegurar("webhooks")
    with tablas.transaccion() as cur:
        cur.execute("SELECT webhookId, deletedAt FROM nexus.webhooks WITH (UPDLOCK, HOLDLOCK) WHERE publicId = ? AND tenant = ?", identificador, tenant)
        f = tablas.una(cur)
        if f is None:
            return False
        if f["deletedAt"] is None:
            cur.execute("UPDATE nexus.webhooks SET deletedAt = ?, encryptedSecret = NULL WHERE webhookId = ?", _naive(ahora), f["webhookId"])
        return True


@_como_almacen
def wh_cifrado(identificador: str) -> str | None:
    """El secret CIFRADO de un webhook vigente, o `None`."""
    importacion_json.asegurar("webhooks")
    with tablas.lectura() as cur:
        cur.execute("SELECT encryptedSecret FROM nexus.webhooks WHERE publicId = ? AND deletedAt IS NULL", identificador)
        f = cur.fetchone()
    return f[0] if f else None


@_como_almacen
def wh_para_validar(tenant: str, identificador: str) -> tuple[str, str] | None:
    importacion_json.asegurar("webhooks")
    with tablas.lectura() as cur:
        f = _vigente_de(cur, tenant, identificador)
    return (f["url"], f["encryptedSecret"]) if f else None


def _marcar(columna: str, tenant: str, identificador: str, en: datetime) -> tuple | None:
    importacion_json.asegurar("webhooks")
    with tablas.transaccion() as cur:
        f = _vigente_de(cur, tenant, identificador, bloquear=True)
        if f is None:
            return None
        cur.execute(f"UPDATE nexus.webhooks SET {columna} = ? WHERE webhookId = ?", _naive(en), f["webhookId"])
        f[columna] = _naive(en)
        return _vista_de(cur, f)


@_como_almacen
def wh_marcar_validado(tenant: str, identificador: str, en: datetime) -> tuple | None:
    """Anota que el endpoint respondió. NO borra su última validación fallida (igual que el almacén)."""
    return _marcar("validatedAt", tenant, identificador, en)


@_como_almacen
def wh_marcar_fallida(tenant: str, identificador: str, en: datetime) -> tuple | None:
    return _marcar("validationFailedAt", tenant, identificador, en)


@_como_almacen
def wh_suscritos(tenant: str, tipo: str) -> list[str]:
    """Los vigentes, VALIDADOS y activos del tenant suscritos a `tipo`, en el orden en que se registraron."""
    importacion_json.asegurar("webhooks")
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT w.publicId FROM nexus.webhooks w JOIN nexus.webhookSubscriptions s ON s.webhookId = w.webhookId "
            "WHERE w.tenant = ? AND w.deletedAt IS NULL AND w.validatedAt IS NOT NULL AND w.status = 'activo' AND s.eventType = ? ORDER BY w.webhookId",
            tenant, tipo,
        )
        return [f[0] for f in cur.fetchall()]


@_como_almacen
def wh_para_entregar(identificador: str) -> tuple[str, str] | None:
    """`(url, secret cifrado)` si sigue en condiciones de recibir (vigente, validado y activo)."""
    importacion_json.asegurar("webhooks")
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT url, encryptedSecret FROM nexus.webhooks WHERE publicId = ? AND deletedAt IS NULL AND validatedAt IS NOT NULL AND status = 'activo'",
            identificador,
        )
        f = cur.fetchone()
    return (f[0], f[1]) if f else None


@_como_almacen
def wh_editar(tenant: str, identificador: str, url: str, eventos: list[str]):
    """Nueva URL y eventos. `None` si no existe / es de otro tenant / se eliminó; `"duplicado"` si otra de sus URLs ya es esa; si no, la vista
    nueva. Si la URL cambió queda SIN validar y sin su fallo: el endpoint es otro."""
    importacion_json.asegurar("webhooks")
    try:
        with tablas.transaccion() as cur:
            _candado(cur, f"nexus-webhooks-{tenant.lower()}")
            f = _vigente_de(cur, tenant, identificador, bloquear=True)
            if f is None:
                return None
            cambio_url = url != f["url"]
            cur.execute(
                f"SELECT 1 FROM nexus.webhooks WITH (UPDLOCK, HOLDLOCK) WHERE tenant = ? AND urlHash = {_HASH_URL} AND deletedAt IS NULL AND webhookId <> ?",
                tenant, url, f["webhookId"],
            )
            if cur.fetchone() is not None:
                return "duplicado"
            if cambio_url:
                cur.execute("UPDATE nexus.webhooks SET url = ?, validatedAt = NULL, validationFailedAt = NULL WHERE webhookId = ?", url, f["webhookId"])
                f["url"], f["validatedAt"], f["validationFailedAt"] = url, None, None
            cur.execute("DELETE FROM nexus.webhookSubscriptions WHERE webhookId = ?", f["webhookId"])
            cur.executemany("INSERT INTO nexus.webhookSubscriptions (webhookId, eventType) VALUES (?, ?)", [(f["webhookId"], e) for e in eventos])
            return _vista_de(cur, f)
    except pyodbc.IntegrityError as exc:
        if "UX_webhooks_tenantUrl" in str(exc):
            return "duplicado"
        raise


# ── Entregas de webhooks ────────────────────────────────────────────────────────────────────────────────────────────────


@_como_almacen
def ent_programar(tenant: str, entrada_id: str, tipo: str, ahora: datetime, cuerpo: bytes, destinos: list[tuple[str, str]], finales) -> str | list[tuple[str, str]]:
    """Anota el aviso y programa UNA entrega por destino `(id de mensaje, webhook)`, todo o nada. Devuelve `"duplicado"` si ese mismo aviso ya
    se había recibido, `"final"` si de esa entrada ya se avisó otro resultado final, o las `(mensaje, webhook)` que sí se programaron."""
    importacion_json.asegurar("entregas-webhooks")
    with tablas.transaccion() as cur:
        _candado(cur, f"nexus-avisos-{tenant.lower()}-{entrada_id.lower()}")
        cur.execute("SELECT type FROM nexus.webhookNotices WITH (UPDLOCK, HOLDLOCK) WHERE tenant = ? AND entryId = ?", tenant, entrada_id)
        anteriores = [f[0] for f in cur.fetchall()]
        if tipo in anteriores:
            return "duplicado"
        if any(t in finales for t in anteriores):
            return "final"
        creado = _naive(ahora)
        cur.execute(
            "INSERT INTO nexus.webhookNotices (tenant, entryId, type, occurredAt, destinations) VALUES (?, ?, ?, ?, ?)",
            tenant, entrada_id, tipo, creado, len(destinos),
        )
        creadas = []
        for mensaje, webhook in destinos:
            cur.execute(
                "INSERT INTO nexus.webhookDeliveries (publicId, webhookId, tenant, type, entryId, bodyJson, createdAt) "
                "SELECT ?, webhookId, ?, ?, ?, ?, ? FROM nexus.webhooks WHERE publicId = ? AND deletedAt IS NULL",
                mensaje, tenant, tipo, entrada_id, cuerpo.decode(), creado, webhook,
            )
            if cur.rowcount:
                creadas.append((mensaje, webhook))
        return creadas


@_como_almacen
def ent_pendientes() -> list[dict]:
    """Las entregas que siguen en reintentos, para que el trabajador las reconstruya al arrancar."""
    importacion_json.asegurar("entregas-webhooks")
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT d.publicId, w.publicId AS webhook, d.tenant, d.type, d.bodyJson, d.attempts, d.lastAttemptAt, d.createdAt "
            "FROM nexus.webhookDeliveries d JOIN nexus.webhooks w ON w.webhookId = d.webhookId WHERE d.status = 'pendiente' ORDER BY d.deliveryId"
        )
        return [
            {
                "id": f["publicId"], "webhook": f["webhook"], "tenant": f["tenant"], "tipo": f["type"], "cuerpo": f["bodyJson"],
                "intentos": f["attempts"], "ultimoIntento": _aware(f["lastAttemptAt"]), "creadaEn": _aware(f["createdAt"]),
            }
            for f in tablas.filas(cur)
        ]


@_como_almacen
def ent_anotar(evento: dict) -> None:
    """Lo que el trabajador y la validación anotaban en el JSONL: un `intento` (de una entrega o de una validación), o el cierre de una entrega
    (`agotada`, `cancelada`). Un intento que respondió 2xx cierra su entrega como `entregada`."""
    importacion_json.asegurar("entregas-webhooks")
    tipo = evento.get("evento")
    en = _fecha_iso(evento["en"])
    with tablas.transaccion() as cur:
        if tipo == "intento":
            ok = bool(evento.get("ok"))
            cur.execute(
                "INSERT INTO nexus.webhookAttempts (webhookId, tenant, messageId, type, attemptNumber, occurredAt, ok, httpCode, durationMs, reason) "
                "SELECT webhookId, ?, ?, ?, ?, ?, ?, ?, ?, ? FROM nexus.webhooks WHERE publicId = ?",
                evento.get("tenant"), evento["id"], str(evento.get("tipo") or "")[:40], int(evento.get("n") or 0), en, ok, evento.get("codigo"),
                evento.get("ms"), (str(evento["motivo"])[:1000] if evento.get("motivo") else None), evento.get("webhook"),
            )
            if ok:
                cur.execute(
                    "UPDATE nexus.webhookDeliveries SET attempts = ?, lastAttemptAt = ?, status = 'entregada', closedAt = ? WHERE publicId = ? AND status = 'pendiente'",
                    int(evento.get("n") or 0), en, en, evento["id"],
                )
            else:
                cur.execute(
                    "UPDATE nexus.webhookDeliveries SET attempts = ?, lastAttemptAt = ? WHERE publicId = ? AND status = 'pendiente'",
                    int(evento.get("n") or 0), en, evento["id"],
                )
        elif tipo in ("agotada", "cancelada"):
            cur.execute(
                "UPDATE nexus.webhookDeliveries SET status = ?, closedAt = ?, closeReason = ? WHERE publicId = ? AND status = 'pendiente'",
                tipo, en, str(evento.get("motivo") or "")[:1000], evento["id"],
            )


@_como_almacen
def ent_cancelar_de(webhook: str, motivo: str, ahora: datetime) -> list[str]:
    """Cancela TODAS las entregas pendientes de un webhook (no solo las que este proceso tiene en memoria) y devuelve sus ids."""
    importacion_json.asegurar("entregas-webhooks")
    with tablas.transaccion() as cur:
        cur.execute(
            "UPDATE d SET status = 'cancelada', closedAt = ?, closeReason = ? OUTPUT inserted.publicId "
            "FROM nexus.webhookDeliveries d JOIN nexus.webhooks w ON w.webhookId = d.webhookId WHERE w.publicId = ? AND d.status = 'pendiente'",
            _naive(ahora), motivo[:1000], webhook,
        )
        return [f[0] for f in cur.fetchall()]


@_como_almacen
def ent_intentos(tenant: str, webhook: str, desde: datetime, hasta: datetime, tipo_validacion: str) -> list[tuple[datetime, dict]]:
    """Los intentos de ENTREGA (no los de validar) de un webhook entre `desde` y `hasta` (UTC), como `(fecha, {"ok", "ms"})`."""
    importacion_json.asegurar("entregas-webhooks")
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT a.occurredAt, a.ok, a.durationMs FROM nexus.webhookAttempts a JOIN nexus.webhooks w ON w.webhookId = a.webhookId "
            "WHERE w.publicId = ? AND a.tenant = ? AND a.type <> ? AND a.occurredAt >= ? AND a.occurredAt < ? ORDER BY a.attemptId",
            webhook, tenant, tipo_validacion, _naive(desde), _naive(hasta),
        )
        return [(_aware(cuando), {"ok": bool(ok), "ms": ms}) for cuando, ok, ms in cur.fetchall()]


@_como_almacen
def ent_historial(tenant: str, webhook: str, limite: int) -> list[dict]:
    importacion_json.asegurar("entregas-webhooks")
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT TOP (?) a.occurredAt, a.type, a.attemptNumber, a.ok, a.httpCode, a.durationMs, a.reason, a.messageId, d.entryId "
            "FROM nexus.webhookAttempts a JOIN nexus.webhooks w ON w.webhookId = a.webhookId "
            "LEFT JOIN nexus.webhookDeliveries d ON d.publicId = a.messageId AND d.webhookId = a.webhookId "
            "WHERE w.publicId = ? AND a.tenant = ? ORDER BY a.attemptId DESC",
            limite, webhook, tenant,
        )
        return [
            {
                "en": _iso(f["occurredAt"]), "tipo": f["type"], "n": f["attemptNumber"], "ok": bool(f["ok"]), "codigo": f["httpCode"],
                "ms": f["durationMs"], "motivo": f["reason"], "mensajeId": f["messageId"], "entradaId": f["entryId"],
            }
            for f in tablas.filas(cur)
        ]
