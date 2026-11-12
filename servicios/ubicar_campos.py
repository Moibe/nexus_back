"""Ubicar los campos en un documento de ejemplo (Gemini), para MARCARLOS sobre él (Moibe, 2026-11-11: «cuando hacemos la creación de los campos de
extracción con un documento, que a un lado mostremos el documento y marcados etiquetados los campos seleccionados»).

«Generar a partir de un documento» (`servicios.procesadores.generar_campos`) usa `schemaVersions:generate` de Document AI, que propone el ESQUEMA
(nombres, tipos) pero no dice DÓNDE está cada dato. Para pintarlo sobre el documento, aquí se le pregunta a Gemini, con el documento adjunto y la
lista de campos ya propuestos: «¿dónde está el valor de cada uno?». Contesta, por campo, la página y una caja `[ymin, xmin, ymax, xmax]`
normalizada de 0 a 1000 (la convención de Gemini para ubicar cosas en una imagen), y el valor que leyó.

Medido el 2026-11-11 con un pasaporte SINTÉTICO (datos inventados, 900×600 px) y `gemini-3.1-flash-lite`: 12 de 12 campos ubicados, cada caja sobre su
valor con error de unos pocos píxeles, el campo que no existía («Autoridad») devuelto como no encontrado; 6.4 s, ~2,400 tokens (~US$0.0005).

Es una AYUDA VISUAL: no decide nada, no se guarda y no es lo que extraerá el procesador (eso lo hace Document AI al activar el tipo). Si falla, la
propuesta de campos sigue sirviendo igual: el front enseña el documento sin marcas. Lo que SÍ llega al esquema es lo que se escribe a partir de
cada recuadro (descripción, tipo y un ejemplo de la forma: `completar` y `proponer`, al final, desde el 2026-11-12). Misma credencial, región y modelo que `servicios/gemini.py`.
Los documentos salen a Google con esta llamada, como ya pasa con Document AI y con «Sugerir descripción con documentos».
"""

import base64
import json
import logging
import re
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


async def _preguntar(
    contenido: bytes, mime: str, prompt: str, esquema: dict, que: str, fallo: str, no_disponible: str, max_tokens: int = MAX_TOKENS_SALIDA
) -> tuple[str, str]:
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
            "maxOutputTokens": max_tokens,
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


# ── Completar los campos con su recuadro ─────────────────────────────────────────────────────────────────────────────────────────────────
# 2026-11-11 (Moibe, «sí, excelente, por favor la 1»): mover o dibujar un recuadro no cambiaba el esquema, porque el recuadro no se guarda ni se le
# manda a Google; lo que el extractor sí lee de cada campo es su nombre, su tipo, su DESCRIPCIÓN y su «valor de estructura» (un ejemplo de su forma,
# que viaja dentro de la descripción: `esquema._descripcion_de`). Entonces el recuadro solo SUGERÍA la descripción del campo que se editaba.
#
# 2026-11-12 (Moibe: «quiero que ese etiquetado sí sirva para definir el schema»; eligió «El recuadro completa el campo» y «Dibujar crea el campo»):
# ahora cada recuadro se convierte en esas cosas. Gemini mira lo que hay dentro de cada uno y escribe, por campo, la descripción (qué dato es, en qué
# zona y junto a qué rótulo, y su forma), el tipo de dato que corresponde a lo que se lee y un ejemplo INVENTADO con la misma forma (`completar`). Y un
# recuadro dibujado donde no había campo PROPONE uno nuevo, con su nombre (`proponer`). Nada se guarda aquí: el front lo pone en la lista, sin pisar
# lo que la persona escribió, y ella decide qué agrega al tipo.

