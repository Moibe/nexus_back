"""Dominio: IA (Google Document AI).

Capa HTTP de los endpoints que llaman a los procesadores de Document AI. Se
agrupan bajo el tag "IA" en Swagger para distinguirlos de un vistazo de los que
pegan a SQL Server.

Este archivo no sabe de Document AI: la autenticación, las URLs de procesador y
el parseo de la respuesta viven en `servicios.ia`.
"""

import logging

import httpx
from fastapi import APIRouter, File, HTTPException, UploadFile, status

from config import MAX_SUBIDA_BYTES, MAX_SUBIDA_MB
from errores import ErrorDocumentAI
from servicios import ia

logger = logging.getLogger(__name__)

router = APIRouter()

# Qué ve el usuario por cada causa, y con qué status. El texto se escribe AQUÍ
# y no en el servicio a propósito: el servicio sabe qué falló, el router sabe a
# quién se lo está contando. Mismo criterio que `_mensaje_para` en
# routers/procesadores.py.
#
# Los mensajes son cortos porque el front los pinta dentro del renglón del
# documento en el Pipeline, no en un panel aparte.
#
# El status NO es 502 para todos: un PDF corrupto o demasiado largo es un
# problema del documento que mandó el cliente (4xx), no una falla de la
# pasarela; mezclarlos hacía que cualquier monitoreo por status code contara
# archivos malos del usuario como caídas de Document AI.
_POR_MOTIVO: dict[str, tuple[int, str]] = {
    "limite_paginas": (
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "El documento excede el máximo de páginas que Document AI procesa en línea.",
    ),
    "archivo_ilegible": (
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        "El archivo no se pudo abrir: puede estar dañado, incompleto o protegido con contraseña.",
    ),
    "cuota": (
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "Se alcanzó el límite de páginas por minuto de Document AI. Espera un momento y vuelve a intentar.",
    ),
    "servicio": (
        status.HTTP_502_BAD_GATEWAY,
        "Document AI no está disponible en este momento. Intenta de nuevo en unos minutos.",
    ),
}


def _fallar(exc: Exception, generico: str) -> HTTPException:
    """Traduce lo que sea que haya tronado al par (status, mensaje) que ve el
    usuario. `generico` es el texto de cada endpoint para lo no clasificado —
    lo único que existía antes de este mapeo."""
    if isinstance(exc, ErrorDocumentAI):
        codigo, mensaje = _POR_MOTIVO.get(
            exc.motivo, (status.HTTP_502_BAD_GATEWAY, generico)
        )
        return HTTPException(status_code=codigo, detail=mensaje)
    if isinstance(exc, httpx.TimeoutException):
        # No llegó a haber respuesta: del otro lado el trabajo pudo terminar
        # bien. Se dice así en vez de "falló", que sería afirmar de más.
        return HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Document AI tardó demasiado en responder. El documento puede ser muy grande.",
        )
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=generico)

# Los tipos que Document AI acepta en `rawDocument.mimeType`. NO es una lista
# arbitraria nuestra: es la del proveedor, y mandar algo fuera de ella se
# traduce en un 400 de Google que llegaría aquí disfrazado de 502.
#
# DOCX y XLSX quedan FUERA a propósito aunque la bandeja del front los admita:
# Document AI no los procesa. Se rechazan aquí con un mensaje que lo dice, en
# vez de dejar que fallen más adentro con un error del proveedor.
MIME_SOPORTADOS = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/gif",
        "image/bmp",
        "image/webp",
    }
)


