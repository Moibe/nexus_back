# Búsqueda Semántica · historias en Jira

Preparadas el 2026-11-12; siete historias desde el 13 de noviembre de 2026. **Publicadas en Jira el 2026-11-17 como NEX-605 a NEX-611**; la 8 (NEX-612) se sumó el 2026-11-20 con los casos de uso del expediente que pidió Producto (ver `casos-de-uso-expediente.md`), y ese día se ampliaron la 4 y la 5. **La 9 (los valores finales de las ejecuciones) y la 10 (que la búsqueda conozca los procesos) se prepararon el 2026-11-26 y están SIN PUBLICAR**: salieron al construir la 8, y el trabajo ya está hecho en dev. Todas van bajo la épica NEX-597 («EPICA 14: BUSQUEDA SEMANTICA»), en NEX Sprint 7, «Por hacer» y con Moisés de responsable. El ticket del DBA es **NEX-604** (Tarea, asignada a Charlie, en el mismo sprint y la misma épica). Los enlaces «bloquea» siguen el «Depende de» de cada una. Este archivo y el Excel para el PM (`Busqueda-Semantica-historias.xlsx`, en esta misma carpeta) salen del mismo texto.

Biblioratum contestará preguntas en lenguaje natural sobre los datos ya extraídos de los documentos de cada organización («¿cuántas INE vencen este año?»). Un modelo de lenguaje escribe la consulta y la base la ejecuta con un usuario de solo lectura que solo ve la organización de quien pregunta. Los datos de los documentos no se mandan al modelo.

Antes de la historia 1 va el ticket del DBA «Datos · Usuario de SOLO LECTURA, separado por organización, para consultas en lenguaje natural», preparado aparte. Está en `docs/solicitudes-dba.md` §8 y en Jira es NEX-604.

Orden sugerido: la 2 puede empezar ya; la 1, cuando el DBA entregue su ticket; la 3, con la 1 y la 2 terminadas; la 4, la 5, la 6 y la 7, después de la 3 (la 7 también necesita la 4); la 8, con la 1 y la 3; la 9, después de la 8; y la 10, con la 2 y la 9. Todas son de tipo Historia.

En la épica conviven con las HU-094 a HU-099 de César (NEX-598 a NEX-603), que son otra cosa: buscar fragmentos de los documentos con embeddings. No se fusionaron. El informador quedó Moisés porque el token no puede fijar otro (solo César lo cambiaba, y ya no está en la empresa). NEX-606 (la 2) ya está en LISTO y no se tocó. El texto que va a Jira no nombra la herramienta ni usa palabras de la planeación interna.

---

## 1 · NEX-605 · Historia · Búsqueda Semántica · Conexión de solo lectura que solo ve la organización de quien pregunta

**Historia de usuario:** Como Administrador de una organización, quiero que la búsqueda solo pueda leer los datos de mi organización, sin poder modificarlos ni ver los de otras, para que mi equipo pregunte con la seguridad de que la información está aislada.

**Contexto**

Biblioratum va a contestar preguntas en lenguaje natural sobre lo que ya se extrajo de los documentos («¿cuántas INE vencen este año?»). Un modelo de lenguaje escribe la consulta SQL y la base la ejecuta. Como la consulta la escribe un modelo, es la base la que garantiza dos cosas, sin depender de que el modelo acierte: que solo se lee, y que solo se ve la organización de quien pregunta.

**Qué incluye**

- Activar la migración 0016, ya escrita y probada: crea en el esquema [consulta] las vistas vDocumentoExtraido y vDatoExtraido, que solo devuelven los renglones de la organización fijada en la conexión.
- La conexión del back para la búsqueda: entra con el usuario de solo lectura y, antes que nada, fija la organización de la sesión de quien pregunta, de forma que ya no se pueda cambiar. Una conexión por pregunta; nunca se reutiliza para otra organización.
- Tiempo máximo por consulta y tope de renglones, para que una consulta pesada no cargue la base.
- La contraseña del usuario de solo lectura vive en el servidor en un archivo propio, fuera de los repositorios y del .env de la API, como la de migraciones.

**Criterios de aceptación**

