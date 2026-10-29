-- 0001 · Línea base: el esquema de Nexus como estaba al llegar el corredor de migraciones.
--
-- GENERADO por `python -m migraciones.linea_base` a partir del catálogo de una base de Nexus: 36 tablas, 36 funciones y procedimientos
-- (congelados: son la especificación del dominio, ver la decisión del 2026-10-21), los catálogos de `reference` con sus filas, `security.roles` y
-- la fila de `security.tenantSequence`. NO se edita a mano: si el esquema cambia, se agrega una migración nueva.
--
-- En una base que YA tiene este esquema no se ejecuta: se MARCA (`python -m migraciones marcar --version 1`). Solo corre en una base vacía.
-- No crea los esquemas ni `nexusOwner` ni los usuarios y permisos: eso es del DBA.

IF SCHEMA_ID('documents') IS NULL OR SCHEMA_ID('security') IS NULL OR SCHEMA_ID('reference') IS NULL OR SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Faltan los esquemas documents, security, reference o nexus: los crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
IF OBJECT_ID('security.tenants', 'U') IS NOT NULL
    THROW 51002, 'Esta base ya tiene el esquema de la línea base: se MARCA (python -m migraciones marcar --version 1), no se ejecuta.', 1;
GO

-- ── Tablas, con sus llaves primarias, restricciones únicas, CHECK y valores por omisión ──

CREATE TABLE [documents].[cases] (
    [caseId] bigint IDENTITY(1,1) NOT NULL,
    [caseGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_cases_caseGuid] DEFAULT (newid()),
    [tenantId] int NOT NULL,
    [externalRef] varchar(120) NULL,
    [caseStatusId] int NOT NULL,
    [openedAt] datetime2(7) NOT NULL CONSTRAINT [DF_cases_openedAt] DEFAULT (sysdatetime()),
    [closedAt] datetime2(7) NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_cases_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_cases_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_cases] PRIMARY KEY CLUSTERED ([caseId]),
    CONSTRAINT [UQ_cases_caseGuid] UNIQUE NONCLUSTERED ([caseGuid])
);
GO

CREATE TABLE [documents].[documents] (
    [documentId] bigint IDENTITY(1,1) NOT NULL,
    [documentGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_documents_documentGuid] DEFAULT (newid()),
    [tenantId] int NOT NULL,
    [fileId] bigint NOT NULL,
    [documentTypeId] int NULL,
    [segmentIndex] int NOT NULL,
    [pageStart] int NOT NULL,
    [pageEnd] int NOT NULL,
    [classificationConfidence] decimal(5,2) NULL,
    [documentStatusId] int NOT NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_documents_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_documents_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_documents] PRIMARY KEY CLUSTERED ([documentId]),
    CONSTRAINT [UQ_documents_documentGuid] UNIQUE NONCLUSTERED ([documentGuid]),
    CONSTRAINT [UQ_documents_file_segmentIndex] UNIQUE NONCLUSTERED ([fileId], [segmentIndex]),
    CONSTRAINT [CK_documents_pageRange] CHECK ([pageEnd]>=[pageStart])
);
GO

CREATE TABLE [documents].[entityFacts] (
    [entityFactId] bigint IDENTITY(1,1) NOT NULL,
    [entityFactGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_entityFacts_entityFactGuid] DEFAULT (newid()),
    [tenantId] int NOT NULL,
    [extractionRunId] bigint NOT NULL,
    [fieldDefinitionId] int NOT NULL,
    [ocrBlockId] bigint NULL,
    [valueRaw] nvarchar(MAX) NULL,
    [valueNormalized] nvarchar(MAX) NULL,
    [pageNumber] int NULL,
    [confidence] decimal(5,2) NULL,
    [confidenceMethodId] int NULL,
    [confidenceRaw] nvarchar(MAX) NULL,
    [nullReasonId] int NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_entityFacts_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_entityFacts_createdBy] DEFAULT (suser_sname()),
    CONSTRAINT [PK_entityFacts] PRIMARY KEY CLUSTERED ([entityFactId]),
    CONSTRAINT [UQ_entityFacts_entityFactGuid] UNIQUE NONCLUSTERED ([entityFactGuid]),
    CONSTRAINT [UQ_entityFacts_run_field] UNIQUE NONCLUSTERED ([extractionRunId], [fieldDefinitionId]),
    CONSTRAINT [CK_entityFacts_value_or_nullReason] CHECK ([valueNormalized] IS NOT NULL AND [nullReasonId] IS NULL OR [valueNormalized] IS NULL AND [nullReasonId] IS NOT NULL)
);
GO

CREATE TABLE [documents].[entityFactSources] (
    [entityFactSourceId] bigint IDENTITY(1,1) NOT NULL,
    [entityFactSourceGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_entityFactSources_entityFactSourceGuid] DEFAULT (newid()),
    [entityFactId] bigint NOT NULL,
    [ocrBlockId] bigint NOT NULL,
    [sourceOrder] smallint NOT NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_entityFactSources_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_entityFactSources_createdBy] DEFAULT (suser_sname()),
    CONSTRAINT [PK_entityFactSources] PRIMARY KEY CLUSTERED ([entityFactSourceId]),
    CONSTRAINT [UQ_entityFactSources_entityFactSourceGuid] UNIQUE NONCLUSTERED ([entityFactSourceGuid]),
    CONSTRAINT [UQ_entityFactSources_fact_block] UNIQUE NONCLUSTERED ([entityFactId], [ocrBlockId]),
    CONSTRAINT [UQ_entityFactSources_fact_order] UNIQUE NONCLUSTERED ([entityFactId], [sourceOrder])
);
GO

CREATE TABLE [documents].[extractionRuns] (
    [extractionRunId] bigint IDENTITY(1,1) NOT NULL,
    [extractionRunGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_extractionRuns_extractionRunGuid] DEFAULT (newid()),
    [tenantId] int NOT NULL,
    [documentId] bigint NOT NULL,
    [configVersionId] int NOT NULL,
    [ocrResultId] bigint NOT NULL,
    [extractionEngineId] int NOT NULL,
    [engineVersion] varchar(80) NOT NULL,
    [promptHash] char(64) NULL,
    [startedAt] datetime2(7) NOT NULL,
    [finishedAt] datetime2(7) NULL,
    [runStatusId] int NOT NULL,
    [errorDetail] nvarchar(MAX) NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_extractionRuns_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_extractionRuns_createdBy] DEFAULT (suser_sname()),
    CONSTRAINT [PK_extractionRuns] PRIMARY KEY CLUSTERED ([extractionRunId]),
    CONSTRAINT [UQ_extractionRuns_extractionRunGuid] UNIQUE NONCLUSTERED ([extractionRunGuid]),
    CONSTRAINT [CK_extractionRuns_engineVersion] CHECK (len(ltrim(rtrim([engineVersion])))>(0)),
    CONSTRAINT [CK_extractionRuns_promptHash] CHECK ([promptHash] IS NULL OR len([promptHash])=(64) AND NOT [promptHash] like '%[^0-9A-Fa-f]%')
);
GO

CREATE TABLE [documents].[fieldCorrections] (
    [fieldCorrectionId] bigint IDENTITY(1,1) NOT NULL,
    [fieldCorrectionGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_fieldCorrections_fieldCorrectionGuid] DEFAULT (newid()),
    [tenantId] int NOT NULL,
    [entityFactId] bigint NOT NULL,
    [correctedValue] nvarchar(MAX) NULL,
    [correctionReasonId] int NOT NULL,
    [correctedBy] int NOT NULL,
    [notes] nvarchar(MAX) NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_fieldCorrections_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_fieldCorrections_createdBy] DEFAULT (suser_sname()),
    CONSTRAINT [PK_fieldCorrections] PRIMARY KEY CLUSTERED ([fieldCorrectionId]),
    CONSTRAINT [UQ_fieldCorrections_fieldCorrectionGuid] UNIQUE NONCLUSTERED ([fieldCorrectionGuid])
);
GO

CREATE TABLE [documents].[fileEvents] (
    [fileEventId] bigint IDENTITY(1,1) NOT NULL,
    [fileEventGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_fileEvents_fileEventGuid] DEFAULT (newid()),
    [fileId] bigint NOT NULL,
    [fileEventTypeId] int NOT NULL,
    [occurredAt] datetime2(7) NOT NULL CONSTRAINT [DF_fileEvents_occurredAt] DEFAULT (sysdatetime()),
    [byUserId] int NULL,
    [reason] nvarchar(500) NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_fileEvents_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_fileEvents_createdBy] DEFAULT (suser_sname()),
    CONSTRAINT [PK_fileEvents] PRIMARY KEY CLUSTERED ([fileEventId]),
    CONSTRAINT [UQ_fileEvents_fileEventGuid] UNIQUE NONCLUSTERED ([fileEventGuid]),
    CONSTRAINT [CK_fileEvents_reason] CHECK ([reason] IS NULL OR len(ltrim(rtrim([reason])))>(0))
);
GO

CREATE TABLE [documents].[files] (
    [fileId] bigint IDENTITY(1,1) NOT NULL,
    [fileGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_files_fileGuid] DEFAULT (newid()),
    [tenantId] int NOT NULL,
    [caseId] bigint NOT NULL,
    [ingestionChannelId] int NOT NULL,
    [storageUri] nvarchar(2048) NOT NULL,
    [originalFileName] nvarchar(260) NOT NULL,
    [mimeType] varchar(100) NOT NULL,
    [sizeBytes] bigint NOT NULL,
    [sha256] char(64) NOT NULL,
    [pageCount] int NULL,
    [receivedAt] datetime2(7) NOT NULL CONSTRAINT [DF_files_receivedAt] DEFAULT (sysdatetime()),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_files_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_files_createdBy] DEFAULT (suser_sname()),
    [fileValidationStatusId] int NULL,
    [ingestionSourceRef] nvarchar(100) NULL,
    CONSTRAINT [PK_files] PRIMARY KEY CLUSTERED ([fileId]),
    CONSTRAINT [UQ_files_fileGuid] UNIQUE NONCLUSTERED ([fileGuid]),
    CONSTRAINT [CK_files_ingestionSourceRef] CHECK ([ingestionSourceRef] IS NULL OR len(ltrim(rtrim([ingestionSourceRef])))>(0)),
    CONSTRAINT [CK_files_mimeType] CHECK (len(ltrim(rtrim([mimeType])))>(0)),
    CONSTRAINT [CK_files_originalFileName] CHECK (len(ltrim(rtrim([originalFileName])))>(0)),
    CONSTRAINT [CK_files_pageCount] CHECK ([pageCount] IS NULL OR [pageCount]>(0)),
    CONSTRAINT [CK_files_sha256] CHECK (len([sha256])=(64) AND NOT [sha256] like '%[^0-9A-Fa-f]%'),
    CONSTRAINT [CK_files_sizeBytes] CHECK ([sizeBytes]>=(0)),
    CONSTRAINT [CK_files_storageUri] CHECK (len(ltrim(rtrim([storageUri])))>(0))
);
GO

CREATE TABLE [documents].[ocrBlocks] (
    [ocrBlockId] bigint IDENTITY(1,1) NOT NULL,
    [ocrBlockGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_ocrBlocks_ocrBlockGuid] DEFAULT (newid()),
    [ocrResultId] bigint NOT NULL,
    [pageNumber] int NOT NULL,
    [blockIndex] int NOT NULL,
    [blockTypeId] int NULL,
    [textContent] nvarchar(MAX) NOT NULL,
    [bboxX] decimal(7,6) NOT NULL,
    [bboxY] decimal(7,6) NOT NULL,
    [bboxW] decimal(7,6) NOT NULL,
    [bboxH] decimal(7,6) NOT NULL,
    [rotation] smallint NOT NULL CONSTRAINT [DF_ocrBlocks_rotation] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_ocrBlocks_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_ocrBlocks_createdBy] DEFAULT (suser_sname()),
    CONSTRAINT [PK_ocrBlocks] PRIMARY KEY CLUSTERED ([ocrBlockId]),
    CONSTRAINT [UQ_ocrBlocks_ocrBlockGuid] UNIQUE NONCLUSTERED ([ocrBlockGuid]),
    CONSTRAINT [UQ_ocrBlocks_result_page_index] UNIQUE NONCLUSTERED ([ocrResultId], [pageNumber], [blockIndex]),
    CONSTRAINT [CK_ocrBlocks_bboxH] CHECK ([bboxH]>=(0) AND [bboxH]<=(1)),
    CONSTRAINT [CK_ocrBlocks_bboxW] CHECK ([bboxW]>=(0) AND [bboxW]<=(1)),
    CONSTRAINT [CK_ocrBlocks_bboxX] CHECK ([bboxX]>=(0) AND [bboxX]<=(1)),
    CONSTRAINT [CK_ocrBlocks_bboxY] CHECK ([bboxY]>=(0) AND [bboxY]<=(1)),
    CONSTRAINT [CK_ocrBlocks_rotation] CHECK ([rotation]=(270) OR [rotation]=(180) OR [rotation]=(90) OR [rotation]=(0))
);
GO

CREATE TABLE [documents].[ocrResults] (
    [ocrResultId] bigint IDENTITY(1,1) NOT NULL,
    [ocrResultGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_ocrResults_ocrResultGuid] DEFAULT (newid()),
    [tenantId] int NOT NULL,
    [documentId] bigint NOT NULL,
    [ocrEngineId] int NOT NULL,
    [engineVersion] varchar(60) NOT NULL,
    [language] varchar(10) NULL,
    [qualityScore] decimal(5,2) NULL,
    [pageCount] int NOT NULL,
    [runStatusId] int NOT NULL,
    [processedAt] datetime2(7) NOT NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_ocrResults_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_ocrResults_createdBy] DEFAULT (suser_sname()),
    CONSTRAINT [PK_ocrResults] PRIMARY KEY CLUSTERED ([ocrResultId]),
    CONSTRAINT [UQ_ocrResults_ocrResultGuid] UNIQUE NONCLUSTERED ([ocrResultGuid]),
    CONSTRAINT [CK_ocrResults_engineVersion] CHECK (len(ltrim(rtrim([engineVersion])))>(0)),
    CONSTRAINT [CK_ocrResults_pageCount] CHECK ([pageCount]>(0))
);
GO

CREATE TABLE [reference].[blockTypes] (
    [blockTypeId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(20) NOT NULL,
    [name] nvarchar(200) NOT NULL,
    [description] nvarchar(600) NULL,
    [sortOrder] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_blockTypes_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_blockTypes_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_blockTypes_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_blockTypes_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_blockTypes] PRIMARY KEY CLUSTERED ([blockTypeId]),
    CONSTRAINT [UQ_blockTypes_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_blockTypes_code] CHECK ([code]='OTHER' OR [code]='SIGNATURE' OR [code]='TABLE_CELL' OR [code]='WORD' OR [code]='LINE' OR [code]='PARAGRAPH')
);
GO

CREATE TABLE [reference].[caseStatuses] (
    [caseStatusId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(20) NOT NULL,
    [name] nvarchar(200) NOT NULL,
    [description] nvarchar(600) NULL,
    [sortOrder] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_caseStatuses_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_caseStatuses_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_caseStatuses_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_caseStatuses_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_caseStatuses] PRIMARY KEY CLUSTERED ([caseStatusId]),
    CONSTRAINT [UQ_caseStatuses_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_caseStatuses_code] CHECK ([code]='CANCELLED' OR [code]='CLOSED' OR [code]='DECIDED' OR [code]='READY_FOR_REVIEW' OR [code]='PROCESSING' OR [code]='OPEN')
);
GO

CREATE TABLE [reference].[confidenceMethods] (
    [confidenceMethodId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(30) NOT NULL,
    [name] nvarchar(200) NOT NULL,
    [description] nvarchar(600) NULL,
    [sortOrder] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_confidenceMethods_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_confidenceMethods_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_confidenceMethods_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_confidenceMethods_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_confidenceMethods] PRIMARY KEY CLUSTERED ([confidenceMethodId]),
    CONSTRAINT [UQ_confidenceMethods_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_confidenceMethods_code] CHECK ([code]='COMPOSITE' OR [code]='EXTRACTOR_CONFIDENCE' OR [code]='LOGPROB_RESCALED')
);
GO

CREATE TABLE [reference].[correctionReasons] (
    [correctionReasonId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(30) NOT NULL,
    [name] nvarchar(200) NOT NULL,
    [description] nvarchar(600) NULL,
    [sortOrder] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_correctionReasons_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_correctionReasons_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_correctionReasons_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_correctionReasons_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_correctionReasons] PRIMARY KEY CLUSTERED ([correctionReasonId]),
    CONSTRAINT [UQ_correctionReasons_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_correctionReasons_code] CHECK ([code]='FORMAT_FIX' OR [code]='NOT_PRESENT_CONFIRMED' OR [code]='WRONG_SOURCE' OR [code]='WRONG_VALUE')
);
GO

CREATE TABLE [reference].[documentStatuses] (
    [documentStatusId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(20) NOT NULL,
    [name] nvarchar(200) NOT NULL,
    [description] nvarchar(600) NULL,
    [sortOrder] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_documentStatuses_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_documentStatuses_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_documentStatuses_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_documentStatuses_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_documentStatuses] PRIMARY KEY CLUSTERED ([documentStatusId]),
    CONSTRAINT [UQ_documentStatuses_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_documentStatuses_code] CHECK ([code]='FAILED' OR [code]='EXTRACTED' OR [code]='CLASSIFIED' OR [code]='PENDING')
);
GO

CREATE TABLE [reference].[documentTypes] (
    [documentTypeId] int IDENTITY(1,1) NOT NULL,
    [documentTypeGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_documentTypes_documentTypeGuid] DEFAULT (newid()),
    [tenantId] int NULL,
    [documentTypeStatusId] int NOT NULL,
    [code] varchar(40) NOT NULL,
    [name] nvarchar(150) NOT NULL,
    [description] nvarchar(500) NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_documentTypes_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_documentTypes_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_documentTypes] PRIMARY KEY CLUSTERED ([documentTypeId]),
    CONSTRAINT [UQ_documentTypes_documentTypeGuid] UNIQUE NONCLUSTERED ([documentTypeGuid]),
    CONSTRAINT [CK_documentTypes_code] CHECK (len(ltrim(rtrim([code])))>(0) AND NOT [code] like '% %'),
    CONSTRAINT [CK_documentTypes_name] CHECK (len(ltrim(rtrim([name])))>(0))
);
GO

CREATE TABLE [reference].[documentTypeStatuses] (
    [documentTypeStatusId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(20) NOT NULL,
    [name] nvarchar(100) NOT NULL,
    [description] nvarchar(300) NULL,
    [sortOrder] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_documentTypeStatuses_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_documentTypeStatuses_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_documentTypeStatuses_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_documentTypeStatuses_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_documentTypeStatuses] PRIMARY KEY CLUSTERED ([documentTypeStatusId]),
    CONSTRAINT [UQ_documentTypeStatuses_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_documentTypeStatuses_code] CHECK ([code]='RETIRED' OR [code]='ACTIVE')
);
GO

CREATE TABLE [reference].[extractionEngines] (
    [extractionEngineId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(30) NOT NULL,
    [name] nvarchar(200) NOT NULL,
    [description] nvarchar(600) NULL,
    [sortOrder] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_extractionEngines_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_extractionEngines_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_extractionEngines_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_extractionEngines_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_extractionEngines] PRIMARY KEY CLUSTERED ([extractionEngineId]),
    CONSTRAINT [UQ_extractionEngines_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_extractionEngines_code] CHECK ([code]='OTRO' OR [code]='DOCUMENT_AI_EXTRACTOR' OR [code]='AZURE_OPENAI')
);
GO

CREATE TABLE [reference].[fileEventTypes] (
    [fileEventTypeId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(30) NOT NULL,
    [name] nvarchar(200) NOT NULL,
    [description] nvarchar(600) NULL,
    [sortOrder] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_fileEventTypes_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_fileEventTypes_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_fileEventTypes_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_fileEventTypes_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_fileEventTypes] PRIMARY KEY CLUSTERED ([fileEventTypeId]),
    CONSTRAINT [UQ_fileEventTypes_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_fileEventTypes_code] CHECK ([code]='RESTORED' OR [code]='DISCARDED' OR [code]='SENT_TO_PIPELINE')
);
GO

CREATE TABLE [reference].[fileValidationStatuses] (
    [fileValidationStatusId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(20) NOT NULL,
    [name] nvarchar(200) NOT NULL,
    [description] nvarchar(600) NULL,
    [sortOrder] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_fileValidationStatuses_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_fileValidationStatuses_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_fileValidationStatuses_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_fileValidationStatuses_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_fileValidationStatuses] PRIMARY KEY CLUSTERED ([fileValidationStatusId]),
    CONSTRAINT [UQ_fileValidationStatuses_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_fileValidationStatuses_code] CHECK ([code]='CORRUPT' OR [code]='PASSWORD_PROTECTED' OR [code]='READY')
);
GO

CREATE TABLE [reference].[ingestionChannels] (
    [ingestionChannelId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(20) NOT NULL,
    [name] nvarchar(100) NOT NULL,
    [description] nvarchar(300) NULL,
    [sortOrder] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_ingestionChannels_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_ingestionChannels_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_ingestionChannels_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_ingestionChannels_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_ingestionChannels] PRIMARY KEY CLUSTERED ([ingestionChannelId]),
    CONSTRAINT [UQ_ingestionChannels_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_ingestionChannels_code] CHECK ([code]='ART' OR [code]='API' OR [code]='SHAREPOINT' OR [code]='SFTP' OR [code]='SCRIPT' OR [code]='MANUAL')
);
GO

CREATE TABLE [reference].[nullReasons] (
    [nullReasonId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(30) NOT NULL,
    [name] nvarchar(200) NOT NULL,
    [description] nvarchar(600) NULL,
    [sortOrder] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_nullReasons_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_nullReasons_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_nullReasons_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_nullReasons_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_nullReasons] PRIMARY KEY CLUSTERED ([nullReasonId]),
    CONSTRAINT [UQ_nullReasons_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_nullReasons_code] CHECK ([code]='EXTRACTION_ERROR' OR [code]='BELOW_THRESHOLD' OR [code]='ILLEGIBLE' OR [code]='NOT_PRESENT')
);
GO

CREATE TABLE [reference].[ocrEngines] (
    [ocrEngineId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(20) NOT NULL,
    [name] nvarchar(200) NOT NULL,
    [description] nvarchar(600) NULL,
    [sortOrder] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_ocrEngines_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_ocrEngines_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_ocrEngines_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_ocrEngines_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_ocrEngines] PRIMARY KEY CLUSTERED ([ocrEngineId]),
    CONSTRAINT [UQ_ocrEngines_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_ocrEngines_code] CHECK ([code]='NATIVE_PDF' OR [code]='AZURE_OCR' OR [code]='DOCUMENT_AI')
);
GO

CREATE TABLE [reference].[runStatuses] (
    [runStatusId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(20) NOT NULL,
    [name] nvarchar(200) NOT NULL,
    [description] nvarchar(600) NULL,
    [sortOrder] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_runStatuses_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_runStatuses_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_runStatuses_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_runStatuses_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_runStatuses] PRIMARY KEY CLUSTERED ([runStatusId]),
    CONSTRAINT [UQ_runStatuses_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_runStatuses_code] CHECK ([code]='PARTIAL' OR [code]='FAILED' OR [code]='SUCCEEDED')
);
GO

