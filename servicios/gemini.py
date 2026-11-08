"""Gemini por Vertex AI: «Sugerir descripción» de un tipo documental (Moibe, 2026-11-05).

Es lo ÚNICO del sistema que usa un modelo de lenguaje de texto; todo lo demás es Document AI. A partir del nombre de un tipo documental y
de los nombres de los otros tipos de la organización, propone la `descripcion` del tipo: la etiqueta con la que el CLASIFICADOR reconoce
ese tipo entre los demás (qué documento es y cómo distinguirlo). La persona la revisa y la edita; aquí no se guarda nada.

## Cómo se llama

- La MISMA credencial de Google que ya usa el back para Document AI (`servicios.ia._token`, cacheada): no hay proveedor ni llave nueva.
- A Vertex AI: `https://aiplatform.googleapis.com/v1/projects/{DOCAI_PROJECT_ID}/locations/{GEMINI_REGION}/publishers/google/models/
  {GEMINI_MODELO}:generateContent` (`global` es la región donde respondieron los modelos 3.x al probarlos, 2026-11-05).
- Con la cabecera `x-goog-user-project`: la cuenta de servicio pertenece a OTRO proyecto de Google (donde Vertex AI no está habilitada) y esa
  cabecera hace que se use, y se cobre, el de Document AI. Sin ella la consulta de modelos falló con 403; las llamadas que funcionaron la llevaban.
- El modelo es configurable (`GEMINI_MODELO`): los nombres caducan rápido (los Gemini 2.5 ya no existen para este proyecto).

## Lo que hay que respetar de estos modelos

- Son modelos que «piensan»: el razonamiento se cuenta contra `maxOutputTokens`. Con 200 la respuesta salió cortada («El acta constitutiva es»);
  con 1500 sale completa (en la prueba, ~100 tokens de salida). El tope es holgado a propósito.
- Un modelo barato afirma con seguridad detalles que no sabe (en la prueba escribió «incluye obligatoriamente el logotipo»). El prompt pide no
  inventar, pero lo que de verdad lo corrige es que la persona revise: la pantalla lo dice y nada se guarda solo.

## Errores

Todos los fallos levantan `ErrorSugerencia` con un `status_code` y un mensaje que SÍ se puede mostrar. El detalle de Google (que puede traer ids
de proyecto) va al log y nunca a la respuesta:

- 503 si el servidor no tiene proyecto o credencial, o Google contesta 401/403/404 (API no habilitada, sin permiso, modelo retirado);
- 429 si Google contesta 429 (cuota);
- 502 si Google falla, tarda más de `GEMINI_TIMEOUT` o devuelve una respuesta vacía o bloqueada.
"""

import base64
import logging
import re

import httpx

from config import DOCAI_PROJECT_ID, GEMINI_MODELO, GEMINI_REGION, GEMINI_TIMEOUT
from servicios.ia import _token  # misma credencial cacheada que Document AI

logger = logging.getLogger(__name__)

#: Cuántos de los OTROS tipos de la organización se le dicen al modelo, y cuánto de cada nombre. Una organización con cientos de tipos no debe
#: volver enorme el prompt (y su costo): con los primeros basta para que distinga.
MAX_OTROS = 30
MAX_NOMBRE = 80
#: Largo máximo de lo que se devuelve. Una descripción para el clasificador son un par de frases; esto es un techo, no una meta.
MAX_DESCRIPCION = 700
#: Los tokens que se le dejan al modelo para razonar Y contestar. Ver «Lo que hay que respetar» arriba: con pocos, la respuesta sale cortada.
MAX_TOKENS_SALIDA = 1500

#: «Sugerir descripción» con DOCUMENTOS de ejemplo (2026-11-07, Moibe: «va»): cuántos como máximo, de qué formatos (los que Gemini lee: PDF, JPG y PNG; el TIFF que sí
#: acepta Document AI, no) y cuánto pueden pesar en total (van EN la petición, en base64, y la petición a Gemini tiene tope).
MAX_DOCUMENTOS_DE_EJEMPLO = 3
MIME_PARA_GEMINI = ("application/pdf", "image/jpeg", "image/png")
MAX_BYTES_DOCUMENTOS = 18 * 1024 * 1024

MENSAJE_NO_DISPONIBLE = "La sugerencia con IA no está disponible en este servidor. Escribe la descripción a mano y avisa al equipo técnico."
MENSAJE_FALLO = "No se pudo generar la sugerencia. Intenta de nuevo o escribe la descripción a mano."
MENSAJE_VACIA = "El modelo no devolvió una descripción. Intenta de nuevo o escríbela a mano."
MENSAJE_CUOTA = "Hay muchas solicitudes a la vez. Intenta de nuevo en un momento."


