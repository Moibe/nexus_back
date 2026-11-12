-- 0015 · Dos vistas de LECTURA sobre los datos extraídos, para consultarlos sin pelearse con su forma (Moibe, 2026-11-11).
--
-- Para qué: Moibe quiere hacer preguntas en lenguaje natural sobre lo que ya se extrajo («¿cuántas INE de Dorama vencen este año?»), con
-- una herramienta de texto a SQL (Vanna AI, por ahora). Las tablas de la 0007 son buenas para guardar, malas para preguntar:
--   - un documento tiene VARIAS corridas y cada campo puede tener correcciones: lo vigente lo resuelve `vEntityFactCurrent`, pero sin el
--     nombre del tipo documental, de la organización ni del archivo;
--   - todo valor es texto (`nvarchar`): una fecha o un número hay que convertirlos en cada consulta.
-- Estas vistas dejan eso resuelto, con nombres de columna en español (son los que usará quien pregunte, y los campos ya se llaman en
-- español: `apellido_materno`, `vigencia`…). NO guardan nada: si cambian los datos, cambian ellas.
--
--   vDocumentoExtraido  un renglón por documento con su extracción VIGENTE (la última que terminó, igual que `vEntityFactCurrent`):
--                       organización, archivo, tipo documental, confianzas y cuántos de sus campos tienen valor, faltaron o se corrigieron.
--                       Un documento que nunca se extrajo bien no aparece.
--   vDatoExtraido       un renglón por CAMPO vigente de cada documento, con su organización, tipo, valor (ya corregido si se corrigió) y el
--                       mismo valor convertido a número y a fecha cuando se puede (`valorNumero`, `valorFecha`; NULL si no es eso).
--
-- Por qué una vista por FORMA y no una por tipo documental (una `vINE` con una columna por campo): los tipos documentales son DATOS, los
-- crea cada organización desde la pantalla y cambian de campos con cada versión; una vista por tipo tendría que crearse con DDL cada vez,
-- y la API no tiene ese permiso (solo `usrNexusDDL`, en migraciones). Para pivotear un tipo basta un `MAX(CASE WHEN nombreCampo = …)`
-- sobre `vDatoExtraido`, y eso se le enseña a la herramienta con ejemplos.
--
-- Datos de clientes: estas vistas exponen valores. Quien las consulte con lenguaje natural tiene que hacerlo con un usuario de SOLO
-- lectura y filtrando por organización en el servidor (no confiando en que el modelo lo recuerde). Eso se decide al conectar la
-- herramienta; aquí solo están las vistas.

IF SCHEMA_ID('nexus') IS NULL
    THROW 51001, 'Falta el esquema nexus: lo crea el DBA (usrNexusDDL no puede crear esquemas).', 1;
GO

CREATE VIEW [nexus].[vDocumentoExtraido]
AS
SELECT
    d.publicId AS documento,
    d.tenant AS organizacionGuid,
    o.name AS organizacion,
    d.originalName AS archivo,
    d.mime,
    d.channel AS canal,
    d.receivedAt AS recibidoEn,
    r.publicId AS extraccion,
    r.documentTypePublicId AS tipoDocumentalId,
    COALESCE(t.name, r.documentTypeName) AS tipoDocumental,
    r.documentTypeRevision AS revisionTipoDocumental,
    r.status AS estado,
    r.confidenceAverage AS confianzaPromedio,
    r.confidenceMinimum AS confianzaMinima,
    r.pageCount AS paginas,
    r.finishedAt AS extraidoEn,
    (SELECT count(*) FROM [nexus].[extractionRuns] x WHERE x.documentId = d.documentId) AS extracciones,
    v.campos,
    v.camposConValor,
    v.camposFaltantes,
    v.camposCorregidos
FROM [nexus].[documents] d
CROSS APPLY (
    SELECT TOP (1) r0.publicId, r0.documentTypeId, r0.documentTypePublicId, r0.documentTypeName, r0.documentTypeRevision, r0.status,
        r0.confidenceAverage, r0.confidenceMinimum, r0.pageCount, r0.finishedAt
    FROM [nexus].[extractionRuns] r0
    WHERE r0.documentId = d.documentId AND r0.status <> 'error'
    ORDER BY r0.runId DESC
) r
LEFT JOIN [nexus].[documentTypes] t ON t.documentTypeId = r.documentTypeId
LEFT JOIN [nexus].[tenants] o ON o.guid = d.tenant
OUTER APPLY (
    SELECT
        count(*) AS campos,
        sum(CASE WHEN f.valueNormalized IS NOT NULL THEN 1 ELSE 0 END) AS camposConValor,
        sum(CASE WHEN f.nullReason IS NOT NULL THEN 1 ELSE 0 END) AS camposFaltantes,
        sum(CAST(f.isCorrected AS int)) AS camposCorregidos
    FROM [nexus].[vEntityFactCurrent] f
    WHERE f.documentId = d.documentId
) v;
GO

CREATE VIEW [nexus].[vDatoExtraido]
AS
SELECT
    d.publicId AS documento,
    d.tenant AS organizacionGuid,
    o.name AS organizacion,
    d.originalName AS archivo,
    f.documentTypePublicId AS tipoDocumentalId,
    COALESCE(t.name, r.documentTypeName) AS tipoDocumental,
    -- La ruta completa (`domicilio.calle`, `socios[2].nombre`) y el nombre del campo hoja (`calle`, `nombre`).
    f.fieldPath AS campo,
    f.fieldName AS nombreCampo,
    -- La fila, en lo que se repite (`socios[2].nombre` → 2); NULL si el campo no se repite.
    f.recordIndex AS fila,
    f.valueNormalized AS valor,
    TRY_CAST(f.valueNormalized AS decimal(38, 6)) AS valorNumero,
    -- Solo fechas ISO (`2024-03-15`, que es como las normaliza la extracción); un rango como `2024-2034` queda NULL.
    CASE
        WHEN f.valueNormalized LIKE '[12][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]%' THEN TRY_CONVERT(date, LEFT(f.valueNormalized, 10), 23)
    END AS valorFecha,
    f.confidence AS confianza,
    f.pageNumber AS pagina,
    f.isCorrected AS corregido,
    -- Por qué no hay valor: `not_present` (no apareció), `illegible`, `below_threshold`, `extraction_error`. NULL si hay valor.
    f.nullReason AS motivoSinValor,
    r.finishedAt AS extraidoEn
FROM [nexus].[vEntityFactCurrent] f
JOIN [nexus].[documents] d ON d.documentId = f.documentId
JOIN [nexus].[extractionRuns] r ON r.runId = f.runId
LEFT JOIN [nexus].[documentTypes] t ON t.documentTypeId = f.documentTypeId
LEFT JOIN [nexus].[tenants] o ON o.guid = d.tenant;
GO
