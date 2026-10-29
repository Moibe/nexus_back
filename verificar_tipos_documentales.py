"""¿El catálogo de tipos documentales vive en el servidor, por organización, y el clasificador sale de él?

    .venv/Scripts/python verificar_tipos_documentales.py

OFFLINE y sin tocar nada tuyo, como los demás `verificar_*`: el almacén es una carpeta temporal y Document AI se sustituye por
un doble que anota lo que le llegaría.

Lo que se prueba:

- crear, leer, guardar y borrar un tipo; lo ve TODA la organización (no solo quien lo creó) y ninguna otra;
- dos personas editando lo mismo: la segunda recibe 409 con la revisión vigente y no pisa nada;
- un tipo de otra organización responde como si no existiera (404), y su tenant, 403 `sin_acceso`;
- lo que no es un tipo (id raro, sin nombre, estado inventado, enorme) se rechaza, y lo que el servidor pone no se puede forzar;
- el orden de la biblioteca se guarda por organización;
- crear, borrar, cambiar de estado y calibrar dejan rastro con quién; guardar a secas, no;
- el clasificador de CADA organización se sincroniza SIN cuerpo, con los tipos activos de ESA organización, y `asegurar` solo corrige
  si hay desfase (o lo crea si falta); quien no es de la organización recibe 403 y el administrador de plataforma solo toca el suyo.
"""

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-tipos-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from routers import procesadores as router_procesadores  # noqa: E402
from servicios import auth, procesadores_huerfanos, tipos_documentales, usuarios  # noqa: E402
from errores import SinClasificador  # noqa: E402
from servicios.procesadores import DocumentAIError  # noqa: E402

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
    return "tipo-" + uuid.uuid4().hex


def tipo(nombre: str, estado: str = "borrador", **extra) -> dict:
    return {"nombre": nombre, "descripcion": f"Descripción de {nombre}", "estado": estado, "campos": [], "version": 0, "procesadorId": "", **extra}


def guardar(bearer: str, tenant: str, tipo_id: str, datos: dict, revision: int):
    return c.put(f"/tipos-documentales/{tipo_id}", json={"tenant": tenant, "revision": revision, "datos": datos}, headers=cab(bearer))


def listar(bearer: str, tenant: str):
    return c.get("/tipos-documentales/", params={"tenant": tenant}, headers=cab(bearer))


