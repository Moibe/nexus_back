"""Dominio: IA (Document AI).

Capa HTTP de los endpoints que delegan en los servicios de Document AI. Se
agrupan bajo el tag "IA" en Swagger para distinguirlos de un vistazo de los que
pegan a SQL Server.

Este archivo no llama httpx directo: la lógica vive en `servicios.ia`.
Los handlers son `async def` porque las llamadas a IA son I/O de red que puede
tardar decenas de segundos.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from servicios import ia

router = APIRouter()


class CalidadOcrIn(BaseModel):
    documento_id: int = Field(..., description="Documento ya ingresado a evaluar")


@router.post(
    "/ocr/calidad",
    tags=["IA"],
    summary="Evaluar calidad del OCR",
    description="Evalúa qué tan confiable quedó el OCR de un documento (HU032).",
)
async def evaluar_calidad_ocr(payload: CalidadOcrIn):
    try:
        return await ia.evaluar_calidad_ocr(payload.documento_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Falló el servicio de IA: {exc}") from exc


class ExtraccionIn(BaseModel):
    documento_id: int = Field(..., description="Documento del que se extraen los campos")
    tipo_documental_id: int = Field(
        ..., description="Tipo documental que define qué campos extraer (Configuration Table)"
    )


@router.post(
    "/extraccion",
    tags=["IA"],
    summary="Extraer campos del documento",
    description="Extrae los campos configurados para ese tipo documental.",
)
async def extraer_campos(payload: ExtraccionIn):
    try:
        return await ia.extraer_campos(payload.documento_id, payload.tipo_documental_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Falló el servicio de IA: {exc}") from exc
