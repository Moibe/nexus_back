"""¿El generador de la línea base (`migraciones/linea_base.py`) escribe el SQL correcto a partir del catálogo?

    .venv/Scripts/python verificar_linea_base.py

OFFLINE: no toca ninguna base. `generar(catalogo)` es una función pura, así que se le da un catálogo hecho a mano con todos los casos que tiene el esquema real
(identity, valores por omisión, CHECK, llave primaria, restricción única, índice filtrado con columnas incluidas y descendentes, llave foránea con acciones, filas
sembradas con comillas, nulos, fechas, bits, decimales y bytes, módulos con SET distintos) y se comprueba el texto. También se comprueba el archivo REAL
`0001_linea_base.sql` que está en el repositorio: que cada lote sea válido para partirse, que los módulos vayan DESPUÉS de las tablas y las funciones antes que los
procedimientos, y que no haya nada de usuarios ni permisos. Que ese archivo se pueda EJECUTAR y reproduzca la base lo prueba `verificar_migraciones_sql.py` (Docker).
"""

import re
from datetime import date, datetime
from decimal import Decimal

from migraciones.corredor import descubrir, partir_en_lotes
from migraciones.linea_base import generar, literal, q, tipo_sql

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + extra if extra and not ok else ''}")
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


def col(tabla, nombre, tipo, nulo=False, largo=0, precision=0, escala=0, identity=None, df=None):
    return {"tabla": tabla, "nombre": nombre, "tipo": tipo, "largo": largo, "precision": precision, "escala": escala, "nulo": nulo, "identity": identity, "df_nombre": df[0] if df else None, "df_def": df[1] if df else None}


titulo("1 · Nombres, tipos y literales")
rev("los nombres van entre corchetes y el corchete de cierre se duplica", q("tenants") == "[tenants]" and q("a]b") == "[a]]b]")
rev("varchar con largo, y MAX cuando el largo es -1", tipo_sql(col("t", "c", "varchar", largo=120)) == "varchar(120)" and tipo_sql(col("t", "c", "varchar", largo=-1)) == "varchar(MAX)")
rev("nvarchar: el catálogo da BYTES, el SQL pide caracteres (200 bytes = 100)", tipo_sql(col("t", "c", "nvarchar", largo=200)) == "nvarchar(100)" and tipo_sql(col("t", "c", "nvarchar", largo=-1)) == "nvarchar(MAX)")
rev("decimal con precisión y escala; datetime2 con su escala", tipo_sql(col("t", "c", "decimal", precision=9, escala=4)) == "decimal(9,4)" and tipo_sql(col("t", "c", "datetime2", escala=7)) == "datetime2(7)" and tipo_sql(col("t", "c", "datetime2", escala=3)) == "datetime2(3)")
rev("los tipos simples quedan como son", all(tipo_sql(col("t", "c", x)) == x for x in ("int", "bigint", "smallint", "bit", "uniqueidentifier", "datetime")))
try:
    tipo_sql(col("t", "c", "geography"))
    rev("un tipo que no conoce: se niega en vez de adivinar", False)
except ValueError as exc:
    rev("un tipo que no conoce: se niega en vez de adivinar, y dice cuál columna", "geography" in str(exc) and "t.c" in str(exc))
rev("NULL, bit, entero y decimal", literal(None, "int") == "NULL" and literal(True, "bit") == "1" and literal(False, "bit") == "0" and literal(7, "int") == "7" and literal(Decimal("1.50"), "decimal") == "1.50")
rev("cadenas con comilla: se duplica la comilla y van como unicode", literal("O'Brien", "varchar") == "N'O''Brien'" and literal("PROCHURGRUPOCSI\\carlos", "varchar") == "N'PROCHURGRUPOCSI\\carlos'")
rev("una fecha que llega como TEXTO conserva todos sus decimales", literal("2026-08-31 11:00:15.6497287", "datetime2") == "CONVERT(datetime2(7), '2026-08-31 11:00:15.6497287', 121)")
rev("datetime y date, también desde texto", literal("2026-08-31 11:00:15.123", "datetime") == "CONVERT(datetime, '2026-08-31 11:00:15.123', 121)" and literal("2026-08-31", "date") == "CONVERT(date, '2026-08-31', 23)")
rev("un datetime de Python (sin texto) completa a 7 decimales", literal(datetime(2026, 1, 2, 3, 4, 5, 120000), "datetime2") == "CONVERT(datetime2(7), '2026-01-02 03:04:05.1200000', 121)")
rev("un date de Python y bytes", literal(date(2026, 1, 2), "date") == "CONVERT(date, '2026-01-02', 23)" and literal(b"\x01\xab", "varbinary") == "0x01ab")

