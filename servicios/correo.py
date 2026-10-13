"""Envío de correo, con una costura para poder trabajar sin servidor de correo.

## Por qué existe la costura

El canal real —qué relay SMTP, desde qué remitente, y si la red de CSI deja
salir el puerto— depende de Soporte TI y es lo que más tarda. Esperar a eso
para empezar a construir la recuperación de contraseña habría dejado parado
todo el Sprint 1. Así que el envío tiene dos modos y el resto del código no
sabe cuál está activo:

    CORREO_MODO=archivo  (default)  deja el correo en disco; no toca la red
    CORREO_MODO=smtp                lo manda de verdad, por un relay SMTP
    CORREO_MODO=api                 lo manda por el servicio HTTP de notificaciones
                                    de ART Broker (la respuesta de Ray, 2026-10-13)
    CORREO_MODO=silencio            lo descarta; solo para pruebas de carga

En `archivo`, cada correo se escribe dentro del almacén, en `correos/`, con su
fecha y destinatario en el nombre. Las pruebas lo leen de ahí y comprueban que
el enlace sea el correcto, lo cual es MÁS fiable que un buzón real: no depende
de la red ni de que alguien abra Outlook.

El día que TI conteste, cambia una variable de entorno y nada más.

## Lo que este módulo NO hace

No arma los textos. Las plantillas viven con su historia (bienvenida, enlace
de activación, recuperación), porque cada una tiene su propio asunto, su
destinatario y sus datos. Aquí solo está el transporte.

## El modo `api`

Es el servicio de ART Broker: `POST /OAuth/AuthToken` con usuario y contraseña
da un token, y `POST /II_1_General_Notifications/Emailsending` con ese token
manda el correo (`mailBody` es HTML). Lo que hay que saber, porque el contrato
que nos pasaron es corto:

  · El REMITENTE lo decide el servicio; aquí no hay "from". Hay que pedirles uno
    propio de NexusDoc: un "restablece tu contraseña" que llega de otra marca
    confunde y parece phishing.
  · Las credenciales son de ART Broker (compartidas): conviene pedir unas
    dedicadas. Viven solo en el `.env` del servidor.
  · Solo se usa el envío SIN adjuntos. El de adjuntos recibe rutas de red del
    servidor de ART; aquí no se usa ni se va a usar.
  · El token se guarda en memoria hasta poco antes de su `exp` (si no se puede
    leer, 5 minutos); un 401 o 403 al enviar lo renueva UNA vez. Un timeout o un
    5xx NO se reintentan: el servicio pudo haberlo aceptado y se duplicaría.
  · El servicio puede contestar HTTP 200 con `error: true`: eso es una falla.
  · Solo https (el usuario, la contraseña y el token viajan por ahí); http se
    admite únicamente para localhost, que es como lo prueba `verificar_correo.py`.

## Secretos

Ni el token ni la contraseña temporal se escriben en el log: van en el CUERPO
del correo, y el cuerpo nunca se registra. Lo que se registra es a quién se le
mandó y con qué asunto, que es lo que sirve para diagnosticar. Lo mismo con el
usuario, la contraseña y el token de la API: no salen en el log ni en los
mensajes de error, aunque el servicio los repitiera en su respuesta.
"""

import base64
import json
import logging
import re
import smtplib
import threading
import time
import unicodedata
from datetime import datetime, timezone
from email.message import EmailMessage
from html import escape as _escapar_html
from pathlib import Path
from urllib.parse import urlparse

import httpx

import config

logger = logging.getLogger(__name__)

MODO_ARCHIVO = "archivo"
MODO_SMTP = "smtp"
MODO_API = "api"
MODO_SILENCIO = "silencio"
MODOS = frozenset({MODO_ARCHIVO, MODO_SMTP, MODO_API, MODO_SILENCIO})

