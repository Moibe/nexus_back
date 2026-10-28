"""¿El corredor y la línea base funcionan contra un SQL Server DE VERDAD?

    .venv/Scripts/python verificar_migraciones_sql.py            (necesita Docker Desktop encendido; baja la imagen la primera vez, ~1.6 GB)
    .venv/Scripts/python verificar_migraciones_sql.py --dejar    (deja el contenedor vivo para investigar: `docker rm -f nexus-sql-prueba` al terminar)

NO toca las bases de CSI. Levanta un SQL Server 2022 desechable en un contenedor (127.0.0.1:14331), crea en él las TRES bases (`IA_Nexus_Dev`,
`IA_Nexus_Staging`, `IA_Nexus`) con la misma intercalación, los mismos esquemas (de `nexusOwner`) y los mismos usuarios y permisos que armó Charlie
(`usrNexusDDL` y `usrNexus`), apunta el corredor a él con un archivo de credenciales temporal y comprueba lo que la prueba sin base (`verificar_corredor.py`) no puede:

- que la línea base SE EJECUTA en una base vacía y reproduce el esquema: 36 tablas, 36 módulos, los catálogos con sus filas, y que volver a leer el catálogo
  y generar otra vez da EXACTAMENTE el mismo texto que el archivo (el círculo completo);
- que una simulación no deja NADA (ni la tabla de control) y que cada migración corre en su transacción: una que falla a medias deshace hasta su DDL;
- la cadena dev → staging → prod y `marcar` sobre bases que ya tenían el esquema, la guarda de la línea base contra una base ya construida y el candado;
- que los procedimientos congelados siguen funcionando con los esquemas de `nexusOwner` y los permisos de `usrNexus` (la cadena de propiedad).
Sin Docker, no hace nada y lo dice (código de salida 0): no es una prueba que se pueda dar por pasada si no corrió.
"""

import contextlib
import io
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

CONTENEDOR = "nexus-sql-prueba"
IMAGEN = "mcr.microsoft.com/mssql/server:2022-latest"
PUERTO = 14331
BASES = ["IA_Nexus_Dev", "IA_Nexus_Staging", "IA_Nexus"]
ESQUEMAS = ["documents", "security", "reference", "nexus"]
DEJAR = "--dejar" in sys.argv

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + extra if extra and not ok else ''}")
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


def docker(*args: str, capturar: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=capturar, text=True, encoding="utf-8", errors="replace", timeout=900)


try:
    ok = docker("info", "--format", "{{.ServerVersion}}")
except (FileNotFoundError, subprocess.TimeoutExpired):
    ok = None
if ok is None or ok.returncode != 0:
    print("SALTADA: Docker no está disponible (enciende Docker Desktop). Esta prueba NO corrió: no cuenta como pasada.")
    raise SystemExit(0)

import pyodbc  # noqa: E402

from migraciones import corredor  # noqa: E402
from migraciones.corredor import BaseSql, ErrorMigracion, Migracion, descubrir, ejecutar, estado, normalizar  # noqa: E402
from migraciones.linea_base import generar, leer_catalogo  # noqa: E402

SA = "Sa-" + secrets.token_urlsafe(12) + "-9!"
DDL = "Ddl-" + secrets.token_urlsafe(12) + "-7!"
APP = "App-" + secrets.token_urlsafe(12) + "-5!"
DRIVER = "ODBC Driver 17 for SQL Server"
carpeta_env = Path(tempfile.mkdtemp(prefix="verificar-migraciones-sql-"))


def conectar(usuario: str, clave: str, base: str = "master", autocommit: bool = True):
    return pyodbc.connect(f"DRIVER={{{DRIVER}}};SERVER=127.0.0.1,{PUERTO};DATABASE={base};UID={usuario};PWD={clave};TrustServerCertificate=yes;Encrypt=yes", timeout=15, autocommit=autocommit)


def como_sa(base: str = "master"):
    return conectar("sa", SA, base)


