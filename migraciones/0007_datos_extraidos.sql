-- 0007 · Lo que se sabe de cada documento: su clasificación y sus datos extraídos (entity_fact), en SQL (Moibe, 2026-10-29).
--
-- «Una cosa es el file y otra los datos que se extrajeron». Hasta hoy el Pipeline clasificaba y extraía en el navegador y no guardaba
-- nada: el documento (`nexus.documents`) existía, pero no sabía qué tipo documental era ni qué decía. Estas tablas lo guardan, con el
-- diseño del diccionario v0.4 (`extraction_run`, `entity_fact`, `field_correction`, `v_entity_fact_current`):
--
--   documentClassifications  cada vez que el clasificador opina sobre un documento (categoría, tipo documental, confianza).
--   extractionRuns           cada extracción de un documento: con qué tipo documental (y qué revisión de su configuración), qué
--                            procesador de Google y en qué versión, cuándo, quién, y cómo terminó.
--   entityFacts              UN renglón por campo, por corrida. Valor crudo y normalizado, confianza 0-100 con la original y su método,
--                            página. Un campo que el esquema pedía y no apareció TAMBIÉN tiene renglón, con `nullReason`.
--   fieldCorrections         lo que una persona corrige a mano. Nunca se toca el dato original.
--   vEntityFactCurrent       el valor vigente de cada campo de cada documento: el de su última corrida, o su última corrección.
--
-- Todo es de SOLO AGREGAR: reprocesar un documento crea otra corrida, y la anterior se queda como historia.
-- Lo extraído antes de esta migración no se guardó en ningún lado y no se recupera (Moibe: «podemos prescindir de lo que no se guardó»).
-- Fuera de esta entrega, a propósito: la capa de OCR (`ocr_result`/`ocr_block`). Cuando llegue, `entityFacts` gana `ocrBlockId`.
--
-- Nacen directo en SQL, sin gemelo en JSON (decisión del 2026-10-28). Arrancan vacías.

IF SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Falta el esquema nexus: lo crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
GO

-- ── Clasificaciones ─────────────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[documentClassifications] (
    [classificationId] bigint IDENTITY(1,1) NOT NULL,
    [documentId] bigint NOT NULL,
    -- Lo que dijo el clasificador, tal cual: la categoría es el id normalizado del tipo documental, o `otro`. NULL si no dijo nada.
    [category] varchar(120) NULL,
    -- El tipo documental al que corresponde esa categoría en la biblioteca de la organización. NULL con `otro`, o si ya no existe.
    [documentTypeId] bigint NULL,
    [confidence] decimal(5,2) NULL,
    [processorId] varchar(120) NOT NULL,
    [classifiedAt] datetime2(7) NOT NULL CONSTRAINT [DF_documentClassifications_classifiedAt] DEFAULT (sysutcdatetime()),
    [classifiedBy] varchar(254) NOT NULL,

    CONSTRAINT [PK_documentClassifications] PRIMARY KEY CLUSTERED ([classificationId]),
    CONSTRAINT [FK_documentClassifications_document] FOREIGN KEY ([documentId]) REFERENCES [nexus].[documents] ([documentId]) ON DELETE CASCADE,
    CONSTRAINT [FK_documentClassifications_documentType] FOREIGN KEY ([documentTypeId]) REFERENCES [nexus].[documentTypes] ([documentTypeId]) ON DELETE SET NULL,
    CONSTRAINT [CK_documentClassifications_confidence] CHECK ([confidence] IS NULL OR [confidence] BETWEEN (0) AND (100))
);
GO

CREATE NONCLUSTERED INDEX [IX_documentClassifications_document] ON [nexus].[documentClassifications] ([documentId], [classificationId]);
GO

