"""¿La conexión de solo lectura de la búsqueda (db/consulta.py, NEX-605) solo lee, solo ve la organización de quien pregunta y respeta sus topes?

    .venv/Scripts/python verificar_consulta_lectura.py

Corre contra el SQL Server desechable de `pruebas_sql.py` (sin Docker se salta y lo dice), con TODAS las migraciones (la 0017 incluida) y lo que
hizo el DBA en NEX-604: el login `usrNexusLectura` con SELECT sobre `[consulta]` y nada más. Lo que se prueba, por `consultar()` y no a mano:
- las credenciales: sin archivo, con otro usuario o con otra base, se niega;
- cada organización ve exactamente lo suyo, también alternando muchas veces (el pool del driver no arrastra la organización de la pregunta anterior);
- cambiar la organización dentro de la consulta falla (15664), al principio o al final, y la siguiente pregunta no se entera;
- leer `[nexus]`, escribir, crear, `EXECUTE AS`, `OPENROWSET`: rechazado por la base, con su número;
- el tiempo máximo corta la consulta y la conexión siguiente funciona; el tope de renglones se respeta y avisa;
- `/health/consulta` dice `ok` con credenciales y `sin_configurar` sin ellas.
"""

import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pruebas_sql  # noqa: E402

BASE = pruebas_sql.preparar("nexus-consulta-lectura", 14343)

import pyodbc  # noqa: E402

DB = BASE["base"]
LECTURA = "usrNexusLectura"
CLAVE = "Lec;=}x-" + secrets.token_urlsafe(12) + "-9!"  # con ; = y } para probar que la cadena de conexión la escapa
fallos = 0


def rev(condicion: bool, texto: str, detalle: str = "") -> None:
    global fallos
    print(("OK   " if condicion else "FALLA") + " " + texto + ("" if condicion or not detalle else f"\n      {detalle}"))
    if not condicion:
        fallos += 1


sa = BASE["conectar"]("sa", BASE["sa"], DB)
k = sa.cursor()
m = BASE["conectar"]("sa", BASE["sa"])
clave_sql = CLAVE.replace("'", "''")
m.cursor().execute(
    f"IF SUSER_ID('{LECTURA}') IS NULL CREATE LOGIN {LECTURA} WITH PASSWORD = '{clave_sql}', CHECK_POLICY = ON "
    f"ELSE ALTER LOGIN {LECTURA} WITH PASSWORD = '{clave_sql}'"
)
k.execute(f"CREATE USER {LECTURA} FOR LOGIN {LECTURA}")
k.execute(f"GRANT SELECT ON SCHEMA::consulta TO {LECTURA}")
rev(k.execute("SELECT max(version) FROM nexus.schemaMigrations").fetchone()[0] >= 17, "la base de prueba trae la 0017 (vistas de [consulta])")

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


carpeta = Path(tempfile.mkdtemp(prefix="consulta-lectura-"))
archivo = carpeta / "lectura.env"


def credenciales(usuario: str = LECTURA, clave: str = CLAVE, base: str | None = None) -> None:
    lineas = [f"SQLSERVER_USER={usuario}", f"SQLSERVER_PASSWORD='{clave}'"] + ([f"SQLSERVER_DB={base}"] if base else [])
    archivo.write_text("\n".join(lineas) + "\n", encoding="utf-8")


os.environ["NEXUS_LECTURA_ENV"] = str(archivo)

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from db import consulta  # noqa: E402

cliente = TestClient(app)


def falla(funcion, *args, **kwargs):
    try:
        funcion(*args, **kwargs)
        return None
    except Exception as e:  # noqa: BLE001
        return e


print("\n[1] Credenciales")
e = falla(consulta.consultar, ORG_A, "SELECT 1")
rev(isinstance(e, consulta.ConsultaNoDisponible) and "No encuentro" in str(e), "sin archivo: ConsultaNoDisponible", repr(e))
rev(cliente.get("/health/consulta").json() == {"status": "sin_configurar"}, "…y /health/consulta dice sin_configurar (sin rutas ni detalle)")
credenciales(usuario="usrNexus", clave=pruebas_sql.json.loads(pruebas_sql._CREDENCIALES.read_text(encoding="utf-8"))["app"])
e = falla(consulta.consultar, ORG_A, "SELECT 1")
rev(isinstance(e, consulta.ConsultaNoDisponible) and LECTURA in str(e), "con las credenciales de OTRO usuario (usrNexus): se niega antes de conectar", repr(e))
credenciales(base="IA_Nexus")
e = falla(consulta.consultar, ORG_A, "SELECT 1")
rev(isinstance(e, consulta.ConsultaNoDisponible) and "IA_Nexus" in str(e), "con otra base en el archivo: se niega", repr(e))
credenciales(clave="equivocada-123")
e = falla(consulta.consultar, ORG_A, "SELECT 1")
rev(isinstance(e, consulta.ConsultaNoDisponible) and "equivocada" not in str(e), "con contraseña equivocada: ConsultaNoDisponible, sin la contraseña en el mensaje", repr(e))
credenciales(base=DB)
rev(consulta.consultar(ORG_A, "SELECT 1 AS uno").filas == [[1]], "con usrNexusLectura y la base del back: conecta (la contraseña con ; = y } viaja bien)")
for malo in ("", "plataforma", ORG_A + "'; DROP TABLE x --", None):
    rev(isinstance(falla(consulta.consultar, malo, "SELECT 1"), ValueError), f"organización inválida {malo!r}: ValueError antes de conectar")