class ErrorSugerencia(Exception):
    """No se pudo sugerir. `status_code` es el que debe contestar la ruta; `str(exc)` es un texto que sí se puede mostrar."""

    def __init__(self, mensaje: str, status_code: int):
        super().__init__(mensaje)
        self.status_code = status_code


def _limpio(texto: str, tope: int) -> str:
    """Un nombre dentro del prompt: sin saltos de línea ni las comillas angulares con que se acotan, y con tope."""
    return " ".join(str(texto).replace("«", " ").replace("»", " ").split())[:tope].strip()


def armar_prompt(nombre: str, otros: list[str], documentos: int = 0) -> str:
    """El texto que se le manda al modelo. `otros` son los nombres de los demás tipos de la organización (ya sin el propio ni los archivados).
    `documentos` es cuántos documentos de ejemplo van adjuntos a la petición (0 = solo el nombre)."""
    propio = _limpio(nombre, 200)
    vistos: list[str] = []
    # Sin repetidos y sin distinguir mayúsculas (la misma regla de los nombres únicos), y sin el propio nombre: no es «otro» de sí mismo.
    claves = {propio.casefold()}
    for o in otros:
        limpio = _limpio(o, MAX_NOMBRE)
        if limpio and limpio.casefold() not in claves:
            vistos.append(limpio)
            claves.add(limpio.casefold())
        if len(vistos) >= MAX_OTROS:
            break
    partes = [
        "Escribe la descripción de un tipo documental para un clasificador de documentos de una empresa.",
        f"Tipo: «{propio}».",
    ]
    if vistos:
        partes.append(f"Otros tipos que ya existen en la organización: {', '.join(vistos)}.")
    if documentos > 0:
        # Con documentos a la vista el modelo puede describir lo que de verdad traen, pero la descripción se GUARDA y se le muestra a todo el equipo: no debe copiar
        # los datos de las personas de los ejemplos (nombres, números, fechas, domicilios). Se lo pide, y la pantalla pide revisarla.
        partes.append(
            f"Se adjunta{'n' if documentos > 1 else ''} {documentos} documento{'s' if documentos > 1 else ''} de ejemplo de este tipo: "
            "descríbelo a partir de lo que ves en " + ("ellos" if documentos > 1 else "él") + " (qué documento es, quién lo emite, qué secciones o clases de dato trae) "
            "y de lo que tienen en común. NUNCA copies datos concretos de las personas de los ejemplos (nombres, números de documento, fechas, domicilios, "
            "firmas): di qué clase de dato hay, no cuál es."
        )
    partes.append(
        "Di qué documento es y cómo distinguirlo de los otros (qué trae impreso, quién lo emite). Si es de un país en particular, dilo. "
        "Máximo 3 frases, en español, sin viñetas, sin encabezados y sin comillas. "
        "No inventes datos que no sepas con seguridad: si el nombre " + ("y los documentos no te dicen" if documentos > 0 else "no te dice") + " con claridad de qué documento se trata, "
        "descríbelo en términos generales y no supongas detalles."
    )
    return " ".join(partes)


def limpiar(texto: str) -> str:
    """Lo que dijo el modelo, listo para ponerse en la caja de la descripción: un solo párrafo, sin rótulo, viñeta, negritas ni comillas."""
    # Las negritas primero: con «**Descripción:**» el rótulo no se reconocería si los asteriscos siguieran ahí.
    t = (texto or "").replace("**", "").replace("__", "").strip()
    t = re.sub(r"^\s*(descripci[oó]n|sugerencia)\s*:\s*", "", t, flags=re.IGNORECASE)
    t = re.sub(r"^[-*•]\s+", "", t)
    t = " ".join(t.split())
    pares = {('"', '"'), ("«", "»"), ("“", "”"), ("'", "'")}
    while len(t) > 1 and (t[0], t[-1]) in pares:
        t = t[1:-1].strip()
    if len(t) > MAX_DESCRIPCION:
        corte = t.rfind(".", 0, MAX_DESCRIPCION)
        t = t[: corte + 1] if corte > MAX_DESCRIPCION // 2 else t[:MAX_DESCRIPCION].rsplit(" ", 1)[0]
    return t.strip()


def _url() -> str:
    host = "aiplatform.googleapis.com" if GEMINI_REGION == "global" else f"{GEMINI_REGION}-aiplatform.googleapis.com"
    return f"https://{host}/v1/projects/{DOCAI_PROJECT_ID}/locations/{GEMINI_REGION}/publishers/google/models/{GEMINI_MODELO}:generateContent"


async def _llamar(url: str, headers: dict, cuerpo: dict, timeout: float) -> tuple[int, object]:
    """El POST a Vertex AI, separado para poder sustituirlo en las pruebas. Devuelve `(estado HTTP, cuerpo JSON o texto)`."""
    async with httpx.AsyncClient() as cliente:
        r = await cliente.post(url, headers=headers, json=cuerpo, timeout=timeout)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:300]