def correr_cli(*argumentos: str) -> tuple[int, str]:
    salida = io.StringIO()
    try:
        with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(salida):
            codigo = corredor.main(list(argumentos))
    except SystemExit as exc:
        codigo = int(exc.code or 0)
    return codigo, salida.getvalue()


def uno(cur, sql: str, *a):
    cur.execute(sql, *a)
    return cur.fetchone()[0]


def levantar() -> None:
    docker("rm", "-f", CONTENEDOR)
    r = docker("run", "-d", "--name", CONTENEDOR, "-e", "ACCEPT_EULA=Y", "-e", f"MSSQL_SA_PASSWORD={SA}", "-p", f"127.0.0.1:{PUERTO}:1433", IMAGEN)
    if r.returncode != 0:
        raise SystemExit(f"No se pudo levantar el contenedor: {r.stderr.strip()[:300]}")
    limite = time.monotonic() + 180
    while time.monotonic() < limite:
        try:
            como_sa().close()
            return
        except pyodbc.Error:
            time.sleep(3)
    raise SystemExit("SQL Server no contestó en 3 minutos. Revisa `docker logs " + CONTENEDOR + "`.")


def armar_bases() -> None:
    """Lo que el DBA deja listo: bases con la intercalación de CSI, esquemas de nexusOwner, y usrNexusDDL / usrNexus con los permisos de Charlie."""
    con = como_sa()
    cur = con.cursor()
    cur.execute(f"CREATE LOGIN usrNexusDDL WITH PASSWORD = '{DDL}', CHECK_POLICY = ON, CHECK_EXPIRATION = OFF")
    cur.execute(f"CREATE LOGIN usrNexus WITH PASSWORD = '{APP}', CHECK_POLICY = ON, CHECK_EXPIRATION = OFF")
    for base in BASES:
        cur.execute(f"CREATE DATABASE [{base}] COLLATE Modern_Spanish_CI_AS")
        c2 = como_sa(base)
        k = c2.cursor()
        k.execute("CREATE USER nexusOwner WITHOUT LOGIN")
        for e in ESQUEMAS:
            k.execute(f"CREATE SCHEMA {e} AUTHORIZATION nexusOwner")
        k.execute("CREATE USER usrNexusDDL FOR LOGIN usrNexusDDL WITH DEFAULT_SCHEMA = nexus")
        k.execute("GRANT CREATE TABLE, CREATE VIEW, CREATE FUNCTION, CREATE PROCEDURE, CREATE TYPE TO usrNexusDDL")
        for e in ESQUEMAS:
            k.execute(f"GRANT ALTER, SELECT, INSERT, UPDATE, DELETE, EXECUTE, REFERENCES, VIEW DEFINITION ON SCHEMA::{e} TO usrNexusDDL")
        k.execute("CREATE USER usrNexus FOR LOGIN usrNexus")
        k.execute("ALTER ROLE db_datareader ADD MEMBER usrNexus")
        k.execute("ALTER ROLE db_datawriter ADD MEMBER usrNexus")
        k.execute("GRANT EXECUTE TO usrNexus")
        k.execute("GRANT VIEW DEFINITION ON SCHEMA::documents TO usrNexus")
        k.execute("GRANT VIEW DEFINITION ON SCHEMA::security TO usrNexus")
        k.execute("DENY CREATE TABLE, CREATE VIEW, CREATE FUNCTION, CREATE PROCEDURE, CREATE TYPE, CREATE SCHEMA, ALTER ANY SCHEMA TO usrNexus")
        c2.close()
    con.close()


def existe(base: str, objeto: str) -> bool:
    c = como_sa(base)
    r = uno(c.cursor(), "SELECT OBJECT_ID(?)", objeto) is not None
    c.close()
    return r


def contar(base: str, sql: str):
    c = como_sa(base)
    r = uno(c.cursor(), sql)
    c.close()
    return r


