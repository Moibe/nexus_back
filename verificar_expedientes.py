"""¿Los expedientes y sus documentos funcionan de punta a punta? (documento = nace al entrar; expediente = conjunto; muchos a muchos)

    .venv/Scripts/python verificar_expedientes.py

OFFLINE y sin tocar nada tuyo, como los demás `verificar_*`: el almacén es una carpeta temporal.

Lo que se prueba:

- un documento NACE al entrar por la bandeja, queda en el «void» (sin ningún expediente), conserva lo que se sabe de cómo llegó, sobrevive a
  que su entrada se retire de la bandeja, y subir dos veces el mismo archivo da dos documentos;
- un TIPO de expediente («expediente médico», «de un choque») se crea con nombre único por organización (sin distinguir mayúsculas ni espacios);
  lo crean quienes administran y lo leen todos; todo expediente nace de un tipo de su organización, es su instancia, y se filtra y cuenta por tipo;
- el ESQUEMA de un tipo (los tipos documentales que lo describen) se reemplaza completo, en orden, sin repetidos y solo con tipos documentales de
  la misma organización; DESCRIBE Y NO RESTRINGE: los expedientes de un tipo con esquema siguen recibiendo cualquier documento;
- un expediente se crea con un nombre limpio (y dos pueden llamarse igual); los nombres que no sirven se rechazan;
- un documento puede estar en VARIOS expedientes a la vez; meterlo dos veces no repite nada; sacarlo de su último expediente lo devuelve
  al void sin borrarlo; el orden es el de entrada;
- lo de otra organización responde como si no existiera (404) y su tenant, 403 `sin_acceso`;
- crear, meter y sacar piden un rol que organice (Compliance, Auditor y Viewer solo leen) y cada rechazo deja `ROLE_DENIED`;
- crear, meter y sacar dejan rastro con quién; meter un documento que ya estaba, no;
- el tope de documentos por expediente, un corte a media escritura y dos peticiones simultáneas.
"""

import json
import os
import shutil
import tempfile
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-expedientes-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import auth, bandeja, documentos, expedientes, llaves_cliente, roles, tipos_expediente, usuarios  # noqa: E402

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


#: Un tipo de expediente de cada organización (se llena en el montaje): todo expediente nace de un tipo.
TIPOS: dict[str, str] = {}
TIPO_DE_ESTA_ORG = object()


def crear(bearer: str, tenant: str, nombre, tipo=TIPO_DE_ESTA_ORG):
    """Crea un expediente. Sin `tipo`, usa el tipo de esa organización; con `tipo=None`, no manda `tipoId`."""
    tipo_id = TIPOS.get(tenant) if tipo is TIPO_DE_ESTA_ORG else tipo
    cuerpo = {"tenant": tenant, "nombre": nombre, **({"tipoId": tipo_id} if tipo_id is not None else {})}
    return c.post("/expedientes/", json=cuerpo, headers=cab(bearer))


def crear_tipo(bearer: str, tenant: str, nombre):
    return c.post("/tipos-expediente/", json={"tenant": tenant, "nombre": nombre}, headers=cab(bearer))


def listar_tipos(bearer: str, tenant: str):
    return c.get("/tipos-expediente/", params={"tenant": tenant}, headers=cab(bearer))


def ver_tipo(bearer: str, tenant: str, tipo: str):
    return c.get(f"/tipos-expediente/{tipo}", params={"tenant": tenant}, headers=cab(bearer))


def crear_tipo_documental(bearer: str, tenant: str, nombre: str) -> str:
    """Un tipo documental en la biblioteca de esa organización (para armar el esquema de un tipo de expediente)."""
    tid = "tipo-" + uuid.uuid4().hex
    r = c.put(f"/tipos-documentales/{tid}", json={"tenant": tenant, "revision": 0, "datos": {"nombre": nombre, "descripcion": "", "estado": "activo", "procesadorId": ""}}, headers=cab(bearer))
    assert r.status_code == 200, r.text
    return tid


