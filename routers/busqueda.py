"""Dominio: la búsqueda en lenguaje natural sobre los datos extraídos (NEX-607; ver `servicios/busqueda.py`) y su bitácora y ejemplos (NEX-609; ver
`servicios/busqueda_bitacora.py`).

Todo va detrás de la llave de servicio y del JWT de la persona (HU11). El administrador de plataforma, que opera en su espacio sin guid, no tiene
organización sobre la cual preguntar: 400 al preguntar.

## Quién puede qué

- **Preguntar, ver sus preguntas recientes y valorar sus respuestas** («¿Te sirvió?»): CUALQUIER rol que opere en la organización (solo lee, como los
  expedientes): `aislamiento.exigir`. Solo se valora lo propio.
- **Ver lo que está por revisar, aprobar como ejemplo, descartar y quitar ejemplos**: quien administra la organización (`roles.PUEDEN_ADMINISTRAR`,
  `aislamiento.exigir_administrar`); los demás roles reciben 403 `sin_permiso` y queda `ROLE_DENIED`.

## Preguntar: `POST /busqueda/preguntar`

- Recibe `{tenant, pregunta, conversacion?}`. `conversacion` (NEX-611) es el id que devolvió la pregunta anterior: la nueva se entiende con las anteriores
  de esa conversación. Sin él (o si no es de esta persona en esta organización), empieza una conversación nueva.
- 200 `{columnas, filas, hayMas, tope, milisegundos, consulta, explicacion, intentos, preguntaId, origen, reusoDe, conversacion, turno, usaAnteriores,
  sugerencias}`. `filas` es una lista de listas en el orden de `columnas`; si había más renglones que `tope`, `hayMas` lo dice. `origen` es `modelo`,
  `ejemplo` (la consulta de un ejemplo aprobado de la organización, la misma pregunta) o `anterior` (la de una pregunta igual que ya había salido bien);
  con `reusoDe` dice de cuál. `preguntaId` es para valorarla (null si la bitácora no se pudo escribir). `conversacion` y `turno` dicen en qué conversación
  quedó y qué número de pregunta es; `usaAnteriores`, si tomó algo de las anteriores (la explicación lo dice); `sugerencias`, hasta tres preguntas para
  seguir.
- 400 pregunta vacía o demasiado larga, o el espacio de plataforma.
- 422 con `codigo`: `no_se_puede` (la pregunta no se responde con estos datos, con el motivo que dio el modelo; también «¿y de esas?» sin preguntas
  anteriores), `consulta_invalida` (lo que escribió el modelo no pasó la revisión ni corregido, o la base lo rechazó dos veces), `tardo` (pasó del tiempo
  máximo). Todos traen un `mensaje` para la persona, y `conversacion` y `turno`; el detalle técnico va al log.
- 429 `tope` (llegó al tope de uso: `{codigo, mensaje, alcance: persona|organizacion, tope, ventana: hora|dia, reintentarEn}`; NO se le preguntó al
  modelo) o `modelo` (cuota del modelo); 502 el modelo falló; 503 la búsqueda no está configurada en este servidor (sin credencial del modelo o sin
  `~/.nexus-lectura.env`). Nunca nombran al proveedor ni traen lo que dijo Google.

Toda pregunta con organización y texto válidos queda en la bitácora, salga como salga (también las que frena el tope).
"""

import asyncio
import logging

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from db import consulta
from servicios import aislamiento, auditoria, busqueda
from servicios import busqueda_bitacora as bitacora

logger = logging.getLogger(__name__)

router = APIRouter()

