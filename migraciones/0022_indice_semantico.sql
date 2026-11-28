-- 0022 · Índice semántico: la capa OCR y los fragmentos de cada documento con su vector (épica 14, HU-094 a HU-099; Moisés, 2026-11-27:
-- «vámonos con SQL Server»). La decisión y sus números están en docs/adr-indice-semantico.md.
--
--   ocrResults               lo que se LEYÓ de un documento en una extracción: motor, versión, páginas, idioma. Uno por corrida (de solo agregar).
--   ocrBlocks                cada línea leída: su texto, su página y su caja (normalizada 0-1, origen arriba a la izquierda). Es la capa que quedó
--                            fuera de la 0007; desde aquí `entityFacts.ocrBlockId` liga cada dato extraído con la línea de donde salió.
--   documentChunks           los fragmentos de un documento con su vector: los de `texto` (el texto de cada página, en trozos que nunca cruzan de
--                            página) y uno de `campos` por página (los datos extraídos vigentes, que se rehace al corregir).
--   documentIndexes          en qué va el índice de cada documento: INDEX_PENDING, INDEXED o INDEX_INCONSISTENT, con sus intentos.
--   semanticSearchSettings   lo configurable por organización: tamaño del fragmento, solapamiento y parecido mínimo. Sin renglón, lo de siempre.
--   consulta.vFragmento      lo ÚNICO que lee la búsqueda: los fragmentos de la extracción VIGENTE de cada documento, solo de la organización fijada
--                            en la conexión (como las vistas de la 0017). Un fragmento de una extracción anterior nunca sale, aunque siga en la tabla.
--
-- El vector se guarda como float32 en varbinary (768 dimensiones = 3,072 bytes) con la etiqueta del modelo que lo sacó.
-- Como la 0017, la 0020 y la 0021, se detiene si la base no tiene el esquema [consulta] (51016): ⚠ staging y producción todavía no lo tienen.

IF SCHEMA_ID('consulta') IS NULL
    THROW 51016, 'Falta el esquema consulta: lo crea el DBA (CREATE SCHEMA consulta AUTHORIZATION nexusOwner). Ver docs/solicitudes-dba.md §8.', 1;
GO

IF HAS_PERMS_BY_NAME(N'consulta', N'SCHEMA', N'ALTER') = 0
    THROW 51018, 'usrNexusDDL no tiene ALTER sobre el esquema consulta: falta GRANT ALTER ON SCHEMA::consulta TO usrNexusDDL. Ver docs/solicitudes-dba.md §8.', 1;
GO

IF (SELECT principal_id FROM sys.schemas WHERE name = N'consulta') <> (SELECT principal_id FROM sys.schemas WHERE name = N'nexus')
    THROW 51017, 'El esquema consulta no es del mismo dueño que nexus (nexusOwner): el encadenamiento de propiedad no funcionaría. Ver docs/solicitudes-dba.md §8.', 1;
GO

-- ── La capa OCR ──────────────────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[ocrResults] (
    [ocrResultId] bigint IDENTITY(1,1) NOT NULL,
    [runId] bigint NOT NULL,
    -- document_ai hoy; el diccionario contempla azure_ocr y native_pdf.
    [engine] varchar(20) NOT NULL,
    -- La lectura y la extracción salen de la MISMA llamada: es la versión del procesador.
    [engineVersion] varchar(120) NULL,
    [pageCount] int NOT NULL,
    -- BCP-47 (`es`), el de la primera página con idioma detectado.
    [language] varchar(20) NULL,
    -- Cada página con su tamaño y su idioma, tal como los dio el motor.
    [pagesJson] nvarchar(MAX) NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_ocrResults_createdAt] DEFAULT (sysutcdatetime()),

    CONSTRAINT [PK_ocrResults] PRIMARY KEY CLUSTERED ([ocrResultId]),
    CONSTRAINT [UQ_ocrResults_run] UNIQUE NONCLUSTERED ([runId]),
    CONSTRAINT [FK_ocrResults_run] FOREIGN KEY ([runId]) REFERENCES [nexus].[extractionRuns] ([runId]) ON DELETE CASCADE,
    CONSTRAINT [CK_ocrResults_engine] CHECK ([engine] COLLATE Latin1_General_BIN2 IN ('document_ai', 'azure_ocr', 'native_pdf')),
    CONSTRAINT [CK_ocrResults_pageCount] CHECK ([pageCount] >= (0)),
    CONSTRAINT [CK_ocrResults_pagesJson] CHECK ([pagesJson] IS NULL OR ISJSON([pagesJson]) = 1)
);
GO