titulo("2 · El catálogo de mentira, con todos los casos")
T = "security.cosas"
catalogo = {
    "tablas": [
        {
            "id": 1, "esquema": "reference", "nombre": "estados",
            "columnas": [col("reference.estados", "estadoId", "int", identity=(1, 1)), col("reference.estados", "code", "varchar", largo=20), col("reference.estados", "createdAt", "datetime2", escala=7, df=("DF_estados_createdAt", "(sysdatetime())")), col("reference.estados", "nota", "nvarchar", nulo=True, largo=200)],
            "indices": [
                {"nombre": "PK_estados", "tipo": "CLUSTERED", "unico": True, "pk": True, "uq": False, "filtro": None, "claves": [("estadoId", False)], "incluidas": []},
                {"nombre": "UQ_estados_code", "tipo": "NONCLUSTERED", "unico": True, "pk": False, "uq": True, "filtro": None, "claves": [("code", False)], "incluidas": []},
            ],
            "checks": [("CK_estados_code", "([code]='A' OR [code]='B')")], "fks": [],
            "semilla": {"columnas": ["estadoId", "code", "createdAt", "nota"], "filas": [(1, "A", "2026-08-31 11:00:15.6497287", None), (2, "B", "2026-08-31 11:00:15.6497287", "con 'comillas'")]},
        },
        {
            "id": 2, "esquema": "security", "nombre": "cosas",
            "columnas": [col(T, "cosaId", "bigint", identity=(5, 2)), col(T, "estadoId", "int"), col(T, "cuando", "datetime2", escala=7), col(T, "activo", "bit", df=("DF_cosas_activo", "((1))")), col(T, "monto", "decimal", nulo=True, precision=9, escala=2)],
            "indices": [
                {"nombre": "PK_cosas", "tipo": "CLUSTERED", "unico": True, "pk": True, "uq": False, "filtro": None, "claves": [("cosaId", False)], "incluidas": []},
                {"nombre": "IX_cosas_estado_cuando", "tipo": "NONCLUSTERED", "unico": False, "pk": False, "uq": False, "filtro": "([activo]=(1))", "claves": [("estadoId", False), ("cuando", True)], "incluidas": [("monto", False)]},
                {"nombre": "UX_cosas_unica", "tipo": "NONCLUSTERED", "unico": True, "pk": False, "uq": False, "filtro": None, "claves": [("cuando", False)], "incluidas": []},
            ],
            "checks": [("CK_cosas_monto", "([monto] IS NULL OR [monto]>=(0))")],
            "fks": [{"nombre": "FK_cosas_estados", "ref_esquema": "reference", "ref_tabla": "estados", "borrar": "CASCADE", "actualizar": "NO_ACTION", "cols": [("estadoId", "estadoId")]}],
        },
    ],
    "modulos": [
        {"tipo": "P", "esquema": "security", "nombre": "uspUno", "definicion": "CREATE PROCEDURE security.uspUno AS SELECT 1", "ansi_nulls": True, "quoted": True},
        {"tipo": "IF", "esquema": "security", "nombre": "ufnUno", "definicion": "\r\nCREATE FUNCTION security.ufnUno() RETURNS TABLE AS RETURN SELECT 1 AS x\r\n", "ansi_nulls": False, "quoted": False},
    ],
}
texto = generar(catalogo)
rev("es determinista: el mismo catálogo da el mismo texto", generar(catalogo) == texto)
rev("no trae saltos de Windows", "\r" not in texto)
rev("empieza con la guarda de esquemas y la de base ya construida", "THROW 51001" in texto and "THROW 51002" in texto and texto.index("THROW 51001") < texto.index("CREATE TABLE"))
rev("la columna identity con su semilla y salto, y NOT NULL", "[cosaId] bigint IDENTITY(5,2) NOT NULL" in texto)
rev("el valor por omisión con el nombre que le dio Charlie", "[activo] bit NOT NULL CONSTRAINT [DF_cosas_activo] DEFAULT ((1))" in texto and "CONSTRAINT [DF_estados_createdAt] DEFAULT (sysdatetime())" in texto)
rev("las columnas que aceptan nulos dicen NULL", "[nota] nvarchar(100) NULL" in texto and "[monto] decimal(9,2) NULL" in texto)
rev("llave primaria CLUSTERED y restricción única, dentro de la tabla", "CONSTRAINT [PK_estados] PRIMARY KEY CLUSTERED ([estadoId])" in texto and "CONSTRAINT [UQ_estados_code] UNIQUE NONCLUSTERED ([code])" in texto)
rev("el CHECK, tal cual", "CONSTRAINT [CK_cosas_monto] CHECK ([monto] IS NULL OR [monto]>=(0))" in texto)
rev("el índice filtrado, con DESC, INCLUDE y WHERE, FUERA de la tabla",
    "CREATE NONCLUSTERED INDEX [IX_cosas_estado_cuando] ON [security].[cosas] ([estadoId], [cuando] DESC) INCLUDE ([monto]) WHERE ([activo]=(1));" in texto)