#: Las rutas del servicio de notificaciones (modo `api`). Fijas: entre ambientes
#: solo cambia el host.
RUTA_TOKEN = "/OAuth/AuthToken"
RUTA_ENVIO = "/II_1_General_Notifications/Emailsending"
#: El token se renueva así de antes de que venza, y dura esto si no se le puede
#: leer el vencimiento.
MARGEN_TOKEN_S = 60
VIGENCIA_TOKEN_DESCONOCIDA_S = 300
#: Lo que no puede llevar el destinatario en el modo `api`: `mailRecipients` es UNA
#: cadena y el servicio podría tomar `;` o `,` como separador de varios destinatarios.
NO_PERMITIDO_EN_DESTINATARIO = ';,<>"'

#: Subcarpeta del almacén donde caen los correos en modo `archivo`.
CARPETA = "correos"


class ErrorCorreo(RuntimeError):
    """No se pudo enviar. Quien llama decide si lo reintenta o lo reporta."""


def modo() -> str:
    valor = (config.CORREO_MODO or MODO_ARCHIVO).strip().lower()
    return valor if valor in MODOS else MODO_ARCHIVO


def esta_configurado() -> bool:
    """¿Se puede mandar correo en este ambiente? En `archivo` siempre sí; en
    `smtp` hace falta al menos el host y el remitente; en `api`, la URL, el
    usuario y la contraseña."""
    if modo() == MODO_API:
        return bool(config.CORREO_API_URL and config.CORREO_API_USUARIO and config.CORREO_API_CONTRASENA)
    if modo() != MODO_SMTP:
        return True
    return bool(config.CORREO_SMTP_HOST and config.CORREO_REMITENTE)


def _remitente() -> str:
    return config.CORREO_REMITENTE or "nexusdoc@localhost"


def _nombre_de_archivo(destino: str, asunto: str) -> str:
    """Un nombre que se pueda leer de un vistazo y no rompa en Windows."""
    marca = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S-%f")
    limpio = unicodedata.normalize("NFKD", f"{destino}-{asunto}")
    limpio = limpio.encode("ascii", "ignore").decode("ascii")
    limpio = re.sub(r"[^A-Za-z0-9._-]+", "-", limpio).strip("-")[:80]
    return f"{marca}-{limpio or 'correo'}.eml"


def _armar(destino: str, asunto: str, texto: str, html: str | None) -> EmailMessage:
    mensaje = EmailMessage()
    mensaje["From"] = _remitente()
    mensaje["To"] = destino
    mensaje["Subject"] = asunto
    mensaje["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")
    mensaje.set_content(texto)
    if html:
        mensaje.add_alternative(html, subtype="html")
    return mensaje


def _carpeta_archivo() -> Path:
    raiz = Path(config.ALMACEN_RUTA) if config.ALMACEN_RUTA else Path(".")
    destino = raiz / CARPETA
    destino.mkdir(parents=True, exist_ok=True)
    return destino


def _enviar_a_archivo(mensaje: EmailMessage, destino: str, asunto: str) -> str:
    ruta = _carpeta_archivo() / _nombre_de_archivo(destino, asunto)
    try:
        ruta.write_bytes(bytes(mensaje))
    except OSError as exc:
        raise ErrorCorreo(f"No se pudo escribir el correo en {ruta}: {exc}") from exc
    return str(ruta)


def _enviar_por_smtp(mensaje: EmailMessage) -> str:
    host, puerto = config.CORREO_SMTP_HOST, config.CORREO_SMTP_PUERTO
    if not host:
        raise ErrorCorreo("Falta CORREO_SMTP_HOST.")
    try:
        if config.CORREO_SMTP_SSL:
            cliente = smtplib.SMTP_SSL(host, puerto, timeout=config.CORREO_TIMEOUT)
        else:
            cliente = smtplib.SMTP(host, puerto, timeout=config.CORREO_TIMEOUT)
        with cliente:
            if config.CORREO_SMTP_TLS and not config.CORREO_SMTP_SSL:
                cliente.starttls()
            if config.CORREO_SMTP_USUARIO:
                cliente.login(config.CORREO_SMTP_USUARIO, config.CORREO_SMTP_CONTRASENA)
            cliente.send_message(mensaje)
    except (smtplib.SMTPException, OSError) as exc:
        # El mensaje de error puede traer el host, pero nunca el cuerpo.
        raise ErrorCorreo(f"No se pudo enviar por SMTP a través de {host}:{puerto}: {exc}") from exc
    return f"smtp://{host}:{puerto}"