rev(isinstance(falla(consulta.consultar, ORG_A, "   "), ValueError), "consulta vacía: ValueError")

print("\n[2] Cada organización ve lo suyo")
VISTAS = ("vDocumentoExtraido", "vDatoExtraido")
for org, nombre in ((ORG_A, "A"), (ORG_B, "B"), (ORG_C, "C (sin documentos)")):
    for vista in VISTAS:
        r = consulta.consultar(org, f"SELECT * FROM consulta.{vista}")
        rev(sorted(tuple(f) for f in r.filas) == de_nexus(vista, org), f"{nombre}: consulta.{vista} = lo de {nombre} en [nexus] ({len(r.filas)})")
rev(consulta.consultar(ORG_A, f"SELECT COUNT(*) FROM consulta.vDatoExtraido WHERE organizacionGuid = '{ORG_B}'").filas == [[0]], "A pidiendo a B con su WHERE: 0")
rev(pyodbc.pooling is True, "este proceso usa el pool del driver (como la API)")
bien = 0
for vuelta in range(12):
    org = ORG_A if vuelta % 2 == 0 else ORG_B
    r = consulta.consultar(org, "SELECT DISTINCT organizacionGuid FROM consulta.vDatoExtraido")
    bien += r.filas == [[org]]
rev(bien == 12, f"alternando A y B 12 veces: cada pregunta ve solo su organización ({bien}/12)")

print("\n[3] La organización no se cambia desde la consulta")
for sql, donde in (
    (f"EXEC sp_set_session_context N'organizacion', '{ORG_B}'; SELECT * FROM consulta.vDatoExtraido", "al principio"),
    (f"DECLARE @o sql_variant = '{ORG_B}'; EXEC sys.sp_set_session_context @key = N'organizacion', @value = @o; SELECT 1", "con variable"),
):
    e = falla(consulta.consultar, ORG_A, sql)
    rev(isinstance(e, consulta.ConsultaRechazada) and e.numero == 15664, f"cambiarla {donde}: ConsultaRechazada 15664", repr(e))
r = consulta.consultar(ORG_A, f"SELECT DISTINCT organizacionGuid FROM consulta.vDatoExtraido; EXEC sp_set_session_context N'organizacion', '{ORG_B}'")
rev(r.filas == [[ORG_A]], "cambiarla al final: lo que se devuelve es solo de A")
rev(consulta.consultar(ORG_B, "SELECT DISTINCT organizacionGuid FROM consulta.vDatoExtraido").filas == [[ORG_B]], "…y la pregunta siguiente (de B) ve solo B")

print("\n[4] Solo lectura: lo rechaza la base")
casos = {
    "SELECT sobre nexus.entityFacts": ("SELECT TOP (1) * FROM nexus.entityFacts", {229}),
    "SELECT sobre nexus.vDatoExtraido": ("SELECT TOP (1) * FROM nexus.vDatoExtraido", {229}),
    "SQL dinámico hacia [nexus]": ("EXEC('SELECT TOP (1) * FROM nexus.tenants')", {229}),
    "INSERT en la vista": ("INSERT INTO consulta.vDatoExtraido (campo) VALUES ('x')", {229}),
    "UPDATE en la vista": ("UPDATE consulta.vDatoExtraido SET campo = campo", {229}),
    # La vista junta varias tablas: SQL Server la rechaza por eso (4405) antes de mirar el permiso; en dev, con el usuario real, igual.
    "DELETE en la vista": ("DELETE FROM consulta.vDocumentoExtraido", {229, 4405}),
    "CREATE TABLE": ("CREATE TABLE consulta.t (x int)", {262}),
    "EXECUTE AS": ("EXECUTE AS USER = 'usrNexus'", {15517, 15406}),
    "OPENROWSET": ("SELECT * FROM OPENROWSET('SQLNCLI', 'Server=(local);Trusted_Connection=yes;', 'SELECT 1')", {15281, 7415}),
    "otra base con tres partes": ("SELECT TOP (1) * FROM master.sys.sql_logins", set()),
}
for que, (sql, numeros) in casos.items():
    e = falla(consulta.consultar, ORG_A, sql)
    if not numeros:  # master.sys.sql_logins se puede LEER: sin permisos de servidor solo trae el propio login y `sa` (visible para todos)
        r = consulta.consultar(ORG_A, "SELECT name FROM master.sys.sql_logins")
        rev({f[0] for f in r.filas} <= {LECTURA, "sa"}, "master.sys.sql_logins: no ve los logins de la aplicación (usrNexus, usrNexusDDL)", str(r.filas))
        continue
    rev(isinstance(e, consulta.ConsultaRechazada) and e.numero in numeros, f"{que}: ConsultaRechazada {sorted(numeros)}", repr(e))
