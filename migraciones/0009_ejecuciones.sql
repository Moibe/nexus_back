-- 0009 · Ejecuciones: lo que pasó cada vez que se corrió un proceso del motor de reglas sobre un expediente virtual (Moibe, 2026-11-03).
--
-- Es el historial de la tercera sección del módulo de uso («Ejecuciones»). Cada corrida deja UN renglón con la palabra con la que terminó
-- el proceso (su Resultado: «Aprobado», «Certificado»…) y, aparte, UN renglón por cada valor final que entregó. La lista de valores finales
-- de la corrida es una TABLA y no un JSON (Moibe: «cualquier propuesta mejor que me hagas por mí está perfecto»): así se puede preguntar «¿en
-- qué corridas el RFC fue tal?» sin abrir texto. Lo que solo sirve para VER y auditar —cómo salió cada regla, de qué documento y campo se
-- tomó cada dato, y el proceso tal como estaba al correr— se guarda como foto en JSON, porque nadie lo consulta por columnas.
--
-- Es HISTORIAL: solo se agrega, y lo que se guarda es una FOTO. No hay llaves foráneas al expediente, al expediente virtual ni al proceso a
-- propósito: si después se renombran o se borran, la ejecución sigue diciendo lo que se corrió, con los nombres y el dibujo de ese momento
-- (HU-59 CA-5: «el historial es inmutable»). Tampoco hay ninguna ruta que lo edite o lo borre.
--
-- Nace directo en SQL, sin gemelo en JSON (decisión del 2026-10-28). Arranca vacía.

IF SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Falta el esquema nexus: lo crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
GO

-- ── Una corrida ─────────────────────────────────────────────────────────────────────────────────────────────────────────
CREATE TABLE [nexus].[processRuns] (
    [processRunId] bigint IDENTITY(1,1) NOT NULL,
    -- `ejecucion-` + 32 hexadecimales; lo pone el servidor.
    [publicId] varchar(63) NOT NULL,
    [tenant] varchar(63) NOT NULL,
    -- Lo que se corrió, con el id Y el nombre de ENTONCES (foto, sin llave foránea; ver arriba).
    [caseId] varchar(63) NOT NULL,
    [caseName] nvarchar(200) NOT NULL,
    [virtualCaseId] varchar(63) NOT NULL,
    [virtualCaseName] nvarchar(200) NOT NULL,
    [virtualCaseTypeName] nvarchar(200) NULL,
    [processId] varchar(63) NOT NULL,
    [processName] nvarchar(200) NOT NULL,
    -- La revisión del proceso que se corrió: la foto completa está en `definitionJson`.
    [processRevision] int NOT NULL,
    -- Cuál de las tres palabras del Resultado salió: `si` (la regla se cumplió), `no` (no se cumplió) o `sin` (faltaron datos para saberlo).
    [outcomeCase] varchar(3) NOT NULL,
    -- La palabra misma, tal como la escribió la organización en el nodo («Aprobado», «Certificado»…). Se guarda aparte del caso para poder
    -- filtrar por el caso aunque cada proceso use otras palabras.
    [outcomeText] nvarchar(60) NOT NULL,
    -- `completa` si se pudo decidir; `incompleta` si faltaron datos (el caso `sin`).
    [status] varchar(10) NOT NULL,
    -- La foto para ver y auditar: cómo salió cada regla, qué documentos se usaron y de qué campo salió cada dato.
    [detailJson] nvarchar(MAX) NOT NULL,
    -- El proceso tal como estaba al correr (su dibujo completo).
    [definitionJson] nvarchar(MAX) NOT NULL,
    [executedAt] datetime2(7) NOT NULL CONSTRAINT [DF_processRuns_executedAt] DEFAULT (sysutcdatetime()),
    [executedBy] varchar(254) NOT NULL,

    CONSTRAINT [PK_processRuns] PRIMARY KEY CLUSTERED ([processRunId]),
    CONSTRAINT [UQ_processRuns_publicId] UNIQUE NONCLUSTERED ([publicId]),
    CONSTRAINT [CK_processRuns_publicId] CHECK ([publicId] LIKE 'ejecucion-%' AND [publicId] COLLATE Latin1_General_BIN2 = lower([publicId]) AND len([publicId]) = (42)),
    CONSTRAINT [CK_processRuns_tenant] CHECK (len(ltrim(rtrim([tenant]))) > (0) AND [tenant] COLLATE Latin1_General_BIN2 = lower([tenant]) AND NOT [tenant] LIKE '% %'),
    CONSTRAINT [CK_processRuns_processRevision] CHECK ([processRevision] > (0)),
    CONSTRAINT [CK_processRuns_outcomeCase] CHECK ([outcomeCase] COLLATE Latin1_General_BIN2 IN ('si', 'no', 'sin')),
    CONSTRAINT [CK_processRuns_outcomeText] CHECK (len(ltrim(rtrim([outcomeText]))) > (0)),
    CONSTRAINT [CK_processRuns_status] CHECK ([status] COLLATE Latin1_General_BIN2 IN ('completa', 'incompleta')),
    -- Incompleta es exactamente «faltaron datos»: no hay una corrida completa que diga que faltaron, ni una incompleta que haya decidido.
    CONSTRAINT [CK_processRuns_statusOutcome] CHECK (([outcomeCase] = 'sin' AND [status] = 'incompleta') OR ([outcomeCase] <> 'sin' AND [status] = 'completa')),
    CONSTRAINT [CK_processRuns_detailJson] CHECK (isjson([detailJson]) = (1)),
    CONSTRAINT [CK_processRuns_definitionJson] CHECK (isjson([definitionJson]) = (1))
);
GO

