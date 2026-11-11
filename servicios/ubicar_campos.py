"""Ubicar los campos en un documento de ejemplo (Gemini), para MARCARLOS sobre él (Moibe, 2026-11-11: «cuando hacemos la creación de los campos de
extracción con un documento, que a un lado mostremos el documento y marcados etiquetados los campos seleccionados»).

«Generar a partir de un documento» (`servicios.procesadores.generar_campos`) usa `schemaVersions:generate` de Document AI, que propone el ESQUEMA
(nombres, tipos) pero no dice DÓNDE está cada dato. Para pintarlo sobre el documento, aquí se le pregunta a Gemini, con el documento adjunto y la
lista de campos ya propuestos: «¿dónde está el valor de cada uno?». Contesta, por campo, la página y una caja `[ymin, xmin, ymax, xmax]`
normalizada de 0 a 1000 (la convención de Gemini para ubicar cosas en una imagen), y el valor que leyó.

Medido el 2026-11-11 con un pasaporte SINTÉTICO (datos inventados, 900×600 px) y `gemini-3.1-flash-lite`: 12 de 12 campos ubicados, cada caja sobre su
valor con error de unos pocos píxeles, el campo que no existía («Autoridad») devuelto como no encontrado; 6.4 s, ~2,400 tokens (~US$0.0005).

Es una AYUDA VISUAL: no decide nada, no se guarda y no es lo que extraerá el procesador (eso lo hace Document AI al activar el tipo). Si falla, la
propuesta de campos sigue sirviendo igual: el front enseña el documento sin marcas. Misma credencial, región y modelo que `servicios/gemini.py`.
Los documentos salen a Google con esta llamada, como ya pasa con Document AI y con «Sugerir descripción con documentos».
"""

import base64
import json
import logging
import unicodedata

import httpx

from config import DOCAI_PROJECT_ID, GEMINI_MODELO, GEMINI_REGION, GEMINI_TIMEOUT
from servicios import gemini
from servicios.ia import _token

logger = logging.getLogger(__name__)

#: Cuántos campos se pueden ubicar en una petición, y cuánto de cada nombre y descripción viaja. Una propuesta normal trae 10 a 30.
MAX_CAMPOS = 60
MAX_NOMBRE = 80
MAX_DESCRIPCION = 200
#: Cuánto del valor leído se devuelve (solo se enseña como ayuda al pasar el puntero).
MAX_VALOR = 200
#: ~70 tokens por campo más el razonamiento (ver «Lo que hay que respetar» en `servicios/gemini.py`): con 13 campos se usaron 932.
MAX_TOKENS_SALIDA = 8192
#: Ubicar tarda más que sugerir una descripción (6.4 s con 13 campos): el tope es el de Gemini, pero nunca menos de un minuto.
TIMEOUT = max(GEMINI_TIMEOUT, 60)

MENSAJE_NO_DISPONIBLE = "Marcar los campos sobre el documento no está disponible en este servidor."
MENSAJE_FALLO = "No se pudieron marcar los campos sobre este documento."
MENSAJE_CUOTA = "Hay muchas solicitudes a la vez. Intenta de nuevo en un momento."

_ESQUEMA_RESPUESTA = {
    "type": "OBJECT",
    "properties": {
        "campos": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "campo": {"type": "STRING"},
                    "encontrado": {"type": "BOOLEAN"},
                    "valor": {"type": "STRING"},
                    "pagina": {"type": "INTEGER"},
                    "caja": {"type": "ARRAY", "items": {"type": "INTEGER"}},
                },
                "required": ["campo", "encontrado"],
            },
        }
    },
    "required": ["campos"],
}


class ErrorUbicar(Exception):
    """No se pudo ubicar. `status_code` es el que debe contestar la ruta; `str(exc)` es un texto que sí se puede mostrar."""

    def __init__(self, mensaje: str, status_code: int):
        super().__init__(mensaje)
        self.status_code = status_code


def _clave(nombre: str) -> str:
    """El nombre para compararlo con lo que devolvió el modelo: sin acentos, sin mayúsculas y sin espacios de más."""
    sin_acentos = "".join(c for c in unicodedata.normalize("NFD", nombre) if unicodedata.category(c) != "Mn")
    return " ".join(sin_acentos.lower().split())


def limpiar_campos(campos: object) -> list[dict]:
    """Los campos que pidió el front, validados: `[{nombre, descripcion}]` sin vacíos ni repetidos, con topes. `ValueError` si no sirven."""
    if not isinstance(campos, list):
        raise ValueError("Los campos deben ser una lista.")
    limpios: list[dict] = []
    vistos: set[str] = set()
    for c in campos:
        if not isinstance(c, dict) or not isinstance(c.get("nombre"), str):
            raise ValueError("Cada campo debe traer su nombre.")
        nombre = " ".join(c["nombre"].split())[:MAX_NOMBRE]
        descripcion = c.get("descripcion")
        descripcion = " ".join(descripcion.split())[:MAX_DESCRIPCION] if isinstance(descripcion, str) else ""
        if nombre and _clave(nombre) not in vistos:
            vistos.add(_clave(nombre))
            limpios.append({"nombre": nombre, "descripcion": descripcion})
    if not limpios:
        raise ValueError("No hay campos que ubicar.")
    if len(limpios) > MAX_CAMPOS:
        raise ValueError(f"Son demasiados campos: a lo más {MAX_CAMPOS}.")
    return limpios


