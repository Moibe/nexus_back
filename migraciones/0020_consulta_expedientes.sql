-- 0020 · Preguntarle al expediente: vistas de consulta sobre expedientes, procesos, ejecuciones y correcciones (NEX-612; Moibe, 2026-11-26: «va,
-- dale al 612»).
--
-- La búsqueda en lenguaje natural (NEX-607) contestaba sobre los DOCUMENTOS y sus datos extraídos (las vistas de la 0017). Producto pide además
-- preguntarle al EXPEDIENTE («¿qué documentos le faltan?», «¿por qué quedó en Requiere revisión?», «¿qué se corrigió y quién?»; documento de Alexis
-- Quintero del 18 de noviembre, `docs/casos-de-uso-expediente.md`). Casi todo ya se guardaba; esta migración lo expone, con el mismo aislamiento:
--
--   vExpediente               un renglón por expediente: nombre, quién y cuándo lo creó, cuántos documentos y ejecuciones, y la última ejecución.
--   vDocumentoDeExpediente    un renglón por documento de cada expediente (uno puede estar en varios), con su tipo documental vigente: el de su
--                             última extracción o, si nunca se extrajo, el de su última clasificación (lo mismo que usa el motor al ejecutar).
--   vDocumentoRequerido       los tipos documentales que USA cada proceso (los nodos de su lienzo que tienen algo conectado: los mismos que una
--                             ejecución da por faltantes) y si el proceso está activo (las mismas reglas que `procesos.motivos_de_inactividad`).
--   vDocumentoFaltante        por expediente y proceso activo, los tipos documentales que el proceso usa y el expediente no trae.
--   vEjecucion                un renglón por ejecución (la foto inmutable del historial): expediente, proceso, la palabra del Resultado y su caso,
--                             cuándo, quién, si es la última de ese proceso en ese expediente, y cuántas reglas no pasaron y cuántas cosas faltaron.
--   vReglaDeEjecucion         un renglón por regla de cada ejecución: cómo salió, su explicación y lo que comparó (cada entrada con su origen y valor).
--   vEvidenciaDeEjecucion     un renglón por campo que leyó cada ejecución: tipo documental, documento, campo, valor, confianza y página.
--   vFaltanteDeEjecucion      un renglón por cada cosa que le faltó a una ejecución («INE: el expediente no trae el documento»).
--   vCorreccion               un renglón por campo corregido a mano en la extracción vigente: valor extraído, valor corregido, motivo, quién y cuándo.
--
-- El AISLAMIENTO es el de la 0017: todas filtran por `SESSION_CONTEXT(N'organizacion')`, que el back fija de solo lectura al abrir la conexión. Y
-- además, cuando se pregunta DESDE UN EXPEDIENTE, el back fija también `SESSION_CONTEXT(N'expediente')` (de solo lectura, no se puede cambiar: 15664)
-- y TODAS las vistas de [consulta] —también las dos de la 0017, que se redefinen aquí— devuelven solo lo de ese expediente: «este expediente» no
-- depende de que el modelo escriba bien el filtro. Sin expediente fijado (el cuadro del Módulo de uso), devuelven lo de toda la organización.
--
-- Por persona se da su NOMBRE (el de `nexus.users`, o su correo), no su guid. Las vistas nuevas no dan nada que la persona que pregunta no pudiera ver
-- en la pantalla del expediente y su historial. El permiso del DBA (NEX-604: `GRANT SELECT ON SCHEMA::consulta`) ya cubre estas vistas: no hace falta
-- otro ticket. Como la 0017, esta migración se detiene si la base no tiene el esquema [consulta] (51016): ⚠ staging y producción todavía no lo tienen.
--
-- Y en la bitácora de la búsqueda (`nexus.searchQuestions`, 0018), `caseId`: el expediente desde el que se preguntó (NULL: desde el Módulo de uso). Una
-- conversación no se mezcla entre el Módulo de uso y un expediente, ni entre expedientes.

IF SCHEMA_ID('consulta') IS NULL
    THROW 51016, 'Falta el esquema consulta: lo crea el DBA (CREATE SCHEMA consulta AUTHORIZATION nexusOwner). Ver docs/solicitudes-dba.md §8.', 1;
