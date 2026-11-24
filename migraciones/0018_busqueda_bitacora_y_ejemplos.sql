-- 0018 · La bitácora de la búsqueda en lenguaje natural y los ejemplos que aprueba cada organización (NEX-609; Moibe, 2026-11-23: «va, dale al
-- 609»).
--
--   searchQuestions   UN renglón por pregunta hecha en la búsqueda (NEX-607): quién, de qué organización y cuándo; la consulta que se corrió (o
--                     por qué no hubo), de dónde salió (el modelo, un ejemplo aprobado o una pregunta igual que ya había salido bien), cuántos
--                     renglones dio, cuánto tardó, cuántos tokens usó y cuánto costó, y si a la persona le sirvió. Los RENGLONES de la respuesta
--                     NO se guardan: solo cuántos fueron. Sobre esta tabla se cuentan los topes de uso, se arma «Tus preguntas recientes», se
--                     reconoce una pregunta repetida para reusar su consulta y se suma lo que Nexus Monitor enseña por organización. Es bitácora:
--                     solo se agrega; lo único que se cambia después es la valoración («¿Te sirvió?») y si quien administra la descartó como
--                     ejemplo.
--   searchExamples    las preguntas que quien administra una organización aprobó como EJEMPLO, con su consulta. La búsqueda de ESA organización (y
--                     solo de esa) las usa para escribir las siguientes, y si alguien hace exactamente la misma pregunta usa esa consulta sin
--                     preguntarle al modelo. `embeddingJson` es su vector de significado: con él se eligen los ejemplos más parecidos a cada
--                     pregunta.
--
-- Nacen directo en SQL (decisión del 2026-10-28) y arrancan vacías. No tocan el esquema [consulta]: el usuario de solo lectura de la búsqueda
-- (usrNexusLectura, NEX-604) no las ve, y no hace falta nada del DBA.

IF SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Falta el esquema nexus: lo crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
GO

