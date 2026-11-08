-- 0004 · Expedientes virtuales: el expediente deja de tener tipo y los tipos pasan a ser de EXPEDIENTE VIRTUAL.
--
-- Decisión de producto (Moibe, 2026-10-29): un expediente es solo un RECEPTÁCULO de documentos (la carpeta de un cliente, de un caso) y ya
-- no es instancia de un tipo. Los que tienen esquema son los EXPEDIENTES VIRTUALES: un subconjunto de los documentos de UN expediente,
-- instancia de un tipo de expediente virtual («Solicitud de crédito»: INE, comprobante de domicilio, estado de cuenta). Un expediente
-- puede tener varios, del mismo tipo o de tipos distintos. Sirven para tomar grupitos de documentos y usarlos en el motor de reglas.
--
-- 1. `cases` pierde `caseTypeId`: los expedientes que ya existen se quedan, sin tipo.
-- 2. `caseTypes` y su esquema se RENOMBRAN a `virtualCaseTypes` y `virtualCaseTypeDocumentTypes`: un tipo de expediente virtual es lo mismo
--    que era un tipo de expediente (nombre único por organización + esquema de tipos documentales), así que lo dado de alta se conserva con
--    su id (`tipo-expediente-…`).
-- 3. Nacen `virtualCases` y `virtualCaseDocuments`. Que un expediente virtual SOLO tenga documentos de su expediente lo cuida la base: cada
--    documento de un virtual apunta a la pertenencia (`caseDocuments`) del documento en ese mismo expediente, así que sacar un documento del
--    expediente lo saca también de sus virtuales (ON DELETE CASCADE).

-- ── 1 · El expediente ya no tiene tipo ─────────────────────────────────────────────────────────────────────────────────────
ALTER TABLE [nexus].[cases] DROP CONSTRAINT [FK_cases_caseType];
GO

DROP INDEX [IX_cases_caseType] ON [nexus].[cases];
GO

DROP INDEX [IX_cases_tenant] ON [nexus].[cases];
GO

ALTER TABLE [nexus].[cases] DROP COLUMN [caseTypeId];
GO

CREATE NONCLUSTERED INDEX [IX_cases_tenant] ON [nexus].[cases] ([tenant], [caseId]);
GO

-- ── 2 · Los tipos de expediente pasan a ser tipos de expediente VIRTUAL ────────────────────────────────────────────────────
EXEC sp_rename 'nexus.caseTypes', 'virtualCaseTypes';
EXEC sp_rename 'nexus.PK_caseTypes', 'PK_virtualCaseTypes', 'OBJECT';
EXEC sp_rename 'nexus.UQ_caseTypes_publicId', 'UQ_virtualCaseTypes_publicId', 'OBJECT';
EXEC sp_rename 'nexus.UQ_caseTypes_tenant_name', 'UQ_virtualCaseTypes_tenant_name', 'OBJECT';
EXEC sp_rename 'nexus.CK_caseTypes_publicId', 'CK_virtualCaseTypes_publicId', 'OBJECT';
EXEC sp_rename 'nexus.CK_caseTypes_tenant', 'CK_virtualCaseTypes_tenant', 'OBJECT';
EXEC sp_rename 'nexus.CK_caseTypes_name', 'CK_virtualCaseTypes_name', 'OBJECT';
EXEC sp_rename 'nexus.DF_caseTypes_createdAt', 'DF_virtualCaseTypes_createdAt', 'OBJECT';
EXEC sp_rename 'nexus.virtualCaseTypes.caseTypeId', 'virtualCaseTypeId', 'COLUMN';
GO

EXEC sp_rename 'nexus.caseTypeDocumentTypes', 'virtualCaseTypeDocumentTypes';
EXEC sp_rename 'nexus.PK_caseTypeDocumentTypes', 'PK_virtualCaseTypeDocumentTypes', 'OBJECT';
EXEC sp_rename 'nexus.FK_caseTypeDocumentTypes_caseType', 'FK_virtualCaseTypeDocumentTypes_virtualCaseType', 'OBJECT';
EXEC sp_rename 'nexus.FK_caseTypeDocumentTypes_documentType', 'FK_virtualCaseTypeDocumentTypes_documentType', 'OBJECT';
EXEC sp_rename 'nexus.CK_caseTypeDocumentTypes_position', 'CK_virtualCaseTypeDocumentTypes_position', 'OBJECT';
EXEC sp_rename 'nexus.virtualCaseTypeDocumentTypes.IX_caseTypeDocumentTypes_documentType', 'IX_virtualCaseTypeDocumentTypes_documentType', 'INDEX';
EXEC sp_rename 'nexus.virtualCaseTypeDocumentTypes.caseTypeId', 'virtualCaseTypeId', 'COLUMN';
GO

