"""¿Los tipos documentales se comportan IGUAL guardados en SQL Server que guardados en el almacén?

    .venv/Scripts/python verificar_tipos_documentales_sql.py
    .venv/Scripts/python verificar_tipos_documentales_sql.py --dejar   (deja el contenedor vivo para investigar)

NO toca las bases de CSI. Levanta un SQL Server 2022 desechable en un contenedor (127.0.0.1:14332), le arma `IA_Nexus_Dev` con
los permisos que dejó Charlie, le aplica las migraciones `0001` y `0002`, y corre LA MISMA batería de pruebas dos veces: una
contra `tipos_documentales_archivo` (el JSONL de siempre, en una carpeta temporal) y otra contra `tipos_documentales_sql` (la
tabla `nexus.documentTypes`). Al final compara los dos resultados.

Esto es lo que protege de que los dos caminos se separen: desde el 2026-10-28 el producto usa SQL donde hay base y el archivo
donde no (la laptop, las pruebas offline), así que un cambio de comportamiento en uno solo sería un error que únicamente
aparecería en dev — o solo en la laptop.

De paso comprueba algo que no se puede comprobar de otro modo sin desplegar: que `usrNexus`, el usuario de la API, PUEDE leer y
escribir en una tabla recién creada del esquema `nexus` (sus roles son de base entera, así que debería; aquí se ve de verdad).

Sin Docker, se salta y lo dice: no cuenta como pasada.
"""

import json
import os
import shutil
import subprocess
import secrets
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

DEJAR = "--dejar" in sys.argv
CONTENEDOR = "nexus-tipos-prueba"
IMAGEN = "mcr.microsoft.com/mssql/server:2022-latest"
PUERTO = 14332
BASE = "IA_Nexus_Dev"
ESQUEMAS = ("documents", "security", "reference", "nexus")
SA = "Sa-" + secrets.token_urlsafe(12) + "-9!"
DDL = "Ddl-" + secrets.token_urlsafe(12) + "-9!"
APP = "App-" + secrets.token_urlsafe(12) + "-9!"

fallos: list[str] = []

# El entorno se arma ANTES de importar nada de la app: `config.py` lee las variables UNA vez, al importarse, y `db/sqlserver.py`
# se queda con esos valores. Si se pusieran más abajo, el módulo de SQL ya habría nacido creyendo que no hay base.
tmp = Path(tempfile.mkdtemp(prefix="verificar-tipos-sql-"))
RAIZ = tmp / "almacen"
RAIZ.mkdir()
(RAIZ / ".nexus-almacen").touch()
os.environ.update(
    ALMACEN_RUTA=str(RAIZ),
    SQLSERVER_HOST="127.0.0.1", SQLSERVER_PORT=str(PUERTO), SQLSERVER_DB=BASE,
    SQLSERVER_USER="usrNexus", SQLSERVER_PASSWORD=APP, SQLSERVER_TRUST_CERT="yes",
)


def rev(que: str, ok: bool, extra: str = "") -> None:
    print(f"  {'OK   ' if ok else 'FALLA'}  {que}{(' — ' + extra) if extra and not ok else ''}")
    if not ok:
        fallos.append(que)


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


def docker(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)


info = docker("info", "--format", "{{.ServerVersion}}")
if info.returncode != 0:
    print("Docker no responde: esta prueba se SALTA (no cuenta como pasada).")
    print("Para correrla, abre Docker Desktop y vuelve a intentar.")
    raise SystemExit(0)

import pyodbc  # noqa: E402

from migraciones import corredor  # noqa: E402

DRIVER = corredor.elegir_driver({}, pyodbc.drivers())
os.environ["SQLSERVER_DRIVER"] = DRIVER


def conectar(usuario: str, clave: str, base: str = "master", autocommit: bool = True):
    return pyodbc.connect(
        f"DRIVER={{{DRIVER}}};SERVER=127.0.0.1,{PUERTO};DATABASE={base};UID={usuario};PWD={clave};TrustServerCertificate=yes;",
        autocommit=autocommit,
    )


