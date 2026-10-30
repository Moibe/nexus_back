-- 0008 · Enlaces de un solo uso, API keys de cliente, su uso, webhooks y sus entregas, en SQL (Moibe, 2026-10-30: «va, éntrale justo así»).
--
-- Cinco registros que vivían en JSONL del almacén (`tokens.jsonl`, `llaves.jsonl`, `uso-llaves.jsonl`, `webhooks.jsonl`,
-- `entregas-webhooks.jsonl`). Como en la 0006, NO arrancan vacías en la práctica: el back importa cada registro UNA vez desde su JSONL la
-- primera vez que lo usa en cada ambiente (`servicios/importacion_json.py`) y lo anota en `nexus.jsonImports`.
--
-- Hasta hoy los tres ambientes compartían esos archivos: una llave o un webhook emitido en uno valía en los otros. Desde aquí cada base
-- tiene los suyos, empezando con una copia de lo que había. Las entregas que estaban PENDIENTES de reintento al importar se cierran como
-- canceladas, para que un ambiente no reenvíe avisos que ya manejaba otro.
--
-- Los identificadores públicos distinguen mayúsculas (`a7K9` y `A7k9` son dos llaves), así que van con intercalación binaria; la base trae la
-- de CSI, que no las distingue.
--
-- `accessTokens.userGuid` NO tiene llave foránea todavía: los usuarios siguen en JSON. Se agrega cuando migren.

IF SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Falta el esquema nexus: lo crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
GO

-- ── Enlaces de un solo uso: activación de cuenta y recuperación de contraseña ──────────────────────────────────────────
CREATE TABLE [nexus].[accessTokens] (
    [tokenId] bigint IDENTITY(1,1) NOT NULL,
    [guid] char(36) NOT NULL,
    -- SHA-256 del token (lo único que se guarda: el token viaja en el correo y no se puede volver a leer).
    [tokenHash] char(64) NOT NULL,
    -- De quién es el enlace. Sin llave foránea hasta que los usuarios vivan en SQL.
    [userGuid] varchar(63) NOT NULL,
    [kind] varchar(15) NOT NULL,
    [expiresAt] datetime2(0) NOT NULL,
    [createdAt] datetime2(0) NOT NULL,
    -- Cuándo se gastó; NULL = nunca.
    [usedAt] datetime2(0) NULL,
    -- Cuándo se apagó sin usarse (se pidió otro, se restableció la contraseña, se retiró la cuenta); NULL = nunca.
    [invalidatedAt] datetime2(0) NULL,
    [invalidationReason] nvarchar(100) NULL,

    CONSTRAINT [PK_accessTokens] PRIMARY KEY CLUSTERED ([tokenId]),
    CONSTRAINT [UQ_accessTokens_guid] UNIQUE NONCLUSTERED ([guid]),
    CONSTRAINT [UQ_accessTokens_tokenHash] UNIQUE NONCLUSTERED ([tokenHash]),
    CONSTRAINT [CK_accessTokens_tokenHash] CHECK ([tokenHash] COLLATE Latin1_General_BIN2 NOT LIKE '%[^0-9a-f]%'),
    CONSTRAINT [CK_accessTokens_kind] CHECK ([kind] COLLATE Latin1_General_BIN2 IN ('ACTIVACION', 'RECUPERACION'))
);
GO

-- Emitir uno nuevo apaga los pendientes del mismo tipo de esa persona.
CREATE NONCLUSTERED INDEX [IX_accessTokens_user] ON [nexus].[accessTokens] ([userGuid], [kind]) INCLUDE ([usedAt], [invalidatedAt], [expiresAt]);
GO

