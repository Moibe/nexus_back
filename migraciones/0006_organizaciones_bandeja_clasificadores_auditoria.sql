-- 0006 · Organizaciones, bandeja de entrada, clasificadores y bitácora de auditoría, en SQL (Moibe, 2026-10-29: «dale a las 4»).
--
-- Cuatro registros que vivían en JSONL del almacén (`tenants.jsonl`, `bandeja-{tenant}.jsonl`, `clasificadores.jsonl`, `auditoria.jsonl`).
-- A diferencia de 0002–0005, estas tablas NO arrancan vacías en la práctica: el back importa cada registro UNA vez desde su JSONL la
-- primera vez que lo usa en cada ambiente (`servicios/importacion_json.py`), y lo anota en `nexus.jsonImports`. Sin las organizaciones,
-- ninguna cuenta tendría a qué pertenecer; sin los clasificadores, cada organización volvería a buscar su procesador en Google.
--
-- «Plataforma se trata aparte» (Moibe): el espacio de plataforma (`plataforma`, y lo heredado como `csi` o `demo`) NO es una organización
-- y no se registra en `nexus.tenants`. Por eso las tablas no amarran `tenant` directo: cada una gana `organizationGuid`, una columna
-- calculada que vale el tenant cuando tiene forma de guid de organización y NULL cuando no; la llave foránea va sobre ella. Las de las
-- tablas que ya existían se crean WITH NOCHECK: sus filas son anteriores a que existieran las organizaciones en SQL (se importan después).

IF SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Falta el esquema nexus: lo crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
GO

-- ── Qué registros JSON ya se importaron en ESTA base ────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[jsonImports] (
    [registryName] varchar(60) NOT NULL,
    [importedAt] datetime2(7) NOT NULL CONSTRAINT [DF_jsonImports_importedAt] DEFAULT (sysutcdatetime()),
    [rowsImported] int NOT NULL,
    [rowsSkipped] int NOT NULL,
    CONSTRAINT [PK_jsonImports] PRIMARY KEY CLUSTERED ([registryName])
);
GO

-- ── Organizaciones ──────────────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[tenants] (
    [tenantId] bigint IDENTITY(1,1) NOT NULL,
    -- El guid que usa todo el producto como `tenant` (minúsculas, 36 caracteres). varchar(63), igual que las columnas `tenant`.
    [guid] varchar(63) NOT NULL,
    [name] nvarchar(200) NOT NULL,
    -- Único: el nombre se puede repetir, el slug no (al segundo «Seguros Monterrey» le toca `seguros-monterrey-1`).
    [slug] varchar(63) NOT NULL,
    -- `NEX-00001`: lo asigna el servidor con un contador sobre los ya emitidos.
    [code] varchar(20) NOT NULL,
    [status] varchar(10) NOT NULL CONSTRAINT [DF_tenants_status] DEFAULT ('ACTIVE'),
    [adminGuid] varchar(63) NULL,
    -- Los datos de recuperación de la organización (correo, teléfono…), tal como los manda la pantalla.
    [recoveryJson] nvarchar(MAX) NOT NULL CONSTRAINT [DF_tenants_recoveryJson] DEFAULT (N'{}'),
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_tenants_createdAt] DEFAULT (sysutcdatetime()),

    CONSTRAINT [PK_tenants] PRIMARY KEY CLUSTERED ([tenantId]),
    CONSTRAINT [UQ_tenants_guid] UNIQUE NONCLUSTERED ([guid]),
    CONSTRAINT [UQ_tenants_slug] UNIQUE NONCLUSTERED ([slug]),
    CONSTRAINT [UQ_tenants_code] UNIQUE NONCLUSTERED ([code]),
    CONSTRAINT [CK_tenants_guid] CHECK ([guid] LIKE '________-____-____-____-____________' AND [guid] COLLATE Latin1_General_BIN2 = lower([guid])),
    CONSTRAINT [CK_tenants_slug] CHECK (len([slug]) > (0) AND [slug] COLLATE Latin1_General_BIN2 = lower([slug]) AND NOT [slug] LIKE '% %'),
    CONSTRAINT [CK_tenants_status] CHECK ([status] IN ('ACTIVE', 'SUSPENDED', 'CLOSED')),
    CONSTRAINT [CK_tenants_name] CHECK (len(ltrim(rtrim([name]))) > (0)),
    CONSTRAINT [CK_tenants_recoveryJson] CHECK (isjson([recoveryJson]) = (1))
);
GO