- Con el usuario de solo lectura en dev: sin organización fijada, las vistas devuelven 0 renglones; con una, solo los de esa organización.
- Cambiar la organización a media conexión falla, también si el cambio viene escondido dentro de la consulta.
- Leer cualquier tabla o vista de [nexus], escribir, crear objetos o ejecutar procedimientos falla por permiso.
- Una consulta que pasa del tiempo máximo se corta con un error claro, sin afectar a nadie más.
- Si hay más renglones que el tope, se devuelve el tope y se avisa que hay más.

**Depende de:** Ticket del DBA «Datos · Usuario de SOLO LECTURA, separado por organización, para consultas en lenguaje natural».

**Para QA:** No tiene pantalla. Se revisa con la evidencia: las seis comprobaciones del ticket del DBA, repetidas con el usuario real en dev.

---

## 2 · NEX-606 · Historia · Búsqueda Semántica · Lo que sabe de los datos de cada organización

**Historia de usuario:** Como persona que pregunta, quiero que la búsqueda conozca los tipos documentales y los campos de mi organización, para preguntar con mis propias palabras y recibir la respuesta correcta.

**Contexto**

Para escribir una consulta correcta, el modelo necesita saber qué datos hay y cómo se llaman, y eso cambia de una organización a otra: cada una tiene sus tipos documentales y sus campos. Esta historia arma ese conocimiento y lo mantiene al día. Es lo que hace a la búsqueda «semántica»: para cada pregunta se eligen los ejemplos más parecidos por su significado, no por coincidir palabra por palabra.

**Qué incluye**

- Lo común a todas las organizaciones: qué es cada vista y cada columna (qué quiere decir «vigente», «corregido» o «motivo sin valor»; cuándo usar valorFecha y valorNumero), cómo se cuentan documentos y cómo se arma la tabla de un tipo documental con una columna por campo.
- Lo de cada organización, sacado de su catálogo: sus tipos documentales y sus campos, con el nombre que ve la gente, la llave con la que aparece en los datos extraídos, su tipo de dato y su descripción. Se pone al día solo cuando un tipo se crea, cambia o se borra.
- Preguntas de ejemplo con su consulta correcta: un juego común, escrito y probado por nosotros en dev (al menos 20), y después las que apruebe cada organización (historia 5).
- Para cada pregunta se eligen los ejemplos más parecidos, solo entre los comunes y los de la organización de quien pregunta.
- Todo se guarda en SQL, separado por organización.

**Criterios de aceptación**

- Cada consulta de ejemplo corre sin error en dev y devuelve lo que dice su pregunta.
- Al crear o renombrar un tipo documental o un campo, la siguiente pregunta ya lo conoce, sin reiniciar nada; un tipo borrado deja de aparecer.
- Nada de una organización (nombres de tipos, de campos, ejemplos) llega al modelo cuando pregunta otra. Una prueba automática con dos organizaciones lo comprueba.

**Depende de:** Nada; se puede empezar ya.

**Para QA:** No tiene pantalla propia. Se ve en la historia 3, preguntando por un tipo documental recién creado.

---

## 3 · NEX-607 · Historia · Búsqueda Semántica · Preguntar en lenguaje natural y ver la respuesta en una tabla

**Historia de usuario:** Como persona de una organización, quiero escribir una pregunta sobre los datos extraídos de mis documentos y ver la respuesta en una tabla, para obtener información sin saber SQL ni pedírsela a nadie.

**Contexto**

La primera versión que se puede usar: una persona escribe una pregunta sobre los datos de su organización y recibe la respuesta como tabla.

**Qué incluye**

- Un cuadro nuevo «Búsqueda» en el Módulo de uso, con su página: una caja para escribir la pregunta y, debajo, la respuesta como tabla, con cuántos renglones son.
- La ven todos los roles que usan el producto, porque solo lee (como los expedientes).
- En el back: recibe la pregunta, el modelo de lenguaje (el mismo con el que ya se describen campos) escribe la consulta con lo de la historia 2, y el back la revisa y la ejecuta con la conexión de la historia 1. La revisión exige una sola sentencia de lectura y solo sobre las vistas de [consulta]; si no cumple, no se ejecuta. La base ya lo impide: esta es una segunda barrera, que además da un mensaje claro.
- Los datos de los documentos no se mandan al modelo: recibe la pregunta y la descripción de los datos, y los renglones se quedan en nuestro servidor.
- Estados claros: pensando, sin resultados, «no puedo responder eso con los datos extraídos», tardó demasiado y error. La pantalla no nombra al proveedor del modelo.

