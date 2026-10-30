"""¿Se GUARDA lo que Document AI dice de cada documento? (migración `0007`, `servicios/datos_extraidos.py`)

    .venv/Scripts/python verificar_datos_extraidos.py
    .venv/Scripts/python verificar_datos_extraidos.py --dejar     (deja vivo el contenedor para investigar)

Solo SQL (todo lo nuevo directo a SQL), así que corre contra el SQL Server desechable de `pruebas_sql.py`; sin Docker se salta y lo dice.
Sin red: `ia._procesar` y `ia._estructura_de` se sustituyen por respuestas de mentiras.

Lo que se prueba (Moibe, 2026-10-29: «una cosa es el file y otra los datos que se extrajeron»):

- `/ia/clasificar?documento=` guarda la clasificación y la traduce al tipo documental de la organización; `otro` queda sin tipo;
- `/ia/extraer` con `documento` guarda la corrida (tipo, revisión, procesador, versión, confianzas, páginas) y UN renglón por campo: hojas,
  grupos (`domicilio.calle`), filas de lo que se repite (`socios[2].nombre`, con su índice) y lo obligatorio ausente con `not_present`;
- reprocesar crea otra corrida y la vigente es la nueva; una corrida que FALLA se guarda (con su motivo) pero no borra los datos vigentes;
- `quality_alert` se guarda como `no_reconocido`;
- antes de pagarle a Google se rechaza: documento ajeno (404), tipo ajeno o procesador que no es el del tipo (422), quien no es de la
  organización (403); y en esos casos Google no se llama;
- sin `documento`, `/ia/extraer` sigue funcionando como antes y no guarda nada;
- corregir un dato: el vigente cambia, el extraído no; queda el rastro; los roles que solo leen no corrigen; un campo que no existe: 404;
- los listados y el expediente virtual enseñan el tipo documental de cada documento, y el virtual dice qué tipo de su esquema cubre cada uno;
- las reglas de la base: valor o motivo (nunca los dos, nunca ninguno), y borrar el documento se lleva todo lo suyo.
"""

import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pruebas_sql  # noqa: E402

BASE = pruebas_sql.preparar("nexus-datos-extraidos-prueba", 14337, dejar="--dejar" in sys.argv)

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-datos-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import auth, clasificadores, ia, usuarios  # noqa: E402
from servicios.esquema import normalizar_nombre  # noqa: E402

fallos = 0
PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + str(extra) if extra and not ok else ''}")
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


def subir(bearer: str, tenant: str, n: int) -> str:
    r = c.post("/bandeja/", files={"archivo": (f"doc{n}.pdf", PDF + f"%{n}\n".encode(), "application/pdf")}, data={"tenant": tenant}, headers=cab(bearer))
    assert r.status_code == 201, r.text
    return r.json()["documentoId"]


def tipo_documental(bearer: str, tenant: str, nombre: str, procesador: str) -> str:
    tid = "tipo-" + uuid.uuid4().hex
    r = c.put(f"/tipos-documentales/{tid}", json={"tenant": tenant, "revision": 0, "datos": {"nombre": nombre, "descripcion": "", "estado": "activo", "procesadorId": procesador}}, headers=cab(bearer))
    assert r.status_code == 200, r.text
    return tid


# ── Document AI de mentiras ─────────────────────────────────────────────────────────────────────────────────────────────


def entidad(tipo: str, texto: str, confianza: float = 0.9, pagina: int = 0, fecha: dict | None = None) -> dict:
    e = {"type": tipo, "mentionText": texto, "confidence": confianza, "pageAnchor": {"pageRefs": [{"page": pagina}]}}
    if fecha:
        e["normalizedValue"] = {"dateValue": fecha}
    return e


