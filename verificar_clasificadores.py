"""¿Cada organización tiene SU clasificador de Document AI, y nunca el de otra?

    .venv/Scripts/python verificar_clasificadores.py

OFFLINE: Document AI se sustituye por un Google falso que guarda los procesadores en un diccionario. Antes de 2026-10-26 había UN
clasificador para todo el sistema (armado con los tipos activos de todas las organizaciones): un documento de una podía caer en un
tipo de otra, y lo que una guardaba cambiaba el clasificador de las demás. Aquí se vigila lo que lo reemplazó:

- el registro (`servicios/clasificadores.py`): el id de cada organización, y el espacio de plataforma con el del `.env`;
- `sincronizar_clasificador`: crea el procesador de la organización la primera vez, lo reutiliza después (sin pagar un `list`), adopta
  el que quedó a medias, recrea el que se borró a mano, no confunde el nombre de plataforma con el de una organización y no crea dos
  cuando le llegan dos peticiones a la vez;
- clasificar usa el procesador de la organización que pide (y `SinClasificador` si no tiene);
- las rutas piden `tenant` y sesión: quien no es de la organización recibe 403 y a Google no se le llama;
- la limpieza de huérfanos ve como huérfano el clasificador de una organización que ya no existe, y como a salvo el de una que sí.
"""

import asyncio
import json
import os
import shutil
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-clasificadores-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from errores import SinClasificador  # noqa: E402
from routers import ia as router_ia  # noqa: E402
from servicios import auth, clasificadores, ia, procesadores as svc, procesadores_huerfanos, usuarios  # noqa: E402
from servicios.procesadores import DocumentAIError  # noqa: E402

fallos = 0


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


# ── El Google falso ─────────────────────────────────────────────────────────────────────────────────────────────────────────
procesadores_en_google: dict[str, dict] = {}  # id → {"name", "displayName", "esquema"}
llamadas_a_google: list[tuple[str, str]] = []
siguiente = [0]
PADRE = svc._PADRE


async def pedir_falso(cliente, metodo, api, ruta, **kw):
    llamadas_a_google.append((metodo, ruta))
    if metodo == "GET" and ruta == f"{PADRE}/processors":
        return {"processors": [{"name": p["name"], "displayName": p["displayName"]} for p in procesadores_en_google.values()]}
    if metodo == "POST" and ruta == f"{PADRE}/processors":
        await asyncio.sleep(0)  # deja pasar a la otra petición: sin candado, las dos creaban
        siguiente[0] += 1
        pid = f"clas-{siguiente[0]}"
        p = {"name": f"{PADRE}/processors/{pid}", "displayName": kw["json"]["displayName"], "esquema": None}
        procesadores_en_google[pid] = p
        return {"name": p["name"], "displayName": p["displayName"]}
    if metodo == "GET" and "/processors/" in ruta and "/dataset" not in ruta:
        pid = ruta.rsplit("/", 1)[-1]
        if pid not in procesadores_en_google:
            raise DocumentAIError(f"Document AI respondió 404 en GET {ruta}", status_code=404)
        p = procesadores_en_google[pid]
        return {"name": p["name"], "displayName": p["displayName"], "defaultProcessorVersion": f"{p['name']}/processorVersions/pretrained-classifier-v1.5"}
    if metodo == "PATCH" and ruta.endswith("/dataset/datasetSchema"):
        pid = ruta.split("/processors/")[1].split("/")[0]
        procesadores_en_google[pid]["esquema"] = kw["json"]["documentSchema"]
        return {}
    if metodo == "PATCH" and ruta.endswith("/dataset"):
        return {}
    if metodo == "GET" and ruta.endswith("/dataset/datasetSchema"):
        pid = ruta.split("/processors/")[1].split("/")[0]
        return {"documentSchema": procesadores_en_google[pid]["esquema"] or {}}
    raise AssertionError(f"Llamada inesperada a Google: {metodo} {ruta}")


