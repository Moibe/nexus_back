-- 0017 · Las vistas de consulta SEPARADAS POR ORGANIZACIÓN, en el esquema [consulta] (Moibe, 2026-11-11: «Cada organización, lo suyo»).
--
-- Escrita el 2026-11-12 como 0016 y guardada en `migraciones/en_espera/` hasta que el DBA hiciera su parte. Charlie la entregó en
-- `IA_Nexus_Dev` el 2026-11-23 (NEX-604) y se activó ese día como 0017 (la 0016 ya era `ejemplos_etiquetados`; Moibe: «va, actívala en
-- dev, luego vemos lo de staging y prod»). ⚠ IA_Nexus_Staging e IA_Nexus todavía NO tienen el esquema: un deploy de staging o de producción
-- con esta migración se detiene en su guarda (51016) y el back no reinicia. Antes de promover dev → staging hay que pedirle al DBA lo mismo
-- en esas dos bases.
--
-- Para qué: la herramienta de texto a SQL (Vanna AI, por ahora) consulta lo extraído con un usuario de SOLO lectura, `usrNexusLectura`. Las
-- vistas de la 0015 (`nexus.vDocumentoExtraido`, `nexus.vDatoExtraido`) traen TODAS las organizaciones, así que ese usuario no las lee: lee
-- estas, que tienen los mismos nombres y las mismas columnas y solo devuelven los renglones de la organización FIJADA EN LA CONEXIÓN.
--
-- Cómo se fija la organización: al abrir cada conexión, la aplicación ejecuta PRIMERO
--     EXEC sys.sp_set_session_context @key = N'organizacion', @value = '<guid de la organización>', @read_only = 1;
-- Con `@read_only = 1` el valor no se puede cambiar en esa conexión (error 15664), aunque el modelo lo intente en la consulta que escribe.
-- Sin organización fijada, `SESSION_CONTEXT` es NULL, la comparación nunca es cierta y las vistas devuelven 0 renglones. El filtro lo pone la
-- BASE, no el `WHERE` que escriba el modelo (se le puede olvidar o lo pueden engañar para que lo quite).
--
-- Por qué funciona sin dar acceso a [nexus]: `[consulta]` y `[nexus]` son del mismo dueño (`nexusOwner`). Por encadenamiento de propiedad, quien
-- tiene SELECT sobre una vista de `[consulta]` la lee completa sin necesitar permiso sobre las vistas y tablas de `[nexus]` que hay debajo. Si
-- `[consulta]` tuviera OTRO dueño la cadena se rompe y el usuario recibiría el error 229; por eso la migración se niega si los dueños no
-- coinciden.
--
-- Lo que hace el DBA (no esta migración: `usrNexusDDL` no puede crear esquemas ni usuarios), pedido en `docs/solicitudes-dba.md` §8:
-- `CREATE SCHEMA consulta AUTHORIZATION nexusOwner`, el login `usrNexusLectura` con `GRANT SELECT ON SCHEMA::consulta` y nada más, y
-- `GRANT ALTER ON SCHEMA::consulta TO usrNexusDDL` para que esta migración pueda crear las vistas. Mientras eso no exista en una base, esta
-- migración se DETIENE ahí con un mensaje claro (y un deploy con ella no reinicia el back); por eso esperó en `migraciones/en_espera/` hasta que
-- el DBA lo entregó en `IA_Nexus_Dev`, y no se promueve a staging ni a producción sin el mismo ticket en esas bases.
--
-- Las columnas van con nombre (no `SELECT *`): una vista guarda sus columnas al crearse, y con nombres explícitos una columna nueva en la vista
-- de `[nexus]` no aparece aquí sin que una migración lo decida.

IF SCHEMA_ID('consulta') IS NULL
    THROW 51016, 'Falta el esquema consulta: lo crea el DBA (CREATE SCHEMA consulta AUTHORIZATION nexusOwner). Ver docs/solicitudes-dba.md §8.', 1;
GO

-- Medido en SQL Server 2022: el esquema sí se VE sin permiso sobre él, y lo que falla es el CREATE VIEW con un 2760 genérico.
IF HAS_PERMS_BY_NAME(N'consulta', N'SCHEMA', N'ALTER') = 0
    THROW 51018, 'usrNexusDDL no tiene ALTER sobre el esquema consulta: falta GRANT ALTER ON SCHEMA::consulta TO usrNexusDDL. Ver docs/solicitudes-dba.md §8.', 1;
GO

-- Mismo dueño que [nexus], o el usuario de lectura no podría leer lo que hay debajo (encadenamiento de propiedad).
IF (SELECT principal_id FROM sys.schemas WHERE name = N'consulta') <> (SELECT principal_id FROM sys.schemas WHERE name = N'nexus')
    THROW 51017, 'El esquema consulta no es del mismo dueño que nexus (nexusOwner): el encadenamiento de propiedad no funcionaría. Ver docs/solicitudes-dba.md §8.', 1;
GO

CREATE VIEW [consulta].[vDocumentoExtraido]
AS
SELECT
    documento,
    organizacionGuid,
    organizacion,
    archivo,
    mime,
    canal,
    recibidoEn,
    extraccion,
    tipoDocumentalId,
    tipoDocumental,
    revisionTipoDocumental,
    estado,
    confianzaPromedio,
    confianzaMinima,
    paginas,
    extraidoEn,
    extracciones,
    campos,
    camposConValor,
    camposFaltantes,
    camposCorregidos
FROM [nexus].[vDocumentoExtraido]
WHERE organizacionGuid = CAST(SESSION_CONTEXT(N'organizacion') AS varchar(63));
GO

CREATE VIEW [consulta].[vDatoExtraido]
AS
SELECT
    documento,
    organizacionGuid,
    organizacion,
    archivo,
    tipoDocumentalId,
    tipoDocumental,
    campo,
    nombreCampo,
    fila,
    valor,
    valorNumero,
    valorFecha,
    confianza,
    pagina,
    corregido,
    motivoSinValor,
    extraidoEn
FROM [nexus].[vDatoExtraido]
WHERE organizacionGuid = CAST(SESSION_CONTEXT(N'organizacion') AS varchar(63));
GO
