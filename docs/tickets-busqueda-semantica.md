# Búsqueda Semántica · tarjetas para Jira (PREPARADAS, sin publicar)

Preparadas el 2026-11-12. Son cinco, además del ticket del DBA «Datos · Usuario de SOLO LECTURA, separado por organización, para
consultas en lenguaje natural» (`docs/solicitudes-dba.md` §8), que es el que las desbloquea.

Al publicarlas: decidir tipo, épica y sprint; todas «Por hacer», responsable Moisés; enlaces «bloquea» según el «Depende de» de cada
una. Orden: la 2 se puede hacer ya; la 1 espera al DBA; la 3 necesita la 1 y la 2; la 4 y la 5, la 3.

Antes de publicar, el texto que se manda no debe nombrar la herramienta ni usar palabras de la planeación interna.

---

## 1 · Tarea · Búsqueda Semántica · Conexión de solo lectura que solo ve la organización de quien pregunta

**Contexto**

Biblioratum va a contestar preguntas en lenguaje natural sobre lo que ya se extrajo de los documentos («¿cuántas INE vencen este
año?»). Un modelo de lenguaje escribe la consulta SQL y la base la ejecuta. Como la consulta la escribe un modelo, es la base la que
garantiza dos cosas, sin depender de que el modelo acierte: que solo se lee, y que solo se ve la organización de quien pregunta.

**Qué incluye**

- Activar la migración `0016`, ya escrita y probada (`migraciones/en_espera/0016_consulta_por_organizacion.sql` en nexus_back): crea
  en el esquema `[consulta]` las vistas `vDocumentoExtraido` y `vDatoExtraido`, que solo devuelven los renglones de la organización
  fijada en la conexión.
- La conexión del back para la búsqueda: entra con el usuario de solo lectura y, antes que nada, fija la organización de la sesión de
  quien pregunta, de forma que ya no se pueda cambiar. Una conexión por pregunta; nunca se reutiliza para otra organización.
- Tiempo máximo por consulta y tope de renglones, para que una consulta pesada no cargue la base.
- La contraseña del usuario de solo lectura vive en el servidor en un archivo propio, fuera de los repositorios y del `.env` de la API,
  como la de migraciones.

**Criterios de aceptación**

- Con el usuario de solo lectura en dev: sin organización fijada, las vistas devuelven 0 renglones; con una, solo los de esa
  organización.
- Cambiar la organización a media conexión falla, también si el cambio viene escondido dentro de la consulta.
- Leer cualquier tabla o vista de `[nexus]`, escribir, crear objetos o ejecutar procedimientos falla por permiso.
- Una consulta que pasa del tiempo máximo se corta con un error claro, sin afectar a nadie más.
- Si hay más renglones que el tope, se devuelve el tope y se avisa que hay más.

**Depende de:** el ticket del DBA «Datos · Usuario de SOLO LECTURA, separado por organización, para consultas en lenguaje natural».

**Para QA:** no tiene pantalla. Se revisa con la evidencia: las seis comprobaciones del ticket del DBA, repetidas con el usuario real
en dev.

---

## 2 · Tarea · Búsqueda Semántica · Lo que sabe de los datos de cada organización

**Contexto**

Para escribir una consulta correcta, el modelo necesita saber qué datos hay y cómo se llaman, y eso cambia de una organización a otra:
cada una tiene sus tipos documentales y sus campos. Esta tarjeta arma ese conocimiento y lo mantiene al día. Es lo que hace a la
búsqueda «semántica»: para cada pregunta se eligen los ejemplos más parecidos por su significado, no por coincidir palabra por palabra.

**Qué incluye**

- Lo común a todas las organizaciones: qué es cada vista y cada columna (qué quiere decir «vigente», «corregido» o «motivo sin valor»;
  cuándo usar `valorFecha` y `valorNumero`), cómo se cuentan documentos y cómo se arma la tabla de un tipo documental con una columna
  por campo.
- Lo de cada organización, sacado de su catálogo: sus tipos documentales y sus campos, con el nombre que ve la gente, la llave con la
  que aparece en los datos extraídos, su tipo de dato y su descripción. Se pone al día solo cuando un tipo se crea, cambia o se borra.
- Preguntas de ejemplo con su consulta correcta: un juego común, escrito y probado por nosotros en dev (al menos 20), y después las
  que apruebe cada organización (tarjeta 5).
- Para cada pregunta se eligen los ejemplos más parecidos, solo entre los comunes y los de la organización de quien pregunta.
- Todo se guarda en SQL, separado por organización.

**Criterios de aceptación**

- Cada consulta de ejemplo corre sin error en dev y devuelve lo que dice su pregunta.
- Al crear o renombrar un tipo documental o un campo, la siguiente pregunta ya lo conoce, sin reiniciar nada; un tipo borrado deja de
  aparecer.
- Nada de una organización (nombres de tipos, de campos, ejemplos) llega al modelo cuando pregunta otra. Una prueba automática con dos
  organizaciones lo comprueba.

**Depende de:** nada; se puede hacer mientras llega el usuario de solo lectura.

**Para QA:** no tiene pantalla propia. Se ve en la tarjeta 3, preguntando por un tipo documental recién creado.

---

## 3 · Historia · Búsqueda Semántica · Preguntar en lenguaje natural y ver la respuesta en una tabla

