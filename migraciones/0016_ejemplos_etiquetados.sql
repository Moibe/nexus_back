-- 0016 · Ejemplos etiquetados y mediciones de precisión de un tipo documental (Moibe, 2026-11-23: «el propósito sí es que nos sirva de
-- entrenamiento… arranca con el punto 1, la medición»).
--
-- Hasta hoy la pantalla «Ejemplo documental» guardaba, por documento de ejemplo, un RECORTE por campo (4 coordenadas, sin el valor) dentro
-- del `dataJson` del tipo, y nada los leía. Ahora una persona revisa lo que Biblioratum extrae de cada documento de ejemplo y CONFIRMA,
-- CORRIGE o marca como ausente cada campo: eso es la «respuesta buena» del documento (su etiqueta). Con ella se MIDE: se vuelven a extraer
-- los documentos etiquetados con la definición actual del tipo y se compara campo por campo. Es la precisión de verdad, no la confianza
-- que el modelo se pone a sí mismo.
--
--   exampleLabels           UN renglón por documento de ejemplo etiquetado de un tipo: qué archivo es (foto del ejemplo del `dataJson` al
--                           guardar), las etiquetas de sus campos y el documento que devolvió Document AI cuando se revisó (`baseDocumentPath`,
--                           en el almacén): con él se podrá importar el ejemplo a un dataset de Google para entrenar sin volver a extraer.
--   extractionMeasurements  cada medición: con qué procesador y qué revisión del tipo, cuántas comparaciones y cuántos aciertos, y el detalle
--                           (por documento y por campo) para ver qué esperaba y qué leyó. Es HISTORIAL: solo se agrega.
--
-- Los documentos de ejemplo siguen viviendo donde estaban (la lista en el `dataJson` del tipo, los bytes en el almacén): aquí no se copia nada.
-- Si el tipo se borra, se van con él (ON DELETE CASCADE): no significan nada sin su tipo.
--
-- Nace directo en SQL, sin gemelo en JSON (decisión del 2026-10-28). Arranca vacía.
--
-- (La 0016 que esperaba en `migraciones/en_espera/` —el esquema [consulta] de NEX-604— se renumera a la siguiente libre cuando se active,
-- como dice su README.)

IF SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Falta el esquema nexus: lo crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
GO

-- ── Ejemplos etiquetados ────────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[exampleLabels] (
    [exampleLabelId] bigint IDENTITY(1,1) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    [documentTypeId] bigint NOT NULL,
    -- El id del documento de ejemplo dentro del `dataJson` del tipo (`documentosEjemplo[].id`, lo pone el navegador).
    [exampleId] varchar(100) NOT NULL,
    -- Foto del ejemplo al guardar: el archivo que se leyó (en el almacén) y cómo se llamaba.
    [sha256] char(64) NOT NULL,
    [relativePath] nvarchar(400) NOT NULL,
    [mime] varchar(100) NOT NULL,
    [originalName] nvarchar(400) NOT NULL,
    -- La revisión del tipo cuando se guardaron las etiquetas: si después cambian sus campos, las de un campo que ya no existe no cuentan.
    [documentTypeRevision] int NOT NULL,
    -- `{"campos": [{"ruta", "valor" (null = no aparece en el documento), "pagina", "caja" [ymin, xmin, ymax, xmax] de 0 a 1000, "origen"}]}`.
    [labelsJson] nvarchar(MAX) NOT NULL,
    -- El `document` que devolvió Document AI al revisarlo (JSON, en el almacén) y con qué procesador: la base para importarlo a Google.
    [baseDocumentPath] nvarchar(400) NULL,
    [baseProcessorId] varchar(120) NULL,
    [baseProcessorVersion] varchar(120) NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_exampleLabels_createdAt] DEFAULT (sysutcdatetime()),
    [createdBy] varchar(254) NOT NULL,
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(254) NULL,

    CONSTRAINT [PK_exampleLabels] PRIMARY KEY CLUSTERED ([exampleLabelId]),
    -- Un documento de ejemplo tiene UNA etiqueta vigente: volver a guardarla la reemplaza.
    CONSTRAINT [UQ_exampleLabels_example] UNIQUE NONCLUSTERED ([documentTypeId], [exampleId]),
    CONSTRAINT [FK_exampleLabels_documentType] FOREIGN KEY ([documentTypeId]) REFERENCES [nexus].[documentTypes] ([documentTypeId]) ON DELETE CASCADE,
    CONSTRAINT [CK_exampleLabels_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND [tenant] COLLATE Latin1_General_BIN2 = lower([tenant]) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_exampleLabels_exampleId] CHECK (len([exampleId]) > (0) AND NOT [exampleId] LIKE '%[^A-Za-z0-9_-]%'),
    CONSTRAINT [CK_exampleLabels_sha256] CHECK ([sha256] COLLATE Latin1_General_BIN2 = lower([sha256]) AND NOT [sha256] LIKE '%[^0-9a-f]%'),
    CONSTRAINT [CK_exampleLabels_revision] CHECK ([documentTypeRevision] >= (1)),
    CONSTRAINT [CK_exampleLabels_labelsJson] CHECK (isjson([labelsJson]) = (1))
);
GO