GO

IF HAS_PERMS_BY_NAME(N'consulta', N'SCHEMA', N'ALTER') = 0
    THROW 51018, 'usrNexusDDL no tiene ALTER sobre el esquema consulta: falta GRANT ALTER ON SCHEMA::consulta TO usrNexusDDL. Ver docs/solicitudes-dba.md §8.', 1;
GO

IF (SELECT principal_id FROM sys.schemas WHERE name = N'consulta') <> (SELECT principal_id FROM sys.schemas WHERE name = N'nexus')
    THROW 51017, 'El esquema consulta no es del mismo dueño que nexus (nexusOwner): el encadenamiento de propiedad no funcionaría. Ver docs/solicitudes-dba.md §8.', 1;
GO

-- ── Las dos de la 0017, ahora también por expediente ─────────────────────────────────────────────────────────────────────────────────
-- Mismas columnas. Con un expediente fijado, solo los documentos que están en él.
CREATE OR ALTER VIEW [consulta].[vDocumentoExtraido]
AS
SELECT
    v.documento,
    v.organizacionGuid,
    v.organizacion,
    v.archivo,
    v.mime,
    v.canal,
    v.recibidoEn,
    v.extraccion,
    v.tipoDocumentalId,
    v.tipoDocumental,
    v.revisionTipoDocumental,
    v.estado,
    v.confianzaPromedio,
    v.confianzaMinima,
    v.paginas,
    v.extraidoEn,
    v.extracciones,
    v.campos,
    v.camposConValor,
    v.camposFaltantes,
    v.camposCorregidos
FROM [nexus].[vDocumentoExtraido] v
WHERE v.organizacionGuid = CAST(SESSION_CONTEXT(N'organizacion') AS varchar(63))
  AND (
      SESSION_CONTEXT(N'expediente') IS NULL
      OR EXISTS (
          SELECT 1
          FROM [nexus].[cases] c
          JOIN [nexus].[caseDocuments] cd ON cd.caseId = c.caseId
          JOIN [nexus].[documents] d ON d.documentId = cd.documentId
          WHERE c.publicId = CAST(SESSION_CONTEXT(N'expediente') AS varchar(63)) AND c.tenant = v.organizacionGuid AND d.publicId = v.documento
      )
  );
GO

CREATE OR ALTER VIEW [consulta].[vDatoExtraido]
AS
SELECT
    v.documento,
    v.organizacionGuid,
    v.organizacion,
    v.archivo,
    v.tipoDocumentalId,
    v.tipoDocumental,
    v.campo,
    v.nombreCampo,
    v.fila,
    v.valor,
    v.valorNumero,
    v.valorFecha,
    v.confianza,
    v.pagina,
    v.corregido,
    v.motivoSinValor,
    v.extraidoEn
FROM [nexus].[vDatoExtraido] v
WHERE v.organizacionGuid = CAST(SESSION_CONTEXT(N'organizacion') AS varchar(63))
  AND (
      SESSION_CONTEXT(N'expediente') IS NULL
      OR EXISTS (
          SELECT 1
          FROM [nexus].[cases] c
          JOIN [nexus].[caseDocuments] cd ON cd.caseId = c.caseId
          JOIN [nexus].[documents] d ON d.documentId = cd.documentId
          WHERE c.publicId = CAST(SESSION_CONTEXT(N'expediente') AS varchar(63)) AND c.tenant = v.organizacionGuid AND d.publicId = v.documento
      )
  );
GO

-- ── Expedientes y sus documentos ─────────────────────────────────────────────────────────────────────────────────────────────────────
CREATE VIEW [consulta].[vExpediente]
AS
SELECT
    c.publicId AS expediente,
    c.tenant AS organizacionGuid,
    c.name AS nombre,
    c.createdAt AS creadoEn,
    COALESCE(NULLIF(CONCAT_WS(N' ', NULLIF(u.firstName, N''), NULLIF(u.lastName, N''), NULLIF(u.secondLastName, N'')), N''), u.email) AS creadoPor,
    (SELECT count(*) FROM [nexus].[caseDocuments] cd WHERE cd.caseId = c.caseId) AS documentos,
    e.ejecuciones,
    e.ultimaEjecucionEn,
    x.processName AS ultimoProceso,
    x.outcomeText AS ultimoResultado,
    x.outcomeCase AS ultimoCaso
