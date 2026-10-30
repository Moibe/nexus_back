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
- el tipo cuenta sus virtuales (`totalExpedientesVirtuales`) y un renombre del tipo se ve en ellos;
- lo de otra organización contesta 404 (o 403 con su tenant); los roles que solo leen ven pero no tocan; cada cosa deja su rastro;
- borrar un tipo mientras se crean virtuales de él: nunca queda uno huérfano.
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


def expediente(bearer: str, tenant: str, nombre: str) -> str:
    r = c.post("/expedientes/", json={"tenant": tenant, "nombre": nombre}, headers=cab(bearer))
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
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