-- ── API keys de cliente ─────────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[apiKeys] (
    [keyId] bigint IDENTITY(1,1) NOT NULL,
    -- El identificador público (los 4 caracteres de `nxdoc_live_a7K9_sk_…`): NO es secreto. Distingue mayúsculas. varchar(100) para que
    -- coincida con `documents.keyId` y `inboxEntries.keyId`, que ahora lo referencian.
    [publicId] varchar(100) COLLATE Latin1_General_BIN2 NOT NULL,
    [tenant] varchar(63) NOT NULL,
    [organizationGuid] AS CAST(CASE WHEN [tenant] LIKE '________-____-____-____-____________' THEN [tenant] END AS varchar(63)) PERSISTED,
    -- SHA-256 del secret. El secret mismo NUNCA se guarda.
    [secretHash] char(64) NOT NULL,
    [name] nvarchar(100) NOT NULL,
    [description] nvarchar(500) NOT NULL,
    [createdAt] datetime2(0) NOT NULL,
    [expiresAt] datetime2(0) NOT NULL,
    -- Revocar es un hecho que se registra; «vencida» sale de comparar `expiresAt` con el reloj, no se guarda.
    [revokedAt] datetime2(0) NULL,

    CONSTRAINT [PK_apiKeys] PRIMARY KEY CLUSTERED ([keyId]),
    CONSTRAINT [UQ_apiKeys_publicId] UNIQUE NONCLUSTERED ([publicId]),
    CONSTRAINT [FK_apiKeys_organization] FOREIGN KEY ([organizationGuid]) REFERENCES [nexus].[tenants] ([guid]),
    CONSTRAINT [CK_apiKeys_publicId] CHECK (len([publicId]) > (0) AND [publicId] COLLATE Latin1_General_BIN2 NOT LIKE '%[^A-Za-z0-9]%'),
    CONSTRAINT [CK_apiKeys_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_apiKeys_secretHash] CHECK ([secretHash] COLLATE Latin1_General_BIN2 NOT LIKE '%[^0-9a-f]%')
);
GO

CREATE NONCLUSTERED INDEX [IX_apiKeys_tenant] ON [nexus].[apiKeys] ([tenant], [keyId]);
GO

-- Lo que ya se guardaba con la llave con que se subió: ahora con llave foránea. SQL Server exige la MISMA intercalación en las dos columnas, así
-- que las dos `keyId` existentes pasan a binaria (ninguna tiene índice ni restricción). WITH NOCHECK porque lo de antes pudo subirse con una
-- llave que ya no está (o que vivía solo en otro ambiente); lo nuevo sí se valida.
ALTER TABLE [nexus].[documents] ALTER COLUMN [keyId] varchar(100) COLLATE Latin1_General_BIN2 NULL;
ALTER TABLE [nexus].[inboxEntries] ALTER COLUMN [keyId] varchar(100) COLLATE Latin1_General_BIN2 NULL;
GO
ALTER TABLE [nexus].[documents] WITH NOCHECK ADD CONSTRAINT [FK_documents_apiKey] FOREIGN KEY ([keyId]) REFERENCES [nexus].[apiKeys] ([publicId]);
ALTER TABLE [nexus].[inboxEntries] WITH NOCHECK ADD CONSTRAINT [FK_inboxEntries_apiKey] FOREIGN KEY ([keyId]) REFERENCES [nexus].[apiKeys] ([publicId]);
GO

-- ── Uso de las llaves: una fila por llamada de una llave que EXISTE (acepte o rechace) ────────────────────────────────
CREATE TABLE [nexus].[apiKeyUsage] (
    [usageId] bigint IDENTITY(1,1) NOT NULL,
    [keyId] bigint NOT NULL,
    [occurredAt] datetime2(3) NOT NULL,
    [route] nvarchar(400) NOT NULL,
    [method] varchar(10) NOT NULL,
    [status] int NOT NULL,
    [durationMs] int NOT NULL,
    [bytes] bigint NOT NULL,

    CONSTRAINT [PK_apiKeyUsage] PRIMARY KEY CLUSTERED ([usageId]),
    CONSTRAINT [FK_apiKeyUsage_key] FOREIGN KEY ([keyId]) REFERENCES [nexus].[apiKeys] ([keyId])
);
GO

-- Las métricas y el tope semanal preguntan por llave y periodo.
CREATE NONCLUSTERED INDEX [IX_apiKeyUsage_key] ON [nexus].[apiKeyUsage] ([keyId], [occurredAt]) INCLUDE ([status], [durationMs], [bytes]);
GO