CREATE TABLE [nexus].[ocrBlocks] (
    [ocrBlockId] bigint IDENTITY(1,1) NOT NULL,
    [ocrResultId] bigint NOT NULL,
    -- El orden de lectura dentro del documento (empieza en 0): es a lo que apunta `bloque_indice` en la respuesta de la extracción.
    [blockIndex] int NOT NULL,
    [pageNumber] int NOT NULL,
    -- La granularidad que se guarda: `line` (servicios/ocr.py::GRANULARIDAD).
    [blockType] varchar(20) NOT NULL,
    [text] nvarchar(4000) NOT NULL,
    [bboxX] decimal(9, 6) NOT NULL,
    [bboxY] decimal(9, 6) NOT NULL,
    [bboxW] decimal(9, 6) NOT NULL,
    [bboxH] decimal(9, 6) NOT NULL,
    [confidence] decimal(5, 2) NULL,
    [rotation] smallint NOT NULL CONSTRAINT [DF_ocrBlocks_rotation] DEFAULT ((0)),

    CONSTRAINT [PK_ocrBlocks] PRIMARY KEY CLUSTERED ([ocrBlockId]),
    CONSTRAINT [UQ_ocrBlocks_result_index] UNIQUE NONCLUSTERED ([ocrResultId], [blockIndex]),
    CONSTRAINT [FK_ocrBlocks_result] FOREIGN KEY ([ocrResultId]) REFERENCES [nexus].[ocrResults] ([ocrResultId]) ON DELETE CASCADE,
    CONSTRAINT [CK_ocrBlocks_blockIndex] CHECK ([blockIndex] >= (0)),
    CONSTRAINT [CK_ocrBlocks_pageNumber] CHECK ([pageNumber] >= (1)),
    CONSTRAINT [CK_ocrBlocks_blockType] CHECK ([blockType] COLLATE Latin1_General_BIN2 IN ('line', 'paragraph', 'block', 'token')),
    CONSTRAINT [CK_ocrBlocks_bbox] CHECK ([bboxX] BETWEEN (0) AND (1) AND [bboxY] BETWEEN (0) AND (1) AND [bboxW] BETWEEN (0) AND (1) AND [bboxH] BETWEEN (0) AND (1)),
    CONSTRAINT [CK_ocrBlocks_confidence] CHECK ([confidence] IS NULL OR [confidence] BETWEEN (0) AND (100)),
    CONSTRAINT [CK_ocrBlocks_rotation] CHECK ([rotation] IN ((0), (90), (180), (270)))
);
GO

-- El texto de una página, en orden: lo que lee el indexado.
CREATE NONCLUSTERED INDEX [IX_ocrBlocks_page] ON [nexus].[ocrBlocks] ([ocrResultId], [pageNumber], [blockIndex]) INCLUDE ([text]);
GO

-- De qué línea salió cada dato extraído. Sin llave foránea a propósito: `entityFacts` y `ocrBlocks` ya se borran en cascada desde su corrida
-- por dos caminos distintos, y SQL Server no admite un tercer camino hacia la misma fila. Lo escribe solo `registrar_extraccion`, con las dos
-- cosas en la misma transacción.
ALTER TABLE [nexus].[entityFacts] ADD [ocrBlockId] bigint NULL;
GO

CREATE NONCLUSTERED INDEX [IX_entityFacts_ocrBlock] ON [nexus].[entityFacts] ([ocrBlockId]) WHERE [ocrBlockId] IS NOT NULL;
GO

