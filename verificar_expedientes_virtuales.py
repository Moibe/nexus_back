"""¿Los EXPEDIENTES VIRTUALES funcionan de punta a punta? (migración `0004`, `servicios/expedientes_virtuales.py`)

    .venv/Scripts/python verificar_expedientes_virtuales.py
    .venv/Scripts/python verificar_expedientes_virtuales.py --dejar     (deja vivo el contenedor para investigar)

Los expedientes virtuales SOLO viven en SQL (decisión del 2026-10-28: «todo lo nuevo directo a SQL»), así que esta prueba corre siempre contra
el SQL Server desechable de `pruebas_sql.py`; sin Docker se salta y lo dice. El almacén (bandeja, usuarios, auditoría) es una carpeta temporal.

Lo que se prueba (decisión de producto de Moibe, 2026-10-29: el expediente es un receptáculo; los virtuales son grupitos de sus documentos):

- un expediente virtual nace dentro de UN expediente, de un tipo de expediente virtual de la organización, con los documentos elegidos (o
  ninguno); varios en el mismo expediente, del mismo tipo o de distintos; el expediente los enseña al verlo;
- solo puede tener documentos DE SU expediente: uno de otro expediente, uno suelto o uno ajeno se rechaza (404) y, al crear, no deja nada;
- meter es idempotente; sacar del virtual deja el documento en el expediente; sacar el documento DEL EXPEDIENTE lo saca de sus virtuales;
- borrar el virtual no toca documentos; un tipo con virtuales no se borra (409, con cuántos), y sin ellos sí;
- el tipo cuenta sus virtuales (`totalExpedientesVirtuales`), un renombre del tipo se ve en ellos y se pueden listar
  (`GET /tipos-expediente/{id}/virtuales`: lo que enseña el mosaico de Configuración al picar la cuenta);
- lo de otra organización contesta 404 (o 403 con su tenant); los roles que solo leen ven pero no tocan; cada cosa deja su rastro;
- borrar un tipo mientras se crean virtuales de él: nunca queda uno huérfano;
- el CLASIFICADOR mete SOLO a los virtuales (Moibe, 2026-11-02: «los dos clasifican» y, sin confianza mínima, «simplemente por ser de ese tipo»): a todos
  los de su expediente cuyo tipo espera el tipo documental con que se clasificó (también a los dos del mismo tipo), con cualquier confianza, mirando la ÚLTIMA clasificación,
  sin tocar documentos sueltos ni de otra organización, sin repetir, marcando lo que metió, y sin volver a meter lo que alguien sacó a mano;
  y por la API, en sus cuatro disparadores: subir un archivo a un expediente (con Document AI falseado: sin clasificador, falla, lento y rechazos que
  no gastan), meter a un expediente un documento ya clasificado, clasificar uno que ya está en un expediente y crear un virtual nuevo.
"""

import json
import shutil
import sys
import tempfile
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pruebas_sql  # noqa: E402

BASE = pruebas_sql.preparar("nexus-virtuales-prueba", 14335, dejar="--dejar" in sys.argv)

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-virtuales-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import auth, usuarios  # noqa: E402

fallos = 0
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + extra if extra and not ok else ''}")
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


def cab(bearer: str | None = None) -> dict:
    return {"x-api-key": SERVICIO, **({"Authorization": f"Bearer {bearer}"} if bearer else {})}


c = TestClient(app)


def entrar(correo: str, contrasena: str) -> str:
    r = c.post("/auth/login", json={"email": correo, "password": contrasena, "ip": "127.0.0.1", "userAgent": "verificar"}, headers=cab())
    assert r.status_code == 200, r.text
    return r.json()["accessToken"]


def con_primer_acceso(correo: str, temporal: str, definitiva: str) -> str:
    r = c.post("/auth/contrasena", json={"nueva": definitiva, "confirmacion": definitiva}, headers=cab(entrar(correo, temporal)))
    assert r.status_code == 200, r.text
    return entrar(correo, definitiva)


def organizacion(bearer_super: str, nombre: str, correo: str) -> tuple[str, str]:
    r = c.post("/organizaciones/", json={"nombre": nombre, "adminNombre": "Admin", "adminApellidoPaterno": nombre, "adminEmail": correo}, headers=cab(bearer_super))
    assert r.status_code == 201, r.text
    return r.json()["organizacion"]["guid"], con_primer_acceso(correo, r.json()["contrasenaTemporal"], "Rumbo-Sereno-Azul-47!")


def pdf(n: int) -> bytes:
    return PDF + f"%{n}\n".encode()


def subir(bearer: str, tenant: str, nombre: str, n: int) -> str:
    r = c.post("/bandeja/", files={"archivo": (nombre, pdf(n), "application/pdf")}, data={"tenant": tenant}, headers=cab(bearer))
    assert r.status_code == 201, r.text
    return r.json()["documentoId"]


def tipo_documental(bearer: str, tenant: str, nombre: str) -> str:
    import uuid

    tid = "tipo-" + uuid.uuid4().hex
    r = c.put(f"/tipos-documentales/{tid}", json={"tenant": tenant, "revision": 0, "datos": {"nombre": nombre, "descripcion": "", "estado": "activo", "procesadorId": ""}}, headers=cab(bearer))
    assert r.status_code == 200, r.text
    return tid


def tipo(bearer: str, tenant: str, nombre: str, esquema: list[str]):
    return c.post("/tipos-expediente/", json={"tenant": tenant, "nombre": nombre, "tiposDocumentales": esquema}, headers=cab(bearer))


def ver_tipo(bearer: str, tenant: str, t: str):
    return c.get(f"/tipos-expediente/{t}", params={"tenant": tenant}, headers=cab(bearer))


#: Un grupo «General» por organización, creado la primera vez que se necesita: desde la migración 0010 un expediente nuevo nace en un grupo.
_GRUPOS: dict[str, str] = {}


def grupo_de(bearer: str, tenant: str) -> str:
    if tenant not in _GRUPOS:
        r = c.post("/grupos/", json={"tenant": tenant, "nombre": "General"}, headers=cab(bearer))
        assert r.status_code == 201, r.text
        _GRUPOS[tenant] = r.json()["id"]
    return _GRUPOS[tenant]


def expediente(bearer: str, tenant: str, nombre: str) -> str:
    r = c.post("/expedientes/", json={"tenant": tenant, "nombre": nombre, "grupoId": grupo_de(bearer, tenant)}, headers=cab(bearer))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def meter(bearer: str, tenant: str, exp: str, doc: str):
    return c.put(f"/expedientes/{exp}/documentos/{doc}", params={"tenant": tenant}, headers=cab(bearer))


