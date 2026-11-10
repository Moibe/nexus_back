"""«Parecido a»: qué tanto se parecen dos textos, según Gemini (Moibe, 2026-11-10: «vamos directo con la 3»).

Es el operador del motor de reglas que compara dos textos que no tienen por qué ser iguales letra por letra pero sí significar lo mismo:
«ACME COMERCIAL SA DE CV» y «Acme Comercial, S.A. de C.V.», o «S.A. de C.V.» y «Sociedad Anónima de Capital Variable». Una distancia entre letras
no sabe de abreviaturas; un modelo de lenguaje sí. Se eligió así (y no un diccionario de equivalencias) sabiendo el precio:

- **Cuesta y tarda**: una llamada a Vertex AI (~US$0.0003, ~1.5 s con `gemini-3.1-flash-lite`). Por eso se piden VARIOS pares en una llamada.
- **No es determinista por naturaleza**: se pide con `temperature: 0` y el resultado se GUARDA en una caché por par (`_CACHE`), así que el mismo par
  da el mismo número mientras el servidor no se reinicie. Lo que de verdad lo hace reproducible es que una EJECUCIÓN guarda en su historial el
  porcentaje y la razón que dio el modelo: lo que se decidió queda escrito, aunque otro día el modelo dijera otra cosa.
- **Los textos salen a Google**, como ya pasa con Document AI y con «Sugerir descripción».

Qué devuelve por par: `porcentaje` (0 a 100, entero) y `razon` (una frase corta en español que dice por qué). Quién decide si «pasa» NO es este
módulo: es el operador del front, que compara el porcentaje con el mínimo que se puso en el nodo (`reglas.ts`, la única definición del motor).

Usa la misma credencial, región y modelo que `servicios/gemini.py` (`GEMINI_MODELO`, `GEMINI_REGION`, `GEMINI_TIMEOUT`).
"""

import json
import logging
import time
from collections import OrderedDict

import httpx

from config import DOCAI_PROJECT_ID, GEMINI_MODELO, GEMINI_REGION, GEMINI_TIMEOUT
from servicios import gemini
from servicios.ia import _token

logger = logging.getLogger(__name__)

#: Cuántos pares caben en una petición, y cuánto puede medir cada texto. Un proceso normal tiene uno o dos «Parecido a»; esto es un techo.
MAX_PARES = 20
MAX_TEXTO = 500
#: Ver «Lo que hay que respetar» en `servicios/gemini.py`: el razonamiento cuenta contra el tope.
MAX_TOKENS_SALIDA = 2000
#: Cuántos pares recuerda la caché (los más recientes).
MAX_CACHE = 5000
#: Lo que puede pedir una persona por minuto (en PARES, no en peticiones): el lienzo pregunta al escribir los ejemplos, con pausa, y una ejecución
#: pregunta una vez. Esto frena un cliente descontrolado, no el uso normal.
MAX_PARES_POR_MINUTO = 200
MAX_RAZON = 200

MENSAJE_NO_DISPONIBLE = "La comparación con IA no está disponible en este servidor. Avisa al equipo técnico."
MENSAJE_FALLO = "No se pudo medir el parecido con IA. Intenta de nuevo en un momento."
MENSAJE_CUOTA = "Hay muchas comparaciones con IA a la vez. Intenta de nuevo en un momento."


class ErrorParecido(Exception):
    """No se pudo medir. `status_code` es el que debe contestar la ruta; `str(exc)` es un texto que sí se puede mostrar."""

    def __init__(self, mensaje: str, status_code: int):
        super().__init__(mensaje)
        self.status_code = status_code


_CACHE: "OrderedDict[tuple[str, str], dict]" = OrderedDict()
_PEDIDOS: dict[str, list[tuple[float, int]]] = {}


def _llave(a: str, b: str) -> tuple[str, str]:
    """El parecido es simétrico y no depende de espacios de más: «A ~ B» y «B ~ A» son la misma pregunta."""
    x, y = " ".join(a.split()), " ".join(b.split())
    return (x, y) if x <= y else (y, x)


def frenar(quien: str, pares: int) -> None:
    """Cuenta los pares que pide una persona en el último minuto; si se pasa de `MAX_PARES_POR_MINUTO`, 429."""
    ahora = time.monotonic()
    recientes = [(t, n) for t, n in _PEDIDOS.get(quien, []) if ahora - t < 60]
    if sum(n for _, n in recientes) + pares > MAX_PARES_POR_MINUTO:
        _PEDIDOS[quien] = recientes
        raise ErrorParecido(MENSAJE_CUOTA, 429)
    recientes.append((ahora, pares))
    _PEDIDOS[quien] = recientes


def armar_prompt(pares: list[tuple[str, str]]) -> str:
    """El texto que se le manda al modelo: los pares numerados y cómo contestar (JSON)."""
    renglones = "\n".join(f"{i}. A: {json.dumps(a, ensure_ascii=False)} | B: {json.dumps(b, ensure_ascii=False)}" for i, (a, b) in enumerate(pares))
    return (
        "Compara cada par de textos (A y B), que vienen de documentos de empresas en México, y di qué tan probable es que se refieran a LO MISMO, "
        "como un porcentaje de 0 a 100.\n"
        "Cuenta como iguales las diferencias que no cambian el significado: mayúsculas y minúsculas, acentos, puntuación, espacios, el orden de las palabras, "
        "errores de captura menores y abreviaturas comunes (por ejemplo «S.A. de C.V.» es «Sociedad Anónima de Capital Variable», «S. de R.L.» es "
        "«Sociedad de Responsabilidad Limitada», «Cía.» es «Compañía», «Av.» es «Avenida»).\n"
        "Si los textos nombran cosas distintas aunque se escriban parecido (por ejemplo «Acme Comercial» y «Acme Industrial», o dos personas, fechas o "
        "números distintos), el porcentaje debe ser bajo. 100 solo si significan exactamente lo mismo.\n"
        "Contesta SOLO con un arreglo JSON, un objeto por par y en el mismo orden: "
        '[{"indice": 0, "porcentaje": 96, "razon": "una frase corta en español que diga por qué"}].\n'
        f"Pares:\n{renglones}"
    )


