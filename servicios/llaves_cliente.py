"""Registro PROVISIONAL de las API keys de cliente.

Lo definitivo es la tabla que se le pidió al DBA (`docs/solicitudes-dba.md`,
sección 5). Mientras tanto las llaves viven en un registro de solo agregar en
el NAS, `llaves.jsonl` (ver `servicios/registro.py`): un evento `emitida` por
llave y un evento `revocada` si se revoca. El día que exista la tabla, cambia
ESTE módulo y nada más.

## Qué se guarda, y qué NUNCA

De cada llave: su identificador público, el tenant al que pertenece, nombre,
descripción, cuándo se creó y cuándo expira, y el SHA-256 del secret. El
secret mismo NUNCA: se devuelve una sola vez, al emitirla, y se olvida. Con el
hash alcanza para verificar (`seguridad_llaves.verificar`), y si alguien lee
este archivo no puede usar ninguna llave.

Un solo archivo para todos los tenants, y no uno por tenant como la bandeja: la
verificación busca la llave por su identificador ANTES de saber de qué tenant
es — el tenant sale justamente de la llave.

## Desde el 2026-10-30: en SQL donde hay base

Migración `0008`: donde hay base, las llaves viven en `nexus.apiKeys` (una fila por llave; revocarla es su columna `revokedAt`), importadas una
vez desde `llaves.jsonl` (`servicios/importacion_json.py`). Este módulo elige por `SQLSERVER_HOST` en cada llamada; el JSONL es lo que corre
donde no hay base. Cada ambiente tiene SUS llaves: una emitida en uno ya no vale en los otros. `documents.keyId` y `inboxEntries.keyId` ya
son llaves foráneas a ellas.

## El estado no se guarda, se calcula

"Expirada" sale de comparar `expiraEn` contra el reloj; lo único que se
registra es la revocación, que es un hecho. Misma regla que en el front.
"""

from datetime import datetime, timedelta, timezone

import seguridad_llaves
from config import SQLSERVER_HOST
from servicios import almacen, registro

ARCHIVO = "llaves.jsonl"


def _sql():
    """El módulo de SQL si el `.env` dice que hay base; `None` si no. Se importa tarde: sin base, `pyodbc` ni se carga."""
    if not SQLSERVER_HOST:
        return None
    from servicios import integraciones_sql

    return integraciones_sql