-- ── Fragmentos con su vector ─────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[documentChunks] (
    [chunkId] bigint IDENTITY(1,1) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    [documentId] bigint NOT NULL,
    -- La extracción de la que salió. Sin llave foránea (mismo motivo que `ocrBlockId`); la vista solo enseña los de la extracción vigente.
    [runId] bigint NOT NULL,
    -- `texto`: un trozo del texto de una página. `campos`: los datos extraídos de una página («Nombre: … · CURP: …»).
    [kind] varchar(10) NOT NULL,
    -- NULL solo en un fragmento de `campos` con datos sin página.
    [pageNumber] int NULL,
    -- El orden del fragmento dentro de su documento y su clase (empieza en 0).
    [chunkIndex] int NOT NULL,
    [content] nvarchar(MAX) NOT NULL,
    -- Tokens estimados del contenido (servicios/fragmentos.py).
    [tokenCount] int NOT NULL,
    -- float32, little-endian, una dimensión tras otra.
    [embedding] varbinary(8000) NOT NULL,
    -- `gemini-embedding-001:768`: con qué modelo y cuántas dimensiones. Un vector de otro modelo no se compara.
    [embeddingModel] varchar(80) NOT NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_documentChunks_createdAt] DEFAULT (sysutcdatetime()),

    CONSTRAINT [PK_documentChunks] PRIMARY KEY CLUSTERED ([chunkId]),
    CONSTRAINT [UQ_documentChunks_orden] UNIQUE NONCLUSTERED ([documentId], [runId], [kind], [chunkIndex]),
    CONSTRAINT [FK_documentChunks_document] FOREIGN KEY ([documentId]) REFERENCES [nexus].[documents] ([documentId]) ON DELETE CASCADE,
    CONSTRAINT [CK_documentChunks_kind] CHECK ([kind] COLLATE Latin1_General_BIN2 IN ('texto', 'campos')),
    CONSTRAINT [CK_documentChunks_pageNumber] CHECK (([pageNumber] IS NULL AND [kind] = 'campos') OR ([pageNumber] IS NOT NULL AND [pageNumber] >= (1))),
    CONSTRAINT [CK_documentChunks_chunkIndex] CHECK ([chunkIndex] >= (0)),
    CONSTRAINT [CK_documentChunks_content] CHECK (len([content]) > (0)),
    CONSTRAINT [CK_documentChunks_tokenCount] CHECK ([tokenCount] > (0)),
    CONSTRAINT [CK_documentChunks_embedding] CHECK (datalength([embedding]) > (0) AND datalength([embedding]) % (4) = (0))
);
GO

CREATE NONCLUSTERED INDEX [IX_documentChunks_tenant] ON [nexus].[documentChunks] ([tenant], [chunkId]) INCLUDE ([documentId], [runId]);
GO

CREATE NONCLUSTERED INDEX [IX_documentChunks_document] ON [nexus].[documentChunks] ([documentId], [runId]);
GO

-- ── En qué va el índice de cada documento ───────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[documentIndexes] (
    [documentId] bigint NOT NULL,
    [tenant] varchar(63) NOT NULL,
    -- INDEX_PENDING: falta indexar (o reintentar). INDEXED: sus fragmentos son los de `runId`. INDEX_INCONSISTENT: quitar los viejos o escribir
    -- los nuevos falló a la mitad; se reintenta, y mientras tanto la vista ya no enseña fragmentos de otra extracción.
    [status] varchar(20) NOT NULL,
    -- La extracción que se indexó o que toca indexar.
    [runId] bigint NULL,
    [chunkCount] int NULL,
    -- Intentos fallidos seguidos. Al tercero ya no se programa otro y se avisa (`alertedAt`).
    [attempts] int NOT NULL CONSTRAINT [DF_documentIndexes_attempts] DEFAULT ((0)),
    -- Cuándo toca el siguiente intento; NULL si no hay nada programado.
    [nextAttemptAt] datetime2(7) NULL,
    [lastError] nvarchar(1000) NULL,
    [alertedAt] datetime2(7) NULL,
    [indexedAt] datetime2(7) NULL,
    [updatedAt] datetime2(7) NOT NULL CONSTRAINT [DF_documentIndexes_updatedAt] DEFAULT (sysutcdatetime()),

    CONSTRAINT [PK_documentIndexes] PRIMARY KEY CLUSTERED ([documentId]),
    CONSTRAINT [FK_documentIndexes_document] FOREIGN KEY ([documentId]) REFERENCES [nexus].[documents] ([documentId]) ON DELETE CASCADE,
    CONSTRAINT [CK_documentIndexes_status] CHECK ([status] COLLATE Latin1_General_BIN2 IN ('INDEX_PENDING', 'INDEXED', 'INDEX_INCONSISTENT')),
    CONSTRAINT [CK_documentIndexes_attempts] CHECK ([attempts] BETWEEN (0) AND (10)),
    CONSTRAINT [CK_documentIndexes_indexed] CHECK ([status] <> 'INDEXED' OR ([runId] IS NOT NULL AND [chunkCount] IS NOT NULL AND [indexedAt] IS NOT NULL))
);
GO

