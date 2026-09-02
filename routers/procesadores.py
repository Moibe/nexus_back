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
from servicios.procesadores import DocumentAIError, activar_tipo_documental, eliminar_procesador

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


def _mensaje_para(exc: DocumentAIError, *, mensaje_4xx: str) -> str:
    """Traduce un DocumentAIError al mensaje que ve el usuario.

    El branch de 5xx SÍ es idéntico para activar y eliminar: en los dos casos
    significa lo mismo ("Document AI no respondió; no es algo que se arregle
    cambiando nada, solo reintentar"). El de 4xx NO puede compartirse: para
    activar, un 4xx casi siempre es el esquema mal formado, y "revisa los
    campos" es cierto y accionable. Para eliminar, un DELETE no tiene campos
    que revisar — decir eso ahí sería tan confuso como el texto técnico crudo
    que este mensaje existe para evitar. Cada endpoint manda SU frase.
    """
    if 400 <= exc.status_code < 500:
        return mensaje_4xx
    return "No se pudo contactar a Document AI en este momento. Intenta de nuevo en unos minutos."


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
    except DocumentAIError as exc:
        # El texto técnico completo (status HTTP, ruta, el JSON de error de
        # Google) va al log, no al usuario: un "Invalid JSON payload received
        # at process_options.schema_override..." no le dice nada a quien está
        # configurando un tipo documental, y en el peor caso expone detalles
        # internos (nombres de recurso de GCP) sin necesidad.
        logger.exception("Falló la activación del tipo %s", tipo.id)
        mensaje = _mensaje_para(
            exc,
            mensaje_4xx=(
                "Document AI rechazó la configuración de este modelo. Revisa "
                "los nombres y tipos de los campos, y vuelve a intentar la "
                "activación."
            ),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=mensaje) from exc
    except RuntimeError as exc:
        # Fallas que NO vienen de una respuesta HTTP de Google (falta
        # DOCAI_PROJECT_ID en el .env, o el polling de la operación se agotó a
        # los 90s): no hay un status code que triar, así que van a un mensaje
        # genérico pero honesto — nunca el texto técnico crudo.
        logger.exception("Falló la activación del tipo %s", tipo.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo completar la activación. Intenta de nuevo; si el problema persiste, avisa al equipo técnico.",
        ) from exc

    return resultado


@router.delete(
    "/{procesador_id}",
    tags=["Procesadores"],
    summary="Borrar el Custom Extractor de un tipo documental",
    description=(
        "Borra el procesador de Document AI, su dataset y su esquema. "
        "IRREVERSIBLE. Si el procesador ya no existe en Google (borrado a mano, "
        "o nunca se llegó a crear), responde éxito igual: el objetivo es que no "
        "quede ahí, y si ya no está, el objetivo ya se cumplió."
    ),
)
async def eliminar(procesador_id: str):
    try:
        await eliminar_procesador(procesador_id)
    except DocumentAIError as exc:
        if exc.status_code == 404:
            # Ya no existe: exactamente el estado que se pedía. No es un error
            # para quien llamó — insistir en que "falló" cuando el procesador
            # ya no está ahí sería mentir en la dirección contraria.
            logger.info("Procesador %s ya no existía al querer borrarlo.", procesador_id)
            return {"eliminado": True}
        logger.exception("Falló el borrado del procesador %s", procesador_id)
        # A diferencia de activar, un 4xx aquí NO es "revisa tus campos" — un
        # DELETE no tiene campos que revisar. Lo más honesto es admitir que
        # Google no lo permitió, sin inventar una causa que no se conoce.
        mensaje = _mensaje_para(
            exc,
            mensaje_4xx=(
                "Document AI no permitió borrar este procesador en este "
                "momento. Intenta de nuevo; si el problema persiste, avisa "
                "al equipo técnico."
            ),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=mensaje) from exc
    except RuntimeError as exc:
        logger.exception("Falló el borrado del procesador %s", procesador_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo completar el borrado. Intenta de nuevo; si el problema persiste, avisa al equipo técnico.",
        ) from exc

    return {"eliminado": True}
