# NexusDoc AI · API (`nexus_back`)

Backend de NexusDoc AI. Es el par de [`nexus_poc_svelte`](../nexus_poc_svelte) (el front).

## Responsabilidades

1. **SQL Server** — dueño **exclusivo** de la base. El front nunca se conecta directo.
   La base la diseña y mantiene el DBA; aquí solo se consumen sus stored procedures
   (sin ORM, sin migraciones de este lado).
2. **Google Document AI** — llama **directo** a los procesadores de Document AI
   (no pasa por el proyecto hermano `document_ai`, que es una API aparte con el
   mismo propósito para otro sistema). Empezando por INE.
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
| `servicios/` | Externo | **Todo lo que llama a servicios de terceros** (Document AI, y más adelante SFTP/SharePoint) |
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

### Credenciales de Document AI

Hace falta el JSON de una cuenta de servicio con permiso sobre Document AI.
`google-auth` lo encuentra leyendo `GOOGLE_APPLICATION_CREDENTIALS` del entorno
— el código nunca toca esa variable.

**El JSON no vive en el repo.** Va fuera, con ruta absoluta en el `.env`:

| Ambiente | Ubicación |
|---|---|
| Local (Windows) | `C:/Users/<usuario>/.secretos/nexus-back-sa.json` |
| Server de CSI | `/etc/nexus-back/sa.json` — `chmod 600`, dueño = usuario de pm2 |

Las dos decisiones tienen razón de ser:

- **Absoluta, no relativa.** `google-auth` resuelve rutas relativas contra el
  *cwd del proceso*, no contra la raíz del proyecto. En local funciona de
  casualidad porque arrancas uvicorn parado en la raíz; bajo pm2 el cwd puede ser
  otro y falla con `DefaultCredentialsError: File ... was not found`, que se lee
  como problema de credenciales cuando en realidad es de ruta.
- **Fuera del repo.** El `.gitignore` sigue teniendo la entrada como red de
  seguridad, pero deja de ser lo único que impide subir la llave.

**Una llave distinta por ambiente.** La cuenta de servicio admite hasta 10
llaves, así que local y el server de CSI deben usar llaves diferentes: si se
compromete la del server, se revoca solo esa y lo demás sigue funcionando.

```bash
gcloud iam service-accounts keys list --iam-account=<SA>       # inventario
gcloud iam service-accounts keys create /etc/nexus-back/sa.json --iam-account=<SA>
gcloud iam service-accounts keys delete <KEY_ID> --iam-account=<SA>
```

La cuenta de servicio es **compartida y de otro proyecto** (vive en un proyecto
distinto al dueño de los procesadores, y otras apps la usan). Dos consecuencias:
no le recortes roles IAM — romperías a los otros consumidores; y para distinguir
en los audit logs quién llamó, la pista es
`authenticationInfo.serviceAccountKeyName` (requiere habilitar los Data Access
audit logs del proyecto de los procesadores, que vienen apagados por default).

Los IDs de proyecto y de procesador van en el `.env` (`DOCAI_PROJECT_ID`,
`DOCAI_PROCESADOR_INE`), **no hardcodeados**, para poder apuntar a procesadores
distintos por ambiente.

Prueba: `POST /ia/ine` con un `multipart/form-data` con campo `imagen`.

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