CREATE TABLE [reference].[tenantStatuses] (
    [tenantStatusId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(20) NOT NULL,
    [name] nvarchar(100) NOT NULL,
    [description] nvarchar(300) NULL,
    [sortOrder] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_tenantStatuses_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_tenantStatuses_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_tenantStatuses_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_tenantStatuses_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_tenantStatuses] PRIMARY KEY CLUSTERED ([tenantStatusId]),
    CONSTRAINT [UQ_tenantStatuses_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_tenantStatuses_code] CHECK ([code]='CLOSED' OR [code]='SUSPENDED' OR [code]='ACTIVE')
);
GO

CREATE TABLE [security].[accessTokens] (
    [accessTokenId] bigint IDENTITY(1,1) NOT NULL,
    [accessTokenGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_accessTokens_accessTokenGuid] DEFAULT (newid()),
    [userId] int NOT NULL,
    [kind] varchar(20) NOT NULL,
    [tokenHash] char(64) NOT NULL,
    [expiresAt] datetime2(7) NOT NULL,
    [usedAt] datetime2(7) NULL,
    [invalidatedAt] datetime2(7) NULL,
    [invalidatedReason] varchar(40) NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_accessTokens_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_accessTokens_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_accessTokens] PRIMARY KEY CLUSTERED ([accessTokenId]),
    CONSTRAINT [UQ_accessTokens_accessTokenGuid] UNIQUE NONCLUSTERED ([accessTokenGuid]),
    CONSTRAINT [UQ_accessTokens_tokenHash] UNIQUE NONCLUSTERED ([tokenHash]),
    CONSTRAINT [CK_accessTokens_expiresAt] CHECK ([expiresAt]>[createdAt]),
    CONSTRAINT [CK_accessTokens_invalidated] CHECK ([invalidatedAt] IS NULL AND [invalidatedReason] IS NULL OR [invalidatedAt] IS NOT NULL AND [invalidatedReason] IS NOT NULL),
    CONSTRAINT [CK_accessTokens_kind] CHECK ([kind]='ACTIVATION' OR [kind]='RECOVERY'),
    CONSTRAINT [CK_accessTokens_tokenHash] CHECK (len([tokenHash])=(64) AND NOT [tokenHash] like '%[^0-9A-Fa-f]%'),
    CONSTRAINT [CK_accessTokens_usedOrInvalidated] CHECK ([usedAt] IS NULL OR [invalidatedAt] IS NULL)
);
GO

CREATE TABLE [security].[auditEvents] (
    [auditEventId] bigint IDENTITY(1,1) NOT NULL,
    [auditEventGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_auditEvents_auditEventGuid] DEFAULT (newid()),
    [action] varchar(40) NOT NULL,
    [userId] int NULL,
    [actorUserId] int NULL,
    [tenantId] int NULL,
    [reason] nvarchar(500) NULL,
    [dataJson] nvarchar(MAX) NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_auditEvents_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_auditEvents_createdBy] DEFAULT (suser_sname()),
    CONSTRAINT [PK_auditEvents] PRIMARY KEY CLUSTERED ([auditEventId]),
    CONSTRAINT [UQ_auditEvents_auditEventGuid] UNIQUE NONCLUSTERED ([auditEventGuid]),
    CONSTRAINT [CK_auditEvents_action] CHECK (len([action])>(0) AND NOT [action] like '% %'),
    CONSTRAINT [CK_auditEvents_dataJson] CHECK ([dataJson] IS NULL OR isjson([dataJson])=(1))
);
GO

CREATE TABLE [security].[loginAttempts] (
    [loginAttemptId] bigint IDENTITY(1,1) NOT NULL,
    [userId] int NULL,
    [email] varchar(254) NOT NULL,
    [succeeded] bit NOT NULL,
    [failureReason] varchar(40) NULL,
    [attemptedAt] datetime2(7) NOT NULL CONSTRAINT [DF_loginAttempts_attemptedAt] DEFAULT (sysdatetime()),
    [ipAddress] varchar(45) NULL,
    [userAgent] nvarchar(500) NULL,
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_loginAttempts_createdBy] DEFAULT (suser_sname()),
    [originId] varchar(64) NULL,
    CONSTRAINT [PK_loginAttempts] PRIMARY KEY CLUSTERED ([loginAttemptId]),
    CONSTRAINT [CK_loginAttempts_failureReason] CHECK ([failureReason]='EXPIRED' OR [failureReason]='UNKNOWN_EMAIL' OR [failureReason]='INACTIVE' OR [failureReason]='LOCKED' OR [failureReason]='BAD_PASSWORD'),
    CONSTRAINT [CK_loginAttempts_result] CHECK ([succeeded]=(1) AND [failureReason] IS NULL OR [succeeded]=(0) AND [failureReason] IS NOT NULL)
);
GO

CREATE TABLE [security].[logsEndpoint] (
    [id] int IDENTITY(1,1) NOT NULL,
    [Message] varchar(1000) NULL,
    [levelError] nvarchar(50) NULL,
    [Exception] nvarchar(4000) NULL,
    [Eventype] nvarchar(20) NULL,
    [endpoint] nvarchar(100) NULL,
    [requestbody] nvarchar(4000) NULL,
    [responsestatus] int NULL,
    [userId] nvarchar(450) NULL,
    [responseBody] nvarchar(4000) NULL,
    [timestamp] datetime NULL CONSTRAINT [DF__logsEndpo__times__49C3F6B7] DEFAULT (getdate()),
    [requestCode] varchar(20) NULL,
    CONSTRAINT [PK__logsEndp__3213E83F896033B5] PRIMARY KEY CLUSTERED ([id])
);
GO

CREATE TABLE [security].[roles] (
    [roleId] int IDENTITY(1,1) NOT NULL,
    [code] varchar(40) NOT NULL,
    [name] nvarchar(100) NOT NULL,
    [description] nvarchar(300) NULL,
    [sortOrder] int NOT NULL CONSTRAINT [DF_roles_sortOrder] DEFAULT ((0)),
    [isActive] bit NOT NULL CONSTRAINT [DF_roles_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_roles_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_roles_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_roles_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_roles] PRIMARY KEY CLUSTERED ([roleId]),
    CONSTRAINT [UQ_roles_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_roles_code] CHECK (len(ltrim(rtrim([code])))>(0) AND NOT [code] like '% %'),
    CONSTRAINT [CK_roles_name] CHECK (len(ltrim(rtrim([name])))>(0))
);
GO

CREATE TABLE [security].[sessions] (
    [sessionId] bigint IDENTITY(1,1) NOT NULL,
    [sessionGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_sessions_sessionGuid] DEFAULT (newid()),
    [userId] int NOT NULL,
    [refreshTokenHash] char(64) NOT NULL,
    [issuedAt] datetime2(7) NOT NULL CONSTRAINT [DF_sessions_issuedAt] DEFAULT (sysdatetime()),
    [expiresAt] datetime2(7) NOT NULL,
    [revokedAt] datetime2(7) NULL,
    [revokedReason] varchar(40) NULL,
    [ipAddress] varchar(45) NULL,
    [userAgent] nvarchar(500) NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_sessions_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_sessions_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_sessions] PRIMARY KEY CLUSTERED ([sessionId]),
    CONSTRAINT [UQ_sessions_refreshTokenHash] UNIQUE NONCLUSTERED ([refreshTokenHash]),
    CONSTRAINT [UQ_sessions_sessionGuid] UNIQUE NONCLUSTERED ([sessionGuid]),
    CONSTRAINT [CK_sessions_expiresAt] CHECK ([expiresAt]>[issuedAt]),
    CONSTRAINT [CK_sessions_refreshTokenHash] CHECK (len([refreshTokenHash])=(64) AND NOT [refreshTokenHash] like '%[^0-9A-Fa-f]%'),
    CONSTRAINT [CK_sessions_revoked] CHECK ([revokedAt] IS NULL AND [revokedReason] IS NULL OR [revokedAt] IS NOT NULL AND [revokedReason] IS NOT NULL)
);
GO

CREATE TABLE [security].[tenantMemberships] (
    [tenantMembershipId] int IDENTITY(1,1) NOT NULL,
    [tenantId] int NOT NULL,
    [userId] int NOT NULL,
    [statusCode] varchar(20) NOT NULL CONSTRAINT [DF_tenantMemberships_statusCode] DEFAULT ('ACTIVE'),
    [joinedAt] datetime2(7) NOT NULL CONSTRAINT [DF_tenantMemberships_joinedAt] DEFAULT (sysdatetime()),
    [isActive] bit NOT NULL CONSTRAINT [DF_tenantMemberships_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_tenantMemberships_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_tenantMemberships_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_tenantMemberships_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    [roleId] int NOT NULL,
    CONSTRAINT [PK_tenantMemberships] PRIMARY KEY CLUSTERED ([tenantMembershipId]),
    CONSTRAINT [UQ_tenantMemberships_tenant_user] UNIQUE NONCLUSTERED ([tenantId], [userId]),
    CONSTRAINT [CK_tenantMemberships_statusCode] CHECK ([statusCode]='REVOKED' OR [statusCode]='SUSPENDED' OR [statusCode]='ACTIVE')
);
GO

CREATE TABLE [security].[tenants] (
    [tenantId] int IDENTITY(1,1) NOT NULL,
    [tenantGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_tenants_tenantGuid] DEFAULT (newid()),
    [tenantStatusId] int NOT NULL,
    [tenantCode] varchar(11) NULL,
    [name] nvarchar(200) NOT NULL,
    [slug] varchar(63) NOT NULL,
    [settingsJson] nvarchar(MAX) NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_tenants_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_tenants_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_tenants_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_tenants_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    [recoveryEmail] varchar(254) NULL,
    [recoveryPhone] varchar(25) NULL,
    CONSTRAINT [PK_tenants] PRIMARY KEY CLUSTERED ([tenantId]),
    CONSTRAINT [UQ_tenants_slug] UNIQUE NONCLUSTERED ([slug]),
    CONSTRAINT [UQ_tenants_tenantGuid] UNIQUE NONCLUSTERED ([tenantGuid]),
    CONSTRAINT [CK_tenants_name] CHECK (len(ltrim(rtrim([name])))>(0)),
    CONSTRAINT [CK_tenants_recoveryEmail] CHECK ([recoveryEmail] IS NULL OR [recoveryEmail] like '%_@_%._%'),
    CONSTRAINT [CK_tenants_settingsJson] CHECK ([settingsJson] IS NULL OR isjson([settingsJson])=(1)),
    CONSTRAINT [CK_tenants_slug] CHECK (len(ltrim(rtrim([slug])))>(0) AND [slug]=lower([slug]) AND NOT [slug] like '% %')
);
GO

CREATE TABLE [security].[tenantSequence] (
    [tenantSequenceId] int IDENTITY(1,1) NOT NULL,
    [prefix] varchar(5) NOT NULL,
    [lastSequence] int NOT NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_tenantSequence_isActive] DEFAULT ((1)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_tenantSequence_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_tenantSequence_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    CONSTRAINT [PK_tenantSequence] PRIMARY KEY CLUSTERED ([tenantSequenceId]),
    CONSTRAINT [UQ_tenantSequence_prefix] UNIQUE NONCLUSTERED ([prefix]),
    CONSTRAINT [CK_tenantSequence_lastSequence] CHECK ([lastSequence]>=(0)),
    CONSTRAINT [CK_tenantSequence_prefix] CHECK (len(ltrim(rtrim([prefix])))>(0))
);
GO

CREATE TABLE [security].[userCredentials] (
    [userId] int NOT NULL,
    [passwordHash] varchar(255) NOT NULL,
    [mustChangePassword] bit NOT NULL CONSTRAINT [DF_userCredentials_mustChangePassword] DEFAULT ((1)),
    [passwordChangedAt] datetime2(7) NULL,
    [failedAttempts] int NOT NULL CONSTRAINT [DF_userCredentials_failedAttempts] DEFAULT ((0)),
    [lockedUntil] datetime2(7) NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_userCredentials_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_userCredentials_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    [temporaryPasswordIssuedAt] datetime2(7) NULL,
    CONSTRAINT [PK_userCredentials] PRIMARY KEY CLUSTERED ([userId]),
    CONSTRAINT [CK_userCredentials_failedAttempts] CHECK ([failedAttempts]>=(0)),
    CONSTRAINT [CK_userCredentials_passwordHash] CHECK (len(ltrim(rtrim([passwordHash])))>(0))
);
GO

CREATE TABLE [security].[users] (
    [userId] int IDENTITY(1,1) NOT NULL,
    [userGuid] uniqueidentifier NOT NULL CONSTRAINT [DF_users_userGuid] DEFAULT (newid()),
    [email] varchar(254) NOT NULL,
    [firstName] nvarchar(100) NOT NULL,
    [secondName] nvarchar(100) NULL,
    [lastName] nvarchar(100) NOT NULL,
    [secondLastName] nvarchar(100) NULL,
    [phoneNumber] varchar(20) NULL,
    [emailConfirmed] bit NOT NULL CONSTRAINT [DF_users_emailConfirmed] DEFAULT ((0)),
    [phoneConfirmed] bit NOT NULL CONSTRAINT [DF_users_phoneConfirmed] DEFAULT ((0)),
    [lastLoginAt] datetime2(7) NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_users_isActive] DEFAULT ((1)),
    [isDeleted] bit NOT NULL CONSTRAINT [DF_users_isDeleted] DEFAULT ((0)),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_users_createdAt] DEFAULT (sysdatetime()),
    [createdBy] varchar(50) NOT NULL CONSTRAINT [DF_users_createdBy] DEFAULT (suser_sname()),
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(50) NULL,
    [isPlatformAdmin] bit NOT NULL CONSTRAINT [DF_users_isPlatformAdmin] DEFAULT ((0)),
    [recoveryEmail] varchar(254) NULL,
    [recoveryPhone] varchar(25) NULL,
    CONSTRAINT [PK_users] PRIMARY KEY CLUSTERED ([userId]),
    CONSTRAINT [UQ_users_userGuid] UNIQUE NONCLUSTERED ([userGuid]),
    CONSTRAINT [CK_users_email] CHECK (len(ltrim(rtrim([email])))>(0) AND [email] like '%_@_%._%'),
    CONSTRAINT [CK_users_firstName] CHECK (len(ltrim(rtrim([firstName])))>(0)),
    CONSTRAINT [CK_users_lastName] CHECK (len(ltrim(rtrim([lastName])))>(0)),
    CONSTRAINT [CK_users_recoveryEmail] CHECK ([recoveryEmail] IS NULL OR [recoveryEmail] like '%_@_%._%')
);
GO

-- ── Índices ──

CREATE NONCLUSTERED INDEX [IX_cases_caseStatusId] ON [documents].[cases] ([caseStatusId]);
GO

CREATE NONCLUSTERED INDEX [IX_cases_tenantId] ON [documents].[cases] ([tenantId]);
GO

CREATE UNIQUE NONCLUSTERED INDEX [UX_cases_tenant_externalRef] ON [documents].[cases] ([tenantId], [externalRef]) WHERE ([externalRef] IS NOT NULL);
GO

CREATE NONCLUSTERED INDEX [IX_documents_documentStatusId] ON [documents].[documents] ([documentStatusId]);
GO

CREATE NONCLUSTERED INDEX [IX_documents_documentTypeId] ON [documents].[documents] ([documentTypeId]);
GO

CREATE NONCLUSTERED INDEX [IX_documents_fileId] ON [documents].[documents] ([fileId]);
GO

CREATE NONCLUSTERED INDEX [IX_documents_tenantId] ON [documents].[documents] ([tenantId]);
GO

CREATE NONCLUSTERED INDEX [IX_entityFacts_confidenceMethodId] ON [documents].[entityFacts] ([confidenceMethodId]);
GO

CREATE NONCLUSTERED INDEX [IX_entityFacts_extractionRunId] ON [documents].[entityFacts] ([extractionRunId]);
GO

CREATE NONCLUSTERED INDEX [IX_entityFacts_fieldDefinitionId] ON [documents].[entityFacts] ([fieldDefinitionId]);
GO

CREATE NONCLUSTERED INDEX [IX_entityFacts_nullReasonId] ON [documents].[entityFacts] ([nullReasonId]);
GO

CREATE NONCLUSTERED INDEX [IX_entityFacts_ocrBlockId] ON [documents].[entityFacts] ([ocrBlockId]);
GO

CREATE NONCLUSTERED INDEX [IX_entityFacts_tenantId] ON [documents].[entityFacts] ([tenantId]);
GO

CREATE NONCLUSTERED INDEX [IX_entityFactSources_ocrBlockId] ON [documents].[entityFactSources] ([ocrBlockId]);
GO

CREATE NONCLUSTERED INDEX [IX_extractionRuns_configVersionId] ON [documents].[extractionRuns] ([configVersionId]);
GO

CREATE NONCLUSTERED INDEX [IX_extractionRuns_documentId] ON [documents].[extractionRuns] ([documentId]);
GO

CREATE NONCLUSTERED INDEX [IX_extractionRuns_extractionEngineId] ON [documents].[extractionRuns] ([extractionEngineId]);
GO

CREATE NONCLUSTERED INDEX [IX_extractionRuns_ocrResultId] ON [documents].[extractionRuns] ([ocrResultId]);
GO

CREATE NONCLUSTERED INDEX [IX_extractionRuns_runStatusId] ON [documents].[extractionRuns] ([runStatusId]);
GO

CREATE NONCLUSTERED INDEX [IX_extractionRuns_tenantId] ON [documents].[extractionRuns] ([tenantId]);
GO

CREATE NONCLUSTERED INDEX [IX_fieldCorrections_correctedBy] ON [documents].[fieldCorrections] ([correctedBy]);
GO

CREATE NONCLUSTERED INDEX [IX_fieldCorrections_correctionReasonId] ON [documents].[fieldCorrections] ([correctionReasonId]);
GO

CREATE NONCLUSTERED INDEX [IX_fieldCorrections_entityFactId] ON [documents].[fieldCorrections] ([entityFactId]);
GO

CREATE NONCLUSTERED INDEX [IX_fieldCorrections_tenantId] ON [documents].[fieldCorrections] ([tenantId]);
GO

CREATE NONCLUSTERED INDEX [IX_fileEvents_file_last] ON [documents].[fileEvents] ([fileId], [fileEventId] DESC) INCLUDE ([fileEventTypeId], [occurredAt], [reason]);
GO

CREATE NONCLUSTERED INDEX [IX_files_tenant_receivedAt] ON [documents].[files] ([tenantId], [receivedAt], [fileId]) INCLUDE ([ingestionChannelId], [fileValidationStatusId], [sha256]);
GO

CREATE NONCLUSTERED INDEX [IX_files_tenant_sha256] ON [documents].[files] ([tenantId], [sha256]) INCLUDE ([receivedAt]);
GO

CREATE NONCLUSTERED INDEX [IX_ocrBlocks_ocrResultId] ON [documents].[ocrBlocks] ([ocrResultId]);
GO

CREATE NONCLUSTERED INDEX [IX_ocrResults_documentId] ON [documents].[ocrResults] ([documentId]);
GO

CREATE NONCLUSTERED INDEX [IX_ocrResults_ocrEngineId] ON [documents].[ocrResults] ([ocrEngineId]);
GO

CREATE NONCLUSTERED INDEX [IX_ocrResults_runStatusId] ON [documents].[ocrResults] ([runStatusId]);
GO

CREATE NONCLUSTERED INDEX [IX_ocrResults_tenantId] ON [documents].[ocrResults] ([tenantId]);
GO

CREATE NONCLUSTERED INDEX [IX_accessTokens_user_kind_pending] ON [security].[accessTokens] ([userId], [kind]) WHERE ([usedAt] IS NULL AND [invalidatedAt] IS NULL);
GO

CREATE NONCLUSTERED INDEX [IX_auditEvents_action_createdAt] ON [security].[auditEvents] ([action], [createdAt]);
GO

CREATE NONCLUSTERED INDEX [IX_auditEvents_tenant_createdAt] ON [security].[auditEvents] ([tenantId], [createdAt] DESC) WHERE ([tenantId] IS NOT NULL);
GO

CREATE NONCLUSTERED INDEX [IX_auditEvents_user_createdAt] ON [security].[auditEvents] ([userId], [createdAt] DESC) WHERE ([userId] IS NOT NULL);
GO

CREATE NONCLUSTERED INDEX [IX_loginAttempts_email_attemptedAt] ON [security].[loginAttempts] ([email], [attemptedAt] DESC);
GO

CREATE NONCLUSTERED INDEX [IX_loginAttempts_origin_attemptedAt] ON [security].[loginAttempts] ([originId], [attemptedAt]) INCLUDE ([succeeded], [failureReason]) WHERE ([originId] IS NOT NULL);
GO

CREATE NONCLUSTERED INDEX [IX_loginAttempts_user_attemptedAt] ON [security].[loginAttempts] ([userId], [attemptedAt] DESC) WHERE ([userId] IS NOT NULL);
GO

CREATE NONCLUSTERED INDEX [IX_sessions_user_active] ON [security].[sessions] ([userId]) INCLUDE ([expiresAt]) WHERE ([revokedAt] IS NULL);
GO

CREATE NONCLUSTERED INDEX [IX_tenantMemberships_role] ON [security].[tenantMemberships] ([roleId]);
GO

CREATE NONCLUSTERED INDEX [IX_tenantMemberships_user] ON [security].[tenantMemberships] ([userId], [isActive], [isDeleted]);
GO

CREATE UNIQUE NONCLUSTERED INDEX [UX_tenantMemberships_user] ON [security].[tenantMemberships] ([userId]) WHERE ([isDeleted]=(0));
GO

CREATE NONCLUSTERED INDEX [IX_tenants_status] ON [security].[tenants] ([tenantStatusId], [isActive], [isDeleted]);
GO

CREATE UNIQUE NONCLUSTERED INDEX [UX_tenantSequence_singleActive] ON [security].[tenantSequence] ([isActive]) WHERE ([isActive]=(1));
GO

CREATE UNIQUE NONCLUSTERED INDEX [UX_users_email] ON [security].[users] ([email]) WHERE ([isDeleted]=(0));
GO

-- ── Llaves foráneas ──

ALTER TABLE [documents].[cases] ADD CONSTRAINT [FK_cases_caseStatuses] FOREIGN KEY ([caseStatusId]) REFERENCES [reference].[caseStatuses] ([caseStatusId]);
GO

ALTER TABLE [documents].[cases] ADD CONSTRAINT [FK_cases_tenants] FOREIGN KEY ([tenantId]) REFERENCES [security].[tenants] ([tenantId]);
GO

ALTER TABLE [documents].[documents] ADD CONSTRAINT [FK_documents_documentStatuses] FOREIGN KEY ([documentStatusId]) REFERENCES [reference].[documentStatuses] ([documentStatusId]);
GO

ALTER TABLE [documents].[documents] ADD CONSTRAINT [FK_documents_documentTypes] FOREIGN KEY ([documentTypeId]) REFERENCES [reference].[documentTypes] ([documentTypeId]);
GO

ALTER TABLE [documents].[documents] ADD CONSTRAINT [FK_documents_files] FOREIGN KEY ([fileId]) REFERENCES [documents].[files] ([fileId]);
GO

ALTER TABLE [documents].[documents] ADD CONSTRAINT [FK_documents_tenants] FOREIGN KEY ([tenantId]) REFERENCES [security].[tenants] ([tenantId]);
GO

ALTER TABLE [documents].[entityFacts] ADD CONSTRAINT [FK_entityFacts_confidenceMethods] FOREIGN KEY ([confidenceMethodId]) REFERENCES [reference].[confidenceMethods] ([confidenceMethodId]);
GO

ALTER TABLE [documents].[entityFacts] ADD CONSTRAINT [FK_entityFacts_extractionRuns] FOREIGN KEY ([extractionRunId]) REFERENCES [documents].[extractionRuns] ([extractionRunId]);
GO

