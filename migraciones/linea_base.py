"""Genera la LÍNEA BASE (`0001_linea_base.sql`) leyendo el catálogo de una base de Nexus.

    python -m migraciones.linea_base --base IA_Nexus_Dev                 (imprime un resumen; no escribe nada)
    python -m migraciones.linea_base --base IA_Nexus_Dev --salida migraciones/0001_linea_base.sql
    python -m migraciones.linea_base --base IA_Nexus_Dev --comparar migraciones/0001_linea_base.sql     (¿la base sigue siendo igual al archivo?)

La línea base describe el esquema que Charlie construyó ANTES de que existiera el corredor: 36 tablas (con sus llaves, restricciones, índices y valores por
omisión), los catálogos de `reference` y `security.roles` con sus filas, la fila de `security.tenantSequence` (en su estado inicial: contador en 0), las 4 funciones y los 32 stored
procedures congelados. Sirve para dos cosas: levantar una base VACÍA desde cero (pruebas locales, una base nueva) y dejar el punto de partida por escrito.
En las tres bases que ya tienen ese esquema NO se ejecuta: se MARCA (`python -m migraciones marcar --version 1`).

Lo que NO incluye, a propósito: los esquemas y el usuario `nexusOwner` (los crea el DBA: `usrNexusDDL` no puede), y los usuarios y permisos. El archivo
empieza con una guarda que se niega a correr si faltan los esquemas o si la base ya tiene el esquema.

## Cómo está armado

`leer_catalogo(cursor)` solo lee (`sys.*`) y devuelve un diccionario; `generar(catalogo)` es PURA (mismo catálogo → mismo texto) y es lo que prueba
`verificar_linea_base.py`. Como el texto sale solo del catálogo, la prueba más fuerte es un círculo: aplicar el archivo a una base vacía, volver a leer
su catálogo y generar otra vez; los dos textos tienen que ser idénticos (`--comparar` hace esa comparación contra una base).
"""

import argparse
import sys
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

ESQUEMAS = ("documents", "security", "reference")
# Catálogos y configuración que se siembran con sus filas (el resto de las tablas nace vacío).
CON_FILAS = lambda esquema, tabla: esquema == "reference" or (esquema, tabla) in {("security", "roles"), ("security", "tenantSequence")}  # noqa: E731
TIPOS_SIMPLES = {"int", "bigint", "smallint", "tinyint", "bit", "uniqueidentifier", "datetime", "date", "float", "real", "money", "text", "ntext", "xml", "varbinary_max"}


def q(nombre: str) -> str:
    return "[" + nombre.replace("]", "]]") + "]"


def tipo_sql(c: dict) -> str:
    t = c["tipo"]
    if t in ("varchar", "char", "varbinary", "binary"):
        return f"{t}({'MAX' if c['largo'] == -1 else c['largo']})"
    if t in ("nvarchar", "nchar"):
        return f"{t}({'MAX' if c['largo'] == -1 else c['largo'] // 2})"
    if t in ("decimal", "numeric"):
        return f"{t}({c['precision']},{c['escala']})"
    if t in ("datetime2", "time", "datetimeoffset"):
        return f"{t}({c['escala']})"
    if t in TIPOS_SIMPLES:
        return t
    raise ValueError(f"Tipo de columna que el generador no conoce: {t} ({c['tabla']}.{c['nombre']}). Agrégalo a `tipo_sql` con su prueba.")


def literal(valor, tipo: str) -> str:
    """Un valor de una fila sembrada, como literal T-SQL que reproduce exactamente el valor."""
    if valor is None:
        return "NULL"
    # Las fechas de las filas sembradas llegan como TEXTO (ver `leer_catalogo`): así no se pierde el 7.º decimal de datetime2.
    if isinstance(valor, str) and tipo == "datetime2":
        return f"CONVERT(datetime2(7), '{valor}', 121)"
    if isinstance(valor, str) and tipo == "datetime":
        return f"CONVERT(datetime, '{valor}', 121)"
    if isinstance(valor, str) and tipo == "date":
        return f"CONVERT(date, '{valor}', 23)"
    if isinstance(valor, bool):
        return "1" if valor else "0"
    if isinstance(valor, int):
        return str(valor)
    if isinstance(valor, Decimal):
        return format(valor, "f")
    if isinstance(valor, datetime):
        texto = valor.strftime("%Y-%m-%d %H:%M:%S") + f".{valor.microsecond:06d}0"
        return f"CONVERT(datetime2(7), '{texto}', 121)" if tipo == "datetime2" else f"CONVERT({tipo}, '{valor.strftime('%Y-%m-%d %H:%M:%S.') + format(valor.microsecond // 1000, '03d')}', 121)"
    if isinstance(valor, date):
        return f"CONVERT(date, '{valor.isoformat()}', 23)"
    if isinstance(valor, (bytes, bytearray)):
        return "0x" + bytes(valor).hex()
    if isinstance(valor, float):
        return repr(valor)
    return "N'" + str(valor).replace("'", "''") + "'"


