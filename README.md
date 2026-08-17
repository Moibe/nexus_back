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

Ahí **no hay nginx**: las apps se exponen `IP:puerto` directo y las alcanza quien
esté en la red de la empresa. (El nginx y los dominios son solo del droplet de
DigitalOcean.) Dos consecuencias de diseño: el límite de tamaño de subida vive en
el código (`MAX_SUBIDA_MB`, no hay `client_max_body_size`), y no hacen falta
`--proxy-headers` ni `--forwarded-allow-ips` en el arranque.

**Nombre del proyecto: `nexus_back`** — con guion bajo, idéntico en las tres
piezas de webhook-central. `deploy.sh` resuelve `projects/$1.conf`, y la UI cruza
`apps.json.id` contra el `PROJECT_NAME` que `deploy.sh` escribe en el jsonl: si
las tres no coinciden textualmente, el deploy corre pero la UI se queda en
`idle`, o el hook aborta con "config no encontrada".

### 1. Verificar el server (antes de tocar nada)

Todo esto es de solo lectura y responde lo que no se puede saber desde local:

```bash
hostname && ip -4 addr show | grep inet      # ¿es srviaproducto? ¿es .30.15?
sudo ss -ltnp | grep -w 8083 || echo "8083 LIBRE"
sudo ss -ltnp                                # inventario completo
python3 --version                            # tiene que ser >= 3.10
odbcinst -q -d 2>/dev/null || echo "unixODBC no instalado"
ldconfig -p | grep -i libodbc || echo "runtime de unixODBC ausente"
pm2 list && pm2 describe document-ai-api | head -25
ls ~/.pm2/dump.pm2 && echo "pm2 save hecho"  # ¿sobrevive un reboot?
ls /home/mbriseno/code/                      # ¿ya existe nexus_back?
```

El `sudo` en `ss -ltnp` no es opcional: sin privilegios no muestra qué proceso
tiene tomado cada puerto.

El puerto **8083** es una *propuesta*: los puertos que este README listaba como
ocupados salieron de `apps.json`, que es el catálogo de la UI y no un inventario
del SO. `ss -ltnp` es el único dato que decide.

### 2. Primer arranque (manual, una sola vez)

`deploy.sh` solo hace pull + `pip install` + `pm2 restart`; nada de esto lo hace
él, y si el proceso pm2 no existe todavía, **aborta**.

```bash
cd /home/mbriseno/code && git clone https://github.com/Moibe/nexus_back.git
cd nexus_back && python3 -m venv venv          # 'venv' SIN punto: es la
source venv/bin/activate                       # convención de los .conf del server
pip install -r requirements.txt

# El directorio de la llave nace con dueño = usuario de pm2. Si queda root:root
# con 700, mbriseno no puede ni atravesarlo: aunque el archivo sea suyo y 600,
# google-auth reporta "File ... was not found", que se lee como error de ruta.
sudo mkdir -p /etc/nexus-back
sudo chown mbriseno:mbriseno /etc/nexus-back && sudo chmod 700 /etc/nexus-back
# una llave de servicio DISTINTA a la local:
gcloud iam service-accounts keys create /etc/nexus-back/sa.json --iam-account=<SA>
chmod 600 /etc/nexus-back/sa.json

# .env de producción — NO viaja por git, hay que escribirlo a mano
# (ver .env.example; GOOGLE_APPLICATION_CREDENTIALS=/etc/nexus-back/sa.json)

pm2 start venv/bin/python --name nexus-back-api --cwd /home/mbriseno/code/nexus_back \
  -- -m uvicorn app:app --host 0.0.0.0 --port 8083
pm2 save
curl -s localhost:8083/health   # documentos_disponible:false es lo esperado sin ODBC
```

Se clona por **HTTPS**: el repo es público, así que no hace falta llave SSH en el
server, y `deploy.sh` solo hace `checkout`/`fetch`/`pull` — nunca push.

**Un solo proceso, uvicorn puro** — a propósito. El `gunicorn --workers 4` de
`document_ai` no es el molde: esa app es stateless, y esta abrirá conexiones a
SQL Server, donde 4 workers son 4 pools ODBC independientes. Se sube después, si
el DBA confirma cuántas conexiones tolera.

### 3. Registrar en webhook-central

Las tres piezas van **en el server** (`/home/mbriseno/webhook-central`):
webhook-central no se auto-despliega, así que editar el clon local no tiene
efecto — y ese clon ya divergió del server antes, conviene comparar primero.

