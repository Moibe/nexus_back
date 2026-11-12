"""¿Separa la 0016 (en espera) las organizaciones, y el usuario de solo lectura no puede salirse? (`migraciones/en_espera/0016_consulta_por_organizacion.sql`)

    .venv/Scripts/python verificar_consulta_por_organizacion.py

Corre contra el SQL Server desechable de `pruebas_sql.py` (sin Docker se salta y lo dice). La 0016 espera en `migraciones/en_espera/` a que el
DBA cree el esquema `[consulta]` y el usuario `usrNexusLectura` (`docs/solicitudes-dba.md` §8). Esta prueba hace en el contenedor lo mismo que se
le pidió al DBA, con las mismas sentencias del ticket, y aplica la 0016 con el corredor de verdad y `usrNexusDDL`.

Lo que se prueba:
- ANTES de lo del DBA, la 0016 se detiene en su guarda (51016) sin dejar nada; con el esquema sin el GRANT ALTER, igual; con el esquema de
  OTRO dueño, se detiene en la segunda guarda (51017);
- con lo del DBA, se aplica, queda anotada y las vistas son de `nexusOwner` con las MISMAS columnas que las de `[nexus]`;
- las seis comprobaciones del ticket, conectado como `usrNexusLectura`: sin organización 0 renglones; con una, solo los suyos; no se puede
  cambiar (15664); `[nexus]` da 229; no escribe, no crea, no ejecuta; no entra a las otras dos bases;
- las salidas que anotamos de este lado: `OPENROWSET`, `EXECUTE AS`, nombres de tres partes, y una conexión nueva empieza sin organización.
"""

import json
import secrets
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pruebas_sql  # noqa: E402

BASE = pruebas_sql.preparar("nexus-consulta-prueba", 14342)  # aplica 0001…0015 (las de migraciones/)

import pyodbc  # noqa: E402

from migraciones import corredor  # noqa: E402

RAIZ = Path(__file__).parent
EN_ESPERA = RAIZ / "migraciones" / "en_espera" / "0016_consulta_por_organizacion.sql"
DB = BASE["base"]
OTRAS = ("IA_Nexus", "IA_Nexus_Staging")
LECTURA = "usrNexusLectura"
CLAVE_LECTURA = "Lec-" + secrets.token_urlsafe(12) + "-9!"

fallos = 0


def rev(condicion: bool, texto: str, detalle: str = "") -> None:
    global fallos
    print(("OK   " if condicion else "FALLA") + " " + texto + ("" if condicion or not detalle else f"\n      {detalle}"))
    if not condicion:
        fallos += 1


def error_de(cur, sql: str, *args) -> str:
    """El texto del error de SQL Server al ejecutar `sql` ('' si NO falló)."""
    try:
        cur.execute(sql, *args)
        while cur.nextset():
            pass
        return ""
    except pyodbc.Error as e:
        return str(e)


def numero(e: str, n: int) -> bool:
    """¿El error trae el número de SQL Server `n`? (pyodbc lo pone como `(n)` en el texto)."""
    return f"({n})" in e


sa = BASE["conectar"]("sa", BASE["sa"], DB)
k = sa.cursor()


def aplicar_0016() -> str:
    """Aplica lo de `migraciones/` + la 0016 con el corredor de verdad y usrNexusDDL. Devuelve el error ('' si se aplicó)."""
    carpeta = Path(tempfile.mkdtemp(prefix="consulta-0016-"))
    for ruta in (RAIZ / "migraciones").glob("*.sql"):
        shutil.copy(ruta, carpeta / ruta.name)
    shutil.copy(EN_ESPERA, carpeta / EN_ESPERA.name)
    claves = json.loads(pruebas_sql._CREDENCIALES.read_text(encoding="utf-8"))
    valores = {
        "SQLSERVER_HOST": "127.0.0.1", "SQLSERVER_PORT": str(BASE["puerto"]), "SQLSERVER_USER": "usrNexusDDL",
        "SQLSERVER_PASSWORD": claves["ddl"], "SQLSERVER_DRIVER": BASE["driver"],
    }
    base = corredor.BaseSql(DB, valores)
    try:
        corredor.ejecutar(base, corredor.descubrir(carpeta), accion="aplicar", confirmar=True)
        return ""
    except Exception as e:  # noqa: BLE001
        return str(e)
    finally:
        base.cerrar()
        shutil.rmtree(carpeta, ignore_errors=True)