-- ── Webhooks de cliente ─────────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[webhooks] (
    [webhookId] bigint IDENTITY(1,1) NOT NULL,
    -- `wh_` + 12 hexadecimales.
    [publicId] varchar(40) COLLATE Latin1_General_BIN2 NOT NULL,
    [tenant] varchar(63) NOT NULL,
    [organizationGuid] AS CAST(CASE WHEN [tenant] LIKE '________-____-____-____-____________' THEN [tenant] END AS varchar(63)) PERSISTED,
    [url] nvarchar(2048) NOT NULL,
    -- Para que una organización no tenga dos vigentes con la misma dirección (la URL es demasiado larga para un índice).
    [urlHash] AS CAST(HASHBYTES('SHA2_256', [url]) AS binary(32)) PERSISTED,
    -- El secret de firma, CIFRADO (Fernet; la llave vive en el .env del servidor). Se puede cifrar de nuevo pero no leer sin ella.
    -- Al eliminar el webhook se BORRA (NULL): ya no hay con qué firmar nada en su nombre.
    [encryptedSecret] varchar(600) NULL,
    [status] varchar(10) NOT NULL CONSTRAINT [DF_webhooks_status] DEFAULT ('activo'),
    [createdAt] datetime2(0) NOT NULL,
    -- Su endpoint respondió 2xx al aviso de prueba; NULL = sin validar. Solo recibe avisos un webhook validado.
    [validatedAt] datetime2(0) NULL,
    -- Su última validación fallida (en ningún intento respondió): el front lo muestra «Con fallos» si sigue sin validar.
    [validationFailedAt] datetime2(0) NULL,
    -- Eliminado: la fila se queda (su historial de entregas la referencia) pero ya no cuenta ni guarda su secreto.
    [deletedAt] datetime2(0) NULL,

    CONSTRAINT [PK_webhooks] PRIMARY KEY CLUSTERED ([webhookId]),
    CONSTRAINT [UQ_webhooks_publicId] UNIQUE NONCLUSTERED ([publicId]),
    CONSTRAINT [FK_webhooks_organization] FOREIGN KEY ([organizationGuid]) REFERENCES [nexus].[tenants] ([guid]),
    CONSTRAINT [CK_webhooks_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_webhooks_status] CHECK ([status] COLLATE Latin1_General_BIN2 IN ('activo', 'inactivo')),
    CONSTRAINT [CK_webhooks_secret] CHECK (([deletedAt] IS NULL AND [encryptedSecret] IS NOT NULL) OR ([deletedAt] IS NOT NULL AND [encryptedSecret] IS NULL))
);
GO

CREATE UNIQUE NONCLUSTERED INDEX [UX_webhooks_tenantUrl] ON [nexus].[webhooks] ([tenant], [urlHash]) WHERE [deletedAt] IS NULL;
GO

CREATE TABLE [nexus].[webhookSubscriptions] (
    [webhookId] bigint NOT NULL,
    [eventType] varchar(40) NOT NULL,

    CONSTRAINT [PK_webhookSubscriptions] PRIMARY KEY CLUSTERED ([webhookId], [eventType]),
    CONSTRAINT [FK_webhookSubscriptions_webhook] FOREIGN KEY ([webhookId]) REFERENCES [nexus].[webhooks] ([webhookId]) ON DELETE CASCADE,
    CONSTRAINT [CK_webhookSubscriptions_eventType] CHECK ([eventType] COLLATE Latin1_General_BIN2 IN ('documento.completado', 'documento.fallido', 'documento.rechazado', 'expediente.completado'))
);
GO

-- ── Avisos recibidos del front: UNO por entrada y tipo (repetirlo no programa nada de nuevo) ───────────────────────────
CREATE TABLE [nexus].[webhookNotices] (
    [noticeId] bigint IDENTITY(1,1) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    [entryId] varchar(64) NOT NULL,
    [type] varchar(40) NOT NULL,
    [occurredAt] datetime2(0) NOT NULL,
    -- A cuántos webhooks se les programó (puede ser 0).
    [destinations] int NOT NULL,

    CONSTRAINT [PK_webhookNotices] PRIMARY KEY CLUSTERED ([noticeId]),
    CONSTRAINT [UQ_webhookNotices_entry] UNIQUE NONCLUSTERED ([tenant], [entryId], [type]),
    CONSTRAINT [CK_webhookNotices_destinations] CHECK ([destinations] >= (0))
);
GO