#: Lo más largo que puede ser la descripción propuesta: lo mismo que admite el formulario del front.
MAX_DESCRIPCION_PROPUESTA = 200
MAX_TEXTO_TIPO = 600
#: Un valor más corto que esto no se busca dentro de la descripción (una «F» o un «1» aparecen en cualquier frase).
MIN_VALOR_A_QUITAR = 4
#: Lo más largo de un ejemplo de estructura (una línea de MRZ mide 44 caracteres; un domicilio completo, más).
MAX_ESTRUCTURA = 120
#: Cuántos nombres de campos que ya existen viajan al proponer uno nuevo (para que no repita ninguno).
MAX_EXISTENTES = 200
#: ~100 tokens por campo más el razonamiento (ver «Lo que hay que respetar» en `servicios/gemini.py`): con 60 campos, 8192 se quedaría corto.
MAX_TOKENS_COMPLETAR = 16384
#: Los tipos que se pueden deducir de UN valor. «Lista» y «grupo» no: dependen de todos los valores posibles o de cómo está armado el documento.
TIPOS_DEDUCIBLES = ("texto", "numero", "fecha", "moneda", "booleano")
_SINONIMOS_TIPO = {
    "texto": "texto", "text": "texto", "string": "texto", "cadena": "texto",
    "numero": "numero", "número": "numero", "number": "numero", "entero": "numero", "integer": "numero",
    "fecha": "fecha", "date": "fecha",
    "moneda": "moneda", "currency": "moneda", "importe": "moneda", "dinero": "moneda",
    "booleano": "booleano", "boolean": "booleano", "bool": "booleano", "si o no": "booleano", "sí o no": "booleano",
}

MENSAJE_DESCRIBIR_FALLO = "No se pudo sugerir una descripción con este recuadro."
MENSAJE_DESCRIBIR_NO_DISPONIBLE = "Sugerir la descripción con el recuadro no está disponible en este servidor."
MENSAJE_COMPLETAR_FALLO = "No se pudieron completar los campos con sus recuadros."
MENSAJE_COMPLETAR_NO_DISPONIBLE = "Completar los campos con sus recuadros no está disponible en este servidor."
MENSAJE_PROPONER_FALLO = "No se pudo proponer un campo con este recuadro."
MENSAJE_PROPONER_NO_DISPONIBLE = "Proponer un campo con un recuadro no está disponible en este servidor."

_PROPIEDADES_COMPLETADO = {
    "descripcion": {"type": "STRING"},
    "tipoDato": {"type": "STRING"},
    "estructura": {"type": "STRING"},
    "valor": {"type": "STRING"},
}
# `propertyOrdering`: Gemini escribe las propiedades en ESTE orden (sin él, en orden alfabético: la descripción antes que el valor). Primero lee lo que
# hay en la caja y después lo describe; al revés, con Gemini real (2026-11-12), un recuadro dibujado sobre el número de pasaporte salía como «Fecha de
# expedición», el único campo que faltaba en la lista, con el valor de esa fecha.
_ESQUEMA_COMPLETAR = {
    "type": "OBJECT",
    "properties": {
        "campos": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {"campo": {"type": "STRING"}, **_PROPIEDADES_COMPLETADO},
                "required": ["campo", "descripcion"],
                "propertyOrdering": ["campo", "valor", "descripcion", "tipoDato", "estructura"],
            },
        }
    },
    "required": ["campos"],
}
_ESQUEMA_PROPONER = {
    "type": "OBJECT",
    "properties": {"nombre": {"type": "STRING"}, **_PROPIEDADES_COMPLETADO},
    "required": ["nombre", "descripcion"],
    "propertyOrdering": ["valor", "nombre", "descripcion", "tipoDato", "estructura"],
}

_TIPOS_LEGIBLES = {"texto": "texto", "numero": "número", "fecha": "fecha", "moneda": "moneda", "booleano": "sí o no", "lista": "lista de valores", "grupo": "grupo de campos"}

# Lo que se pide de cada campo: lo mismo al completar los que ya están en la lista que al proponer uno nuevo.
_PIDE_DESCRIPCION = (
    "- `descripcion`: una descripción NUEVA que ayude a encontrar ese campo en CUALQUIER documento de este tipo, no solo en este: qué dato es, en qué "
    "zona de la página suele estar y junto a qué rótulo o encabezado impreso, y su forma (cuántos renglones, formato de la fecha o del número, "
    f"caracteres típicos). En español, una o dos frases, a lo más {MAX_DESCRIPCION_PROPUESTA - 20} caracteres.\n"
)
_PIDE_TIPO = (
    "- `tipoDato`: según lo que se lee en la caja, uno de: texto, numero, fecha, moneda, booleano. `fecha` si es una fecha; `numero` si es solo una "
    "cantidad; `moneda` si es un importe de dinero; `booleano` si es sí o no, o una casilla marcada o vacía. Los códigos, folios, claves y números de "
    "identificación (con letras, o con ceros a la izquierda) son `texto`.\n"
)
_PIDE_ESTRUCTURA = (
    "- `estructura`: un ejemplo INVENTADO del valor, con exactamente la misma forma (largo, separadores y tipo de caracteres) pero con datos ficticios: "
    "sirve para que el extractor sepa qué forma tiene. Nunca uses el valor de este documento ni partes de él.\n"
)
_PIDE_VALOR = "- `valor`: el texto que se lee dentro de la caja, tal cual.\n"
_NO_COPIES = "NO copies el valor ni ningún dato de este documento en particular (nombres, números, fechas concretas)"