M1 = descubrir()[0]
env = carpeta_env / "ddl.env"
env.write_text(f"SQLSERVER_HOST=127.0.0.1\nSQLSERVER_PORT={PUERTO}\nSQLSERVER_USER=usrNexusDDL\nSQLSERVER_PASSWORD={DDL}\nSQLSERVER_DRIVER={DRIVER}\n", encoding="utf-8")
os.environ["NEXUS_DDL_ENV"] = str(env)

try:
    titulo("0 · Un SQL Server desechable con las tres bases vacías y los usuarios de Charlie")
    print(f"  (imagen {IMAGEN}; si es la primera vez, la descarga tarda)")
    levantar()
    armar_bases()
    rev("las tres bases existen, están vacías y sus esquemas son de nexusOwner", all(contar(b, "SELECT COUNT(*) FROM sys.tables") == 0 and contar(b, "SELECT COUNT(*) FROM sys.schemas s WHERE s.name IN ('documents','security','reference','nexus') AND USER_NAME(s.principal_id) = 'nexusOwner'") == 4 for b in BASES))

    titulo("1 · Estado y simulación sobre una base VACÍA")
    codigo, texto = correr_cli("estado", "--base", "IA_Nexus_Dev")
    rev("estado: la línea base está pendiente", codigo == 0 and "0001_linea_base" in texto and "pendiente" in texto, texto)
    rev("y consultar el estado no creó la tabla de control", not existe("IA_Nexus_Dev", "nexus.schemaMigrations"))
    codigo, texto = correr_cli("aplicar", "--base", "IA_Nexus_Dev")
    rev("aplicar SIN --confirmar: simulación que corre toda la línea base y dice que se DESHIZO", codigo == 0 and "SIMULACIÓN" in texto and "0001_linea_base.sql" in texto, texto)
    rev("y no dejó NADA: ni una tabla, ni un procedimiento, ni la tabla de control", contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM sys.tables") == 0 and contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM sys.procedures") == 0)

    titulo("2 · Aplicar la línea base de verdad en IA_Nexus_Dev")
    codigo, texto = correr_cli("aplicar", "--base", "IA_Nexus_Dev", "--confirmar")
    rev("se aplica", codigo == 0 and "Listo" in texto, texto)
    rev("36 tablas de negocio y catálogo, más la de control", contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM sys.tables WHERE SCHEMA_NAME(schema_id) <> 'nexus'") == 36 and existe("IA_Nexus_Dev", "nexus.schemaMigrations"))
    rev("32 procedimientos y 4 funciones", contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM sys.procedures") == 32 and contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM sys.objects WHERE type IN ('IF','FN','TF')") == 4)
    rev("48 llaves foráneas, 62 CHECK", contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM sys.foreign_keys") == 48 and contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM sys.check_constraints WHERE SCHEMA_NAME(schema_id) <> 'nexus'") == 62)
    rev("los catálogos traen sus filas (6 canales, 10 roles) y la secuencia de organizaciones está en 0", contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM reference.ingestionChannels") == 6 and contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM security.roles") == 10 and contar("IA_Nexus_Dev", "SELECT lastSequence FROM security.tenantSequence WHERE prefix = 'NEX'") == 0)
    rev("sin ninguna organización ni usuario", contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM security.tenants") == 0 and contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM security.users") == 0)
    rev("la tabla de control anotó la 1 como `ejecutada`, con la suma del archivo", contar("IA_Nexus_Dev", f"SELECT COUNT(*) FROM nexus.schemaMigrations WHERE version = 1 AND modo = 'ejecutada' AND suma = '{M1.suma}'") == 1)
    rev("los objetos quedaron a nombre de nexusOwner (heredan el dueño del esquema)", contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM sys.tables t WHERE SCHEMA_NAME(t.schema_id) <> 'nexus' AND USER_NAME(COALESCE(t.principal_id, (SELECT principal_id FROM sys.schemas s WHERE s.schema_id = t.schema_id))) <> 'nexusOwner'") == 0)
    codigo, texto = correr_cli("estado", "--base", "IA_Nexus_Dev")
    rev("estado: aplicada", codigo == 0 and "aplicada" in texto and "pendiente" not in texto, texto)

    titulo("3 · EL CÍRCULO: volver a leer el catálogo y generar da el MISMO archivo")
    b = BaseSql("IA_Nexus_Dev", corredor._credenciales())
    try:
        cur = b._con.cursor()
        regenerado = generar(leer_catalogo(cur))
        b._con.rollback()
    finally:
        b.cerrar()
    igual = normalizar(regenerado) == normalizar(M1.sql)
    rev("la base construida desde el archivo se describe IDÉNTICA al archivo (tablas, columnas, índices, llaves, filas, módulos)", igual)
    if not igual:
        a, c = normalizar(regenerado).splitlines(), normalizar(M1.sql).splitlines()
        for i, (x, y) in enumerate(zip(a, c)):
            if x != y:
                print(f"     primera diferencia en la línea {i + 1}:\n       regenerado: {x[:150]}\n       archivo:    {y[:150]}")
                break
        else:
            print(f"     una es más larga: {len(a)} vs {len(c)} líneas")

    titulo("4 · Repetir, y la guarda de la línea base")
    codigo, texto = correr_cli("aplicar", "--base", "IA_Nexus_Dev", "--confirmar")
    rev("repetir: no hay nada pendiente", codigo == 0 and "no hay migraciones pendientes" in texto, texto)
    # Una base que YA tiene el esquema (como IA_Nexus de CSI): la línea base se niega a ejecutarse encima.
    bs = BaseSql("IA_Nexus_Staging", corredor._credenciales())
    try:
        cur = bs._con.cursor()
        for lote in M1.lotes:
            cur.execute(lote)
            while cur.nextset():
                pass
        bs._con.commit()
    finally:
        bs.cerrar()
    rev("armé a mano (como la base de CSI) el esquema en Staging y en IA_Nexus", True)
    bp = BaseSql("IA_Nexus", corredor._credenciales())
    try:
        cur = bp._con.cursor()
        for lote in M1.lotes:
            cur.execute(lote)
            while cur.nextset():
                pass
        bp._con.commit()
    finally:
        bp.cerrar()
    bs = BaseSql("IA_Nexus_Staging", corredor._credenciales())
    try:
        try:
            ejecutar(bs, [M1], accion="aplicar", confirmar=True)  # sin cadena: aquí solo se prueba la guarda del propio archivo
            rev("ejecutar la línea base sobre una base que ya la tiene: se niega con SQL Server", False)
        except pyodbc.Error as exc:
            rev("ejecutar la línea base sobre una base que ya la tiene: SQL Server la rechaza con su guarda (51002)", "MARCA" in str(exc) or "51002" in str(exc), str(exc)[:200])
        rev("y no dejó la migración anotada", contar("IA_Nexus_Staging", "SELECT COUNT(*) FROM sys.tables WHERE name = 'schemaMigrations'") == 0 or contar("IA_Nexus_Staging", "SELECT COUNT(*) FROM nexus.schemaMigrations") == 0)
    finally:
        bs.cerrar()

    titulo("5 · Marcar en las bases que ya tenían el esquema, con la cadena dev → staging → prod")
    codigo, texto = correr_cli("marcar", "--base", "IA_Nexus", "--version", "1", "--confirmar", "--produccion")
    rev("marcar en PRODUCCIÓN antes que en staging: la cadena lo niega", codigo == 2 and "va primero ahí" in texto and "IA_Nexus_Staging" in texto, texto)
    codigo, texto = correr_cli("marcar", "--base", "IA_Nexus_Staging", "--version", "1", "--confirmar")
    rev("marcar en staging (dev ya la tiene, con la misma suma)", codigo == 0 and "marcada" in texto, texto)
    rev("quedó `marcada` y NO se ejecutó nada (las tablas siguen siendo las armadas a mano)", contar("IA_Nexus_Staging", "SELECT COUNT(*) FROM nexus.schemaMigrations WHERE version = 1 AND modo = 'marcada'") == 1)
    codigo, texto = correr_cli("marcar", "--base", "IA_Nexus", "--version", "1", "--confirmar")
    rev("producción con --confirmar pero SIN --produccion: se niega", codigo == 2 and "--produccion" in texto, texto)
    codigo, texto = correr_cli("marcar", "--base", "IA_Nexus", "--version", "1")
    rev("la SIMULACIÓN de marcar en producción sí se puede (no escribe)", codigo == 0 and "SIMULACIÓN" in texto and contar("IA_Nexus", "SELECT COUNT(*) FROM sys.tables WHERE name = 'schemaMigrations'") == 0, texto)
    codigo, texto = correr_cli("marcar", "--base", "IA_Nexus", "--version", "1", "--confirmar", "--produccion")
    rev("marcar en producción con las dos banderas, estando en staging", codigo == 0 and "marcada" in texto, texto)
    codigo, texto = correr_cli("estado", "--base", "IA_Nexus")
    rev("estado de producción: marcada", codigo == 0 and "marcada" in texto, texto)

    titulo("6 · Una migración que falla deshace TODO lo suyo, hasta su DDL; las anteriores se quedan")
    dev = BaseSql("IA_Nexus_Dev", corredor._credenciales())
    try:
        buena = Migracion(2, "agrega_prueba_ok", "CREATE TABLE nexus.zz_ok (id int NOT NULL PRIMARY KEY);\nGO\nINSERT INTO nexus.zz_ok (id) VALUES (1);\nGO\n")
        mala = Migracion(3, "falla_a_medias", "CREATE TABLE nexus.zz_a (id int);\nGO\nCREATE TABLE nexus.zz_b (id int);\nGO\nSELECT 1/0 AS exploto;\nGO\n")
        try:
            ejecutar(dev, [M1, buena, mala], accion="aplicar", confirmar=True)
            rev("la tercera revienta", False)
        except pyodbc.Error as exc:
            rev("la tercera revienta con el error de SQL Server (no se traga)", "zero" in str(exc).lower() or "cero" in str(exc).lower() or "divid" in str(exc).lower(), str(exc)[:160])
    finally:
        dev.cerrar()
    rev("la 2 SÍ quedó (tabla y fila) y anotada", existe("IA_Nexus_Dev", "nexus.zz_ok") and contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM nexus.zz_ok") == 1 and contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM nexus.schemaMigrations WHERE version = 2") == 1)
    rev("la 3 no dejó NADA: ni sus dos tablas (su DDL se deshizo) ni su anotación", not existe("IA_Nexus_Dev", "nexus.zz_a") and not existe("IA_Nexus_Dev", "nexus.zz_b") and contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM nexus.schemaMigrations WHERE version = 3") == 0)

    titulo("7 · La simulación de varias migraciones encadenadas usa UNA transacción y no deja nada")
    dev = BaseSql("IA_Nexus_Dev", corredor._credenciales())
    try:
        m3 = Migracion(3, "crea_t", "CREATE TABLE nexus.zz_t (id int);\nGO\n")
        m4 = Migracion(4, "usa_t", "INSERT INTO nexus.zz_t (id) VALUES (7);\nGO\n")
        lineas = ejecutar(dev, [M1, buena, m3, m4], accion="aplicar", confirmar=False)
        rev("la 4 usa lo que creó la 3 DENTRO de la simulación", any("SIMULACIÓN" in x for x in lineas), str(lineas))
        rev("y al final no existe ni la tabla ni las anotaciones", not existe("IA_Nexus_Dev", "nexus.zz_t") and contar("IA_Nexus_Dev", "SELECT COUNT(*) FROM nexus.schemaMigrations WHERE version >= 3") == 0)
        editada = Migracion(2, "agrega_prueba_ok", buena.sql + "-- editada después\n")
        try:
            ejecutar(dev, [M1, editada], accion="aplicar", confirmar=True)
            rev("editar una migración ya aplicada: se niega", False)
        except ErrorMigracion as exc:
            rev("editar una migración ya aplicada: se niega, con SQL Server de por medio", "agrega_prueba_ok" in str(exc))
    finally:
        dev.cerrar()

    titulo("8 · El candado: dos corredores no trabajan a la vez sobre la misma base")
    rival = conectar("usrNexusDDL", DDL, "IA_Nexus_Staging", autocommit=True)
    try:
        cur = rival.cursor()
        cur.execute("DECLARE @r int; EXEC @r = sp_getapplock @Resource = N'nexus.migraciones', @LockMode = 'Exclusive', @LockOwner = 'Session', @LockTimeout = 0; SELECT @r")
        rev("(un corredor rival toma el candado de IA_Nexus_Staging)", cur.fetchone()[0] >= 0)
        codigo, texto = correr_cli("aplicar", "--base", "IA_Nexus_Staging")
        rev("el corredor se niega y dice que otro está trabajando", codigo == 2 and "Otro corredor" in texto, texto)
        codigo, texto = correr_cli("aplicar", "--base", "IA_Nexus_Dev")
        rev("pero el candado es POR BASE: en Dev sí puede trabajar (aquí solo dice que Dev tiene una migración de prueba que no está en la carpeta)", "Otro corredor" not in texto, texto)
    finally:
        # El pool de conexiones de ESTE proceso de prueba mantendría vivo el candado de sesión después de close(): se suelta a mano.
        try:
            rival.cursor().execute("EXEC sp_releaseapplock @Resource = N'nexus.migraciones', @LockOwner = 'Session'")
        except pyodbc.Error:
            pass
        rival.close()
    codigo, texto = correr_cli("aplicar", "--base", "IA_Nexus_Staging")
    rev("al soltarse el candado, el corredor vuelve a poder (y no hay nada pendiente)", codigo == 0 and "no hay migraciones pendientes" in texto, texto)
    rev("el corredor deja SU candado suelto al terminar (corridas seguidas no chocan entre sí)", correr_cli("aplicar", "--base", "IA_Nexus_Staging")[0] == 0 and correr_cli("aplicar", "--base", "IA_Nexus_Staging")[0] == 0)

    titulo("9 · Los procedimientos congelados funcionan con los esquemas de nexusOwner y los permisos de usrNexus")
    app = conectar("usrNexus", APP, "IA_Nexus_Dev")
    try:
        cur = app.cursor()
        cur.execute("EXEC security.uspListTenants")
        filas = cur.fetchall()
        rev("usrNexus ejecuta uspListTenants en la base construida desde el archivo (la cadena de propiedad funciona): 0 organizaciones", filas == [], str(filas)[:120])
        cur.execute("SELECT OBJECT_DEFINITION(OBJECT_ID('documents.uspCreateFile'))")
        rev("y ve la definición de un procedimiento de documents (VIEW DEFINITION)", cur.fetchone()[0] is not None)
        try:
            cur.execute("CREATE TABLE documents.zz_no (id int)")
            rev("usrNexus NO puede crear tablas", False)
        except pyodbc.Error:
            rev("usrNexus NO puede crear tablas (el DENY de Charlie, reproducido)", True)
    finally:
        app.close()
finally:
    if DEJAR:
        print(f"\n(el contenedor «{CONTENEDOR}» sigue vivo en 127.0.0.1:{PUERTO}; quítalo con: docker rm -f {CONTENEDOR})")
    else:
        docker("rm", "-f", CONTENEDOR)
    shutil.rmtree(carpeta_env, ignore_errors=True)
    os.environ.pop("NEXUS_DDL_ENV", None)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
