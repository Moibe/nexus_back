"""¿Organizaciones, clasificadores, bandeja y auditoría se comportan IGUAL en SQL (migración 0006) que en el almacén, y la importación única
desde sus JSONL funciona?

    .venv/Scripts/python verificar_registros_sql.py [--dejar]

Levanta el SQL Server desechable de `pruebas_sql.py` hasta la 0005, le mete filas de antes (un tipo documental de una organización y uno de
plataforma), siembra los JSONL como los tiene dev (organizaciones con un cambio de estado y una repetida, un clasificador reasignado y uno de una
organización que no existe, entradas pendientes, pasadas al pipeline y descartadas, eventos de auditoría), aplica la 0006 con el corredor real,
y luego:

1. importa una sola vez y compara cada lectura contra el MISMO módulo leyendo el JSONL (se cambia `SQLSERVER_HOST` del módulo para elegir);
2. hace las mismas escrituras en los dos y compara lo que devuelven (salvo ids y horas);
3. comprueba lo que solo la base cuida: la llave foránea de organización (la plataforma, fuera), y que importar dos veces no duplica.

Sin Docker, se salta y lo dice.
"""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pruebas_sql  # noqa: E402

import pyodbc  # noqa: E402

BASE = pruebas_sql.preparar("nexus-registros-prueba", 14338, dejar="--dejar" in sys.argv, hasta=5)
RAIZ = Path(tempfile.mkdtemp(prefix="verificar-registros-"))
(RAIZ / ".nexus-almacen").touch()
(RAIZ / ".registro").mkdir()
os.environ.update(ALMACEN_RUTA=str(RAIZ), DOCAI_CLASIFICADOR_ID="clasificador-de-plataforma")

fallos: list[str] = []

# `asegurar_todo()` también importa lo de la 0008 (tokens, llaves, webhooks…), cuyas tablas esta prueba todavía no tiene: aquí solo los cuatro de la 0006.
DE_LA_0006 = ("organizaciones", "clasificadores", "bandeja", "auditoria")


def rev(que: str, ok: bool, extra: str = "") -> None:
    print(f"  {'OK   ' if ok else 'FALLA'}  {que}{(' — ' + extra) if extra and not ok else ''}")
    if not ok:
        fallos.append(que)


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


ORG_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
ORG_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
ORG_REPETIDA = "cccccccc-3333-4333-8333-cccccccccccc"
NO_EXISTE = "dddddddd-4444-4444-8444-dddddddddddd"


def escribir(nombre: str, eventos: list[dict]) -> None:
    (RAIZ / ".registro" / nombre).write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in eventos), encoding="utf-8")


def entrada(ident: str, tenant: str, n: int, en: str) -> dict:
    return {"evento": "entrada", "id": ident, "documentoId": f"documento-{n:032x}", "tenant": tenant, "rutaRelativa": f"{tenant}/ab/{n}", "sha256": f"{n:064x}",
            "tamanoBytes": 100 + n, "mime": "application/pdf", "nombreOriginal": f"archivo-{n}.pdf", "canal": "API", "llaveId": None, "recibidoEn": en}