def version_aplicada() -> int:
    return k.execute("SELECT max(version) FROM nexus.schemaMigrations").fetchone()[0]


def vistas_de_consulta() -> list[str]:
    return [f[0] for f in k.execute("SELECT name FROM sys.views WHERE schema_id = SCHEMA_ID('consulta') ORDER BY name").fetchall()]


print("\n[1] Antes de lo del DBA, la 0016 se detiene sin dejar nada")
rev(version_aplicada() == 15 and EN_ESPERA.parent.name == "en_espera", "la base de prueba llega con 0001…0015 (la 0016 no está en migraciones/)")
e = aplicar_0016()
rev("51016" in e, "sin el esquema [consulta]: se detiene en la guarda 51016", e[:300])
rev(version_aplicada() == 15, "…y no quedó anotada")
k.execute("CREATE SCHEMA consulta AUTHORIZATION nexusOwner")
e = aplicar_0016()
rev("51018" in e and "GRANT ALTER" in e, "con el esquema pero SIN el GRANT ALTER a usrNexusDDL: se detiene en la guarda 51018, que lo nombra", e[:300])
rev(version_aplicada() == 15 and vistas_de_consulta() == [], "…sin anotar nada ni crear vistas")
k.execute("DROP SCHEMA consulta")
k.execute("CREATE SCHEMA consulta AUTHORIZATION dbo")
k.execute("GRANT ALTER ON SCHEMA::consulta TO usrNexusDDL")
e = aplicar_0016()
rev("51017" in e, "con el esquema de OTRO dueño: se detiene en la guarda 51017", e[:300])
rev(version_aplicada() == 15 and vistas_de_consulta() == [], "…sin anotar nada ni crear vistas")
k.execute("DROP SCHEMA consulta")

print("\n[2] Lo que hace el DBA (las sentencias del ticket) y la 0016")
m = BASE["conectar"]("sa", BASE["sa"])
m.cursor().execute(
    f"IF SUSER_ID('{LECTURA}') IS NULL CREATE LOGIN {LECTURA} WITH PASSWORD = '{CLAVE_LECTURA}', CHECK_POLICY = ON "
    f"ELSE ALTER LOGIN {LECTURA} WITH PASSWORD = '{CLAVE_LECTURA}'"
)
for otra in OTRAS:  # las otras dos bases existen (vacías) para comprobar que el usuario no entra
    m.cursor().execute(f"IF DB_ID('{otra}') IS NULL CREATE DATABASE [{otra}] COLLATE Modern_Spanish_CI_AS")
k.execute("CREATE SCHEMA consulta AUTHORIZATION nexusOwner")
k.execute(f"CREATE USER {LECTURA} FOR LOGIN {LECTURA}")
k.execute(f"GRANT SELECT ON SCHEMA::consulta TO {LECTURA}")
k.execute("GRANT ALTER ON SCHEMA::consulta TO usrNexusDDL")
e = aplicar_0016()
rev(e == "", "con lo del DBA, la 0016 se aplica con el corredor y usrNexusDDL", e[:300])
fila = k.execute("SELECT modo FROM nexus.schemaMigrations WHERE version = 16").fetchone()
rev(version_aplicada() == 16 and fila is not None and fila[0] == "ejecutada", "queda anotada como la 16, ejecutada")
rev(vistas_de_consulta() == ["vDatoExtraido", "vDocumentoExtraido"], "crea las dos vistas en [consulta]", str(vistas_de_consulta()))
duenos = k.execute(
    "SELECT DISTINCT USER_NAME(COALESCE(o.principal_id, s.principal_id)) FROM sys.objects o JOIN sys.schemas s ON s.schema_id = o.schema_id "
    "WHERE s.name = 'consulta'"
).fetchall()
rev([d[0] for d in duenos] == ["nexusOwner"], "las vistas son de nexusOwner (la cadena de propiedad con [nexus])", str(duenos))
for vista in ("vDocumentoExtraido", "vDatoExtraido"):
    cols = lambda esquema: [f[0] for f in k.execute(f"SELECT name FROM sys.columns WHERE object_id = OBJECT_ID('{esquema}.{vista}') ORDER BY column_id").fetchall()]  # noqa: E731
    rev(cols("consulta") == cols("nexus") and len(cols("nexus")) > 10, f"consulta.{vista} tiene las mismas columnas, en el mismo orden, que nexus.{vista}")
