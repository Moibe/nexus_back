"""Dominio: IA (Google Document AI).

Capa HTTP de los endpoints que llaman a los procesadores de Document AI. Se
agrupan bajo el tag "IA" en Swagger para distinguirlos de un vistazo de los que
pegan a SQL Server.

Este archivo no sabe de Document AI: la autenticación, las URLs de procesador y
el parseo de la respuesta viven en `servicios.ia`.
"""

import logging

from fastapi import APIRouter, File, HTTPException, UploadFile, status

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

    contenido = await imagen.read()
    if not contenido:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="El archivo llegó vacío."
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
