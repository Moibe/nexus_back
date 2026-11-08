"""Qué procesadores de Document AI tienen dueño EN ESTE AMBIENTE, de solo lectura.

Lo usa Nexus Monitor para limpiar procesadores huérfanos desde una pantalla (antes: `limpiar_procesadores_huerfanos.py` en una
terminal del servidor). El proyecto de Google es UNO para dev, staging y prod, pero cada back solo conoce los tipos y las
organizaciones de SU almacén: un procesador sin dueño aquí puede tenerlo en otro ambiente. Por eso este endpoint no decide
nada: entrega lo que este ambiente sabe y Monitor junta lo de los tres antes de llamar huérfano a algo.

Pide solo la llave de servicio, como `/admins-plataforma` (ver su docstring). No expone datos de negocio: ids de procesador,
de tipo y guids de organización.
"""

from fastapi import APIRouter

from config import DOCAI_CLASIFICADOR_ID, DOCAI_PROCESADOR_INE
from routers.errores import almacen_no_disponible
from servicios import tenants_registro, tipos_documentales
from servicios.almacen import ErrorAlmacen

router = APIRouter()


@router.get(
    "/",
    tags=["Procesadores"],
    summary="Procesadores con dueño, organizaciones y protegidos de este ambiente",
    description=(
        "`conDueno`: `{procesadorId: {tenant, tipo, version}}` de cada procesador que algún tipo documental señala (vigente o de su "
        "historial). `organizaciones`: los guids de las que existen (su `nexusdoc-clasificador--{guid}` tiene dueño). `protegidos`: "
        "los ids que el `.env` usa (clasificador e INE legado) y que nunca se borran."
    ),
)
def duenos():
    try:
        con_dueno = tipos_documentales.procesadores_con_dueno()
        organizaciones = [t["guid"] for t in tenants_registro.listar()]
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    return {
        "conDueno": con_dueno,
        "organizaciones": organizaciones,
        "protegidos": [i for i in (DOCAI_CLASIFICADOR_ID, DOCAI_PROCESADOR_INE) if i],
    }
