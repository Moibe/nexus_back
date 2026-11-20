# Interacción con el expediente · cómo encaja con la Búsqueda Semántica

Fuente: `Biblioratum_Casos_de_uso_interaccion_expediente.pdf`, de Alexis Quintero Morales (Product Owner), del 18 de noviembre de 2026. Está en esta misma carpeta.

El documento trae seis preguntas que el usuario debe poder hacerle a un expediente, cinco principios para todas las respuestas y cuatro puntos para definir con Desarrollo. Aquí se cruzan con dos cosas:

- lo que ya guarda la base;
- las historias de Búsqueda Semántica (NEX-605 a NEX-611) y el ticket del DBA (NEX-604), en `tickets-busqueda-semantica.md`.

Revisado contra el código el 2026-11-20, back `dbce676` y front `fc6727a`.

## En una frase

El plan de hoy contesta preguntas sobre **documentos y sus datos extraídos**. Las vistas de la 0015 y de la 0016 no saben de expedientes, ejecuciones ni correcciones. El documento de Alexis pregunta sobre el **expediente y el resultado del motor de reglas**. Casi todo eso ya está guardado, pero falta exponerlo a la búsqueda.

## Los seis casos

| # | Pregunta | ¿Ya se guarda? | Qué falta |
|---|---|---|---|
| 1 | ¿Qué documentos le faltan? | Sí. Lo que exige un proceso son los tipos documentales de su lienzo (`processes.dataJson`). Lo que trae el expediente está en `caseDocuments` y la extracción vigente. Además, cada ejecución ya guarda sus faltantes en `detailJson.faltantes`. | Vistas: documentos del expediente, y requeridos por proceso. La regla que los exige es el proceso, no una regla suelta. |
| 2 | ¿Por qué quedó en «requiere revisión»? | Sí. `processRuns` guarda la palabra del Resultado y `detailJson` guarda cada regla con su estado y lo que le llegó a cada entrada. La evidencia incluye documento, campo, valor y página. | Vistas que abran ese JSON en renglones. Las reglas no tienen nombre de negocio: se llaman «Igual a #2». |
| 3 | ¿Coincide con el tabulador? ¿La factura es válida ante el SAT? | No. No existen los «paquetes de datos del cliente» y validar ante el SAT es una consulta a un servicio externo, no a la base. | Capacidad nueva, fuera de esta épica (Alexis ya la marca como «capacidad objetivo»). |
| 4 | ¿Esta persona tiene banderas en expedientes anteriores? | En parte. Con expediente y ejecuciones en las vistas se puede cruzar por un campo como CURP o RFC. | Definir qué campo identifica a una persona en cada tipo documental, y la retención. Capacidad nueva. |
| 5 | Expedientes del mes que incumplen la regla X | Sí. `processRuns` tiene fecha y caso (si, no, sin), y el JSON tiene cada regla. | Las mismas vistas que en el caso 2. También hay que decidir si cuenta la última ejecución de cada expediente o cualquiera. Mismo problema de nombres de regla. |
| 6 | ¿Qué datos corrigió una persona y quién? | Sí. `fieldCorrections` nunca toca el dato original y guarda valor, motivo, quién y cuándo. Se corrige desde el expediente (`DatosExtraidosModal`). | Una vista de correcciones. Hoy `vDatoExtraido` solo dice `corregido` sí o no. Ver también el **hallazgo** de abajo. |

### Hallazgo: una corrección se deja de aplicar al reprocesar

Una corrección cuelga del dato de UNA corrida (`fieldCorrections.factId`). `vEntityFactCurrent` toma la última corrida del documento. Si se reprocesa un documento que alguien corrigió a mano, el valor vigente vuelve a ser el que extrajo la máquina.

La corrección no se borra, porque es historia, pero deja de aplicarse sin que nadie lo note. Ningún código la pasa a la corrida nueva. Esto contesta en parte el segundo punto de Alexis y hay que decidirlo antes de prometer el caso 6. Propuesta: al reprocesar, si un campo con corrección vigente sale otra vez, la corrección pasa a la corrida nueva y queda registro de que se arrastró.

**Resuelto (NEX-614, 2026-11-20; Moibe: «que la corrección se arrastre al reprocesar»).** Al guardar una corrida nueva, cada corrección vigente pasa a ella, con la misma persona, fecha y motivo. El valor que extrajo la máquina queda aparte (`valorExtraido`), y la corrida guarda en `metadataJson.correcciones` qué se trasladó y de qué extracción. Por campo, la API dice `correccionTrasladadaDe`.

No se trasladan, y se avisa en `correccionesSinTrasladar` con su motivo:
- `campo_ausente`: la extracción nueva ya no trae el campo;
- `fila_distinta`: en lo que se repite, la fila nueva trae otra cosa que la vieja, porque las filas pueden cambiar de lugar;
- `otro_tipo`: se extrajo como otro tipo documental.

Sin migración: `vEntityFactCurrent` y las vistas de la 0015 ya leen la corrección de la corrida vigente.

## Los principios

- **Sale solo del expediente; si no hay información, lo dice.** Ya está en el diseño: la respuesta son los renglones que devuelve la base y ningún dato va al modelo (NEX-607). Cero renglones se contesta con «no hay información».
- **Indica de dónde sale.** Hoy la tabla trae documento y página (`vDatoExtraido`). Falta poder abrir el documento en esa página desde la respuesta, o la ejecución cuando la respuesta sale de una regla (NEX-608).
- **Misma pregunta, misma respuesta.** Los datos sí: se contesta sobre lo guardado, sin volver a extraer. La consulta SQL no: el modelo puede escribirla distinta dos veces. Hay que amarrarla:
  - si la pregunta coincide con un ejemplo aprobado, se usa su SQL tal cual;
  - una pregunta que la organización ya hizo reusa la consulta guardada en la bitácora;
  - el modelo genera con temperatura 0.

  Va en NEX-606 y NEX-609.
