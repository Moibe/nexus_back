"""¿Las EJECUCIONES funcionan de punta a punta? (migración `0009`, `servicios/ejecuciones.py`, `routers/ejecuciones.py`)

    .venv/Scripts/python verificar_ejecuciones.py
    .venv/Scripts/python verificar_ejecuciones.py --dejar     (deja vivo el contenedor para investigar)

Las ejecuciones SOLO viven en SQL (decisión del 2026-10-28: «todo lo nuevo directo a SQL»), así que esta prueba corre siempre contra el SQL Server
desechable de `pruebas_sql.py`; sin Docker se salta y lo dice. El almacén (bandeja, usuarios, auditoría) es una carpeta temporal.

Lo que se prueba (Moibe, 2026-11-03: se ejecuta un proceso sobre un expediente virtual con los datos REALES de sus documentos; con duplicados no se
corre, se avisa y los documentos quedan marcados; el historial es inmutable):

- `preparar`: el proceso con su dibujo y su revisión, los tipos documentales que usa y, por cada uno, el documento del virtual que lo cubre con sus
  valores VIGENTES por el nombre del campo en la BIBLIOTECA («Año de registro», `socios.nombre`) aunque Document AI los guarde como `ano_de_registro`
  y `socios[2].nombre`; listas, filas de un grupo, campos ausentes, la confianza más baja y la página; una corrección a mano manda sobre lo extraído;
  un documento solo clasificado (sin extraer) cubre su tipo pero no trae valores; un tipo sin documento queda sin ellos;
- DUPLICADOS: dos documentos del mismo tipo en el virtual se entregan en `duplicados` y no dan valores; el virtual y el expediente los marcan
  (`duplicado`, `duplicadoEn`) y sacar uno quita la marca; un tipo que el proceso no usa puede estar duplicado sin estorbar;
- lo que impide ejecutar, cada cosa con su código (409): proceso inactivo, que corre sobre otro tipo, sin Resultado, con el Resultado sin regla;
- `registrar`: guarda la corrida con los nombres y el dibujo que toma de la base (no de la petición), la lista de valores finales como renglones, y se
  niega a lo que no cuadra (una palabra que no es la del proceso, un valor final ajeno, una revisión vieja, formas imposibles);
- el historial: de la más reciente a la más antigua, filtros, paginación, abrir una; sigue igual si el proceso se renombra, se edita o el virtual se
  borra; es inmutable (ninguna ruta lo edita ni lo borra) y la base misma rechaza una fila incoherente;
- roles (ver todos; ejecutar solo quien organiza), aislamiento entre organizaciones y rastro.
"""

import json
import shutil
import sys
import tempfile
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pruebas_sql  # noqa: E402

BASE = pruebas_sql.preparar("nexus-ejecuciones-prueba", 14341, dejar="--dejar" in sys.argv)

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-ejecuciones-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import auth, datos_extraidos, esquema, expedientes_virtuales, usuarios  # noqa: E402

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


def subir(bearer: str, tenant: str, nombre: str, n: int) -> str:
    r = c.post("/bandeja/", files={"archivo": (nombre, PDF + f"%{n}\n".encode(), "application/pdf")}, data={"tenant": tenant}, headers=cab(bearer))
    assert r.status_code == 201, r.text
    return r.json()["documentoId"]


def campo_def(nombre: str, tipo_dato: str = "texto", **extra) -> dict:
    return {
        "id": "campo-" + uuid.uuid4().hex[:8], "nombre": nombre, "tipoDato": tipo_dato, "valorEstructura": "", "descripcion": "", "obligatorio": False,
        "umbralConfianza": "80", "reglaTransformacion": "", "cardinalidad": "unico", "valoresLista": [], "valorListaEnCaptura": "", "subcampos": [], **extra,
    }


def sub_def(nombre: str, tipo_dato: str = "texto") -> dict:
    return {"id": "sub-" + uuid.uuid4().hex[:8], "nombre": nombre, "tipoDato": tipo_dato, "descripcion": "", "obligatorio": False}


def tipo_documental(bearer: str, tenant: str, nombre: str, campos: list[dict]) -> str:
    tid = "tipo-" + uuid.uuid4().hex
    datos = {
        "nombre": nombre, "descripcion": "", "vertical": "", "campos": campos, "pasoMaximo": 3, "guardadoEn": "2026-11-03T00:00:00Z", "estado": "activo", "version": 1,
        "procesadorId": "", "procesadorVersion": "", "procesadorDisplayName": "", "ejemploDocumental": False, "activadoEn": None, "calibradoEn": None,
        "historialVersiones": [], "versionEnEdicion": None, "documentosEjemplo": [], "recortesPorDocumento": {},
    }
    r = c.put(f"/tipos-documentales/{tid}", json={"tenant": tenant, "revision": 0, "datos": datos}, headers=cab(bearer))
    assert r.status_code == 200, r.text
    return tid


def tipo_virtual(bearer: str, tenant: str, nombre: str, esquema_ids: list[str]) -> str:
    r = c.post("/tipos-expediente/", json={"tenant": tenant, "nombre": nombre, "tiposDocumentales": esquema_ids}, headers=cab(bearer))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def expediente(bearer: str, tenant: str, nombre: str) -> str:
    r = c.post("/expedientes/", json={"tenant": tenant, "nombre": nombre}, headers=cab(bearer))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def meter(bearer: str, tenant: str, exp: str, doc: str):
    return c.put(f"/expedientes/{exp}/documentos/{doc}", params={"tenant": tenant}, headers=cab(bearer))


def crear_v(bearer: str, tenant: str, exp: str, t: str, nombre: str, documentos=None):
    r = c.post(f"/expedientes/{exp}/virtuales", json={"tenant": tenant, "tipoId": t, "nombre": nombre, "documentos": documentos or []}, headers=cab(bearer))
    assert r.status_code == 201, r.text
    return r.json()["id"]


def meter_v(bearer: str, tenant: str, exp: str, v: str, doc: str):
    return c.put(f"/expedientes/{exp}/virtuales/{v}/documentos/{doc}", params={"tenant": tenant}, headers=cab(bearer))


def sacar_v(bearer: str, tenant: str, exp: str, v: str, doc: str):
    return c.delete(f"/expedientes/{exp}/virtuales/{v}/documentos/{doc}", params={"tenant": tenant}, headers=cab(bearer))


def ver_v(bearer: str, tenant: str, exp: str, v: str) -> dict:
    return c.get(f"/expedientes/{exp}/virtuales/{v}", params={"tenant": tenant}, headers=cab(bearer)).json()


def ver_exp(bearer: str, tenant: str, exp: str) -> dict:
    return c.get(f"/expedientes/{exp}", params={"tenant": tenant}, headers=cab(bearer)).json()


# ── los dibujos de un proceso ──
def n_tipo(tipo_id: str) -> dict:
    return {"id": f"tipo-{tipo_id}", "type": "tipo-documental", "position": {"x": 0, "y": 0}, "data": {"tipoId": tipo_id}}


def n_op(n: int, operador: str, a: str = "", b: str = "") -> dict:
    return {"id": f"operador-{n}", "type": "operador", "position": {"x": 300, "y": n * 10}, "data": {"operador": operador, "a": a, "b": b}}


def n_fijo(n: int, familia: str, valor: str) -> dict:
    return {"id": f"constante-{n}", "type": "constante", "position": {"x": 300, "y": 400}, "data": {"familia": familia, "valor": valor}}


def n_vf(n: int, nombre: str, a: str = "") -> dict:
    return {"id": f"valor-final-{n}", "type": "valor-final", "position": {"x": 700, "y": n * 10}, "data": {"nombre": nombre, "a": a}}


