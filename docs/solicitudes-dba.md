# Solicitudes al DBA

Lista viva de lo que `nexus_back` necesita de la base. Charlie (el Lic.) tiene
libertad sobre la capa física: nombres de tabla, tipos exactos, índices y
estrategia son suyos. Este documento describe lo que la aplicación necesita
poder **representar** y **pedir**, no cómo debe construirse.

**Regla de trabajo**: la aplicación consume **stored procedures**, nunca tablas
directas. Si un SP cambia de forma, se adapta `repositorios/`; si cambian los
nombres de columna internos, a la aplicación no debería enterarse.

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

**Llaves y restricciones:** FK(config_version_id) · FK(field_definition_id) ·
UNIQUE(config_version_id, engine, source_path) ·
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

| SP | Entrada | Salida esperada | Usado por |
|---|---|---|---|
| `sp_ListarBandejaPreparacion` | `@TenantId` | un renglón por archivo pendiente de pipeline | `repositorios/documentos.py::listar_bandeja` (HU027) |
| `sp_RegistrarDocumento` | `@TenantId`, `@NombreArchivo`, `@HashSha256`, `@Origen` | el renglón creado | `repositorios/documentos.py::registrar_documento` |

⚠️ El segundo está **mal nombrado**: por el modelo registra un **`file`**, no un
`document` (un archivo puede contener varios documentos lógicos, 1.5). Además le
falta `@ExpedienteId`, que en el modelo es NOT NULL. Corregir al pedirlo.

### Pendientes de definir

- Alta de expediente (`file.expediente_id` es NOT NULL, así que algo tiene que
  crearlo antes de la primera carga — o existe un expediente de entrada).
- Guardado de una corrida completa: `ocr_result` + sus `ocr_block` +
  `extraction_run` + sus `entity_fact`, idealmente en **un solo SP
  transaccional** para que no queden corridas a medias.
- Lectura de la configuración activa de un tipo documental: `config_version`
  activa + sus `field_definition` + el mapeo de 2.6, que es lo que
  `servicios/ia.py` necesita para traducir la salida del motor.