ACTA = {
    "document": {
        "text": "",
        "pages": [{"pageNumber": 1}, {"pageNumber": 2}],
        "entities": [
            entidad("denominacion", "ACME SA DE CV", 0.97),
            entidad("fecha", "3 de marzo de 2020", 0.8, pagina=1, fecha={"year": 2020, "month": 3, "day": 3}),
            {"type": "domicilio", "mentionText": "", "confidence": 0.9, "properties": [entidad("calle", "Reforma 10", 0.88)]},
            {"type": "socios", "mentionText": "", "confidence": 0.9, "properties": [entidad("nombre", "Ana", 0.95), entidad("acciones", "50", 0.9)]},
            {"type": "socios", "mentionText": "", "confidence": 0.9, "properties": [entidad("nombre", "Beto", 0.6)]},
        ],
    }
}
ESTRUCTURA = {
    "denominacion": {"obligatorio": True, "multiple": False},
    "capital": {"obligatorio": True, "multiple": False},
    "fecha": {"obligatorio": False, "multiple": False},
    "domicilio": {"obligatorio": False, "multiple": False, "hijos": {"calle": {"obligatorio": True, "multiple": False}}},
    "socios": {"obligatorio": False, "multiple": True, "hijos": {"nombre": {"obligatorio": True, "multiple": False}, "acciones": {"obligatorio": True, "multiple": False}}},
}
respuestas: dict[str, dict] = {}
llamadas: list[str] = []
falla_google = {"si": False}


async def _procesar_falso(procesador_id, contenido, mime_type, version="", opciones=None):
    from errores import ErrorDocumentAI

    llamadas.append(procesador_id)
    if falla_google["si"]:
        raise ErrorDocumentAI("Google caído", status_code=503, motivo="servicio")
    return respuestas[procesador_id]


async def _estructura_falsa(procesador_id):
    return ESTRUCTURA


ia._procesar = _procesar_falso  # type: ignore[assignment]
ia._estructura_de = _estructura_falsa  # type: ignore[assignment]


def extraer(bearer, tenant, doc, tipo, procesador, version="7"):
    data = {"procesador": procesador, "version": version}
    if doc:
        data |= {"documento": doc, "tipoDocumental": tipo, "tenant": tenant}
    return c.post("/ia/extraer", files={"imagen": ("a.pdf", PDF, "application/pdf")}, data=data, headers=cab(bearer))


def clasificar(bearer, tenant, doc):
    return c.post("/ia/clasificar", params={"tenant": tenant, "documento": doc}, files={"archivo": ("a.pdf", PDF, "application/pdf")}, headers=cab(bearer))


def datos(bearer, tenant, doc):
    return c.get(f"/expedientes/documentos/{doc}/datos", params={"tenant": tenant}, headers=cab(bearer))


def campos(bearer, tenant, doc) -> dict[str, dict]:
    return {f["ruta"]: f for f in datos(bearer, tenant, doc).json()["campos"]}


def base():
    return BASE["conectar"]("sa", BASE["sa"], BASE["base"])