rev(aplicar_0016() == "", "volver a aplicar no hace nada (ya no hay pendientes)")

print("\n[3] Datos de tres organizaciones")
ORG_A = "11111111-1111-4111-8111-111111111111"
ORG_B = "22222222-2222-4222-8222-222222222222"
ORG_C = "33333333-3333-4333-8333-333333333333"  # registrada, sin documentos


def documento(tenant: str, nombre: str) -> int:
    k.execute(
        "INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel) "
        "OUTPUT INSERTED.documentId VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        f"documento-{secrets.token_hex(16)}", tenant, "a" * 64, f"{tenant}/aa/aa/{'a' * 64}", 1234, "application/pdf", nombre, "MANUAL",
    )
    return k.fetchone()[0]


def corrida(doc: int, tipo: str, nombre: str) -> int:
    k.execute(
        "INSERT INTO nexus.extractionRuns (publicId, documentId, documentTypeId, documentTypePublicId, documentTypeName, documentTypeRevision, "
        "engine, processorId, processorVersion, status, errorDetail, confidenceAverage, confidenceMinimum, pageCount, metadataJson, startedAt, createdBy) "
        "OUTPUT INSERTED.runId VALUES (?, ?, NULL, ?, ?, 1, 'document_ai_extractor', 'proc123', NULL, 'ok', NULL, 90, 80, 1, NULL, SYSUTCDATETIME(), 'prueba')",
        f"extraccion-{secrets.token_hex(16)}", doc, tipo, nombre,
    )
    return k.fetchone()[0]


def dato(run: int, ruta: str, valor: str) -> None:
    k.execute(
        "INSERT INTO nexus.entityFacts (runId, fieldPath, fieldName, recordIndex, valueRaw, valueNormalized, confidence, nullReason) VALUES (?, ?, ?, NULL, ?, ?, 95, NULL)",
        run, ruta, ruta, valor, valor,
    )


for guid, nombre, slug, codigo in ((ORG_A, "Dorama Prueba", "dorama-prueba", "NEX-09001"), (ORG_B, "Otra Prueba", "otra-prueba", "NEX-09002"), (ORG_C, "Vacía Prueba", "vacia-prueba", "NEX-09003")):
    k.execute("INSERT INTO nexus.tenants (guid, name, slug, code) VALUES (?, ?, ?, ?)", guid, nombre, slug, codigo)
for n in range(2):
    r = corrida(documento(ORG_A, f"ine-a-{n}.pdf"), "tipo-ine", "INE")
    dato(r, "nombre", f"PERSONA A{n}")
    dato(r, "vigencia", "2030-12-31")
r = corrida(documento(ORG_B, "ine-b.pdf"), "tipo-ine-b", "INE")
for campo, valor in (("nombre", "PERSONA B"), ("curp", "BBBB000000"), ("vigencia", "2027-01-01")):
    dato(r, campo, valor)


def de_nexus(vista: str, org: str) -> list[tuple]:
    return sorted(tuple(f) for f in k.execute(f"SELECT * FROM nexus.{vista} WHERE organizacionGuid = ?", org).fetchall())


rev(len(de_nexus("vDocumentoExtraido", ORG_A)) == 2 and len(de_nexus("vDocumentoExtraido", ORG_B)) == 1, "en [nexus]: 2 documentos de A y 1 de B")


def lectura(base: str = DB):
    return BASE["conectar"](LECTURA, CLAVE_LECTURA, base)


def fijar(cur, org: str) -> str:
    return error_de(cur, "EXEC sys.sp_set_session_context @key = N'organizacion', @value = ?, @read_only = 1", org)


def leer(cur, vista: str) -> list[tuple]:
    return sorted(tuple(f) for f in cur.execute(f"SELECT * FROM consulta.{vista}").fetchall())


