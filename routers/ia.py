"""Dominio: IA (Google Document AI).

Capa HTTP de los endpoints que llaman a los procesadores de Document AI. Se
agrupan bajo el tag "IA" en Swagger para distinguirlos de un vistazo de los que
pegan a SQL Server.

Este archivo no sabe de Document AI: la autenticación, las URLs de procesador y
el parseo de la respuesta viven en `servicios.ia`.
"""

import logging

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from config import MAX_SUBIDA_BYTES, MAX_SUBIDA_MB
from servicios import ia

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post(
    "/ine",
    tags=["IA"],
    summary="Extraer datos de INE",
    description=(
        "Recibe la imagen de una credencial INE y devuelve los campos extraídos "
        "por Document AI, con el domicilio anidado y las fechas en formato ISO."
    ),
)
async def extraer_ine(imagen: UploadFile = File(...)):
    if not imagen.content_type or not imagen.content_type.startswith("image/"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="El archivo no es una imagen. Sube un archivo con Content-Type: image/*.",
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
        # solo se devuelve un mensaje genérico para no filtrarlo al cliente.
        logger.exception("Falló la extracción de INE")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo procesar la credencial con Document AI.",
        ) from exc
