# Solicitudes al DBA

Lista viva de lo que `nexus_back` necesita de la base. Charlie (el Lic.) tiene
libertad sobre la capa física: nombres de tabla, tipos exactos, índices y
estrategia son suyos. Este documento describe lo que la aplicación necesita
poder **representar** y **pedir**, no cómo debe construirse.

**Regla de trabajo**: la aplicación consume **stored procedures**, nunca tablas
directas. Si un SP cambia de forma, se adapta `repositorios/`; si cambian los
nombres de columna internos, a la aplicación no debería enterarse.

---

## 0. Orden en que se va pidiendo

Se pide **de poco a poco**, en pláticas cortas de escritorio. El orden no es
arbitrario: cada paso desbloquea al siguiente, y pedir de más de golpe invita a
rehacer trabajo. Marcar aquí lo que ya quedó.

| # | Qué pedir | Por qué va en este lugar | Así se verifica | Estado |
|---|---|---|---|---|
| 1 | Cadena de conexión + **un SP trivial** que devuelva cualquier cosa (un `SELECT 1`, la fecha del servidor) | Antes de cualquier esquema hay que probar que la app **alcanza** la base: driver ODBC, credenciales, firewall. Si algo de esa cadena falla, se descubre ahora y no enterrado dentro de un SP real | `GET /health/db` en verde | 🟡 conexión ✅ 2026-08-25, SP pendiente |
| 2 | `sp_ListarBandejaPreparacion` (solo lectura) | Primer SP de verdad, y de **lectura**: no puede corromper nada. Además es lo que el front ya necesita para HU027 | La Bandeja del front deja de ser memoria del navegador | ⬜ |
| 3 | Alta de **expediente** + alta de **file** (juntas) | Van juntas porque `file.expediente_id` es NOT NULL: no se puede registrar un archivo sin que exista antes su expediente | Subir un archivo en el front y que sobreviva a un refresh | ⬜ |
| 4 | Las secciones **2.6** y **2.7** del diccionario | Esto no es un SP, es una **plática de diseño** — conviene cuando ya haya confianza en el trato y él tenga contexto de lo anterior | Que quede acordado dónde vive el mapeo | ⬜ |
| 5 | Lectura de la **configuración activa** de un tipo documental | Depende de que exista lo de 2.6, porque incluye el mapeo | `servicios/ia.py` puede traducir nombres de Document AI a `field_definition_id` | ⬜ |
| 6 | **SP transaccional** de guardado de corrida completa | El más grande y el que más decisiones de forma tiene. Con todo lo anterior andando, ya se le puede plantear con datos reales en la mano | Una extracción de INE queda guardada entera o no queda | ⬜ |

### Estado del paso 1 al 2026-08-25

**La conexión ya funciona.** `GET /health/db` contra `172.10.30.15:8083` devuelve
`status: ok` — SQL Server 2022 Standard sobre Windows Server 2022, base `master`.
Quedó probado de punta a punta: driver ODBC 18 instalado y registrado, ruta de
red abierta, y las credenciales de `usrNexus` aceptadas.

Se cerró en este orden, y el orden importa porque cada falla se disfrazaba de la
siguiente:

1. Driver `msodbcsql18` instalado en el server (tarea de sysadmin, no del DBA).
   Confirmado con `odbcinst -q -d` → `[ODBC Driver 18 for SQL Server]`, que
   coincide textual con el default de `config.py`.
2. Datos de conexión al `.env`: host `192.168.104.47`, **puerto 2025** (no 1433),
   `usrNexus`, y `SQLSERVER_DB=master` como **sonda** — `master` existe en todo
   SQL Server, así que valida la cadena completa sin depender del nombre real de
   la base, que todavía no teníamos.
3. La red. Fue lo último y lo que de verdad bloqueaba: el puerto se liberó el
   2026-08-25. Antes de eso, `pyodbc` daba `OperationalError` con TCP cerrado, y
   ese error se ve igual que un problema de credenciales.

**Lo que sigue faltando del paso 1: el SP trivial.** Y no es un trámite: la
prueba de arriba corre `SELECT @@VERSION`, que demuestra que el login **conecta**
pero NO que pueda ejecutar stored procedures. Como el modelo de permisos
acordado es `EXECUTE` y nada más (sin `SELECT` a tablas), hasta que no haya un SP
real invocado no sabemos si ese permiso está bien puesto. Es exactamente el tipo
de cosa que aparecería después, disfrazada de bug de la aplicación.

**Sigue faltando el nombre real de la base.** Mientras siga en `master`, la app
está apuntando a la base de sistema. Alternativa a preguntarle: desde el server,
`SELECT name FROM sys.databases` muestra las que el login alcanza a ver.

**Lo que este paso dejó como aprendizaje de diagnóstico**: `/health/db` ahora
publica el `SQLSTATE` siempre (no solo cuando está en la tabla de pistas), y
distingue "falta configuración" de "falló la conexión". La primera versión
devolvía únicamente `{"status":"error","error":"OperationalError"}`, que no
alcanza para saber cuál de los tres eslabones se rompió.