-- El historial se lee de la más reciente a la más antigua, de toda la organización o de lo que se corrió sobre un virtual o un proceso.
CREATE NONCLUSTERED INDEX [IX_processRuns_tenant] ON [nexus].[processRuns] ([tenant], [processRunId]);
GO
CREATE NONCLUSTERED INDEX [IX_processRuns_virtualCase] ON [nexus].[processRuns] ([tenant], [virtualCaseId], [processRunId]);
GO
CREATE NONCLUSTERED INDEX [IX_processRuns_process] ON [nexus].[processRuns] ([tenant], [processId], [processRunId]);
GO

-- ── Los valores finales de una corrida: UN renglón por cada nodo «Valor final» del proceso ──────────────────────────────
CREATE TABLE [nexus].[processRunFinalValues] (
    [finalValueId] bigint IDENTITY(1,1) NOT NULL,
    [processRunId] bigint NOT NULL,
    -- El orden en que el proceso los entrega (el de sus nodos).
    [position] int NOT NULL,
    -- El nodo del dibujo (`valor-final-2`) y el nombre que llevaba en la lista cuando se corrió.
    [nodeId] varchar(100) NOT NULL,
    [name] nvarchar(200) NOT NULL,
    -- Qué entregó: `valor` (un dato), `lista` (varios) o `veredicto` (el pasa / no pasa de una regla, guardado como «Pasa» o «No pasa»).
    [kind] varchar(10) NOT NULL,
    -- De qué clase es el dato (`texto`, `numero`, `fecha`, `booleano`); NULL para un veredicto.
    [family] varchar(10) NULL,
    -- `listo`: tiene su valor; `sin_valores`: estaba conectado pero faltó el dato; `sin_conexion`: no tenía nada conectado.
    [status] varchar(12) NOT NULL,
    -- El valor, como se muestra. De una lista, sus elementos separados por coma. NULL si no hay valor.
    [valueText] nvarchar(MAX) NULL,
    -- Solo de una lista: sus elementos como arreglo JSON, para no tener que partir el texto.
    [valueJson] nvarchar(MAX) NULL,
    -- De dónde salió, en palabras: «rfc · INE», «Igual a #1», «Valor fijo #2».
    [origin] nvarchar(400) NULL,

    CONSTRAINT [PK_processRunFinalValues] PRIMARY KEY CLUSTERED ([finalValueId]),
    CONSTRAINT [FK_processRunFinalValues_run] FOREIGN KEY ([processRunId]) REFERENCES [nexus].[processRuns] ([processRunId]),
    CONSTRAINT [UQ_processRunFinalValues_position] UNIQUE NONCLUSTERED ([processRunId], [position]),
    CONSTRAINT [CK_processRunFinalValues_position] CHECK ([position] >= (1)),
    CONSTRAINT [CK_processRunFinalValues_kind] CHECK ([kind] COLLATE Latin1_General_BIN2 IN ('valor', 'lista', 'veredicto')),
    CONSTRAINT [CK_processRunFinalValues_family] CHECK ([family] IS NULL OR [family] COLLATE Latin1_General_BIN2 IN ('texto', 'numero', 'fecha', 'booleano')),
    CONSTRAINT [CK_processRunFinalValues_status] CHECK ([status] COLLATE Latin1_General_BIN2 IN ('listo', 'sin_valores', 'sin_conexion')),
    -- O tiene valor, o no lo tiene y entonces no hay ni texto ni lista: nunca un valor a medias.
    CONSTRAINT [CK_processRunFinalValues_value] CHECK (([status] = 'listo' AND [valueText] IS NOT NULL) OR ([status] <> 'listo' AND [valueText] IS NULL AND [valueJson] IS NULL)),
    CONSTRAINT [CK_processRunFinalValues_valueJson] CHECK ([valueJson] IS NULL OR ([kind] = 'lista' AND isjson([valueJson]) = (1)))
);
GO
