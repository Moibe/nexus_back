"""Configuración central leída del .env.

Sigue el patrón de geospace_nucleo: constantes a nivel de módulo, sin clases ni
pydantic-settings. Se importa como `from config import ALGO`.
"""

import ipaddress
import os

from dotenv import load_dotenv

load_dotenv()


def _numero(nombre: str, default: float) -> float:
    """Lee una variable numérica del entorno tolerando que esté presente pero vacía.

    `os.getenv(x, default)` solo aplica el default cuando la variable NO existe.
    Un renglón `PORT=` en el .env devuelve `''`, y `int('')` truena con un
    ValueError durante el import de este módulo — o sea, antes de que exista la
    app, con un traceback que ni menciona el .env. Ese caso es fácil de provocar
    escribiendo el .env de producción a mano.
    """
    crudo = os.getenv(nombre)
    if crudo is None or not crudo.strip():
        return default
    try:
        return float(crudo)
    except ValueError as exc:
        raise RuntimeError(
            f"{nombre}={crudo!r} en el .env no es un número válido."
        ) from exc


# ── Ambiente ──────────────────────────────────────────────────────────────────
ENVIRONMENT = os.getenv("ENVIRONMENT", "local")
# Ojo: en el server el puerto real lo fija el `--port` de la línea de comandos de
# uvicorn que pm2 guardó; esta variable solo la usa el bloque __main__ de app.py.
PORT = int(_numero("PORT", 8083))

# ── CORS ──────────────────────────────────────────────────────────────────────
# El front (SvelteKit) llama a esta API desde su capa server, no desde el
# navegador, así que en producción CORS casi no importa. Se deja configurable
# por si algún día se consume directo desde el browser.
_origins_env = os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3400,http://127.0.0.1:3400")
CORS_ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()]

# ── Dominio público ───────────────────────────────────────────────────────────
# Los nombres con los que esta API se publica hacia clientes: el reverse proxy
# de Soporte TI reenvía por ellos. Lo que llega por uno de estos nombres solo
# puede usar lo de la documentación pública (mandar un documento); ver
# superficie_publica.py. Varios, separados por coma. Una variable presente pero
# VACÍA vale lo mismo que ausente: un renglón en blanco en el .env no debe
# apagar la restricción sin que nadie se entere.
_hosts_env = os.getenv("NEXUS_HOSTS_PUBLICOS", "").strip() or "nexus-doc-api.buzzword.com.mx"
HOSTS_PUBLICOS = frozenset(h.strip().lower() for h in _hosts_env.split(",") if h.strip())

# ── SQL Server ────────────────────────────────────────────────────────────────
# La base la diseña y mantiene el DBA; aquí solo se consumen sus stored
# procedures. No hay ORM ni migraciones de este lado a propósito.
# Llave compartida con el front (ver seguridad.py). Vacía = la API falla
# cerrada en los endpoints protegidos, nunca abierta.
NEXUS_API_KEY = os.getenv("NEXUS_API_KEY", "")

# Llave Fernet con la que se cifran los secrets de firma de los webhooks (ver
# servicios/webhooks_cliente.py). Sin ella el alta de webhooks responde 503: un
# secret que no se sabe cifrar no se guarda. NO se cambia a la ligera: lo que se
# cifró con la anterior ya no se puede descifrar.
WEBHOOKS_CLAVE_CIFRADO = os.getenv("WEBHOOKS_CLAVE_CIFRADO", "").strip()


def _redes(nombre: str, default: str) -> list:
    """Una lista de redes separadas por coma. Vacía o ausente = el default: una
    guarda de seguridad no se apaga por dejar el renglón en blanco."""
    crudo = (os.getenv(nombre) or "").strip() or default
    redes = []
    for parte in crudo.split(","):
        parte = parte.strip()
        if not parte:
            continue
        try:
            redes.append(ipaddress.ip_network(parte, strict=False))
        except ValueError as exc:
            raise RuntimeError(f"{nombre}: {parte!r} no es una red válida (ej. 172.10.0.0/16).") from exc
    return redes


# La guarda contra SSRF de los webhooks (servicios/entrega_webhooks.py) ya
# rechaza toda dirección que no sea pública. Pero la red interna de CSI usa
# 172.10.x.x, que NO es privada según el estándar (las privadas son 172.16 a
# 172.31): para Python es "pública" y pasaría. Por eso se bloquea aquí, de forma
# explícita. Varias, separadas por coma.
WEBHOOKS_REDES_BLOQUEADAS = _redes("WEBHOOKS_REDES_BLOQUEADAS", "172.10.0.0/16")

