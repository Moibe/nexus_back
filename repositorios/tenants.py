"""Repositorio del dominio tenants: los SPs del esquema `[security]`.

Primer dominio con stored procedures REALES, entregados por Charlie el
2026-08-26. Todo lo anterior en `repositorios/` era placeholder inventado.

La convención de nombres es de él y este módulo se adapta, no al revés:
esquema `[security]`, prefijo `usp`, inglés, y parámetros en camelCase
(`@tenantGuid`, `@settingsJson`) — distinto de lo que asumían los placeholders
de `documentos.py` (`dbo.sp_*`, español, PascalCase). Los nombres van entre
corchetes tal como él los expone; el validador de `db.sqlserver` ya los acepta.

Firmas leídas de `sys.parameters`, no supuestas:

    [security].[uspCreateTenant](@name nvarchar(200), @slug varchar(63),
                                 @settingsJson nvarchar(MAX))
    [security].[uspGetTenant](@tenantGuid uniqueidentifier)
    [security].[uspUpdateTenant](@tenantGuid uniqueidentifier,
                                 @name nvarchar(200), @slug varchar(63),
                                 @tenantStatusCode varchar(20),
                                 @settingsJson nvarchar(MAX))

Ninguno declara parámetros `OUTPUT`, así que todo lo que devuelvan vuelve como
result set.

Vocabulario de `@tenantStatusCode` (leído de `[reference].[tenantStatuses]` el
2026-08-26 — la base es la dueña, esto es referencia, no validación):

    ACTIVE     · Tenant enabled for normal platform operation.
    SUSPENDED  · Tenant temporarily blocked from operating.
    CLOSED     · Tenant permanently closed.

A propósito NO se replica como constante con validación de este lado: el
catálogo vive en la base y puede crecer. Si se validara aquí, agregar un estado
exigiría un despliegue de la API — el mismo razonamiento por el que el
`transform` de la sección 2.6 es un catálogo cerrado *deliberado* y esto no.

⚠️ `[security].[tenantSequence]` está VACÍA al 2026-08-26, y por eso
`uspCreateTenant` falla con `RAISERROR` 50006 "The tenant sequence is not
configured". Es una fila de configuración que le toca sembrar al DBA; ver
`docs/solicitudes-dba.md`. Hasta que exista, `crear_tenant` no funciona —
`obtener_tenant` y `actualizar_tenant` sí, si ya hay un tenant.
"""

from typing import Any

from db.sqlserver import ejecutar_sp_multiple


def _primer_set_con_filas(sets: list[list[dict]]) -> list[dict]:
    """Primer result set que traiga filas, ignorando los vacíos de adelante.

    Existe por una trampa de pyodbc: si un SP hace `INSERT` y luego `SELECT`
    sin `SET NOCOUNT ON` al inicio, el driver ve PRIMERO el contador de filas
    del INSERT — un "result set" sin `description` — y solo después los datos.
    `ejecutar_sp()` (que toma únicamente el primero) devolvería una lista vacía
    como si el SP no hubiera retornado nada, y el bug se leería como "el SP no
    regresa el GUID" cuando en realidad sí lo regresa.

    Usar esto en vez de `ejecutar_sp()` hace que el código funcione igual con o
    sin `SET NOCOUNT ON`, así que no hay que pedirle a Charlie que lo agregue
    para que esto sirva. Si otro dominio necesita el mismo patrón, conviene
    subirlo a `db/sqlserver.py`.
    """
    for conjunto in sets:
        if conjunto:
            return conjunto
    return []


def crear_tenant(nombre: str, slug: str, settings_json: str = "{}") -> dict:
    """Da de alta un tenant y devuelve lo que el SP reporte del registro creado.

    El GUID lo genera la base (el SP no recibe `@tenantGuid`), y como tampoco
    hay parámetro OUTPUT, la única vía para conocerlo es el result set. Importa
    porque `obtener_tenant` EXIGE el GUID y no existe un SP de "listar
    tenants": si esto regresara vacío, un tenant recién creado sería
    irrecuperable desde la aplicación.
    """
    sets = ejecutar_sp_multiple(
        "[security].[uspCreateTenant]",
        {"name": nombre, "slug": slug, "settingsJson": settings_json},
        commit=True,
    )
    filas = _primer_set_con_filas(sets)
    return filas[0] if filas else {}


def obtener_tenant(tenant_guid: str) -> dict:
    """Un tenant por su GUID. Diccionario vacío si no existe.

    `uniqueidentifier` viaja como string; pyodbc lo convierte. SQL Server
    acepta la forma con y sin guiones.
    """
    sets = ejecutar_sp_multiple(
        "[security].[uspGetTenant]", {"tenantGuid": tenant_guid}
    )
    filas = _primer_set_con_filas(sets)
    return filas[0] if filas else {}


def actualizar_tenant(
    tenant_guid: str,
    nombre: str,
    slug: str,
    tenant_status_code: str,
    settings_json: str = "{}",
) -> dict:
    """Actualiza un tenant.

    OJO — el SP recibe TODOS los campos, así que la firma obliga a mandarlos
    completos: no es un update parcial. Si el cuerpo del SP no hace `COALESCE`
    contra el valor actual, mandar un campo en None lo borraría. Por eso aquí
    son parámetros obligatorios (menos `settings_json`) en vez de opcionales
    con default None: la firma del SP no permite omitir sin decidir qué se
    escribe, y un opcional invitaría a pisar datos sin querer.

    `tenant_status_code` es varchar(20); los valores válidos son `ACTIVE`,
    `SUSPENDED` y `CLOSED` — ver la nota del encabezado del módulo.
    """
    sets = ejecutar_sp_multiple(
        "[security].[uspUpdateTenant]",
        {
            "tenantGuid": tenant_guid,
            "name": nombre,
            "slug": slug,
            "tenantStatusCode": tenant_status_code,
            "settingsJson": settings_json,
        },
        commit=True,
    )
    filas = _primer_set_con_filas(sets)
    return filas[0] if filas else {}
