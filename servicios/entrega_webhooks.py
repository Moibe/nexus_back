"""Entregar un aviso a un webhook de cliente: la guarda contra SSRF y la firma.

Lo usan la validación de conexión (`POST /webhooks/{id}/validar`, un aviso de
prueba) y las entregas de eventos reales con sus reintentos
(`servicios/entregas_webhooks.py`). Aquí vive lo que es igual para los dos: la
guarda, la firma y el envío de UN intento.

## La guarda contra SSRF: el servidor no le pega a la red interna

Validar significa que el servidor le hace una petición a una URL que escribió
alguien, desde adentro de la red de CSI. Sin esta guarda, una URL podría apuntar
a servicios internos —los hooks de despliegue, por ejemplo, no piden token—. Se
revisa al ENTREGAR, no al registrar, porque el DNS puede cambiar entre las dos:

  1. Solo `https`. (`http` únicamente hacia loopback, y solo con
     `WEBHOOKS_PERMITIR_LOCAL`, que es para desarrollo y nunca va en el server.)
  2. Se resuelve el nombre y se rechaza si CUALQUIERA de sus direcciones no es
     pública: privadas, loopback, link-local, reservadas, multicast, y las IPv4
     escondidas en IPv6 (mapeadas, 6to4, Teredo).
  3. Además se rechazan las redes de `WEBHOOKS_REDES_BLOQUEADAS`. Por defecto
     `172.10.0.0/16`: la red interna de CSI usa 172.10.x.x, que NO es privada
     según el estándar (las privadas son 172.16-172.31), así que para Python es
     "pública" y el punto 2 la dejaría pasar. Medido: `172.10.30.15`, el server,
     da `is_global=True`.
  4. Se CONECTA a la IP ya revisada —no se vuelve a resolver—, con el nombre
     original en `Host` y en el SNI, así que el certificado se sigue revisando
     contra el nombre. Sin esto, un DNS que cambia entre la revisión y la
     conexión (DNS rebinding) se colaría.
  5. No se siguen redirecciones (una 302 podría mandar a una IP interna), ni se
     usan proxies del entorno (resolverían el nombre por su cuenta), ni se lee
     el cuerpo de la respuesta.

## La firma: Standard Webhooks

Cada aviso va firmado según https://www.standardwebhooks.com/ —de ahí el prefijo
`whsec_` del secret—, para que el cliente lo verifique con las bibliotecas que
ya existen para casi cualquier lenguaje en vez de programarlo a mano:

  webhook-id:        msg_...           (único por aviso)
  webhook-timestamp: 1696180000        (segundos Unix)
  webhook-signature: v1,<base64>       (HMAC-SHA256)

Lo firmado es `{webhook-id}.{webhook-timestamp}.{cuerpo}`, y la llave HMAC es lo
que va después de `whsec_`, decodificado en base64. El cuerpo es JSON:
`{"type": ..., "timestamp": ..., "data": {...}}`.

## Cuántas validaciones

Si cualquiera puede apretar "Validar conexión", el servidor no debe servir de
relevo para mandar ráfagas de peticiones a terceros. `limite_validaciones` deja
una cada 5 segundos por webhook y 20 por minuto en total. Vive en memoria: con
un solo proceso uvicorn alcanza.
"""

import base64
import hashlib
import hmac
import ipaddress
import json
import secrets
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx

import config

PREFIJO_SECRET = "whsec_"
USER_AGENT = "NexusDoc-Webhooks/1.0"

_MENSAJE_INTERNA = (
    "La URL apunta a una dirección interna o reservada. Por seguridad, NexusDoc "
    "solo envía avisos a direcciones públicas."
)


class Rechazo(Exception):
    """El aviso no se mandó, o el endpoint no lo aceptó. El mensaje es para el
    usuario: dice qué pasó sin detalles de la red interna. `codigo` y `ms`, si
    se llegó a hablar con el endpoint: alimentan las métricas de entregas."""

    def __init__(self, mensaje: str, codigo: int | None = None, ms: int | None = None):
        super().__init__(mensaje)
        self.codigo = codigo
        self.ms = ms


# ── La firma ────────────────────────────────────────────────────────────────


