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

**Cómo se marca el estado** — la columna lleva un solo símbolo y, si aplica, la
fecha en que se cerró:

| Símbolo | Significa |
|---|---|
| ⬜ | Sin empezar. No se le ha pedido todavía. |
| 🟡 | En curso: pedido y parcialmente resuelto. Se anota **qué falta**, no solo que falta. |
| ✅ | Cerrado **y verificado desde la aplicación**, con fecha. Que él diga "ya quedó" no basta: se marca cuando `nexus_back` lo pudo consumir de verdad. |
| 🔵 | Entregado por él sin habérselo pedido, fuera del orden planeado. |

La distinción de ✅ importa: casi todo lo que salió mal en el paso 1 (driver,
puerto, red, permiso de base) se veía como "ya está" de un lado y seguía roto
del otro. La verificación es siempre desde la app, no desde SSMS.

| # | Qué pedir | Por qué va en este lugar | Así se verifica | Estado |
|---|---|---|---|---|
| 1 | Cadena de conexión + **un SP trivial** que devuelva cualquier cosa (un `SELECT 1`, la fecha del servidor) | Antes de cualquier esquema hay que probar que la app **alcanza** la base: driver ODBC, credenciales, firewall. Si algo de esa cadena falla, se descubre ahora y no enterrado dentro de un SP real | `GET /health/db` en verde | ✅ **2026-08-26**. El SP trivial resultó innecesario: un SP real (`uspCreateTenant`) probó el `EXECUTE` al fallar con `RAISERROR` 50006 |
| — | **Tenants: `uspGetTenant` / `uspCreateTenant` / `uspUpdateTenant`** | No estaba en el plan: los entregó él por su cuenta. Encaja bien porque el tenant es la raíz de todo lo demás — el `@TenantId` que asumían los placeholders sale de aquí | Crear, leer y actualizar un tenant desde `repositorios/tenants.py` | 🔵🟡 firmas y vocabulario ✅; **bloqueado** por `tenantSequence` vacía (ver 0b) |
| 2 | Listar bandeja de preparación (solo lectura) | Primer SP de **lectura** de datos documentales: no puede corromper nada. Además es lo que el front ya necesita para HU027 | La Bandeja del front deja de ser memoria del navegador | ⬜ |
| 3 | Alta de **expediente** + alta de **file** (juntas) | Van juntas porque `file.expediente_id` es NOT NULL: no se puede registrar un archivo sin que exista antes su expediente | Subir un archivo en el front y que sobreviva a un refresh | ⬜ |
| 4 | Las secciones **2.6** y **2.7** del diccionario | Esto no es un SP, es una **plática de diseño** — conviene cuando ya haya confianza en el trato y él tenga contexto de lo anterior | Que quede acordado dónde vive el mapeo | ⬜ |
| 5 | Lectura de la **configuración activa** de un tipo documental | Depende de que exista lo de 2.6, porque incluye el mapeo | `servicios/ia.py` puede traducir nombres de Document AI a `field_definition_id` | ⬜ |
| 6 | **SP transaccional** de guardado de corrida completa | El más grande y el que más decisiones de forma tiene. Con todo lo anterior andando, ya se le puede plantear con datos reales en la mano | Una extracción de INE queda guardada entera o no queda | ⬜ |

**Consecuencia de los SPs de tenant para lo que sigue:** `[security].[tenants]`
tiene **dos** identificadores — `tenantId int IDENTITY` (interno) y
`tenantGuid uniqueidentifier` (público), y `uspGetTenant` recibe el **GUID**.
Los placeholders de `repositorios/documentos.py` asumían un `@TenantId`
entero. Al pedir los SPs de los pasos 2 y 3 conviene alinearse a su convención
y recibir el **GUID**, no el int: expone menos del modelo interno y es lo que
ya viaja por la API.

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

**Lo que faltaba del paso 1 en ese momento: el SP trivial.** _(Superado el
2026-08-26, ver más abajo — se deja el razonamiento porque sigue siendo válido y
explica por qué se insistía en ese paso.)_ No era un trámite: la prueba de
arriba corre `SELECT @@VERSION`, que demuestra que el login **conecta** pero NO
que pueda ejecutar stored procedures. Como el modelo de permisos acordado es
`EXECUTE` y nada más (sin `SELECT` a tablas), hasta no invocar un SP real no se
sabía si ese permiso estaba bien puesto. Es exactamente el tipo de cosa que
aparece después, disfrazada de bug de la aplicación.