# ── Modo `api`: el servicio de notificaciones de ART Broker ─────────────────

_candado_token = threading.Lock()
#: El token vigente y cuándo vence (segundos desde la época). Solo en memoria.
_token: dict[str, object] = {"valor": None, "vence": 0.0}


def _sin_secretos(texto: str, cuerpo_correo: str = "") -> str:
    """Quita del texto lo que NO debe llegar a un log ni a un mensaje de error, por si el servicio
    lo repitiera en su respuesta: las credenciales, el token, el cuerpo del correo y cualquier
    enlace (los correos llevan enlaces de un solo uso)."""
    for secreto in (config.CORREO_API_USUARIO, config.CORREO_API_CONTRASENA, str(_token["valor"] or ""), cuerpo_correo):
        if secreto:
            texto = texto.replace(secreto, "***")
    return " ".join("<enlace>" if palabra.startswith(("http://", "https://")) else palabra for palabra in texto.split())


def _respuesta_json(respuesta: httpx.Response) -> object:
    try:
        return respuesta.json()
    except ValueError:
        return None


def _mensaje_del_servicio(cuerpo: object, cuerpo_correo: str = "") -> str:
    """El `msgResponse` del servicio, acortado y sin secretos, para el mensaje de error."""
    if not isinstance(cuerpo, dict):
        return ""
    return _sin_secretos(str(cuerpo.get("msgResponse") or ""), cuerpo_correo)[:200]


def _base_de_la_api() -> str:
    url = config.CORREO_API_URL
    if not url:
        raise ErrorCorreo("Falta CORREO_API_URL.")
    if not (config.CORREO_API_USUARIO and config.CORREO_API_CONTRASENA):
        raise ErrorCorreo("Faltan CORREO_API_USUARIO y CORREO_API_CONTRASENA.")
    partes = urlparse(url)
    if partes.scheme != "https" and partes.hostname not in {"localhost", "127.0.0.1"}:
        raise ErrorCorreo("CORREO_API_URL debe ser https: ahí viajan el usuario, la contraseña y el token.")
    return url


def _vencimiento_del_token(token: str) -> float:
    """Cuándo vence el token: su claim `exp` (sin verificar la firma, que no es nuestra) o, si no
    se le puede leer, dentro de `VIGENCIA_TOKEN_DESCONOCIDA_S`."""
    ahora = time.time()
    try:
        carga = token.split(".")[1]
        exp = float(json.loads(base64.urlsafe_b64decode(carga + "=" * (-len(carga) % 4)))["exp"])
    except (IndexError, ValueError, KeyError, TypeError):
        return ahora + VIGENCIA_TOKEN_DESCONOCIDA_S
    return exp


def _obtener_token(cliente: httpx.Client, base: str, forzar: bool = False) -> str:
    """El token vigente; si no hay (o `forzar`), lo pide. Un solo hilo lo pide a la vez: los demás
    esperan y reusan el suyo."""
    with _candado_token:
        if not forzar and _token["valor"] and time.time() < float(_token["vence"]) - MARGEN_TOKEN_S:
            return str(_token["valor"])
        respuesta = cliente.post(
            base + RUTA_TOKEN, json={"usr": config.CORREO_API_USUARIO, "psd": config.CORREO_API_CONTRASENA}
        )
        cuerpo = _respuesta_json(respuesta)
        resultados = cuerpo.get("results") if isinstance(cuerpo, dict) else None
        token = resultados.get("accessToken") if isinstance(resultados, dict) else None
        if not 200 <= respuesta.status_code < 300 or (isinstance(cuerpo, dict) and cuerpo.get("error")) or not token:
            raise ErrorCorreo(
                f"El servicio de correo no entregó un token (HTTP {respuesta.status_code}): {_mensaje_del_servicio(cuerpo)}"
            )
        _token["valor"], _token["vence"] = str(token), _vencimiento_del_token(str(token))
        return str(token)