def armar_prompt(campos: list[dict]) -> str:
    renglones = "\n".join(f"- {c['nombre']}" + (f": {c['descripcion']}" if c["descripcion"] else "") for c in campos)
    return (
        "Eres un localizador de datos en documentos. Para cada campo de la lista, encuentra en el documento adjunto el VALOR de ese campo "
        "(el dato, no su rótulo ni su título) y devuelve dónde está.\n"
        "- Devuelve TODOS los campos de la lista, en el mismo orden y con el nombre escrito exactamente igual.\n"
        "- `caja` es [ymin, xmin, ymax, xmax], normalizada de 0 a 1000 respecto a la página donde está el valor. `pagina` empieza en 1.\n"
        "- Si el valor ocupa varios renglones o es una tabla o sección que se repite, la caja cubre todo ese bloque.\n"
        "- Si un campo no aparece en el documento, devuélvelo con encontrado=false y sin caja. No inventes ubicaciones.\n"
        "- `valor` es el texto que se lee en la caja, tal cual.\n\n"
        f"Campos:\n{renglones}"
    )


def _caja_valida(caja: object) -> list[int] | None:
    if not isinstance(caja, list) or len(caja) != 4 or not all(isinstance(n, (int, float)) for n in caja):
        return None
    ymin, xmin, ymax, xmax = (max(0, min(1000, int(round(n)))) for n in caja)
    if ymax <= ymin or xmax <= xmin:
        return None
    return [ymin, xmin, ymax, xmax]


def leer_respuesta(texto: str, campos: list[dict]) -> list[dict]:
    """Las ubicaciones válidas, con el nombre del campo TAL COMO LO PIDIÓ el front (el modelo puede escribirlo distinto). Lo que no se reconoce o
    trae una caja imposible se descarta: es mejor no marcar un campo que marcarlo en otro lado. `ValueError` si la respuesta no es el JSON pedido."""
    datos = json.loads(texto)
    if not isinstance(datos, dict) or not isinstance(datos.get("campos"), list):
        raise ValueError("la respuesta no trae `campos`")
    por_clave = {_clave(c["nombre"]): c["nombre"] for c in campos}
    ubicaciones: list[dict] = []
    ya: set[str] = set()
    for c in datos["campos"]:
        if not isinstance(c, dict) or c.get("encontrado") is not True or not isinstance(c.get("campo"), str):
            continue
        nombre = por_clave.get(_clave(c["campo"]))
        caja = _caja_valida(c.get("caja"))
        if nombre is None or caja is None or nombre in ya:
            continue
        pagina = c.get("pagina")
        pagina = int(pagina) if isinstance(pagina, (int, float)) and pagina >= 1 else 1
        valor = c.get("valor")
        ya.add(nombre)
        ubicaciones.append({"campo": nombre, "pagina": pagina, "caja": caja, "valor": valor[:MAX_VALOR] if isinstance(valor, str) else ""})
    return ubicaciones


async def ubicar(contenido: bytes, mime: str, campos: list[dict]) -> dict:
    """Dónde está cada campo en el documento. Devuelve `{ubicaciones: [{campo, pagina, caja, valor}], modelo}` o levanta `ErrorUbicar`.

    `campos` ya pasó por `limpiar_campos`; `mime` es uno de `gemini.MIME_PARA_GEMINI` (lo valida la ruta). Nada se guarda."""
    if not DOCAI_PROJECT_ID:
        logger.warning("Ubicar campos: falta DOCAI_PROJECT_ID en el .env; no se llama a Gemini.")
        raise ErrorUbicar(MENSAJE_NO_DISPONIBLE, 503)
    try:
        token = _token()
    except Exception as exc:  # noqa: BLE001 — sin credencial o sin red a Google: el servidor no puede
        logger.warning("Ubicar campos: no se pudo obtener la credencial de Google: %s", exc)
        raise ErrorUbicar(MENSAJE_NO_DISPONIBLE, 503) from exc

    cuerpo = {
        # El documento va ANTES del texto, como en `servicios/gemini.py`.
        "contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(contenido).decode("ascii")}},
            {"text": armar_prompt(campos)},
        ]}],
        "generationConfig": {
            "maxOutputTokens": MAX_TOKENS_SALIDA,
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": _ESQUEMA_RESPUESTA,
        },
    }
    cabeceras = {"Authorization": f"Bearer {token}", "x-goog-user-project": DOCAI_PROJECT_ID}
    try:
        estado, respuesta = await gemini._llamar(gemini._url(), cabeceras, cuerpo, TIMEOUT)
    except httpx.TimeoutException as exc:
        logger.warning("Ubicar campos: Gemini no contestó en %s s (modelo %s).", TIMEOUT, GEMINI_MODELO)
        raise ErrorUbicar(MENSAJE_FALLO, 502) from exc
    except httpx.HTTPError as exc:
        logger.warning("Ubicar campos: error de red al llamar a Gemini: %s", type(exc).__name__)
        raise ErrorUbicar(MENSAJE_FALLO, 502) from exc

    if estado != 200:
        error = respuesta.get("error", {}) if isinstance(respuesta, dict) else {}
        logger.warning(
            "Ubicar campos: Gemini respondió %s (%s) con el modelo %s en %s: %s",
            estado, error.get("status", ""), GEMINI_MODELO, GEMINI_REGION, str(error.get("message") or respuesta)[:300],
        )
        if estado == 429:
            raise ErrorUbicar(MENSAJE_CUOTA, 429)
        if estado in (401, 403, 404):
            raise ErrorUbicar(MENSAJE_NO_DISPONIBLE, 503)
        raise ErrorUbicar(MENSAJE_FALLO, 502)

    texto, fin = gemini._texto_de(respuesta)
    try:
        ubicaciones = leer_respuesta(texto, campos)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Ubicar campos: no se entendió la respuesta de Gemini (fin %s, modelo %s): %s", fin or "?", GEMINI_MODELO, exc)
        raise ErrorUbicar(MENSAJE_FALLO, 502) from exc
    return {"ubicaciones": ubicaciones, "modelo": GEMINI_MODELO}
