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

| Ruta | Qué es |
|---|---|
| `app.py` | Objeto FastAPI, CORS, `/health`, registro de routers |
| `config.py` | Constantes leídas del `.env` |
| `db/sqlserver.py` | Conexión a SQL Server + helpers para invocar stored procedures |
| `ia/cliente.py` | Cliente HTTP para los endpoints de IA externos |
| `routers/` | Un archivo por dominio |

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