**Criterios de aceptación**

- Preguntas como «¿cuántas INE vencen este año?», «¿qué documentos tienen campos corregidos?» o «¿qué campo falta más en las actas constitutivas?» (con los tipos de la organización de prueba) dan el mismo resultado que una consulta escrita a mano.
- Pedir datos de otra organización solo devuelve los de la propia, o nada.
- «Borra los documentos de…» o «ignora tus instrucciones y…» no ejecutan nada que no sea una lectura.
- Una pregunta que no tiene que ver con los datos recibe el mensaje de que no se puede responder con los datos extraídos, sin inventar una consulta.
- Los errores llegan a la persona con un mensaje entendible; el detalle técnico queda en el log del servidor.

**Depende de:** Historias 1 y 2.

**Para QA:** En dev, entrar con una organización que tenga documentos extraídos, hacer tres preguntas de ejemplo y compararlas con lo que se ve en sus expedientes. Repetir con un Viewer. Entrar con otra organización y confirmar que no ve nada de la primera.

---

## 4 · NEX-608 · Historia · Búsqueda Semántica · Entender la respuesta y llevársela

**Historia de usuario:** Como persona que pregunta, quiero ver cómo se obtuvo la respuesta, ordenarla y descargarla, para confiar en ella y usarla en mi trabajo.

**Contexto**

Que la persona pueda confiar en la respuesta y usarla: saber cómo se obtuvo, ordenarla y llevársela.

**Qué incluye**

- Debajo de cada respuesta, una frase de cómo se entendió la pregunta («Conté las INE cuya vigencia termina en 2026») y, plegada, la consulta que se usó.
- La tabla se ordena por cualquier columna, con fechas y números en formato legible.
- Exportar la respuesta a CSV.
- Preguntas sugeridas al abrir la página, armadas con los tipos documentales de la organización («¿Cuántos contratos llegaron este mes?»), para que se vea qué se puede preguntar.
- Cada respuesta dice de dónde sale: un renglón que viene de un documento abre ese documento en la página de donde salió el dato, y uno que viene de la ejecución de un proceso abre esa ejecución en su expediente.
- Si la consulta no devuelve nada, la respuesta dice que no hay información; nunca se completa con datos que no salieron de la base.

**Criterios de aceptación**

- Cada respuesta trae su frase y su consulta, y la consulta se puede copiar.
- Desde un renglón con documento y página se abre el documento en esa página; desde uno de una ejecución, esa ejecución.
- El CSV trae exactamente lo que muestra la tabla y se abre en Excel con los acentos bien, sin acomodar nada.
- Las preguntas sugeridas solo usan tipos documentales de la propia organización y, al picarlas, se preguntan.

**Depende de:** Historia 3.

**Para QA:** Hacer una pregunta, abrir la consulta que se usó, ordenar por una columna, exportar y abrir el archivo en Excel. Probar una pregunta sugerida.

---

## 5 · NEX-609 · Historia · Búsqueda Semántica · Bitácora de preguntas, límites de uso y respuestas que mejoran los ejemplos

**Historia de usuario:** Como Administrador de una organización, quiero que las buenas respuestas se vuelvan ejemplos y que el uso tenga límites, para que la búsqueda mejore con el uso y sin sorpresas de costo.

**Contexto**

Saber qué se pregunta y cuánto cuesta, evitar abusos, y que la búsqueda mejore con el uso.

**Qué incluye**

- Se guarda cada pregunta: quién, de qué organización, cuándo, la consulta que se generó, cuántos renglones salieron, cuánto tardó, si falló y cuánto costó el modelo. Los renglones de la respuesta no se guardan.
- Tope de preguntas por persona y por organización; al pasarlo, un mensaje que dice cuándo se puede volver a preguntar.
- En cada respuesta, «¿Te sirvió?» (sí / no).
- Quien administra la organización ve las respuestas marcadas como buenas y puede aprobarlas como ejemplo; desde ese momento la búsqueda de esa organización, y solo de esa, las usa como ejemplo (historia 2).
- «Tus preguntas recientes» en la página, para repetir una con un clic.
- La misma pregunta da la misma respuesta: si coincide con un ejemplo aprobado de la organización, o con una pregunta que la organización ya hizo y salió bien, se usa esa misma consulta, sin pedírsela otra vez al modelo. Lo que se genera en cada pregunta es la consulta, no los datos: se contesta siempre sobre lo ya guardado.
- El equipo de plataforma ve en Nexus Monitor las preguntas por organización: cuántas, cuántas fallaron, tiempo y costo.