def leer_catalogo(cur) -> dict:
    """Todo lo que `generar` necesita, leído del catálogo de la base a la que está conectado el cursor. Solo SELECT."""
    # Todo se ordena por NOMBRE, nunca por número de índice o de objeto: esos números los asigna SQL Server a su manera y cambian al reconstruir la base,
    # y entonces el círculo (leer → generar → aplicar → leer → generar) no cerraría aunque el esquema fuera el mismo.
    cur.execute(f"SELECT t.object_id, s.name, t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE s.name IN ({','.join('?' * len(ESQUEMAS))}) ORDER BY s.name, t.name", *ESQUEMAS)
    tablas = {r[0]: {"id": r[0], "esquema": r[1], "nombre": r[2], "columnas": [], "indices": [], "checks": [], "fks": []} for r in cur.fetchall()}
    if not tablas:
        raise ValueError("La base no tiene tablas en documents, security ni reference.")
    ids = ",".join(str(i) for i in tablas)

    cur.execute(
        f"""SELECT c.object_id, c.name, ty.name, c.max_length, c.precision, c.scale, c.is_nullable, CONVERT(bigint, ic.seed_value), CONVERT(bigint, ic.increment_value), dc.name, dc.definition
            FROM sys.columns c JOIN sys.types ty ON ty.user_type_id = c.user_type_id
            LEFT JOIN sys.identity_columns ic ON ic.object_id = c.object_id AND ic.column_id = c.column_id
            LEFT JOIN sys.default_constraints dc ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
            WHERE c.object_id IN ({ids}) ORDER BY c.object_id, c.column_id"""
    )
    for r in cur.fetchall():
        t = tablas[r[0]]
        t["columnas"].append(
            {"tabla": f"{t['esquema']}.{t['nombre']}", "nombre": r[1], "tipo": r[2], "largo": r[3], "precision": r[4], "escala": r[5], "nulo": bool(r[6]),
             "identity": None if r[7] is None else (int(r[7]), int(r[8])), "df_nombre": r[9], "df_def": r[10]}
        )

    cur.execute(
        f"""SELECT i.object_id, i.index_id, i.name, i.type_desc, i.is_unique, i.is_primary_key, i.is_unique_constraint, i.filter_definition
            FROM sys.indexes i WHERE i.object_id IN ({ids}) AND i.index_id > 0
            ORDER BY i.object_id, CASE WHEN i.is_primary_key = 1 THEN 0 ELSE 1 END, i.name"""
    )
    por_indice = {}
    for r in cur.fetchall():
        d = {"nombre": r[2], "tipo": r[3], "unico": bool(r[4]), "pk": bool(r[5]), "uq": bool(r[6]), "filtro": r[7], "claves": [], "incluidas": []}
        tablas[r[0]]["indices"].append(d)
        por_indice[(r[0], r[1])] = d
    cur.execute(
        f"""SELECT ic.object_id, ic.index_id, c.name, ic.is_descending_key, ic.is_included_column
            FROM sys.index_columns ic JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE ic.object_id IN ({ids}) AND ic.index_id > 0 ORDER BY ic.object_id, ic.index_id, ic.is_included_column, ic.key_ordinal, ic.index_column_id"""
    )
    for r in cur.fetchall():
        d = por_indice[(r[0], r[1])]
        (d["incluidas"] if r[4] else d["claves"]).append((r[2], bool(r[3])))

    cur.execute(f"SELECT parent_object_id, name, definition, is_disabled FROM sys.check_constraints WHERE parent_object_id IN ({ids}) ORDER BY parent_object_id, name")
    for r in cur.fetchall():
        if r[3]:
            raise ValueError(f"La restricción {r[1]} está deshabilitada: el generador no sabe reproducir eso.")
        tablas[r[0]]["checks"].append((r[1], r[2]))

    cur.execute(
        f"""SELECT fk.parent_object_id, fk.object_id, fk.name, SCHEMA_NAME(rt.schema_id), rt.name, fk.delete_referential_action_desc, fk.update_referential_action_desc, fk.is_disabled
            FROM sys.foreign_keys fk JOIN sys.tables rt ON rt.object_id = fk.referenced_object_id WHERE fk.parent_object_id IN ({ids}) ORDER BY fk.parent_object_id, fk.name"""
    )
    fks = {}
    for r in cur.fetchall():
        if r[7]:
            raise ValueError(f"La llave foránea {r[2]} está deshabilitada: el generador no sabe reproducir eso.")
        d = {"nombre": r[2], "ref_esquema": r[3], "ref_tabla": r[4], "borrar": r[5], "actualizar": r[6], "cols": []}
        tablas[r[0]]["fks"].append(d)
        fks[r[1]] = d
    cur.execute(
        f"""SELECT fkc.constraint_object_id, pc.name, rc.name FROM sys.foreign_key_columns fkc
            JOIN sys.columns pc ON pc.object_id = fkc.parent_object_id AND pc.column_id = fkc.parent_column_id
            JOIN sys.columns rc ON rc.object_id = fkc.referenced_object_id AND rc.column_id = fkc.referenced_column_id
            WHERE fkc.parent_object_id IN ({ids}) ORDER BY fkc.constraint_object_id, fkc.constraint_column_id"""
    )
    for r in cur.fetchall():
        fks[r[0]]["cols"].append((r[1], r[2]))

    for t in tablas.values():
        if CON_FILAS(t["esquema"], t["nombre"]):
            expresiones = []
            for c in t["columnas"]:
                n = q(c["nombre"])
                if c["tipo"] == "datetime2":
                    expresiones.append(f"CONVERT(varchar(27), {n}, 121)")
                elif c["tipo"] == "datetime":
                    expresiones.append(f"CONVERT(varchar(23), {n}, 121)")
                elif c["tipo"] == "date":
                    expresiones.append(f"CONVERT(varchar(10), {n}, 23)")
                else:
                    expresiones.append(n)
            cur.execute(f"SELECT {', '.join(expresiones)} FROM {q(t['esquema'])}.{q(t['nombre'])}")
            nombres = [c["nombre"] for c in t["columnas"]]
            filas = [tuple(f) for f in cur.fetchall()]
            if (t["esquema"], t["nombre"]) == ("security", "tenantSequence"):
                # El contador de organizaciones y quién/cuándo lo tocó son ESTADO, no esquema: cambian con el uso. La línea base siembra la fila en su estado
                # INICIAL (contador en 0, sin actualizar); así dos bases con el mismo esquema se describen igual aunque una ya haya creado organizaciones.
                inicial = {"lastSequence": 0, "updatedAt": None, "updatedBy": None}
                filas = [tuple(inicial.get(n, v) for n, v in zip(nombres, f)) for f in filas]
            pk = next((i for i in t["indices"] if i["pk"]), None)
            orden = [n for n, _ in pk["claves"]] if pk else nombres[:1]
            filas.sort(key=lambda f: tuple(f[nombres.index(n)] for n in orden))
            t["semilla"] = {"columnas": nombres, "filas": filas}

    cur.execute(
        """SELECT o.type, s.name, o.name, m.definition, m.uses_ansi_nulls, m.uses_quoted_identifier FROM sys.sql_modules m
           JOIN sys.objects o ON o.object_id = m.object_id JOIN sys.schemas s ON s.schema_id = o.schema_id
           WHERE o.is_ms_shipped = 0 AND s.name IN ('documents', 'security', 'reference') AND o.type IN ('FN', 'IF', 'TF', 'V', 'P')
           ORDER BY CASE WHEN o.type IN ('FN', 'IF', 'TF') THEN 0 WHEN o.type = 'V' THEN 1 ELSE 2 END, s.name, o.name"""
    )
    modulos = []
    for r in cur.fetchall():
        if r[3] is None:
            raise ValueError(f"No puedo leer la definición de {r[1]}.{r[2]} (falta VIEW DEFINITION).")
        modulos.append({"tipo": r[0], "esquema": r[1], "nombre": r[2], "definicion": r[3], "ansi_nulls": bool(r[4]), "quoted": bool(r[5])})
    return {"tablas": list(tablas.values()), "modulos": modulos}


