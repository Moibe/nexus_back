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


async def _preguntar(contenido: bytes, mime: str, prompt: str, esquema: dict, que: str, fallo: str, no_disponible: str) -> tuple[str, str]:
    """Le manda a Gemini el documento y el prompt, pidiendo JSON con `esquema`. Devuelve `(texto, motivo_de_fin)` o levanta `ErrorUbicar` con
    un mensaje que se puede mostrar (nunca el detalle de Google: va al log). `que` solo sirve para el log."""
    if not DOCAI_PROJECT_ID:
        logger.warning("%s: falta DOCAI_PROJECT_ID en el .env; no se llama a Gemini.", que)
        raise ErrorUbicar(no_disponible, 503)
    try:
        token = _token()
    except Exception as exc:  # noqa: BLE001 — sin credencial o sin red a Google: el servidor no puede
        logger.warning("%s: no se pudo obtener la credencial de Google: %s", que, exc)
        raise ErrorUbicar(no_disponible, 503) from exc

    cuerpo = {
        # El documento va ANTES del texto, como en `servicios/gemini.py`.
        "contents": [{"role": "user", "parts": [
            {"inlineData": {"mimeType": mime, "data": base64.b64encode(contenido).decode("ascii")}},
            {"text": prompt},
        ]}],
        "generationConfig": {
            "maxOutputTokens": MAX_TOKENS_SALIDA,
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": esquema,
        },
    }
    cabeceras = {"Authorization": f"Bearer {token}", "x-goog-user-project": DOCAI_PROJECT_ID}
    try:
        estado, respuesta = await gemini._llamar(gemini._url(), cabeceras, cuerpo, TIMEOUT)
    except httpx.TimeoutException as exc:
        logger.warning("%s: Gemini no contestó en %s s (modelo %s).", que, TIMEOUT, GEMINI_MODELO)
        raise ErrorUbicar(fallo, 502) from exc
    except httpx.HTTPError as exc:
        logger.warning("%s: error de red al llamar a Gemini: %s", que, type(exc).__name__)
        raise ErrorUbicar(fallo, 502) from exc

    if estado != 200:
        error = respuesta.get("error", {}) if isinstance(respuesta, dict) else {}
        logger.warning(
            "%s: Gemini respondió %s (%s) con el modelo %s en %s: %s",
            que, estado, error.get("status", ""), GEMINI_MODELO, GEMINI_REGION, str(error.get("message") or respuesta)[:300],
        )
        if estado == 429:
            raise ErrorUbicar(MENSAJE_CUOTA, 429)
        if estado in (401, 403, 404):
            raise ErrorUbicar(no_disponible, 503)
        raise ErrorUbicar(fallo, 502)
    return gemini._texto_de(respuesta)


async def ubicar(contenido: bytes, mime: str, campos: list[dict]) -> dict:
    """Dónde está cada campo en el documento. Devuelve `{ubicaciones: [{campo, pagina, caja, valor}], modelo}` o levanta `ErrorUbicar`.

    `campos` ya pasó por `limpiar_campos`; `mime` es uno de `gemini.MIME_PARA_GEMINI` (lo valida la ruta). Nada se guarda."""
    texto, fin = await _preguntar(contenido, mime, armar_prompt(campos), _ESQUEMA_RESPUESTA, "Ubicar campos", MENSAJE_FALLO, MENSAJE_NO_DISPONIBLE)
    try:
        ubicaciones = leer_respuesta(texto, campos)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Ubicar campos: no se entendió la respuesta de Gemini (fin %s, modelo %s): %s", fin or "?", GEMINI_MODELO, exc)
        raise ErrorUbicar(MENSAJE_FALLO, 502) from exc
    return {"ubicaciones": ubicaciones, "modelo": GEMINI_MODELO}


# ── Describir un campo a partir de su recuadro ───────────────────────────────────────────────────────────────────────────────────────────
# Moibe, 2026-11-11 («sí, excelente, por favor la 1»): mover o dibujar un recuadro no cambia el esquema (no se guarda ni se le manda a Google);
# lo que el extractor sí lee es la DESCRIPCIÓN de cada campo. Aquí Gemini mira la zona que la persona marcó y PROPONE una descripción que sirva para
# encontrar ese dato en cualquier documento del tipo. La persona decide si la usa; nada se guarda aquí.

#: Lo más largo que puede ser la descripción propuesta: lo mismo que admite el formulario del front.
MAX_DESCRIPCION_PROPUESTA = 200
MAX_TEXTO_TIPO = 600
#: Un valor más corto que esto no se busca dentro de la descripción (una «F» o un «1» aparecen en cualquier frase).
MIN_VALOR_A_QUITAR = 4

MENSAJE_DESCRIBIR_FALLO = "No se pudo sugerir una descripción con este recuadro."
MENSAJE_DESCRIBIR_NO_DISPONIBLE = "Sugerir la descripción con el recuadro no está disponible en este servidor."

_ESQUEMA_DESCRIPCION = {
    "type": "OBJECT",
    "properties": {"descripcion": {"type": "STRING"}, "valor": {"type": "STRING"}},
    "required": ["descripcion"],
}

_TIPOS_LEGIBLES = {"texto": "texto", "numero": "número", "fecha": "fecha", "moneda": "moneda", "booleano": "sí o no", "lista": "lista de valores", "grupo": "grupo de campos"}