print(f"\n[4] Las seis comprobaciones del ticket, conectado como {LECTURA}")
con = lectura()
c = con.cursor()
rev(tuple(c.execute("SELECT SUSER_NAME(), DB_NAME()").fetchone()) == (LECTURA, DB), f"entra a {DB} como {LECTURA}")
rev(leer(c, "vDatoExtraido") == [] and leer(c, "vDocumentoExtraido") == [], "1. sin fijar organización: 0 renglones en las dos vistas")
rev(fijar(c, ORG_A) == "", "…la aplicación fija la organización A (solo lectura)")
rev(leer(c, "vDocumentoExtraido") == de_nexus("vDocumentoExtraido", ORG_A), "2. con A fijada: exactamente los documentos de A (2), ninguno de B")
rev(leer(c, "vDatoExtraido") == de_nexus("vDatoExtraido", ORG_A) and len(leer(c, "vDatoExtraido")) == 4, "…y exactamente los datos de A (4)")
rev(c.execute("SELECT count(*) FROM consulta.vDatoExtraido WHERE organizacionGuid = ?", ORG_B).fetchone()[0] == 0, "…aunque el modelo pida a B con su WHERE: 0")
e = fijar(c, ORG_B)
rev(numero(e, 15664), "3. volver a fijar otra organización en la misma conexión: error 15664", e[:200])
e = error_de(c, f"EXEC sp_set_session_context N'organizacion', '{ORG_B}'; SELECT * FROM consulta.vDatoExtraido")
rev(numero(e, 15664), "…tampoco escondido al principio de la consulta del modelo", e[:200])
rev(leer(c, "vDocumentoExtraido") == de_nexus("vDocumentoExtraido", ORG_A), "…y sigue viendo solo A")
for objeto in ("nexus.vDatoExtraido", "nexus.vDocumentoExtraido", "nexus.entityFacts", "nexus.documents", "nexus.tenants", "nexus.users"):
    e = error_de(c, f"SELECT TOP (1) * FROM {objeto}")
    rev(numero(e, 229), f"4. SELECT sobre {objeto}: error 229", e[:200])
rev(numero(error_de(c, f"SELECT TOP (1) * FROM {DB}.nexus.entityFacts"), 229), "…ni con el nombre de tres partes")
escrituras = {
    "INSERT en una tabla": "INSERT INTO nexus.tenants (guid, name, slug, code) VALUES ('x', 'x', 'x', 'NEX-99999')",
    "INSERT en la vista": "INSERT INTO consulta.vDatoExtraido (valor) VALUES ('x')",
    "UPDATE en la vista": "UPDATE consulta.vDatoExtraido SET valor = 'x'",
    "DELETE en la vista": "DELETE FROM consulta.vDatoExtraido",
    "UPDATE en una tabla": "UPDATE nexus.entityFacts SET valueNormalized = 'x'",
    "CREATE TABLE": "CREATE TABLE consulta.t (x int)",
    "CREATE VIEW": "CREATE VIEW consulta.v AS SELECT 1 AS x",
    "DROP VIEW": "DROP VIEW consulta.vDatoExtraido",
    "ALTER USER sobre otro usuario": "ALTER USER usrNexus WITH DEFAULT_SCHEMA = consulta",
    "meterse a un rol": f"ALTER ROLE db_datareader ADD MEMBER {LECTURA}",
}
for que, sql in escrituras.items():
    e = error_de(c, sql)
    rev(e != "", f"5. {que}: falla", "¡se ejecutó!")

def permisos() -> list[tuple]:
    return sorted(tuple(f) for f in k.execute(
        "SELECT p.class_desc, SCHEMA_NAME(p.major_id), p.permission_name, p.state_desc FROM sys.database_permissions p "
        f"WHERE p.grantee_principal_id = USER_ID('{LECTURA}')"
    ).fetchall())


rev(permisos() == [("DATABASE", None, "CONNECT", "GRANT"), ("SCHEMA", "consulta", "SELECT", "GRANT")], "sus únicos permisos: CONNECT y SELECT sobre [consulta]", str(permisos()))
# Darse permisos a sí mismo NO da error: SQL Server contesta con el aviso 4624 («…or yourself») y no cambia nada.
try:
    c.execute(f"GRANT SELECT ON SCHEMA::nexus TO {LECTURA}")
    negado = any("(4624)" in m[0] for m in c.messages)  # leer los avisos ANTES de nextset(), que los borra
except pyodbc.Error:
    negado = True
