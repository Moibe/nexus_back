"""Prueba de punta a punta de los SPs de tenant, contra SQL Server real.

Corre en el SERVER (esta es la única máquina con ruta a SQL Server):

    cd /home/mbriseno/code/nexus_back && venv/bin/python verificar_tenants.py

Es el primer ejercicio de stored procedures REALES del proyecto, así que además
de probar el CRUD responde tres cosas que no se sabían y que cambian cómo se
escribe el código que los consuma:

  1. ¿`uspCreateTenant` devuelve el GUID que generó? Sin eso, un tenant recién
     creado es irrecuperable: `uspGetTenant` exige el GUID y no hay SP de listar.
  2. ¿Los SPs traen `SET NOCOUNT ON`? Si no, el primer result set que ve pyodbc
     es el contador de filas del INSERT y los datos quedan en el segundo — la
     razón por la que el repositorio usa `ejecutar_sp_multiple`.
  3. ¿Qué valores acepta `@tenantStatusCode varchar(20)`? Su vocabulario lo
     define la base y no está documentado.

Crea datos reales, marcados con prefijo TEST_ y un timestamp para que sean
obvios y fáciles de limpiar después.
"""

import json
import sys
import uuid
from datetime import datetime, timezone

from db.sqlserver import ejecutar_sp_multiple, obtener_conexion
from repositorios import tenants

SELLO = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
NOMBRE = f"TEST_moibe_borrar {SELLO}"
SLUG = f"test-moibe-borrar-{SELLO}"


def titulo(texto: str) -> None:
    print()
    print("=" * 68)
    print(texto)
    print("=" * 68)


def descubrir_estados() -> list[str]:
    """Lee el catálogo de estados de tenant, para no adivinar el status code.

    Busca en TODOS los esquemas, no solo en `security`: la primera versión de
    esto solo miraba ahí y reportó "(ninguna)" — el catálogo vive en
    `[reference].[tenantStatuses]`, un esquema aparte que no conocíamos.

    Es diagnóstico de una sola vez sobre una tabla de catálogo, no algo que la
    aplicación haga en runtime: la regla de "solo SPs" sigue aplicando para el
    código de producción.
    """
    con = obtener_conexion()
    try:
        cur = con.cursor()
        cur.execute(
            """
            SELECT SCHEMA_NAME(t.schema_id) AS esquema, t.name
            FROM sys.tables t
            WHERE t.name LIKE '%Status%' OR t.name LIKE '%Estado%'
            ORDER BY esquema, t.name
            """
        )
        tablas = cur.fetchall()
        print(f"Catálogos de estado encontrados: "
              f"{[f'[{e}].[{n}]' for e, n in tablas] or '(ninguno)'}")

        valores: list[str] = []
        for esquema, tabla in tablas:
            # Los nombres vienen de sys.tables, no de una request — no hay
            # superficie de inyección aquí.
            cur.execute(f"SELECT TOP 20 * FROM [{esquema}].[{tabla}]")
            columnas = [c[0] for c in cur.description]
            print(f"\n  [{esquema}].[{tabla}] — columnas: {columnas}")
            for fila in cur.fetchall():
                registro = dict(zip(columnas, fila))
                print(f"    {registro}")
                # Solo se consideran los estados ACTIVOS del catálogo: sembrar
                # un estado con isActive=0 y luego usarlo sería incoherente.
                if registro.get("isActive") is False:
                    continue
                codigo = registro.get("code")
                if isinstance(codigo, str):
                    valores.append(codigo)
        return valores
    finally:
        con.close()