**Actualización 2026-08-26: el nombre real de la base es `IA_Nexus`, y YA
QUEDÓ VERIFICADO en producción.** Charlie lo confirmó, se actualizó
`SQLSERVER_DB` en el `.env` del server y se reinició `nexus-back-api`.
`GET /health/db` respondió:

```json
{"status":"ok","version":"Microsoft SQL Server 2022 (RTM) - 16.0.1000.6 (X64) ... Standard Edition (64-bit) on Windows Server 2022 Standard","base":"IA_Nexus"}
```

Esto prueba algo DISTINTO de lo que ya probaba `master`: que `usrNexus` puede
**entrar** a `IA_Nexus` específicamente, no solo autenticarse contra el server
— son permisos independientes en SQL Server, y ya están bien puestos.

**Paso 1 CERRADO el 2026-08-26 — el permiso `EXECUTE` quedó confirmado, y sin
necesitar el SP trivial.** Charlie entregó tres SPs de negocio reales
(`[security].[uspGetTenant]`, `[uspCreateTenant]`, `[uspUpdateTenant]`) y al
llamar al de creación devolvió:

```
('42000', '[42000] ... The tenant sequence is not configured. (50006)')
```

Ese número **50006 está por encima de 50000**, o sea es un error *definido por
el usuario*: un `RAISERROR`/`THROW` desde dentro del propio SP. Eso prueba que
el SP **se ejecutó** y que lo que falló fue una validación suya — el permiso
`EXECUTE` está bien puesto. El SP trivial ya no hace falta: un SP real lo
demostró.

Aprendizaje que se llevó a código: `SQLSTATE 42000` está **sobrecargado** — lo
usa tanto un problema de permisos como un `RAISERROR` desde un SP. La pista de
`app.py` decía solo "no hay permiso sobre la base", y en este caso habría
mandado a revisar permisos justo cuando estaban correctos. Ya distingue las dos
causas (la señal es el número >= 50000).

**Lo que este paso dejó como aprendizaje de diagnóstico**: `/health/db` ahora
publica el `SQLSTATE` siempre (no solo cuando está en la tabla de pistas), y
distingue "falta configuración" de "falló la conexión". La primera versión
devolvía únicamente `{"status":"error","error":"OperationalError"}`, que no
alcanza para saber cuál de los tres eslabones se rompió.

---

## 0a. LO QUE SE LE LLEVA AHORA (2026-08-26)

**Queda la siembra de `[security].[tenantSequence]`**, y confirmar si el `GRANT
VIEW DEFINITION` ya se ejecutó (ver abajo — pedido, sin verificar). La pregunta
del `tenantGuid` sí quedó contestada.

> **Sembrar `[security].[tenantSequence]` — es lo único que bloquea.**
>
> `uspCreateTenant` falla con `The tenant sequence is not configured (50006)`
> porque esa tabla está en 0 filas. Ya verificamos que el SP corre bien y que
> los permisos están correctos; solo le falta su fila de configuración.
>
> **Prefijo propuesto: `NEX`** (decisión de Moibe, 2026-08-26 — sigue siendo de
> él si prefiere otro). El razonamiento: `tenantCode` es `varchar(8)` y `prefix`
> es `varchar(5)`, así que se reparten esos 8 caracteres. Con 3 letras quedan 5
> dígitos (`NEX00001` → ~99,999 tenants); con 5 letras quedan 3 (`NEXUS001` →
> 999). **Un prefijo más corto no cuesta nada** — no se gana nada usando 5
> letras salvo estética, así que se prefiere el margen. 999 alcanzaría de sobra
> para el negocio real, pero el margen es gratis.
>
> Es una **puerta de una sola dirección**: hoy es gratis cambiar de opinión
> porque hay 0 tenants; con el primero creado, los códigos viejos quedarían con
> el prefijo viejo y habría dos formatos conviviendo (o un cambio de esquema
> para ampliar `tenantCode`).
>
> La fila completa: `prefix='NEX'`, `lastSequence=0`, `isActive=1`, más
> `createdAt` y `createdBy` con el estilo que él use (sus filas llevan su cuenta
> de dominio, `PROCHURGRUPOCSI\carlos.ramirez`). `tenantSequenceId` es
> `IDENTITY`, no se da.