FROM [nexus].[cases] c
LEFT JOIN [nexus].[users] u ON u.guid = c.createdBy
OUTER APPLY (
    SELECT count(*) AS ejecuciones, max(r.executedAt) AS ultimaEjecucionEn
    FROM [nexus].[processRuns] r
    WHERE r.tenant = c.tenant AND r.caseId = c.publicId
) e
OUTER APPLY (
    SELECT TOP (1) r.processName, r.outcomeText, r.outcomeCase
    FROM [nexus].[processRuns] r
    WHERE r.tenant = c.tenant AND r.caseId = c.publicId
    ORDER BY r.executedAt DESC, r.processRunId DESC
) x
WHERE c.tenant = CAST(SESSION_CONTEXT(N'organizacion') AS varchar(63))
  AND (SESSION_CONTEXT(N'expediente') IS NULL OR c.publicId = CAST(SESSION_CONTEXT(N'expediente') AS varchar(63)));
GO

CREATE VIEW [consulta].[vDocumentoDeExpediente]
AS
SELECT
    c.publicId AS expediente,
    c.name AS nombreExpediente,
    c.tenant AS organizacionGuid,
    d.publicId AS documento,
    d.originalName AS archivo,
    COALESCE(x.tipoId, y.tipoId) AS tipoDocumentalId,
    COALESCE(x.tipoNombre, y.tipoNombre) AS tipoDocumental,
    -- De dónde se sabe el tipo: `extraccion` (ya se extrajo) o `clasificacion` (solo se clasificó); NULL si no se sabe.
    CASE WHEN x.tipoId IS NOT NULL THEN 'extraccion' WHEN y.tipoId IS NOT NULL THEN 'clasificacion' END AS origenTipo,
    d.receivedAt AS recibidoEn,
    cd.addedAt AS agregadoEn
FROM [nexus].[cases] c
JOIN [nexus].[caseDocuments] cd ON cd.caseId = c.caseId
JOIN [nexus].[documents] d ON d.documentId = cd.documentId
OUTER APPLY (
    SELECT TOP (1) r.documentTypePublicId AS tipoId, COALESCE(t.name, r.documentTypeName) AS tipoNombre
    FROM [nexus].[extractionRuns] r
    LEFT JOIN [nexus].[documentTypes] t ON t.documentTypeId = r.documentTypeId
    WHERE r.documentId = d.documentId AND r.status <> 'error'
    ORDER BY r.runId DESC
) x
OUTER APPLY (
    -- La ÚLTIMA clasificación, aunque haya dicho «otro» (sin tipo): igual que `datos_extraidos.tipos_de`.
    SELECT TOP (1) t.publicId AS tipoId, t.name AS tipoNombre
    FROM [nexus].[documentClassifications] k
    LEFT JOIN [nexus].[documentTypes] t ON t.documentTypeId = k.documentTypeId
    WHERE k.documentId = d.documentId
    ORDER BY k.classificationId DESC
) y
WHERE c.tenant = CAST(SESSION_CONTEXT(N'organizacion') AS varchar(63))
  AND d.tenant = c.tenant
  AND (SESSION_CONTEXT(N'expediente') IS NULL OR c.publicId = CAST(SESSION_CONTEXT(N'expediente') AS varchar(63)));
GO

-- ── Lo que exige cada proceso y lo que le falta a cada expediente ───────────────────────────────────────────────────────────────────
CREATE VIEW [consulta].[vDocumentoRequerido]
AS
SELECT
    p.publicId AS proceso,
    p.name AS nombreProceso,
    p.tenant AS organizacionGuid,
    a.activo,
    n.tipoId AS tipoDocumentalId,
    COALESCE(t.name, n.tipoId) AS tipoDocumental
