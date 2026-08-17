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

### 0. Acceso al server

SSH escucha en el puerto **11725**, no en el 22, y pide contraseña (no hay llave
publicada para él):

```bash
ssh -p 11725 mbriseno@172.10.30.15
```

### 1. Estado del server (verificado 2026-08-17)

- `hostname` = **`srviaproducto`**, IP `172.10.30.15/25` en `enp5s0`. Los dos
  nombres son el mismo host (el DNS de CSI no resuelve `srviaproducto` y no hay
  PTR, así que esto solo se confirma desde dentro).
- **Ubuntu 24.04.3 LTS**, **Python 3.12.3**, usuario `mbriseno`.
- **Puerto 8083 libre** (verificado con `ss`, no contra `apps.json`).
  Ocupados: 8077, 8080, 8082, 8090, 8099, 3300, 4173-4177, 1111, 11725, 3306 y
  11434/43993 (ollama, solo en loopback).
- **12 procesos pm2** online (4-17 días de uptime) + módulo `pm2-logrotate` 3.0.0.
  `pm2 save` hecho (existe `~/.pm2/dump.pm2`).
- **ODBC a medias**: el CLI `odbcinst` NO está instalado, pero el runtime SÍ
  (`libodbc.so.2` y `libodbcinst.so.2` en `/lib/x86_64-linux-gnu/`, probablemente
  arrastrado por mariadb). Ver la nota de ODBC abajo: esto cambia el
  comportamiento esperado.
- **Reinicio del sistema pendiente**, con 111 actualizaciones (13 de seguridad).

### Preflight (solo lectura, cierra lo que falta)

```bash
systemctl is-enabled pm2-mbriseno; systemctl is-active pm2-mbriseno
pm2 --version
pm2 describe webhook-listener | grep -A2 "script args"   # ruta real del -hooks
python3 -c "import ensurepip; print('ensurepip OK')"
command -v gcloud || echo "gcloud AUSENTE (la llave viaja por scp)"
dpkg -l needrestart 2>/dev/null | tail -1
git -C /home/mbriseno/webhook-central status --short
git -C /home/mbriseno/webhook-central log --oneline -3
```

La primera línea es la más importante y **no depende de este proyecto**: que
exista `dump.pm2` prueba que se corrió `pm2 save`, **no** que la unit de systemd
esté habilitada. Si no está `enabled`, el reinicio pendiente se lleva las 12 apps
de producción y hay dos (`mide-chatbot-api` y `constructor-agente-rag`) cuyo
comando de arranque solo vive en `dump.pm2` — no hay `.conf` de dónde
reconstruirlas.

### 2. Primer arranque (manual, una sola vez)

`deploy.sh` solo hace pull + `pip install` + `pm2 restart`; nada de esto lo hace
él, y si el proceso pm2 no existe todavía, **aborta**.

```bash
# --- código y dependencias ---
cd /home/mbriseno/code && git clone https://github.com/Moibe/nexus_back.git
cd nexus_back && python3 -m venv venv        # 'venv' SIN punto: convención del server
venv/bin/python -m pip install -r requirements.txt
venv/bin/python -c "import pyodbc; print('pyodbc', pyodbc.version)"

# --- llave de servicio ---
# El directorio nace con dueño = usuario de pm2. Si queda root:root con 700,
# mbriseno no puede ni atravesarlo: aunque el archivo sea suyo y 600, google-auth
# reporta "File ... was not found", que se lee como error de ruta y no de permisos.
# Lo ideal seria una llave DISTINTA por ambiente, pero crear llaves esta
# BLOQUEADO (verificado 2026-08-17: `gcloud iam service-accounts keys create`
# da PERMISSION_DENIED — la SA vive en un proyecto ajeno, mapstuff-272921).
# Mientras el dueno de ese proyecto no entregue una llave nueva, se reutiliza la
# llave local, mandandola en DOS pasos: primero al home, luego a su lugar. Asi un
# transporte fallido no deja un secreto a medio escribir en /etc.
#
#   # en PowerShell (el OpenSSH de Windows no viene en el PATH):
#   $env:Path += ";C:\Windows\System32\OpenSSH"
#   cd C:\Users\usuario\.secretos      # entrar a la carpeta evita que scp lea
#   scp -P 11725 nexus-back-sa.json mbriseno@172.10.30.15:sa.json   # "C:" como host
#
sudo install -d -o mbriseno -g mbriseno -m 700 /etc/nexus-back
install -m 600 ~/sa.json /etc/nexus-back/sa.json
shred -u ~/sa.json                 # no dejar la llave suelta en el home
ls -l /etc/nexus-back/             # esperado: -rw------- mbriseno mbriseno 2353

# --- .env de producción (a mano; no viaja por git) ---
#   ENVIRONMENT=produccion        <- el health check lo usa como testigo
#   GOOGLE_APPLICATION_CREDENTIALS=/etc/nexus-back/sa.json
#   DOCAI_PROJECT_ID / DOCAI_PROCESADOR_INE

# --- validar ANTES de crear el proceso pm2 ---
# Comprueba la llave, los permisos de ruta y la salida HTTPS a Google. Es gratis:
# pide un token, no toca Document AI. `import app` NO sirve para esto — las
# credenciales se resuelven de forma perezosa dentro de servicios.ia._token().
# El `import config` del inicio NO es decorativo: es quien corre load_dotenv() y
# mete GOOGLE_APPLICATION_CREDENTIALS al entorno; sin él, google-auth no ve la
# variable y falla aunque la llave esté perfecta. Correrlo parado en la raíz del
# proyecto.
venv/bin/python -c "
import config
from google.auth import default
from google.auth.transport.requests import Request
c,_ = default(scopes=['https://www.googleapis.com/auth/cloud-platform'])
c.refresh(Request()); print('credencial OK:', c.service_account_email)"
venv/bin/python -c "import app; print('DOCUMENTOS_DISPONIBLE =', app.DOCUMENTOS_DISPONIBLE)"

# --- pm2, desde una shell LIMPIA (sin haber hecho `activate`) ---
pm2 start /home/mbriseno/code/nexus_back/venv/bin/python \
  --name nexus-back-api --interpreter none \
  --cwd /home/mbriseno/code/nexus_back \
  -- -m uvicorn app:app --host 0.0.0.0 --port 8083
cp ~/.pm2/dump.pm2 ~/.pm2/dump.pm2.$(date +%F)   # respaldo fechado antes de pisar
pm2 save
curl -s localhost:8083/health                     # exige "environment":"produccion"
```