def n_res(si: str = "Certificado", no: str = "Rechazado", sin: str = "Pendiente") -> dict:
    return {"id": "resultado-final", "type": "resultado-final", "position": {"x": 1000, "y": 0}, "data": {"si": si, "no": no, "sin": sin}}


def cable(origen: str, salida: str | None, destino: str, entrada: str) -> dict:
    return {"id": f"cable-{origen}-{salida}-{destino}-{entrada}", "source": origen, "sourceHandle": salida, "target": destino, "targetHandle": entrada}


def guardar_proceso(bearer: str, tenant: str, pid: str, datos: dict, revision: int):
    return c.put(f"/procesos/{pid}", json={"tenant": tenant, "revision": revision, "datos": datos}, headers=cab(bearer))


def preparar(bearer: str, tenant: str, exp: str, v: str, p: str):
    return c.get("/ejecuciones/preparar", params={"tenant": tenant, "expediente": exp, "virtual": v, "proceso": p}, headers=cab(bearer))


def registrar(bearer: str, cuerpo: dict):
    return c.post("/ejecuciones/", json=cuerpo, headers=cab(bearer))


def historial(bearer: str, tenant: str, **filtros):
    return c.get("/ejecuciones/", params={"tenant": tenant, **filtros}, headers=cab(bearer))


def abrir(bearer: str, tenant: str, eid: str):
    return c.get(f"/ejecuciones/{eid}", params={"tenant": tenant}, headers=cab(bearer))


# ── lo extraído ──
def hecho(valor: str, confianza: float = 90, pagina: int = 1) -> dict:
    """Un campo hoja como lo empaqueta `ia._campo`."""
    return {"value_raw": valor, "value_normalized": valor, "confianza": confianza, "confianza_cruda": confianza / 100, "metodo_confianza": "extractor_confidence", "page_number": pagina}


def extraer(tenant: str, doc: str, tipo_id: str, resultado: dict) -> None:
    datos_extraidos.registrar_extraccion(tenant, doc, tipo_id, "", "", datetime.now(timezone.utc).replace(tzinfo=None), "prueba", resultado={"confianza_promedio": 85, "confianza_minima": 60, "_metadata": {}, **resultado})


def clasificar(tenant: str, doc: str, tipo_id: str, confianza: float = 95) -> None:
    datos_extraidos.registrar_clasificacion(tenant, doc, {"categoria": esquema.normalizar_nombre(tipo_id), "confianza": confianza}, "clasificador-de-prueba", "prueba")


def auditoria(accion: str) -> list[dict]:
    from servicios import auditoria as bitacora

    return bitacora.eventos({accion})


