"""Vectores de significado («embeddings») por Vertex AI, para elegir los ejemplos de la búsqueda más parecidos a cada pregunta (NEX-609, 2026-11-23).

Un vector es una lista de números que resume el SIGNIFICADO de un texto: dos preguntas que dicen lo mismo con otras palabras («credenciales de elector
caducadas» y «¿Qué INE ya vencieron?») quedan cerca aunque no compartan ni una palabra. Medido con el modelo real el 2026-11-23: esas dos dieron 0.78 de
similitud, «accionistas de cada empresa» cayó en «¿Cuántos socios tiene cada acta constitutiva?» y «papeles que entraron por la API» en «por canal».

Se llama igual que Gemini (`servicios/gemini.py`): la MISMA credencial de Google, el proyecto de Document AI y la cabecera `x-goog-user-project`, a
`…/publishers/google/models/{EMBEDDINGS_MODELO}:predict`. Se pide `SEMANTIC_SIMILARITY` (se comparan preguntas con preguntas) y `EMBEDDINGS_DIMENSIONES`
dimensiones. La llamada pasa por `gemini._llamar`, así las pruebas sustituyen las dos con un solo doble.

Para elegir ejemplos se mandan PREGUNTAS (la de la persona y las de los ejemplos), nunca datos de los documentos. Si falla, levanta `SinEmbeddings` y
quien llama sigue sin elegir (con los ejemplos de siempre): la búsqueda no se cae porque esto falle.

Desde la 0022 (épica 14, búsqueda en el texto de los documentos) también se mandan los FRAGMENTOS de cada documento (`servicios/indice_semantico.py`)
y la consulta de quien busca (`servicios/busqueda_documentos.py`): al mismo proyecto de Google que ya lee los documentos completos con Document AI.
"""

from __future__ import annotations

import logging
import math

import httpx

import config
from servicios import gemini

logger = logging.getLogger(__name__)

#: Cuántos textos por llamada (Vertex acepta varios en `instances`; con lotes chicos una falla no se lleva todo).
LOTE = 25
TAREA = "SEMANTIC_SIMILARITY"
#: La búsqueda en el texto de los documentos (épica 14, 0022) compara una CONSULTA contra FRAGMENTOS: Google recomienda pedir cada lado con su tarea.
TAREA_DOCUMENTO = "RETRIEVAL_DOCUMENT"
TAREA_CONSULTA = "RETRIEVAL_QUERY"


class SinEmbeddings(Exception):
    """No se pudieron sacar los vectores (sin proyecto, sin credencial, Google falló o tardó). `str(exc)` es para el log."""


def etiqueta() -> str:
    """Con qué modelo y cuántas dimensiones se sacan los vectores: se guarda junto a cada vector, y uno con otra etiqueta se vuelve a sacar."""
    return f"{config.EMBEDDINGS_MODELO}:{config.EMBEDDINGS_DIMENSIONES}"


def _url() -> str:
    region = config.EMBEDDINGS_REGION
    host = "aiplatform.googleapis.com" if region == "global" else f"{region}-aiplatform.googleapis.com"
    return f"https://{host}/v1/projects/{gemini.DOCAI_PROJECT_ID}/locations/{region}/publishers/google/models/{config.EMBEDDINGS_MODELO}:predict"


async def vectores(textos: list[str], tarea: str = TAREA) -> tuple[list[list[float]], int]:
    """Los vectores de `textos`, en el mismo orden, y cuántos tokens contó Google. Levanta `SinEmbeddings`.

    `tarea`: `SEMANTIC_SIMILARITY` (preguntas contra preguntas, NEX-609), `RETRIEVAL_DOCUMENT` (fragmentos) o `RETRIEVAL_QUERY` (la consulta)."""
    if not textos:
        return [], 0
    if not gemini.DOCAI_PROJECT_ID:
        raise SinEmbeddings("falta DOCAI_PROJECT_ID")
    try:
        token = gemini._token()
    except Exception as exc:  # noqa: BLE001 — sin credencial o sin red a Google
        raise SinEmbeddings(f"sin credencial: {type(exc).__name__}") from exc
    cabeceras = {"Authorization": f"Bearer {token}", "x-goog-user-project": gemini.DOCAI_PROJECT_ID}
    salida: list[list[float]] = []
    tokens = 0
    for i in range(0, len(textos), LOTE):
        lote = textos[i : i + LOTE]
        cuerpo = {
            "instances": [{"content": t, "task_type": tarea} for t in lote],
            "parameters": {"outputDimensionality": config.EMBEDDINGS_DIMENSIONES},
        }
        try:
            estado, respuesta = await gemini._llamar(_url(), cabeceras, cuerpo, config.EMBEDDINGS_TIMEOUT)
        except httpx.HTTPError as exc:
            raise SinEmbeddings(f"red: {type(exc).__name__}") from exc
        except Exception as exc:  # noqa: BLE001 — cualquier otra cosa tampoco tumba la pregunta: se elige sin vectores
            raise SinEmbeddings(f"falló: {type(exc).__name__}") from exc
        if estado != 200 or not isinstance(respuesta, dict):
            error = respuesta.get("error", {}) if isinstance(respuesta, dict) else {}
            raise SinEmbeddings(f"Google respondió {estado} ({error.get('status', '')}) con {config.EMBEDDINGS_MODELO}: {str(error.get('message') or respuesta)[:200]}")
        predicciones = respuesta.get("predictions")
        if not isinstance(predicciones, list) or len(predicciones) != len(lote):
            raise SinEmbeddings("respuesta sin un vector por texto")
        for p in predicciones:
            datos = (p or {}).get("embeddings") or {}
            valores = datos.get("values")
            if not isinstance(valores, list) or not valores or not all(isinstance(v, (int, float)) for v in valores):
                raise SinEmbeddings("un vector vino vacío o no es una lista de números")
            salida.append([float(v) for v in valores])
            estadistica = datos.get("statistics") or {}
            tokens += int(estadistica.get("token_count") or 0)
    return salida, tokens


def coseno(a: list[float], b: list[float]) -> float:
    """Qué tan parecidos son dos vectores, de -1 a 1 (1 = el mismo significado). 0 si alguno está vacío o no miden lo mismo."""
    if not a or not b or len(a) != len(b):
        return 0.0
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)