Se clona por **HTTPS**: el repo es público, así que no hace falta llave SSH en el
server, y `deploy.sh` solo hace `checkout`/`fetch`/`pull` — nunca push.

**Nada de `source venv/bin/activate` antes del `pm2 start`.** pm2 congela el
`process.env` de la shell en el proceso y en `dump.pm2`, y como `load_dotenv()`
no sobrescribe lo que ya viene del entorno, cualquier variable exportada en esa
shell le gana al `.env` **de forma permanente y silenciosa**. Por eso el
`pip install` se hace con `venv/bin/python -m pip`, que no necesita activate.

**Verifica el dump por nombre, no por conteo:** `pm2-logrotate` es un módulo pmx y
no entra a `dump.pm2`, así que `pm2 list` mostrará una fila más que el dump.
Confirma que aparece `nexus-back-api` y que siguen las 12 previas.

**Un solo proceso, uvicorn puro** — a propósito. El `gunicorn --workers 4` de
`document_ai` no es el molde: esa app es stateless, y esta abrirá conexiones a
SQL Server, donde 4 workers son 4 pools ODBC independientes. Se sube después, si
el DBA confirma cuántas conexiones tolera.

### 3. Registrar en webhook-central

Las tres piezas van **en el server** (`/home/mbriseno/webhook-central`), editadas
**ahí mismo**, y luego commit + push desde el server el mismo día.

**No subas el clon local por `scp -r`**: eso pisa `hooks.json`, `apps.json` y
`scripts/deploy.sh` de los 12 proyectos de golpe y sin respaldo. Y hay divergencia
comprobada — el clon local registra `conmutador` en las tres piezas, pero en el
server no existe ni el proceso pm2, ni el puerto, ni la carpeta; mientras
`webhook-listener` corre y no está en `apps.json`.

Respaldar antes de tocar nada:

```bash
cd /home/mbriseno/webhook-central
cp hooks.json hooks.json.bak.$(date +%F) && cp apps.json apps.json.bak.$(date +%F)
```

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
2. `hooks.json` — copiar una entrada existente y cambiarle `id`,
   `response-message` y el `name` de `pass-arguments-to-command` (ojo: las
   entradas **no** tienen un campo `name` de primer nivel; ese `name` de adentro
   es lo que llega como `$1` a `deploy.sh`). Los indispensables son `id`,
   `execute-command` y `pass-arguments-to-command`: sin `execute-command` el hook
   no ejecuta nada, y el `id` es lo que forma la URL.
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
idéntico en el nombre del `.conf`, en el `name` de `pass-arguments-to-command` y en
el `id` de `apps.json`; el `id` del hook es una cuarta cadena
(`despliegue-nexus_back`) que solo debe coincidir con el `deploy_url`.

**El `.conf` no debe definir `APP_ENV`.** Si lo define, `deploy.sh` cambia el
restart a `pm2 restart --update-env`, y entonces cada deploy rutinario reemplaza
el entorno del proceso por el del `webhook-listener`. El `.conf` de arriba no lo
define: dejarlo así.

