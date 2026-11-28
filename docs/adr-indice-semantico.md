# ADR · Dónde viven los vectores de la búsqueda en el texto de los documentos

**Estado:** aceptada (Moisés, 2026-11-27: «vámonos con SQL Server»).
**Épica:** 14 · Búsqueda semántica, historias HU-094 a HU-099 (NEX-598 a NEX-603). HU-094 la marca como bloqueante de toda la épica.

## Contexto

Las seis historias piden buscar **dentro del texto** de los documentos ya procesados y devolver el **fragmento** con su página, su parecido
(score) y el expediente, sin redactar respuestas. Para eso cada documento se parte en fragmentos y cada fragmento se convierte en un vector
(«embedding»): una lista de números que resume su significado. Buscar es sacar el vector de la pregunta y quedarse con los fragmentos más
parecidos.

Nuestra base es **SQL Server 2022** (nivel de compatibilidad 160 en dev, staging y producción). No tiene tipo `VECTOR`, ni funciones de
distancia, ni índice vectorial: eso llega con SQL Server 2025.

## Opciones

| | A · SQL Server 2022 + comparación en el back | B · Postgres con pgvector | C · Esperar SQL Server 2025 |
|---|---|---|---|
| Infraestructura nueva | Ninguna | Un servidor Postgres, respaldos, monitoreo, credenciales; el DBA (Charlie) | Actualizar el motor de las tres bases |
| Dónde se compara | En memoria del back (numpy), por organización | En la base (`<=>`, índice HNSW) | En la base (`VECTOR_DISTANCE`, índice DiskANN) |
| Aislamiento por organización | Vista de `[consulta]` con la organización fijada en la conexión (como NEX-605) + un espacio en memoria por organización | Esquema o filtro por organización en Postgres; dos bases que mantener en sincronía | Igual que A, dentro de la base |
| Cuándo se puede | Ya | Cuando exista el servidor | Sin fecha |
| Límite | La memoria del back (ver «Números») | Muy alto | Muy alto |

## Decisión

**A.** Los fragmentos y sus vectores viven en SQL Server, junto a todo lo demás, y la comparación se hace en el back con numpy sobre una matriz
en memoria por organización. No se agrega infraestructura y el camino a C queda abierto: el día que la base sea 2025, la columna del vector
cambia de tipo y la comparación pasa a la base; las tablas, la API y la pantalla no cambian.

## Diseño

**La capa OCR** (pendiente desde la 0007). Document AI ya devuelve el texto por línea, con su página y su caja, en cada extracción
(`resultado["ocr"]`), pero no se guardaba. Desde la 0022: `nexus.ocrResults` (uno por extracción) y `nexus.ocrBlocks` (una fila por
línea, con su texto, página y caja normalizada 0–1), y `nexus.entityFacts.ocrBlockId` liga cada dato extraído con la línea de donde salió.

**Fragmentos** (`nexus.documentChunks`). Dos clases:

- `texto`: el texto de cada página, en fragmentos de **512 tokens con 64 de solapamiento** (lo que pide HU-094; configurable por
  organización en `nexus.semanticSearchSettings`). Un fragmento **nunca cruza de página**: así la página que se enseña es exacta y abrir el
  documento cae donde está el pasaje.
- `campos`: uno por página con los **datos extraídos vigentes** («Nombre: ANA LÓPEZ · CURP: …»). Una corrección a mano lo rehace y su
  vector anterior se borra: es el reindexado de HU-094 CA-3.

**El vector.** `gemini-embedding-001` por Vertex AI (la misma cuenta y proyecto que ya usan Document AI y la búsqueda de NEX-609), 768
dimensiones, con tarea `RETRIEVAL_DOCUMENT` para los fragmentos y `RETRIEVAL_QUERY` para la consulta. Se guarda como `float32` en
`varbinary(8000)` (3,072 bytes) junto con la etiqueta del modelo; un vector de otro modelo no se compara y el documento se vuelve a indexar.

**Estado de cada documento** (`nexus.documentIndexes`): `INDEX_PENDING`, `INDEXED` o `INDEX_INCONSISTENT`, con intentos, siguiente
intento, último error y cuándo se avisó. Indexar no bloquea nada: la extracción termina, el documento queda `INDEX_PENDING` y un trabajador del
back lo indexa en segundos. Si Google no responde, se reintenta con espera (máximo 3 intentos) y al tercero se anota una alerta en la
bitácora de auditoría y en el log.

