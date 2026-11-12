"""Un SQL Server DESECHABLE para las pruebas: el ayudante común de todos los `verificar_*` que tocan la base.

Desde el 2026-11-11 el back no tiene otro almacenamiento que SQL Server (ya no hay camino JSONL para cuando no había base), así que toda prueba
que guarde o lea algo del producto corre contra SQL de verdad. Esto levanta SQL Server 2022 en un contenedor de Docker (solo en 127.0.0.1), le
arma la base como la deja el DBA (intercalación de CSI, los esquemas de `nexusOwner`, `usrNexusDDL` para migrar y `usrNexus` para la API) y le
aplica las migraciones con el corredor de verdad. NUNCA toca las bases de CSI.

Uso, ANTES de importar nada de la app (`config.py` lee el entorno una sola vez, al importarse):

    import pruebas_sql
    BASE = pruebas_sql.preparar("nexus-expedientes-prueba", 14333)   # deja SQLSERVER_* en el entorno

## Un contenedor para todas, una base nueva para cada una

Arrancar un SQL Server tarda; recrear una base, unos segundos. Así que todas las pruebas comparten UN contenedor (`nexus-pruebas-sql`, puerto
14399), que se queda encendido entre corridas, y cada corrida BORRA y vuelve a crear `IA_Nexus_Dev` desde cero: ninguna ve lo que dejó la
anterior. Las contraseñas del contenedor se guardan en `%TEMP%/nexus-pruebas-sql.json` (solo sirven en 127.0.0.1). Mientras una prueba
corre, tiene el candado `%TEMP%/nexus-pruebas-sql.lock`: otra que arranque (de esta terminal o de otra) espera su turno en vez de borrarle la
base. Para apagarlo y liberar la memoria: `python pruebas_sql.py --apagar`.

Los argumentos `contenedor` y `puerto` se conservan por compatibilidad y ya no se usan; `dejar` tampoco hace falta (el contenedor se queda).

Sin Docker, `preparar` termina el proceso con salida 0 y lo dice: la prueba se SALTA, no cuenta como pasada.
"""

import atexit
import json
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
CONTENEDOR = "nexus-pruebas-sql"
PUERTO = 14399
_CREDENCIALES = Path(tempfile.gettempdir()) / "nexus-pruebas-sql.json"
_CANDADO = Path(tempfile.gettempdir()) / "nexus-pruebas-sql.lock"
_candado_abierto = None  # el archivo del candado, abierto mientras viva el proceso


def _docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)


def _tomar_candado() -> None:
    """Espera su turno: una sola prueba a la vez usa la base (se suelta solo al terminar el proceso)."""
    global _candado_abierto
    if _candado_abierto is not None:
        return
    f = open(_CANDADO, "a+b")
    espera = time.time()
    while True:
        try:
            if os.name == "nt":
                import msvcrt

                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() - espera > 3600:
                raise SystemExit("Otra prueba lleva más de una hora con la base de pruebas: revisa si quedó colgada.")
            time.sleep(1)
    if time.time() - espera > 2:
        print(f"  (esperé {time.time() - espera:.0f} s a que otra prueba soltara la base)")
    _candado_abierto = f


def _conectar_con(driver: str, usuario: str, clave: str, base: str = "master"):
    import pyodbc

    return pyodbc.connect(
        f"DRIVER={{{driver}}};SERVER=127.0.0.1,{PUERTO};DATABASE={base};UID={usuario};PWD={clave};TrustServerCertificate=yes;",
        autocommit=True,
    )


def _contenedor_vivo(driver: str) -> dict | None:
    """Las credenciales del contenedor compartido si está encendido y contesta; `None` si hay que levantarlo."""
    if not _CREDENCIALES.exists():
        return None
    estado = _docker("inspect", "-f", "{{.State.Running}}", CONTENEDOR)
    if estado.returncode != 0 or estado.stdout.strip() != "true":
        return None
    try:
        claves = json.loads(_CREDENCIALES.read_text(encoding="utf-8"))
        _conectar_con(driver, "sa", claves["sa"]).close()
        return claves
    except Exception:  # noqa: BLE001
        return None


