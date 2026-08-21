"""Configuración central leída del .env.

Sigue el patrón de geospace_nucleo: constantes a nivel de módulo, sin clases ni
pydantic-settings. Se importa como `from config import ALGO`.
"""

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
_origins_env = os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:7000,http://127.0.0.1:7000")
CORS_ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()]

# ── SQL Server ────────────────────────────────────────────────────────────────
# La base la diseña y mantiene el DBA; aquí solo se consumen sus stored
# procedures. No hay ORM ni migraciones de este lado a propósito.
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
MAX_SUBIDA_MB = _numero("MAX_SUBIDA_MB", 10)
if MAX_SUBIDA_MB <= 0:
    raise RuntimeError(f"MAX_SUBIDA_MB tiene que ser mayor que 0 (llegó {MAX_SUBIDA_MB}).")
MAX_SUBIDA_BYTES = int(MAX_SUBIDA_MB * 1024 * 1024)
