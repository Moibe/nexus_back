"""La bandeja de entrada: qué archivos llegaron (por la API o a mano) y siguen esperando en la bandeja de preparación del front.

## Por qué existe

La bandeja del front vivía solo en la memoria del navegador: un archivo que un cliente sube por la API no tenía forma de aparecer ahí. Desde
el 2026-10-29 (migración `0006`) las entradas viven en `nexus.inboxEntries`: una fila por entrada, y el retiro (al pipeline o descartada) son
sus columnas `removedAt`, `removalReason` y `removalDetail`. Se importaron una vez desde los viejos `bandeja-*.jsonl`
(`servicios/importacion_json.py`); desde el 2026-11-11 no hay otro almacenamiento. Lo pendiente es lo que entró y no ha salido.

## Cada entrada es también un documento

Al entrar, la entrada crea su **documento** (`servicios/documentos.py`) y guarda su `documentoId`. La entrada es pasajera (sale de la
bandeja al pasar al pipeline o al descartarse); el documento no: es lo que después se organiza en expedientes (`servicios/expedientes.py`).
"""

import re

from servicios import almacen, documentos, registros_sql


CANALES = {"API", "MANUAL"}
MOTIVOS_RETIRO = {"pipeline", "descartado"}


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
    la fila de `file`: bytes primero, fila después. Si esto falla, queda un
    objeto sin fila, que es barato; al revés quedaría una entrada apuntando
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
    return registros_sql.bandeja_entrar(almacen._validar_tenant(tenant), documento["id"], guardado, mime, nombre_original, canal, llave_id)


def listar_pendientes(tenant: str) -> list[dict]:
    """Lo que entró y todavía no se retira, del más viejo al más nuevo."""
    return registros_sql.bandeja_pendientes(almacen._validar_tenant(tenant))


def datos_de_entrada(tenant: str, id_entrada: str) -> dict | None:
    """La entrada tal como llegó (de ese tenant), o `None`. Para anotar qué documento se descartó."""
    f = registros_sql.bandeja_entrada(almacen._validar_tenant(tenant), id_entrada) if isinstance(id_entrada, str) else None
    return registros_sql._a_entrada(f) if f else None


def listar_descartadas(tenant: str) -> list[dict]:
    """Lo que se DESCARTÓ de esa bandeja, del descarte más reciente al más viejo: la entrada tal como llegó, más `descartadaEn` y,
    si la persona lo escribió, `detalle`. Es lo que ve el filtro «Descartado» de la bandeja; el archivo sigue en el almacén."""
    return registros_sql.bandeja_descartadas(almacen._validar_tenant(tenant))


def retirar(tenant: str, id_entrada: str, motivo: str, detalle: str | None = None) -> bool:
    """Saca una entrada de la bandeja. `False` si no existe o ya había salido.

    El archivo NO se borra del almacén: se retira de la bandeja porque pasó al
    pipeline o porque alguien lo descartó, y en los dos casos los bytes siguen
    haciendo falta (o pueden hacerla) — y con la deduplicación por contenido,
    borrar sería peligroso sin un conteo de referencias.
    """
    if motivo not in MOTIVOS_RETIRO:
        raise ValueError(f"Motivo de retiro inválido: {motivo!r}")
    return isinstance(id_entrada, str) and registros_sql.bandeja_retirar(almacen._validar_tenant(tenant), id_entrada, motivo, detalle or None)


_RE_ID = re.compile(r"[0-9a-f]{32}")


def existe_en_otro_tenant(tenant: str, id_entrada: str) -> bool:
    """¿Hay una entrada con ese id en la bandeja de OTRO tenant? Solo para anotar un intento cruzado (HU11 CA-6): a quien
    pidió se le contesta igual que si no existiera.

    Un valor que ni tiene forma de id (32 hexadecimales) no se busca: no cuesta nada rechazarlo y no se puede usar para hacer
    trabajar al servidor."""
    if not _RE_ID.fullmatch(id_entrada):
        return False
    return registros_sql.bandeja_en_otro_tenant(almacen._validar_tenant(tenant), id_entrada)


def entrada_en_pipeline(tenant: str, id_entrada: str) -> dict | None:
    """La entrada, si entró a la bandeja de `tenant` y salió de ella porque PASÓ
    AL PIPELINE. `None` si no existe, es de otro tenant, sigue pendiente o se
    descartó. Es lo que deja aceptar un aviso de "este documento terminó" solo
    de algo que de verdad se mandó a procesar (ver `entregas_webhooks.avisar`)."""
    f = registros_sql.bandeja_entrada(almacen._validar_tenant(tenant), id_entrada) if isinstance(id_entrada, str) else None
    return registros_sql._a_entrada(f) if f and f["removalReason"] == "pipeline" else None