def _levantar(driver: str) -> dict:
    """Un contenedor nuevo, con los logins del DBA; guarda sus contraseñas para las corridas siguientes."""
    import pyodbc

    claves = {
        "sa": "Sa-" + secrets.token_urlsafe(12) + "-9!",
        "ddl": "Ddl-" + secrets.token_urlsafe(12) + "-9!",
        "app": "App-" + secrets.token_urlsafe(12) + "-9!",
    }
    _docker("rm", "-f", CONTENEDOR)
    r = _docker("run", "-d", "--name", CONTENEDOR, "-e", "ACCEPT_EULA=Y", "-e", f"MSSQL_SA_PASSWORD={claves['sa']}", "-p", f"127.0.0.1:{PUERTO}:1433", IMAGEN)
    if r.returncode != 0:
        raise SystemExit(f"No se pudo levantar el contenedor: {r.stderr.strip()}")
    for _ in range(90):
        try:
            _conectar_con(driver, "sa", claves["sa"]).close()
            break
        except pyodbc.Error:
            time.sleep(2)
    else:
        raise SystemExit(f"SQL Server no contestó en 3 minutos. Revisa `docker logs {CONTENEDOR}`.")
    con = _conectar_con(driver, "sa", claves["sa"])
    cur = con.cursor()
    cur.execute(f"CREATE LOGIN usrNexusDDL WITH PASSWORD = '{claves['ddl']}', CHECK_POLICY = ON, CHECK_EXPIRATION = OFF")
    cur.execute(f"CREATE LOGIN usrNexus WITH PASSWORD = '{claves['app']}', CHECK_POLICY = ON, CHECK_EXPIRATION = OFF")
    con.close()
    _CREDENCIALES.write_text(json.dumps(claves), encoding="utf-8")
    return claves


def _base_nueva(driver: str, sa: str) -> None:
    """Borra la base de la corrida anterior (si quedó) y la arma otra vez como la deja el DBA."""
    con = _conectar_con(driver, "sa", sa)
    cur = con.cursor()
    cur.execute(f"IF DB_ID('{BASE}') IS NOT NULL BEGIN ALTER DATABASE [{BASE}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{BASE}]; END")
    cur.execute(f"CREATE DATABASE [{BASE}] COLLATE Modern_Spanish_CI_AS")
    con.close()
    con = _conectar_con(driver, "sa", sa, BASE)
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


def preparar(contenedor: str = CONTENEDOR, puerto: int = PUERTO, dejar: bool = False, hasta: int | None = None) -> dict:
    """Una base de pruebas recién hecha, con las migraciones aplicadas, y `SQLSERVER_*` en `os.environ`.

    Devuelve `{"sa", "app", "puerto", "base", "driver", "conectar", "aplicar"}` por si la prueba quiere asomarse a la base con otro
    usuario. Con `hasta`, solo aplica hasta esa versión, y `aplicar(None)` aplica después las que falten: así se prueba una migración sobre
    una base que ya tiene datos de la anterior. `contenedor`, `puerto` y `dejar` ya no se usan (ver el encabezado)."""
    if _docker("info", "--format", "{{.ServerVersion}}").returncode != 0:
        print("Docker no responde: esta prueba se SALTA (no cuenta como pasada). Abre Docker Desktop y vuelve a intentar.")
        raise SystemExit(0)

    import pyodbc

    from migraciones import corredor

    _tomar_candado()
    driver = corredor.elegir_driver({}, pyodbc.drivers())
    claves = _contenedor_vivo(driver) or _levantar(driver)
    _base_nueva(driver, claves["sa"])
    atexit.register(_soltar_base, driver, claves["sa"])

    def conectar(usuario: str, clave: str, base: str = "master"):
        return _conectar_con(driver, usuario, clave, base)

    def aplicar(hasta_version: int | None) -> None:
        """Las migraciones de verdad, con el corredor y el usuario de migraciones."""
        carpeta = Path(tempfile.mkdtemp(prefix="pruebas-sql-"))
        env_ddl = carpeta / "ddl.env"
        env_ddl.write_text(
            f"SQLSERVER_HOST=127.0.0.1\nSQLSERVER_PORT={PUERTO}\nSQLSERVER_USER=usrNexusDDL\nSQLSERVER_PASSWORD={claves['ddl']}\nSQLSERVER_DRIVER={driver}\n",
            encoding="utf-8",
        )
        orden = [sys.executable, "-m", "migraciones", "aplicar", "--base", BASE, "--confirmar"]
        if hasta_version is not None:
            orden += ["--hasta", str(hasta_version)]
        r = subprocess.run(
            orden, capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(Path(__file__).parent),
            env={**os.environ, "NEXUS_DDL_ENV": str(env_ddl)}, timeout=600,
        )
        env_ddl.unlink()
        if r.returncode != 0:
            raise SystemExit(f"El corredor no pudo aplicar las migraciones:\n{(r.stdout + r.stderr).strip()[-1500:]}")

    aplicar(hasta)
    os.environ.update(
        SQLSERVER_HOST="127.0.0.1", SQLSERVER_PORT=str(PUERTO), SQLSERVER_DB=BASE,
        SQLSERVER_USER="usrNexus", SQLSERVER_PASSWORD=claves["app"], SQLSERVER_TRUST_CERT="yes", SQLSERVER_DRIVER=driver,
    )
    return {"sa": claves["sa"], "app": claves["app"], "puerto": PUERTO, "base": BASE, "driver": driver, "conectar": conectar, "aplicar": aplicar}