1. `projects/nexus_back.conf`:
   ```bash
   APP_STACK="python"
   PROJECT_ROOT="/home/mbriseno/code/nexus_back"
   PROJECT_BRANCH="main"
   VENV_PATH="$PROJECT_ROOT/venv"
   PM2_NAME="nexus-back-api"
   # Documental: deploy.sh nunca lo ejecuta, pero es la ÚNICA copia del comando
   # real de arranque que sobrevive si hay que reconstruir el server.
   PM2_START_CMD='pm2 start venv/bin/python --name nexus-back-api --cwd /home/mbriseno/code/nexus_back -- -m uvicorn app:app --host 0.0.0.0 --port 8083'
   ```
2. `hooks.json` — lo más seguro es **copiar una entrada existente** y cambiarle
   `id`, `name` y `response-message`. Los 5 campos son indispensables: sin
   `execute-command` el hook no ejecuta nada, y el `id` es lo que forma la URL.
   ```json
   {
     "id": "despliegue-nexus_back",
     "execute-command": "/home/mbriseno/webhook-central/scripts/deploy.sh",
     "command-working-directory": "/home/mbriseno/webhook-central",
     "response-message": "✅ Despliegue de nexus_back iniciado",
     "pass-arguments-to-command": [{ "source": "string", "name": "nexus_back" }]
   }
   ```
3. `apps.json` — `deploy_url` **tiene** que apuntar al `id` del hook; sin él la
   app aparece listada pero el botón Desplegar de la UI no funciona:
   ```json
   {
     "id": "nexus_back",
     "name": "NexusDoc AI · API",
     "stack": "python",
     "branch": "main",
     "pm2_name": "nexus-back-api",
     "deploy_url": "/hooks/despliegue-nexus_back",
     "app_url": "http://172.10.30.15:8083",
     "repo_url": "https://github.com/Moibe/nexus_back"
   }
   ```

Ojo: son **cuatro** cadenas distintas y cada una tiene su regla. `nexus_back` va
idéntico en el nombre del `.conf`, en el `name` del hook y en el `id` de
`apps.json`; el `id` del hook es una cuarta cadena (`despliegue-nexus_back`) que
solo debe coincidir con el `deploy_url`.

Luego `pm2 restart webhook-listener`. **Validar el JSON antes**
(`python3 -m json.tool hooks.json > /dev/null`): un `hooks.json` inválido tumba
los deploys de los 12 proyectos, no solo este.

Para probar el circuito completo:

```bash
curl -X POST http://172.10.30.15:8090/hooks/despliegue-nexus_back
```

### Notas de operación

- **Sin ODBC instalado la app arranca igual**, pero sin `/documentos/*`
  (ver el `try/except` de `app.py`). `/health` y `/ia/ine` siguen vivos y el
  arranque loguea un `WARNING`. Como SQL Server aún no existe, se puede desplegar
  sin instalar `msodbcsql18` — y cuando toque instalarlo, revisar si basta el
  runtime de unixODBC: `unixodbc-dev` solo hace falta si `pip` compila `pyodbc`
  desde fuente, y `pyodbc` 5.x publica wheels manylinux.
- **`deploy.sh` no hace healthcheck** y un `pip install` fallido es solo una
  advertencia: puede reportar "✓ Desplegado" con la API en ciclo de restart.
  Después de cada deploy, `curl http://172.10.30.15:8083/health` a mano y revisar
  que `documentos_disponible` sea el valor esperado.
- **El hook solo reinicia si hay commits nuevos.** `deploy.sh` compara `HEAD`
  contra `origin/main` y, si coinciden, imprime "Nada que desplegar" y hace
  `exit 0` **antes** del `pip install` y del `pm2 restart`. O sea: no sirve para
  reiniciar la app tras editar el `.env` a mano en el server — eso pide un
  `pm2 restart nexus-back-api --update-env` directo.
- **El puerto no vive en el `.env`.** El arranque real lleva `--port 8083` en la
  línea de comandos que pm2 guardó al crear el proceso; `PORT` del `.env` solo lo
  usa el bloque `__main__` de `app.py`, que en producción no corre. Para cambiar
  de puerto hay que borrar y recrear el proceso pm2, no basta un deploy.
- **No hay autenticación en ningún endpoint**, y `/ia/ine` cuesta dinero por
  página en Document AI. En la red interna es una decisión defendible, pero es
  una decisión: cualquier miembro de la empresa que alcance el puerto puede
  generar costo.
