"""Dominio: archivos — recibir bytes y guardarlos en el almacén.

Esta es la puerta que le faltaba a `servicios/almacen.py`, que llevaba desde el
2026-09-08 construido, probado y deliberadamente desconectado esperando
exactamente esto.

## Qué hace y qué NO

Recibe un archivo, lo guarda y devuelve DÓNDE quedó. No registra nada en la
base: la tabla `file` todavía no existe (ver `docs/solicitudes-dba.md`,
secciones 3 y 4). Es a propósito y el orden importa — ver "Bytes primero, fila
después", abajo.

## Por qué el handler es `def` y no `async def`

El almacén escribe en disco y bloquea. Un `async def` que llame a algo
bloqueante congela el event loop y con él TODAS las peticiones en vuelo, no
solo la suya. Declarándolo `def`, FastAPI lo corre en su threadpool. Es la
misma convención que ya sigue `pyodbc` en este proyecto, por el mismo motivo.

Consecuencia práctica: los bytes se leen con `archivo.file.read()` (síncrono)
y no con `await archivo.read()`.

## Bytes primero, fila después — y por qué en ese orden

El día que exista la tabla, el registro va DESPUÉS de escribir los bytes, no
antes. Si falla el segundo paso, lo que queda es un objeto huérfano: barato,
deduplicado y que una limpieza posterior puede encontrar por su hash. Al revés
—fila primero— lo que queda es una fila apuntando a un archivo que no existe, y
eso se descubre meses después, cuando alguien quiere leer el documento.

El lugar exacto donde se enchufa está marcado con un comentario abajo.

## Quién es el `tenant`

Un CLIENTE (decidido el 2026-09-24; ver el docstring del almacén). Hoy llega
como parámetro porque no hay de dónde deducirlo: las llaves de API todavía no
están atadas a un tenant. **Eso es provisional y hay que saberlo** — mientras
siga así, quien tenga la llave puede escribir en el prefijo de cualquier
cliente cambiando un campo del formulario. Cerrar eso es el paso 10 del plan:
que el tenant se DEDUZCA de la credencial en vez de que el cliente lo afirme.

Mientras tanto, el único llamador real es el front de CSI subiendo sus propios
documentos de ejemplo, que van bajo el prefijo de operador.
"""

import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status

from servicios import almacen
from servicios.almacen import ErrorAlmacen
from servicios.subidas import SubidaInvalida, normalizar_tipo, revisar_tamano, revisar_tipo

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/",
    tags=["Archivos"],
    summary="Subir un archivo al almacén",
    description=(
        "Guarda los bytes y devuelve su ruta RELATIVA, que es lo que la base "
        "guardará el día que exista la tabla `file`. Es idempotente: subir dos "
        "veces el mismo contenido no duplica nada y responde `yaExistia: true`. "
        "El `sha256` es opcional y sirve para VERIFICAR la integridad de la "
        "transferencia, no para nombrar el objeto — el nombre siempre se "
        "recalcula sobre los bytes que de verdad llegaron."
    ),
)
def subir_archivo(
    archivo: UploadFile = File(...),
    tenant: str = Form(..., description="Prefijo de almacenamiento del cliente"),
    sha256: str | None = Form(
        None, description="Hash que calculó el cliente, para verificar la transferencia"
    ),
):
    try:
        revisar_tipo(archivo.content_type)
        revisar_tamano(archivo.size, "El archivo")
    except SubidaInvalida as invalida:
        raise HTTPException(status_code=invalida.http_status, detail=invalida.mensaje)

    # Se comprueba ANTES de leer el archivo a memoria: si el almacén está
    # apagado, subir 20 MB a RAM para tirarlos es trabajo regalado.
    if not almacen.esta_configurado():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "El almacén de documentos no está configurado en este servidor. "
                "Falta la variable ALMACEN_RUTA."
            ),
        )

    # Síncrono a propósito — ver el docstring del módulo.
    contenido = archivo.file.read()
    if not contenido:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="El archivo llegó vacío."
        )

    # Segundo cinturón, igual que en `/ia/*`: `archivo.size` viene en None si el
    # UploadFile no lo construyó el parser multipart, y ahí este es el único
    # guardia que queda.
    try:
        revisar_tamano(len(contenido), "El archivo")
    except SubidaInvalida as invalida:
        raise HTTPException(status_code=invalida.http_status, detail=invalida.mensaje)

    try:
        guardado = almacen.guardar(tenant, contenido, sha256)
    except ValueError as exc:
        # Entrada inválida: tenant con caracteres raros, hash mal formado, o el
        # hash del cliente no coincide con los bytes que llegaron. Reintentar no
        # ayuda, así que es 400 y el mensaje SÍ se propaga — lo escribió este
        # proyecto y no expone nada del servidor.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        # Fallo de escritura: disco lleno, permisos, el NAS que no responde. El
        # detalle NO se propaga —trae rutas del servidor— y sí se registra
        # completo. Mismo patrón que el resto de los routers.
        logger.exception("Falló el guardado en el almacén (tenant=%s)", tenant)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo guardar el archivo. Intenta de nuevo en unos minutos.",
        ) from exc

    # ── AQUÍ va el registro en `file`, cuando exista el SP ──────────────────
    # Orden: bytes (arriba) y DESPUÉS la fila. Ver el docstring del módulo.
    #   expediente_id = repo.obtener_expediente_de_entrada(tenant)
    #   file_id = repo.crear_file(tenant, expediente_id, guardado["rutaRelativa"],
    #                             guardado["hash"], guardado["bytes"], mime,
    #                             archivo.filename, canal)
    # Hasta entonces el objeto vive sin registro, y el índice de qué es de quién
    # lo lleva quien subió. Es aceptable para los ejemplos del asistente
    # (decenas, recuperables resubiendo) y NO lo es para el pipeline a volumen.
    # ────────────────────────────────────────────────────────────────────────

    return {
        "rutaRelativa": guardado["rutaRelativa"],
        "sha256": guardado["hash"],
        "tamanoBytes": guardado["bytes"],
        "yaExistia": guardado["yaExistia"],
        "mime": normalizar_tipo(archivo.content_type),
        "nombreOriginal": archivo.filename or "",
    }
