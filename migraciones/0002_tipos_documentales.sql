-- 0002 · Tipos documentales: la primera entidad del producto que vive en SQL.
--
-- Es el PILOTO de la decisión del 2026-10-21 (Nexus sin stored procedures): el esquema se versiona aquí, en el repo, y lo aplica
-- el corredor dentro del deploy. Hasta hoy la biblioteca de tipos de cada organización vivía en un JSONL de solo-agregar en el
-- almacén (`servicios/tipos_documentales.py`); este archivo crea su tabla. La capa de repositorio del back viene aparte.
--
-- ⚠️ NO confundir con `reference.documentTypes`: ese es un catálogo GLOBAL del diccionario viejo, congelado como especificación.
-- Este es la biblioteca del producto, POR ORGANIZACIÓN, la que se ve en Configuración y la que alimenta al clasificador.
--
-- Arranca VACÍA a propósito (decisión de Moibe, 2026-10-28: «dev limpio»): los tipos que hoy existen en el almacén de plataforma
-- no se importan, se vuelven a dar de alta. Sus ids nuevos no coincidirán con los de los 5 procesadores de Document AI que hay
-- creados en Google, que quedan huérfanos (se limpian con `limpiar_procesadores_huerfanos.py`).

IF SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Falta el esquema nexus: lo crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
GO

CREATE TABLE [nexus].[documentTypes] (
    -- Llave interna. El id que ve el mundo es `publicId`; esta solo agrupa las filas en disco.
    [documentTypeId] bigint IDENTITY(1,1) NOT NULL,

    -- El id público: `tipo-` + 32 hexadecimales, lo inventa el navegador. Nombra la categoría del clasificador y el procesador
    -- en Google, así que es único en TODO el sistema, no por organización: dos tipos con el mismo id serían el mismo tipo.
    -- Hasta hoy esa regla solo la cuidaba Python; a partir de aquí la cuida la base.
    [publicId] varchar(63) NOT NULL,

    -- La organización dueña, por su `slug` (`plataforma`, `csi`…). Es texto y NO una llave foránea a `security.tenants` porque
    -- las organizaciones todavía viven en el almacén, no en SQL. Cuando se muden, esta columna se vuelve la foránea.
    [tenant] varchar(63) NOT NULL,

    -- Para que dos personas no se pisen: al guardar hay que decir sobre qué revisión se editó. Empieza en 1.
    [revision] int NOT NULL,

    -- El tipo tal como lo maneja el front (campos, pasos, historial de versiones, recortes de ejemplo). El back lo trata como
    -- una caja y solo lee de adentro lo que necesita, que es lo que sacan las tres columnas calculadas de abajo.
    [dataJson] nvarchar(MAX) NOT NULL,

    -- Calculadas del JSON y guardadas en disco (PERSISTED): así nunca pueden desfasarse de `dataJson`, se pueden indexar y se
    -- pueden restringir con CHECK. Son las únicas tres cosas que el servidor mira: el nombre y el estado para el clasificador,
    -- el procesador para saber cuáles de Google tienen dueño.
    [name] AS CAST(JSON_VALUE([dataJson], '$.nombre') AS nvarchar(200)) PERSISTED,
    [status] AS CAST(JSON_VALUE([dataJson], '$.estado') AS varchar(20)) PERSISTED,
    [processorId] AS CAST(JSON_VALUE([dataJson], '$.procesadorId') AS varchar(120)) PERSISTED,

    -- El orden que la organización le dio a su biblioteca. NULL = nunca se ordenó: esos van al final, por fecha de creación,
    -- igual que hoy.
    [orderIndex] int NULL,

    -- Quién y cuándo, de la PERSONA (su correo), no del login de SQL: es lo que el producto enseña. Por eso `createdBy` no trae
    -- el `suser_sname()` por omisión que usan las tablas del diccionario viejo, y las fechas son UTC, como todo lo que escribe la app.
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_documentTypes_createdAt] DEFAULT (sysutcdatetime()),
    [createdBy] varchar(254) NOT NULL,
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(254) NULL,

    CONSTRAINT [PK_documentTypes] PRIMARY KEY CLUSTERED ([documentTypeId]),
    CONSTRAINT [UQ_documentTypes_publicId] UNIQUE NONCLUSTERED ([publicId]),
    -- El `COLLATE … _BIN2` no es adorno: la base es CI (no distingue mayúsculas), así que un `publicId = lower(publicId)` a secas
    -- es SIEMPRE cierto y dejaría pasar «tipo-AB…». Comparando en binario sí distingue. (La misma trampa está en el
    -- `CK_tenants_slug` del diccionario viejo, que es de Charlie y aquí no se toca.)
    CONSTRAINT [CK_documentTypes_publicId] CHECK ([publicId] LIKE 'tipo-%' AND [publicId] COLLATE Latin1_General_BIN2 = lower([publicId]) AND len([publicId]) >= (11) AND NOT [publicId] LIKE '% %'),
    CONSTRAINT [CK_documentTypes_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND [tenant] COLLATE Latin1_General_BIN2 = lower([tenant]) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_documentTypes_revision] CHECK ([revision] > (0)),
    CONSTRAINT [CK_documentTypes_dataJson] CHECK (isjson([dataJson]) = (1)),
    -- Las tres calculadas se comprueban con `IS NOT NULL` por delante: un CHECK deja pasar NULL, así que sin eso un `dataJson`
    -- sin nombre o sin estado entraría sin que nadie chistara.
    CONSTRAINT [CK_documentTypes_name] CHECK ([name] IS NOT NULL AND len(ltrim(rtrim([name]))) > (0)),
    CONSTRAINT [CK_documentTypes_status] CHECK ([status] IS NOT NULL AND [status] IN ('borrador', 'activo', 'archivado')),
    CONSTRAINT [CK_documentTypes_orderIndex] CHECK ([orderIndex] IS NULL OR [orderIndex] >= (0))
);
GO

-- La biblioteca siempre se pide de una organización a la vez, y casi siempre en su orden.
CREATE NONCLUSTERED INDEX [IX_documentTypes_tenant] ON [nexus].[documentTypes] ([tenant], [orderIndex]) INCLUDE ([status]);
GO
