"""Dominio: documentos (ingesta, bandeja de preparación, pipeline).

⚠️ PLANTILLA — los nombres de stored procedures aquí son PLACEHOLDER. Se
reemplazan cuando el DBA entregue las firmas reales. La estructura sí es la
definitiva: un router por dominio, modelos Pydantic arriba de su endpoint,
y toda lectura/escritura vía `ejecutar_sp`.
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from db.sqlserver import ejecutar_sp

router = APIRouter()


@router.get(
    "/bandeja",
    tags=["Documentos"],
    summary="Consultar bandeja de preparación",
    description="Lista los documentos que ya ingresaron pero aún no entran al pipeline (HU027).",
)
def listar_bandeja(tenant_id: int):
    try:
        return ejecutar_sp("dbo.sp_ListarBandejaPreparacion", {"TenantId": tenant_id})
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo consultar la bandeja: {exc}") from exc


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
        filas = ejecutar_sp(
            "dbo.sp_RegistrarDocumento",
            {
                "TenantId": documento.tenant_id,
                "NombreArchivo": documento.nombre_archivo,
                "HashSha256": documento.hash_sha256,
                "Origen": documento.origen,
            },
            commit=True,
        )
        return filas[0] if filas else {"ok": True}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"No se pudo registrar el documento: {exc}") from exc