ALTER TABLE [documents].[entityFacts] ADD CONSTRAINT [FK_entityFacts_nullReasons] FOREIGN KEY ([nullReasonId]) REFERENCES [reference].[nullReasons] ([nullReasonId]);
GO

ALTER TABLE [documents].[entityFacts] ADD CONSTRAINT [FK_entityFacts_ocrBlocks] FOREIGN KEY ([ocrBlockId]) REFERENCES [documents].[ocrBlocks] ([ocrBlockId]);
GO

ALTER TABLE [documents].[entityFacts] ADD CONSTRAINT [FK_entityFacts_tenants] FOREIGN KEY ([tenantId]) REFERENCES [security].[tenants] ([tenantId]);
GO

ALTER TABLE [documents].[entityFactSources] ADD CONSTRAINT [FK_entityFactSources_entityFacts] FOREIGN KEY ([entityFactId]) REFERENCES [documents].[entityFacts] ([entityFactId]);
GO

ALTER TABLE [documents].[entityFactSources] ADD CONSTRAINT [FK_entityFactSources_ocrBlocks] FOREIGN KEY ([ocrBlockId]) REFERENCES [documents].[ocrBlocks] ([ocrBlockId]);
GO

ALTER TABLE [documents].[extractionRuns] ADD CONSTRAINT [FK_extractionRuns_documents] FOREIGN KEY ([documentId]) REFERENCES [documents].[documents] ([documentId]);
GO

ALTER TABLE [documents].[extractionRuns] ADD CONSTRAINT [FK_extractionRuns_extractionEngines] FOREIGN KEY ([extractionEngineId]) REFERENCES [reference].[extractionEngines] ([extractionEngineId]);
GO

ALTER TABLE [documents].[extractionRuns] ADD CONSTRAINT [FK_extractionRuns_ocrResults] FOREIGN KEY ([ocrResultId]) REFERENCES [documents].[ocrResults] ([ocrResultId]);
GO

ALTER TABLE [documents].[extractionRuns] ADD CONSTRAINT [FK_extractionRuns_runStatuses] FOREIGN KEY ([runStatusId]) REFERENCES [reference].[runStatuses] ([runStatusId]);
GO

ALTER TABLE [documents].[extractionRuns] ADD CONSTRAINT [FK_extractionRuns_tenants] FOREIGN KEY ([tenantId]) REFERENCES [security].[tenants] ([tenantId]);
GO

ALTER TABLE [documents].[fieldCorrections] ADD CONSTRAINT [FK_fieldCorrections_correctionReasons] FOREIGN KEY ([correctionReasonId]) REFERENCES [reference].[correctionReasons] ([correctionReasonId]);
GO

ALTER TABLE [documents].[fieldCorrections] ADD CONSTRAINT [FK_fieldCorrections_entityFacts] FOREIGN KEY ([entityFactId]) REFERENCES [documents].[entityFacts] ([entityFactId]);
GO

ALTER TABLE [documents].[fieldCorrections] ADD CONSTRAINT [FK_fieldCorrections_tenants] FOREIGN KEY ([tenantId]) REFERENCES [security].[tenants] ([tenantId]);
GO

ALTER TABLE [documents].[fieldCorrections] ADD CONSTRAINT [FK_fieldCorrections_users] FOREIGN KEY ([correctedBy]) REFERENCES [security].[users] ([userId]);
GO

ALTER TABLE [documents].[fileEvents] ADD CONSTRAINT [FK_fileEvents_fileEventTypes] FOREIGN KEY ([fileEventTypeId]) REFERENCES [reference].[fileEventTypes] ([fileEventTypeId]);
GO

ALTER TABLE [documents].[fileEvents] ADD CONSTRAINT [FK_fileEvents_files] FOREIGN KEY ([fileId]) REFERENCES [documents].[files] ([fileId]);
GO

ALTER TABLE [documents].[fileEvents] ADD CONSTRAINT [FK_fileEvents_users] FOREIGN KEY ([byUserId]) REFERENCES [security].[users] ([userId]);
GO

ALTER TABLE [documents].[files] ADD CONSTRAINT [FK_files_cases] FOREIGN KEY ([caseId]) REFERENCES [documents].[cases] ([caseId]);
GO

ALTER TABLE [documents].[files] ADD CONSTRAINT [FK_files_fileValidationStatuses] FOREIGN KEY ([fileValidationStatusId]) REFERENCES [reference].[fileValidationStatuses] ([fileValidationStatusId]);
GO

ALTER TABLE [documents].[files] ADD CONSTRAINT [FK_files_ingestionChannels] FOREIGN KEY ([ingestionChannelId]) REFERENCES [reference].[ingestionChannels] ([ingestionChannelId]);
GO

ALTER TABLE [documents].[files] ADD CONSTRAINT [FK_files_tenants] FOREIGN KEY ([tenantId]) REFERENCES [security].[tenants] ([tenantId]);
GO

ALTER TABLE [documents].[ocrBlocks] ADD CONSTRAINT [FK_ocrBlocks_blockTypes] FOREIGN KEY ([blockTypeId]) REFERENCES [reference].[blockTypes] ([blockTypeId]);
GO

ALTER TABLE [documents].[ocrBlocks] ADD CONSTRAINT [FK_ocrBlocks_ocrResults] FOREIGN KEY ([ocrResultId]) REFERENCES [documents].[ocrResults] ([ocrResultId]);
GO

ALTER TABLE [documents].[ocrResults] ADD CONSTRAINT [FK_ocrResults_documents] FOREIGN KEY ([documentId]) REFERENCES [documents].[documents] ([documentId]);
GO

ALTER TABLE [documents].[ocrResults] ADD CONSTRAINT [FK_ocrResults_ocrEngines] FOREIGN KEY ([ocrEngineId]) REFERENCES [reference].[ocrEngines] ([ocrEngineId]);
GO

ALTER TABLE [documents].[ocrResults] ADD CONSTRAINT [FK_ocrResults_runStatuses] FOREIGN KEY ([runStatusId]) REFERENCES [reference].[runStatuses] ([runStatusId]);
GO

ALTER TABLE [documents].[ocrResults] ADD CONSTRAINT [FK_ocrResults_tenants] FOREIGN KEY ([tenantId]) REFERENCES [security].[tenants] ([tenantId]);
GO

ALTER TABLE [reference].[documentTypes] ADD CONSTRAINT [FK_documentTypes_documentTypeStatuses] FOREIGN KEY ([documentTypeStatusId]) REFERENCES [reference].[documentTypeStatuses] ([documentTypeStatusId]);
GO

ALTER TABLE [reference].[documentTypes] ADD CONSTRAINT [FK_documentTypes_tenants] FOREIGN KEY ([tenantId]) REFERENCES [security].[tenants] ([tenantId]);
GO

ALTER TABLE [security].[accessTokens] ADD CONSTRAINT [FK_accessTokens_users] FOREIGN KEY ([userId]) REFERENCES [security].[users] ([userId]);
GO

ALTER TABLE [security].[auditEvents] ADD CONSTRAINT [FK_auditEvents_actor] FOREIGN KEY ([actorUserId]) REFERENCES [security].[users] ([userId]);
GO

ALTER TABLE [security].[auditEvents] ADD CONSTRAINT [FK_auditEvents_tenants] FOREIGN KEY ([tenantId]) REFERENCES [security].[tenants] ([tenantId]);
GO

ALTER TABLE [security].[auditEvents] ADD CONSTRAINT [FK_auditEvents_users] FOREIGN KEY ([userId]) REFERENCES [security].[users] ([userId]);
GO

ALTER TABLE [security].[loginAttempts] ADD CONSTRAINT [FK_loginAttempts_users] FOREIGN KEY ([userId]) REFERENCES [security].[users] ([userId]);
GO

ALTER TABLE [security].[sessions] ADD CONSTRAINT [FK_sessions_users] FOREIGN KEY ([userId]) REFERENCES [security].[users] ([userId]);
GO

ALTER TABLE [security].[tenantMemberships] ADD CONSTRAINT [FK_tenantMemberships_roles] FOREIGN KEY ([roleId]) REFERENCES [security].[roles] ([roleId]);
GO

ALTER TABLE [security].[tenantMemberships] ADD CONSTRAINT [FK_tenantMemberships_tenants] FOREIGN KEY ([tenantId]) REFERENCES [security].[tenants] ([tenantId]);
GO

ALTER TABLE [security].[tenantMemberships] ADD CONSTRAINT [FK_tenantMemberships_users] FOREIGN KEY ([userId]) REFERENCES [security].[users] ([userId]);
GO

ALTER TABLE [security].[tenants] ADD CONSTRAINT [FK_tenants_tenantStatuses] FOREIGN KEY ([tenantStatusId]) REFERENCES [reference].[tenantStatuses] ([tenantStatusId]);
GO

ALTER TABLE [security].[userCredentials] ADD CONSTRAINT [FK_userCredentials_users] FOREIGN KEY ([userId]) REFERENCES [security].[users] ([userId]);
GO

-- ── Catálogos y configuración (con sus ids: otras tablas los referencian) ──

