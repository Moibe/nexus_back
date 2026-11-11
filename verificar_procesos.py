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
- crear y borrar dejan rastro con quién; guardar a secas, no;
- (2026-11-05) un proceso ya NO se liga a ningún tipo (se quitaron los expedientes virtuales): si un cliente viejo todavía manda
  `datos.tipoExpedienteVirtualId` —válido, inexistente o basura— se ignora (se guarda igual, sin él), no viaja al JSON guardado ni se devuelve, ni de lo que
  el almacén traía guardado de antes; `resultado_conectado` sigue pidiendo el nodo, una regla y su punto «a».

Con `--sql` corre LO MISMO contra SQL Server (migración `0005`, contenedor desechable de `pruebas_sql.py`); lo que lee el JSONL a mano se
salta ahí.

    .venv/Scripts/python verificar_procesos.py --sql
"""

import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
SQL = "--sql" in sys.argv
if SQL:
    # ANTES de importar la app: `config.py` lee el entorno una sola vez.
    import pruebas_sql

    BASE_SQL = pruebas_sql.preparar("nexus-procesos-prueba", 14337, dejar="--dejar" in sys.argv)

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
    # Por el servicio y no leyendo el JSONL: con --sql la bitácora vive en nexus.auditEvents (migración 0006).
    from servicios import auditoria as bitacora

    return bitacora.eventos({accion})


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
    # Supervisor y no Operador: desde NEX-472 (2026-10-22) solo Administrador y Supervisor editan esto (`verificar_roles.py` prueba al resto).
    r = c.post("/usuarios/", json={"nombre": "Omar", "apellidoPaterno": "Supervisor", "email": "omar@ejemplo.com", "rol": "SUPERVISOR"}, headers=cab(a_admin))
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
    # `activo` lo marca la lista de procesos del front (2026-10-29). El back no lo interpreta, pero el front depende de que lo conserve
    # (también el `false`) y de poder cambiarlo con un guardado normal sobre la revisión que leyó: si algún día `_validar` descarta lo
    # desconocido, activar y desactivar fallaría en silencio.
    r = guardar(a_admin, org_a, p3, {**d, "activo": False}, 1)
    rev("`activo: false` (lo marca la lista de procesos del front) se conserva al guardar", r.status_code == 200 and r.json()["datos"].get("activo") is False and r.json()["revision"] == 2, r.text[:200])
    leido = next(x for x in listar(a_admin, org_a).json()["procesos"] if x["id"] == p3)
    rev("y al leer la lista, con su dibujo intacto", leido["datos"].get("activo") is False and leido["datos"]["nodos"] == d["nodos"] and leido["datos"]["aristas"] == d["aristas"])
    r = guardar(a_admin, org_a, p3, {**leido["datos"], "activo": True}, leido["revision"])
    rev("y se vuelve a cambiar con un guardado normal (activo: true, revisión 3)", r.status_code == 200 and r.json()["datos"].get("activo") is True and r.json()["revision"] == 3, r.text[:200])

    titulo("8b · Las listas: operadores de listas, valores fijos y muestras de varios renglones")
    listas = proceso("Socios")
    muestra_larga = "\n".join(f"Socio número {i}" for i in range(200))  # unos 3 KB: una lista de ejemplo
    listas["nodos"] += [
        {"id": "operador-2", "type": "operador", "position": {"x": 700, "y": 10}, "data": {"operador": "cantidad", "a": muestra_larga, "b": "", "resultado": "200"}},
        {"id": "constante-1", "type": "constante", "position": {"x": 700, "y": 300}, "data": {"familia": "numero", "valor": "2", "basura": "x"}, "selected": True},
        {"id": "operador-3", "type": "operador", "position": {"x": 1000, "y": 10}, "data": {"operador": "mayor"}},
        # «Entre» (2026-11-10): la única regla con TRES entradas; su tercera muestra va en `c`. Otro operador que la mande no la guarda.
        {"id": "operador-4", "type": "operador", "position": {"x": 1300, "y": 10}, "data": {"operador": "entre", "a": "2026-06-15", "b": "2026-01-01", "c": "2026-12-31"}},
        {"id": "operador-5", "type": "operador", "position": {"x": 1600, "y": 10}, "data": {"operador": "entre"}},
        {"id": "operador-6", "type": "operador", "position": {"x": 1900, "y": 10}, "data": {"operador": "igual", "a": "x", "b": "y", "c": "no va"}},
        # «Antigüedad contra hoy» (2026-11-10): guarda su `maximo` y su `unidad` (3 meses si no trae); el `hoy` de ejemplo del lienzo NO se guarda.
        {"id": "operador-7", "type": "operador", "position": {"x": 2200, "y": 10}, "data": {"operador": "antiguedad", "a": "2026-09-10", "b": "", "maximo": 90, "unidad": "dias", "hoy": "2026-11-04"}},
        {"id": "operador-8", "type": "operador", "position": {"x": 2500, "y": 10}, "data": {"operador": "antiguedad"}},
        {"id": "operador-9", "type": "operador", "position": {"x": 2800, "y": 10}, "data": {"operador": "mayor", "a": "1", "b": "2", "maximo": 5, "unidad": "dias"}},
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
    datos_de = lambda nodo_id: next(n for n in d["nodos"] if n["id"] == nodo_id)["data"]
    rev("«Entre» guarda sus TRES muestras (valor, desde y hasta)", datos_de("operador-4") == {"operador": "entre", "a": "2026-06-15", "b": "2026-01-01", "c": "2026-12-31"}, str(datos_de("operador-4")))
    rev("«Entre» sin muestras guarda las tres vacías", datos_de("operador-5") == {"operador": "entre", "a": "", "b": "", "c": ""}, str(datos_de("operador-5")))
    rev("otro operador que mande `c` no la guarda (solo tiene dos entradas)", datos_de("operador-6") == {"operador": "igual", "a": "x", "b": "y"}, str(datos_de("operador-6")))
    rev("«Antigüedad contra hoy» guarda su máximo y su unidad, y NO el hoy de ejemplo", datos_de("operador-7") == {"operador": "antiguedad", "a": "2026-09-10", "b": "", "maximo": 90, "unidad": "dias"}, str(datos_de("operador-7")))
    rev("«Antigüedad contra hoy» sin máximo ni unidad guarda 3 meses", datos_de("operador-8") == {"operador": "antiguedad", "a": "", "b": "", "maximo": 3, "unidad": "meses"}, str(datos_de("operador-8")))
    rev("otro operador que mande máximo o unidad no los guarda", datos_de("operador-9") == {"operador": "mayor", "a": "1", "b": "2"}, str(datos_de("operador-9")))
    for que, malo in (("máximo 0", {"maximo": 0}), ("máximo 10000", {"maximo": 10000}), ("máximo con decimales", {"maximo": 2.5}), ("máximo como texto", {"maximo": "3"}), ("máximo true", {"maximo": True}), ("unidad «semanas»", {"unidad": "semanas"}), ("unidad «años» con ñ", {"unidad": "años"})):
        mal = proceso("Antigüedad mala")
        mal["nodos"] = mal["nodos"] + [{"id": "operador-7", "type": "operador", "position": {"x": 2200, "y": 10}, "data": {"operador": "antiguedad", **malo}}]
        r = guardar(a_admin, org_a, nuevo_id(), mal, 0)
        rev(f"«Antigüedad contra hoy» con {que}: 400", r.status_code == 400 and "Antigüedad contra hoy" in r.text, f"{r.status_code} {r.text[:160]}")
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

    titulo("8d · Los nodos FINALES: «Valor final» y «Resultado» (2026-11-03)")

    def nodo_valor_final(nodo_id: str, nombre: str, a: str = "", x: float = 700, y: float = 0) -> dict:
        return {"id": nodo_id, "type": "valor-final", "position": {"x": x, "y": y}, "data": {"nombre": nombre, "a": a}}

    def nodo_resultado(si: str = "Aprobado", no: str = "Rechazado", sin: str = "Sin datos", nodo_id: str = "resultado-final", x: float = 1000, y: float = 0) -> dict:
        return {"id": nodo_id, "type": "resultado-final", "position": {"x": x, "y": y}, "data": {"si": si, "no": no, "sin": sin}}

    con_finales = proceso("Con valores finales y resultado")
    con_finales["nodos"] += [
        nodo_valor_final("valor-final-1", "RFC del acta", "AB1", y=-200),
        nodo_valor_final("valor-final-2", "¿Coincide el RFC?", y=-60),
        nodo_resultado("Certificado", "Rechazado", "Pendiente de documentos"),
    ]
    con_finales["aristas"] += [
        cable("tipo-acta", "rfc", "valor-final-1", "a"),
        cable("operador-1", "resultado", "valor-final-2", "a"),
        cable("operador-1", "resultado", "resultado-final", "a"),
    ]
    p7 = nuevo_id()
    r = guardar(a_admin, org_a, p7, con_finales, 0)
    rev("un proceso con dos valores finales y un resultado se guarda", r.status_code == 200, r.text[:200])
    d = r.json()["datos"]
    vf = next(n for n in d["nodos"] if n["id"] == "valor-final-1")
    res = next(n for n in d["nodos"] if n["id"] == "resultado-final")
    rev("un valor final guarda su nombre y su ejemplo (nada más)", vf["data"] == {"nombre": "RFC del acta", "a": "AB1"} and set(vf) == {"id", "type", "position", "data"}, str(vf))
    rev("el resultado guarda sus tres palabras, tal como se escribieron", res["data"] == {"si": "Certificado", "no": "Rechazado", "sin": "Pendiente de documentos"}, str(res))
    rev("los cables hacia los nodos finales se guardan", {(a["source"], a["target"], a["targetHandle"]) for a in d["aristas"]} >= {("tipo-acta", "valor-final-1", "a"), ("operador-1", "resultado-final", "a")})
    leido = next(p for p in listar(a_admin, org_a).json()["procesos"] if p["id"] == p7)
    rev("lo que se lee de vuelta es lo mismo que se guardó", leido["datos"]["nodos"] == d["nodos"] and leido["datos"]["aristas"] == d["aristas"])
    sucio = proceso("Datos de más")
    sucio["nodos"] += [
        {"id": "valor-final-1", "type": "valor-final", "position": {"x": 1, "y": 2}, "data": {"nombre": "X", "a": "", "basura": "x", "valor": "7"}},
        {"id": "resultado-final", "type": "resultado-final", "position": {"x": 1, "y": 2}, "data": {"si": "Sí", "no": "No", "sin": "?", "basura": "x"}, "selected": True},
    ]
    r = guardar(a_admin, org_a, nuevo_id(), sucio, 0)
    rev("de un nodo final solo se guarda lo suyo (lo que sobra se descarta)", r.status_code == 200 and next(n for n in r.json()["datos"]["nodos"] if n["id"] == "valor-final-1")["data"] == {"nombre": "X", "a": ""} and set(next(n for n in r.json()["datos"]["nodos"] if n["id"] == "resultado-final")["data"]) == {"si", "no", "sin"}, r.text[:200])
    sin_nombre = proceso("Sin nombre de valor final")
    sin_nombre["nodos"].append({"id": "valor-final-1", "type": "valor-final", "position": {"x": 1, "y": 2}, "data": {}})
    r = guardar(a_admin, org_a, nuevo_id(), sin_nombre, 0)
    rev("un valor final sin nombre se guarda con el nombre vacío (el lienzo le pone el suyo por su número)", r.status_code == 200 and next(n for n in r.json()["datos"]["nodos"] if n["id"] == "valor-final-1")["data"] == {"nombre": "", "a": ""}, r.text[:200])
    largo = proceso("Nombre de 80")
    largo["nodos"].append(nodo_valor_final("valor-final-1", "n" * 80))
    r = guardar(a_admin, org_a, nuevo_id(), largo, 0)
    rev("un nombre de exactamente 80 caracteres cabe", r.status_code == 200, r.text[:120])
    largo["nodos"][-1]["data"]["nombre"] = "n" * 81
    rev("uno de 81 no", guardar(a_admin, org_a, nuevo_id(), largo, 0).status_code == 400)
    r = guardar(a_admin, org_a, nuevo_id(), {**proceso("Texto de 60"), "nodos": proceso("x")["nodos"] + [nodo_resultado("s" * 60)]}, 0)
    rev("una palabra del resultado de exactamente 60 caracteres cabe", r.status_code == 200, r.text[:120])
    rev("una de 61 no", guardar(a_admin, org_a, nuevo_id(), {**proceso("Texto de 61"), "nodos": proceso("x")["nodos"] + [nodo_resultado("s" * 61)]}, 0).status_code == 400)
    for caso in ("si", "no", "sin"):
        incompleto = proceso("Resultado incompleto")
        nodo = nodo_resultado()
        del nodo["data"][caso]
        incompleto["nodos"].append(nodo)
        rev(f"al resultado le falta la palabra «{caso}»: no se guarda", guardar(a_admin, org_a, nuevo_id(), incompleto, 0).status_code == 400)
        vacio = proceso("Resultado vacío")
        nodo = nodo_resultado()
        nodo["data"][caso] = "   "
        vacio["nodos"].append(nodo)
        rev(f"la palabra «{caso}» en blanco: no se guarda", guardar(a_admin, org_a, nuevo_id(), vacio, 0).status_code == 400)
    mala = proceso("Palabra que no es texto")
    mala["nodos"].append(nodo_resultado(si=7))
    rev("una palabra del resultado que no es texto: no se guarda", guardar(a_admin, org_a, nuevo_id(), mala, 0).status_code == 400)
    dos = proceso("Dos resultados")
    dos["nodos"] += [nodo_resultado(nodo_id="resultado-final"), nodo_resultado("Otra", nodo_id="resultado-final-2", y=200)]
    r = guardar(a_admin, org_a, nuevo_id(), dos, 0)
    rev("dos nodos de resultado en un proceso: no se guarda, y dice que es uno solo", r.status_code == 400 and "solo puede tener un resultado" in r.text, r.text[:200])
    dos_valores = proceso("Dos valores finales")
    dos_valores["nodos"] += [nodo_valor_final("valor-final-1", "Igual"), nodo_valor_final("valor-final-2", "Igual", y=100)]
    rev("en cambio puede haber todos los valores finales que hagan falta, aun con el mismo nombre (el lienzo avisa, el servidor no se mete)", guardar(a_admin, org_a, nuevo_id(), dos_valores, 0).status_code == 200)
    c.delete(f"/procesos/{p7}", params={"tenant": org_a}, headers=cab(a_admin))

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

    if not SQL:  # en SQL no hay archivo que cortar
        titulo("12 · Resiste un corte a media escritura")
        ruta = RAIZ / ".registro" / "procesos.jsonl"
        with open(ruta, "ab") as f:
            f.write(b'{"op": "guardar", "id": "proceso-roto')
        antes = ids(a_admin, org_a)
        rev("una última línea rota se ignora al leer", ids(a_admin, org_a) == antes and len(antes) >= 3)
        n = nuevo_id()
        r = guardar(a_admin, org_a, n, proceso("Después del corte"), 0)
        rev("y el guardado siguiente no queda pegado a ella", r.status_code == 200 and n in ids(a_admin, org_a))

    titulo("13 · Un proceso ya no se liga a ningún tipo (Moibe, 2026-11-05: se quitaron los expedientes virtuales)")
    from servicios import procesos as servicio_procesos  # noqa: E402

    rev("ningún proceso guardado trae `tipoExpedienteVirtualId`", all("tipoExpedienteVirtualId" not in x["datos"] for x in listar(a_admin, org_a).json()["procesos"]))
    # Lo que un cliente viejo todavía puede mandar. Antes de esta decisión lo válido se guardaba y lo demás era 400; ahora TODO se ignora y nada falla.
    mandados = {
        "un tipo que parece válido": "tipo-expediente-" + "a" * 32,
        "un tipo que no existe": "tipo-expediente-" + "0" * 32,
        "algo sin forma de id": "cualquier-cosa",
        "un número": 123,
        "null": None,
    }
    for que, valor in mandados.items():
        r = guardar(a_admin, org_a, nuevo_id(), proceso(f"De un cliente viejo con {que}", tipoExpedienteVirtualId=valor), 0)
        rev(f"un cliente viejo que manda `tipoExpedienteVirtualId` ({que}): se guarda igual (200) y la respuesta no lo trae",
            r.status_code == 200 and "tipoExpedienteVirtualId" not in r.json()["datos"] and r.json()["revision"] == 1, f"{r.status_code} {r.text[:200]}")
    listados = listar(a_admin, org_a).json()["procesos"]
    rev("y al listarlos tampoco: ningún proceso lo trae, ni siquiera `null`", all("tipoExpedienteVirtualId" not in x["datos"] for x in listados) and sum(1 for x in listados if x["datos"]["nombre"].startswith("De un cliente viejo")) == len(mandados))
    pl = nuevo_id()
    guardar(a_admin, org_a, pl, proceso("Validar crédito"), 0)
    r = guardar(a_admin, org_a, pl, proceso("Validar crédito", tipoExpedienteVirtualId="tipo-expediente-" + "b" * 32), 1)
    rev("actualizar un proceso mandando el campo: se ignora igual, la revisión sube y el dibujo no cambia",
        r.status_code == 200 and r.json()["revision"] == 2 and "tipoExpedienteVirtualId" not in r.json()["datos"] and r.json()["datos"]["nodos"] == proceso("Validar crédito")["nodos"], r.text[:200])
    rev("abrir ese proceso por la lista lo devuelve sin el campo", "tipoExpedienteVirtualId" not in next(x for x in listar(a_admin, org_a).json()["procesos"] if x["id"] == pl)["datos"])
    # Lo que quedó GUARDADO (no solo lo que se devuelve): el campo no viaja al JSON del proceso.
    if SQL:
        con = BASE_SQL["conectar"]("usrNexus", BASE_SQL["app"], BASE_SQL["base"])
        con_campo = con.cursor().execute("SELECT count(*) FROM nexus.processes WHERE dataJson LIKE '%tipoExpedienteVirtualId%'").fetchone()[0]
        rev("en la base, ningún `dataJson` guarda el campo", con_campo == 0, f"{con_campo} procesos lo guardaron")
        # Una fila con la liga DENTRO de su JSON: solo se puede por SQL directo (desde la 0005 la liga iba en su propia columna y la 0012 la borró), pero el
        # contrato dice que el campo NUNCA se devuelve, y eso no puede depender de cómo llegó la fila.
        legado = nuevo_id()
        antiguo = {"nombre": "Con la liga dentro de su JSON", "descripcion": "", "nodos": [], "aristas": [], "activo": False, "tipoExpedienteVirtualId": "tipo-expediente-" + "c" * 32}
        con.cursor().execute("INSERT INTO nexus.processes (publicId, tenant, revision, dataJson, createdBy) VALUES (?, ?, 1, ?, 'x')", legado, org_a, json.dumps(antiguo))
        visto = next(x for x in listar(a_admin, org_a).json()["procesos"] if x["id"] == legado)
        rev("una fila con la liga dentro de su JSON (solo por SQL directo) se lee sin ella y con lo demás intacto",
            "tipoExpedienteVirtualId" not in visto["datos"] and visto["datos"]["nombre"] == "Con la liga dentro de su JSON" and visto["datos"]["activo"] is False, str(visto["datos"]))
        r = guardar(a_admin, org_a, legado, {**visto["datos"], "nombre": "Con la liga dentro de su JSON (editado)"}, 1)
        rev("y al editarla se vuelve a guardar sin ella", r.status_code == 200 and "tipoExpedienteVirtualId" not in r.json()["datos"], r.text[:200])
        quedan = con.cursor().execute("SELECT count(*) FROM nexus.processes WHERE publicId = ? AND dataJson LIKE '%tipoExpedienteVirtualId%'", legado).fetchone()[0]
        rev("y en la base ya no la guarda", quedan == 0, f"{quedan}")
        con.close()
        c.delete(f"/procesos/{legado}", params={"tenant": org_a}, headers=cab(a_admin))
    else:
        eventos = [json.loads(x) for x in (RAIZ / ".registro" / "procesos.jsonl").read_text(encoding="utf-8").splitlines() if x.strip().endswith("}")]
        rev("en el registro, ningún evento `guardar` de lo anterior lleva el campo dentro de `datos`",
            not any("tipoExpedienteVirtualId" in e.get("datos", {}) for e in eventos if e.get("op") == "guardar"), str([e["datos"].get("nombre") for e in eventos if "tipoExpedienteVirtualId" in e.get("datos", {})]))
        # Un proceso que el almacén guardó ANTES del 2026-11-05, con la liga dentro de sus datos: se lee sin ella (y con lo demás intacto).
        legado = nuevo_id()
        antiguo = {"nombre": "Guardado antes del cambio", "descripcion": "", "nodos": [], "aristas": [], "activo": False, "tipoExpedienteVirtualId": "tipo-expediente-" + "c" * 32}
        with open(RAIZ / ".registro" / "procesos.jsonl", "ab") as f:
            f.write((json.dumps({"op": "guardar", "id": legado, "tenant": org_a, "revision": 1, "datos": antiguo, "por": "x", "en": "2026-10-30T00:00:00Z"}) + "\n").encode("utf-8"))
        visto = next(x for x in listar(a_admin, org_a).json()["procesos"] if x["id"] == legado)
        rev("un proceso que el almacén guardó ANTES del cambio (con la liga en sus datos) se lee sin ella y con lo demás intacto",
            "tipoExpedienteVirtualId" not in visto["datos"] and visto["datos"]["nombre"] == "Guardado antes del cambio" and visto["datos"]["activo"] is False, str(visto["datos"]))
        r = guardar(a_admin, org_a, legado, {**visto["datos"], "nombre": "Guardado antes del cambio (editado)"}, 1)
        rev("y al editarlo se vuelve a guardar sin ella", r.status_code == 200 and "tipoExpedienteVirtualId" not in r.json()["datos"], r.text[:200])
        c.delete(f"/procesos/{legado}", params={"tenant": org_a}, headers=cab(a_admin))
    # `resultado_conectado` es lo que hace EJECUTABLE a un proceso (lo usan `/ejecuciones/procesos` y `/ejecuciones/preparar`).
    con_resultado = proceso("Con resultado")
    con_resultado["nodos"].append({"id": "resultado-final", "type": "resultado-final", "position": {"x": 1000, "y": 0}, "data": {"si": "Sí", "no": "No", "sin": "Sin datos"}})
    con_resultado["aristas"].append(cable("operador-1", "resultado", "resultado-final", "a"))
    rev("`resultado_conectado`: pide el nodo, la regla (no un campo) y su punto «a»", servicio_procesos.resultado_conectado(con_resultado) and not servicio_procesos.resultado_conectado(proceso("Sin resultado"))
        and not servicio_procesos.resultado_conectado({**con_resultado, "aristas": [a for a in con_resultado["aristas"] if a["target"] != "resultado-final"]})
        and not servicio_procesos.resultado_conectado({**con_resultado, "aristas": [cable("tipo-acta", "rfc", "resultado-final", "a")]})
        and not servicio_procesos.resultado_conectado({**con_resultado, "aristas": [cable("operador-1", "resultado", "resultado-final", "b")]}))
    rev("el servicio ya no sabe de tipos de expediente: sin `de_tipo` y sin la liga", not hasattr(servicio_procesos, "de_tipo") and not hasattr(servicio_procesos, "tipos_expediente"))
    titulo("8d · (2026-11-04, NEX-529 y NEX-542) el nombre es único en la organización; el nodo del tipo recuerda su versión")
    mio = nuevo_id()
    r = guardar(a_admin, org_a, mio, proceso("Nombre único"), 0)
    rev("un nombre que nadie tiene se guarda", r.status_code == 200, r.text[:160])
    r = guardar(a_admin, org_a, nuevo_id(), proceso("Nombre único"), 0)
    rev("crear otro con el MISMO nombre: 409 `ya_existe`, con su mensaje", r.status_code == 409 and r.json()["detail"]["codigo"] == "ya_existe" and "ese nombre" in r.json()["detail"]["mensaje"], r.text[:200])
    r = guardar(a_admin, org_a, nuevo_id(), proceso("  NOMBRE   único "), 0)
    rev("…también con otras mayúsculas y espacios de más", r.status_code == 409 and r.json()["detail"]["codigo"] == "ya_existe", r.text[:200])
    rev("…y no se creó nada (en la lista sigue uno solo con ese nombre)", [x["datos"]["nombre"] for x in listar(a_admin, org_a).json()["procesos"]].count("Nombre único") == 1)
    r = guardar(b_admin, org_b, nuevo_id(), proceso("Nombre único"), 0)
    rev("otra organización SÍ puede usar ese nombre (la regla es por organización)", r.status_code == 200, r.text[:160])
    otro_nombre = nuevo_id()
    guardar(a_admin, org_a, otro_nombre, proceso("Otro nombre distinto"), 0)
    r = guardar(a_admin, org_a, otro_nombre, proceso("nombre único"), 1)
    rev("renombrar a uno que ya tiene OTRO proceso: 409 `ya_existe` y el proceso se queda como estaba", r.status_code == 409 and r.json()["detail"]["codigo"] == "ya_existe" and next(x for x in listar(a_admin, org_a).json()["procesos"] if x["id"] == otro_nombre)["datos"]["nombre"] == "Otro nombre distinto", r.text[:200])
    r = guardar(a_admin, org_a, mio, proceso("NOMBRE ÚNICO".replace("Ú", "ú").replace("NOMBRE ", "Nombre ")), 1)
    rev("cambiar solo las mayúsculas del PROPIO nombre sí se puede (no choca consigo mismo)", r.status_code == 200, r.text[:160])
    r = guardar(a_admin, org_a, mio, proceso("Nombre único", activo=False), 2)
    rev("guardar sin cambiar el nombre (activar o desactivar) no se revisa ni falla", r.status_code == 200 and r.json()["datos"]["activo"] is False, r.text[:160])
    r = guardar(a_admin, org_a, otro_nombre, proceso("Otro nombre libre"), 1)
    rev("renombrar a uno libre funciona, y el nombre anterior queda libre", r.status_code == 200 and guardar(a_admin, org_a, nuevo_id(), proceso("Otro nombre distinto"), 0).status_code == 200, r.text[:160])
    rev("sin nombre sigue siendo 400 (no es un conflicto)", guardar(a_admin, org_a, nuevo_id(), proceso("  "), 0).status_code == 400)

    con_version = proceso("Con versión del tipo")
    con_version["nodos"][0]["data"]["version"] = 3
    pv = nuevo_id()
    r = guardar(a_admin, org_a, pv, con_version, 0)
    rev("el nodo de un tipo documental guarda la versión con la que se armó", r.status_code == 200 and r.json()["datos"]["nodos"][0]["data"].get("version") == 3, r.text[:200])
    rev("…y los nodos sin versión (procesos de antes) se guardan igual, sin inventarla", "version" not in r.json()["datos"]["nodos"][1]["data"])
    for malo in (-1, 1.5, "3", True, 10**9):
        d = proceso(f"Versión mala {malo!r}")
        d["nodos"][0]["data"]["version"] = malo
        rev(f"una versión que no es un entero razonable ({malo!r}) se rechaza con 400", guardar(a_admin, org_a, nuevo_id(), d, 0).status_code == 400)
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