rev("un índice único que NO es restricción sale como CREATE UNIQUE INDEX", "CREATE UNIQUE NONCLUSTERED INDEX [UX_cosas_unica] ON [security].[cosas] ([cuando]);" in texto)
rev("las restricciones únicas NO se repiten como índices", "CREATE UNIQUE NONCLUSTERED INDEX [UQ_estados_code]" not in texto and "CREATE NONCLUSTERED INDEX [PK_" not in texto)
rev("la llave foránea con su acción, y solo la que no es la normal", "ADD CONSTRAINT [FK_cosas_estados] FOREIGN KEY ([estadoId]) REFERENCES [reference].[estados] ([estadoId]) ON DELETE CASCADE;" in texto and "ON UPDATE" not in texto)
rev("las filas sembradas, con identity insert, comillas duplicadas y NULL",
    "SET IDENTITY_INSERT [reference].[estados] ON;" in texto and "N'con ''comillas'''" in texto and ", NULL);" in texto and "SET IDENTITY_INSERT [reference].[estados] OFF;" in texto)
rev("una tabla sin filas sembradas no genera INSERT", "INSERT INTO [security].[cosas]" not in texto)
rev("cada módulo lleva sus SET antes, en su propio lote", "SET ANSI_NULLS ON\nSET QUOTED_IDENTIFIER ON\nGO\nCREATE PROCEDURE security.uspUno" in texto and "SET ANSI_NULLS OFF\nSET QUOTED_IDENTIFIER OFF\nGO\nCREATE FUNCTION security.ufnUno()" in texto)
rev("la definición del módulo va recortada (sin espacios ni saltos de más)", "RETURN SELECT 1 AS x\nGO\n" in texto)
rev("orden: tablas, índices, llaves foráneas, filas, módulos", texto.index("CREATE TABLE") < texto.index("CREATE NONCLUSTERED INDEX") < texto.index("ADD CONSTRAINT [FK_") < texto.index("INSERT INTO") < texto.index("CREATE PROCEDURE"))
lotes = partir_en_lotes(texto)
rev("el texto se parte en lotes y ninguno está vacío", len(lotes) > 8 and all(l.strip() for l in lotes))
rev("cada CREATE PROCEDURE / FUNCTION es lo PRIMERO de su lote (SQL Server lo exige)", all(re.match(r"(--.*\n|\s)*CREATE (PROCEDURE|FUNCTION)", l) for l in lotes if re.search(r"^CREATE (PROCEDURE|FUNCTION)", l, re.MULTILINE)))

titulo("3 · El archivo real `0001_linea_base.sql` del repositorio")
real = descubrir()[0]
rev("se llama 0001_linea_base y es la primera migración", real.version == 1 and real.nombre == "linea_base")
sql = real.sql
rev("trae las 36 tablas", len(re.findall(r"^CREATE TABLE \[", sql, re.MULTILINE)) == 36)
rev("trae 36 módulos: 4 funciones y 32 procedimientos", len(re.findall(r"^CREATE\s+(?:OR\s+ALTER\s+)?PROCEDURE", sql, re.MULTILINE | re.IGNORECASE)) == 32 and len(re.findall(r"^CREATE\s+(?:OR\s+ALTER\s+)?FUNCTION", sql, re.MULTILINE | re.IGNORECASE)) == 4)
rev("trae las llaves foráneas (48) y los 62 CHECK", len(re.findall(r"ADD CONSTRAINT \[FK_", sql)) == 48 and len(re.findall(r"CONSTRAINT \[CK_", sql)) == 62)
primera_funcion = re.search(r"^CREATE\s+(?:OR\s+ALTER\s+)?FUNCTION", sql, re.MULTILINE | re.IGNORECASE).start()
primer_procedimiento = re.search(r"^CREATE\s+(?:OR\s+ALTER\s+)?PROCEDURE", sql, re.MULTILINE | re.IGNORECASE).start()
ultima_funcion = [m.start() for m in re.finditer(r"^CREATE\s+(?:OR\s+ALTER\s+)?FUNCTION", sql, re.MULTILINE | re.IGNORECASE)][-1]
rev("las funciones van ANTES que los procedimientos (los procedimientos las usan)", ultima_funcion < primer_procedimiento)
rev("todos los módulos van después de las tablas y de los datos", sql.rfind("CREATE TABLE") < sql.find("INSERT INTO") < min(primera_funcion, primer_procedimiento))
rev("NO crea esquemas, usuarios, roles ni permisos (eso es del DBA)", not re.search(r"^\s*(CREATE (SCHEMA|USER|ROLE|LOGIN)|GRANT|DENY|ALTER ROLE|ALTER AUTHORIZATION)\b", sql, re.MULTILINE | re.IGNORECASE))
rev("no tiene saltos de Windows", "\r" not in sql)
rev("no lleva contraseñas ni credenciales (una contraseña con valor literal, PWD= o UID=)", not re.search(r"(password|pwd)\s*=\s*N?'|PWD=|UID=", sql, re.IGNORECASE))
rev("la tabla de control NO es parte de la línea base (la crea el corredor)", "schemaMigrations" not in sql)
rev("siembra la secuencia de organizaciones en CERO", re.search(r"INSERT INTO \[security\]\.\[tenantSequence\] .*VALUES \(1, N'NEX', 0, ", sql) is not None)
rev("cada lote es no vacío y se parte sin sorpresas", all(l.strip() for l in real.lotes) and len(real.lotes) > 100)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
