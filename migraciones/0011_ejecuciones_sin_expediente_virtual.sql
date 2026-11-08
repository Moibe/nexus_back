-- 0011 · Las ejecuciones ya no se corren sobre un expediente virtual (Moibe, 2026-11-05: quitar los expedientes virtuales).
--
-- Decisión de producto: «voy a quitar los expedientes virtuales, ahora podremos meter el expediente completo a la ejecución de un proceso». Una
-- ejecución (0009) ya no junta UN expediente virtual con UN proceso: junta el EXPEDIENTE con el proceso. Lo que se dibuja en el lienzo del proceso
-- (los tipos documentales) es lo que declara qué documentos necesita, y los toma del expediente completo.
--
-- Qué cambia en `nexus.processRuns` (el historial de ejecuciones):
--
-- 1. `virtualCaseId` y `virtualCaseName` pasan a admitir NULL: una ejecución nueva no tiene virtual y deja los tres campos de virtual
--    (`virtualCaseId`, `virtualCaseName`, `virtualCaseTypeName`) en NULL. Las ejecuciones que ya existen CONSERVAN los suyos: el historial es una
--    foto inmutable (0009, HU-59 CA-5) y las viejas siguen diciendo sobre qué expediente virtual se corrieron. Nada se reescribe ni se borra.
-- 2. Se quita `IX_processRuns_virtualCase` y NO se vuelve a crear: servía para «lo que se corrió sobre este virtual» (organización, `virtualCaseId` y
--    corrida), y el filtro por virtual desaparece de la aplicación. Con las ejecuciones nuevas sin virtual solo acumularía entradas nulas y ninguna
--    consulta lo usaría. Se quita PRIMERO, antes de tocar las columnas que lo forman, para que el cambio de nulabilidad ya no pase por un índice que va
--    a desaparecer. (Se comprobó que SQL Server 2022 acepta el cambio de nulabilidad también con el índice puesto: quitarlo primero es por limpieza, no porque
--    haga falta.)
-- 3. `CK_processRuns_virtual`: el virtual de una corrida está completo o no está. `virtualCaseId` y `virtualCaseName` son AMBOS nulos o AMBOS con valor,
--    y el nombre del tipo del virtual (`virtualCaseTypeName`) solo puede existir si hay virtual. Así nunca queda una foto a medias: ni un id sin nombre,
--    ni un tipo flotando sin virtual. Las filas de antes cumplen la segunda forma (las dos columnas traían valor desde la 0009), así que la regla
--    se valida sobre lo que ya hay.
--
-- Qué NO cambia: `caseId` y `caseName` (el expediente, que ahora es sobre lo que se corre de verdad) ni `processRunFinalValues`. Las tablas de los
-- expedientes virtuales y la columna con que un proceso se ligaba a su tipo no se tocan aquí: se borran aparte, en la 0012, que es la que no tiene
-- vuelta atrás.

IF SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Falta el esquema nexus: lo crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
GO

-- ── 1 · Fuera el índice por virtual (antes de tocar la columna que usa) ───────────────────────────────────────────────────
DROP INDEX [IX_processRuns_virtualCase] ON [nexus].[processRuns];
GO

-- ── 2 · Una ejecución nueva no tiene virtual: las dos columnas admiten NULL (las filas viejas conservan su valor) ──────────
ALTER TABLE [nexus].[processRuns] ALTER COLUMN [virtualCaseId] varchar(63) NULL;
GO

ALTER TABLE [nexus].[processRuns] ALTER COLUMN [virtualCaseName] nvarchar(200) NULL;
GO

-- ── 3 · El virtual está completo o no está ────────────────────────────────────────────────────────────────────────────────
ALTER TABLE [nexus].[processRuns] ADD CONSTRAINT [CK_processRuns_virtual] CHECK (
    ([virtualCaseId] IS NULL AND [virtualCaseName] IS NULL AND [virtualCaseTypeName] IS NULL)
    OR ([virtualCaseId] IS NOT NULL AND [virtualCaseName] IS NOT NULL)
);
GO