SET IDENTITY_INSERT [reference].[blockTypes] ON;
INSERT INTO [reference].[blockTypes] ([blockTypeId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'PARAGRAPH', N'PÃ¡rrafo', NULL, 1, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3441020', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[blockTypes] ([blockTypeId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'LINE', N'LÃ­nea', NULL, 2, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3441020', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[blockTypes] ([blockTypeId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (3, N'WORD', N'Palabra', NULL, 3, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3441020', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[blockTypes] ([blockTypeId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (4, N'TABLE_CELL', N'Celda de tabla', NULL, 4, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3441020', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[blockTypes] ([blockTypeId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (5, N'SIGNATURE', N'Firma', NULL, 5, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3441020', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[blockTypes] ([blockTypeId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (6, N'OTHER', N'Otro', NULL, 6, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3441020', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [reference].[blockTypes] OFF;
GO

SET IDENTITY_INSERT [reference].[caseStatuses] ON;
INSERT INTO [reference].[caseStatuses] ([caseStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'OPEN', N'Abierto', NULL, 1, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.2502760', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[caseStatuses] ([caseStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'PROCESSING', N'En procesamiento', NULL, 2, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.2502760', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[caseStatuses] ([caseStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (3, N'READY_FOR_REVIEW', N'Listo para revisiÃ³n', NULL, 3, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.2502760', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[caseStatuses] ([caseStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (4, N'DECIDED', N'Decidido', NULL, 4, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.2502760', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[caseStatuses] ([caseStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (5, N'CLOSED', N'Cerrado', NULL, 5, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.2502760', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[caseStatuses] ([caseStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (6, N'CANCELLED', N'Cancelado', NULL, 6, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.2502760', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [reference].[caseStatuses] OFF;
GO

SET IDENTITY_INSERT [reference].[confidenceMethods] ON;
INSERT INTO [reference].[confidenceMethods] ([confidenceMethodId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'LOGPROB_RESCALED', N'Logprob reescalado', NULL, 1, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.4222241', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[confidenceMethods] ([confidenceMethodId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'EXTRACTOR_CONFIDENCE', N'Confianza del extractor', NULL, 2, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.4222241', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[confidenceMethods] ([confidenceMethodId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (3, N'COMPOSITE', N'Compuesto', NULL, 3, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.4222241', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [reference].[confidenceMethods] OFF;
GO

SET IDENTITY_INSERT [reference].[correctionReasons] ON;
INSERT INTO [reference].[correctionReasons] ([correctionReasonId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'WRONG_VALUE', N'Valor incorrecto', NULL, 1, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.4691008', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[correctionReasons] ([correctionReasonId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'WRONG_SOURCE', N'Fuente incorrecta', NULL, 2, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.4691008', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[correctionReasons] ([correctionReasonId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (3, N'NOT_PRESENT_CONFIRMED', N'Confirmado que no existe en el documento', NULL, 3, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.4691008', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[correctionReasons] ([correctionReasonId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (4, N'FORMAT_FIX', N'CorrecciÃ³n de formato', NULL, 4, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.4691008', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [reference].[correctionReasons] OFF;
GO

SET IDENTITY_INSERT [reference].[documentStatuses] ON;
INSERT INTO [reference].[documentStatuses] ([documentStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'PENDING', N'Pendiente', NULL, 1, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.2816053', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[documentStatuses] ([documentStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'CLASSIFIED', N'Clasificado', NULL, 2, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.2816053', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[documentStatuses] ([documentStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (3, N'EXTRACTED', N'ExtraÃ­do', NULL, 3, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.2816053', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[documentStatuses] ([documentStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (4, N'FAILED', N'Fallido', NULL, 4, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.2816053', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [reference].[documentStatuses] OFF;
GO

SET IDENTITY_INSERT [reference].[documentTypeStatuses] ON;
INSERT INTO [reference].[documentTypeStatuses] ([documentTypeStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'ACTIVE', N'Active', N'Document type available for classification and processing.', 1, 1, 0, CONVERT(datetime2(7), '2026-08-31 11:05:14.5559749', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[documentTypeStatuses] ([documentTypeStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'RETIRED', N'Retired', N'Document type no longer available for new processing.', 2, 1, 0, CONVERT(datetime2(7), '2026-08-31 11:05:14.5559749', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [reference].[documentTypeStatuses] OFF;
GO

SET IDENTITY_INSERT [reference].[extractionEngines] ON;
INSERT INTO [reference].[extractionEngines] ([extractionEngineId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'AZURE_OPENAI', N'Azure OpenAI', NULL, 1, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3909806', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[extractionEngines] ([extractionEngineId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'DOCUMENT_AI_EXTRACTOR', N'Document AI Extractor', NULL, 2, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3909806', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[extractionEngines] ([extractionEngineId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (3, N'OTRO', N'Otro (ADR de motor de extracciÃ³n abierto)', NULL, 3, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3909806', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [reference].[extractionEngines] OFF;
GO

SET IDENTITY_INSERT [reference].[fileEventTypes] ON;
INSERT INTO [reference].[fileEventTypes] ([fileEventTypeId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'SENT_TO_PIPELINE', N'Enviado al pipeline', N'The file left the preparation inbox and was sent to the cognitive pipeline.', 1, 1, 0, CONVERT(datetime2(7), '2026-10-19 11:14:41.7824394', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[fileEventTypes] ([fileEventTypeId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'DISCARDED', N'Descartado', N'The file was discarded from the preparation inbox by a person.', 2, 1, 0, CONVERT(datetime2(7), '2026-10-19 11:14:41.7824394', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[fileEventTypes] ([fileEventTypeId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (3, N'RESTORED', N'Recuperado', N'Reserved: a discarded file returned to the preparation inbox.', 3, 1, 0, CONVERT(datetime2(7), '2026-10-19 11:14:41.7824394', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [reference].[fileEventTypes] OFF;
GO

SET IDENTITY_INSERT [reference].[fileValidationStatuses] ON;
INSERT INTO [reference].[fileValidationStatuses] ([fileValidationStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'READY', N'Listo', N'The file passed the technical validation and can go to the pipeline.', 1, 1, 0, CONVERT(datetime2(7), '2026-10-19 11:14:41.7511830', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[fileValidationStatuses] ([fileValidationStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'PASSWORD_PROTECTED', N'Protegido con contraseña', N'The file is password protected and cannot be read.', 2, 1, 0, CONVERT(datetime2(7), '2026-10-19 11:14:41.7511830', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[fileValidationStatuses] ([fileValidationStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (3, N'CORRUPT', N'Corrupto', N'The file is damaged or its structure cannot be read.', 3, 1, 0, CONVERT(datetime2(7), '2026-10-19 11:14:41.7511830', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [reference].[fileValidationStatuses] OFF;
GO

SET IDENTITY_INSERT [reference].[ingestionChannels] ON;
INSERT INTO [reference].[ingestionChannels] ([ingestionChannelId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'MANUAL', N'Manual', N'File uploaded manually by a user.', 1, 1, 0, CONVERT(datetime2(7), '2026-08-31 11:04:50.1497319', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[ingestionChannels] ([ingestionChannelId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'SCRIPT', N'Script', N'File received through an automated script.', 2, 1, 0, CONVERT(datetime2(7), '2026-08-31 11:04:50.1497319', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[ingestionChannels] ([ingestionChannelId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (3, N'SFTP', N'SFTP', N'File received through an SFTP connection.', 3, 1, 0, CONVERT(datetime2(7), '2026-08-31 11:04:50.1497319', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[ingestionChannels] ([ingestionChannelId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (4, N'SHAREPOINT', N'SharePoint', N'File received from SharePoint.', 4, 1, 0, CONVERT(datetime2(7), '2026-08-31 11:04:50.1497319', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[ingestionChannels] ([ingestionChannelId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (5, N'API', N'API', N'File received through an application interface.', 5, 1, 0, CONVERT(datetime2(7), '2026-08-31 11:04:50.1497319', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[ingestionChannels] ([ingestionChannelId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (6, N'ART', N'ART', N'File received from an ART integration.', 6, 1, 0, CONVERT(datetime2(7), '2026-08-31 11:04:50.1497319', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [reference].[ingestionChannels] OFF;
GO

SET IDENTITY_INSERT [reference].[nullReasons] ON;
INSERT INTO [reference].[nullReasons] ([nullReasonId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'NOT_PRESENT', N'No presente', NULL, 1, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.4378541', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[nullReasons] ([nullReasonId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'ILLEGIBLE', N'Ilegible', NULL, 2, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.4378541', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[nullReasons] ([nullReasonId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (3, N'BELOW_THRESHOLD', N'Bajo el umbral de confianza', NULL, 3, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.4378541', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[nullReasons] ([nullReasonId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (4, N'EXTRACTION_ERROR', N'Error de extracciÃ³n', NULL, 4, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.4378541', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [reference].[nullReasons] OFF;
GO

SET IDENTITY_INSERT [reference].[ocrEngines] ON;
INSERT INTO [reference].[ocrEngines] ([ocrEngineId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'DOCUMENT_AI', N'Document AI', NULL, 1, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3128500', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[ocrEngines] ([ocrEngineId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'AZURE_OCR', N'Azure OCR', NULL, 2, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3128500', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[ocrEngines] ([ocrEngineId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (3, N'NATIVE_PDF', N'Texto nativo de PDF (sin OCR)', NULL, 3, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3128500', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [reference].[ocrEngines] OFF;
GO

SET IDENTITY_INSERT [reference].[runStatuses] ON;
INSERT INTO [reference].[runStatuses] ([runStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'SUCCEEDED', N'Exitoso', NULL, 1, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3597286', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[runStatuses] ([runStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'FAILED', N'Fallido', NULL, 2, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3597286', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[runStatuses] ([runStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (3, N'PARTIAL', N'Parcial', NULL, 3, 1, 0, CONVERT(datetime2(7), '2026-09-22 11:29:47.3597286', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [reference].[runStatuses] OFF;
GO

SET IDENTITY_INSERT [reference].[tenantStatuses] ON;
INSERT INTO [reference].[tenantStatuses] ([tenantStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'ACTIVE', N'Active', N'Tenant enabled for normal platform operation.', 1, 1, 0, CONVERT(datetime2(7), '2026-08-26 09:44:25.5065139', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[tenantStatuses] ([tenantStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'SUSPENDED', N'Suspended', N'Tenant temporarily blocked from operating.', 2, 1, 0, CONVERT(datetime2(7), '2026-08-26 09:44:25.5065139', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [reference].[tenantStatuses] ([tenantStatusId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (3, N'CLOSED', N'Closed', N'Tenant permanently closed.', 3, 1, 0, CONVERT(datetime2(7), '2026-08-26 09:44:25.5065139', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [reference].[tenantStatuses] OFF;
GO

SET IDENTITY_INSERT [security].[roles] ON;
INSERT INTO [security].[roles] ([roleId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'ADMIN', N'Administrator', N'Full administration of the tenant.', 1, 1, 0, CONVERT(datetime2(7), '2026-08-26 10:08:42.3502612', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [security].[roles] ([roleId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (2, N'CONFIGURATOR', N'Configurator', N'Manages document configuration and business rules.', 2, 0, 0, CONVERT(datetime2(7), '2026-08-26 10:08:42.3502612', 121), N'PROCHURGRUPOCSI\carlos.ramirez', CONVERT(datetime2(7), '2026-10-14 11:33:09.1388206', 121), N'PROCHURGRUPOCSI\carlos.ramirez');
INSERT INTO [security].[roles] ([roleId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (3, N'REVIEWER', N'Reviewer', N'Reviews extracted information and case results.', 3, 0, 0, CONVERT(datetime2(7), '2026-08-26 10:08:42.3502612', 121), N'PROCHURGRUPOCSI\carlos.ramirez', CONVERT(datetime2(7), '2026-10-14 11:33:09.1388206', 121), N'PROCHURGRUPOCSI\carlos.ramirez');
INSERT INTO [security].[roles] ([roleId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (4, N'QUERY', N'Query', N'Read-only access to authorized tenant information.', 4, 0, 0, CONVERT(datetime2(7), '2026-08-26 10:08:42.3502612', 121), N'PROCHURGRUPOCSI\carlos.ramirez', CONVERT(datetime2(7), '2026-10-14 11:33:09.1388206', 121), N'PROCHURGRUPOCSI\carlos.ramirez');
INSERT INTO [security].[roles] ([roleId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (5, N'SUPERVISOR', N'Supervisor', N'Monitorea cola HITL, métricas del equipo, reasigna casos, panel de calidad del pipeline, ve log de actividad de otros usuarios, marca alertas como revisadas.', 2, 1, 0, CONVERT(datetime2(7), '2026-10-14 11:33:09.1388206', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [security].[roles] ([roleId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (6, N'ANALISTA', N'Analista', N'Revisa cola HITL, corrige campos, resuelve casos estándar, consulta expediente, búsqueda semántica, exporta resultados de búsqueda.', 3, 1, 0, CONVERT(datetime2(7), '2026-10-14 11:33:09.1388206', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [security].[roles] ([roleId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (7, N'OPERADOR', N'Operador', N'Carga documentos manualmente, edita metadata básica, descarga archivo original, consulta bandeja de documentos.', 4, 1, 0, CONVERT(datetime2(7), '2026-10-14 11:33:09.1388206', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [security].[roles] ([roleId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (8, N'COMPLIANCE_OFFICER', N'Compliance Officer', N'Resuelve casos de alerta PLD, atiende solicitudes ARCO, ve campos sin enmascarar, exporta reportes de auditoría, ve eventos PLD del audit trail.', 5, 1, 0, CONVERT(datetime2(7), '2026-10-14 11:33:09.1388206', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [security].[roles] ([roleId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (9, N'AUDITOR', N'Auditor', N'Revisa trazabilidad de decisiones, línea de tiempo de auditoría (eventos públicos), ve OT-01 y OT-02 sin poder descargar, exporta reportes de auditoría.', 6, 1, 0, CONVERT(datetime2(7), '2026-10-14 11:33:09.1388206', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
INSERT INTO [security].[roles] ([roleId], [code], [name], [description], [sortOrder], [isActive], [isDeleted], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (10, N'VIEWER', N'Viewer', N'Consulta OT-01 y OT-02 en modo lectura sin descarga, ve historial de firmas y línea de tiempo pública.', 7, 1, 0, CONVERT(datetime2(7), '2026-10-14 11:33:09.1388206', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [security].[roles] OFF;
GO

SET IDENTITY_INSERT [security].[tenantSequence] ON;
INSERT INTO [security].[tenantSequence] ([tenantSequenceId], [prefix], [lastSequence], [isActive], [createdAt], [createdBy], [updatedAt], [updatedBy]) VALUES (1, N'NEX', 0, 1, CONVERT(datetime2(7), '2026-08-31 11:00:15.6497287', 121), N'PROCHURGRUPOCSI\carlos.ramirez', NULL, NULL);
SET IDENTITY_INSERT [security].[tenantSequence] OFF;
GO

-- ── Funciones y procedimientos (congelados: la especificación del dominio) ──

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   0) documents.ufnInboxFiles
   Una fila por archivo del tenant con su estado en la bandeja:
   - lastEventCode: código del último evento (NULL = sin eventos).
   - isDuplicate: hay otro archivo del MISMO tenant con la misma huella,
     que llegó antes y que no está descartado.
   - inboxStatus, en este orden:
       SENT_TO_PIPELINE  si el último evento es SENT_TO_PIPELINE
       DISCARDED         si el último evento es DISCARDED
       PENDING           si la validación es NULL, PASSWORD_PROTECTED o CORRUPT
       DUPLICATE         si isDuplicate
       READY             en otro caso
     RESTORED (reservado) devuelve el archivo a la bandeja.
   ===================================================================== */
CREATE   FUNCTION [documents].[ufnInboxFiles]
(
    @tenantId INT
)
RETURNS TABLE
AS
RETURN
(
    SELECT
        f.[fileId],
        f.[fileGuid],
        f.[originalFileName],
        f.[mimeType],
        f.[sizeBytes],
        f.[sha256],
        f.[storageUri],
        ic.[code]          AS [ingestionChannelCode],
        f.[receivedAt],
        vs.[code]          AS [validationCode],
        f.[ingestionSourceRef],
        le.[eventCode]     AS [lastEventCode],
        le.[occurredAt]    AS [lastEventAt],
        le.[reason]        AS [lastEventReason],
        dup.[isDuplicate],
        st.[inboxStatus]
    FROM [documents].[files] f
    INNER JOIN [reference].[ingestionChannels] ic
        ON ic.[ingestionChannelId] = f.[ingestionChannelId]
    LEFT JOIN [reference].[fileValidationStatuses] vs
        ON vs.[fileValidationStatusId] = f.[fileValidationStatusId]
    OUTER APPLY
    (
        SELECT TOP (1)
            et.[code] AS [eventCode],
            e.[occurredAt],
            e.[reason]
        FROM [documents].[fileEvents] e
        INNER JOIN [reference].[fileEventTypes] et
            ON et.[fileEventTypeId] = e.[fileEventTypeId]
        WHERE e.[fileId] = f.[fileId]
        ORDER BY e.[fileEventId] DESC
    ) le
    CROSS APPLY
    (
        SELECT CAST(CASE WHEN EXISTS
        (
            SELECT 1
            FROM [documents].[files] d
            WHERE d.[tenantId] = f.[tenantId]
              AND d.[sha256]   = f.[sha256]
              AND (d.[receivedAt] < f.[receivedAt]
                   OR (d.[receivedAt] = f.[receivedAt] AND d.[fileId] < f.[fileId]))
              AND ISNULL
                  (
                      (
                          SELECT TOP (1) det.[code]
                          FROM [documents].[fileEvents] de
                          INNER JOIN [reference].[fileEventTypes] det
                              ON det.[fileEventTypeId] = de.[fileEventTypeId]
                          WHERE de.[fileId] = d.[fileId]
                          ORDER BY de.[fileEventId] DESC
                      ),
                      ''
                  ) <> 'DISCARDED'
        ) THEN 1 ELSE 0 END AS BIT) AS [isDuplicate]
    ) dup
    CROSS APPLY
    (
        SELECT CASE
                   WHEN le.[eventCode] = 'SENT_TO_PIPELINE'                        THEN 'SENT_TO_PIPELINE'
                   WHEN le.[eventCode] = 'DISCARDED'                               THEN 'DISCARDED'
                   WHEN vs.[code] IS NULL
                        OR vs.[code] IN ('PASSWORD_PROTECTED', 'CORRUPT')          THEN 'PENDING'
                   WHEN dup.[isDuplicate] = 1                                      THEN 'DUPLICATE'
                   ELSE 'READY'
               END AS [inboxStatus]
    ) st
    WHERE f.[tenantId] = @tenantId
);
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   0a) security.ufnGetOriginLockout
   Intentos fallidos SEGUIDOS de un navegador (NEX-319, decisión 9/oct):
   - Cuentan BAD_PASSWORD y UNKNOWN_EMAIL; LOCKED, INACTIVE y EXPIRED no.
   - Un succeeded = 1 reinicia la cuenta.
   - Cada 5 fallos contados: bloqueado 15 min desde el quinto. Mientras
     dura el bloqueo los intentos se anotan como LOCKED y no cuentan, así
     que el siguiente bloqueo solo llega con 5 fallos nuevos.
   Una sola definición para uspGetOriginLockout y uspRecordLoginResult.
   ===================================================================== */
CREATE   FUNCTION [security].[ufnGetOriginLockout]
(
    @originId VARCHAR(64),
    @now      DATETIME2
)
RETURNS TABLE
AS
RETURN
(
    WITH [lastSuccess] AS
    (
        SELECT MAX(la.[attemptedAt]) AS [attemptedAt]
        FROM [security].[loginAttempts] la
        WHERE la.[originId]  = @originId
          AND la.[succeeded] = 1
    ),
    [counted] AS
    (
        SELECT
            COUNT(*)            AS [failures],
            MAX(la.[attemptedAt]) AS [lastFailureAt]
        FROM [security].[loginAttempts] la
        CROSS JOIN [lastSuccess] ls
        WHERE la.[originId]      = @originId
          AND la.[succeeded]     = 0
          AND la.[failureReason] IN ('BAD_PASSWORD', 'UNKNOWN_EMAIL')
          AND (ls.[attemptedAt] IS NULL OR la.[attemptedAt] > ls.[attemptedAt])
    )
    SELECT
        c.[failures] % 5 AS [failedAttempts],
        CASE
            WHEN c.[failures] > 0
             AND c.[failures] % 5 = 0
             AND DATEADD(MINUTE, 15, c.[lastFailureAt]) > @now
                THEN DATEADD(MINUTE, 15, c.[lastFailureAt])
        END AS [lockedUntil]
    FROM [counted] c
);
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   0) security.ufnGetScopedUser
   Una sola definición del filtro de HU11. Devuelve 0 o 1 fila.
   ===================================================================== */
CREATE   FUNCTION [security].[ufnGetScopedUser]
(
    @userGuid   UNIQUEIDENTIFIER,
    @tenantGuid UNIQUEIDENTIFIER
)
RETURNS TABLE
AS
RETURN
(
    SELECT
        u.[userId],
        u.[userGuid],
        u.[email],
        u.[isActive],
        u.[isPlatformAdmin],
        tm.[tenantMembershipId],
        tm.[tenantId],
        r.[code] AS [roleCode]
    FROM [security].[users] u
    LEFT JOIN [security].[tenantMemberships] tm
        ON tm.[userId] = u.[userId]
       AND tm.[isDeleted] = 0
    LEFT JOIN [security].[tenants] t
        ON t.[tenantId] = tm.[tenantId]
       AND t.[isDeleted] = 0
    LEFT JOIN [security].[roles] r
        ON r.[roleId] = tm.[roleId]
    WHERE u.[userGuid]  = @userGuid
      AND u.[isDeleted] = 0
      AND (
               (@tenantGuid IS NULL     AND tm.[tenantMembershipId] IS NULL)
            OR (@tenantGuid IS NOT NULL AND t.[tenantGuid] = @tenantGuid)
          )
);
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   0) security.ufnGetUserActiveMemberships
   Una sola definición de "membresía activa" para los SPs 2 y 6:
   membresía ACTIVE no borrada + tenant ACTIVE no borrado + rol activo.
   ===================================================================== */
CREATE   FUNCTION [security].[ufnGetUserActiveMemberships]
(
    @userId INT
)
RETURNS TABLE
AS
RETURN
(
    SELECT
        t.[tenantGuid],
        t.[tenantCode],
        t.[name],
        t.[slug],
        ts.[code] AS [tenantStatusCode],
        r.[code]  AS [roleCode]
    FROM [security].[tenantMemberships] tm
    INNER JOIN [security].[tenants] t
        ON t.[tenantId] = tm.[tenantId]
    INNER JOIN [reference].[tenantStatuses] ts
        ON ts.[tenantStatusId] = t.[tenantStatusId]
    INNER JOIN [security].[roles] r
        ON r.[roleId] = tm.[roleId]
    WHERE tm.[userId]     = @userId
      AND tm.[statusCode] = 'ACTIVE'
      AND tm.[isActive]   = 1
      AND tm.[isDeleted]  = 0
      AND t.[isActive]    = 1
      AND t.[isDeleted]   = 0
      AND ts.[code]       = 'ACTIVE'
      AND r.[isActive]    = 1
      AND r.[isDeleted]   = 0
);
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   1) documents.uspCreateFile  (NEX-295, ampliado en NEX-377)
   Igual que antes, más dos parámetros OPCIONALES al final, así que no
   se rompe a quien ya lo llama:
   - @validationCode: READY, PASSWORD_PROTECTED o CORRUPT. NULL = sin
     revisar.
   - @sourceRef: de dónde vino (el id de la API Key). Hasta 100.
   ===================================================================== */
CREATE   PROCEDURE [documents].[uspCreateFile]
(
    @tenantGuid           UNIQUEIDENTIFIER,
    @caseGuid             UNIQUEIDENTIFIER,
    @ingestionChannelCode VARCHAR(20),
    @storageUri           NVARCHAR(2048),
    @originalFileName     NVARCHAR(260),
    @mimeType             VARCHAR(100),
    @sizeBytes            BIGINT,
    @sha256               CHAR(64),
    @pageCount            INT           = NULL,
    @validationCode       VARCHAR(20)   = NULL,
    @sourceRef            NVARCHAR(200) = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @tenantId               INT,
        @caseId                 BIGINT,
        @ingestionChannelId     INT,
        @fileValidationStatusId INT,
        @fileId                 BIGINT,
        @fileGuid               UNIQUEIDENTIFIER,
        @userAction             VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SELECT @tenantId = [tenantId]
        FROM [security].[tenants]
        WHERE [tenantGuid] = @tenantGuid
          AND [isDeleted] = 0;

        IF @tenantId IS NULL
            THROW 50001, 'Tenant not found.', 1;

        SELECT @caseId = [caseId]
        FROM [documents].[cases]
        WHERE [caseGuid] = @caseGuid
          AND [tenantId] = @tenantId;

        IF @caseId IS NULL
            THROW 50002, 'Case not found for this tenant.', 1;

        SET @ingestionChannelCode = UPPER(LTRIM(RTRIM(@ingestionChannelCode)));

        SELECT @ingestionChannelId = [ingestionChannelId]
        FROM [reference].[ingestionChannels]
        WHERE [code] = @ingestionChannelCode
          AND [isActive] = 1
          AND [isDeleted] = 0;

        IF @ingestionChannelId IS NULL
            THROW 50003, 'Unknown or inactive ingestionChannelCode.', 1;

        SET @storageUri = NULLIF(LTRIM(RTRIM(@storageUri)), '');
        IF @storageUri IS NULL
            THROW 50004, 'storageUri is required.', 1;

        SET @originalFileName = NULLIF(LTRIM(RTRIM(@originalFileName)), '');
        IF @originalFileName IS NULL
            THROW 50005, 'originalFileName is required.', 1;

        SET @mimeType = NULLIF(LTRIM(RTRIM(@mimeType)), '');
        IF @mimeType IS NULL
            THROW 50006, 'mimeType is required.', 1;

        IF @sha256 IS NULL
           OR LEN(@sha256) <> 64
           OR @sha256 LIKE '%[^0-9A-Fa-f]%'
            THROW 50007, 'sha256 must be exactly 64 hexadecimal characters.', 1;

        IF @sizeBytes IS NULL OR @sizeBytes < 0
            THROW 50008, 'sizeBytes must be a non-negative number.', 1;

        IF @pageCount IS NOT NULL AND @pageCount < 0
            THROW 50009, 'pageCount must be a non-negative number.', 1;

        SET @validationCode = UPPER(NULLIF(LTRIM(RTRIM(@validationCode)), ''));
        IF @validationCode IS NOT NULL
        BEGIN
            SELECT @fileValidationStatusId = [fileValidationStatusId]
            FROM [reference].[fileValidationStatuses]
            WHERE [code] = @validationCode
              AND [isActive] = 1
              AND [isDeleted] = 0;

            IF @fileValidationStatusId IS NULL
                THROW 50010, 'Unknown validationCode (READY, PASSWORD_PROTECTED or CORRUPT).', 1;
        END;

        SET @sourceRef = NULLIF(LTRIM(RTRIM(@sourceRef)), '');
        IF LEN(@sourceRef) > 100
            THROW 50011, 'sourceRef must be at most 100 characters.', 1;

        SET @fileGuid = NEWID();

        INSERT INTO [documents].[files]
        (
            [fileGuid],
            [tenantId],
            [caseId],
            [ingestionChannelId],
            [storageUri],
            [originalFileName],
            [mimeType],
            [sizeBytes],
            [sha256],
            [pageCount],
            [fileValidationStatusId],
            [ingestionSourceRef],
            [receivedAt],
            [createdAt],
            [createdBy]
        )
        VALUES
        (
            @fileGuid,
            @tenantId,
            @caseId,
            @ingestionChannelId,
            @storageUri,
            @originalFileName,
            @mimeType,
            @sizeBytes,
            @sha256,
            @pageCount,
            @fileValidationStatusId,
            @sourceRef,
            SYSDATETIME(),
            SYSDATETIME(),
            @userAction
        );

        SET @fileId = SCOPE_IDENTITY();

        SELECT
            @fileId AS [fileId],
            @fileGuid AS [fileGuid];
    END TRY
    BEGIN CATCH
        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   3) documents.uspDiscardFile
   Agrega el evento DISCARDED y escribe DOCUMENT_DISCARDED en
   security.auditEvents, en la MISMA transacción. Atómico: el archivo se
   bloquea (UPDLOCK) mientras se revisa su último evento; si dos
   personas descartan lo mismo, una gana y la otra recibe 50004.
   No borra nada ni toca el archivo del almacén (se comparte por
   contenido).
   ===================================================================== */
CREATE   PROCEDURE [documents].[uspDiscardFile]
(
    @tenantGuid UNIQUEIDENTIFIER,
    @fileGuid   UNIQUEIDENTIFIER,
    @reason     NVARCHAR(500)    = NULL,
    @byUserGuid UNIQUEIDENTIFIER
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now              DATETIME2 = SYSDATETIME(),
        @tzOffset         INT = DATEPART(TZOFFSET, SYSDATETIMEOFFSET()),
        @tenantId         INT,
        @byUserId         INT,
        @fileId           BIGINT,
        @originalFileName NVARCHAR(260),
        @lastEventCode    VARCHAR(30),
        @eventTypeId      INT,
        @fileEventGuid    UNIQUEIDENTIFIER = NEWID(),
        @userAction       VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SET @reason = NULLIF(LTRIM(RTRIM(@reason)), '');

        SELECT @tenantId = [tenantId]
        FROM [security].[tenants]
        WHERE [tenantGuid] = @tenantGuid
          AND [isDeleted] = 0;

        IF @tenantId IS NULL
            THROW 50001, 'Tenant not found.', 1;

        SELECT @byUserId = u.[userId]
        FROM [security].[users] u
        WHERE u.[userGuid]  = @byUserGuid
          AND u.[isDeleted] = 0
          AND u.[isActive]  = 1
          AND (
                   u.[isPlatformAdmin] = 1
                OR EXISTS
                   (
                       SELECT 1
                       FROM [security].[tenantMemberships] tm
                       WHERE tm.[userId]     = u.[userId]
                         AND tm.[tenantId]   = @tenantId
                         AND tm.[isDeleted]  = 0
                         AND tm.[isActive]   = 1
                         AND tm.[statusCode] = 'ACTIVE'
                   )
              );

        IF @byUserId IS NULL
            THROW 50003, 'byUserGuid is required and must be an active user of this organization.', 1;

        SELECT @eventTypeId = [fileEventTypeId]
        FROM [reference].[fileEventTypes]
        WHERE [code] = 'DISCARDED'
          AND [isActive] = 1
          AND [isDeleted] = 0;

        BEGIN TRANSACTION;

        SELECT
            @fileId           = [fileId],
            @originalFileName = [originalFileName]
        FROM [documents].[files] WITH (UPDLOCK, HOLDLOCK)
        WHERE [fileGuid] = @fileGuid
          AND [tenantId] = @tenantId;

        IF @fileId IS NULL
            THROW 50002, 'File not found in this tenant.', 1;

        SELECT TOP (1) @lastEventCode = et.[code]
        FROM [documents].[fileEvents] e
        INNER JOIN [reference].[fileEventTypes] et
            ON et.[fileEventTypeId] = e.[fileEventTypeId]
        WHERE e.[fileId] = @fileId
        ORDER BY e.[fileEventId] DESC;

        IF @lastEventCode = 'DISCARDED'
            THROW 50004, 'File is already discarded.', 1;

        IF @lastEventCode = 'SENT_TO_PIPELINE'
            THROW 50005, 'File was already sent to the pipeline.', 1;

        INSERT INTO [documents].[fileEvents]
            ([fileEventGuid], [fileId], [fileEventTypeId], [occurredAt], [byUserId], [reason], [createdBy])
        VALUES
            (@fileEventGuid, @fileId, @eventTypeId, @now, @byUserId, @reason, @userAction);

        INSERT INTO [security].[auditEvents] ([action], [userId], [actorUserId], [tenantId], [reason], [dataJson], [createdAt])
        VALUES
        (
            'DOCUMENT_DISCARDED',
            NULL,
            @byUserId,
            @tenantId,
            @reason,
            (SELECT @fileGuid AS [fileGuid], @originalFileName AS [originalFileName] FOR JSON PATH, WITHOUT_ARRAY_WRAPPER),
            @now
        );

        COMMIT TRANSACTION;

        SELECT
            @fileEventGuid                    AS [fileEventGuid],
            TODATETIMEOFFSET(@now, @tzOffset) AS [discardedAt];
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;

        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   1) documents.uspGetOrCreateInboxCase  (NEX-294)
   ===================================================================== */
CREATE   PROCEDURE [documents].[uspGetOrCreateInboxCase]
(
    @tenantGuid UNIQUEIDENTIFIER
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @tenantId         INT,
        @openCaseStatusId INT,
        @caseId           BIGINT,
        @caseGuid         UNIQUEIDENTIFIER,
        @userAction       VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SELECT @tenantId = [tenantId]
        FROM [security].[tenants]
        WHERE [tenantGuid] = @tenantGuid
          AND [isDeleted] = 0;

        IF @tenantId IS NULL
            THROW 50001, 'Tenant not found.', 1;

        SELECT @openCaseStatusId = [caseStatusId]
        FROM [reference].[caseStatuses]
        WHERE [code] = 'OPEN'
          AND [isActive] = 1
          AND [isDeleted] = 0;

        IF @openCaseStatusId IS NULL
            THROW 50002, 'The OPEN case status is not configured.', 1;

        BEGIN TRY
            INSERT INTO [documents].[cases]
            (
                [tenantId],
                [externalRef],
                [caseStatusId],
                [openedAt],
                [createdBy]
            )
            SELECT
                @tenantId,
                'INBOX',
                @openCaseStatusId,
                SYSDATETIME(),
                @userAction
            WHERE NOT EXISTS
            (
                SELECT 1
                FROM [documents].[cases]
                WHERE [tenantId] = @tenantId
                  AND [externalRef] = 'INBOX'
            );
        END TRY
        BEGIN CATCH
            -- Otra sesión ganó la carrera e insertó primero: no es un
            -- error real, el SELECT de abajo recupera esa fila.
            IF ERROR_NUMBER() NOT IN (2601, 2627)
                THROW;
        END CATCH;

        SELECT
            @caseId = [caseId],
            @caseGuid = [caseGuid]
        FROM [documents].[cases]
        WHERE [tenantId] = @tenantId
          AND [externalRef] = 'INBOX';

        IF @caseId IS NULL
            THROW 50003, 'Could not get or create the inbox case.', 1;

        SELECT
            @caseId AS [caseId],
            @caseGuid AS [caseGuid];
    END TRY
    BEGIN CATCH
        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   2) documents.uspListInboxFiles  (HU 25)
   - @statuses: lista separada por comas de PENDING, READY, DUPLICATE,
     DISCARDED. NULL = bandeja ACTIVA (todo menos DISCARDED). Con
     DISCARDED en la lista salen los descartados ("sección de
     descartados"). Lo que ya pasó al pipeline nunca sale.
   - @channels: códigos de reference.ingestionChannels, separados por
     comas. NULL = todos.
   - [@receivedFrom, @receivedTo): periodo semiabierto, con zona.
   - Orden por receivedAt y fileId (desempate). Página fuera de rango:
     0 filas y el totalCount correcto.
   Resultado 1: la página.
   Resultado 2: totalCount (con filtros) y duplicateCount (archivos en
                DUPLICATE de todo el tenant, para la insignia de Filtros).
   ===================================================================== */
CREATE   PROCEDURE [documents].[uspListInboxFiles]
(
    @tenantGuid     UNIQUEIDENTIFIER,
    @statuses       VARCHAR(100)   = NULL,
    @channels       VARCHAR(100)   = NULL,
    @receivedFrom   DATETIMEOFFSET = NULL,
    @receivedTo     DATETIMEOFFSET = NULL,
    @pageNumber     INT            = 1,
    @pageSize       INT            = 8,
    @sortDescending BIT            = 0
)
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE
        @tenantId       INT,
        @tzOffset       INT = DATEPART(TZOFFSET, SYSDATETIMEOFFSET()),
        @fromLocal      DATETIME2,
        @toLocal        DATETIME2,
        @badCode        VARCHAR(100),
        @message        NVARCHAR(400),
        @totalCount     INT,
        @duplicateCount INT;

    DECLARE @statusList  TABLE ([code] VARCHAR(30) PRIMARY KEY);
    DECLARE @channelList TABLE ([code] VARCHAR(20) PRIMARY KEY);

    SELECT @tenantId = [tenantId]
    FROM [security].[tenants]
    WHERE [tenantGuid] = @tenantGuid
      AND [isDeleted] = 0;

    IF @tenantId IS NULL
        THROW 50001, 'Tenant not found.', 1;

    SET @statuses = NULLIF(LTRIM(RTRIM(@statuses)), '');
    IF @statuses IS NOT NULL
    BEGIN
        INSERT INTO @statusList ([code])
        SELECT DISTINCT UPPER(LTRIM(RTRIM([value])))
        FROM STRING_SPLIT(@statuses, ',')
        WHERE LTRIM(RTRIM([value])) <> '';

        SELECT TOP (1) @badCode = [code]
        FROM @statusList
        WHERE [code] NOT IN ('PENDING', 'READY', 'DUPLICATE', 'DISCARDED');

        IF @badCode IS NOT NULL
        BEGIN
            SET @message = CONCAT('Unknown status ''', @badCode, ''' (PENDING, READY, DUPLICATE or DISCARDED).');
            THROW 50002, @message, 1;
        END;
    END;

    SET @channels = NULLIF(LTRIM(RTRIM(@channels)), '');
    IF @channels IS NOT NULL
    BEGIN
        INSERT INTO @channelList ([code])
        SELECT DISTINCT UPPER(LTRIM(RTRIM([value])))
        FROM STRING_SPLIT(@channels, ',')
        WHERE LTRIM(RTRIM([value])) <> '';

        SELECT TOP (1) @badCode = cl.[code]
        FROM @channelList cl
        WHERE NOT EXISTS
        (
            SELECT 1
            FROM [reference].[ingestionChannels] ic
            WHERE ic.[code] = cl.[code]
              AND ic.[isDeleted] = 0
        );

        IF @badCode IS NOT NULL
        BEGIN
            SET @message = CONCAT('Unknown ingestion channel ''', @badCode, '''.');
            THROW 50003, @message, 1;
        END;
    END;

    IF @pageNumber IS NULL OR @pageNumber < 1
        THROW 50004, 'pageNumber must be 1 or greater.', 1;

    IF @pageSize IS NULL OR @pageSize < 1 OR @pageSize > 100
        THROW 50005, 'pageSize must be between 1 and 100.', 1;

    IF @receivedFrom IS NOT NULL AND @receivedTo IS NOT NULL AND @receivedFrom >= @receivedTo
        THROW 50006, 'receivedFrom must be earlier than receivedTo.', 1;

    SET @fromLocal = CONVERT(DATETIME2, SWITCHOFFSET(@receivedFrom, @tzOffset));
    SET @toLocal   = CONVERT(DATETIME2, SWITCHOFFSET(@receivedTo,   @tzOffset));
    SET @sortDescending = ISNULL(@sortDescending, 0);

    -- Todo lo que pasa los filtros; de aquí salen la página y el total.
    SELECT i.*
    INTO #filtered
    FROM [documents].[ufnInboxFiles](@tenantId) i
    WHERE i.[inboxStatus] <> 'SENT_TO_PIPELINE'
      AND (
               (@statuses IS NULL     AND i.[inboxStatus] <> 'DISCARDED')
            OR (@statuses IS NOT NULL AND i.[inboxStatus] IN (SELECT [code] FROM @statusList))
          )
      AND (@channels  IS NULL OR i.[ingestionChannelCode] IN (SELECT [code] FROM @channelList))
      AND (@fromLocal IS NULL OR i.[receivedAt] >= @fromLocal)
      AND (@toLocal   IS NULL OR i.[receivedAt] <  @toLocal);

    SET @totalCount = @@ROWCOUNT;

    SELECT @duplicateCount = COUNT(*)
    FROM [documents].[ufnInboxFiles](@tenantId)
    WHERE [inboxStatus] = 'DUPLICATE';

    SELECT
        [fileGuid],
        [originalFileName]                               AS [originalName],
        [mimeType],
        [sizeBytes],
        [sha256],
        [storageUri],
        [ingestionChannelCode]                           AS [ingestionChannel],
        TODATETIMEOFFSET([receivedAt], @tzOffset)        AS [receivedAt],
        [validationCode],
        [inboxStatus],
        [isDuplicate],
        CASE WHEN [inboxStatus] = 'DISCARDED'
             THEN TODATETIMEOFFSET([lastEventAt], @tzOffset) END AS [discardedAt],
        CASE WHEN [inboxStatus] = 'DISCARDED'
             THEN [lastEventReason] END                  AS [discardedReason],
        [ingestionSourceRef]                             AS [sourceRef]
    FROM #filtered
    ORDER BY
        CASE WHEN @sortDescending = 0 THEN [receivedAt] END ASC,
        CASE WHEN @sortDescending = 0 THEN [fileId]     END ASC,
        CASE WHEN @sortDescending = 1 THEN [receivedAt] END DESC,
        CASE WHEN @sortDescending = 1 THEN [fileId]     END DESC
    OFFSET (@pageNumber - 1) * @pageSize ROWS
    FETCH NEXT @pageSize ROWS ONLY;

    SELECT
        @totalCount     AS [totalCount],
        @duplicateCount AS [duplicateCount];
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   4) documents.uspSendFileToPipeline  (HU 27)
   Agrega SENT_TO_PIPELINE: el archivo sale de la bandeja. Mismas reglas
   de atomicidad y errores que uspDiscardFile, más una:
   - Solo pasa si la validación es READY (50006). Un duplicado sí pasa.
   @byUserGuid NULL = lo mandó la API o un proceso.
   ===================================================================== */
CREATE   PROCEDURE [documents].[uspSendFileToPipeline]
(
    @tenantGuid UNIQUEIDENTIFIER,
    @fileGuid   UNIQUEIDENTIFIER,
    @byUserGuid UNIQUEIDENTIFIER = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now            DATETIME2 = SYSDATETIME(),
        @tzOffset       INT = DATEPART(TZOFFSET, SYSDATETIMEOFFSET()),
        @tenantId       INT,
        @byUserId       INT,
        @fileId         BIGINT,
        @validationCode VARCHAR(20),
        @lastEventCode  VARCHAR(30),
        @eventTypeId    INT,
        @fileEventGuid  UNIQUEIDENTIFIER = NEWID(),
        @userAction     VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SELECT @tenantId = [tenantId]
        FROM [security].[tenants]
        WHERE [tenantGuid] = @tenantGuid
          AND [isDeleted] = 0;

        IF @tenantId IS NULL
            THROW 50001, 'Tenant not found.', 1;

        IF @byUserGuid IS NOT NULL
        BEGIN
            SELECT @byUserId = u.[userId]
            FROM [security].[users] u
            WHERE u.[userGuid]  = @byUserGuid
              AND u.[isDeleted] = 0
              AND u.[isActive]  = 1
              AND (
                       u.[isPlatformAdmin] = 1
                    OR EXISTS
                       (
                           SELECT 1
                           FROM [security].[tenantMemberships] tm
                           WHERE tm.[userId]     = u.[userId]
                             AND tm.[tenantId]   = @tenantId
                             AND tm.[isDeleted]  = 0
                             AND tm.[isActive]   = 1
                             AND tm.[statusCode] = 'ACTIVE'
                       )
                  );

            IF @byUserId IS NULL
                THROW 50003, 'byUserGuid must be an active user of this organization.', 1;
        END;

        SELECT @eventTypeId = [fileEventTypeId]
        FROM [reference].[fileEventTypes]
        WHERE [code] = 'SENT_TO_PIPELINE'
          AND [isActive] = 1
          AND [isDeleted] = 0;

        BEGIN TRANSACTION;

        SELECT
            @fileId         = f.[fileId],
            @validationCode = vs.[code]
        FROM [documents].[files] f WITH (UPDLOCK, HOLDLOCK)
        LEFT JOIN [reference].[fileValidationStatuses] vs
            ON vs.[fileValidationStatusId] = f.[fileValidationStatusId]
        WHERE f.[fileGuid] = @fileGuid
          AND f.[tenantId] = @tenantId;

        IF @fileId IS NULL
            THROW 50002, 'File not found in this tenant.', 1;

        SELECT TOP (1) @lastEventCode = et.[code]
        FROM [documents].[fileEvents] e
        INNER JOIN [reference].[fileEventTypes] et
            ON et.[fileEventTypeId] = e.[fileEventTypeId]
        WHERE e.[fileId] = @fileId
        ORDER BY e.[fileEventId] DESC;

        IF @lastEventCode = 'DISCARDED'
            THROW 50004, 'File is discarded.', 1;

        IF @lastEventCode = 'SENT_TO_PIPELINE'
            THROW 50005, 'File was already sent to the pipeline.', 1;

        IF ISNULL(@validationCode, '') <> 'READY'
            THROW 50006, 'File is not ready: its validation is pending, password protected or corrupt.', 1;

        INSERT INTO [documents].[fileEvents]
            ([fileEventGuid], [fileId], [fileEventTypeId], [occurredAt], [byUserId], [createdBy])
        VALUES
            (@fileEventGuid, @fileId, @eventTypeId, @now, @byUserId, @userAction);

        COMMIT TRANSACTION;

        SELECT
            @fileEventGuid                    AS [fileEventGuid],
            TODATETIMEOFFSET(@now, @tzOffset) AS [sentAt];
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;

        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   1) security.uspBootstrapPlatformAdmin
   Crea un superadministrador. Puede haber varios: cada llamada crea
   otro. Solo falla si el correo ya está registrado.
   Nace con contraseña temporal (mustChangePassword = 1,
   temporaryPasswordIssuedAt = ahora) y deja USER_CREATED en auditoría.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspBootstrapPlatformAdmin]
(
    @email        VARCHAR(254),
    @firstName    NVARCHAR(100),
    @lastName     NVARCHAR(100),
    @passwordHash VARCHAR(255)
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now        DATETIME2 = SYSDATETIME(),
        @userId     INT,
        @userGuid   UNIQUEIDENTIFIER,
        @userAction VARCHAR(50) = SUSER_SNAME();

    DECLARE @newUser TABLE ([userId] INT, [userGuid] UNIQUEIDENTIFIER);

    BEGIN TRY
        SET @email        = LOWER(NULLIF(LTRIM(RTRIM(@email)), ''));
        SET @firstName    = NULLIF(LTRIM(RTRIM(@firstName)), '');
        SET @lastName     = NULLIF(LTRIM(RTRIM(@lastName)), '');
        SET @passwordHash = NULLIF(LTRIM(RTRIM(@passwordHash)), '');

        IF @email IS NULL OR @email NOT LIKE '%_@_%._%'
            THROW 50001, 'A valid email is required.', 1;

        IF @firstName IS NULL
            THROW 50002, 'firstName is required.', 1;

        IF @lastName IS NULL
            THROW 50003, 'lastName is required.', 1;

        IF @passwordHash IS NULL
            THROW 50004, 'passwordHash is required.', 1;

        BEGIN TRANSACTION;

        -- 50005 ya no se usa: antes era "ya existe un superadministrador".
        IF EXISTS
        (
            SELECT 1
            FROM [security].[users] WITH (UPDLOCK, HOLDLOCK)
            WHERE [email] = @email
              AND [isDeleted] = 0
        )
            THROW 50006, 'Email is already registered.', 1;

        INSERT INTO [security].[users]
        (
            [email],
            [firstName],
            [lastName],
            [isPlatformAdmin],
            [createdBy]
        )
        OUTPUT inserted.[userId], inserted.[userGuid] INTO @newUser
        VALUES
        (
            @email,
            @firstName,
            @lastName,
            1,
            @userAction
        );

        SELECT @userId = [userId], @userGuid = [userGuid] FROM @newUser;

        INSERT INTO [security].[userCredentials]
        (
            [userId],
            [passwordHash],
            [mustChangePassword],
            [temporaryPasswordIssuedAt],
            [createdBy]
        )
        VALUES
        (
            @userId,
            @passwordHash,
            1,
            @now,
            @userAction
        );

        INSERT INTO [security].[auditEvents] ([action], [userId], [dataJson], [createdAt])
        VALUES ('USER_CREATED', @userId, N'{"isPlatformAdmin":true}', @now);

        COMMIT TRANSACTION;

        SELECT @userGuid AS [userGuid];
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   2) security.uspConsumeAccessToken
   ATÓMICO (UPDLOCK): marca usedAt y devuelve a quién pertenece. Con
   dos peticiones con el mismo enlace, una gana y la otra recibe 50003.
   Códigos:
     50002 no existe
     50003 ya se usó
     50004 venció
     50005 fue invalidado (otro enlace, cambio de contraseña,
           desactivación, reinvitación o baja)
   ===================================================================== */
CREATE   PROCEDURE [security].[uspConsumeAccessToken]
(
    @tokenHash CHAR(64)
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now           DATETIME2 = SYSDATETIME(),
        @accessTokenId BIGINT,
        @userId        INT,
        @kind          VARCHAR(20),
        @expiresAt     DATETIME2,
        @usedAt        DATETIME2,
        @invalidatedAt DATETIME2,
        @userAction    VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        IF @tokenHash IS NULL
            THROW 50001, 'tokenHash is required.', 1;

        BEGIN TRANSACTION;

        SELECT
            @accessTokenId = [accessTokenId],
            @userId        = [userId],
            @kind          = [kind],
            @expiresAt     = [expiresAt],
            @usedAt        = [usedAt],
            @invalidatedAt = [invalidatedAt]
        FROM [security].[accessTokens] WITH (UPDLOCK, ROWLOCK)
        WHERE [tokenHash] = @tokenHash;

        IF @accessTokenId IS NULL
            THROW 50002, 'Access token does not exist.', 1;

        IF @usedAt IS NOT NULL
            THROW 50003, 'Access token was already used.', 1;

        IF @invalidatedAt IS NOT NULL
            THROW 50005, 'Access token was invalidated.', 1;

        IF @expiresAt <= @now
            THROW 50004, 'Access token expired.', 1;

        UPDATE [security].[accessTokens]
        SET [usedAt]    = @now,
            [updatedAt] = @now,
            [updatedBy] = @userAction
        WHERE [accessTokenId] = @accessTokenId;

        COMMIT TRANSACTION;

        SELECT
            u.[userGuid],
            @kind AS [kind]
        FROM [security].[users] u
        WHERE u.[userId] = @userId;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   5) security.uspCountAuditEvents
   Cuenta TODOS los eventos de una acción desde @since, exista o no la
   persona: si solo contara cuentas reales delataría cuáles existen.
   Usa IX_auditEvents_action_createdAt.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspCountAuditEvents]
(
    @action VARCHAR(40),
    @since  DATETIME2
)
AS
BEGIN
    SET NOCOUNT ON;

    SET @action = UPPER(NULLIF(LTRIM(RTRIM(@action)), ''));

    IF @action IS NULL
        THROW 50001, 'action is required.', 1;

    IF @since IS NULL
        THROW 50002, 'since is required.', 1;

    SELECT COUNT(*) AS [total]
    FROM [security].[auditEvents]
    WHERE [action]    = @action
      AND [createdAt] >= @since;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   1) security.uspCreateAccessToken
   Crea el enlace e INVALIDA los pendientes del mismo tipo de esa
   persona (NEW_TOKEN), en la misma transacción: pedir otro enlace
   apaga el anterior.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspCreateAccessToken]
(
    @userGuid  UNIQUEIDENTIFIER,
    @kind      VARCHAR(20),
    @tokenHash CHAR(64),
    @expiresAt DATETIME2
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now             DATETIME2 = SYSDATETIME(),
        @userId          INT,
        @isActive        BIT,
        @accessTokenGuid UNIQUEIDENTIFIER = NEWID(),
        @invalidated     INT,
        @userAction      VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SET @kind = UPPER(NULLIF(LTRIM(RTRIM(@kind)), ''));

        IF @kind IS NULL OR @kind NOT IN ('RECOVERY', 'ACTIVATION')
            THROW 50001, 'kind must be RECOVERY or ACTIVATION.', 1;

        IF @tokenHash IS NULL
           OR LEN(@tokenHash) <> 64
           OR @tokenHash LIKE '%[^0-9A-Fa-f]%'
            THROW 50002, 'tokenHash must be exactly 64 hexadecimal characters.', 1;

        IF @expiresAt IS NULL OR @expiresAt <= @now
            THROW 50003, 'expiresAt must be in the future.', 1;

        BEGIN TRANSACTION;

        SELECT
            @userId   = [userId],
            @isActive = [isActive]
        FROM [security].[users] WITH (UPDLOCK, ROWLOCK)
        WHERE [userGuid] = @userGuid
          AND [isDeleted] = 0;

        IF @userId IS NULL
            THROW 50004, 'User not found.', 1;

        IF @isActive = 0
            THROW 50005, 'User is inactive.', 1;

        UPDATE [security].[accessTokens]
        SET [invalidatedAt]     = @now,
            [invalidatedReason] = 'NEW_TOKEN',
            [updatedAt]         = @now,
            [updatedBy]         = @userAction
        WHERE [userId] = @userId
          AND [kind]   = @kind
          AND [usedAt] IS NULL
          AND [invalidatedAt] IS NULL;

        SET @invalidated = @@ROWCOUNT;

        INSERT INTO [security].[accessTokens]
        (
            [accessTokenGuid],
            [userId],
            [kind],
            [tokenHash],
            [expiresAt],
            [createdAt],
            [createdBy]
        )
        VALUES
        (
            @accessTokenGuid,
            @userId,
            @kind,
            @tokenHash,
            @expiresAt,
            @now,
            @userAction
        );

        COMMIT TRANSACTION;

        SELECT
            @accessTokenGuid AS [accessTokenGuid],
            @expiresAt       AS [expiresAt],
            @invalidated     AS [invalidatedPrevious];
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;

        IF ERROR_NUMBER() IN (2601, 2627)
            THROW 50006, 'tokenHash is already in use.', 1;

        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   5) security.uspCreateSession
   Abre una sesión después de un login exitoso.
   @expectedPasswordHash (opcional): solo crea la sesión si el hash
   vigente es ESE. El login verifica argon2 fuera de la base; si en esa
   ventana alguien restablece la contraseña o desactiva la cuenta, la
   sesión no nace. Cada caso falla sin escribir y con su código.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspCreateSession]
(
    @userGuid             UNIQUEIDENTIFIER,
    @refreshTokenHash     CHAR(64),
    @expiresAt            DATETIME2,
    @ipAddress            VARCHAR(45)   = NULL,
    @userAgent            NVARCHAR(500) = NULL,
    @expectedPasswordHash VARCHAR(255)  = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now                 DATETIME2 = SYSDATETIME(),
        @userId              INT,
        @isActive            BIT,
        @currentPasswordHash VARCHAR(255),
        @sessionGuid         UNIQUEIDENTIFIER = NEWID(),
        @userAction          VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SET @expectedPasswordHash = NULLIF(LTRIM(RTRIM(@expectedPasswordHash)), '');

        IF @refreshTokenHash IS NULL
           OR LEN(@refreshTokenHash) <> 64
           OR @refreshTokenHash LIKE '%[^0-9A-Fa-f]%'
            THROW 50003, 'refreshTokenHash must be exactly 64 hexadecimal characters.', 1;

        IF @expiresAt IS NULL OR @expiresAt <= @now
            THROW 50004, 'expiresAt must be in the future.', 1;

        BEGIN TRANSACTION;

        SELECT
            @userId   = [userId],
            @isActive = [isActive]
        FROM [security].[users] WITH (UPDLOCK, ROWLOCK)
        WHERE [userGuid] = @userGuid
          AND [isDeleted] = 0;

        IF @userId IS NULL
            THROW 50001, 'User not found.', 1;

        IF @isActive = 0
            THROW 50002, 'User is inactive.', 1;

        IF @expectedPasswordHash IS NOT NULL
        BEGIN
            SELECT @currentPasswordHash = [passwordHash]
            FROM [security].[userCredentials] WITH (UPDLOCK, ROWLOCK)
            WHERE [userId] = @userId;

            IF @currentPasswordHash IS NULL OR @currentPasswordHash <> @expectedPasswordHash
                THROW 50006, 'The password changed after it was verified: expectedPasswordHash does not match.', 1;
        END;

        IF EXISTS
        (
            SELECT 1
            FROM [security].[sessions]
            WHERE [refreshTokenHash] = @refreshTokenHash
        )
            THROW 50005, 'refreshTokenHash is already in use.', 1;

        INSERT INTO [security].[sessions]
        (
            [sessionGuid],
            [userId],
            [refreshTokenHash],
            [issuedAt],
            [expiresAt],
            [ipAddress],
            [userAgent],
            [createdBy]
        )
        VALUES
        (
            @sessionGuid,
            @userId,
            @refreshTokenHash,
            @now,
            @expiresAt,
            @ipAddress,
            @userAgent,
            @userAction
        );

        COMMIT TRANSACTION;

        SELECT
            @sessionGuid AS [sessionGuid],
            @expiresAt   AS [expiresAt];
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;

        IF ERROR_NUMBER() IN (2601, 2627)
            THROW 50005, 'refreshTokenHash is already in use.', 1;

        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   1) security.uspCreateTenant
   ===================================================================== */
CREATE   PROCEDURE [security].[uspCreateTenant]
(
    @name          NVARCHAR(200),
    @slug          VARCHAR(63),
    @settingsJson  NVARCHAR(MAX) = NULL,
    @recoveryEmail VARCHAR(254)  = NULL,
    @recoveryPhone VARCHAR(25)   = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @tenantId INT,
        @tenantGuid UNIQUEIDENTIFIER,
        @tenantStatusId INT,
        @prefix VARCHAR(5),
        @sequenceNumber INT,
        @tenantCode VARCHAR(11),
        @userAction VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SET @name = NULLIF(LTRIM(RTRIM(@name)), '');
        SET @slug = LOWER(NULLIF(LTRIM(RTRIM(@slug)), ''));
        SET @recoveryEmail = LOWER(NULLIF(LTRIM(RTRIM(@recoveryEmail)), ''));
        SET @recoveryPhone = NULLIF(LTRIM(RTRIM(@recoveryPhone)), '');

        IF @name IS NULL
            THROW 50001, 'Tenant name is required.', 1;

        IF @slug IS NULL
            THROW 50002, 'Tenant slug is required.', 1;

        IF @settingsJson IS NOT NULL
           AND ISJSON(@settingsJson) = 0
            THROW 50003, 'Tenant settings must contain valid JSON.', 1;

        IF @recoveryEmail IS NOT NULL
           AND @recoveryEmail NOT LIKE '%_@_%._%'
            THROW 50009, 'recoveryEmail is not a valid email.', 1;

        IF EXISTS
        (
            SELECT 1
            FROM [security].[tenants]
            WHERE [slug] = @slug
              AND [isDeleted] = 0
        )
            THROW 50004, 'A tenant with the same slug already exists.', 1;

        SELECT
            @tenantStatusId = [tenantStatusId]
        FROM [reference].[tenantStatuses]
        WHERE [code] = 'ACTIVE'
          AND [isActive] = 1
          AND [isDeleted] = 0;

        IF @tenantStatusId IS NULL
            THROW 50005, 'The ACTIVE tenant status is not configured.', 1;

        BEGIN TRANSACTION;

        -- NEX-182: prefix y lastSequence salen de la fila activa de
        -- tenantSequence, no de un valor fijo en el código. TOP(1) +
        -- el índice único filtrado del paso 2 garantizan que solo hay
        -- una fila candidata.
        UPDATE TOP (1) [security].[tenantSequence] WITH (UPDLOCK, HOLDLOCK)
        SET
            @prefix = [prefix],
            @sequenceNumber = [lastSequence] + 1,
            [lastSequence] = [lastSequence] + 1,
            [updatedAt] = SYSDATETIME(),
            [updatedBy] = @userAction
        WHERE [isActive] = 1;

        IF @sequenceNumber IS NULL
            THROW 50006, 'There is no active row in tenantSequence.', 1;

        IF @sequenceNumber > 99999
            THROW 50007, 'The tenant sequence has reached its maximum value.', 1;

        SET @tenantCode =
            @prefix + '-' +
            RIGHT
            (
                '00000' + CONVERT(VARCHAR(5), @sequenceNumber),
                5
            );

        -- NEX-212: si algún día tenantCode se angosta otra vez por error,
        -- que truene explícito en vez de truncar en silencio.
        IF LEN(@tenantCode) > 11
            THROW 50008, 'The generated tenantCode exceeds the allowed length.', 1;

        SET @tenantGuid = NEWID();

        INSERT INTO [security].[tenants]
        (
            [tenantGuid],
            [tenantCode],
            [tenantStatusId],
            [name],
            [slug],
            [settingsJson],
            [recoveryEmail],
            [recoveryPhone],
            [isActive],
            [isDeleted],
            [createdAt],
            [createdBy]
        )
        VALUES
        (
            @tenantGuid,
            @tenantCode,
            @tenantStatusId,
            @name,
            @slug,
            @settingsJson,
            @recoveryEmail,
            @recoveryPhone,
            1,
            0,
            SYSDATETIME(),
            @userAction
        );

        SET @tenantId = SCOPE_IDENTITY();

        COMMIT TRANSACTION;

        SELECT
            @tenantId AS [tenantId],
            @tenantGuid AS [tenantGuid],
            @tenantCode AS [tenantCode],
            @name AS [name],
            @slug AS [slug],
            @recoveryEmail AS [recoveryEmail],
            @recoveryPhone AS [recoveryPhone];
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;

        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   2) security.uspCreateTenantUser
   En UNA transacción: usuario + credencial temporal + membresía con rol
   + USER_CREATED. Si algo falla no queda nada a medias.
   - El correo es único en TODA la plataforma (decisión de producto).
   - La organización debe existir y estar ACTIVE.
   - Acepta ADMIN (así nace el administrador de cada organización) y
     cualquier rol activo; rechaza los desactivados.
   - Credencial: mustChangePassword = 1, temporaryPasswordIssuedAt = ahora.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspCreateTenantUser]
(
    @tenantGuid     UNIQUEIDENTIFIER,
    @email          VARCHAR(254),
    @firstName      NVARCHAR(200),
    @lastName       NVARCHAR(200),
    @secondLastName NVARCHAR(200)    = NULL,
    @phoneNumber    VARCHAR(20)      = NULL,
    @passwordHash   VARCHAR(255),
    @roleCode       VARCHAR(40),
    @byUserGuid     UNIQUEIDENTIFIER = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now        DATETIME2 = SYSDATETIME(),
        @tenantId   INT,
        @roleId     INT,
        @byUserId   INT,
        @userId     INT,
        @userGuid   UNIQUEIDENTIFIER,
        @userAction VARCHAR(50) = SUSER_SNAME();

    DECLARE @newUser TABLE ([userId] INT, [userGuid] UNIQUEIDENTIFIER);

    BEGIN TRY
        SET @email          = LOWER(NULLIF(LTRIM(RTRIM(@email)), ''));
        SET @firstName      = NULLIF(LTRIM(RTRIM(@firstName)), '');
        SET @lastName       = NULLIF(LTRIM(RTRIM(@lastName)), '');
        SET @secondLastName = NULLIF(LTRIM(RTRIM(@secondLastName)), '');
        SET @phoneNumber    = NULLIF(LTRIM(RTRIM(@phoneNumber)), '');
        SET @passwordHash   = NULLIF(LTRIM(RTRIM(@passwordHash)), '');
        SET @roleCode       = UPPER(NULLIF(LTRIM(RTRIM(@roleCode)), ''));

        IF @tenantGuid IS NULL
            THROW 50001, 'tenantGuid is required.', 1;

        IF @email IS NULL OR @email NOT LIKE '%_@_%._%'
            THROW 50002, 'A valid email is required.', 1;

        IF @firstName IS NULL
            THROW 50003, 'firstName is required.', 1;

        IF @lastName IS NULL
            THROW 50004, 'lastName is required.', 1;

        IF @passwordHash IS NULL
            THROW 50005, 'passwordHash is required.', 1;

        SELECT @tenantId = t.[tenantId]
        FROM [security].[tenants] t
        INNER JOIN [reference].[tenantStatuses] ts
            ON ts.[tenantStatusId] = t.[tenantStatusId]
        WHERE t.[tenantGuid] = @tenantGuid
          AND t.[isDeleted]  = 0
          AND t.[isActive]   = 1
          AND ts.[code]      = 'ACTIVE';

        IF @tenantId IS NULL
            THROW 50006, 'Tenant was not found or is not ACTIVE.', 1;

        SELECT @roleId = [roleId]
        FROM [security].[roles]
        WHERE [code]      = @roleCode
          AND [isActive]  = 1
          AND [isDeleted] = 0;

        IF @roleId IS NULL
            THROW 50007, 'roleCode was not found or is not active.', 1;

        IF @byUserGuid IS NOT NULL
        BEGIN
            SELECT @byUserId = [userId]
            FROM [security].[users]
            WHERE [userGuid] = @byUserGuid
              AND [isDeleted] = 0;

            IF @byUserId IS NULL
                THROW 50008, 'byUserGuid was not found.', 1;
        END;

        BEGIN TRANSACTION;

        IF EXISTS
        (
            SELECT 1
            FROM [security].[users] WITH (UPDLOCK, HOLDLOCK)
            WHERE [email] = @email
              AND [isDeleted] = 0
        )
            THROW 50009, 'Email is already registered.', 1;

        INSERT INTO [security].[users]
        (
            [email],
            [firstName],
            [lastName],
            [secondLastName],
            [phoneNumber],
            [isPlatformAdmin],
            [createdBy]
        )
        OUTPUT inserted.[userId], inserted.[userGuid] INTO @newUser
        VALUES
        (
            @email,
            @firstName,
            @lastName,
            @secondLastName,
            @phoneNumber,
            0,
            @userAction
        );

        SELECT @userId = [userId], @userGuid = [userGuid] FROM @newUser;

        INSERT INTO [security].[userCredentials]
        (
            [userId],
            [passwordHash],
            [mustChangePassword],
            [temporaryPasswordIssuedAt],
            [createdBy]
        )
        VALUES
        (
            @userId,
            @passwordHash,
            1,
            @now,
            @userAction
        );

        INSERT INTO [security].[tenantMemberships]
        (
            [tenantId],
            [userId],
            [roleId],
            [statusCode],
            [joinedAt],
            [createdBy]
        )
        VALUES
        (
            @tenantId,
            @userId,
            @roleId,
            'ACTIVE',
            @now,
            @userAction
        );

        INSERT INTO [security].[auditEvents] ([action], [userId], [actorUserId], [tenantId], [dataJson], [createdAt])
        VALUES
        (
            'USER_CREATED',
            @userId,
            @byUserId,
            @tenantId,
            (SELECT @roleCode AS [roleCode] FOR JSON PATH, WITHOUT_ARRAY_WRAPPER),
            @now
        );

        COMMIT TRANSACTION;

        SELECT @userGuid AS [userGuid];
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;

        IF ERROR_NUMBER() IN (2601, 2627)
            THROW 50009, 'Email is already registered.', 1;

        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   9) security.uspDeleteUser  (HU06 CA-6)
   Baja lógica: users.isDeleted = 1 (libera el correo gracias a
   UX_users_email filtrado), membresía borrada (REVOKED), sesiones
   revocadas (DELETED), enlaces invalidados y USER_DELETED.
   Se niega, salvo @force = 1, si es el último superadministrador
   activo (50005) o el último ADMIN activo de su organización (50006).
   ===================================================================== */
CREATE   PROCEDURE [security].[uspDeleteUser]
(
    @userGuid   UNIQUEIDENTIFIER,
    @reason     NVARCHAR(200),
    @byUserGuid UNIQUEIDENTIFIER = NULL,
    @force      BIT              = 0,
    @tenantGuid UNIQUEIDENTIFIER = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now               DATETIME2 = SYSDATETIME(),
        @userId            INT,
        @tenantId          INT,
        @membershipId      INT,
        @roleCode          VARCHAR(40),
        @isPlatformAdmin   BIT,
        @byUserId          INT,
        @revokedSessions   INT,
        @invalidatedTokens INT,
        @userAction        VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SET @reason = NULLIF(LTRIM(RTRIM(@reason)), '');
        SET @force  = ISNULL(@force, 0);

        IF @reason IS NULL
            THROW 50001, 'reason is required.', 1;

        IF @byUserGuid IS NOT NULL
        BEGIN
            SELECT @byUserId = [userId]
            FROM [security].[users]
            WHERE [userGuid] = @byUserGuid
              AND [isDeleted] = 0;

            IF @byUserId IS NULL
                THROW 50002, 'byUserGuid was not found.', 1;

            IF @byUserGuid = @userGuid
                THROW 50003, 'A user cannot delete themselves.', 1;
        END;

        BEGIN TRANSACTION;

        SELECT
            @userId          = [userId],
            @tenantId        = [tenantId],
            @membershipId    = [tenantMembershipId],
            @roleCode        = [roleCode],
            @isPlatformAdmin = [isPlatformAdmin]
        FROM [security].[ufnGetScopedUser](@userGuid, @tenantGuid);

        IF @userId IS NULL
            THROW 50004, 'User not found in this organization.', 1;

        IF @force = 0 AND @isPlatformAdmin = 1
           AND NOT EXISTS
           (
               SELECT 1
               FROM [security].[users] WITH (UPDLOCK, HOLDLOCK)
               WHERE [isPlatformAdmin] = 1
                 AND [isActive]  = 1
                 AND [isDeleted] = 0
                 AND [userId]   <> @userId
           )
            THROW 50005, 'This user is the last active platform admin. Use @force = 1 to delete anyway.', 1;

        IF @force = 0 AND @roleCode = 'ADMIN'
           AND NOT EXISTS
           (
               SELECT 1
               FROM [security].[tenantMemberships] tm WITH (UPDLOCK, HOLDLOCK)
               INNER JOIN [security].[roles] r
                   ON r.[roleId] = tm.[roleId]
               INNER JOIN [security].[users] u
                   ON u.[userId] = tm.[userId]
               WHERE tm.[tenantId]  = @tenantId
                 AND tm.[userId]   <> @userId
                 AND tm.[isDeleted] = 0
                 AND r.[code]       = 'ADMIN'
                 AND u.[isActive]   = 1
                 AND u.[isDeleted]  = 0
           )
            THROW 50006, 'This user is the last active ADMIN of the organization. Use @force = 1 to delete anyway.', 1;

        UPDATE [security].[users]
        SET [isDeleted] = 1,
            [isActive]  = 0,
            [updatedAt] = @now,
            [updatedBy] = @userAction
        WHERE [userId] = @userId;

        IF @membershipId IS NOT NULL
            UPDATE [security].[tenantMemberships]
            SET [isDeleted]  = 1,
                [isActive]   = 0,
                [statusCode] = 'REVOKED',
                [updatedAt]  = @now,
                [updatedBy]  = @userAction
            WHERE [tenantMembershipId] = @membershipId;

        UPDATE [security].[sessions]
        SET [revokedAt]     = @now,
            [revokedReason] = 'DELETED',
            [updatedAt]     = @now,
            [updatedBy]     = @userAction
        WHERE [userId] = @userId
          AND [revokedAt] IS NULL;

        SET @revokedSessions = @@ROWCOUNT;

        UPDATE [security].[accessTokens]
        SET [invalidatedAt]     = @now,
            [invalidatedReason] = 'DELETED',
            [updatedAt]         = @now,
            [updatedBy]         = @userAction
        WHERE [userId] = @userId
          AND [usedAt] IS NULL
          AND [invalidatedAt] IS NULL;

        SET @invalidatedTokens = @@ROWCOUNT;

        INSERT INTO [security].[auditEvents] ([action], [userId], [actorUserId], [tenantId], [reason], [dataJson], [createdAt])
        VALUES
        (
            'USER_DELETED',
            @userId,
            @byUserId,
            @tenantId,
            @reason,
            (SELECT @force AS [force], @revokedSessions AS [revokedSessions], @invalidatedTokens AS [invalidatedTokens]
             FOR JSON PATH, WITHOUT_ARRAY_WRAPPER),
            @now
        );

        COMMIT TRANSACTION;

        SELECT
            @userGuid          AS [userGuid],
            @revokedSessions   AS [revokedSessions],
            @invalidatedTokens AS [invalidatedTokens];
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   7) security.uspGetOriginLockout
   Solo lectura. Estado de bloqueo de un navegador (NEX-319). La misma
   regla la aplica uspRecordLoginResult al decidir el resultado.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspGetOriginLockout]
(
    @originId VARCHAR(64)
)
AS
BEGIN
    SET NOCOUNT ON;

    SET @originId = NULLIF(LTRIM(RTRIM(@originId)), '');

    IF @originId IS NULL
        THROW 50001, 'originId is required.', 1;

    SELECT
        o.[failedAttempts],
        o.[lockedUntil],
        CAST(CASE WHEN o.[lockedUntil] IS NOT NULL THEN 1 ELSE 0 END AS BIT) AS [isLocked]
    FROM [security].[ufnGetOriginLockout](@originId, SYSDATETIME()) o;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   6) security.uspGetSession
   Solo lectura. Busca la sesión por el hash del refresh token.
   Siempre devuelve 2 result sets (vacíos si el hash no existe):
     1) sesión + usuario (sin passwordHash) + isValid ya calculado
     2) membresías activas en tenants activos
   isValid = no revocada, no expirada, usuario activo y no bloqueado.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspGetSession]
(
    @refreshTokenHash CHAR(64)
)
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE
        @now    DATETIME2 = SYSDATETIME(),
        @userId INT;

    IF @refreshTokenHash IS NULL
        THROW 50001, 'refreshTokenHash is required.', 1;

    SELECT @userId = [userId]
    FROM [security].[sessions]
    WHERE [refreshTokenHash] = @refreshTokenHash;

    SELECT
        s.[sessionGuid],
        u.[userGuid],
        s.[issuedAt],
        s.[expiresAt],
        s.[revokedAt],
        s.[revokedReason],
        CAST(CASE
                 WHEN s.[revokedAt] IS NULL
                  AND s.[expiresAt] > @now
                  AND u.[isActive] = 1
                  AND u.[isDeleted] = 0
                  AND (uc.[lockedUntil] IS NULL OR uc.[lockedUntil] <= @now)
                 THEN 1 ELSE 0
             END AS BIT) AS [isValid],
        u.[email],
        u.[firstName],
        u.[lastName],
        u.[isActive],
        u.[isDeleted],
        u.[isPlatformAdmin],
        uc.[mustChangePassword],
        uc.[failedAttempts],
        uc.[lockedUntil],
        CAST(CASE WHEN uc.[lockedUntil] > @now THEN 1 ELSE 0 END AS BIT) AS [isLocked]
    FROM [security].[sessions] s
    INNER JOIN [security].[users] u
        ON u.[userId] = s.[userId]
    LEFT JOIN [security].[userCredentials] uc
        ON uc.[userId] = u.[userId]
    WHERE s.[refreshTokenHash] = @refreshTokenHash;

    SELECT
        m.[tenantGuid],
        m.[tenantCode],
        m.[name],
        m.[slug],
        m.[tenantStatusCode],
        m.[roleCode]
    FROM [security].[ufnGetUserActiveMemberships](@userId) m
    ORDER BY m.[name];
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   8) security.uspGetSessionStatus
   Solo lectura y barata (búsqueda por UQ_sessions_sessionGuid). Es lo
   que contesta GET /auth/sesion con el sid del JWT.
   Una fila, o ninguna si la sesión no existe o no es de esa persona.
   isActive es el de la CUENTA; isValid ya resume todo.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspGetSessionStatus]
(
    @userGuid    UNIQUEIDENTIFIER,
    @sessionGuid UNIQUEIDENTIFIER
)
AS
BEGIN
    SET NOCOUNT ON;

    IF @userGuid IS NULL OR @sessionGuid IS NULL
        THROW 50001, 'userGuid and sessionGuid are required.', 1;

    SELECT
        s.[sessionGuid],
        u.[userGuid],
        s.[expiresAt],
        s.[revokedAt],
        s.[revokedReason],
        u.[isActive],
        CAST(CASE
                 WHEN s.[revokedAt] IS NULL
                  AND s.[expiresAt] > SYSDATETIME()
                  AND u.[isActive] = 1
                  AND u.[isDeleted] = 0
                 THEN 1 ELSE 0
             END AS BIT) AS [isValid]
    FROM [security].[sessions] s
    INNER JOIN [security].[users] u
        ON u.[userId] = s.[userId]
    WHERE s.[sessionGuid] = @sessionGuid
      AND u.[userGuid]    = @userGuid;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   3) security.uspGetTenant
   ===================================================================== */
CREATE   PROCEDURE [security].[uspGetTenant]
(
    @tenantGuid UNIQUEIDENTIFIER
)
AS
BEGIN
    SET NOCOUNT ON;

    IF @tenantGuid IS NULL
        THROW 50001, 'Tenant identifier is required.', 1;

    IF NOT EXISTS
    (
        SELECT 1
        FROM [security].[tenants]
        WHERE [tenantGuid] = @tenantGuid
          AND [isDeleted] = 0
    )
        THROW 50002, 'Tenant was not found.', 1;

    SELECT
        t.[tenantId],
        t.[tenantGuid],
        t.[tenantCode],
        t.[name],
        t.[slug],
        ts.[tenantStatusId],
        ts.[code] AS [tenantStatusCode],
        ts.[name] AS [tenantStatusName],
        ts.[description] AS [tenantStatusDescription],
        t.[settingsJson],
        t.[isActive],
        t.[isDeleted],
        t.[createdAt],
        t.[createdBy],
        t.[updatedAt],
        t.[updatedBy],
        t.[recoveryEmail],
        t.[recoveryPhone]
    FROM [security].[tenants] t
    INNER JOIN [reference].[tenantStatuses] ts
        ON ts.[tenantStatusId] = t.[tenantStatusId]
    WHERE t.[tenantGuid] = @tenantGuid
      AND t.[isDeleted] = 0;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   5) security.uspGetTenantUsers
   Usuarios de una organización, sin los borrados, del más nuevo al más
   viejo. hasActiveSession = 1 si tiene alguna sesión no revocada y no
   vencida («Con sesión / Sin sesión» y el botón «Cerrar sesión»).
   ===================================================================== */
CREATE   PROCEDURE [security].[uspGetTenantUsers]
(
    @tenantGuid UNIQUEIDENTIFIER
)
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE
        @now      DATETIME2 = SYSDATETIME(),
        @tenantId INT;

    IF @tenantGuid IS NULL
        THROW 50001, 'tenantGuid is required.', 1;

    SELECT @tenantId = [tenantId]
    FROM [security].[tenants]
    WHERE [tenantGuid] = @tenantGuid
      AND [isDeleted] = 0;

    IF @tenantId IS NULL
        THROW 50002, 'Tenant was not found.', 1;

    SELECT
        u.[userGuid],
        u.[email],
        u.[firstName],
        u.[lastName],
        u.[secondLastName],
        u.[phoneNumber],
        r.[code] AS [roleCode],
        u.[isActive],
        u.[createdAt],
        u.[lastLoginAt],
        uc.[mustChangePassword],
        uc.[temporaryPasswordIssuedAt],
        CAST(CASE WHEN EXISTS
             (
                 SELECT 1
                 FROM [security].[sessions] s
                 WHERE s.[userId] = u.[userId]
                   AND s.[revokedAt] IS NULL
                   AND s.[expiresAt] > @now
             ) THEN 1 ELSE 0 END AS BIT) AS [hasActiveSession]
    FROM [security].[tenantMemberships] tm
    INNER JOIN [security].[users] u
        ON u.[userId] = tm.[userId]
    INNER JOIN [security].[roles] r
        ON r.[roleId] = tm.[roleId]
    LEFT JOIN [security].[userCredentials] uc
        ON uc.[userId] = u.[userId]
    WHERE tm.[tenantId]  = @tenantId
      AND tm.[isDeleted] = 0
      AND u.[isDeleted]  = 0
    ORDER BY u.[createdAt] DESC, u.[userId] DESC;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   2) security.uspGetUserForLogin
   Igual que en NEX-319 + temporaryPasswordIssuedAt al final del
   primer result set.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspGetUserForLogin]
(
    @email VARCHAR(254)
)
AS
BEGIN
    SET NOCOUNT ON;

    DECLARE @userId INT;

    SET @email = LOWER(NULLIF(LTRIM(RTRIM(@email)), ''));

    IF @email IS NULL
        THROW 50001, 'email is required.', 1;

    SELECT @userId = [userId]
    FROM [security].[users]
    WHERE [email] = @email
      AND [isDeleted] = 0;

    SELECT
        u.[userGuid],
        u.[email],
        u.[firstName],
        u.[lastName],
        u.[isActive],
        u.[isDeleted],
        u.[isPlatformAdmin],
        uc.[passwordHash],
        uc.[mustChangePassword],
        uc.[failedAttempts],
        uc.[lockedUntil],
        CAST(CASE WHEN uc.[lockedUntil] > SYSDATETIME() THEN 1 ELSE 0 END AS BIT) AS [isLocked],
        CAST(CASE WHEN uc.[userId] IS NULL THEN 0 ELSE 1 END AS BIT)              AS [hasPassword],
        uc.[temporaryPasswordIssuedAt]
    FROM [security].[users] u
    LEFT JOIN [security].[userCredentials] uc
        ON uc.[userId] = u.[userId]
    WHERE u.[userId] = @userId;

    SELECT
        m.[tenantGuid],
        m.[tenantCode],
        m.[name],
        m.[slug],
        m.[tenantStatusCode],
        m.[roleCode]
    FROM [security].[ufnGetUserActiveMemberships](@userId) m
    ORDER BY m.[name];
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   3) security.uspInvalidateAccessTokens
   Apaga los enlaces pendientes de una persona. @kind NULL = todos los
   tipos. Devuelve cuántos apagó.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspInvalidateAccessTokens]
(
    @userGuid UNIQUEIDENTIFIER,
    @kind     VARCHAR(20) = NULL,
    @reason   VARCHAR(40)
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now         DATETIME2 = SYSDATETIME(),
        @userId      INT,
        @invalidated INT,
        @userAction  VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SET @kind   = UPPER(NULLIF(LTRIM(RTRIM(@kind)), ''));
        SET @reason = UPPER(NULLIF(LTRIM(RTRIM(@reason)), ''));

        IF @kind IS NOT NULL AND @kind NOT IN ('RECOVERY', 'ACTIVATION')
            THROW 50001, 'kind must be RECOVERY, ACTIVATION or NULL.', 1;

        IF @reason IS NULL
            THROW 50002, 'reason is required.', 1;

        IF @reason LIKE '% %'
            THROW 50003, 'reason must not contain spaces.', 1;

        SELECT @userId = [userId]
        FROM [security].[users]
        WHERE [userGuid] = @userGuid;

        IF @userId IS NULL
            THROW 50004, 'User not found.', 1;

        UPDATE [security].[accessTokens]
        SET [invalidatedAt]     = @now,
            [invalidatedReason] = @reason,
            [updatedAt]         = @now,
            [updatedBy]         = @userAction
        WHERE [userId] = @userId
          AND (@kind IS NULL OR [kind] = @kind)
          AND [usedAt] IS NULL
          AND [invalidatedAt] IS NULL;

        SET @invalidated = @@ROWCOUNT;

        SELECT @invalidated AS [invalidated];
    END TRY
    BEGIN CATCH
        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   1) security.uspListTenants
   Pantalla de organizaciones. userCount = usuarios no borrados con
   membresía vigente (activos e inactivos).
   ===================================================================== */
CREATE   PROCEDURE [security].[uspListTenants]
AS
BEGIN
    SET NOCOUNT ON;

    SELECT
        t.[tenantGuid],
        t.[tenantCode],
        t.[name],
        t.[slug],
        ts.[code] AS [tenantStatusCode],
        t.[createdAt],
        (
            SELECT COUNT(*)
            FROM [security].[tenantMemberships] tm
            INNER JOIN [security].[users] u
                ON u.[userId] = tm.[userId]
            WHERE tm.[tenantId]  = t.[tenantId]
              AND tm.[isDeleted] = 0
              AND u.[isDeleted]  = 0
        ) AS [userCount]
    FROM [security].[tenants] t
    INNER JOIN [reference].[tenantStatuses] ts
        ON ts.[tenantStatusId] = t.[tenantStatusId]
    WHERE t.[isDeleted] = 0
    ORDER BY t.[createdAt] DESC;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   3) security.uspRecordLoginResult
   El back llama esto después de verificar el hash:
     @succeeded       = 1 si la contraseña coincidió, 0 si no.
     @originId        = GUID anónimo del navegador (cookie), opcional.
     @passwordExpired = 1 si la contraseña era la temporal y ya pasaron
                        las 48 h (la cuenta la hace el back).
   El SP decide el RESULTADO FINAL, en este orden:
     - navegador bloqueado          -> LOCKED        (no entra, no cuenta)
     - usuario no existe            -> UNKNOWN_EMAIL (cuenta por navegador)
     - usuario inactivo             -> INACTIVE      (no entra, no cuenta)
     - sin credencial de contraseña -> BAD_PASSWORD  (no entra)
     - cuenta bloqueada vigente     -> LOCKED        (no entra, no cuenta)
     - contraseña incorrecta        -> BAD_PASSWORD  (+1; a los 5 bloquea
                                       15 min y el contador vuelve a 0)
     - temporal correcta caducada   -> EXPIRED       (no entra, no cuenta)
     - contraseña correcta          -> entra; contador 0, sin bloqueo,
                                       lastLoginAt = ahora
   Siempre escribe una fila en loginAttempts (con originId).
   Devuelve también el estado del navegador después del intento.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspRecordLoginResult]
(
    @userGuid        UNIQUEIDENTIFIER = NULL,
    @email           VARCHAR(254)     = NULL,
    @succeeded       BIT,
    @ipAddress       VARCHAR(45)      = NULL,
    @userAgent       NVARCHAR(500)    = NULL,
    @originId        VARCHAR(64)      = NULL,
    @passwordExpired BIT              = 0
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @maxFailedAttempts   INT = 5,
        @lockMinutes         INT = 15,
        @now                 DATETIME2 = SYSDATETIME(),
        @userId              INT,
        @userEmail           VARCHAR(254),
        @isActive            BIT,
        @hasCredential       BIT = 0,
        @failedAttempts      INT,
        @lockedUntil         DATETIME2,
        @mustChangePassword  BIT,
        @originLockedUntil   DATETIME2,
        @loginSucceeded      BIT = 0,
        @failureReason       VARCHAR(40),
        @userAction          VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SET @email    = LOWER(NULLIF(LTRIM(RTRIM(@email)), ''));
        SET @originId = NULLIF(LTRIM(RTRIM(@originId)), '');
        SET @passwordExpired = ISNULL(@passwordExpired, 0);

        IF @succeeded IS NULL
            THROW 50001, 'succeeded is required.', 1;

        IF @userGuid IS NULL AND @email IS NULL
            THROW 50002, 'userGuid or email is required.', 1;

        BEGIN TRANSACTION;

        SELECT
            @userId    = u.[userId],
            @userEmail = u.[email],
            @isActive  = u.[isActive]
        FROM [security].[users] u
        WHERE u.[isDeleted] = 0
          AND (
                   (@userGuid IS NOT NULL AND u.[userGuid] = @userGuid)
                OR (@userGuid IS NULL     AND u.[email]    = @email)
              );

        IF @userId IS NULL AND @userGuid IS NOT NULL
            THROW 50003, 'User not found.', 1;

        IF @userId IS NOT NULL
        BEGIN
            -- UPDLOCK: dos intentos simultáneos del mismo usuario se
            -- serializan y el contador no pierde incrementos.
            SELECT
                @hasCredential      = 1,
                @failedAttempts     = [failedAttempts],
                @lockedUntil        = [lockedUntil],
                @mustChangePassword = [mustChangePassword]
            FROM [security].[userCredentials] WITH (UPDLOCK, ROWLOCK)
            WHERE [userId] = @userId;
        END;

        IF @originId IS NOT NULL
            SELECT @originLockedUntil = [lockedUntil]
            FROM [security].[ufnGetOriginLockout](@originId, @now);

        IF @originLockedUntil IS NOT NULL
            SET @failureReason = 'LOCKED';
        ELSE IF @userId IS NULL
            SET @failureReason = 'UNKNOWN_EMAIL';
        ELSE IF @isActive = 0
            SET @failureReason = 'INACTIVE';
        ELSE IF @hasCredential = 0
            SET @failureReason = 'BAD_PASSWORD';
        ELSE IF @lockedUntil > @now
            SET @failureReason = 'LOCKED';
        ELSE IF @succeeded = 0
        BEGIN
            SET @failureReason  = 'BAD_PASSWORD';
            SET @failedAttempts = @failedAttempts + 1;
            SET @lockedUntil    = NULL;

            IF @failedAttempts >= @maxFailedAttempts
            BEGIN
                SET @lockedUntil    = DATEADD(MINUTE, @lockMinutes, @now);
                SET @failedAttempts = 0;
            END;

            UPDATE [security].[userCredentials]
            SET [failedAttempts] = @failedAttempts,
                [lockedUntil]    = @lockedUntil,
                [updatedAt]      = @now,
                [updatedBy]      = @userAction
            WHERE [userId] = @userId;
        END
        ELSE IF @passwordExpired = 1 AND @mustChangePassword = 1
            SET @failureReason = 'EXPIRED';
        ELSE
        BEGIN
            SET @loginSucceeded = 1;
            SET @failedAttempts = 0;
            SET @lockedUntil    = NULL;

            UPDATE [security].[userCredentials]
            SET [failedAttempts] = 0,
                [lockedUntil]    = NULL,
                [updatedAt]      = @now,
                [updatedBy]      = @userAction
            WHERE [userId] = @userId;

            UPDATE [security].[users]
            SET [lastLoginAt] = @now,
                [updatedAt]   = @now,
                [updatedBy]   = @userAction
            WHERE [userId] = @userId;
        END;

        INSERT INTO [security].[loginAttempts]
        (
            [userId],
            [email],
            [succeeded],
            [failureReason],
            [attemptedAt],
            [ipAddress],
            [userAgent],
            [originId],
            [createdBy]
        )
        VALUES
        (
            @userId,
            COALESCE(@email, @userEmail),
            @loginSucceeded,
            @failureReason,
            @now,
            @ipAddress,
            @userAgent,
            @originId,
            @userAction
        );

        COMMIT TRANSACTION;

        SELECT
            @loginSucceeded     AS [loginSucceeded],
            @failureReason      AS [failureReason],
            @failedAttempts     AS [failedAttempts],
            @lockedUntil        AS [lockedUntil],
            @mustChangePassword AS [mustChangePassword],
            o.[failedAttempts]  AS [originFailedAttempts],
            o.[lockedUntil]     AS [originLockedUntil]
        FROM (SELECT 1 AS [x]) d
        OUTER APPLY
        (
            -- Sin originId no hay estado de navegador: las dos en NULL.
            SELECT ol.[failedAttempts], ol.[lockedUntil]
            FROM [security].[ufnGetOriginLockout](@originId, @now) ol
            WHERE @originId IS NOT NULL
        ) o;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   8) security.uspReissueTemporaryPassword  (HU06 CA-5)
   Otra contraseña temporal para una cuenta que sigue en su primer
   acceso. SOLO si isActive = 1 y mustChangePassword = 1: si la persona
   terminó su primer acceso mientras el admin miraba la lista, falla
   sin escribir (50005) para no pisar la contraseña que acaba de elegir.
   Guarda el hash, temporaryPasswordIssuedAt = ahora, limpia intentos y
   bloqueo, revoca sesiones (REINVITED), invalida enlaces pendientes y
   deja INVITATION_REISSUED.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspReissueTemporaryPassword]
(
    @userGuid     UNIQUEIDENTIFIER,
    @passwordHash VARCHAR(255),
    @byUserGuid   UNIQUEIDENTIFIER,
    @tenantGuid   UNIQUEIDENTIFIER = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now                DATETIME2 = SYSDATETIME(),
        @userId             INT,
        @tenantId           INT,
        @isActive           BIT,
        @mustChangePassword BIT,
        @byUserId           INT,
        @revokedSessions    INT,
        @invalidatedTokens  INT,
        @userAction         VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SET @passwordHash = NULLIF(LTRIM(RTRIM(@passwordHash)), '');

        IF @passwordHash IS NULL
            THROW 50001, 'passwordHash is required.', 1;

        SELECT @byUserId = [userId]
        FROM [security].[users]
        WHERE [userGuid] = @byUserGuid
          AND [isDeleted] = 0;

        IF @byUserId IS NULL
            THROW 50002, 'byUserGuid was not found.', 1;

        BEGIN TRANSACTION;

        SELECT
            @userId   = [userId],
            @tenantId = [tenantId]
        FROM [security].[ufnGetScopedUser](@userGuid, @tenantGuid);

        IF @userId IS NULL
            THROW 50003, 'User not found in this organization.', 1;

        -- UPDLOCK: se serializa con el FIRST_LOGIN de uspSetPassword.
        SELECT @isActive = [isActive]
        FROM [security].[users] WITH (UPDLOCK, ROWLOCK)
        WHERE [userId] = @userId;

        SELECT @mustChangePassword = [mustChangePassword]
        FROM [security].[userCredentials] WITH (UPDLOCK, ROWLOCK)
        WHERE [userId] = @userId;

        IF @isActive = 0
            THROW 50004, 'User is inactive.', 1;

        IF ISNULL(@mustChangePassword, 0) = 0
            THROW 50005, 'User already completed the first access: the temporary password was not reissued.', 1;

        UPDATE [security].[userCredentials]
        SET [passwordHash]              = @passwordHash,
            [mustChangePassword]        = 1,
            [temporaryPasswordIssuedAt] = @now,
            [passwordChangedAt]         = @now,
            [failedAttempts]            = 0,
            [lockedUntil]               = NULL,
            [updatedAt]                 = @now,
            [updatedBy]                 = @userAction
        WHERE [userId] = @userId;

        UPDATE [security].[sessions]
        SET [revokedAt]     = @now,
            [revokedReason] = 'REINVITED',
            [updatedAt]     = @now,
            [updatedBy]     = @userAction
        WHERE [userId] = @userId
          AND [revokedAt] IS NULL;

        SET @revokedSessions = @@ROWCOUNT;

        UPDATE [security].[accessTokens]
        SET [invalidatedAt]     = @now,
            [invalidatedReason] = 'REINVITED',
            [updatedAt]         = @now,
            [updatedBy]         = @userAction
        WHERE [userId] = @userId
          AND [usedAt] IS NULL
          AND [invalidatedAt] IS NULL;

        SET @invalidatedTokens = @@ROWCOUNT;

        INSERT INTO [security].[auditEvents] ([action], [userId], [actorUserId], [tenantId], [dataJson], [createdAt])
        VALUES
        (
            'INVITATION_REISSUED',
            @userId,
            @byUserId,
            @tenantId,
            (SELECT @revokedSessions AS [revokedSessions], @invalidatedTokens AS [invalidatedTokens] FOR JSON PATH, WITHOUT_ARRAY_WRAPPER),
            @now
        );

        COMMIT TRANSACTION;

        SELECT
            @userGuid          AS [userGuid],
            @now               AS [temporaryPasswordIssuedAt],
            @revokedSessions   AS [revokedSessions],
            @invalidatedTokens AS [invalidatedTokens];
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   6) security.uspRevokeSessions
   - Con @sessionGuid: revoca esa sesión (logout). Debe ser del usuario.
   - Sin @sessionGuid: revoca todas las vigentes.
   - Con @byUserGuid: es un cierre forzado por otra persona (HU05 CA-4
     y CA-5). Se registra FORCED_LOGOUT en auditEvents con quién, el
     motivo escrito (@note) y la hora, en la misma transacción.
     Quien cierra debe ser superadministrador o alguien activo de la
     misma organización que la persona (HU11; 50006 si no).
   Idempotente: revocar algo ya revocado devuelve revoked = 0 (y el
   cierre forzado igual queda registrado).
   ===================================================================== */
CREATE   PROCEDURE [security].[uspRevokeSessions]
(
    @userGuid    UNIQUEIDENTIFIER,
    @sessionGuid UNIQUEIDENTIFIER = NULL,
    @reason      VARCHAR(40),
    @byUserGuid  UNIQUEIDENTIFIER = NULL,
    @note        NVARCHAR(200)    = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now        DATETIME2 = SYSDATETIME(),
        @userId     INT,
        @byUserId   INT,
        @tenantId   INT,
        @revoked    INT,
        @userAction VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SET @reason = UPPER(NULLIF(LTRIM(RTRIM(@reason)), ''));
        SET @note   = NULLIF(LTRIM(RTRIM(@note)), '');

        IF @reason IS NULL
            THROW 50001, 'reason is required.', 1;

        IF @reason LIKE '% %'
            THROW 50002, 'reason must not contain spaces (e.g. LOGOUT, LOGOUT_ALL).', 1;

        SELECT @userId = [userId]
        FROM [security].[users]
        WHERE [userGuid] = @userGuid;

        IF @userId IS NULL
            THROW 50003, 'User not found.', 1;

        IF @sessionGuid IS NOT NULL
           AND NOT EXISTS
           (
               SELECT 1
               FROM [security].[sessions]
               WHERE [sessionGuid] = @sessionGuid
                 AND [userId] = @userId
           )
            THROW 50004, 'Session not found for this user.', 1;

        SELECT @tenantId = [tenantId]
        FROM [security].[tenantMemberships]
        WHERE [userId] = @userId
          AND [isDeleted] = 0;

        IF @byUserGuid IS NOT NULL
        BEGIN
            SELECT @byUserId = [userId]
            FROM [security].[users]
            WHERE [userGuid] = @byUserGuid
              AND [isDeleted] = 0;

            IF @byUserId IS NULL
                THROW 50005, 'byUserGuid was not found.', 1;

            -- HU11: un cierre forzado solo lo hace un superadministrador o
            -- alguien activo de la MISMA organización que la persona.
            IF NOT EXISTS
            (
                SELECT 1
                FROM [security].[users] u
                WHERE u.[userId]   = @byUserId
                  AND u.[isActive] = 1
                  AND (
                           u.[isPlatformAdmin] = 1
                        OR EXISTS
                           (
                               SELECT 1
                               FROM [security].[tenantMemberships] tm
                               WHERE tm.[userId]     = u.[userId]
                                 AND tm.[tenantId]   = @tenantId
                                 AND tm.[isDeleted]  = 0
                                 AND tm.[isActive]   = 1
                                 AND tm.[statusCode] = 'ACTIVE'
                           )
                      )
            )
                THROW 50006, 'byUserGuid cannot close sessions of a user from another organization.', 1;
        END;

        BEGIN TRANSACTION;

        UPDATE [security].[sessions]
        SET [revokedAt]     = @now,
            [revokedReason] = @reason,
            [updatedAt]     = @now,
            [updatedBy]     = @userAction
        WHERE [userId] = @userId
          AND [revokedAt] IS NULL
          AND (@sessionGuid IS NULL OR [sessionGuid] = @sessionGuid);

        SET @revoked = @@ROWCOUNT;

        IF @byUserId IS NOT NULL
            INSERT INTO [security].[auditEvents]
            (
                [action],
                [userId],
                [actorUserId],
                [tenantId],
                [reason],
                [dataJson],
                [createdAt]
            )
            VALUES
            (
                'FORCED_LOGOUT',
                @userId,
                @byUserId,
                @tenantId,
                @note,
                (SELECT @reason AS [revokedReason], @sessionGuid AS [sessionGuid], @revoked AS [revoked]
                 FOR JSON PATH, WITHOUT_ARRAY_WRAPPER, INCLUDE_NULL_VALUES),
                @now
            );

        COMMIT TRANSACTION;

        SELECT @revoked AS [revoked];
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   9) security.uspRotateSession
   Canje del refresh token, ATÓMICO. Un refresh es de UN solo uso.
   En una transacción con UPDLOCK sobre la sesión:
     - no existe o ya se revocó (incluido ya canjeado) -> 50002
     - venció                                           -> 50003
     - cuenta desactivada o borrada                     -> 50004
   Si pasa: revoca la vieja (ROTATED) y crea la sucesora con el mismo
   ipAddress/userAgent que mande el back.
   Devuelve 2 result sets, como uspGetSession:
     1) newSessionGuid, userGuid, expiresAt + datos del usuario sin hash
     2) membresías activas en tenants activos
   Con dos peticiones simultáneas con el mismo refresh, la segunda
   espera el bloqueo, ve la sesión ya revocada y recibe 50002.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspRotateSession]
(
    @refreshTokenHash    CHAR(64),
    @newRefreshTokenHash CHAR(64),
    @newExpiresAt        DATETIME2,
    @ipAddress           VARCHAR(45)   = NULL,
    @userAgent           NVARCHAR(500) = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now            DATETIME2 = SYSDATETIME(),
        @sessionId      BIGINT,
        @userId         INT,
        @revokedAt      DATETIME2,
        @expiresAt      DATETIME2,
        @isActive       BIT,
        @isDeleted      BIT,
        @newSessionGuid UNIQUEIDENTIFIER = NEWID(),
        @userAction     VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        IF @refreshTokenHash IS NULL
            THROW 50001, 'refreshTokenHash is required.', 1;

        IF @newRefreshTokenHash IS NULL
           OR LEN(@newRefreshTokenHash) <> 64
           OR @newRefreshTokenHash LIKE '%[^0-9A-Fa-f]%'
            THROW 50005, 'newRefreshTokenHash must be exactly 64 hexadecimal characters.', 1;

        IF @newRefreshTokenHash = @refreshTokenHash
            THROW 50006, 'newRefreshTokenHash must be different from refreshTokenHash.', 1;

        IF @newExpiresAt IS NULL OR @newExpiresAt <= @now
            THROW 50007, 'newExpiresAt must be in the future.', 1;

        BEGIN TRANSACTION;

        SELECT
            @sessionId = s.[sessionId],
            @userId    = s.[userId],
            @revokedAt = s.[revokedAt],
            @expiresAt = s.[expiresAt]
        FROM [security].[sessions] s WITH (UPDLOCK, ROWLOCK)
        WHERE s.[refreshTokenHash] = @refreshTokenHash;

        IF @sessionId IS NULL OR @revokedAt IS NOT NULL
            THROW 50002, 'Refresh token does not exist or was already used.', 1;

        IF @expiresAt <= @now
            THROW 50003, 'Refresh token expired.', 1;

        SELECT
            @isActive  = [isActive],
            @isDeleted = [isDeleted]
        FROM [security].[users] WITH (UPDLOCK, ROWLOCK)
        WHERE [userId] = @userId;

        IF @isActive = 0 OR @isDeleted = 1
            THROW 50004, 'User is inactive.', 1;

        UPDATE [security].[sessions]
        SET [revokedAt]     = @now,
            [revokedReason] = 'ROTATED',
            [updatedAt]     = @now,
            [updatedBy]     = @userAction
        WHERE [sessionId] = @sessionId;

        INSERT INTO [security].[sessions]
        (
            [sessionGuid],
            [userId],
            [refreshTokenHash],
            [issuedAt],
            [expiresAt],
            [ipAddress],
            [userAgent],
            [createdBy]
        )
        VALUES
        (
            @newSessionGuid,
            @userId,
            @newRefreshTokenHash,
            @now,
            @newExpiresAt,
            @ipAddress,
            @userAgent,
            @userAction
        );

        COMMIT TRANSACTION;

        SELECT
            @newSessionGuid AS [newSessionGuid],
            u.[userGuid],
            @newExpiresAt   AS [expiresAt],
            u.[email],
            u.[firstName],
            u.[lastName],
            u.[isActive],
            u.[isDeleted],
            u.[isPlatformAdmin],
            uc.[mustChangePassword],
            uc.[failedAttempts],
            uc.[lockedUntil],
            CAST(CASE WHEN uc.[lockedUntil] > @now THEN 1 ELSE 0 END AS BIT) AS [isLocked],
            CAST(CASE WHEN uc.[userId] IS NULL THEN 0 ELSE 1 END AS BIT)     AS [hasPassword],
            uc.[temporaryPasswordIssuedAt]
        FROM [security].[users] u
        LEFT JOIN [security].[userCredentials] uc
            ON uc.[userId] = u.[userId]
        WHERE u.[userId] = @userId;

        SELECT
            m.[tenantGuid],
            m.[tenantCode],
            m.[name],
            m.[slug],
            m.[tenantStatusCode],
            m.[roleCode]
        FROM [security].[ufnGetUserActiveMemberships](@userId) m
        ORDER BY m.[name];
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;

        IF ERROR_NUMBER() IN (2601, 2627)
            THROW 50008, 'newRefreshTokenHash is already in use.', 1;

        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   4) security.uspSetMembershipRole  (HU07)
   Cambia el rol de la persona en su organización. No permite dejar a
   la organización sin ADMIN activo. Deja ROLE_CHANGED en auditoría.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspSetMembershipRole]
(
    @userGuid   UNIQUEIDENTIFIER,
    @tenantGuid UNIQUEIDENTIFIER,
    @roleCode   VARCHAR(40),
    @byUserGuid UNIQUEIDENTIFIER = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now          DATETIME2 = SYSDATETIME(),
        @userId       INT,
        @tenantId     INT,
        @membershipId INT,
        @oldRoleCode  VARCHAR(40),
        @roleId       INT,
        @byUserId     INT,
        @userAction   VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SET @roleCode = UPPER(NULLIF(LTRIM(RTRIM(@roleCode)), ''));

        IF @tenantGuid IS NULL
            THROW 50001, 'tenantGuid is required.', 1;

        SELECT @roleId = [roleId]
        FROM [security].[roles]
        WHERE [code]      = @roleCode
          AND [isActive]  = 1
          AND [isDeleted] = 0;

        IF @roleId IS NULL
            THROW 50002, 'roleCode was not found or is not active.', 1;

        IF @byUserGuid IS NOT NULL
        BEGIN
            SELECT @byUserId = [userId]
            FROM [security].[users]
            WHERE [userGuid] = @byUserGuid
              AND [isDeleted] = 0;

            IF @byUserId IS NULL
                THROW 50003, 'byUserGuid was not found.', 1;
        END;

        BEGIN TRANSACTION;

        SELECT
            @userId       = [userId],
            @tenantId     = [tenantId],
            @membershipId = [tenantMembershipId],
            @oldRoleCode  = [roleCode]
        FROM [security].[ufnGetScopedUser](@userGuid, @tenantGuid);

        IF @membershipId IS NULL
            THROW 50004, 'User not found in this organization.', 1;

        -- Serializa con otros cambios de rol, desactivaciones y bajas
        -- de la misma organización antes de contar administradores.
        IF @oldRoleCode = 'ADMIN'
           AND @roleCode <> 'ADMIN'
           AND NOT EXISTS
           (
               SELECT 1
               FROM [security].[tenantMemberships] tm WITH (UPDLOCK, HOLDLOCK)
               INNER JOIN [security].[roles] r
                   ON r.[roleId] = tm.[roleId]
               INNER JOIN [security].[users] u
                   ON u.[userId] = tm.[userId]
               WHERE tm.[tenantId]  = @tenantId
                 AND tm.[userId]   <> @userId
                 AND tm.[isDeleted] = 0
                 AND r.[code]       = 'ADMIN'
                 AND u.[isActive]   = 1
                 AND u.[isDeleted]  = 0
           )
            THROW 50005, 'This user is the last active ADMIN of the organization.', 1;

        IF @oldRoleCode <> @roleCode
        BEGIN
            UPDATE [security].[tenantMemberships]
            SET [roleId]    = @roleId,
                [updatedAt] = @now,
                [updatedBy] = @userAction
            WHERE [tenantMembershipId] = @membershipId;

            INSERT INTO [security].[auditEvents] ([action], [userId], [actorUserId], [tenantId], [dataJson], [createdAt])
            VALUES
            (
                'ROLE_CHANGED',
                @userId,
                @byUserId,
                @tenantId,
                (SELECT @oldRoleCode AS [fromRoleCode], @roleCode AS [toRoleCode] FOR JSON PATH, WITHOUT_ARRAY_WRAPPER),
                @now
            );
        END;

        COMMIT TRANSACTION;

        SELECT
            @userGuid AS [userGuid],
            @roleCode AS [roleCode],
            CAST(CASE WHEN @oldRoleCode <> @roleCode THEN 1 ELSE 0 END AS BIT) AS [changed];
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   4) security.uspSetPassword
   @reason: FIRST_LOGIN | RESET | CHANGE
   - FIRST_LOGIN / CHANGE: la persona elige su contraseña
       -> mustChangePassword = 0, temporaryPasswordIssuedAt = NULL
   - RESET: contraseña temporal
       -> mustChangePassword = 1, temporaryPasswordIssuedAt = ahora
   - Siempre: passwordChangedAt = ahora, limpia intentos y bloqueo,
     revoca TODAS las sesiones vigentes (PASSWORD_CHANGED) e invalida
     los enlaces pendientes (PASSWORD_CHANGED).
   Condiciones contra carreras (HU13), opcionales:
   - @sessionGuid: el cambio solo procede si esa sesión es de la
     persona, sigue viva y la cuenta está activa.
   - @expectedPasswordHash: solo procede si el hash vigente es ESE.
   Si alguna no se cumple, falla sin escribir.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspSetPassword]
(
    @userGuid             UNIQUEIDENTIFIER,
    @passwordHash         VARCHAR(255),
    @reason               VARCHAR(20),
    @sessionGuid          UNIQUEIDENTIFIER = NULL,
    @expectedPasswordHash VARCHAR(255)     = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now                  DATETIME2 = SYSDATETIME(),
        @userId               INT,
        @isActive             BIT,
        @currentPasswordHash  VARCHAR(255),
        @mustChangePassword   BIT,
        @temporaryIssuedAt    DATETIME2,
        @revokedSessions      INT,
        @invalidatedTokens    INT,
        @userAction           VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SET @passwordHash         = NULLIF(LTRIM(RTRIM(@passwordHash)), '');
        SET @expectedPasswordHash = NULLIF(LTRIM(RTRIM(@expectedPasswordHash)), '');
        SET @reason               = UPPER(LTRIM(RTRIM(@reason)));

        IF @passwordHash IS NULL
            THROW 50001, 'passwordHash is required.', 1;

        IF @reason IS NULL OR @reason NOT IN ('FIRST_LOGIN', 'RESET', 'CHANGE')
            THROW 50002, 'reason must be FIRST_LOGIN, RESET or CHANGE.', 1;

        SET @mustChangePassword = CASE WHEN @reason = 'RESET' THEN 1 ELSE 0 END;
        SET @temporaryIssuedAt  = CASE WHEN @reason = 'RESET' THEN @now END;

        BEGIN TRANSACTION;

        -- UPDLOCK en users y en la credencial: se serializa con
        -- uspCreateSession, uspSetUserActive y otro uspSetPassword.
        SELECT
            @userId   = [userId],
            @isActive = [isActive]
        FROM [security].[users] WITH (UPDLOCK, ROWLOCK)
        WHERE [userGuid] = @userGuid
          AND [isDeleted] = 0;

        IF @userId IS NULL
            THROW 50003, 'User not found.', 1;

        SELECT @currentPasswordHash = [passwordHash]
        FROM [security].[userCredentials] WITH (UPDLOCK, ROWLOCK)
        WHERE [userId] = @userId;

        IF @sessionGuid IS NOT NULL
        BEGIN
            IF @isActive = 0
                THROW 50004, 'User is inactive.', 1;

            IF NOT EXISTS
            (
                SELECT 1
                FROM [security].[sessions] WITH (UPDLOCK, ROWLOCK)
                WHERE [sessionGuid] = @sessionGuid
                  AND [userId]      = @userId
                  AND [revokedAt] IS NULL
                  AND [expiresAt] > @now
            )
                THROW 50005, 'Session is not active for this user.', 1;
        END;

        IF @expectedPasswordHash IS NOT NULL
           AND (@currentPasswordHash IS NULL OR @currentPasswordHash <> @expectedPasswordHash)
            THROW 50006, 'The current password changed: expectedPasswordHash does not match.', 1;

        UPDATE [security].[userCredentials]
        SET [passwordHash]              = @passwordHash,
            [mustChangePassword]        = @mustChangePassword,
            [temporaryPasswordIssuedAt] = @temporaryIssuedAt,
            [passwordChangedAt]         = @now,
            [failedAttempts]            = 0,
            [lockedUntil]               = NULL,
            [updatedAt]                 = @now,
            [updatedBy]                 = @userAction
        WHERE [userId] = @userId;

        IF @@ROWCOUNT = 0
        BEGIN
            INSERT INTO [security].[userCredentials]
            (
                [userId],
                [passwordHash],
                [mustChangePassword],
                [temporaryPasswordIssuedAt],
                [passwordChangedAt],
                [createdBy]
            )
            VALUES
            (
                @userId,
                @passwordHash,
                @mustChangePassword,
                @temporaryIssuedAt,
                @now,
                @userAction
            );
        END;

        UPDATE [security].[sessions]
        SET [revokedAt]     = @now,
            [revokedReason] = 'PASSWORD_CHANGED',
            [updatedAt]     = @now,
            [updatedBy]     = @userAction
        WHERE [userId] = @userId
          AND [revokedAt] IS NULL;

        SET @revokedSessions = @@ROWCOUNT;

        UPDATE [security].[accessTokens]
        SET [invalidatedAt]     = @now,
            [invalidatedReason] = 'PASSWORD_CHANGED',
            [updatedAt]         = @now,
            [updatedBy]         = @userAction
        WHERE [userId] = @userId
          AND [usedAt] IS NULL
          AND [invalidatedAt] IS NULL;

        SET @invalidatedTokens = @@ROWCOUNT;

        COMMIT TRANSACTION;

        SELECT
            @mustChangePassword AS [mustChangePassword],
            @revokedSessions    AS [revokedSessions],
            @invalidatedTokens  AS [invalidatedTokens];
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   6) security.uspSetUserActive  (HU08, HU09)
   - Guarda el motivo, quién y cuándo en auditEvents
     (USER_DEACTIVATED / USER_REACTIVATED), en la misma transacción.
   - Al desactivar: revoca todas sus sesiones (USER_DEACTIVATED) e
     invalida sus enlaces pendientes. Sin esto, el JWT de 15 minutos lo
     dejaría entrar hasta que venciera.
   - No desactiva al último ADMIN activo de la organización ni al
     último superadministrador activo.
   - Si ya estaba en ese estado no escribe nada y devuelve changed = 0.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspSetUserActive]
(
    @userGuid   UNIQUEIDENTIFIER,
    @isActive   BIT,
    @reason     NVARCHAR(200),
    @byUserGuid UNIQUEIDENTIFIER,
    @tenantGuid UNIQUEIDENTIFIER = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @now             DATETIME2 = SYSDATETIME(),
        @userId          INT,
        @tenantId        INT,
        @roleCode        VARCHAR(40),
        @isPlatformAdmin BIT,
        @currentIsActive BIT,
        @byUserId        INT,
        @changed         BIT = 0,
        @revokedSessions INT = 0,
        @userAction      VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SET @reason = NULLIF(LTRIM(RTRIM(@reason)), '');

        IF @isActive IS NULL
            THROW 50001, 'isActive is required.', 1;

        IF @reason IS NULL
            THROW 50002, 'reason is required.', 1;

        SELECT @byUserId = [userId]
        FROM [security].[users]
        WHERE [userGuid] = @byUserGuid
          AND [isDeleted] = 0;

        IF @byUserId IS NULL
            THROW 50003, 'byUserGuid was not found.', 1;

        BEGIN TRANSACTION;

        SELECT
            @userId          = [userId],
            @tenantId        = [tenantId],
            @roleCode        = [roleCode],
            @isPlatformAdmin = [isPlatformAdmin]
        FROM [security].[ufnGetScopedUser](@userGuid, @tenantGuid);

        IF @userId IS NULL
            THROW 50004, 'User not found in this organization.', 1;

        SELECT @currentIsActive = [isActive]
        FROM [security].[users] WITH (UPDLOCK, ROWLOCK)
        WHERE [userId] = @userId;

        IF @currentIsActive <> @isActive
        BEGIN
            IF @isActive = 0 AND @roleCode = 'ADMIN'
               AND NOT EXISTS
               (
                   SELECT 1
                   FROM [security].[tenantMemberships] tm WITH (UPDLOCK, HOLDLOCK)
                   INNER JOIN [security].[roles] r
                       ON r.[roleId] = tm.[roleId]
                   INNER JOIN [security].[users] u
                       ON u.[userId] = tm.[userId]
                   WHERE tm.[tenantId]  = @tenantId
                     AND tm.[userId]   <> @userId
                     AND tm.[isDeleted] = 0
                     AND r.[code]       = 'ADMIN'
                     AND u.[isActive]   = 1
                     AND u.[isDeleted]  = 0
               )
                THROW 50005, 'This user is the last active ADMIN of the organization.', 1;

            IF @isActive = 0 AND @isPlatformAdmin = 1
               AND NOT EXISTS
               (
                   SELECT 1
                   FROM [security].[users] WITH (UPDLOCK, HOLDLOCK)
                   WHERE [isPlatformAdmin] = 1
                     AND [isActive]  = 1
                     AND [isDeleted] = 0
                     AND [userId]   <> @userId
               )
                THROW 50006, 'This user is the last active platform admin.', 1;

            UPDATE [security].[users]
            SET [isActive]  = @isActive,
                [updatedAt] = @now,
                [updatedBy] = @userAction
            WHERE [userId] = @userId;

            IF @isActive = 0
            BEGIN
                UPDATE [security].[sessions]
                SET [revokedAt]     = @now,
                    [revokedReason] = 'USER_DEACTIVATED',
                    [updatedAt]     = @now,
                    [updatedBy]     = @userAction
                WHERE [userId] = @userId
                  AND [revokedAt] IS NULL;

                SET @revokedSessions = @@ROWCOUNT;

                UPDATE [security].[accessTokens]
                SET [invalidatedAt]     = @now,
                    [invalidatedReason] = 'USER_DEACTIVATED',
                    [updatedAt]         = @now,
                    [updatedBy]         = @userAction
                WHERE [userId] = @userId
                  AND [usedAt] IS NULL
                  AND [invalidatedAt] IS NULL;
            END;

            INSERT INTO [security].[auditEvents] ([action], [userId], [actorUserId], [tenantId], [reason], [dataJson], [createdAt])
            VALUES
            (
                CASE WHEN @isActive = 1 THEN 'USER_REACTIVATED' ELSE 'USER_DEACTIVATED' END,
                @userId,
                @byUserId,
                @tenantId,
                @reason,
                (SELECT @revokedSessions AS [revokedSessions] FOR JSON PATH, WITHOUT_ARRAY_WRAPPER),
                @now
            );

            SET @changed = 1;
        END;

        COMMIT TRANSACTION;

        SELECT
            u.[userGuid],
            u.[email],
            u.[firstName],
            u.[lastName],
            u.[isActive],
            u.[updatedAt],
            @changed         AS [changed],
            @revokedSessions AS [revokedSessions]
        FROM [security].[users] u
        WHERE u.[userId] = @userId;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;
        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   2) security.uspUpdateTenant
   ===================================================================== */
CREATE   PROCEDURE [security].[uspUpdateTenant]
(
    @tenantGuid       UNIQUEIDENTIFIER,
    @name             NVARCHAR(200) = NULL,
    @slug             VARCHAR(63)   = NULL,
    @tenantStatusCode VARCHAR(20)   = NULL,
    @settingsJson     NVARCHAR(MAX) = NULL,
    @recoveryEmail    VARCHAR(254)  = NULL,
    @recoveryPhone    VARCHAR(25)   = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @tenantId INT,
        @tenantStatusId INT,
        @userAction VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        IF @tenantGuid IS NULL
            THROW 50001, 'Tenant identifier is required.', 1;

        SET @name =
            NULLIF(LTRIM(RTRIM(@name)), '');

        SET @slug =
            LOWER(NULLIF(LTRIM(RTRIM(@slug)), ''));

        SET @tenantStatusCode =
            UPPER(NULLIF(LTRIM(RTRIM(@tenantStatusCode)), ''));

        SET @recoveryEmail =
            LOWER(NULLIF(LTRIM(RTRIM(@recoveryEmail)), ''));

        SET @recoveryPhone =
            NULLIF(LTRIM(RTRIM(@recoveryPhone)), '');

        SELECT
            @tenantId = [tenantId]
        FROM [security].[tenants]
        WHERE [tenantGuid] = @tenantGuid
          AND [isDeleted] = 0;

        IF @tenantId IS NULL
            THROW 50002, 'Tenant was not found.', 1;

        IF @settingsJson IS NOT NULL
           AND ISJSON(@settingsJson) = 0
            THROW 50003, 'Tenant settings must contain valid JSON.', 1;

        IF @recoveryEmail IS NOT NULL
           AND @recoveryEmail NOT LIKE '%_@_%._%'
            THROW 50006, 'recoveryEmail is not a valid email.', 1;

        IF @slug IS NOT NULL
           AND EXISTS
           (
               SELECT 1
               FROM [security].[tenants]
               WHERE [slug] = @slug
                 AND [tenantId] <> @tenantId
                 AND [isDeleted] = 0
           )
            THROW 50004, 'A tenant with the same slug already exists.', 1;

        IF @tenantStatusCode IS NOT NULL
        BEGIN
            SELECT
                @tenantStatusId = [tenantStatusId]
            FROM [reference].[tenantStatuses]
            WHERE [code] = @tenantStatusCode
              AND [isActive] = 1
              AND [isDeleted] = 0;

            IF @tenantStatusId IS NULL
                THROW 50005, 'Tenant status was not found.', 1;
        END;

        BEGIN TRANSACTION;

        UPDATE [security].[tenants]
        SET
            [tenantStatusId] = COALESCE
            (
                @tenantStatusId,
                [tenantStatusId]
            ),
            [name] = COALESCE
            (
                @name,
                [name]
            ),
            [slug] = COALESCE
            (
                @slug,
                [slug]
            ),
            [settingsJson] = COALESCE
            (
                @settingsJson,
                [settingsJson]
            ),
            [recoveryEmail] = COALESCE
            (
                @recoveryEmail,
                [recoveryEmail]
            ),
            [recoveryPhone] = COALESCE
            (
                @recoveryPhone,
                [recoveryPhone]
            ),
            [isActive] =
                CASE
                    WHEN @tenantStatusCode = 'ACTIVE'
                        THEN 1
                    WHEN @tenantStatusCode IN ('SUSPENDED', 'CLOSED')
                        THEN 0
                    ELSE [isActive]
                END,
            [updatedAt] = SYSDATETIME(),
            [updatedBy] = @userAction
        WHERE [tenantId] = @tenantId
          AND [isDeleted] = 0;

        COMMIT TRANSACTION;

        SELECT
            t.[tenantId],
            t.[tenantGuid],
            t.[tenantCode],
            t.[name],
            t.[slug],
            ts.[code] AS [tenantStatusCode],
            ts.[name] AS [tenantStatusName],
            t.[settingsJson],
            t.[isActive],
            t.[isDeleted],
            t.[createdAt],
            t.[createdBy],
            t.[updatedAt],
            t.[updatedBy],
            t.[recoveryEmail],
            t.[recoveryPhone]
        FROM [security].[tenants] t
        INNER JOIN [reference].[tenantStatuses] ts
            ON ts.[tenantStatusId] = t.[tenantStatusId]
        WHERE t.[tenantId] = @tenantId;
    END TRY
    BEGIN CATCH
        IF @@TRANCOUNT > 0
            ROLLBACK TRANSACTION;

        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   3) security.uspUpdateUser
   Datos de la persona que edita un administrador. El correo NO se
   cambia (es con el que se inicia sesión). Reemplaza los campos.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspUpdateUser]
(
    @userGuid       UNIQUEIDENTIFIER,
    @firstName      NVARCHAR(200),
    @lastName       NVARCHAR(200),
    @secondLastName NVARCHAR(200)    = NULL,
    @phoneNumber    VARCHAR(20)      = NULL,
    @tenantGuid     UNIQUEIDENTIFIER = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @userId     INT,
        @userAction VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SET @firstName      = NULLIF(LTRIM(RTRIM(@firstName)), '');
        SET @lastName       = NULLIF(LTRIM(RTRIM(@lastName)), '');
        SET @secondLastName = NULLIF(LTRIM(RTRIM(@secondLastName)), '');
        SET @phoneNumber    = NULLIF(LTRIM(RTRIM(@phoneNumber)), '');

        IF @firstName IS NULL
            THROW 50001, 'firstName is required.', 1;

        IF @lastName IS NULL
            THROW 50002, 'lastName is required.', 1;

        SELECT @userId = [userId]
        FROM [security].[ufnGetScopedUser](@userGuid, @tenantGuid);

        IF @userId IS NULL
            THROW 50003, 'User not found in this organization.', 1;

        UPDATE [security].[users]
        SET [firstName]      = @firstName,
            [lastName]       = @lastName,
            [secondLastName] = @secondLastName,
            [phoneNumber]    = @phoneNumber,
            [updatedAt]      = SYSDATETIME(),
            [updatedBy]      = @userAction
        WHERE [userId] = @userId;

        SELECT
            u.[userGuid],
            u.[email],
            u.[firstName],
            u.[lastName],
            u.[secondLastName],
            u.[phoneNumber],
            u.[updatedAt]
        FROM [security].[users] u
        WHERE u.[userId] = @userId;
    END TRY
    BEGIN CATCH
        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   7) security.uspUpdateUserProfile  (HU12)
   Lo que cada quien edita de sí mismo. No toca el correo de inicio de
   sesión ni el rol. Reemplaza los campos. El back ya normaliza el
   correo de recuperación; aquí se repite la red mínima.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspUpdateUserProfile]
(
    @userGuid       UNIQUEIDENTIFIER,
    @firstName      NVARCHAR(200),
    @lastName       NVARCHAR(200),
    @secondLastName NVARCHAR(200)    = NULL,
    @phoneNumber    VARCHAR(20)      = NULL,
    @recoveryEmail  VARCHAR(254)     = NULL,
    @recoveryPhone  VARCHAR(25)      = NULL,
    @tenantGuid     UNIQUEIDENTIFIER = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @userId     INT,
        @email      VARCHAR(254),
        @userAction VARCHAR(50) = SUSER_SNAME();

    BEGIN TRY
        SET @firstName      = NULLIF(LTRIM(RTRIM(@firstName)), '');
        SET @lastName       = NULLIF(LTRIM(RTRIM(@lastName)), '');
        SET @secondLastName = NULLIF(LTRIM(RTRIM(@secondLastName)), '');
        SET @phoneNumber    = NULLIF(LTRIM(RTRIM(@phoneNumber)), '');
        SET @recoveryEmail  = LOWER(NULLIF(LTRIM(RTRIM(@recoveryEmail)), ''));
        SET @recoveryPhone  = NULLIF(LTRIM(RTRIM(@recoveryPhone)), '');

        IF @firstName IS NULL
            THROW 50001, 'firstName is required.', 1;

        IF @lastName IS NULL
            THROW 50002, 'lastName is required.', 1;

        IF @recoveryEmail IS NOT NULL
           AND @recoveryEmail NOT LIKE '%_@_%._%'
            THROW 50003, 'recoveryEmail is not a valid email.', 1;

        SELECT
            @userId = [userId],
            @email  = [email]
        FROM [security].[ufnGetScopedUser](@userGuid, @tenantGuid);

        IF @userId IS NULL
            THROW 50004, 'User not found in this organization.', 1;

        IF @recoveryEmail = @email
            THROW 50005, 'recoveryEmail must be different from the login email.', 1;

        UPDATE [security].[users]
        SET [firstName]      = @firstName,
            [lastName]       = @lastName,
            [secondLastName] = @secondLastName,
            [phoneNumber]    = @phoneNumber,
            [recoveryEmail]  = @recoveryEmail,
            [recoveryPhone]  = @recoveryPhone,
            [updatedAt]      = SYSDATETIME(),
            [updatedBy]      = @userAction
        WHERE [userId] = @userId;

        SELECT
            u.[userGuid],
            u.[email],
            u.[firstName],
            u.[lastName],
            u.[secondLastName],
            u.[phoneNumber],
            u.[recoveryEmail],
            u.[recoveryPhone],
            u.[updatedAt]
        FROM [security].[users] u
        WHERE u.[userId] = @userId;
    END TRY
    BEGIN CATCH
        THROW;
    END CATCH;
END;
GO

SET ANSI_NULLS ON
SET QUOTED_IDENTIFIER ON
GO
/* =====================================================================
   4) security.uspWriteAuditEvent
   Escribe un evento en auditEvents. Para lo que solo conoce el back
   (PASSWORD_RESET_REQUESTED, PASSWORD_RESET_COMPLETED, LOGOUT).
   - @action en mayúsculas, sin espacios, hasta 40 caracteres.
   - Los GUID que lleguen tienen que existir; NULL está permitido
     (la solicitud de recuperación con un correo inexistente).
   - @dataJson debe ser JSON válido. Nunca contraseñas ni tokens.
   ===================================================================== */
CREATE   PROCEDURE [security].[uspWriteAuditEvent]
(
    @action        VARCHAR(40),
    @userGuid      UNIQUEIDENTIFIER = NULL,
    @actorUserGuid UNIQUEIDENTIFIER = NULL,
    @tenantGuid    UNIQUEIDENTIFIER = NULL,
    @reason        NVARCHAR(200)    = NULL,
    @dataJson      NVARCHAR(MAX)    = NULL
)
AS
BEGIN
    SET NOCOUNT ON;
    SET XACT_ABORT ON;

    DECLARE
        @userId         INT,
        @actorUserId    INT,
        @tenantId       INT,
        @auditEventGuid UNIQUEIDENTIFIER = NEWID(),
        @now            DATETIME2 = SYSDATETIME();

    BEGIN TRY
        SET @action   = UPPER(NULLIF(LTRIM(RTRIM(@action)), ''));
        SET @reason   = NULLIF(LTRIM(RTRIM(@reason)), '');
        SET @dataJson = NULLIF(LTRIM(RTRIM(@dataJson)), '');

        IF @action IS NULL
            THROW 50001, 'action is required.', 1;

        IF @action LIKE '%[^A-Z0-9_]%'
            THROW 50002, 'action must contain only A-Z, 0-9 and underscore (e.g. PASSWORD_RESET_REQUESTED).', 1;

        IF @dataJson IS NOT NULL AND ISJSON(@dataJson) = 0
            THROW 50003, 'dataJson must contain valid JSON.', 1;

        IF @userGuid IS NOT NULL
        BEGIN
            SELECT @userId = [userId]
            FROM [security].[users]
            WHERE [userGuid] = @userGuid;

            IF @userId IS NULL
                THROW 50004, 'userGuid was not found.', 1;
        END;

        IF @actorUserGuid IS NOT NULL
        BEGIN
            SELECT @actorUserId = [userId]
            FROM [security].[users]
            WHERE [userGuid] = @actorUserGuid;

            IF @actorUserId IS NULL
                THROW 50005, 'actorUserGuid was not found.', 1;
        END;

        IF @tenantGuid IS NOT NULL
        BEGIN
            SELECT @tenantId = [tenantId]
            FROM [security].[tenants]
            WHERE [tenantGuid] = @tenantGuid;

            IF @tenantId IS NULL
                THROW 50006, 'tenantGuid was not found.', 1;
        END;

        INSERT INTO [security].[auditEvents]
        (
            [auditEventGuid],
            [action],
            [userId],
            [actorUserId],
            [tenantId],
            [reason],
            [dataJson],
            [createdAt]
        )
        VALUES
        (
            @auditEventGuid,
            @action,
            @userId,
            @actorUserId,
            @tenantId,
            @reason,
            @dataJson,
            @now
        );

        SELECT
            @auditEventGuid AS [auditEventGuid],
            @now            AS [createdAt];
    END TRY
    BEGIN CATCH
        THROW;
    END CATCH;
END;
GO