---

## 1. Sección faltante del diccionario: mapeo motor ↔ campo

El diccionario v0.4 define `field_definition` como el catálogo de campos, y
`entity_fact.field_definition_id` como NOT NULL. Pero cuando la extracción la
hace un motor con **esquema propio y externo** —como un procesador custom de
Document AI— nada dice cómo se traduce el nombre que emite el motor al
`field_definition` que le corresponde. Sin esa traducción no se puede insertar
un solo `entity_fact`.

Redactado en el formato del diccionario para que se pueda pegar tal cual:

### 2.6 · extractor_field_map

Traducción entre los campos que emite un motor de extracción externo y el
catálogo `field_definition` de una versión de configuración. Solo aplica a
motores cuyo esquema vive fuera de la base (hoy: `document_ai_extractor`).

| Columna | Tipo | Nulo | Descripción y reglas |
|---|---|---|---|
| `id` | uuid | N | PK. |
| `config_version_id` | uuid | N | FK → config_version. El mapeo pertenece a la versión, no al tipo documental. Ver nota. |
| `field_definition_id` | uuid | N | FK → field_definition. Campo del catálogo al que corresponde lo que emitió el motor. |
| `engine` | enum extraction_engine | N | El mismo enum de `extraction_run.engine`. Un mapeo solo aplica al motor que declara. |
| `source_path` | varchar(120) | N | Ruta del campo tal como la emite el motor, con notación punteada para el anidamiento (`domicilio.estado`). Ver nota. |
| `transform` | varchar(60) | S | Qué parte del valor emitido corresponde a este campo. NULL = el valor completo. Ver nota. |

**Llaves y restricciones:** FK(config_version_id) · FK(field_definition_id) ·
UNIQUE(config_version_id, engine, source_path, **transform**) ·
UNIQUE(config_version_id, engine, field_definition_id)

**Nota:** Cuelga de `config_version` y no de `document_type` por la misma razón
que `field_definition`: `extraction_run.config_version_id` existe para congelar
bajo qué configuración se extrajo. Si el mapeo colgara del tipo, se podría
cambiar sin crear versión nueva y una re-corrida del mismo `config_version_id`
daría otro resultado — se rompe la garantía en silencio. El mapeo **es**
configuración, así que hereda la inmutabilidad de la versión activada (2.1).

**Nota:** La llave es una **ruta**, no un nombre de campo, porque los motores
emiten estructuras anidadas donde un mismo nombre aparece en varios niveles con
significados distintos. Verificado contra el procesador de INE en producción:
`estado` y `localidad` existen **a la vez** en la raíz y dentro de `domicilio`,
con valores distintos (los de raíz son claves del padrón electoral, los de
domicilio son texto del renglón de dirección). Un mapeo por nombre simple los
colapsaría y perdería un dato sin avisar.

**Nota (CORRECCIÓN del 2026-08-25, hallada midiendo datos reales):** La primera
versión de esta sección declaraba `UNIQUE(config_version_id, engine, source_path)`
—o sea, un campo emitido mapea a **un solo** `field_definition`— y eso resultó
demasiado estrecho. Existe al menos un caso real que no cabe:

El procesador de INE emite `fecha_registro` con el valor `"2024 00"`. En la
credencial es **un solo campo impreso** (la etiqueta dice "AÑO DE REGISTRO"),
pero contiene **dos hechos distintos**: el año en que la persona se registró en
el padrón, y el número de emisión de la credencial. Medido sobre 46 INEs reales,
ver [`esquema-procesador-ine.md`](esquema-procesador-ine.md) para la evidencia.

Son dos hechos con semántica, tipo y uso distintos, así que corresponden a dos
`field_definition`. Pero los dos vienen del MISMO `source_path`, y la restricción
original lo prohibía. De ahí la columna `transform` y la llave nueva: el par
(`source_path`, `transform`) es lo único que tiene que ser único.

`transform` NO es código ni una expresión: es un **nombre de catálogo** que la
aplicación sabe aplicar (para este caso, algo como `anio` y `numero_emision`).
La base guarda el nombre; la lógica vive en la aplicación. Meter expresiones
regulares en una columna sería darle a la base un trabajo que no le toca, y
volvería el mapeo imposible de validar.

**Nota:** No todos los motores necesitan renglones aquí. Si la extracción se
arma como prompt desde `field_definition` (la ruta `azure_openai`), el motor
devuelve los campos por su propio `code` y no hay nada que traducir. La tabla
existe para los motores de esquema externo; el `engine` en la llave permite que
convivan ambas rutas sin migrar nada.

**Nota:** Integridad que la aplicación debe cuidar y que conviene decidir:
1. Campo que emite el motor y **no está mapeado** → se descarta, pero debería
   dejar rastro en `audit_event`: significa que el procesador cambió y la
   configuración no se actualizó.