**Criterios de aceptación**

- Cada pregunta deja su registro, también las que fallan o se cortan por tiempo.
- Hacer dos veces la misma pregunta, o una que es un ejemplo aprobado, usa exactamente la misma consulta y, si los datos no cambiaron, da los mismos renglones; la bitácora dice que se reusó.
- Al llegar al tope, la siguiente pregunta no llega al modelo y la persona ve el mensaje.
- Un ejemplo aprobado en una organización mejora sus respuestas y nunca aparece en las de otra.
- Quien no administra la organización no puede aprobar ejemplos: el back lo rechaza.

**Depende de:** Historias 2 y 3.

**Para QA:** Hacer unas preguntas, marcar una como buena, aprobarla como Administrador y repetir una pregunta parecida; revisar que en otra organización no cambie nada. Pasar el tope con preguntas seguidas y ver el mensaje.

---

## 6 · NEX-610 · Historia · Búsqueda Semántica · Ver la respuesta como gráfica

**Historia de usuario:** Como persona que pregunta, quiero ver la respuesta como gráfica cuando se presta (conteos por tipo, por mes), para entender de un vistazo comparaciones y tendencias.

**Contexto**

Muchas preguntas se responden con conteos o montos por categoría o por fecha, y una gráfica los hace evidentes. La gráfica la arma Biblioratum con los datos de la tabla: el modelo no la dibuja ni escribe código para dibujarla.

**Qué incluye**

- Cuando la respuesta trae una columna de categorías o de fechas y otra de números, se ofrece la gráfica junto a la tabla: barras para categorías, línea para fechas.
- Cambiar entre tabla y gráfica con un clic.
- Con muchas categorías, se muestran las 12 mayores y el resto se junta en «Otras».
- Descargar la gráfica como imagen (PNG).
- Se ve bien en modo claro y oscuro.

**Criterios de aceptación**

- «¿Cuántos documentos llegaron por mes este año?» se ve como línea por mes, y «¿cuántos documentos hay por tipo documental?» como barras.
- Una respuesta que no se presta (un solo número, una lista de nombres) no ofrece gráfica.
- Los números de la gráfica coinciden con los de la tabla.
- La gráfica se arma sin volver a llamar al modelo.

**Depende de:** Historia 3.

**Para QA:** Hacer las dos preguntas de ejemplo, cambiar entre tabla y gráfica, comparar los números con la tabla, descargar la imagen y revisarla en modo oscuro.

---

## 7 · NEX-611 · Historia · Búsqueda Semántica · Seguir preguntando sobre la respuesta

**Historia de usuario:** Como persona que pregunta, quiero hacer preguntas de seguimiento sobre la respuesta anterior («¿y solo las de octubre?»), para afinar lo que busco sin repetir toda la pregunta.

**Contexto**

Una búsqueda pocas veces sale a la primera: la gente afina sobre lo que ya vio («¿y de esas?», «ahora por mes»). Esta historia convierte las preguntas en una conversación.

**Qué incluye**

- La página lleva una conversación: cada pregunta nueva se entiende con las anteriores de esa misma conversación.
- Al modelo solo van las preguntas anteriores y sus consultas; nunca los datos de las respuestas.
- Debajo de cada respuesta, hasta tres preguntas sugeridas para seguir («¿Y por tipo documental?»), escritas sin ver los datos.
- «Nueva conversación» empieza desde cero.

**Criterios de aceptación**

- «¿Cuántas INE llegaron este año?» seguida de «¿y cuántas de esas tienen campos corregidos?» responde sobre las INE de este año, no sobre todos los documentos.
- La frase de cómo se entendió dice qué se tomó de la pregunta anterior («Tomé las INE de este año de tu pregunta anterior»).
- Después de «Nueva conversación», «¿y de esas?» ya no tiene a qué referirse y la búsqueda lo dice.
- Si la persona cambia de organización, la conversación empieza de cero: nunca mezcla organizaciones.

**Depende de:** Historias 3 y 4.

**Para QA:** Hacer una pregunta, dos de seguimiento y luego una nueva conversación; revisar que las de seguimiento partan de la anterior y que la nueva no.

---

