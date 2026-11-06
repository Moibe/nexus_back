"""¿Los expedientes y sus documentos funcionan de punta a punta? (documento = nace al entrar; expediente = conjunto; muchos a muchos)

    .venv/Scripts/python verificar_expedientes.py

OFFLINE y sin tocar nada tuyo, como los demás `verificar_*`: el almacén es una carpeta temporal.

Lo que se prueba:

- un documento NACE al entrar por la bandeja, queda en el «void» (sin ningún expediente), conserva lo que se sabe de cómo llegó, sobrevive a
  que su entrada se retire de la bandeja, y subir dos veces el mismo archivo da dos documentos;
- un expediente se crea con un nombre limpio (y dos pueden llamarse igual); los nombres que no sirven se rechazan;
- un documento puede estar en VARIOS expedientes a la vez; meterlo dos veces no repite nada; sacarlo de su último expediente lo devuelve
  al void sin borrarlo; el orden es el de entrada;
- lo de otra organización responde como si no existiera (404) y su tenant, 403 `sin_acceso`;
- crear, meter y sacar piden un rol que organice (Compliance, Auditor y Viewer solo leen) y cada rechazo deja `ROLE_DENIED`;
- crear, meter y sacar dejan rastro con quién; meter un documento que ya estaba, no;
- SUBIR un archivo directo a un expediente (`POST /expedientes/{id}/documentos`, 2026-10-30): nace como carga manual y entra al expediente en una
  sola llamada; solo lo hace quien organiza y todo se rechaza ANTES de guardar (formato, vacío, expediente ajeno o inexistente, tope): nunca deja un
  documento suelto; desde el 2026-11-02 también intenta clasificarlo (aquí, sin clasificador, la respuesta dice que no se clasificó);
- el tope de documentos por expediente, un corte a media escritura y dos peticiones simultáneas;
- (Moibe, 2026-11-05) el expediente ya NO sabe de tipos de expediente ni de expedientes virtuales (se quitaron: sus rutas dan 404, su detalle no trae
  `expedientesVirtuales`) y calcula sus DUPLICADOS al leer: `duplicado` en cada documento y `duplicados` en el expediente entero (con `--sql`, que es donde
  un documento tiene tipo);
- (Moibe, 2026-11-06) tampoco vive en un GRUPO (se quitaron): crear pide solo `{tenant, nombre}` y un `grupoId` que todavía llegue de un cliente viejo se IGNORA
  (con o sin base, de cualquier forma: el expediente se crea igual y sin `grupo`); ni la lista, ni el detalle, ni la respuesta de crear traen `grupo`;
  `CASE_CREATED` no anota grupo; `/grupos…` y `PUT /expedientes/{id}/grupo` dan 404 y el servicio ya no tiene con qué atenderlos; con `--sql`, la base ya
  no tiene la tabla de grupos ni la columna del expediente (migración `0013`);
- (con `--sql`, Document AI falseado) subir un archivo a un expediente lo CLASIFICA y guarda lo que dijo el clasificador, sin meterlo a ningún lado:
  `clasificado`, `sin_tipo`, `sin_clasificador` y `no_clasificado` (falla, lento, sin base), lo rechazado no gasta, y `/ia/clasificar` con `documento` guarda la
  misma clasificación;
- (con `--sql`, Document AI falseado, sección 23) subir un archivo a un expediente también EXTRAE sus datos si el clasificador reconoció un tipo ACTIVO con
  procesador, y nada más (Moibe, 2026-11-06: se quitó el paso «Calibrar» de los tipos documentales, así que un tipo activo con procesador extrae aunque no tenga
  `calibradoEn` —sin la llave, nulo o vacío— y los de antes que sí lo traen también): cada estado de `extraccion` (`extraido`, `no_reconocido`, `error`,
  `no_extraido`, `sin_tipo` y `sin_procesador`; `sin_calibrar` ya no existe) con lo que queda en la base y en `/datos`, que Google solo se llame con el procesador
  y la versión DEL TIPO y los bytes del archivo y nunca sin tipo ni sin procesador, que un tipo archivado en el aire no se extraiga, que la corrida quede en la
  organización de quien sube, subidas seguidas y a la vez, y que lo rechazado, la bandeja y la carga manual no extraigan.

Con `--sql` corre LO MISMO contra SQL Server (migración `0003`, en un contenedor desechable: ver `pruebas_sql.py`), que es donde viven los documentos y
los expedientes en dev, staging y producción desde el 2026-10-29. Las comprobaciones que leen el JSONL a mano se saltan ahí (no hay archivo) y
se agregan las que solo la base puede cuidar (secciones 19 a 21 y 23: lo que la base impide, los duplicados, y la clasificación y la extracción al subir).

    .venv/Scripts/python verificar_expedientes.py --sql
"""

import json
import os
import shutil
import sys
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
SQL = "--sql" in sys.argv
if SQL:
    # ANTES de importar la app: `config.py` lee el entorno una sola vez.
    import pruebas_sql

    BASE_SQL = pruebas_sql.preparar("nexus-expedientes-prueba", 14333, dejar="--dejar" in sys.argv)

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-expedientes-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import auth, bandeja, documentos, expedientes, llaves_cliente, roles, usuarios  # noqa: E402

fallos = 0
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + extra if extra and not ok else ''}")
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


def cab(bearer: str | None = None, servicio: bool = True) -> dict:
    return {**({"x-api-key": SERVICIO} if servicio else {}), **({"Authorization": f"Bearer {bearer}"} if bearer else {})}


c = TestClient(app)


def entrar(correo: str, contrasena: str) -> dict:
    r = c.post("/auth/login", json={"email": correo, "password": contrasena, "ip": "127.0.0.1", "userAgent": "verificar"}, headers=cab())
    assert r.status_code == 200, r.text
    return r.json()


def con_primer_acceso(correo: str, temporal: str, definitiva: str) -> str:
    s = entrar(correo, temporal)
    r = c.post("/auth/contrasena", json={"nueva": definitiva, "confirmacion": definitiva}, headers=cab(s["accessToken"]))
    assert r.status_code == 200, r.text
    return entrar(correo, definitiva)["accessToken"]


def crear_organizacion(bearer_super: str, nombre: str, correo: str, contrasena: str) -> tuple[str, str]:
    r = c.post("/organizaciones/", json={"nombre": nombre, "adminNombre": "Admin", "adminApellidoPaterno": nombre, "adminEmail": correo}, headers=cab(bearer_super))
    assert r.status_code == 201, r.text
    return r.json()["organizacion"]["guid"], con_primer_acceso(correo, r.json()["contrasenaTemporal"], contrasena)


def pdf(n: int) -> bytes:
    return PDF + f"%{n}\n".encode()


def subir(bearer: str, tenant: str, nombre: str, contenido: bytes):
    return c.post("/bandeja/", files={"archivo": (nombre, contenido, "application/pdf")}, data={"tenant": tenant}, headers=cab(bearer))


def crear(bearer: str, tenant: str, nombre):
    """Crea un expediente: solo lleva organización y nombre (sin tipo desde el 2026-10-29 y sin grupo desde el 2026-11-06)."""
    return c.post("/expedientes/", json={"tenant": tenant, "nombre": nombre}, headers=cab(bearer))


def crear_tipo_documental(bearer: str, tenant: str, nombre: str) -> str:
    """Un tipo documental en la biblioteca de esa organización."""
    tid = "tipo-" + uuid.uuid4().hex
    r = c.put(f"/tipos-documentales/{tid}", json={"tenant": tenant, "revision": 0, "datos": {"nombre": nombre, "descripcion": "", "estado": "activo", "procesadorId": ""}}, headers=cab(bearer))
    assert r.status_code == 200, r.text
    return tid


def listar(bearer: str, tenant: str):
    return c.get("/expedientes/", params={"tenant": tenant}, headers=cab(bearer))


def ver(bearer: str, tenant: str, exp: str):
    return c.get(f"/expedientes/{exp}", params={"tenant": tenant}, headers=cab(bearer))


def meter(bearer: str, tenant: str, exp: str, doc: str):
    return c.put(f"/expedientes/{exp}/documentos/{doc}", params={"tenant": tenant}, headers=cab(bearer))


def sacar(bearer: str, tenant: str, exp: str, doc: str):
    return c.delete(f"/expedientes/{exp}/documentos/{doc}", params={"tenant": tenant}, headers=cab(bearer))


def docs(bearer: str, tenant: str, sin: bool | None = None):
    params = {"tenant": tenant, **({"sin_expediente": str(sin).lower()} if sin is not None else {})}
    return c.get("/expedientes/documentos", params=params, headers=cab(bearer))


def ids_docs(bearer: str, tenant: str, sin: bool | None = None) -> list[str]:
    return [d["id"] for d in docs(bearer, tenant, sin).json()["documentos"]]


def ids_docs_de(bearer: str, tenant: str, exp: str) -> list[str]:
    return [d["id"] for d in ver(bearer, tenant, exp).json()["documentos"]]


def auditoria(accion: str) -> list[dict]:
    # Por el servicio y no leyendo el JSONL: con --sql la bitácora vive en nexus.auditEvents (migración 0006).
    from servicios import auditoria as bitacora

    return bitacora.eventos({accion})


ORGANIZAN = ["SUPERVISOR", "ANALISTA", "OPERADOR"]
SOLO_LEEN = ["COMPLIANCE_OFFICER", "AUDITOR", "VIEWER"]

