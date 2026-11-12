"""Las API keys de cliente (`nexus.apiKeys`, migración `0008`).

## Qué se guarda, y qué NUNCA

De cada llave: su identificador público, el tenant al que pertenece, nombre,
descripción, cuándo se creó y cuándo expira, y el SHA-256 del secret. El
secret mismo NUNCA: se devuelve una sola vez, al emitirla, y se olvida. Con el
hash alcanza para verificar (`seguridad_llaves.verificar`), y si alguien lee
este archivo no puede usar ninguna llave.

La verificación busca la llave por su identificador ANTES de saber de qué
tenant es — el tenant sale justamente de la llave.

## Dónde viven

En `nexus.apiKeys` (una fila por llave; revocarla es su columna `revokedAt`), importadas una vez desde el viejo `llaves.jsonl`
(`servicios/importacion_json.py`); desde el 2026-11-11 no hay otro almacenamiento. Cada ambiente tiene SUS llaves. `documents.keyId` e
`inboxEntries.keyId` son llaves foráneas a ellas.

## El estado no se guarda, se calcula

"Expirada" sale de comparar `expiraEn` contra el reloj; lo único que se
registra es la revocación, que es un hecho. Misma regla que en el front.
"""

from datetime import datetime, timedelta, timezone

import seguridad_llaves
from servicios import almacen, integraciones_sql

# Las cuatro opciones del desplegable del front. No hay "sin expiración".
DIAS_PERMITIDOS = {1, 7, 30, 90}
_LARGO_NOMBRE = 100
_LARGO_DESCRIPCION = 500


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


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

    creada = _ahora()
    expira = creada + timedelta(days=dias)
    identificador, secret = integraciones_sql.llaves_emitir(
        tenant, nombre, descripcion, seguridad_llaves.hash_de, creada, expira, seguridad_llaves.generar
    )
    llave = {
        "id": identificador,
        "tenant": tenant,
        "nombre": nombre,
        "descripcion": descripcion,
        "creadaEn": creada.isoformat(timespec="seconds"),
        "expiraEn": expira.isoformat(timespec="seconds"),
    }
    return secret, _publica(llave, None)


def listar(tenant: str) -> list[dict]:
    """Las llaves del tenant, de la más nueva a la más vieja, sin hashes."""
    tenant = almacen._validar_tenant(tenant)
    return integraciones_sql.llaves_listar(tenant)


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
    return integraciones_sql.llaves_revocar(tenant, identificador, _ahora())


def existe_en_otro_tenant(tenant: str, identificador: str) -> bool:
    """¿Esa llave existe y es de OTRO tenant? Solo para anotar un intento cruzado (HU11 CA-6): a quien pidió se le
    contesta igual que si no existiera."""
    return integraciones_sql.llaves_existe_en_otro_tenant(tenant, identificador)


def buscar_por_id(identificador: str) -> dict | None:
    """La fila que necesita `seguridad_llaves.verificar`, o `None` si no existe:
    `secret_hash`, `revocada_en` y `expira_en` (como `datetime`), más `tenant`,
    que es lo que la petición hereda si la llave sirve."""
    return integraciones_sql.llaves_buscar_por_id(identificador)
