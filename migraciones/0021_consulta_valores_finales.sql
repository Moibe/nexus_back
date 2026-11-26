-- 0021 · Los valores finales de cada ejecución, en [consulta] (NEX-612; Moibe, 2026-11-26: «va, agrega la vista de valores finales»).
--
-- La 0020 expuso a la búsqueda las ejecuciones del motor de reglas (su resultado, cada regla, su evidencia y lo que les faltó), pero no lo que cada
-- ejecución ENTREGA: sus valores finales (`nexus.processRunFinalValues`, 0009), los nodos «Valor final» del proceso. Con esta vista se puede preguntar
-- «¿qué valores finales dio la última ejecución?» o «¿en qué expedientes el valor final Monto pasa de 100,000?».
--
--   vValorFinalDeEjecucion   un renglón por valor final de cada ejecución: su nombre, qué es (un valor, una lista o un veredicto), de qué clase es el dato,
--                            si tiene valor (o por qué no: le faltó el dato o no tenía nada conectado), el valor como se enseña, ese valor como número o
--                            como fecha cuando lo es, cuántos elementos trae una lista y de dónde salió.
--
-- Se arma sobre `consulta.vEjecucion`, así que hereda su aislamiento: la organización fijada en la conexión y, cuando se pregunta desde un expediente,
-- solo ese expediente. Como la 0017 y la 0020, se detiene si la base no tiene el esquema [consulta] (51016): ⚠ staging y producción todavía no lo tienen.

IF SCHEMA_ID('consulta') IS NULL
    THROW 51016, 'Falta el esquema consulta: lo crea el DBA (CREATE SCHEMA consulta AUTHORIZATION nexusOwner). Ver docs/solicitudes-dba.md §8.', 1;
GO

IF HAS_PERMS_BY_NAME(N'consulta', N'SCHEMA', N'ALTER') = 0
    THROW 51018, 'usrNexusDDL no tiene ALTER sobre el esquema consulta: falta GRANT ALTER ON SCHEMA::consulta TO usrNexusDDL. Ver docs/solicitudes-dba.md §8.', 1;
GO

IF (SELECT principal_id FROM sys.schemas WHERE name = N'consulta') <> (SELECT principal_id FROM sys.schemas WHERE name = N'nexus')
    THROW 51017, 'El esquema consulta no es del mismo dueño que nexus (nexusOwner): el encadenamiento de propiedad no funcionaría. Ver docs/solicitudes-dba.md §8.', 1;
GO

CREATE VIEW [consulta].[vValorFinalDeEjecucion]
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
    v.position AS orden,
    v.name AS valorFinal,
    -- valor (un dato), lista (varios) o veredicto (el Pasa / No pasa de una regla).
    v.kind AS forma,
    -- texto, numero, fecha o booleano; NULL en un veredicto.
    v.family AS familia,
    -- listo (tiene valor), sin_valores (estaba conectado y faltó el dato) o sin_conexion (no tenía nada conectado). Lo mismo, como lo dice la pantalla.
    v.status AS estado,
    CASE v.status WHEN 'listo' THEN N'Con valor' WHEN 'sin_valores' THEN N'Sin valor (faltó el dato)' ELSE N'Sin conectar' END AS situacion,
    -- Como se enseña: el valor; de una lista, sus elementos separados por coma; de un veredicto, «Pasa» o «No pasa». NULL si no tiene valor.
    v.valueText AS valor,
    CASE WHEN v.status = 'listo' AND v.kind = 'valor' AND v.family = 'numero' THEN TRY_CAST(v.valueText AS decimal(38, 6)) END AS valorNumero,
    CASE
        WHEN v.status = 'listo' AND v.kind = 'valor' AND v.family = 'fecha' AND v.valueText LIKE '[12][0-9][0-9][0-9]-[01][0-9]-[0-3][0-9]%'
            THEN TRY_CONVERT(date, LEFT(v.valueText, 10), 23)
    END AS valorFecha,
    CASE WHEN v.kind = 'lista' AND v.valueJson IS NOT NULL THEN (SELECT count(*) FROM OPENJSON(v.valueJson)) END AS elementos,
    -- De dónde salió, en palabras: «rfc · INE», «Igual a #1», «Valor fijo #2».
    v.origin AS origen
FROM [consulta].[vEjecucion] e
JOIN [nexus].[processRuns] r ON r.publicId = e.ejecucion
JOIN [nexus].[processRunFinalValues] v ON v.processRunId = r.processRunId;
GO
