"""Los datos extraídos de todas las organizaciones, de solo lectura, para la sección «Datos» de Nexus Monitor.

Pide solo la llave de servicio, como `/duenos-procesadores` y `/esquema`. A diferencia de ellos, SÍ devuelve datos de negocio (lo que
Document AI leyó); Monitor los enmascara en producción antes de enseñarlos. Ver `servicios/datos_monitor.py`.
"""

from fastapi import APIRouter, HTTPException

from servicios import datos_monitor

router = APIRouter()


@router.get(
    "/",
    summary="Documentos extraídos y desempeño de cada campo por tipo documental",
    description=(
        "`documentos`: los que tienen al menos una extracción, la más reciente primero (tope `limite`), con su última corrida y cuántos "
        "campos vigentes tienen valor, faltaron o se corrigieron. `tipos`: por tipo documental y organización, cuántas veces salió, faltó "
        "o se corrigió cada campo y su confianza. Sin base: vacío con `sinBase: true`."
    ),
)
def panorama():
    return datos_monitor.panorama()


@router.get(
    "/{documento_id}",
    summary="La ficha de un documento: sus campos vigentes y todas sus corridas",
)
def documento(documento_id: str):
    try:
        return datos_monitor.documento(documento_id)
    except datos_monitor.DocumentoNoEncontrado as exc:
        raise HTTPException(status_code=404, detail="No hay tal documento en este ambiente.") from exc