def llave_de(secret: str) -> bytes:
    """La llave HMAC de un secret: lo que va después de `whsec_`, decodificado en
    base64, exactamente como lo hacen del lado del cliente las bibliotecas de
    Standard Webhooks. (Los primeros secrets se generaron en hexadecimal, que
    también es base64 válido: siguen sirviendo con esta misma regla.)"""
    return base64.b64decode(secret.removeprefix(PREFIJO_SECRET), validate=True)


def firmar(secret: str, id_mensaje: str, marca: str, cuerpo: bytes) -> str:
    """El valor de la cabecera `webhook-signature`: `v1,<base64 del HMAC-SHA256>`."""
    contenido = f"{id_mensaje}.{marca}.".encode() + cuerpo
    digest = hmac.new(llave_de(secret), contenido, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


# ── La guarda ───────────────────────────────────────────────────────────────


def _bloqueada(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if isinstance(ip, ipaddress.IPv6Address):
        # Una IPv4 escondida en IPv6 se juzga como la IPv4 que es.
        escondida = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if escondida is not None:
            return _bloqueada(escondida)
    if config.WEBHOOKS_PERMITIR_LOCAL and ip.is_loopback:
        return False
    if not ip.is_global or ip.is_multicast:
        return True
    return any(ip in red for red in config.WEBHOOKS_REDES_BLOQUEADAS)


def _resolver(host: str, puerto: int) -> str:
    """La dirección a la que se va a conectar, ya revisada. Rechaza si CUALQUIERA
    de las que devuelve el DNS está bloqueada: con varias, el sistema podría
    escoger cualquiera."""
    try:
        infos = socket.getaddrinfo(host, puerto, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise Rechazo(f"No se pudo resolver el nombre {host}. Revisa que la URL esté bien escrita.") from exc
    direcciones = []
    for *_, direccion in infos:
        try:
            # `%` es el scope de una IPv6 link-local (fe80::1%eth0): fuera.
            direcciones.append(ipaddress.ip_address(str(direccion[0]).split("%")[0]))
        except ValueError:
            continue
    if not direcciones:
        raise Rechazo(f"No se pudo resolver el nombre {host}. Revisa que la URL esté bien escrita.")
    if any(_bloqueada(d) for d in direcciones):
        raise Rechazo(_MENSAJE_INTERNA)
    return str(direcciones[0])


# ── La entrega ──────────────────────────────────────────────────────────────


def armar_cuerpo(tipo: str, datos: dict, momento: datetime | None = None) -> bytes:
    """El cuerpo de un aviso, como lo pide Standard Webhooks: `type`,
    `timestamp` (cuándo ocurrió lo que se avisa) y `data`. Se arma UNA vez por
    aviso: en sus reintentos viaja idéntico."""
    momento = momento or datetime.now(timezone.utc)
    return json.dumps(
        {"type": tipo, "timestamp": momento.isoformat(timespec="seconds"), "data": datos},
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode()


def nuevo_id_mensaje() -> str:
    return "msg_" + secrets.token_hex(12)


def entregar(url: str, secret: str, tipo: str, datos: dict) -> dict:
    """Arma un aviso nuevo y lo manda en UN intento. Lo usa la validación."""
    return enviar(url, secret, armar_cuerpo(tipo, datos), nuevo_id_mensaje())


def enviar(url: str, secret: str, cuerpo: bytes, id_mensaje: str) -> dict:
    """Manda `cuerpo` firmado a `url`, en UN intento, y devuelve
    `{"codigo", "ms"}` si el endpoint respondió 2xx. Si no se mandó o no lo
    aceptó, levanta `Rechazo` con el motivo.

    `id_mensaje` es el MISMO en todos los intentos de un aviso (`webhook-id`);
    la marca de tiempo y la firma son de este intento."""
    partes = urlsplit(url)
    esquema = partes.scheme
    host = partes.hostname
    if esquema not in ("https", "http") or not host:
        raise Rechazo("La URL del webhook no es válida.")
    defecto = 443 if esquema == "https" else 80
    puerto = partes.port or defecto
    ip = _resolver(host, puerto)
    if esquema == "http" and not ipaddress.ip_address(ip).is_loopback:
        raise Rechazo("Los avisos solo se envían por https://.")

    marca = str(int(time.time()))

    nombre = f"[{host}]" if ":" in host else host
    destino_ip = f"[{ip}]" if ":" in ip else ip
    destino = f"{esquema}://{destino_ip}:{puerto}{partes.path or '/'}" + (f"?{partes.query}" if partes.query else "")
    cabeceras = {
        # A la IP revisada, pero con el nombre original: el servidor del cliente
        # lo necesita para saber a cuál de sus sitios va.
        "Host": nombre if puerto == defecto else f"{nombre}:{puerto}",
        "Content-Type": "application/json",
        "User-Agent": USER_AGENT,
        "webhook-id": id_mensaje,
        "webhook-timestamp": marca,
        "webhook-signature": firmar(secret, id_mensaje, marca, cuerpo),
    }
    # El SNI y la revisión del certificado van contra el NOMBRE, no contra la IP.
    extensiones = {"sni_hostname": host} if esquema == "https" else {}

    espera = config.WEBHOOKS_TIMEOUT_S
    inicio = time.monotonic()
    try:
        with httpx.Client(timeout=espera, follow_redirects=False, trust_env=False) as cliente:
            # `stream` y salir sin leer: el cuerpo de la respuesta no importa y
            # podría ser enorme.
            with cliente.stream("POST", destino, content=cuerpo, headers=cabeceras, extensions=extensiones) as r:
                codigo = r.status_code
    except httpx.TimeoutException as exc:
        raise Rechazo(f"El endpoint no respondió en {espera:g} segundos.", ms=_transcurrido(inicio)) from exc
    except httpx.ConnectError as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc) or "certificate" in str(exc).lower():
            raise Rechazo("El certificado HTTPS del endpoint no es válido para ese nombre.", ms=_transcurrido(inicio)) from exc
        raise Rechazo("No se pudo conectar con el endpoint.", ms=_transcurrido(inicio)) from exc
    except httpx.HTTPError as exc:
        raise Rechazo("La conexión con el endpoint falló antes de recibir respuesta.", ms=_transcurrido(inicio)) from exc
    ms = _transcurrido(inicio)

    if 200 <= codigo < 300:
        return {"codigo": codigo, "ms": ms}
    if 300 <= codigo < 400:
        raise Rechazo(
            f"El endpoint respondió con una redirección ({codigo}). NexusDoc no sigue "
            "redirecciones: registra la dirección final.",
            codigo=codigo,
            ms=ms,
        )
    raise Rechazo(
        f"El endpoint respondió {codigo}. Tiene que responder con un código 2xx.", codigo=codigo, ms=ms
    )


def _transcurrido(inicio: float) -> int:
    return round((time.monotonic() - inicio) * 1000)


# ── Cuántas validaciones ────────────────────────────────────────────────────

ESPERA_MISMO_WEBHOOK_S = 5
VENTANA_S = 60
MAXIMO_POR_VENTANA = 20


class _Limite:
    def __init__(self) -> None:
        self._candado = threading.Lock()
        self._ultima: dict[str, float] = {}
        self._recientes: deque[float] = deque()

    def motivo_para_esperar(self, identificador: str) -> str | None:
        """`None` si se puede validar ahora (y lo cuenta); si no, por qué no."""
        ahora = time.monotonic()
        with self._candado:
            ultima = self._ultima.get(identificador)
            if ultima is not None and ahora - ultima < ESPERA_MISMO_WEBHOOK_S:
                return "Espera unos segundos antes de volver a validar este webhook."
            while self._recientes and ahora - self._recientes[0] > VENTANA_S:
                self._recientes.popleft()
            if len(self._recientes) >= MAXIMO_POR_VENTANA:
                return "Hay demasiadas validaciones en este momento. Intenta de nuevo en un minuto."
            self._ultima[identificador] = ahora
            self._recientes.append(ahora)
            return None

    def reiniciar(self) -> None:
        """Para las pruebas."""
        with self._candado:
            self._ultima.clear()
            self._recientes.clear()


limite_validaciones = _Limite()