# SOLO para desarrollo: deja validar webhooks que apuntan a localhost. En el
# server NUNCA: abriría la puerta a que una URL le pegue a los servicios de la
# propia máquina (los hooks de despliegue no piden token).
WEBHOOKS_PERMITIR_LOCAL = (os.getenv("WEBHOOKS_PERMITIR_LOCAL") or "").strip().lower() in {"1", "true", "si", "sí", "yes"}

# Cuánto se espera al endpoint del cliente al validar (y, cuando exista, al
# entregar).
WEBHOOKS_TIMEOUT_S = _numero("WEBHOOKS_TIMEOUT_S", 10)

# Apaga el trabajador que entrega y reintenta los avisos de webhook
# (servicios/entregas_webhooks.py). Es para un entorno que COMPARTE el almacén
# con otro: el trabajador reconstruye sus pendientes leyendo el registro del
# NAS, y con el registro compartido reintentaría las entregas del otro entorno
# hacia los endpoints reales de los clientes. Vacía o ausente = encendido, o
# sea que producción no cambia; solo lo apagan los valores off/0/no/false.
ENTREGAS_WEBHOOKS_ACTIVAS = (os.getenv("ENTREGAS_WEBHOOKS") or "").strip().lower() not in {"off", "0", "no", "false"}

SQLSERVER_HOST = os.getenv("SQLSERVER_HOST", "")
SQLSERVER_PORT = os.getenv("SQLSERVER_PORT", "1433")
SQLSERVER_DB = os.getenv("SQLSERVER_DB", "")
SQLSERVER_USER = os.getenv("SQLSERVER_USER", "")
SQLSERVER_PASSWORD = os.getenv("SQLSERVER_PASSWORD", "")
# El nombre del driver ODBC tiene que coincidir EXACTO con el instalado en el SO
# (en Ubuntu: `odbcinst -q -d` lo lista).
SQLSERVER_DRIVER = os.getenv("SQLSERVER_DRIVER", "ODBC Driver 18 for SQL Server")
# Un SQL Server interno normalmente trae certificado autofirmado; con Driver 18
# el default es Encrypt=yes y truena si no se confía en el cert.
SQLSERVER_TRUST_CERT = os.getenv("SQLSERVER_TRUST_CERT", "yes")
# El Driver 18 cambió el default a Encrypt=yes (el 17 era "no"), y contra un SQL
# Server viejo eso puede tumbar la conexión ANTES del login: OpenSSL 3 (Ubuntu
# 22.04+) rechaza TLS 1.0/1.1 y protocolos que ese SQL Server quizá sea el único
# que ofrece. El síntoma engaña: TCP abre bien y el error se ve como problema de
# conexión o de credenciales, no de cifrado.
#
# Se deja en "yes" a propósito — cifrar es lo correcto. Ponerlo en "no" es un
# escape documentado para una red interna donde el servidor no puede negociar
# TLS moderno, no la configuración recomendada.
SQLSERVER_ENCRYPT = os.getenv("SQLSERVER_ENCRYPT", "yes")

# ── Google Document AI ────────────────────────────────────────────────────────
# Se llaman directo los procesadores de Document AI (no vía el proyecto
# hermano `document_ai`). Los IDs van aquí y no hardcodeados en el código, para
# poder apuntar a procesadores distintos por ambiente sin tocar fuentes.
#
# La credencial la resuelve google-auth sola leyendo GOOGLE_APPLICATION_CREDENTIALS
# del entorno (ruta al JSON de la cuenta de servicio), por eso no se lee aquí.
DOCAI_PROJECT_ID = os.getenv("DOCAI_PROJECT_ID", "")
DOCAI_LOCATION = os.getenv("DOCAI_LOCATION", "us")
DOCAI_PROCESADOR_INE = os.getenv("DOCAI_PROCESADOR_INE", "")
# El ÚNICO Custom Document Classifier del sistema (ver servicios/procesadores.py
# sincronizar_clasificador). A diferencia de DOCAI_PROCESADOR_INE, este no
# identifica un tipo documental sino el paso previo: a cuál tipo pertenece un
# documento entrante, antes de decidir a qué extractor mandarlo.
DOCAI_CLASIFICADOR_ID = os.getenv("DOCAI_CLASIFICADOR_ID", "")
# Misma razón que DOCAI_VERSION_INE: sin fijarla, Google puede promover otra
# versión a "default" sin aviso y la clasificación deja de ser reproducible.
DOCAI_VERSION_CLASIFICADOR = os.getenv("DOCAI_VERSION_CLASIFICADOR", "")

