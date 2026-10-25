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
| `servicios/` | Externo | **Todo lo que sale de este proceso** (Document AI, el almacén en disco, y más adelante SFTP/SharePoint) |
| `db/sqlserver.py` | Plomería | Conexión + helpers genéricos para invocar stored procedures |
| `verificar_*.py` | Pruebas | Scripts sueltos que se corren a mano y dicen sí o no. No hay pytest a propósito |

**Regla:** un `router` nunca importa `pyodbc` ni `httpx` directo — solo llama
funciones de `repositorios/` o `servicios/`. Así un endpoint puede combinar
ambas fuentes sin que el front se entere de dónde viene cada dato.

`servicios/` dice "Externo" en el sentido de **fuera de este proceso**, no
necesariamente de terceros: `almacen.py` escribe en un disco montado, no llama a
nadie, y aun así vive ahí porque es E/S que puede tardar y fallar por su cuenta.

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

### Versión del modelo: fija a propósito

`DOCAI_VERSION_INE` ancla la versión del modelo con la que se llama al
procesador. **Sin ella Google usa su "default"**, que él puede mover el día que
promueva otra versión a estable — y ahí la extracción deja de ser reproducible y
`extraction_run.engine_version` guardaría una suposición en vez de un hecho. Si
se deja vacía, la API arranca igual pero **loguea un warning** en cada llamada, y
`engine_version` viene en `null` (se prefiere decir "no sé" antes que inventar).

El procesador `ine` tiene 10 versiones, **todas administradas por Google** — o
sea lo "custom" del `CUSTOM_EXTRACTION_PROCESSOR` es el *esquema de campos*, no
un modelo entrenado a la medida. Hoy está anclado a
`pretrained-foundation-model-v1.5-pro-2025-06-20` ("Google Stable"), que era la
default al momento de fijarla y es la misma que el proyecto `document_ai` usa
para cédula. Para ver el inventario:

```
GET .../processors/<id>                    → defaultProcessorVersion
GET .../processors/<id>/processorVersions  → todas, con displayName y state
```

⚠️ **La confianza NO es reproducible aunque la versión esté anclada.** Medido:
dos llamadas seguidas al mismo documento con la misma versión devuelven valores
y posiciones idénticos, pero la `confianza` de algunos campos varía un poco
(ej. 98.74 → 97.18). Son modelos generativos (`MODEL_TYPE_GENERATIVE`) y esa
variación es inherente. Importa porque el diccionario dice que la confianza es
"lo que consumen motor de reglas y ruteo a revisión": un campo que quede pegado
a un umbral puede rutear a revisión en una corrida y no en la siguiente. Los
umbrales necesitan margen, o histéresis.

Prueba: `POST /ia/ine` con un `multipart/form-data` con campo `imagen`.

**Forma de la respuesta.** Sigue el diccionario de datos v0.4, para que
guardarla sea mecánico cuando exista SQL Server. Cada campo hoja es un
`entity_fact` en potencia, y `ocr` es la cabecera `ocr_result` con sus
`ocr_block`. `domicilio` sigue anidado, con la misma estructura en cada hijo:

```json
{
  "curp": {
    "value_raw": "ROVX970508MSRMLC08",
    "value_normalized": "ROVX970508MSRMLC08",
    "confianza": 100.0,
    "confianza_cruda": 0.9999771,
    "metodo_confianza": "extractor_confidence",
    "page_number": 1,
    "bloque_indice": 15,
    "posicion": { "x": 0.383, "y": 0.714506, "ancho": 0.263, "alto": 0.03858 }
  },
  "domicilio": {
    "estado": { "value_raw": "SON.", "value_normalized": "SON", "...": "..." }
  },
  "confianza_promedio": 99.31,
  "confianza_minima": 98.74,
  "ocr": {
    "engine": "document_ai",
    "language": "es",
    "page_count": 1,
    "paginas": [{ "page_number": 1, "ancho": 1000, "alto": 648, "unidad": "pixels", "idioma": "es" }],
    "bloques": [
      {
        "indice": 0,
        "page_number": 1,
        "block_type": "line",
        "texto": "MÉXICO",
        "bbox": { "x": 0.163, "y": 0.075617, "ancho": 0.044, "alto": 0.08179 },
        "confianza": 88.76,
        "orientacion": "PAGE_RIGHT",
        "orientacion_grados": 90
      }
    ]
  },
  "_metadata": { "procesado_en": "2026-08-18T18:44:25Z", "quality_alert": false }
}
```

Puntos donde esto se apega al diccionario y no a lo que da Google:

- **`value_raw` y `value_normalized` son dos cosas distintas.** El crudo es el
  texto tal como se leyó; el normalizado aplica la regla del campo (fechas
  completas a ISO, y la limpieza de puntuación del domicilio). Antes se pisaban
  y el original se perdía.
- **`confianza` va 0-100**, como `entity_fact.confidence numeric(5,2)`. Google
  la reporta 0-1; se convierte en la frontera y el original se conserva en
  `confianza_cruda` porque el diccionario lo pide: "permite recalibrar sin
  re-extraer".
- **`bloque_indice`** apunta por posición a `ocr.bloques`; es el precursor de
  `entity_fact.ocr_block_id`. Al persistir se cambia por el id real de la fila.