try:
    titulo("0 · Montaje: dos organizaciones, personas de varios roles, tipos documentales con campos, un tipo de expediente virtual, un expediente y su virtual")
    temporal = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal, "Cobre-Lento-Azul-47!")
    org, adm = organizacion(bearer_super, "Alfa", "rita@ejemplo.com")
    otra, adm_b = organizacion(bearer_super, "Beta", "beto@ejemplo.com")
    guid_rita = usuarios.para_login("rita@ejemplo.com")["guid"]
    P = {}
    for rol in ("ANALISTA", "VIEWER", "AUDITOR"):
        correo = f"{rol.lower()}@ejemplo.com"
        r = c.post("/usuarios/", json={"nombre": rol.title(), "apellidoPaterno": "Prueba", "email": correo, "rol": rol}, headers=cab(adm))
        assert r.status_code == 201, r.text
        P[rol] = con_primer_acceso(correo, r.json()["contrasenaTemporal"], "Cielo-Abierto-Verde-52!")

    CAMPOS_INE = [
        campo_def("Nombre"),
        campo_def("CURP"),
        campo_def("Año de registro", "numero"),
        campo_def("Fecha de nacimiento", "fecha"),
        campo_def("Teléfonos", "texto", cardinalidad="multiple"),
        campo_def("Socios", "grupo", cardinalidad="multiple", subcampos=[sub_def("Nombre"), sub_def("Acciones", "numero")]),
        campo_def("Domicilio", "grupo", subcampos=[sub_def("Calle")]),
    ]
    td_ine = tipo_documental(adm, org, "INE", CAMPOS_INE)
    td_comp = tipo_documental(adm, org, "Comprobante de domicilio", [campo_def("Titular"), campo_def("Monto", "moneda")])
    td_otro = tipo_documental(adm, org, "Estado de cuenta", [campo_def("Saldo", "moneda")])
    td_b = tipo_documental(adm_b, otra, "INE de Beta", [campo_def("Nombre")])
    credito = tipo_virtual(adm, org, "Solicitud de crédito", [td_ine, td_comp, td_otro])
    seguro = tipo_virtual(adm, org, "Póliza de seguro", [td_ine])
    tipo_b = tipo_virtual(adm_b, otra, "Solicitud de Beta", [td_b])

    juan = expediente(adm, org, "Juan Pérez")
    ine1, ine2, comp, cuenta1, cuenta2, suelto = (subir(adm, org, f"{n}.pdf", i) for i, n in enumerate(("ine", "ine-2", "domicilio", "cuenta", "cuenta-2", "suelto")))
    for d in (ine1, ine2, comp, cuenta1, cuenta2):
        meter(adm, org, juan, d)
    v1 = crear_v(adm, org, juan, credito, "Crédito automotriz", [ine1, comp, cuenta1])
    # Se crean ANTES de clasificar nada: un virtual nuevo se llena con lo que su expediente ya tiene clasificado, y estos tienen que quedar como se arman.
    sin_extraer = subir(adm, org, "sin-extraer.pdf", 200)
    meter(adm, org, juan, sin_extraer)
    v_solo = crear_v(adm, org, juan, credito, "Con un comprobante solo clasificado", [ine1, sin_extraer])
    v_vacio = crear_v(adm, org, juan, credito, "Sin comprobante", [ine1])
    exp_b = expediente(adm_b, otra, "De Beta")
    d_b = subir(adm_b, otra, "beta.pdf", 90)
    meter(adm_b, otra, exp_b, d_b)
    v_b = crear_v(adm_b, otra, exp_b, tipo_b, "Virtual de Beta", [d_b])

    # Lo que Document AI dejó de cada documento.
    for d, td in ((ine1, td_ine), (ine2, td_ine), (comp, td_comp), (cuenta1, td_otro), (cuenta2, td_otro)):
        clasificar(org, d, td)
    extraer(org, ine1, td_ine, {
        "nombre": hecho("Juan Pérez", 95, 1),
        "curp": hecho("PEPJ800101HDFRRN09", 80, 1),
        "ano_de_registro": hecho("2015", 70, 2),
        "fecha_de_nacimiento": hecho("1980-01-01", 99, 1),
        "telefonos": [hecho("5511", 90, 1), hecho("5522", 60, 2)],
        "socios": [{"nombre": hecho("Ana", 90, 3), "acciones": hecho("50", 90, 3)}, {"nombre": hecho("Luis", 85, 3), "acciones": hecho("50", 85, 3)}],
        "domicilio": {"calle": hecho("Reforma 1", 88, 1)},
    })
    extraer(org, comp, td_comp, {"titular": hecho("Juan Pérez", 92, 1), "monto": hecho("$1,250.50", 81, 1)})

    # El proceso de la prueba: «el CURP del INE es el que escribió quien capturó»; entrega dos valores finales y termina en una palabra.
    def proceso_base(**extra) -> dict:
        return {
            "nombre": "Validar identidad", "descripcion": "", "tipoExpedienteVirtualId": credito,
            "nodos": [n_tipo(td_ine), n_tipo(td_comp), n_op(1, "igual"), n_fijo(1, "texto", "PEPJ800101HDFRRN09"), n_vf(1, "CURP"), n_vf(2, "Teléfonos"), n_res()],
            "aristas": [
                cable(f"tipo-{td_ine}", "CURP", "operador-1", "a"), cable("constante-1", "valor", "operador-1", "b"),
                cable(f"tipo-{td_ine}", "CURP", "valor-final-1", "a"), cable(f"tipo-{td_ine}", "Teléfonos", "valor-final-2", "a"),
                cable("operador-1", "resultado", "resultado-final", "a"),
            ],
            **extra,
        }

    pid = "proceso-" + uuid.uuid4().hex
    r = guardar_proceso(adm, org, pid, proceso_base(), 0)
    assert r.status_code == 200, r.text
    rev_p = r.json()["revision"]
    rev("hay un expediente con su virtual (3 documentos), un proceso guardado y los documentos con su tipo y sus datos", ver_v(adm, org, juan, v1)["totalDocumentos"] == 3 and rev_p == 1)

    titulo("1 · Preparar: el proceso, sus tipos y los valores REALES por el nombre de la biblioteca")
    r = preparar(adm, org, juan, v1, pid)
    rev("preparar: 200", r.status_code == 200, r.text[:300])
    prep = r.json()
    rev("trae el expediente, el virtual con su tipo y el proceso con su revisión y su dibujo entero",
        prep["expediente"] == {"id": juan, "nombre": "Juan Pérez"} and prep["virtual"]["id"] == v1 and prep["virtual"]["tipo"] == {"id": credito, "nombre": "Solicitud de crédito"}
        and prep["proceso"]["id"] == pid and prep["proceso"]["revision"] == 1 and prep["proceso"]["nombre"] == "Validar identidad"
        and {n["id"] for n in prep["proceso"]["datos"]["nodos"]} >= {"operador-1", "resultado-final", "valor-final-1", "valor-final-2"}, str(prep)[:300])
    rev("trae los tipos documentales que el proceso usa, con sus campos tal como están en la biblioteca", {t["id"] for t in prep["tipos"]} == {td_ine, td_comp} and next(t for t in prep["tipos"] if t["id"] == td_ine)["campos"][2]["nombre"] == "Año de registro")
    docs = {d["tipoId"]: d for d in prep["documentos"]}
    rev("por cada tipo, los documentos del virtual que lo cubren (uno) y de qué se supo su tipo", docs[td_ine]["encontrados"] == [{"id": ine1, "nombre": "ine.pdf", "origenTipo": "extraccion"}] and docs[td_comp]["encontrados"][0]["id"] == comp)
    rev("sin duplicados", prep["duplicados"] == [])
    v = docs[td_ine]["valores"]
    rev("un campo suelto: su valor, su confianza y su página", v["Nombre"] == {"forma": "valor", "valores": ["Juan Pérez"], "confianza": 95.0, "pagina": 1}, str(v.get("Nombre")))
    rev("el campo con ñ y mayúsculas («Año de registro») se encuentra aunque Google lo guarde como ano_de_registro", v["Año de registro"]["valores"] == ["2015"] and v["Año de registro"]["pagina"] == 2 and v["Año de registro"]["confianza"] == 70.0, str(v.get("Año de registro")))
    rev("«Fecha de nacimiento» y «CURP», por su nombre de la biblioteca", v["Fecha de nacimiento"]["valores"] == ["1980-01-01"] and v["CURP"]["valores"] == ["PEPJ800101HDFRRN09"])
    rev("un campo «1 a N» es una LISTA en el orden del documento, con la confianza MÁS BAJA", v["Teléfonos"] == {"forma": "lista", "valores": ["5511", "5522"], "confianza": 60.0, "pagina": 1}, str(v.get("Teléfonos")))
    rev("un grupo que se repite entrega sus FILAS (cuántas son)", v["Socios"]["forma"] == "filas" and v["Socios"]["valores"] == ["1", "2"], str(v.get("Socios")))
    rev("y cada columna del grupo, como lista: «Socios.Nombre» y «Socios.Acciones»", v["Socios.Nombre"]["valores"] == ["Ana", "Luis"] and v["Socios.Acciones"]["valores"] == ["50", "50"] and v["Socios.Nombre"]["confianza"] == 85.0, str(v.get("Socios.Nombre")))
    rev("una columna de un grupo que NO se repite es un valor: «Domicilio.Calle»; y ese grupo no entrega nada por sí mismo", v["Domicilio.Calle"] == {"forma": "valor", "valores": ["Reforma 1"], "confianza": 88.0, "pagina": 1} and "Domicilio" not in v)
    rev("el otro tipo trae sus valores (el monto como lo escribió el documento)", docs[td_comp]["valores"]["Monto"]["valores"] == ["$1,250.50"] and docs[td_comp]["valores"]["Titular"]["valores"] == ["Juan Pérez"])

    # Un tipo documental que ya no está en la biblioteca, o puesto dos veces en el dibujo, no estorba: se prepara con los que sí hay, cada uno UNA vez.
    pid_raro = "proceso-" + uuid.uuid4().hex
    raro = proceso_base()
    raro["nodos"] = raro["nodos"] + [n_tipo("tipo-" + uuid.uuid4().hex), {**n_tipo(td_ine), "id": "tipo-repetido"}]
    assert guardar_proceso(adm, org, pid_raro, raro, 0).status_code == 200
    r = preparar(adm, org, juan, v1, pid_raro)
    rev("un tipo documental que ya no existe en la biblioteca no estorba: se prepara con los que sí hay", r.status_code == 200 and {t["id"] for t in r.json()["tipos"]} == {td_ine, td_comp} and {d["tipoId"] for d in r.json()["documentos"]} == {td_ine, td_comp}, r.text[:300])
    rev("y un tipo puesto dos veces en el dibujo sale UNA sola vez, en `tipos` y en `documentos`", len(r.json()["tipos"]) == 2 and len(r.json()["documentos"]) == 2, r.text[:300])
    assert c.delete(f"/procesos/{pid_raro}", params={"tenant": org}, headers=cab(adm)).status_code in (200, 204)

    # Una fila de la que SOLO se sabe que le faltó un dato (Document AI la devolvió, pero sin ese campo) también es una fila del grupo.
    solo_ausente = {
        "socios": [{"nombre": hecho("Ana", 90, 3), "acciones": hecho("50", 90, 3)}, {"nombre": hecho("Luis", 85, 3), "acciones": hecho("50", 85, 3)}],
        "_metadata": {"campos_ausentes": ["socios[3].nombre"]},
    }
    hechos_ausentes = [
        {"ruta": f["fieldPath"], "registro": f["recordIndex"], "valor": f["valueNormalized"], "confianza": f["confidence"], "pagina": f["pageNumber"]}
        for f in datos_extraidos.aplanar(solo_ausente)
    ]
    from servicios import ejecuciones as ejecuciones_servicio

    vals_ausentes = ejecuciones_servicio._valores_de(CAMPOS_INE, hechos_ausentes)
    rev("una fila de la que solo se sabe que le faltó un dato cuenta como fila del grupo (3), y sus columnas solo traen lo que sí vino", vals_ausentes["Socios"]["valores"] == ["1", "2", "3"] and vals_ausentes["Socios.Nombre"]["valores"] == ["Ana", "Luis"], str(vals_ausentes.get("Socios")))

    titulo("2 · Los nombres: lo que el lienzo llama de una manera y Google de otra")
    rev("`nombres_en_google`: «Año de registro» → ano_de_registro; un subcampo lleva el grupo; el grupo, su propio nombre", esquema.nombres_en_google(CAMPOS_INE) == {
        "Nombre": (None, "nombre"), "CURP": (None, "curp"), "Año de registro": (None, "ano_de_registro"), "Fecha de nacimiento": (None, "fecha_de_nacimiento"),
        "Teléfonos": (None, "telefonos"), "Socios": (None, "socios"), "Socios.Nombre": ("socios", "nombre"), "Socios.Acciones": ("socios", "acciones"),
        "Domicilio": (None, "domicilio"), "Domicilio.Calle": ("domicilio", "calle")}, str(esquema.nombres_en_google(CAMPOS_INE)))
    rev("dos campos que colapsan al mismo nombre («Año» y «ano») caen en los mismos sufijos que en el esquema que se le mandó a Google",
        esquema.nombres_en_google([campo_def("Año"), campo_def("ano"), campo_def("ANO")]) == {"Año": (None, "ano"), "ano": (None, "ano_2"), "ANO": (None, "ano_3")})
    rev("un grupo sin subcampos usables no se declara en el esquema, y aquí tampoco aparece", esquema.nombres_en_google([campo_def("Vacío", "grupo")]) == {})
    rev("`nombres_en_google` y `esquema_desde_campos` concuerdan: los nombres que declara el esquema son los mismos",
        {p["name"] for p in esquema.esquema_desde_campos("INE", "", CAMPOS_INE)["entityTypes"][0]["properties"]} == {n for (g, n) in esquema.nombres_en_google(CAMPOS_INE).values() if g is None})

    titulo("3 · Lo que falta o cambia: ausentes, correcciones, solo clasificado, sin documento")
    extraer(org, ine1, td_ine, {"nombre": hecho("Juan Pérez", 95, 1), "curp": hecho("PEPJ800101HDFRRN09", 80, 1), "_metadata": {"campos_ausentes": ["ano_de_registro"]}})
    v = {d["tipoId"]: d for d in preparar(adm, org, juan, v1, pid).json()["documentos"]}[td_ine]["valores"]
    rev("una reextracción manda (la última corrida): lo que ya no trae queda sin valores y las listas, vacías", v["Año de registro"]["valores"] == [] and v["Teléfonos"]["valores"] == [] and v["Socios"]["valores"] == [] and v["Nombre"]["valores"] == ["Juan Pérez"], str(v))
    datos_extraidos.corregir(org, ine1, "curp", "CORREGIDO", "lo escribió mal", "prueba")
    v = {d["tipoId"]: d for d in preparar(adm, org, juan, v1, pid).json()["documentos"]}[td_ine]["valores"]
    rev("una corrección a mano es el valor vigente", v["CURP"]["valores"] == ["CORREGIDO"], str(v.get("CURP")))
    # Se devuelve la extracción completa para el resto de la prueba.
    extraer(org, ine1, td_ine, {
        "nombre": hecho("Juan Pérez", 95, 1), "curp": hecho("PEPJ800101HDFRRN09", 80, 1), "ano_de_registro": hecho("2015", 70, 2),
        "telefonos": [hecho("5511", 90, 1), hecho("5522", 60, 2)], "socios": [{"nombre": hecho("Ana", 90, 3)}], "domicilio": {"calle": hecho("Reforma 1", 88, 1)},
    })
    clasificar(org, sin_extraer, td_comp)
    prep_solo = preparar(adm, org, juan, v_solo, pid).json()
    d_comp = {d["tipoId"]: d for d in prep_solo["documentos"]}[td_comp]
    rev("un documento solo CLASIFICADO cubre su tipo pero no trae valores (y dice que su tipo salió de la clasificación)", d_comp["encontrados"] == [{"id": sin_extraer, "nombre": "sin-extraer.pdf", "origenTipo": "clasificacion"}] and d_comp["valores"]["Monto"]["valores"] == [] and d_comp["valores"]["Titular"]["valores"] == [], str(d_comp))
    d_comp = {d["tipoId"]: d for d in preparar(adm, org, juan, v_vacio, pid).json()["documentos"]}[td_comp]
    rev("un tipo que el virtual no cubre queda sin documentos y sin valores", d_comp["encontrados"] == [] and d_comp["valores"] is None)

    titulo("4 · DUPLICADOS: dos documentos del mismo tipo")
    rev("antes de duplicar, ni el virtual ni el expediente marcan nada", ver_v(adm, org, juan, v1)["duplicados"] == [] and all(not d["duplicado"] for d in ver_v(adm, org, juan, v1)["documentos"])
        and all(d["duplicadoEn"] == [] for d in ver_exp(adm, org, juan)["documentos"]))
    rev("meter el segundo INE al virtual", meter_v(adm, org, juan, v1, ine2).status_code == 200)
    prep = preparar(adm, org, juan, v1, pid).json()
    rev("preparar los entrega en `duplicados`, con los dos documentos, y NO da valores de ese tipo", prep["duplicados"] == [{"tipoId": td_ine, "tipoNombre": "INE", "documentos": [{"id": ine1, "nombre": "ine.pdf"}, {"id": ine2, "nombre": "ine-2.pdf"}]}]
        and {d["tipoId"]: d for d in prep["documentos"]}[td_ine]["valores"] is None and len({d["tipoId"]: d for d in prep["documentos"]}[td_ine]["encontrados"]) == 2, str(prep["duplicados"]))
    rev("el otro tipo sigue dando sus valores", {d["tipoId"]: d for d in prep["documentos"]}[td_comp]["valores"]["Titular"]["valores"] == ["Juan Pérez"])
    vv = ver_v(adm, org, juan, v1)
    rev("el expediente virtual marca los dos documentos (`duplicado`) y lista el duplicado por tipo", {d["id"]: d["duplicado"] for d in vv["documentos"]} == {ine1: True, comp: False, cuenta1: False, ine2: True}
        and vv["duplicados"] == [{"tipoId": td_ine, "tipoNombre": "INE", "documentos": [{"id": ine1, "nombre": "ine.pdf"}, {"id": ine2, "nombre": "ine-2.pdf"}]}], str(vv["duplicados"]))
    ee = ver_exp(adm, org, juan)
    marcas = {d["id"]: d["duplicadoEn"] for d in ee["documentos"]}
    rev("el expediente marca los documentos con el virtual en el que están duplicados (`duplicadoEn`)", marcas[ine1] == [{"id": v1, "nombre": "Crédito automotriz", "tipoNombre": "INE"}] and marcas[ine2] == marcas[ine1] and marcas[comp] == [], str(marcas))
    rev("y cada virtual del expediente dice sus duplicados", {x["id"]: x["duplicados"] for x in ee["expedientesVirtuales"]}[v1][0]["tipoId"] == td_ine and {x["id"]: x["duplicados"] for x in ee["expedientesVirtuales"]}[v_vacio] == [])
    rev("el INE en OTRO virtual donde está solo no se marca (un duplicado es de un virtual)", ver_v(adm, org, juan, v_vacio)["duplicados"] == [] and not any(d["duplicado"] for d in ver_v(adm, org, juan, v_vacio)["documentos"]))
    # un tipo que el proceso NO usa, duplicado: no estorba
    meter_v(adm, org, juan, v1, cuenta2)
    prep = preparar(adm, org, juan, v1, pid).json()
    rev("dos estados de cuenta (un tipo que el proceso NO usa) también se marcan en el virtual, pero no estorban a `preparar`: solo se entregan los duplicados de los tipos que el proceso usa",
        [g["tipoId"] for g in prep["duplicados"]] == [td_ine] and any(g["tipoId"] == td_otro for g in ver_v(adm, org, juan, v1)["duplicados"]))
    rev("sacar uno de los INE quita la marca (se calcula al leer, no se guarda)", sacar_v(adm, org, juan, v1, ine2).status_code == 200 and [g["tipoId"] for g in preparar(adm, org, juan, v1, pid).json()["duplicados"]] == []
        and not ver_v(adm, org, juan, v1)["documentos"][0]["duplicado"])
    sacar_v(adm, org, juan, v1, cuenta2)

    titulo("5 · Lo que impide ejecutar, cada cosa con su código")
    def codigo(respuesta) -> str | None:
        return (respuesta.json().get("detail") or {}).get("codigo") if respuesta.status_code in (404, 409) else None

    p_inactivo = "proceso-" + uuid.uuid4().hex
    guardar_proceso(adm, org, p_inactivo, proceso_base(activo=False, nombre="Apagado"), 0)
    r = preparar(adm, org, juan, v1, p_inactivo)
    rev("un proceso desactivado: 409 `proceso_inactivo`", r.status_code == 409 and codigo(r) == "proceso_inactivo" and "desactivado" in r.json()["detail"]["mensaje"], r.text[:200])
    p_otro_tipo = "proceso-" + uuid.uuid4().hex
    guardar_proceso(adm, org, p_otro_tipo, proceso_base(tipoExpedienteVirtualId=seguro, nombre="De seguros"), 0)
    r = preparar(adm, org, juan, v1, p_otro_tipo)
    rev("un proceso que corre sobre OTRO tipo de expediente virtual: 409 `no_corre_aqui`, y dice cuál es el tipo", r.status_code == 409 and codigo(r) == "no_corre_aqui" and "Solicitud de crédito" in r.json()["detail"]["mensaje"], r.text[:200])
    sin_res = proceso_base(nombre="Sin resultado")
    sin_res["nodos"] = [n for n in sin_res["nodos"] if n["type"] != "resultado-final"]
    sin_res["aristas"] = [a for a in sin_res["aristas"] if a["target"] != "resultado-final"]
    p_sin = "proceso-" + uuid.uuid4().hex
    guardar_proceso(adm, org, p_sin, sin_res, 0)
    r = preparar(adm, org, juan, v1, p_sin)
    rev("un proceso sin Resultado (los de antes de que fuera obligatorio): 409 `sin_resultado`", r.status_code == 409 and codigo(r) == "sin_resultado", r.text[:200])
    res_suelto = proceso_base(nombre="Resultado suelto")
    res_suelto["aristas"] = [a for a in res_suelto["aristas"] if a["target"] != "resultado-final"]
    p_suelto = "proceso-" + uuid.uuid4().hex
    guardar_proceso(adm, org, p_suelto, res_suelto, 0)
    r = preparar(adm, org, juan, v1, p_suelto)
    rev("un Resultado sin ninguna regla conectada: 409 `resultado_sin_regla`", r.status_code == 409 and codigo(r) == "resultado_sin_regla", r.text[:200])
    res_campo = proceso_base(nombre="Resultado de un campo")
    res_campo["aristas"] = [a for a in res_campo["aristas"] if a["target"] != "resultado-final"] + [cable(f"tipo-{td_ine}", "CURP", "resultado-final", "a")]
    p_campo = "proceso-" + uuid.uuid4().hex
    guardar_proceso(adm, org, p_campo, res_campo, 0)
    rev("un Resultado cableado a algo que no es una regla (un campo, a mano) tampoco cuenta como conectado", codigo(preparar(adm, org, juan, v1, p_campo)) == "resultado_sin_regla")
    rev("un proceso que no existe: 404 `proceso_no_existe`", codigo(preparar(adm, org, juan, v1, "proceso-" + uuid.uuid4().hex)) == "proceso_no_existe")
    rev("un expediente virtual que no existe: 404 `virtual_no_existe`", codigo(preparar(adm, org, juan, "expediente-virtual-" + uuid.uuid4().hex, pid)) == "virtual_no_existe")
    rev("un virtual que no es de ese expediente: 404", codigo(preparar(adm, org, exp_b, v1, pid)) == "virtual_no_existe")
    rev("ids que ni parecen ids: 404, no un error del servidor", codigo(preparar(adm, org, juan, "x", pid)) == "virtual_no_existe" and codigo(preparar(adm, org, juan, v1, "x")) == "proceso_no_existe")

    titulo("6 · Roles y organizaciones")
    rev("quien organiza (Analista) prepara", preparar(P["ANALISTA"], org, juan, v1, pid).status_code == 200)
    r = preparar(P["VIEWER"], org, juan, v1, pid)
    rev("un rol que solo lee no ejecuta: 403 `sin_permiso`", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso", r.text[:200])
    rev("el Auditor tampoco", preparar(P["AUDITOR"], org, juan, v1, pid).status_code == 403)
    rev("sin sesión: 401", c.get("/ejecuciones/preparar", params={"tenant": org, "expediente": juan, "virtual": v1, "proceso": pid}, headers=cab()).status_code == 401)
    r = preparar(adm_b, org, juan, v1, pid)
    rev("quien es de OTRA organización, con el tenant ajeno: 403 `sin_acceso`", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso", r.text[:200])
    rev("con SU tenant pero ids ajenos: 404, igual que si no existieran", codigo(preparar(adm_b, otra, juan, v1, pid)) == "virtual_no_existe" and codigo(preparar(adm_b, otra, exp_b, v_b, pid)) == "proceso_no_existe")

    titulo("7 · Registrar una ejecución")
    prep = preparar(adm, org, juan, v1, pid).json()

    def cuerpo(**cambios) -> dict:
        base = {
            "tenant": org, "expedienteId": juan, "virtualId": v1, "procesoId": pid, "procesoRevision": prep["proceso"]["revision"],
            "resultado": {"caso": "si", "texto": "Certificado"},
            "valoresFinales": [
                {"nodoId": "valor-final-1", "nombre": "CURP", "forma": "valor", "familia": "texto", "estado": "listo", "valor": "PEPJ800101HDFRRN09", "lista": None, "origen": "CURP · INE"},
                {"nodoId": "valor-final-2", "nombre": "Teléfonos", "forma": "lista", "familia": "texto", "estado": "listo", "valor": "5511, 5522", "lista": ["5511", "5522"], "origen": "Teléfonos · INE"},
            ],
            "detalle": {"reglas": [{"id": "operador-1", "etiqueta": "Igual a #1", "estado": "pasa"}], "evidencia": [{"documentoId": ine1, "campo": "CURP", "valor": "PEPJ800101HDFRRN09", "confianza": 80}]},
        }
        base.update(cambios)
        return base

    antes = auditoria("PROCESS_RUN_RECORDED")
    r = registrar(adm, cuerpo())
    rev("guardar: 201", r.status_code == 201, r.text[:300])
    e1 = r.json()
    rev("su id lo pone el servidor (`ejecucion-` + 32 hexadecimales)", e1["id"].startswith("ejecucion-") and len(e1["id"]) == 42, e1["id"])
    rev("trae quién (con su id), cuándo, sobre qué y con qué proceso, con los NOMBRES que la base tiene",
        e1["ejecutadaPor"]["id"] == guid_rita and e1["ejecutadaEn"] and e1["expediente"] == {"id": juan, "nombre": "Juan Pérez"} and e1["virtual"] == {"id": v1, "nombre": "Crédito automotriz", "tipoNombre": "Solicitud de crédito"}
        and e1["proceso"] == {"id": pid, "nombre": "Validar identidad", "revision": 1}, str(e1)[:300])
    rev("el resultado es la palabra y su caso; con el caso `si` queda `completa`", e1["resultado"] == {"caso": "si", "texto": "Certificado"} and e1["estado"] == "completa")
    rev("la lista de valores finales vuelve en el orden del proceso, completa", [(f["nodoId"], f["nombre"], f["forma"], f["valor"], f["lista"]) for f in e1["valoresFinales"]] == [("valor-final-1", "CURP", "valor", "PEPJ800101HDFRRN09", None), ("valor-final-2", "Teléfonos", "lista", "5511, 5522", ["5511", "5522"])])
    rev("trae la foto de cómo salió cada regla y el proceso tal como estaba", e1["detalle"]["reglas"][0]["estado"] == "pasa" and e1["definicion"]["nombre"] == "Validar identidad" and len(e1["definicion"]["nodos"]) == len(prep["proceso"]["datos"]["nodos"]))
    rev("deja rastro `PROCESS_RUN_RECORDED` con el proceso, el virtual y cómo terminó", len(auditoria("PROCESS_RUN_RECORDED")) == len(antes) + 1 and auditoria("PROCESS_RUN_RECORDED")[-1].get("ejecucionId") == e1["id"] and auditoria("PROCESS_RUN_RECORDED")[-1].get("caso") == "si" and auditoria("PROCESS_RUN_RECORDED")[-1].get("resultado") == "Certificado")
    # Las otras dos palabras
    e_no = registrar(adm, cuerpo(resultado={"caso": "no", "texto": "Rechazado"})).json()
    e_sin = registrar(adm, cuerpo(resultado={"caso": "sin", "texto": "Pendiente"})).json()
    rev("el caso `no` es completa; el caso `sin` (faltaron datos) es INCOMPLETA", e_no["estado"] == "completa" and e_no["resultado"]["texto"] == "Rechazado" and e_sin["estado"] == "incompleta" and e_sin["resultado"]["caso"] == "sin")
    sin_valor = cuerpo(valoresFinales=[
        {"nodoId": "valor-final-1", "nombre": "CURP", "forma": "valor", "familia": "texto", "estado": "sin_valores", "valor": None, "lista": None, "origen": "CURP · INE"},
        {"nodoId": "valor-final-2", "nombre": "Teléfonos", "forma": "veredicto", "familia": None, "estado": "listo", "valor": "Pasa", "lista": None, "origen": "Igual a #1"},
    ])
    rev("un valor final sin valor (faltó el dato) y uno que es el pasa / no pasa de una regla se guardan", registrar(adm, sin_valor).status_code == 201)
    rev("la analista también ejecuta", registrar(P["ANALISTA"], cuerpo()).status_code == 201)

    # Un booleano no vale como revisión del proceso (`True == 1`): por HTTP lo vuelve 1 Pydantic antes de llegar, así que se prueba llamando al servicio.
    try:
        ejecuciones_servicio.registrar(org, guid_rita, {k: v for k, v in cuerpo(procesoRevision=True).items() if k != "tenant"})
        revision_bool = "se guardó"
    except ejecuciones_servicio.EjecucionInvalida:
        revision_bool = "rechazada"
    rev("un booleano no vale como revisión del proceso (aunque `True == 1`): el servicio la rechaza", revision_bool == "rechazada", revision_bool)

    titulo("8 · Lo que NO se guarda")
    def rechazada(que: str, cambios: dict, texto: str | None = None, codigo_http: int = 400) -> None:
        r = registrar(adm, cuerpo(**cambios))
        rev(f"{que}: {codigo_http}", r.status_code == codigo_http and (texto is None or texto in r.text), f"{r.status_code} {r.text[:200]}")

    n_antes = historial(adm, org).json()["total"]
    rechazada("una palabra que no es la que el proceso tiene para ese caso", {"resultado": {"caso": "si", "texto": "Aprobado"}}, "no es la que el proceso")
    rechazada("la palabra de otro caso", {"resultado": {"caso": "si", "texto": "Rechazado"}})
    rechazada("un caso que no existe", {"resultado": {"caso": "quizá", "texto": "Certificado"}}, "El resultado no es válido")
    rechazada("sin palabra", {"resultado": {"caso": "si", "texto": "  "}})
    rechazada("una palabra de más de 60 caracteres", {"resultado": {"caso": "si", "texto": "x" * 61}})
    rechazada("un resultado que no es un objeto", {"resultado": "Certificado"}, codigo_http=422)
    rechazada("un valor final que no es de este proceso", {"valoresFinales": [{"nodoId": "valor-final-9", "nombre": "X", "forma": "valor", "familia": None, "estado": "sin_valores", "valor": None, "lista": None, "origen": None}]}, "no es de este proceso")
    rechazada("el mismo valor final dos veces", {"valoresFinales": [cuerpo()["valoresFinales"][0], cuerpo()["valoresFinales"][0]]}, "repetido")
    rechazada("un valor final sin nombre", {"valoresFinales": [{**cuerpo()["valoresFinales"][0], "nombre": "   "}]})
    rechazada("una forma que no existe", {"valoresFinales": [{**cuerpo()["valoresFinales"][0], "forma": "mapa"}]})
    rechazada("una clase de dato que no existe", {"valoresFinales": [{**cuerpo()["valoresFinales"][0], "familia": "color"}]})
    rechazada("un estado que no existe", {"valoresFinales": [{**cuerpo()["valoresFinales"][0], "estado": "casi"}]})
    rechazada("«listo» sin valor", {"valoresFinales": [{**cuerpo()["valoresFinales"][0], "valor": None}]})
    rechazada("con valor pero no «listo»", {"valoresFinales": [{**cuerpo()["valoresFinales"][0], "estado": "sin_valores"}]})
    rechazada("una lista en algo que no es lista", {"valoresFinales": [{**cuerpo()["valoresFinales"][0], "lista": ["a"]}]})
    rechazada("una lista «lista» y «listo» sin sus elementos", {"valoresFinales": [{**cuerpo()["valoresFinales"][1], "lista": None}]})
    rechazada("una lista con elementos que no son texto", {"valoresFinales": [{**cuerpo()["valoresFinales"][1], "lista": [1, 2]}]})
    rechazada("una lista de más de mil elementos", {"valoresFinales": [{**cuerpo()["valoresFinales"][1], "lista": ["a"] * 1001}]})
    rechazada("una lista que dice no tener valor", {"valoresFinales": [{**cuerpo()["valoresFinales"][1], "estado": "sin_valores", "valor": None}]}, "sin valor no trae lista")
    rechazada("más de 300 valores finales", {"valoresFinales": [cuerpo()["valoresFinales"][0]] * 301}, "Los valores finales no son válidos")
    rechazada("los valores finales que no son una lista", {"valoresFinales": {}}, codigo_http=422)
    rechazada("un detalle que no es un objeto", {"detalle": []}, codigo_http=422)
    rechazada("un detalle de medio megabyte", {"detalle": {"x": "y" * 500_000}}, "demasiado grande")
    rechazada("una revisión que no es número", {"procesoRevision": "uno"}, codigo_http=422)
    rechazada("una revisión de cero", {"procesoRevision": 0}, codigo_http=422)
    r = registrar(adm, cuerpo(virtualId="expediente-virtual-" + uuid.uuid4().hex))
    rev("un virtual que no existe: 404", r.status_code == 404 and r.json()["detail"]["codigo"] == "virtual_no_existe")
    r = registrar(adm, cuerpo(procesoId="proceso-" + uuid.uuid4().hex))
    rev("un proceso que no existe: 404", r.status_code == 404 and r.json()["detail"]["codigo"] == "proceso_no_existe")
    r = registrar(adm, cuerpo(procesoId=p_otro_tipo, procesoRevision=1))
    rev("un proceso que corre sobre OTRO tipo de virtual: 400", r.status_code == 400 and "no corre sobre" in r.text, r.text[:200])
    rev("nada de lo rechazado quedó en el historial", historial(adm, org).json()["total"] == n_antes)
    r = registrar(P["VIEWER"], cuerpo())
    rev("un rol que solo lee no guarda: 403 `sin_permiso`", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso")
    r = registrar(adm_b, cuerpo())
    rev("otra organización con el tenant ajeno: 403 `sin_acceso`", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso")
    r = registrar(adm_b, cuerpo(tenant=otra))
    rev("con su tenant pero ids ajenos: 404", r.status_code == 404)
    rev("y con todo eso, el historial de la otra organización sigue vacío", historial(adm_b, otra).json() == {"ejecuciones": [], "total": 0, "hayMas": False})

    titulo("9 · El proceso cambió mientras se ejecutaba")
    r = guardar_proceso(adm, org, pid, proceso_base(descripcion="otra versión"), rev_p)
    assert r.status_code == 200, r.text
    r = registrar(adm, cuerpo())
    rev("la revisión que se preparó ya no es la vigente: 409 `proceso_cambio`, con la revisión actual", r.status_code == 409 and r.json()["detail"]["codigo"] == "proceso_cambio" and r.json()["detail"]["revisionActual"] == 2, r.text[:200])
    prep = preparar(adm, org, juan, v1, pid).json()
    rev("preparar de nuevo trae la revisión 2", prep["proceso"]["revision"] == 2)
    e2 = registrar(adm, cuerpo(procesoRevision=2))
    rev("con la revisión vigente se guarda, y la ejecución dice que corrió la 2", e2.status_code == 201 and e2.json()["proceso"]["revision"] == 2 and e2.json()["definicion"]["descripcion"] == "otra versión", e2.text[:200])
    e2 = e2.json()

    titulo("10 · El historial: orden, filtros, páginas, abrir una")
    lista = historial(adm, org).json()
    ids_h = [e["id"] for e in lista["ejecuciones"]]
    rev("de la más reciente a la más antigua, con su total", ids_h[0] == e2["id"] and ids_h[-1] == e1["id"] and lista["total"] == len(ids_h) == 6 and lista["hayMas"] is False, str(ids_h))
    rev("cada renglón trae lo de la tabla: cuándo, expediente, virtual, proceso, resultado y estado, y quién (con su nombre)", (lista["ejecuciones"][0]["ejecutadaPor"]["nombre"] or "").startswith("Admin") and lista["ejecuciones"][0]["resultado"]["texto"] == "Certificado" and lista["ejecuciones"][0]["estado"] == "completa", str(lista["ejecuciones"][0]))
    rev("un renglón del historial NO trae la foto pesada (ni el dibujo ni el detalle)", "definicion" not in lista["ejecuciones"][0] and "detalle" not in lista["ejecuciones"][0] and "valoresFinales" not in lista["ejecuciones"][0])
    rev("filtra por expediente, por virtual y por proceso", historial(adm, org, expediente=juan).json()["total"] == 6 and historial(adm, org, virtual=v1).json()["total"] == 6 and historial(adm, org, proceso=pid).json()["total"] == 6 and historial(adm, org, virtual="expediente-virtual-" + uuid.uuid4().hex).json()["total"] == 0 and historial(adm, org, proceso=p_inactivo).json()["total"] == 0)
    pagina1 = historial(adm, org, limite=2).json()
    rev("pagina: `limite` y `hayMas`, con el total de todas", len(pagina1["ejecuciones"]) == 2 and pagina1["hayMas"] is True and pagina1["total"] == 6)
    pagina2 = historial(adm, org, limite=2, antes=pagina1["ejecuciones"][-1]["id"]).json()
    pagina3 = historial(adm, org, limite=2, antes=pagina2["ejecuciones"][-1]["id"]).json()
    rev("`antes` pide las siguientes sin repetir ni saltar, hasta el final", [e["id"] for e in pagina1["ejecuciones"] + pagina2["ejecuciones"] + pagina3["ejecuciones"]] == ids_h and pagina3["hayMas"] is False)
    rev("un `limite` fuera de rango se rechaza", historial(adm, org, limite=0).status_code == 422 and historial(adm, org, limite=201).status_code == 422)
    rev("lo ven todos los roles: Viewer y Auditor leen el historial", historial(P["VIEWER"], org).json()["total"] == 6 and historial(P["AUDITOR"], org).json()["total"] == 6)
    r = abrir(adm, org, e1["id"])
    rev("abrir una: la misma que se guardó", r.status_code == 200 and r.json() == e1, r.text[:200])
    rev("abrir una de otra organización o que no existe: 404 igual", abrir(adm_b, otra, e1["id"]).status_code == 404 and abrir(adm, org, "ejecucion-" + uuid.uuid4().hex).status_code == 404 and abrir(adm, org, "x").status_code == 404)
    rev("la otra organización con el tenant ajeno: 403", abrir(adm_b, org, e1["id"]).status_code == 403)
    rev("el Auditor puede abrirla", abrir(P["AUDITOR"], org, e1["id"]).status_code == 200)

    titulo("11 · Es una FOTO: no cambia aunque cambie todo lo demás")
    guardar_proceso(adm, org, pid, proceso_base(nombre="Validar identidad (renombrado)"), 2)
    r = c.delete(f"/expedientes/{juan}/virtuales/{v_vacio}", params={"tenant": org}, headers=cab(adm))
    r2 = c.delete(f"/expedientes/{juan}/virtuales/{v1}", params={"tenant": org}, headers=cab(adm))
    rev("se borra el virtual sobre el que se corrió (no estorban las ejecuciones: no hay llaves foráneas a propósito)", r.status_code == 200 and r2.status_code == 200, f"{r.text[:100]} {r2.text[:100]}")
    despues = abrir(adm, org, e1["id"]).json()
    rev("la ejecución sigue diciendo los nombres de entonces: el proceso sin renombrar y el virtual aunque ya no exista", despues == e1 and despues["proceso"]["nombre"] == "Validar identidad" and despues["virtual"]["nombre"] == "Crédito automotriz")
    rev("y el historial sigue completo", historial(adm, org).json()["total"] == 6 and historial(adm, org, virtual=v1).json()["total"] == 6)
    r = c.delete(f"/procesos/{pid}", params={"tenant": org}, headers=cab(adm))
    rev("se borra el proceso: la ejecución se queda con su nombre y su dibujo", r.status_code == 200 and abrir(adm, org, e1["id"]).json()["definicion"]["nombre"] == "Validar identidad")

    titulo("12 · Inmutable: ninguna ruta la edita o la borra, y la base rechaza lo incoherente")
    rev("no hay PUT, PATCH ni DELETE sobre una ejecución (405)", all(m(f"/ejecuciones/{e1['id']}", params={"tenant": org}, headers=cab(adm)).status_code == 405 for m in (c.put, c.patch, c.delete)))
    rev("ni sobre la lista (405)", c.delete("/ejecuciones/", params={"tenant": org}, headers=cab(adm)).status_code == 405 and c.put("/ejecuciones/", params={"tenant": org}, headers=cab(adm)).status_code == 405)
    rev("el servicio no ofrece cómo editar ni borrar", not any(hasattr(__import__("servicios.ejecuciones", fromlist=["x"]), n) for n in ("editar", "actualizar", "borrar", "eliminar", "modificar")))

    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    cur = con.cursor()

    def insertar_corrida(**cambios) -> str | None:
        valores = {"publicId": "ejecucion-" + uuid.uuid4().hex, "tenant": org, "caseId": "expediente-x", "caseName": "X", "virtualCaseId": "expediente-virtual-x", "virtualCaseName": "V",
                   "processId": "proceso-x", "processName": "P", "processRevision": 1, "outcomeCase": "si", "outcomeText": "Aprobado", "status": "completa",
                   "detailJson": "{}", "definitionJson": "{}", "executedBy": "prueba"}
        valores.update(cambios)
        try:
            cur.execute(f"INSERT INTO nexus.processRuns ({', '.join(valores)}) VALUES ({', '.join('?' * len(valores))})", *valores.values())
            return None
        except Exception as exc:  # noqa: BLE001
            return str(exc)

    rev("la fila mínima válida entra (control de la prueba)", insertar_corrida() is None)
    rev("la base rechaza «completa» con el caso `sin` (faltaron datos no es una corrida completa)", "CK_processRuns_statusOutcome" in (insertar_corrida(outcomeCase="sin", status="completa") or ""))
    rev("y «incompleta» con un caso que sí decidió", "CK_processRuns_statusOutcome" in (insertar_corrida(outcomeCase="no", status="incompleta") or ""))
    rev("un caso que no es si / no / sin", "CK_processRuns_outcomeCase" in (insertar_corrida(outcomeCase="xx") or ""))
    rev("una palabra en blanco", "CK_processRuns_outcomeText" in (insertar_corrida(outcomeText="   ") or ""))
    rev("un detalle que no es JSON", "CK_processRuns_detailJson" in (insertar_corrida(detailJson="no es json") or ""))
    rev("una foto del proceso que no es JSON", "CK_processRuns_definitionJson" in (insertar_corrida(definitionJson="{") or ""))
    rev("un id público que no es `ejecucion-` + 32 hexadecimales en minúsculas", "CK_processRuns_publicId" in (insertar_corrida(publicId="ejecucion-ABC") or "") and "CK_processRuns_publicId" in (insertar_corrida(publicId="ejecucion-" + uuid.uuid4().hex.upper()) or ""))
    rev("una revisión de cero", "CK_processRuns_processRevision" in (insertar_corrida(processRevision=0) or ""))
    rev("un tenant con mayúsculas o espacios", "CK_processRuns_tenant" in (insertar_corrida(tenant="Con Espacio") or ""))
    rev("el mismo id público dos veces", "UQ_processRuns_publicId" in (insertar_corrida(publicId=e1["id"]) or ""))
    cur.execute("SELECT processRunId FROM nexus.processRuns WHERE publicId = ?", e1["id"])
    corrida_id = cur.fetchone()[0]

    def insertar_valor(**cambios) -> str | None:
        valores = {"processRunId": corrida_id, "position": 90, "nodeId": "valor-final-x", "name": "X", "kind": "valor", "family": "texto", "status": "listo", "valueText": "v", "valueJson": None, "origin": None}
        valores.update(cambios)
        try:
            cur.execute(f"INSERT INTO nexus.processRunFinalValues ({', '.join(valores)}) VALUES ({', '.join('?' * len(valores))})", *valores.values())
            return None
        except Exception as exc:  # noqa: BLE001
            return str(exc)

    rev("un valor final válido entra (control)", insertar_valor() is None)
    rev("un valor final «listo» sin valor", "CK_processRunFinalValues_value" in (insertar_valor(position=91, valueText=None) or ""))
    rev("uno sin valor que trae texto", "CK_processRunFinalValues_value" in (insertar_valor(position=92, status="sin_valores") or ""))
    rev("una lista en algo que no es lista", "CK_processRunFinalValues_valueJson" in (insertar_valor(position=93, valueJson='["a"]') or ""))
    rev("una lista cuyo JSON no lo es", "CK_processRunFinalValues_valueJson" in (insertar_valor(position=94, kind="lista", valueJson="[a") or ""))
    rev("una forma, una clase o un estado que no existen", all(x in (insertar_valor(position=95 + i, **cambio) or "") for i, (cambio, x) in enumerate((({"kind": "mapa"}, "kind"), ({"family": "color"}, "family"), ({"status": "casi"}, "status")))))
    rev("la misma posición dos veces en una corrida", "UQ_processRunFinalValues_position" in (insertar_valor(position=90) or ""))
    rev("un valor final de una corrida que no existe", "FK_processRunFinalValues_run" in (insertar_valor(processRunId=999999, position=1) or ""))
    con.close()

    titulo("13 · Los procesos que se ofrecen: `ejecutable` en el expediente virtual")
    pid2 = "proceso-" + uuid.uuid4().hex
    guardar_proceso(adm, org, pid2, proceso_base(nombre="Ejecutable"), 0)
    p_sin2 = "proceso-" + uuid.uuid4().hex
    guardar_proceso(adm, org, p_sin2, {**sin_res, "nombre": "Sin Resultado"}, 0)
    v_n = crear_v(adm, org, juan, credito, "Nuevo", [ine1])
    ofrecidos = {p["nombre"]: p for p in ver_v(adm, org, juan, v_n)["procesos"]}
    rev("el expediente virtual dice cuáles de sus procesos se pueden ejecutar", ofrecidos["Ejecutable"]["ejecutable"] is True and ofrecidos["Sin Resultado"]["ejecutable"] is False and ofrecidos["Apagado"]["activo"] is False and ofrecidos["Resultado suelto"]["ejecutable"] is False and ofrecidos["Resultado de un campo"]["ejecutable"] is False, str(ofrecidos)[:300])
    rev("`resultado_conectado` es solo mirar el dibujo", __import__("servicios.procesos", fromlist=["x"]).resultado_conectado({"nodos": [], "aristas": []}) is False)

    titulo("14 · Detalles del servicio")
    prep14 = preparar(adm, org, juan, v_n, pid2).json()
    r = registrar(adm, {
        "tenant": org, "expedienteId": juan, "virtualId": v_n, "procesoId": pid2, "procesoRevision": prep14["proceso"]["revision"],
        "resultado": {"caso": "si", "texto": "  Certificado "},
        "valoresFinales": [{"nodoId": "valor-final-1", "nombre": "  CURP ", "forma": "valor", "familia": "texto", "estado": "listo", "valor": "X", "lista": None, "origen": None}],
        "detalle": {},
    })
    rev("una palabra con espacios de más se reconoce, y se guarda sin ellos", r.status_code == 201 and r.json()["resultado"]["texto"] == "Certificado", r.text[:200])
    rev("y el nombre de un valor final también se guarda sin los espacios de los extremos", r.json()["valoresFinales"][0]["nombre"] == "CURP", r.text[:200])
    rev("el rastro de auditoría de esa ejecución lleva la palabra con la que terminó", auditoria("PROCESS_RUN_RECORDED")[-1].get("resultado") == "Certificado")
    rev("el historial acota el límite que se le pide: 0 se vuelve 1", len(ejecuciones_servicio.listar(org, limite=0)["ejecuciones"]) == 1)
    # Un documento sin tipo documental (nunca se clasificó) no es un duplicado de nada, ni se cae al calcular los duplicados.
    meter(adm, org, juan, suelto)
    v_sin_tipo = crear_v(adm, org, juan, credito, "Con un documento sin tipo", [suelto])
    vst = ver_v(adm, org, juan, v_sin_tipo)
    marca_suelto = next(d for d in vst["documentos"] if d["id"] == suelto)
    rev("un documento sin tipo documental no es un duplicado de nada (y calcular los duplicados no se cae)", marca_suelto["duplicado"] is False and all(suelto not in {d["id"] for d in g["documentos"]} for g in vst["duplicados"]), str(vst["duplicados"])[:200])
    from servicios import procesos as procesos_servicio

    rev("un cable a otro punto del Resultado no cuenta: su única entrada es «a»", procesos_servicio.resultado_conectado({
        "nodos": [{"id": "operador-1", "type": "operador"}, {"id": "resultado-final", "type": "resultado-final"}],
        "aristas": [{"source": "operador-1", "target": "resultado-final", "targetHandle": "b"}],
    }) is False)
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
print("Todo bien." if not fallos else f"{fallos} FALLA(S).")
sys.exit(1 if fallos else 0)