@router.post(
    "/ine",
    tags=["IA"],
    summary="Extraer datos de INE",
    description=(
        "Recibe una credencial INE (imagen o PDF) y devuelve los campos extraídos "
        "por Document AI, con el domicilio anidado y las fechas en formato ISO. "
        "Cada campo trae `valor`, `confianza` (0-1) y `posicion` (caja "
        "normalizada 0-1: x, y, ancho, alto) para poder marcar en la UI los "
        "campos de baja confianza o resaltarlos sobre la imagen original. "
        "`confianza_minima`, a nivel raíz, es la menor confianza entre todos "
        "los campos — útil como semáforo de un vistazo. `_metadata.procesado_en` "
        "es la fecha/hora (UTC) en que este llamado a Document AI terminó. "
        "`_metadata.quality_alert` es `true` cuando la imagen no se reconoció "
        "como una INE (viene con `_metadata.motivo` y sin campos); en cualquier "
        "otro caso es `false` y `motivo` no aparece."
    ),
)
async def extraer_ine(imagen: UploadFile = File(...)):
    # `content_type` puede traer parámetros ("image/jpeg; charset=binary"), así
    # que se compara solo el tipo/subtipo en minúsculas.
    tipo = (imagen.content_type or "").split(";")[0].strip().lower()
    if tipo not in MIME_SOPORTADOS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Document AI no procesa '{tipo or 'desconocido'}'. "
                f"Formatos aceptados: {', '.join(sorted(MIME_SOPORTADOS))}."
            ),
        )

    # Respaldo del tope global de app.py, que mide `Content-Length`: una subida
    # con `Transfer-Encoding: chunked` no manda ese header y se le cuela.
    #
    # Va ANTES del read() a propósito: el parser multipart ya dejó `imagen.size`
    # poblado sin que haya que leer nada, así que se evita subir el archivo
    # entero a RAM para luego inflarlo ~1.33x al pasarlo a base64 en servicios.ia.
    #
    # Lo que esto NO evita: Starlette ya escribió el cuerpo completo en un
    # temporal en disco antes de que este handler corra su primera línea. Taparlo
    # exigiría contar bytes en el middleware conforme llegan.
    if imagen.size is not None and imagen.size > MAX_SUBIDA_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"La imagen excede el límite de {MAX_SUBIDA_MB:g} MB.",
        )

    contenido = await imagen.read()
    if not contenido:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="El archivo llegó vacío."
        )

    # Segundo cinturón: `imagen.size` viene en None si el UploadFile no lo
    # construyó el parser multipart (por ejemplo un test que lo instancia a mano),
    # y ahí este `len` es el único guardia. Redundante en el camino HTTP real.
    if len(contenido) > MAX_SUBIDA_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"La imagen excede el límite de {MAX_SUBIDA_MB:g} MB.",
        )

    try:
        return await ia.extraer_ine(contenido, imagen.content_type)
    except Exception as exc:  # noqa: BLE001
        # El detalle de Google ya quedó en el log dentro de servicios.ia; aquí
        # solo se devuelve el mensaje que corresponde a la causa, nunca el
        # texto crudo.
        logger.exception("Falló la extracción de INE")
        raise _fallar(exc, "No se pudo procesar la credencial con Document AI.") from exc


@router.post(
    "/clasificar",
    tags=["IA"],
    summary="Clasificar un documento entrante",
    description=(
        "Recibe un documento (imagen o PDF) y devuelve a cuál tipo documental "
        "activo pertenece, según el Custom Document Classifier. `categoria` es "
        "el `procesadorId` del Extractor al que hay que mandar el documento a "
        "continuación, o `\"otro\"` cuando no corresponde a ningún tipo "
        "configurado — en ese caso NO debe llamarse ningún extractor, el "
        "documento debe quedar marcado para revisión manual. `confianza` "
        "(0-100) es la de la categoría ganadora."
    ),
)
async def clasificar(archivo: UploadFile = File(...)):
    tipo = (archivo.content_type or "").split(";")[0].strip().lower()
    if tipo not in MIME_SOPORTADOS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Document AI no procesa '{tipo or 'desconocido'}'. "
                f"Formatos aceptados: {', '.join(sorted(MIME_SOPORTADOS))}."
            ),
        )
    if archivo.size is not None and archivo.size > MAX_SUBIDA_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"El archivo excede el límite de {MAX_SUBIDA_MB:g} MB.",
        )

    contenido = await archivo.read()
    if not contenido:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="El archivo llegó vacío."
        )
    if len(contenido) > MAX_SUBIDA_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"El archivo excede el límite de {MAX_SUBIDA_MB:g} MB.",
        )

    try:
        return await ia.clasificar_documento(contenido, archivo.content_type)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falló la clasificación del documento")
        raise _fallar(exc, "No se pudo clasificar el documento con Document AI.") from exc