def sacar(bearer: str, tenant: str, exp: str, doc: str):
    return c.delete(f"/expedientes/{exp}/documentos/{doc}", params={"tenant": tenant}, headers=cab(bearer))


def ver(bearer: str, tenant: str, exp: str):
    return c.get(f"/expedientes/{exp}", params={"tenant": tenant}, headers=cab(bearer))


def crear_v(bearer: str, tenant: str, exp: str, t: str, nombre="Solicitud de crédito", documentos=None):
    return c.post(f"/expedientes/{exp}/virtuales", json={"tenant": tenant, "tipoId": t, "nombre": nombre, "documentos": documentos or []}, headers=cab(bearer))


def ver_v(bearer: str, tenant: str, exp: str, v: str):
    return c.get(f"/expedientes/{exp}/virtuales/{v}", params={"tenant": tenant}, headers=cab(bearer))


def meter_v(bearer: str, tenant: str, exp: str, v: str, doc: str):
    return c.put(f"/expedientes/{exp}/virtuales/{v}/documentos/{doc}", params={"tenant": tenant}, headers=cab(bearer))


def sacar_v(bearer: str, tenant: str, exp: str, v: str, doc: str):
    return c.delete(f"/expedientes/{exp}/virtuales/{v}/documentos/{doc}", params={"tenant": tenant}, headers=cab(bearer))


def borrar_v(bearer: str, tenant: str, exp: str, v: str):
    return c.delete(f"/expedientes/{exp}/virtuales/{v}", params={"tenant": tenant}, headers=cab(bearer))


def docs_v(bearer: str, tenant: str, exp: str, v: str) -> list[str]:
    return [d["id"] for d in ver_v(bearer, tenant, exp, v).json()["documentos"]]


def auditoria(accion: str) -> list[dict]:
    # Por el servicio y no leyendo el JSONL: con --sql la bitácora vive en nexus.auditEvents (migración 0006).
    from servicios import auditoria as bitacora

    return bitacora.eventos({accion})


