-- 0003 · Expedientes: tipos de expediente (con su esquema), documentos, expedientes y la pertenencia documento–expediente.
--
-- Segunda entidad del producto que deja el almacén (la primera fue `0002`, tipos documentales). Hasta hoy estas tres vivían en
-- JSONL de solo-agregar por organización (`tipos-expediente-{tenant}.jsonl`, `documentos-{tenant}.jsonl`,
-- `expedientes-{tenant}.jsonl`); se migran juntas porque se referencian entre sí: un expediente es instancia de un tipo, el esquema
-- de un tipo apunta a tipos documentales y la pertenencia une expedientes con documentos.
--
-- Arrancan VACÍAS, como `0002` («dev limpio», 2026-10-28): lo que había en los JSONL no se importa. Los BYTES de los documentos
-- siguen en el almacén (NAS), direccionados por su sha256; aquí solo vive el registro de que entraron.
--
-- `tenant` es texto y no llave foránea, por la misma razón que en `0002`: las organizaciones todavía viven en el almacén.

IF SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Falta el esquema nexus: lo crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
GO

-- ── Tipos de expediente: la CLASE («expediente médico»); cada expediente es una instancia ─────────────────────────────────
CREATE TABLE [nexus].[caseTypes] (
    [caseTypeId] bigint IDENTITY(1,1) NOT NULL,
    -- `tipo-expediente-` + 32 hexadecimales; lo pone el servidor.
    [publicId] varchar(63) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    -- Ya limpio (sin espacios de más). Único por organización SIN distinguir mayúsculas: la base es CI, así que la UNIQUE de
    -- abajo ya compara como pide el producto («Médico» y «médico» chocan).
    [name] nvarchar(200) NOT NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_caseTypes_createdAt] DEFAULT (sysutcdatetime()),
    [createdBy] varchar(254) NOT NULL,
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(254) NULL,

    CONSTRAINT [PK_caseTypes] PRIMARY KEY CLUSTERED ([caseTypeId]),
    CONSTRAINT [UQ_caseTypes_publicId] UNIQUE NONCLUSTERED ([publicId]),
    CONSTRAINT [UQ_caseTypes_tenant_name] UNIQUE NONCLUSTERED ([tenant], [name]),
    CONSTRAINT [CK_caseTypes_publicId] CHECK ([publicId] LIKE 'tipo-expediente-%' AND [publicId] COLLATE Latin1_General_BIN2 = lower([publicId]) AND len([publicId]) = (48)),
    CONSTRAINT [CK_caseTypes_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND [tenant] COLLATE Latin1_General_BIN2 = lower([tenant]) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_caseTypes_name] CHECK (len(ltrim(rtrim([name]))) > (0))
);
GO

-- El esquema de un tipo: los tipos documentales que lo DESCRIBEN (no restringen), en orden. La llave foránea a
-- `documentTypes` es real: borrar un tipo documental lo quita de los esquemas que lo tenían (antes se quedaba como un id sin nombre).
CREATE TABLE [nexus].[caseTypeDocumentTypes] (
    [caseTypeId] bigint NOT NULL,
    [documentTypePublicId] varchar(63) NOT NULL,
    [position] int NOT NULL,

    CONSTRAINT [PK_caseTypeDocumentTypes] PRIMARY KEY CLUSTERED ([caseTypeId], [documentTypePublicId]),
    CONSTRAINT [FK_caseTypeDocumentTypes_caseType] FOREIGN KEY ([caseTypeId]) REFERENCES [nexus].[caseTypes] ([caseTypeId]) ON DELETE CASCADE,
    CONSTRAINT [FK_caseTypeDocumentTypes_documentType] FOREIGN KEY ([documentTypePublicId]) REFERENCES [nexus].[documentTypes] ([publicId]) ON DELETE CASCADE,
    CONSTRAINT [CK_caseTypeDocumentTypes_position] CHECK ([position] >= (0))
);
GO

CREATE NONCLUSTERED INDEX [IX_caseTypeDocumentTypes_documentType] ON [nexus].[caseTypeDocumentTypes] ([documentTypePublicId]);
GO