def _columna(c: dict) -> str:
    partes = [f"    {q(c['nombre'])} {tipo_sql(c)}"]
    if c["identity"]:
        partes.append(f"IDENTITY({c['identity'][0]},{c['identity'][1]})")
    partes.append("NULL" if c["nulo"] else "NOT NULL")
    if c["df_nombre"]:
        partes.append(f"CONSTRAINT {q(c['df_nombre'])} DEFAULT {c['df_def']}")
    return " ".join(partes)


def _lista(cols: list[tuple[str, bool]]) -> str:
    return ", ".join(q(n) + (" DESC" if desc else "") for n, desc in cols)


def generar(cat: dict) -> str:
    """El texto de `0001_linea_base.sql`. Pura: el mismo catálogo da siempre el mismo texto."""
    tablas = cat["tablas"]
    n_mod = len(cat["modulos"])
    s: list[str] = []
    s.append(
        f"-- 0001 · Línea base: el esquema de Nexus como estaba al llegar el corredor de migraciones.\n"
        f"--\n"
        f"-- GENERADO por `python -m migraciones.linea_base` a partir del catálogo de una base de Nexus: {len(tablas)} tablas, {n_mod} funciones y procedimientos\n"
        f"-- (congelados: son la especificación del dominio, ver la decisión del 2026-10-21), los catálogos de `reference` con sus filas, `security.roles` y\n"
        f"-- la fila de `security.tenantSequence`. NO se edita a mano: si el esquema cambia, se agrega una migración nueva.\n"
        f"--\n"
        f"-- En una base que YA tiene este esquema no se ejecuta: se MARCA (`python -m migraciones marcar --version 1`). Solo corre en una base vacía.\n"
        f"-- No crea los esquemas ni `nexusOwner` ni los usuarios y permisos: eso es del DBA.\n"
    )
    s.append(
        "IF SCHEMA_ID('documents') IS NULL OR SCHEMA_ID('security') IS NULL OR SCHEMA_ID('reference') IS NULL OR SCHEMA_ID('nexus') IS NULL\n"
        "    THROW 51001, 'Faltan los esquemas documents, security, reference o nexus: los crea el DBA (usrNexusDDL no puede crear esquemas).', 1;\n"
        "IF OBJECT_ID('security.tenants', 'U') IS NOT NULL\n"
        "    THROW 51002, 'Esta base ya tiene el esquema de la línea base: se MARCA (python -m migraciones marcar --version 1), no se ejecuta.', 1;\n"
        "GO\n"
    )
    s.append("-- ── Tablas, con sus llaves primarias, restricciones únicas, CHECK y valores por omisión ──\n")
    for t in tablas:
        cuerpo = [_columna(c) for c in t["columnas"]]
        for ix in t["indices"]:
            if ix["pk"]:
                cuerpo.append(f"    CONSTRAINT {q(ix['nombre'])} PRIMARY KEY {'CLUSTERED' if ix['tipo'] == 'CLUSTERED' else 'NONCLUSTERED'} ({_lista(ix['claves'])})")
            elif ix["uq"]:
                cuerpo.append(f"    CONSTRAINT {q(ix['nombre'])} UNIQUE {'CLUSTERED' if ix['tipo'] == 'CLUSTERED' else 'NONCLUSTERED'} ({_lista(ix['claves'])})")
        for nombre, definicion in t["checks"]:
            cuerpo.append(f"    CONSTRAINT {q(nombre)} CHECK {definicion}")
        s.append(f"CREATE TABLE {q(t['esquema'])}.{q(t['nombre'])} (\n" + ",\n".join(cuerpo) + "\n);\nGO\n")
    s.append("-- ── Índices ──\n")
    for t in tablas:
        for ix in t["indices"]:
            if ix["pk"] or ix["uq"]:
                continue
            linea = f"CREATE {'UNIQUE ' if ix['unico'] else ''}{'CLUSTERED' if ix['tipo'] == 'CLUSTERED' else 'NONCLUSTERED'} INDEX {q(ix['nombre'])} ON {q(t['esquema'])}.{q(t['nombre'])} ({_lista(ix['claves'])})"
            if ix["incluidas"]:
                linea += f" INCLUDE ({_lista(ix['incluidas'])})"
            if ix["filtro"]:
                linea += f" WHERE {ix['filtro']}"
            s.append(linea + ";\nGO\n")
    s.append("-- ── Llaves foráneas ──\n")
    for t in tablas:
        for fk in t["fks"]:
            acciones = "".join(
                f" ON {que} {a.replace('_', ' ')}" for que, a in (("DELETE", fk["borrar"]), ("UPDATE", fk["actualizar"])) if a != "NO_ACTION"
            )
            s.append(
                f"ALTER TABLE {q(t['esquema'])}.{q(t['nombre'])} ADD CONSTRAINT {q(fk['nombre'])} FOREIGN KEY ({', '.join(q(a) for a, _ in fk['cols'])}) "
                f"REFERENCES {q(fk['ref_esquema'])}.{q(fk['ref_tabla'])} ({', '.join(q(b) for _, b in fk['cols'])}){acciones};\nGO\n"
            )
    s.append("-- ── Catálogos y configuración (con sus ids: otras tablas los referencian) ──\n")
    for t in tablas:
        sem = t.get("semilla")
        if not sem or not sem["filas"]:
            continue
        tipos = {c["nombre"]: c["tipo"] for c in t["columnas"]}
        tiene_identity = any(c["identity"] for c in t["columnas"])
        destino = f"{q(t['esquema'])}.{q(t['nombre'])}"
        lineas = []
        if tiene_identity:
            lineas.append(f"SET IDENTITY_INSERT {destino} ON;")
        columnas = ", ".join(q(n) for n in sem["columnas"])
        for fila in sem["filas"]:
            lineas.append(f"INSERT INTO {destino} ({columnas}) VALUES ({', '.join(literal(v, tipos[n]) for n, v in zip(sem['columnas'], fila))});")
        if tiene_identity:
            lineas.append(f"SET IDENTITY_INSERT {destino} OFF;")
        s.append("\n".join(lineas) + "\nGO\n")
    s.append("-- ── Funciones y procedimientos (congelados: la especificación del dominio) ──\n")
    for m in cat["modulos"]:
        s.append(f"SET ANSI_NULLS {'ON' if m['ansi_nulls'] else 'OFF'}\nSET QUOTED_IDENTIFIER {'ON' if m['quoted'] else 'OFF'}\nGO\n{m['definicion'].strip()}\nGO\n")
    return "\n".join(s).replace("\r\n", "\n").replace("\r", "\n")


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from migraciones.corredor import CADENA, BaseSql, ErrorMigracion, normalizar

    parser = argparse.ArgumentParser(prog="python -m migraciones.linea_base")
    parser.add_argument("--base", required=True, choices=list(CADENA))
    parser.add_argument("--salida", help="Escribe aquí el archivo.")
    parser.add_argument("--comparar", help="Compara la base contra este archivo y dice si es igual.")
    args = parser.parse_args(argv)
    try:
        base = BaseSql(args.base)
    except ErrorMigracion as exc:
        print(f"NO: {exc}", file=sys.stderr)
        return 2
    try:
        cur = base._con.cursor()
        cat = leer_catalogo(cur)
        base._con.rollback()
    finally:
        base.cerrar()
    texto = generar(cat)
    print(f"{args.base}: {len(cat['tablas'])} tablas, {sum(1 for t in cat['tablas'] if t.get('semilla') and t['semilla']['filas'])} con filas sembradas, {len(cat['modulos'])} módulos, {len(texto.splitlines())} líneas.")
    if args.salida:
        Path(args.salida).write_text(texto, encoding="utf-8", newline="\n")
        print("Escrito:", args.salida)
    if args.comparar:
        esperado = normalizar(Path(args.comparar).read_text(encoding="utf-8"))
        igual = normalizar(texto) == esperado
        print("IGUAL al archivo." if igual else "DIFIERE del archivo.")
        return 0 if igual else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