def de_google(display: str) -> list[dict]:
    return [p for p in procesadores_en_google.values() if p["displayName"] == display]


def categorias(pid: str) -> list[str]:
    return sorted(t["name"] for t in (procesadores_en_google[pid]["esquema"] or {}).get("entityTypes", []) if t["name"] != "otro")


svc._pedir = pedir_falso  # type: ignore[assignment]
svc.DOCAI_PROJECT_ID = svc.DOCAI_PROJECT_ID or "proyecto-de-prueba"
clasificadores.DOCAI_CLASIFICADOR_ID = "clas-legado"
procesadores_en_google["clas-legado"] = {"name": f"{PADRE}/processors/clas-legado", "displayName": "nexusdoc-clasificador", "esquema": None}


def tipo(id_: str, nombre: str) -> dict:
    return {"id": id_, "nombre": nombre, "descripcion": f"Descripción de {nombre}"}


try:
    titulo("0 · Montaje: dos organizaciones y el administrador de plataforma")
    temporal = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal, "Cobre-Lento-Azul-47!")
    org_a, a_admin = crear_organizacion(bearer_super, "Alfa", "rita@ejemplo.com", "Rumbo-Sereno-Azul-47!")
    org_b, b_admin = crear_organizacion(bearer_super, "Beta", "beto@ejemplo.com", "Rumbo-Sereno-Rojo-48!")
    rev("hay dos organizaciones distintas", org_a != org_b)

    titulo("1 · El registro: quién usa qué clasificador")
    rev("el nombre de una organización lleva su guid detrás de `--`", clasificadores.nombre_visible(org_a) == f"nexusdoc-clasificador--{org_a}")
    rev("el del espacio de plataforma conserva el nombre de siempre", clasificadores.nombre_visible("demo") == "nexusdoc-clasificador")
    rev("una organización sin clasificador todavía: ninguno", clasificadores.id_de(org_a) is None)
    rev("el espacio de plataforma usa el del `.env`", clasificadores.id_de("demo") == "clas-legado")
    rev("y cualquier otro tenant que no es organización, también", clasificadores.id_de("cli-acme") == "clas-legado")
    try:
        clasificadores.id_de("../x")
        rev("un tenant con caracteres inválidos se rechaza", False)
    except ValueError:
        rev("un tenant con caracteres inválidos se rechaza", True)
    clasificadores.asignar("demo", "otro-id")
    rev("asignarle uno al espacio de plataforma no hace nada (su id es el del `.env`)", clasificadores.id_de("demo") == "clas-legado" and not (RAIZ / ".registro" / "clasificadores.jsonl").exists())

    titulo("2 · Sincronizar crea el procesador de la organización la primera vez")
    tipos_a = [tipo("tipo-aaa", "Póliza"), tipo("tipo-bbb", "INE")]
    llamadas_a_google.clear()
    r1 = asyncio.run(svc.sincronizar_clasificador(tipos_a, org_a))
    rev("lo crea con el nombre de SU organización", r1["creado"] is True and len(de_google(f"nexusdoc-clasificador--{org_a}")) == 1, str(r1))
    rev("y su esquema trae sus dos tipos (más `otro`)", categorias(r1["procesadorId"]) == ["tipo-aaa", "tipo-bbb"], str(categorias(r1["procesadorId"])))
    rev("queda anotado en el registro", clasificadores.id_de(org_a) == r1["procesadorId"])
    rev("el de plataforma ni se tocó", procesadores_en_google["clas-legado"]["esquema"] is None)

    titulo("3 · La segunda vez lo reutiliza por su id, sin listar todos los procesadores del proyecto")
    llamadas_a_google.clear()
    r2 = asyncio.run(svc.sincronizar_clasificador([tipo("tipo-aaa", "Póliza")], org_a))
    rev("mismo procesador, `creado` falso", r2["procesadorId"] == r1["procesadorId"] and r2["creado"] is False, str(r2))
    rev("el esquema se REEMPLAZA: ya solo trae el tipo que quedó", categorias(r2["procesadorId"]) == ["tipo-aaa"])
    rev("y el registro no repite el evento: un solo `asignado` para esa organización tras dos sincronizaciones",
        sum(1 for e in clasificadores.registro.eventos(clasificadores.ARCHIVO) if e.get("tenant") == org_a) == 1)
    rev("sin un `list` de todos los procesadores ni un POST",("GET", f"{PADRE}/processors") not in llamadas_a_google and not any(m == "POST" for m, _ in llamadas_a_google), str(llamadas_a_google))

    titulo("4 · Cada organización, el suyo: lo de una no toca lo de la otra")
    r_b = asyncio.run(svc.sincronizar_clasificador([tipo("tipo-ccc", "Acta")], org_b))
    rev("B tiene OTRO procesador", r_b["procesadorId"] != r1["procesadorId"] and de_google(f"nexusdoc-clasificador--{org_b}")[0]["name"].endswith(r_b["procesadorId"]))
    rev("con SOLO sus categorías", categorias(r_b["procesadorId"]) == ["tipo-ccc"])
    rev("y el de A sigue con las suyas", categorias(r1["procesadorId"]) == ["tipo-aaa"])

    titulo("5 · El espacio de plataforma sigue con el procesador del `.env`")
    llamadas_a_google.clear()
    r_p = asyncio.run(svc.sincronizar_clasificador([tipo("tipo-ddd", "Demo")], "demo"))
    rev("reutiliza el del `.env` sin crear nada", r_p["procesadorId"] == "clas-legado" and r_p["creado"] is False and not any(m == "POST" for m, _ in llamadas_a_google), str(r_p))
    rev("y su esquema es solo el suyo", categorias("clas-legado") == ["tipo-ddd"])
    rev("no deja nada anotado en el registro para plataforma", clasificadores._asignados().get("demo") is None)

    titulo("6 · Adopta el que un intento anterior dejó creado a medias")
    org_c, _ = crear_organizacion(bearer_super, "Gamma", "gabi@ejemplo.com", "Rumbo-Sereno-Verde-49!")
    huerfano = {"name": f"{PADRE}/processors/clas-a-medias", "displayName": f"nexusdoc-clasificador--{org_c}", "esquema": None}
    procesadores_en_google["clas-a-medias"] = huerfano
    llamadas_a_google.clear()
    r_c = asyncio.run(svc.sincronizar_clasificador([tipo("tipo-eee", "Licencia")], org_c))
    rev("lo encuentra por su nombre EXACTO y lo adopta (no crea otro)", r_c["procesadorId"] == "clas-a-medias" and r_c["creado"] is False and len(de_google(f"nexusdoc-clasificador--{org_c}")) == 1, str(r_c))
    rev("y lo anota", clasificadores.id_de(org_c) == "clas-a-medias")

    titulo("7 · Si lo borraron a mano en la consola de Google, se vuelve a crear")
    del procesadores_en_google[r1["procesadorId"]]
    r_nuevo = asyncio.run(svc.sincronizar_clasificador([tipo("tipo-aaa", "Póliza")], org_a))
    rev("crea uno nuevo (no truena con el 404)", r_nuevo["creado"] is True and r_nuevo["procesadorId"] != r1["procesadorId"], str(r_nuevo))
    rev("y el registro apunta al nuevo", clasificadores.id_de(org_a) == r_nuevo["procesadorId"])

    titulo("8 · Dos peticiones a la vez para una organización nueva crean UN solo clasificador")
    org_d, _ = crear_organizacion(bearer_super, "Delta", "dani@ejemplo.com", "Rumbo-Sereno-Gris-50!")

    async def dos_a_la_vez():
        return await asyncio.gather(
            svc.sincronizar_clasificador([tipo("tipo-fff", "Recibo")], org_d), svc.sincronizar_clasificador([tipo("tipo-fff", "Recibo")], org_d)
        )

    ra, rb = asyncio.run(dos_a_la_vez())
    rev("un solo procesador en Google con su nombre", len(de_google(f"nexusdoc-clasificador--{org_d}")) == 1, str([p["name"] for p in de_google(f"nexusdoc-clasificador--{org_d}")]))
    rev("y las dos respuestas hablan del mismo", ra["procesadorId"] == rb["procesadorId"])

    titulo("9 · El nombre de plataforma NO se confunde con el de una organización")
    clasificadores.DOCAI_CLASIFICADOR_ID = ""
    for pid in [k for k, p in procesadores_en_google.items() if p["displayName"] == "nexusdoc-clasificador"]:
        del procesadores_en_google[pid]
    r_sin_env = asyncio.run(svc.sincronizar_clasificador([tipo("tipo-ddd", "Demo")], "demo"))
    rev("sin id en el `.env` y habiendo clasificadores de organizaciones, busca el EXACTO y crea el de plataforma",
        r_sin_env["creado"] is True and len(de_google("nexusdoc-clasificador")) == 1, str(r_sin_env))
    clasificadores.DOCAI_CLASIFICADOR_ID = "clas-legado"
    procesadores_en_google["clas-legado"] = {"name": f"{PADRE}/processors/clas-legado", "displayName": "nexusdoc-clasificador", "esquema": None}

    titulo("10 · Ver el clasificador de cada organización")
    ver_a = asyncio.run(svc.consultar_esquema_clasificador(org_a))
    rev("devuelve el procesador de ESA organización y sus categorías", ver_a["procesadorId"] == r_nuevo["procesadorId"] and [c_["name"] for c_ in ver_a["categorias"] if c_["name"] != "otro"] == ["tipo-aaa"], str(ver_a))
    org_e, e_admin = crear_organizacion(bearer_super, "Epsilon", "eli@ejemplo.com", "Rumbo-Sereno-Rosa-51!")
    try:
        asyncio.run(svc.consultar_esquema_clasificador(org_e))
        rev("una organización sin clasificador: SinClasificador", False)
    except SinClasificador as exc:
        rev("una organización sin clasificador: SinClasificador, con una frase que se puede mostrar", "todavía no tiene clasificador" in str(exc))

    titulo("11 · Clasificar usa el procesador de la organización que pide")
    usados: list[str] = []

    async def procesar_falso(procesador_id, contenido, mime, version, opciones=None, *a, **k):
        usados.append(procesador_id)
        return {"document": {"entities": [{"type": "tipo-aaa", "confidence": 0.9}]}}

    original = ia._procesar
    ia._procesar = procesar_falso  # type: ignore[assignment]
    try:
        asyncio.run(ia.clasificar_documento(b"x", "application/pdf", org_a))
        asyncio.run(ia.clasificar_documento(b"x", "application/pdf", org_b))
        asyncio.run(ia.clasificar_documento(b"x", "application/pdf", "demo"))
        rev("A usa el suyo, B el suyo y plataforma el del `.env`", usados == [r_nuevo["procesadorId"], r_b["procesadorId"], "clas-legado"], str(usados))
        usados.clear()
        try:
            asyncio.run(ia.clasificar_documento(b"x", "application/pdf", org_e))
            rev("sin clasificador: SinClasificador y NO se llama a Google", False)
        except SinClasificador as exc:
            rev("sin clasificador: SinClasificador y NO se llama a Google", usados == [])
            rev("y el texto dice QUÉ hacer y DÓNDE: crear y activar al menos un tipo documental, en Configuración",
                "ningún tipo documental activo" in str(exc) and "Crea y activa al menos uno" in str(exc) and "Configuración" in str(exc), str(exc))
            rev("sin nombrar piezas internas (nada de «clasificador» ni de Google)", "clasificador" not in str(exc).lower() and "google" not in str(exc).lower(), str(exc))
    finally:
        ia._procesar = original  # type: ignore[assignment]

    titulo("12 · La ruta POST /ia/clasificar pide organización y sesión")
    recibidos: list[str] = []

    async def clasificar_falso(contenido, mime, tenant):
        recibidos.append(tenant)
        if tenant == org_e:
            raise SinClasificador("Todavía no se pueden clasificar documentos: tu organización no tiene ningún tipo documental activo. Crea y activa al menos uno en Configuración, Tipos documentales, y vuelve a intentarlo.")
        return {"categoria": "tipo-aaa", "confianza": 90}

    router_ia.ia.clasificar_documento = clasificar_falso  # type: ignore[assignment]
    pdf = {"archivo": ("a.pdf", b"%PDF-1.4 prueba", "application/pdf")}
    r = c.post("/ia/clasificar", params={"tenant": org_a}, files=pdf, headers=cab(a_admin))
    rev("un miembro de la organización: 200 y a la función le llega SU tenant", r.status_code == 200 and recibidos == [org_a], r.text[:200])
    recibidos.clear()
    r = c.post("/ia/clasificar", params={"tenant": org_a}, files=pdf, headers=cab(b_admin))
    rev("alguien de OTRA organización: 403 sin_acceso y no llega a clasificar", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso" and not recibidos, r.text[:200])
    rev("sin sesión (solo la llave de servicio): 401", c.post("/ia/clasificar", params={"tenant": org_a}, files=pdf, headers=cab()).status_code == 401 and not recibidos)
    rev("sin decir de qué organización: 422", c.post("/ia/clasificar", files=pdf, headers=cab(a_admin)).status_code == 422)
    rev("el administrador de plataforma NO puede clasificar con el clasificador de una organización", c.post("/ia/clasificar", params={"tenant": org_a}, files=pdf, headers=cab(bearer_super)).status_code == 403 and not recibidos)
    rev("pero sí con el de su espacio", c.post("/ia/clasificar", params={"tenant": "demo"}, files=pdf, headers=cab(bearer_super)).status_code == 200 and recibidos == ["demo"])
    r = c.post("/ia/clasificar", params={"tenant": org_e}, files=pdf, headers=cab(e_admin))
    rev("una organización sin clasificador: 409 con una frase que se puede mostrar", r.status_code == 409 and "ningún tipo documental activo" in r.json()["detail"], r.text[:200])

    titulo("13 · La limpieza de huérfanos y los clasificadores de organización")
    en_google = [
        {"id": "k1", "displayName": f"nexusdoc-clasificador--{org_a}", "tipo": "CUSTOM_CLASSIFICATION_PROCESSOR", "creado": "2026-10-26"},
        {"id": "k2", "displayName": "nexusdoc-clasificador--una-organizacion-purgada", "tipo": "CUSTOM_CLASSIFICATION_PROCESSOR", "creado": "2026-10-20"},
        {"id": "k3", "displayName": "nexusdoc-clasificador", "tipo": "CUSTOM_CLASSIFICATION_PROCESSOR", "creado": "2026-09-06"},
        {"id": "k4", "displayName": "nexusdoc--tipo-x--v1--huerfano", "tipo": "CUSTOM_EXTRACTION_PROCESSOR", "creado": "2026-10-01"},
    ]
    rep = procesadores_huerfanos.clasificar(en_google, {}, set(), {org_a, org_b})
    rev("el clasificador de una organización que EXISTE queda a salvo", "k1" in [p["id"] for p in rep["protegidos"]])
    rev("el de una que ya NO existe es huérfano", "k2" in [p["id"] for p in rep["huerfanos"]], str(rep))
    rev("el de plataforma sigue protegido y el extractor sin dueño sigue siendo huérfano", "k3" in [p["id"] for p in rep["protegidos"]] and "k4" in [p["id"] for p in rep["huerfanos"]])
    rev("cada uno está en UNA sola lista", sum(len(v) for v in rep.values()) == len(en_google))
    rep_sin_lista = procesadores_huerfanos.clasificar(en_google, {}, set())
    rev("sin saber cuáles organizaciones existen, TODOS los de organización quedan a salvo", {"k1", "k2"} <= {p["id"] for p in rep_sin_lista["protegidos"]})
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