### 🟡 Pedido, SIN VERIFICAR: `GRANT VIEW DEFINITION ON SCHEMA::security TO usrNexus`

Moibe se lo pidió a Charlie el 2026-08-26. **No está confirmado que se haya
ejecutado** — y esto vale como recordatorio de la propia convención de arriba:
se marca ✅ cuando `nexus_back` lo consume, no cuando alguien dice que quedó.
(Este renglón estuvo un rato marcado como ✅ por una suposición; se corrigió.)

**Cómo verificarlo en un comando** — si devuelve el código del SP, quedó; si
devuelve `None`, todavía no:

```bash
cd /home/mbriseno/code/nexus_back && venv/bin/python -c "
import config
from db.sqlserver import obtener_conexion
c = obtener_conexion(); cur = c.cursor()
cur.execute(\"SELECT OBJECT_DEFINITION(OBJECT_ID('[security].[uspCreateTenant]'))\")
d = cur.fetchone()[0]
print('GRANT ACTIVO' if d else 'TODAVIA NO — OBJECT_DEFINITION devuelve NULL')
c.close()"
```

Cuando esté, `diagnostico_tenants.py` puede volcar el cuerpo de los SPs y la
definición de las restricciones `CHECK`, y con eso confirmar tres cosas que hoy
siguen siendo deducción: cómo arma exactamente `tenantCode` a partir del
`prefix`, qué status asigna por default, y qué escribe en `createdBy` cuando lo
llama la aplicación.

### ✅ Resuelto: `uspCreateTenant` sí devuelve el `tenantGuid`

Confirmado por Charlie el 2026-08-26. Dos consecuencias:

1. **No hay que pedirle nada del `SELECT` final** — devuelve lo que la
   aplicación necesita. `uspGetTenant` recibe `uniqueidentifier`, así que el
   GUID era el dato indispensable: con solo el `tenantId` interno, un tenant
   recién creado habría quedado irrecuperable (no hay SP de listar).
2. **Tampoco hace falta pedirle `SET NOCOUNT ON`.** Era un plan B por si el
   contador de filas del `INSERT` se colaba como primer result set;
   `_primer_set_con_filas` en `repositorios/tenants.py` ya lo neutraliza, así
   que da igual si el SP lo tiene o no.

Queda por verificar **desde la aplicación** en cuanto exista la fila de
`tenantSequence` — que él lo confirme es la mitad; la otra es que
`verificar_tenants.py` complete el ciclo crear → leer → actualizar.

Lo que **no** conviene pedirle todavía (para no romper el "de poco a poco"): el
SP de listar tenants, el de baja, ni nada de los pasos 2-6. Primero que esto
funcione de punta a punta.

---

## 0b. Detalle técnico: sembrar `[security].[tenantSequence]`

**Es lo único que bloquea `uspCreateTenant` hoy**, y es una decisión suya, no un
bug. La tabla existe con su estructura completa pero está en **0 filas**, y el
SP levanta el `RAISERROR` 50006 justo por eso.

Verificado que **no** es un objeto `SEQUENCE` de SQL Server: `sys.sequences`
está vacío en toda la base. Es esta tabla de configuración:

| Columna | Tipo | Nulo | Notas |
|---|---|---|---|
| `tenantSequenceId` | int | N | IDENTITY |
| `prefix` | varchar(5) | N | **decisión de negocio** — ver abajo |
| `lastSequence` | int | N | presumiblemente arranca en 0 |
| `isActive` | bit | N | sugiere que el SP busca la fila activa; probablemente debe haber exactamente una |
| `createdAt` | datetime2 | N | **sin DEFAULT** — hay que darle valor al insertar |
| `createdBy` | varchar(50) | N | **sin DEFAULT** |
| `updatedAt` / `updatedBy` | | S | |

**Por qué el `prefix` es decisión suya y no nuestra:** `[security].[tenants]`
tiene `tenantCode varchar(8)`, así que el prefijo y el consecutivo tienen que
caber juntos en 8 caracteres — un prefijo de 3 deja 5 dígitos (`NEX00001`), uno
de 5 deja 3 (`NEXUS001`). Eso fija el techo de tenants numerables y es
nomenclatura de negocio; no conviene que la aplicación lo elija.

**Preguntas que conviene cerrar junto con la siembra:**