CREATE NONCLUSTERED INDEX [IX_documentIndexes_due] ON [nexus].[documentIndexes] ([nextAttemptAt]) INCLUDE ([status]) WHERE [nextAttemptAt] IS NOT NULL;
GO

CREATE NONCLUSTERED INDEX [IX_documentIndexes_tenant] ON [nexus].[documentIndexes] ([tenant], [status]);
GO

-- ── Lo configurable por organización ────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[semanticSearchSettings] (
    [tenant] varchar(63) NOT NULL,
    [chunkTokens] int NOT NULL,
    [overlapTokens] int NOT NULL,
    -- El parecido mínimo (coseno, 0 a 1) para que un fragmento cuente como resultado.
    [minScore] decimal(4, 3) NOT NULL,
    [updatedAt] datetime2(7) NOT NULL CONSTRAINT [DF_semanticSearchSettings_updatedAt] DEFAULT (sysutcdatetime()),
    [updatedBy] varchar(254) NULL,

    CONSTRAINT [PK_semanticSearchSettings] PRIMARY KEY CLUSTERED ([tenant]),
    CONSTRAINT [FK_semanticSearchSettings_tenant] FOREIGN KEY ([tenant]) REFERENCES [nexus].[tenants] ([guid]) ON DELETE CASCADE,
    CONSTRAINT [CK_semanticSearchSettings_chunk] CHECK ([chunkTokens] BETWEEN (64) AND (2000)),
    CONSTRAINT [CK_semanticSearchSettings_overlap] CHECK ([overlapTokens] >= (0) AND [overlapTokens] < [chunkTokens]),
    CONSTRAINT [CK_semanticSearchSettings_minScore] CHECK ([minScore] BETWEEN (0) AND (1))
);
GO

-- ── Lo que lee la búsqueda ──────────────────────────────────────────────────────────────────────────────────────────────
CREATE VIEW [consulta].[vFragmento]
AS
SELECT
    c.chunkId AS fragmento,
    d.publicId AS documento,
    c.tenant AS organizacionGuid,
    d.originalName AS archivo,
    r.documentTypePublicId AS tipoDocumentalId,
    COALESCE(t.name, r.documentTypeName) AS tipoDocumental,
    -- La fecha de procesamiento: cuándo terminó la extracción vigente.
    r.finishedAt AS procesadoEn,
    c.kind AS clase,
    c.pageNumber AS pagina,
    c.chunkIndex AS orden,
    c.content AS texto,
    c.embedding AS vector,
    c.embeddingModel AS modelo
FROM [nexus].[documentChunks] c
JOIN [nexus].[documents] d ON d.documentId = c.documentId
JOIN [nexus].[extractionRuns] r ON r.runId = c.runId
LEFT JOIN [nexus].[documentTypes] t ON t.documentTypeId = r.documentTypeId
WHERE c.tenant = CAST(SESSION_CONTEXT(N'organizacion') AS varchar(63))
  AND d.tenant = c.tenant
  -- Solo la extracción vigente (la última que no falló): re-extraer saca de la búsqueda lo anterior en el acto.
  AND c.runId = (SELECT MAX(r2.runId) FROM [nexus].[extractionRuns] r2 WHERE r2.documentId = c.documentId AND r2.status <> 'error')
  AND (
      SESSION_CONTEXT(N'expediente') IS NULL
      OR EXISTS (
          SELECT 1 FROM [nexus].[caseDocuments] cd JOIN [nexus].[cases] k ON k.caseId = cd.caseId
          WHERE cd.documentId = c.documentId AND k.publicId = CAST(SESSION_CONTEXT(N'expediente') AS varchar(63))
      )
  );
GO