def levantar() -> None:
    docker("rm", "-f", CONTENEDOR)
    r = docker("run", "-d", "--name", CONTENEDOR, "-e", "ACCEPT_EULA=Y", "-e", f"MSSQL_SA_PASSWORD={SA}",
               "-p", f"127.0.0.1:{PUERTO}:1433", IMAGEN)
    if r.returncode != 0:
        raise SystemExit(f"No se pudo levantar el contenedor: {r.stderr.strip()}")
    import time

    for _ in range(90):
        try:
            conectar("sa", SA).close()
            return
        except pyodbc.Error:
            time.sleep(2)
    raise SystemExit(f"SQL Server no contestó en 3 minutos. Revisa `docker logs {CONTENEDOR}`.")


def armar_base() -> None:
    """Lo que el DBA deja listo: la base con la intercalación de CSI, los esquemas de nexusOwner y los dos usuarios."""
    con = conectar("sa", SA)
    cur = con.cursor()
    cur.execute(f"CREATE LOGIN usrNexusDDL WITH PASSWORD = '{DDL}', CHECK_POLICY = ON, CHECK_EXPIRATION = OFF")
    cur.execute(f"CREATE LOGIN usrNexus WITH PASSWORD = '{APP}', CHECK_POLICY = ON, CHECK_EXPIRATION = OFF")
    cur.execute(f"CREATE DATABASE [{BASE}] COLLATE Modern_Spanish_CI_AS")
    con.close()
    con = conectar("sa", SA, BASE)
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


