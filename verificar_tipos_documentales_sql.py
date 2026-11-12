"""¿Los tipos documentales se comportan en SQL Server como se comportaban guardados en el almacén?

    .venv/Scripts/python verificar_tipos_documentales_sql.py
    .venv/Scripts/python verificar_tipos_documentales_sql.py --dejar   (deja el contenedor vivo para investigar)

NO toca las bases de CSI. Levanta un SQL Server 2022 desechable en un contenedor (127.0.0.1:14332), le arma `IA_Nexus_Dev` con
los permisos que dejó Charlie, le aplica las migraciones `0001` y `0002`, y corre la batería de pruebas contra `tipos_documentales_sql`
(la tabla `nexus.documentTypes`).

Hasta el 2026-11-11 la batería corría DOS veces —contra el JSONL de `tipos_documentales_archivo` y contra SQL— y comparaba: así se cuidaba
que los dos caminos no se separaran. Ese día se quitó el camino JSONL (Moibe: «si házlo») y lo observado, que era idéntico en los dos, se
CONGELÓ en `verificar_tipos_documentales_sql.congelado.json`: ahora se compara contra eso (ver `pruebas_sql.congelado`).

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

    # Nombre único en la organización (2026-11-04): sin distinguir mayúsculas ni espacios de más; los archivados no cuentan; solo se
    # revisa cuando el tipo ENTRA a un nombre (crear, renombrar, desarchivar). A esta altura la organización tiene «INE 2» y «Pasaporte».
    n1, n2, n3, n4, n5, n6 = ("tipo-" + c * 32 for c in "123456")
    m.guardar(ORG, n1, datos("Cédula Profesional", estado="borrador"), 0, YO)
    obs["nombre.crear_repetido"] = cae("", lambda: m.guardar(ORG, n2, datos("  cédula   PROFESIONAL "), 0, YO))
    obs["nombre.el_repetido_no_quedo"] = m.obtener(ORG, n2)
    obs["nombre.sin_acento_es_otro"] = m.guardar(ORG, n2, datos("Cedula Profesional", estado="borrador"), 0, YO)[0]["revision"]
    obs["nombre.renombrar_a_repetido"] = cae("", lambda: m.guardar(ORG, n2, datos("CÉDULA PROFESIONAL", estado="borrador"), 1, YO))
    obs["nombre.sigue_con_el_suyo"] = (m.obtener(ORG, n2)["datos"]["nombre"], m.obtener(ORG, n2)["revision"])
    obs["nombre.sus_propias_mayusculas"] = m.guardar(ORG, n1, datos("cédula  profesional", estado="borrador"), 1, YO)[0]["datos"]["nombre"]
    obs["nombre.otra_org_puede"] = m.guardar(OTRA, n3, datos("Cédula Profesional"), 0, TU)[0]["revision"]
    # Archivar libera el nombre; desarchivar cuando ya lo tomó otro choca; guardar el archivado a secas, no.
    m.guardar(ORG, n1, datos("cédula  profesional", estado="archivado"), 2, YO)
    obs["nombre.archivado_libera"] = m.guardar(ORG, n2, datos("Cédula Profesional", estado="borrador"), 1, YO)[0]["revision"]
    obs["nombre.desarchivar_choca"] = cae("", lambda: m.guardar(ORG, n1, datos("cédula  profesional", estado="borrador"), 3, YO))
    obs["nombre.archivado_se_guarda"] = m.guardar(ORG, n1, datos("cédula  profesional", estado="archivado", descripcion="otra"), 3, YO)[0]["revision"]
    obs["nombre.crear_ya_archivado"] = m.guardar(ORG, n4, datos("Cédula Profesional", estado="archivado"), 0, YO)[0]["revision"]
    obs["nombre.desarchivar_libre"] = m.guardar(ORG, n4, datos("Cédula Profesional Vieja", estado="borrador"), 1, YO)[0]["revision"]
    # Un duplicado de ANTES de la regla (se simula apagándola un momento): se sigue guardando sin tocar su nombre (activarlo, otra
    # descripción, solo mayúsculas), pero no puede pasar a otro nombre ocupado; a uno libre, sí.
    regla = m.exigir_nombre_libre
    m.exigir_nombre_libre = lambda *a, **k: None
    try:
        m.guardar(ORG, n5, datos("Cédula Profesional", estado="borrador"), 0, YO)
    finally:
        m.exigir_nombre_libre = regla
    obs["nombre.duplicado_viejo_existe"] = sorted(t["id"] for t in m.listar(ORG) if t["datos"]["nombre"] == "Cédula Profesional")
    obs["nombre.duplicado_viejo_se_activa"] = m.guardar(ORG, n5, datos("Cédula Profesional", estado="activo"), 1, YO)[0]["revision"]
    obs["nombre.duplicado_viejo_otra_descripcion"] = m.guardar(ORG, n5, datos("Cédula Profesional", descripcion="Nueva"), 2, YO)[0]["revision"]
    obs["nombre.duplicado_viejo_mayusculas"] = m.guardar(ORG, n5, datos("CÉDULA PROFESIONAL"), 3, YO)[0]["revision"]
    obs["nombre.duplicado_viejo_a_otro_ocupado"] = cae("", lambda: m.guardar(ORG, n5, datos("pasaporte"), 4, YO))
    obs["nombre.duplicado_viejo_a_uno_libre"] = m.guardar(ORG, n5, datos("Cédula Profesional (federal)"), 4, YO)[0]["datos"]["nombre"]
    obs["nombre.sigue_ocupado_por_el_otro"] = cae("", lambda: m.guardar(ORG, n6, datos("cédula profesional"), 0, YO))
    return obs


#: Lo que DEBE observarse en el bloque del nombre único (las demás observaciones solo se comparan entre los dos almacenamientos;
#: estas, además, contra lo que pide la regla, para que no puedan estar los dos igual de mal).
ESPERADO_NOMBRE = {
    "nombre.crear_repetido": "NombreRepetido",
    "nombre.el_repetido_no_quedo": None,
    "nombre.sin_acento_es_otro": 1,
    "nombre.renombrar_a_repetido": "NombreRepetido",
    "nombre.sigue_con_el_suyo": ("Cedula Profesional", 1),
    "nombre.sus_propias_mayusculas": "cédula  profesional",
    "nombre.otra_org_puede": 1,
    "nombre.archivado_libera": 2,
    "nombre.desarchivar_choca": "NombreRepetido",
    "nombre.archivado_se_guarda": 4,
    "nombre.crear_ya_archivado": 1,
    "nombre.desarchivar_libre": 2,
    "nombre.duplicado_viejo_existe": sorted(["tipo-" + "2" * 32, "tipo-" + "5" * 32]),
    "nombre.duplicado_viejo_se_activa": 2,
    "nombre.duplicado_viejo_otra_descripcion": 3,
    "nombre.duplicado_viejo_mayusculas": 4,
    "nombre.duplicado_viejo_a_otro_ocupado": "NombreRepetido",
    "nombre.duplicado_viejo_a_uno_libre": "Cédula Profesional (federal)",
    "nombre.sigue_ocupado_por_el_otro": "NombreRepetido",
}


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

    titulo("2 · La batería contra SQL SERVER (nexus.documentTypes)")
    import pruebas_sql  # noqa: E402  (solo por `congelado`: esta prueba levanta su propio contenedor)
    from servicios import tipos_documentales_comun, tipos_documentales_sql  # noqa: E402

    obs_sql = bateria(tipos_documentales_sql, tipos_documentales_comun)
    rev(f"corrió entera ({len(obs_sql)} observaciones)", True)

    titulo("3 · ¿Se comporta como lo congelado (lo que el JSONL y SQL observaban igual el 2026-11-11)?")
    for k in sorted(obs_sql):
        ok, detalle = pruebas_sql.congelado(__file__, k, obs_sql[k])
        rev(k, ok, detalle)

    titulo("4 · El nombre único hace lo que pide la regla")
    for clave, esperado in ESPERADO_NOMBRE.items():
        rev(clave, obs_sql.get(clave) == esperado, f"se observó {obs_sql.get(clave)!r}, se esperaba {esperado!r}")

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