## 8 · NEX-612 · Historia · Búsqueda Semántica · Preguntarle al expediente: documentos faltantes, resultados de los procesos y correcciones

**Historia de usuario:** Como persona que revisa expedientes, quiero preguntarle a un expediente qué documentos le faltan, por qué quedó con el resultado que tiene y qué datos se corrigieron a mano, para entenderlo sin abrir documento por documento.

**Contexto**

Las historias 1 y 3 contestan sobre los datos extraídos de los documentos. Producto pide además preguntarle al expediente (documento «Interacción con el expediente: casos de uso esperados», del 18 de noviembre). Casi todo ya se guarda: qué documentos tiene cada expediente, qué tipos documentales exige cada proceso, cada ejecución con el resultado de cada regla, lo que comparó y de qué documento y página salió, y cada corrección a mano con el valor original, quién y cuándo. Falta exponerlo a la búsqueda, con el mismo aislamiento por organización.

**Qué incluye**

- Una migración nueva con vistas de solo lectura en el esquema [consulta], filtradas por la organización de la conexión igual que las de la historia 1: expedientes; documentos de cada expediente; tipos documentales que exige cada proceso; ejecuciones (expediente, proceso, resultado, fecha y quién la corrió); cada regla de cada ejecución, con su resultado y lo que comparó; la evidencia (documento, campo, valor y página); y las correcciones (valor extraído, valor corregido, motivo, quién y cuándo). El permiso del DBA ya cubre cualquier vista de ese esquema: no hace falta otro ticket.
- Que la búsqueda las conozca: qué es cada vista y cada columna, y preguntas de ejemplo probadas en dev para estas cuatro: «¿qué documentos le faltan a este expediente?», «¿por qué quedó en Requiere revisión?», «expedientes de este mes que quedaron en Requiere revisión» y «¿qué datos de este expediente se corrigieron y quién lo hizo?».
- Un cuadro de búsqueda dentro del expediente. Al preguntar ahí, la conexión fija también el expediente, igual que la organización y sin poder cambiarlo, y las vistas solo devuelven lo de ese expediente: «este expediente» no depende de que el modelo escriba bien el filtro.
- Los casos que piden datos de fuera (comparar contra un tabulador del cliente, validar una factura ante el SAT) o el historial de una persona en varios expedientes NO entran aquí: necesitan capacidades nuevas.

**Criterios de aceptación**

- En dev, las cuatro preguntas de ejemplo dan lo mismo que se ve en la pantalla del expediente y en su historial de ejecuciones.
- Desde el cuadro de un expediente, preguntar por otro expediente o por todos devuelve solo lo del expediente abierto.
- Sin expediente fijado (el cuadro del Módulo de uso), las vistas nuevas devuelven lo de toda la organización y nada de otra.
- Un expediente al que todavía no se le ha ejecutado ningún proceso lo dice así, sin inventar un resultado.
- Las vistas nuevas no se pueden leer con ninguna organización fijada distinta de la propia (las mismas comprobaciones de la historia 1).

**Depende de:** Historias 1 y 3.

**Para QA:** En dev, abrir un expediente con documentos y al menos una ejecución, hacer las cuatro preguntas desde su cuadro y compararlas con la pantalla. Corregir un dato a mano y preguntar qué se corrigió. Repetir desde el Módulo de uso y confirmar que ahí contesta sobre toda la organización.

---

## 9 · sin publicar · Historia · Búsqueda Semántica · Preguntar por los valores finales de las ejecuciones

**Historia de usuario:** Como persona que revisa expedientes, quiero preguntar qué entregó cada ejecución de un proceso (sus valores finales), para encontrar expedientes por lo que resolvió el motor de reglas sin abrir ejecución por ejecución.

**Contexto**

La historia 8 lleva a la búsqueda las ejecuciones del motor de reglas: su resultado, cada regla, de qué documento salió cada dato y lo que les faltó. Le falta lo que cada ejecución entrega: sus valores finales, los nodos «Valor final» del proceso (un dato, una lista o el Pasa / No pasa de una regla). Ya se guardan con cada ejecución; falta exponerlos.

**Qué incluye**