-- ── Clasificador de Document AI de cada organización (el espacio de plataforma usa el del .env) ────────────────────────
CREATE TABLE [nexus].[tenantClassifiers] (
    [tenant] varchar(63) NOT NULL,
    [processorId] varchar(120) NOT NULL,
    [assignedAt] datetime2(7) NOT NULL CONSTRAINT [DF_tenantClassifiers_assignedAt] DEFAULT (sysutcdatetime()),
    CONSTRAINT [PK_tenantClassifiers] PRIMARY KEY CLUSTERED ([tenant]),
    -- Solo organizaciones: aquí sí va directo, porque la plataforma nunca tiene fila.
    CONSTRAINT [FK_tenantClassifiers_tenant] FOREIGN KEY ([tenant]) REFERENCES [nexus].[tenants] ([guid]),
    CONSTRAINT [CK_tenantClassifiers_processorId] CHECK (len([processorId]) > (0))
);
GO

-- ── Bandeja de entrada: lo que llegó y espera en la bandeja de preparación ─────────────────────────────────────────────
CREATE TABLE [nexus].[inboxEntries] (
    [entryId] bigint IDENTITY(1,1) NOT NULL,
    -- 32 hexadecimales: el id que ya conocen el front y la API.
    [publicId] char(32) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    [organizationGuid] AS CAST(CASE WHEN [tenant] LIKE '________-____-____-____-____________' THEN [tenant] END AS varchar(63)) PERSISTED,
    -- El documento que nació con la entrada. Sin llave foránea: las entradas importadas de antes de la 0003 apuntan a documentos que
    -- vivían en JSON.
    [documentPublicId] varchar(63) NULL,
    [relativePath] nvarchar(400) NOT NULL,
    [sha256] char(64) NOT NULL,
    [sizeBytes] bigint NOT NULL,
    [mime] varchar(100) NOT NULL,
    [originalName] nvarchar(400) NOT NULL,
    [channel] varchar(10) NOT NULL,
    [keyId] varchar(100) NULL,
    [receivedAt] datetime2(7) NOT NULL,
    -- Salió de la bandeja: al pipeline o descartada. NULL = sigue pendiente.
    [removedAt] datetime2(7) NULL,
    [removalReason] varchar(20) NULL,
    [removalDetail] nvarchar(2000) NULL,

    CONSTRAINT [PK_inboxEntries] PRIMARY KEY CLUSTERED ([entryId]),
    CONSTRAINT [UQ_inboxEntries_publicId] UNIQUE NONCLUSTERED ([publicId]),
    CONSTRAINT [FK_inboxEntries_organization] FOREIGN KEY ([organizationGuid]) REFERENCES [nexus].[tenants] ([guid]),
    CONSTRAINT [CK_inboxEntries_publicId] CHECK ([publicId] COLLATE Latin1_General_BIN2 NOT LIKE '%[^0-9a-f]%'),
    CONSTRAINT [CK_inboxEntries_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_inboxEntries_channel] CHECK ([channel] IN ('API', 'MANUAL')),
    CONSTRAINT [CK_inboxEntries_removal] CHECK (([removedAt] IS NULL AND [removalReason] IS NULL) OR ([removedAt] IS NOT NULL AND [removalReason] IN ('pipeline', 'descartado')))
);
GO

CREATE NONCLUSTERED INDEX [IX_inboxEntries_tenant] ON [nexus].[inboxEntries] ([tenant], [entryId]) INCLUDE ([removedAt], [removalReason]);
GO