1. ¿La tabla admite **más de una** fila (varias secuencias, una activa)? El
   `isActive` lo sugiere. Importa porque si él siembra una y alguien más siembra
   otra, el SP podría elegir la equivocada en silencio.
2. `uspCreateTenant` no recibe `@createdBy`, así que ese campo lo llena el SP
   solo. Con el login de la aplicación quedaría `usrNexus` en la auditoría de
   todo lo que cree la API — correcto, pero conviene que sea a propósito. Sus
   propias filas sembradas traen `PROCHURGRUPOCSI\carlos.ramirez`.
3. `uspCreateTenant` tampoco recibe status, y `tenants.tenantStatusId` es NOT
   NULL: ¿asigna `ACTIVE` (id 1) por default?
4. ~~¿`uspCreateTenant` **devuelve** el registro creado?~~ **✅ Resuelto
   2026-08-26: sí, y devuelve el `tenantGuid` específicamente** — que era el
   dato indispensable, porque `uspGetTenant` exige el GUID y no hay SP de
   listar. Pendiente solo confirmarlo desde la aplicación cuando la siembra
   permita insertar.

## 0c. Estado del esquema al 2026-08-26 (leído, no supuesto)

Charlie tiene el esquema construido pero casi sin sembrar. De 6 tablas en
`[security]`, solo `roles` tiene datos:

| Tabla | Filas |
|---|---|
| `[security].[roles]` | 4 — `ADMIN`, `CONFIGURATOR`, `REVIEWER`, `QUERY` |
| `[reference].[tenantStatuses]` | 3 — `ACTIVE`, `SUSPENDED`, `CLOSED` |
| `[security].[tenants]` | 0 |
| `[security].[tenantSequence]` | 0 ← bloqueante |
| `[security].[users]` | 0 |
| `[security].[tenantMemberships]` | 0 |
| `[security].[logsEndpoint]` | 0 |

**Convenciones suyas que conviene respetar en todo lo que se le pida:** cada
tabla lleva `isActive` + `isDeleted` (borrado lógico, no físico), auditoría con
`createdAt`/`createdBy`/`updatedAt`/`updatedBy`, catálogos con
`code`/`name`/`description`/`sortOrder`, PK `int IDENTITY` interna más
`uniqueidentifier` público (`tenants` tiene ambos: `tenantId` y `tenantGuid`), y
un esquema `[reference]` aparte para los catálogos. Los nombres van en inglés
camelCase.

**Pendiente menor que ahorraría mucho tiempo:** hoy `usrNexus` no tiene
`VIEW DEFINITION`, así que no se puede leer el cuerpo de los SPs ni la
definición de las restricciones `CHECK` (`OBJECT_DEFINITION` devuelve NULL).
Cada falla hay que deducirla desde afuera en vez de leer qué valida el SP. Un
`GRANT VIEW DEFINITION ON SCHEMA::security TO usrNexus` lo resolvería y **no da
acceso a ningún dato** — solo a la definición de los objetos.

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

`transform` NO es código ni una expresión: es un **nombre de catálogo cerrado**
que la aplicación sabe aplicar. La base guarda el nombre; la lógica vive en la
aplicación. Meter expresiones regulares en una columna sería darle a la base un
trabajo que no le toca, y volvería el mapeo imposible de validar por inspección.

**Nota (vocabulario de `transform`): posicional, no semántico.** El catálogo
arranca con dos valores genéricos, no con nombres específicos del tipo documental:

| `transform` | Qué hace |
|---|---|
| NULL | El valor completo, sin partir. Es el caso de 18 de los 19 campos de INE. |
| `token_1` | Primer grupo al partir el valor por espacios o puntuación. |
| `token_2` | Segundo grupo. |

Se eligió posicional después de descartar el semántico (`anio`, `numero_emision`),
por dos razones:

1. **El catálogo no crece con cada tipo documental.** Un nombre como `anio` solo
   sirve para este campo de la INE; `token_1` sirve para cualquier valor compuesto
   de cualquier documento. Como el catálogo es cerrado —y eso es a propósito— cada
   entrada nueva exige un despliegue, no una configuración. Mantenerlo genérico es
   lo que evita que ese costo se pague una vez por tipo documental.