Validar y recargar:

```bash
python3 -m json.tool hooks.json > /dev/null && echo "JSON válido"
# json.tool NO detecta ids duplicados, que es justo el error del método
# "copiar una entrada": verificarlo aparte.
python3 -c "
import json,collections
ids=[h['id'] for h in json.load(open('hooks.json'))]
d=[k for k,v in collections.Counter(ids).items() if v>1]
print('ids duplicados:', d or 'ninguno')"
pm2 restart webhook-listener
```

Un `hooks.json` inválido tumba los deploys de los 13 proyectos, no solo este — y
con un reinicio pendiente, un `pm2 resurrect` sobre un JSON roto los deja muertos
en frío sin que nadie se entere.

Para probar el circuito completo, en dos pasos (el primero cae en `no_changes`, así
que **no** ejercita el camino del restart):

```bash
curl -X POST http://172.10.30.15:8090/hooks/despliegue-nexus_back   # -> no_changes
# luego un commit trivial al repo y otra vez, para probar el restart de verdad
tail -2 /home/mbriseno/webhook-central/logs/deploys.jsonl
```

### Notas de operación

- **ODBC: hay que distinguir tres cosas que se confunden.**
  1. Que `import pyodbc` funcione → solo necesita el runtime `libodbc.so.2`
     (paquete `libodbc2`), que **ya está** en el server. `pyodbc` 5.x publica
     wheel manylinux para cp312 que enlaza esa librería dinámicamente, así que
     `pip install` **no compila** nada y `unixodbc-dev` no hace falta.
  2. Que se pueda **conectar** a SQL Server → necesita el driver `msodbcsql18`
     del repo de Microsoft, que no existe en los repos de Ubuntu. Eso está
     pendiente y no urge hasta que el DBA entregue la base.
  3. Que exista el CLI `odbcinst` → no hace falta para nada en runtime, solo para
     diagnóstico. Dato no obvio: en Ubuntu 24.04 `apt install unixodbc` **no** lo
     instala; es su propio paquete. `msodbcsql18` lo arrastra por dependencia.

  Y cuando toque instalar `msodbcsql18`: no es un `apt install` aislado. Agrega el
  repo y el keyring de `packages.microsoft.com` al host de forma permanente, pide
  `ACCEPT_EULA=Y`, y crea o modifica `/etc/odbcinst.ini` en un server donde ya
  corre mariadb. Ventana propia.
- **El grupo `/documentos/*` se publica solo si hay base configurada.** Con
  `SQLSERVER_HOST` vacío, `app.py` no registra ese router y Swagger muestra
  únicamente lo que de verdad funciona (`/health`, `/health/db`, `/ia/ine`); el
  arranque loguea un `WARNING` y `/health` reporta
  `documentos_disponible: false`. Cuando el DBA entregue la base, basta llenar el
  `.env` y `pm2 restart nexus-back-api` — sin tocar código. Efecto secundario:
  sin `SQLSERVER_HOST` ni se importa `pyodbc`, porque ese import vive en la cadena
  del router.
- **`deploy.sh` no hace healthcheck** y un `pip install` fallido es solo una
  advertencia: puede reportar "✓ Desplegado" con la API en ciclo de restart.
  Después de cada deploy, `curl http://172.10.30.15:8083/health` a mano — y exigir
  que diga `"environment":"produccion"`. Si dice `local`, el `.env` no se leyó: es
  el único testigo que existe de eso, porque la app arranca igual sin `.env`.
- **El hook solo reinicia si hay commits nuevos.** `deploy.sh` compara `HEAD`
  contra `origin/main` y, si coinciden, imprime "Nada que desplegar" y hace
  `exit 0` **antes** del `pip install` y del `pm2 restart`. O sea: no sirve para
  recargar el `.env` tras editarlo a mano en el server — eso pide un
  `pm2 restart nexus-back-api` directo (**sin** `--update-env`: `config.py` lee el
  `.env` del disco en cada arranque, y `--update-env` traería el entorno de quien
  lanzó el comando, que es justo lo que no se quiere).
- **El puerto no vive en el `.env`.** El arranque real lleva `--port 8083` en la
  línea de comandos que pm2 guardó al crear el proceso; `PORT` del `.env` solo lo
  usa el bloque `__main__` de `app.py`, que en producción no corre. Para cambiar
  de puerto hay que borrar y recrear el proceso pm2, no basta un deploy.
- **No hay autenticación en ningún endpoint**, y `/ia/ine` cuesta dinero por
  página en Document AI. En la red interna es una decisión defendible, pero es
  una decisión: cualquier miembro de la empresa que alcance el puerto puede
  generar costo.