**La capa `ocr`** es lo que hace posible la trazabilidad visual (clic en un dato
→ resaltar su región). Se publican las **líneas** como `ocr_block`: medido en
una INE real hay 13 blocks / 22 paragraphs / 23 lines / 57 tokens por página, y
las líneas son el punto donde cada campo extraído solapa exactamente una sin
inflar el volumen (el diccionario advierte que `ocr_block` será "la tabla más
grande del sistema por órdenes de magnitud"). La granularidad se cambia en
`servicios/ocr.py::GRANULARIDAD`.

⚠️ Los bloques de OCR **sí vienen rotados** (`orientacion: PAGE_RIGHT` en la
INE), a diferencia de las entidades. Su `bbox` es la caja envolvente alineada a
los ejes, que es la forma que pide `ocr_block.bbox_*` pero es más grande que la
extensión real del texto. Para resaltar alcanza; para el contorno exacto habría
que guardar los cuatro vértices.

**Lo que todavía NO cumple del diccionario**, porque necesita la base: los
campos ausentes deberían generar renglón con `null_reason`, y para saber cuáles
se esperaban hace falta el catálogo `field_definition`. Hoy un campo que
Document AI no encontró simplemente no aparece.

A nivel raíz vienen **dos** resúmenes de la calidad, los dos en escala 0-100 y
calculados sobre **todos** los campos, incluyendo los de `domicilio`. Contestan
preguntas distintas y por eso viajan los dos:

| Llave | Qué contesta | Quién la usa |
|---|---|---|
| `confianza_promedio` | Qué tan bien salió la extracción en conjunto | La interfaz, en "Nivel de confianza obtenida" (desde el 2026-09-10) |
| `confianza_minima` | Cuál es el peor campo | Nadie todavía; es el número que delata un dato concreto mal leído |

El promedio es lo que se muestra porque es la pregunta que se hace quien mira la
pantalla. El mínimo se conserva porque es justo lo que el promedio esconde: un
solo campo mal leído —la CURP, digamos— se diluye si el resto de la credencial
salió perfecta. Hasta el 2026-09-10 la interfaz mostraba el mínimo.

Los dos se calculan **antes** de agregar la capa de OCR, a propósito: los
bloques también traen `confianza`, pero esa es de LECTURA, no de extracción, y
mezclarlas daría un número sin significado. Las dos vienen en `None` cuando
`_metadata.quality_alert` es `true` (ver abajo) — ahí no hay campos.

`_metadata.procesado_en` es la fecha/hora (UTC, formato ISO) en que **ese
llamado** a Document AI terminó. Se captura una sola vez, del lado del servidor,
justo al recibir la respuesta — Google no manda un timestamp propio en su JSON.
No confundir con `fecha_registro` (esa sí es un campo de la credencial, la
fecha impresa en la INE). Cuando el front guarde estos datos en SQL Server, debe
**cargar este mismo valor tal cual**, no regenerarlo en ese momento: si lo
regenera, deja de significar "cuándo procesó Document AI" y pasa a significar
"cuándo se guardó" — que puede ser minutos u horas después si alguien revisó
campos de baja confianza antes de confirmar. La fila en la base querrá además
su propia columna de `created_at`/`fecha_registro` con `DEFAULT GETUTCDATE()`
(o equivalente), para "cuándo se insertó el registro" — un dato distinto, que
debe generar la base y nunca el cliente.

`_metadata.quality_alert` es `true` **solo** cuando Document AI respondió `200`
pero sin ninguna estructura de documento reconocible — la imagen no es una INE,
o es tan ilegible que no se reconoce como una. En ese caso viene junto con
`_metadata.motivo` (`"Calidad del OCR inferior al umbral requerido o nulo"`), y
la respuesta no trae ningún campo del documento, y las dos confianzas resumen
quedan en `None`. En cualquier otro caso `quality_alert` es `false` y la llave `motivo`
**ni siquiera aparece** en el diccionario.

Esto es DISTINTO de un `502 Bad Gateway` (`{"detail": "No se pudo procesar la
credencial con Document AI."}`), que sigue existiendo para cuando la llamada a
Document AI **misma** falla — credenciales vencidas, cuota agotada, red, Google
caído, o una imagen tan corrupta que ni siquiera es un archivo de imagen
válido. Ese caso a propósito **no** se convierte en `quality_alert` y a
propósito NO comparte redacción con `motivo`: es una falla del servicio, no de
la calidad del documento, y disfrazarla de "hay que pedir otra foto" escondería
un problema operativo real.

## API de clientes: mandar archivos a la bandeja (desde el 2026-09-30)

Por aquí un cliente manda documentos a la **bandeja de preparación** del front
(el panel de en medio), donde aparecen solos con origen **«API REST»**, en
menos de 10 segundos, sin que nadie suba nada a mano.

```bash
curl -X POST http://<servidor>:8083/bandeja/   -H "X-API-Key: nxdoc_live_XXXX_sk_…"   -F "archivo=@ine_juan_perez.pdf;type=application/pdf"
```

- **La llave es una API Key de cliente**, emitida en el módulo "API Key" del
  front (menú del engrane). El cliente NO manda `tenant`: sale de la llave. Si
  lo manda y es otro, es 403 y no se sube nada.
- Esa llave abre **solo** `POST /bandeja/`. Todo lo demás (`/ia`,
  `/procesadores`, `/archivos`, `/llaves`, listar o retirar de la bandeja) es
  401 con ella.
- Acepta **PDF, JPEG, PNG y TIFF** de hasta 20 MB: lo que el pipeline sabe
  procesar. Otro formato da 400 con un mensaje que lo dice.
- Responde **201** con la entrada creada (`id`, `rutaRelativa`, `sha256`,
  `tamanoBytes`, `mime`, `nombreOriginal`, `canal`, `llaveId`, `recibidoEn`).
- Mandar dos veces el mismo archivo no duplica disco; en la bandeja el segundo
  sale marcado como duplicado.
- Revocada o vencida, la llave deja de servir en la siguiente petición.

También acepta la llave de servicio del front (`NEXUS_API_KEY`), y ahí `tenant`
es obligatorio. **Esa llave no se le da a un cliente**: abre todo.

### Swagger: una documentación para clientes y otra interna

- **`/docs` es la PÚBLICA**: solo "Mandar un documento", con textos para quien
  integra, y el formulario pide únicamente el archivo. Se prueba con una API Key
  de cliente: **Authorize** → **Try it out** → archivo → **Execute**.
- **`/docs-interno` tiene todo** (bandeja, llaves, IA, procesadores, archivos),
  para el equipo.

La pública se DERIVA del esquema completo en `documentacion.py`, así que no
puede desfasarse de lo que la API hace. Ocultar rutas no es la seguridad —eso
lo dan las llaves—, es orden. Cuando se publique la API hacia fuera, solo se
expone la pública. ReDoc está apagado.

Lo usa también el front: `GET /bandeja/?tenant=` para listar lo pendiente, y
`POST /bandeja/{id}/retirar` (`tenant`, `motivo`: `pipeline` | `descartado`)
cuando el documento pasa al pipeline o se descarta, para que no reaparezca.
Retirar NO borra el archivo del almacén.

### Las API Keys de cliente

Las emite, lista y revoca el front contra `/llaves/` (solo con la llave de
servicio). El servidor genera la llave, guarda **solo el SHA-256 del secret** y
devuelve el secret una única vez. Formato y verificación en
`seguridad_llaves.py` (espejo de `src/lib/apiKeys/formato.ts` del front);
quién acepta qué, en `seguridad.py`.

### Uso y métricas por llave

Cada llamada con una API Key de cliente que EXISTE queda registrada
(`servicios/uso_llaves.py`, desde un middleware: cuentan también las rechazadas,
como las de una llave revocada que sigue llamando) y alimenta "Métricas" en
el front vía `GET /llaves/{id}/metricas?tenant=&desde=&hasta=`. Cada llave
tiene un **tope semanal** (`LIMITE_SEMANAL`, 500,000; lunes a domingo, UTC):
al llegar, la API responde **429** a esa llave hasta el lunes. El conteo
semanal vive en memoria y se calienta del registro al arrancar (un proceso).

**El periodo son dos instantes con zona, `[desde, hasta)`**, no fechas: el
registro guarda UTC pero "hoy" es el día de quien mira, y con fechas leídas
como días UTC lo que entraba después de las 18:00 en México (ya es mañana en
UTC) quedaba fuera de "hoy". Para el 30 de septiembre en México:
`desde=2026-09-30T00:00:00-06:00` y `hasta=2026-10-01T00:00:00-06:00` (el `+`
de un desfase positivo va como `%2B` en la URL). Sin zona responde 400. El
tope semanal NO sigue la zona del periodo: es siempre la semana natural UTC,
porque es lo que el servidor aplica.

### La llave se revisa antes de leer el cuerpo

FastAPI parsea el `multipart` ANTES de resolver la dependencia que exige la
llave, así que una subida sin llave válida se recibía ENTERA —hasta el tope de
20 MiB— y recién entonces contestaba 401 (medido: 19 MB a 4 MB/s, 4.5 s y todos
los bytes enviados). Desde el 2026-10-01 la API está publicada en internet, así
que el middleware `autenticar_y_medir_las_llaves` de `app.py` resuelve la llave
primero y contesta el 401 **sin leer un byte** (medido: 0.00 s, 0 bytes). Solo
corta cuando la petición trae cuerpo, para no pedirle llave a `/health` ni a
`/docs`, que están abiertos a propósito; que ninguna ruta abierta reciba cuerpo
lo comprueba `verificar_llaves_cliente.py` recorriendo el OpenAPI. La llave se
resuelve UNA vez por petición y se comparte con la dependencia del endpoint vía
`request.state` (`seguridad.analisis_de`), así que no se lee el registro dos
veces. Verificado con sockets reales en `probar-corte-temprano.mjs`.

**Un efecto del proxy, medido el 2026-10-01.** Contestar antes de leer el
cuerpo es lo estándar (el cliente manda `Expect: 100-continue` y el servidor
responde sin que se suba nada), pero el Apache de Soporte TI no sabe relevar
esa respuesta temprana: la convierte en un **500 HTML**. Pasa SOLO con llave
inválida Y cuerpo grande Y un cliente que use `Expect` (curl lo activa arriba
de ~1 MB). Medido por el dominio con llave mala: hasta 1 MB llega el 401
limpio; de 5 MB en adelante sale el 500. Sin `Expect` —que es lo que hacen los
navegadores y la mayoría de las librerías— siempre llega el 401 limpio. Por la
IP interna el 401 siempre es limpio. Se queda así: lo degradado es la claridad
del error en una petición que de todos modos iba a fallar por autenticación, y
a cambio el servidor no recibe los bytes. Está pedido a Soporte TI.

Al mismo Apache le pasa con el **413**, por lo mismo: `limitar_tamano_subida`
también contesta sin leer el cuerpo.

**El tope de tamaño es NUESTRO, no del proxy** (medido el 2026-10-01, y
corrige lo que se creyó primero). `MAX_SUBIDA_MB` son 20, o sea 20 MiB
exactos, y es lo que dice el Swagger público. Apache no tiene ningún límite
en medio: sin `Expect`, una subida de 60 MB por el dominio llega hasta el
back y la rechaza NUESTRO 413 con su mensaje. Lo que se vio como "el proxy
corta en 20 MiB" era nuestro 413 temprano convertido en 500. O sea que no hay
nada que pedirle a Soporte sobre el tamaño.

### Provisional

**Dónde viven las cosas.** Lo pendiente de la bandeja, las llaves y su uso
están en registros de solo agregar dentro del NAS (`{ALMACEN_RUTA}/.registro/`, ver
`servicios/registro.py`), no en SQL Server. Pasan a la base cuando existan los
SPs del DBA; solo cambian `servicios/bandeja.py` y
`servicios/llaves_cliente.py`.

**Un tenant por organización** (desde el 2026-10-09, HU11). El front saca el tenant de la organización de la
sesión (`tenantDe`); el administrador de plataforma, que no tiene organización, opera en el espacio de plataforma:
`NEXUS_TENANT` en el `.env` del front (default `demo`). Y desde el 2026-10-14 este back no se fía de ese cálculo: ver
«Aislamiento entre organizaciones», al final.

**El dominio de los clientes** es `nexus-doc-api.buzzword.com.mx` (proxy de
Soporte TI hacia el 8083, con certificado válido). Se pidió que reenviara solo
`/bandeja/`, `/docs` y `/openapi.json`, pero el 2026-10-01, al estrenarlo, se
vio que reenvía TODO, incluidos `/docs-interno` y `/health/db` (que sin llave da
la versión de SQL Server y el nombre de la base). Por eso el back se protege
solo, sin depender del proxy: `superficie_publica.py` y un middleware en
`app.py` hacen que **lo que llega por un nombre público** (`Host`,
`X-Forwarded-Host` o `Forwarded`; ver `NEXUS_HOSTS_PUBLICOS`) **solo pueda usar
`POST /bandeja/`, `GET /docs` y `GET /openapi.json`**; lo demás responde 404,
incluso con la llave de servicio. El front llama por IP, y eso no cambia. Si el
proxy no conservara ningún nombre, la guarda no lo vería: al estrenar un dominio
se comprueba desde fuera que `/docs-interno` responde 404.

Verificarlo, offline: `venv/bin/python verificar_bandeja.py` y
`venv/bin/python verificar_llaves_cliente.py`.

## Webhooks de cliente: el registro (desde el 2026-10-01)

El módulo "Webhooks" del front registra aquí los endpoints a los que NexusDoc le
avisa a un cliente cuando un documento suyo termina de procesarse, falla o es
rechazado. Desde el 2026-10-01 los avisos **se envían**: ver "Los avisos de
eventos", abajo.

| Ruta | Qué hace |
|---|---|
| `POST /webhooks/` | Lo registra ACTIVO y devuelve su secret de firma (`whsec_` + 48 hexadecimales) **una sola vez**, con `Cache-Control: no-store`. 409 si el cliente ya tiene esa URL (se compara normalizada: esquema y host en minúsculas, sin el puerto de siempre). |
| `GET /webhooks/?tenant=` | Los vigentes, del más nuevo al más viejo. Nunca trae el secret ni su cifrado. |
| `POST /webhooks/{id}/estado` | Lo activa o desactiva. |
| `POST /webhooks/{id}/eliminar` | Lo da de baja. Repetirlo no es error. |
| `POST /webhooks/{id}/validar` | Le manda al endpoint un **aviso de prueba firmado** hasta 5 veces y lo da por validado en el primer 2xx; si ninguno entra, queda **"Con fallos"** (`fallidaEn`). Un endpoint que no responde bien no es error de esta llamada: contesta 200 con `validado: false`, el motivo, el código y cuántos intentos hubo. 429 si se repite muy seguido o hay demasiadas validaciones en curso. |
| `GET /webhooks/{id}/intentos` | El **historial de intentos**: cada vez que NexusDoc le habló al endpoint —validaciones y entregas—, con hora, tipo, número de intento, código, ms, motivo y, en las entregas, el `entradaId`. |
| `POST /webhooks/eventos` | El front avisa que un documento terminó; se programa una entrega por cada webhook que deba recibirlo. |
| `GET /webhooks/{id}/metricas` | Solicitudes, errores, tasa de error y P50/P90/P99 de sus entregas en un periodo, contra el anterior. |

Las cuatro piden la llave de servicio (solo las usa el front) y por el dominio
público responden 404, como todo lo que no sea `POST /bandeja/`. La URL se
valida con la misma regla que en el front —`https://`, o `http://` solo hacia
localhost, sin usuario ni contraseña y sin fragmento—, porque la del front se
puede saltar. Eventos: `documento.completado`, `documento.fallido`,
`documento.rechazado` y `expediente.completado`.

**El secret se guarda CIFRADO, no hasheado.** Al revés que una API Key —que la
presenta el cliente y aquí solo se comprueba—, el secret de un webhook lo usa el
SERVIDOR para firmar cada aviso, así que tiene que poder recuperarlo. Se cifra
con Fernet y la llave `WEBHOOKS_CLAVE_CIFRADO` del `.env`, que nunca va al NAS:
quien lea el registro no puede firmar avisos falsos. Sin esa llave el alta
responde 503 y no se guarda nada.

> **La llave no se cambia ni se pierde.** Lo cifrado con ella ya no se puede
> descifrar con otra, y esos webhooks habría que volver a crearlos. Respáldala
> aparte. Generarla (una por ambiente):
> `python3 -c "import base64,os; print(base64.urlsafe_b64encode(os.urandom(32)).decode())"`

**Provisional**: un registro de solo agregar en el NAS
(`{ALMACEN_RUTA}/.registro/webhooks.jsonl`), como el de las llaves, hasta que
el DBA entregue las tablas; solo cambia `servicios/webhooks_cliente.py`. Como
no se reescribe nada, eliminar un webhook deja su línea —con el secret
cifrado— en el archivo, sin usarse.

### Validar la conexión (desde el 2026-10-01)

Un webhook nace **sin validar** (`validadoEn: null`) y no recibirá avisos hasta
que su endpoint responda 2xx a un aviso de prueba. El front, mientras tanto,
solo le ofrece "Validar conexión" (y su historial, y eliminarlo).

**Hasta 5 intentos.** Validar manda el aviso de prueba hasta 5 veces, con el
mismo `webhook-id` y esperas de 1, 2, 4 y 8 s entre intentos, y se detiene en el
primer 2xx. Si ninguno entra, se anota `validacion_fallida` y el webhook queda
**"Con fallos"** (`fallidaEn`) hasta que se valide. No se reintenta lo que no
puede cambiar —una dirección interna, una URL que no es https—: ahí es un solo
intento. Lo peor, cinco tiempos agotados, tarda ~65 s y ocupa un hilo, así que
se admiten 3 validaciones a la vez en total y una sola por webhook (si no, 429).
Cada intento queda en el **historial** (`GET /webhooks/{id}/intentos`); las
métricas, en cambio, cuentan solo los avisos: las validaciones son pruebas.

**Editar** (`POST /webhooks/{id}/editar`) cambia URL y eventos y conserva el
secret. Solo **cambiar la URL** lo deja otra vez sin validar —el endpoint es
otro— y cancela lo que tuviera pendiente; cambiar solo los eventos no toca la
validación ni lo pendiente. El evento `editado` lleva `urlCambio`; uno viejo sin
esa marca se lee como cambio de URL (ante la duda, que se valide de nuevo).

**La guarda contra SSRF** (`servicios/entrega_webhooks.py`). Validar es que el
servidor le haga una petición, desde la red de CSI, a una URL que escribió
alguien. Por eso, al ENTREGAR —no al registrar: el DNS puede cambiar entre las
dos—:

- solo `https://`;
- se resuelve el nombre y se rechaza si **cualquiera** de sus direcciones no es
  pública (privadas, loopback, link-local, reservadas, multicast, IPv4
  escondidas en IPv6);
- y además las de `WEBHOOKS_REDES_BLOQUEADAS`, por defecto **`172.10.0.0/16`**:
  la red interna de CSI usa 172.10.x.x, que **no es privada** según el estándar
  (las privadas son 172.16-172.31), así que para Python es "pública" y la regla
  anterior la dejaría pasar. Medido: `172.10.30.15` da `is_global=True`;
- se conecta a la IP ya revisada, con el nombre original en `Host` y en el SNI
  (el certificado se sigue revisando contra el nombre): un DNS que cambie
  entre la revisión y la conexión no se cuela;
- sin seguir redirecciones, sin proxies del entorno y sin leer la respuesta.

`WEBHOOKS_PERMITIR_LOCAL=1` deja validar contra localhost: **solo para
desarrollo**, nunca en el server. Y para que el botón no sirva de relevo de
ráfagas hacia terceros, se deja una validación cada 5 s por webhook y 20 por
minuto en total.

### Cómo se firma un aviso (lo que el cliente necesita)

Según **[Standard Webhooks](https://www.standardwebhooks.com/)**, de donde viene
el prefijo `whsec_`. El cliente puede verificarlo con las bibliotecas que ya
existen para casi cualquier lenguaje (`standardwebhooks`, `svix`), sin
programarlo a mano. Cada aviso es un `POST` con JSON:

```http
POST /webhooks/nexusdoc HTTP/1.1
Content-Type: application/json
webhook-id: msg_2c1f0e9a8b7d6c5e4f3a2b1c
webhook-timestamp: 1696180000
webhook-signature: v1,<HMAC-SHA256 en base64>

{"type":"webhook.validacion","timestamp":"2026-10-01T20:00:00+00:00","data":{"webhookId":"wh_...","mensaje":"..."}}
```

- Lo firmado es `{webhook-id}.{webhook-timestamp}.{cuerpo tal cual llegó}`.
- La llave HMAC-SHA256 es lo que va después de `whsec_`, **decodificado en
  base64**. La firma va en base64, precedida de `v1,`.
- El cliente debe rechazar marcas de tiempo de más de 5 minutos (repeticiones)
  y puede usar `webhook-id` para no procesar dos veces el mismo aviso.

`verificar_webhooks.py` comprueba la firma contra el **vector de prueba
publicado** por el estándar, y contra una verificación escrita aparte.

### Los avisos de eventos (desde el 2026-10-01)

`servicios/entregas_webhooks.py`. **De dónde salen:** el pipeline corre en el
navegador, así que el front le dice al servidor cómo terminó cada documento que
llegó por la API (`POST /webhooks/eventos`). Solo esos: son los únicos cuyo id
conoce el cliente, el `id` que recibió al subir. Lo subido a mano no se avisa.

| Resultado en el pipeline | Aviso | `motivo` |
|---|---|---|
| procesado | `documento.completado` | — |
| no soportado / no reconocido / sin tipo que aplique | `documento.rechazado` | `formato_no_soportado` / `documento_no_reconocido` / `tipo_no_identificado` |
| error del servicio | `documento.fallido` | `error_del_servicio` |
| tipo reconocido pero sin extractor o sin calibrar | `documento.fallido` | `tipo_sin_configurar` |

**No se le cree a ciegas al front** (hasta el 2026-10-07 se alcanzaba desde internet sin
iniciar sesión; hoy pide login y esta ruta, además, comprueba por su cuenta que la persona sea de ese cliente: ver
«Aislamiento entre organizaciones»): la entrada tiene que existir en la bandeja de ese cliente y
haber salido de ella hacia el pipeline; el tipo y el motivo vienen de listas
cerradas y el resto del cuerpo lo arma el servidor; y se acepta un resultado
final por entrada (repetir el mismo aviso no programa nada). Así, quien use el
front para mandar avisos falsos no puede inventar documentos ni multiplicar
envíos: a lo sumo adelantar el resultado de una entrada real. Cerrar eso del
todo es cerrar el front.

**La entrega.** Una por cada webhook validado, activo y suscrito a ese tipo. El
`webhook-id` y el cuerpo son los mismos en todos los intentos; la marca de
tiempo y la firma, de cada uno. Si no responde 2xx en 10 s se reintenta a los
5 s, 5 min, 30 min, 2 h y 5 h (seis intentos en ~7.6 h, el calendario de
Standard Webhooks recortado); agotados, queda `agotada`. Desactivar o eliminar
el webhook cancela lo pendiente.

**El trabajador** es un hilo que arranca con la app y revisa cada segundo. Lo
pendiente vive en memoria (un solo proceso uvicorn) y el registro
(`.registro/entregas-webhooks.jsonl`) es lo durable: al arrancar se reconstruye,
así que un reinicio no pierde avisos, a lo sumo los retrasa. Entrega de uno en
uno: con muchos clientes y endpoints lentos, para entonces un pool de hilos y la
tabla del DBA.

**Las métricas** salen de los intentos: cada uno es una solicitud; la tasa de
error es la de intentos fallidos; las latencias, de los que llegaron a la red.

**Lo que este diseño no cubre todavía:**

- Si el navegador no logra avisarle al servidor (tres intentos), ese aviso se
  pierde. Se va el día que el pipeline pase al servidor.
- Un endpoint que falla siempre no se desactiva solo.
- El cliente se entera de que terminó, pero todavía no hay ruta de la API para
  que consulte el resultado extraído.

El formato y cómo verificar la firma están en la documentación pública
(`/docs`), que es lo que lee el cliente.

Verificarlo, offline: `venv/bin/python verificar_webhooks.py`.

## Almacén de documentos (encendido en producción desde el 2026-09-25)

`servicios/almacen.py` guarda los **bytes** de un archivo subido en disco, y
`routers/archivos.py` es su puerta: `POST /archivos/` para subir y
`GET /archivos/{ruta}` para leer, los dos detrás de `X-API-Key`. Hoy lo usa el
asistente de configuración del front para los documentos de ejemplo: el catálogo
guarda un puntero (ruta relativa + sha256 + MIME) en vez de los bytes. La tabla
`file` todavía no existe, así que nada de esto queda registrado en SQL Server —
ver "Bytes primero, fila después" en el docstring del router.

Con `ALMACEN_RUTA` vacía el almacén queda apagado: los dos endpoints responden
503 y nada más cambia. Es un estado válido para una máquina de desarrollo que no
lo necesite.

**Dónde viven los archivos.** En el NAS de infraestructura de CSI, montado
en el server, con esta forma dentro de la raíz:

```
{tenant}/{aa}/{bb}/{sha256}      p. ej.  csi/3f/a9/3fa9c1…e04b
```

Tres decisiones que conviene entender antes de tocarlo:

| Decisión | Por qué |
|---|---|
| El nombre del archivo **es su SHA-256**, sin extensión | Subir dos veces el mismo documento ocupa un solo objeto. El front ya calcula ese hash para detectar duplicados en la Bandeja, así que no hay trabajo nuevo. Y el nombre es verificable: si el contenido se corrompe, deja de coincidir consigo mismo. El MIME vive en la base, no en la extensión. |
| Dedup **dentro de cada tenant**, nunca global | Una dedup global filtraría información entre clientes y volvería imposible borrar o poner cuota por tenant. Se prefiere gastar disco. |
| En la base se guarda la ruta **relativa** | El día que el NAS cambie de punto de montaje solo cambia `ALMACEN_RUTA`. Con rutas absolutas, mover el montaje obligaría a migrar datos. |

**La trampa de `borrar`.** Con dedup por contenido, dos filas de `file` —el mismo
documento en dos expedientes— apuntan al MISMO archivo. `almacen.borrar()` no
consulta nada: borra. Quien lo llame tiene que haber comprobado **en la base** que
ya no queda ninguna referencia. El módulo no tiene forma de saberlo.

**Es síncrono.** Escribir en un NAS puede tardar, así que los endpoints de
`routers/archivos.py` son `def` normales y **no** `async def`, para que FastAPI
los corra en su threadpool y no congelen el event loop. Misma regla que ya
aplica a `pyodbc` en este proyecto, por el mismo motivo. Quien agregue otro
endpoint que toque el almacén tiene que seguirla.

### Cómo está montado en el server

El recurso SMB `//172.10.30.58/Nexus` del NAS, montado por CIFS en `/mnt/nexus`.
La raíz del almacén es una subcarpeta, para que el recurso pueda guardar otras
cosas sin revolverse con el árbol de hashes:

```
ALMACEN_RUTA=/mnt/nexus/documentos        # en el .env del server
```

La línea de `/etc/fstab`:

```
//172.10.30.58/Nexus /mnt/nexus cifs credentials=/etc/nexus-nas.cred,uid=mbriseno,gid=mbriseno,file_mode=0660,dir_mode=0770,iocharset=utf8,_netdev,nofail 0 0
```

- `credentials=`: usuario, contraseña y dominio viven en `/etc/nexus-nas.cred`
  (de root, `chmod 600`), **nunca** en este repo ni en el `fstab`. Se edita con
  `sudo nano`: un heredoc dejaría la contraseña en el historial de bash.
- `uid`/`gid`: SMB no conoce los usuarios de Linux; esto hace que todo aparezca
  como de `mbriseno`, que es quien corre pm2.
- `_netdev,nofail`: montar cuando ya hay red y, si el NAS no contesta al
  arrancar, arrancar de todos modos. Sin `nofail` el server puede quedarse
  colgado en el boot.

**El centinela.** `/mnt/nexus/documentos/.nexus-almacen` es un archivo vacío que
marca "esta carpeta ES el almacén". Si el montaje se cae, `/mnt/nexus` sigue ahí
como carpeta vacía del disco local; sin el centinela el almacén escribiría en el
disco equivocado y esos archivos quedarían tapados al volver el NAS. Con él,
subir y leer responden 503 y no se escribe nada. **No lo borres.**

**Si un día responde 503** con "no se encuentra '.nexus-almacen'":

```bash
mount | grep nexus          # ¿está montado?
sudo mount -a               # remonta lo del fstab
sudo dmesg | tail -5        # el motivo si no monta (error 13 = credenciales)
```

Hoy monta con una cuenta de dominio personal: cuando cambie esa contraseña, el
siguiente remontaje falla con error 13. Se corrige editando el `.cred` y
corriendo `sudo mount -a`. Está pendiente una cuenta de servicio de Soporte TI;
cuando llegue, solo cambia ese archivo.

### Verificarlo

```bash
venv/bin/python verificar_almacen.py                               # en un temporal
venv/bin/python verificar_almacen.py --en /mnt/nexus/documentos    # DENTRO del NAS
venv/bin/python verificar_subida.py                                # los endpoints
```

Sin argumentos corre **offline** en una carpeta temporal que crea y borra él
mismo: no necesita NAS, ni credenciales, ni red, y no toca nada real. Comprueba
lo que fallaría en silencio: que la escritura sea atómica (temporal +
`os.replace`, sin dejar `.tmp-*` tirados), que no haya dedup entre tenants, que
ninguna ruta pueda salir de la raíz, y que con el centinela ausente no se
escriba ni se diga "no está" de lo que sí está.

Con `--en <ruta>` corre lo mismo dentro de esa ruta, en una subcarpeta propia
que borra al terminar. Es la prueba de aceptación de un montaje nuevo: un
recurso SMB no se comporta como disco local (renombrado atómico, `fsync`,
permisos), y esto lo contesta con datos. Pasó sobre el NAS el 2026-09-25.

`verificar_subida.py` prueba `POST`/`GET /archivos/` en proceso con
`TestClient`, también offline: llave, rechazos, idempotencia, separación por
cliente, el MIME de lectura contra XSS, y el 503 con el montaje caído.

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
   PM2_START_CMD='pm2 start /home/mbriseno/code/nexus_back/venv/bin/python --name nexus-back-api --interpreter none --cwd /home/mbriseno/code/nexus_back -- -m uvicorn app:app --host 0.0.0.0 --port 8083'
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

## Acceso: login con JWT (Sprint 1, desde el 2026-10-07)

HU01 (alta de administradores), HU03 (login), HU04 (cambio de contraseña en el
primer acceso) y HU10 (recuperar la contraseña, más abajo). Código: `routers/auth.py`, `servicios/auth.py`
(argon2id, reglas, JWT, refresh) y `servicios/usuarios.py` (registro
PROVISIONAL `usuarios.jsonl`, espejo de los SPs pedidos en **NEX-319**; cuando
lleguen, cambia solo ese módulo). Verificación: `verificar_auth.py`.

**Configurar** (los dos procesos, back y front):

```
AUTH_JWT_SECRET=<mínimo 32 caracteres; python -c "import secrets; print(secrets.token_urlsafe(48))">
```

El mismo valor en el `.env` de `nexus_back` (firma los JWT) y en el del front
(los verifica con `jose`, y además le pregunta a `GET /auth/sesion` si la sesión sigue viva). Sin él, `/auth/*` responde 503 y el front no
deja entrar a nadie.

**Crear un administrador de plataforma** (puede haber varios: cada corrida con otro correo crea otro; en el servidor; ahí el virtualenv es `venv/`, sin punto — en la laptop es `.venv/`):

```
cd /home/mbriseno/code/nexus_back-dev   # o nexus_back en prod
venv/bin/python bootstrap_admin.py --email alguien@grupocsi.com --nombre Nombre --apellidos "Apellidos"
```

Imprime la contraseña temporal UNA vez y, si este ambiente manda correo de
verdad, también le manda a la persona su bienvenida con los accesos (ver «La
bienvenida con los accesos temporales»; `--sin-correo` lo evita). Al entrar con
ella, la app obliga a cambiarla antes de mostrar nada.

**Endpoints** (`/auth/*`, con la llave de servicio; los llama solo el BFF del
front, que pone las cookies httpOnly `nx_acceso` y `nx_refresh`):

| Ruta | Qué hace |
|---|---|
| `POST /auth/login` | correo + contraseña → JWT de acceso (15 min) + refresh (7 días). Errores con `codigo`: `correo_invalido`, `correo_no_existe`, `credenciales` (+ `intentosRestantes`), `bloqueada` (423, + `hastaEn`), `desactivada` (403). |
| `POST /auth/refresh` | refresh vigente → tokens nuevos; el refresh anterior queda revocado. |
| `POST /auth/logout` | revoca la sesión de ESE refresh token y solo esa (las de otros dispositivos siguen). 204 aunque el token no exista o ya esté revocado; 503 solo si el almacén no responde. Deja un evento `LOGOUT` en la bitácora de auditoría. Su JWT deja de valer en este back al instante (401 `sesion_cerrada`) y en el front en unos segundos; ver *El JWT de acceso y la sesión cerrada* abajo. |
| `GET /auth/yo` | el usuario del Bearer. |
| `GET /auth/sesion` | ¿sigue viva la sesión de este JWT? (HU05, 2ª etapa). 200 con `estado`: `viva`, `cerrada` (se cerró o nunca existió), `desactivada` o `inexistente`; un 401 o 503 quiere decir que NO se pudo contestar. Lo consulta el guard del front, una vez cada pocos segundos por sesión abierta. |
| `POST /auth/contrasena` | primer acceso: `nueva` + `confirmacion` (cierra todas las sesiones); después: también `actual`. Reglas: 12 caracteres, mayúscula, minúscula, número, carácter especial. |

**La contraseña (HU04 CA-3).** Solo se exigen las cinco reglas de los chips (12
caracteres, mayúscula, minúscula, número, carácter especial): 400 `reglas`, con
`faltan`. **No hay regla de «contraseña obvia»**: ni palabras comunes, ni el
nombre o el correo de la persona, ni secuencias como `abcde` o `12345`. Existió
del 2026-10-14 al 2026-10-19 (`auth.contrasena_obvia`, 400 `contrasena_debil`) y
se QUITÓ a pedido de Moibe: el medidor del front solo cuenta las cinco reglas y
decía «Muy segura» de contraseñas como `Abcdef@182182`, que el servidor luego
rechazaba al final, y la lista de palabras era corta y fácil de esquivar
(`Bcdefg@182182` pasaba). El criterio de HU04 pedía "no contenga el nombre de
usuario ni palabras comunes de diccionario"; hoy eso NO se cumple, y
`Password123!` es una contraseña válida. Si se vuelve a pedir, que el front y el
back apliquen LA MISMA regla, para que el medidor no prometa lo que el servidor
rechaza. El primer acceso deja el evento
`FIRST_LOGIN_PASSWORD_CHANGED` (HU04 CA-5: la persona, la sesión desde la que
se cambió y cuántas sesiones se cerraron; nunca la contraseña ni su hash).

**Política** (del diseño; vivirá en `uspRecordLoginResult`): 5 intentos
fallidos seguidos → 15 minutos de bloqueo, que se levanta solo; los intentos
durante el bloqueo no lo alargan. La base nunca ve contraseñas ni tokens: se
guardan el hash argon2id y el SHA-256 del refresh.

### Recuperar la contraseña (HU10)

Tres pasos, los tres SIN sesión, porque quien los usa no puede entrar:

| Ruta | Qué hace |
|---|---|
| `POST /auth/recuperacion/solicitar` | "olvidé mi contraseña": manda el enlace al correo de la cuenta. Responde SIEMPRE lo mismo si el correo tiene forma de correo (`correo_invalido` 400 si no; `demasiados_intentos` 429 si rebasa un límite). |
| `POST /auth/recuperacion/revisar` | ¿el enlace todavía sirve? Lo pregunta la pantalla antes de pintar el formulario. Sin gastarlo. 410 `enlace_invalido` / `enlace_usado` / `enlace_vencido` (el mismo mensaje para los tres). No devuelve de quién es. |
| `POST /auth/recuperacion/confirmar` | contraseña nueva + confirmación + token. Valida la contraseña ANTES de gastar el enlace (un typo no lo quema), lo gasta, cambia la contraseña y **cierra todas las sesiones**. |

Código: `routers/auth.py` (los tres endpoints), `servicios/tokens.py` (enlaces de
un solo uso), `servicios/correo.py` (envío), `servicios/plantillas_correo.py`
(los textos), `servicios/auditoria.py` (bitácora y límites). Verificación:
`verificar_recuperacion.py` (una sección por criterio de aceptación).

**Decisiones, escritas como decisiones y no como omisiones** (conviene que Cesar
las conozca; NEX-241):

1. **CA-2 gana sobre CA-3 y sobre el Figma.** CA-2 dice que nunca se revele si
   una cuenta existe; CA-3 pide ofrecer a quien escribe un correo elegir entre
   el principal y el de recuperación, ambos enmascarados. No caben juntos: una
   pantalla de elección ES la prueba de que la cuenta existe. El enlace va al
   correo **principal** y la respuesta es siempre la misma. Por la misma razón
   no se dibuja el estado "El correo electrónico no existe" del Figma ni se dice
   "te enviamos un enlace a X" (ambos afirman o niegan que la cuenta existe).
   El teléfono de recuperación no se usa: no hay canal SMS.

   *Ojo:* el login (HU03) sí distingue "el correo no existe" de "contraseña
   incorrecta", porque así lo dibujó UX. Mientras eso siga, este endurecimiento
   no cierra del todo la enumeración. Es una decisión de producto para las dos
   pantallas juntas.

   **Revertido a pedido de QA el 2026-10-20** (incidencia HU10 CA4: «al ingresar un correo que no está registrado
   debería mandar la notificación en letras rojas "El correo electrónico no existe…"»). Con
   `RECUPERACION_DICE_SI_EXISTE` encendida —el valor por defecto— `/auth/recuperacion/solicitar` contesta
   `404 correo_no_existe` cuando el correo no tiene cuenta, y la pantalla dibuja el error del Figma. Una cuenta
   desactivada o sin primer acceso sigue recibiendo el 200 neutral. La solicitud se anota igual (`SIN_CUENTA`) y los
   límites de abajo se aplican antes. **Es una decisión de producto con costo de seguridad**: con ella cualquiera puede
   preguntar si un correo tiene cuenta (el login ya lo permitía). Para volver a CA-2 basta
   `RECUPERACION_DICE_SI_EXISTE=0` en el `.env` y reiniciar; `verificar_recuperacion.py` prueba los dos modos.

2. **CA-9 se resuelve en el servidor.** Una cuenta que nunca ha entrado no recibe
   enlace de recuperación, pero la pantalla le responde lo mismo que a todos; lo
   que cambia es el correo, que le dice que termine su primer acceso. Además es
   lo único correcto: restablecer apagaría en silencio `debeCambiar`.

3. **CA-7 cierra las sesiones y, desde HU05, también el JWT.**
   Restablecer revoca todas las sesiones: nadie puede renovar y los guards que
   validan un Bearer (`/auth/*`, `/usuarios/*`, `/organizaciones/*`) rechazan
   ya el JWT de esas sesiones (401 `sesion_cerrada`). Lo que decide solo el
   front (bandeja, llaves, webhooks...) lo rechaza su guard, que lo consulta en
   `GET /auth/sesion` (ver *El JWT de acceso y la sesión cerrada* más abajo).

4. **Restablecer levanta el bloqueo del NAVEGADOR**, no solo el de la cuenta.
   Quien agotó sus cinco intentos y por eso vino aquí no debe toparse con
   "cuenta bloqueada" al entrar con la contraseña nueva. Solo el del navegador
   que completó el restablecimiento.

5. **El correo se manda después de responder** (`BackgroundTasks`), para que el
   tiempo de envío no delate si la cuenta existe, y un fallo de envío no cambia
   lo que ve la persona. Queda una diferencia de milisegundos (con cuenta se
   crea un token y se escribe un renglón más) que no se pretende eliminar.

**Límites contra abuso** (`RECUPERACION_*` en `config.py`, en segundos salvo lo que
dice "por hora"). Esa pantalla la abre cualquiera y cada solicitud puede mandar un
correo: sin límite sería una forma de llenarle el buzón a alguien o de gastar el
relay de CSI. Cuentan TODAS las solicitudes, existan o no las cuentas (si no,
delatarían cuáles existen) y se aplican antes de mirar si la cuenta existe: 60 s
entre dos del mismo correo, 5 por hora por correo, 5 por hora por navegador, 30
por hora por IP y 200 por hora en total. Los de IP y total no dependen de nada que
el cliente controle (la cookie del navegador se puede no mandar).

**Auditoría.** `PASSWORD_RESET_REQUESTED` (una por solicitud válida, exista o no
la cuenta) y `PASSWORD_RESET_COMPLETED` caen hoy en `auditoria.jsonl`: es un
registro PROVISIONAL, como los demás. La tabla `audit_event` está en el
diccionario de datos pero todavía no existe en la base ni se le ha pedido al
DBA. Nunca llevan el token ni la contraseña.

**El correo.** `CORREO_MODO=archivo` (el default) deja cada correo como `.eml` en
`<ALMACEN_RUTA>/correos/` y no toca la red; con `smtp` sale de verdad (necesita
`CORREO_SMTP_HOST` y `CORREO_REMITENTE`); con `api` sale por el servicio HTTP de
ART Broker (la subsección de abajo). **Mientras en dev siga en `archivo`, nadie
recibe el enlace**: la HU está completa pero el correo se lee del disco.
`FRONT_URL` tiene que apuntar al front real de cada ambiente (en dev de CSI es
`https://dev-nexus-doc.buzzword.com.mx` —el dominio, no la IP—, no el localhost del
default), porque de ahí sale el enlace de todos los correos. Los `.eml` no se limpian solos.

#### Salir por el servicio de ART Broker (`CORREO_MODO=api`, desde el 2026-10-13)

TI todavía no entrega el relay SMTP, pero ART Broker ya tiene un servicio HTTP que
manda correo (Ray y Brandon, 2026-10-13). El modo `api` lo usa: pide un token con
`POST /OAuth/AuthToken` (`{usr, psd}`) y manda con
`POST /II_1_General_Notifications/Emailsending` (`mailRecipients`, `mailCc`,
`mailBcc`, `mailSubject` y `mailBody` en HTML) llevando ese token como `Bearer`.
Se enciende con cuatro variables del `.env` DEL SERVIDOR (nunca del repo, nunca de
un chat):

    CORREO_MODO=api
    CORREO_API_URL=https://<host del servicio>   # sin ruta; solo https (http solo a localhost)
    CORREO_API_USUARIO=<el usuario que dé ART>
    CORREO_API_CONTRASENA=<su contraseña>

Lo que hace el código, y por qué:

- **Un token para todos.** Se guarda en memoria hasta un minuto antes de su `exp` (si
  no es un JWT legible, cinco minutos); varios hilos con el token en frío piden UNO.
  Un 401 o 403 al enviar lo renueva UNA vez y reintenta.
- **No reintenta lo que pudo haber salido.** Un timeout o un 5xx no se repiten: el
  servicio pudo haber aceptado el correo y se duplicaría. Quien llama ya decide qué
  hacer con el `ErrorCorreo` (la recuperación contesta lo mismo exista o no la cuenta).
- **200 no es éxito por sí solo.** Un HTTP 200 con `error: true`, o con algo que no
  es JSON (una página de login de un proxy), es una falla.
- **Un solo destinatario.** `mailRecipients` es una cadena y el servicio podría
  partirla: no se admiten `;`, `,`, `<`, `>`, comillas ni espacios.
- **Nada de secretos afuera.** No sigue redirecciones, y ni el log ni el mensaje de
  error llevan el cuerpo del correo, sus enlaces, el token ni las credenciales,
  aunque el servicio los repita en su respuesta.
- **Sin adjuntos.** El envío con adjuntos del servicio recibe rutas de red del servidor
  de ART; aquí no se usa.

Lo que NO controlamos, y hay que pedirles a Ray y a Brandon: el **remitente** (lo
decide el servicio; sin uno propio de NexusDoc, un "restablece tu contraseña" de otra
marca parece phishing), **credenciales dedicadas** (las que nos pasaron son de ART
Broker y se comparten), la **vigencia del token**, los **límites de envío**, la
**forma de sus errores** y la **URL de producción**.

`python verificar_correo.py` prueba todo esto contra un servidor FALSO en localhost y
no toca el servicio real. La única prueba real que cabe es UN correo al propio
buzón de quien la corra, a mano y nunca en bucle: `python probar_correo.py
tu.correo@grupocsi.com` (en el servidor, o donde estén puestas las variables). Dice
si salió y por dónde, o por qué no, sin imprimir nunca el usuario, la contraseña ni
el token.

#### La bienvenida con los accesos temporales (HU01, HU02, HU06 CA-1; desde el 2026-10-13)

Al dar de alta a alguien —`bootstrap_admin.py` (superadmin), `POST /organizaciones/` (el
administrador de la organización) y `POST /usuarios/` (un usuario)— se le manda el correo de
bienvenida del diseño: su usuario, su contraseña temporal y el enlace a la pantalla de inicio de
sesión (`FRONT_URL`). Los textos son LITERALES del Figma, con tres presentaciones (Super
Administrador, administrador de una organización y usuario): `servicios/plantillas_correo.py`.
Lo manda `servicios/invitaciones.py`, EN LÍNEA (hace falta saber si salió) y con la espera acotada
a 10 s por llamada. Contesta una de tres cosas:

- **`enviado`**: salió de verdad (`CORREO_MODO` `api` o `smtp`). La contraseña NO se devuelve: la
  persona la tiene en su bandeja, y quien la dio de alta no tiene por qué verla.
- **`sin_canal`**: este ambiente no manda correo (`archivo`, `silencio`). Todo como antes: la
  contraseña se devuelve UNA vez y la pantalla la muestra. No se escribe un `.eml` con ella, porque
  en un servidor sería una contraseña en claro, guardada, en el NAS.
- **`fallo`**: había canal y no se pudo (el servicio no contestó, rechazó el envío). Igual que
  `sin_canal`, y la pantalla avisa en rojo que el correo no salió.

La respuesta del alta lleva `correoEnviado` y `correoMotivo`; `contrasenaTemporal` solo viene si el
correo NO salió. El alta deja el evento `USER_CREATED` en la bitácora (HU06 CA-1 y CA-7: el usuario
creado, su rol, quién lo dio de alta y qué pasó con su correo; nunca la contraseña). El script de
bootstrap imprime la contraseña SIEMPRE, salga o no el correo: es el respaldo de quien lo corre.

**La contraseña viaja por correo, como dibuja el diseño** (y, con `api`, por el servicio de ART).
Por eso el cuerpo jamás se registra, y por eso la cuenta nace obligada a cambiarla en el primer
acceso. El botón «Reenviar invitación» (HU06 CA-6) llama a `invitaciones.reenviar` por HTTP (ver abajo).

**La contraseña temporal CADUCA (HU06 CA-5, desde el 2026-10-13).** Sirve `TOKEN_ACTIVACION_HORAS`
(48 por default; admite fracciones —`0.01` es menos de un minuto, para probar sin esperar dos
días— y `0` apaga la caducidad) a partir del alta o de la última reemisión. Pasado ese tiempo,
entrar con ella —aunque sea la correcta— da **403 `activacion_vencida`** (el front lleva a «Tu enlace
de activación ha caducado»), no cuenta como intento fallido ni bloquea (queda `EXPIRED` en la bitácora
de intentos) y se dice DESPUÉS de verificar la contraseña: a una equivocada se le sigue diciendo
`credenciales`. Quien entró a tiempo y no terminó tampoco puede completar el primer acceso
(`POST /auth/contrasena` da el mismo 403 y le cierra las sesiones). Aplica a todos los que dan de alta
en pantalla, el administrador de una organización incluido; **NO** a los administradores de
plataforma (su alta es por línea de comandos y no habría quien les generara un acceso nuevo desde
pantalla) ni a quien ya puso su contraseña. Ojo al desplegar: las cuentas que ya estaban pendientes
y tengan más de 48 horas caducan en cuanto se despliega.

**Reemitir la contraseña** (`reenviar_invitacion.py --email x@y.com`, o `invitaciones.reenviar`): a una
cuenta que SIGUE en su primer acceso le genera una contraseña temporal NUEVA, invalida la anterior y
las sesiones que se hubieran abierto con ella, levanta los bloqueos, empieza otra vez las horas de
vigencia y le manda la bienvenida. Imprime la contraseña siempre. Deja el evento `INVITATION_REISSUED`
en la bitácora. A quien ya hizo su primer acceso se le niega: para eso está «Olvidé mi contraseña».
Es lo que se usa para quien dejó caducar su acceso; desde el 2026-10-13 también por pantalla (siguiente
párrafo). El script sigue sirviendo para el administrador de plataforma, que no tiene quién se lo reenvíe.

**«Reenviar invitación» por pantalla (HU06 CA-6, 2026-10-13):** dos endpoints, el mismo servicio.
`POST /usuarios/{guid}/reenviar-invitacion` (administrador de la organización, sobre un usuario de la
suya) y `POST /organizaciones/{guid}/usuarios/{usuario}/reenviar-invitacion` (administrador de
plataforma, sobre un usuario de esa organización: es quien le reenvía el acceso al ADMINISTRADOR de una
organización, que no tiene a nadie por encima en la suya). Sin cuerpo. La respuesta es la del alta:
`usuario` (el renglón del listado, ya actualizado) más `correoEnviado`, y `contrasenaTemporal` UNA vez
solo si el correo no salió (`correoMotivo`: `sin_canal` o `fallo`). Se niega con 409 `ya_activa` (ya hizo
su primer acceso) o 409 `desactivada` (reactívala antes: a una cuenta desactivada no se le manda nada);
`invitaciones.por_que_no_reenviar` decide ambos. 404 si el usuario no es de esa organización. Queda
`INVITATION_REISSUED` con el administrador responsable. La respuesta no se guarda en caché
(`Cache-Control: no-store`: puede llevar una contraseña).

Para que la pantalla pinte «Pendiente» y «Caducada», cada renglón del listado trae
`debeCambiarContrasena` y `accesoVencido` (pendiente Y ya pasaron las horas; HU06 CA-5).

Y `sesionActiva` (2026-10-14): ¿tiene una sesión sin cerrar y sin vencer? Cerrar una sesión (HU05) no
cambia el estatus —la cuenta sigue activa y puede volver a entrar—, así que sin este dato la pantalla se
veía igual antes y después de «Cerrar sesión». Sale de `usuarios.tiene_sesion`; una sesión sin fecha de
vencimiento legible se toma por vencida, igual que al canjear el refresh. Con la base hará falta que
`uspGetTenantUsers` (NEX-336) lo devuelva (p. ej. `hasActiveSession`): no está en lo pedido.

Se prueba en `verificar_invitaciones.py` (la plantilla, el servicio, las altas por HTTP con un canal
simulado, el script de bootstrap, la caducidad con el reloj movido y la reemisión) y, en el navegador,
con `probar-correo-bienvenida.mjs`, `probar-activacion-caducada.mjs` y `probar-reenviar-invitacion.mjs`.

### El JWT de acceso y la sesión cerrada (HU05)

**Dos etapas, las dos HECHAS (la 1ª el 2026-10-12, la 2ª el 2026-10-13).** Cerrar
una sesión —voluntaria (HU05 CA-1), forzada (HU05 CA-4), por restablecer la
contraseña (HU10 CA-7) o por desactivar la cuenta— revoca la sesión. Hasta el
2026-10-12 eso solo impedía RENOVAR: el **JWT de acceso** ya emitido seguía
valiendo hasta que venciera (`AUTH_ACCESO_MIN`, 15 minutos; medido: 897 de 900
segundos), también para ESCRIBIR y para páginas de administrador. Lo más grave que
se midió: con el JWT de un administrador al que se le forzó el cierre se podía
crear un usuario, hacerlo administrador, y ese usuario tenía una sesión de 7 días
que sobrevivía al cierre.

**1ª etapa (2026-10-12): lo que valida este back.** Los tres guards que validan un
Bearer (`_usuario_del_bearer`, `_admin_de_organizacion`, `_exigir_admin_plataforma`)
comprueban, en la misma lectura del registro (`usuarios.por_guid_y_sesion`), que la
sesión del `sid` del JWT siga viva. Si no, **401 `sesion_cerrada`**; el front, al
verlo en las pantallas de Usuarios y Organizaciones, borra las cookies y manda al
login con `?volver=`. Una sesión está viva si existe, es de ese usuario y no está
revocada por ningún motivo; `ROTATED` cuenta como revocada (al renovar se emite una
sesión nueva con su propio JWT, y el front solo renueva cuando el anterior ya
venció: si algún día renovara por anticipado, `verificar_auth.py` 4c lo detecta).
Una sesión que no existe en el registro, o que es de OTRA persona, tampoco vale
(ante la duda, se rechaza). Una cuenta **desactivada** sigue recibiendo su 403
`desactivada`, que se comprueba antes.

**2ª etapa (2026-10-13): lo que decide solo el front.** Las rutas que decide solo el
front —bandeja, llaves, webhooks, archivos, procesadores: unas veinte rutas `/api`—
mandan al back solo la llave de servicio, no el Bearer, y las protege el guard de
`hooks.server.ts`. Ese guard verificaba el JWT con `jose` y nada más, así que
aceptaba el de una sesión cerrada (o de una cuenta desactivada) hasta que venciera.
Ahora, además, le pregunta a este back `GET /auth/sesion` con el JWT como Bearer. Si
la respuesta es `cerrada`, `desactivada` o `inexistente`, tira las cookies: las
pantallas van al login (o a "Tu cuenta se encuentra desactivada") y las rutas `/api/*`
contestan 401 `sesion_cerrada` (o 403 `desactivada`). Lo que hay que saber:

- **Una respuesta definitiva es un 200 con `estado`.** Un 401 o un 503 (también el del
  almacén caído) quieren decir que no se pudo contestar, y el guard no lo toma como
  sesión muerta: es una CONSULTA, no un intento de acceso.
- **Se deja pasar si el back no contesta** (caído, mal configurado, sin esta ruta o más
  lento que el tope de 2 s): un back con problemas no debe dejar a todo el mundo fuera
  de la app. Solo se rechaza con una respuesta explícita. Mientras el back no conteste,
  un cierre de sesión no se nota en esas rutas —igual que antes de la 2ª etapa—; lo que
  pasa por el back lo sigue rechazando él. Queda un aviso en el log del front
  (`[sesion] /auth/sesion ...: se deja pasar`). **Orden de despliegue: el back
  primero.** Con el front nuevo y el back viejo la consulta da 404 y la protección queda
  apagada sin más señal que ese aviso; después de desplegar hay que comprobar, con un
  JWT real, que `GET /auth/sesion` responde 200 `viva`.
- **La respuesta se guarda 3 segundos por sesión** en el front
  (`VIGENCIA_COMPROBACION_MS`): es lo máximo que tarda en notarse un cierre en esas
  rutas, a cambio de no preguntar en cada petición (como mucho una vez cada 3 s por
  sesión; la bandeja sola consulta cada 10 s). Si no se pudo saber, se recuerda 1.5 s,
  para que un back colgado no haga esperar su tope a cada petición. La clave es persona
  + sesión, y un JWT sin `sid` no se acepta (el back tampoco). Quien llega con el
  refresh viejo dentro de la gracia de la renovación (ver abajo) también pasa por esta
  pregunta, con el JWT nuevo: un cierre no se cuela por ahí.
- **Para que preguntar tan seguido fuera viable hubo que arreglar el registro
  provisional.** `usuarios._indice()` releía y repasaba `usuarios.jsonl` completo en
  CADA llamada. Ahora se guarda mientras el archivo no cambie (su "firma": ruta, inodo,
  tamaño y fecha; el registro es de solo agregar, y lo que agrega otro proceso, como
  `bootstrap_admin.py`, también se nota por el tamaño; una edición EN SITIO del mismo
  tamaño solo se nota por la fecha de modificación, y si cae en el mismo tick del reloj
  ni así: contradice el contrato del registro), y el repaso es lineal:
  antes cada revocación recorría todas las sesiones (20 mil eventos con 10 mil
  sesiones: de 1.4 a 2.2 s; ahora, de 12 a 60 ms). El índice se comparte entre quienes
  lo consultan y no se modifica; lo que sale hacia afuera (`_publico`) son copias.
  **Lo que NO resuelve:** tras CADA escritura (login, renovación, cierre)
  la siguiente consulta reconstruye todo, bajo el candado global del registro. Medido
  por los revisores: 78 ms con 10 mil renglones, 0.4 s con 100 mil y 3.6 s con 500 mil.
  Pasado ~2 s el guard deja pasar sin comprobar, y el back avisa en el log cuando una
  reconstrucción tarda más de 0.5 s (`verificar_auth.py` 4j). Con unas 50 personas activas son unos 3 mil
  renglones al día: es cuestión de meses. La salida es la base, no más caché.
- **Hoy `GET /auth/sesion` contesta `usuarios.por_guid_y_sesion` sobre ese registro, y
  con la base NO bastará con cambiar ese módulo.** `security.uspGetSession` (NEX-319)
  busca por el hash del refresh, y el JWT trae el `sid`, no ese hash: hace falta una
  consulta por (persona, sesión) que NO está en lo pedido al DBA. Está propuesta en
  `docs/solicitudes-dba.md` (6.2, `uspGetSessionStatus`) y falta pedirla. Se hizo sin
  esperar al DBA porque el registro provisional lo permite; el guard del front no
  cambia cuando llegue.
- **Renovar la sesión sin perderla (mismo día).** Revisándolo aparecieron dos defectos
  de fondo que esta etapa volvía más visibles. (1) En el front, varias peticiones con el
  mismo refresh renovaban cada una por su cuenta: un refresh es de un solo uso, así que
  el back rechazaba a las demás y esos rechazos tiraban las cookies recién emitidas.
  Ahora comparten UNA renovación (`renovarSesion`, con una gracia de 5 s para las que
  salieron con el refresh viejo, contada desde que el back CONTESTA: con un back lento
  de 6 s la gracia no se gasta esperando, y mientras la llamada está en curso nadie la
  reemplaza) y solo un rechazo EXPLÍCITO del back (401 `sesion_invalida` o
  `sesion_vencida`) tira las cookies: un 5xx, un timeout o el back caído las conservan y
  contestan 503. Si el front se rinde (30 s) justo después de que el back canjeó el
  refresh, la persona tiene que volver a entrar: su sesión nueva queda huérfana y vence
  sola. (2) En el back, `/auth/refresh` comprobaba, revocaba y creaba la sucesora en
  tres pasos sueltos: de un refresh podían salir varias sesiones vivas (de ocho
  peticiones a la vez, entre cinco y ocho según la ronda) y un cierre forzado a medias
  dejaba una sucesora viva. Ahora es un solo paso atómico (`usuarios.rotar_sesion`), y el login
  revalida, bajo el mismo candado, que la contraseña verificada siga siendo la vigente
  y la cuenta siga activa (`crear_sesion(hash_verificado=...)`).
- **El cambio de contraseña revalida igual (2ª revisión, 2026-10-13).** Las dos
  verificaciones con argon2 del cambio se hacen fuera del candado. Si en esa ventana el
  dueño restablecía la contraseña (lo que cierra TODAS las sesiones), el cambio de la
  sesión ya cerrada la pisaba: quien tuviera una sesión robada recuperaba la cuenta él
  solo (reproducido por HTTP). Ahora `fijar_contrasena(..., sesion_guid=...,
  hash_verificado=...)` comprueba, bajo el candado, que esa sesión siga viva y que la
  contraseña verificada siga siendo la vigente: si no, 401 `sesion_cerrada` o 401
  `actual_incorrecta` (otro dispositivo la cambió mientras tanto) y no escribe nada. Y un
  login que se rechaza en la revalidación ya no queda anotado como acierto: cuenta como
  contraseña mala (con sus `intentosRestantes`) o como cuenta desactivada.
- **Lo que queda abierto.** (a) `POST /auth/logout` lee la sesión por su refresh y la
  revoca después, en dos pasos: si una renovación canjea ESE refresh en medio, la sesión
  sucesora sigue abierta (no se guarda de quién viene). Con el front compartiendo la
  renovación, la ventana es de milisegundos. (b) El canje son dos escrituras: si falla
  la segunda (el disco, el NAS), la sesión queda cerrada y la persona debe entrar de
  nuevo; nunca quedan dos vivas. (c) `rotar_sesion`, `crear_sesion(hash_verificado)` y
  `fijar_contrasena(sesion_guid, hash_verificado)` valen por el candado de UN proceso:
  con la base cada una debe ir en una transacción o ser un SP propio
  (`docs/solicitudes-dba.md`, 6.2).

Se prueba en `verificar_auth.py` (4e el endpoint, 4f la caché, 4g el repaso lineal
contra el cuadrático de antes con 300 registros al azar, 4h el canje atómico, 4i el
cableado POR HTTP —login, refresh y cambio de contraseña ante carreras—, 4j el aviso de
las reconstrucciones lentas), `verificar_usuarios.py` (8c, el escenario del
administrador cerrado a la fuerza), `verificar_organizaciones.py` (6b) y, en el
navegador, con `probar-sesion-cerrada-front.mjs` y `probar-renovacion.mjs` (en la
suite) y `probar-guard-renovacion.mjs` y `probar-sesion-sin-back.mjs` (a mano, con el
build de producción).

**Lo que NO cubre.** Los claims del JWT (`rol`, `tnt`) se leen al emitirlo: cambiar el
rol de alguien (HU07) o moverlo de organización se nota hasta que renueva (15 minutos
como máximo). Eso no es la sesión y no se tocó.

## Organizaciones (Sprint 1 · HU02, desde el 2026-10-07)

`routers/organizaciones.py` + `servicios/tenants_registro.py`. Verificación:
`verificar_organizaciones.py`.

**Dónde viven.** En el registro provisional `tenants.jsonl`, NO en
`security.tenants` — que ya existe, con su `uspCreateTenant`. La razón es
práctica: **dev no tiene base de datos** (`SQLSERVER_HOST` vacío a propósito),
así que contra SQL esta pantalla no se podría ni probar. El contrato del
módulo imita al de los SPs; cuando haya base dev, cambia ese archivo y nada
más.

**Quién puede.** Solo el administrador de plataforma: además de la llave de
servicio, cada endpoint exige que el Bearer sea de alguien con
`isPlatformAdmin`. La llave la tiene el front entero, así que sola no
distingue quién pide.

| Ruta | Qué hace |
|---|---|
| `GET /organizaciones/` | listado, con los datos de su administrador |
| `GET /organizaciones/coincidencias?nombre=` | el aviso del alta: qué organizaciones se llaman igual (`ID encontrado`) y qué slug se asignaría (`ID asignado`) |
| `POST /organizaciones/` | crea la organización **y la cuenta de su administrador** y le manda su bienvenida por correo (`correoEnviado`); devuelve `contrasenaTemporal` UNA vez SOLO si el correo no salió (`correoMotivo`). 409 `correo_registrado` |
| `GET /organizaciones/{guid}/usuarios` | sus usuarios, con la misma forma que el listado de su administrador (`GET /usuarios/`): número de orden, nombre, rol EN esa organización, estado y fecha de alta |
| `POST /organizaciones/{guid}/usuarios/{usuario}/reenviar-invitacion` | HU06 CA-6: lo mismo que el de `/usuarios`, pero lo ejecuta el administrador de plataforma sobre un usuario de esa organización (sobre todo su administrador). 404 si no es de ella |
| `POST /organizaciones/{guid}/usuarios/{usuario}/cerrar-sesion` | HU05 CA-4: el administrador de plataforma le cierra TODAS las sesiones a un usuario de esa organización. Ver «Desactivar, reactivar y cerrar sesión» |

**Nombre, slug y código.** El nombre se puede repetir; el slug no. Al segundo
"Seguros Monterrey" le toca `seguros-monterrey-1`, que es el "ID asignado" que
anuncia el diseño. El código (`NEX-00001`) sigue una secuencia.

## Usuarios de una organización (Sprint 1 · HU06 y HU07, desde el 2026-10-07)

`routers/usuarios.py` + `servicios/roles.py`. Verificación:
`verificar_usuarios.py`.

**Quién puede**: el administrador de la organización (membresía con rol
`ADMIN`). Opera siempre sobre SU organización, la del JWT — nunca recibe el
tenant por parámetro, para que nadie toque la de al lado cambiando un id.

| Ruta | Qué hace |
|---|---|
| `GET /usuarios/` | los de mi organización, numerados (001, 002…) y con el nombre del rol resuelto |
| `GET /usuarios/roles` | el catálogo que se puede asignar |
| `POST /usuarios/` | alta con rol y bienvenida por correo (`correoEnviado`); devuelve `contrasenaTemporal` UNA vez SOLO si el correo no salió (`correoMotivo`). 409 `correo_registrado` |
| `POST /usuarios/{guid}` | HU07: cambia datos y rol. El correo no se toca |

**Los roles salen del DISEÑO, no de la base**: Supervisor, Analista, Operador,
Compliance Officer, Auditor y Viewer, con la descripción exacta que la
pantalla muestra. `security.roles` tiene otros cuatro (ADMIN, CONFIGURATOR,
REVIEWER, QUERY) de antes del diseño; hay que pedirle al DBA que siembre estos
seis. `ADMIN` existe aparte: es el administrador, nace con la organización
(HU02) y no se ofrece en el selector.

**Dos reglas que encierra el router**: nadie puede asignarse `ADMIN` desde el
selector, y un administrador no puede quitarse a sí mismo ese rol (dejaría la
organización sin quien la administre).

El JWT lleva ahora `tnt` (su organización) y `rol`, para que el front sepa qué
ofrecer sin preguntar en cada pantalla.

### Desactivar, reactivar y cerrar sesión (HU05, HU08, HU09 — 2026-10-07)

Dos endpoints más en `/usuarios`, con las mismas reglas de permiso (solo el
administrador de la organización, solo sobre la suya):

| Ruta | Qué hace |
|---|---|
| `POST /usuarios/{guid}/estado` | `{activo, motivo}`. Desactivar cierra TODAS sus sesiones y deja de poder entrar; reactivar le devuelve el acceso con su misma contraseña |
| `POST /usuarios/{guid}/reenviar-invitacion` | HU06 CA-6, sin cuerpo: contraseña temporal NUEVA (la anterior deja de servir), bienvenida otra vez y el mismo contrato que el alta (`correoEnviado`; `contrasenaTemporal` solo si el correo no salió). 409 `ya_activa` / `desactivada` |
| `POST /usuarios/{guid}/cerrar-sesion` | `{motivo}`. Revoca TODAS sus sesiones (no podrá renovar) sin desactivarlo: puede volver a entrar. Deja un evento `FORCED_LOGOUT` ANTES de cerrar (si no se puede escribir, no se cierra nada). Esta ruta es la del administrador de una organización, sobre usuarios de la suya; el Super Admin recibe 403 aquí porque no tiene organización (la suya cuelga de `/organizaciones`, abajo). Su JWT deja de valer en este back al instante (401 `sesion_cerrada`). |

El **motivo es obligatorio** en los dos y queda en el registro con quién lo
hizo: es lo que vuelve auditable que a alguien se le quitó el acceso. Nadie
puede desactivarse a sí mismo ni cerrar su propia sesión desde aquí (para eso
está el menú de su cuenta), y desactivar dos veces responde 409 `sin_cambio`
en vez de fingir que hizo algo.

**El administrador de plataforma también puede cerrar sesiones** (2026-10-12;
el criterio CA-4 dice «Admin de organización o Super Admin»):
`POST /organizaciones/{guid}/usuarios/{usuario}/cerrar-sesion`, con el mismo
cuerpo `{motivo}` y las mismas reglas —motivo obligatorio, `FORCED_LOGOUT`
escrito ANTES de cerrar, no a sí mismo—, y una más: el usuario tiene que ser
MIEMBRO de la organización de la URL (404 si no, y 404 si la organización no
existe). Las dos rutas llaman a `usuarios.forzar_cierre`, así que dejan
exactamente el mismo rastro; el evento lleva `actor`: `ADMIN_PLATAFORMA` o
`ADMIN_ORGANIZACION`. El administrador de una organización recibe 403
`sin_permiso` en la ruta de plataforma. **Desactivar** sigue siendo solo del
administrador de la organización. Lo que NO existe: cerrarle la sesión a OTRO
administrador de plataforma (no pertenece a ninguna organización donde
listarlo).

### Retirar una cuenta: borrado lógico (2026-10-13)

No hay pantalla ni endpoint para borrar usuarios: ninguna historia de Sprint 1 lo pide, y desactivar (HU08) deja el correo
ocupado. Lo que hay es un script para quien administra el servidor:

```
venv/bin/python retirar_cuenta.py --email a@b.com --email c@d.com --motivo "Cuentas de prueba"             # simula
venv/bin/python retirar_cuenta.py --email a@b.com --email c@d.com --motivo "Cuentas de prueba" --confirmar  # lo hace
```

Sin `--confirmar` NO toca nada: dice qué cuenta es (administrador de plataforma, rol y organización, si ya hizo su primer
acceso) y si se negaría. Con él, a cada cuenta: se le cierran las sesiones, se apagan sus enlaces pendientes, deja de
existir para el login, los listados y los JWT (un token suyo que todavía no vence recibe «el usuario ya no existe»), **su
correo queda libre para darse de alta otra vez** (como una cuenta NUEVA, con otro guid) y queda el evento `USER_DELETED`
con el correo, el motivo y quién lo hizo. Es LÓGICO: el registro es de solo agregar y no se borra ningún renglón, así que el
rastro de lo que esa cuenta hizo se queda. No se puede deshacer desde aquí.

Se niega a retirar al **último administrador de plataforma activo**, y al **administrador de una organización** salvo con
`--forzar` (la organización queda sin administrador; el listado de organizaciones lo tolera y muestra la tarjeta sin
administrador). En la base esto será `security.users.isDeleted` (ya existe en el diccionario, con el índice único de correo
que lo ignora), y ese día este script cambia por el SP; todavía no se le ha pedido a Charlie. Se prueba en
`verificar_retiro.py`.

### Mi perfil (HU12, HU13 — 2026-10-07)

`POST /auth/perfil` actualiza los datos de QUIEN LLAMA: nombre, apellido
paterno y materno, teléfono y los datos de recuperación (correo y teléfono).
`GET /auth/yo` los devuelve todos.

Guardar cambios deja el evento `PROFILE_UPDATED` en la bitácora de auditoría
(**HU12 CA-6, 2026-10-14**): la persona, la sesión desde la que se guardó, la
hora y `campos`, la lista de los **nombres** de los campos que cambiaron
(`nombre`, `apellidoPaterno`, `apellidoMaterno`, `telefono`,
`correoRecuperacion`, `telefonoRecuperacion`). Nunca los valores: son datos
personales y ya viven en el registro de usuarios. Se compara lo que QUEDÓ
guardado con lo de antes, no lo que se mandó, así que el mismo correo escrito con
espacios y mayúsculas no cuenta como cambio; si no cambió nada, no hay evento;
un guardado rechazado (sin nombre, correo de recuperación inválido) tampoco lo
deja. Se prueba en `verificar_auth.py`, sección 6b.

Lo que **no** se toca ahí: el correo con el que inicia sesión (es su identidad)
y su rol (lo mueve quien administra su organización). El cambio de contraseña
desde la sesión es `POST /auth/contrasena` con `actual`, que ya existía. Con
motivo `CHANGE` (**HU13 CA-4, 2026-10-14**) se cierran las sesiones de los
**OTROS dispositivos** y se conserva la de quien lo hace: si la contraseña vieja
estaba comprometida, quien la tuviera deja de entrar, y la dueña no se queda
fuera de su pantalla. Hasta el 2026-10-13 `CHANGE` no cerraba ninguna, y una
sesión robada seguía viva con la contraseña vieja. La respuesta trae
`otrasSesionesCerradas` (cuántas) y `sesionesCerradas: false`; con el primer
acceso (`FIRST_LOGIN`) y la recuperación (`RESET`) se siguen cerrando TODAS, la
actual incluida (`sesionesCerradas: true`). Cada cambio desde una sesión deja el
evento `PASSWORD_CHANGED` en la bitácora de auditoría (la persona, la sesión
desde la que se cambió y cuántas se cerraron; **nunca** la contraseña ni su
hash), también cuando no había otras que cerrar. Un cambio rechazado (actual
incorrecta, nueva igual a la actual, confirmación distinta, política) no cierra
nada ni deja evento. Las otras sesiones se cierran ANTES de escribir la
contraseña: si el almacén falla a la mitad, no queda la contraseña nueva con las
viejas sesiones abiertas. Se prueba en `verificar_auth.py`, sección 4k.

Los apellidos se guardan **separados** (`apellidoPaterno`, `apellidoMaterno`),
como los pide el diseño y como los tiene `security.users` (lastName,
secondLastName). Los usuarios creados antes solo tienen `apellidos`: se parten
al leerlos (`usuarios.separar_nombre`), sin migrar nada.

**Datos de recuperación del administrador de una organización (2026-10-14).** El alta de la organización los pide bajo «tu
cuenta», pero se guardaban solo en la organización: Mi perfil del administrador los mostraba vacíos. Ahora `POST /organizaciones/`
también los guarda en SU usuario, y a los administradores anteriores se les leen de los de su organización mientras no hayan
guardado los suyos (`usuarios._heredar_recuperacion`; si los borran en su perfil, quedan borrados). Se prueba en
`verificar_organizaciones.py`, sección 6.

**Desde el 2026-10-14 los formularios de alta piden Nombre(s), Apellido paterno (obligatorio) y Apellido materno** y los mandan
en `apellidoPaterno`/`apellidoMaterno` (usuarios) o `adminApellidoPaterno`/`adminApellidoMaterno` (organizaciones): se guardan
tal cual, sin adivinar. Lo de abajo es el respaldo para quien manda el nombre completo junto (la API directa) y para las cuentas
anteriores. **Antes el formulario traía UN campo «nombre completo»** (el diseño no los separaba) y mandaba `apellidos` vacío. Hasta el 2026-10-14 todo se guardaba en `nombre` y «Mi perfil» mostraba el
nombre entero con los apellidos en «—». Ahora `usuarios.separar_nombre` lo parte como se escribe en México: las **dos últimas
unidades** son el apellido paterno y el materno, con sus partículas («De la Cruz», «Del Valle», «Ortega y Gasset» cuentan como
UNA unidad) y lo anterior son los nombres: «Cesar Boris Hernandez Alvarado» → Cesar Boris / Hernandez / Alvarado. Con dos
palabras, la segunda es el paterno; con una, solo hay nombre; siempre queda al menos un nombre. Con `apellidos` explícitos se
respetan (el materno es todo lo que sigue al primero). Es una heurística —no puede saber si «María José Pérez» son dos nombres y
un apellido—: la persona lo corrige en su perfil, y **editar a un usuario con el mismo nombre completo que mostró el listado no
lo deshace**. Las cuentas y las ediciones de antes se separan igual al leerlas. El listado, la confirmación y el JWT siguen
diciendo el nombre completo; el correo de bienvenida saluda solo con los nombres. Se prueba en `verificar_usuarios.py`, sección 3b.

## Aislamiento entre organizaciones (Sprint 1 · HU11, desde el 2026-10-14)

Dos organizaciones no se ven entre sí: ni sus datos ni sus archivos, ni siquiera para saber si lo del otro existe. Hay dos
redes, y la segunda es de esta API.

### La segunda red: ya no se le cree al front

La primera red está en el front: el BFF saca el `tenant` de la organización de la **sesión** (`tenantDe`, en
`src/lib/server/nexus.ts`) y nunca del navegador. Era lo único que había. Un olvido ahí —y lo hubo: `/api/archivos` tomaba el
`tenant` del formulario que arma el navegador, así que cualquiera con sesión podía subir al prefijo de otra organización— y
una organización leía o escribía en otra, porque este back se creía el `tenant` que el front le mandara.

Desde el 2026-10-14 las rutas con `tenant` exigen **también el JWT de la persona** (el BFF lo reenvía junto con la llave de
servicio) y `servicios/aislamiento.py` comprueba, contra el registro y no contra los claims del JWT (que pueden tener
minutos), que esa persona pueda operar en ese tenant:

- Si pertenece a una organización, **solo en la suya** (su membresía, con el guid exacto).
- El **administrador de plataforma**, que no pertenece a ninguna, opera en el *espacio de plataforma*: cualquier tenant que
  **no** sea el guid de una organización (`demo`, el heredado `csi`…). **Nunca** en el de una organización: para eso están las
  rutas `/organizaciones/…`. El guid se compara sin espacios en los extremos y sin distinguir mayúsculas, porque el almacén
  recorta el tenant y un montaje que no distingue mayúsculas llevaría `ABC…` al directorio de `abc…`: escribirlo así no es un
  rodeo.
- Sin Bearer: 401 `sin_sesion`. Con una sesión cerrada: 401 `sesion_cerrada`. Con la cuenta desactivada: 403 `desactivada`.
  Igual que el resto de las rutas que piden persona.

| Ruta | Qué comprueba |
|---|---|
| `GET /bandeja/`, `POST /bandeja/{id}/retirar`, `POST /bandeja/` (con la llave de servicio) | el `tenant` que se manda |
| `GET/POST /llaves/`, `POST /llaves/{id}/revocar`, `GET /llaves/{id}/metricas` | el `tenant` que se manda |
| `/webhooks/…` (las nueve: alta, lista, estado, eliminar, validar, editar, eventos, métricas, intentos) | el `tenant` que se manda |
| `POST /archivos/` | el `tenant` del formulario |
| `GET /archivos/{ruta}` | el primer segmento de la ruta (`{tenant}/aa/bb/sha256`) |
| `/usuarios/{guid}…` | que el usuario sea de la organización del administrador (ya se comprobaba; ahora además se anota) |

**No** pasa por aquí `POST /bandeja/` con una **API Key de cliente**: el tenant sale de la llave y no hay persona. Tampoco
`/ia/*`, `/procesadores/*` ni `/organizaciones/*` (esas piden su propio rol).

### Qué se contesta (CA-4 y CA-5)

- **Un tenant que no es tuyo**: 403 `sin_acceso` con «No tienes acceso a esa organización.» —no nombra nada de la otra—.
- **Algo que existe pero es de otra organización** (un webhook, una llave, una entrada de la bandeja, un archivo, un usuario):
  un 404 **idéntico** al de algo que no existe (mismo estado y mismo cuerpo; `verificar_aislamiento.py` los compara byte por
  byte). Quien lo intentó no aprende si existe. Los mensajes ya no dicen «o es de otro cliente».
- **Un fallo del almacén**: 503 con un mensaje fijo («El servicio no está disponible en este momento…»). El texto del sistema
  —rutas del servidor, nombres de los registros, que llevan el guid de la organización— va al log, no a la respuesta
  (`routers/errores.py`; antes lo devolvían tal cual `routers/auth.py`, `organizaciones.py` y `usuarios.py`).

### Qué se anota (CA-6)

Cada intento deja `CROSS_TENANT_ACCESS` en la bitácora de auditoría: `usuarioGuid`, `sesionGuid`, `tenantsDelActor` (las
organizaciones a las que pertenece; vacío para el administrador de plataforma), qué pidió (`tenantPedido`, `recurso`,
`recursoId`) y por dónde se detectó (`via`: `tenant` si mandó un tenant ajeno, `id` si pidió por id algo de otra
organización, `ruta` si pidió un archivo bajo el prefijo de otra; `sesionGuid` va vacío cuando el intento se detecta en las
rutas de `/usuarios`, cuyo guard no lleva la sesión). La anotación **nunca cambia la respuesta**: si la bitácora
falla, se avisa en el log y el rechazo se da igual. No cuentan como cruce: un id inventado (no existe en ningún lado), faltar
la sesión (es otro error) ni lo heredado de abajo.

### Lo heredado: el prefijo `csi`

Los ejemplos documentales de la configuración se guardaban TODOS bajo el prefijo fijo `csi`, para cualquiera. Ese prefijo
sigue en el almacén (`PREFIJOS_HEREDADOS` en `servicios/aislamiento.py`): lo lee el administrador de plataforma; **quien
pertenece a una organización recibe el 404 de siempre**, sin que cuente como intento cruzado (es su propio apuntador viejo, no un
ataque). Los ejemplos nuevos van bajo el tenant de la sesión. Consecuencia: los ejemplos que una organización guardó antes
bajo `csi` dejan de cargarse para sus usuarios (el front pinta el hueco de siempre); si hace falta recuperarlos, se mueven los
objetos a su carpeta o se vuelven a subir.

Los archivos se sirven con `Cache-Control: private`: una caché compartida (un proxy, una CDN) no debe poder entregárselo a
quien no pasó por esta comprobación.

### Límites conocidos (decididos el 2026-10-14)

- **`/procesadores/*`** son recursos globales de Document AI, no de una organización: cualquier sesión del front los ve. No
  se aíslan en esta pasada; cuando haya un procesador por organización habrá que atarlos a ella.
- **`/documentos/*`** (SQL, hoy sin montar porque la base no existe) usan `tenant_id` entero y no pasan por esta red: se
  rediseñan con el DBA.
- **Los roles no se aplican** en bandeja, llaves ni webhooks: cualquier miembro puede emitir llaves o registrar webhooks de SU
  organización. No es de HU11 (aísla organizaciones, no personas dentro de una); queda anotado.
- La bitácora es el registro provisional (`.registro/auditoria.jsonl`); `CROSS_TENANT_ACCESS` pasa a la tabla de auditoría
  con los demás eventos cuando exista.

### Verificarlo

`verificar_aislamiento.py` (offline, como los demás): dos organizaciones con su administrador y lo suyo (webhook, llave,
entrada, archivo, usuario); la sesión de la segunda intenta, por **cada** ruta que recibe un tenant o un id, llegar a lo de la
primera, y se comprueba que no ve nada, que no se toca nada, que ninguna respuesta lleva un dato de la otra, que lo ajeno se
contesta como lo que no existe y que cada intento queda anotado. Es la prueba de seguridad multi-tenant del criterio CA-3.

Los otros `verificar_*` que llaman a estas rutas con la llave de servicio (`verificar_bandeja.py`, `verificar_subida.py`,
`verificar_llaves_cliente.py`, `verificar_webhooks.py`) usan `verificacion_comun.cliente_con_sesion_de_plataforma`: un cliente
de prueba que hace lo que hace el BFF, agregar el Bearer de un administrador de plataforma a toda petición con la llave de
servicio.

## Quién administra: el rol (NEX-472, 2026-10-22)

La auditoría de caja negra de QA (NEX-470) encontró que las rutas de API Keys, webhooks y procesos solo comprobaban que la persona
**perteneciera a la organización**: un Auditor podía leer y crear API Keys y webhooks. Ahora piden además un rol que administre:
`roles.PUEDEN_ADMINISTRAR` = Administrador y Supervisor, más el administrador de plataforma en su espacio. Propuesta de producto,
**pendiente de confirmar con César**; si cambia, cambia ese conjunto (y su espejo en el front, `src/lib/usuarios/permisos.ts`).

- **Qué rutas lo piden** (`aislamiento.exigir_administrar`): TODAS las de `/llaves`, TODAS las de `/webhooks` salvo `POST /webhooks/eventos`,
  TODAS las de `/procesos`, y los **cambios** de `/tipos-documentales` (guardar, ordenar, borrar).
- **Qué sigue abierto a cualquier rol de la organización**: leer la biblioteca de tipos documentales (`GET /tipos-documentales/`: el
  pipeline la carga para clasificar) y `POST /webhooks/eventos` (lo dispara el front cuando quien OPERA el pipeline ve terminar un
  documento). También la bandeja, los archivos y todo lo que ya existía sin rol.
- **Qué responde**: 403 `sin_permiso` con «Tu rol no puede administrar esto.» (no dice qué rol sí puede). El aislamiento va primero:
  quien no es de la organización recibe `sin_acceso` y su `CROSS_TENANT_ACCESS`, no `sin_permiso`.
- **Rastro**: cada rechazo deja `ROLE_DENIED` en la bitácora (quién, organización, qué intentó, con qué roles cuenta). El rol se lee
  del registro, no del JWT (que dura 15 minutos): un descenso de rol surte efecto de inmediato.
- **Sigue sin rol**: `/procesadores/*` y `/ia/extraer` (ver más abajo) y la clasificación/extracción del pipeline.

`verificar_roles.py` (offline): una organización con una persona de cada rol y TODAS las rutas probadas por petición directa; que lo
rechazado no cambie nada en el servidor; que lo del pipeline siga abierto; el `ROLE_DENIED`; y que el aislamiento siga primero.
Los demás `verificar_*` que creaban una segunda persona como Operador/Analista para editar (`verificar_procesos.py`,
`verificar_tipos_documentales.py`, `verificar_aislamiento.py`) se ajustaron a esta regla.

## Descartar documentos de la bandeja (2026-10-15)

«Descartar» es `POST /bandeja/{id}/retirar` con `motivo=descartado`, y ahora además:

- **`detalle`** (opcional, hasta 500 caracteres): el porqué que escribe la persona. Se guarda sin espacios sobrantes en el evento
  `retiro` de la bandeja y en la auditoría. Más de 500: 422.
- **Solo ciertos roles**: administrador de la organización, supervisor, analista y operador (`roles.PUEDEN_DESCARTAR`), y el
  administrador de plataforma en su espacio. Compliance, auditor y viewer reciben **403 `sin_permiso`** («Tu rol no puede descartar
  documentos.»), que se contesta ANTES de mirar si la entrada existe: quien no puede no aprende nada de la bandeja. Se lee del
  registro, no del JWT. **Es una decisión de producto mía, pendiente de confirmar con César**: el diseño de los roles no dice quién
  descarta. Retirar por `pipeline` no cambió (no se restringe por rol).
- **`DOCUMENT_DISCARDED`** en la bitácora de auditoría: `usuarioGuid`, `sesionGuid`, `tenant`, `entradaId`, `nombreOriginal`,
  `sha256`, `canal` y `detalle`. Se escribe DESPUÉS de retirar (el retiro es atómico); si la bitácora falla se avisa en el log y no
  se le dice a la persona que no se descartó, igual que `CROSS_TENANT_ACCESS`.
- **No se borra nada del almacén**: los bytes se comparten por contenido y borrar sería peligroso sin conteo de referencias. Descartar
  es reversible a nivel de datos (el archivo y la entrada siguen en los registros), pero todavía NO hay pantalla para recuperar.
- Solo aplica a lo que vive en el servidor (lo que llegó por la API). Lo subido a mano está en la memoria de la pestaña: se quita
  de la lista y no deja rastro (no hay registro donde dejarlo) hasta que lo ingerido por la interfaz también se guarde en el servidor.

**Consultar lo descartado** (2026-10-15): `GET /bandeja/?tenant=…&estado=descartados` trae lo descartado, del más reciente al más viejo,
con `descartadaEn` y su `detalle`; sin `estado` (o `estado=pendientes`) es lo de siempre. Cualquier rol de la organización puede
consultarlo: ver no destruye nada. Es lo que alimenta el filtro «Descartado» de la bandeja (la «sección de descartados» del Figma);
el front lo trae solo cuando ese filtro está puesto. Otro valor de `estado`: 422.

Se prueba en `verificar_descartar.py` (36 comprobaciones; 9 mutaciones del descarte muertas).

## Tipos documentales: el catálogo vive en el servidor, por organización (2026-10-15)

Hasta hoy la biblioteca de Configuración (tipos, campos, estado, el procesador de Document AI de cada uno, historial de
versiones, ejemplos recortados) vivía en el `localStorage` de **cada navegador**. Ahora vive aquí, **por organización**:
`servicios/tipos_documentales.py` (registro provisional, `tipos-documentales.jsonl` dentro de `.registro/`) y
`routers/tipos_documentales.py`.

| Ruta | Qué hace |
|---|---|
| `GET /tipos-documentales/?tenant=` | Los tipos de la organización (borradores, activos y archivados), en su orden. |
| `PUT /tipos-documentales/{id}` | Crea (`revision: 0`) o guarda (`revision: n`) el tipo completo. |
| `PUT /tipos-documentales/orden` | Guarda el orden de la biblioteca de la organización. |
| `DELETE /tipos-documentales/{id}?tenant=` | Lo quita (no toca Google: el front borra antes el procesador con `/procesadores`). |

Piden la llave de servicio **y** el JWT de la persona (HU11): leer los de otra organización es 403 `sin_acceso` y un id ajeno
responde 404 igual que uno que no existe.

**Dos personas editando lo mismo.** Cada tipo lleva una `revision`. Guardar exige la que se editó; si otra persona guardó antes,
es un 409 `conflicto` con `revisionActual` y no se pisa nada. Crear con un id que ya existe —en esta organización o en
otra— también es 409.

**Ids.** Los inventa el cliente (`tipo-` + 32 hexadecimales). Tienen que ser únicos en TODAS las organizaciones, porque el
clasificador es global y nombra cada categoría con el id.

**Auditoría.** Crear, borrar, cambiar de estado y calibrar dejan `DOCUMENT_TYPE_CREATED`, `DOCUMENT_TYPE_DELETED`,
`DOCUMENT_TYPE_STATE_CHANGED` y `DOCUMENT_TYPE_CALIBRATED`, con quién y de qué organización. Guardar a secas, no.

### El clasificador sale de este registro

`POST /procesadores/clasificador/sincronizar` **ya no recibe cuerpo**: arma las categorías con los tipos `activo` de TODAS las
organizaciones, leídos del registro (antes los mandaba el navegador, y el último que sincronizaba pisaba a los demás). Por eso
lo que cambia de estado hay que **guardarlo antes** de llamar. `POST /procesadores/clasificador/asegurar` compara lo que Google
tiene (nombre y descripción de cada categoría) con el registro y sincroniza solo si difieren; el front lo llama una vez por
sesión, antes de clasificar.

### Procesadores huérfanos

Al pasar el catálogo al servidor sin migrar lo de cada computadora, los extractores que esas pruebas crearon en Google quedaron
sin dueño. `limpiar_procesadores_huerfanos.py` los lista (solo lectura) y, con `--borrar`, borra ÚNICAMENTE los que ningún tipo
señala —ni como vigente, ni en su historial—, pidiendo escribir BORRAR. Nunca toca el clasificador, el extractor de INE del
`.env` ni lo que no se llame `nexusdoc--…`. Se corre en el servidor (necesita las credenciales de Google y el almacén).

`--antes-de AAAA-MM-DD` es un corte de seguridad: solo borra los huérfanos creados ANTES de esa fecha y lista aparte (sin tocar) los
más recientes. Hay que usarlo cuando el registro que lee el script pueda no ser el de todos los ambientes que comparten el proyecto de
Google: lo creado antes del 2026-10-15 (cuando el catálogo pasó al servidor) no puede tener dueño en ningún registro.

### Límites conocidos

- **No distingue roles**: quien opera en la organización lee y escribe sus tipos, como en llaves y webhooks. El catálogo de roles
  de HU07 no tiene uno que configure; cuando lo haya, se exige aquí.
- **`/procesadores/*` y `/ia/extraer` siguen sin comprobar la organización** (lo vio HU11): quien tiene la llave de servicio y un
  `procesadorId` puede usarlo. Falta ligarlos al registro.
- **Un tipo en edición sale del clasificador** (`crearNuevaVersion` lo pasa a `borrador`): mientras alguien edita el «INE» de su
  organización, el clasificador deja de conocerlo en la siguiente sincronización. Es lo de siempre, pero ahora el clasificador es
  de todos.
- **El archivo crece con cada guardado** (cada uno escribe el tipo entero). Con el uso de hoy basta; el remedio es compactarlo o
  la base.
- **Los ejemplos subidos** (`/archivos`, bajo el prefijo de cada organización) que ya estaban en el almacén sin tipo que los
  señale quedan ahí: no se borran.

### Verificarlo

```
.venv/Scripts/python verificar_tipos_documentales.py
```

Offline: crear, ver desde otra persona, 409 por revisión, aislamiento, validación, orden, auditoría, clasificador sin cuerpo con
los activos de las dos organizaciones, `asegurar`, y qué procesadores quedarían huérfanos.

## Administradores de plataforma desde Nexus Monitor (2026-10-20)

Un administrador de plataforma no se da de alta desde NexusDoc sino desde fuera, por quien opera el sistema. Eso eran tres scripts
en una terminal del servidor (`bootstrap_admin.py`, `reenviar_invitacion.py`, `retirar_cuenta.py`); ahora Nexus Monitor (el
proyecto hermano, la herramienta del operador) lo hace desde una pantalla con estas rutas, que usan los MISMOS servicios. Los
scripts siguen sirviendo: `routers/admins_plataforma.py`.

| Ruta | Qué hace |
|---|---|
| `GET /admins-plataforma/` | Los que existen, con su estado (`pendiente`, `activo`, `inactivo`), su último acceso y si tienen sesión. Los retirados ya no salen. |
| `POST /admins-plataforma/` | Crea uno con contraseña temporal y le manda la bienvenida (`enviarCorreo: false` equivale a `--sin-correo`). Devuelve la contraseña SIEMPRE, y `correo`: `enviado`, `sin_canal`, `fallo` u `omitido`. Un correo repetido: 409. |
| `POST /admins-plataforma/{guid}/reenviar-invitacion` | Regenera la contraseña temporal de quien no ha hecho su primer acceso (la anterior deja de servir). Quien ya entró: 409 `ya_hizo_su_primer_acceso`. |
| `DELETE /admins-plataforma/{guid}?motivo=` | Lo retira (borrado lógico; su correo queda libre). El motivo es obligatorio. Al último administrador activo no se le retira: 409 `ultimo_superadmin`. |

**Se toca solo a administradores de PLATAFORMA**: una cuenta de una organización responde 404 como si no existiera, así que Monitor no
sirve para administrar usuarios ajenos. Crear, regenerar y retirar dejan `USER_CREATED`, `INVITATION_REISSUED` y `USER_DELETED` con
`actor: "monitor"` y qué pasó con el correo; nunca la contraseña.

### ⚠️ Sin protección extra (decisión del usuario, 2026-10-20: «sin protección extra por ahora»)

Estas rutas piden SOLO la llave de servicio, como `/esquema` o `/procesos`: no hay un JWT de persona (Monitor no tiene login) ni una
llave de administración aparte. **Quien tenga la llave de servicio y llegue al puerto del back puede crear un administrador de
plataforma** —la cuenta con más poder del sistema—, y quien abra Monitor desde la VPN, también. Antes de esto, crear uno exigía tener
acceso al servidor. Para cerrarlo después: una `ADMIN_PLATAFORMA_LLAVE` en el `.env` que estas rutas exijan en un encabezado (y que
Monitor pida tecleada en cada acción), y apagar las rutas si no está puesta. Al promover esto a `main`, las rutas quedan activas en
producción.

```
.venv/Scripts/python verificar_admins_plataforma.py
```

Offline, con el correo simulado: crear (con y sin canal, con fallo y omitido), contraseña que sirve y obliga a cambiarse, listar con
estados, regenerar, retirar con motivo y sin retirar al último, solo cuentas de plataforma, sin llave nada, y la bitácora sin
contraseñas. Se probó rompiendo 15 reglas a propósito: las 15 se detectan.

## Expedientes y documentos: el documento nace al entrar, el expediente los reúne (2026-10-24)

Un **documento** nace cuando entra al sistema (hoy, por `POST /bandeja/`) y existe por sí mismo. Un **tipo de expediente** («expediente médico»,
«expediente de un choque») es la clase; un **expediente** es una instancia de un tipo —la carpeta de UN paciente, de UN choque— y reúne los
documentos de ese caso. Un documento puede estar en **ningún** expediente (el «void»), en **uno** o en **varios**. El modelo completo, sus
decisiones y lo que falta están en [`docs/modelo-entidades.md`](docs/modelo-entidades.md).

| Ruta | Qué hace | Quién |
|---|---|---|
| `GET /tipos-expediente/?tenant=` | los tipos de la organización, cada uno con su esquema (`tiposDocumentales`, con id y nombre) y su `totalExpedientes` | cualquiera que opere en ella |
| `GET /tipos-expediente/{id}?tenant=` | un tipo, su esquema y cuántos expedientes tiene | ídem |
| `PUT /tipos-expediente/{id}/esquema` `{tenant, tiposDocumentales}` | reemplaza el esquema por esa lista ordenada de tipos documentales de la misma organización (sin repetidos, hasta 50); idempotente (`cambiado: false` si es la misma); `[]` lo deja sin esquema. **Describe, no restringe:** los expedientes de ese tipo siguen recibiendo cualquier documento | `roles.PUEDEN_ADMINISTRAR` |
| `POST /tipos-expediente/` `{tenant, nombre}` | crea un tipo (201); el nombre es único por organización, sin distinguir mayúsculas ni espacios de más: uno repetido da 409 `ya_existe` | `roles.PUEDEN_ADMINISTRAR` |
| `GET /expedientes/?tenant=&tipo=` | los expedientes de la organización, cada uno con su `tipo`, su `totalDocumentos` y su `tamanoBytes` (lo que pesan sus documentos juntos; se calcula al consultar); con `tipo`, solo los de ese tipo | cualquiera que opere en ella |
| `GET /expedientes/{id}?tenant=` | el expediente (con los mismos totales) y sus documentos completos, en el orden en que se agregaron | ídem |
| `GET /expedientes/documentos?tenant=&sin_expediente=` | los documentos y los expedientes en que está cada uno; `true` = solo el void, `false` = solo los organizados | ídem |
| `POST /expedientes/` `{tenant, nombre, tipoId}` | crea un expediente vacío (201) como instancia de ese tipo, que debe ser de la misma organización (si no, 404) | `roles.PUEDEN_ORGANIZAR` |
| `PUT /expedientes/{id}/documentos/{documento_id}?tenant=` | mete un documento; idempotente (`agregado: false` si ya estaba) | ídem |
| `DELETE /expedientes/{id}/documentos/{documento_id}?tenant=` | lo saca; el documento NO se borra y, si era su último expediente, vuelve al void | ídem |

Todas piden la llave de servicio **y** el JWT de la persona (HU11); un id de otra organización responde 404 como si no existiera. Crear, meter y
sacar dejan `CASE_CREATED` (con el tipo), `CASE_DOCUMENT_ADDED` y `CASE_DOCUMENT_REMOVED` en la bitácora, crear un tipo deja `CASE_TYPE_CREATED` y cambiar su esquema deja `CASE_TYPE_SCHEMA_CHANGED` (con el esquema antes y después; no se repite si no cambió).
Los roles de solo consulta (Compliance, Auditor y Viewer) reciben 403 `sin_permiso` al intentar organizar; Analista y Operador organizan pero no
crean tipos (propuesta de producto, pendiente de confirmar con César).

`POST /bandeja/` no cambia para quien sube, salvo que la entrada trae además su `documentoId`. Los documentos viven en
`.registro/documentos-{tenant}.jsonl`, los expedientes en `.registro/expedientes-{tenant}.jsonl` y los tipos en `.registro/tipos-expediente-{tenant}.jsonl`. La ruta de documentos cuelga de
`/expedientes` y no de `/documentos` porque ese prefijo ya es del grupo que habla con SQL Server.

Verificarlo, offline: `venv/bin/python verificar_expedientes.py`.

## Procesos: el motor de reglas guarda su dibujo, por organización (2026-10-20)

Un **proceso** es un conjunto de reglas y corre sobre un **expediente** (un conjunto de documentos). Se arma en un lienzo del front:
los tipos documentales de la organización son sus entradas, las reglas son operadores (igual, distinto, contiene, mayor, menor;
cantidad, suma, alguno, todos y unicos, que trabajan con **listas**; existe, que pregunta si el expediente trae un documento; y, o, no,
implica), un **valor fijo** es un dato que se escribe en el lienzo (el «2» de «más de 2 socios») y los cables llevan un campo, el documento
entero (el punto `@documento` de un tipo, solo para `existe`), un valor fijo o el resultado de una regla a la entrada de un operador. Aquí solo se
**guarda ese dibujo**: `servicios/procesos.py` (registro provisional, `procesos.jsonl` dentro de `.registro/`) y `routers/procesos.py`.
**Todavía no hay un motor que corra los procesos sobre un expediente de verdad**: el back no interpreta las reglas, solo
comprueba que el dibujo esté entero.

| Ruta | Qué hace |
|---|---|
| `GET /procesos/?tenant=` | Los procesos de la organización, en el orden de su creación. |
| `PUT /procesos/{id}` | Crea (`revision: 0`) o guarda (`revision: n`) el proceso completo. |
| `DELETE /procesos/{id}?tenant=` | Lo quita. |

Piden la llave de servicio **y** el JWT de la persona (HU11), igual que los tipos documentales: leer los de otra organización es 403
`sin_acceso` y un id ajeno responde 404 igual que uno que no existe. Crear con un id que ya existe —en esta organización o en otra—
es 409; **dos personas editando lo mismo**: guardar exige la `revision` que se editó y, si otra persona guardó antes, es un 409
`conflicto` con `revisionActual`, sin pisar nada.

**Qué es un proceso guardado:** `nombre` (hasta 200), `descripcion`, `nodos` y `aristas`. Cada nodo es `{id, type, position, data}`
con `type` `tipo-documental` (`data.tipoId`), `operador` (`data.operador` y los valores de ejemplo `a` y `b`, de hasta 5000
caracteres porque el ejemplo de una lista son varios renglones) o `constante` (`data.familia` —`texto`, `numero`, `fecha` o
`booleano`— y `data.valor`, de hasta 500); cada cable, `{id,
source, sourceHandle, target, targetHandle}`. Se rechazan con 400: un id que no sea `proceso-…`, un nodo de un tipo inventado o sin
posición, posiciones que no son números o pasan de un millón, ids de nodo o de cable repetidos, un cable que une un nodo que no
existe, más de 300 nodos o 600 cables, y más de 1 MB. **Lo que se guarda sale limpio**: del nodo y del cable solo lo que el lienzo
usa (la librería manda también medidas y banderas de selección); lo demás que mande la pantalla (por ejemplo la vista) se conserva
en `datos`. `revision`, `creadoPor`, `creadoEn`, `modificadoPor`, `modificadoEn` y `tenant` los pone el servidor y no se pueden forzar.

**Auditoría.** Crear y borrar dejan `PROCESS_CREATED` y `PROCESS_DELETED` (quién, qué organización, `procesoId`, `nombre`); guardar a
secas, no: mover un nodo o escribir un valor de ejemplo son muchos guardados y no cambian lo que el proceso ES. El rastro no lleva el
dibujo.

**Límites conocidos:** no distingue roles (quien opera en la organización lee y escribe sus procesos); borrar un tipo documental no
avisa a los procesos que lo usan (el lienzo muestra «Tipo documental no disponible»); el archivo crece con cada guardado.

### Verificarlo

```
.venv/Scripts/python verificar_procesos.py
```

Offline: crear, ver desde otra persona, 409 por revisión, aislamiento entre organizaciones, validación del dibujo, que lo guardado
salga limpio, listas de ejemplo largas y valores fijos, auditoría y corte a media escritura. Se probó rompiendo 22 reglas del
servicio y del router a propósito: las 22 se detectan.

## Grupos de campos y forma del resultado de la extracción (2026-10-16)

Pedido para las **actas constitutivas**: ahí un dato no es un valor suelto sino una *fila* de varios (un accionista = nombre +
acciones + monto). Un campo de un tipo documental puede ser ahora un **grupo**:

- **El esquema** (`servicios/esquema.py`, `esquema_desde_campos`): un campo con `tipoDato: "grupo"` y `subcampos: [...]` se declara
  como lo declara Google en el procesador de INE para `domicilio`: la Property apunta por nombre a un EntityType auxiliar con
  `baseTypes: ["object"]` y sus propias `properties`. Con `cardinalidad: "multiple"` la Property es `*_MULTIPLE` (una instancia por
  fila). **Un solo nivel**: los subcampos son campos sueltos (no otro grupo). Un grupo sin subcampos no se puede declarar:
  `POST /procesadores/activar` responde 422 con el nombre del grupo.
- **El resultado** (`servicios/ia.py`, `_extraer_entidades`): antes el diccionario se llenaba por nombre y de varias entidades del
  mismo tipo **solo sobrevivía la última** (de tres socios quedaba uno). Ahora un campo que el esquema declara múltiple sale
  **siempre como lista** (aunque haya aparecido una vez), y un grupo múltiple como **lista de filas** con sus subcampos en el
  orden del esquema (Google los devuelve en orden alfabético); un grupo único (domicilio) sigue siendo un diccionario. Un campo
  único que llegó repetido se queda con la aparición de mayor confianza (antes, la última).
- **Qué se repite y qué es obligatorio sale del esquema que el procesador tiene EN GOOGLE** (`procesadores.estructura_del_procesador`,
  lectura que no cobra, con memoria de 5 minutos que `activar` invalida): la respuesta de Document AI no dice si un campo se repite.
  Funciona igual para el procesador de INE. Si no se puede leer, la extracción sigue y aplana por lo que llegó.
- **`_metadata.campos_ausentes`**: lo obligatorio que no salió, como rutas (`capital_social`; `accionistas` si el grupo
  obligatorio no trae ninguna fila; `accionistas[2].nombre` si la fila 2 salió sin ese subcampo, que casi siempre es una instancia
  espuria que el modelo sacó de otra parte del documento). Lista vacía = se revisó y no faltó nada; **sin la clave = no se pudo leer
  el esquema** (no se sabe).

Lo que se midió con un acta real de 10 páginas: un grupo entrega cada fila con sus datos **juntos** (cada accionista con SUS
acciones y SU monto, que con listas paralelas salían desordenadas), pero el modelo también saca filas de más: una «accionista» de la
cláusula del capital y ~30 «cargos» de todo el texto legal. Por eso se señalan en vez de esconderse.

**Límites**: la confianza no distingue lo bueno de lo malo (ver `docs/` del front, medición del acta); una fila que cruza una
página se parte en dos (el subcampo que cae en la otra página sale en otra fila); los subcampos no pueden ser lista ni grupo.

### Un error que vale la pena recordar

El 2026-10-15 el commit de «tipos documentales en el servidor» borró por accidente `POST /procesadores/activar` y el ayudante
`_mensaje_para`: un parche cortó «todo lo que hay entre A y B» y entre A y B estaba la ruta. Se desplegó y la activación en dev dio
«Method Not Allowed» hasta que se restauró (2026-10-16). Nada lo atrapó porque ninguna prueba tocaba esa ruta. Ahora
`verificar_procesadores.py` vigila el **inventario de rutas** (si falta una, o sobra una, se pone en rojo), la activación y la
traducción de errores.

### Verificarlo

```
.venv/Scripts/python verificar_esquema.py              # el esquema con grupos y la lectura de su estructura
.venv/Scripts/python verificar_extraccion_generica.py  # listas, filas, ausentes, confianza
.venv/Scripts/python verificar_procesadores.py         # rutas, activar con grupos, errores, memoria de la estructura
```

## Documentos largos: extracción por bloques de páginas (2026-10-16)

Document AI rechaza en línea un documento de **más de 15 páginas** («Document pages exceed the limit: 15 got 20») y un acta
constitutiva ronda las 20. `POST /ia/extraer` ahora procesa esos documentos **por bloques de páginas** y une el resultado, hasta
`EXTRACCION_MAX_PAGINAS` (20 por default). Un documento de 15 páginas o menos sigue como siempre: una sola llamada.

**Lo que se midió contra Google antes de construirlo** (procesador de prueba, PDF de 20 páginas armado con el acta de 10 dos veces):

- El rechazo llega en ~2 s, **no se cobra**, y trae cuántas páginas tiene el archivo:
  `details[].metadata = {"page_limit": "15", "pages": "20"}` (`reason: PAGE_LIMIT_EXCEEDED`). No hace falta abrir el PDF ni una
  librería para contar páginas.
- `processOptions.individualPageSelector.pages` **esquiva el tope**: pidiendo solo las páginas 11 a 20 de un PDF de 20, Google
  contestó bien y cobró diez.
- `document.pages[].pageNumber` viene **absoluto** (11…20), pero las entidades apuntan **por índice** dentro de esa lista
  (`pageRefs[].page` = 0…9, y Google omite `page` cuando es 0). `ia._pagina_de` traduce con la lista de páginas del bloque.
- De punta a punta, por el camino del producto: 20 páginas en 13 s (los dos bloques corren a la vez), 21 páginas cobradas
  (US$0.63), 117 campos, ninguno apuntando al renglón de OCR de otra página.

**Cómo funciona** (`servicios/ia.py` orquesta; `servicios/bloques.py` es lógica pura, sin red):

1. Se intenta el documento entero, como siempre. Si Google lo rechaza por largo, el rechazo dice cuántas páginas tiene.
2. Más páginas que `EXTRACCION_MAX_PAGINAS` → 422 «El documento tiene 25 páginas y el máximo que se procesa es 20», **sin procesar
   (ni cobrar) nada**.
3. `planear_bloques`: el menor número de bloques, **parejos** (20 páginas con tope 15 son 11 + 10, no 15 + 5) y con **una página de
   solape**: una cláusula o una fila de tabla que cruza el corte sale completa en alguno de los dos bloques.
4. Cada bloque se pide con `individualPageSelector` (hasta 3 a la vez). Un bloque que falla por algo pasajero (`servicio`, `cuota`)
   se repite **una** vez; lo que es del documento (`archivo_ilegible`) no. **Todo o nada**: si un bloque falla, falla la extracción
   —entregar el documento sin un pedazo, callado, sería peor—, y los demás bloques se cancelan. Todo el trabajo tiene el tope de
   `IA_TIMEOUT` (504 si no termina), por debajo de los 130 s que espera el front.
5. Se unen los resultados como si el documento hubiera pasado entero: páginas del documento (no del bloque), una sola capa de OCR,
   `ocr.page_count` = el total, y `bloque_indice` de cada campo reapuntado a esa capa.

**Cómo se une** (las reglas, para que nadie las redescubra):

- Un campo **único** que salió en varios bloques: gana la **mayor confianza** (empate: el bloque más temprano). Excepción: un valor
  largo (30+ caracteres) en una página de solape que otro bloque trae completo —su texto lo contiene— se descarta: es el pedazo que
  el borde del bloque le cortó a la cláusula.
- Un campo **múltiple**: se concatenan las listas en el orden del documento, sin repetir lo que el solape mostró dos veces.
- Un **grupo** múltiple: se concatenan las filas, y la fila que dos bloques vieron (la del solape) se une en una sola con las columnas
  de los dos. «La misma entidad» = misma página, mismo texto y casi la misma caja (la caja no sale idéntica entre dos llamadas).
- `_metadata.campos_ausentes` se calcula sobre lo ya unido, y `_metadata` agrega `paginas_totales`, `bloques` (`[{desde, hasta}]`) y
  `paginas_procesadas` (lo que Google cobró: más que el total por las páginas del solape).

**Por qué bloques GRANDES** (medido con el acta real de 10 páginas y el esquema plano de 24 campos): 2 bloques dieron **lo mismo** que el
documento entero (16/24 exactos, campo por campo igual); 3 bloques de 4 páginas dieron 15/24. Un bloque que no trae el dato igual
devuelve un candidato para el campo, y a veces con más confianza que el bueno (la confianza no separa lo bueno de lo malo): entre más
bloques, más candidatos que se pelean un campo único. Por eso `EXTRACCION_PAGINAS_POR_BLOQUE` es 15, el máximo de Google: partir
más chico no mejora nada y empeora lo que se une.

**Configuración** (`.env`; los defaults sirven y no hay que tocar el del server):

| Variable | Default | Qué hace |
|---|---|---|
| `EXTRACCION_MAX_PAGINAS` | 20 | lo más largo que se acepta (política de negocio acordada para las actas); mínimo 15 |
| `EXTRACCION_PAGINAS_POR_BLOQUE` | 15 | tope de páginas de un bloque (15 es el de Google) |
| `EXTRACCION_SOLAPE_PAGINAS` | 1 | páginas que cada bloque repite del anterior (cada una se cobra dos veces) |

**Lo que NO cambia ni cubre:**

- `POST /ia/ine` **no** parte nada (`permitir_bloques=False`): una credencial de 20 páginas no es una credencial, y no se paga por
  procesarla. Responde 422 con los números de Google («tiene 20 páginas y el máximo es 15»).
- La **clasificación** ya miraba solo las primeras 2 páginas (`PAGINAS_PARA_CLASIFICAR`), así que un acta larga se clasifica igual.
- Un valor que cruza **tres** páginas sobre un corte puede salir partido (el solape de una página no alcanza para que un bloque lo vea
  entero). Una tabla o una cláusula típica de acta no llega a eso.
- La confianza sigue sin separar lo bueno de lo malo: lo que sale mal en un documento entero sale igual de mal por bloques.

**Verificarlo** (sin red):

```
.venv/Scripts/python verificar_bloques.py            # partir y unir: 53 casos (planear: 150×15×5 combinaciones; filas, OCR)
.venv/Scripts/python verificar_documentos_largos.py  # el flujo con un Google de mentiras: 66 casos (20 págs, solape, reintentos, tope, tiempo)
```