MENSAJE_TARDO = "La consulta tardó demasiado y se canceló. Intenta una pregunta más acotada (un tipo documental, un periodo)."
MENSAJE_FALLO_CONSULTA = "No se pudo correr la consulta. Intenta preguntar de otra forma."
_NO_EXISTE = {"codigo": "pregunta_no_existe", "mensaje": "Esa pregunta no existe."}
_EJEMPLO_NO_EXISTE = {"codigo": "ejemplo_no_existe", "mensaje": "Ese ejemplo no existe."}
_NO_VALORABLE = {"codigo": "no_valorable", "mensaje": "Solo se puede valorar o aprobar una respuesta que salió bien."}
_DEPENDE = {
    "codigo": "depende_de_la_conversacion",
    "mensaje": "Esa respuesta sigue de una pregunta anterior de su conversación: sola, la misma pregunta quiere decir otra cosa y no sirve de ejemplo.",
}


class Pregunta(BaseModel):
    tenant: str = Field(..., description="La organización")
    pregunta: str = Field(..., max_length=busqueda.MAX_PREGUNTA * 4, description="La pregunta, con las palabras de la persona")
    conversacion: str | None = Field(None, max_length=60, description="NEX-611: la conversación que sigue (el id que devolvió la pregunta anterior)")


class Valoracion(BaseModel):
    tenant: str = Field(..., description="La organización")
    util: bool = Field(..., description="¿Te sirvió? true sí, false no")


class SoloTenant(BaseModel):
    tenant: str = Field(..., description="La organización")


class Aprobacion(BaseModel):
    tenant: str = Field(..., description="La organización")
    preguntaId: str = Field(..., max_length=60, description="La pregunta de la bitácora que se vuelve ejemplo")


def _de_la_conversacion(exc: BaseException) -> dict:
    """NEX-611: en qué conversación quedó una pregunta que no se pudo responder (la pantalla sigue en ella)."""
    conversacion, turno = getattr(exc, "conversacion", None), getattr(exc, "turno", None)
    return {"conversacion": conversacion, "turno": turno} if conversacion else {}


def _422(codigo: str, mensaje: str, exc: BaseException | None = None) -> HTTPException:
    return HTTPException(status_code=422, detail={"codigo": codigo, "mensaje": mensaje, **(_de_la_conversacion(exc) if exc else {})})


def _anotar(accion: str, actor: aislamiento.Actor, tenant: str, **datos) -> None:
    """El rastro nunca cambia la respuesta: si la bitácora de auditoría falla, se avisa en el log."""
    try:
        auditoria.registrar(accion, usuario_guid=actor.guid, sesionGuid=actor.sesion, tenant=tenant, **datos)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo anotar %s en la bitácora de auditoría: %s", accion, exc)