FROM [nexus].[processes] p
CROSS APPLY (
    -- Los nodos de tipo documental con algo conectado (un campo o el documento): los que una ejecución da por faltantes si el expediente no los trae.
    SELECT DISTINCT JSON_VALUE(x.value, '$.data.tipoId') AS tipoId
    FROM OPENJSON(p.dataJson, '$.nodos') x
    WHERE JSON_VALUE(x.value, '$.type') = 'tipo-documental'
      AND JSON_VALUE(x.value, '$.data.tipoId') IS NOT NULL
      AND EXISTS (SELECT 1 FROM OPENJSON(p.dataJson, '$.aristas') e WHERE JSON_VALUE(e.value, '$.source') = JSON_VALUE(x.value, '$.id'))
) n
CROSS APPLY (
    -- Activo: lo mismo que `procesos.motivos_de_inactividad` más el `activo` guardado. No lo está si se desactivó, si usa un tipo documental que no
    -- está activo o ya no existe, o si ninguna regla llega a su Resultado.
    SELECT CAST(CASE
        WHEN JSON_VALUE(p.dataJson, '$.activo') = 'false' THEN 0
        WHEN EXISTS (
            SELECT 1 FROM OPENJSON(p.dataJson, '$.nodos') x
            WHERE JSON_VALUE(x.value, '$.type') = 'tipo-documental'
              AND NOT EXISTS (
                  SELECT 1 FROM [nexus].[documentTypes] dt
                  WHERE dt.tenant = p.tenant AND dt.publicId = JSON_VALUE(x.value, '$.data.tipoId') AND dt.status = 'activo'
              )
        ) THEN 0
        WHEN NOT EXISTS (
            SELECT 1
            FROM OPENJSON(p.dataJson, '$.aristas') e
            JOIN OPENJSON(p.dataJson, '$.nodos') r ON JSON_VALUE(r.value, '$.id') = JSON_VALUE(e.value, '$.target') AND JSON_VALUE(r.value, '$.type') = 'resultado-final'
            JOIN OPENJSON(p.dataJson, '$.nodos') o ON JSON_VALUE(o.value, '$.id') = JSON_VALUE(e.value, '$.source') AND JSON_VALUE(o.value, '$.type') = 'operador'
            WHERE JSON_VALUE(e.value, '$.targetHandle') = 'a'
        ) THEN 0
        ELSE 1
    END AS bit) AS activo
) a
LEFT JOIN [nexus].[documentTypes] t ON t.tenant = p.tenant AND t.publicId = n.tipoId
WHERE p.tenant = CAST(SESSION_CONTEXT(N'organizacion') AS varchar(63));
GO

CREATE VIEW [consulta].[vDocumentoFaltante]
AS
SELECT
    e.expediente,
    e.nombre AS nombreExpediente,
    e.organizacionGuid,
    r.proceso,
    r.nombreProceso,
    r.tipoDocumentalId,
    r.tipoDocumental
FROM [consulta].[vExpediente] e
JOIN [consulta].[vDocumentoRequerido] r ON r.organizacionGuid = e.organizacionGuid AND r.activo = 1
WHERE NOT EXISTS (
    SELECT 1 FROM [consulta].[vDocumentoDeExpediente] d WHERE d.expediente = e.expediente AND d.tipoDocumentalId = r.tipoDocumentalId
);
GO

-- ── Las ejecuciones (el historial, que es una foto inmutable) ───────────────────────────────────────────────────────────────────────
CREATE VIEW [consulta].[vEjecucion]
AS
SELECT
    r.publicId AS ejecucion,
    r.caseId AS expediente,
    r.caseName AS nombreExpediente,
    r.tenant AS organizacionGuid,
    r.processId AS proceso,
    r.processName AS nombreProceso,
    r.processRevision AS revisionProceso,
    -- La palabra con la que terminó (la escribe cada organización: «Aprobado», «Requiere revisión»…) y su caso: si, no o sin (sin datos).
    r.outcomeText AS resultado,
    r.outcomeCase AS caso,
    r.status AS estado,
    r.executedAt AS ejecutadaEn,
    COALESCE(NULLIF(CONCAT_WS(N' ', NULLIF(u.firstName, N''), NULLIF(u.lastName, N''), NULLIF(u.secondLastName, N'')), N''), u.email) AS ejecutadaPor,
    CAST(CASE WHEN ROW_NUMBER() OVER (PARTITION BY r.caseId, r.processId ORDER BY r.executedAt DESC, r.processRunId DESC) = 1 THEN 1 ELSE 0 END AS bit) AS esLaUltima,
    (SELECT count(*) FROM OPENJSON(r.detailJson, '$.reglas') g WHERE JSON_VALUE(g.value, '$.estado') = 'no_pasa') AS reglasQueNoPasaron,
    (SELECT count(*) FROM OPENJSON(r.detailJson, '$.faltantes')) AS faltantes