def _del_tipo(p: dict) -> str:
    if not p["tipo"]:
        return ""
    return f" del tipo «{p['tipo']}»" + (f" ({p['descripcionTipo']})" if p["descripcionTipo"] else "")


def _contexto_tipo(nombre_tipo: str, descripcion_tipo: str) -> dict:
    return {
        "tipo": " ".join((nombre_tipo or "").split())[:MAX_NOMBRE],
        "descripcionTipo": " ".join((descripcion_tipo or "").split())[:MAX_TEXTO_TIPO],
    }


def _pagina_valida(pagina: object) -> int:
    if isinstance(pagina, bool) or not isinstance(pagina, int) or not 1 <= pagina <= 2000:
        raise ValueError("La página no es válida.")
    return pagina


def _campo_con_caja(c: object) -> dict:
    """Un campo con su recuadro, validado: `{nombre, tipoDato, descripcion, pagina, caja}`. `ValueError` con un texto mostrable si no sirve."""
    if not isinstance(c, dict) or not isinstance(c.get("nombre"), str) or not " ".join(c["nombre"].split()):
        raise ValueError("Cada campo debe traer su nombre.")
    caja = _caja_valida(c.get("caja"))
    if caja is None:
        raise ValueError("El recuadro no es válido.")
    tipo_dato = c.get("tipoDato") if isinstance(c.get("tipoDato"), str) else "texto"
    descripcion = c.get("descripcion") if isinstance(c.get("descripcion"), str) else ""
    return {
        "nombre": " ".join(c["nombre"].split())[:MAX_NOMBRE],
        "tipoDato": tipo_dato if tipo_dato in _TIPOS_LEGIBLES else "texto",
        "descripcion": " ".join(descripcion.split())[:MAX_DESCRIPCION],
        "pagina": _pagina_valida(c.get("pagina", 1)),
        "caja": caja,
    }


def limpiar_pedido_campos(campos: object, nombre_tipo: str, descripcion_tipo: str) -> dict:
    """Varios campos con su recuadro sobre UN documento: `{campos: [...], tipo, descripcionTipo}`, sin repetidos. `ValueError` si no sirven."""
    if not isinstance(campos, list):
        raise ValueError("Los campos deben ser una lista.")
    limpios: list[dict] = []
    vistos: set[str] = set()
    for c in campos:
        campo = _campo_con_caja(c)
        if _clave(campo["nombre"]) in vistos:
            raise ValueError(f"El campo «{campo['nombre']}» viene repetido.")
        vistos.add(_clave(campo["nombre"]))
        limpios.append(campo)
    if not limpios:
        raise ValueError("No hay campos que completar.")
    if len(limpios) > MAX_CAMPOS:
        raise ValueError(f"Son demasiados campos: a lo más {MAX_CAMPOS}.")
    return {"campos": limpios, **_contexto_tipo(nombre_tipo, descripcion_tipo)}


def limpiar_pedido_descripcion(campo: object, pagina: int, caja: object, nombre_tipo: str, descripcion_tipo: str) -> dict:
    """UN campo con su recuadro (la ruta `/describir-campo`): el mismo pedido que arma `limpiar_pedido_campos`, con una sola entrada."""
    if not isinstance(campo, dict):
        raise ValueError("Cada campo debe traer su nombre.")
    return {"campos": [_campo_con_caja({**campo, "pagina": pagina, "caja": caja})], **_contexto_tipo(nombre_tipo, descripcion_tipo)}