# Versión del modelo con la que se llama al procesador. Si se deja vacía, Google
# usa la "default" del procesador — y esa la puede cambiar él, sin avisar, el
# día que promueva otra a estable. Eso rompería la reproducibilidad que exige el
# diccionario de datos ("Modelo y versión exactos") y haría que
# `extraction_run.engine_version` guardara una suposición en vez de un hecho.
# Fijarla es la diferencia entre saber y creer con qué modelo se extrajo.
DOCAI_VERSION_INE = os.getenv("DOCAI_VERSION_INE", "")
IA_TIMEOUT = _numero("IA_TIMEOUT", 120)

# ── Límite de subida ──────────────────────────────────────────────────────────
# En el server de CSI la API se expone IP:puerto directo, sin nginx delante (los
# dominios y el proxy solo se usan en el droplet de DigitalOcean). O sea: no hay
# `client_max_body_size` que nos proteja, el límite tiene que vivir aquí.
#
# Importa porque `/ia/ine` codifica el archivo a base64 para mandarlo a Document
# AI, y eso infla el contenido ~1.33x en memoria.
# 20 MB y no 10: es el mismo número que promete el dropzone del front
# ("PDF, DOCX, XLSX, JPG, JPEG, TIFF | Max 20 MB", que viene de Figma) y que
# valida su capa BFF. Con 10 quedaba una franja de 10-20 MB que la UI
# aceptaba y esta API rechazaba, o sea un error que el usuario no podía
# prever. 20 MB es además el tope de Document AI para procesamiento en
# línea, así que subir más no serviría de nada.
MAX_SUBIDA_MB = _numero("MAX_SUBIDA_MB", 20)
if MAX_SUBIDA_MB <= 0:
    raise RuntimeError(f"MAX_SUBIDA_MB tiene que ser mayor que 0 (llegó {MAX_SUBIDA_MB}).")
MAX_SUBIDA_BYTES = int(MAX_SUBIDA_MB * 1024 * 1024)

# ── Almacén de documentos ─────────────────────────────────────────────────────
# Dónde se guardan los BYTES de un archivo subido (ver servicios/almacen.py).
#
# VACÍA A PROPÓSITO por ahora. El almacén está construido y probado pero NADIE
# lo llama todavía: se dejó listo para el día que exista la tabla `file` en SQL
# Server, sin cambiar en nada el comportamiento actual de Nexus. Mientras esté
# vacía, `almacen.esta_configurado()` responde False y cualquier intento de
# escribir levanta un error explícito en vez de inventarse una carpeta.
#
# Va a ser el punto de montaje del NAS de infraestructura de CSI. Se configura
# aquí y no se hardcodea porque el montaje cambia por ambiente (y podría
# cambiar en el server sin que cambie el código): en la base se guardan rutas
# RELATIVAS justamente para que mover el montaje no obligue a migrar datos.
#
# Local (Windows):  C:/Moibe/almacen-nexus
# Server de CSI:    /mnt/nas/nexus/documentos
ALMACEN_RUTA = os.getenv("ALMACEN_RUTA", "")

# ── Acceso (Sprint 1: login con JWT) ─────────────────────────────────────────
# El secreto firma los JWT de acceso; el front lo comparte para verificarlos
# sin llamar aquí en cada petición. Sin él, /auth/* responde 503. Mínimo 32
# caracteres: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
AUTH_JWT_SECRET = os.getenv("AUTH_JWT_SECRET", "").strip()
# Vida del JWT de acceso (minutos) y del refresh token (días).
AUTH_ACCESO_MIN = int(_numero("AUTH_ACCESO_MIN", 15))
AUTH_REFRESH_DIAS = int(_numero("AUTH_REFRESH_DIAS", 7))
# La política del diseño: 5 intentos fallidos → 15 minutos de bloqueo. Cuando
# existan los SPs de NEX-319, vivirá en `uspRecordLoginResult` y esto sobra.
AUTH_MAX_INTENTOS = int(_numero("AUTH_MAX_INTENTOS", 5))
AUTH_BLOQUEO_MIN = int(_numero("AUTH_BLOQUEO_MIN", 15))