def aplicar_migraciones(tmp: Path) -> tuple[int, str]:
    """Las migraciones de verdad, con el corredor y el usuario de migraciones."""
    env_ddl = tmp / "ddl.env"
    env_ddl.write_text(
        f"SQLSERVER_HOST=127.0.0.1\nSQLSERVER_PORT={PUERTO}\nSQLSERVER_USER=usrNexusDDL\nSQLSERVER_PASSWORD={DDL}\n"
        f"SQLSERVER_DRIVER={DRIVER}\n",
        encoding="utf-8",
    )
    entorno = {**os.environ, "NEXUS_DDL_ENV": str(env_ddl)}
    r = subprocess.run(
        [sys.executable, "-m", "migraciones", "aplicar", "--base", BASE, "--confirmar"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=str(Path(__file__).parent), env=entorno, timeout=600,
    )
    return r.returncode, (r.stdout + r.stderr).strip()


# ── La batería: lo mismo contra cualquiera de los dos almacenamientos ──────────────────────────────────────────────────────

ID_A = "tipo-" + "a" * 32
ID_B = "tipo-" + "b" * 32
ID_C = "tipo-" + "c" * 32
ORG, OTRA = "pruebas-org", "pruebas-otra"
YO, TU = "guid-de-moi", "guid-de-cris"


def datos(nombre: str, estado: str = "activo", **extra) -> dict:
    return {"nombre": nombre, "descripcion": f"Descripción de {nombre}", "estado": estado, "procesadorId": "", **extra}


def bateria(m, comun) -> dict:
    """Corre todo contra el módulo `m` y devuelve lo observado, para comparar un almacenamiento con el otro."""
    obs: dict = {}

    def cae(que, fn) -> str:
        try:
            fn()
            return "no levantó nada"
        except Exception as e:  # noqa: BLE001 — justamente se quiere saber CUÁL
            return type(e).__name__ + (f"({e.revision_actual})" if isinstance(e, comun.Conflicto) else "")

    # Crear
    tipo, previo = m.guardar(ORG, ID_A, datos("INE"), 0, YO)
    obs["crear.revision"] = tipo["revision"]
    obs["crear.previo"] = previo
    obs["crear.creadoPor"] = tipo["creadoPor"]
    obs["crear.modificado_igual_a_creado"] = (tipo["creadoEn"] == tipo["modificadoEn"], tipo["creadoPor"] == tipo["modificadoPor"])
    obs["crear.id_en_datos"] = tipo["datos"].get("id")
    obs["crear.listar"] = [t["id"] for t in m.listar(ORG)]
    obs["crear.no_lo_ve_otra_org"] = m.obtener(OTRA, ID_A)

    # Editar
    tipo2, previo2 = m.guardar(ORG, ID_A, datos("INE 2"), 1, TU)
    obs["editar.revision"] = tipo2["revision"]
    obs["editar.previo_nombre"] = previo2["datos"]["nombre"]
    obs["editar.creado_se_conserva"] = (tipo2["creadoEn"] == tipo["creadoEn"], tipo2["creadoPor"] == YO)
    obs["editar.modificado_por"] = tipo2["modificadoPor"]

    # Choques
    obs["choque.revision_vieja"] = cae("", lambda: m.guardar(ORG, ID_A, datos("X"), 1, YO))
    obs["choque.crear_repetido_propio"] = cae("", lambda: m.guardar(ORG, ID_A, datos("X"), 0, YO))
    obs["choque.crear_repetido_ajeno"] = cae("", lambda: m.guardar(OTRA, ID_A, datos("X"), 0, YO))
    obs["choque.editar_ajeno"] = cae("", lambda: m.guardar(OTRA, ID_A, datos("X"), 2, YO))
    obs["choque.borrar_ajeno"] = cae("", lambda: m.eliminar(OTRA, ID_A, YO))

    # Lo que no es un tipo
    obs["malo.id"] = cae("", lambda: m.guardar(ORG, "nada-que-ver", datos("X"), 0, YO))
    obs["malo.sin_nombre"] = cae("", lambda: m.guardar(ORG, ID_C, {"estado": "activo"}, 0, YO))
    obs["malo.estado"] = cae("", lambda: m.guardar(ORG, ID_C, datos("X", estado="raro"), 0, YO))
    obs["malo.enorme"] = cae("", lambda: m.guardar(ORG, ID_C, datos("X", relleno="z" * 1_100_000), 0, YO))
    obs["malo.revision_negativa"] = cae("", lambda: m.guardar(ORG, ID_C, datos("X"), -1, YO))
    # Lo que pone el servidor no se puede forzar desde los datos.
    forzado, _ = m.guardar(ORG, ID_B, datos("Pasaporte", revision=99, creadoPor="otro", tenant="ajena"), 0, YO)
    obs["malo.reservadas_fuera"] = (forzado["revision"], forzado["creadoPor"], sorted(k for k in forzado["datos"] if k in comun.RESERVADAS))

    # Orden: se respeta, los ajenos se ignoran y los no ordenados van al final
    m.guardar(ORG, ID_C, datos("Acta", estado="borrador"), 0, YO)
    m.ordenar(ORG, [ID_C, ID_A, "tipo-" + "f" * 32], YO)
    obs["orden.respetado"] = [t["id"] for t in m.listar(ORG)]
    obs["orden.no_cambia_modificado"] = m.obtener(ORG, ID_A)["modificadoEn"] == tipo2["modificadoEn"]
    m.ordenar(ORG, [], YO)
    obs["orden.vaciado"] = [t["id"] for t in m.listar(ORG)]

    # El clasificador: solo los activos de ESA organización
    m.guardar(OTRA, "tipo-" + "d" * 32, datos("De la otra"), 0, TU)
    obs["activos.org"] = sorted(t["nombre"] for t in m.tipos_activos(ORG))
    obs["activos.otra"] = sorted(t["nombre"] for t in m.tipos_activos(OTRA))

    # Procesadores con dueño: el vigente, el historial y el que está en edición
    m.guardar(
        ORG, ID_B,
        datos("Pasaporte", procesadorId="proc-vigente", version=3,
              historialVersiones=[{"procesadorId": "proc-viejo", "version": 1}],
              versionEnEdicion={"procesadorId": "proc-edicion", "version": 4}),
        1, YO,
    )
    dueños = m.procesadores_con_dueno()
    obs["procesadores"] = {k: (v["tenant"], v["tipo"], v["version"]) for k, v in sorted(dueños.items())}

    # Borrar
    quitado = m.eliminar(ORG, ID_C, YO)
    obs["borrar.devuelve"] = quitado["datos"]["nombre"]
    obs["borrar.ya_no_esta"] = m.obtener(ORG, ID_C)
    obs["borrar.otra_vez"] = cae("", lambda: m.eliminar(ORG, ID_C, YO))
    obs["borrar.listar"] = sorted(t["id"] for t in m.listar(ORG))
    return obs


# ── Corrida ───────────────────────────────────────────────────────────────────────────────────────────────────────────────

levantado = False
try:
    titulo("1 · El contenedor, la base y las migraciones")
    levantar()
    levantado = True
    armar_base()
    rev("se levantó SQL Server y se armó la base como la deja el DBA", True)
    codigo, salida = aplicar_migraciones(tmp)
    rev(f"el corredor aplicó 0001 y 0002 (salida {codigo})", codigo == 0, salida[-400:])
    if codigo != 0:
        raise SystemExit(1)

    con = conectar("usrNexus", APP, BASE)
    c = con.cursor()
    c.execute("SELECT count(*) FROM nexus.documentTypes")
    rev("usrNexus (el usuario de la API) PUEDE leer la tabla nueva del esquema nexus", c.fetchone()[0] == 0)
    con.close()

    titulo("2 · La batería contra el ALMACÉN (el JSONL de siempre)")
    from servicios import tipos_documentales_archivo, tipos_documentales_comun  # noqa: E402

    obs_archivo = bateria(tipos_documentales_archivo, tipos_documentales_comun)
    rev(f"corrió entera ({len(obs_archivo)} observaciones)", True)

    titulo("3 · La batería contra SQL SERVER (nexus.documentTypes)")
    from servicios import tipos_documentales_sql  # noqa: E402

    obs_sql = bateria(tipos_documentales_sql, tipos_documentales_comun)
    rev(f"corrió entera ({len(obs_sql)} observaciones)", True)

    titulo("4 · ¿Se comportan IGUAL?")
    distintas = [k for k in obs_archivo if obs_archivo[k] != obs_sql.get(k)]
    for k in sorted(obs_archivo):
        if k in distintas:
            rev(f"{k}", False, f"almacén={obs_archivo[k]!r}  sql={obs_sql.get(k)!r}")
    rev(f"las {len(obs_archivo)} observaciones coinciden entre los dos almacenamientos", not distintas)

    titulo("5 · Lo que SOLO la base puede cuidar")
    con = conectar("usrNexus", APP, BASE)
    c = con.cursor()
    c.execute("SELECT publicId, name, status, processorId FROM nexus.documentTypes WHERE publicId = ?", ID_B)
    f = c.fetchone()
    rev("las columnas calculadas salen solas del JSON", tuple(f) == (ID_B, "Pasaporte", "activo", "proc-vigente"), str(tuple(f)))
    try:
        c.execute(
            "INSERT INTO nexus.documentTypes (publicId, tenant, revision, dataJson, createdBy) VALUES (?, ?, 1, ?, ?)",
            ID_A, "colada", '{"nombre":"X","estado":"activo"}', YO,
        )
        con.commit()
        rev("un id repetido NO puede colarse por fuera del servicio", False, "lo aceptó")
    except pyodbc.Error:
        rev("un id repetido NO puede colarse por fuera del servicio (lo impide la base, no Python)", True)
    con.close()
finally:
    if levantado and not DEJAR:
        docker("rm", "-f", CONTENEDOR)
    elif levantado:
        print(f"\n(el contenedor «{CONTENEDOR}» sigue vivo en 127.0.0.1:{PUERTO}; quítalo con: docker rm -f {CONTENEDOR})")
    shutil.rmtree(tmp, ignore_errors=True)

print("\nTodo bien." if not fallos else f"\nFALLARON {len(fallos)}:\n  - " + "\n  - ".join(fallos))
raise SystemExit(1 if fallos else 0)