-- Las etiquetas se piden por tipo (la pantalla y la medición).
CREATE NONCLUSTERED INDEX [IX_exampleLabels_documentType] ON [nexus].[exampleLabels] ([documentTypeId], [exampleLabelId]);
GO

-- ── Mediciones ──────────────────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[extractionMeasurements] (
    [measurementId] bigint IDENTITY(1,1) NOT NULL,
    -- `medicion-` + 32 hexadecimales; lo pone el servidor.
    [publicId] varchar(63) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    [documentTypeId] bigint NOT NULL,
    -- La definición que se midió: la revisión del tipo en ese momento.
    [documentTypeRevision] int NOT NULL,
    -- Con qué extrajo: el procesador del tipo (`tipo`, si está activo) o el compartido (`compartido`, para un tipo sin procesador propio).
    [processorOrigin] varchar(12) NOT NULL,
    [processorId] varchar(120) NOT NULL,
    [processorVersion] varchar(120) NULL,
    -- `completa`: se leyeron todos; `parcial`: alguno no se pudo leer (va en el detalle); `fallida`: ninguno.
    [status] varchar(10) NOT NULL,
    [documentsCount] int NOT NULL,
    [documentsMeasured] int NOT NULL,
    [comparisonsCount] int NOT NULL,
    [correctCount] int NOT NULL,
    -- Por documento y por campo: qué se esperaba, qué se leyó y cómo salió; y el resumen por campo.
    [detailJson] nvarchar(MAX) NOT NULL,
    [startedAt] datetime2(7) NOT NULL,
    [finishedAt] datetime2(7) NOT NULL CONSTRAINT [DF_extractionMeasurements_finishedAt] DEFAULT (sysutcdatetime()),
    [createdBy] varchar(254) NOT NULL,

    CONSTRAINT [PK_extractionMeasurements] PRIMARY KEY CLUSTERED ([measurementId]),
    CONSTRAINT [UQ_extractionMeasurements_publicId] UNIQUE NONCLUSTERED ([publicId]),
    CONSTRAINT [FK_extractionMeasurements_documentType] FOREIGN KEY ([documentTypeId]) REFERENCES [nexus].[documentTypes] ([documentTypeId]) ON DELETE CASCADE,
    CONSTRAINT [CK_extractionMeasurements_publicId] CHECK ([publicId] LIKE 'medicion-%' AND [publicId] COLLATE Latin1_General_BIN2 = lower([publicId]) AND len([publicId]) = (41)),
    CONSTRAINT [CK_extractionMeasurements_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND [tenant] COLLATE Latin1_General_BIN2 = lower([tenant]) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_extractionMeasurements_revision] CHECK ([documentTypeRevision] >= (1)),
    CONSTRAINT [CK_extractionMeasurements_origin] CHECK ([processorOrigin] COLLATE Latin1_General_BIN2 IN ('tipo', 'compartido')),
    CONSTRAINT [CK_extractionMeasurements_status] CHECK ([status] COLLATE Latin1_General_BIN2 IN ('completa', 'parcial', 'fallida')),
    CONSTRAINT [CK_extractionMeasurements_counts] CHECK (
        [documentsCount] >= (0) AND [documentsMeasured] BETWEEN (0) AND [documentsCount]
        AND [comparisonsCount] >= (0) AND [correctCount] BETWEEN (0) AND [comparisonsCount]
    ),
    CONSTRAINT [CK_extractionMeasurements_detailJson] CHECK (isjson([detailJson]) = (1))
);
GO

-- El historial de un tipo, de la más reciente a la más antigua.
CREATE NONCLUSTERED INDEX [IX_extractionMeasurements_documentType] ON [nexus].[extractionMeasurements] ([documentTypeId], [measurementId]);
GO
