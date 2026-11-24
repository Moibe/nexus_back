"""Dominio: la búsqueda en lenguaje natural sobre los datos extraídos (NEX-607; ver `servicios/busqueda.py`).

Una ruta: `POST /busqueda/preguntar` con `{tenant, pregunta}`. Detrás de la llave de servicio y del JWT de la persona (HU11); la ve CUALQUIER rol que
opere en la organización (solo lee, como los expedientes): `aislamiento.exigir`. El administrador de plataforma, que opera en su espacio sin guid,
recibe 400: no hay organización sobre la cual preguntar.

## Respuestas

- 200 `{columnas, filas, hayMas, tope, milisegundos, consulta, explicacion, intentos}`. `filas` es una lista de listas en el orden de `columnas`; si
  había más renglones que `tope`, `hayMas` lo dice.
- 400 pregunta vacía o demasiado larga, o el espacio de plataforma.
- 422 con `codigo`: `no_se_puede` (la pregunta no se responde con estos datos, con el motivo que dio el modelo), `consulta_invalida` (lo que escribió
  el modelo no pasó la revisión ni corregido, o la base lo rechazó dos veces), `tardo` (pasó del tiempo máximo). Todos traen un `mensaje` para la
  persona; el detalle técnico va al log.
- 429 cuota del modelo; 502 el modelo falló; 503 la búsqueda no está configurada en este servidor (sin credencial del modelo o sin
  `~/.nexus-lectura.env`). Nunca nombran al proveedor ni traen lo que dijo Google.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, status
from pydantic import BaseModel, Field

from db import consulta
from servicios import aislamiento, busqueda

logger = logging.getLogger(__name__)

router = APIRouter()

MENSAJE_TARDO = "La consulta tardó demasiado y se canceló. Intenta una pregunta más acotada (un tipo documental, un periodo)."
MENSAJE_FALLO_CONSULTA = "No se pudo correr la consulta. Intenta preguntar de otra forma."


class Pregunta(BaseModel):
    tenant: str = Field(..., description="La organización")
    pregunta: str = Field(..., max_length=busqueda.MAX_PREGUNTA * 4, description="La pregunta, con las palabras de la persona")


def _422(codigo: str, mensaje: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"codigo": codigo, "mensaje": mensaje})


@router.post(
    "/preguntar",
    tags=["Búsqueda"],
    summary="Preguntar en lenguaje natural sobre los datos extraídos y recibir la respuesta como tabla",
    description=(
        "Un modelo de lenguaje escribe UNA consulta de solo lectura con la pregunta y la descripción de los datos de la organización (sus tipos "
        "documentales y campos); el servidor la revisa y la corre con la conexión de solo lectura que solo ve esa organización (NEX-605). Los datos de "
        "los documentos no se mandan al modelo. Cualquier rol que opere en la organización. 200 `{columnas, filas, hayMas, tope, milisegundos, "
        "consulta, explicacion, intentos}`; 400 pregunta vacía o larga; 422 `no_se_puede`, `consulta_invalida` o `tardo`; 429/502/503 el modelo o la "
        "conexión no están disponibles."
    ),
)
async def preguntar(entrada: Pregunta, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir(authorization, entrada.tenant, "busqueda")
    try:
        r = await busqueda.preguntar(entrada.tenant, entrada.pregunta)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except busqueda.NoSePuede as exc:
        raise _422("no_se_puede", str(exc)) from exc
    except busqueda.ConsultaInvalida as exc:
        logger.info("Búsqueda: sin consulta válida para %s en %s: %s", actor.guid, entrada.tenant, str(exc)[:300])
        raise _422("consulta_invalida", busqueda.MENSAJE_INVALIDA) from exc
    except consulta.ConsultaTardo as exc:
        raise _422("tardo", MENSAJE_TARDO) from exc
    except consulta.ConsultaNoDisponible as exc:
        logger.warning("Búsqueda: la conexión de solo lectura no está disponible: %s", exc)
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail={"codigo": "sin_configurar", "mensaje": busqueda.MENSAJE_NO_DISPONIBLE}) from exc
    except busqueda.ModeloNoDisponible as exc:
        raise HTTPException(status_code=exc.status_code, detail={"codigo": "modelo", "mensaje": str(exc)}) from exc
    logger.info(
        "Búsqueda: %s en %s → %s renglón(es)%s en %s ms, %s intento(s).",
        actor.guid, entrada.tenant, len(r.filas), " (hay más)" if r.hay_mas else "", r.milisegundos, r.intentos,
    )
    return {
        "columnas": r.columnas,
        "filas": r.filas,
        "hayMas": r.hay_mas,
        "tope": r.tope,
        "milisegundos": r.milisegundos,
        "consulta": r.consulta,
        "explicacion": r.explicacion,
        "intentos": r.intentos,
    }