-- ── Entregas: UNA por aviso y webhook, con su cuerpo ya armado (viaja idéntico en cada reintento) ───────────────────
CREATE TABLE [nexus].[webhookDeliveries] (
    [deliveryId] bigint IDENTITY(1,1) NOT NULL,
    -- `msg_` + 24 hexadecimales: el `webhook-id` de Standard Webhooks, igual en todos sus intentos.
    [publicId] varchar(60) COLLATE Latin1_General_BIN2 NOT NULL,
    [webhookId] bigint NOT NULL,
    [tenant] varchar(63) NOT NULL,
    [type] varchar(40) NOT NULL,
    -- La entrada de la bandeja que se avisaba (sale del cuerpo).
    [entryId] varchar(64) NULL,
    [bodyJson] nvarchar(MAX) NOT NULL,
    [createdAt] datetime2(0) NOT NULL,
    -- pendiente: sigue en reintentos · entregada: un intento respondió 2xx · agotada: se acabó el calendario · cancelada: el webhook se desactivó o
    -- eliminó (o se importó pendiente de otro ambiente).
    [status] varchar(10) NOT NULL CONSTRAINT [DF_webhookDeliveries_status] DEFAULT ('pendiente'),
    [attempts] int NOT NULL CONSTRAINT [DF_webhookDeliveries_attempts] DEFAULT (0),
    [lastAttemptAt] datetime2(0) NULL,
    [closedAt] datetime2(0) NULL,
    [closeReason] nvarchar(1000) NULL,

    CONSTRAINT [PK_webhookDeliveries] PRIMARY KEY CLUSTERED ([deliveryId]),
    CONSTRAINT [UQ_webhookDeliveries_publicId] UNIQUE NONCLUSTERED ([publicId]),
    CONSTRAINT [FK_webhookDeliveries_webhook] FOREIGN KEY ([webhookId]) REFERENCES [nexus].[webhooks] ([webhookId]),
    CONSTRAINT [CK_webhookDeliveries_status] CHECK ([status] COLLATE Latin1_General_BIN2 IN ('pendiente', 'entregada', 'agotada', 'cancelada')),
    CONSTRAINT [CK_webhookDeliveries_closed] CHECK (([status] = 'pendiente' AND [closedAt] IS NULL) OR ([status] <> 'pendiente' AND [closedAt] IS NOT NULL)),
    CONSTRAINT [CK_webhookDeliveries_attempts] CHECK ([attempts] >= (0)),
    CONSTRAINT [CK_webhookDeliveries_bodyJson] CHECK (isjson([bodyJson]) = (1))
);
GO

-- Al arrancar, el trabajador reconstruye de aquí lo pendiente.
CREATE NONCLUSTERED INDEX [IX_webhookDeliveries_pending] ON [nexus].[webhookDeliveries] ([status], [webhookId]) INCLUDE ([publicId]);
GO

-- ── Intentos: cada vez que NexusDoc le habló al endpoint de un webhook (entregas Y validaciones) ──────────────────────
CREATE TABLE [nexus].[webhookAttempts] (
    [attemptId] bigint IDENTITY(1,1) NOT NULL,
    [webhookId] bigint NOT NULL,
    [tenant] varchar(63) NOT NULL,
    -- El `msg_…` del aviso. Una validación también lleva uno, aunque no tenga entrega.
    [messageId] varchar(60) COLLATE Latin1_General_BIN2 NOT NULL,
    -- El tipo de aviso, o `webhook.validacion` para el de prueba.
    [type] varchar(40) NOT NULL,
    [attemptNumber] int NOT NULL,
    [occurredAt] datetime2(0) NOT NULL,
    [ok] bit NOT NULL,
    [httpCode] int NULL,
    -- Solo los intentos que llegaron a la red tienen latencia.
    [durationMs] int NULL,
    [reason] nvarchar(1000) NULL,

    CONSTRAINT [PK_webhookAttempts] PRIMARY KEY CLUSTERED ([attemptId]),
    CONSTRAINT [FK_webhookAttempts_webhook] FOREIGN KEY ([webhookId]) REFERENCES [nexus].[webhooks] ([webhookId])
);
GO

CREATE NONCLUSTERED INDEX [IX_webhookAttempts_webhook] ON [nexus].[webhookAttempts] ([webhookId], [attemptId]) INCLUDE ([tenant], [type], [occurredAt], [ok], [durationMs]);
GO
