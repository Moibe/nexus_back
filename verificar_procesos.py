"""¿Los procesos del motor de reglas viven en el servidor, por organización, y el dibujo se guarda entero?

    .venv/Scripts/python verificar_procesos.py

OFFLINE y sin tocar nada tuyo, como los demás `verificar_*`: el almacén es una carpeta temporal.

Lo que se prueba:

- crear, leer, guardar y borrar un proceso; lo ve TODA la organización (no solo quien lo creó) y ninguna otra;
- dos personas editando lo mismo: la segunda recibe 409 con la revisión vigente y no pisa nada;
- un proceso de otra organización responde como si no existiera (404), y su tenant, 403 `sin_acceso`;
- lo que no es un proceso (id raro, sin nombre, nodos de un tipo inventado, cables a nodos que no existen, ids repetidos, posiciones
  absurdas, enorme) se rechaza, y lo que el servidor pone no se puede forzar;
- lo que se guarda sale LIMPIO: del nodo y del cable solo lo que el lienzo necesita;
- crear y borrar dejan rastro con quién; guardar a secas, no.
"""

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-procesos-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import auth, usuarios  # noqa: E402

fallos = 0


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


def nuevo_id() -> str:
    return "proceso-" + uuid.uuid4().hex


def nodo_tipo(nodo_id: str, tipo_id: str, x: float = 0, y: float = 0) -> dict:
    return {"id": nodo_id, "type": "tipo-documental", "position": {"x": x, "y": y}, "data": {"tipoId": tipo_id}}


def nodo_op(nodo_id: str, operador: str, a: str = "", b: str = "", x: float = 300, y: float = 0) -> dict:
    return {"id": nodo_id, "type": "operador", "position": {"x": x, "y": y}, "data": {"operador": operador, "a": a, "b": b}}


def cable(origen: str, salida: str | None, destino: str, entrada: str | None) -> dict:
    return {"id": f"xy-edge__{origen}{salida}-{destino}{entrada}", "source": origen, "sourceHandle": salida, "target": destino, "targetHandle": entrada}


def proceso(nombre: str, **extra) -> dict:
    """Un proceso con dos documentos y una regla: el rfc del acta IGUAL al rfc de la constancia."""
    return {
        "nombre": nombre,
        "descripcion": f"Descripción de {nombre}",
        "nodos": [nodo_tipo("tipo-acta", "tipo-aaaa1111"), nodo_tipo("tipo-const", "tipo-bbbb2222", y=400), nodo_op("operador-1", "igual", "AB1", "ab1")],
        "aristas": [cable("tipo-acta", "rfc", "operador-1", "a"), cable("tipo-const", "rfc", "operador-1", "b")],
        **extra,
    }


def guardar(bearer: str, tenant: str, proceso_id: str, datos: dict, revision: int):
    return c.put(f"/procesos/{proceso_id}", json={"tenant": tenant, "revision": revision, "datos": datos}, headers=cab(bearer))


def listar(bearer: str, tenant: str):
    return c.get("/procesos/", params={"tenant": tenant}, headers=cab(bearer))


def ids(bearer: str, tenant: str) -> list[str]:
    return [p["id"] for p in listar(bearer, tenant).json()["procesos"]]


def auditoria(accion: str) -> list[dict]:
    ruta = RAIZ / ".registro" / "auditoria.jsonl"
    if not ruta.exists():
        return []
    return [e for e in (json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip()) if e.get("accion") == accion]


def crear_organizacion(bearer_super: str, nombre: str, correo: str, contrasena: str) -> tuple[str, str]:
    r = c.post("/organizaciones/", json={"nombre": nombre, "adminNombre": "Admin", "adminApellidoPaterno": nombre, "adminEmail": correo}, headers=cab(bearer_super))
    assert r.status_code == 201, r.text
    return r.json()["organizacion"]["guid"], con_primer_acceso(correo, r.json()["contrasenaTemporal"], contrasena)