-- ── La bitácora de preguntas ─────────────────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[searchQuestions] (
    [questionId] bigint IDENTITY(1,1) NOT NULL,
    -- `pregunta-` + 32 hexadecimales: con él la pantalla valora la respuesta y quien administra la aprueba como ejemplo.
    [publicId] varchar(41) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    -- Quién preguntó (el guid de `nexus.users`, el `sub` del JWT).
    [userGuid] varchar(63) NOT NULL,
    [question] nvarchar(500) NOT NULL,
    -- La pregunta normalizada (minúsculas, sin acentos ni signos, espacios simples): con ella se reconoce «la misma pregunta».
    [questionKey] nvarchar(500) NOT NULL,
    -- Cómo terminó: ok (hubo tabla, aunque viniera vacía), no_se_puede (el modelo dijo que no se responde con estos datos), consulta_invalida (no
    -- hubo una consulta segura ni corrigiéndola), tardo (pasó del tiempo máximo), modelo (el modelo falló o no está disponible), sin_configurar (la
    -- conexión de solo lectura no está lista), tope (llegó al tope de uso: no se hizo nada) o error (cualquier otra cosa).
    [outcome] varchar(20) NOT NULL,
    -- De dónde salió la consulta: modelo, ejemplo (uno aprobado de la organización, la misma pregunta) o anterior (una pregunta igual que salió bien).
    [origin] varchar(10) NULL,
    -- El `publicId` del ejemplo o de la pregunta anterior cuya consulta se reusó.
    [reusedFrom] varchar(41) NULL,
    [sqlText] nvarchar(MAX) NULL,
    [explanation] nvarchar(300) NULL,
    -- Por qué no hubo respuesta (el motivo que dio el modelo, o el error), para quien opera; nunca datos de los documentos.
    [detail] nvarchar(500) NULL,
    [resultRows] int NULL,
    [hasMore] bit NULL,
    -- Cuántas veces se le pidió la consulta al modelo (0 si se reusó una).
    [attempts] tinyint NOT NULL CONSTRAINT [DF_searchQuestions_attempts] DEFAULT ((0)),
    -- Lo que tardó toda la pregunta y lo que tardó la consulta en la base.
    [durationMs] int NOT NULL,
    [databaseMs] int NULL,
    [model] varchar(80) NULL,
    -- Lo que usó el modelo (de `usageMetadata`): entrada, de ella cuánta vino de la caché de Google (más barata) y salida (con lo que «pensó»); y lo
    -- que usó el modelo de vectores. `costUsd` es la estimación con los precios de lista del `.env` del back al momento de preguntar.
    [inputTokens] int NULL,
    [cachedTokens] int NULL,
    [outputTokens] int NULL,
    [embeddingTokens] int NULL,
    [costUsd] decimal(12, 6) NULL,
    -- Huella (sha256) de lo que el modelo supo del catálogo de la organización: una pregunta igual solo reusa la consulta si el catálogo no cambió.
    [catalogHash] char(64) NULL,
    -- Qué ejemplos se le dieron al modelo (`["comun-04", "ejemplo-…"]`).
    [examplesJson] nvarchar(MAX) NULL,
    -- «¿Te sirvió?»: 1 sí, 0 no, NULL sin contestar. Solo en una respuesta que salió bien.
    [useful] bit NULL,
    [ratedAt] datetime2(7) NULL,
    -- Quien administra la organización la quitó de «Por revisar» (no la aprobó como ejemplo).
    [dismissedAt] datetime2(7) NULL,
    [dismissedBy] varchar(63) NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_searchQuestions_createdAt] DEFAULT (sysutcdatetime()),

    CONSTRAINT [PK_searchQuestions] PRIMARY KEY CLUSTERED ([questionId]),
    CONSTRAINT [UQ_searchQuestions_publicId] UNIQUE NONCLUSTERED ([publicId]),
    CONSTRAINT [FK_searchQuestions_tenant] FOREIGN KEY ([tenant]) REFERENCES [nexus].[tenants] ([guid]),
    CONSTRAINT [CK_searchQuestions_publicId] CHECK ([publicId] LIKE 'pregunta-%' AND [publicId] COLLATE Latin1_General_BIN2 = lower([publicId]) AND len([publicId]) = (41) AND NOT substring([publicId], 10, 32) LIKE '%[^0-9a-f]%'),
    CONSTRAINT [CK_searchQuestions_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND [tenant] COLLATE Latin1_General_BIN2 = lower([tenant]) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_searchQuestions_user] CHECK (len(ltrim(rtrim([userGuid]))) > (0)),
    CONSTRAINT [CK_searchQuestions_question] CHECK (len(ltrim(rtrim([question]))) > (0) AND len(ltrim(rtrim([questionKey]))) > (0)),
    CONSTRAINT [CK_searchQuestions_outcome] CHECK ([outcome] COLLATE Latin1_General_BIN2 IN ('ok', 'no_se_puede', 'consulta_invalida', 'tardo', 'modelo', 'sin_configurar', 'tope', 'error')),
    -- Un CHECK deja pasar NULL: por eso el IS NULL explícito y no un IN solo.
    CONSTRAINT [CK_searchQuestions_origin] CHECK ([origin] IS NULL OR [origin] COLLATE Latin1_General_BIN2 IN ('modelo', 'ejemplo', 'anterior')),
    -- Solo se reusa con origen ejemplo o anterior.
    CONSTRAINT [CK_searchQuestions_reused] CHECK ([reusedFrom] IS NULL OR ([origin] IS NOT NULL AND [origin] COLLATE Latin1_General_BIN2 IN ('ejemplo', 'anterior'))),
    -- Una respuesta que salió bien trae su consulta, de dónde salió y cuántos renglones dio.
    CONSTRAINT [CK_searchQuestions_ok] CHECK ([outcome] <> 'ok' OR ([sqlText] IS NOT NULL AND [origin] IS NOT NULL AND [resultRows] IS NOT NULL AND [hasMore] IS NOT NULL)),
    CONSTRAINT [CK_searchQuestions_counts] CHECK (
        [attempts] >= (0) AND [durationMs] >= (0) AND ([databaseMs] IS NULL OR [databaseMs] >= (0)) AND ([resultRows] IS NULL OR [resultRows] >= (0))
        AND ([inputTokens] IS NULL OR [inputTokens] >= (0)) AND ([cachedTokens] IS NULL OR [cachedTokens] >= (0)) AND ([outputTokens] IS NULL OR [outputTokens] >= (0))
        AND ([embeddingTokens] IS NULL OR [embeddingTokens] >= (0)) AND ([costUsd] IS NULL OR [costUsd] >= (0))
    ),
    CONSTRAINT [CK_searchQuestions_useful] CHECK ([useful] IS NULL OR [outcome] = 'ok'),
    CONSTRAINT [CK_searchQuestions_catalogHash] CHECK ([catalogHash] IS NULL OR ([catalogHash] COLLATE Latin1_General_BIN2 = lower([catalogHash]) AND NOT [catalogHash] LIKE '%[^0-9a-f]%')),
    CONSTRAINT [CK_searchQuestions_examplesJson] CHECK ([examplesJson] IS NULL OR isjson([examplesJson]) = (1))
);
GO