-- ── Bitácora de auditoría: una sola tabla general, de solo agregar ─────────────────────────────────────────────────────
CREATE TABLE [nexus].[auditEvents] (
    [eventId] bigint IDENTITY(1,1) NOT NULL,
    [action] varchar(60) NOT NULL,
    [userGuid] varchar(63) NULL,
    -- De qué organización, si el evento lo dice (los de acceso o de plataforma, no). Sin llave foránea: la bitácora nunca debe
    -- fallar por eso, y un intento cruzado puede nombrar una organización que no existe.
    [tenant] varchar(63) NULL,
    [occurredAt] datetime2(7) NOT NULL,
    -- El evento entero, tal como se escribía en el JSONL (incluye `accion`, `usuarioGuid` y `en`).
    [dataJson] nvarchar(MAX) NOT NULL,
    CONSTRAINT [PK_auditEvents] PRIMARY KEY CLUSTERED ([eventId]),
    CONSTRAINT [CK_auditEvents_dataJson] CHECK (isjson([dataJson]) = (1))
);
GO

-- Lo que más se pregunta: los de una acción en una ventana de tiempo (la recuperación de contraseña cuenta así).
CREATE NONCLUSTERED INDEX [IX_auditEvents_action] ON [nexus].[auditEvents] ([action], [occurredAt]);
GO

-- ── La organización como llave foránea en las tablas que ya existían (plataforma: NULL, fuera) ─────────────────────────
ALTER TABLE [nexus].[documentTypes] ADD [organizationGuid] AS CAST(CASE WHEN [tenant] LIKE '________-____-____-____-____________' THEN [tenant] END AS varchar(63)) PERSISTED;
ALTER TABLE [nexus].[virtualCaseTypes] ADD [organizationGuid] AS CAST(CASE WHEN [tenant] LIKE '________-____-____-____-____________' THEN [tenant] END AS varchar(63)) PERSISTED;
ALTER TABLE [nexus].[documents] ADD [organizationGuid] AS CAST(CASE WHEN [tenant] LIKE '________-____-____-____-____________' THEN [tenant] END AS varchar(63)) PERSISTED;
ALTER TABLE [nexus].[cases] ADD [organizationGuid] AS CAST(CASE WHEN [tenant] LIKE '________-____-____-____-____________' THEN [tenant] END AS varchar(63)) PERSISTED;
ALTER TABLE [nexus].[virtualCases] ADD [organizationGuid] AS CAST(CASE WHEN [tenant] LIKE '________-____-____-____-____________' THEN [tenant] END AS varchar(63)) PERSISTED;
ALTER TABLE [nexus].[processes] ADD [organizationGuid] AS CAST(CASE WHEN [tenant] LIKE '________-____-____-____-____________' THEN [tenant] END AS varchar(63)) PERSISTED;
GO

ALTER TABLE [nexus].[documentTypes] WITH NOCHECK ADD CONSTRAINT [FK_documentTypes_organization] FOREIGN KEY ([organizationGuid]) REFERENCES [nexus].[tenants] ([guid]);
ALTER TABLE [nexus].[virtualCaseTypes] WITH NOCHECK ADD CONSTRAINT [FK_virtualCaseTypes_organization] FOREIGN KEY ([organizationGuid]) REFERENCES [nexus].[tenants] ([guid]);
ALTER TABLE [nexus].[documents] WITH NOCHECK ADD CONSTRAINT [FK_documents_organization] FOREIGN KEY ([organizationGuid]) REFERENCES [nexus].[tenants] ([guid]);
ALTER TABLE [nexus].[cases] WITH NOCHECK ADD CONSTRAINT [FK_cases_organization] FOREIGN KEY ([organizationGuid]) REFERENCES [nexus].[tenants] ([guid]);
ALTER TABLE [nexus].[virtualCases] WITH NOCHECK ADD CONSTRAINT [FK_virtualCases_organization] FOREIGN KEY ([organizationGuid]) REFERENCES [nexus].[tenants] ([guid]);
ALTER TABLE [nexus].[processes] WITH NOCHECK ADD CONSTRAINT [FK_processes_organization] FOREIGN KEY ([organizationGuid]) REFERENCES [nexus].[tenants] ([guid]);
GO