-- ── Documentos: nacen al entrar (por la API o a mano) y existen por sí mismos ──────────────────────────────────────────────
CREATE TABLE [nexus].[documents] (
    [documentId] bigint IDENTITY(1,1) NOT NULL,
    -- `documento-` + 32 hexadecimales; lo pone el servidor.
    [publicId] varchar(63) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    -- Dónde están los bytes en el almacén: se direccionan por contenido, así que dos documentos pueden compartirlos.
    [sha256] char(64) NOT NULL,
    [relativePath] nvarchar(400) NOT NULL,
    [sizeBytes] bigint NOT NULL,
    [mime] varchar(100) NOT NULL,
    [originalName] nvarchar(400) NOT NULL,
    [channel] varchar(10) NOT NULL,
    -- La API Key con la que se subió (NULL si entró a mano).
    [keyId] varchar(100) NULL,
    [receivedAt] datetime2(0) NOT NULL CONSTRAINT [DF_documents_receivedAt] DEFAULT (sysutcdatetime()),

    CONSTRAINT [PK_documents] PRIMARY KEY CLUSTERED ([documentId]),
    CONSTRAINT [UQ_documents_publicId] UNIQUE NONCLUSTERED ([publicId]),
    CONSTRAINT [CK_documents_publicId] CHECK ([publicId] LIKE 'documento-%' AND [publicId] COLLATE Latin1_General_BIN2 = lower([publicId]) AND len([publicId]) = (42)),
    CONSTRAINT [CK_documents_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND [tenant] COLLATE Latin1_General_BIN2 = lower([tenant]) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_documents_sha256] CHECK ([sha256] COLLATE Latin1_General_BIN2 NOT LIKE '%[^0-9a-f]%'),
    CONSTRAINT [CK_documents_sizeBytes] CHECK ([sizeBytes] >= (0)),
    CONSTRAINT [CK_documents_channel] CHECK ([channel] IN ('API', 'MANUAL'))
);
GO

-- «Archivos procesados» pide los de una organización, del más viejo al más nuevo.
CREATE NONCLUSTERED INDEX [IX_documents_tenant] ON [nexus].[documents] ([tenant], [documentId]);
GO

-- ── Expedientes: la carpeta de UN paciente, de UN choque… instancia de un tipo de su organización ────────────────────────
CREATE TABLE [nexus].[cases] (
    [caseId] bigint IDENTITY(1,1) NOT NULL,
    -- `expediente-` + 32 hexadecimales; lo pone el servidor.
    [publicId] varchar(63) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    -- Sin ON DELETE: un tipo con expedientes no se puede borrar (también lo cuida el servicio, que lo explica con un 409).
    [caseTypeId] bigint NOT NULL,
    -- Dos expedientes SÍ pueden llamarse igual: un mismo cliente puede tener dos solicitudes.
    [name] nvarchar(200) NOT NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_cases_createdAt] DEFAULT (sysutcdatetime()),
    [createdBy] varchar(254) NOT NULL,

    CONSTRAINT [PK_cases] PRIMARY KEY CLUSTERED ([caseId]),
    CONSTRAINT [UQ_cases_publicId] UNIQUE NONCLUSTERED ([publicId]),
    CONSTRAINT [FK_cases_caseType] FOREIGN KEY ([caseTypeId]) REFERENCES [nexus].[caseTypes] ([caseTypeId]),
    CONSTRAINT [CK_cases_publicId] CHECK ([publicId] LIKE 'expediente-%' AND [publicId] COLLATE Latin1_General_BIN2 = lower([publicId]) AND len([publicId]) = (43)),
    CONSTRAINT [CK_cases_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND [tenant] COLLATE Latin1_General_BIN2 = lower([tenant]) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_cases_name] CHECK (len(ltrim(rtrim([name]))) > (0))
);
GO

CREATE NONCLUSTERED INDEX [IX_cases_tenant] ON [nexus].[cases] ([tenant], [caseId]) INCLUDE ([caseTypeId]);
GO

CREATE NONCLUSTERED INDEX [IX_cases_caseType] ON [nexus].[cases] ([caseTypeId]);
GO

-- ── La pertenencia: muchos a muchos. Un documento sin ninguna fila aquí está en el «void» ─────────────────────────────────
CREATE TABLE [nexus].[caseDocuments] (
    -- Solo da el orden en que se agregaron, que es el que enseña el expediente.
    [caseDocumentId] bigint IDENTITY(1,1) NOT NULL,
    [caseId] bigint NOT NULL,
    [documentId] bigint NOT NULL,
    [addedAt] datetime2(7) NOT NULL CONSTRAINT [DF_caseDocuments_addedAt] DEFAULT (sysutcdatetime()),
    [addedBy] varchar(254) NOT NULL,

    CONSTRAINT [PK_caseDocuments] PRIMARY KEY CLUSTERED ([caseDocumentId]),
    -- Meter dos veces el mismo documento no repite nada: lo impide la base, no solo Python.
    CONSTRAINT [UQ_caseDocuments_case_document] UNIQUE NONCLUSTERED ([caseId], [documentId]),
    CONSTRAINT [FK_caseDocuments_case] FOREIGN KEY ([caseId]) REFERENCES [nexus].[cases] ([caseId]) ON DELETE CASCADE,
    CONSTRAINT [FK_caseDocuments_document] FOREIGN KEY ([documentId]) REFERENCES [nexus].[documents] ([documentId])
);
GO

CREATE NONCLUSTERED INDEX [IX_caseDocuments_document] ON [nexus].[caseDocuments] ([documentId]) INCLUDE ([caseId]);
GO
