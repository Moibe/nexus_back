-- 0012 · Se borran las tablas de los expedientes virtuales (Moibe, 2026-11-05).
--
-- Decisión de producto: «voy a quitar los expedientes virtuales, ahora podremos meter el expediente completo a la ejecución de un proceso». El
-- expediente virtual (migración 0004) era un subconjunto de los documentos de UN expediente, instancia de un TIPO de expediente virtual (un nombre
-- y un esquema de tipos documentales), y servía para elegir qué documentos alimentaban a un proceso. Un proceso ya no corre sobre un virtual sino
-- sobre el expediente completo, y lo que se dibuja en su lienzo (los tipos documentales) es la declaración de las entradas que necesita.
-- La aplicación dejó de usar estas tablas en el mismo cambio (la 0011 ya dejó nulas las columnas de virtual de las ejecuciones) y esta
-- migración las borra. Se hizo APARTE de la 0011 a propósito: la 0011 conserva todo lo que hay; esta es la que no tiene vuelta atrás.
--
-- Qué se borra:
--   1. La liga de cada proceso con su tipo (`processes.virtualCaseTypePublicId`, su llave foránea y su índice, de la 0005): un proceso ya no se
--      limita a un tipo. El proceso mismo (su dibujo, su nombre, su revisión) NO se toca.
--   2. `virtualCaseDocuments` (los documentos de cada virtual), `virtualCases` (los virtuales), `virtualCaseTypeDocumentTypes` (el esquema de
--      cada tipo) y `virtualCaseTypes` (los tipos de expediente virtual). Con ellas se van sus llaves, sus índices y la llave de organización
--      que les puso la 0006.
--
-- Qué NO se borra: los expedientes, sus documentos y la pertenencia de cada documento a su expediente (`cases`, `caseDocuments`, `documents`), las
-- clasificaciones y los datos extraídos, los procesos y el historial de ejecuciones. Las ejecuciones viejas siguen diciendo sobre qué virtual se
-- corrieron: es una FOTO (`processRuns.virtualCase*`, sin llaves foráneas desde la 0009), así que no depende de estas tablas.
--
-- ⚠ NO TIENE VUELTA ATRÁS: los expedientes virtuales ya creados, los tipos de expediente virtual y qué tipos documentales traía cada uno se
-- pierden. Donde haya datos reales de virtuales hay que respaldar la base antes de aplicarla.
--
-- Se borra en el orden que piden las llaves foráneas: primero lo que apunta, después lo apuntado. Cada migración corre en UNA transacción: si algo
-- falla, no queda nada a medias.

IF SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Falta el esquema nexus: lo crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
GO

-- ── 1 · Los procesos dejan de ligarse a un tipo ────────────────────────────────────────────────────────────────────────────
ALTER TABLE [nexus].[processes] DROP CONSTRAINT [FK_processes_virtualCaseType];
GO

DROP INDEX [IX_processes_virtualCaseType] ON [nexus].[processes];
GO

ALTER TABLE [nexus].[processes] DROP COLUMN [virtualCaseTypePublicId];
GO

-- ── 2 · Las tablas de los virtuales, de la que apunta a la apuntada ────────────────────────────────────────────────────────
-- Los documentos de cada virtual apuntan al virtual y a la pertenencia del documento en su expediente.
DROP TABLE [nexus].[virtualCaseDocuments];
GO

-- Los virtuales apuntan a su expediente y a su tipo.
DROP TABLE [nexus].[virtualCases];
GO

-- El esquema de un tipo apunta al tipo y a los tipos documentales.
DROP TABLE [nexus].[virtualCaseTypeDocumentTypes];
GO

-- Los tipos de expediente virtual, ya sin nada que los apunte.
DROP TABLE [nexus].[virtualCaseTypes];
GO