-- ── 3 · Los expedientes virtuales ──────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[virtualCases] (
    [virtualCaseId] bigint IDENTITY(1,1) NOT NULL,
    -- `expediente-virtual-` + 32 hexadecimales; lo pone el servidor.
    [publicId] varchar(63) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    -- El expediente del que es subconjunto. Sin ON DELETE: un expediente con virtuales no se borra por debajo (hoy no se borran).
    [caseId] bigint NOT NULL,
    -- El tipo del que es instancia. Sin ON DELETE: un tipo con expedientes virtuales no se puede borrar (también lo explica el servicio).
    [virtualCaseTypeId] bigint NOT NULL,
    -- Dos pueden llamarse igual, también en el mismo expediente: un cliente puede tener dos solicitudes de crédito.
    [name] nvarchar(200) NOT NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_virtualCases_createdAt] DEFAULT (sysutcdatetime()),
    [createdBy] varchar(254) NOT NULL,

    CONSTRAINT [PK_virtualCases] PRIMARY KEY CLUSTERED ([virtualCaseId]),
    CONSTRAINT [UQ_virtualCases_publicId] UNIQUE NONCLUSTERED ([publicId]),
    -- Solo para que `virtualCaseDocuments` pueda exigir que el documento sea del MISMO expediente (llave foránea compuesta).
    CONSTRAINT [UQ_virtualCases_virtualCase_case] UNIQUE NONCLUSTERED ([virtualCaseId], [caseId]),
    CONSTRAINT [FK_virtualCases_case] FOREIGN KEY ([caseId]) REFERENCES [nexus].[cases] ([caseId]),
    CONSTRAINT [FK_virtualCases_virtualCaseType] FOREIGN KEY ([virtualCaseTypeId]) REFERENCES [nexus].[virtualCaseTypes] ([virtualCaseTypeId]),
    CONSTRAINT [CK_virtualCases_publicId] CHECK ([publicId] LIKE 'expediente-virtual-%' AND [publicId] COLLATE Latin1_General_BIN2 = lower([publicId]) AND len([publicId]) = (51)),
    CONSTRAINT [CK_virtualCases_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND [tenant] COLLATE Latin1_General_BIN2 = lower([tenant]) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_virtualCases_name] CHECK (len(ltrim(rtrim([name]))) > (0))
);
GO

CREATE NONCLUSTERED INDEX [IX_virtualCases_case] ON [nexus].[virtualCases] ([caseId]) INCLUDE ([virtualCaseTypeId]);
GO

CREATE NONCLUSTERED INDEX [IX_virtualCases_virtualCaseType] ON [nexus].[virtualCases] ([virtualCaseTypeId]);
GO

CREATE TABLE [nexus].[virtualCaseDocuments] (
    -- Solo da el orden en que se agregaron.
    [virtualCaseDocumentId] bigint IDENTITY(1,1) NOT NULL,
    [virtualCaseId] bigint NOT NULL,
    -- El expediente del virtual, repetido a propósito: es lo que deja amarrar el documento a SU pertenencia en ese expediente.
    [caseId] bigint NOT NULL,
    [documentId] bigint NOT NULL,
    [addedAt] datetime2(7) NOT NULL CONSTRAINT [DF_virtualCaseDocuments_addedAt] DEFAULT (sysutcdatetime()),
    [addedBy] varchar(254) NOT NULL,

    CONSTRAINT [PK_virtualCaseDocuments] PRIMARY KEY CLUSTERED ([virtualCaseDocumentId]),
    CONSTRAINT [UQ_virtualCaseDocuments_virtualCase_document] UNIQUE NONCLUSTERED ([virtualCaseId], [documentId]),
    -- Borrar el expediente virtual se lleva sus renglones.
    CONSTRAINT [FK_virtualCaseDocuments_virtualCase] FOREIGN KEY ([virtualCaseId], [caseId]) REFERENCES [nexus].[virtualCases] ([virtualCaseId], [caseId]) ON DELETE CASCADE,
    -- EL SUBCONJUNTO: el documento tiene que estar en el expediente del virtual, y si sale de él, sale también de aquí.
    CONSTRAINT [FK_virtualCaseDocuments_caseDocument] FOREIGN KEY ([caseId], [documentId]) REFERENCES [nexus].[caseDocuments] ([caseId], [documentId]) ON DELETE CASCADE
);
GO

CREATE NONCLUSTERED INDEX [IX_virtualCaseDocuments_caseDocument] ON [nexus].[virtualCaseDocuments] ([caseId], [documentId]);
GO