def esquema(bearer: str, tenant: str, tipo: str, lista):
    return c.put(f"/tipos-expediente/{tipo}/esquema", json={"tenant": tenant, "tiposDocumentales": lista}, headers=cab(bearer))


def exps_de_tipo(bearer: str, tenant: str, tipo: str) -> list[str]:
    return [e["id"] for e in c.get("/expedientes/", params={"tenant": tenant, "tipo": tipo}, headers=cab(bearer)).json()["expedientes"]]


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
    ruta = RAIZ / ".registro" / "auditoria.jsonl"
    if not ruta.exists():
        return []
    return [e for e in (json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip()) if e.get("accion") == accion]


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

    titulo("0b · Tipos de expediente: la clase de la que salen los expedientes")
    r = crear_tipo(a_admin, org_a, "  Expediente   médico ")
    rev("crear un tipo: 201", r.status_code == 201, r.text[:200])
    t = r.json()
    rev("el nombre sale limpio y lo demás lo pone el servidor", t["nombre"] == "Expediente médico" and t["id"].startswith("tipo-expediente-") and len(t["id"]) == 16 + 32 and t["creadoPor"] == guid_rita and t["creadoEn"] and t["totalExpedientes"] == 0, str(t))
    tipo_a = t["id"]
    tipo_a2 = crear_tipo(a_admin, org_a, "Expediente de un choque").json()["id"]
    r = crear_tipo(b_admin, org_b, "Expediente médico")
    rev("el MISMO nombre en OTRA organización sí se puede (el nombre es único por organización)", r.status_code == 201, r.text[:200])
    tipo_b = r.json()["id"]
    TIPOS.update({org_a: tipo_a, org_b: tipo_b})
    for repetido in ("Expediente médico", "expediente MÉDICO", "  Expediente    médico  "):
        r = crear_tipo(a_admin, org_a, repetido)
        rev(f"repetir «{repetido.strip()}» (sin distinguir mayúsculas ni espacios): 409 ya_existe", r.status_code == 409 and r.json()["detail"]["codigo"] == "ya_existe", r.text[:150])
    nombres = [x["nombre"] for x in listar_tipos(a_admin, org_a).json()["tipos"]]
    rev("los repetidos no crearon nada, y la lista va en el orden de creación", nombres == ["Expediente médico", "Expediente de un choque"], str(nombres))
    for malo, que in (("", "vacío"), ("   ", "solo espacios"), ("x" * 201, "de 201 caracteres"), ("a\u0000b", "con un carácter de control")):
        r = crear_tipo(a_admin, org_a, malo)
        rev(f"un nombre {que} se rechaza (400)", r.status_code == 400, f"{r.status_code} {r.text[:100]}")
    rev("un nombre que ni es texto se rechaza", crear_tipo(a_admin, org_a, 123).status_code in (400, 422))
    rev("uno de exactamente 200 caracteres sí cabe", crear_tipo(a_admin, org_a, "x" * 200).status_code == 201)
    rev("lo rechazado no creó nada: solo los tres", len(listar_tipos(a_admin, org_a).json()["tipos"]) == 3)
    rev("el Supervisor (que administra) también crea un tipo", crear_tipo(P["SUPERVISOR"], org_a, "Creado por el supervisor").status_code == 201)
    for rol in ["ANALISTA", "OPERADOR"] + SOLO_LEEN:
        r = crear_tipo(P[rol], org_a, f"De {rol}")
        rev(f"{rol}: crear un tipo da 403 sin_permiso", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso", r.text[:150])
        leen = [listar_tipos(P[rol], org_a).status_code, ver_tipo(P[rol], org_a, tipo_a).status_code]
        rev(f"{rol}: pero lee los tipos (lista y ver uno)", leen == [200, 200], str(leen))
    rev("lo denegado no creó nada: cuatro en total", len(listar_tipos(a_admin, org_a).json()["tipos"]) == 4)
    puestos = [x for x in auditoria("CASE_TYPE_CREATED") if x.get("tipoId") == tipo_a]
    rev("`CASE_TYPE_CREATED` dice quién, dónde y cuál", len(puestos) == 1 and puestos[0].get("usuarioGuid") == guid_rita and puestos[0].get("tenant") == org_a and puestos[0].get("nombre") == "Expediente médico", str(puestos))
    rev("y no se anotó nada por los rechazados", len(auditoria("CASE_TYPE_CREATED")) == 5)

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
    rev("el expediente trae su tipo (id y nombre)", e1["tipo"] == {"id": tipo_a, "nombre": "Expediente médico"}, str(e1))
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
    rev("y cada expediente de la lista trae su tipo", all(e["tipo"] == {"id": tipo_a, "nombre": "Expediente médico"} for e in lista), str(lista[:1]))
    r = crear(a_admin, org_a, "Sin tipo", tipo=None)
    rev("un expediente SIN tipo se rechaza: todo expediente nace de uno (422)", r.status_code == 422, f"{r.status_code} {r.text[:120]}")
    inexistente = crear(a_admin, org_a, "Tipo fantasma", tipo="tipo-expediente-" + "0" * 32)
    rev("de un tipo que no existe: 404", inexistente.status_code == 404 and "tipo de expediente" in inexistente.text, inexistente.text[:150])
    rev("de un tipo que ni tiene forma de id: 404", crear(a_admin, org_a, "Tipo raro", tipo="cualquier-cosa").status_code == 404)
    rev("de un tipo DOCUMENTAL (otra cosa con nombre parecido): 404", crear(a_admin, org_a, "Tipo documental", tipo="tipo-" + "a" * 32).status_code == 404)
    ajeno = crear(a_admin, org_a, "Tipo de otra organización", tipo=tipo_b)
    rev("de un tipo de OTRA organización: 404, igual que uno que no existe", ajeno.status_code == 404 and ajeno.json() == inexistente.json(), f"{ajeno.text[:120]} | {inexistente.text[:120]}")
    rev("uno de exactamente 200 caracteres sí cabe", crear(a_admin, org_a, "x" * 200).status_code == 201)
    rev("lo rechazado no creó nada (los tres de antes, y el de 200)", len(listar(a_admin, org_a).json()["expedientes"]) == 4)
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
    rev("nada de lo anterior tocó la primera: expedientes y documentos igual", ids_docs_de(a_admin, org_a, e1) == [d2, d1] and ids_docs_de(a_admin, org_a, e2) == [d1] and len(listar(a_admin, org_a).json()["expedientes"]) == 4)
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

    titulo("12 · La forma de lo guardado (la de una tabla)")
    eventos_doc = [json.loads(x) for x in (RAIZ / ".registro" / f"documentos-{org_a}.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    campos = {"op", "id", "tenant", "sha256", "rutaRelativa", "tamanoBytes", "mime", "nombreOriginal", "canal", "llaveId", "recibidoEn"}
    rev("cada documento guardado tiene exactamente los campos de la entidad", all(set(e) == campos and e["op"] == "nacer" for e in eventos_doc) and len(eventos_doc) == 5, str([sorted(set(e) ^ campos) for e in eventos_doc][:2]))
    eventos_exp = [json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip().endswith("}")]
    rev("cada expediente y cada pertenencia, también, y de solo agregar", {e["op"] for e in eventos_exp} == {"crear", "agregar", "quitar"} and all(e["tenant"] == org_a for e in eventos_exp))
    rev("el registro de una organización no lleva nada de otra", not (RAIZ / ".registro" / f"documentos-{org_b}.jsonl").exists() and all(b_exp not in json.dumps(e) for e in eventos_exp))

    titulo("13 · Tipos de expediente: sus instancias, el filtro, el conteo y el aislamiento")
    ec1 = crear(a_admin, org_a, "Choque del 12 de marzo", tipo=tipo_a2).json()["id"]
    ec2 = crear(a_admin, org_a, "Choque del 3 de mayo", tipo=tipo_a2).json()["id"]
    rev("el expediente de un choque trae su tipo al verlo entero", ver(a_admin, org_a, ec1).json()["tipo"] == {"id": tipo_a2, "nombre": "Expediente de un choque"}, str(ver(a_admin, org_a, ec1).json()))
    rev("filtrar por tipo trae solo las instancias de ese tipo", exps_de_tipo(a_admin, org_a, tipo_a2) == [ec1, ec2], str(exps_de_tipo(a_admin, org_a, tipo_a2)))
    de_a = exps_de_tipo(a_admin, org_a, tipo_a)
    rev("y las del otro tipo no incluyen las de choque", {e1, e2, e3} <= set(de_a) and not ({ec1, ec2} & set(de_a)))
    rev("sin filtro vienen todas, de los dos tipos", {e1, ec1, ec2} <= {e["id"] for e in listar(a_admin, org_a).json()["expedientes"]})
    rev("un tipo que no existe no tiene expedientes (ni da error)", exps_de_tipo(a_admin, org_a, "tipo-expediente-" + "0" * 32) == [] and exps_de_tipo(a_admin, org_a, "cualquier-cosa") == [])
    rev("el tipo de OTRA organización como filtro no trae nada", exps_de_tipo(a_admin, org_a, tipo_b) == [])
    tipos = {x["id"]: x for x in listar_tipos(a_admin, org_a).json()["tipos"]}
    cuentas = {k: v["totalExpedientes"] for k, v in tipos.items()}
    rev("la lista de tipos cuenta sus expedientes", cuentas[tipo_a2] == 2 and cuentas[tipo_a] == len(de_a) and len(de_a) >= 3, str(cuentas))
    rev("y los tipos sin expedientes cuentan cero", sorted(v for k, v in cuentas.items() if k not in (tipo_a, tipo_a2)) == [0, 0], str(cuentas))
    uno = ver_tipo(a_admin, org_a, tipo_a2).json()
    rev("ver un tipo trae su nombre y su conteo", uno["nombre"] == "Expediente de un choque" and uno["totalExpedientes"] == 2, str(uno))
    creado_choque = [x for x in auditoria("CASE_CREATED") if x.get("expedienteId") == ec1]
    rev("`CASE_CREATED` anota de qué tipo es el expediente", len(creado_choque) == 1 and creado_choque[0].get("tipoId") == tipo_a2 and creado_choque[0].get("tipoNombre") == "Expediente de un choque", str(creado_choque))

    r = listar_tipos(b_admin, org_a)
    rev("pedir los tipos de OTRA organización: 403 sin_acceso, sin filtrar nada", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso" and "choque" not in r.text and tipo_a not in r.text, r.text[:200])
    ajeno = ver_tipo(b_admin, org_b, tipo_a)
    rev("ver un tipo AJENO por su id: 404, igual que uno que no existe", ajeno.status_code == 404 and ajeno.json() == ver_tipo(b_admin, org_b, "tipo-expediente-" + "0" * 32).json(), ajeno.text[:150])
    rev("ver un tipo de la primera poniendo SU tenant: 403", ver_tipo(b_admin, org_a, tipo_a).status_code == 403)
    rev("crear un tipo poniendo el tenant de OTRA: 403", crear_tipo(b_admin, org_a, "Colado").status_code == 403)
    tipos_b = listar_tipos(b_admin, org_b).json()["tipos"]
    rev("la otra organización sigue teniendo solo lo suyo", [(x["id"], x["nombre"]) for x in tipos_b] == [(tipo_b, "Expediente médico")], str(tipos_b))
    rev("y la primera no ganó ninguno", len(listar_tipos(a_admin, org_a).json()["tipos"]) == 4)
    intentos_tipos = [i for i in auditoria("CROSS_TENANT_ACCESS") if i.get("recurso") == "tipo de expediente"]
    rev("los intentos con el tenant ajeno quedan en la bitácora", len(intentos_tipos) >= 3, str(intentos_tipos[-1:]))

    with ThreadPoolExecutor(max_workers=8) as pool:
        codigos = list(pool.map(lambda _: crear_tipo(a_admin, org_a, "Carrera de nombres").status_code, range(8)))
    rev("ocho peticiones a la vez para crear el MISMO tipo: solo una lo crea (el nombre es único)", codigos.count(201) == 1 and codigos.count(409) == 7, str(codigos))

    eventos_tipos = [json.loads(x) for x in (RAIZ / ".registro" / f"tipos-expediente-{org_a}.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    rev("cada tipo guardado tiene exactamente los campos de la entidad", eventos_tipos and all(set(e) == {"op", "id", "tenant", "nombre", "por", "en"} and e["op"] == "crear" and e["tenant"] == org_a for e in eventos_tipos), str(eventos_tipos[:1]))
    crear_exp = [e for e in (json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip().endswith("}")) if e["op"] == "crear"]
    rev("y cada expediente guardado lleva el id de su tipo", crear_exp and all(set(e) == {"op", "id", "tenant", "nombre", "tipo", "por", "en"} and e["tipo"].startswith("tipo-expediente-") for e in crear_exp), str(crear_exp[:1]))
    tipos_de_b = (RAIZ / ".registro" / f"tipos-expediente-{org_b}.jsonl").read_text(encoding="utf-8")
    rev("el registro de tipos de una organización no lleva nada de otra", tipo_a not in tipos_de_b and tipo_b in tipos_de_b and tipo_b not in "".join(json.dumps(e) for e in eventos_tipos))
    rev("`tipos_expediente` guarda por organización: el de B no existe en A", tipos_expediente.obtener(org_a, tipo_b) is None and tipos_expediente.obtener(org_b, tipo_a) is None)

    titulo("14 · El esquema de un tipo de expediente: describe, no restringe")
    td_ine = crear_tipo_documental(a_admin, org_a, "INE")
    td_acta = crear_tipo_documental(a_admin, org_a, "Acta constitutiva")
    td_rec = crear_tipo_documental(a_admin, org_a, "Receta")
    td_b = crear_tipo_documental(b_admin, org_b, "Tipo de Beta")
    esperados: list[tuple[list, list]] = []  # los cambios REALES de tipo_a: (antes, despues)

    def definir(bearer: str, lista: list[str]):
        """Define el esquema de `tipo_a` y anota (antes, después) si de verdad cambió."""
        previo = [x["id"] for x in ver_tipo(a_admin, org_a, tipo_a).json()["tiposDocumentales"]]
        r = esquema(bearer, org_a, tipo_a, lista)
        if r.status_code == 200 and r.json()["cambiado"]:
            esperados.append((previo, list(lista)))
        return r

    ver_a = lambda: [(x["id"], x["nombre"]) for x in ver_tipo(a_admin, org_a, tipo_a).json()["tiposDocumentales"]]  # noqa: E731
    rev("un tipo de expediente nuevo nace sin esquema", ver_a() == [] and all(t["tiposDocumentales"] == [] for t in listar_tipos(a_admin, org_a).json()["tipos"]))
    r = definir(a_admin, [td_ine, td_acta])
    rev("definir el esquema: 200 y `cambiado: true`", r.status_code == 200 and r.json()["cambiado"] is True, r.text[:200])
    rev("el tipo trae su esquema con el id y el nombre de cada tipo documental, en ese orden", r.json()["tipo"]["tiposDocumentales"] == [{"id": td_ine, "nombre": "INE"}, {"id": td_acta, "nombre": "Acta constitutiva"}], str(r.json()))
    en_lista = next(t for t in listar_tipos(a_admin, org_a).json()["tipos"] if t["id"] == tipo_a)
    rev("y lo mismo al verlo y al listar", ver_a() == [(td_ine, "INE"), (td_acta, "Acta constitutiva")] and [x["id"] for x in en_lista["tiposDocumentales"]] == [td_ine, td_acta], str(en_lista))
    r = definir(a_admin, [td_ine, td_acta])
    rev("definir el MISMO esquema: 200 y `cambiado: false`", r.status_code == 200 and r.json()["cambiado"] is False, r.text[:150])
    r = definir(a_admin, [td_acta, td_ine])
    rev("cambiar solo el ORDEN cuenta como cambio y se conserva", r.json()["cambiado"] is True and [i for i, _ in ver_a()] == [td_acta, td_ine], r.text[:200])
    definir(a_admin, [td_acta, td_ine, td_rec])
    rev("agregar uno más: quedan tres, en el orden dado", [i for i, _ in ver_a()] == [td_acta, td_ine, td_rec])
    definir(a_admin, [td_ine])
    rev("quitar: queda solo el que se dejó", ver_a() == [(td_ine, "INE")])

    estado_antes = ver_tipo(a_admin, org_a, tipo_a).json()
    sin_existir = esquema(a_admin, org_a, tipo_a, ["tipo-" + "0" * 32])
    for lista, que in (([td_ine, td_ine], "con un tipo documental repetido"), (["tipo-" + "0" * 32], "con un tipo documental que no existe"), ([td_b], "con un tipo documental de OTRA organización")):
        r = esquema(a_admin, org_a, tipo_a, lista)
        rev(f"un esquema {que}: 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    ajeno = esquema(a_admin, org_a, tipo_a, [td_b])
    rev("uno de OTRA organización se contesta igual que uno que no existe", ajeno.status_code == 400 and ajeno.json() == sin_existir.json(), f"{ajeno.text} | {sin_existir.text}")
    rev("un esquema que no es una lista: 422", c.put(f"/tipos-expediente/{tipo_a}/esquema", json={"tenant": org_a, "tiposDocumentales": "uno"}, headers=cab(a_admin)).status_code == 422)
    rev("sin `tiposDocumentales`: 422", c.put(f"/tipos-expediente/{tipo_a}/esquema", json={"tenant": org_a}, headers=cab(a_admin)).status_code == 422)
    rev("lo rechazado no cambió nada", ver_tipo(a_admin, org_a, tipo_a).json() == estado_antes)
    tope_esquema = tipos_expediente.MAX_ESQUEMA
    try:
        tipos_expediente.MAX_ESQUEMA = 2
        r = esquema(a_admin, org_a, tipo_a, [td_ine, td_acta, td_rec])
        rev("pasado el tope de tipos documentales (con tipos reales): 400 que dice cuál es", r.status_code == 400 and "hasta 2" in r.text, r.text[:150])
        r = definir(a_admin, [td_ine, td_acta])
        rev("justo en el tope sí cabe", r.status_code == 200 and r.json()["cambiado"] is True, r.text[:150])
    finally:
        tipos_expediente.MAX_ESQUEMA = tope_esquema
    definir(a_admin, [td_ine])
    inexistente = esquema(a_admin, org_a, "tipo-expediente-" + "0" * 32, [td_ine])
    rev("definir el esquema de un tipo que no existe: 404", inexistente.status_code == 404 and "tipo de expediente" in inexistente.text, inexistente.text[:150])
    rev("y de uno que ni tiene forma de id: 404", esquema(a_admin, org_a, "xyz", [td_ine]).status_code == 404)
    r = esquema(a_admin, org_a, tipo_b, [td_ine])
    rev("el tipo de OTRA organización: 404, igual que uno que no existe", r.status_code == 404 and r.json() == inexistente.json(), r.text[:150])

    for rol in ["ANALISTA", "OPERADOR"] + SOLO_LEEN:
        r = esquema(P[rol], org_a, tipo_a, [td_rec])
        rev(f"{rol}: definir el esquema da 403 sin_permiso", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso", r.text[:150])
        rev(f"{rol}: pero ve el esquema", [x["id"] for x in ver_tipo(P[rol], org_a, tipo_a).json()["tiposDocumentales"]] == [td_ine])
    rev("lo denegado no cambió el esquema", ver_a() == [(td_ine, "INE")])
    r = definir(P["SUPERVISOR"], [td_ine, td_rec])
    rev("el Supervisor (que administra) sí lo define", r.status_code == 200 and r.json()["cambiado"] is True and [i for i, _ in ver_a()] == [td_ine, td_rec], r.text[:150])

    rev("la otra organización no puede definir el de la primera poniendo SU tenant: 403", esquema(b_admin, org_a, tipo_a, [td_b]).status_code == 403)
    r = esquema(b_admin, org_b, tipo_a, [td_b])
    rev("ni con el suyo y el id ajeno: 404, igual que uno que no existe", r.status_code == 404 and r.json() == esquema(b_admin, org_b, "tipo-expediente-" + "0" * 32, [td_b]).json(), r.text[:150])
    rev("pero sí define el suyo, con SU tipo documental", esquema(b_admin, org_b, tipo_b, [td_b]).status_code == 200 and [x["nombre"] for x in ver_tipo(b_admin, org_b, tipo_b).json()["tiposDocumentales"]] == ["Tipo de Beta"])
    rev("y el esquema de la primera sigue como estaba", [i for i, _ in ver_a()] == [td_ine, td_rec])

    docs_de_e1 = ids_docs_de(a_admin, org_a, e1)
    suelto = subir(a_admin, org_a, "foto-suelta.pdf", pdf(77)).json()["documentoId"]
    r = meter(a_admin, org_a, e1, suelto)
    rev("a un expediente cuyo tipo TIENE esquema le entra cualquier documento: el esquema no restringe", r.status_code == 200 and r.json()["agregado"] is True and suelto in ids_docs_de(a_admin, org_a, e1), r.text[:200])
    rev("y a uno cuyo tipo NO tiene esquema también", meter(a_admin, org_a, ec1, suelto).json()["agregado"] is True)
    definir(a_admin, [])
    rev("quitarle el esquema al tipo NO toca los documentos de sus expedientes", ver_a() == [] and ids_docs_de(a_admin, org_a, e1) == docs_de_e1 + [suelto])
    definir(a_admin, [td_ine, td_rec])

    r = c.delete(f"/tipos-documentales/{td_rec}", params={"tenant": org_a}, headers=cab(a_admin))
    rev("borrar de la biblioteca un tipo documental que está en un esquema: 200", r.status_code == 200, r.text[:150])
    rev("queda en el esquema como un id SIN nombre, a la vista", ver_a() == [(td_ine, "INE"), (td_rec, None)], str(ver_a()))
    definir(a_admin, [td_ine])
    rev("y se puede quitar del esquema", ver_a() == [(td_ine, "INE")])

    cambios = [x for x in auditoria("CASE_TYPE_SCHEMA_CHANGED") if x.get("tipoId") == tipo_a]
    rev("`CASE_TYPE_SCHEMA_CHANGED` dice quién, dónde y cuál tipo", cambios and cambios[0].get("usuarioGuid") == guid_rita and cambios[0].get("tenant") == org_a and cambios[0].get("nombre") == "Expediente médico", str(cambios[:1]))
    rev("lleva el esquema ANTES y DESPUÉS, uno por cada cambio real (ni por repetidos ni por rechazados)", [(x.get("antes"), x.get("despues")) for x in cambios] == esperados, f"{[(x.get('antes'), x.get('despues')) for x in cambios]} != {esperados}")
    eventos_tipos = [json.loads(x) for x in (RAIZ / ".registro" / f"tipos-expediente-{org_a}.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    de_esquema = [e for e in eventos_tipos if e["op"] == "esquema"]
    rev("cada cambio de esquema guardado tiene exactamente sus campos", bool(de_esquema) and all(set(e) == {"op", "id", "tenant", "tiposDocumentales", "por", "en"} and e["tenant"] == org_a for e in de_esquema), str(de_esquema[:1]))
    rev("y se guarda un evento por cada cambio real, ni uno más", len(de_esquema) == len(esperados), f"{len(de_esquema)} eventos, {len(esperados)} cambios")
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
