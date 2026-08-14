"""Repositorio del dominio documentos: TODO lo que toca SQL Server.

⚠️ Los nombres de stored procedures son PLACEHOLDER hasta que el DBA entregue
sus firmas reales.

Convención de capas: este archivo es el ÚNICO que sabe de SPs para este dominio.
Los routers nunca importan `db.sqlserver` directo — llaman funciones de aquí.
Nombres de función en español, verbo primero (listar_, obtener_, crear_,
registrar_, actualizar_, eliminar_), igual que en el resto de tus proyectos.
"""

from db.sqlserver import ejecutar_sp


def listar_bandeja(tenant_id: int) -> list[dict]:
    """Documentos que ya ingresaron pero aún no entran al pipeline (HU027)."""
    return ejecutar_sp("dbo.sp_ListarBandejaPreparacion", {"TenantId": tenant_id})


def registrar_documento(
    tenant_id: int, nombre_archivo: str, hash_sha256: str, origen: str
) -> dict:
    """Da de alta un documento recién cargado o detectado por un conector."""
    filas = ejecutar_sp(
        "dbo.sp_RegistrarDocumento",
        {
            "TenantId": tenant_id,
            "NombreArchivo": nombre_archivo,
            "HashSha256": hash_sha256,
            "Origen": origen,
        },
        commit=True,
    )
    return filas[0] if filas else {}