e = falla(consulta.consultar, ORG_A, "SELECT noExiste FROM consulta.vDatoExtraido")
rev(isinstance(e, consulta.ConsultaRechazada) and e.numero == 207 and e.mensaje.startswith("Invalid column name") and "[" not in e.mensaje,
    "una columna que no existe: 207 con el mensaje limpio (sirve para pedirle al modelo que corrija)", repr(getattr(e, "mensaje", e)))

print("\n[5] Topes")
inicio = time.monotonic()
e = falla(consulta.consultar, ORG_A, "WAITFOR DELAY '00:00:06'; SELECT 1", tiempo_maximo=1)
rev(isinstance(e, consulta.ConsultaTardo) and time.monotonic() - inicio < 4, f"una consulta de 6 s con tiempo máximo de 1 s: ConsultaTardo en {time.monotonic() - inicio:.1f} s", repr(e))
# Con el WHERE, SQL Server no puede contar el producto sin recorrerlo (sin él lo resuelve en menos de un segundo).
pesada = "SELECT COUNT_BIG(*) FROM sys.all_objects a CROSS JOIN sys.all_objects b CROSS JOIN sys.all_objects c WHERE CHECKSUM(a.name, b.name, c.name) = 7"
inicio = time.monotonic()
e = falla(consulta.consultar, ORG_A, pesada, tiempo_maximo=2)
rev(isinstance(e, consulta.ConsultaTardo) and time.monotonic() - inicio < 5, f"una consulta pesada: cortada en {time.monotonic() - inicio:.1f} s", repr(e))
vivas = k.execute("SELECT COUNT(*) FROM sys.dm_exec_requests r JOIN sys.dm_exec_sessions s ON s.session_id = r.session_id WHERE s.login_name = ?", LECTURA).fetchone()[0]
rev(vivas == 0, "…y no queda corriendo en el servidor", f"{vivas} peticiones vivas")
rev(consulta.consultar(ORG_A, "SELECT COUNT(*) FROM consulta.vDatoExtraido").filas == [[4]], "la pregunta siguiente funciona")
r = consulta.consultar(ORG_A, "SELECT * FROM consulta.vDatoExtraido", tope=3)
rev(len(r.filas) == 3 and r.hay_mas and r.tope == 3, "4 renglones con tope 3: devuelve 3 y avisa que hay más")
r = consulta.consultar(ORG_A, "SELECT * FROM consulta.vDatoExtraido", tope=4)
rev(len(r.filas) == 4 and not r.hay_mas, "con tope 4: los 4, sin aviso")
r = consulta.consultar(ORG_A, "SELECT TOP (5000) a.object_id FROM sys.all_objects a CROSS JOIN sys.all_objects b")
rev(len(r.filas) == consulta.config.CONSULTA_TOPE_RENGLONES and r.hay_mas, f"sin tope explícito: el de config ({consulta.config.CONSULTA_TOPE_RENGLONES}) y avisa")
r = consulta.consultar(ORG_A, "DECLARE @x int = 1")
rev(r.columnas == [] and r.filas == [], "una consulta sin resultados: columnas y filas vacías")
r = consulta.consultar(ORG_A, "SELECT campo, valor FROM consulta.vDatoExtraido WHERE campo = 'nombre' ORDER BY valor")
rev(r.columnas == ["campo", "valor"] and r.filas == [["nombre", "PERSONA A0"], ["nombre", "PERSONA A1"]] and r.milisegundos >= 0, "columnas con su nombre y filas como listas")

print("\n[6] /health/consulta")
rev(cliente.get("/health/consulta").json() == {"status": "ok", "sin_organizacion_vacio": True, "nexus_cerrado": True}, "con credenciales: ok, vacío sin organización y [nexus] cerrado")
archivo.unlink()
os.rmdir(carpeta)

print("\n" + ("TODO EN VERDE" if not fallos else f"{fallos} FALLA(S)"))
sys.exit(1 if fallos else 0)