FROM [nexus].[processRuns] r
LEFT JOIN [nexus].[users] u ON u.guid = r.executedBy
WHERE r.tenant = CAST(SESSION_CONTEXT(N'organizacion') AS varchar(63))
  AND (SESSION_CONTEXT(N'expediente') IS NULL OR r.caseId = CAST(SESSION_CONTEXT(N'expediente') AS varchar(63)));
GO

CREATE VIEW [consulta].[vReglaDeEjecucion]
AS
SELECT
    e.ejecucion,
    e.expediente,
    e.nombreExpediente,
    e.organizacionGuid,
    e.proceso,
    e.nombreProceso,
    e.resultado AS resultadoEjecucion,
    e.caso,
    e.ejecutadaEn,
    e.esLaUltima,
    CAST(k.[key] AS int) + 1 AS orden,
    g.etiqueta AS regla,
    g.operador,
    -- pasa, no_pasa, sin_valores (le faltó un dato), sin_conexion (incompleta) o listo (calculó un valor).
    g.estado,
    CASE g.estado
        WHEN 'pasa' THEN N'Pasa'
        WHEN 'no_pasa' THEN N'No pasa'
        WHEN 'sin_valores' THEN N'Sin valores'
        WHEN 'listo' THEN N'Calculada'
        ELSE N'Incompleta'
    END AS resultadoRegla,
    g.texto AS explicacion,
    g.valor,
    (
        SELECT STRING_AGG(CONCAT(i.etiqueta, N' (', COALESCE(i.origen, N'sin conexión'), N'): ', COALESCE(i.valor, N'—')), N'; ')
               WITHIN GROUP (ORDER BY CAST(j.[key] AS int))
        FROM OPENJSON(g.entradas) j
        CROSS APPLY OPENJSON(j.value) WITH (etiqueta nvarchar(200) '$.etiqueta', origen nvarchar(400) '$.origen', valor nvarchar(max) '$.valor') i
    ) AS comparo
FROM [consulta].[vEjecucion] e
JOIN [nexus].[processRuns] r ON r.publicId = e.ejecucion
CROSS APPLY OPENJSON(r.detailJson, '$.reglas') k
CROSS APPLY OPENJSON(k.value) WITH (
    etiqueta nvarchar(200) '$.etiqueta',
    operador varchar(40) '$.operador',
    estado varchar(20) '$.estado',
    texto nvarchar(1000) '$.texto',
    valor nvarchar(max) '$.valor',
    entradas nvarchar(max) '$.entradas' AS JSON
) g;
GO

CREATE VIEW [consulta].[vEvidenciaDeEjecucion]
AS
SELECT
    e.ejecucion,
    e.expediente,
    e.nombreExpediente,
    e.organizacionGuid,
    e.nombreProceso,
    e.resultado AS resultadoEjecucion,
    e.ejecutadaEn,
    e.esLaUltima,
    v.tipoDocumentalId,
    v.tipoDocumental,
    v.documento,
    v.archivo,
    c.campo,
    x.valor,
    c.confianza,
    c.pagina
FROM [consulta].[vEjecucion] e
JOIN [nexus].[processRuns] r ON r.publicId = e.ejecucion
CROSS APPLY OPENJSON(r.detailJson, '$.evidencia') WITH (
    tipoDocumentalId varchar(100) '$.tipoId',
    tipoDocumental nvarchar(200) '$.tipoNombre',
    documento varchar(63) '$.documentoId',
    archivo nvarchar(400) '$.documentoNombre',
    campos nvarchar(max) '$.campos' AS JSON
) v
OUTER APPLY OPENJSON(v.campos) WITH (
    campo nvarchar(200) '$.campo',
    valores nvarchar(max) '$.valores' AS JSON,
    confianza decimal(9, 4) '$.confianza',
    pagina int '$.pagina'
) c
OUTER APPLY (
    SELECT STRING_AGG(CAST(w.value AS nvarchar(max)), N', ') WITHIN GROUP (ORDER BY CAST(w.[key] AS int)) AS valor
    FROM OPENJSON(c.valores) w
) x;
GO