def _leer(texto: str, cuantos: int) -> list[dict]:
    """Los resultados que dio el modelo, en el orden de los pares. Algo que no se entiende levanta `ValueError`."""
    t = (texto or "").strip()
    if t.startswith("```"):
        t = t.strip("`")
        t = t[t.find("[") :] if "[" in t else t
    inicio, fin = t.find("["), t.rfind("]")
    if inicio < 0 or fin < inicio:
        raise ValueError("sin arreglo JSON")
    datos = json.loads(t[inicio : fin + 1])
    if not isinstance(datos, list):
        raise ValueError("no es un arreglo")
    por_indice: dict[int, dict] = {}
    for d in datos:
        if not isinstance(d, dict):
            continue
        i, p = d.get("indice"), d.get("porcentaje")
        if isinstance(i, bool) or not isinstance(i, int) or isinstance(p, bool) or not isinstance(p, (int, float)):
            continue
        razon = " ".join(str(d.get("razon") or "").split())[:MAX_RAZON]
        por_indice[i] = {"porcentaje": max(0, min(100, round(p))), "razon": razon}
    faltan = [i for i in range(cuantos) if i not in por_indice]
    if faltan:
        raise ValueError(f"faltan los pares {faltan}")
    return [por_indice[i] for i in range(cuantos)]


async def medir(pares: list[tuple[str, str]]) -> list[dict]:
    """Qué tanto se parece cada par: `[{porcentaje, razon}]` en el mismo orden. Lo que ya está en la caché no se vuelve a preguntar."""
    resultados: list[dict | None] = [_CACHE.get(_llave(a, b)) for a, b in pares]
    pendientes = [i for i, r in enumerate(resultados) if r is None]
    # Dos veces el mismo par en la petición: se pregunta una sola.
    unicos: list[int] = []
    vistas: set[tuple[str, str]] = set()
    for i in pendientes:
        llave = _llave(*pares[i])
        if llave not in vistas:
            vistas.add(llave)
            unicos.append(i)
    if unicos:
        nuevos = await _preguntar([pares[i] for i in unicos])
        for i, r in zip(unicos, nuevos):
            _CACHE[_llave(*pares[i])] = r
            _CACHE.move_to_end(_llave(*pares[i]))
        while len(_CACHE) > MAX_CACHE:
            _CACHE.popitem(last=False)
        resultados = [_CACHE.get(_llave(a, b)) for a, b in pares]
    return [dict(r) for r in resultados]  # type: ignore[arg-type]


async def _preguntar(pares: list[tuple[str, str]]) -> list[dict]:
    if not DOCAI_PROJECT_ID:
        logger.warning("Parecido a: falta DOCAI_PROJECT_ID en el .env; no se llama a Gemini.")
        raise ErrorParecido(MENSAJE_NO_DISPONIBLE, 503)
    try:
        token = _token()
    except Exception as exc:  # noqa: BLE001 — sin credencial o sin red a Google: el servidor no puede
        logger.warning("Parecido a: no se pudo obtener la credencial de Google: %s", exc)
        raise ErrorParecido(MENSAJE_NO_DISPONIBLE, 503) from exc

    cuerpo = {
        "contents": [{"role": "user", "parts": [{"text": armar_prompt(pares)}]}],
        "generationConfig": {"maxOutputTokens": MAX_TOKENS_SALIDA, "temperature": 0, "responseMimeType": "application/json"},
    }
    cabeceras = {"Authorization": f"Bearer {token}", "x-goog-user-project": DOCAI_PROJECT_ID}
    try:
        estado, respuesta = await gemini._llamar(gemini._url(), cabeceras, cuerpo, GEMINI_TIMEOUT)
    except httpx.TimeoutException as exc:
        logger.warning("Parecido a: Gemini no contestó en %s s (modelo %s).", GEMINI_TIMEOUT, GEMINI_MODELO)
        raise ErrorParecido(MENSAJE_FALLO, 502) from exc
    except httpx.HTTPError as exc:
        logger.warning("Parecido a: error de red al llamar a Gemini: %s", type(exc).__name__)
        raise ErrorParecido(MENSAJE_FALLO, 502) from exc

    if estado != 200:
        error = respuesta.get("error", {}) if isinstance(respuesta, dict) else {}
        logger.warning(
            "Parecido a: Gemini respondió %s (%s) con el modelo %s en %s: %s",
            estado, error.get("status", ""), GEMINI_MODELO, GEMINI_REGION, str(error.get("message") or respuesta)[:300],
        )
        if estado == 429:
            raise ErrorParecido(MENSAJE_CUOTA, 429)
        if estado in (401, 403, 404):
            raise ErrorParecido(MENSAJE_NO_DISPONIBLE, 503)
        raise ErrorParecido(MENSAJE_FALLO, 502)

    texto, fin = gemini._texto_de(respuesta)
    try:
        return _leer(texto, len(pares))
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Parecido a: no se entendió la respuesta de Gemini (fin %s, modelo %s): %s", fin or "?", GEMINI_MODELO, exc)
        raise ErrorParecido(MENSAJE_FALLO, 502) from exc
