"""¿Las rutas de `/procesadores` existen, aceptan lo que deben y traducen bien sus errores?

    .venv/Scripts/python verificar_procesadores.py

OFFLINE y sin tocar nada tuyo, como los demás `verificar_*`: Document AI se sustituye por dobles que anotan lo que les llegaría.

Nació el 2026-10-16 de un error propio. Al pasar el clasificador a salir del registro del servidor (2026-10-15) un parche cortó
de más y se llevó la ruta `POST /procesadores/activar` y el ayudante `_mensaje_para`; el cambio se desplegó y nada lo detectó, porque
ninguna prueba tocaba esa ruta. Lo que se vigila:

- el INVENTARIO de rutas: que ninguna desaparezca sin que una prueba se ponga roja;
- activar: lo que recibe (campos sueltos y GRUPOS con subcampos), lo que le manda al generador de esquemas y qué rechaza con 422;
- los errores de Google: un 4xx es «revisa lo que mandaste», todo lo demás es «no respondió», y ninguno se vuelve un 500.
"""

import os
import shutil
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-procesadores-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

import verificacion_comun  # noqa: E402

from app import app  # noqa: E402
from routers import procesadores as router  # noqa: E402
from servicios.procesadores import DocumentAIError  # noqa: E402

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + extra if extra and not ok else ''}")
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


c = verificacion_comun.cliente_con_sesion_de_plataforma(app, SERVICIO)
LLAVE = {"x-api-key": SERVICIO}