_RE_FECHA = None


def _normal(valor):
    """El valor como lo guarda JSON (tuplas → listas, fechas → texto) y con cada fecha ISO cambiada por `<fecha>`: lo que sale de una corrida
    se puede comparar con lo que salió en otra, aunque las fechas de las semillas se calculen desde «ahora»."""
    import re

    global _RE_FECHA
    if _RE_FECHA is None:
        _RE_FECHA = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?([+-]\d{2}:\d{2}|Z)?")

    def limpiar(x):
        if isinstance(x, str):
            # El hash argon2id lleva su sal al azar: cambia en cada corrida aunque la contraseña sea la misma.
            return re.sub(r"\$argon2id\$\S+", "<hash>", _RE_FECHA.sub("<fecha>", x))
        if isinstance(x, list):
            return [limpiar(v) for v in x]
        if isinstance(x, dict):
            return {k: limpiar(v) for k, v in x.items()}
        return x

    return limpiar(json.loads(json.dumps(valor, default=str, ensure_ascii=False)))


def congelado(prueba: str, clave: str, valor) -> tuple[bool, str]:
    """Compara `valor` con lo que esa prueba dejó CONGELADO para `clave` en `<prueba>.congelado.json`, junto al archivo de la prueba.

    Es lo que quedó de las comparaciones «el almacén contesta lo mismo que SQL»: el 2026-11-11 (commit 61aea2b) las dos salidas eran idénticas
    en cada comprobación, y al quitar el camino JSONL esas salidas se congelaron. Con `--congelar` en la línea de comandos se (re)escribe en vez
    de comparar: hacerlo solo cuando el cambio de comportamiento sea a propósito, y revisar el diff del archivo. Devuelve `(igual, detalle)`."""
    ruta = Path(prueba).with_suffix(".congelado.json")
    guardado = json.loads(ruta.read_text(encoding="utf-8")) if ruta.exists() else {}
    actual = _normal(valor)
    if "--congelar" in sys.argv:
        guardado[clave] = actual
        ruta.write_text(json.dumps(guardado, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        return True, ""
    if clave not in guardado:
        return False, f"no hay nada congelado para «{clave}» en {ruta.name} (corre la prueba con --congelar)"
    return guardado[clave] == actual, f"congelado={str(guardado[clave])[:300]!r}\n          ahora={str(actual)[:300]!r}"


def volcado(base: dict) -> str:
    """TODO lo que hay en las tablas del esquema `nexus`, como texto: para comprobar que algo (una contraseña, un token en claro) no quedó
    escrito en NINGUNA parte de la base. Es lo que antes se hacía leyendo los JSONL del almacén."""
    con = base["conectar"]("sa", base["sa"], base["base"])
    try:
        cur = con.cursor()
        tablas = [f[0] for f in cur.execute("SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID('nexus') ORDER BY name").fetchall()]
        partes = []
        for t in tablas:
            for fila in cur.execute(f"SELECT * FROM nexus.[{t}]").fetchall():
                partes.append(f"{t}: " + " | ".join("" if v is None else str(v) for v in fila))
        return "\n".join(partes)
    finally:
        con.close()


def _soltar_base(driver: str, sa: str) -> None:
    """Al terminar, la base de la corrida se borra (el contenedor se queda para la siguiente)."""
    try:
        con = _conectar_con(driver, "sa", sa)
        con.cursor().execute(f"IF DB_ID('{BASE}') IS NOT NULL BEGIN ALTER DATABASE [{BASE}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE; DROP DATABASE [{BASE}]; END")
        con.close()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    if "--apagar" in sys.argv:
        _docker("rm", "-f", CONTENEDOR)
        _CREDENCIALES.unlink(missing_ok=True)
        print(f"Contenedor {CONTENEDOR} apagado y borrado.")
    else:
        print(__doc__)