- **Lenguaje de negocio.** Es NEX-606: el catálogo de cada organización con el nombre que ve la gente. Falta lo de los nombres de regla (casos 2 y 5).
- **Solo expedientes a los que tiene acceso.** Hoy leer expedientes está abierto a todo el que opera en la organización (`servicios/roles.py`), así que el filtro por organización de la 0016 equivale a «lo que puede ver». Si un día hay acceso por expediente, el filtro de la conexión tiene que crecer.

## Preguntarle A UN expediente

Los casos 1, 2 y 6 dicen «este expediente». Propuesta: además del cuadro de búsqueda del Módulo de uso (NEX-607), poner uno dentro del expediente. La conexión fijaría también el expediente en `SESSION_CONTEXT`, de solo lectura, igual que la organización, y las vistas filtrarían por él. Así «este expediente» no depende de que el modelo escriba bien el filtro.

## Qué cambió en el plan (Jira, 2026-11-20)

Moibe dio el visto bueno («los otros puedes cambiarlos libremente»). NEX-604, el ticket de Charlie, no cambia: su `GRANT SELECT ON SCHEMA::consulta` ya cubre cualquier vista nueva del esquema. NEX-606 ya estaba en LISTO y no se tocó, así que lo que le tocaba se fue a una historia nueva. Lo publicado:

- **NEX-612 (nueva, épica NEX-597):** «Preguntarle al expediente: documentos faltantes, resultados de los procesos y correcciones». Cubre:
  - una migración NUEVA con las vistas de abajo, para no mover la 0016 de NEX-605;
  - su descripción y los ejemplos de los casos 1, 2, 5 y 6;
  - el cuadro de búsqueda dentro del expediente, con el expediente fijado en la conexión.

  La bloquean NEX-605 y NEX-607.
- **NEX-608 (ampliada):** cada renglón abre el documento en su página, o la ejecución. Si no hay información, lo dice.
- **NEX-609 (ampliada):** la misma pregunta reusa la misma consulta, la de un ejemplo aprobado o una que ya salió bien.
- **NEX-613 (nueva, épica NEX-482, motor de reglas):** nombre propio opcional para cada regla.
- **NEX-614 (nueva, épica NEX-501, revisión humana):** una corrección a mano sigue valiendo cuando el documento se vuelve a extraer.

Las cinco están en NEX Sprint 7, «Por hacer», con Moisés de responsable. Los casos 3 y 4 no tienen ticket: son capacidad objetivo, de una épica aparte.

## La propuesta original

1. **Más vistas en `[consulta]`, sin otro ticket para el DBA** (al final van en una migración nueva, no en la 0016). Todas filtradas por organización y, si se fija, por expediente:
   - `vExpediente`: expediente, tipo y fechas;
   - `vDocumentoDeExpediente`: expediente y documento, porque un documento puede estar en varios;
   - `vDocumentoRequerido`: proceso y tipo documental, sacado del lienzo;
   - `vEjecucion`: expediente, proceso, palabra del Resultado, caso, fecha y quién;
   - `vReglaDeEjecucion`: una regla por renglón, con su estado, su texto y sus entradas;
   - `vEvidenciaDeEjecucion`: tipo, documento, campo, valor y página;
   - `vCorreccion`: valor extraído, valor corregido, motivo, quién y cuándo.
2. **NEX-606:** el conocimiento y los ejemplos incluyen las vistas nuevas, con al menos un ejemplo probado para cada uno de los casos 1, 2, 5 y 6.
3. **NEX-607:** cuadro de búsqueda también dentro del expediente, con el expediente fijado en la conexión.
4. **NEX-608:** cada renglón con origen abre el documento en su página, o la ejecución.
5. **NEX-609:** reusar el SQL aprobado o ya usado para la misma pregunta.
6. **Nuevas, fuera de la búsqueda:**
   - nombre de negocio opcional para cada regla del lienzo;
   - que una corrección sobreviva al reprocesar.
7. **Capacidad objetivo, épica aparte:** paquetes de datos del cliente y validación externa (caso 3), e identidad e historial por persona con su retención (caso 4).

## Respuestas a «Para definir con Desarrollo»

1. **¿Sobre lo guardado o generado en cada consulta?** Sobre lo guardado: los datos ya extraídos, las correcciones y el historial de ejecuciones, que no cambia. Lo que se genera en cada pregunta es la consulta, no los datos. Para que la misma pregunta dé la misma respuesta, se reusa la consulta aprobada o ya usada.
2. **¿Se sobrescribe al reprocesar? ¿La corrección conserva el original?** No se sobrescribe nada: reprocesar crea una corrida nueva y la anterior queda como historia. La corrección conserva el original, quién, cuándo y el motivo. Pero hoy, al reprocesar, la corrección deja de aplicarse (ver el hallazgo); hay que decidir si se arrastra.
3. **¿Qué se puede con lo que existe?** Los casos 1, 2, 5 y 6 se resuelven con datos que ya se guardan, exponiéndolos a la búsqueda. Los casos 2 y 5 se leen mejor si las reglas tienen nombre. Los casos 3 y 4 requieren capacidades nuevas.
4. **¿Cómo se muestra?** Tabla como base, con una frase que resume y gráfica cuando aplica (NEX-607, NEX-608 y NEX-610). Desde cada renglón se abre el documento en su página o la ejecución de la que sale.
