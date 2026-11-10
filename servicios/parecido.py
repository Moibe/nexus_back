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

**La forma de escribir no cuenta** (2026-11-10, Moibe: «KEYSI» contra «KEISY» no pasaba y «Keysi» contra «KEISY» sí; «para mí carece de sentido»).
Medido con Gemini real, el MISMO nombre daba 20 %, 90 % o 40 % según las mayúsculas, aunque el prompt ya decía que no importaban: lo que el modelo
ve le cambia el juicio. Por eso, antes de preguntar, los dos textos pasan por `comparable` (mayúsculas, sin acentos, sin espacios de más): el modelo
recibe siempre la misma pregunta, y la caché la reconoce. Si así quedan iguales, es el mismo texto: 100 sin preguntarle a nadie. Y cada par se
pregunta SOLO, en su propia llamada (en paralelo): juntos en una misma pregunta, lo que el modelo decidía de un par podía depender de los otros.

Usa la misma credencial, región y modelo que `servicios/gemini.py` (`GEMINI_MODELO`, `GEMINI_REGION`, `GEMINI_TIMEOUT`).
"""

import asyncio
import json
import logging
import time
import unicodedata
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
#: Cuántos pares se le preguntan a Gemini a la vez (cada uno en su llamada).
MAX_LLAMADAS_A_LA_VEZ = 5

#: Lo que se contesta, sin preguntarle a la IA, cuando los dos textos solo se escriben distinto. El front dice lo mismo (`MISMO_TEXTO` de `reglas.ts`).
RAZON_MISMO_TEXTO = "Es el mismo texto: solo cambian mayúsculas, acentos o espacios."

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


def comparable(texto: str) -> str:
    """El texto como se compara: en mayúsculas, sin acentos (ni la tilde de la ñ) y sin espacios de más. «Pérez  lópez» → «PEREZ LOPEZ».

    Es la misma regla que `textoComparable` de `reglas.ts` en el front (las marcas que se quitan son las de la categoría Unicode M, igual que `\\p{M}`)."""
    sin_marcas = "".join(c for c in unicodedata.normalize("NFD", texto) if not unicodedata.category(c).startswith("M"))
    return " ".join(sin_marcas.upper().split())


def _llave(a: str, b: str) -> tuple[str, str]:
    """El parecido es simétrico y no depende de cómo se escriba: «A ~ B», «B ~ A» y «a ~ b» son la misma pregunta."""
    x, y = comparable(a), comparable(b)
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
    """El texto que se le manda al modelo: los pares numerados (ya pasados por `comparable`) y cómo contestar (JSON).

    Los ejemplos del prompt NO son los casos con que se probó (KEYSI/KEISY se midió aparte, sin estar aquí): son para que el modelo entienda la
    regla, no para que se aprenda respuestas."""
    renglones = "\n".join(f"{i}. A: {json.dumps(a, ensure_ascii=False)} | B: {json.dumps(b, ensure_ascii=False)}" for i, (a, b) in enumerate(pares))
    return (
        "Compara cada par de textos (A y B), que vienen de documentos de personas y empresas en México, y di qué tan probable es que sean EL MISMO "
        "valor escrito de otra forma, como un porcentaje de 0 a 100.\n"
        "Los textos ya vienen en mayúsculas, sin acentos y sin espacios de más: eso nunca es una diferencia.\n"
        "Tampoco cambian el valor la puntuación, el orden de las palabras ni las abreviaturas comunes (por ejemplo «S.A. DE C.V.» es «SOCIEDAD ANONIMA "
        "DE CAPITAL VARIABLE», «S. DE R.L.» es «SOCIEDAD DE RESPONSABILIDAD LIMITADA», «CIA.» es «COMPANIA», «AV.» es «AVENIDA»).\n"
        "Una o dos letras distintas, de más, de menos o en otro orden dentro de una palabra son un error de captura del mismo valor, no otro valor: "
        "si se pronuncia igual (por ejemplo «MAYRA» y «MAIRA», o «GONZALEZ» y «GONZALES»), 90 o más; si cambia cómo suena, entre 70 y 89.\n"
        "Si los textos nombran cosas distintas (otra palabra, como «ACME COMERCIAL» y «ACME INDUSTRIAL»; otro nombre de persona, como «JUAN PEREZ» y "
        "«MARIA PEREZ»; otra fecha u otro número), el porcentaje debe ser bajo. 100 solo si son exactamente el mismo valor.\n"
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
    """Qué tanto se parece cada par: `[{porcentaje, razon}]` en el mismo orden.

    Un par que solo se escribe distinto es 100 sin preguntar; lo que ya está en la caché no se vuelve a preguntar; el resto se pregunta un par por
    llamada (`MAX_LLAMADAS_A_LA_VEZ` a la vez), con los textos ya pasados por `comparable`. Si una llamada falla, lo que sí salió queda en la caché y
    se levanta el error de la primera que falló: no se devuelve una medición a medias."""
    mismo = {"porcentaje": 100, "razon": RAZON_MISMO_TEXTO}
    llaves = [_llave(a, b) for a, b in pares]
    # Lo que ya se sabe se toma AQUÍ y no al final: guardar lo nuevo puede sacar de la caché algo viejo que esta misma petición usa.
    conocidos: dict[tuple[str, str], dict] = {}
    por_preguntar: list[tuple[str, str]] = []
    for llave in llaves:
        # Dos veces el mismo par en la petición: se pregunta una sola.
        if llave[0] == llave[1] or llave in conocidos or llave in por_preguntar:
            continue
        if llave in _CACHE:
            conocidos[llave] = _CACHE[llave]
            _CACHE.move_to_end(llave)
        else:
            por_preguntar.append(llave)
    if por_preguntar:
        limite = asyncio.Semaphore(MAX_LLAMADAS_A_LA_VEZ)

        async def una(llave: tuple[str, str]) -> dict:
            async with limite:
                return (await _preguntar([llave]))[0]

        nuevos = await asyncio.gather(*(una(llave) for llave in por_preguntar), return_exceptions=True)
        for llave, r in zip(por_preguntar, nuevos):
            if isinstance(r, dict):
                conocidos[llave] = r
                _CACHE[llave] = r
                _CACHE.move_to_end(llave)
        while len(_CACHE) > MAX_CACHE:
            _CACHE.popitem(last=False)
        error = next((r for r in nuevos if isinstance(r, BaseException)), None)
        if error is not None:
            raise error
    return [dict(mismo if llave[0] == llave[1] else conocidos[llave]) for llave in llaves]


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