try:
    titulo("0 · Montaje: una organización con una persona de cada rol y otra ajena")
    temporal_super = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal_super))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal_super, "Cobre-Lento-Azul-47!")
    org_a, a_admin = crear_organizacion(bearer_super, "Alfa", "rita@ejemplo.com", "Rumbo-Sereno-Azul-47!")
    org_b, b_admin = crear_organizacion(bearer_super, "Beta", "beto@ejemplo.com", "Rumbo-Sereno-Rojo-48!")
    guid_rita = usuarios.para_login("rita@ejemplo.com")["guid"]
    P = {}
    for rol in ORGANIZAN + SOLO_LEEN:
        correo = f"{rol.lower()}@ejemplo.com"
        r = c.post("/usuarios/", json={"nombre": rol.title(), "apellidoPaterno": "Prueba", "email": correo, "rol": rol}, headers=cab(a_admin))
        assert r.status_code == 201, r.text
        P[rol] = con_primer_acceso(correo, r.json()["contrasenaTemporal"], "Cielo-Abierto-Verde-52!")
    rev("hay dos organizaciones y una persona por cada uno de los 6 roles", org_a != org_b and all(P.values()) and len(P) == 6)

    titulo("1 · Un documento NACE al entrar, y nace en el void")
    r1 = subir(a_admin, org_a, "acta.pdf", pdf(1))
    r2 = subir(a_admin, org_a, "ine.pdf", pdf(2))
    r3 = subir(a_admin, org_a, "comprobante.pdf", pdf(3))
    r4 = subir(a_admin, org_a, "acta-copia.pdf", pdf(1))
    rev("subir a la bandeja sigue respondiendo 201 (nada cambió para quien sube)", [r.status_code for r in (r1, r2, r3, r4)] == [201] * 4, str([r.text[:80] for r in (r1, r2, r3, r4)]))
    d1, d2, d3, d4 = (r.json()["documentoId"] for r in (r1, r2, r3, r4))
    rev("la entrada trae el `documentoId` del documento que nació", all(x.startswith("documento-") and len(x) == 10 + 32 for x in (d1, d2, d3, d4)), str((d1, d2)))
    rev("cuatro entradas, cuatro documentos distintos", len({d1, d2, d3, d4}) == 4)
    r = docs(a_admin, org_a)
    rev("la ruta de documentos responde 200 con la clave `documentos` (no se confunde con el id de un expediente)", r.status_code == 200 and "documentos" in r.json(), r.text[:200])
    lista = r.json()["documentos"]
    rev("están los cuatro, del más viejo al más nuevo", [d["id"] for d in lista] == [d1, d2, d3, d4], str([d["nombreOriginal"] for d in lista]))
    rev("ninguno está en un expediente: nacen en el void", all(d["expedientes"] == [] for d in lista))
    uno = lista[0]
    rev("conserva lo que se sabe de cómo llegó", uno["nombreOriginal"] == "acta.pdf" and uno["canal"] == "API" and uno["mime"] == "application/pdf" and uno["tamanoBytes"] == len(pdf(1)) and uno["tenant"] == org_a and uno["llaveId"] is None and uno["recibidoEn"], str(uno))
    rev("y dónde están sus bytes", uno["rutaRelativa"].startswith(org_a + "/") and len(uno["sha256"]) == 64, str(uno))
    rev("el mismo archivo subido dos veces son DOS documentos con los mismos bytes", lista[0]["id"] != lista[3]["id"] and lista[0]["sha256"] == lista[3]["sha256"] and lista[0]["rutaRelativa"] == lista[3]["rutaRelativa"])
    rev("`sin_expediente=true` devuelve el void completo y `false` nada", ids_docs(a_admin, org_a, True) == [d1, d2, d3, d4] and ids_docs(a_admin, org_a, False) == [])
    entradas = c.get("/bandeja/", params={"tenant": org_a}, headers=cab(a_admin)).json()["entradas"]
    rev("la bandeja las lista con su `documentoId`, en el mismo orden", [e.get("documentoId") for e in entradas] == [d1, d2, d3, d4], str([e.get("documentoId") for e in entradas]))
    r = c.post(f"/bandeja/{entradas[1]['id']}/retirar", data={"tenant": org_a, "motivo": "pipeline"}, headers=cab(a_admin))
    rev("retirar una entrada de la bandeja (pasó al pipeline): 200", r.status_code == 200, r.text[:200])
    rev("el documento sigue existiendo: la bandeja es pasajera y el documento no", ids_docs(a_admin, org_a) == [d1, d2, d3, d4])
    secreto, llave = llaves_cliente.emitir(org_a, "Cliente de prueba", "Para la prueba", 7)
    r = c.post("/bandeja/", files={"archivo": ("cliente.pdf", pdf(9), "application/pdf")}, headers={"X-API-Key": secreto})
    rev("un cliente que sube con su API Key también crea un documento", r.status_code == 201 and r.json().get("documentoId", "").startswith("documento-"), r.text[:200])
    d_cliente = next(d for d in docs(a_admin, org_a).json()["documentos"] if d["nombreOriginal"] == "cliente.pdf")
    rev("y queda con la llave con la que llegó", d_cliente["llaveId"] == llave["id"] and d_cliente["tenant"] == org_a, str(d_cliente))
    rev("la otra organización no ve ninguno", ids_docs(b_admin, org_b) == [])
    rev("`documentos.CANALES` y `bandeja.CANALES` son el mismo conjunto", set(documentos.CANALES) == set(bandeja.CANALES))
    try:
        documentos.nacer(org_a, {"hash": "a" * 64, "rutaRelativa": "x", "bytes": 1}, "application/pdf", "x.pdf", "FAX")
        rev("un canal inventado no crea un documento", False)
    except ValueError:
        rev("un canal inventado no crea un documento", True)
    manual = documentos.nacer("verificacion-manual", {"hash": "b" * 64, "rutaRelativa": "verificacion-manual/bb/bb/x", "bytes": 7}, "image/png", "foto.png", "MANUAL")
    rev("un documento de canal MANUAL conserva su canal (y sin llave)", manual["canal"] == "MANUAL" and manual["llaveId"] is None and documentos.listar("verificacion-manual") == [manual], str(manual))
    d_cliente_id = d_cliente["id"]

    titulo("2 · Crear un expediente")
    r = crear(a_admin, org_a, "  Solicitud   de  crédito ")
    rev("crear: 201", r.status_code == 201, r.text[:200])
    e1 = r.json()
    rev("el nombre sale limpio (espacios de más fuera) y lo demás lo pone el servidor", e1["nombre"] == "Solicitud de crédito" and e1["id"].startswith("expediente-") and len(e1["id"]) == 11 + 32 and e1["creadoPor"] == guid_rita and e1["creadoEn"] and e1["totalDocumentos"] == 0, str(e1))
    rev("el expediente NO trae tipo: es solo un receptáculo", "tipo" not in e1, str(e1))
    rev("ni grupo (se quitaron el 2026-11-06): la respuesta de crear no trae `grupo`, con o sin base", "grupo" not in e1 and "grupoId" not in e1 and "grupoNombre" not in e1, str(e1))
    e1 = e1["id"]
    e2 = crear(a_admin, org_a, "Constitución de sociedad").json()["id"]
    e3 = crear(a_admin, org_a, "Solicitud de crédito").json()["id"]
    rev("dos expedientes pueden llamarse igual (son ids distintos)", e3 != e1)
    lista = listar(a_admin, org_a).json()["expedientes"]
    rev("la lista los trae en el orden de su creación, vacíos", [e["id"] for e in lista] == [e1, e2, e3] and all(e["totalDocumentos"] == 0 for e in lista), str(lista))
    for malo, que in (("", "vacío"), ("   ", "solo espacios"), ("x" * 201, "de 201 caracteres"), ("a\u0000b", "con un carácter de control")):
        r = crear(a_admin, org_a, malo)
        rev(f"un nombre {que} se rechaza (400)", r.status_code == 400, f"{r.status_code} {r.text[:100]}")
    rev("un nombre que ni es texto se rechaza", crear(a_admin, org_a, 123).status_code in (400, 422))
    rev("ningún expediente de la lista trae tipo", all("tipo" not in e for e in lista), str(lista[:1]))
    rev("ni grupo", all("grupo" not in e and "grupoId" not in e and "grupoNombre" not in e for e in lista), str(lista[:1]))
    r = c.post("/expedientes/", json={"tenant": org_a, "nombre": "Con un tipo de los de antes", "tipoId": "tipo-expediente-" + "a" * 32, "grupoId": "grupo-" + "a" * 32}, headers=cab(a_admin))
    rev("un cliente viejo que todavía manda `tipoId` y `grupoId` no rompe nada: el expediente se crea, sin tipo y sin grupo", r.status_code == 201 and "tipo" not in r.json() and "grupo" not in r.json(), r.text[:150])
    rev("uno de exactamente 200 caracteres sí cabe", crear(a_admin, org_a, "x" * 200).status_code == 201)
    rev("lo rechazado no creó nada (los tres de antes, el del cliente viejo y el de 200)", len(listar(a_admin, org_a).json()["expedientes"]) == 5)
    rev("el expediente aparece entero al pedirlo por su id", ver(a_admin, org_a, e1).status_code == 200 and ver(a_admin, org_a, e1).json()["documentos"] == [])

    titulo("3 · Un documento en varios expedientes, y meterlo dos veces no repite nada")
    r = meter(a_admin, org_a, e1, d1)
    rev("meter un documento: 200, `agregado: true`", r.status_code == 200 and r.json()["agregado"] is True, r.text[:200])
    rev("y devuelve el expediente con ese documento adentro", [d["id"] for d in r.json()["expediente"]["documentos"]] == [d1] and r.json()["expediente"]["totalDocumentos"] == 1, r.text[:300])
    meter(a_admin, org_a, e1, d2)
    rev("un segundo documento va después del primero (orden de entrada)", ids_docs_de(a_admin, org_a, e1) == [d1, d2])
    antes = len(auditoria("CASE_DOCUMENT_ADDED"))
    r = meter(a_admin, org_a, e1, d1)
    rev("meter el que ya estaba: 200, `agregado: false`, nada cambia", r.status_code == 200 and r.json()["agregado"] is False and ids_docs_de(a_admin, org_a, e1) == [d1, d2], r.text[:200])
    rev("y no repite el rastro en la bitácora", len(auditoria("CASE_DOCUMENT_ADDED")) == antes)
    r = meter(a_admin, org_a, e2, d1)
    rev("el MISMO documento en un segundo expediente: 200, `agregado: true`", r.status_code == 200 and r.json()["agregado"] is True, r.text[:200])
    rev("sigue en el primero y está en el segundo", ids_docs_de(a_admin, org_a, e1) == [d1, d2] and ids_docs_de(a_admin, org_a, e2) == [d1])
    todos = {d["id"]: d for d in docs(a_admin, org_a).json()["documentos"]}
    rev("el documento dice en qué expedientes está (id y nombre, en el orden de creación)", todos[d1]["expedientes"] == [{"id": e1, "nombre": "Solicitud de crédito"}, {"id": e2, "nombre": "Constitución de sociedad"}], str(todos[d1]["expedientes"]))
    rev("y el que está en uno solo, en uno solo; el que no está en ninguno, en ninguno", [x["id"] for x in todos[d2]["expedientes"]] == [e1] and todos[d3]["expedientes"] == [])
    rev("`sin_expediente=true` es el void (d3, d4 y el del cliente) y `false` los organizados (d1, d2)", ids_docs(a_admin, org_a, True) == [d3, d4, d_cliente_id] and ids_docs(a_admin, org_a, False) == [d1, d2], f"{ids_docs(a_admin, org_a, True)} {ids_docs(a_admin, org_a, False)}")
    rev("el duplicado de d1 (mismos bytes) sigue en el void: es otro documento", d4 in ids_docs(a_admin, org_a, True))
    rev("la lista de expedientes cuenta sus documentos", {e["id"]: e["totalDocumentos"] for e in listar(a_admin, org_a).json()["expedientes"]}.get(e1) == 2 and {e["id"]: e["totalDocumentos"] for e in listar(a_admin, org_a).json()["expedientes"]}.get(e2) == 1)
    det = ver(a_admin, org_a, e1).json()["documentos"][0]
    rev("el expediente trae cada documento completo", det["nombreOriginal"] == "acta.pdf" and det["sha256"] and det["rutaRelativa"], str(det))
    noexiste_doc = "documento-" + "0" * 32
    noexiste_exp = "expediente-" + "0" * 32
    r_doc = meter(a_admin, org_a, e1, noexiste_doc)
    rev("meter un documento que no existe: 404", r_doc.status_code == 404, r_doc.text[:200])
    rev("meter un documento con un id que ni tiene forma de id: 404", meter(a_admin, org_a, e1, "cualquier-cosa").status_code == 404)
    r_exp = meter(a_admin, org_a, noexiste_exp, d1)
    rev("meter a un expediente que no existe: 404", r_exp.status_code == 404, r_exp.text[:200])
    rev("y con un id que ni tiene forma de id: 404", meter(a_admin, org_a, "xyz", d1).status_code == 404 and ver(a_admin, org_a, "xyz").status_code == 404)
    rev("nada de lo anterior metió ni sacó nada", ids_docs_de(a_admin, org_a, e1) == [d1, d2] and ids_docs_de(a_admin, org_a, e2) == [d1])

    titulo("4 · Sacar un documento: no se borra, y si era el último expediente vuelve al void")
    r = sacar(a_admin, org_a, e1, d2)
    rev("sacar: 200 `quitado: true`", r.status_code == 200 and r.json() == {"quitado": True}, r.text[:200])
    rev("ya no está en el expediente", ids_docs_de(a_admin, org_a, e1) == [d1])
    rev("era su único expediente: vuelve al void, completo y sin perderse", d2 in ids_docs(a_admin, org_a, True) and d2 in ids_docs(a_admin, org_a), str(ids_docs(a_admin, org_a, True)))
    sacar(a_admin, org_a, e1, d1)
    rev("d1 sale de uno de sus dos expedientes y NO vuelve al void: sigue en el otro", d1 not in ids_docs(a_admin, org_a, True) and ids_docs_de(a_admin, org_a, e2) == [d1])
    r = sacar(a_admin, org_a, e1, d3)
    rev("sacar uno que no estaba en el expediente: 404", r.status_code == 404 and "no está en el expediente" in r.text, r.text[:200])
    r2 = sacar(a_admin, org_a, noexiste_exp, d1)
    rev("sacar de un expediente que no existe: 404", r2.status_code == 404 and r2.json() != r.json(), r2.text[:200])
    rev("sacar dos veces el mismo: la segunda es 404 y no repite el rastro", sacar(a_admin, org_a, e1, d2).status_code == 404 and len(auditoria("CASE_DOCUMENT_REMOVED")) == 2)
    meter(a_admin, org_a, e1, d2)
    meter(a_admin, org_a, e1, d1)
    rev("se puede volver a meter lo que se sacó, y va al final", ids_docs_de(a_admin, org_a, e1) == [d2, d1], str(ids_docs_de(a_admin, org_a, e1)))
    rev("los cuatro documentos (y el del cliente) siguen existiendo", len(ids_docs(a_admin, org_a)) == 5)

    titulo("5 · Lo de otra organización responde como si no existiera")
    b_exp = crear(b_admin, org_b, "Expediente de Beta").json()["id"]
    r = listar(b_admin, org_a)
    rev("pedir los expedientes de OTRA organización: 403 sin_acceso", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso", r.text[:200])
    rev("y no dice nada de la ajena", "Alfa" not in r.text and org_a not in r.text and e1 not in r.text)
    rev("lo mismo con sus documentos", docs(b_admin, org_a).status_code == 403)
    r = ver(b_admin, org_a, e1)
    rev("ver un expediente de OTRA organización poniendo SU tenant: 403 sin_acceso, sin filtrar su nombre", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso" and "Solicitud de crédito" not in r.text and e1 not in r.text, r.text[:200])
    r = ver(b_admin, org_b, e1)
    rev("ver un expediente AJENO por su id: 404", r.status_code == 404, r.text[:200])
    rev("es la MISMA respuesta que para uno que no existe", r.json() == ver(b_admin, org_b, noexiste_exp).json(), f"{r.text} | {ver(b_admin, org_b, noexiste_exp).text}")
    r = meter(b_admin, org_b, b_exp, d1)
    rev("meter un documento AJENO a un expediente propio: 404, igual que un documento que no existe", r.status_code == 404 and r.json() == meter(b_admin, org_b, b_exp, noexiste_doc).json(), r.text[:200])
    r = meter(b_admin, org_b, e1, d3)
    rev("meter a un expediente AJENO: 404, igual que un expediente que no existe", r.status_code == 404 and r.json() == meter(b_admin, org_b, noexiste_exp, d3).json(), r.text[:200])
    r = sacar(b_admin, org_b, e1, d1)
    rev("sacar de un expediente ajeno: 404", r.status_code == 404, r.text[:200])
    rev("escribir poniendo el tenant de OTRA: 403 sin_acceso (crear, meter y sacar)", crear(b_admin, org_a, "Colado").status_code == 403 and meter(b_admin, org_a, e1, d3).status_code == 403 and sacar(b_admin, org_a, e1, d1).status_code == 403)
    rev("nada de lo anterior tocó la primera: expedientes y documentos igual", ids_docs_de(a_admin, org_a, e1) == [d2, d1] and ids_docs_de(a_admin, org_a, e2) == [d1] and len(listar(a_admin, org_a).json()["expedientes"]) == 5)
    rev("y la otra tiene solo lo suyo", [e["id"] for e in listar(b_admin, org_b).json()["expedientes"]] == [b_exp] and ids_docs(b_admin, org_b) == [])
    intentos = auditoria("CROSS_TENANT_ACCESS")
    rev("los intentos con el tenant ajeno quedan en la bitácora", len(intentos) >= 5 and all(i.get("recurso") == "expediente" for i in intentos), str(intentos[-1:]))

    titulo("6 · Cada rol hace solo lo que le toca")
    for rol in ORGANIZAN:
        h = P[rol]
        r = crear(h, org_a, f"De {rol}")
        nuevo = r.json().get("id")
        rev(f"{rol}: crea un expediente", r.status_code == 201 and nuevo, r.text[:150])
        rev(f"{rol}: mete y saca un documento", meter(h, org_a, nuevo, d3).status_code == 200 and sacar(h, org_a, nuevo, d3).status_code == 200)
    antes_denegados = len(auditoria("ROLE_DENIED"))
    total_antes = len(listar(a_admin, org_a).json()["expedientes"])
    for rol in SOLO_LEEN:
        h = P[rol]
        leen = [listar(h, org_a).status_code, ver(h, org_a, e1).status_code, docs(h, org_a).status_code, docs(h, org_a, True).status_code]
        rev(f"{rol}: lee (lista, expediente, documentos, void)", leen == [200] * 4, str(leen))
        malas = [(n, r.status_code, r.json().get("detail", {}).get("codigo")) for n, r in (("crear", crear(h, org_a, "No debería")), ("meter", meter(h, org_a, e3, d3)), ("sacar", sacar(h, org_a, e1, d2)))]
        rev(f"{rol}: crear, meter y sacar dan 403 sin_permiso", all(s == 403 and cod == "sin_permiso" for _, s, cod in malas), str(malas))
        rev(f"{rol}: el rechazo no dice qué rol sí puede", not any(x in crear(h, org_a, "No debería").text for x in ("SUPERVISOR", "OPERADOR", "ANALISTA", "ADMIN")))
    rev("lo denegado no creó ni movió nada", len(listar(a_admin, org_a).json()["expedientes"]) == total_antes and ids_docs_de(a_admin, org_a, e1) == [d2, d1] and ids_docs_de(a_admin, org_a, e3) == [])
    rev("cada rechazo dejó `ROLE_DENIED`", len(auditoria("ROLE_DENIED")) - antes_denegados >= 3 * len(SOLO_LEEN) and all(x.get("recurso") == "expediente" for x in auditoria("ROLE_DENIED")[antes_denegados:]))
    rev("los roles que organizan son exactamente los de `roles.PUEDEN_ORGANIZAR`", set(ORGANIZAN) | {"ADMIN"} == set(roles.PUEDEN_ORGANIZAR) and not (set(SOLO_LEEN) & set(roles.PUEDEN_ORGANIZAR)))

    titulo("7 · Crear, meter y sacar dejan rastro, con quién")
    creados = [x for x in auditoria("CASE_CREATED") if x.get("expedienteId") == e1]
    rev("`CASE_CREATED` dice quién, dónde y cuál", len(creados) == 1 and creados[0].get("usuarioGuid") == guid_rita and creados[0].get("tenant") == org_a and creados[0].get("nombre") == "Solicitud de crédito", str(creados))
    metidos = [x for x in auditoria("CASE_DOCUMENT_ADDED") if x.get("expedienteId") == e1 and x.get("documentoId") == d1]
    rev("`CASE_DOCUMENT_ADDED`: una vez al meterlo, otra al volver a meterlo, y NINGUNA por el intento repetido", len(metidos) == 2 and metidos[0].get("usuarioGuid") == guid_rita and metidos[0].get("nombreOriginal") == "acta.pdf", str(metidos))
    sacados = [x for x in auditoria("CASE_DOCUMENT_REMOVED") if x.get("expedienteId") == e1]
    rev("`CASE_DOCUMENT_REMOVED` dice qué documento y de cuál expediente", len(sacados) == 2 and {x.get("documentoId") for x in sacados} == {d1, d2} and all(x.get("nombre") == "Solicitud de crédito" for x in sacados), str(sacados))
    rev("las lecturas no dejan rastro propio", not [x for x in auditoria("CASE_CREATED") if x.get("nombre") in ("No debería", "Colado")])

    titulo("8 · Sin llave o sin persona, no pasa nada")
    rev("sin Bearer: 401", c.get("/expedientes/", params={"tenant": org_a}, headers=cab()).status_code == 401)
    rev("sin llave de servicio: se rechaza", c.get("/expedientes/", params={"tenant": org_a}, headers=cab(a_admin, servicio=False)).status_code in (401, 403))
    rev("sin decir el tenant: 422", c.get("/expedientes/", headers=cab(a_admin)).status_code == 422)
    rev("un tenant con caracteres raros no abre el disco: se rechaza", c.get("/expedientes/", params={"tenant": "../x"}, headers=cab(a_admin)).status_code in (400, 403, 404))

    titulo("9 · Un corte a media escritura no rompe el registro")
    ruta = RAIZ / ".registro" / f"expedientes-{org_a}.jsonl"
    if not SQL:  # en SQL no hay archivo que cortar: lo que se comprueba abajo debe seguir siendo cierto igual
        with open(ruta, "ab") as f:
            f.write(b'{"op": "crear", "id": "expediente-roto')
    antes = [e["id"] for e in listar(a_admin, org_a).json()["expedientes"]]
    rev("una última línea rota se ignora al leer", e1 in antes and len(antes) >= 4)
    nuevo = crear(a_admin, org_a, "Después del corte")
    rev("y el guardado siguiente no queda pegado a ella", nuevo.status_code == 201 and nuevo.json()["id"] in [e["id"] for e in listar(a_admin, org_a).json()["expedientes"]])
    rev("el de antes se conserva intacto", ids_docs_de(a_admin, org_a, e1) == [d2, d1])

    titulo("10 · Tope de documentos por expediente")
    tope = expedientes.MAX_DOCUMENTOS
    try:
        expedientes.MAX_DOCUMENTOS = 2
        lleno = crear(a_admin, org_a, "Con tope").json()["id"]
        rev("hasta el tope se puede meter", meter(a_admin, org_a, lleno, d1).status_code == 200 and meter(a_admin, org_a, lleno, d2).status_code == 200)
        r = meter(a_admin, org_a, lleno, d3)
        rev("pasado el tope: 400 que dice cuál es", r.status_code == 400 and "máximo" in r.text, r.text[:200])
        rev("y el que ya estaba no cuenta como pasarse", meter(a_admin, org_a, lleno, d1).json()["agregado"] is False)
        rev("no se agregó el que no cabía", ids_docs_de(a_admin, org_a, lleno) == [d1, d2])
    finally:
        expedientes.MAX_DOCUMENTOS = tope

    titulo("11 · Dos peticiones a la vez")
    simultaneo = crear(a_admin, org_a, "Simultáneo").json()["id"]
    with ThreadPoolExecutor(max_workers=8) as pool:
        resultados = list(pool.map(lambda _: meter(a_admin, org_a, simultaneo, d4).json()["agregado"], range(8)))
    rev("ocho peticiones a la vez para meter el MISMO documento: solo una lo agrega", resultados.count(True) == 1 and resultados.count(False) == 7, str(resultados))
    rev("y queda una sola vez", ids_docs_de(a_admin, org_a, simultaneo) == [d4])
    antes_n = len(listar(a_admin, org_a).json()["expedientes"])
    with ThreadPoolExecutor(max_workers=8) as pool:
        nuevos = list(pool.map(lambda i: crear(a_admin, org_a, f"Simultáneo {i}").json()["id"], range(8)))
    rev("ocho expedientes creados a la vez: ocho ids distintos y ninguno se pierde", len(set(nuevos)) == 8 and len(listar(a_admin, org_a).json()["expedientes"]) == antes_n + 8)

    titulo("12 · La forma de lo guardado (la de una tabla)" + (" — solo en el almacén" if SQL else ""))
    if not SQL:
        eventos_doc = [json.loads(x) for x in (RAIZ / ".registro" / f"documentos-{org_a}.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
        campos = {"op", "id", "tenant", "sha256", "rutaRelativa", "tamanoBytes", "mime", "nombreOriginal", "canal", "llaveId", "recibidoEn"}
        rev("cada documento guardado tiene exactamente los campos de la entidad", all(set(e) == campos and e["op"] == "nacer" for e in eventos_doc) and len(eventos_doc) == 5, str([sorted(set(e) ^ campos) for e in eventos_doc][:2]))
        eventos_exp = [json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip().endswith("}")]
        rev("cada expediente y cada pertenencia, también, y de solo agregar", {e["op"] for e in eventos_exp} == {"crear", "agregar", "quitar"} and all(e["tenant"] == org_a for e in eventos_exp))
        rev("el registro de una organización no lleva nada de otra", not (RAIZ / ".registro" / f"documentos-{org_b}.jsonl").exists() and all(b_exp not in json.dumps(e) for e in eventos_exp))

    titulo("13 · El expediente ya no sabe de tipos, de virtuales (Moibe, 2026-11-05) ni de grupos (2026-11-06) y las rutas que quitamos dan 404")
    rev("pedir el listado con `?tipo=` no filtra: el expediente no tiene tipo", [e["id"] for e in c.get("/expedientes/", params={"tenant": org_a, "tipo": "tipo-expediente-" + "a" * 32}, headers=cab(a_admin)).json()["expedientes"]] == [e["id"] for e in listar(a_admin, org_a).json()["expedientes"]])
    detalle_e1 = ver(a_admin, org_a, e1).json()
    rev("el detalle trae `duplicados` y NO trae `expedientesVirtuales` ni la marca vieja `duplicadoEn`", detalle_e1["duplicados"] == [] and "expedientesVirtuales" not in detalle_e1 and "duplicadoEn" not in json.dumps(detalle_e1), str(sorted(detalle_e1)))
    rev("cada documento trae `tipoDocumental` y `duplicado: false` (aquí ninguno está clasificado, así que ninguno es duplicado de nada)", bool(detalle_e1["documentos"]) and all(d["duplicado"] is False and d["tipoDocumental"] is None for d in detalle_e1["documentos"]), str(detalle_e1["documentos"][:1]))
    rev("lo que devuelve meter un documento es `{agregado, expediente}`: ya sin `expedientesVirtuales`", sorted(meter(a_admin, org_a, e1, d1).json()) == ["agregado", "expediente"] and "expedientesVirtuales" not in meter(a_admin, org_a, e1, d1).json()["expediente"])
    creado_e1 = [x for x in auditoria("CASE_CREATED") if x.get("expedienteId") == e1]
    rev("`CASE_CREATED` ya no anota tipo", len(creado_e1) == 1 and "tipoId" not in creado_e1[0], str(creado_e1))
    rev("ni grupo (`grupoId`, `grupoNombre`)", len(creado_e1) == 1 and "grupoId" not in creado_e1[0] and "grupoNombre" not in creado_e1[0], str(creado_e1))
    rev("el detalle de un expediente no trae `grupo`", "grupo" not in detalle_e1 and "grupoId" not in detalle_e1 and "grupoNombre" not in detalle_e1, str(sorted(detalle_e1)))
    fila_e1 = next(e for e in listar(a_admin, org_a).json()["expedientes"] if e["id"] == e1)
    rev("tampoco la fila de la lista, que solo trae lo suyo: id, nombre, quién lo creó, cuándo y cuánto tiene", sorted(fila_e1) == ["creadoEn", "creadoPor", "id", "nombre", "tamanoBytes", "totalDocumentos"], str(sorted(fila_e1)))
    inexistente_v = "expediente-virtual-" + "0" * 32
    inexistente_t = "tipo-expediente-" + "0" * 32
    quitadas = {
        "GET /tipos-expediente/": c.get("/tipos-expediente/", params={"tenant": org_a}, headers=cab(a_admin)),
        "POST /tipos-expediente/": c.post("/tipos-expediente/", json={"tenant": org_a, "nombre": "X", "tiposDocumentales": []}, headers=cab(a_admin)),
        "GET /tipos-expediente/{id}": c.get(f"/tipos-expediente/{inexistente_t}", params={"tenant": org_a}, headers=cab(a_admin)),
        "PATCH /tipos-expediente/{id}": c.patch(f"/tipos-expediente/{inexistente_t}", json={"tenant": org_a, "nombre": "X"}, headers=cab(a_admin)),
        "PUT /tipos-expediente/{id}/esquema": c.put(f"/tipos-expediente/{inexistente_t}/esquema", json={"tenant": org_a, "tiposDocumentales": []}, headers=cab(a_admin)),
        "DELETE /tipos-expediente/{id}": c.delete(f"/tipos-expediente/{inexistente_t}", params={"tenant": org_a}, headers=cab(a_admin)),
        "GET /tipos-expediente/{id}/virtuales": c.get(f"/tipos-expediente/{inexistente_t}/virtuales", params={"tenant": org_a}, headers=cab(a_admin)),
        "POST /expedientes/{id}/virtuales": c.post(f"/expedientes/{e1}/virtuales", json={"tenant": org_a, "tipoId": inexistente_t, "nombre": "X", "documentos": []}, headers=cab(a_admin)),
        "GET /expedientes/{id}/virtuales/{vid}": c.get(f"/expedientes/{e1}/virtuales/{inexistente_v}", params={"tenant": org_a}, headers=cab(a_admin)),
        "PUT /expedientes/{id}/virtuales/{vid}/documentos/{doc}": c.put(f"/expedientes/{e1}/virtuales/{inexistente_v}/documentos/{d1}", params={"tenant": org_a}, headers=cab(a_admin)),
        "DELETE /expedientes/{id}/virtuales/{vid}/documentos/{doc}": c.delete(f"/expedientes/{e1}/virtuales/{inexistente_v}/documentos/{d1}", params={"tenant": org_a}, headers=cab(a_admin)),
        "DELETE /expedientes/{id}/virtuales/{vid}": c.delete(f"/expedientes/{e1}/virtuales/{inexistente_v}", params={"tenant": org_a}, headers=cab(a_admin)),
    }
    rev("las rutas de los tipos de expediente y de los expedientes virtuales ya no existen: las doce dan 404", all(r_.status_code == 404 for r_ in quitadas.values()), str({k_: v_.status_code for k_, v_ in quitadas.items() if v_.status_code != 404}))
    rev("y el servicio no tiene de dónde sacarlas", all(not hasattr(expedientes, n_) for n_ in ("expedientes_virtuales", "tipos_expediente")) and not any(m_ in sys.modules for m_ in ("servicios.tipos_expediente", "servicios.expedientes_virtuales", "routers.tipos_expediente")))
    inexistente_g = "grupo-" + "0" * 32
    grupos_quitados = {
        "GET /grupos/": c.get("/grupos/", params={"tenant": org_a}, headers=cab(a_admin)),
        "POST /grupos/": c.post("/grupos/", json={"tenant": org_a, "nombre": "X"}, headers=cab(a_admin)),
        "GET /grupos/{id}": c.get(f"/grupos/{inexistente_g}", params={"tenant": org_a}, headers=cab(a_admin)),
        "PATCH /grupos/{id}": c.patch(f"/grupos/{inexistente_g}", json={"tenant": org_a, "nombre": "X"}, headers=cab(a_admin)),
        "DELETE /grupos/{id}": c.delete(f"/grupos/{inexistente_g}", params={"tenant": org_a}, headers=cab(a_admin)),
        "PUT /expedientes/{id}/grupo": c.put(f"/expedientes/{e1}/grupo", json={"tenant": org_a, "grupoId": inexistente_g}, headers=cab(a_admin)),
    }
    rev("las rutas de los grupos y la de asignar un expediente a uno ya no existen: las seis dan 404", all(r_.status_code == 404 for r_ in grupos_quitados.values()), str({k_: v_.status_code for k_, v_ in grupos_quitados.items() if v_.status_code != 404}))
    rev("y el servicio no tiene de dónde sacarlas: ni `grupos` ni `asignar_grupo` ni sus errores, y ningún módulo de grupos se cargó", all(not hasattr(expedientes, n_) for n_ in ("grupos", "asignar_grupo", "GrupoNoEncontrado", "GrupoRequerido")) and not any(m_ in sys.modules for m_ in ("servicios.grupos", "routers.grupos")))

    titulo("14 · A un expediente le entra cualquier documento, y quitar un tipo documental de la biblioteca no lo toca")
    docs_de_e1 = ids_docs_de(a_admin, org_a, e1)
    suelto = subir(a_admin, org_a, "foto-suelta.pdf", pdf(77)).json()["documentoId"]
    r = meter(a_admin, org_a, e1, suelto)
    rev("a un expediente le entra cualquier documento (no tiene tipo ni esquema que lo restrinja)", r.status_code == 200 and r.json()["agregado"] is True and suelto in ids_docs_de(a_admin, org_a, e1), r.text[:200])
    td_rec = crear_tipo_documental(a_admin, org_a, "Receta")
    r = c.delete(f"/tipos-documentales/{td_rec}", params={"tenant": org_a}, headers=cab(a_admin))
    rev("borrar de la biblioteca un tipo documental: 200, y ni el expediente ni sus documentos se enteran", r.status_code == 200 and ids_docs_de(a_admin, org_a, e1) == docs_de_e1 + [suelto], r.text[:150])

    titulo("15 · Cuánto pesa un expediente: la suma de sus documentos, calculada al consultar")

    def peso(bearer: str, tenant: str, exp: str) -> int:
        return next(e for e in listar(bearer, tenant).json()["expedientes"] if e["id"] == exp)["tamanoBytes"]

    chico = subir(a_admin, org_a, "chico.pdf", pdf(88)).json()["documentoId"]
    grande = subir(a_admin, org_a, "grande.pdf", pdf(89) + b"x" * 5000).json()["documentoId"]
    tam = {d["id"]: d["tamanoBytes"] for d in docs(a_admin, org_a).json()["documentos"]}
    rev("los dos documentos nuevos pesan distinto (si no, una suma equivocada pasaría inadvertida)", tam[chico] == len(pdf(88)) and tam[grande] == len(pdf(89)) + 5000 and tam[chico] != tam[grande], str((tam[chico], tam[grande])))
    alta = crear(a_admin, org_a, "Con peso").json()
    pesado = alta["id"]
    rev("al crearlo, el expediente pesa 0: lo dice el alta, la lista y el expediente entero", alta["tamanoBytes"] == 0 and peso(a_admin, org_a, pesado) == 0 and ver(a_admin, org_a, pesado).json()["tamanoBytes"] == 0, str(alta))
    meter(a_admin, org_a, pesado, chico)
    rev("con un documento adentro pesa lo que pesa ese", peso(a_admin, org_a, pesado) == tam[chico], f"{peso(a_admin, org_a, pesado)} != {tam[chico]}")
    meter(a_admin, org_a, pesado, grande)
    rev("con los dos pesa la suma", peso(a_admin, org_a, pesado) == tam[chico] + tam[grande], f"{peso(a_admin, org_a, pesado)} != {tam[chico] + tam[grande]}")
    rev("la lista y el expediente entero dicen lo mismo", ver(a_admin, org_a, pesado).json()["tamanoBytes"] == peso(a_admin, org_a, pesado) and meter(a_admin, org_a, pesado, grande).json()["expediente"]["tamanoBytes"] == tam[chico] + tam[grande])
    rev("meter otra vez el mismo documento no lo cuenta dos veces", peso(a_admin, org_a, pesado) == tam[chico] + tam[grande])
    otro = crear(a_admin, org_a, "Otro con peso").json()["id"]
    meter(a_admin, org_a, otro, grande)
    rev("el mismo documento en OTRO expediente pesa completo ahí y no cambia el peso del primero", peso(a_admin, org_a, otro) == tam[grande] and peso(a_admin, org_a, pesado) == tam[chico] + tam[grande])
    sacar(a_admin, org_a, pesado, chico)
    rev("sacar un documento resta SU peso, no el del otro", peso(a_admin, org_a, pesado) == tam[grande], f"{peso(a_admin, org_a, pesado)} != {tam[grande]}")
    rev("y el documento sacado sigue existiendo con su mismo peso (no se borró)", {d["id"]: d["tamanoBytes"] for d in docs(a_admin, org_a).json()["documentos"]}[chico] == tam[chico])
    sacar(a_admin, org_a, pesado, grande)
    rev("sin documentos vuelve a pesar 0", peso(a_admin, org_a, pesado) == 0)
    rev("y sacar de uno no toca el peso del otro", peso(a_admin, org_a, otro) == tam[grande])
    for e in listar(a_admin, org_a).json()["expedientes"]:
        suma = sum(d["tamanoBytes"] for d in ver(a_admin, org_a, e["id"]).json()["documentos"])
        rev(f"«{e['nombre'][:30]}»: el peso de la lista es la suma de lo que muestra al abrirlo ({suma} B)", e["tamanoBytes"] == suma and isinstance(e["tamanoBytes"], int), f"{e['tamanoBytes']} != {suma}")
    doc_b = subir(b_admin, org_b, "de-beta.pdf", pdf(90) + b"y" * 300).json()["documentoId"]
    exp_b = crear(b_admin, org_b, "Peso de Beta").json()["id"]
    rev("la otra organización parte de 0, aunque la primera ya tenga documentos", peso(b_admin, org_b, exp_b) == 0)
    meter(b_admin, org_b, exp_b, doc_b)
    rev("y pesa solo lo suyo", peso(b_admin, org_b, exp_b) == len(pdf(90)) + 300, str(peso(b_admin, org_b, exp_b)))
    rev("sin que cambie el peso de la primera", peso(a_admin, org_a, otro) == tam[grande] and peso(a_admin, org_a, pesado) == 0)
    if not SQL:
        eventos_exp = [json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip().endswith("}")]
        rev("el peso no se guarda: ningún evento del registro lo lleva", all("tamanoBytes" not in e and "totalDocumentos" not in e for e in eventos_exp))

    # ---------------------------------------------------------------- 18
    titulo("18 · Lo que se carga a mano también es un documento (POST /expedientes/documentos): es lo que llena «Archivos procesados»")

    def manual(bearer, tenant, nombre, contenido, tipo="application/pdf", **campos):
        return c.post("/expedientes/documentos", files={"archivo": (nombre, contenido, tipo)}, data={"tenant": tenant, **campos}, headers=cab(bearer))

    antes = ids_docs(a_admin, org_a)
    antes_b = ids_docs(b_admin, org_b)
    r = manual(a_admin, org_a, "manual-uno.pdf", pdf(901))
    nuevo = r.json() if r.status_code == 201 else {}
    rev("201 con el documento que nació: canal MANUAL, su nombre, su tamaño, su tipo, su organización y sin llave de cliente",
        r.status_code == 201 and nuevo["canal"] == "MANUAL" and nuevo["nombreOriginal"] == "manual-uno.pdf" and nuevo["tamanoBytes"] == len(pdf(901))
        and nuevo["mime"] == "application/pdf" and nuevo["tenant"] == org_a and nuevo["llaveId"] is None and nuevo["id"].startswith("documento-"), r.text[:240])
    rev("sus bytes quedan en el prefijo de SU organización", nuevo.get("rutaRelativa", "").startswith(org_a + "/") and len(nuevo.get("sha256", "")) == 64, str(nuevo))
    lista = docs(a_admin, org_a).json()["documentos"]
    rev("aparece en la lista de documentos de su organización (la que lee «Archivos procesados»), al final y en el void",
        len(lista) == len(antes) + 1 and lista[-1]["id"] == nuevo.get("id") and lista[-1]["expedientes"] == [] and nuevo.get("id") in ids_docs(a_admin, org_a, True))
    rev("conserva su canal en la lista, y los que entraron por la API siguen siendo API", lista[-1]["canal"] == "MANUAL" and all(d["canal"] == "API" for d in lista[:-1]))
    rev("lo ve cualquiera de su organización, no solo quien lo cargó", nuevo.get("id") in ids_docs(P["VIEWER"], org_a))
    rev("la otra organización NO lo ve", ids_docs(b_admin, org_b) == antes_b)
    otra = manual(a_admin, org_a, "manual-uno.pdf", pdf(901))
    rev("el mismo archivo cargado dos veces son DOS documentos con los mismos bytes (igual que por la API)",
        otra.status_code == 201 and otra.json()["id"] != nuevo["id"] and otra.json()["sha256"] == nuevo["sha256"] and otra.json()["rutaRelativa"] == nuevo["rutaRelativa"])

    cantidad = len(ids_docs(a_admin, org_a))
    rev("con la sesión de OTRA organización: 403 y no crea nada", manual(b_admin, org_a, "intruso.pdf", pdf(902)).status_code == 403 and len(ids_docs(a_admin, org_a)) == cantidad)
    rev("sin Bearer (solo la llave de servicio): 401", manual(None, org_a, "sin-sesion.pdf", pdf(903)).status_code == 401)
    rev("sin llave de servicio: se rechaza", c.post("/expedientes/documentos", files={"archivo": ("x.pdf", pdf(904), "application/pdf")}, data={"tenant": org_a}, headers=cab(a_admin, servicio=False)).status_code in (401, 403))
    rev("sin decir la organización: 422", c.post("/expedientes/documentos", files={"archivo": ("x.pdf", pdf(905), "application/pdf")}, headers=cab(a_admin)).status_code == 422)
    rev("una organización con caracteres raros no abre el disco: se rechaza", manual(a_admin, "../x", "x.pdf", pdf(906)).status_code in (400, 403, 404))
    rev("nada de lo rechazado dejó un documento", len(ids_docs(a_admin, org_a)) == cantidad and ids_docs(b_admin, org_b) == antes_b)

    rev("un formato que la bandeja tampoco acepta (GIF): 400 y no crea nada", manual(a_admin, org_a, "x.gif", b"GIF89a", "image/gif").status_code == 400 and len(ids_docs(a_admin, org_a)) == cantidad)
    rev("un archivo vacío: 400 y no crea nada", manual(a_admin, org_a, "vacio.pdf", b"").status_code == 400 and len(ids_docs(a_admin, org_a)) == cantidad)
    for nombre, tipo in (("foto.jpg", "image/jpeg"), ("captura.png", "image/png"), ("escaneo.tiff", "image/tiff")):
        rev(f"{tipo}: se acepta, como en la bandeja", manual(a_admin, org_a, nombre, b"\x00" + nombre.encode() * 3, tipo).status_code == 201)
    rev("cualquier rol de la organización puede cargar (también los que solo leen), igual que `/archivos` y que clasificar: es la sesión la que manda",
        all(manual(P[rol], org_a, f"de-{rol.lower()}.pdf", pdf(910 + i)).status_code == 201 for i, rol in enumerate(ORGANIZAN + SOLO_LEEN)))
    rev("y la forma de lo guardado es la de siempre: el mismo evento `nacer`, con los mismos campos que los que entran por la API",
        all(set(d) == {"id", "tenant", "sha256", "rutaRelativa", "tamanoBytes", "mime", "nombreOriginal", "canal", "llaveId", "recibidoEn", "expedientes", "tipoDocumental"} for d in docs(a_admin, org_a).json()["documentos"]),
        str(docs(a_admin, org_a).json()["documentos"][-1]))

    # ---------------------------------------------------------------- 18b
    titulo("18b · Subir un archivo directo a un expediente (POST /expedientes/{id}/documentos): nace como carga manual y entra al expediente en UNA llamada")

    def subir_a(bearer, tenant, exp, nombre, contenido, tipo="application/pdf", **campos):
        return c.post(f"/expedientes/{exp}/documentos", files={"archivo": (nombre, contenido, tipo)}, data={"tenant": tenant, **campos}, headers=cab(bearer))

    destino = crear(a_admin, org_a, "Subidas directas").json()["id"]
    vecino = crear(a_admin, org_a, "El de al lado").json()["id"]
    ajeno = crear(b_admin, org_b, "De Beta").json()["id"]
    antes = ids_docs(a_admin, org_a)
    r = subir_a(a_admin, org_a, destino, "directo-uno.pdf", pdf(950))
    nuevo = r.json().get("documento", {}) if r.status_code == 201 else {}
    rev("201 con `agregado: true` y el documento que nació: canal MANUAL, su nombre, su tamaño, su tipo y su organización",
        r.status_code == 201 and r.json()["agregado"] is True and nuevo["canal"] == "MANUAL" and nuevo["nombreOriginal"] == "directo-uno.pdf"
        and nuevo["tamanoBytes"] == len(pdf(950)) and nuevo["mime"] == "application/pdf" and nuevo["tenant"] == org_a and nuevo["id"].startswith("documento-"), r.text[:240])
    # Subir también CLASIFICA (2026-11-02): aquí ninguna organización tiene clasificador (y sin base ni se pregunta), así que el archivo se sube igual
    # y la respuesta lo dice. El clasificador de verdad (con tipo y confianza, Document AI falseado) se prueba más abajo, en la sección 21 (solo con `--sql`).
    clasificacion = r.json().get("clasificacion") if r.status_code == 201 else None
    esperado = {"estado": "sin_clasificador" if SQL else "no_clasificado", "tipo": None, "confianza": None}
    rev(f"la respuesta dice que no se clasificó (`{esperado['estado']}`) y trae solo lo del contrato: `{{agregado, documento, clasificacion, extraccion}}`, sin virtuales",
        clasificacion == esperado and sorted(r.json()) == ["agregado", "clasificacion", "documento", "extraccion"], str(sorted(r.json())))
    rev("sin tipo reconocido no hay con qué extraer: `extraccion.estado` es `sin_tipo`", r.json().get("extraccion") == {"estado": "sin_tipo"}, str(r.json().get("extraccion")))
    rev("queda EN ese expediente y en ningún otro, en la misma llamada", ids_docs_de(a_admin, org_a, destino) == [nuevo.get("id")] and ids_docs_de(a_admin, org_a, vecino) == [])
    en_lista = [d for d in docs(a_admin, org_a).json()["documentos"] if d["id"] == nuevo.get("id")]
    rev("y es un documento de la organización como cualquier otro: sale en la lista de «Archivos procesados», con su expediente",
        len(ids_docs(a_admin, org_a)) == len(antes) + 1 and len(en_lista) == 1 and en_lista[0]["canal"] == "MANUAL" and en_lista[0]["expedientes"] == [{"id": destino, "nombre": "Subidas directas"}], str(en_lista))
    rev("el expediente lo enseña como carga manual y SIN tipo documental (todavía no pasó por el pipeline)",
        [(d["canal"], d["tipoDocumental"]) for d in ver(a_admin, org_a, destino).json()["documentos"]] == [("MANUAL", None)])
    rev("lo ve cualquiera de la organización y la otra no", nuevo.get("id") in ids_docs(P["VIEWER"], org_a) and nuevo.get("id") not in ids_docs(b_admin, org_b))
    otra = subir_a(a_admin, org_a, destino, "directo-uno.pdf", pdf(950))
    rev("el mismo archivo dos veces son DOS documentos con los mismos bytes, y los dos quedan en el expediente, en orden",
        otra.status_code == 201 and otra.json()["documento"]["id"] != nuevo["id"] and otra.json()["documento"]["sha256"] == nuevo["sha256"]
        and ids_docs_de(a_admin, org_a, destino) == [nuevo["id"], otra.json()["documento"]["id"]])
    for nombre, tipo in (("foto.jpg", "image/jpeg"), ("captura.png", "image/png"), ("escaneo.tiff", "image/tiff")):
        rev(f"{tipo}: se acepta, como en la bandeja", subir_a(a_admin, org_a, destino, nombre, b"\x00" + nombre.encode() * 3, tipo).status_code == 201)
    metidos = [x for x in auditoria("CASE_DOCUMENT_ADDED") if x.get("documentoId") == nuevo["id"]]
    rev("deja `CASE_DOCUMENT_ADDED` con quién, el expediente, el documento y que fue una subida",
        len(metidos) == 1 and metidos[0].get("usuarioGuid") == guid_rita and metidos[0].get("expedienteId") == destino and metidos[0].get("nombre") == "Subidas directas"
        and metidos[0].get("nombreOriginal") == "directo-uno.pdf" and metidos[0].get("subida") is True, str(metidos))

    # Todo lo que se rechaza se rechaza ANTES de guardar: ni un documento suelto en el void ni nada en el expediente.
    cantidad, en_destino, cantidad_b = len(ids_docs(a_admin, org_a)), ids_docs_de(a_admin, org_a, destino), len(ids_docs(b_admin, org_b))
    rev("un formato que la bandeja tampoco acepta (GIF): 400", subir_a(a_admin, org_a, destino, "x.gif", b"GIF89a", "image/gif").status_code == 400)
    rev("un archivo vacío: 400", subir_a(a_admin, org_a, destino, "vacio.pdf", b"").status_code == 400)
    r = subir_a(a_admin, org_a, "expediente-" + "0" * 32, "x.pdf", pdf(951))
    rev("un expediente que no existe: 404 que lo dice", r.status_code == 404 and "Ese expediente no existe" in r.text, f"{r.status_code} {r.text[:100]}")
    rev("el expediente de OTRA organización, con la sesión y el tenant de ésta: 404", subir_a(a_admin, org_a, ajeno, "x.pdf", pdf(952)).status_code == 404)
    rev("…poniendo el tenant de la otra: 403", subir_a(a_admin, org_b, ajeno, "x.pdf", pdf(953)).status_code == 403)
    rev("sin Bearer (solo la llave de servicio): 401", subir_a(None, org_a, destino, "x.pdf", pdf(954)).status_code == 401)
    rev("sin decir la organización: 422", c.post(f"/expedientes/{destino}/documentos", files={"archivo": ("x.pdf", pdf(955), "application/pdf")}, headers=cab(a_admin)).status_code == 422)
    antes_denegados = len(auditoria("ROLE_DENIED"))
    malas = [(rol, r.status_code, r.json().get("detail", {}).get("codigo")) for rol in SOLO_LEEN for r in [subir_a(P[rol], org_a, destino, f"de-{rol.lower()}.pdf", pdf(956))]]
    rev("quien solo lee (Compliance, Auditor, Viewer) recibe 403 sin_permiso, y no se cuela ni como carga manual (a diferencia de `POST /expedientes/documentos`)",
        all(s == 403 and cod == "sin_permiso" for _, s, cod in malas), str(malas))
    rev("cada rechazo de rol deja `ROLE_DENIED` sobre el expediente", len(auditoria("ROLE_DENIED")) - antes_denegados == len(SOLO_LEEN) and all(x.get("recurso") == "expediente" for x in auditoria("ROLE_DENIED")[antes_denegados:]))
    rev("nada de lo rechazado dejó un documento ni movió el expediente",
        len(ids_docs(a_admin, org_a)) == cantidad and ids_docs_de(a_admin, org_a, destino) == en_destino and len(ids_docs(b_admin, org_b)) == cantidad_b)
    rev("quienes organizan (Supervisor, Analista y Operador) sí suben", all(subir_a(P[rol], org_a, destino, f"de-{rol.lower()}.pdf", pdf(960 + i)).status_code == 201 for i, rol in enumerate(ORGANIZAN)))

    tope = expedientes.MAX_DOCUMENTOS
    try:
        expedientes.MAX_DOCUMENTOS = 2
        con_tope = crear(a_admin, org_a, "Con tope de subidas").json()["id"]
        rev("hasta el tope se puede subir", subir_a(a_admin, org_a, con_tope, "uno.pdf", pdf(970)).status_code == 201 and subir_a(a_admin, org_a, con_tope, "dos.pdf", pdf(971)).status_code == 201)
        antes_tope = len(ids_docs(a_admin, org_a))
        r = subir_a(a_admin, org_a, con_tope, "tres.pdf", pdf(972))
        rev("pasado el tope: 400 que dice cuál es, y NO deja un documento suelto", r.status_code == 400 and "máximo" in r.text and len(ids_docs(a_admin, org_a)) == antes_tope, r.text[:200])
        rev("el expediente se quedó con los dos que cupieron", len(ids_docs_de(a_admin, org_a, con_tope)) == 2)
    finally:
        expedientes.MAX_DOCUMENTOS = tope

    if SQL:
        titulo("19 · Lo que SOLO la base puede cuidar (aunque alguien escriba por fuera del servicio)")
        import pyodbc  # noqa: E402

        con = BASE_SQL["conectar"]("usrNexus", BASE_SQL["app"], BASE_SQL["base"])
        k = con.cursor()

        def rechaza(sql: str, *params) -> bool:
            try:
                k.execute(sql, *params)
                return False
            except pyodbc.Error:
                return True

        rev("usrNexus (el usuario de la API) lee las tablas de documentos y expedientes", all(
            k.execute(f"SELECT count(*) FROM nexus.{t}").fetchone()[0] >= 0 for t in ("documents", "cases", "caseDocuments")))
        id_e1 = k.execute("SELECT caseId FROM nexus.cases WHERE publicId = ?", e1).fetchone()[0]
        id_d1 = k.execute("SELECT documentId FROM nexus.documents WHERE publicId = ?", d1).fetchone()[0]
        rev("meter dos veces el mismo documento al mismo expediente lo impide la base", rechaza("INSERT INTO nexus.caseDocuments (caseId, documentId, addedBy) VALUES (?, ?, 'x')", id_e1, id_d1))
        rev("un canal que no es API ni MANUAL, o un sha256 que no es hexadecimal, no entran", rechaza(
            "INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel) VALUES (?, ?, ?, 'x', 1, 'x', 'x', 'CORREO')", "documento-" + "e" * 32, org_a, "a" * 64)
            and rechaza("INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel) VALUES (?, ?, ?, 'x', 1, 'x', 'x', 'API')", "documento-" + "e" * 32, org_a, "Z" * 64))
        rev("un id con mayúsculas o un tenant con espacios no entran", rechaza(
            "INSERT INTO nexus.cases (publicId, tenant, name, createdBy) VALUES (?, ?, 'x', 'x')", "expediente-" + "A" * 32, org_a)
            and rechaza("INSERT INTO nexus.cases (publicId, tenant, name, createdBy) VALUES (?, ?, 'x', 'x')", "expediente-" + "a" * 32, "con espacio"))
        rev("…y uno bien formado sí entra (las dos de arriba no fallaban por otra cosa)", not rechaza(
            "INSERT INTO nexus.cases (publicId, tenant, name, createdBy) VALUES (?, ?, 'x', 'x')", "expediente-" + "f" * 32, org_a))
        k.execute("DELETE FROM nexus.cases WHERE publicId = ?", "expediente-" + "f" * 32)
        rev("los grupos ya no existen en la base (migración 0013): ni la tabla `caseGroups` ni la columna `cases.caseGroupId` (y `cases.caseId`, de control, sí se ve)",
            k.execute("SELECT OBJECT_ID('nexus.caseGroups', 'U')").fetchone()[0] is None
            and k.execute("SELECT COL_LENGTH('nexus.cases', 'caseGroupId')").fetchone()[0] is None
            and k.execute("SELECT COL_LENGTH('nexus.cases', 'caseId')").fetchone()[0] is not None)
        con.close()
    if SQL:
        titulo("20 · Duplicados: dos documentos del mismo tipo en UN expediente se marcan al leer (Moibe, 2026-11-05: sobre el expediente entero)")
        from servicios import datos_extraidos
        from servicios.esquema import normalizar_nombre

        def tipar(doc: str, tipo: str, confianza: float = 90) -> None:
            """Anota una clasificación como la anota `/ia/clasificar`: la categoría es el id normalizado del tipo documental."""
            datos_extraidos.registrar_clasificacion(org_a, doc, {"categoria": normalizar_nombre(tipo), "confianza": confianza}, "clasificador-de-prueba", "usuario-de-prueba")

        def marcas(exp: str) -> dict[str, bool]:
            return {d["id"]: d["duplicado"] for d in ver(a_admin, org_a, exp).json()["documentos"]}

        td_ine = crear_tipo_documental(a_admin, org_a, "INE")
        td_acta = crear_tipo_documental(a_admin, org_a, "Acta constitutiva")
        dup = crear(a_admin, org_a, "Con duplicados").json()["id"]
        aparte = crear(a_admin, org_a, "Con uno solo").json()["id"]
        x1, x2, x3, x4, x5, x6 = (subir(a_admin, org_a, f"dup-{n}.pdf", pdf(300 + n)).json()["documentoId"] for n in range(1, 7))
        for d_ in (x1, x2, x3, x4):
            meter(a_admin, org_a, dup, d_)
        tipar(x1, td_ine)
        tipar(x2, td_ine)
        tipar(x3, td_acta)  # x4 se queda sin clasificar
        det = ver(a_admin, org_a, dup).json()
        rev("el detalle trae el tipo de cada documento (INE, INE, acta y uno sin tipo)", [(d["id"], (d["tipoDocumental"] or {}).get("id")) for d in det["documentos"]] == [(x1, td_ine), (x2, td_ine), (x3, td_acta), (x4, None)], str(det["documentos"])[:300])
        rev("`duplicados` lista el tipo repetido con sus dos documentos, en el orden en que están, y nada más", det["duplicados"] == [{"tipoId": td_ine, "tipoNombre": "INE", "documentos": [{"id": x1, "nombre": "dup-1.pdf"}, {"id": x2, "nombre": "dup-2.pdf"}]}], str(det["duplicados"]))
        rev("cada documento dice si es de los repetidos: los dos INE, sí; el acta y el que no tiene tipo, no", marcas(dup) == {x1: True, x2: True, x3: False, x4: False}, str(marcas(dup)))
        rev("y no queda rastro de la marca de antes (`duplicadoEn`) ni de los virtuales", "expedientesVirtuales" not in det and all("duplicadoEn" not in d for d in det["documentos"]))
        meter(a_admin, org_a, dup, x5)
        rev("dos documentos SIN tipo no son duplicados de nada: un documento sin tipo no cuenta", marcas(dup)[x4] is False and marcas(dup)[x5] is False and [g["tipoId"] for g in ver(a_admin, org_a, dup).json()["duplicados"]] == [td_ine])
        meter(a_admin, org_a, aparte, x1)
        rev("el INE que está solo en OTRO expediente no se marca: un duplicado es de UN expediente", ver(a_admin, org_a, aparte).json()["duplicados"] == [] and marcas(aparte) == {x1: False} and marcas(dup)[x1] is True)
        tipar(x6, td_acta)
        meter(a_admin, org_a, dup, x6)
        grupos_ = ver(a_admin, org_a, dup).json()["duplicados"]
        rev("con otro tipo repetido (dos actas) salen dos grupos, en el orden en que aparece el primer documento de cada tipo", [g["tipoId"] for g in grupos_] == [td_ine, td_acta] and grupos_[1]["documentos"] == [{"id": x3, "nombre": "dup-3.pdf"}, {"id": x6, "nombre": "dup-6.pdf"}], str(grupos_))
        sacar(a_admin, org_a, dup, x2)
        rev("sacar uno de los INE quita su marca solo (se calcula al leer, no se guarda): ya no hay grupo de INE y el otro INE deja de estar marcado", [g["tipoId"] for g in ver(a_admin, org_a, dup).json()["duplicados"]] == [td_acta] and marcas(dup)[x1] is False)
        meter(a_admin, org_a, dup, x2)
        rev("y volver a meterlo la vuelve a poner", [g["tipoId"] for g in ver(a_admin, org_a, dup).json()["duplicados"]] == [td_ine, td_acta] and marcas(dup)[x1] is True and marcas(dup)[x2] is True)
        tipar(x2, td_acta)
        grupos_ = ver(a_admin, org_a, dup).json()["duplicados"]
        rev("la marca sigue al tipo VIGENTE: reclasificar uno de los INE como acta saca al INE de los duplicados y deja tres actas en su grupo",
            [g["tipoId"] for g in grupos_] == [td_acta] and sorted(d["id"] for d in grupos_[0]["documentos"]) == sorted([x2, x3, x6]) and marcas(dup)[x1] is False, str(grupos_))
        rev("lo de otra organización no se mezcla: la otra organización no ve estos expedientes (404) ni sus marcas", ver(b_admin, org_b, dup).status_code == 404)

    if SQL:
        titulo("21 · Subir un archivo a un expediente lo CLASIFICA y guarda lo que dijo, sin meterlo a ningún lado (Document AI se falsea: no se gasta en pruebas)")
        import asyncio

        import routers.expedientes as router_expedientes
        from errores import SinClasificador
        from servicios import ia as servicio_ia

        CONDUCTA: dict = {"respuesta": None, "error": None, "demora": 0.0}
        LLAMADAS: list[int] = []

        async def clasificador_falso(contenido, mime_type, tenant):
            LLAMADAS.append(len(contenido))
            if CONDUCTA["demora"]:
                await asyncio.sleep(CONDUCTA["demora"])
            if CONDUCTA["error"] is not None:
                raise CONDUCTA["error"]
            return CONDUCTA["respuesta"]

        def reconoce(tipo_doc: str | None, confianza: float | None) -> None:
            CONDUCTA.update(respuesta={"categoria": normalizar_nombre(tipo_doc) if tipo_doc else "otro", "confianza": confianza}, error=None, demora=0.0)

        def tipo_de(exp: str, doc: str) -> dict | None:
            return next(d for d in ver(a_admin, org_a, exp).json()["documentos"] if d["id"] == doc)["tipoDocumental"]

        original, espera = servicio_ia.clasificar_documento, router_expedientes.ESPERA_CLASIFICAR_AL_SUBIR
        servicio_ia.clasificar_documento = clasificador_falso
        try:
            pia = crear(a_admin, org_a, "Pía Salas").json()["id"]
            reconoce(td_ine, 92.5)
            r = subir_a(a_admin, org_a, pia, "pia-ine.pdf", pdf(170))
            j = r.json() if r.status_code == 201 else {}
            rev("un archivo que el clasificador reconoce como INE con 92.5 %: 201 y `clasificacion` dice el estado, el tipo y la confianza",
                r.status_code == 201 and j["clasificacion"] == {"estado": "clasificado", "tipo": {"id": td_ine, "nombre": "INE"}, "confianza": 92.5}, r.text[:300])
            rev("la respuesta es solo `{agregado, documento, clasificacion, extraccion}`: ya no trae `expedientesVirtuales`", sorted(j) == ["agregado", "clasificacion", "documento", "extraccion"], str(sorted(j)))
            tipo_pia = tipo_de(pia, j["documento"]["id"])
            rev("el expediente ya enseña el documento CON su tipo documental (el que dijo el clasificador) y la confianza", tipo_pia is not None and tipo_pia["id"] == td_ine and tipo_pia["confianza"] == 92.5, str(tipo_pia))
            rev("al clasificador solo se le mandaron los bytes del archivo, una vez", LLAMADAS == [len(pdf(170))], str(LLAMADAS))
            rev("clasificar no mete el documento a ningún otro lado: está en ese expediente y en ningún otro", [d["expedientes"] for d in docs(a_admin, org_a).json()["documentos"] if d["id"] == j["documento"]["id"]] == [[{"id": pia, "nombre": "Pía Salas"}]])
            rev("y no deja más rastro que el de siempre (`CASE_DOCUMENT_ADDED` de la subida)", [x for x in auditoria("CASE_DOCUMENT_ADDED") if x.get("documentoId") == j["documento"]["id"]] != [] and not auditoria("VIRTUAL_CASE_DOCUMENT_ADDED"))

            reconoce(td_acta, 68)
            r = subir_a(a_admin, org_a, pia, "pia-acta.pdf", pdf(171))
            rev("con 68 % de confianza también queda tipado (no hay confianza mínima)", r.status_code == 201 and r.json()["clasificacion"] == {"estado": "clasificado", "tipo": {"id": td_acta, "nombre": "Acta constitutiva"}, "confianza": 68.0}
                and (tipo_de(pia, r.json()["documento"]["id"]) or {}).get("id") == td_acta, r.text[:300])

            reconoce(None, 88)
            r = subir_a(a_admin, org_a, pia, "pia-rara.pdf", pdf(172))
            rev("«otro» (no es ninguno de los tipos): estado `sin_tipo`, con su confianza y sin tipo", r.status_code == 201 and r.json()["clasificacion"] == {"estado": "sin_tipo", "tipo": None, "confianza": 88.0}
                and tipo_de(pia, r.json()["documento"]["id"]) is None, r.text[:300])

            CONDUCTA.update(error=SinClasificador("sin clasificador"))
            r = subir_a(a_admin, org_a, pia, "pia-sin-clasificador.pdf", pdf(173))
            rev("una organización sin clasificador todavía: el archivo se SUBE igual y la respuesta dice `sin_clasificador`",
                r.status_code == 201 and r.json()["clasificacion"]["estado"] == "sin_clasificador" and r.json()["clasificacion"]["tipo"] is None and r.json()["documento"]["id"] in ids_docs_de(a_admin, org_a, pia), r.text[:300])

            CONDUCTA.update(error=RuntimeError("Document AI no contesta"))
            r = subir_a(a_admin, org_a, pia, "pia-falla.pdf", pdf(174))
            rev("si Document AI falla: 201, el archivo está en el expediente sin tipo, y se dice `no_clasificado` (nada de la falla viaja al navegador)",
                r.status_code == 201 and r.json()["clasificacion"]["estado"] == "no_clasificado" and "Document AI" not in r.text and r.json()["documento"]["id"] in ids_docs_de(a_admin, org_a, pia), r.text[:300])

            router_expedientes.ESPERA_CLASIFICAR_AL_SUBIR = 0.05
            CONDUCTA.update(error=None, demora=1.0, respuesta={"categoria": normalizar_nombre(td_ine), "confianza": 99})
            r = subir_a(a_admin, org_a, pia, "pia-lenta.pdf", pdf(175))
            rev("si el clasificador tarda más de la espera, no se cuelga: 201 y `no_clasificado`", r.status_code == 201 and r.json()["clasificacion"]["estado"] == "no_clasificado", r.text[:300])
            router_expedientes.ESPERA_CLASIFICAR_AL_SUBIR = espera

            reconoce(td_ine, 99)
            gastadas = len(LLAMADAS)
            rechazos = [
                subir_a(P["VIEWER"], org_a, pia, "viewer.pdf", pdf(176)).status_code == 403,
                subir_a(a_admin, org_a, pia, "gif.gif", b"GIF89a", "image/gif").status_code == 400,
                subir_a(a_admin, org_a, "expediente-" + "0" * 32, "x.pdf", pdf(178)).status_code == 404,
                subir_a(a_admin, org_b, pia, "x.pdf", pdf(179)).status_code == 403,
            ]
            rev("lo que se rechaza (rol que solo lee, formato, expediente inexistente, otra organización) NO le pregunta al clasificador: no se gasta", all(rechazos) and len(LLAMADAS) == gastadas, f"{rechazos} {len(LLAMADAS) - gastadas}")

            guardada = router_expedientes.SQLSERVER_HOST
            try:
                router_expedientes.SQLSERVER_HOST = ""
                r = subir_a(a_admin, org_a, pia, "pia-sin-base.pdf", pdf(180))
                rev("sin base (no hay dónde anotar la clasificación): `no_clasificado` y tampoco se gasta", r.status_code == 201 and r.json()["clasificacion"]["estado"] == "no_clasificado" and len(LLAMADAS) == gastadas)
            finally:
                router_expedientes.SQLSERVER_HOST = guardada

            titulo("21b · Clasificar un documento que YA está en un expediente (POST /ia/clasificar con `documento`) guarda la clasificación y no mueve nada")
            marta = crear(a_admin, org_a, "Marta Gil").json()["id"]
            marta_ine, marta_void = (subir(a_admin, org_a, f"marta-{n}.pdf", pdf(190 + i)).json()["documentoId"] for i, n in enumerate(("ine", "void")))
            meter(a_admin, org_a, marta, marta_ine)

            def clasificar_por_api(doc: str | None, n: int, bearer: str = a_admin):
                return c.post("/ia/clasificar", params={"tenant": org_a, **({"documento": doc} if doc else {})}, files={"archivo": (f"m{n}.pdf", pdf(n), "application/pdf")}, headers=cab(bearer))

            reconoce(td_ine, 91)
            r = clasificar_por_api(marta_ine, 190)
            tipo_marta = tipo_de(marta, marta_ine)
            rev("la respuesta de `/ia/clasificar` es la misma de siempre y la clasificación queda guardada: el expediente enseña el documento con su tipo y su confianza",
                r.status_code == 200 and r.json() == {"categoria": normalizar_nombre(td_ine), "confianza": 91} and tipo_marta is not None and tipo_marta["id"] == td_ine and tipo_marta["confianza"] == 91.0, r.text[:200])
            reconoce(td_acta, 77)
            r = clasificar_por_api(marta_void, 192)
            rev("un documento que no está en ningún expediente se clasifica sin error, y su tipo queda guardado", r.status_code == 200
                and next(d for d in docs(a_admin, org_a).json()["documentos"] if d["id"] == marta_void)["tipoDocumental"]["id"] == td_acta)
            reconoce(td_acta, 60)
            r = clasificar_por_api(None, 193)
            rev("sin `documento` no guarda nada: responde lo de siempre y el documento de antes queda como estaba", r.status_code == 200 and tipo_de(marta, marta_ine)["id"] == td_ine)
            reconoce(td_acta, 93)
            clasificar_por_api(marta_ine, 190)
            rev("volver a clasificar un documento es un hecho nuevo: manda la última opinión (ahora acta)", tipo_de(marta, marta_ine)["id"] == td_acta and tipo_de(marta, marta_ine)["confianza"] == 93.0)
        finally:
            servicio_ia.clasificar_documento = original
            router_expedientes.ESPERA_CLASIFICAR_AL_SUBIR = espera

    # Al final a propósito: cada petición crea un expediente más en Alfa, y las secciones de arriba cuentan los que hay.
    titulo("22 · Un cliente viejo que todavía manda `grupoId` (Moibe, 2026-11-06: se quitaron los grupos): se IGNORA, sea lo que sea, y el expediente se crea igual")
    antes_g = len(listar(a_admin, org_a).json()["expedientes"])
    variantes = (
        ("un id con la forma de uno que existió", "grupo-" + "b" * 32),
        ("un id que ni parece de grupo", "cualquier-cosa"),
        ("vacío", ""),
        ("nulo", None),
        ("un número", 123),
        ("una lista", ["grupo-" + "c" * 32]),
        ("un objeto", {"id": "grupo-" + "d" * 32}),
    )
    creados_g = []
    for que, valor in variantes:
        r = c.post("/expedientes/", json={"tenant": org_a, "nombre": f"Con grupoId {que}", "grupoId": valor}, headers=cab(a_admin))
        rev(f"con `grupoId` {que}: 201 y sin `grupo` (ni 400 ni 422)", r.status_code == 201 and "grupo" not in r.json() and "grupoId" not in r.json(), f"{r.status_code} {r.text[:150]}")
        creados_g.append(r.json().get("id"))
    rev("los siete se crearon, cada uno con su id", len(variantes) == 7 and len({i_ for i_ in creados_g if i_}) == 7 and len(listar(a_admin, org_a).json()["expedientes"]) == antes_g + 7)
    rev("y al abrirlos no traen grupo, igual que los demás", all("grupo" not in ver(a_admin, org_a, i_).json() for i_ in creados_g))
    rev("`grupoId` no se guarda en ningún lado: ni en el rastro de `CASE_CREATED`", all("grupoId" not in x and "grupoNombre" not in x for x in auditoria("CASE_CREATED") if x.get("expedienteId") in creados_g) and len([x for x in auditoria("CASE_CREATED") if x.get("expedienteId") in creados_g]) == 7)
    rev("y crear sigue exigiendo lo de siempre: sin nombre, 422; sin organización, 422", c.post("/expedientes/", json={"tenant": org_a, "grupoId": "grupo-" + "e" * 32}, headers=cab(a_admin)).status_code == 422 and c.post("/expedientes/", json={"nombre": "X", "grupoId": "grupo-" + "e" * 32}, headers=cab(a_admin)).status_code == 422)
    rev("un nombre malo con `grupoId` también se rechaza (400): el grupo no cambia las reglas del nombre", c.post("/expedientes/", json={"tenant": org_a, "nombre": "   ", "grupoId": "grupo-" + "e" * 32}, headers=cab(a_admin)).status_code == 400)

    # Al final a propósito (otra vez): esta sección crea expedientes y tipos en Alfa y Beta, y las de arriba cuentan los que hay.
    if SQL:
        titulo("23 · Subir un archivo a un expediente también EXTRAE sus datos (Moibe, 2026-11-06: «en las bandejas sí se extraen y en expedientes no»). Document AI se falsea: no se llama a Google")
        import asyncio
        import hashlib
        import time

        import routers.expedientes as router_expedientes
        from errores import SinClasificador
        from servicios import datos_extraidos as servicio_datos
        from servicios import ia as servicio_ia
        from servicios.esquema import normalizar_nombre

        VACIO_TIPO = {
            "descripcion": "", "vertical": "", "campos": [], "pasoMaximo": 3, "guardadoEn": "2026-11-06T00:00:00Z", "version": 1,
            "procesadorDisplayName": "", "ejemploDocumental": False, "activadoEn": None,
            "historialVersiones": [], "versionEnEdicion": None, "documentosEjemplo": [], "recortesPorDocumento": {},
        }

        def tipo_x(bearer: str, tenant: str, nombre: str, estado: str = "activo", procesador: str = "", version: str = "", **mas) -> str:
            """Un tipo documental SIN `calibradoEn`: desde el 2026-11-06 no existe el paso «Calibrar». `mas` agrega llaves a su caja (23f prueba las de antes)."""
            tid = "tipo-" + uuid.uuid4().hex
            datos = {**VACIO_TIPO, "nombre": nombre, "estado": estado, "procesadorId": procesador, "procesadorVersion": version, **mas}
            r_ = c.put(f"/tipos-documentales/{tid}", json={"tenant": tenant, "revision": 0, "datos": datos}, headers=cab(bearer))
            assert r_.status_code == 200, r_.text
            return tid

        def cambiar_estado(bearer: str, tenant: str, tid: str, estado: str) -> None:
            t_ = next(x for x in c.get("/tipos-documentales/", params={"tenant": tenant}, headers=cab(bearer)).json()["tipos"] if x["id"] == tid)
            r_ = c.put(f"/tipos-documentales/{tid}", json={"tenant": tenant, "revision": t_["revision"], "datos": {**t_["datos"], "estado": estado}}, headers=cab(bearer))
            assert r_.status_code == 200, r_.text

        FECHA_VIEJA = "2026-11-05T12:00:00Z"  # un `calibradoEn` de cuando existía el paso «Calibrar»: ya no cuenta para nada, pero los tipos de antes lo traen
        tx_ced = tipo_x(a_admin, org_a, "Cédula", procesador="proc-ced", version="7")
        tx_recibo = tipo_x(a_admin, org_a, "Recibo", procesador="proc-recibo", version="3")
        tx_sinproc = tipo_x(a_admin, org_a, "Sin procesador", procesador="")
        tx_borrador = tipo_x(a_admin, org_a, "Todavía borrador", estado="borrador", procesador="proc-borrador", version="1")
        tx_archivado = tipo_x(a_admin, org_a, "Ya archivado", estado="archivado", procesador="proc-archivado", version="1")
        tx_vuela = tipo_x(a_admin, org_a, "Se archiva en el aire", procesador="proc-vuela", version="1")
        tx_ced_b = tipo_x(b_admin, org_b, "Cédula de Beta", procesador="proc-ced-beta", version="9")

        CL = {"respuesta": None, "error": None, "antes_de_responder": None}
        EX = {"respuesta": None, "error": None, "demora": 0.0}
        LL_CL: list[int] = []
        LL_EX: list[tuple] = []

        async def clasificador_x(contenido, mime_type, tenant):
            LL_CL.append(len(contenido))
            if CL["antes_de_responder"]:
                CL["antes_de_responder"]()
            if CL["error"] is not None:
                raise CL["error"]
            return CL["respuesta"]

        async def extractor_x(procesador, contenido, mime_type, version="", *args, **kwargs):
            LL_EX.append((procesador, contenido, mime_type, version))
            if EX["demora"]:
                await asyncio.sleep(EX["demora"])
            if EX["error"] is not None:
                raise EX["error"]
            return EX["respuesta"](procesador, contenido) if callable(EX["respuesta"]) else EX["respuesta"]

        def campo_x(valor: str, conf: float = 91.0) -> dict:
            return {"value_raw": valor, "value_normalized": valor, "confianza": conf, "confianza_cruda": conf / 100, "metodo_confianza": "extractor_confidence", "page_number": 1}

        def datos_de_ejemplo(procesador: str, contenido: bytes, alerta: bool = False) -> dict:
            sello = hashlib.sha256(contenido).hexdigest()[:8]
            return {
                "confianza_promedio": 90.0, "confianza_minima": 85.0,
                "nombre": campo_x(f"Persona {sello}", 95.0), "folio": campo_x(f"{procesador}-{sello}", 85.0),
                "ocr": {"paginas": [{}]}, "_metadata": {"quality_alert": alerta, "paginas_totales": 1},
            }

        def reconoce_x(tipo_id: str | None, confianza: float = 90.0) -> None:
            CL.update(respuesta={"categoria": normalizar_nombre(tipo_id) if tipo_id else "otro", "confianza": confianza}, error=None, antes_de_responder=None)

        def extrae_ok() -> None:
            EX.update(respuesta=lambda p, b: datos_de_ejemplo(p, b), error=None, demora=0.0)

        def datos_x(bearer: str, tenant: str, doc: str):
            return c.get(f"/expedientes/documentos/{doc}/datos", params={"tenant": tenant}, headers=cab(bearer))

        def corridas_de(doc: str) -> list[tuple]:
            """Las corridas guardadas de un documento, por la base (con el usuario de la API, que lee)."""
            con_ = BASE_SQL["conectar"]("usrNexus", BASE_SQL["app"], BASE_SQL["base"])
            try:
                k_ = con_.cursor()
                k_.execute(
                    "SELECT r.publicId, r.status, r.processorId, r.processorVersion, r.documentTypePublicId, d.tenant, r.errorDetail, "
                    "(SELECT count(*) FROM nexus.entityFacts f WHERE f.runId = r.runId) FROM nexus.extractionRuns r JOIN nexus.documents d ON d.documentId = r.documentId "
                    "WHERE d.publicId = ? ORDER BY r.runId",
                    doc,
                )
                return [tuple(f_) for f_ in k_.fetchall()]
            finally:
                con_.close()

        def corridas_totales() -> int:
            con_ = BASE_SQL["conectar"]("usrNexus", BASE_SQL["app"], BASE_SQL["base"])
            try:
                return con_.cursor().execute("SELECT count(*) FROM nexus.extractionRuns").fetchone()[0]
            finally:
                con_.close()

        def en_exp(bearer: str, tenant: str, exp: str, doc: str) -> dict | None:
            return next((d_ for d_ in ver(bearer, tenant, exp).json()["documentos"] if d_["id"] == doc), None)

        orig_clas, orig_ext = servicio_ia.clasificar_documento, servicio_ia.extraer_con_procesador
        orig_registrar, orig_obtener = servicio_datos.registrar_extraccion, router_expedientes.tipos_documentales.obtener
        orig_espera_ex, orig_espera_cl = router_expedientes.ESPERA_EXTRAER_AL_SUBIR, router_expedientes.ESPERA_CLASIFICAR_AL_SUBIR
        servicio_ia.clasificar_documento = clasificador_x
        servicio_ia.extraer_con_procesador = extractor_x
        try:
            ex_exp = crear(a_admin, org_a, "Extracción al subir").json()["id"]
            ex_exp_b = crear(b_admin, org_b, "Extracción al subir de Beta").json()["id"]

            # ---- (a) el caso feliz
            titulo("23a · Tipo activo y con procesador (nada más): se extrae, de verdad queda guardado y se llama a Document AI UNA vez")
            reconoce_x(tx_ced, 92.5)
            extrae_ok()
            bytes_a = pdf(2301)
            antes_corridas = corridas_totales()
            r = subir_a(a_admin, org_a, ex_exp, "cedula.pdf", bytes_a)
            j = r.json() if r.status_code == 201 else {}
            doc_a = (j.get("documento") or {}).get("id", "")
            rev("201 y `extraccion.estado` es `extraido`, con el id de la corrida", r.status_code == 201 and j["extraccion"]["estado"] == "extraido" and str(j["extraccion"].get("extraccionId", "")).startswith("extraccion-") and sorted(j["extraccion"]) == ["estado", "extraccionId"], r.text[:300])
            rev("la clasificación sigue diciendo lo suyo (clasificado, con el tipo y la confianza)", j["clasificacion"] == {"estado": "clasificado", "tipo": {"id": tx_ced, "nombre": "Cédula"}, "confianza": 92.5}, str(j.get("clasificacion")))
            rev("Document AI de extracción se llamó UNA sola vez, con el procesador y la versión DEL TIPO, el mime y los bytes del archivo", LL_EX == [("proc-ced", bytes_a, "application/pdf", "7")], str([(x[0], len(x[1]), x[2], x[3]) for x in LL_EX]))
            rev("el clasificador se llamó una vez y solo con los bytes del archivo", LL_CL[-1:] == [len(bytes_a)])
            x = datos_x(a_admin, org_a, doc_a).json()
            rev("`GET …/documentos/{id}/datos` devuelve la corrida: la misma que dijo la subida, del tipo, con su procesador y versión, estado ok y una sola corrida",
                x["extraccion"] is not None and x["extraccion"]["id"] == j["extraccion"]["extraccionId"] and x["extraccion"]["estado"] == "ok" and x["extraccion"]["tipoDocumental"]["id"] == tx_ced
                and x["extraccion"]["procesador"] == {"id": "proc-ced", "version": "7"} and x["extraccion"]["totalCorridas"] == 1, str(x["extraccion"]))
            sello = hashlib.sha256(bytes_a).hexdigest()[:8]
            campos_a = {f_["ruta"]: f_ for f_ in x["campos"]}
            rev("y sus datos: un renglón por campo con el valor y la confianza que dijo el extractor (de ESTE archivo)", sorted(campos_a) == ["folio", "nombre"] and campos_a["nombre"]["valor"] == f"Persona {sello}" and campos_a["nombre"]["confianza"] == 95.0 and campos_a["folio"]["valor"] == f"proc-ced-{sello}", str(campos_a))
            filas = corridas_de(doc_a)
            rev("en la base: UNA corrida `ok` con 2 `entityFacts`, del procesador y la versión del tipo, en la organización de quien subió",
                len(filas) == 1 and filas[0][1] == "ok" and filas[0][2] == "proc-ced" and filas[0][3] == "7" and filas[0][4] == tx_ced and filas[0][5] == org_a and filas[0][7] == 2 and filas[0][0] == j["extraccion"]["extraccionId"], str(filas))
            rev("y solo esa corrida se agregó (ninguna de más)", corridas_totales() == antes_corridas + 1)
            rev("el documento dice que es de ese tipo y quedó en el expediente", (en_exp(a_admin, org_a, ex_exp, doc_a) or {}).get("tipoDocumental", {}).get("id") == tx_ced)
            rev("lo ve la gente de la organización y la otra no (404)", datos_x(P["VIEWER"], org_a, doc_a).status_code == 200 and datos_x(b_admin, org_b, doc_a).status_code == 404)

            # ---- (b) no reconocido
            titulo("23b · Document AI no reconoció los campos (`_metadata.quality_alert`)")
            EX.update(respuesta=lambda p, b: datos_de_ejemplo(p, b, alerta=True))
            r = subir_a(a_admin, org_a, ex_exp, "ilegible.pdf", pdf(2302))
            j = r.json()
            doc_b = j["documento"]["id"]
            rev("201 y `extraccion.estado` es `no_reconocido`, con el id de la corrida que quedó", r.status_code == 201 and j["extraccion"]["estado"] == "no_reconocido" and str(j["extraccion"].get("extraccionId", "")).startswith("extraccion-"), r.text[:300])
            filas = corridas_de(doc_b)
            rev("la corrida quedó con estado `no_reconocido`", len(filas) == 1 and filas[0][1] == "no_reconocido" and datos_x(a_admin, org_a, doc_b).json()["extraccion"]["estado"] == "no_reconocido", str(filas))
            rev("el archivo sigue en el expediente y con su tipo", (en_exp(a_admin, org_a, ex_exp, doc_b) or {}).get("tipoDocumental", {}).get("id") == tx_ced)

            # ---- (c) falla
            titulo("23c · La extracción LANZA: la subida sigue siendo 201 y la corrida fallida queda anotada")
            EX.update(error=RuntimeError("Google caído: secreto-interno-123"))
            r = subir_a(a_admin, org_a, ex_exp, "falla.pdf", pdf(2303))
            j = r.json() if r.status_code == 201 else {}
            doc_c = (j.get("documento") or {}).get("id", "")
            rev("201 con `extraccion.estado == error` y sin `extraccionId`", r.status_code == 201 and j["extraccion"] == {"estado": "error"}, r.text[:300])
            rev("nada de la falla viaja al navegador", "secreto-interno" not in r.text and "Google" not in r.text)
            filas = corridas_de(doc_c)
            rev("queda UNA corrida con estado `error`, su motivo, sin datos y del tipo y procesador correctos", len(filas) == 1 and filas[0][1] == "error" and "secreto-interno-123" in (filas[0][6] or "") and filas[0][7] == 0 and filas[0][2] == "proc-ced" and filas[0][4] == tx_ced, str(filas))
            xc = datos_x(a_admin, org_a, doc_c).json()
            rev("`/datos` dice que la última corrida falló y no inventa campos", xc["extraccion"]["estado"] == "error" and xc["campos"] == [], str(xc["extraccion"]))
            rev("el archivo está en el expediente, con su tipo", (en_exp(a_admin, org_a, ex_exp, doc_c) or {}).get("tipoDocumental", {}).get("id") == tx_ced)
            n_antes = corridas_totales()
            servicio_datos.registrar_extraccion = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("la base no contesta"))
            try:
                r = subir_a(a_admin, org_a, ex_exp, "falla-doble.pdf", pdf(2304))
            finally:
                servicio_datos.registrar_extraccion = orig_registrar
            rev("si ADEMÁS no se puede anotar la corrida fallida: sigue 201 y `error`", r.status_code == 201 and r.json()["extraccion"] == {"estado": "error"} and r.json()["documento"]["id"] in ids_docs_de(a_admin, org_a, ex_exp), r.text[:300])
            rev("y no quedó ninguna corrida", corridas_totales() == n_antes)
            EX.update(error=None)

            # ---- (d) tarda
            titulo("23d · La extracción tarda más que el tope: 201, `no_extraido` y no queda corrida")
            router_expedientes.ESPERA_EXTRAER_AL_SUBIR = 0.05
            EX.update(demora=1.0)
            n_antes = corridas_totales()
            t0 = time.monotonic()
            r = subir_a(a_admin, org_a, ex_exp, "lenta.pdf", pdf(2305))
            dur = time.monotonic() - t0
            j = r.json() if r.status_code == 201 else {}
            router_expedientes.ESPERA_EXTRAER_AL_SUBIR = orig_espera_ex
            EX.update(demora=0.0)
            rev("201 con `extraccion.estado == no_extraido`", r.status_code == 201 and j["extraccion"] == {"estado": "no_extraido"}, r.text[:300])
            rev("no se quedó esperando: respondió bastante antes de que Document AI contestara (1 s)", dur < 0.9, f"{dur:.2f} s")
            time.sleep(1.2)
            rev("pasada la demora tampoco aparece una corrida (la tarea se canceló) y el archivo sigue con su tipo", corridas_totales() == n_antes and corridas_de(j["documento"]["id"]) == [] and (en_exp(a_admin, org_a, ex_exp, j["documento"]["id"]) or {}).get("tipoDocumental", {}).get("id") == tx_ced)
            rev("el tope de la extracción y el del clasificador son independientes: el del clasificador no se tocó", router_expedientes.ESPERA_CLASIFICAR_AL_SUBIR == orig_espera_cl)

            extrae_ok()  # (b) dejó a Document AI falseado diciendo «no reconocido»: de aquí en adelante, extrae bien
            # ---- (e) sin procesador
            titulo("23e · Tipo activo SIN procesador (o con el procesador en blanco): `sin_procesador` y no se llama a Google")
            n_ex, n_corr = len(LL_EX), corridas_totales()
            reconoce_x(tx_sinproc)
            r = subir_a(a_admin, org_a, ex_exp, "sin-procesador.pdf", pdf(2306))
            rev("sin procesador: 201 y `sin_procesador` (la clasificación sí dijo el tipo)", r.status_code == 201 and r.json()["extraccion"] == {"estado": "sin_procesador"} and r.json()["clasificacion"]["estado"] == "clasificado", r.text[:300])
            # un procesador con solo espacios cuenta como ausente
            tx_blanco = tipo_x(a_admin, org_a, "Procesador en blanco", procesador="   ")
            reconoce_x(tx_blanco)
            r3 = subir_a(a_admin, org_a, ex_exp, "blanco.pdf", pdf(2308))
            rev("un procesador en blanco (solo espacios) también es `sin_procesador`", r3.status_code == 201 and r3.json()["extraccion"] == {"estado": "sin_procesador"} and r3.json()["clasificacion"]["estado"] == "clasificado", r3.text[:300])
            rev("a Document AI de extracción NO se le llamó ni una vez, y no quedó ninguna corrida", len(LL_EX) == n_ex and corridas_totales() == n_corr, f"{len(LL_EX) - n_ex} llamadas")
            rev("los dos archivos siguen en el expediente con su tipo", all((en_exp(a_admin, org_a, ex_exp, x_.json()["documento"]["id"]) or {}).get("tipoDocumental") for x_ in (r, r3)))
            rev("`/datos` de esos documentos: clasificación sí, extracción no", datos_x(a_admin, org_a, r.json()["documento"]["id"]).json()["extraccion"] is None and datos_x(a_admin, org_a, r.json()["documento"]["id"]).json()["clasificacion"]["tipoDocumental"]["id"] == tx_sinproc)

            # ---- (f) sin el paso «Calibrar»
            titulo("23f · SIN el paso «Calibrar» (2026-11-06): un tipo activo con procesador extrae aunque no tenga `calibradoEn` (sin la llave, nulo o vacío), y uno de antes que sí lo trae, también")
            variantes_f = (
                ("sin la llave `calibradoEn`", {}),
                ("con `calibradoEn` nulo", {"calibradoEn": None}),
                ("con `calibradoEn` vacío", {"calibradoEn": ""}),
                ("con `calibradoEn` de cuando existía el paso (una fecha)", {"calibradoEn": FECHA_VIEJA}),
            )
            respuestas_f = []
            for n_f, (que_f, mas_f) in enumerate(variantes_f):
                proc_f, version_f = f"proc-f{n_f}", str(10 + n_f)
                tid_f = tipo_x(a_admin, org_a, f"Tipo de la prueba 23f nº {n_f}", procesador=proc_f, version=version_f, **mas_f)
                guardado_f = next(t_["datos"] for t_ in c.get("/tipos-documentales/", params={"tenant": org_a}, headers=cab(a_admin)).json()["tipos"] if t_["id"] == tid_f)
                rev(f"{que_f}: el tipo quedó guardado como se mandó (activo, con su procesador y su `calibradoEn` tal cual, o sin la llave)",
                    guardado_f["estado"] == "activo" and guardado_f["procesadorId"] == proc_f and ("calibradoEn" in guardado_f) == ("calibradoEn" in mas_f) and guardado_f.get("calibradoEn") == mas_f.get("calibradoEn"), str(guardado_f))
                reconoce_x(tid_f)
                bytes_f = pdf(2370 + n_f)
                n_ex, n_corr = len(LL_EX), corridas_totales()
                r_f = subir_a(a_admin, org_a, ex_exp, f"marca-{n_f}.pdf", bytes_f)
                respuestas_f.append(r_f)
                j_f = r_f.json() if r_f.status_code == 201 else {}
                doc_f = (j_f.get("documento") or {}).get("id", "")
                ex_f = j_f.get("extraccion") or {}
                rev(f"{que_f}: 201 y `extraccion.estado` es `extraido`, con el id de la corrida", r_f.status_code == 201 and ex_f.get("estado") == "extraido" and str(ex_f.get("extraccionId", "")).startswith("extraccion-"), r_f.text[:300])
                rev(f"{que_f}: Document AI se llamó UNA sola vez, con el procesador y la versión DEL TIPO, el mime y los bytes del archivo", LL_EX[n_ex:] == [(proc_f, bytes_f, "application/pdf", version_f)], str([(x_[0], len(x_[1]), x_[2], x_[3]) for x_ in LL_EX[n_ex:]]))
                filas = corridas_de(doc_f)
                rev(f"{que_f}: queda UNA corrida `ok` del tipo y su procesador, con sus 2 `entityFacts`, y solo esa se agregó", len(filas) == 1 and filas[0][1] == "ok" and filas[0][2] == proc_f and filas[0][3] == version_f and filas[0][4] == tid_f and filas[0][7] == 2 and corridas_totales() == n_corr + 1, str(filas))
                dx_f = datos_x(a_admin, org_a, doc_f).json() if doc_f else {}
                rev(f"{que_f}: `/datos` trae los campos de ESE archivo", (dx_f.get("extraccion") or {}).get("estado") == "ok" and {f_["ruta"]: f_["valor"] for f_ in dx_f.get("campos", [])}.get("nombre") == f"Persona {hashlib.sha256(bytes_f).hexdigest()[:8]}", str(dx_f.get("extraccion")))
            rev("las cuatro respuestas llegaron y ninguna dice `sin_calibrar`: ese estado ya no existe", len(respuestas_f) == 4 and all("sin_calibrar" not in x_.text for x_ in respuestas_f))

            # ---- (g) sin tipo
            titulo("23g · Si no hay tipo (`sin_tipo`, `sin_clasificador`, `no_clasificado`, sin base): `extraccion.estado == sin_tipo` y NO se llama a Google")
            n_ex, n_corr = len(LL_EX), corridas_totales()
            reconoce_x(None, 88)
            ra = subir_a(a_admin, org_a, ex_exp, "otro.pdf", pdf(2310))
            CL.update(error=SinClasificador("sin clasificador"))
            rb = subir_a(a_admin, org_a, ex_exp, "sin-clasificador.pdf", pdf(2311))
            CL.update(error=RuntimeError("Document AI no contesta"))
            rc = subir_a(a_admin, org_a, ex_exp, "clasificador-falla.pdf", pdf(2312))
            reconoce_x(tx_ced)
            guardada = router_expedientes.SQLSERVER_HOST
            try:
                router_expedientes.SQLSERVER_HOST = ""
                rd = subir_a(a_admin, org_a, ex_exp, "sin-base.pdf", pdf(2313))
            finally:
                router_expedientes.SQLSERVER_HOST = guardada
            estados = [(x_.status_code, x_.json()["clasificacion"]["estado"], x_.json()["extraccion"]) for x_ in (ra, rb, rc, rd)]
            rev("`otro`, sin clasificador, clasificador caído y sin base: 201, la clasificación dice lo suyo y la extracción es `sin_tipo`",
                estados == [(201, "sin_tipo", {"estado": "sin_tipo"}), (201, "sin_clasificador", {"estado": "sin_tipo"}), (201, "no_clasificado", {"estado": "sin_tipo"}), (201, "no_clasificado", {"estado": "sin_tipo"})], str(estados))
            rev("a Document AI de extracción no se le llamó y no quedó ninguna corrida", len(LL_EX) == n_ex and corridas_totales() == n_corr)
            router_expedientes.ESPERA_CLASIFICAR_AL_SUBIR = 0.05
            CL.update(error=None, respuesta={"categoria": normalizar_nombre(tx_ced), "confianza": 99})
            orig_cl_x = servicio_ia.clasificar_documento

            async def clasificador_lento(contenido, mime_type, tenant):
                await asyncio.sleep(1.0)
                return CL["respuesta"]

            servicio_ia.clasificar_documento = clasificador_lento
            try:
                re_ = subir_a(a_admin, org_a, ex_exp, "clasificador-lento.pdf", pdf(2314))
            finally:
                servicio_ia.clasificar_documento = orig_cl_x
                router_expedientes.ESPERA_CLASIFICAR_AL_SUBIR = orig_espera_cl
            rev("si el clasificador tarda más que su tope: `no_clasificado` y `sin_tipo`, y tampoco se extrae", re_.status_code == 201 and re_.json()["clasificacion"]["estado"] == "no_clasificado" and re_.json()["extraccion"] == {"estado": "sin_tipo"} and len(LL_EX) == n_ex)
            reconoce_x(tx_ced_b)  # el tipo de OTRA organización: para Alfa no existe
            rf = subir_a(a_admin, org_a, ex_exp, "tipo-ajeno.pdf", pdf(2315))
            rev("un clasificador que dice el tipo de OTRA organización no extrae (Alfa no tiene ese tipo): `sin_tipo` y no se llama a Google", rf.status_code == 201 and rf.json()["clasificacion"]["estado"] == "sin_tipo" and rf.json()["extraccion"] == {"estado": "sin_tipo"} and len(LL_EX) == n_ex, rf.text[:300])

            # ---- (h) borrador / archivado
            titulo("23h · Un tipo que no está activo (borrador, archivado, o que se archivó entre clasificar y extraer) no se extrae")
            n_ex, n_corr = len(LL_EX), corridas_totales()
            reconoce_x(tx_borrador)
            rb1 = subir_a(a_admin, org_a, ex_exp, "borrador.pdf", pdf(2316))
            reconoce_x(tx_archivado)
            rb2 = subir_a(a_admin, org_a, ex_exp, "archivado.pdf", pdf(2317))
            rev("el clasificador pudo decir un tipo borrador o archivado, pero NO se extrae (`sin_tipo`, sin llamar a Google ni guardar corrida)",
                rb1.status_code == 201 and rb2.status_code == 201 and rb1.json()["extraccion"] == {"estado": "sin_tipo"} and rb2.json()["extraccion"] == {"estado": "sin_tipo"} and len(LL_EX) == n_ex and corridas_totales() == n_corr,
                f"{rb1.text[:150]} {rb2.text[:150]}")
            reconoce_x(tx_vuela)
            CL["antes_de_responder"] = lambda: cambiar_estado(a_admin, org_a, tx_vuela, "archivado")
            rv = subir_a(a_admin, org_a, ex_exp, "se-archiva-en-el-aire.pdf", pdf(2318))
            CL["antes_de_responder"] = None
            rev("un tipo que se archiva JUSTO mientras se clasificaba (era activo al empezar) ya no se extrae: `sin_tipo` y no se llama a Google",
                rv.status_code == 201 and rv.json()["clasificacion"]["estado"] == "clasificado" and rv.json()["extraccion"] == {"estado": "sin_tipo"} and len(LL_EX) == n_ex and corridas_totales() == n_corr, rv.text[:300])
            cambiar_estado(a_admin, org_a, tx_vuela, "activo")
            reconoce_x(tx_vuela)
            rv2 = subir_a(a_admin, org_a, ex_exp, "otra-vez-activo.pdf", pdf(2319))
            rev("y al volver a estar activo, vuelve a extraerse (la regla lee el estado vigente, no uno guardado)", rv2.json()["extraccion"]["estado"] == "extraido" and LL_EX[-1][0] == "proc-vuela" and LL_EX[-1][3] == "1", rv2.text[:300])

            # ---- (i) el tenant
            titulo("23i · La extracción se guarda en la organización de quien sube, nunca en otra")
            reconoce_x(tx_ced_b)
            bytes_b = pdf(2320)
            rb_ = subir_a(b_admin, org_b, ex_exp_b, "cedula-beta.pdf", bytes_b)
            jb = rb_.json() if rb_.status_code == 201 else {}
            doc_bb = (jb.get("documento") or {}).get("id", "")
            rev("Beta sube un archivo de SU tipo: `extraido`, y Document AI se llamó con el procesador y la versión de SU tipo", rb_.status_code == 201 and jb["extraccion"]["estado"] == "extraido" and LL_EX[-1] == ("proc-ced-beta", bytes_b, "application/pdf", "9"), rb_.text[:300])
            filas = corridas_de(doc_bb)
            rev("la corrida quedó en la base de Beta (tenant Beta, su tipo, su procesador)", len(filas) == 1 and filas[0][5] == org_b and filas[0][4] == tx_ced_b and filas[0][2] == "proc-ced-beta" and filas[0][7] == 2, str(filas))
            rev("Beta lee sus datos y Alfa NO los ve (404, ni con su tenant ni poniendo el de Beta: 403)", datos_x(b_admin, org_b, doc_bb).json()["extraccion"]["estado"] == "ok" and datos_x(a_admin, org_a, doc_bb).status_code == 404 and datos_x(a_admin, org_b, doc_bb).status_code == 403)
            rev("ninguna corrida de Alfa mencionó a Beta ni al revés", all(f_[5] == org_a for d_ in ids_docs(a_admin, org_a) for f_ in corridas_de(d_)) and all(f_[5] == org_b for d_ in ids_docs(b_admin, org_b) for f_ in corridas_de(d_)))
            n_ex, n_corr = len(LL_EX), corridas_totales()
            r_ = subir_a(a_admin, org_b, ex_exp_b, "colado.pdf", pdf(2321))
            r2_ = subir_a(a_admin, org_a, ex_exp_b, "expediente-ajeno.pdf", pdf(2322))
            rev("subir con el tenant ajeno (403) o a un expediente ajeno (404) no extrae ni gasta nada", r_.status_code == 403 and r2_.status_code == 404 and len(LL_EX) == n_ex and corridas_totales() == n_corr)

            # ---- (j) varios archivos
            titulo("23j · Varios archivos seguidos (y a la vez): cada uno guarda su PROPIA corrida con sus propios datos")
            extrae_ok()
            seguidos = []
            n_corr = corridas_totales()
            for n_, (tid_, proc_) in enumerate(((tx_ced, "proc-ced"), (tx_recibo, "proc-recibo"), (tx_ced, "proc-ced"), (tx_sinproc, None))):
                reconoce_x(tid_)
                bts = pdf(2330 + n_)
                rr = subir_a(a_admin, org_a, ex_exp, f"seguido-{n_}.pdf", bts)
                seguidos.append((rr, proc_, bts))
            ids_s = [x_[0].json()["documento"]["id"] for x_ in seguidos]
            rev("los cuatro: 201, y los tres con tipo que tiene procesador `extraido`, el cuarto (tipo sin procesador) `sin_procesador`", [x_[0].status_code for x_ in seguidos] == [201] * 4 and [x_[0].json()["extraccion"]["estado"] for x_ in seguidos] == ["extraido", "extraido", "extraido", "sin_procesador"], str([x_[0].text[:100] for x_ in seguidos]))
            rev("cada uno tiene UNA corrida, distinta de las demás, con SU procesador", all(len(corridas_de(i_)) == 1 for i_ in ids_s[:3]) and corridas_de(ids_s[3]) == [] and len({corridas_de(i_)[0][0] for i_ in ids_s[:3]}) == 3 and [corridas_de(i_)[0][2] for i_ in ids_s[:3]] == ["proc-ced", "proc-recibo", "proc-ced"], str([corridas_de(i_) for i_ in ids_s]))
            ok_datos = True
            for rr, proc_, bts in seguidos[:3]:
                cs = {f_["ruta"]: f_["valor"] for f_ in datos_x(a_admin, org_a, rr.json()["documento"]["id"]).json()["campos"]}
                ok_datos = ok_datos and cs.get("nombre") == f"Persona {hashlib.sha256(bts).hexdigest()[:8]}" and cs.get("folio", "").startswith(proc_ + "-")
            rev("y los datos de cada documento son los de SU archivo (nada se cruza entre ellos)", ok_datos)
            rev("solo se agregaron tres corridas", corridas_totales() == n_corr + 3)
            # a la vez
            reconoce_x(tx_ced)
            n_corr = corridas_totales()
            n_ex = len(LL_EX)
            with ThreadPoolExecutor(max_workers=4) as pool:
                paralelas = list(pool.map(lambda i_: subir_a(a_admin, org_a, ex_exp, f"paralelo-{i_}.pdf", pdf(2340 + i_)), range(4)))
            ids_p = [x_.json()["documento"]["id"] for x_ in paralelas]
            rev("cuatro subidas a la vez: las cuatro 201 y `extraido`, cada una con su corrida y sus datos",
                [x_.status_code for x_ in paralelas] == [201] * 4 and all(x_.json()["extraccion"]["estado"] == "extraido" for x_ in paralelas) and corridas_totales() == n_corr + 4 and len(LL_EX) == n_ex + 4
                and all(len(corridas_de(i_)) == 1 for i_ in ids_p) and len({corridas_de(i_)[0][0] for i_ in ids_p}) == 4, str([x_.text[:80] for x_ in paralelas]))
            rev("y cada una trae los datos de su propio archivo", all({f_["ruta"]: f_["valor"] for f_ in datos_x(a_admin, org_a, ids_p[i_]).json()["campos"]}.get("nombre") == f"Persona {hashlib.sha256(pdf(2340 + i_)).hexdigest()[:8]}" for i_ in range(4)))

            # ---- (k) registrar falla
            titulo("23k · No se pudo GUARDAR la corrida (o leer el tipo): `no_extraido`, y el archivo sigue subido y con su tipo")
            reconoce_x(tx_ced, 90)
            extrae_ok()
            n_corr = corridas_totales()
            servicio_datos.registrar_extraccion = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("la base no contesta"))
            try:
                rk = subir_a(a_admin, org_a, ex_exp, "no-se-guarda.pdf", pdf(2350))
            finally:
                servicio_datos.registrar_extraccion = orig_registrar
            jk = rk.json() if rk.status_code == 201 else {}
            rev("`registrar_extraccion` lanza: 201 y `no_extraido` (sin `extraccionId`)", rk.status_code == 201 and jk["extraccion"] == {"estado": "no_extraido"} and "la base" not in rk.text, rk.text[:300])
            rev("el archivo sigue en el expediente, con su tipo y su clasificación, y no quedó ninguna corrida", jk["documento"]["id"] in ids_docs_de(a_admin, org_a, ex_exp) and jk["clasificacion"]["estado"] == "clasificado" and (en_exp(a_admin, org_a, ex_exp, jk["documento"]["id"]) or {}).get("tipoDocumental", {}).get("id") == tx_ced and corridas_totales() == n_corr)
            router_expedientes.tipos_documentales.obtener = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no se pudo leer el tipo"))
            try:
                n_ex = len(LL_EX)
                rl = subir_a(a_admin, org_a, ex_exp, "no-lee-el-tipo.pdf", pdf(2351))
            finally:
                router_expedientes.tipos_documentales.obtener = orig_obtener
            rev("si ni se puede leer el tipo: 201, `no_extraido` y no se llama a Google", rl.status_code == 201 and rl.json()["extraccion"] == {"estado": "no_extraido"} and len(LL_EX) == n_ex and rl.json()["clasificacion"]["estado"] == "clasificado", rl.text[:300])
            # lo reanudado: ya todo normal otra vez
            rm = subir_a(a_admin, org_a, ex_exp, "ya-normal.pdf", pdf(2352))
            rev("quitadas las fallas, la siguiente subida vuelve a extraer", rm.json()["extraccion"]["estado"] == "extraido")

            titulo("23l · Lo que se rechaza no extrae ni gasta, y la bandeja no cambió")
            n_ex, n_corr, n_cl = len(LL_EX), corridas_totales(), len(LL_CL)
            rechazos = [
                subir_a(P["VIEWER"], org_a, ex_exp, "viewer.pdf", pdf(2360)).status_code == 403,
                subir_a(a_admin, org_a, ex_exp, "gif.gif", b"GIF89a", "image/gif").status_code == 400,
                subir_a(a_admin, org_a, "expediente-" + "0" * 32, "x.pdf", pdf(2361)).status_code == 404,
                subir_a(a_admin, org_b, ex_exp, "x.pdf", pdf(2362)).status_code == 403,
                subir_a(a_admin, org_a, ex_exp, "vacio.pdf", b"").status_code == 400,
            ]
            rev("rol que solo lee, formato, expediente inexistente, otra organización y archivo vacío: nadie llama a Document AI y no queda corrida", all(rechazos) and len(LL_EX) == n_ex and len(LL_CL) == n_cl and corridas_totales() == n_corr, str(rechazos))
            rb_ban = subir(a_admin, org_a, "por-bandeja.pdf", pdf(2363))
            rev("la bandeja (`POST /bandeja/`) NO extrae al subir: sigue como antes (el pipeline extrae desde el front)", rb_ban.status_code == 201 and len(LL_EX) == n_ex and corridas_de(rb_ban.json()["documentoId"]) == [])
            rev("`POST /expedientes/documentos` (carga manual sin expediente) tampoco extrae", manual(a_admin, org_a, "manual-sin-exp.pdf", pdf(2364)).status_code == 201 and len(LL_EX) == n_ex)
        finally:
            servicio_ia.clasificar_documento = orig_clas
            servicio_ia.extraer_con_procesador = orig_ext
            servicio_datos.registrar_extraccion = orig_registrar
            router_expedientes.tipos_documentales.obtener = orig_obtener
            router_expedientes.ESPERA_EXTRAER_AL_SUBIR = orig_espera_ex
            router_expedientes.ESPERA_CLASIFICAR_AL_SUBIR = orig_espera_cl
except Exception:  # noqa: BLE001 — una excepción que no es una aserción (p. ej. un resultado sin la forma que se esperaba) se anota como falla, y la prueba llega a su resumen
    import traceback

    traceback.print_exc()
    rev("la prueba se cayó por una EXCEPCIÓN antes de terminar (no por una aserción): " + traceback.format_exc().strip().splitlines()[-1][:200], False)
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
