"""Registro PROVISIONAL de la bandeja de entrada: qué archivos llegaron por la
API y siguen esperando en la bandeja de preparación del front.

## Por qué existe, y por qué es provisional

La bandeja del front vivía solo en la memoria del navegador: un archivo que
un cliente sube por la API no tenía forma de aparecer ahí. Lo definitivo es
una fila en `[documents].[files]` dentro del expediente de entrada del tenant
(ver `docs/solicitudes-dba.md`, sección 4), pero eso espera los SPs del DBA.
Esto hace el mismo trabajo mientras tanto, para no depender de ese calendario.

El día que existan los SPs, lo que cambia es ESTE módulo: `registrar_entrada`,
`listar_pendientes` y `retirar` pasan a llamar a la base, y ni el router ni el
front se enteran. Los eventos ya guardados aquí sirven para el backfill: traen
exactamente lo que pide `uspCreateFile`.

## Dónde y cómo se guarda

Un registro de solo agregar por tenant, `bandeja-{tenant}.jsonl` (ver
`servicios/registro.py`, que tiene los detalles de escritura, cortes y
concurrencia): cada línea es un evento, `entrada` o `retiro`, y lo pendiente es
lo que entró y no ha salido.
"""

import uuid
from datetime import datetime, timezone

from servicios import almacen, registro

CANALES = {"API", "MANUAL"}
MOTIVOS_RETIRO = {"pipeline", "descartado"}


def _nombre(tenant: str) -> str:
    # Valida el tenant con la MISMA regla que el almacén: es también la defensa
    # contra un `../` en el nombre del archivo de registro.
    return f"bandeja-{almacen._validar_tenant(tenant)}.jsonl"


def registrar_entrada(
    tenant: str,
    guardado: dict,
    mime: str,
    nombre_original: str,
    canal: str,
    llave_id: str | None = None,
) -> dict:
    """Anota que un archivo YA GUARDADO en el almacén entró a la bandeja.

    Va después de `almacen.guardar`, nunca antes — el mismo orden que tendrá
    la fila de `file`: bytes primero, registro después. Si esto falla, queda un
    objeto sin registro, que es barato; al revés quedaría una entrada apuntando
    a un archivo que no existe.

    `llave_id` es el identificador público de la llave de cliente con la que se
    subió (`None` si lo subió el propio front con la llave de servicio): deja
    contestar "¿quién mandó esto?" sin guardar nada secreto.
    """
    if canal not in CANALES:
        raise ValueError(f"Canal de ingesta inválido: {canal!r}")
    entrada = {
        "evento": "entrada",
        "id": uuid.uuid4().hex,
        "tenant": tenant,
        "rutaRelativa": guardado["rutaRelativa"],
        "sha256": guardado["hash"],
        "tamanoBytes": guardado["bytes"],
        "mime": mime,
        "nombreOriginal": nombre_original,
        "canal": canal,
        "llaveId": llave_id,
        "recibidoEn": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    registro.agregar(_nombre(tenant), entrada)
    return _publica(entrada)


def listar_pendientes(tenant: str) -> list[dict]:
    """Lo que entró y todavía no se retira, del más viejo al más nuevo."""
    eventos = registro.eventos(_nombre(tenant))
    retirados = {e.get("id") for e in eventos if e.get("evento") == "retiro"}
    return [
        _publica(e)
        for e in eventos
        if e.get("evento") == "entrada" and e.get("id") not in retirados
    ]


def retirar(tenant: str, id_entrada: str, motivo: str) -> bool:
    """Saca una entrada de la bandeja. `False` si no existe o ya había salido.

    El archivo NO se borra del almacén: se retira de la bandeja porque pasó al
    pipeline o porque alguien lo descartó, y en los dos casos los bytes siguen
    haciendo falta (o pueden hacerla) — y con la deduplicación por contenido,
    borrar sería peligroso sin un conteo de referencias.
    """
    if motivo not in MOTIVOS_RETIRO:
        raise ValueError(f"Motivo de retiro inválido: {motivo!r}")
    nombre = _nombre(tenant)
    # Leer y agregar bajo el MISMO candado: dos retiros simultáneos de la misma
    # entrada no deben dejar dos eventos de retiro.
    with registro.candado:
        eventos = registro.eventos(nombre)
        entro = any(e.get("evento") == "entrada" and e.get("id") == id_entrada for e in eventos)
        salio = any(e.get("evento") == "retiro" and e.get("id") == id_entrada for e in eventos)
        if not entro or salio:
            return False
        registro.agregar(
            nombre,
            {
                "evento": "retiro",
                "id": id_entrada,
                "motivo": motivo,
                "en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
    return True


def _publica(entrada: dict) -> dict:
    """La entrada como la ve quien consulta, sin el campo interno `evento`."""
    return {k: v for k, v in entrada.items() if k != "evento"}