try:
    titulo("1 · Inventario: ninguna ruta de /procesadores desaparece")
    rutas = {(metodo.upper(), ruta) for ruta, ops in app.openapi()["paths"].items() for metodo in ops if ruta.startswith("/procesadores")}
    esperadas = {
        ("POST", "/procesadores/activar"),
        # 2026-10-29: «Generar a partir de un documento» del paso 2 (ver verificar_generar_campos.py).
        ("POST", "/procesadores/generar-campos"),
        ("POST", "/procesadores/clasificador/sincronizar"),
        ("POST", "/procesadores/clasificador/asegurar"),
        ("GET", "/procesadores/clasificador"),
        ("GET", "/procesadores/{procesador_id}"),
        ("DELETE", "/procesadores/{procesador_id}"),
    }
    for ruta in sorted(esperadas):
        rev(f"{ruta[0]} {ruta[1]} existe", ruta in rutas)
    rev("y no hay rutas de más sin que alguien lo haya decidido", rutas == esperadas, str(sorted(rutas - esperadas)))
    rev("el ayudante de errores sigue ahí", callable(getattr(router, "_mensaje_para", None)))

    titulo("2 · Activar: sin la llave de servicio, 401")
    rev("sin llave: 401", c.post("/procesadores/activar", json={"tenant": "demo", "id": "tipo-x", "nombre": "X", "campos": []}).status_code == 401)

    # El doble de Document AI: anota lo que le llega y contesta como contestaría Google.
    recibido: dict = {}
    respuesta_de_google: dict = {"procesadorId": "proc-1", "procesadorNombre": "projects/p/locations/us/processors/proc-1", "procesadorDisplayName": "nexusdoc--tipo-x--v1--x", "versionDefault": "pretrained-foundation-model-v1.5", "creado": True, "camposEnEsquema": 1}
    falla: list[Exception] = []

    async def activar_falso(id_tipo, nombre, esquema, version_actual=0):
        if falla:
            raise falla[0]
        recibido.update(id_tipo=id_tipo, nombre=nombre, esquema=esquema, version=version_actual)
        return respuesta_de_google

    router.activar_tipo_documental = activar_falso  # type: ignore[assignment]

    titulo("3 · Activar con campos sueltos: pasa tal cual y el esquema sale del generador")
    cuerpo = {
        "tenant": "demo",
        "id": "tipo-abc",
        "nombre": "Póliza",
        "descripcion": "Póliza de seguro",
        "version": 2,
        "campos": [
            {"nombre": "Número de póliza", "tipoDato": "texto", "descripcion": "El número", "obligatorio": True, "umbralConfianza": "50", "id": "campo-1"},
            {"nombre": "Tipo", "tipoDato": "lista", "valoresLista": ["A", "B"]},
        ],
    }
    r = c.post("/procesadores/activar", json=cuerpo, headers=LLAVE)
    rev("200 y contesta lo que dijo Google (con llaves de más del front, id y umbral, que no estorban)", r.status_code == 200 and r.json() == respuesta_de_google, r.text[:200])
    rev("al generador le llegan el id, el nombre y la versión ya publicada", (recibido.get("id_tipo"), recibido.get("nombre"), recibido.get("version")) == ("tipo-abc", "Póliza", 2))
    props = [p["name"] for p in recibido["esquema"]["entityTypes"][0]["properties"]]
    rev("el esquema trae los campos normalizados", props == ["numero_de_poliza", "tipo"], str(props))

    titulo("4 · Activar con un GRUPO: los subcampos viajan al esquema como un EntityType `object`")
    recibido.clear()
    cuerpo_grupo = {
        "tenant": "demo",
        "id": "tipo-acta",
        "nombre": "Acta",
        "campos": [
            {"nombre": "Denominación", "tipoDato": "texto"},
            {
                "nombre": "Accionistas",
                "tipoDato": "grupo",
                "descripcion": "Cada socio",
                "obligatorio": True,
                "cardinalidad": "multiple",
                "subcampos": [
                    {"nombre": "Nombre", "tipoDato": "texto", "obligatorio": True, "id": "sub-1"},
                    {"nombre": "Acciones", "tipoDato": "numero"},
                ],
            },
        ],
    }
    r = c.post("/procesadores/activar", json=cuerpo_grupo, headers=LLAVE)
    rev("200", r.status_code == 200, r.text[:200])
    esquema = recibido.get("esquema", {})
    grupo = next((t for t in esquema.get("entityTypes", []) if t["name"] == "accionistas"), {})
    prop = next((p for p in esquema.get("entityTypes", [{}])[0].get("properties", []) if p["name"] == "accionistas"), {})
    rev("la Property `accionistas` es REQUIRED_MULTIPLE y apunta a su tipo", prop.get("occurrenceType") == "REQUIRED_MULTIPLE" and prop.get("valueType") == "accionistas", str(prop))
    rev("y su EntityType es `object` con los dos subcampos en orden", grupo.get("baseTypes") == ["object"] and [h["name"] for h in grupo.get("properties", [])] == ["nombre", "acciones"], str(grupo))
    rev("la obligatoriedad del subcampo se respeta", grupo["properties"][0]["occurrenceType"] == "REQUIRED_ONCE" and grupo["properties"][1]["occurrenceType"] == "OPTIONAL_ONCE")

    titulo("4b · Instrucciones para la extracción (2026-10-30): llegan al esquema como el `documentPrompt` de Google")
    recibido.clear()
    r = c.post("/procesadores/activar", json=cuerpo, headers=LLAVE)
    rev("sin instrucciones: 200 y el esquema NO lleva `documentPrompt`", r.status_code == 200 and "documentPrompt" not in recibido["esquema"], r.text[:200])
    recibido.clear()
    r = c.post("/procesadores/activar", json={**cuerpo, "instrucciones": "  La persona que interesa es el titular.  "}, headers=LLAVE)
    rev(
        "con instrucciones: 200 y el esquema trae `documentPrompt`, sin los espacios de los extremos",
        r.status_code == 200 and recibido["esquema"].get("documentPrompt") == "La persona que interesa es el titular.",
        str(recibido.get("esquema", {}).get("documentPrompt")),
    )
    recibido.clear()
    r = c.post("/procesadores/activar", json={**cuerpo, "instrucciones": "x" * 1001}, headers=LLAVE)
    rev("de más de 1000 caracteres: 422 y a Google no se le llamó", r.status_code == 422 and not recibido, r.text[:160])
    rev("de justo 1000: se aceptan", c.post("/procesadores/activar", json={**cuerpo, "instrucciones": "x" * 1000}, headers=LLAVE).status_code == 200)

    titulo("5 · Activar rechaza lo que no se puede activar (422, sin llamar a Google)")
    recibido.clear()
    casos = {
        "sin campos": ({**cuerpo, "campos": []}, "al menos uno"),
        "un grupo sin subcampos": ({**cuerpo_grupo, "campos": [{"nombre": "Socios", "tipoDato": "grupo", "subcampos": []}]}, "Socios"),
        "un grupo dentro de un grupo": (
            {**cuerpo_grupo, "campos": [{"nombre": "Socios", "tipoDato": "grupo", "subcampos": [{"nombre": "Otro", "tipoDato": "grupo"}]}]},
            "Socios",
        ),
    }
    for nombre, (envio, trozo) in casos.items():
        r = c.post("/procesadores/activar", json=envio, headers=LLAVE)
        rev(f"{nombre}: 422 y dice cuál es el problema", r.status_code == 422 and trozo in r.text, r.text[:160])
    rev("y a Google no se le llamó ninguna de esas veces", not recibido)
    rev("un nombre de campo vacío (lo valida el modelo): 422", c.post("/procesadores/activar", json={**cuerpo, "campos": [{"nombre": ""}]}, headers=LLAVE).status_code == 422)
    rev("un subcampo sin nombre: 422", c.post("/procesadores/activar", json={**cuerpo_grupo, "campos": [{"nombre": "G", "tipoDato": "grupo", "subcampos": [{"nombre": ""}]}]}, headers=LLAVE).status_code == 422)

    titulo("6 · Los errores de Google no se vuelven un 500")
    falla.append(DocumentAIError("Document AI respondió 400 en PATCH x: esquema inválido, con el id de proyecto 62740263137", status_code=400))
    r = c.post("/procesadores/activar", json=cuerpo, headers=LLAVE)
    rev("un 4xx de Google: 502 con «revisa los nombres y tipos de los campos»", r.status_code == 502 and "Revisa los nombres y tipos" in r.json()["detail"], r.text[:200])
    rev("y el texto técnico (id de proyecto) NO llega al usuario", "62740263137" not in r.text and "PATCH" not in r.text)
    falla[0] = DocumentAIError("Document AI respondió 503", status_code=503)
    r = c.post("/procesadores/activar", json=cuerpo, headers=LLAVE)
    rev("un 5xx de Google: 502 «no se pudo contactar»", r.status_code == 502 and "El servicio de procesamiento no respondió" in r.json()["detail"] and "Document AI" not in r.json()["detail"], r.text[:200])
    falla[0] = RuntimeError("el polling se agotó a los 90 s")
    r = c.post("/procesadores/activar", json=cuerpo, headers=LLAVE)
    rev("un fallo que no es HTTP (polling agotado): 502 genérico", r.status_code == 502 and "No se pudo completar la activación" in r.json()["detail"] and "polling" not in r.text, r.text[:200])
    falla.clear()

    titulo("7 · Las demás rutas también traducen sus errores (usan el mismo ayudante)")

    async def consultar_falso(pid):
        raise DocumentAIError("Document AI respondió 500", status_code=500)

    async def eliminar_falso(pid):
        raise DocumentAIError("Document AI respondió 403", status_code=403)

    async def esquema_falso(tenant):
        raise DocumentAIError("Document AI respondió 404", status_code=404)

    router.consultar_procesador = consultar_falso  # type: ignore[assignment]
    router.eliminar_procesador = eliminar_falso  # type: ignore[assignment]
    router.consultar_esquema_clasificador = esquema_falso  # type: ignore[assignment]
    # Desde NEX-550 consultar y borrar piden `tenant` y que el procesador sea de un tipo de esa organización: `proc-1` lo es de uno de «demo».
    from servicios import tipos_documentales as registro_tipos

    registro_tipos.guardar("demo", "tipo-con-proc1", {"nombre": "Con proc-1", "estado": "activo", "procesadorId": "proc-1", "campos": []}, 0, "verificar")
    DEMO = {"tenant": "demo"}
    r = c.get("/procesadores/proc-1", params=DEMO, headers=LLAVE)
    rev("consultar con un 5xx: 502, no 500", r.status_code == 502, r.text[:160])
    r = c.delete("/procesadores/proc-1", params=DEMO, headers=LLAVE)
    rev("borrar con un 4xx: 502 con SU frase (no la de «revisa los campos»)", r.status_code == 502 and "No se pudo borrar este procesador" in r.json()["detail"] and "campos" not in r.json()["detail"] and "Document AI" not in r.json()["detail"], r.text[:200])
    # Desde el 2026-10-26 el clasificador es de cada organización: pide `tenant` y la sesión de quien administra (aquí, el administrador
    # de plataforma en su espacio, que es el que pone `verificacion_comun`). `verificar_tipos_documentales.py` prueba el aislamiento.
    r = c.get("/procesadores/clasificador", params={"tenant": "demo"}, headers=LLAVE)
    rev("ver el clasificador con un 4xx: 502 con su frase", r.status_code == 502 and "no permitió leer el esquema del clasificador" in r.json()["detail"], r.text[:200])

    async def inexistente(pid):
        raise DocumentAIError("Document AI respondió 404", status_code=404)

    router.consultar_procesador = inexistente  # type: ignore[assignment]
    router.eliminar_procesador = inexistente  # type: ignore[assignment]
    rev("consultar uno que ya no existe: 404", c.get("/procesadores/proc-1", params=DEMO, headers=LLAVE).status_code == 404)
    r = c.delete("/procesadores/proc-1", params=DEMO, headers=LLAVE)
    rev("borrar uno que ya no existe: éxito (el objetivo ya se cumplió)", r.status_code == 200 and r.json() == {"eliminado": True}, r.text[:160])

    titulo("8 · La estructura del procesador se lee de Google UNA vez y se invalida al subir un esquema nuevo")
    import asyncio

    from servicios import procesadores as svc

    peticiones: list[tuple[str, str]] = []
    esquema_en_google = {
        "documentSchema": {
            "entityTypes": [
                {"name": "custom_extraction_document_type", "baseTypes": ["document"], "properties": [{"name": "a", "valueType": "string", "occurrenceType": "REQUIRED_ONCE"}]}
            ]
        }
    }

    async def pedir_falso(cliente, metodo, api, ruta, **kw):
        peticiones.append((metodo, ruta))
        if metodo == "GET" and ruta.endswith("/dataset/datasetSchema"):
            return esquema_en_google
        if metodo == "GET" and "/processors" in ruta and ruta.endswith("/processors"):
            return {"processors": []}
        if metodo == "POST":
            return {"name": f"{svc._PADRE}/processors/pr-1"}
        if metodo == "GET":
            return {"name": f"{svc._PADRE}/processors/pr-1", "displayName": "nexusdoc--x", "defaultProcessorVersion": "p/l/pr/v1"}
        return {}

    original = svc._pedir
    svc._pedir = pedir_falso  # type: ignore[assignment]
    try:
        svc._CACHE_ESTRUCTURA.clear()
        e1 = asyncio.run(svc.estructura_del_procesador("pr-1"))
        e2 = asyncio.run(svc.estructura_del_procesador("pr-1"))
        lecturas = [p for p in peticiones if p[1].endswith("/dataset/datasetSchema")]
        rev("la estructura sale del esquema de Google", e1 == {"a": {"obligatorio": True, "multiple": False}}, str(e1))
        rev("la segunda lectura sale de la memoria: UNA sola petición a Google", e2 == e1 and len(lecturas) == 1, str(len(lecturas)))
        esquema_en_google["documentSchema"]["entityTypes"][0]["properties"].append({"name": "b", "valueType": "string", "occurrenceType": "OPTIONAL_MULTIPLE"})
        rev("si el esquema cambia por fuera, se sigue usando la memoria (hasta que venza)", asyncio.run(svc.estructura_del_procesador("pr-1")) == e1)
        svc._CACHE_ESTRUCTURA["pr-1"] = (svc._CACHE_ESTRUCTURA["pr-1"][0] - svc.TTL_ESTRUCTURA_S - 1, e1)
        rev("al vencer, se vuelve a leer y aparece lo nuevo", "b" in asyncio.run(svc.estructura_del_procesador("pr-1")))
        asyncio.run(svc.estructura_del_procesador("pr-1"))
        peticiones.clear()
        resultado = asyncio.run(svc.activar_tipo_documental("tipo-zz", "Z", {"entityTypes": [{"name": "custom_extraction_document_type", "properties": []}]}, 0))
        rev("(activar con los dobles termina)", resultado["procesadorId"] == "pr-1", str(resultado))
        rev("activar un esquema nuevo INVALIDA la memoria de ese procesador", "pr-1" not in svc._CACHE_ESTRUCTURA)
    finally:
        svc._pedir = original  # type: ignore[assignment]

    titulo("9 · Los extractores NUEVOS se llaman `biblioratum--…`; los de antes (`nexusdoc--…`) se siguen encontrando")
    en_google_9: list[dict] = []
    posts_9: list[dict] = []
    ESQUEMA_9 = {"entityTypes": [{"name": "custom_extraction_document_type", "properties": []}]}

    async def pedir_google_9(cliente, metodo, api, ruta, **kw):
        if metodo == "GET" and ruta.endswith("/processors"):
            return {"processors": list(en_google_9)}
        if metodo == "POST" and ruta.endswith("/processors"):
            posts_9.append(kw["json"])
            nuevo = {"name": f"{svc._PADRE}/processors/pr-{len(posts_9)}", "displayName": kw["json"]["displayName"], "type": kw["json"]["type"], "createTime": "2026-11-09T10:00:00Z"}
            en_google_9.append(nuevo)
            return nuevo
        if metodo == "GET":
            return next((p for p in en_google_9 if p["name"] == ruta), {"name": ruta, "displayName": ""})
        return {}

    svc._pedir = pedir_google_9  # type: ignore[assignment]
    try:
        r1 = asyncio.run(svc.activar_tipo_documental("tipo-aa", "INE Frontal", ESQUEMA_9, 0))
        rev("un tipo nuevo se crea con el prefijo `biblioratum--`", r1["creado"] is True and posts_9[-1]["displayName"] == "biblioratum--tipo-aa--v1--ine-frontal", str(posts_9))
        rev("y lo que se devuelve para mostrar es ese nombre", r1["procesadorDisplayName"] == "biblioratum--tipo-aa--v1--ine-frontal", r1["procesadorDisplayName"])
        r1b = asyncio.run(svc.activar_tipo_documental("tipo-aa", "INE Frontal renombrado", ESQUEMA_9, 0))
        rev("el doble clic (misma versión, aunque se renombre el tipo) adopta el que ya hay: nada se duplica", r1b["creado"] is False and r1b["procesadorId"] == r1["procesadorId"] and len(posts_9) == 1, str(posts_9))

        en_google_9.append({"name": f"{svc._PADRE}/processors/viejo-bb", "displayName": "nexusdoc--tipo-bb--v1--ine", "type": "CUSTOM_EXTRACTION_PROCESSOR", "createTime": "2026-10-01T10:00:00Z"})
        antes = len(posts_9)
        r2 = asyncio.run(svc.activar_tipo_documental("tipo-bb", "INE", ESQUEMA_9, 0))
        rev("un procesador que YA existía con el nombre de antes (`nexusdoc--`) se adopta, no se duplica", r2["creado"] is False and r2["procesadorId"] == "viejo-bb" and len(posts_9) == antes, str(r2))
        r3 = asyncio.run(svc.activar_tipo_documental("tipo-bb", "INE", ESQUEMA_9, 1))
        rev("pero la versión SIGUIENTE de ese tipo nace con el nombre nuevo", r3["creado"] is True and posts_9[-1]["displayName"] == "biblioratum--tipo-bb--v2--ine", str(posts_9[-1]))

        en_google_9.append({"name": f"{svc._PADRE}/processors/v10-nuevo", "displayName": "biblioratum--tipo-cc--v10--x", "type": "CUSTOM_EXTRACTION_PROCESSOR", "createTime": "2026-11-09T10:00:00Z"})
        en_google_9.append({"name": f"{svc._PADRE}/processors/v10-viejo", "displayName": "nexusdoc--tipo-cc--v10--x", "type": "CUSTOM_EXTRACTION_PROCESSOR", "createTime": "2026-10-01T10:00:00Z"})
        r4 = asyncio.run(svc.activar_tipo_documental("tipo-cc", "C", ESQUEMA_9, 0))
        rev("la v1 NO se confunde con la v10, con ninguno de los dos prefijos", r4["creado"] is True and posts_9[-1]["displayName"] == "biblioratum--tipo-cc--v1--c", str(posts_9[-1]))

        en_google_9.append({"name": f"{svc._PADRE}/processors/ajeno", "displayName": "mi-procesador-a-mano", "type": "CUSTOM_EXTRACTION_PROCESSOR", "createTime": "2026-01-01T10:00:00Z"})
        en_google_9.append({"name": f"{svc._PADRE}/processors/clas", "displayName": "nexusdoc-clasificador", "type": "CUSTOM_CLASSIFICATION_PROCESSOR", "createTime": "2026-09-06T10:00:00Z"})
        listados = asyncio.run(svc.listar_procesadores_de_la_aplicacion())
        nombres = {p["displayName"] for p in listados}
        rev("listar trae los `biblioratum--`, los `nexusdoc--` y los clasificadores", {"biblioratum--tipo-aa--v1--ine-frontal", "nexusdoc--tipo-bb--v1--ine", "nexusdoc-clasificador"} <= nombres, str(sorted(nombres)))
        rev("y NO trae lo que no es de la aplicación", "mi-procesador-a-mano" not in nombres, str(sorted(nombres)))
    finally:
        svc._pedir = original  # type: ignore[assignment]
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