def limpiar_pedido_descripcion(campo: object, pagina: int, caja: object, nombre_tipo: str, descripcion_tipo: str) -> dict:
    """El pedido validado: `{nombre, tipoDato, descripcion, pagina, caja, tipo, descripcionTipo}`. `ValueError` con un texto mostrable si no sirve."""
    if not isinstance(campo, dict) or not isinstance(campo.get("nombre"), str) or not " ".join(campo["nombre"].split()):
        raise ValueError("El campo debe traer su nombre.")
    caja_ok = _caja_valida(caja)
    if caja_ok is None:
        raise ValueError("El recuadro no es válido.")
    if not 1 <= pagina <= 2000:
        raise ValueError("La página no es válida.")
    tipo_dato = campo.get("tipoDato") if isinstance(campo.get("tipoDato"), str) else "texto"
    descripcion = campo.get("descripcion") if isinstance(campo.get("descripcion"), str) else ""
    return {
        "nombre": " ".join(campo["nombre"].split())[:MAX_NOMBRE],
        "tipoDato": tipo_dato if tipo_dato in _TIPOS_LEGIBLES else "texto",
        "descripcion": " ".join(descripcion.split())[:MAX_DESCRIPCION],
        "pagina": pagina,
        "caja": caja_ok,
        "tipo": " ".join((nombre_tipo or "").split())[:MAX_NOMBRE],
        "descripcionTipo": " ".join((descripcion_tipo or "").split())[:MAX_TEXTO_TIPO],
    }


def armar_prompt_descripcion(p: dict) -> str:
    del_tipo = ""
    if p["tipo"]:
        del_tipo = f" del tipo «{p['tipo']}»" + (f" ({p['descripcionTipo']})" if p["descripcionTipo"] else "")
    actual = f"«{p['descripcion']}»" if p["descripcion"] else "no tiene"
    return (
        "Eres un redactor de descripciones de campos para un extractor de datos de documentos. El extractor lee documentos"
        f"{del_tipo} y encuentra cada campo SOLO por su nombre y su descripción.\n"
        f"En el documento adjunto, una persona marcó dónde está el valor del campo «{p['nombre']}» (tipo de dato: {_TIPOS_LEGIBLES[p['tipoDato']]}): "
        f"página {p['pagina']}, caja [ymin, xmin, ymax, xmax] = {p['caja']}, normalizada de 0 a 1000 respecto a esa página.\n"
        f"Su descripción actual: {actual}.\n\n"
        "Escribe una descripción NUEVA de ese campo que ayude a encontrarlo en CUALQUIER documento de este tipo, no solo en este:\n"
        "- Qué dato es, y dónde suele estar: zona de la página y junto a qué rótulo o encabezado.\n"
        "- Su forma: cuántos renglones abarca la caja, formato de la fecha o del número, caracteres típicos.\n"
        "- Lo que está DENTRO de la caja es el campo; lo de fuera no.\n"
        "- NO copies el valor ni ningún dato de este documento en particular (nombres, números, fechas concretas).\n"
        f"- En español, una o dos frases, a lo más {MAX_DESCRIPCION_PROPUESTA - 20} caracteres.\n"
        "`valor` es el texto que se lee dentro de la caja, tal cual."
    )


def _sin_valor(descripcion: str, valor: str) -> str:
    """La descripción sin el valor de ESTE documento, por si el modelo lo copió pese a la instrucción: la descripción termina en el esquema que se
    sube a Google y es la misma para todos los documentos, así que no debe llevar datos de uno."""
    v = valor.strip()
    if len(v) >= MIN_VALOR_A_QUITAR:
        i = descripcion.lower().find(v.lower())
        while i >= 0:
            descripcion = descripcion[:i] + descripcion[i + len(v):]
            i = descripcion.lower().find(v.lower())
    descripcion = " ".join(descripcion.replace(" ,", ",").replace(" .", ".").replace("«»", "").replace('""', "").split())
    return descripcion


def _recortar(texto: str, tope: int) -> str:
    if len(texto) <= tope:
        return texto
    corte = texto[:tope].rsplit(" ", 1)[0].rstrip(",;: ")
    return corte if corte.endswith(".") else corte + "…"


def leer_descripcion(texto: str) -> dict:
    """`{descripcion, valor}` de la respuesta. `ValueError` si no es el JSON pedido o la descripción viene vacía."""
    datos = json.loads(texto)
    if not isinstance(datos, dict) or not isinstance(datos.get("descripcion"), str):
        raise ValueError("la respuesta no trae `descripcion`")
    valor = datos.get("valor") if isinstance(datos.get("valor"), str) else ""
    descripcion = _recortar(_sin_valor(" ".join(datos["descripcion"].split()), valor), MAX_DESCRIPCION_PROPUESTA)
    if not descripcion:
        raise ValueError("la descripción vino vacía")
    return {"descripcion": descripcion, "valor": valor[:MAX_VALOR]}


async def describir(contenido: bytes, mime: str, pedido: dict) -> dict:
    """Una descripción propuesta para el campo, a partir de la zona marcada. Devuelve `{descripcion, valor, modelo}` o levanta `ErrorUbicar`.

    `pedido` ya pasó por `limpiar_pedido_descripcion`. Nada se guarda: la persona decide si la usa."""
    texto, fin = await _preguntar(
        contenido, mime, armar_prompt_descripcion(pedido), _ESQUEMA_DESCRIPCION, "Describir campo", MENSAJE_DESCRIBIR_FALLO, MENSAJE_DESCRIBIR_NO_DISPONIBLE
    )
    try:
        propuesta = leer_descripcion(texto)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Describir campo: no se entendió la respuesta de Gemini (fin %s, modelo %s): %s", fin or "?", GEMINI_MODELO, exc)
        raise ErrorUbicar(MENSAJE_DESCRIBIR_FALLO, 502) from exc
    return {**propuesta, "modelo": GEMINI_MODELO}