# Las cuatro opciones del desplegable del front. No hay "sin expiración".
DIAS_PERMITIDOS = {1, 7, 30, 90}
_LARGO_NOMBRE = 100
_LARGO_DESCRIPCION = 500


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _fecha(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def _indice() -> tuple[dict[str, dict], dict[str, str]]:
    """Las llaves emitidas por id, y cuándo se revocó cada una."""
    emitidas: dict[str, dict] = {}
    revocadas: dict[str, str] = {}
    for e in registro.eventos(ARCHIVO):
        if e.get("evento") == "emitida" and isinstance(e.get("id"), str):
            # Si un id apareciera dos veces (no debería: se sortea contra los
            # existentes), gana la PRIMERA — nadie puede "reemitir" una llave
            # ajena escribiendo otra con el mismo identificador.
            emitidas.setdefault(e["id"], e)
        elif e.get("evento") == "revocada" and isinstance(e.get("id"), str):
            revocadas.setdefault(e["id"], e.get("en", ""))
    return emitidas, revocadas


def _publica(emitida: dict, revocada_en: str | None) -> dict:
    """La llave como la ve el listado: SIN el hash del secret. Si el listado lo
    trajera, viajaría hasta el navegador, y con el hash y tiempo se puede
    intentar adivinar el secret sin que el servidor se entere de los intentos."""
    return {
        "id": emitida["id"],
        "tenant": emitida.get("tenant"),
        "nombre": emitida.get("nombre", ""),
        "descripcion": emitida.get("descripcion", ""),
        "creadaEn": emitida.get("creadaEn"),
        "expiraEn": emitida.get("expiraEn"),
        "revocadaEn": revocada_en or None,
    }


def emitir(tenant: str, nombre: str, descripcion: str, dias: int) -> tuple[str, dict]:
    """Emite una llave nueva para `tenant`. Devuelve `(secret, llave)`.

    El secret se devuelve AQUÍ y nunca más: quien llama tiene que mostrarlo y
    olvidarlo. Levanta `ValueError` con datos inválidos.
    """
    tenant = almacen._validar_tenant(tenant)
    nombre = (nombre or "").strip()
    descripcion = (descripcion or "").strip()
    if not nombre:
        raise ValueError("Falta el nombre de la llave.")
    if not descripcion:
        raise ValueError("Falta la descripción de la llave.")
    if len(nombre) > _LARGO_NOMBRE:
        raise ValueError(f"El nombre no puede pasar de {_LARGO_NOMBRE} caracteres.")
    if len(descripcion) > _LARGO_DESCRIPCION:
        raise ValueError(f"La descripción no puede pasar de {_LARGO_DESCRIPCION} caracteres.")
    if dias not in DIAS_PERMITIDOS:
        raise ValueError("La expiración tiene que ser de 1, 7, 30 o 90 días.")

    if sql := _sql():
        creada = _ahora()
        expira = creada + timedelta(days=dias)
        identificador, secret = sql.llaves_emitir(tenant, nombre, descripcion, seguridad_llaves.hash_de, creada, expira, seguridad_llaves.generar)
        evento = {
            "id": identificador,
            "tenant": tenant,
            "nombre": nombre,
            "descripcion": descripcion,
            "creadaEn": creada.isoformat(timespec="seconds"),
            "expiraEn": expira.isoformat(timespec="seconds"),
        }
        return secret, _publica(evento, None)

    # Sortear el id y escribir la llave bajo el MISMO candado: si no, dos
    # emisiones simultáneas podrían sacar el mismo identificador libre.
    with registro.candado:
        emitidas, _ = _indice()
        identificador, secret = seguridad_llaves.generar(lambda i: i in emitidas)
        creada = _ahora()
        evento = {
            "evento": "emitida",
            "id": identificador,
            "tenant": tenant,
            "secret_hash": seguridad_llaves.hash_de(secret),
            "nombre": nombre,
            "descripcion": descripcion,
            "creadaEn": creada.isoformat(timespec="seconds"),
            "expiraEn": (creada + timedelta(days=dias)).isoformat(timespec="seconds"),
        }
        registro.agregar(ARCHIVO, evento)
    return secret, _publica(evento, None)


def listar(tenant: str) -> list[dict]:
    """Las llaves del tenant, de la más nueva a la más vieja, sin hashes."""
    tenant = almacen._validar_tenant(tenant)
    if sql := _sql():
        return sql.llaves_listar(tenant)
    emitidas, revocadas = _indice()
    propias = [e for e in emitidas.values() if e.get("tenant") == tenant]
    propias.sort(key=lambda e: e.get("creadaEn") or "", reverse=True)
    return [_publica(e, revocadas.get(e["id"])) for e in propias]


def revocar(tenant: str, identificador: str) -> str | None:
    """Revoca una llave del tenant.

    Devuelve cuándo quedó revocada (ISO-8601), o `None` si no existe o es de
    otro tenant — las dos cosas se contestan igual a propósito, para no
    confirmarle a nadie que una llave ajena existe.

    Es IDEMPOTENTE: revocar una que ya estaba revocada devuelve la fecha de la
    primera vez y no escribe nada. Antes contestaba lo mismo que "no existe", y
    un segundo administrador que revocaba la misma llave desde otra pestaña veía
    "la llave sigue activa" de una llave que ya estaba muerta.
    """
    tenant = almacen._validar_tenant(tenant)
    if sql := _sql():
        return sql.llaves_revocar(tenant, identificador, _ahora())
    with registro.candado:
        emitidas, revocadas = _indice()
        llave = emitidas.get(identificador)
        if llave is None or llave.get("tenant") != tenant:
            return None
        if identificador in revocadas:
            return revocadas[identificador] or _ahora().isoformat(timespec="seconds")
        en = _ahora().isoformat(timespec="seconds")
        registro.agregar(ARCHIVO, {"evento": "revocada", "id": identificador, "en": en})
    return en


def existe_en_otro_tenant(tenant: str, identificador: str) -> bool:
    """¿Esa llave existe y es de OTRO tenant? Solo para anotar un intento cruzado (HU11 CA-6): a quien pidió se le
    contesta igual que si no existiera."""
    if sql := _sql():
        return sql.llaves_existe_en_otro_tenant(tenant, identificador)
    emitidas, _ = _indice()
    llave = emitidas.get(identificador)
    return llave is not None and llave.get("tenant") != tenant


def buscar_por_id(identificador: str) -> dict | None:
    """La fila que necesita `seguridad_llaves.verificar`, o `None` si no existe:
    `secret_hash`, `revocada_en` y `expira_en` (como `datetime`), más `tenant`,
    que es lo que la petición hereda si la llave sirve."""
    if sql := _sql():
        return sql.llaves_buscar_por_id(identificador)
    emitidas, revocadas = _indice()
    llave = emitidas.get(identificador)
    if llave is None:
        return None
    revocada_en = None
    if identificador in revocadas:
        # Revocar GANA aunque la fecha del evento venga ilegible: con `None`,
        # `verificar` la daría por viva, que es exactamente al revés.
        revocada_en = _fecha(revocadas[identificador]) or _ahora()
    return {
        "id": identificador,
        "tenant": llave.get("tenant"),
        "secret_hash": llave.get("secret_hash"),
        "revocada_en": revocada_en,
        # Una fecha de expiración ilegible sale como `None`, y `verificar`
        # rechaza la llave: ante la duda, no pasa.
        "expira_en": _fecha(llave.get("expiraEn")),
    }
