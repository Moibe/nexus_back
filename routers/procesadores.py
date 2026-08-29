"""Endpoints del ciclo de vida de procesadores: el "Activar" del front.

El front manda el tipo documental COMPLETO (nombre, descripción y campos tal
como los capturó el wizard) en lugar de un id a buscar en la base — porque
todavía no hay base para esto: los tipos documentales viven en el localStorage
del navegador mientras el DBA entrega SQL Server. Cuando exista la tabla
`config_version`, este endpoint pasará a recibir solo el id y a leer de ahí.
"""

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from servicios.esquema import esquema_desde_campos
from servicios.procesadores import activar_tipo_documental

logger = logging.getLogger(__name__)

router = APIRouter()


class CampoEntrada(BaseModel):
    nombre: str = Field(min_length=1, max_length=200)
    tipoDato: str = "texto"
    descripcion: str = ""
    valorEstructura: str = ""
    obligatorio: bool = False
    cardinalidad: str = "unico"
    valoresLista: list[str] = []


class TipoDocumentalEntrada(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    nombre: str = Field(min_length=1, max_length=200)
    descripcion: str = ""
    campos: list[CampoEntrada]


@router.post(
    "/activar",
    tags=["Procesadores"],
    summary="Activar un tipo documental",
    description=(
        "Crea (o adopta, si ya existía) el Custom Extractor de Document AI del "
        "tipo documental y le sube el esquema armado desde sus campos. El "
        "procesador nace en modalidad zero-shot: extrae leyendo solo el esquema, "
        "sin entrenamiento. Devuelve `procesadorId` y `versionDefault`, que el "
        "front debe guardar junto al tipo — la versión se fija en cada "
        "extracción para que el resultado sea reproducible."
    ),
)
async def activar(tipo: TipoDocumentalEntrada):
    # Sin campos no hay nada que extraer: un procesador con esquema vacío se
    # crearía bien y fallaría después, en la primera extracción, lejos de la
    # causa. Mejor el error aquí, donde todavía se llama por su nombre.
    if not tipo.campos:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El tipo documental no tiene campos de extracción; agrega al menos uno antes de activar.",
        )

    esquema = esquema_desde_campos(
        tipo.nombre, tipo.descripcion, [c.model_dump() for c in tipo.campos]
    )
    try:
        resultado = await activar_tipo_documental(tipo.id, tipo.nombre, esquema)
    except RuntimeError as exc:
        logger.exception("Falló la activación del tipo %s", tipo.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"No se pudo activar en Document AI: {exc}",
        ) from exc

    return resultado