def _texto_de(respuesta: object) -> tuple[str, str]:
    """`(texto, motivo de fin)` de una respuesta de `generateContent`; vacío si no hay candidatos o el modelo se negó."""
    if not isinstance(respuesta, dict):
        return "", ""
    candidatos = respuesta.get("candidates") or []
    if not candidatos or not isinstance(candidatos[0], dict):
        return "", str((respuesta.get("promptFeedback") or {}).get("blockReason", ""))
    c = candidatos[0]
    partes = (c.get("content") or {}).get("parts") or []
    # Las partes de «pensamiento» (`thought: true`), si el modelo las devolviera, no son la respuesta.
    texto = " ".join(p.get("text", "") for p in partes if isinstance(p, dict) and not p.get("thought"))
    return texto, str(c.get("finishReason", ""))


async def sugerir_descripcion(nombre: str, otros: list[str], documentos: list[tuple[bytes, str]] | None = None) -> dict:
    """Propone la descripción de un tipo documental. Devuelve `{descripcion, modelo}` o levanta `ErrorSugerencia`.

    `documentos` son documentos de ejemplo `(bytes, mime)` que viajan EN la petición como `inlineData` (a lo más `MAX_DOCUMENTOS_DE_EJEMPLO`, de los formatos de
    `MIME_PARA_GEMINI`; lo valida la ruta). No se guardan: salen a Google con esta llamada y se descartan."""
    if not DOCAI_PROJECT_ID:
        logger.warning("Sugerir descripción: falta DOCAI_PROJECT_ID en el .env; no se llama a Gemini.")
        raise ErrorSugerencia(MENSAJE_NO_DISPONIBLE, 503)
    try:
        token = _token()
    except Exception as exc:  # noqa: BLE001 — sin credencial o sin red a Google: el servidor no puede, no es culpa de la persona
        logger.warning("Sugerir descripción: no se pudo obtener la credencial de Google: %s", exc)
        raise ErrorSugerencia(MENSAJE_NO_DISPONIBLE, 503) from exc

    adjuntos = [{"inlineData": {"mimeType": mime, "data": base64.b64encode(contenido).decode("ascii")}} for contenido, mime in (documentos or [])]
    cuerpo = {
        # Los documentos van ANTES del texto: así lo recomienda Gemini para entradas multimodales.
        "contents": [{"role": "user", "parts": [*adjuntos, {"text": armar_prompt(nombre, otros, len(adjuntos))}]}],
        "generationConfig": {"maxOutputTokens": MAX_TOKENS_SALIDA, "temperature": 0.3},
    }
    cabeceras = {"Authorization": f"Bearer {token}", "x-goog-user-project": DOCAI_PROJECT_ID}
    try:
        estado, respuesta = await _llamar(_url(), cabeceras, cuerpo, GEMINI_TIMEOUT)
    except httpx.TimeoutException as exc:
        logger.warning("Sugerir descripción: Gemini no contestó en %s s (modelo %s).", GEMINI_TIMEOUT, GEMINI_MODELO)
        raise ErrorSugerencia(MENSAJE_FALLO, 502) from exc
    except httpx.HTTPError as exc:
        logger.warning("Sugerir descripción: error de red al llamar a Gemini: %s", type(exc).__name__)
        raise ErrorSugerencia(MENSAJE_FALLO, 502) from exc

    if estado != 200:
        error = respuesta.get("error", {}) if isinstance(respuesta, dict) else {}
        logger.warning(
            "Sugerir descripción: Gemini respondió %s (%s) con el modelo %s en %s: %s",
            estado, error.get("status", ""), GEMINI_MODELO, GEMINI_REGION, str(error.get("message") or respuesta)[:300],
        )
        if estado == 429:
            raise ErrorSugerencia(MENSAJE_CUOTA, 429)
        if estado in (401, 403, 404):
            raise ErrorSugerencia(MENSAJE_NO_DISPONIBLE, 503)
        raise ErrorSugerencia(MENSAJE_FALLO, 502)

    texto, fin = _texto_de(respuesta)
    descripcion = limpiar(texto)
    if fin == "MAX_TOKENS":
        # Se le acabaron los tokens a media respuesta (con 1500 casi no pasa): no se ofrece una frase a medias, solo lo que cierra en un punto.
        logger.warning("Sugerir descripción: la respuesta de Gemini se cortó por MAX_TOKENS (modelo %s).", GEMINI_MODELO)
        corte = descripcion.rfind(".")
        descripcion = descripcion[: corte + 1] if corte > 0 else ""
    if not descripcion:
        logger.warning("Sugerir descripción: Gemini contestó sin texto (fin: %s, modelo %s).", fin or "?", GEMINI_MODELO)
        raise ErrorSugerencia(MENSAJE_VACIA, 502)
    return {"descripcion": descripcion, "modelo": GEMINI_MODELO}
