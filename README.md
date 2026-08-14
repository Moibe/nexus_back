# NexusDoc AI · API (`nexus_back`)

Backend de NexusDoc AI. Es el par de [`nexus_poc_svelte`](../nexus_poc_svelte) (el front).

## Responsabilidades

1. **SQL Server** — dueño **exclusivo** de la base. El front nunca se conecta directo.
   La base la diseña y mantiene el DBA; aquí solo se consumen sus stored procedures
   (sin ORM, sin migraciones de este lado).
2. **Endpoints de IA** — orquesta las llamadas a los servicios de IA externos
   (OCR, extracción de campos, clasificación documental).
3. **Conectores documentales** — SFTP / SharePoint / API REST por tenant (HU014-016).
   _Pendiente._

## Cómo habla el front con esta API

El navegador **no** llama aquí directo. El flujo es:

```
navegador → SvelteKit (capa server) → esta API → SQL Server / IA
```

Así la sesión vive en una sola cookie de SvelteKit y esta API no necesita estar
expuesta a internet.

## Estructura

Separación por **capas**, no por fuente de datos. La API HTTP se agrupa por
dominio (un router por dominio, y eso es lo que se ve en Swagger); lo que
distingue "esto va a la base" de "esto llama a un servicio externo" es en qué
carpeta vive el código:

| Ruta | Capa | Qué es |
|---|---|---|
| `app.py` | — | Objeto FastAPI, CORS, `/health`, registro de routers |
| `config.py` | — | Constantes leídas del `.env` |
| `routers/` | HTTP | Un archivo por dominio. Traduce request/response y elige códigos de error |
| `repositorios/` | Datos | **Todo lo que toca SQL Server.** Un archivo por dominio, funciones que invocan SPs |
| `servicios/` | Externo | **Todo lo que llama a servicios de terceros** (IA, y más adelante SFTP/SharePoint) |
| `db/sqlserver.py` | Plomería | Conexión + helpers genéricos para invocar stored procedures |

**Regla:** un `router` nunca importa `pyodbc` ni `httpx` directo — solo llama
funciones de `repositorios/` o `servicios/`. Así un endpoint puede combinar
ambas fuentes sin que el front se entere de dónde viene cada dato.

Nombres de función: español, verbo primero (`listar_bandeja`,
`registrar_documento`, `extraer_campos`) — igual que en el resto de tus proyectos.

## Setup local

```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env      # y llenar los valores
.venv\Scripts\python.exe -m uvicorn app:app --reload --port 8083
```

```bash
# macOS / Linux
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m uvicorn app:app --reload --port 8083
```

Swagger en `http://localhost:8083/docs`.

### Dependencia a nivel de sistema operativo

`pyodbc` **no** trae el driver de SQL Server — hay que instalarlo aparte:

- **Windows**: [ODBC Driver 18 for SQL Server](https://learn.microsoft.com/sql/connect/odbc/download-odbc-driver-for-sql-server) (instalador `.msi`).
- **Ubuntu** (server de CSI): paquete `msodbcsql18` del repo de Microsoft, más `unixodbc-dev`.

Verificar qué quedó instalado: `odbcinst -q -d`. El nombre que salga ahí tiene que
ir textual en `SQLSERVER_DRIVER` del `.env`.

Prueba rápida de conectividad: `GET /health/db`.

## Despliegue

Va al **server interno de CSI** (`172.10.30.15`), no al droplet — SQL Server vive
en la red interna y el droplet no tiene ruta hacia allá.

Se despliega con el patrón de [`webhook-central`](../webhook-central): este repo no
lleva GitHub Action. Hay que registrar en `webhook-central`:

1. `projects/nexus-back.conf` — `PROJECT_ROOT`, `PROJECT_BRANCH`, `VENV_PATH`, `PM2_NAME`, `APP_STACK="python"`
2. una entrada en `hooks.json`
3. una entrada en `apps.json`

El primer `git clone` y el primer `pm2 start` son manuales (el `deploy.sh` de
webhook-central solo hace pull + `pm2 restart`).

Puerto propuesto: **8083** (en ese server ya están ocupados 7860, 8077, 8080, 8082, 8099).
