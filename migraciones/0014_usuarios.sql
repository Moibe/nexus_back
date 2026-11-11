-- 0014 · Usuarios en SQL: cuentas, membresías, sesiones, intentos de inicio de sesión y lo que un administrador le hace a una cuenta
-- (Moibe, 2026-11-11: «hagamos lo de sql faltante, o sea usuarios, para ya dejar todo sin jsons»).
--
-- `usuarios.jsonl` era el ÚLTIMO registro del producto que vivía en el almacén. Como en la 0006 y la 0008, estas tablas NO arrancan vacías en
-- la práctica: el back importa el registro UNA vez, la primera vez que lo usa en cada ambiente (`servicios/importacion_json.py`), y lo anota en
-- `nexus.jsonImports`. Se importan también las sesiones: nadie tiene que volver a iniciar sesión por la mudanza. El JSONL se queda en el almacén
-- tal como estaba (nadie lo vuelve a escribir donde hay base): es el historial completo de antes de hoy.
--
-- El JSONL era una bitácora de hechos de la que se CALCULABA el estado al leerla. Aquí se guarda el ESTADO de cada cuenta (su contraseña, sus
-- intentos fallidos y su bloqueo, si está activa…) y, aparte, lo que necesita historia: cada intento de inicio de sesión, cada sesión con su
-- cierre, y lo que un administrador le hizo a una cuenta con el motivo que escribió (desactivar, reactivar, retirar, cerrar sus sesiones).
--
-- Una cuenta retirada (borrado lógico) se queda como fila con `deletedAt`: su rastro (sesiones, intentos, enlaces) la sigue apuntando, y su
-- correo queda libre para otra alta.
--
-- Con los usuarios ya en SQL, `accessTokens.userGuid` (0008) gana por fin su llave foránea (WITH NOCHECK: lo de antes no se revisa; lo nuevo sí).
--
-- Los textos que llegan de la pantalla se limitan en CARACTERES y un nvarchar se mide en unidades de UTF-16 (un emoji ocupa dos): las columnas
-- nvarchar tienen el doble de lo que la API acepta (nombre 200 → 400, user agent 500 → 1000…), para que ningún texto válido se rechace por largo.

IF SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Falta el esquema nexus: lo crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
GO

-- ── Cuentas ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[users] (
    [userId] bigint IDENTITY(1,1) NOT NULL,
    -- El guid que usa todo el producto (el `sub` del JWT): minúsculas, 36 caracteres. varchar(63), igual que las columnas que lo referencian.
    [guid] varchar(63) NOT NULL,
    -- Con el que se entra. El back lo guarda sin espacios y en minúsculas. Único entre las cuentas que existen (ver `UX_users_email`).
    [email] nvarchar(254) NOT NULL,
    -- Nombre(s), apellido paterno y apellido materno, por separado como los pide el perfil ('' cuando no hay).
    [firstName] nvarchar(400) NOT NULL,
    [lastName] nvarchar(400) NOT NULL,
    [secondLastName] nvarchar(400) NOT NULL,
    [phone] nvarchar(50) NULL,
    -- Correo y teléfono de recuperación, tal como los guarda la pantalla de perfil.
    [recoveryJson] nvarchar(MAX) NOT NULL CONSTRAINT [DF_users_recoveryJson] DEFAULT (N'{}'),
    [isPlatformAdmin] bit NOT NULL,
    -- argon2id autocontenido (algoritmo, sal y parámetros van dentro). La contraseña NUNCA se guarda.
    [passwordHash] varchar(500) NULL,
    -- La contraseña vigente es temporal: el primer acceso pide cambiarla.
    [mustChangePassword] bit NOT NULL,
    [passwordChangedAt] datetime2(0) NULL,
    -- Cuándo se emitió la contraseña temporal vigente (el alta o la última reemisión): de aquí cuentan sus horas de vigencia.
    [temporaryIssuedAt] datetime2(0) NULL,
    -- Errores seguidos con la contraseña y hasta cuándo está bloqueada. Los calcula el back con la política del .env (5 intentos, 15 minutos).
    [failedAttempts] int NOT NULL CONSTRAINT [DF_users_failedAttempts] DEFAULT (0),
    [lockedUntil] datetime2(0) NULL,
    [lastLoginAt] datetime2(0) NULL,
    [isActive] bit NOT NULL CONSTRAINT [DF_users_isActive] DEFAULT (1),
    [createdAt] datetime2(0) NOT NULL,
    -- Retirada (borrado lógico). El motivo y quién lo hizo están en `userAdminEvents`.
    [deletedAt] datetime2(0) NULL,

    CONSTRAINT [PK_users] PRIMARY KEY CLUSTERED ([userId]),
    CONSTRAINT [UQ_users_guid] UNIQUE NONCLUSTERED ([guid]),
    CONSTRAINT [CK_users_guid] CHECK ([guid] LIKE '________-____-____-____-____________' AND [guid] COLLATE Latin1_General_BIN2 NOT LIKE '%[^0-9a-f-]%'),
    CONSTRAINT [CK_users_email] CHECK (len([email]) > (0) AND NOT [email] LIKE '% %'),
    CONSTRAINT [CK_users_recoveryJson] CHECK (isjson([recoveryJson]) = (1)),
    CONSTRAINT [CK_users_failedAttempts] CHECK ([failedAttempts] >= (0))
);
GO