-- ── Corridas de extracción ──────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[extractionRuns] (
    [runId] bigint IDENTITY(1,1) NOT NULL,
    -- `extraccion-` + 32 hexadecimales; lo pone el servidor.
    [publicId] varchar(63) NOT NULL,
    [documentId] bigint NOT NULL,
    -- Con qué tipo documental se extrajo. Si el tipo se borra, la corrida se queda (es historia) y conserva su id y nombre de entonces.
    [documentTypeId] bigint NULL,
    [documentTypePublicId] varchar(63) NOT NULL,
    [documentTypeName] nvarchar(200) NULL,
    -- La revisión del tipo documental al momento de extraer: es la «versión de la configuración» del diccionario.
    [documentTypeRevision] int NULL,
    [engine] varchar(40) NOT NULL,
    [processorId] varchar(120) NOT NULL,
    -- NULL = no se pidió versión y Google usó su default de ese momento.
    [processorVersion] varchar(120) NULL,
    -- `ok`: extrajo; `no_reconocido`: Google contestó sin ningún campo; `error`: la llamada falló (el motivo en `errorDetail`).
    [status] varchar(15) NOT NULL,
    [errorDetail] nvarchar(1000) NULL,
    [confidenceAverage] decimal(5,2) NULL,
    [confidenceMinimum] decimal(5,2) NULL,
    [pageCount] int NULL,
    -- El `_metadata` de la respuesta (bloques, páginas cobradas, campos ausentes…), tal cual.
    [metadataJson] nvarchar(MAX) NULL,
    [startedAt] datetime2(7) NOT NULL,
    [finishedAt] datetime2(7) NOT NULL CONSTRAINT [DF_extractionRuns_finishedAt] DEFAULT (sysutcdatetime()),
    [createdBy] varchar(254) NOT NULL,

    CONSTRAINT [PK_extractionRuns] PRIMARY KEY CLUSTERED ([runId]),
    CONSTRAINT [UQ_extractionRuns_publicId] UNIQUE NONCLUSTERED ([publicId]),
    CONSTRAINT [FK_extractionRuns_document] FOREIGN KEY ([documentId]) REFERENCES [nexus].[documents] ([documentId]) ON DELETE CASCADE,
    CONSTRAINT [FK_extractionRuns_documentType] FOREIGN KEY ([documentTypeId]) REFERENCES [nexus].[documentTypes] ([documentTypeId]) ON DELETE SET NULL,
    CONSTRAINT [CK_extractionRuns_publicId] CHECK ([publicId] LIKE 'extraccion-%' AND [publicId] COLLATE Latin1_General_BIN2 = lower([publicId]) AND len([publicId]) = (43)),
    CONSTRAINT [CK_extractionRuns_status] CHECK ([status] COLLATE Latin1_General_BIN2 IN ('ok', 'no_reconocido', 'error')),
    CONSTRAINT [CK_extractionRuns_error] CHECK (([status] = 'error' AND [errorDetail] IS NOT NULL) OR ([status] <> 'error' AND [errorDetail] IS NULL)),
    CONSTRAINT [CK_extractionRuns_confidence] CHECK (([confidenceAverage] IS NULL OR [confidenceAverage] BETWEEN (0) AND (100)) AND ([confidenceMinimum] IS NULL OR [confidenceMinimum] BETWEEN (0) AND (100)))
);
GO

-- La última corrida de un documento es la que manda.
CREATE NONCLUSTERED INDEX [IX_extractionRuns_document] ON [nexus].[extractionRuns] ([documentId], [runId]) INCLUDE ([status], [documentTypeId]);
GO