- Una migración con una vista de solo lectura en el esquema [consulta] con los valores finales de cada ejecución: su nombre; si es un valor, una lista o un veredicto; de qué clase es el dato; si tiene valor o por qué no (le faltó el dato o no tenía nada conectado, con las mismas palabras de la pantalla); el valor como se enseña; ese valor como número o como fecha cuando lo es; cuántos elementos trae una lista; y de dónde salió. Con el mismo aislamiento de la historia 8: la organización de la conexión y, desde un expediente, solo ese expediente.
- Que la búsqueda la conozca: qué es cada columna, que un valor final se busca por su nombre aunque la persona lo diga con otras palabras, que los números y las fechas se comparan como tales, y preguntas de ejemplo probadas: «¿qué valores finales dio la última ejecución?» y «¿en qué expedientes el valor final Monto pasa de 100,000?».
- Cada renglón abre la ejecución en su expediente, como los de la historia 8.

**Criterios de aceptación**

- Desde un expediente, «¿qué valores finales dio la última ejecución?» da lo mismo que la ventana de esa ejecución en su historial.
- Un valor final sin valor dice por qué («Sin valor (faltó el dato)» o «Sin conectar»), sin inventar uno.
- Comparar un valor final numérico o de fecha («pasa de 100,000», «vence antes de…») lo hace como número o como fecha.
- Cuenta la última ejecución de cada proceso en cada expediente, salvo que se pregunte por el historial.
- Desde un expediente solo trae los suyos; desde el Módulo de uso, los de la organización y nada de otra.

**Depende de:** Historia 8.

**Para QA:** En dev, abrir un expediente con al menos una ejecución que entregue valores finales y preguntar desde su «Preguntar» «¿qué valores finales dio la última ejecución?»; compararlo con la ventana de esa ejecución en el historial. Desde el Módulo de uso, preguntar por el nombre de un valor final y por uno numérico con «mayor a».

---

## 10 · sin publicar · Historia · Búsqueda Semántica · Que la búsqueda conozca los procesos de la organización

**Historia de usuario:** Como persona que pregunta, quiero nombrar un proceso, una palabra de su resultado o un valor final con mis palabras, para no tener que decir qué tipo de cosa es («la Revisión de Completitud» en vez de «el valor final Revisión de Completitud»).

**Contexto**

Lo que la búsqueda sabe de cada organización (historia 2) son sus tipos documentales y sus campos. Desde las historias 8 y 9 también contesta sobre los procesos, sus resultados y sus valores finales, pero no conocía sus nombres: si la pregunta no decía qué era «Revisión de Completitud», la tomaba por el nombre de un proceso y no encontraba nada.

**Qué incluye**

- En cada pregunta, junto con los tipos documentales, la búsqueda lee los procesos de la organización: su nombre, si está activo, las tres palabras de su Resultado (las que escribió la organización o, si alguna quedó vacía, la de siempre), los nombres de sus valores finales (sin nombre, «Valor final #N», como en la pantalla) y los tipos documentales que usa.
- Como la búsqueda contesta sobre el historial de ejecuciones, que es una foto, también los nombres que quedaron ahí y ya no están en el proceso de hoy: cómo se llamaba antes, con qué palabras terminó y cómo se llamaban sus valores finales; y los procesos que ya no existen pero tienen ejecuciones.
- Solo nombres que escribió la organización: nada de los documentos va al modelo, y nunca los procesos de otra organización.
- Como se lee al preguntar, un proceso nuevo o renombrado ya se conoce en la siguiente pregunta; y como cambia lo que sabe la búsqueda, la misma pregunta no reusa una consulta de antes del cambio.

**Criterios de aceptación**

- «¿En qué expedientes la Revisión de Completitud quedó en Pasa?», sin decir que es un valor final, contesta sobre ese valor final.
- Una pregunta con la palabra de un resultado («¿cuántos expedientes quedaron en Certificado?») la busca como resultado aunque no diga que lo es.
- Un valor final que el proceso ya no tiene, pero que está en ejecuciones anteriores, también se encuentra por su nombre.
- Los procesos de una organización nunca llegan al modelo cuando pregunta otra.
- Un proceso nuevo o renombrado se conoce en la siguiente pregunta, sin reiniciar nada.

**Depende de:** Historias 2 y 9.

**Para QA:** En dev, con una organización que tenga procesos ejecutados, preguntar por un valor final y por una palabra de resultado sin decir qué son, y comparar con el historial de ejecuciones. Renombrar un proceso en el motor de reglas y volver a preguntar con el nombre nuevo.