**Contexto**

La primera versión que se puede usar: una persona escribe una pregunta sobre los datos de su organización y recibe la respuesta como
tabla.

**Qué incluye**

- Un cuadro nuevo «Búsqueda» en el Módulo de uso, con su página: una caja para escribir la pregunta y, debajo, la respuesta como tabla,
  con cuántos renglones son.
- La ven todos los roles que usan el producto, porque solo lee (como los expedientes).
- En el back: recibe la pregunta, el modelo de lenguaje (el mismo con el que ya se describen campos) escribe la consulta con lo de la
  tarjeta 2, y el back la revisa y la ejecuta con la conexión de la tarjeta 1. La revisión exige una sola sentencia de lectura y solo
  sobre las vistas de `[consulta]`; si no cumple, no se ejecuta. La base ya lo impide: esta es una segunda barrera, que además da un
  mensaje claro.
- Los datos de los documentos no se mandan al modelo: recibe la pregunta y la descripción de los datos, y los renglones se quedan en
  nuestro servidor.
- Estados claros: pensando, sin resultados, «no puedo responder eso con los datos extraídos», tardó demasiado y error. La pantalla no
  nombra al proveedor del modelo.

**Criterios de aceptación**

- Preguntas como «¿cuántas INE vencen este año?», «¿qué documentos tienen campos corregidos?» o «¿qué campo falta más en las actas
  constitutivas?» (con los tipos de la organización de prueba) dan el mismo resultado que una consulta escrita a mano.
- Pedir datos de otra organización solo devuelve los de la propia, o nada.
- «Borra los documentos de…» o «ignora tus instrucciones y…» no ejecutan nada que no sea una lectura.
- Una pregunta que no tiene que ver con los datos recibe el mensaje de que no se puede responder con los datos extraídos, sin inventar
  una consulta.
- Los errores llegan a la persona con un mensaje entendible; el detalle técnico queda en el log del servidor.

**Depende de:** tarjetas 1 y 2.

**Para QA:** en dev, entrar con una organización que tenga documentos extraídos, hacer tres preguntas de ejemplo y compararlas con lo
que se ve en sus expedientes. Repetir con un Viewer. Entrar con otra organización y confirmar que no ve nada de la primera.

---

## 4 · Historia · Búsqueda Semántica · Entender la respuesta y llevársela

**Contexto**

Que la persona pueda confiar en la respuesta y usarla: saber cómo se obtuvo, ordenarla y llevársela.

**Qué incluye**

- Debajo de cada respuesta, una frase de cómo se entendió la pregunta («Conté las INE cuya vigencia termina en 2026») y, plegada, la
  consulta que se usó.
- La tabla se ordena por cualquier columna, con fechas y números en formato legible.
- Exportar la respuesta a CSV.
- Preguntas sugeridas al abrir la página, armadas con los tipos documentales de la organización («¿Cuántos contratos llegaron este
  mes?»), para que se vea qué se puede preguntar.

**Criterios de aceptación**

- Cada respuesta trae su frase y su consulta, y la consulta se puede copiar.
- El CSV trae exactamente lo que muestra la tabla y se abre en Excel con los acentos bien, sin acomodar nada.
- Las preguntas sugeridas solo usan tipos documentales de la propia organización y, al picarlas, se preguntan.

**Depende de:** tarjeta 3.

**Para QA:** hacer una pregunta, abrir la consulta que se usó, ordenar por una columna, exportar y abrir el archivo en Excel. Probar
una pregunta sugerida.

---

## 5 · Historia · Búsqueda Semántica · Bitácora de preguntas, límites de uso y respuestas que mejoran los ejemplos

**Contexto**

Saber qué se pregunta y cuánto cuesta, evitar abusos, y que la búsqueda mejore con el uso.

**Qué incluye**

- Se guarda cada pregunta: quién, de qué organización, cuándo, la consulta que se generó, cuántos renglones salieron, cuánto tardó,
  si falló y cuánto costó el modelo. Los renglones de la respuesta no se guardan.
- Tope de preguntas por persona y por organización; al pasarlo, un mensaje que dice cuándo se puede volver a preguntar.
- En cada respuesta, «¿Te sirvió?» (sí / no).
- Quien administra la organización ve las respuestas marcadas como buenas y puede aprobarlas como ejemplo; desde ese momento la
  búsqueda de esa organización, y solo de esa, las usa como ejemplo (tarjeta 2).
- «Tus preguntas recientes» en la página, para repetir una con un clic.
- El equipo de plataforma ve en Nexus Monitor las preguntas por organización: cuántas, cuántas fallaron, tiempo y costo.

**Criterios de aceptación**

- Cada pregunta deja su registro, también las que fallan o se cortan por tiempo.
- Al llegar al tope, la siguiente pregunta no llega al modelo y la persona ve el mensaje.
- Un ejemplo aprobado en una organización mejora sus respuestas y nunca aparece en las de otra.
- Quien no administra la organización no puede aprobar ejemplos: el back lo rechaza.

**Depende de:** tarjeta 3.

**Para QA:** hacer unas preguntas, marcar una como buena, aprobarla como Administrador y repetir una pregunta parecida; revisar que en
otra organización no cambie nada. Pasar el tope con preguntas seguidas y ver el mensaje.