CREATE VIEW [consulta].[vFaltanteDeEjecucion]
AS
SELECT
    e.ejecucion,
    e.expediente,
    e.nombreExpediente,
    e.organizacionGuid,
    e.nombreProceso,
    e.resultado AS resultadoEjecucion,
    e.ejecutadaEn,
    e.esLaUltima,
    CAST(f.[key] AS int) + 1 AS orden,
    CAST(f.value AS nvarchar(1000)) AS faltante
FROM [consulta].[vEjecucion] e
JOIN [nexus].[processRuns] r ON r.publicId = e.ejecucion
CROSS APPLY OPENJSON(r.detailJson, '$.faltantes') f;
GO

-- ── Las correcciones a mano (de la extracción vigente: las de antes que siguen valiendo ya se trasladaron a ella, NEX-614) ──────────
CREATE VIEW [consulta].[vCorreccion]
AS
SELECT
    d.publicId AS documento,
    d.tenant AS organizacionGuid,
    d.originalName AS archivo,
    r.documentTypePublicId AS tipoDocumentalId,
    COALESCE(t.name, r.documentTypeName) AS tipoDocumental,
    f.fieldPath AS campo,
    f.fieldName AS nombreCampo,
    f.recordIndex AS fila,
    f.valueNormalized AS valorExtraido,
    c.valueNormalized AS valorCorregido,
    c.reason AS motivo,
    c.correctedAt AS corregidoEn,
    COALESCE(NULLIF(CONCAT_WS(N' ', NULLIF(u.firstName, N''), NULLIF(u.lastName, N''), NULLIF(u.secondLastName, N'')), N''), u.email) AS corregidoPor,
    f.pageNumber AS pagina
FROM [nexus].[documents] d
CROSS APPLY (
    SELECT TOP (1) r0.runId, r0.documentTypeId, r0.documentTypePublicId, r0.documentTypeName
    FROM [nexus].[extractionRuns] r0
    WHERE r0.documentId = d.documentId AND r0.status <> 'error'
    ORDER BY r0.runId DESC
) r
JOIN [nexus].[entityFacts] f ON f.runId = r.runId
CROSS APPLY (
    SELECT TOP (1) fc.valueNormalized, fc.reason, fc.correctedAt, fc.correctedBy
    FROM [nexus].[fieldCorrections] fc
    WHERE fc.factId = f.factId
    ORDER BY fc.correctionId DESC
) c
LEFT JOIN [nexus].[documentTypes] t ON t.documentTypeId = r.documentTypeId
LEFT JOIN [nexus].[users] u ON u.guid = c.correctedBy
WHERE d.tenant = CAST(SESSION_CONTEXT(N'organizacion') AS varchar(63))
  AND (
      SESSION_CONTEXT(N'expediente') IS NULL
      OR EXISTS (
          SELECT 1
          FROM [nexus].[cases] k
          JOIN [nexus].[caseDocuments] cd ON cd.caseId = k.caseId
          WHERE k.publicId = CAST(SESSION_CONTEXT(N'expediente') AS varchar(63)) AND k.tenant = d.tenant AND cd.documentId = d.documentId
      )
  );
GO

-- ── La bitácora: desde qué expediente se preguntó ────────────────────────────────────────────────────────────────────────────────────
ALTER TABLE [nexus].[searchQuestions] ADD [caseId] varchar(63) NULL;
GO

ALTER TABLE [nexus].[searchQuestions] ADD CONSTRAINT [CK_searchQuestions_case] CHECK (
    [caseId] IS NULL OR ([caseId] LIKE 'expediente-%' AND [caseId] COLLATE Latin1_General_BIN2 = lower([caseId]) AND len([caseId]) = (43))
);
GO
