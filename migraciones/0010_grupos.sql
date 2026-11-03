-- 0010 · Grupos: el nivel de arriba de los expedientes.
--
-- Decisión de producto (Moibe, 2026-11-03): varios expedientes viven juntos en un GRUPO. Cada organización lo llama como quiere (áreas,
-- departamentos, trimestres del año…), así que el nombre genérico es «Grupo». Es UN SOLO nivel: un grupo tiene expedientes y nada más; los
-- expedientes no se anidan dentro de expedientes ni los grupos dentro de grupos. El grupo se crea antes, en el módulo de configuración, y al
-- crear un expediente se elige a cuál pertenece.
--
-- 1. Nace `caseGroups`: un nombre único por organización (la base es CI, así que «Ventas» y «ventas» chocan, que es lo que pide el producto).
-- 2. `cases` gana `caseGroupId`, NULLABLE a propósito: los expedientes que ya existen no tienen grupo y se les asigna después. Que el expediente
--    nuevo SÍ lo traiga lo exige el servicio, no la base: si fuera NOT NULL, esta migración no podría correr sobre expedientes ya creados.
-- 3. La llave foránea es COMPUESTA con la organización: un expediente solo puede ir en un grupo de SU organización, y eso lo cuida la base (no
--    solo el servicio). Con `caseGroupId` en NULL la llave no se revisa (SQL Server no revisa una llave compuesta con una columna nula), que es
--    justo el expediente sin grupo.
-- 4. Sin ON DELETE: un grupo con expedientes no se borra por debajo (el servicio lo explica con un 409 y cuántos tiene).

IF SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Falta el esquema nexus: lo crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
GO

-- ── 1 · Los grupos ─────────────────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[caseGroups] (
    [caseGroupId] bigint IDENTITY(1,1) NOT NULL,
    -- `grupo-` + 32 hexadecimales; lo pone el servidor.
    [publicId] varchar(63) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    -- Ya limpio (sin espacios de más). Único por organización SIN distinguir mayúsculas (la base es CI).
    [name] nvarchar(200) NOT NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_caseGroups_createdAt] DEFAULT (sysutcdatetime()),
    [createdBy] varchar(254) NOT NULL,
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(254) NULL,

    CONSTRAINT [PK_caseGroups] PRIMARY KEY CLUSTERED ([caseGroupId]),
    CONSTRAINT [UQ_caseGroups_publicId] UNIQUE NONCLUSTERED ([publicId]),
    CONSTRAINT [UQ_caseGroups_tenant_name] UNIQUE NONCLUSTERED ([tenant], [name]),
    -- Solo para que `cases` pueda exigir que el grupo sea de la MISMA organización (llave foránea compuesta).
    CONSTRAINT [UQ_caseGroups_tenant_caseGroup] UNIQUE NONCLUSTERED ([tenant], [caseGroupId]),
    CONSTRAINT [CK_caseGroups_publicId] CHECK ([publicId] LIKE 'grupo-%' AND [publicId] COLLATE Latin1_General_BIN2 = lower([publicId]) AND len([publicId]) = (38)),
    CONSTRAINT [CK_caseGroups_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND [tenant] COLLATE Latin1_General_BIN2 = lower([tenant]) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_caseGroups_name] CHECK (len(ltrim(rtrim([name]))) > (0))
);
GO

-- ── 2 · El expediente pertenece a un grupo (o todavía no) ──────────────────────────────────────────────────────────────────
ALTER TABLE [nexus].[cases] ADD [caseGroupId] bigint NULL;
GO

ALTER TABLE [nexus].[cases] ADD CONSTRAINT [FK_cases_caseGroup]
    FOREIGN KEY ([tenant], [caseGroupId]) REFERENCES [nexus].[caseGroups] ([tenant], [caseGroupId]);
GO

-- «Cuántos expedientes tiene este grupo» y «los expedientes de este grupo».
CREATE NONCLUSTERED INDEX [IX_cases_caseGroup] ON [nexus].[cases] ([caseGroupId]);
GO