-- ── Datos extraídos: un renglón por campo, por corrida ──────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[entityFacts] (
    [factId] bigint IDENTITY(1,1) NOT NULL,
    [runId] bigint NOT NULL,
    -- Dónde vive el campo: `rfc`, `domicilio.calle`, `accionistas[2].nombre` (las filas de un grupo que se repite cuentan desde 1).
    [fieldPath] nvarchar(400) NOT NULL,
    -- El nombre del campo hoja (`nombre`), y el índice de su registro cuando pertenece a algo que se repite (NULL si no).
    [fieldName] nvarchar(200) NOT NULL,
    [recordIndex] int NULL,
    [valueRaw] nvarchar(MAX) NULL,
    [valueNormalized] nvarchar(MAX) NULL,
    [confidence] decimal(5,2) NULL,
    -- La confianza tal como la reportó el motor (Document AI: 0-1). «Permite recalibrar sin re-extraer».
    [confidenceRaw] float NULL,
    [confidenceMethod] varchar(30) NULL,
    [pageNumber] int NULL,
    -- Por qué NO hay valor: el campo se pidió y no apareció (`not_present`), no se pudo leer (`illegible`), salió con confianza
    -- por debajo del umbral (`below_threshold`) o la extracción falló (`extraction_error`).
    [nullReason] varchar(20) NULL,

    CONSTRAINT [PK_entityFacts] PRIMARY KEY CLUSTERED ([factId]),
    CONSTRAINT [FK_entityFacts_run] FOREIGN KEY ([runId]) REFERENCES [nexus].[extractionRuns] ([runId]) ON DELETE CASCADE,
    CONSTRAINT [UQ_entityFacts_runPath] UNIQUE NONCLUSTERED ([runId], [fieldPath]),
    -- La regla del diccionario: o hay valor normalizado, o hay motivo de que no lo haya. Nunca los dos, nunca ninguno.
    CONSTRAINT [CK_entityFacts_valueOrReason] CHECK (([valueNormalized] IS NULL AND [nullReason] IS NOT NULL) OR ([valueNormalized] IS NOT NULL AND [nullReason] IS NULL)),
    CONSTRAINT [CK_entityFacts_nullReason] CHECK ([nullReason] IS NULL OR [nullReason] COLLATE Latin1_General_BIN2 IN ('not_present', 'illegible', 'below_threshold', 'extraction_error')),
    CONSTRAINT [CK_entityFacts_confidenceMethod] CHECK ([confidenceMethod] IS NULL OR [confidenceMethod] COLLATE Latin1_General_BIN2 IN ('extractor_confidence', 'logprob', 'composite')),
    CONSTRAINT [CK_entityFacts_confidence] CHECK ([confidence] IS NULL OR [confidence] BETWEEN (0) AND (100)),
    CONSTRAINT [CK_entityFacts_recordIndex] CHECK ([recordIndex] IS NULL OR [recordIndex] >= (1)),
    CONSTRAINT [CK_entityFacts_pageNumber] CHECK ([pageNumber] IS NULL OR [pageNumber] >= (1))
);
GO

-- ── Correcciones a mano ─────────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[fieldCorrections] (
    [correctionId] bigint IDENTITY(1,1) NOT NULL,
    [factId] bigint NOT NULL,
    [valueNormalized] nvarchar(MAX) NOT NULL,
    [reason] nvarchar(500) NULL,
    [correctedAt] datetime2(7) NOT NULL CONSTRAINT [DF_fieldCorrections_correctedAt] DEFAULT (sysutcdatetime()),
    [correctedBy] varchar(254) NOT NULL,

    CONSTRAINT [PK_fieldCorrections] PRIMARY KEY CLUSTERED ([correctionId]),
    CONSTRAINT [FK_fieldCorrections_fact] FOREIGN KEY ([factId]) REFERENCES [nexus].[entityFacts] ([factId]) ON DELETE CASCADE
);
GO

CREATE NONCLUSTERED INDEX [IX_fieldCorrections_fact] ON [nexus].[fieldCorrections] ([factId], [correctionId]);
GO

-- ── El valor vigente ────────────────────────────────────────────────────────────────────────────────────────────────────
-- Por documento, los campos de su ÚLTIMA corrida que terminó (`ok` o `no_reconocido`; una que falló no borra lo que ya se sabía), cada
-- uno con su última corrección si la tiene. Es lo que leerá el motor de reglas.
CREATE VIEW [nexus].[vEntityFactCurrent]
AS
SELECT
    r.documentId,
    r.runId,
    r.documentTypeId,
    r.documentTypePublicId,
    f.factId,
    f.fieldPath,
    f.fieldName,
    f.recordIndex,
    f.valueRaw,
    COALESCE(c.valueNormalized, f.valueNormalized) AS valueNormalized,
    CASE WHEN c.correctionId IS NULL THEN f.nullReason END AS nullReason,
    f.valueNormalized AS valueExtracted,
    f.confidence,
    f.pageNumber,
    CAST(CASE WHEN c.correctionId IS NULL THEN 0 ELSE 1 END AS bit) AS isCorrected,
    c.correctedAt,
    c.correctedBy
FROM [nexus].[extractionRuns] r
JOIN [nexus].[entityFacts] f ON f.runId = r.runId
OUTER APPLY (
    SELECT TOP (1) fc.correctionId, fc.valueNormalized, fc.correctedAt, fc.correctedBy
    FROM [nexus].[fieldCorrections] fc
    WHERE fc.factId = f.factId
    ORDER BY fc.correctionId DESC
) c
WHERE r.runId = (
    SELECT MAX(r2.runId) FROM [nexus].[extractionRuns] r2 WHERE r2.documentId = r.documentId AND r2.status <> 'error'
);
GO
