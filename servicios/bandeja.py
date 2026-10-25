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

## Cada entrada es también un documento

Al entrar, la entrada crea su **documento** (`servicios/documentos.py`) y guarda su `documentoId`. La entrada es pasajera (sale de la
bandeja al pasar al pipeline o al descartarse); el documento no: es lo que después se organiza en expedientes (`servicios/expedientes.py`).
"""

import re
import uuid
from datetime import datetime, timezone

from servicios import almacen, documentos, registro

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
    # El documento NACE aquí, al entrar al sistema, y antes que la entrada: si fallara la entrada quedaría un documento sin expediente
    # (permanente y sin daño); al revés quedaría una entrada pendiente que no es un documento. `documentoId` las une.
    documento = documentos.nacer(tenant, guardado, mime, nombre_original, canal, llave_id)
    entrada = {
        "evento": "entrada",
        "id": uuid.uuid4().hex,
        "documentoId": documento["id"],
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


def datos_de_entrada(tenant: str, id_entrada: str) -> dict | None:
    """La entrada tal como llegó (de ese tenant), o `None`. Para anotar qué documento se descartó."""
    for e in registro.eventos(_nombre(tenant)):
        if e.get("evento") == "entrada" and e.get("id") == id_entrada:
            return _publica(e)
    return None


def listar_descartadas(tenant: str) -> list[dict]:
    """Lo que se DESCARTÓ de esa bandeja, del descarte más reciente al más viejo: la entrada tal como llegó, más `descartadaEn` y,
    si la persona lo escribió, `detalle`. Es lo que ve el filtro «Descartado» de la bandeja; el archivo sigue en el almacén."""
    eventos = registro.eventos(_nombre(tenant))
    entradas = {e["id"]: e for e in eventos if e.get("evento") == "entrada" and isinstance(e.get("id"), str)}
    descartadas = [
        {**_publica(entradas[e["id"]]), "descartadaEn": e.get("en"), **({"detalle": e["detalle"]} if e.get("detalle") else {})}
        for e in eventos
        if e.get("evento") == "retiro" and e.get("motivo") == "descartado" and e.get("id") in entradas
    ]
    return list(reversed(descartadas))


def retirar(tenant: str, id_entrada: str, motivo: str, detalle: str | None = None) -> bool:
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
                # Lo que escribió la persona al descartar (opcional): el porqué, para quien revise después.
                **({"detalle": detalle} if detalle else {}),
                "en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
    return True


_RE_ID = re.compile(r"[0-9a-f]{32}")


def existe_en_otro_tenant(tenant: str, id_entrada: str) -> bool:
    """¿Hay una entrada con ese id en la bandeja de OTRO tenant? Solo para anotar un intento cruzado (HU11 CA-6): a quien
    pidió se le contesta igual que si no existiera.

    Repasa el registro de cada tenant, así que un valor que ni tiene forma de id (32 hexadecimales) no se busca: no cuesta
    nada rechazarlo y no se puede usar para hacer trabajar al servidor."""
    if not _RE_ID.fullmatch(id_entrada):
        return False
    propio = _nombre(tenant)
    for archivo in registro.ruta(propio).parent.glob("bandeja-*.jsonl"):
        if archivo.name != propio and any(e.get("evento") == "entrada" and e.get("id") == id_entrada for e in registro.eventos(archivo.name)):
            return True
    return False


def entrada_en_pipeline(tenant: str, id_entrada: str) -> dict | None:
    """La entrada, si entró a la bandeja de `tenant` y salió de ella porque PASÓ
    AL PIPELINE. `None` si no existe, es de otro tenant, sigue pendiente o se
    descartó. Es lo que deja aceptar un aviso de "este documento terminó" solo
    de algo que de verdad se mandó a procesar (ver `entregas_webhooks.avisar`)."""
    eventos = registro.eventos(_nombre(tenant))
    entrada = next((e for e in eventos if e.get("evento") == "entrada" and e.get("id") == id_entrada), None)
    if entrada is None:
        return None
    retiro = next((e for e in eventos if e.get("evento") == "retiro" and e.get("id") == id_entrada), None)
    if retiro is None or retiro.get("motivo") != "pipeline":
        return None
    return _publica(entrada)


def _publica(entrada: dict) -> dict:
    """La entrada como la ve quien consulta, sin el campo interno `evento`."""
    return {k: v for k, v in entrada.items() if k != "evento"}