-- Los topes y Monitor cuentan por organización y fecha; «Tus preguntas recientes» y el tope por persona, por persona; reusar, por la pregunta.
CREATE NONCLUSTERED INDEX [IX_searchQuestions_tenant_createdAt] ON [nexus].[searchQuestions] ([tenant], [createdAt]) INCLUDE ([outcome], [origin], [userGuid], [durationMs], [costUsd]);
CREATE NONCLUSTERED INDEX [IX_searchQuestions_user] ON [nexus].[searchQuestions] ([tenant], [userGuid], [createdAt]) INCLUDE ([outcome], [questionKey]);
CREATE NONCLUSTERED INDEX [IX_searchQuestions_key] ON [nexus].[searchQuestions] ([tenant], [questionKey]) INCLUDE ([outcome], [useful], [catalogHash], [dismissedAt]);
CREATE NONCLUSTERED INDEX [IX_searchQuestions_createdAt] ON [nexus].[searchQuestions] ([createdAt]) INCLUDE ([tenant], [outcome], [origin], [durationMs], [costUsd]);
GO

-- ── Los ejemplos aprobados ───────────────────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[searchExamples] (
    [exampleId] bigint IDENTITY(1,1) NOT NULL,
    -- `ejemplo-` + 32 hexadecimales.
    [publicId] varchar(40) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    [question] nvarchar(500) NOT NULL,
    [questionKey] nvarchar(500) NOT NULL,
    [sqlText] nvarchar(MAX) NOT NULL,
    [explanation] nvarchar(300) NULL,
    -- La pregunta de la bitácora de la que salió (la que alguien marcó como útil y quien administra aprobó).
    [sourceQuestionId] bigint NULL,
    -- Su vector de significado (JSON, una lista de números) y con qué modelo y cuántas dimensiones se sacó (`gemini-embedding-001:768`): si el
    -- modelo cambia, se vuelve a sacar.
    [embeddingJson] nvarchar(MAX) NULL,
    [embeddingModel] varchar(120) NULL,
    [approvedBy] varchar(63) NOT NULL,
    [approvedAt] datetime2(7) NOT NULL CONSTRAINT [DF_searchExamples_approvedAt] DEFAULT (sysutcdatetime()),

    CONSTRAINT [PK_searchExamples] PRIMARY KEY CLUSTERED ([exampleId]),
    CONSTRAINT [UQ_searchExamples_publicId] UNIQUE NONCLUSTERED ([publicId]),
    -- Una pregunta es UN ejemplo por organización.
    CONSTRAINT [UQ_searchExamples_question] UNIQUE NONCLUSTERED ([tenant], [questionKey]),
    CONSTRAINT [FK_searchExamples_tenant] FOREIGN KEY ([tenant]) REFERENCES [nexus].[tenants] ([guid]),
    CONSTRAINT [FK_searchExamples_question] FOREIGN KEY ([sourceQuestionId]) REFERENCES [nexus].[searchQuestions] ([questionId]),
    CONSTRAINT [CK_searchExamples_publicId] CHECK ([publicId] LIKE 'ejemplo-%' AND [publicId] COLLATE Latin1_General_BIN2 = lower([publicId]) AND len([publicId]) = (40) AND NOT substring([publicId], 9, 32) LIKE '%[^0-9a-f]%'),
    CONSTRAINT [CK_searchExamples_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND [tenant] COLLATE Latin1_General_BIN2 = lower([tenant]) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_searchExamples_question] CHECK (len(ltrim(rtrim([question]))) > (0) AND len(ltrim(rtrim([questionKey]))) > (0) AND len(ltrim(rtrim([sqlText]))) > (0)),
    CONSTRAINT [CK_searchExamples_embedding] CHECK (([embeddingJson] IS NULL AND [embeddingModel] IS NULL) OR ([embeddingJson] IS NOT NULL AND [embeddingModel] IS NOT NULL AND isjson([embeddingJson]) = (1)))
);
GO