def main() -> int:
    titulo("0 · Vocabulario de @tenantStatusCode")
    try:
        estados = descubrir_estados()
    except Exception as exc:  # noqa: BLE001
        print(f"No se pudo leer el catálogo ({type(exc).__name__}: {exc})")
        estados = []

    titulo("1 · uspCreateTenant — ¿devuelve el GUID que generó?")
    print(f"name = {NOMBRE!r}\nslug = {SLUG!r}")
    # Se llama al SP crudo (no vía el repositorio) para VER todos los result
    # sets: es lo que revela si falta SET NOCOUNT ON.
    sets = ejecutar_sp_multiple(
        "[security].[uspCreateTenant]",
        {"name": NOMBRE, "slug": SLUG, "settingsJson": json.dumps({"prueba": True})},
        commit=True,
    )
    print(f"\nresult sets devueltos: {len(sets)}")
    for i, conjunto in enumerate(sets):
        print(f"  set[{i}]: {len(conjunto)} filas — {conjunto}")

    if len(sets) > 1 and not sets[0]:
        print(
            "\n⚠️  El primer set viene vacío y los datos están en el segundo: "
            "al SP le falta `SET NOCOUNT ON`. No rompe nada aquí porque el "
            "repositorio usa ejecutar_sp_multiple, pero vale decírselo a Charlie."
        )

    creado = tenants._primer_set_con_filas(sets)
    if not creado:
        print(
            "\n❌ El SP no devolvió ninguna fila. Sin el GUID, este tenant es "
            "IRRECUPERABLE desde la app (uspGetTenant lo exige y no hay SP de "
            "listar). Es lo primero que hay que pedirle a Charlie: que "
            "uspCreateTenant devuelva el registro creado."
        )
        return 1

    # Nota: si el SP falla con RAISERROR 50006 "The tenant sequence is not
    # configured", NO es un problema de este script ni de permisos —
    # `[security].[tenantSequence]` está vacía y le toca al DBA sembrarla.
    # Ver la sección 0b de docs/solicitudes-dba.md.

    registro = creado[0]
    print(f"\n✅ Fila devuelta: {registro}")

    # Se busca el GUID por TIPO (uuid.UUID) y por nombre exacto, no con un
    # `"id" in nombre`: en [security].[tenants] el orden de columnas es
    # tenantId(1), tenantGuid(2), tenantStatusId(3)... así que un heurístico
    # laxo agarraría `tenantId` —el int interno— y luego reventaría al pasarlo
    # a uspGetTenant, que espera uniqueidentifier. Lo que la app necesita es
    # específicamente el GUID: es lo único que uspGetTenant acepta.
    guid = None
    for clave, valor in registro.items():
        if isinstance(valor, uuid.UUID) or clave.lower() in ("tenantguid", "guid"):
            guid = valor
            break

    if guid is None:
        print("❌ La fila NO trae el tenantGuid. Columnas:", list(registro))
        print(
            "\nEsto no alcanza: uspGetTenant recibe `uniqueidentifier`, así que\n"
            "aunque el SP devuelva la fila, sin el GUID el tenant sigue siendo\n"
            "irrecuperable desde la app. Hay que pedirle a Charlie que el SELECT\n"
            "final incluya tenantGuid (el tenantId int interno no sirve aquí)."
        )
        return 1
    print(f"GUID del tenant nuevo: {guid}")

    titulo("2 · uspGetTenant — recuperar lo que se acaba de crear")
    leido = tenants.obtener_tenant(str(guid))
    print(f"Devuelto: {leido}")
    if not leido:
        print("❌ No se pudo leer de vuelta un tenant que sí se creó.")
        return 1
    coincide = leido.get("name") == NOMBRE or NOMBRE in str(leido.values())
    print(f"{'✅' if coincide else '⚠️ '} El name coincide con lo que se insertó: {coincide}")

    titulo("3 · uspUpdateTenant — actualizar ese mismo registro")
    nombre_nuevo = f"{NOMBRE} (actualizado)"
    # Si el catálogo dio valores, se usa el primero; si no, se prueba un valor
    # común y el error dirá cuál era el correcto.
    estado = estados[0] if estados else "ACTIVE"
    print(f"tenantStatusCode a probar: {estado!r}"
          f"{' (del catálogo)' if estados else ' (adivinado — no había catálogo)'}")
    try:
        actualizado = tenants.actualizar_tenant(
            tenant_guid=str(guid),
            nombre=nombre_nuevo,
            slug=SLUG,
            tenant_status_code=estado,
            settings_json=json.dumps({"prueba": True, "actualizado": True}),
        )
        print(f"Devuelto: {actualizado}")
    except Exception as exc:  # noqa: BLE001
        print(f"❌ {type(exc).__name__}: {exc}")
        print(
            "\nSi el error menciona una restricción CHECK o una FK sobre el "
            "status, el valor probado no es del vocabulario válido — el mensaje "
            "de SQL Server normalmente nombra la restricción, y de ahí sale la "
            "tabla o la lista de valores permitidos."
        )
        return 1

    titulo("4 · uspGetTenant otra vez — confirmar que el update persistió")
    final = tenants.obtener_tenant(str(guid))
    print(f"Devuelto: {final}")
    persistio = nombre_nuevo in str(final.values())
    print(f"{'✅' if persistio else '❌'} El cambio de name persistió: {persistio}")

    titulo("RESUMEN")
    print(f"Tenant de prueba creado — GUID {guid}")
    print(f"  name original : {NOMBRE!r}")
    print(f"  name final    : {nombre_nuevo!r}")
    print(f"  slug          : {SLUG!r}")
    print("\nQuedó en la base. Para limpiarlo hace falta un SP de baja/borrado")
    print("que hoy no existe — es algo que se le puede pedir a Charlie, o")
    print("dejarlo si el status code sirve para desactivarlo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