@router.post(
    "/preguntar",
    tags=["Búsqueda"],
    summary="Preguntar en lenguaje natural sobre los datos extraídos y recibir la respuesta como tabla",
    description=(
        "Un modelo de lenguaje escribe UNA consulta de solo lectura con la pregunta, la descripción de los datos de la organización (sus tipos "
        "documentales y campos) y los ejemplos más parecidos; el servidor la revisa y la corre con la conexión de solo lectura que solo ve esa "
        "organización (NEX-605). La misma pregunta que un ejemplo aprobado o que una que ya salió bien reusa su consulta sin llamar al modelo. Los datos "
        "de los documentos no se mandan al modelo. Con `conversacion` (NEX-611) la pregunta se entiende con las anteriores de esa conversación (al modelo "
        "solo van esas preguntas y sus consultas). Cualquier rol que opere en la organización. 200 `{columnas, filas, hayMas, tope, milisegundos, "
        "consulta, explicacion, intentos, preguntaId, origen, reusoDe, conversacion, turno, usaAnteriores, sugerencias}`; 400 pregunta vacía o larga; 422 "
        "`no_se_puede`, `consulta_invalida` o `tardo` (con `conversacion` y `turno`); 429 `tope` (con `reintentarEn`) o cuota del modelo; 502/503 el "
        "modelo o la conexión no están disponibles."
    ),
)
async def preguntar(entrada: Pregunta, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir(authorization, entrada.tenant, "busqueda")
    try:
        r = await busqueda.preguntar(entrada.tenant, actor.guid, entrada.pregunta, entrada.conversacion)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except bitacora.TopeAlcanzado as exc:
        logger.info("Búsqueda: %s en %s llegó al tope (%s, %s por %s).", actor.guid, entrada.tenant, exc.alcance, exc.tope, exc.ventana)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "codigo": "tope", "mensaje": exc.mensaje, "alcance": exc.alcance, "tope": exc.tope, "ventana": exc.ventana,
                "reintentarEn": exc.reintentar_en.strftime("%Y-%m-%dT%H:%M:%SZ"), **_de_la_conversacion(exc),
            },
        ) from exc
    except busqueda.NoSePuede as exc:
        raise _422("no_se_puede", str(exc), exc) from exc
    except busqueda.ConsultaInvalida as exc:
        logger.info("Búsqueda: sin consulta válida para %s en %s: %s", actor.guid, entrada.tenant, str(exc)[:300])
        raise _422("consulta_invalida", busqueda.MENSAJE_INVALIDA, exc) from exc
    except consulta.ConsultaTardo as exc:
        raise _422("tardo", MENSAJE_TARDO, exc) from exc
    except consulta.ConsultaNoDisponible as exc:
        logger.warning("Búsqueda: la conexión de solo lectura no está disponible: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"codigo": "sin_configurar", "mensaje": busqueda.MENSAJE_NO_DISPONIBLE, **_de_la_conversacion(exc)},
        ) from exc
    except busqueda.ModeloNoDisponible as exc:
        raise HTTPException(status_code=exc.status_code, detail={"codigo": "modelo", "mensaje": str(exc), **_de_la_conversacion(exc)}) from exc
    logger.info(
        "Búsqueda: %s en %s → %s renglón(es)%s en %s ms, %s intento(s), origen %s, pregunta %s de su conversación%s.",
        actor.guid, entrada.tenant, len(r.filas), " (hay más)" if r.hay_mas else "", r.milisegundos, r.intentos, r.origen, r.turno,
        " (tomó algo de las anteriores)" if r.usa_anteriores else "",
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
        "preguntaId": r.pregunta_id,
        "origen": r.origen,
        "reusoDe": r.reuso_de,
        "conversacion": r.conversacion,
        "turno": r.turno,
        "usaAnteriores": r.usa_anteriores,
        "sugerencias": r.sugerencias,
    }


@router.get(
    "/preguntas",
    tags=["Búsqueda"],
    summary="Tus preguntas recientes",
    description="Las últimas preguntas DISTINTAS de quien pide en esa organización (la más reciente de cada una), para repetirlas con un clic. Cualquier rol.",
)
def recientes(
    tenant: str = Query(..., description="La organización"),
    limite: int = Query(8, ge=1, le=20),
    authorization: str | None = Header(default=None),
):
    actor = aislamiento.exigir(authorization, tenant, "busqueda")
    return {"preguntas": bitacora.recientes(tenant, actor.guid, limite)}


@router.post(
    "/preguntas/{pregunta_id}/valoracion",
    tags=["Búsqueda"],
    summary="«¿Te sirvió?» sobre una respuesta propia",
    description="Solo quien preguntó, y solo una respuesta que salió bien; se puede cambiar. 404 si no existe o no es suya; 409 `no_valorable` si no salió bien.",
)
def valorar(pregunta_id: str, entrada: Valoracion, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir(authorization, entrada.tenant, "busqueda")
    try:
        bitacora.valorar(entrada.tenant, actor.guid, pregunta_id, entrada.util)
    except bitacora.NoEncontrada as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except bitacora.NoValorable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_NO_VALORABLE) from exc
    return {"preguntaId": pregunta_id, "util": entrada.util}


