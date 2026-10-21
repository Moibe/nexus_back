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

## Quién es el `tenant` y quién puede leer qué (HU11)

Un CLIENTE (decidido el 2026-09-24; ver el docstring del almacén). Llega como
parámetro del formulario, pero ya no se le cree al llamador: desde el
2026-10-14 las dos rutas exigen TAMBIÉN el JWT de la persona (el BFF lo reenvía)
y comprueban aquí que pueda operar en ese tenant (`servicios/aislamiento.py`).
Subir a un tenant que no es el tuyo: 403. Leer un archivo cuya ruta empieza por
el tenant de otro: 404, igual que si no existiera, y queda en la bitácora de
auditoría como `CROSS_TENANT_ACCESS`.

El único llamador real es el front de CSI subiendo los documentos de ejemplo
del asistente, que van bajo el tenant de quien los sube.
"""

import logging

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile, status
from fastapi.responses import Response

from servicios import aislamiento, almacen
from servicios.almacen import ErrorAlmacen
from servicios.subidas import (
    MIME_SOPORTADOS,
    SubidaInvalida,
    normalizar_tipo,
    revisar_tamano,
    revisar_tipo,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def guardar_subida(archivo: UploadFile, tenant: str, sha256: str | None) -> dict:
    """Valida una subida y guarda sus bytes en el almacén. Devuelve lo que
    `almacen.guardar` devuelve, o levanta la `HTTPException` que corresponda.

    Es la ÚNICA forma de recibir un archivo en esta API: la usan
    `POST /archivos/` y `POST /bandeja/`. Tenerla en un solo lugar es lo que
    garantiza que las dos puertas acepten y rechacen exactamente lo mismo.
    """
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

    return guardado


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
    authorization: str | None = Header(default=None),
):
    aislamiento.exigir(authorization, tenant, "archivo")
    guardado = guardar_subida(archivo, tenant, sha256)

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


@router.get(
    "/{ruta:path}",
    tags=["Archivos"],
    summary="Leer un archivo del almacén",
    description=(
        "Devuelve los bytes guardados en esa ruta relativa. El `mime` viaja como "
        "parámetro porque el almacén NO lo guarda: en disco los objetos no tienen "
        "extensión, y el tipo vive en la base (o, mientras no exista la tabla, en "
        "el catálogo de quien subió)."
    ),
)
def leer_archivo(
    ruta: str,
    mime: str = Query(
        "application/octet-stream",
        description="Con qué Content-Type devolverlo. Debe ser uno de los soportados.",
    ),
    authorization: str | None = Header(default=None),
):
    # Primero, de quién es la ruta: lo demás (el `mime`, si el almacén está o no) no debe contestarle nada a quien no puede
    # leerla.
    aislamiento.exigir_ruta(authorization, ruta)

    # El `mime` lo manda el cliente, así que NO se devuelve tal cual: se valida
    # contra la misma lista blanca de la subida. Sin esto, cualquiera podría
    # pedir un objeto y hacer que el navegador lo interprete como `text/html`,
    # que es un XSS servido desde nuestro propio origen.
    limpio = normalizar_tipo(mime)
    if limpio not in MIME_SOPORTADOS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"MIME no permitido para lectura: '{limpio or 'desconocido'}'.",
        )

    if not almacen.esta_configurado():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "El almacén de documentos no está configurado en este servidor. "
                "Falta la variable ALMACEN_RUTA."
            ),
        )

    # `almacen.leer` levanta `ErrorAlmacen` tanto si el archivo no está como si
    # el disco falló, y eso aquí NO da igual: lo primero es un 404 definitivo y
    # lo segundo un 503 que sí vale reintentar. Se distinguen con `existe()`,
    # que es un `stat` y no trae el contenido a memoria — y que con el montaje
    # caído levanta en vez de decir "no está", para que eso sea 503 y no 404.
    try:
        presente = almacen.existe(ruta)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        logger.exception("El almacén no está disponible para leer (ruta=%s)", ruta)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo leer el archivo. Intenta de nuevo en unos minutos.",
        ) from exc
    if not presente:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Ese archivo no está en el almacén."
        )

    try:
        contenido = almacen.leer(ruta)
    except ValueError as exc:
        # Ruta mal formada o que intenta salirse de la raíz. `almacen` ya valida
        # eso con su doble cinturón; aquí solo se traduce.
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        logger.exception("Falló la lectura del almacén (ruta=%s)", ruta)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No se pudo leer el archivo. Intenta de nuevo en unos minutos.",
        ) from exc

    # Inmutable por definición: el nombre del objeto ES el hash de su contenido,
    # así que una ruta nunca cambia de bytes. Se puede cachear para siempre, pero
    # `private`: el archivo es de una organización, y una caché compartida (un
    # proxy, una CDN) se lo serviría a quien lo pidiera sin pasar por aquí (HU11).
    return Response(
        content=contenido,
        media_type=limpio,
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )
