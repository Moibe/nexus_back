"""Un SQL Server DESECHABLE para las pruebas: el ayudante común de los `verificar_*` que corren contra la base.

Desde el 2026-10-28 lo que se migra del almacén a SQL tiene que probarse contra SQL de verdad (ver `servicios/expedientes_sql.py`). Esto
levanta SQL Server 2022 en un contenedor de Docker (solo en 127.0.0.1), le arma la base como la deja el DBA (intercalación de CSI, los
esquemas de `nexusOwner`, `usrNexusDDL` para migrar y `usrNexus` para la API) y le aplica TODAS las migraciones con el corredor de verdad.
NUNCA toca las bases de CSI.

Uso, ANTES de importar nada de la app (`config.py` lee el entorno una sola vez, al importarse):

    import pruebas_sql
    pruebas_sql.preparar("nexus-expedientes-prueba", 14333)   # deja SQLSERVER_* en el entorno y quita el contenedor al salir

Sin Docker, `preparar` termina el proceso con salida 0 y lo dice: la prueba se SALTA, no cuenta como pasada.
"""

import atexit
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path

IMAGEN = "mcr.microsoft.com/mssql/server:2022-latest"
BASE = "IA_Nexus_Dev"
ESQUEMAS = ("documents", "security", "reference", "nexus")


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)


def preparar(contenedor: str, puerto: int, dejar: bool = False) -> dict:
    """Levanta el contenedor, arma la base, aplica las migraciones y deja `SQLSERVER_*` en `os.environ`.

    Devuelve `{"sa", "app", "puerto", "base", "driver"}` por si la prueba quiere asomarse a la base con otro usuario. Con `dejar`, el
    contenedor sigue vivo al terminar (para investigar)."""
    if _docker("info", "--format", "{{.ServerVersion}}").returncode != 0:
        print("Docker no responde: esta prueba se SALTA (no cuenta como pasada). Abre Docker Desktop y vuelve a intentar.")
        raise SystemExit(0)

    import pyodbc

    from migraciones import corredor

    driver = corredor.elegir_driver({}, pyodbc.drivers())
    sa = "Sa-" + secrets.token_urlsafe(12) + "-9!"
    ddl = "Ddl-" + secrets.token_urlsafe(12) + "-9!"
    app = "App-" + secrets.token_urlsafe(12) + "-9!"

    def conectar(usuario: str, clave: str, base: str = "master"):
        return pyodbc.connect(
            f"DRIVER={{{driver}}};SERVER=127.0.0.1,{puerto};DATABASE={base};UID={usuario};PWD={clave};TrustServerCertificate=yes;",
            autocommit=True,
        )

    _docker("rm", "-f", contenedor)
    r = _docker("run", "-d", "--name", contenedor, "-e", "ACCEPT_EULA=Y", "-e", f"MSSQL_SA_PASSWORD={sa}", "-p", f"127.0.0.1:{puerto}:1433", IMAGEN)
    if r.returncode != 0:
        raise SystemExit(f"No se pudo levantar el contenedor: {r.stderr.strip()}")
    if dejar:
        atexit.register(lambda: print(f"\n(el contenedor «{contenedor}» sigue vivo en 127.0.0.1:{puerto}; quítalo con: docker rm -f {contenedor})"))
    else:
        atexit.register(lambda: _docker("rm", "-f", contenedor))

    for _ in range(90):
        try:
            conectar("sa", sa).close()
            break
        except pyodbc.Error:
            time.sleep(2)
    else:
        raise SystemExit(f"SQL Server no contestó en 3 minutos. Revisa `docker logs {contenedor}`.")

    # Lo que deja el DBA.
    con = conectar("sa", sa)
    cur = con.cursor()
    cur.execute(f"CREATE LOGIN usrNexusDDL WITH PASSWORD = '{ddl}', CHECK_POLICY = ON, CHECK_EXPIRATION = OFF")
    cur.execute(f"CREATE LOGIN usrNexus WITH PASSWORD = '{app}', CHECK_POLICY = ON, CHECK_EXPIRATION = OFF")
    cur.execute(f"CREATE DATABASE [{BASE}] COLLATE Modern_Spanish_CI_AS")
    con.close()
    con = conectar("sa", sa, BASE)
    k = con.cursor()
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
    k.execute("DENY CREATE TABLE, CREATE VIEW, CREATE FUNCTION, CREATE PROCEDURE, CREATE TYPE, CREATE SCHEMA, ALTER ANY SCHEMA TO usrNexus")
    con.close()

    # Las migraciones de verdad, con el corredor y el usuario de migraciones.
    carpeta = Path(tempfile.mkdtemp(prefix="pruebas-sql-"))
    env_ddl = carpeta / "ddl.env"
    env_ddl.write_text(
        f"SQLSERVER_HOST=127.0.0.1\nSQLSERVER_PORT={puerto}\nSQLSERVER_USER=usrNexusDDL\nSQLSERVER_PASSWORD={ddl}\nSQLSERVER_DRIVER={driver}\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, "-m", "migraciones", "aplicar", "--base", BASE, "--confirmar"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(Path(__file__).parent),
        env={**os.environ, "NEXUS_DDL_ENV": str(env_ddl)}, timeout=600,
    )
    env_ddl.unlink()
    if r.returncode != 0:
        raise SystemExit(f"El corredor no pudo aplicar las migraciones:\n{(r.stdout + r.stderr).strip()[-1500:]}")

    os.environ.update(
        SQLSERVER_HOST="127.0.0.1", SQLSERVER_PORT=str(puerto), SQLSERVER_DB=BASE,
        SQLSERVER_USER="usrNexus", SQLSERVER_PASSWORD=app, SQLSERVER_TRUST_CERT="yes", SQLSERVER_DRIVER=driver,
    )
    return {"sa": sa, "app": app, "puerto": puerto, "base": BASE, "driver": driver, "conectar": conectar}