@router.get(
    "/por-revisar",
    tags=["Búsqueda"],
    summary="Respuestas marcadas como útiles, para aprobarlas como ejemplo",
    description="Una por pregunta (la más reciente), con cuántas veces se hizo y a cuántas personas les sirvió. Solo quien administra la organización.",
)
def por_revisar(tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir_administrar(authorization, tenant, "busqueda_ejemplos")
    return {"preguntas": bitacora.por_revisar(tenant)}


@router.post(
    "/preguntas/{pregunta_id}/descartar",
    tags=["Búsqueda"],
    summary="Quitar una respuesta de «Por revisar» sin aprobarla",
    description="Solo quien administra la organización. Vuelve a aparecer si alguien la marca útil otra vez.",
)
def descartar(pregunta_id: str, entrada: SoloTenant, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, entrada.tenant, "busqueda_ejemplos")
    try:
        descartadas = bitacora.descartar(entrada.tenant, actor.guid, pregunta_id)
    except bitacora.NoEncontrada as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    return {"preguntaId": pregunta_id, "descartadas": descartadas}


@router.get(
    "/ejemplos",
    tags=["Búsqueda"],
    summary="Los ejemplos aprobados de la organización",
    description="El más reciente primero, con quién lo aprobó y cuántas veces se reusó su consulta tal cual. Solo quien administra la organización.",
)
def ejemplos(tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir_administrar(authorization, tenant, "busqueda_ejemplos")
    return {"ejemplos": bitacora.ejemplos(tenant)}


@router.post(
    "/ejemplos",
    status_code=status.HTTP_201_CREATED,
    tags=["Búsqueda"],
    summary="Aprobar una respuesta como ejemplo de la organización",
    description=(
        "Copia la pregunta, su consulta y su explicación a los ejemplos de la organización: desde la siguiente pregunta, la búsqueda de ESA organización "
        "(y solo de esa) la usa como ejemplo, y la misma pregunta usa esa consulta sin llamar al modelo. Solo quien administra la organización. 404 si la "
        "pregunta no es de la organización; 409 `no_valorable` si no salió bien; 409 `ya_es_ejemplo` si esa pregunta ya es un ejemplo; 409 "
        "`depende_de_la_conversacion` si la respuesta tomó algo de una pregunta anterior de su conversación (NEX-611)."
    ),
)
async def aprobar(entrada: Aprobacion, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, entrada.tenant, "busqueda_ejemplos")
    try:
        ejemplo = await asyncio.to_thread(bitacora.aprobar, entrada.tenant, actor.guid, entrada.preguntaId)
    except bitacora.NoEncontrada as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except bitacora.NoValorable as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_NO_VALORABLE) from exc
    except bitacora.DependeDeLaConversacion as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_DEPENDE) from exc
    except bitacora.YaEsEjemplo as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"codigo": "ya_es_ejemplo", "mensaje": "Esa pregunta ya es un ejemplo aprobado.", "ejemploId": exc.ejemplo_id},
        ) from exc
    # Su vector se saca ya, para que la siguiente pregunta no espere; si Google no contesta, se sacará cuando haga falta.
    await busqueda.vectorizar_ejemplo(entrada.tenant, ejemplo["id"], ejemplo["pregunta"])
    await asyncio.to_thread(_anotar, auditoria.SEARCH_EXAMPLE_APPROVED, actor, entrada.tenant, ejemploId=ejemplo["id"], preguntaId=entrada.preguntaId)
    return ejemplo


@router.delete(
    "/ejemplos/{ejemplo_id}",
    tags=["Búsqueda"],
    summary="Quitar un ejemplo aprobado",
    description="Deja de usarse desde la siguiente pregunta. Solo quien administra la organización. 404 si no es de la organización.",
)
def quitar(ejemplo_id: str, tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, tenant, "busqueda_ejemplos")
    try:
        quitado = bitacora.quitar_ejemplo(tenant, actor.guid, ejemplo_id)
    except bitacora.NoEncontrada as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_EJEMPLO_NO_EXISTE) from exc
    _anotar(auditoria.SEARCH_EXAMPLE_REMOVED, actor, tenant, ejemploId=ejemplo_id)
    return {"quitado": quitado["id"]}
