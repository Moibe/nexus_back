"""Las preguntas de la búsqueda por organización, para la sección «Búsqueda» de Nexus Monitor (NEX-609, 2026-11-23).

Pide solo la llave de servicio, como `/monitor-datos`. Solo devuelve NÚMEROS (cuántas, cuántas fallaron, tiempo, tokens y costo): nunca el texto de las
preguntas, que puede traer datos de personas. Ver `servicios/busqueda_bitacora.py`.
"""

from fastapi import APIRouter, Query

from servicios import busqueda_bitacora as bitacora

router = APIRouter()


@router.get(
    "/",
    summary="Las preguntas de la búsqueda por organización en los últimos días",
    description=(
        "`organizaciones`: por cada una, cuántas preguntas, respondidas, que no se pudieron responder con los datos (`noSePuede`), fallidas (sin consulta "
        "segura, tardó, el modelo o la conexión no disponibles), frenadas por el tope, que reusaron una consulta, valoradas útiles o no, personas, tiempo "
        "promedio, tokens, costo estimado en dólares y ejemplos aprobados. `totales`, `porResultado`, los `topes` y los `precios` con los que se estimó."
    ),
)
def panorama(dias: int = Query(30, ge=1, le=365, description="Cuántos días hacia atrás")):
    return bitacora.panorama_monitor(dias)
