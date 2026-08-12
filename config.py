"""Configuración central leída del .env.

Sigue el patrón de geospace_nucleo: constantes a nivel de módulo, sin clases ni
pydantic-settings. Se importa como `from config import ALGO`.
"""

import os

from dotenv import load_dotenv

load_dotenv()

# ── Ambiente ──────────────────────────────────────────────────────────────────
ENVIRONMENT = os.getenv("ENVIRONMENT", "local")
PORT = int(os.getenv("PORT", "8083"))

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

# ── Endpoints de IA ───────────────────────────────────────────────────────────
# Servicios de IA externos a esta API (OCR, extracción, clasificación).
IA_BASE_URL = os.getenv("IA_BASE_URL", "")
IA_API_KEY = os.getenv("IA_API_KEY", "")
IA_TIMEOUT = float(os.getenv("IA_TIMEOUT", "120"))