try:
    titulo("0 · Lo que dev ya tenía: filas en SQL hasta la 0005 y los JSONL del almacén")
    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    k = con.cursor()
    for tid, tenant in (("tipo-" + "1" * 32, ORG_A), ("tipo-" + "2" * 32, "plataforma")):
        k.execute("INSERT INTO nexus.documentTypes (publicId, tenant, revision, dataJson, createdBy) VALUES (?, ?, 1, ?, 'x')", tid, tenant, '{"nombre":"INE","estado":"activo"}')
    con.close()
    escribir("tenants.jsonl", [
        {"evento": "creada", "guid": ORG_A, "nombre": "Alfa Seguros", "slug": "alfa-seguros", "codigo": "NEX-00001", "adminGuid": "admin-a", "recuperacion": {"correo": "a@x.com"}, "en": "2026-10-20T10:00:00+00:00"},
        {"evento": "creada", "guid": ORG_B, "nombre": "Alfa Seguros", "slug": "alfa-seguros-1", "codigo": "NEX-00002", "adminGuid": "admin-b", "recuperacion": {}, "en": "2026-10-21T10:00:00+00:00"},
        {"evento": "creada", "guid": ORG_REPETIDA, "nombre": "Repetida", "slug": "alfa-seguros", "codigo": "NEX-00003", "adminGuid": None, "recuperacion": {}, "en": "2026-10-22T10:00:00+00:00"},
        {"evento": "estado", "guid": ORG_B, "estado": "SUSPENDED", "en": "2026-10-23T10:00:00+00:00"},
    ])
    escribir("clasificadores.jsonl", [
        {"evento": "asignado", "tenant": ORG_A, "procesadorId": "proc-viejo", "en": "2026-10-26T10:00:00+00:00"},
        {"evento": "asignado", "tenant": ORG_A, "procesadorId": "proc-actual", "en": "2026-10-27T10:00:00+00:00"},
        {"evento": "asignado", "tenant": NO_EXISTE, "procesadorId": "proc-huerfano", "en": "2026-10-27T10:00:00+00:00"},
    ])
    escribir(f"bandeja-{ORG_A}.jsonl", [
        entrada("a" * 32, ORG_A, 1, "2026-10-28T10:00:00+00:00"),
        entrada("b" * 32, ORG_A, 2, "2026-10-28T10:00:01+00:00"),
        entrada("c" * 32, ORG_A, 3, "2026-10-28T10:00:02+00:00"),
        entrada("d" * 32, ORG_A, 4, "2026-10-28T10:00:03+00:00"),
        {"evento": "retiro", "id": "b" * 32, "motivo": "pipeline", "en": "2026-10-28T11:00:00+00:00"},
        {"evento": "retiro", "id": "c" * 32, "motivo": "descartado", "detalle": "borroso", "en": "2026-10-28T11:00:01+00:00"},
        {"evento": "retiro", "id": "d" * 32, "motivo": "descartado", "en": "2026-10-28T11:00:02+00:00"},
    ])
    escribir("bandeja-plataforma.jsonl", [entrada("e" * 32, "plataforma", 5, "2026-10-28T12:00:00+00:00")])
    from datetime import datetime, timedelta, timezone

    hace = lambda m: (datetime.now(timezone.utc) - timedelta(minutes=m)).isoformat(timespec="seconds")  # noqa: E731
    escribir("auditoria.jsonl", [
        {"evento": "auditoria", "accion": "PASSWORD_RESET_REQUESTED", "usuarioGuid": None, "en": hace(300), "email": "a@x.com"},
        {"evento": "auditoria", "accion": "PASSWORD_RESET_REQUESTED", "usuarioGuid": None, "en": hace(5), "email": "a@x.com"},
        {"evento": "auditoria", "accion": "USER_CREATED", "usuarioGuid": "u-1", "en": hace(4), "tenant": ORG_A, "actor": "admin-a"},
        {"evento": "auditoria", "accion": "CROSS_TENANT_ACCESS", "usuarioGuid": "u-2", "en": hace(3), "tenantPedido": NO_EXISTE},
    ])
    rev("se sembraron los JSONL y dos tipos documentales (de una organización y de plataforma) antes de la 0006", True)

    titulo("1 · La 0006, con el corredor real, sobre una base con filas")
    BASE["aplicar"](6)
    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    k = con.cursor()
    rev("la base dice versión 6", k.execute("SELECT MAX(version) FROM nexus.schemaMigrations").fetchone()[0] == 6)
    rev("el tipo documental de la organización tiene su organizationGuid; el de plataforma, NULL",
        [tuple(r) for r in k.execute("SELECT tenant, organizationGuid FROM nexus.documentTypes ORDER BY documentTypeId").fetchall()] == [(ORG_A, ORG_A), ("plataforma", None)])
    con.close()

    from servicios import auditoria, bandeja, clasificadores, importacion_json, tenants_registro  # noqa: E402

    MODULOS = (tenants_registro, clasificadores, bandeja, auditoria)
    REAL = tenants_registro.SQLSERVER_HOST

    def modo(sql: bool) -> None:
        for m in MODULOS:
            m.SQLSERVER_HOST = REAL if sql else ""

    def ambos(fn):
        modo(False)
        a = fn()
        modo(True)
        s = fn()
        return a, s

    titulo("2 · La importación")
    modo(True)
    [importacion_json.asegurar(n) for n in DE_LA_0006]
    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    k = con.cursor()
    hechos = {r[0]: (r[1], r[2]) for r in k.execute("SELECT registryName, rowsImported, rowsSkipped FROM nexus.jsonImports").fetchall()}
    rev("se importaron los cuatro y se anotó cuántos y cuántos se saltaron",
        hechos == {"organizaciones": (3, 0), "clasificadores": (1, 1), "bandeja": (5, 0), "auditoria": (4, 0)}, str(hechos))
    rev("la que traía un slug repetido NO se pierde: entra con un slug libre y conserva su código",
        tuple(k.execute("SELECT slug, code FROM nexus.tenants WHERE guid = ?", ORG_REPETIDA).fetchone()) == ("alfa-seguros-2", "NEX-00003"))
    importacion_json.olvidar()
    [importacion_json.asegurar(n) for n in DE_LA_0006]
    rev("importar otra vez no duplica nada", k.execute("SELECT (SELECT count(*) FROM nexus.tenants) * 1000 + (SELECT count(*) FROM nexus.inboxEntries)").fetchone()[0] == 3005)
    con.close()

    titulo("3 · Las lecturas, iguales en los dos")

    def igual(que: str, fn) -> None:
        a, s = ambos(fn)
        rev(que, a == s, f"almacén={a!r}\n          sql={s!r}")

    # La repetida entró con otro slug: se compara sin él (todo lo demás, igual).
    igual("organizaciones: la lista", lambda: [{**t, "slug": None} if t["guid"] == ORG_REPETIDA else t for t in tenants_registro.listar()])
    igual("organizaciones: una por su guid, con su estado y su recuperación", lambda: tenants_registro.por_guid(ORG_B))
    def _probar(fn, *args):
        try:
            return ("ok", fn(*args))
        except Exception as e:  # noqa: BLE001
            return ("error", type(e).__name__)

    igual("organizaciones: una que no existe da NoEncontrado", lambda: _probar(tenants_registro.por_guid, NO_EXISTE))
    igual("organizaciones: coincidencias por nombre", lambda: sorted(c["guid"] for c in tenants_registro.coincidencias("Alfa  Seguros")))
    igual("clasificadores: el de una organización (el último asignado)", lambda: clasificadores.id_de(ORG_A))
    igual("clasificadores: el de una organización sin clasificador", lambda: clasificadores.id_de(ORG_B))
    igual("clasificadores: el de plataforma sale del .env", lambda: clasificadores.id_de("plataforma"))
    igual("bandeja: pendientes de la organización", lambda: bandeja.listar_pendientes(ORG_A))
    igual("bandeja: pendientes de plataforma", lambda: bandeja.listar_pendientes("plataforma"))
    igual("bandeja: descartadas, la más reciente primero, con su detalle", lambda: bandeja.listar_descartadas(ORG_A))
    igual("bandeja: datos de una entrada, y de una ajena", lambda: (bandeja.datos_de_entrada(ORG_A, "c" * 32), bandeja.datos_de_entrada("plataforma", "a" * 32)))
    igual("bandeja: la que pasó al pipeline sí, la descartada y la pendiente no", lambda: [bool(bandeja.entrada_en_pipeline(ORG_A, x * 32)) for x in "bca"])
    igual("bandeja: existe en otro tenant", lambda: (bandeja.existe_en_otro_tenant("plataforma", "a" * 32), bandeja.existe_en_otro_tenant(ORG_A, "a" * 32), bandeja.existe_en_otro_tenant(ORG_A, "f" * 32)))
    igual("auditoría: los eventos de una acción, en orden", lambda: auditoria.eventos({"PASSWORD_RESET_REQUESTED"}))
    igual("auditoría: todos", lambda: auditoria.eventos())
    igual("auditoría: el conteo de la última hora (el de hace 5 h no cuenta)", lambda: auditoria.conteo_reciente("PASSWORD_RESET_REQUESTED", "email", "a@x.com", 3600))

    titulo("4 · Las escrituras, iguales en los dos")

    def sin_ids(t: dict) -> dict:
        return {k: v for k, v in t.items() if k not in ("guid", "creadaEn")}

    a, s = ambos(lambda: sin_ids(tenants_registro.crear("  Alfa   Seguros ", "admin-c", {"correo": "c@x.com"})))
    # El slug libre difiere a propósito (en SQL la repetida ya ocupa «-2»); lo demás, igual.
    rev("crear una organización con nombre repetido: un slug libre y el código siguiente",
        {**a, "slug": None} == {**s, "slug": None} and s["codigo"] == "NEX-00004" and s["slug"] == "alfa-seguros-3" and a["slug"] == "alfa-seguros-2", f"{a} | {s}")
    a, s = ambos(lambda: tenants_registro.cambiar_estado(ORG_A, "CLOSED")["estado"])
    rev("cambiar el estado", a == s == "CLOSED")
    a, s = ambos(lambda: _probar(tenants_registro.cambiar_estado, NO_EXISTE, "CLOSED"))
    rev("cambiar el estado de una que no existe: NoEncontrado", a == s == ("error", "NoEncontrado"), f"{a} {s}")

    def asignar_y_leer():
        clasificadores.asignar(ORG_B, "proc-b")
        clasificadores.asignar(ORG_B, "proc-b")
        clasificadores.asignar("plataforma", "no-se-guarda")
        return clasificadores.id_de(ORG_B), clasificadores.id_de("plataforma")

    a, s = ambos(asignar_y_leer)
    rev("asignar un clasificador (dos veces el mismo no hace nada; a plataforma, nada)", a == s == ("proc-b", "clasificador-de-plataforma"), f"{a} {s}")

    def ciclo_bandeja():
        guardado = {"hash": "f" * 64, "rutaRelativa": f"{ORG_A}/ff/1", "bytes": 42}
        e = bandeja.registrar_entrada(ORG_A, guardado, "application/pdf", "nuevo.pdf", "MANUAL")
        primero = bandeja.retirar(ORG_A, e["id"], "descartado", "no sirve")
        segundo = bandeja.retirar(ORG_A, e["id"], "pipeline")
        ajeno = bandeja.retirar("plataforma", "e" * 32, "pipeline") and bandeja.retirar(ORG_A, "e" * 32, "pipeline")
        d = bandeja.listar_descartadas(ORG_A)[0]
        return ({k: v for k, v in e.items() if k not in ("id", "documentoId", "recibidoEn")}, primero, segundo, ajeno, d["nombreOriginal"], d["detalle"])

    a, s = ambos(ciclo_bandeja)
    rev("bandeja: entrar, descartar, no volver a retirar, y el descarte nuevo va primero", a == s and s[1] is True and s[2] is False, f"{a}\n          {s}")

    def auditar():
        auditoria.registrar("PROCESS_CREATED", "u-9", tenant=ORG_A, procesoId="proceso-x", nombre="Validar")
        e = auditoria.eventos({"PROCESS_CREATED"})[-1]
        return {k: v for k, v in e.items() if k != "en"}, auditoria.conteo_reciente("PROCESS_CREATED", segundos=60)

    a, s = ambos(auditar)
    rev("auditoría: anotar y leer de vuelta, igual", a == s, f"{a} | {s}")
    a, s = ambos(lambda: _probar(auditoria.registrar, "NO_EXISTE_ESTA_ACCION"))
    rev("auditoría: una acción desconocida se rechaza igual", a == s == ("error", "ValueError"))

    titulo("5 · Lo que solo la base cuida")
    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    k = con.cursor()

    def rechaza(sql: str, *p) -> bool:
        try:
            k.execute(sql, *p)
            return False
        except pyodbc.Error:
            return True

    rev("un documento de una organización que no existe NO entra (llave foránea)", rechaza(
        "INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel) VALUES (?, ?, ?, 'x', 1, 'x', 'x', 'API')", "documento-" + "9" * 32, NO_EXISTE, "a" * 64))
    rev("…y uno de plataforma sí (plataforma va aparte)", not rechaza(
        "INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel) VALUES (?, ?, ?, 'x', 1, 'x', 'x', 'API')", "documento-" + "8" * 32, "plataforma", "a" * 64))
    rev("un clasificador de una organización que no existe NO entra", rechaza("INSERT INTO nexus.tenantClassifiers (tenant, processorId) VALUES (?, 'p')", NO_EXISTE))
    rev("dos organizaciones con el mismo slug NO entran", rechaza(
        "INSERT INTO nexus.tenants (guid, name, slug, code) VALUES (?, 'x', 'alfa-seguros', 'NEX-09999')", NO_EXISTE))
    rev("un retiro con motivo inventado NO entra", rechaza("UPDATE nexus.inboxEntries SET removedAt = sysutcdatetime(), removalReason = 'perdido' WHERE publicId = ?", "a" * 32))
    con.close()
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print("\nTodo bien." if not fallos else f"\nFALLARON {len(fallos)}:\n  - " + "\n  - ".join(fallos))
raise SystemExit(1 if fallos else 0)
