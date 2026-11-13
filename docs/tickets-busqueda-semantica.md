# Búsqueda Semántica · historias para Jira (PREPARADAS, sin publicar)

Preparadas el 2026-11-12; siete historias desde el 13 de noviembre de 2026. Este archivo y el Excel para el PM (`Busqueda-Semantica-historias.xlsx`, en esta misma carpeta) salen del mismo texto.

Biblioratum contestará preguntas en lenguaje natural sobre los datos ya extraídos de los documentos de cada organización («¿cuántas INE vencen este año?»). Un modelo de lenguaje escribe la consulta y la base la ejecuta con un usuario de solo lectura que solo ve la organización de quien pregunta. Los datos de los documentos no se mandan al modelo.

Antes de la historia 1 va el ticket del DBA «Datos · Usuario de SOLO LECTURA, separado por organización, para consultas en lenguaje natural», preparado aparte. Está en `docs/solicitudes-dba.md` §8.

Orden sugerido: la 2 puede empezar ya; la 1, cuando el DBA entregue su ticket; la 3, con la 1 y la 2 terminadas; la 4, la 5, la 6 y la 7, después de la 3 (la 7 también necesita la 4). Todas son de tipo Historia.

Al publicarlas: decidir épica y sprint; todas «Por hacer», responsable Moisés; enlaces «bloquea» según el «Depende de» de cada una. Antes de publicar, el texto que se manda no debe nombrar la herramienta ni usar palabras de la planeación interna.

---

## 1 · Historia · Búsqueda Semántica · Conexión de solo lectura que solo ve la organización de quien pregunta

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

## 2 · Historia · Búsqueda Semántica · Lo que sabe de los datos de cada organización

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

## 3 · Historia · Búsqueda Semántica · Preguntar en lenguaje natural y ver la respuesta en una tabla

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

## 4 · Historia · Búsqueda Semántica · Entender la respuesta y llevársela

**Historia de usuario:** Como persona que pregunta, quiero ver cómo se obtuvo la respuesta, ordenarla y descargarla, para confiar en ella y usarla en mi trabajo.

**Contexto**

Que la persona pueda confiar en la respuesta y usarla: saber cómo se obtuvo, ordenarla y llevársela.

**Qué incluye**

- Debajo de cada respuesta, una frase de cómo se entendió la pregunta («Conté las INE cuya vigencia termina en 2026») y, plegada, la consulta que se usó.
- La tabla se ordena por cualquier columna, con fechas y números en formato legible.
- Exportar la respuesta a CSV.
- Preguntas sugeridas al abrir la página, armadas con los tipos documentales de la organización («¿Cuántos contratos llegaron este mes?»), para que se vea qué se puede preguntar.

**Criterios de aceptación**

- Cada respuesta trae su frase y su consulta, y la consulta se puede copiar.
- El CSV trae exactamente lo que muestra la tabla y se abre en Excel con los acentos bien, sin acomodar nada.
- Las preguntas sugeridas solo usan tipos documentales de la propia organización y, al picarlas, se preguntan.

**Depende de:** Historia 3.

**Para QA:** Hacer una pregunta, abrir la consulta que se usó, ordenar por una columna, exportar y abrir el archivo en Excel. Probar una pregunta sugerida.

---

## 5 · Historia · Búsqueda Semántica · Bitácora de preguntas, límites de uso y respuestas que mejoran los ejemplos

**Historia de usuario:** Como Administrador de una organización, quiero que las buenas respuestas se vuelvan ejemplos y que el uso tenga límites, para que la búsqueda mejore con el uso y sin sorpresas de costo.

**Contexto**

Saber qué se pregunta y cuánto cuesta, evitar abusos, y que la búsqueda mejore con el uso.

**Qué incluye**

- Se guarda cada pregunta: quién, de qué organización, cuándo, la consulta que se generó, cuántos renglones salieron, cuánto tardó, si falló y cuánto costó el modelo. Los renglones de la respuesta no se guardan.
- Tope de preguntas por persona y por organización; al pasarlo, un mensaje que dice cuándo se puede volver a preguntar.
- En cada respuesta, «¿Te sirvió?» (sí / no).
- Quien administra la organización ve las respuestas marcadas como buenas y puede aprobarlas como ejemplo; desde ese momento la búsqueda de esa organización, y solo de esa, las usa como ejemplo (historia 2).
- «Tus preguntas recientes» en la página, para repetir una con un clic.
- El equipo de plataforma ve en Nexus Monitor las preguntas por organización: cuántas, cuántas fallaron, tiempo y costo.

**Criterios de aceptación**

- Cada pregunta deja su registro, también las que fallan o se cortan por tiempo.
- Al llegar al tope, la siguiente pregunta no llega al modelo y la persona ve el mensaje.
- Un ejemplo aprobado en una organización mejora sus respuestas y nunca aparece en las de otra.
- Quien no administra la organización no puede aprobar ejemplos: el back lo rechaza.

**Depende de:** Historias 2 y 3.

**Para QA:** Hacer unas preguntas, marcar una como buena, aprobarla como Administrador y repetir una pregunta parecida; revisar que en otra organización no cambie nada. Pasar el tope con preguntas seguidas y ver el mensaje.

---

## 6 · Historia · Búsqueda Semántica · Ver la respuesta como gráfica

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

## 7 · Historia · Búsqueda Semántica · Seguir preguntando sobre la respuesta

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
