"""Diagnóstico: qué le falta a `[security].[uspCreateTenant]` para funcionar.

Corre en el SERVER:
    cd /home/mbriseno/code/nexus_back && venv/bin/python diagnostico_tenants.py

Contexto — 2026-08-26, `verificar_tenants.py` falló así:

    ('42000', '[42000] ... The tenant sequence is not configured. (50006)')

El número 50006 está por encima de 50000, o sea es un error DEFINIDO POR EL
USUARIO: un `RAISERROR`/`THROW` dentro del propio SP. Eso prueba que el SP se
ejecutó (permiso `EXECUTE` ✅) y que lo que falló fue una validación suya.

Este script busca a qué se refiere con "tenant sequence" leyendo el código
fuente del SP y el catálogo del sistema. Todo es de SOLO LECTURA y sobre
metadata (`sys.*`), no datos de negocio.
"""

import sys

from db.sqlserver import obtener_conexion


def titulo(texto: str) -> None:
    print()
    print("=" * 70)
    print(texto)
    print("=" * 70)


def main() -> int:
    con = obtener_conexion()
    try:
        cur = con.cursor()

        titulo("1 · Código fuente de los SPs de tenant")
        print("Es la fuente de verdad: dice qué valida, qué devuelve, de dónde")
        print("saca la 'sequence' y qué valores acepta @tenantStatusCode.\n")
        for sp in ("uspCreateTenant", "uspGetTenant", "uspUpdateTenant"):
            cur.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?))", f"[security].[{sp}]")
            fila = cur.fetchone()
            fuente = fila[0] if fila else None
            print("-" * 70)
            print(f"----- {sp} -----")
            print("-" * 70)
            if fuente:
                print(fuente)
            else:
                print("(NULL — falta el permiso VIEW DEFINITION sobre este objeto.")
                print(" No es bloqueante: se le puede preguntar a Charlie directo.)")
            print()

        titulo("2 · Objetos SEQUENCE en la base")
        print("'tenant sequence' probablemente es un objeto SEQUENCE de SQL Server")
        print("(CREATE SEQUENCE), usado para generar números o códigos de tenant.\n")
        cur.execute(
            """
            SELECT SCHEMA_NAME(schema_id) AS esquema, name,
                   CAST(current_value AS varchar(40)) AS valor_actual,
                   CAST(start_value  AS varchar(40)) AS valor_inicial,
                   CAST(increment    AS varchar(40)) AS incremento
            FROM sys.sequences
            ORDER BY esquema, name
            """
        )
        filas = cur.fetchall()
        if filas:
            for f in filas:
                print(f"  [{f[0]}].[{f[1]}]  actual={f[2]} inicial={f[3]} incremento={f[4]}")
        else:
            print("  (ninguna) — no hay ningún objeto SEQUENCE en toda la base.")
            print("  Si el SP espera una, hay que pedirle a Charlie que la cree.")

        titulo("3 · Todas las tablas de [security]")
        print("Para ubicar dónde vive la configuración que el SP busca.\n")
        cur.execute(
            """
            SELECT t.name,
                   (SELECT COUNT(*) FROM sys.columns c WHERE c.object_id = t.object_id) AS cols,
                   ISNULL(p.rows, 0) AS filas
            FROM sys.tables t
            LEFT JOIN sys.partitions p
                   ON p.object_id = t.object_id AND p.index_id IN (0, 1)
            WHERE SCHEMA_NAME(t.schema_id) = 'security'
            ORDER BY t.name
            """
        )
        for f in cur.fetchall():
            print(f"  [security].[{f[0]}]  —  {f[1]} columnas, {f[2]} filas")

        titulo("4 · Tablas que suenan a configuración, en CUALQUIER esquema")
        cur.execute(
            """
            SELECT SCHEMA_NAME(t.schema_id) AS esquema, t.name,
                   ISNULL(p.rows, 0) AS filas
            FROM sys.tables t
            LEFT JOIN sys.partitions p
                   ON p.object_id = t.object_id AND p.index_id IN (0, 1)
            WHERE t.name LIKE '%onfig%' OR t.name LIKE '%equence%'
               OR t.name LIKE '%etting%' OR t.name LIKE '%arameter%'
               OR t.name LIKE '%Status%'
            ORDER BY esquema, t.name
            """
        )
        filas = cur.fetchall()
        if filas:
            for f in filas:
                print(f"  [{f[0]}].[{f[1]}]  —  {f[2]} filas")
        else:
            print("  (ninguna)")

        titulo("5 · Restricciones CHECK — de aquí sale el vocabulario de status")
        print("El paso 0 de la prueba anterior no encontró catálogo de estados;")
        print("puede estar como CHECK sobre la columna en vez de tabla aparte.\n")
        cur.execute(
            """
            SELECT SCHEMA_NAME(t.schema_id) AS esquema, t.name AS tabla,
                   cc.name AS restriccion, cc.definition
            FROM sys.check_constraints cc
            JOIN sys.tables t ON t.object_id = cc.parent_object_id
            WHERE SCHEMA_NAME(t.schema_id) = 'security'
               OR cc.definition LIKE '%tatus%'
            ORDER BY esquema, tabla, restriccion
            """
        )
        filas = cur.fetchall()
        if filas:
            for f in filas:
                print(f"  [{f[0]}].[{f[1]}] · {f[2]}")
                print(f"      {f[3]}")
        else:
            print("  (ninguna)")

        titulo("6 · El mensaje de error, tal como lo tiene registrado SQL Server")
        cur.execute(
            "SELECT message_id, severity, text FROM sys.messages "
            "WHERE message_id >= 50000 AND language_id = 1033 ORDER BY message_id"
        )
        filas = cur.fetchall()
        if filas:
            for f in filas:
                print(f"  {f[0]} (sev {f[1]}): {f[2]}")
        else:
            print("  (ninguno) — el mensaje 50006 se levanta con RAISERROR inline")
            print("  dentro del SP, no está registrado en sys.messages.")

        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
