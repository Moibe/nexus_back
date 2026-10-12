"""Envío de correo, con una costura para poder trabajar sin servidor de correo.

## Por qué existe la costura

El canal real —qué relay SMTP, desde qué remitente, y si la red de CSI deja
salir el puerto— depende de Soporte TI y es lo que más tarda. Esperar a eso
para empezar a construir la recuperación de contraseña habría dejado parado
todo el Sprint 1. Así que el envío tiene dos modos y el resto del código no
sabe cuál está activo:

    CORREO_MODO=archivo  (default)  deja el correo en disco; no toca la red
    CORREO_MODO=smtp                lo manda de verdad
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

## Secretos

Ni el token ni la contraseña temporal se escriben en el log: van en el CUERPO
del correo, y el cuerpo nunca se registra. Lo que se registra es a quién se le
mandó y con qué asunto, que es lo que sirve para diagnosticar.
"""

import logging
import re
import smtplib
import unicodedata
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

import config

logger = logging.getLogger(__name__)

MODO_ARCHIVO = "archivo"
MODO_SMTP = "smtp"
MODO_SILENCIO = "silencio"
MODOS = frozenset({MODO_ARCHIVO, MODO_SMTP, MODO_SILENCIO})

#: Subcarpeta del almacén donde caen los correos en modo `archivo`.
CARPETA = "correos"


class ErrorCorreo(RuntimeError):
    """No se pudo enviar. Quien llama decide si lo reintenta o lo reporta."""


def modo() -> str:
    valor = (config.CORREO_MODO or MODO_ARCHIVO).strip().lower()
    return valor if valor in MODOS else MODO_ARCHIVO


def esta_configurado() -> bool:
    """¿Se puede mandar correo en este ambiente? En `archivo` siempre sí; en
    `smtp` hace falta al menos el host y el remitente."""
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
    mensaje = _armar(destino, asunto, texto, html)
    if actual == MODO_SMTP:
        donde = _enviar_por_smtp(mensaje)
    else:
        donde = _enviar_a_archivo(mensaje, destino, asunto)
    # Nunca el cuerpo: ahí van el token y la contraseña temporal.
    logger.info("Correo enviado a %s · %s · %s", destino, asunto, donde)
    return donde