2. `field_definition` **sin mapeo** → cada corrida debería producir su
   `entity_fact` con `null_reason`. `extraction_error` es más honesto que
   `not_present`: el campo nunca se buscó.
3. `field_definition.required = true` **sin mapeo** → debería impedir la
   transición `draft → active` de la versión. Es un error de configuración, no
   un hallazgo de extracción.

### 2.7 · extractor_binding

Qué motor y qué despliegue concreto sirve a una versión de configuración.
`extraction_run.engine_version` registra lo que se usó **en tiempo de corrida**;
esto declara, **en tiempo de configuración**, a quién hay que llamarle.

| Columna | Tipo | Nulo | Descripción y reglas |
|---|---|---|---|
| `id` | uuid | N | PK. |
| `config_version_id` | uuid | N | FK → config_version. |
| `engine` | enum extraction_engine | N | Motor que atiende esta versión. |
| `processor_ref` | varchar(200) | N | Identificador del despliegue en el proveedor. Para Document AI, el id del procesador (`bf35151f51b51521`). |
| `processor_version` | varchar(120) | S | Versión exacta del modelo. NULL = usar la default del proveedor, **con la advertencia de la nota**. |
| `enabled` | boolean | N | Default true. |

**Llaves y restricciones:** FK(config_version_id) · UNIQUE(config_version_id, engine)

**Nota:** Dejar `processor_version` en NULL tiene un costo real: Google resuelve
la versión "default" del procesador en cada llamada y puede moverla el día que
promueva otra a estable. Entonces `extraction_run.engine_version` guardaría una
suposición en una columna que existe justamente para dar reproducibilidad. Hoy
`nexus_back` la fija en su `.env` (`DOCAI_VERSION_INE`) y loguea un warning si
está vacía; al migrar esa configuración a la base, la columna hereda ese papel.

**Nota:** Alternativa considerada y descartada: dos columnas en `config_version`
en vez de tabla aparte. Se prefirió la tabla porque permite que una misma
versión conviva con más de un motor mientras se evalúa un cambio de ADR, sin
alterar la forma de `config_version`.

**Nota (hallazgo de campo):** Anclar la versión **no vuelve determinista la
confianza**. Medido en producción: dos llamadas al mismo documento con la misma
versión anclada devuelven valores y posiciones idénticos, pero la `confidence`
de algunos campos varía (ej. 98.74 → 97.18) — son modelos generativos y esa
variación es inherente. Como el diccionario define `entity_fact.confidence` como
"lo que consumen motor de reglas y ruteo a revisión", un campo pegado a un
umbral puede rutear a revisión en una corrida y no en la siguiente. Los umbrales
necesitan margen o histéresis; `prompt_hash` + `config_version_id` garantizan
que la **entrada** fue la misma, no que la **salida** lo sea.

---

## 2. Stored procedures a solicitar

Contratos que la aplicación necesita. Nombres tentativos: lo que importa son los
parámetros de entrada y las columnas de salida.

### Ya invocados por el código (hoy con nombres placeholder)

| SP | Entrada | Salida esperada | Usado por | Estado |
|---|---|---|---|---|
| `sp_ListarBandejaPreparacion` | `@TenantId` | un renglón por archivo pendiente de pipeline | `repositorios/documentos.py::listar_bandeja` (HU027) | ⬜ pendiente |
| `sp_RegistrarDocumento` | `@TenantId`, `@NombreArchivo`, `@HashSha256`, `@Origen` | el renglón creado | `repositorios/documentos.py::registrar_documento` | ⬜ pendiente |

⚠️ El segundo está **mal nombrado**: por el modelo registra un **`file`**, no un
`document` (un archivo puede contener varios documentos lógicos, 1.5). Además le
falta `@ExpedienteId`, que en el modelo es NOT NULL. Corregir al pedirlo.

### Pendientes de definir

- Alta de expediente (`file.expediente_id` es NOT NULL, así que algo tiene que
  crearlo antes de la primera carga — o existe un expediente de entrada).
- Guardado de una corrida completa: `ocr_result` + sus `ocr_block` +
  `extraction_run` + sus `entity_fact`, idealmente en **un solo SP
  transaccional** para que no queden corridas a medias.

  Volumen medido en una INE real de **una sola página**: 23 `ocr_block` (líneas)
  + 20 `entity_fact` + 1 `ocr_result` + 1 `extraction_run` = **45 renglones por
  documento**. Un PDF de 10 páginas escala a cientos. Por eso importa que sea un
  solo SP y no 45 llamadas: además de la atomicidad, es la diferencia entre una
  vuelta de red y cuarenta y cinco. Vale preguntarle a Charlie si prefiere
  recibir los bloques como **table-valued parameter** — es su decisión de forma,
  pero el dato de volumen es lo que la hace relevante.
- Lectura de la configuración activa de un tipo documental: `config_version`
  activa + sus `field_definition` + el mapeo de 2.6, que es lo que
  `servicios/ia.py` necesita para traducir la salida del motor.
