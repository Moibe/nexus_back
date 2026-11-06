-- 0013 · Se borran los grupos de expedientes (Moibe, 2026-11-06).
--
-- Decisión de producto: «en aras de la simplicidad por el momento también quitaremos grupos, léase que los expedientes no se agruparán en Grupos y no
-- se requiere el menú». El grupo (migración 0010, del 2026-11-03) era el nivel de arriba de los expedientes: un nombre único por organización (áreas,
-- departamentos, trimestres…) al que cada expediente pertenecía, elegido al crearlo. Un expediente ya no vive en ningún grupo: es solo un receptáculo de
-- documentos con su nombre. La aplicación dejó de usar estas tablas en el mismo cambio (las rutas `/grupos` y `PUT /expedientes/{id}/grupo` ya no
-- existen, y crear un expediente no pide ni escribe grupo) y esta migración las borra.
--
-- Qué se borra:
--   1. La pertenencia de cada expediente a su grupo: `cases.caseGroupId`, con su llave foránea compuesta organización+grupo (`FK_cases_caseGroup`) y su
--      índice (`IX_cases_caseGroup`), de la 0010. El expediente mismo (su nombre, su organización, quién lo creó y cuándo) NO se toca.
--   2. La tabla `caseGroups` (los grupos), con sus llaves únicas y sus reglas.
--
-- Qué NO se borra: los expedientes, sus documentos y la pertenencia de cada documento a su expediente (`cases`, `caseDocuments`, `documents`), las
-- clasificaciones y los datos extraídos, los procesos y el historial de ejecuciones. Los procesos nunca se limitaron por grupo y las ejecuciones no lo
-- guardan, así que nada de eso depende de estas tablas.
--
-- ⚠ NO TIENE VUELTA ATRÁS: los grupos ya creados y a qué grupo pertenecía cada expediente se pierden. Donde haya datos reales de grupos hay que
-- respaldar la base antes de aplicarla. (La bitácora de auditoría es de solo agregar: sus renglones `CASE_GROUP_*`, y el `grupoId` y el `grupoNombre`
-- de los `CASE_CREATED` de esos días, se quedan como estaban.)
--
-- Se borra en el orden que piden los objetos: primero lo que apunta (la llave foránea y el índice de `cases`), después la columna que los usaba y, al
-- final, la tabla apuntada. Cada migración corre en UNA transacción: si algo falla, no queda nada a medias.

IF SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Falta el esquema nexus: lo crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
GO

-- ── 1 · Los expedientes dejan de pertenecer a un grupo ─────────────────────────────────────────────────────────────────────
ALTER TABLE [nexus].[cases] DROP CONSTRAINT [FK_cases_caseGroup];
GO

DROP INDEX [IX_cases_caseGroup] ON [nexus].[cases];
GO

ALTER TABLE [nexus].[cases] DROP COLUMN [caseGroupId];
GO

-- ── 2 · La tabla de los grupos, ya sin nada que la apunte ──────────────────────────────────────────────────────────────────
DROP TABLE [nexus].[caseGroups];
GO