2. **Ya cubre un segundo caso real sin tocar código.** `vigencia` a veces llega
   como año suelto (`2029`) y a veces como rango (`2024-2034`); con `token_1` /
   `token_2` se resuelve igual que `fecha_registro`. Sin el vocabulario genérico
   habría hecho falta agregar otro par de nombres.

La semántica no se pierde: vive en el `field_definition` al que el renglón apunta.
El mapeo se lee *"token_1 de `fecha_registro` → campo `anio_registro`"*, que es
inequívoco. Y como el vocabulario es cerrado, la UI que lo marque solo puede
ofrecer un desplegable con lo que el código implementa — nunca un campo de texto
libre.

**Nota (cómo llenan `transform` los campos que no se parten):** Con `NULL`. De
los 19 campos que emite el procesador de INE, **18 llevan `transform` en NULL** —
`curp`, `nombre`, `seccion` y el resto se toman completos. Solo `fecha_registro`
produce dos renglones con la columna llena. O sea la columna está vacía en la
enorme mayoría, y así debe ser: NULL significa "usa el valor tal como lo emitió
el motor".

Eso tiene una consecuencia que conviene decidir a propósito, porque la columna
vive **dentro de la llave única**:

- **SQL Server trata los NULL como IGUALES para efectos de UNIQUE**, a diferencia
  del estándar SQL (donde `NULL ≠ NULL` y los duplicados se colarían). Aquí eso
  nos favorece: dos renglones `(v1, document_ai_extractor, 'curp', NULL)` siguen
  chocando, así que sigue siendo imposible mapear el mismo `source_path` dos
  veces sin transformación. La restricción NO se debilita con los NULL.
- Es una **dependencia del motor**. Confirmado que el servidor es SQL Server 2022
  Standard, así que aplica. Si esto se moviera algún día a PostgreSQL, la
  restricción se volvería laxa en silencio.

**Alternativa a considerar, decisión del DBA:** un valor centinela en vez de NULL
(`transform = 'completo'` en esas 18 filas). Cuesta 18 valores de relleno, pero
deja la columna NOT NULL, no depende de la semántica del NULL de ningún motor, y
vuelve las consultas más simples. Muchos DBAs prefieren no tener columnas
nullables dentro de una llave única justamente por eso. Las dos opciones son
válidas; lo que no conviene es que quede sin decidir.

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

### 2.6a bis · `config_version` necesita el procesador que la respalda

*(Agregado el 2026-08-28, cuando "Activar" empezó a crear el Custom Extractor
por API.)*

Al activar una versión de configuración, el sistema crea (o adopta) un
procesador de Document AI y ese vínculo tiene que persistir. Dos columnas para
`config_version`:

- `processor_id` — el id del Custom Extractor (hoy: `nexusdoc--{id}` como
  displayName; el id real es el hash de Google, p. ej. `5cb9deac0ad13f00`).
- `processor_version` — la versión foundation con la que nació (p. ej.
  `pretrained-foundation-model-v1.5-pro-2025-06-20`). Se FIJA en cada
  extracción: la default de Google cambia sin aviso y ya nos mordió la
  reproducibilidad una vez.

Encaja con la inmutabilidad de `config_version`: cada versión registra
exactamente qué modelo la sirve. Mientras no hay base, los dos valores viven en
el localStorage del front junto al tipo documental.

## 2.6b · Cuándo y dónde se marca el mapeo

No es una tabla, es la secuencia de uso — pero define qué tiene que permitir el
modelo, así que va aquí.

**El mapeo no se puede declarar en abstracto.** El paso 2 del wizard del Módulo de
configuración ("Nuevo campo de extracción": nombre, descripción funcional,
obligatorio) declara el `field_definition` sin ningún documento enfrente. Ahí es
imposible saber que `fecha_registro` trae dos datos pegados: pedir que se marque
una partición sin ver el valor es pedir que se adivine.

**La pantalla correcta ya está diseñada.** El frame de HU039-041, "Calibración de
campos extraídos", dice literalmente *"Carga y etiqueta un documento de ejemplo
para asociar sus valores a los campos configurados"*. Esa asociación **es**
`extractor_field_map`. Es el lugar correcto porque hay un documento real enfrente
y el valor `2024 00` se ve.