**Aislamiento por organización (HU-095).** La búsqueda NO lee las tablas de `[nexus]`: lee `consulta.vFragmento` con el usuario de solo
lectura (`usrNexusLectura`) y la organización fijada en la conexión con `SESSION_CONTEXT` de solo lectura, el mismo mecanismo de NEX-605. El
filtro lo pone la base, no un `WHERE` que alguien pueda olvidar. En memoria, cada organización tiene su propia matriz: no hay una matriz
compartida que se filtre. Una prueba automática con dos organizaciones lo comprueba.

**Nunca una versión vieja (HU-099).** `consulta.vFragmento` solo devuelve los fragmentos de la **extracción vigente** de cada documento (la
última que no falló). Re-extraer un documento deja fuera de la búsqueda sus fragmentos anteriores en el mismo instante, aunque los nuevos
tarden; el trabajador borra los viejos y escribe los nuevos en una sola transacción. Borrar un documento borra sus fragmentos
(`ON DELETE CASCADE`).

**La consulta (HU-096 a HU-098).** Vector de la consulta (se recuerda por un rato, así paginar u ordenar no lo vuelve a pedir), similitud
coseno contra la matriz de la organización, umbral mínimo (0.65 por omisión, configurable), filtros por tipo documental y fecha de
procesamiento, orden por relevancia o por fecha, todo del lado del servidor. Cada resultado trae el documento, su tipo, la página, el score, el
pasaje con las palabras de la consulta resaltadas, la fecha de procesamiento, el expediente si pertenece a uno y los datos extraídos de esa
página. No se genera texto.

## Números

- **El mínimo de parecido.** HU-096 pedía 0.70. Medido el 2026-11-28 con el modelo real, 8 consultas contra 8 fragmentos (acta, INE, recibo de
  luz, póliza): las 8 acertaron, lo relevante dio de **0.68 a 0.82** y lo que no tiene que ver llegó a **0.61**. Con 0.70, «¿quiénes son los
  accionistas?» (0.679) no habría encontrado el acta; por eso el valor por omisión es **0.65**, configurable por organización.
- **Latencia del vector de la consulta:** ~0.3 s reusando la conexión con Google (~1.2 s abriendo una por llamada, como se hacía).

- **Memoria:** un vector ocupa 3 KB. 10,000 documentos con ~5 fragmentos cada uno son ~150 MB para esa organización. Se cargan la primera vez
  que se busca y se recargan solos cuando cambia el índice.
- **Latencia:** comparar 50,000 vectores de 768 dimensiones con numpy toma decenas de milisegundos; el resto es la llamada a Vertex por el
  vector de la consulta (~100–300 ms). Objetivo de HU-096: p95 ≤ 2 s con 10,000 documentos.
- **Costo:** US$0.15 por millón de tokens. Un documento de 300 páginas (~150,000 tokens) cuesta ~US$0.02 una vez; una consulta, una
  fracción de centavo.

## Límites y cuándo cambiar

Cuando una organización pase de ~200,000 fragmentos, o la memoria del servidor no alcance, toca C (SQL Server 2025 con índice DiskANN) o B. Ese
cambio toca la columna del vector y la función que compara; nada más.

## Lo que queda fuera o por decidir

- **API Key de cliente.** Hoy una API Key solo abre `POST /bandeja/`. Dejar que una llave busque en el texto de TODOS los documentos de la
  organización es darle mucho más poder: la búsqueda se expone a la pantalla (sesión) y el acceso con llave espera esa decisión.
- **«Expediente virtual».** No existe desde la decisión «sin expedientes virtuales»: el enlace es al expediente al que pertenece el documento.
- **Documentos extraídos antes de la 0022.** Su texto no se guardó. Se indexan solo con su fragmento de `campos`; su texto completo entra
  si se vuelven a extraer (cuesta la extracción de nuevo).
- **Privacidad.** El texto de los fragmentos va al servicio de vectores de Google, en el mismo proyecto que ya lee los documentos completos
  (Document AI). No se agrega un proveedor nuevo.