def ids(bearer: str, tenant: str) -> list[str]:
    return [t["id"] for t in listar(bearer, tenant).json()["tipos"]]


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
    # Supervisor y no Operador: desde NEX-472 (2026-10-22) solo Administrador y Supervisor editan esto (`verificar_roles.py` prueba al resto).
    r = c.post("/usuarios/", json={"nombre": "Omar", "apellidoPaterno": "Supervisor", "email": "omar@ejemplo.com", "rol": "SUPERVISOR"}, headers=cab(a_admin))
    assert r.status_code == 201, r.text
    a_otra = con_primer_acceso("omar@ejemplo.com", r.json()["contrasenaTemporal"], "Cielo-Abierto-Verde-52!")
    guid_rita = usuarios.para_login("rita@ejemplo.com")["guid"]
    guid_omar = usuarios.para_login("omar@ejemplo.com")["guid"]
    rev("hay dos organizaciones distintas y tres personas con sesión", org_a != org_b and a_admin and b_admin and a_otra)

    titulo("1 · Crear, leer, guardar y borrar")
    ine = nuevo_id()
    r = guardar(a_admin, org_a, ine, tipo("INE"), 0)
    rev("crear (revisión 0) responde 200 con revisión 1", r.status_code == 200 and r.json()["revision"] == 1, r.text[:200])
    d = r.json()
    rev("devuelve el tipo con su id dentro de `datos` y quién lo creó", d["datos"]["id"] == ine and d["creadoPor"] == guid_rita and d["modificadoPor"] == guid_rita and d["creadoEn"], str(d))
    r = listar(a_admin, org_a)
    rev("aparece en la lista de su organización", r.status_code == 200 and ids(a_admin, org_a) == [ine], r.text[:200])
    rev("lo que se guardó vuelve idéntico", r.json()["tipos"][0]["datos"]["nombre"] == "INE" and r.json()["tipos"][0]["datos"]["estado"] == "borrador")

    titulo("2 · Lo ve TODA la organización, y ninguna otra")
    rev("otra persona de la misma organización lo ve", ids(a_otra, org_a) == [ine])
    rev("la otra organización tiene su lista vacía", ids(b_admin, org_b) == [])
    rev("el espacio de plataforma, también", ids(bearer_super, "demo") == [])
    r = listar(b_admin, org_a)
    rev("pedir la lista de OTRA organización: 403 sin_acceso", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso", r.text[:200])
    rev("y no dice nada de la organización ajena", "Alfa" not in r.text and org_a not in r.text, r.text[:200])

    titulo("3 · Otra persona lo edita; el creador se conserva")
    r = guardar(a_otra, org_a, ine, tipo("INE", campos=[{"nombre": "curp"}]), 1)
    rev("guardar sobre la revisión vigente: 200 y revisión 2", r.status_code == 200 and r.json()["revision"] == 2, r.text[:200])
    d = r.json()
    rev("`modificadoPor` es quien lo editó y `creadoPor` sigue siendo quien lo creó", d["modificadoPor"] == guid_omar and d["creadoPor"] == guid_rita, str(d))
    rev("los campos nuevos quedaron", listar(a_admin, org_a).json()["tipos"][0]["datos"]["campos"] == [{"nombre": "curp"}])

    titulo("4 · Dos personas a la vez: no se pisa nada")
    r = guardar(a_admin, org_a, ine, tipo("INE editado por Rita"), 1)
    rev("guardar sobre una revisión vieja: 409", r.status_code == 409, r.text[:200])
    det = r.json()["detail"]
    rev("trae la revisión vigente para volver a leer", det.get("codigo") == "conflicto" and det.get("revisionActual") == 2, str(det))
    rev("y NO se guardó: sigue lo de la otra persona", listar(a_admin, org_a).json()["tipos"][0]["datos"]["nombre"] == "INE")
    r = guardar(a_admin, org_a, ine, tipo("INE"), 0)
    rev("crear con un id que YA existe: 409 con su revisión", r.status_code == 409 and r.json()["detail"]["revisionActual"] == 2, r.text[:200])

    titulo("5 · El de otra organización responde como si no existiera")
    r = guardar(b_admin, org_b, ine, tipo("Robado"), 2)
    rev("editar un id AJENO, con la revisión correcta: 404", r.status_code == 404, r.text[:200])
    r2 = guardar(b_admin, org_b, nuevo_id() + "x", tipo("Nada"), 3)
    rev("es la MISMA respuesta que para uno que no existe", r2.status_code == 404 and r2.json() == r.json(), f"{r.text[:100]} | {r2.text[:100]}")
    r = guardar(b_admin, org_b, ine, tipo("Colado"), 0)
    rev("crear con un id que es de otra organización: 409 sin decir de quién", r.status_code == 409 and r.json()["detail"].get("revisionActual") is None, r.text[:200])
    r = c.delete(f"/tipos-documentales/{ine}", params={"tenant": org_b}, headers=cab(b_admin))
    rev("borrar un id ajeno: 404", r.status_code == 404, r.text[:200])
    r = guardar(b_admin, org_a, ine, tipo("Cruzado"), 2)
    rev("escribir poniendo el tenant de OTRA: 403 sin_acceso", r.status_code == 403, r.text[:200])
    rev("nada de lo anterior tocó el tipo de la primera", listar(a_admin, org_a).json()["tipos"][0]["datos"]["nombre"] == "INE" and ids(b_admin, org_b) == [])
    intentos = auditoria("CROSS_TENANT_ACCESS")
    rev("los intentos con el tenant ajeno quedan en la bitácora", len(intentos) >= 2 and all(i.get("recurso") == "tipo_documental" for i in intentos), str(intentos[-1:]))

    titulo("6 · Sin llave o sin persona, no pasa nada")
    rev("sin Bearer: 401", c.get("/tipos-documentales/", params={"tenant": org_a}, headers=cab()).status_code == 401)
    rev("sin llave de servicio: 401", c.get("/tipos-documentales/", params={"tenant": org_a}, headers=cab(a_admin, servicio=False)).status_code == 401)
    rev("escribir sin Bearer: 401", c.put(f"/tipos-documentales/{ine}", json={"tenant": org_a, "revision": 2, "datos": tipo("x")}, headers=cab()).status_code == 401)

    titulo("7 · Lo que no es un tipo se rechaza")
    r = guardar(a_admin, org_a, "no-es-tipo", tipo("X"), 0)
    rev("un id sin la forma `tipo-…`: 400", r.status_code == 400, r.text[:160])
    rev("un id con mayúsculas o espacios: 400", guardar(a_admin, org_a, "tipo-ABC def", tipo("X"), 0).status_code in (400, 404))
    rev("sin nombre: 400", guardar(a_admin, org_a, nuevo_id(), {**tipo("X"), "nombre": "  "}, 0).status_code == 400)
    rev("un nombre de más de 200 caracteres: 400", guardar(a_admin, org_a, nuevo_id(), tipo("x" * 201), 0).status_code == 400)
    rev("estado inventado: 400", guardar(a_admin, org_a, nuevo_id(), tipo("X", estado="publicado"), 0).status_code == 400)
    rev("sin estado: 400", guardar(a_admin, org_a, nuevo_id(), {"nombre": "X"}, 0).status_code == 400)
    rev("descripción que no es texto: 400", guardar(a_admin, org_a, nuevo_id(), {**tipo("X"), "descripcion": 5}, 0).status_code == 400)
    rev("un tipo de más de 1 MB: 400", guardar(a_admin, org_a, nuevo_id(), tipo("X", relleno="z" * 1_100_000), 0).status_code == 400)
    rev("revisión negativa: 422", c.put(f"/tipos-documentales/{nuevo_id()}", json={"tenant": org_a, "revision": -1, "datos": tipo("X")}, headers=cab(a_admin)).status_code == 422)
    rev("ninguno de esos dejó rastro en la lista", ids(a_admin, org_a) == [ine])
    r = guardar(a_admin, org_a, ine, tipo("INE", revision=99, creadoPor="otro", tenant="otra", modificadoPor="otro"), 2)
    rev("lo que pone el servidor no se puede forzar desde `datos`", r.status_code == 200 and r.json()["revision"] == 3 and r.json()["creadoPor"] == guid_rita and r.json()["modificadoPor"] == guid_rita and not {"revision", "creadoPor", "tenant", "modificadoPor"} & set(r.json()["datos"]), r.text[:300])

    titulo("8 · El orden de la biblioteca")
    pasaporte, licencia = nuevo_id(), nuevo_id()
    guardar(a_admin, org_a, pasaporte, tipo("Pasaporte"), 0)
    guardar(a_admin, org_a, licencia, tipo("Licencia"), 0)
    rev("sin orden pedido, van en el de creación", ids(a_admin, org_a) == [ine, pasaporte, licencia])
    r = c.put("/tipos-documentales/orden", json={"tenant": org_a, "ids": [licencia, ine, pasaporte]}, headers=cab(a_admin))
    rev("fijar el orden: 200", r.status_code == 200, r.text[:160])
    rev("la lista lo respeta", ids(a_otra, org_a) == [licencia, ine, pasaporte])
    otro_b = nuevo_id()
    guardar(b_admin, org_b, otro_b, tipo("De Beta"), 0)
    c.put("/tipos-documentales/orden", json={"tenant": org_a, "ids": [otro_b, pasaporte, licencia, ine, licencia]}, headers=cab(a_admin))
    rev("un id de OTRA organización y los repetidos se ignoran", ids(a_admin, org_a) == [pasaporte, licencia, ine], str(ids(a_admin, org_a)))
    rev("el orden de una organización no mueve el de la otra", ids(b_admin, org_b) == [otro_b])
    nuevo = nuevo_id()
    guardar(a_admin, org_a, nuevo, tipo("Recién llegado"), 0)
    rev("uno nuevo va al final", ids(a_admin, org_a)[-1] == nuevo)
    rev("ordenar en el tenant ajeno: 403", c.put("/tipos-documentales/orden", json={"tenant": org_a, "ids": []}, headers=cab(b_admin)).status_code == 403)

    titulo("9 · Qué deja rastro")
    n_crear = len(auditoria("DOCUMENT_TYPE_CREATED"))
    rev("los tipos creados dejaron `DOCUMENT_TYPE_CREATED` con quién y de qué organización",
        n_crear >= 4 and auditoria("DOCUMENT_TYPE_CREATED")[0].get("usuarioGuid") == guid_rita and auditoria("DOCUMENT_TYPE_CREATED")[0].get("tenant") == org_a and auditoria("DOCUMENT_TYPE_CREATED")[0].get("tipoId") == ine,
        str(auditoria("DOCUMENT_TYPE_CREATED")[:1]))
    rev("guardar a secas (editar un campo) no dejó nada más", len(auditoria("DOCUMENT_TYPE_STATE_CHANGED")) == 0 and len(auditoria("DOCUMENT_TYPE_CALIBRATED")) == 0)

    actual = listar(a_admin, org_a).json()["tipos"]
    rev_ine = next(t["revision"] for t in actual if t["id"] == ine)
    activo = tipo("INE", estado="activo", version=1, procesadorId="proc-ine-1", activadoEn="2026-10-15T00:00:00Z", calibradoEn=None)
    r = guardar(a_otra, org_a, ine, activo, rev_ine)
    rev("activar: 200", r.status_code == 200, r.text[:160])
    cambios = auditoria("DOCUMENT_TYPE_STATE_CHANGED")
    rev("deja `DOCUMENT_TYPE_STATE_CHANGED` borrador → activo, con quién", len(cambios) == 1 and cambios[0].get("de") == "borrador" and cambios[0].get("a") == "activo" and cambios[0].get("usuarioGuid") == guid_omar and cambios[0].get("tipoId") == ine, str(cambios))
    r = guardar(a_admin, org_a, ine, {**activo, "calibradoEn": "2026-10-15T01:00:00Z"}, r.json()["revision"])
    cal = auditoria("DOCUMENT_TYPE_CALIBRATED")
    rev("calibrar deja `DOCUMENT_TYPE_CALIBRATED` con quién", r.status_code == 200 and len(cal) == 1 and cal[0].get("usuarioGuid") == guid_rita and cal[0].get("version") == 1, str(cal))
    rev("y calibrar NO cuenta como cambio de estado", len(auditoria("DOCUMENT_TYPE_STATE_CHANGED")) == 1)
    r = guardar(a_admin, org_a, ine, {**activo, "calibradoEn": "2026-10-15T01:00:00Z", "descripcion": "otra"}, r.json()["revision"])
    rev("guardar otra vez lo ya calibrado no repite el rastro", len(auditoria("DOCUMENT_TYPE_CALIBRATED")) == 1)

    titulo("10 · Cada organización tiene SU clasificador, armado con SUS tipos activos de ESTE registro")
    guardar(b_admin, org_b, otro_b, tipo("De Beta", estado="activo", version=1, procesadorId="proc-beta-1"), 1)
    archivado = nuevo_id()
    guardar(a_admin, org_a, archivado, tipo("Viejo", estado="archivado", procesadorId="proc-viejo"), 0)
    activos_a = tipos_documentales.tipos_activos(org_a)
    activos_b = tipos_documentales.tipos_activos(org_b)
    rev("`tipos_activos` de A trae solo el activo de A (ni el archivado, ni los de B)", [t["id"] for t in activos_a] == [ine], str(activos_a))
    rev("y el de B, solo el de B", [t["id"] for t in activos_b] == [otro_b], str(activos_b))
    rev("con nombre y descripción (que es prompt)", all(t["nombre"] and t["descripcion"] for t in activos_a + activos_b))
    rev("de una organización que no existe: ninguno", tipos_documentales.tipos_activos("no-existe") == [])

    llamadas: list[tuple[str, list[dict]]] = []

    async def sincronizar_falso(tipos, tenant):
        llamadas.append((tenant, tipos))
        return {"procesadorId": f"clas-{tenant[:4]}", "creado": False}

    esquema_google: dict = {"categorias": []}

    async def consultar_falso(tenant):
        if esquema_google.get("falla"):
            raise DocumentAIError("no existe", status_code=404)
        if esquema_google.get("sin_clasificador"):
            raise SinClasificador("Esta organización todavía no tiene clasificador: se crea al activar su primer tipo documental.")
        return esquema_google

    router_procesadores._sincronizar_clasificador = sincronizar_falso
    router_procesadores.consultar_esquema_clasificador = consultar_falso

    def sincronizar(bearer, tenant, **kw):
        return c.post("/procesadores/clasificador/sincronizar", params={"tenant": tenant}, headers=cab(bearer), **kw)

    def asegurar(bearer, tenant):
        return c.post("/procesadores/clasificador/asegurar", params={"tenant": tenant}, headers=cab(bearer))

    r = sincronizar(a_admin, org_a)
    rev("sincronizar SIN cuerpo: 200", r.status_code == 200 and r.json()["procesadorId"] == f"clas-{org_a[:4]}", r.text[:200])
    rev("recibió el tenant y SOLO los tipos activos de esa organización", len(llamadas) == 1 and llamadas[0][0] == org_a and [t["id"] for t in llamadas[0][1]] == [ine], str(llamadas))
    sincronizar(b_admin, org_b)
    rev("la otra organización sincroniza el suyo, con solo sus tipos", llamadas[-1][0] == org_b and [t["id"] for t in llamadas[-1][1]] == [otro_b], str(llamadas[-1]))
    rev("lo que mande el cliente en el cuerpo no cuenta",
        sincronizar(a_admin, org_a, json={"tipos": [{"id": "tipo-falso", "nombre": "Falso"}]}).status_code == 200
        and all(t["id"] != "tipo-falso" for t in llamadas[-1][1]))
    n = len(llamadas)
    r = sincronizar(b_admin, org_a)
    rev("sincronizar el clasificador de OTRA organización: 403 sin_acceso y no llega a Google", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso" and len(llamadas) == n, r.text[:200])
    rev("sin sesión (solo la llave de servicio): 401", c.post("/procesadores/clasificador/sincronizar", params={"tenant": org_a}, headers=cab()).status_code == 401)
    rev("sin decir de qué organización: 422", c.post("/procesadores/clasificador/sincronizar", headers=cab(a_admin)).status_code == 422)
    rev("el administrador de plataforma, en SU espacio, puede", sincronizar(bearer_super, "demo").status_code == 422 and llamadas[-1][0] != "demo", "sin tipos activos en su espacio: 422")
    rev("pero no en el clasificador de una organización", sincronizar(bearer_super, org_a).status_code == 403)

    esperadas = router_procesadores._categorias_esperadas(tipos_documentales.tipos_activos(org_a))
    esquema_google["categorias"] = [{"name": n, "description": d} for n, d in esperadas] + [{"name": "otro", "description": "x"}]
    llamadas.clear()
    r = asegurar(a_admin, org_a)
    rev("asegurar con el clasificador al día: no sincroniza", r.status_code == 200 and r.json() == {"sincronizado": False, "motivo": "al_dia"} and not llamadas, r.text[:200])
    # Con las categorías de B en el clasificador de A, A está desfasado: se compara contra SUS tipos, no contra los de todas.
    esquema_google["categorias"] = [{"name": n, "description": d} for n, d in router_procesadores._categorias_esperadas(activos_b)] + [{"name": "otro", "description": "x"}]
    r = asegurar(a_admin, org_a)
    rev("y compara contra los tipos de SU organización (con los de B en Google, A está desfasado)", r.json()["motivo"] == "desfasado" and llamadas[-1][0] == org_a, r.text[:200])
    esquema_google["categorias"] = [{"name": n, "description": d} for n, d in esperadas] + [{"name": "otro", "description": "x"}]
    esquema_google["categorias"][0]["description"] = "una descripción vieja"
    llamadas.clear()
    r = asegurar(a_admin, org_a)
    rev("una descripción vieja cuenta como desfase: sincroniza", r.json() == {"sincronizado": True, "motivo": "desfasado"} and len(llamadas) == 1, r.text[:200])
    esquema_google["categorias"] = [{"name": n, "description": d} for n, d in esperadas][1:]
    llamadas.clear()
    rev("una categoría que falta: sincroniza", asegurar(a_admin, org_a).json()["motivo"] == "desfasado" and len(llamadas) == 1)
    esquema_google["categorias"] = [{"name": n, "description": d} for n, d in esperadas] + [{"name": "tipo-borrado", "description": ""}]
    llamadas.clear()
    rev("una categoría de más (un tipo que ya no existe): sincroniza", asegurar(a_admin, org_a).json()["motivo"] == "desfasado" and len(llamadas) == 1)
    esquema_google["falla"] = True
    llamadas.clear()
    r = asegurar(a_admin, org_a)
    rev("si Google no deja leerlo, lo sincroniza directo (crea lo que falte)", r.json() == {"sincronizado": True, "motivo": "ilegible"} and len(llamadas) == 1, r.text[:200])
    esquema_google.pop("falla")
    esquema_google["sin_clasificador"] = True
    llamadas.clear()
    r = asegurar(a_admin, org_a)
    rev("si la organización todavía no tiene clasificador, asegurar lo crea", r.json() == {"sincronizado": True, "motivo": "sin_clasificador"} and len(llamadas) == 1, r.text[:200])
    esquema_google.pop("sin_clasificador")
    llamadas.clear()
    org_vacia, vacia_admin = crear_organizacion(bearer_super, "Gamma", "gabi@ejemplo.com", "Rumbo-Sereno-Verde-49!")
    r = asegurar(vacia_admin, org_vacia)
    rev("una organización sin tipos activos: asegurar no exige nada", r.json() == {"sincronizado": False, "motivo": "sin_tipos"} and not llamadas, r.text[:200])
    rev("y sincronizar le dice que no hay con qué armarlo (422)", sincronizar(vacia_admin, org_vacia).status_code == 422)
    rev("asegurar la organización ajena: 403", asegurar(b_admin, org_a).status_code == 403)
    rev("sin llave de servicio: 401", c.post("/procesadores/clasificador/asegurar", params={"tenant": org_a}).status_code == 401)
    esquema_google["sin_clasificador"] = True
    r = c.get("/procesadores/clasificador", params={"tenant": org_a}, headers=cab(a_admin))
    rev("ver el clasificador de una organización que no tiene: 404 con su frase", r.status_code == 404 and "todavía no tiene clasificador" in r.json()["detail"], r.text[:200])
    esquema_google.pop("sin_clasificador")
    rev("ver el de otra organización: 403", c.get("/procesadores/clasificador", params={"tenant": org_a}, headers=cab(b_admin)).status_code == 403)

    titulo("11 · Borrar")
    r = c.delete(f"/tipos-documentales/{archivado}", params={"tenant": org_a}, headers=cab(a_otra))
    rev("borrar: 200", r.status_code == 200 and r.json() == {"eliminado": True}, r.text[:160])
    rev("ya no está en la lista", archivado not in ids(a_admin, org_a))
    borr = auditoria("DOCUMENT_TYPE_DELETED")
    rev("deja `DOCUMENT_TYPE_DELETED` con quién y cuál", len(borr) == 1 and borr[0].get("tipoId") == archivado and borr[0].get("usuarioGuid") == guid_omar and borr[0].get("nombre") == "Viejo", str(borr))
    rev("borrarlo otra vez: 404", c.delete(f"/tipos-documentales/{archivado}", params={"tenant": org_a}, headers=cab(a_otra)).status_code == 404)
    rev("guardar sobre uno borrado (con revisión): 404", guardar(a_admin, org_a, archivado, tipo("Viejo"), 1).status_code == 404)
    guardar(a_admin, org_a, ine, {**activo, "estado": "archivado"}, listar(a_admin, org_a).json()["tipos"][0]["revision"] if False else next(t["revision"] for t in listar(a_admin, org_a).json()["tipos"] if t["id"] == ine))
    rev("un tipo archivado ya no entra al clasificador", ine not in [t["id"] for t in tipos_documentales.tipos_activos(org_a)])

    titulo("12 · Procesadores con dueño (para limpiar los huérfanos)")
    hist = nuevo_id()
    guardar(a_admin, org_a, hist, tipo("Con historial", estado="activo", version=2, procesadorId="proc-v2",
                                        historialVersiones=[{"version": 1, "procesadorId": "proc-v1"}],
                                        versionEnEdicion={"version": 2, "procesadorId": "proc-v2"}), 0)
    duenos = tipos_documentales.procesadores_con_dueno()
    rev("conoce el procesador vigente, los del historial y los de otra organización",
        {"proc-v2", "proc-v1", "proc-beta-1", "proc-ine-1"} <= set(duenos) and duenos["proc-v1"]["tipo"] == hist and duenos["proc-beta-1"]["tenant"] == org_b, str(duenos))
    rev("y no inventa los que no existen", "proc-fantasma" not in duenos)
    en_google = [
        {"id": "proc-v2", "displayName": "nexusdoc--tipo-x--v2--con-historial", "tipo": "CUSTOM_EXTRACTION_PROCESSOR", "creado": "2026-10-01"},
        {"id": "proc-v1", "displayName": "nexusdoc--tipo-x--v1--con-historial", "tipo": "CUSTOM_EXTRACTION_PROCESSOR", "creado": "2026-09-01"},
        {"id": "proc-compu-de-pepe", "displayName": "nexusdoc--tipo-mtsvawu5-1--v1--ine", "tipo": "CUSTOM_EXTRACTION_PROCESSOR", "creado": "2026-09-10"},
        {"id": "clas-1", "displayName": "nexusdoc-clasificador", "tipo": "CUSTOM_CLASSIFICATION_PROCESSOR", "creado": "2026-09-06"},
        {"id": "proc-ine-legado", "displayName": "nexusdoc--legado", "tipo": "CUSTOM_EXTRACTION_PROCESSOR", "creado": "2026-08-01"},
        {"id": "ajeno", "displayName": "otra-cosa-del-proyecto", "tipo": "OCR_PROCESSOR", "creado": "2026-01-01"},
    ]
    rep = procesadores_huerfanos.clasificar(en_google, duenos, {"clas-1", "proc-ine-legado"})
    rev("reparte: con dueño los del registro (vigente e historial)", sorted(p["id"] for p in rep["con_dueno"]) == ["proc-v1", "proc-v2"], str(rep["con_dueno"]))
    rev("huérfano solo el que nadie señala", [p["id"] for p in rep["huerfanos"]] == ["proc-compu-de-pepe"], str(rep["huerfanos"]))
    rev("el clasificador, el legado y lo que no es de la aplicación quedan protegidos",
        sorted(p["id"] for p in rep["protegidos"]) == ["ajeno", "clas-1", "proc-ine-legado"], str(rep["protegidos"]))
    rev("cada procesador está en UNA sola lista", sum(len(v) for v in rep.values()) == len(en_google))

    titulo("12b · Corte por fecha al borrar huérfanos (`--antes-de`)")
    viejos, resto = procesadores_huerfanos.anteriores_a(
        [
            {"id": "a", "creado": "2026-10-14T23:59:59.999Z"},
            {"id": "b", "creado": "2026-10-15T00:00:00Z"},
            {"id": "c", "creado": "2026-10-15T22:03:51.673Z"},
            {"id": "d", "creado": ""},
            {"id": "e"},
            {"id": "f", "creado": "2026-09-02T18:27:38Z"},
        ],
        "2026-10-15",
    )
    rev("solo cuenta lo creado ANTES de la fecha", [p["id"] for p in viejos] == ["a", "f"], str(viejos))
    rev("el día del corte ya es «reciente» y no se toca", {"b", "c"} <= {p["id"] for p in resto}, str(resto))
    rev("sin fecha de creación no se arriesga: queda con lo reciente", {"d", "e"} <= {p["id"] for p in resto} and len(viejos) + len(resto) == 6, str(resto))
    rev("el corte se aplica a los huérfanos del reparto: el de 2026-09-10 cae antes del 15-oct y no antes del 10-sep",
        [p["id"] for p in procesadores_huerfanos.anteriores_a(rep["huerfanos"], "2026-10-15")[0]] == ["proc-compu-de-pepe"]
        and procesadores_huerfanos.anteriores_a(rep["huerfanos"], "2026-09-10")[0] == [])

    def rechaza(texto: str) -> bool:
        try:
            procesadores_huerfanos.fecha_de_corte(texto)
        except ValueError:
            return True
        return False

    rev("la fecha de corte exige AAAA-MM-DD real",
        procesadores_huerfanos.fecha_de_corte("2026-10-15") == "2026-10-15"
        and all(rechaza(t) for t in ("", "15/10/2026", "2026-10-1", "20261015", "2026-13-45", "2026-02-30", "ayer")))

    titulo("13 · Resiste un corte a media escritura")
    ruta = RAIZ / ".registro" / "tipos-documentales.jsonl"
    with open(ruta, "ab") as f:
        f.write(b'{"op": "guardar", "id": "tipo-roto')
    antes = ids(a_admin, org_a)
    rev("una última línea rota se ignora al leer", ids(a_admin, org_a) == antes and len(antes) >= 3)
    n = nuevo_id()
    r = guardar(a_admin, org_a, n, tipo("Después del corte"), 0)
    rev("y el guardado siguiente no queda pegado a ella", r.status_code == 200 and n in ids(a_admin, org_a))
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