**Pero hay una tensión con la inmutabilidad, y hay que resolverla en el diseño.**
Esa sección de Figma se titula "sobre una versión **activa** de la Configuration
Table", y por 2.1 una versión activa está congelada. Los ejemplos few-shot pueden
colgarse de una versión activa —son datos de entrenamiento, no configuración—
pero **el mapeo sí es configuración**: marcarlo sobre una versión activa haría que
una re-corrida del mismo `config_version_id` diera otro resultado, que es
exactamente lo que la inmutabilidad protege.

Fases, entonces:

| Fase de `config_version` | Qué se permite |
|---|---|
| `draft` | Declarar campos, **marcar mapeos y `transform`** |
| Transición a `active` (HU038) | Validar que todo `field_definition.required` tenga mapeo — regla de integridad #3 de 2.6 |
| `active` | Cargar y etiquetar ejemplos few-shot. **Mapeos congelados** |
| Corregir un mapeo ya activo | Obliga a versión nueva. No hay atajo, y eso es la garantía funcionando |

**Falta un paso en el diseño actual del wizard:** no hay un momento de *"corre
este documento de muestra y muéstrame qué emite el motor"* durante `draft`. Es
viable, porque lo que emite Document AI **no depende de nuestra configuración** —
se puede correr una extracción de sonda antes de activar nada. Ese paso
convertiría el paso 2 de *escribir nombres a ciegas* a *descubrirlos del motor*,
que es como funciona el botón "Generar a partir de un documento" de la consola de
Google.

### 2.6c · El vocabulario de `transform` tiene DOS familias

Descubierto el 2026-08-27 al construir el paso 3 del wizard ("Propiedades de
campo"). El UX ya había diseñado ahí un desplegable de **"Reglas de
transformación"** con este catálogo:

| Regla del diseño | Qué hace |
|---|---|
| Normalización de fechas a ISO 8601 | `08/05/1997` → `1997-05-08` |
| Eliminación de símbolos de moneda | `$1,250.00` → `1250.00` |
| Conversión a mayúsculas o minúsculas | uniforma la caja |
| Trim de espacios | quita espacios sobrantes |
| Variantes textuales a valor canónico | `MEX`, `Mexico`, `MÉXICO` → un solo valor |

Eso obliga a corregir lo que dice la sección 2.6 sobre el vocabulario. Hay **dos
familias distintas** de transformación, y la columna tiene que alojar ambas:

1. **Partición** (`token_1`, `token_2`): toma una PARTE de un valor compuesto.
   Es la que resuelve `fecha_registro` = `"2024 00"`. Un `source_path` produce
   varias filas.
2. **Normalización** (las cinco de arriba): limpia o convierte el valor
   COMPLETO. Un `source_path`, una fila.

No compiten — se componen. `fecha_registro` necesitaría `token_1` para quedarse
con el año, y podría además necesitar normalización. Si eso se vuelve común,
`transform` tendría que aceptar una secuencia y no un solo nombre; hoy con un
valor alcanza, y agregar la secuencia después no rompe nada porque la columna ya
es texto.

**Dos de estas reglas YA existen hardcodeadas** en `servicios/ia.py` para INE: la
normalización de fechas a ISO en `_valor_normalizado` y la limpieza de puntuación
del domicilio en `_limpiar_ine`. Cuando esto se guarde en base, esas dos dejan de
ser código fijo y pasan a ser configuración — que es exactamente lo que el
diccionario persigue.

### 2.6d · Cardinalidad + obligatorio = `occurrenceType` de Document AI

El paso 3 también captura **cardinalidad** (valor único / múltiples valores).
Junto con el `obligatorio` que ya venía del paso 2, forma exactamente las cuatro
combinaciones del `occurrenceType` de Document AI. Verificado contra su discovery
document (`GoogleCloudDocumentaiV1beta3DocumentSchemaEntityTypeProperty`):

| Obligatorio | Cardinalidad | `occurrenceType` |
|---|---|---|
| sí | único | `REQUIRED_ONCE` |
| sí | múltiple | `REQUIRED_MULTIPLE` |
| no | único | `OPTIONAL_ONCE` |
| no | múltiple | `OPTIONAL_MULTIPLE` |

Es la correspondencia más limpia que existe hoy entre el wizard y el motor: dos
controles de la UI son, juntos, un solo campo del esquema del procesador. Vale
tenerlo presente si algún día se decide que el wizard sea la fuente de verdad y
empuje el esquema por API — ese mapeo ya no habría que inventarlo.

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