def _texto_como_html(texto: str) -> str:
    """El cuerpo de la API es HTML; si solo hay texto plano, se escapa y se respetan los saltos."""
    return "<p>" + _escapar_html(texto).replace(chr(10), "<br>") + "</p>"


def _enviar_por_api(destino: str, asunto: str, texto: str, html: str | None) -> str:
    if any(c in destino for c in NO_PERMITIDO_EN_DESTINATARIO) or any(c.isspace() for c in destino):
        raise ErrorCorreo(f"Destinatario inválido para la API de correo: {destino!r}")
    base = _base_de_la_api()
    host = urlparse(base).netloc
    carga = {
        "mailRecipients": destino,
        "mailCc": "",
        "mailBcc": "",
        "mailSubject": asunto,
        "mailBody": html or _texto_como_html(texto),
    }
    try:
        with httpx.Client(timeout=config.CORREO_TIMEOUT, follow_redirects=False) as cliente:
            for intento in (1, 2):
                token = _obtener_token(cliente, base, forzar=intento == 2)
                respuesta = cliente.post(base + RUTA_ENVIO, json=carga, headers={"Authorization": f"Bearer {token}"})
                if respuesta.status_code in (401, 403) and intento == 1:
                    continue  # el token pudo vencer: se pide otro UNA vez
                break
    except httpx.HTTPError as exc:
        # Solo el tipo de la excepción: nunca el cuerpo ni las cabeceras.
        raise ErrorCorreo(f"No se pudo enviar por la API de correo ({host}): {type(exc).__name__}") from exc
    cuerpo = _respuesta_json(respuesta)
    if not 200 <= respuesta.status_code < 300 or not isinstance(cuerpo, dict) or cuerpo.get("error"):
        raise ErrorCorreo(
            f"La API de correo ({host}) rechazó el envío (HTTP {respuesta.status_code}): "
            f"{_mensaje_del_servicio(cuerpo, str(carga['mailBody']))}"
        )
    return f"api://{host}"


def enviar(destino: str, asunto: str, texto: str, html: str | None = None) -> str:
    """Manda el correo y devuelve a dónde fue a parar (ruta del archivo o el
    servidor usado), que es lo que las pruebas revisan.

    Levanta `ErrorCorreo` si no se pudo. Quien llama decide qué hacer: en la
    recuperación de contraseña, por ejemplo, el mensaje al usuario es el mismo
    exista o no la cuenta, así que un fallo de envío NO debe cambiar lo que ve."""
    if not destino or "@" not in destino:
        raise ErrorCorreo(f"Destinatario inválido: {destino!r}")
    actual = modo()
    if actual == MODO_SILENCIO:
        logger.info("Correo DESCARTADO (modo silencio) para %s · %s", destino, asunto)
        return "silencio"
    if actual == MODO_API:
        # La API toma UN cuerpo HTML: se manda el HTML (o el texto, escapado, si no hay).
        donde = _enviar_por_api(destino, asunto, texto, html)
    else:
        mensaje = _armar(destino, asunto, texto, html)
        donde = _enviar_por_smtp(mensaje) if actual == MODO_SMTP else _enviar_a_archivo(mensaje, destino, asunto)
    # Nunca el cuerpo: ahí van el token y la contraseña temporal.
    logger.info("Correo enviado a %s · %s · %s", destino, asunto, donde)
    return donde