def armar_prompt_completar(p: dict) -> str:
    renglones = "\n".join(
        f"- «{c['nombre']}» (tipo de dato actual: {_TIPOS_LEGIBLES[c['tipoDato']]}): página {c['pagina']}, "
        f"caja [ymin, xmin, ymax, xmax] = {c['caja']}. Su descripción actual: " + (f"«{c['descripcion']}»." if c["descripcion"] else "no tiene.")
        for c in p["campos"]
    )
    return (
        "Eres un redactor de esquemas para un extractor de datos de documentos. El extractor lee documentos"
        f"{_del_tipo(p)} y encuentra cada campo SOLO por su nombre, su tipo de dato, su descripción y un ejemplo de su forma.\n"
        "En el documento adjunto, una persona marcó con un recuadro dónde está el VALOR de cada campo de la lista: la página y una caja "
        "[ymin, xmin, ymax, xmax] normalizada de 0 a 1000 respecto a esa página. Lo que está DENTRO de la caja es el campo; lo de fuera no.\n\n"
        "Para CADA campo de la lista devuelve en `campo` su nombre, escrito exactamente igual, y:\n"
        + _PIDE_VALOR
        + _PIDE_DESCRIPCION
        + "  Si la descripción actual dice algo útil sobre qué es el dato, consérvalo.\n"
        + _PIDE_TIPO
        + _PIDE_ESTRUCTURA
        + f"{_NO_COPIES} en `descripcion` ni en `estructura`.\n\n"
        f"Campos:\n{renglones}"
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


def _alfanumerico(texto: str) -> str:
    """Solo letras y dígitos, sin acentos y en mayúsculas: `29/10/1996` y `29-10-1996` quedan iguales."""
    return "".join(ch for ch in _clave(texto) if ch.isalnum()).upper()


def _palabras(texto: str) -> list[str]:
    return [p.upper() for p in re.findall(r"[0-9a-z]+", _clave(texto))]


def _comparte_datos(ejemplo: str, valor: str, generico: str = "") -> bool:
    """¿El ejemplo «inventado» trae datos del valor de ESTE documento? El ejemplo termina en el esquema que se sube a Google (viaja dentro de la
    descripción) y es el mismo para todos los documentos, así que no debe llevar datos de uno: ni el valor entero (aunque cambien los separadores), ni
    un pedazo de él, ni una palabra suya de 4 letras o más, ni 8 caracteres seguidos de un código largo (una CURP con la fecha de nacimiento real).

    Una palabra que ya está en `generico` (la descripción y el nombre del campo, que de todos modos van al esquema) no cuenta: es parte de cómo se
    llama o se describe el dato, no un dato. Así «24 meses» se queda aunque el valor sea «12 meses» (medido con Gemini real el 2026-11-12: sin esta
    excepción se tiraba)."""
    e, v = _alfanumerico(ejemplo), _alfanumerico(valor)
    if not e or not v:
        return False
    if (len(v) >= MIN_VALOR_A_QUITAR and v in e) or (len(e) >= MIN_VALOR_A_QUITAR and e in v):
        return True
    del_ejemplo = set(_palabras(ejemplo))
    genericas = set(_palabras(generico))
    for p in _palabras(valor):
        if len(p) >= MIN_VALOR_A_QUITAR and p in del_ejemplo and p not in genericas:
            return True
        if len(p) >= 10 and any(p[i:i + 8] in e for i in range(len(p) - 7)):
            return True
    return False


def _tipo_deducido(crudo: object) -> str | None:
    if not isinstance(crudo, str):
        return None
    tipo = _SINONIMOS_TIPO.get(" ".join(crudo.lower().split()))
    return tipo if tipo in TIPOS_DEDUCIBLES else None


def _completado(c: dict, pedido: dict) -> dict | None:
    """Lo que Gemini contestó de UN campo, limpio: `{descripcion, tipoDato, valorEstructura, valor}`, o `None` si no trae una descripción usable.
    El tipo solo se propone a un campo sencillo (una lista o un grupo se quedan como están) y un grupo no lleva ejemplo de estructura. Un ejemplo que
    trae datos del documento se tira entero (`_comparte_datos`): es mejor no tener ejemplo que mandar a Google un dato de una persona."""
    valor = c.get("valor") if isinstance(c.get("valor"), str) else ""
    crudo = c.get("descripcion") if isinstance(c.get("descripcion"), str) else ""
    descripcion = _recortar(_sin_valor(" ".join(crudo.split()), valor), MAX_DESCRIPCION_PROPUESTA)
    if not descripcion:
        return None
    tipo_actual = pedido.get("tipoDato", "texto")
    estructura = c.get("estructura") if isinstance(c.get("estructura"), str) else ""
    estructura = " ".join(estructura.split())[:MAX_ESTRUCTURA]
    if estructura and _comparte_datos(estructura, valor, f"{descripcion} {pedido.get('nombre', '')}"):
        logger.info("Completar campos: se descartó un ejemplo de estructura que traía datos del documento.")
        estructura = ""
    return {
        "descripcion": descripcion,
        "tipoDato": _tipo_deducido(c.get("tipoDato")) if tipo_actual not in ("lista", "grupo") else None,
        "valorEstructura": "" if tipo_actual == "grupo" else estructura,
        "valor": valor[:MAX_VALOR],
    }


def leer_completados(texto: str, pedidos: list[dict]) -> list[dict]:
    """Por cada campo pedido (en su orden), lo que se pudo leer: `[{nombre, descripcion, tipoDato, valorEstructura, valor}]`, con el nombre TAL COMO SE
    PIDIÓ (el modelo puede escribirlo distinto). Un campo que no vino o vino sin descripción se omite: el front lo deja como estaba. `ValueError` si
    la respuesta no es el JSON pedido o no sirve ningún campo."""
    datos = json.loads(texto)
    if not isinstance(datos, dict) or not isinstance(datos.get("campos"), list):
        raise ValueError("la respuesta no trae `campos`")
    contestados = [c for c in datos["campos"] if isinstance(c, dict)]
    por_clave: dict[str, dict] = {}
    for c in contestados:
        if isinstance(c.get("campo"), str):
            por_clave.setdefault(_clave(c["campo"]), c)
    salida: list[dict] = []
    for pedido in pedidos:
        c = por_clave.get(_clave(pedido["nombre"]))
        if c is None and len(pedidos) == 1 and len(contestados) == 1:
            c = contestados[0]  # con UN campo pedido no hay a cuál más pertenecer, aunque el modelo le cambie el nombre
        hecho = _completado(c, pedido) if c is not None else None
        if hecho is not None:
            salida.append({"nombre": pedido["nombre"], **hecho})
    if not salida:
        raise ValueError("no vino ningún campo usable")
    return salida


async def completar(contenido: bytes, mime: str, pedido: dict) -> dict:
    """La descripción, el tipo y un ejemplo de la forma de cada campo, a partir de su recuadro. Devuelve `{campos: [...], modelo}` o levanta
    `ErrorUbicar`. `pedido` ya pasó por `limpiar_pedido_campos`; `mime` es uno de `gemini.MIME_PARA_GEMINI` (lo valida la ruta). Nada se guarda."""
    texto, fin = await _preguntar(
        contenido, mime, armar_prompt_completar(pedido), _ESQUEMA_COMPLETAR, "Completar campos",
        MENSAJE_COMPLETAR_FALLO, MENSAJE_COMPLETAR_NO_DISPONIBLE, MAX_TOKENS_COMPLETAR,
    )
    try:
        campos = leer_completados(texto, pedido["campos"])
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Completar campos: no se entendió la respuesta de Gemini (fin %s, modelo %s): %s", fin or "?", GEMINI_MODELO, exc)
        raise ErrorUbicar(MENSAJE_COMPLETAR_FALLO, 502) from exc
    return {"campos": campos, "modelo": GEMINI_MODELO}


async def describir(contenido: bytes, mime: str, pedido: dict) -> dict:
    """UN campo, el que se está editando (la ruta `/describir-campo`): `{descripcion, tipoDato, valorEstructura, valor, modelo}` o `ErrorUbicar`. Es
    `completar` con un solo campo y los mensajes de siempre. `pedido` ya pasó por `limpiar_pedido_descripcion`. Nada se guarda."""
    texto, fin = await _preguntar(
        contenido, mime, armar_prompt_completar(pedido), _ESQUEMA_COMPLETAR, "Describir campo", MENSAJE_DESCRIBIR_FALLO, MENSAJE_DESCRIBIR_NO_DISPONIBLE
    )
    try:
        campo = leer_completados(texto, pedido["campos"])[0]
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Describir campo: no se entendió la respuesta de Gemini (fin %s, modelo %s): %s", fin or "?", GEMINI_MODELO, exc)
        raise ErrorUbicar(MENSAJE_DESCRIBIR_FALLO, 502) from exc
    return {
        "descripcion": campo["descripcion"],
        "tipoDato": campo["tipoDato"],
        "valorEstructura": campo["valorEstructura"],
        "valor": campo["valor"],
        "modelo": GEMINI_MODELO,
    }


# ── Proponer un campo con un recuadro dibujado ──
def limpiar_pedido_proponer(pagina: int, caja: object, nombre_tipo: str, descripcion_tipo: str, existentes: object) -> dict:
    """El recuadro dibujado y los nombres que ya existen: `{pagina, caja, existentes, tipo, descripcionTipo}`. `ValueError` si no sirven."""
    caja_ok = _caja_valida(caja)
    if caja_ok is None:
        raise ValueError("El recuadro no es válido.")
    if not isinstance(existentes, list) or not all(isinstance(n, str) for n in existentes):
        raise ValueError("Los campos que ya existen deben ser una lista de nombres.")
    nombres: list[str] = []
    vistos: set[str] = set()
    for n in existentes:
        limpio = " ".join(n.split())[:MAX_NOMBRE]
        if limpio and _clave(limpio) not in vistos:
            vistos.add(_clave(limpio))
            nombres.append(limpio)
    if len(nombres) > MAX_EXISTENTES:
        raise ValueError(f"Son demasiados campos existentes: a lo más {MAX_EXISTENTES}.")
    return {"pagina": _pagina_valida(pagina), "caja": caja_ok, "existentes": nombres, **_contexto_tipo(nombre_tipo, descripcion_tipo)}


def armar_prompt_proponer(p: dict) -> str:
    existentes = ", ".join(f"«{n}»" for n in p["existentes"])
    return (
        "Eres un diseñador de esquemas para un extractor de datos de documentos. El extractor lee documentos"
        f"{_del_tipo(p)} y encuentra cada campo SOLO por su nombre, su tipo de dato, su descripción y un ejemplo de su forma.\n"
        f"En el documento adjunto, una persona marcó un recuadro: página {p['pagina']}, caja [ymin, xmin, ymax, xmax] = {p['caja']}, normalizada de 0 "
        "a 1000 respecto a esa página. Lo que está DENTRO de la caja es el valor de un campo que quiere extraer de todos los documentos de este tipo; "
        "lo de fuera no.\n\n"
        "Propón ese campo. Primero lee lo que está escrito DENTRO de la caja: ese dato, y no otro del documento, es el campo.\n"
        + _PIDE_VALOR
        + "- `nombre`: corto y claro, en español, como lo diría una persona (por ejemplo «Fecha de nacimiento» o «Número de folio»); si junto al valor hay "
        "un rótulo impreso, úsalo. Describe SIEMPRE lo que está dentro de la caja, aunque ya exista."
        + (
            f" Estos campos ya existen: {existentes}. Si lo que está dentro de la caja es uno de ellos, devuelve ese nombre escrito igual; si no, uno "
            "que no se confunda con ellos."
            if existentes
            else ""
        )
        + "\n"
        + _PIDE_DESCRIPCION
        + _PIDE_TIPO
        + _PIDE_ESTRUCTURA
        + f"{_NO_COPIES} en `nombre`, `descripcion` ni `estructura`."
    )


def leer_propuesta(texto: str) -> dict:
    """`{nombre, descripcion, tipoDato, valorEstructura, valor}` de la respuesta. `ValueError` si no es el JSON pedido o el nombre o la descripción
    vienen vacíos. Si el modelo metió el valor en el nombre, se quita (igual que en la descripción)."""
    datos = json.loads(texto)
    if not isinstance(datos, dict) or not isinstance(datos.get("nombre"), str):
        raise ValueError("la respuesta no trae `nombre`")
    valor = datos.get("valor") if isinstance(datos.get("valor"), str) else ""
    nombre = _sin_valor(" ".join(datos["nombre"].replace("«", " ").replace("»", " ").split()), valor)[:MAX_NOMBRE].strip(" .,;:")
    hecho = _completado(datos, {})
    if not nombre or hecho is None:
        raise ValueError("el nombre o la descripción vinieron vacíos")
    return {"nombre": nombre, **hecho}


async def proponer(contenido: bytes, mime: str, pedido: dict) -> dict:
    """Un campo NUEVO a partir de un recuadro dibujado: `{nombre, descripcion, tipoDato, valorEstructura, valor, modelo}` o `ErrorUbicar`. `pedido` ya
    pasó por `limpiar_pedido_proponer`. Nada se guarda: el front abre el formulario con esto y la persona decide."""
    texto, fin = await _preguntar(
        contenido, mime, armar_prompt_proponer(pedido), _ESQUEMA_PROPONER, "Proponer campo", MENSAJE_PROPONER_FALLO, MENSAJE_PROPONER_NO_DISPONIBLE
    )
    try:
        propuesta = leer_propuesta(texto)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Proponer campo: no se entendió la respuesta de Gemini (fin %s, modelo %s): %s", fin or "?", GEMINI_MODELO, exc)
        raise ErrorUbicar(MENSAJE_PROPONER_FALLO, 502) from exc
    return {**propuesta, "modelo": GEMINI_MODELO}