-- Un correo, una cuenta (en TODA la plataforma: decisión del 2026-10-12). El de una cuenta retirada queda libre.
CREATE UNIQUE NONCLUSTERED INDEX [UX_users_email] ON [nexus].[users] ([email]) WHERE [deletedAt] IS NULL;
GO

-- ── A qué organización pertenece cada cuenta, y con qué rol ────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[userMemberships] (
    -- El orden importa: la PRIMERA membresía es la que va en el JWT (`tnt` y `rol`). Cambiar el rol en una organización la manda al final,
    -- como lo hacía el JSONL.
    [membershipId] bigint IDENTITY(1,1) NOT NULL,
    [userGuid] varchar(63) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    [organizationGuid] AS CAST(CASE WHEN [tenant] LIKE '________-____-____-____-____________' THEN [tenant] END AS varchar(63)) PERSISTED,
    [role] varchar(40) NOT NULL,
    [assignedAt] datetime2(0) NOT NULL,

    CONSTRAINT [PK_userMemberships] PRIMARY KEY CLUSTERED ([membershipId]),
    CONSTRAINT [UQ_userMemberships_userTenant] UNIQUE NONCLUSTERED ([userGuid], [tenant]),
    CONSTRAINT [FK_userMemberships_user] FOREIGN KEY ([userGuid]) REFERENCES [nexus].[users] ([guid]),
    CONSTRAINT [FK_userMemberships_organization] FOREIGN KEY ([organizationGuid]) REFERENCES [nexus].[tenants] ([guid]),
    CONSTRAINT [CK_userMemberships_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_userMemberships_role] CHECK (len(ltrim(rtrim([role]))) > (0))
);
GO

-- El listado de usuarios de una organización.
CREATE NONCLUSTERED INDEX [IX_userMemberships_tenant] ON [nexus].[userMemberships] ([tenant], [userGuid]);
GO

-- ── Sesiones: una por inicio de sesión (y una nueva cada vez que se renueva) ───────────────────────────────────────────
CREATE TABLE [nexus].[userSessions] (
    [sessionId] bigint IDENTITY(1,1) NOT NULL,
    -- El `sid` del JWT.
    [guid] varchar(63) NOT NULL,
    [userGuid] varchar(63) NOT NULL,
    -- SHA-256 del refresh token. El token mismo nunca se guarda: vive en la cookie de la persona.
    [refreshHash] char(64) NOT NULL,
    [expiresAt] datetime2(0) NOT NULL,
    [createdAt] datetime2(0) NOT NULL,
    [ip] nvarchar(100) NULL,
    [userAgent] nvarchar(1000) NULL,
    -- Cerrada: por la persona (LOGOUT), al renovarse (ROTATED), al cambiar o restablecer la contraseña (PASSWORD_CHANGED), por un administrador
    -- (FORCED_LOGOUT, DEACTIVATED), al retirar la cuenta (DELETED), al reemitir la invitación (REINVITED)… NULL = sigue abierta.
    [revokedAt] datetime2(0) NULL,
    [revocationReason] varchar(40) NULL,

    CONSTRAINT [PK_userSessions] PRIMARY KEY CLUSTERED ([sessionId]),
    CONSTRAINT [UQ_userSessions_guid] UNIQUE NONCLUSTERED ([guid]),
    CONSTRAINT [UQ_userSessions_refreshHash] UNIQUE NONCLUSTERED ([refreshHash]),
    CONSTRAINT [FK_userSessions_user] FOREIGN KEY ([userGuid]) REFERENCES [nexus].[users] ([guid]),
    CONSTRAINT [CK_userSessions_guid] CHECK (len([guid]) > (0) AND NOT [guid] LIKE '% %'),
    CONSTRAINT [CK_userSessions_refreshHash] CHECK ([refreshHash] COLLATE Latin1_General_BIN2 NOT LIKE '%[^0-9a-f]%'),
    CONSTRAINT [CK_userSessions_revoked] CHECK ([revokedAt] IS NOT NULL OR [revocationReason] IS NULL)
);
GO