try:
    titulo("0 · Montaje: dos organizaciones (la primera con dos personas) y el administrador de plataforma")
    temporal_super = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal_super))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal_super, "Cobre-Lento-Azul-47!")
    org_a, a_admin = crear_organizacion(bearer_super, "Alfa", "rita@ejemplo.com", "Rumbo-Sereno-Azul-47!")
    org_b, b_admin = crear_organizacion(bearer_super, "Beta", "beto@ejemplo.com", "Rumbo-Sereno-Rojo-48!")
    r = c.post("/usuarios/", json={"nombre": "Omar", "apellidoPaterno": "Operador", "email": "omar@ejemplo.com", "rol": "OPERADOR"}, headers=cab(a_admin))
    assert r.status_code == 201, r.text
    a_otra = con_primer_acceso("omar@ejemplo.com", r.json()["contrasenaTemporal"], "Cielo-Abierto-Verde-52!")
    guid_rita = usuarios.para_login("rita@ejemplo.com")["guid"]
    guid_omar = usuarios.para_login("omar@ejemplo.com")["guid"]
    rev("hay dos organizaciones distintas y tres personas con sesión", org_a != org_b and a_admin and b_admin and a_otra)

    titulo("1 · Crear, leer, guardar y borrar")
    p1 = nuevo_id()
    r = guardar(a_admin, org_a, p1, proceso("Alta de cliente"), 0)
    rev("crear (revisión 0) responde 200 con revisión 1", r.status_code == 200 and r.json()["revision"] == 1, r.text[:200])
    d = r.json()
    rev("devuelve el proceso con su id dentro de `datos` y quién lo creó", d["datos"]["id"] == p1 and d["creadoPor"] == guid_rita and d["modificadoPor"] == guid_rita and d["creadoEn"], str(d)[:300])
    r = listar(a_admin, org_a)
    rev("aparece en la lista de su organización", r.status_code == 200 and ids(a_admin, org_a) == [p1], r.text[:200])
    guardado = r.json()["procesos"][0]["datos"]
    rev("el dibujo vuelve idéntico: nombre, nodos y cables", guardado["nombre"] == "Alta de cliente" and len(guardado["nodos"]) == 3 and len(guardado["aristas"]) == 2)
    rev("los valores de ejemplo de la regla se conservan", guardado["nodos"][2]["data"] == {"operador": "igual", "a": "AB1", "b": "ab1"}, str(guardado["nodos"][2]))
    rev("y las posiciones", guardado["nodos"][1]["position"] == {"x": 0, "y": 400})

    titulo("2 · Lo ve TODA la organización, y ninguna otra")
    rev("otra persona de la misma organización lo ve", ids(a_otra, org_a) == [p1])
    rev("la otra organización tiene su lista vacía", ids(b_admin, org_b) == [])
    r = listar(b_admin, org_a)
    rev("pedir la lista de OTRA organización: 403 sin_acceso", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso", r.text[:200])
    rev("y no dice nada de la organización ajena", "Alfa" not in r.text and org_a not in r.text, r.text[:200])

    titulo("3 · Otra persona lo edita; el creador se conserva")
    editado = proceso("Alta de cliente")
    editado["nodos"][2] = nodo_op("operador-1", "igual", "AB1", "xyz")
    r = guardar(a_otra, org_a, p1, editado, 1)
    rev("guardar sobre la revisión vigente: 200 y revisión 2", r.status_code == 200 and r.json()["revision"] == 2, r.text[:200])
    d = r.json()
    rev("`modificadoPor` es quien lo editó y `creadoPor` sigue siendo quien lo creó", d["modificadoPor"] == guid_omar and d["creadoPor"] == guid_rita, str(d)[:300])
    rev("lo editado quedó", listar(a_admin, org_a).json()["procesos"][0]["datos"]["nodos"][2]["data"]["b"] == "xyz")

    titulo("4 · Dos personas a la vez: no se pisa nada")
    r = guardar(a_admin, org_a, p1, proceso("Editado por Rita"), 1)
    rev("guardar sobre una revisión vieja: 409", r.status_code == 409, r.text[:200])
    det = r.json()["detail"]
    rev("trae la revisión vigente para volver a leer", det.get("codigo") == "conflicto" and det.get("revisionActual") == 2, str(det))
    rev("y NO se guardó: sigue lo de la otra persona", listar(a_admin, org_a).json()["procesos"][0]["datos"]["nombre"] == "Alta de cliente")
    r = guardar(a_admin, org_a, p1, proceso("Otro"), 0)
    rev("crear con un id que YA existe: 409 con su revisión", r.status_code == 409 and r.json()["detail"]["revisionActual"] == 2, r.text[:200])

    titulo("5 · El de otra organización responde como si no existiera")
    r = guardar(b_admin, org_b, p1, proceso("Robado"), 2)
    rev("editar un id AJENO, con la revisión correcta: 404", r.status_code == 404, r.text[:200])
    r2 = guardar(b_admin, org_b, nuevo_id() + "x", proceso("Nada"), 3)
    rev("es la MISMA respuesta que para uno que no existe", r2.status_code == 404 and r2.json() == r.json(), f"{r.text[:100]} | {r2.text[:100]}")
    r = guardar(b_admin, org_b, p1, proceso("Colado"), 0)
    rev("crear con un id que es de otra organización: 409 sin decir de quién", r.status_code == 409 and r.json()["detail"].get("revisionActual") is None, r.text[:200])
    r = c.delete(f"/procesos/{p1}", params={"tenant": org_b}, headers=cab(b_admin))
    rev("borrar un id ajeno: 404", r.status_code == 404, r.text[:200])
    r = guardar(b_admin, org_a, p1, proceso("Cruzado"), 2)
    rev("escribir poniendo el tenant de OTRA: 403 sin_acceso", r.status_code == 403, r.text[:200])
    rev("nada de lo anterior tocó el proceso de la primera", listar(a_admin, org_a).json()["procesos"][0]["datos"]["nombre"] == "Alta de cliente" and ids(b_admin, org_b) == [])
    intentos = auditoria("CROSS_TENANT_ACCESS")
    rev("los intentos con el tenant ajeno quedan en la bitácora", len(intentos) >= 2 and all(i.get("recurso") == "proceso" for i in intentos), str(intentos[-1:]))

    titulo("6 · Sin llave o sin persona, no pasa nada")
    rev("sin Bearer: 401", c.get("/procesos/", params={"tenant": org_a}, headers=cab()).status_code == 401)
    rev("sin llave de servicio: 401", c.get("/procesos/", params={"tenant": org_a}, headers=cab(a_admin, servicio=False)).status_code == 401)
    rev("escribir sin Bearer: 401", c.put(f"/procesos/{p1}", json={"tenant": org_a, "revision": 2, "datos": proceso("x")}, headers=cab()).status_code == 401)
    rev("borrar sin Bearer: 401", c.delete(f"/procesos/{p1}", params={"tenant": org_a}, headers=cab()).status_code == 401)

    titulo("7 · Lo que no es un proceso se rechaza")

    def rechaza(que: str, datos: dict | None = None, proceso_id: str | None = None, revision: int = 0, texto: str | None = None) -> None:
        r = guardar(a_admin, org_a, proceso_id or nuevo_id(), datos if datos is not None else proceso("Malo"), revision)
        rev(f"{que}: 400", r.status_code == 400 and (texto is None or texto in r.text), f"{r.status_code} {r.text[:200]}")

    rechaza("un id con mayúsculas y espacios", proceso_id="Proceso Raro")
    rechaza("un id que no empieza con «proceso-»", proceso_id="tipo-" + uuid.uuid4().hex)
    rechaza("un id demasiado corto", proceso_id="proceso-ab")
    rechaza("sin nombre", {**proceso("x"), "nombre": "   "})
    rechaza("con un nombre de más de 200 caracteres", {**proceso("x"), "nombre": "a" * 201})
    rechaza("con una descripción que no es texto", {**proceso("x"), "descripcion": 5})
    rechaza("sin la lista de nodos", {k: v for k, v in proceso("x").items() if k != "nodos"})
    rechaza("sin la lista de cables", {k: v for k, v in proceso("x").items() if k != "aristas"})
    rechaza("con los nodos como diccionario", {**proceso("x"), "nodos": {}})
    rechaza("con un nodo que no es un objeto", {**proceso("x"), "nodos": ["nodo"]})
    n = proceso("x")
    n["nodos"][0]["type"] = "marciano"
    rechaza("con un nodo de un tipo que no existe", n, texto="tipo que no existe")
    n = proceso("x")
    del n["nodos"][0]["position"]
    rechaza("con un nodo sin posición", n, texto="posición")
    for malo in (True, "10", None, 10**9, -(10**9)):
        n = proceso("x")
        n["nodos"][0]["position"]["x"] = malo
        rechaza(f"con una posición absurda ({malo!r})", n)
    n = proceso("x")
    n["nodos"][0]["data"] = {}
    rechaza("con un nodo de documento sin su tipo", n)
    n = proceso("x")
    n["nodos"][2]["data"]["operador"] = ""
    rechaza("con un operador sin nombre", n)
    n = proceso("x")
    n["nodos"][2]["data"]["a"] = 5
    rechaza("con un valor de ejemplo que no es texto", n)
    n = proceso("x")
    n["nodos"][2]["data"]["a"] = "x" * 5001
    rechaza("con un valor de ejemplo demasiado largo (más de 5000 caracteres)", n)
    n = proceso("x")
    n["nodos"].append({"id": "constante-1", "type": "constante", "position": {"x": 0, "y": 0}, "data": {"familia": "numero", "valor": "2"}})
    n["nodos"][-1]["data"]["familia"] = "marciano"
    rechaza("con un valor fijo de una clase que no existe", n, texto="clase")
    n["nodos"][-1]["data"] = {"familia": "numero", "valor": 2}
    rechaza("con un valor fijo que no es texto", n)
    n["nodos"][-1]["data"] = {"valor": "2"}
    rechaza("con un valor fijo sin su clase", n)
    n["nodos"][-1]["data"] = {"familia": "texto", "valor": "x" * 501}
    rechaza("con un valor fijo demasiado largo", n)
    n = proceso("x")
    n["nodos"][1]["id"] = "tipo-acta"
    rechaza("con dos nodos del mismo id", n, texto="mismo identificador")
    n = proceso("x")
    n["aristas"][0]["target"] = "operador-99"
    rechaza("con un cable a un nodo que no existe", n, texto="no existe")
    n = proceso("x")
    n["aristas"][0]["source"] = "fantasma"
    rechaza("con un cable que sale de un nodo que no existe", n, texto="no existe")
    n = proceso("x")
    n["aristas"][1]["id"] = n["aristas"][0]["id"]
    rechaza("con dos cables del mismo id", n, texto="mismo identificador")
    n = proceso("x")
    n["aristas"][0]["id"] = ""
    rechaza("con un cable sin id", n)
    n = proceso("x")
    n["aristas"][0]["sourceHandle"] = 7
    rechaza("con el punto de un cable que no es texto", n)
    n = proceso("x")
    n["nodos"] = [nodo_tipo(f"tipo-{i}", "tipo-aaaa1111") for i in range(301)]
    n["aristas"] = []
    rechaza("con 301 nodos", n, texto="demasiado grande")
    n = proceso("x")
    n["aristas"] = [cable("tipo-acta", f"campo{i}", "operador-1", "a") for i in range(601)]
    rechaza("con 601 cables", n, texto="demasiado grande")
    n = proceso("x")
    n["extra"] = "x" * 1_100_000
    rechaza("enorme (más de 1 MB)", n, texto="demasiado grande")
    r = guardar(a_admin, org_a, nuevo_id(), proceso("x"), -1)
    rev("con una revisión negativa: rechazada", r.status_code in (400, 422), r.text[:200])
    r = c.put(f"/procesos/{nuevo_id()}", json={"tenant": org_a, "revision": 0, "datos": "no"}, headers=cab(a_admin))
    rev("con los datos que no son un objeto: rechazado", r.status_code in (400, 422), r.text[:200])
    rev("nada de lo anterior se guardó", ids(a_admin, org_a) == [p1])

    titulo("8 · Lo que el servidor pone no se puede forzar; lo que sobra se descarta")
    p2 = nuevo_id()
    forzado = proceso("Forzado", revision=99, creadoPor="otra-persona", creadoEn="2000-01-01T00:00:00Z", modificadoPor="x", modificadoEn="y", tenant="otra-org", meta={"x": 1})
    r = guardar(a_admin, org_a, p2, forzado, 0)
    d = r.json()
    rev("se guarda, pero con lo que pone el servidor", r.status_code == 200 and d["revision"] == 1 and d["creadoPor"] == guid_rita and d["creadoEn"] != "2000-01-01T00:00:00Z", str(d)[:300])
    rev("y lo reservado no se cuela dentro de `datos`", not ({"revision", "creadoPor", "creadoEn", "modificadoPor", "modificadoEn", "tenant", "meta"} & set(d["datos"])), str(d["datos"].keys()))
    rev("el proceso es de la organización de quien lo guardó, no de la que dijo el cuerpo", ids(a_admin, org_a).count(p2) == 1 and p2 not in ids(b_admin, org_b))
    sucio = proceso("Sucio")
    sucio["nodos"][0].update({"measured": {"width": 288, "height": 400}, "selected": True, "dragging": False, "zIndex": 4, "internals": {"x": 1}})
    sucio["nodos"][0]["data"]["basura"] = "x"
    sucio["nodos"][2]["data"]["resultado"] = "pasa"
    sucio["aristas"][0].update({"selected": True, "animated": False, "style": "stroke: red", "type": "default"})
    sucio["vista"] = {"x": 10, "y": 20, "zoom": 0.8}
    p3 = nuevo_id()
    r = guardar(a_admin, org_a, p3, sucio, 0)
    d = r.json()["datos"]
    rev("del nodo solo se guardan id, tipo, posición y sus datos", set(d["nodos"][0]) == {"id", "type", "position", "data"}, str(d["nodos"][0]))
    rev("de los datos de un nodo solo lo que el lienzo usa", d["nodos"][0]["data"] == {"tipoId": "tipo-aaaa1111"} and set(d["nodos"][2]["data"]) == {"operador", "a", "b"}, str(d["nodos"]))
    rev("del cable solo id, origen, destino y sus dos puntos", set(d["aristas"][0]) == {"id", "source", "sourceHandle", "target", "targetHandle"}, str(d["aristas"][0]))
    rev("lo demás que mandó la pantalla (la vista) se conserva en `datos`", d.get("vista") == {"x": 10, "y": 20, "zoom": 0.8})

    titulo("8b · Las listas: operadores de listas, valores fijos y muestras de varios renglones")
    listas = proceso("Socios")
    muestra_larga = "\n".join(f"Socio número {i}" for i in range(200))  # unos 3 KB: una lista de ejemplo
    listas["nodos"] += [
        {"id": "operador-2", "type": "operador", "position": {"x": 700, "y": 10}, "data": {"operador": "cantidad", "a": muestra_larga, "b": "", "resultado": "200"}},
        {"id": "constante-1", "type": "constante", "position": {"x": 700, "y": 300}, "data": {"familia": "numero", "valor": "2", "basura": "x"}, "selected": True},
        {"id": "operador-3", "type": "operador", "position": {"x": 1000, "y": 10}, "data": {"operador": "mayor"}},
    ]
    listas["aristas"] += [cable("operador-2", "resultado", "operador-3", "a"), cable("constante-1", "valor", "operador-3", "b")]
    p5 = nuevo_id()
    r = guardar(a_admin, org_a, p5, listas, 0)
    rev("un proceso con una cantidad, un valor fijo y una comparación se guarda", r.status_code == 200, r.text[:200])
    d = r.json()["datos"]
    cant = next(n for n in d["nodos"] if n["id"] == "operador-2")
    fijo = next(n for n in d["nodos"] if n["id"] == "constante-1")
    rev("la muestra de varios renglones (unos 3 KB) se conserva entera", cant["data"]["a"] == muestra_larga and len(muestra_larga) > 2500, str(len(cant["data"]["a"])))
    rev("el valor fijo guarda solo su clase y su valor", fijo["data"] == {"familia": "numero", "valor": "2"} and set(fijo) == {"id", "type", "position", "data"}, str(fijo))
    rev("de un operador solo se guardan cuál es y sus dos muestras (lo que calculó, no)", set(cant["data"]) == {"operador", "a", "b"}, str(cant["data"]))
    rev("un operador sin muestras en sus datos las guarda vacías", next(n for n in d["nodos"] if n["id"] == "operador-3")["data"] == {"operador": "mayor", "a": "", "b": ""})
    rev("los cables del valor fijo y del resultado se guardan", {(a["source"], a["sourceHandle"]) for a in d["aristas"]} >= {("operador-2", "resultado"), ("constante-1", "valor")})
    n = proceso("x")
    n["nodos"][2]["data"]["a"] = "x" * 5000
    r = guardar(a_admin, org_a, nuevo_id(), n, 0)
    rev("una muestra de exactamente 5000 caracteres cabe", r.status_code == 200, r.text[:120])
    r = guardar(a_admin, org_a, p5, listas, 1)
    rev("y se puede volver a guardar encima", r.status_code == 200 and r.json()["revision"] == 2, r.text[:120])
    c.delete(f"/procesos/{p5}", params={"tenant": org_a}, headers=cab(a_admin))

    titulo("8c · «Existe documento» e «Implica»: el back las guarda como cualquier operador y respeta el punto del documento")
    con_doc = proceso("Si hay constancia, su RFC debe coincidir")
    con_doc["nodos"] += [
        nodo_op("operador-2", "existe", "si", "", x=700, y=400),
        nodo_op("operador-3", "implica", x=1000, y=200),
    ]
    con_doc["aristas"] += [
        cable("tipo-const", "@documento", "operador-2", "a"),
        cable("operador-2", "resultado", "operador-3", "a"),
        cable("operador-1", "resultado", "operador-3", "b"),
    ]
    p6 = nuevo_id()
    r = guardar(a_admin, org_a, p6, con_doc, 0)
    rev("un proceso con «Existe documento» e «Implica» se guarda", r.status_code == 200, r.text[:200])
    d = r.json()["datos"]
    existe = next(n for n in d["nodos"] if n["id"] == "operador-2")
    implica = next(n for n in d["nodos"] if n["id"] == "operador-3")
    rev("«Existe documento» guarda cuál es y el ejemplo elegido (sí/no) en `a`", existe["data"] == {"operador": "existe", "a": "si", "b": ""}, str(existe))
    rev("«Implica» guarda cuál es y nada más", implica["data"] == {"operador": "implica", "a": "", "b": ""}, str(implica))
    rev("el cable que sale del punto «@documento» del tipo se conserva tal cual", any(a["source"] == "tipo-const" and a["sourceHandle"] == "@documento" and a["target"] == "operador-2" and a["targetHandle"] == "a" for a in d["aristas"]), str(d["aristas"]))
    leido = next(p for p in listar(a_admin, org_a).json()["procesos"] if p["id"] == p6)
    rev("lo que se lee de vuelta es lo mismo que se guardó", leido["datos"]["nodos"] == d["nodos"] and leido["datos"]["aristas"] == d["aristas"])
    c.delete(f"/procesos/{p6}", params={"tenant": org_a}, headers=cab(a_admin))

    titulo("9 · Un proceso vacío y uno con cables a un punto sin nombre")
    p4 = nuevo_id()
    r = guardar(a_admin, org_a, p4, {"nombre": "Vacío", "nodos": [], "aristas": []}, 0)
    rev("un lienzo sin nada se puede guardar", r.status_code == 200 and r.json()["datos"]["nodos"] == [] and r.json()["datos"]["descripcion"] == "", r.text[:200])
    sin_punto = proceso("Sin punto")
    sin_punto["aristas"][0]["sourceHandle"] = None
    r = guardar(a_admin, org_a, nuevo_id(), sin_punto, 0)
    rev("un cable sin punto de salida (null) es válido: así lo manda la librería", r.status_code == 200 and r.json()["datos"]["aristas"][0]["sourceHandle"] is None, r.text[:200])

    titulo("10 · Borrar")
    r = c.delete(f"/procesos/{p2}", params={"tenant": org_a}, headers=cab(a_otra))
    rev("otra persona de la organización puede borrarlo", r.status_code == 200 and r.json() == {"eliminado": True}, r.text[:200])
    rev("ya no está en la lista", p2 not in ids(a_admin, org_a))
    r = c.delete(f"/procesos/{p2}", params={"tenant": org_a}, headers=cab(a_admin))
    rev("borrarlo otra vez: 404", r.status_code == 404, r.text[:200])
    r = guardar(a_admin, org_a, p2, proceso("Resucitado"), 1)
    rev("editar uno borrado: 404", r.status_code == 404, r.text[:200])
    r = guardar(a_admin, org_a, p2, proceso("De nuevo"), 0)
    rev("el id de uno borrado se puede volver a usar (empieza en revisión 1)", r.status_code == 200 and r.json()["revision"] == 1, r.text[:200])

    titulo("11 · Rastro")
    creados = auditoria("PROCESS_CREATED")
    borrados = auditoria("PROCESS_DELETED")
    rev("crear deja rastro con quién, de qué organización y qué proceso", any(e.get("procesoId") == p1 and e.get("usuarioGuid") == guid_rita and e.get("tenant") == org_a and e.get("nombre") == "Alta de cliente" for e in creados), str(creados[:1]))
    rev("borrar deja rastro con quién lo hizo", any(e.get("procesoId") == p2 and e.get("usuarioGuid") == guid_omar for e in borrados), str(borrados))
    rev("guardar a secas NO deja rastro (el proceso 1 se guardó dos veces y se anotó una sola vez)", sum(1 for e in creados if e.get("procesoId") == p1) == 1 and not any(e.get("procesoId") == p1 for e in borrados))
    rev("el rastro no lleva el dibujo ni los valores de ejemplo", not any("nodos" in e or "aristas" in e or "datos" in e for e in creados + borrados))

    titulo("12 · Resiste un corte a media escritura")
    ruta = RAIZ / ".registro" / "procesos.jsonl"
    with open(ruta, "ab") as f:
        f.write(b'{"op": "guardar", "id": "proceso-roto')
    antes = ids(a_admin, org_a)
    rev("una última línea rota se ignora al leer", ids(a_admin, org_a) == antes and len(antes) >= 3)
    n = nuevo_id()
    r = guardar(a_admin, org_a, n, proceso("Después del corte"), 0)
    rev("y el guardado siguiente no queda pegado a ella", r.status_code == 200 and n in ids(a_admin, org_a))
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