try:
    titulo("0 · Montaje: dos organizaciones, una persona de cada rol, tipos documentales, un tipo de expediente virtual y un expediente con documentos")
    temporal = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal, "Cobre-Lento-Azul-47!")
    org, adm = organizacion(bearer_super, "Alfa", "rita@ejemplo.com")
    otra, adm_b = organizacion(bearer_super, "Beta", "beto@ejemplo.com")
    guid_rita = usuarios.para_login("rita@ejemplo.com")["guid"]
    P = {}
    for rol in ("SUPERVISOR", "OPERADOR", "VIEWER"):
        correo = f"{rol.lower()}@ejemplo.com"
        r = c.post("/usuarios/", json={"nombre": rol.title(), "apellidoPaterno": "Prueba", "email": correo, "rol": rol}, headers=cab(adm))
        assert r.status_code == 201, r.text
        P[rol] = con_primer_acceso(correo, r.json()["contrasenaTemporal"], "Cielo-Abierto-Verde-52!")
    td_ine, td_dom, td_cta = (tipo_documental(adm, org, n) for n in ("INE", "Comprobante de domicilio", "Estado de cuenta"))
    td_b = tipo_documental(adm_b, otra, "INE de Beta")
    credito = tipo(adm, org, "Solicitud de crédito", [td_ine, td_dom, td_cta]).json()["id"]
    seguro = tipo(adm, org, "Póliza de seguro", [td_ine]).json()["id"]
    tipo_b = tipo(adm_b, otra, "Solicitud de crédito", [td_b]).json()["id"]
    juan = expediente(adm, org, "Juan Pérez")
    d_ine, d_dom, d_cta, d_suelto = (subir(adm, org, f"{n}.pdf", i) for i, n in enumerate(("ine", "domicilio", "cuenta", "suelto")))
    for d in (d_ine, d_dom, d_cta):
        meter(adm, org, juan, d)
    maria = expediente(adm, org, "María López")
    d_maria = subir(adm, org, "maria.pdf", 10)
    meter(adm, org, maria, d_maria)
    exp_b = expediente(adm_b, otra, "De Beta")
    d_b = subir(adm_b, otra, "beta.pdf", 20)
    meter(adm_b, otra, exp_b, d_b)
    rev("hay dos tipos de expediente virtual, el expediente de Juan con 3 documentos, el de María con 1 y uno suelto", len(ver(adm, org, juan).json()["documentos"]) == 3)
    rev("un expediente recién creado no tiene expedientes virtuales", ver(adm, org, juan).json()["expedientesVirtuales"] == [])

    titulo("1 · Crear un expediente virtual dentro de un expediente")
    r = crear_v(adm, org, juan, credito, "  Crédito   automotriz ", [d_ine, d_cta])
    rev("crear: 201", r.status_code == 201, r.text[:250])
    v1 = r.json()
    rev("su id lo pone el servidor y el nombre sale limpio", v1["id"].startswith("expediente-virtual-") and len(v1["id"]) == 19 + 32 and v1["nombre"] == "Crédito automotriz", str(v1)[:200])
    rev("trae su tipo con el esquema del tipo (con nombres, en orden)", v1["tipo"]["id"] == credito and v1["tipo"]["nombre"] == "Solicitud de crédito" and [x["nombre"] for x in v1["tipo"]["tiposDocumentales"]] == ["INE", "Comprobante de domicilio", "Estado de cuenta"], str(v1["tipo"]))
    rev("trae su expediente y quién lo creó", v1["expediente"] == {"id": juan, "nombre": "Juan Pérez"} and v1["creadoPor"] == guid_rita and v1["creadoEn"])
    rev("nace con los documentos elegidos, en ese orden", [d["id"] for d in v1["documentos"]] == [d_ine, d_cta] and v1["totalDocumentos"] == 2, str([d["id"] for d in v1["documentos"]]))
    v1 = v1["id"]
    v2 = crear_v(adm, org, juan, credito, "Crédito automotriz").json()["id"]
    rev("otro del MISMO tipo y con el MISMO nombre en el mismo expediente se puede, y nace vacío", v2 != v1 and docs_v(adm, org, juan, v2) == [])
    v3 = crear_v(adm, org, juan, seguro, "Seguro de auto", [d_ine]).json()["id"]
    rev("uno de OTRO tipo en el mismo expediente, también (un documento puede estar en varios virtuales)", docs_v(adm, org, juan, v3) == [d_ine])
    lista = ver(adm, org, juan).json()["expedientesVirtuales"]
    rev("el expediente enseña sus tres virtuales, en el orden de creación, con su tipo y los ids de sus documentos",
        [(v["id"], v["tipo"]["id"], v["documentos"]) for v in lista] == [(v1, credito, [d_ine, d_cta]), (v2, credito, []), (v3, seguro, [d_ine])], json.dumps(lista)[:300])
    rev("los virtuales de un expediente no aparecen en otro", ver(adm, org, maria).json()["expedientesVirtuales"] == [])

    antes = len(ver(adm, org, juan).json()["expedientesVirtuales"])
    for doc, que in ((d_maria, "de OTRO expediente"), (d_suelto, "suelto (en ningún expediente)"), (d_b, "de OTRA organización"), ("documento-" + "0" * 32, "que no existe")):
        r = crear_v(adm, org, juan, credito, "Con uno que no es del expediente", [d_ine, doc])
        rev(f"con un documento {que}: 404 y NO se crea nada (ni con el bueno)", r.status_code == 404 and "no está en el expediente" in r.text, f"{r.status_code} {r.text[:120]}")
    rev("ninguno de los rechazados dejó un virtual a medias", len(ver(adm, org, juan).json()["expedientesVirtuales"]) == antes)
    for t, que in (("tipo-expediente-" + "0" * 32, "que no existe"), (tipo_b, "de OTRA organización"), (td_ine, "DOCUMENTAL (otra cosa)"), ("cualquier-cosa", "sin forma de id")):
        r = crear_v(adm, org, juan, t)
        rev(f"de un tipo {que}: 404", r.status_code == 404 and "tipo de expediente virtual" in r.text, f"{r.status_code} {r.text[:120]}")
    for malo, que in (("", "vacío"), ("   ", "solo espacios"), ("x" * 201, "de 201 caracteres"), ("a\u0000b", "con un carácter de control")):
        rev(f"un nombre {que}: 400", crear_v(adm, org, juan, credito, malo).status_code == 400)
    rev("en un expediente que no existe: 404", crear_v(adm, org, "expediente-" + "0" * 32, credito).status_code == 404)
    rev("en el expediente de OTRA organización, con su tenant: 404", crear_v(adm, org, exp_b, credito).status_code == 404)
    rev("poniendo el tenant de la otra: 403", crear_v(adm, otra, exp_b, tipo_b).status_code == 403)
    rev("los documentos repetidos en la lista cuentan una vez", docs_v(adm, org, juan, crear_v(adm, org, juan, seguro, "Repetidos", [d_dom, d_dom]).json()["id"]) == [d_dom])

    titulo("2 · Meter y sacar documentos de un virtual")
    r = meter_v(adm, org, juan, v2, d_dom)
    rev("meter un documento del expediente: 200 `agregado: true`, y la respuesta trae el virtual", r.status_code == 200 and r.json()["agregado"] is True and [d["id"] for d in r.json()["expedienteVirtual"]["documentos"]] == [d_dom], r.text[:200])
    rev("meterlo otra vez: `agregado: false` y no se repite", meter_v(adm, org, juan, v2, d_dom).json()["agregado"] is False and docs_v(adm, org, juan, v2) == [d_dom])
    for doc, que in ((d_maria, "de OTRO expediente"), (d_suelto, "suelto"), (d_b, "de OTRA organización")):
        r = meter_v(adm, org, juan, v2, doc)
        rev(f"meter uno {que}: 404 y no entra", r.status_code == 404 and docs_v(adm, org, juan, v2) == [d_dom], f"{r.status_code} {r.text[:100]}")
    rev("meter en un virtual que no existe, o uno de otro expediente: 404", meter_v(adm, org, juan, "expediente-virtual-" + "0" * 32, d_dom).status_code == 404 and meter_v(adm, org, maria, v2, d_maria).status_code == 404)
    r = sacar_v(adm, org, juan, v2, d_dom)
    rev("sacar del virtual: 200, y el documento SIGUE en el expediente", r.status_code == 200 and docs_v(adm, org, juan, v2) == [] and d_dom in [d["id"] for d in ver(adm, org, juan).json()["documentos"]])
    r = sacar_v(adm, org, juan, v2, d_dom)
    rev("sacar uno que no está en el virtual: 404 que lo dice", r.status_code == 404 and "expediente virtual" in r.text, r.text[:120])

    titulo("3 · El subconjunto: sacar un documento DEL EXPEDIENTE lo saca de sus virtuales")
    rev("antes: el INE está en dos virtuales de Juan", docs_v(adm, org, juan, v1) == [d_ine, d_cta] and docs_v(adm, org, juan, v3) == [d_ine])
    sacar(adm, org, juan, d_ine)
    rev("al sacarlo del expediente, sale de los dos virtuales (y lo demás se queda)", docs_v(adm, org, juan, v1) == [d_cta] and docs_v(adm, org, juan, v3) == [])
    rev("los virtuales siguen existiendo aunque se queden vacíos", ver_v(adm, org, juan, v3).status_code == 200)
    meter(adm, org, juan, d_ine)
    rev("volver a meterlo al expediente NO lo devuelve solo a los virtuales: eso se decide a mano", docs_v(adm, org, juan, v1) == [d_cta])
    meter_v(adm, org, juan, v1, d_ine)
    rev("y a mano sí vuelve, al final del orden", docs_v(adm, org, juan, v1) == [d_cta, d_ine])

    titulo("4 · El tipo: su conteo, su renombre y su borrado")
    tipos = {t["id"]: t for t in c.get("/tipos-expediente/", params={"tenant": org}, headers=cab(adm)).json()["tipos"]}
    rev("cada tipo cuenta sus expedientes virtuales", tipos[credito]["totalExpedientesVirtuales"] == 2 and tipos[seguro]["totalExpedientesVirtuales"] == 2, str({k: v["totalExpedientesVirtuales"] for k, v in tipos.items()}))
    c.patch(f"/tipos-expediente/{credito}", json={"tenant": org, "nombre": "Crédito"}, headers=cab(adm))
    rev("renombrar el tipo se ve en sus virtuales (abiertos y en el expediente)", ver_v(adm, org, juan, v1).json()["tipo"]["nombre"] == "Crédito" and ver(adm, org, juan).json()["expedientesVirtuales"][0]["tipo"]["nombre"] == "Crédito")
    r = c.delete(f"/tipos-expediente/{credito}", params={"tenant": org}, headers=cab(adm))
    det = r.json().get("detail", {}) if r.status_code == 409 else {}
    rev("borrar un tipo CON virtuales: 409 con_expedientes, con cuántos y diciendo que son virtuales", r.status_code == 409 and det.get("codigo") == "con_expedientes" and det.get("total") == 2 and "2 expedientes virtuales" in det.get("mensaje", ""), r.text[:200])
    c.delete(f"/tipos-documentales/{td_cta}", params={"tenant": org}, headers=cab(adm))
    rev("borrar un tipo documental lo quita del esquema que ven los virtuales", [x["id"] for x in ver_v(adm, org, juan, v1).json()["tipo"]["tiposDocumentales"]] == [td_ine, td_dom])

    titulo("5 · Borrar un expediente virtual")
    r = borrar_v(adm, org, juan, v2)
    rev("borrar: 200 `borrado: true`, y ya no existe", r.status_code == 200 and r.json() == {"borrado": True} and ver_v(adm, org, juan, v2).status_code == 404)
    rev("borrarlo otra vez: 404", borrar_v(adm, org, juan, v2).status_code == 404)
    borrar_v(adm, org, juan, v1)
    rev("borrar uno con documentos NO borra los documentos: siguen en el expediente", {d_ine, d_dom, d_cta} <= {d["id"] for d in ver(adm, org, juan).json()["documentos"]})
    rev("sin virtuales, el tipo ya se puede borrar", c.delete(f"/tipos-expediente/{credito}", params={"tenant": org}, headers=cab(adm)).status_code == 200)

    titulo("6 · Lo de otra organización y lo que cada rol puede")
    rev("ver el virtual con la sesión de OTRA organización y su tenant: 404", ver_v(adm_b, otra, juan, v3).status_code == 404)
    rev("…poniendo el tenant de la primera: 403", ver_v(adm_b, org, juan, v3).status_code == 403)
    rev("meter, sacar y borrar desde la otra organización: 404 (con su tenant)", meter_v(adm_b, otra, juan, v3, d_dom).status_code == 404 and sacar_v(adm_b, otra, juan, v3, d_dom).status_code == 404 and borrar_v(adm_b, otra, juan, v3).status_code == 404)
    rev("el VIEWER lo ve", ver_v(P["VIEWER"], org, juan, v3).status_code == 200)
    malas = [crear_v(P["VIEWER"], org, juan, seguro).status_code, meter_v(P["VIEWER"], org, juan, v3, d_dom).status_code, sacar_v(P["VIEWER"], org, juan, v3, d_ine).status_code, borrar_v(P["VIEWER"], org, juan, v3).status_code]
    rev("…pero no crea, mete, saca ni borra: 403", malas == [403] * 4, str(malas))
    r = crear_v(P["OPERADOR"], org, juan, seguro, "Del operador", [d_dom])
    rev("el OPERADOR (que organiza) sí crea uno", r.status_code == 201, r.text[:150])

    titulo("7 · El rastro")
    creados = [x for x in auditoria("VIRTUAL_CASE_CREATED") if x.get("expedienteVirtualId") == v3]
    rev("`VIRTUAL_CASE_CREATED` dice quién, dónde, de qué expediente, de qué tipo y con qué documentos", len(creados) == 1 and creados[0].get("usuarioGuid") == guid_rita and creados[0].get("tenant") == org and creados[0].get("expedienteId") == juan and creados[0].get("tipoId") == seguro and creados[0].get("documentos") == [d_ine], str(creados))
    rev("`VIRTUAL_CASE_DOCUMENT_ADDED`: uno por cada documento que de verdad entró (el repetido no cuenta)", len([x for x in auditoria("VIRTUAL_CASE_DOCUMENT_ADDED") if x.get("expedienteVirtualId") == v2]) == 1)
    rev("`VIRTUAL_CASE_DOCUMENT_REMOVED` dice qué documento", [x.get("nombreOriginal") for x in auditoria("VIRTUAL_CASE_DOCUMENT_REMOVED") if x.get("expedienteVirtualId") == v2] == ["domicilio.pdf"])
    rev("`VIRTUAL_CASE_DELETED` por cada borrado", {x.get("expedienteVirtualId") for x in auditoria("VIRTUAL_CASE_DELETED")} == {v1, v2})

    titulo("8 · Dos peticiones a la vez")
    with ThreadPoolExecutor(max_workers=8) as pool:
        res = list(pool.map(lambda _: meter_v(adm, org, juan, v3, d_cta).json()["agregado"], range(8)))
    rev("ocho peticiones a la vez para meter el MISMO documento: solo una lo agrega", res.count(True) == 1 and docs_v(adm, org, juan, v3).count(d_cta) == 1, str(res))
    rondas = []
    for ronda in range(5):
        carrera = tipo(adm, org, f"Carrera {ronda}", [td_ine]).json()["id"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            borrando = pool.submit(lambda: c.delete(f"/tipos-expediente/{carrera}", params={"tenant": org}, headers=cab(adm)).status_code)
            creando = [pool.submit(lambda i=i: crear_v(adm, org, maria, carrera, f"Carrera {ronda}-{i}").status_code) for i in range(6)]
            b, creados_n = borrando.result(), [f.result() for f in creando].count(201)
        quedan = [v for v in ver(adm, org, maria).json()["expedientesVirtuales"] if v["tipo"]["id"] == carrera]
        rondas.append((b, creados_n, len(quedan)))
    rev("borrar un tipo mientras se crean virtuales de él: o se borra y no se coló ninguno, o no se borra y están los que se crearon",
        all((b == 200 and n == 0 and q == 0) or (b == 409 and n >= 1 and q == n) for b, n, q in rondas), str(rondas))

    titulo("9 · Sin base, los virtuales lo dicen (no hay gemelo en JSON)")
    from servicios import expedientes_virtuales  # noqa: E402

    guardado = expedientes_virtuales.SQLSERVER_HOST
    try:
        expedientes_virtuales.SQLSERVER_HOST = ""
        r = crear_v(adm, org, juan, seguro)
        rev("crear sin base: 503 que dice que necesitan la base", r.status_code == 503 and "base de datos" in r.text, f"{r.status_code} {r.text[:120]}")
        rev("y contar los virtuales de un tipo sin base no truena: da cero", expedientes_virtuales.contar_por_tipo(org) == {})
    finally:
        expedientes_virtuales.SQLSERVER_HOST = guardado

    # Al final a propósito: crea y borra virtuales, y las secciones de arriba cuentan los de cada tipo y los borrados de la bitácora.
    titulo("10 · Los expedientes virtuales de un tipo (lo que enseña el mosaico al picar la cuenta del tipo)")

    def de_tipo(bearer: str, tenant: str, t: str):
        return c.get(f"/tipos-expediente/{t}/virtuales", params={"tenant": tenant}, headers=cab(bearer))

    alta = tipo(adm, org, "Alta de empresa", [td_ine]).json()["id"]
    otro_tipo = tipo(adm, org, "Alta de sucursal", [td_ine]).json()["id"]
    sin_nada = tipo(adm, org, "Sin ninguno", [td_ine]).json()["id"]
    a1 = crear_v(adm, org, juan, alta, "Alta de Juan", [d_dom]).json()["id"]
    a2 = crear_v(adm, org, maria, alta, "Alta de María").json()["id"]
    crear_v(adm, org, juan, otro_tipo, "Sucursal de Juan")
    r = de_tipo(adm, org, alta)
    rev("200 y dice de qué tipo son", r.status_code == 200 and r.json()["tipo"] == {"id": alta, "nombre": "Alta de empresa"}, r.text[:200])
    lista = r.json()["expedientesVirtuales"]
    rev("trae los virtuales de ESE tipo, de todos los expedientes, los más recientes primero", [v["id"] for v in lista] == [a2, a1], json.dumps(lista)[:300])
    rev("y ninguno de otro tipo", all(v["nombre"] != "Sucursal de Juan" for v in lista))
    por_id = {v["id"]: v for v in lista}
    docs_juan = len(ver(adm, org, juan).json()["documentos"])
    docs_maria = len(ver(adm, org, maria).json()["documentos"])
    rev(
        "cada uno dice su expediente (nombre y cuántos documentos tiene) y cuántos de ellos lo forman",
        por_id[a1]["expediente"] == {"id": juan, "nombre": "Juan Pérez", "totalDocumentos": docs_juan}
        and por_id[a1]["totalDocumentos"] == 1
        and por_id[a2]["expediente"] == {"id": maria, "nombre": "María López", "totalDocumentos": docs_maria}
        and por_id[a2]["totalDocumentos"] == 0,
        json.dumps(lista)[:400],
    )
    rev("y quién y cuándo lo creó", por_id[a1]["creadoPor"] == guid_rita and bool(por_id[a1]["creadoEn"]))
    rev("son tantos como dice la cuenta del tipo", ver_tipo(adm, org, alta).json()["totalExpedientesVirtuales"] == len(lista) == 2)

    sacar(adm, org, juan, d_dom)
    ahora = {v["id"]: v for v in de_tipo(adm, org, alta).json()["expedientesVirtuales"]}
    rev("sacar un documento del expediente lo saca del virtual y baja los dos números", ahora[a1]["totalDocumentos"] == 0 and ahora[a1]["expediente"]["totalDocumentos"] == docs_juan - 1, json.dumps(ahora[a1])[:250])
    borrar_v(adm, org, maria, a2)
    rev("un virtual borrado ya no sale, y la cuenta del tipo baja con él", [v["id"] for v in de_tipo(adm, org, alta).json()["expedientesVirtuales"]] == [a1] and ver_tipo(adm, org, alta).json()["totalExpedientesVirtuales"] == 1)
    r = de_tipo(adm, org, sin_nada)
    rev("un tipo sin virtuales: 200 y la lista vacía", r.status_code == 200 and r.json()["expedientesVirtuales"] == [], r.text[:150])

    for t, que in (("tipo-expediente-" + "0" * 32, "que no existe"), ("cualquier-cosa", "sin forma de id"), (td_ine, "DOCUMENTAL (otra cosa)")):
        r = de_tipo(adm, org, t)
        rev(f"de un tipo {que}: 404", r.status_code == 404 and "tipo de expediente no existe" in r.text, f"{r.status_code} {r.text[:100]}")
    rev("de un tipo de OTRA organización, con la sesión y el tenant de ésta: 404", de_tipo(adm, org, tipo_b).status_code == 404)
    rev("…poniendo el tenant de la otra: 403", de_tipo(adm, otra, tipo_b).status_code == 403)
    rev("la otra organización no ve los virtuales de ésta aunque pida el mismo id de tipo: 404", de_tipo(adm_b, otra, alta).status_code == 404)
    rev("sin sesión: 401", c.get(f"/tipos-expediente/{alta}/virtuales", params={"tenant": org}, headers=cab()).status_code == 401)
    rev("el VIEWER, que solo lee, lo ve", de_tipo(P["VIEWER"], org, alta).status_code == 200)

    guardado = expedientes_virtuales.SQLSERVER_HOST
    try:
        expedientes_virtuales.SQLSERVER_HOST = ""
        rev("sin base, `listar_de_tipo` no truena: da una lista vacía", expedientes_virtuales.listar_de_tipo(org, alta) == [])
    finally:
        expedientes_virtuales.SQLSERVER_HOST = guardado

    titulo("11 · El clasificador mete SOLO a los expedientes virtuales (Moibe, 2026-11-02: sin confianza mínima, y a los dos si hay dos del mismo tipo)")
    import asyncio

    import routers.expedientes as router_expedientes
    from errores import SinClasificador
    from servicios import datos_extraidos, expedientes_virtuales as virtuales
    from servicios import ia as servicio_ia
    from servicios.esquema import normalizar_nombre

    def clasificar(doc: str, tipo_doc: str | None, confianza: float, tenant: str = org):
        """Anota una clasificación como la anota `/ia/clasificar`: la categoría es el id normalizado del tipo documental, o `otro`."""
        categoria = normalizar_nombre(tipo_doc) if tipo_doc else "otro"
        return datos_extraidos.registrar_clasificacion(tenant, doc, {"categoria": categoria, "confianza": confianza}, "clasificador-de-prueba", "usuario-de-prueba")

    def colocar(**filtros):
        return virtuales.colocar_clasificados(org, **filtros)

    def marcas(exp: str, v: str) -> dict[str, bool]:
        return {d["id"]: d["agregadoPorClasificador"] for d in ver_v(adm, org, exp, v).json()["documentos"]}

    def tipo_nuevo(nombre: str, esquema: list[str]) -> str:
        r = tipo(adm, org, nombre, esquema)
        assert r.status_code == 201, f"{nombre}: {r.status_code} {r.text[:200]}"
        return r.json()["id"]

    # `td_cta` se borró en la sección 4: este tipo documental es propio de esta sección.
    td_cuenta = tipo_documental(adm, org, "Estado de cuenta (clasificador)")
    td_extra = tipo_documental(adm, org, "Que ningún virtual espera")
    t_cred = tipo_nuevo("Crédito (clasificador)", [td_ine, td_dom, td_cuenta])
    t_seg = tipo_nuevo("Seguro (clasificador)", [td_ine])
    t_cta = tipo_nuevo("Cuentas (clasificador)", [td_cuenta])
    ana = expediente(adm, org, "Ana Torres")
    ana_ine, ana_dom, ana_cta, ana_otro = (subir(adm, org, f"ana-{n}.pdf", 100 + i) for i, n in enumerate(("ine", "dom", "cta", "otro")))
    for d in (ana_ine, ana_dom, ana_cta, ana_otro):
        meter(adm, org, ana, d)
    c1, c2 = (crear_v(adm, org, ana, t_cred, f"Crédito {i}").json()["id"] for i in (1, 2))
    s1 = crear_v(adm, org, ana, t_seg, "Seguro 1").json()["id"]
    x1 = crear_v(adm, org, ana, t_cta, "Solo cuentas").json()["id"]
    rev("sin clasificar nada, ninguno de los cuatro virtuales recibió nada solo", all(docs_v(adm, org, ana, v) == [] for v in (c1, c2, s1, x1)))
    rev("ya no hay confianza mínima: la constante no existe", not hasattr(virtuales, "CONFIANZA_MINIMA_AUTOMATICA"))

    anotado = clasificar(ana_ine, td_ine, 92)
    rev("anotar la clasificación devuelve lo que quedó: el tipo, su nombre y la confianza", anotado == {"tipoId": td_ine, "tipoNombre": "INE", "confianza": 92.0}, str(anotado))
    metidos = colocar(documento_id=ana_ine)
    rev("un INE reconocido con 92 % entra a los DOS virtuales de crédito (los dos clasifican) y al de seguro, que también lo espera; al de cuentas no",
        [m["expedienteVirtual"]["id"] for m in metidos] == [c1, c2, s1] and docs_v(adm, org, ana, x1) == [], str([m["expedienteVirtual"] for m in metidos]))
    rev("cada uno dice qué documento, de qué tipo, con cuánta confianza y en qué expediente",
        all(m["documentoId"] == ana_ine and m["nombreOriginal"] == "ana-ine.pdf" and m["tipoDocumental"] == {"id": td_ine, "nombre": "INE"} and m["confianza"] == 92.0
            and m["expediente"] == {"id": ana, "nombre": "Ana Torres"} for m in metidos), str(metidos[:1]))
    rev("lo que entró así queda MARCADO como del clasificador, en cada virtual", all(marcas(ana, v) == {ana_ine: True} for v in (c1, c2, s1)))
    rev("meterlo a mano marca lo contrario", meter_v(adm, org, ana, x1, ana_cta).status_code == 200 and marcas(ana, x1) == {ana_cta: False})
    rev("repetir la revisión no repite nada (idempotente)", colocar(documento_id=ana_ine) == [] and docs_v(adm, org, ana, c1) == [ana_ine])

    clasificar(ana_dom, td_dom, 12.5)
    rev("con CUALQUIER confianza (aquí 12.5 %) entra, y solo a los que esperan un comprobante de domicilio",
        [m["expedienteVirtual"]["id"] for m in colocar(documento_id=ana_dom)] == [c1, c2] and docs_v(adm, org, ana, s1) == [ana_ine])

    clasificar(ana_cta, td_cuenta, 95)
    clasificar(ana_cta, None, 99)
    rev("si la ÚLTIMA opinión es «no es ninguno», no se presume la vieja: no entra", colocar(documento_id=ana_cta) == [])
    clasificar(ana_otro, td_extra, 99)
    rev("un tipo que ningún virtual del expediente espera: no entra a ninguno", colocar(documento_id=ana_otro) == [])
    suelto11 = subir(adm, org, "suelto-11.pdf", 140)
    clasificar(suelto11, td_ine, 99)
    rev("un documento que no está en ningún expediente no entra a ningún virtual", colocar(documento_id=suelto11) == [])
    clasificar(d_b, td_b, 99, tenant=otra)
    rev("lo de otra organización no se toca con el tenant de ésta", colocar(documento_id=d_b) == [])
    try:
        virtuales.colocar_clasificados(org)
        pide_algo = False
    except ValueError:
        pide_algo = True
    rev("sin decir de qué documento o virtual se trata no revisa TODO: lo pide", pide_algo)
    for malo in ("cualquier-cosa", "documento-" + "0" * 31):
        rev(f"un id con forma inválida ({malo[:18]}…) no revisa nada ni truena", colocar(documento_id=malo) == [])

    sacar_v(adm, org, ana, c1, ana_ine)
    c3 = crear_v(adm, org, ana, t_cred, "Crédito 3").json()
    rev("un virtual NUEVO nace con lo que su expediente ya tiene clasificado (el INE de 92 % y el domicilio de 12.5 %), y el de 'otro' o sin tipo no",
        [d["id"] for d in c3["documentos"]] == [ana_ine, ana_dom] and all(d["agregadoPorClasificador"] for d in c3["documentos"]), str([d["id"] for d in c3["documentos"]]))
    rev("lo que alguien SACÓ a mano de otro virtual no vuelve a entrar solo (c1 sigue sin el INE)", docs_v(adm, org, ana, c1) == [ana_dom])
    c4 = crear_v(adm, org, ana, t_cred, "Crédito 4", [ana_ine, ana_otro]).json()
    rev("si se piden documentos a mano Y hay clasificados, nace con la unión, sin repetir y respetando quién lo metió (lo manual NO se re-marca)",
        [(d["id"], d["agregadoPorClasificador"]) for d in c4["documentos"]] == [(ana_ine, False), (ana_otro, False), (ana_dom, True)], str([(d["id"], d["agregadoPorClasificador"]) for d in c4["documentos"]]))
    rev("el virtual que ya existía no se tocó al crear otro", docs_v(adm, org, ana, c2) == [ana_ine, ana_dom])

    luis, luis_b = expediente(adm, org, "Luis Ramos"), expediente(adm, org, "Luis Ramos (copia)")
    l1, l2 = (crear_v(adm, org, luis, t_cred, f"Crédito de Luis {i}").json()["id"] for i in (1, 2))
    l3 = crear_v(adm, org, luis_b, t_cred, "Crédito de la copia").json()["id"]
    luis_ine = subir(adm, org, "luis-ine.pdf", 150)
    clasificar(luis_ine, td_ine, 88)
    r = meter(adm, org, luis, luis_ine)
    rev("meter a un expediente un documento YA clasificado lo mete solo a sus virtuales, y la respuesta dice a cuáles",
        r.status_code == 200 and r.json()["agregado"] is True and [v["id"] for v in r.json()["expedientesVirtuales"]] == [l1, l2]
        and all(v["expedienteId"] == luis for v in r.json()["expedientesVirtuales"]), r.text[:300])
    rev("y el expediente que devuelve ya los trae con el documento adentro", [v["documentos"] for v in r.json()["expediente"]["expedientesVirtuales"]] == [[luis_ine], [luis_ine]])
    r = meter(adm, org, luis, luis_ine)
    rev("meterlo otra vez (ya estaba): `agregado: false`, nada se repite", r.json()["agregado"] is False and r.json()["expedientesVirtuales"] == [] and docs_v(adm, org, luis, l1) == [luis_ine])
    r = meter(adm, org, luis_b, luis_ine)
    rev("meterlo a OTRO expediente solo mueve los virtuales de ese expediente, no los del primero",
        [v["id"] for v in r.json()["expedientesVirtuales"]] == [l3] and docs_v(adm, org, luis, l2) == [luis_ine])
    sacar(adm, org, luis, luis_ine)
    rev("sacarlo del expediente lo saca de sus virtuales; volver a meterlo es un hecho nuevo y los vuelve a llenar",
        docs_v(adm, org, luis, l1) == [] and [v["id"] for v in meter(adm, org, luis, luis_ine).json()["expedientesVirtuales"]] == [l1, l2])
    rev("un rol que solo lee no mete (y entonces el clasificador tampoco)", meter(P["VIEWER"], org, luis, luis_ine).status_code == 403)

    titulo("11b · Subir un archivo a un expediente lo CLASIFICA y, si es de un tipo que algún virtual espera, lo mete solo (Document AI se falsea: no se gasta en pruebas)")
    CONDUCTA: dict = {"respuesta": None, "error": None, "demora": 0.0}
    LLAMADAS: list[int] = []

    async def clasificador_falso(contenido, mime_type, tenant):
        LLAMADAS.append(len(contenido))
        if CONDUCTA["demora"]:
            await asyncio.sleep(CONDUCTA["demora"])
        if CONDUCTA["error"] is not None:
            raise CONDUCTA["error"]
        return CONDUCTA["respuesta"]

    def reconoce(tipo_doc: str | None, confianza: float | None):
        CONDUCTA.update(respuesta={"categoria": normalizar_nombre(tipo_doc) if tipo_doc else "otro", "confianza": confianza}, error=None, demora=0.0)

    def subir_a(bearer: str, tenant: str, exp: str, nombre: str, n: int, tipo_mime: str = "application/pdf"):
        return c.post(f"/expedientes/{exp}/documentos", files={"archivo": (nombre, pdf(n), tipo_mime)}, data={"tenant": tenant}, headers=cab(bearer))

    original, espera = servicio_ia.clasificar_documento, router_expedientes.ESPERA_CLASIFICAR_AL_SUBIR
    servicio_ia.clasificar_documento = clasificador_falso
    try:
        pia = expediente(adm, org, "Pía Salas")
        p1, p2 = (crear_v(adm, org, pia, t_cred, f"Crédito de Pía {i}").json()["id"] for i in (1, 2))
        p_seg = crear_v(adm, org, pia, t_seg, "Seguro de Pía").json()["id"]
        p_cta = crear_v(adm, org, pia, t_cta, "Cuentas de Pía").json()["id"]
        antes_audit = len(auditoria("VIRTUAL_CASE_DOCUMENT_ADDED"))

        reconoce(td_ine, 92.5)
        r = subir_a(adm, org, pia, "pia-ine.pdf", 170)
        j = r.json() if r.status_code == 201 else {}
        rev("un archivo que el clasificador reconoce como INE con 92.5 %: 201 y `clasificacion` dice el estado, el tipo y la confianza",
            r.status_code == 201 and j["clasificacion"] == {"estado": "clasificado", "tipo": {"id": td_ine, "nombre": "INE"}, "confianza": 92.5}, r.text[:300])
        rev("…entró solo a los tres virtuales que esperan INE (los dos de crédito y el de seguro) y no al de cuentas; la respuesta dice a cuáles",
            [(v["id"], v["expedienteId"]) for v in j["expedientesVirtuales"]] == [(p1, pia), (p2, pia), (p_seg, pia)] and docs_v(adm, org, pia, p_cta) == [], str(j.get("expedientesVirtuales")))
        rev("…con su marca de clasificador", all(marcas(pia, v) == {j["documento"]["id"]: True} for v in (p1, p2, p_seg)))
        doc_pia = [d for d in ver(adm, org, pia).json()["documentos"] if d["id"] == j["documento"]["id"]][0]
        rev("…y el expediente ya lo enseña CON su tipo documental (el que dijo el clasificador) y la confianza",
            doc_pia["tipoDocumental"] is not None and doc_pia["tipoDocumental"]["id"] == td_ine and doc_pia["tipoDocumental"]["confianza"] == 92.5, str(doc_pia["tipoDocumental"]))
        nuevos = auditoria("VIRTUAL_CASE_DOCUMENT_ADDED")[antes_audit:]
        rev("deja un `VIRTUAL_CASE_DOCUMENT_ADDED` por virtual, con quién subió, el origen, el tipo y la confianza",
            len(nuevos) == 3 and all(x.get("origen") == "clasificador" and x.get("usuarioGuid") == guid_rita and x.get("tipoDocumentalId") == td_ine and x.get("confianza") == 92.5
                                      and x.get("documentoId") == j["documento"]["id"] and x.get("nombreOriginal") == "pia-ine.pdf" for x in nuevos), str(nuevos[:1]))
        rev("al clasificador solo se le mandaron los bytes del archivo, una vez", LLAMADAS == [len(pdf(170))], str(LLAMADAS))

        reconoce(td_dom, 68)
        r = subir_a(adm, org, pia, "pia-dom.pdf", 171)
        rev("con 68 % de confianza también entra (sin mínimo): a los dos de crédito, que esperan un domicilio, y no al de seguro ni al de cuentas",
            r.status_code == 201 and r.json()["clasificacion"] == {"estado": "clasificado", "tipo": {"id": td_dom, "nombre": "Comprobante de domicilio"}, "confianza": 68.0}
            and [v["id"] for v in r.json()["expedientesVirtuales"]] == [p1, p2], r.text[:300])
        d68 = [d for d in ver(adm, org, pia).json()["documentos"] if d["id"] == r.json()["documento"]["id"]][0]
        rev("…y el documento queda tipado como lo reconoció, con su confianza", (d68["tipoDocumental"] or {}).get("id") == td_dom and d68["tipoDocumental"]["confianza"] == 68.0)

        reconoce(None, 88)
        r = subir_a(adm, org, pia, "pia-rara.pdf", 172)
        rev("«otro» (no es ninguno de los tipos): estado `sin_tipo`, sin tipo y sin virtual",
            r.status_code == 201 and r.json()["clasificacion"] == {"estado": "sin_tipo", "tipo": None, "confianza": 88.0} and r.json()["expedientesVirtuales"] == [], r.text[:300])

        CONDUCTA.update(error=SinClasificador("sin clasificador"))
        r = subir_a(adm, org, pia, "pia-sin-clasificador.pdf", 173)
        rev("una organización sin clasificador todavía: el archivo se SUBE igual y la respuesta dice `sin_clasificador`",
            r.status_code == 201 and r.json()["clasificacion"]["estado"] == "sin_clasificador" and r.json()["clasificacion"]["tipo"] is None
            and r.json()["documento"]["id"] in [d["id"] for d in ver(adm, org, pia).json()["documentos"]], r.text[:300])

        CONDUCTA.update(error=RuntimeError("Document AI no contesta"))
        r = subir_a(adm, org, pia, "pia-falla.pdf", 174)
        rev("si Document AI falla: 201, el archivo está en el expediente sin tipo, y se dice `no_clasificado` (nada de la falla viaja al navegador)",
            r.status_code == 201 and r.json()["clasificacion"]["estado"] == "no_clasificado" and "Document AI" not in r.text
            and r.json()["documento"]["id"] in [d["id"] for d in ver(adm, org, pia).json()["documentos"]], r.text[:300])

        router_expedientes.ESPERA_CLASIFICAR_AL_SUBIR = 0.05
        CONDUCTA.update(error=None, demora=1.0, respuesta={"categoria": normalizar_nombre(td_ine), "confianza": 99})
        r = subir_a(adm, org, pia, "pia-lenta.pdf", 175)
        rev("si el clasificador tarda más de la espera, no se cuelga: 201 y `no_clasificado`, sin entrar a ningún virtual",
            r.status_code == 201 and r.json()["clasificacion"]["estado"] == "no_clasificado" and r.json()["expedientesVirtuales"] == [], r.text[:300])
        router_expedientes.ESPERA_CLASIFICAR_AL_SUBIR = espera

        reconoce(td_ine, 99)
        gastadas = len(LLAMADAS)
        rechazos = [
            subir_a(P["VIEWER"], org, pia, "viewer.pdf", 176).status_code == 403,
            subir_a(adm, org, pia, "gif.gif", 177, "image/gif").status_code == 400,
            subir_a(adm, org, "expediente-" + "0" * 32, "x.pdf", 178).status_code == 404,
            subir_a(adm, otra, pia, "x.pdf", 179).status_code == 403,
        ]
        rev("lo que se rechaza (rol que solo lee, formato, expediente inexistente, otra organización) NO le pregunta al clasificador: no se gasta", all(rechazos) and len(LLAMADAS) == gastadas, f"{rechazos} {len(LLAMADAS) - gastadas}")

        guardada = router_expedientes.SQLSERVER_HOST
        try:
            router_expedientes.SQLSERVER_HOST = ""
            r = subir_a(adm, org, pia, "pia-sin-base.pdf", 180)
            rev("sin base (no hay dónde anotar ni virtuales): `no_clasificado` y tampoco se gasta", r.status_code == 201 and r.json()["clasificacion"]["estado"] == "no_clasificado" and len(LLAMADAS) == gastadas)
        finally:
            router_expedientes.SQLSERVER_HOST = guardada

        titulo("11c · Clasificar un documento que YA está en un expediente (POST /ia/clasificar con `documento`)")
        marta = expediente(adm, org, "Marta Gil")
        m1 = crear_v(adm, org, marta, t_seg, "Seguro de Marta").json()["id"]
        marta_ine, marta_baja, marta_void = (subir(adm, org, f"marta-{n}.pdf", 190 + i) for i, n in enumerate(("ine", "baja", "void")))
        meter(adm, org, marta, marta_ine)
        meter(adm, org, marta, marta_baja)

        def clasificar_por_api(doc: str, n: int, bearer: str = adm):
            return c.post("/ia/clasificar", params={"tenant": org, "documento": doc}, files={"archivo": (f"m{n}.pdf", pdf(n), "application/pdf")}, headers=cab(bearer))

        reconoce(td_ine, 91)
        r = clasificar_por_api(marta_ine, 190)
        rev("la respuesta de `/ia/clasificar` es la misma de siempre (no se le agregó nada) y el INE de 91 % entró solo al virtual de seguro",
            r.status_code == 200 and r.json() == {"categoria": normalizar_nombre(td_ine), "confianza": 91} and marcas(marta, m1) == {marta_ine: True}, r.text[:200])
        reconoce(td_ine, 60)
        r = clasificar_por_api(marta_baja, 191)
        rev("con 60 % también entra: no hay mínimo", r.status_code == 200 and marcas(marta, m1) == {marta_ine: True, marta_baja: True})
        reconoce(td_ine, 99)
        r = clasificar_por_api(marta_void, 192)
        rev("un documento que no está en ningún expediente se clasifica sin error y sin entrar a nada", r.status_code == 200 and marcas(marta, m1) == {marta_ine: True, marta_baja: True})
        sacar_v(adm, org, marta, m1, marta_ine)
        reconoce(td_ine, 93)
        clasificar_por_api(marta_ine, 190)
        rev("volver a clasificar uno que alguien sacó a mano es un hecho nuevo: entra otra vez", marcas(marta, m1) == {marta_ine: True, marta_baja: True})
    finally:
        servicio_ia.clasificar_documento = original
        router_expedientes.ESPERA_CLASIFICAR_AL_SUBIR = espera

    titulo("11d · Ya no hay confianza mínima que la pantalla lea de la API")
    rev("ni el expediente ni el expediente virtual traen `clasificacionAutomatica`", "clasificacionAutomatica" not in ver(adm, org, ana).json() and "clasificacionAutomatica" not in ver_v(adm, org, ana, c3["id"]).json())
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
