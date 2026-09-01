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
from servicios.procesadores import DocumentAIError, activar_tipo_documental

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
    except DocumentAIError as exc:
        # El texto técnico completo (status HTTP, ruta, el JSON de error de
        # Google) va al log, no al usuario: un "Invalid JSON payload received
        # at process_options.schema_override..." no le dice nada a quien está
        # configurando un tipo documental, y en el peor caso expone detalles
        # internos (nombres de recurso de GCP) sin necesidad.
        logger.exception("Falló la activación del tipo %s", tipo.id)
        if 400 <= exc.status_code < 500:
            # Un 4xx es Google RECHAZANDO esta configuración puntual — el único
            # caso real donde "revisa tus campos" es cierto y útil.
            mensaje = (
                "Document AI rechazó la configuración de este modelo. Revisa "
                "los nombres y tipos de los campos, y vuelve a intentar la "
                "activación."
            )
        else:
            # 5xx, o cualquier otra cosa: Document AI no respondió bien. No es
            # algo que la configuración pueda arreglar, así que decir
            # "revisa tus campos" mandaría a buscar donde no está el problema.
            mensaje = (
                "No se pudo contactar a Document AI en este momento. Intenta "
                "de nuevo en unos minutos."
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