-- «Cerrar todas las sesiones de esta persona» y «¿tiene alguna abierta?».
CREATE NONCLUSTERED INDEX [IX_userSessions_user] ON [nexus].[userSessions] ([userGuid]) INCLUDE ([revokedAt], [expiresAt]);
GO

-- ── Bitácora de intentos de inicio de sesión (acierte o no) ────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[loginAttempts] (
    [attemptId] bigint IDENTITY(1,1) NOT NULL,
    -- De qué cuenta, si el correo existía; NULL si no (o si el navegador estaba bloqueado y ni se buscó).
    [userGuid] varchar(63) NULL,
    -- El correo que se escribió, normalizado.
    [email] nvarchar(700) NOT NULL,
    [succeeded] bit NOT NULL,
    -- Por qué no entró: BAD_PASSWORD, LOCKED, INACTIVE, UNKNOWN_EMAIL, EXPIRED. NULL si entró.
    [reason] varchar(20) NULL,
    [ip] nvarchar(100) NULL,
    [userAgent] nvarchar(1000) NULL,
    -- El identificador anónimo del navegador (cookie `nx_navegador`).
    [originId] nvarchar(200) COLLATE Latin1_General_BIN2 NULL,
    [occurredAt] datetime2(0) NOT NULL,

    CONSTRAINT [PK_loginAttempts] PRIMARY KEY CLUSTERED ([attemptId]),
    CONSTRAINT [FK_loginAttempts_user] FOREIGN KEY ([userGuid]) REFERENCES [nexus].[users] ([guid]),
    CONSTRAINT [CK_loginAttempts_reason] CHECK ([succeeded] = (0) OR [reason] IS NULL)
);
GO

-- ── El contador de cada navegador: cinco errores (contraseña mala o correo que no existe) lo bloquean, teclee el correo que teclee ────────
CREATE TABLE [nexus].[loginOrigins] (
    [originId] nvarchar(200) COLLATE Latin1_General_BIN2 NOT NULL,
    [failedAttempts] int NOT NULL,
    [lockedUntil] datetime2(0) NULL,
    [updatedAt] datetime2(0) NOT NULL,

    CONSTRAINT [PK_loginOrigins] PRIMARY KEY CLUSTERED ([originId]),
    CONSTRAINT [CK_loginOrigins_failedAttempts] CHECK ([failedAttempts] >= (0))
);
GO

-- ── Lo que un administrador (o un script, o Nexus Monitor) le hizo a una cuenta, con su motivo ─────────────────────────
CREATE TABLE [nexus].[userAdminEvents] (
    [eventId] bigint IDENTITY(1,1) NOT NULL,
    [userGuid] varchar(63) NOT NULL,
    -- DEACTIVATED / REACTIVATED (HU08, HU09), DELETED (retiro: borrado lógico), SESSIONS_CLOSED (cierre forzado de todas sus sesiones, HU05).
    [kind] varchar(20) NOT NULL,
    -- El motivo que escribió quien lo hizo.
    [reason] nvarchar(1000) NULL,
    -- Quién: el guid de la persona, o `CLI` / `monitor` cuando fue un script o Nexus Monitor.
    [byWhom] varchar(63) NULL,
    [occurredAt] datetime2(0) NOT NULL,

    CONSTRAINT [PK_userAdminEvents] PRIMARY KEY CLUSTERED ([eventId]),
    CONSTRAINT [FK_userAdminEvents_user] FOREIGN KEY ([userGuid]) REFERENCES [nexus].[users] ([guid]),
    CONSTRAINT [CK_userAdminEvents_kind] CHECK ([kind] COLLATE Latin1_General_BIN2 IN ('DEACTIVATED', 'REACTIVATED', 'DELETED', 'SESSIONS_CLOSED'))
);
GO

CREATE NONCLUSTERED INDEX [IX_userAdminEvents_user] ON [nexus].[userAdminEvents] ([userGuid], [eventId]);
GO

-- ── Los enlaces de un solo uso (0008) por fin apuntan a una cuenta que existe ──────────────────────────────────────────
ALTER TABLE [nexus].[accessTokens] WITH NOCHECK ADD CONSTRAINT [FK_accessTokens_user] FOREIGN KEY ([userGuid]) REFERENCES [nexus].[users] ([guid]);
GO