try:
    titulo("0 · Montaje")
    temporal = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal, "Cobre-Lento-Azul-47!")
    org, adm = organizacion(bearer_super, "Alfa", "rita@ejemplo.com")
    otra, adm_b = organizacion(bearer_super, "Beta", "beto@ejemplo.com")
    r = c.post("/usuarios/", json={"nombre": "Vera", "apellidoPaterno": "Prueba", "email": "viewer@ejemplo.com", "rol": "VIEWER"}, headers=cab(adm))
    viewer = con_primer_acceso("viewer@ejemplo.com", r.json()["contrasenaTemporal"], "Rumbo-Sereno-Azul-48!")
    acta = tipo_documental(adm, org, "Acta constitutiva", "proc-acta")
    ine = tipo_documental(adm, org, "INE", "proc-ine")
    ajeno = tipo_documental(adm_b, otra, "Acta de Beta", "proc-beta")
    respuestas["proc-acta"] = ACTA
    respuestas["proc-beta"] = ACTA
    clasificadores.asignar(org, "clasif-alfa")
    d1, d2, d3 = subir(adm, org, 1), subir(adm, org, 2), subir(adm, org, 3)
    db = subir(adm_b, otra, 4)
    rev("montaje listo", True)

    titulo("1 · Antes de procesar: nada")
    r = datos(adm, org, d1)
    rev("200 y vacío", r.status_code == 200 and r.json() == {"clasificacion": None, "extraccion": None, "campos": []}, r.text)
    rev("documento de otra organización: 404", datos(adm, org, db).status_code == 404)
    rev("con el tenant de otra: 403", datos(adm, otra, d1).status_code == 403)

    titulo("2 · Clasificar guarda la clasificación")
    respuestas["clasif-alfa"] = {"document": {"entities": [{"type": normalizar_nombre(acta), "confidence": 0.93}, {"type": "otro", "confidence": 0.07}]}}
    r = clasificar(adm, org, d1)
    rev("la respuesta no cambia", r.status_code == 200 and r.json() == {"categoria": normalizar_nombre(acta), "confianza": 93.0}, r.text)
    cl = datos(adm, org, d1).json()["clasificacion"]
    rev("guardada con su tipo documental y confianza", cl and cl["tipoDocumental"] == {"id": acta, "nombre": "Acta constitutiva"} and cl["confianza"] == 93.0, cl)
    respuestas["clasif-alfa"] = {"document": {"entities": [{"type": "otro", "confidence": 0.99}]}}
    clasificar(adm, org, d2)
    cl = datos(adm, org, d2).json()["clasificacion"]
    rev("`otro` queda sin tipo", cl and cl["categoria"] == "otro" and cl["tipoDocumental"] is None, cl)
    r = clasificar(adm, org, db)
    rev("documento ajeno: clasifica igual (no rompe) y no guarda nada", r.status_code == 200 and datos(adm_b, otra, db).json()["clasificacion"] is None, r.text)

    titulo("3 · Extraer guarda la corrida y un renglón por campo")
    r = extraer(adm, org, d1, acta, "proc-acta")
    md = r.json().get("_metadata", {})
    rev("200, con `guardado` y `extraccionId`", r.status_code == 200 and md.get("guardado") is True and str(md.get("extraccionId", "")).startswith("extraccion-"), r.text)
    x = datos(adm, org, d1).json()
    e = x["extraccion"]
    rev("corrida: tipo, revisión, procesador y versión", e["tipoDocumental"]["id"] == acta and e["tipoDocumental"]["revision"] == 1 and e["procesador"] == {"id": "proc-acta", "version": "7"}, e)
    rev("corrida: estado ok, confianzas y 1 corrida", e["estado"] == "ok" and e["confianzaPromedio"] is not None and e["confianzaMinima"] == 60.0 and e["totalCorridas"] == 1, e)
    f = {c_["ruta"]: c_ for c_ in x["campos"]}
    rev(
        "rutas: hojas, grupo y filas",
        set(f) >= {"denominacion", "fecha", "domicilio.calle", "socios[1].nombre", "socios[1].acciones", "socios[2].nombre"},
        sorted(f),
    )
    rev("valor normalizado y crudo (la fecha a ISO)", f["fecha"]["valor"] == "2020-03-03" and f["fecha"]["valorCrudo"] == "3 de marzo de 2020", f["fecha"])
    rev("confianza 0-100 y página", f["denominacion"]["confianza"] == 97.0 and f["fecha"]["pagina"] == 2 and f["denominacion"]["pagina"] == 1, f["fecha"])
    rev("índice de registro en lo que se repite", f["socios[2].nombre"]["registro"] == 2 and f["socios[2].nombre"]["nombre"] == "nombre" and f["denominacion"]["registro"] is None)
    rev("obligatorio ausente: renglón con not_present", f.get("capital", {}).get("motivoSinValor") == "not_present" and f["capital"]["valor"] is None, f.get("capital"))
    rev("fila sin subcampo obligatorio: not_present con su índice", f.get("socios[2].acciones", {}).get("motivoSinValor") == "not_present" and f["socios[2].acciones"]["registro"] == 2, f.get("socios[2].acciones"))
    con = base()
    k = con.cursor()
    k.execute("SELECT confidenceRaw, confidenceMethod FROM nexus.entityFacts WHERE fieldPath = 'denominacion'")
    fila = k.fetchone()
    rev("confianza original y método guardados", abs(fila[0] - 0.97) < 1e-9 and fila[1] == "extractor_confidence", fila)

    titulo("4 · El tipo documental se ve en los listados y en el expediente virtual")
    r = c.get("/expedientes/documentos", params={"tenant": org}, headers=cab(adm))
    tipos = {d["id"]: d["tipoDocumental"] for d in r.json()["documentos"]}
    rev("extraído: el tipo de su extracción", tipos[d1] and tipos[d1]["id"] == acta and tipos[d1]["origen"] == "extraccion", tipos[d1])
    rev("clasificado `otro` y sin extraer: sin tipo", tipos[d2] is None and tipos[d3] is None, (tipos[d2], tipos[d3]))
    t = c.post("/tipos-expediente/", json={"tenant": org, "nombre": "Alta de empresa", "tiposDocumentales": [acta, ine]}, headers=cab(adm)).json()["id"]
    exp = c.post("/expedientes/", json={"tenant": org, "nombre": "ACME"}, headers=cab(adm)).json()["id"]
    for d in (d1, d2):
        c.put(f"/expedientes/{exp}/documentos/{d}", params={"tenant": org}, headers=cab(adm))
    rev("el expediente enseña el tipo", next(d for d in c.get(f"/expedientes/{exp}", params={"tenant": org}, headers=cab(adm)).json()["documentos"] if d["id"] == d1)["tipoDocumental"]["id"] == acta)
    v = c.post(f"/expedientes/{exp}/virtuales", json={"tenant": org, "tipoId": t, "nombre": "Alta", "documentos": [d1, d2]}, headers=cab(adm)).json()["id"]
    esquema = {x_["id"]: x_["documentos"] for x_ in c.get(f"/expedientes/{exp}/virtuales/{v}", params={"tenant": org}, headers=cab(adm)).json()["tipo"]["tiposDocumentales"]}
    rev("el virtual dice qué cubre cada tipo del esquema (y que falta la INE)", esquema == {acta: [d1], ine: []}, esquema)

    titulo("5 · Reprocesar crea otra corrida; la vigente es la nueva")
    respuestas["proc-acta"] = {"document": {"text": "", "pages": [{}], "entities": [entidad("denominacion", "ACME SAPI", 0.99), entidad("capital", "1000", 0.9)]}}
    extraer(adm, org, d1, acta, "proc-acta")
    x = datos(adm, org, d1).json()
    f = {c_["ruta"]: c_ for c_ in x["campos"]}
    rev("2 corridas, la vigente con sus valores", x["extraccion"]["totalCorridas"] == 2 and f["denominacion"]["valor"] == "ACME SAPI" and f["capital"]["valor"] == "1000", f)
    rev("lo de la corrida vieja ya no es vigente", "socios[1].nombre" not in f)
    k.execute("SELECT count(*) FROM nexus.entityFacts")
    rev("pero sigue guardado (solo agregar)", k.fetchone()[0] >= 10)

    titulo("6 · Una corrida que falla se guarda, sin borrar lo vigente")
    falla_google["si"] = True
    r = extraer(adm, org, d1, acta, "proc-acta")
    falla_google["si"] = False
    x = datos(adm, org, d1).json()
    rev("la respuesta es el error de siempre (502)", r.status_code == 502, r.status_code)
    rev("la última corrida dice error con su motivo", x["extraccion"]["estado"] == "error" and "Document AI" in (x["extraccion"]["error"] or ""), x["extraccion"])
    rev("los campos vigentes siguen siendo los de antes", {c_["ruta"]: c_["valor"] for c_ in x["campos"]}.get("denominacion") == "ACME SAPI")

    titulo("7 · quality_alert se guarda como no_reconocido")
    respuestas["proc-acta"] = {"document": {"text": ""}}
    extraer(adm, org, d3, acta, "proc-acta")
    rev("estado no_reconocido", datos(adm, org, d3).json()["extraccion"]["estado"] == "no_reconocido")

    titulo("8 · Lo que se rechaza ANTES de pagarle a Google")
    respuestas["proc-acta"] = ACTA
    antes = len(llamadas)
    rev("documento de otra organización: 404", extraer(adm, org, db, acta, "proc-acta").status_code == 404)
    rev("tipo documental de otra organización: 422", extraer(adm, org, d2, ajeno, "proc-beta").status_code == 422)
    rev("procesador que no es el del tipo: 422", extraer(adm, org, d2, ine, "proc-acta").status_code == 422)
    rev("con el tenant de otra organización: 403", extraer(adm, otra, d2, acta, "proc-acta").status_code == 403)
    rev("sin sesión: 401", extraer(None, org, d2, acta, "proc-acta").status_code == 401)
    rev("Google no se llamó ni una vez", len(llamadas) == antes, len(llamadas) - antes)
    r = extraer(None, org, None, None, "proc-acta")
    rev("sin `documento`: extrae como siempre y no guarda", r.status_code == 200 and "guardado" not in r.json()["_metadata"] and datos(adm, org, d2).json()["extraccion"] is None, r.text[:200])

    titulo("9 · Corregir un dato")
    extraer(adm, org, d2, acta, "proc-acta")
    corr = lambda b, ruta, valor, tenant=org, doc=d2: c.post(f"/expedientes/documentos/{doc}/datos/correcciones", json={"tenant": tenant, "ruta": ruta, "valor": valor, "motivo": "dice otra cosa"}, headers=cab(b))  # noqa: E731
    r = corr(adm, "socios[2].nombre", "Alberto")
    rev("201 con el campo corregido", r.status_code == 201 and r.json()["valor"] == "Alberto" and r.json()["corregido"] and r.json()["valorExtraido"] == "Beto", r.text)
    r = corr(adm, "capital", "5000")
    rev("se puede corregir un campo ausente: deja de ser ausente", r.status_code == 201 and r.json()["valor"] == "5000" and r.json()["motivoSinValor"] is None, r.text)
    r = corr(adm, "socios[2].nombre", "Alberto B.")
    rev("la última corrección manda", r.json()["valor"] == "Alberto B.")
    k.execute("SELECT valueNormalized FROM nexus.entityFacts f JOIN nexus.extractionRuns r ON r.runId = f.runId JOIN nexus.documents d ON d.documentId = r.documentId WHERE d.publicId = ? AND f.fieldPath = 'socios[2].nombre'", d2)
    rev("el dato extraído no se tocó", k.fetchone()[0] == "Beto")
    rev("campo que no existe: 404", corr(adm, "no_existe", "x").status_code == 404)
    rev("valor vacío: 400", corr(adm, "capital", "  ").status_code == 400)
    rev("un Viewer no corrige: 403", corr(viewer, "capital", "1").status_code == 403)
    rev("documento ajeno: 404", corr(adm, "capital", "1", doc=db).status_code == 404)
    from servicios import auditoria as bitacora

    rev("queda el rastro FIELD_CORRECTED", len(bitacora.eventos({"FIELD_CORRECTED"})) == 3)
    rev("reprocesar después de corregir: la corrida nueva manda", (extraer(adm, org, d2, acta, "proc-acta"), campos(adm, org, d2)["socios[2].nombre"]["valor"])[1] == "Beto")

    titulo("10 · Las reglas de la base")
    k.execute("SELECT TOP (1) runId FROM nexus.extractionRuns WHERE status = 'ok'")
    corrida = k.fetchone()[0]
    for desc, sql in (
        ("valor y motivo a la vez", "INSERT INTO nexus.entityFacts (runId, fieldPath, fieldName, valueNormalized, nullReason) VALUES (?, 'z1', 'z1', 'x', 'not_present')"),
        ("ni valor ni motivo", "INSERT INTO nexus.entityFacts (runId, fieldPath, fieldName) VALUES (?, 'z2', 'z2')"),
        ("motivo que no existe", "INSERT INTO nexus.entityFacts (runId, fieldPath, fieldName, nullReason) VALUES (?, 'z3', 'z3', 'flojera')"),
        ("confianza de 150", "INSERT INTO nexus.entityFacts (runId, fieldPath, fieldName, valueNormalized, confidence) VALUES (?, 'z4', 'z4', 'x', 150)"),
        ("la misma ruta dos veces en una corrida", "INSERT INTO nexus.entityFacts (runId, fieldPath, fieldName, valueNormalized) VALUES (?, 'denominacion', 'd', 'x')"),
    ):
        try:
            k.execute(sql, corrida)
            rev(f"la base rechaza: {desc}", False, "lo aceptó")
        except Exception:  # noqa: BLE001
            rev(f"la base rechaza: {desc}", True)
    k.execute("SELECT documentId FROM nexus.documents WHERE publicId = ?", d3)
    interno = k.fetchone()[0]
    k.execute("DELETE FROM nexus.caseDocuments WHERE documentId = ?", interno)
    k.execute("DELETE FROM nexus.documents WHERE documentId = ?", interno)
    k.execute("SELECT count(*) FROM nexus.extractionRuns WHERE documentId = ?", interno)
    rev("borrar el documento se lleva sus corridas", k.fetchone()[0] == 0)
    k.execute("DELETE FROM nexus.documentTypes WHERE publicId = ?", ine)
    rev("borrar un tipo documental no borra corridas de otros", datos(adm, org, d1).json()["extraccion"] is not None)
    con.close()
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print(f"\n{'TODO BIEN' if fallos == 0 else f'{fallos} FALLA(S)'}")
sys.exit(1 if fallos else 0)
