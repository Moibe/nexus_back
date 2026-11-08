-- 0005 · Procesos del motor de reglas, en SQL, ligados a un tipo de expediente virtual.
--
-- Hasta hoy los procesos vivían en `procesos.jsonl` (un archivo de solo-agregar para todas las organizaciones, `servicios/procesos.py`).
-- Se migran ahora porque lo nuevo los toca (decisión del 2026-10-28: lo existente se migra cuando lo nuevo lo toca): desde el 2026-10-29 un
-- proceso se LIGA a un tipo de expediente virtual —el grupito de documentos sobre el que correrá, por ejemplo «Solicitud de crédito»— y esa
-- liga es una llave foránea real a `virtualCaseTypes` (0004).
--
-- Igual que `documentTypes` (0002), el proceso es una caja: `dataJson` guarda el dibujo del lienzo (nombre, descripción, nodos, cables,
-- `activo`…) tal cual, y el back no interpreta las reglas. Lo único que se saca a columnas es el nombre (calculado del JSON) y la liga.
--
-- Arranca VACÍA, como las anteriores: lo de `procesos.jsonl` no se importa.

CREATE TABLE [nexus].[processes] (
    [processId] bigint IDENTITY(1,1) NOT NULL,
    -- `proceso-` + lo que invente el cliente (minúsculas, números y guiones). Único en TODO el sistema, como los tipos documentales.
    [publicId] varchar(63) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    [revision] int NOT NULL,
    [dataJson] nvarchar(MAX) NOT NULL,
    [name] AS CAST(JSON_VALUE([dataJson], '$.nombre') AS nvarchar(200)) PERSISTED,
    -- El tipo de expediente virtual sobre cuyas instancias corre (NULL = todavía sin ligar). Si el tipo se borra, el proceso se queda,
    -- sin liga: se borra la liga, no el proceso.
    [virtualCaseTypePublicId] varchar(63) NULL,
    [createdAt] datetime2(7) NOT NULL CONSTRAINT [DF_processes_createdAt] DEFAULT (sysutcdatetime()),
    [createdBy] varchar(254) NOT NULL,
    [updatedAt] datetime2(7) NULL,
    [updatedBy] varchar(254) NULL,

    CONSTRAINT [PK_processes] PRIMARY KEY CLUSTERED ([processId]),
    CONSTRAINT [UQ_processes_publicId] UNIQUE NONCLUSTERED ([publicId]),
    CONSTRAINT [FK_processes_virtualCaseType] FOREIGN KEY ([virtualCaseTypePublicId]) REFERENCES [nexus].[virtualCaseTypes] ([publicId]) ON DELETE SET NULL,
    CONSTRAINT [CK_processes_publicId] CHECK ([publicId] LIKE 'proceso-%' AND [publicId] COLLATE Latin1_General_BIN2 = lower([publicId]) AND NOT [publicId] LIKE '% %'),
    CONSTRAINT [CK_processes_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND [tenant] COLLATE Latin1_General_BIN2 = lower([tenant]) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_processes_revision] CHECK ([revision] > (0)),
    CONSTRAINT [CK_processes_dataJson] CHECK (isjson([dataJson]) = (1)),
    CONSTRAINT [CK_processes_name] CHECK ([name] IS NOT NULL AND len(ltrim(rtrim([name]))) > (0))
);
GO

CREATE NONCLUSTERED INDEX [IX_processes_tenant] ON [nexus].[processes] ([tenant], [processId]);
GO

-- «Qué procesos corren sobre este tipo»: lo pregunta el expediente virtual al abrirse.
CREATE NONCLUSTERED INDEX [IX_processes_virtualCaseType] ON [nexus].[processes] ([virtualCaseTypePublicId]) INCLUDE ([tenant]);
GO