rev(negado and numero(error_de(c, "SELECT TOP (1) * FROM nexus.entityFacts"), 229), "5. darse SELECT sobre [nexus]: SQL Server se niega (aviso 4624) y [nexus] sigue dando 229")
rev(numero(error_de(c, f"GRANT SELECT ON SCHEMA::nexus TO usrNexus"), 15151) or error_de(c, "GRANT SELECT ON SCHEMA::nexus TO usrNexus") != "", "5. dar permisos a otro usuario: falla")
rev(permisos() == [("DATABASE", None, "CONNECT", "GRANT"), ("SCHEMA", "consulta", "SELECT", "GRANT")], "…y sus permisos siguen siendo los mismos dos")
# SQL Server deja a CUALQUIER usuario cambiar su propio esquema por omisión; no le abre nada: sin esquema, `entityFacts` sigue dando 229.
rev(error_de(c, f"ALTER USER {LECTURA} WITH DEFAULT_SCHEMA = nexus") == "" and numero(error_de(c, "SELECT TOP (1) * FROM entityFacts"), 229), "…cambiar su propio esquema por omisión a nexus sí se puede, pero leer entityFacts sin esquema sigue dando 229")
procedimiento = k.execute("SELECT TOP (1) QUOTENAME(SCHEMA_NAME(schema_id)) + '.' + QUOTENAME(name) FROM sys.procedures ORDER BY name").fetchone()
if procedimiento:
    e = error_de(c, f"EXEC {procedimiento[0]}")
    rev(numero(e, 229), f"5. EXEC de un SP nuestro ({procedimiento[0]}): error 229", e[:200])
else:
    print("  (la base ya no tiene SPs: el EXEC se prueba con sp_executesql sobre una tabla, que debe dar 229)")
    rev(numero(error_de(c, "EXEC sp_executesql N'SELECT * FROM nexus.tenants'"), 229), "5. sp_executesql no salta el permiso: 229")
e = error_de(c, "SELECT * FROM consulta.vDatoExtraido; DELETE FROM consulta.vDatoExtraido")
rev(e != "" and len(leer(c, "vDatoExtraido")) == 4, "…una lectura seguida de un DELETE falla y no borra nada", e[:200] or "¡no falló!")
for otra in OTRAS:
    try:
        lectura(otra).close()
        entro = True
    except pyodbc.Error:
        entro = False
    rev(not entro, f"6. no puede conectarse a {otra}")
    e = error_de(c, f"USE [{otra}]")
    rev(e != "", f"…ni cambiarse a {otra} con USE", "¡entró!")

print("\n[5] Las salidas que anotamos de este lado")
for que, sql in {
    "EXECUTE AS USER = 'usrNexus' (el de la API)": "EXECUTE AS USER = 'usrNexus'",
    "EXECUTE AS USER = 'dbo'": "EXECUTE AS USER = 'dbo'",
    "EXECUTE AS LOGIN = 'sa'": "EXECUTE AS LOGIN = 'sa'",
    "OPENROWSET (consulta ad hoc)": f"SELECT * FROM OPENROWSET('MSOLEDBSQL', 'Server=127.0.0.1;Trusted_Connection=yes;', 'SELECT 1')",
    "OPENQUERY": "SELECT * FROM OPENQUERY([127.0.0.1], 'SELECT 1')",
    "xp_cmdshell": "EXEC master..xp_cmdshell 'whoami'",
}.items():
    e = error_de(c, sql)
    rev(e != "", f"{que}: falla", "¡se ejecutó!")
rev(tuple(c.execute("SELECT SUSER_NAME(), USER_NAME()").fetchone()) == (LECTURA, LECTURA), "…y sigue siendo usrNexusLectura")
con.close()
con2 = lectura()
c2 = con2.cursor()
rev(leer(c2, "vDatoExtraido") == [], "una conexión NUEVA empieza sin organización: 0 renglones")
rev(fijar(c2, ORG_B.upper()) == "" and leer(c2, "vDocumentoExtraido") == de_nexus("vDocumentoExtraido", ORG_B), "con B fijada (aunque llegue en MAYÚSCULAS): solo el documento de B")
con2.close()
con3 = lectura()
c3 = con3.cursor()
rev(fijar(c3, ORG_C) == "" and leer(c3, "vDatoExtraido") == [] and leer(c3, "vDocumentoExtraido") == [], "con C (sin documentos): 0 renglones, no los de nadie más")
con3.close()

# Limpieza: las bases de mentira y el usuario del contenedor compartido (IA_Nexus_Dev la borra pruebas_sql al salir).
for otra in OTRAS:
    m.cursor().execute(f"IF DB_ID('{otra}') IS NOT NULL DROP DATABASE [{otra}]")
m.close()
sa.close()

print(f"\n{'TODO EN VERDE' if fallos == 0 else f'{fallos} FALLA(S)'}")
sys.exit(1 if fallos else 0)
