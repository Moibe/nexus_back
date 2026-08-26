"""Diagnóstico del esquema de tenants: qué hay que sembrar para que funcione.

Corre en el SERVER:
    cd /home/mbriseno/code/nexus_back && venv/bin/python diagnostico_tenants.py

Hallazgos acumulados (2026-08-26):

  - `[security].[uspCreateTenant]` falla con `RAISERROR` 50006 "The tenant
    sequence is not configured". El número >= 50000 prueba que el SP se ejecutó
    → permiso `EXECUTE` confirmado (cierra el paso 1 de docs/solicitudes-dba.md).
  - "tenant sequence" NO es un objeto SEQUENCE de SQL Server: no hay ninguno en
    la base. Es la tabla `[security].[tenantSequence]`, con 8 columnas y **0
    filas** — le falta la fila de configuración que el SP espera.
  - El vocabulario de `@tenantStatusCode` vive en `[reference].[tenantStatuses]`
    (3 filas), en un esquema `reference` aparte.
  - No hay permiso `VIEW DEFINITION`, así que no se puede leer el cuerpo de los
    SPs ni la definición de las restricciones CHECK. Sus NOMBRES sí se ven, y
    de ahí salió que `tenantSequence` tiene columnas `prefix` y `lastSequence`.

Este script saca la estructura y el contenido de esas tablas para poder decirle
a Charlie exactamente qué sembrar, en vez de reenviarle el mensaje de error.
Todo es de solo lectura; las tablas que se leen son de catálogo/configuración,
no datos de negocio.
"""

import sys

from db.sqlserver import obtener_conexion


def titulo(texto: str) -> None:
    print()
    print("=" * 70)
    print(texto)
    print("=" * 70)


def columnas_de(cur, esquema: str, tabla: str) -> None:
    """Estructura de una tabla: tipo, nulabilidad, identity, default."""
    cur.execute(
        """
        SELECT c.column_id, c.name, TYPE_NAME(c.user_type_id) AS tipo,
               c.max_length, c.is_nullable, c.is_identity,
               ISNULL(dc.definition, '') AS valor_default
        FROM sys.columns c
        JOIN sys.tables t ON t.object_id = c.object_id
        LEFT JOIN sys.default_constraints dc
               ON dc.parent_object_id = c.object_id
              AND dc.parent_column_id = c.column_id
        WHERE SCHEMA_NAME(t.schema_id) = ? AND t.name = ?
        ORDER BY c.column_id
        """,
        esquema,
        tabla,
    )
    print(f"\n  [{esquema}].[{tabla}]")
    for f in cur.fetchall():
        marcas = []
        if not f[4]:
            marcas.append("NOT NULL")
        if f[5]:
            marcas.append("IDENTITY")
        if f[6]:
            marcas.append(f"DEFAULT {f[6]}")
        largo = f"({f[3]})" if f[3] not in (None, 0) else ""
        print(f"    {f[0]:2}. {f[1]:24} {f[2]}{largo:10} {' · '.join(marcas)}")


def contenido_de(cur, esquema: str, tabla: str, tope: int = 25) -> None:
    """Filas de una tabla de catálogo, para conocer su vocabulario real."""
    # Los nombres vienen de sys.tables / de este archivo, nunca de una request.
    cur.execute(f"SELECT TOP {tope} * FROM [{esquema}].[{tabla}]")
    columnas = [c[0] for c in cur.description]
    filas = cur.fetchall()
    print(f"\n  [{esquema}].[{tabla}] — {len(filas)} filas")
    print(f"    columnas: {columnas}")
    for f in filas:
        print(f"    {dict(zip(columnas, f))}")


def main() -> int:
    con = obtener_conexion()
    try:
        cur = con.cursor()

        titulo("1 · [security].[tenantSequence] — la tabla que hay que sembrar")
        print("El SP falla porque está vacía. Esto dice qué columnas necesita")
        print("una fila, cuáles son NOT NULL y cuáles tienen DEFAULT.")
        columnas_de(cur, "security", "tenantSequence")
        contenido_de(cur, "security", "tenantSequence")

        titulo("2 · [reference].[tenantStatuses] — vocabulario de @tenantStatusCode")
        columnas_de(cur, "reference", "tenantStatuses")
        contenido_de(cur, "reference", "tenantStatuses")

        titulo("3 · [security].[tenants] — forma de un tenant")
        print("Para entender qué campos existen más allá de los 3 que recibe")
        print("uspCreateTenant, y cuáles los pone la base sola.")
        columnas_de(cur, "security", "tenants")

        titulo("4 · [security].[roles] — ya viene sembrada (4 filas)")
        print("Sirve de EJEMPLO de cómo Charlie siembra un catálogo: mismo")
        print("estilo probablemente aplica a lo que falta en tenantSequence.")
        contenido_de(cur, "security", "roles")

        titulo("5 · Otros esquemas en la base")
        print("Apareció [reference], que no conocíamos. Puede haber más.")
        cur.execute(
            """
            SELECT SCHEMA_NAME(t.schema_id) AS esquema, COUNT(*) AS tablas
            FROM sys.tables t
            GROUP BY SCHEMA_NAME(t.schema_id)
            ORDER BY esquema
            """
        )
        for f in cur.fetchall():
            print(f"  [{f[0]}] — {f[1]} tablas")

        titulo("6 · Todos los stored procedures disponibles")
        print("Para ver el panorama completo de lo que Charlie ya expuso.")
        cur.execute(
            """
            SELECT SCHEMA_NAME(p.schema_id) AS esquema, p.name,
                   (SELECT COUNT(*) FROM sys.parameters par
                     WHERE par.object_id = p.object_id) AS params
            FROM sys.procedures p
            ORDER BY esquema, p.name
            """
        )
        for f in cur.fetchall():
            print(f"  [{f[0]}].[{f[1]}]  ({f[2]} parámetros)")

        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
