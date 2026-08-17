"""Dominio: documentos (ingesta, bandeja de preparación, pipeline).

Capa HTTP. Este archivo NO sabe de SQL Server ni de httpx: solo traduce
requests/responses y decide códigos de error. La lógica de datos vive en
`repositorios.documentos` y las llamadas a IA en `servicios.ia`.
"""

import logging

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from repositorios import documentos as repo

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/bandeja",
    tags=["Documentos"],
    summary="Consultar bandeja de preparación",
    description="Lista los documentos que ya ingresaron pero aún no entran al pipeline (HU027).",
)
def listar_bandeja(tenant_id: int):
    try:
        return repo.listar_bandeja(tenant_id)
    except Exception as exc:  # noqa: BLE001
        # El detalle NO se propaga: el texto de una excepción de pyodbc trae el
        # host, el driver y el usuario de SQL Server. Mismo patrón que
        # routers/ia.py — log completo adentro, mensaje genérico afuera.
        logger.exception("Falló la consulta de la bandeja (tenant_id=%s)", tenant_id)
        raise HTTPException(
            status_code=503, detail="No se pudo consultar la bandeja."
        ) from exc


class DocumentoIn(BaseModel):
    tenant_id: int = Field(..., description="Tenant dueño del documento")
    nombre_archivo: str = Field(..., description="Nombre original del archivo")
    hash_sha256: str = Field(..., description="Hash del contenido, para detectar duplicados (HU024)")
    origen: str = Field(..., description="Cómo llegó: manual, sftp, sharepoint, api")


@router.post(
    "/",
    tags=["Documentos"],
    summary="Registrar documento en ingesta",
    description="Da de alta un documento recién cargado o detectado por un conector (HU021 / HU022).",
)
def registrar_documento(documento: DocumentoIn):
    try:
        return repo.registrar_documento(
            tenant_id=documento.tenant_id,
            nombre_archivo=documento.nombre_archivo,
            hash_sha256=documento.hash_sha256,
            origen=documento.origen,
        ) or {"ok": True}
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            "Falló el registro de documento (tenant_id=%s, archivo=%s)",
            documento.tenant_id,
            documento.nombre_archivo,
        )
        raise HTTPException(
            status_code=503, detail="No se pudo registrar el documento."
        ) from exc
