"""¿Una organización puede llegar a lo de otra? (HU11, CA-1 a CA-6)

    .venv/Scripts/python verificar_aislamiento.py

OFFLINE y sin tocar nada tuyo, como los demás `verificar_*`: levanta la app contra un `ALMACEN_RUTA` temporal.

La prueba de seguridad multi-tenant que pide el criterio CA-3. Hay dos organizaciones con su administrador y sus cosas
(un webhook, una llave, una entrada de la bandeja, un archivo, un usuario), y la sesión de la SEGUNDA intenta, por cada ruta
de la API que recibe un tenant o un identificador, llegar a lo de la PRIMERA:

- CA-1 y CA-2: lo suyo se ve y lo ajeno no, en datos y en archivos; lo que se pide con el tenant ajeno se rechaza.
- CA-3: la sesión del administrador de plataforma tampoco entra a una organización por estas rutas.
- CA-4: ninguna respuesta de rechazo lleva un identificador, un nombre, una ruta ni un dato de la otra organización, y los
  errores del almacén ya no cuentan rutas del servidor.
- CA-5: un recurso ajeno se contesta IGUAL que uno que no existe (misma respuesta, byte por byte).
- CA-6: cada intento deja `CROSS_TENANT_ACCESS`, con quién, qué pidió y por dónde se detectó.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-aislamiento-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

from cryptography.fernet import Fernet  # noqa: E402

os.environ["WEBHOOKS_CLAVE_CIFRADO"] = Fernet.generate_key().decode()

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import almacen, auth, bandeja, usuarios  # noqa: E402

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + extra if extra and not ok else ''}")
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


def cab(bearer: str | None = None) -> dict:
    """La llave de servicio y, si hay, el Bearer de la persona: lo que manda el BFF."""
    return {"x-api-key": SERVICIO, **({"Authorization": f"Bearer {bearer}"} if bearer else {})}


c = TestClient(app)


def entrar(correo: str, contrasena: str) -> dict:
    r = c.post("/auth/login", json={"email": correo, "password": contrasena, "ip": "127.0.0.1", "userAgent": "verificar"}, headers=cab())
    assert r.status_code == 200, r.text
    return r.json()


def con_primer_acceso(correo: str, temporal: str, definitiva: str) -> str:
    """Entra con la temporal, pone su contraseña y vuelve a entrar: el Bearer de una persona ya activada."""
    s = entrar(correo, temporal)
    r = c.post("/auth/contrasena", json={"nueva": definitiva, "confirmacion": definitiva}, headers=cab(s["accessToken"]))
    assert r.status_code == 200, r.text
    return entrar(correo, definitiva)["accessToken"]


def eventos_de_auditoria(accion: str) -> list[dict]:
    ruta = RAIZ / ".registro" / "auditoria.jsonl"
    if not ruta.exists():
        return []
    return [e for e in (json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip()) if e.get("accion") == accion]


def cruzados() -> list[dict]:
    return eventos_de_auditoria("CROSS_TENANT_ACCESS")


try:
    titulo("0 · Montaje: el administrador de plataforma, dos organizaciones con su administrador y lo de cada una")
    temporal_super = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal_super))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal_super, "Cobre-Lento-Azul-47!")

    def crear_org(nombre: str, admin_nombre: str, admin_correo: str) -> dict:
        r = c.post("/organizaciones/", json={"nombre": nombre, "adminNombre": admin_nombre, "adminApellidos": "Zeta", "adminEmail": admin_correo}, headers=cab(bearer_super))
        assert r.status_code == 201, r.text
        cuerpo = r.json()
        return {
            "guid": cuerpo["organizacion"]["guid"], "nombre": nombre, "slug": cuerpo["organizacion"]["slug"], "codigo": cuerpo["organizacion"]["codigo"],
            "admin_guid": cuerpo["admin"]["guid"], "admin_correo": admin_correo,
            "bearer": con_primer_acceso(admin_correo, cuerpo["contrasenaTemporal"], "Rumbo-Sereno-Azul-47!"),
        }

    A = crear_org("Seguros Alfa Omega", "Rita", "rita.alfa@ejemplo.com")
    B = crear_org("Textiles Beta Sigma", "Tomas", "tomas.beta@ejemplo.com")
    rev("el administrador de plataforma crea las dos organizaciones y cada administrador activa su cuenta", bool(A["bearer"] and B["bearer"] and A["guid"] != B["guid"]))

    def montar(o: dict, correo_usuario: str) -> dict:
        """Lo que una organización tiene: un webhook, una llave, una entrada en la bandeja, su archivo y un usuario."""
        r = c.post("/webhooks/", json={"tenant": o["guid"], "url": f"https://api.{o['slug']}.com/avisos", "eventos": ["documento.completado"]}, headers=cab(o["bearer"]))
        assert r.status_code == 201, r.text
        o["webhook"] = r.json()["webhook"]["id"]
        r = c.post("/llaves/", json={"tenant": o["guid"], "nombre": f"Llave {o['slug']}", "descripcion": "Para el aislamiento", "dias": 30}, headers=cab(o["bearer"]))
        assert r.status_code == 201, r.text
        o["llave"] = r.json()["llave"]["id"]
        o["llave_secreta"] = r.json()["secret"]
        g = almacen.guardar(o["guid"], f"%PDF-1.4 contenido propio de {o['slug']}".encode())
        o["ruta"], o["sha"] = g["rutaRelativa"], g["hash"]
        o["entrada"] = bandeja.registrar_entrada(o["guid"], g, "application/pdf", f"{o['slug']}.pdf", "API")["id"]
        r = c.post("/usuarios/", json={"nombre": "Mara", "email": correo_usuario, "rol": "ANALISTA"}, headers=cab(o["bearer"]))
        assert r.status_code == 201, r.text
        o["usuario"] = r.json()["usuario"]["guid"]
        o["usuario_temp"] = r.json()["contrasenaTemporal"]
        o["usuario_correo"] = correo_usuario
        return o

    montar(A, "mara.alfa@ejemplo.com")
    montar(B, "mara.beta@ejemplo.com")
    # Un archivo del espacio heredado (`csi`): el de antes de que existieran las organizaciones.
    heredado = almacen.guardar("csi", b"%PDF-1.4 ejemplo heredado de la configuracion")
    rev("cada organización tiene su webhook, su llave, su entrada, su archivo y su usuario", all(A.get(k) and B.get(k) for k in ("webhook", "llave", "entrada", "ruta", "usuario")))
    SECRETOS_DE_A = [A["guid"], A["nombre"], A["slug"], A["codigo"], A["admin_guid"], A["admin_correo"], A["webhook"], A["llave"], A["entrada"], A["ruta"], A["sha"], A["usuario"], A["usuario_correo"]]
    cuerpos_de_rechazo: list[str] = []
    ev_antes = len(cruzados())

    def rutas_con_tenant(t: str, o: dict) -> list[tuple[str, object]]:
        """Cada ruta que RECIBE un tenant, armada contra ese tenant y los ids de `o` (que son los de la organización A)."""
        periodo = {"desde": "2026-10-01T00:00:00-06:00", "hasta": "2026-10-02T00:00:00-06:00"}
        pdf = ("nuevo.pdf", b"%PDF-1.4 un archivo nuevo", "application/pdf")
        return [
            ("GET /bandeja/", lambda h: c.get("/bandeja/", params={"tenant": t}, headers=h)),
            ("POST /bandeja/{id}/retirar", lambda h: c.post(f"/bandeja/{o['entrada']}/retirar", data={"tenant": t, "motivo": "descartado"}, headers=h)),
            ("GET /llaves/", lambda h: c.get("/llaves/", params={"tenant": t}, headers=h)),
            ("POST /llaves/", lambda h: c.post("/llaves/", json={"tenant": t, "nombre": "Intrusa", "descripcion": "x", "dias": 7}, headers=h)),
            ("POST /llaves/{id}/revocar", lambda h: c.post(f"/llaves/{o['llave']}/revocar", json={"tenant": t}, headers=h)),
            ("GET /llaves/{id}/metricas", lambda h: c.get(f"/llaves/{o['llave']}/metricas", params={"tenant": t, **periodo}, headers=h)),
            ("GET /webhooks/", lambda h: c.get("/webhooks/", params={"tenant": t}, headers=h)),
            ("POST /webhooks/", lambda h: c.post("/webhooks/", json={"tenant": t, "url": "https://api.intrusa.com/x", "eventos": ["documento.completado"]}, headers=h)),
            ("POST /webhooks/{id}/estado", lambda h: c.post(f"/webhooks/{o['webhook']}/estado", json={"tenant": t, "estado": "inactivo"}, headers=h)),
            ("POST /webhooks/{id}/eliminar", lambda h: c.post(f"/webhooks/{o['webhook']}/eliminar", json={"tenant": t}, headers=h)),
            ("POST /webhooks/{id}/validar", lambda h: c.post(f"/webhooks/{o['webhook']}/validar", json={"tenant": t}, headers=h)),
            ("POST /webhooks/{id}/editar", lambda h: c.post(f"/webhooks/{o['webhook']}/editar", json={"tenant": t, "url": "https://api.intrusa.com/y", "eventos": ["documento.completado"]}, headers=h)),
            ("POST /webhooks/eventos", lambda h: c.post("/webhooks/eventos", json={"tenant": t, "tipo": "documento.completado", "entradaId": o["entrada"]}, headers=h)),
            ("GET /webhooks/{id}/metricas", lambda h: c.get(f"/webhooks/{o['webhook']}/metricas", params={"tenant": t, **periodo}, headers=h)),
            ("GET /webhooks/{id}/intentos", lambda h: c.get(f"/webhooks/{o['webhook']}/intentos", params={"tenant": t}, headers=h)),
            ("POST /archivos/", lambda h: c.post("/archivos/", data={"tenant": t}, files={"archivo": pdf}, headers=h)),
            ("POST /bandeja/", lambda h: c.post("/bandeja/", data={"tenant": t}, files={"archivo": pdf}, headers=h)),
        ]

    titulo("1 · CA-1: sin la sesión de una persona, ninguna ruta con tenant responde (la llave de servicio sola ya no basta)")
    sin_sesion = [(n, f(cab())) for n, f in rutas_con_tenant(A["guid"], A)]
    rev("las 17 rutas con tenant dan 401 sin Bearer", all(r.status_code == 401 and r.json()["detail"]["codigo"] == "sin_sesion" for _, r in sin_sesion),
        str([(n, r.status_code) for n, r in sin_sesion if r.status_code != 401]))
    r = c.get(f"/archivos/{A['ruta']}", params={"mime": "application/pdf"}, headers=cab())
    rev("y leer un archivo por su ruta también: 401", r.status_code == 401, str(r.status_code))
    r = c.get("/bandeja/", params={"tenant": A["guid"]}, headers={"Authorization": f"Bearer {A['bearer']}"})
    rev("y un Bearer sin la llave de servicio sigue siendo 401 (la llave es lo primero)", r.status_code == 401, str(r.status_code))
    rev("sin nada rechazado todavía: ningún intento cruzado en la bitácora (faltar la sesión no es cruzar)", len(cruzados()) == ev_antes)

    titulo("2 · CA-1 y CA-2: cada organización ve y toca lo SUYO")
    for o, otra in ((A, B), (B, A)):
        entradas = c.get("/bandeja/", params={"tenant": o["guid"]}, headers=cab(o["bearer"])).json()["entradas"]
        llaves = c.get("/llaves/", params={"tenant": o["guid"]}, headers=cab(o["bearer"])).json()["llaves"]
        hooks = c.get("/webhooks/", params={"tenant": o["guid"]}, headers=cab(o["bearer"])).json()["webhooks"]
        rev(f"{o['slug']}: su bandeja, sus llaves y sus webhooks traen lo suyo y NADA de la otra",
            [e["id"] for e in entradas] == [o["entrada"]] and [x["id"] for x in llaves] == [o["llave"]] and [w["id"] for w in hooks] == [o["webhook"]]
            and otra["entrada"] not in json.dumps(entradas) and otra["llave"] not in json.dumps(llaves) and otra["webhook"] not in json.dumps(hooks))
        r = c.get(f"/archivos/{o['ruta']}", params={"mime": "application/pdf"}, headers=cab(o["bearer"]))
        rev(f"{o['slug']}: lee su propio archivo", r.status_code == 200 and r.content.startswith(b"%PDF"), str(r.status_code))
    r = c.post("/archivos/", data={"tenant": B["guid"]}, files={"archivo": ("b.pdf", b"%PDF-1.4 subido por B", "application/pdf")}, headers=cab(B["bearer"]))
    rev("B sube un archivo a SU prefijo y queda bajo su guid", r.status_code == 200 and r.json()["rutaRelativa"].startswith(B["guid"] + "/"), r.text[:160])
    r = c.get(f"/archivos/{A['ruta']}", params={"mime": "application/pdf"}, headers=cab(A["bearer"]))
    rev("el archivo se puede guardar en el navegador pero NO en una caché compartida (`private`): una CDN o un proxy se lo serviría a cualquiera",
        "private" in r.headers.get("cache-control", "") and "public" not in r.headers.get("cache-control", ""), r.headers.get("cache-control", ""))

    titulo("3 · CA-1, CA-3, CA-4 y CA-6: la sesión de B pide lo de A con el tenant de A → 403, sin tocar nada, y queda anotado")
    antes = len(cruzados())
    intentos = [(n, f(cab(B["bearer"]))) for n, f in rutas_con_tenant(A["guid"], A)]
    cuerpos_de_rechazo += [r.text for _, r in intentos]
    rev("las 17 rutas dan 403 sin_acceso", all(r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso" for _, r in intentos),
        str([(n, r.status_code, r.text[:80]) for n, r in intentos if r.status_code != 403]))
    rev("con un mensaje que no nombra nada de la otra organización",
        all(r.json()["detail"]["mensaje"] == "No tienes acceso a esa organización." for _, r in intentos))
    nuevos = cruzados()[antes:]
    rev("cada intento dejó UN evento CROSS_TENANT_ACCESS (17 rutas, 17 eventos)", len(nuevos) == 17, str(len(nuevos)))
    rev("con quién (B), qué organización pidió (A), de qué ruta y que se detectó por el `tenant`",
        all(e["usuarioGuid"] == B["admin_guid"] and e["tenantPedido"] == A["guid"] and e["via"] == "tenant" and e["tenantsDelActor"] == [B["guid"]] and e["en"] for e in nuevos)
        and {e["recurso"] for e in nuevos} == {"bandeja", "llave", "webhook", "archivo"}, str(nuevos[:1]))
    # Y no pasó nada: lo de A está intacto.
    hooks_a = c.get("/webhooks/", params={"tenant": A["guid"]}, headers=cab(A["bearer"])).json()["webhooks"]
    llaves_a = c.get("/llaves/", params={"tenant": A["guid"]}, headers=cab(A["bearer"])).json()["llaves"]
    entradas_a = c.get("/bandeja/", params={"tenant": A["guid"]}, headers=cab(A["bearer"])).json()["entradas"]
    rev("lo de A quedó intacto: su webhook sigue activo y con su URL, su llave sin revocar, su entrada pendiente y sin llaves ni webhooks de más",
        [(w["id"], w["estado"], w["url"]) for w in hooks_a] == [(A["webhook"], "activo", f"https://api.{A['slug']}.com/avisos")]
        and [(x["id"], x["revocadaEn"]) for x in llaves_a] == [(A["llave"], None)] and [e["id"] for e in entradas_a] == [A["entrada"]])
    rev("y nada se escribió bajo el prefijo de A", sorted(p.name for p in (RAIZ / A["guid"]).rglob("*") if p.is_file()) == [A["sha"]])

    titulo("4 · CA-5 y CA-6: un recurso AJENO con el tenant PROPIO se contesta igual que uno que no existe, y queda anotado")
    inventado = "f" * 32
    PERIODO = {"desde": "2026-10-01T00:00:00-06:00", "hasta": "2026-10-02T00:00:00-06:00"}

    def con_id(t: str, ids: dict) -> list[tuple[str, str, object]]:
        """Las rutas que reciben un IDENTIFICADOR de algo: (recurso, ruta, llamada), armadas con el tenant `t` y esos ids."""
        w, ll, e = ids["webhook"], ids["llave"], ids["entrada"]
        return [
            ("bandeja", "POST /bandeja/{id}/retirar", lambda h: c.post(f"/bandeja/{e}/retirar", data={"tenant": t, "motivo": "descartado"}, headers=h)),
            ("bandeja", "POST /webhooks/eventos", lambda h: c.post("/webhooks/eventos", json={"tenant": t, "tipo": "documento.completado", "entradaId": e}, headers=h)),
            ("llave", "POST /llaves/{id}/revocar", lambda h: c.post(f"/llaves/{ll}/revocar", json={"tenant": t}, headers=h)),
            ("llave", "GET /llaves/{id}/metricas", lambda h: c.get(f"/llaves/{ll}/metricas", params={"tenant": t, **PERIODO}, headers=h)),
            ("webhook", "POST /webhooks/{id}/estado", lambda h: c.post(f"/webhooks/{w}/estado", json={"tenant": t, "estado": "inactivo"}, headers=h)),
            ("webhook", "POST /webhooks/{id}/eliminar", lambda h: c.post(f"/webhooks/{w}/eliminar", json={"tenant": t}, headers=h)),
            ("webhook", "POST /webhooks/{id}/validar", lambda h: c.post(f"/webhooks/{w}/validar", json={"tenant": t}, headers=h)),
            ("webhook", "POST /webhooks/{id}/editar", lambda h: c.post(f"/webhooks/{w}/editar", json={"tenant": t, "url": "https://api.intrusa.com/z", "eventos": ["documento.completado"]}, headers=h)),
            ("webhook", "GET /webhooks/{id}/metricas", lambda h: c.get(f"/webhooks/{w}/metricas", params={"tenant": t, **PERIODO}, headers=h)),
            ("webhook", "GET /webhooks/{id}/intentos", lambda h: c.get(f"/webhooks/{w}/intentos", params={"tenant": t}, headers=h)),
        ]

    antes = len(cruzados())
    ids_de_a = {"webhook": A["webhook"], "llave": A["llave"], "entrada": A["entrada"]}
    ids_falsos = {"webhook": inventado, "llave": inventado, "entrada": inventado}
    ajenas = [(rec, ruta, f(cab(B["bearer"]))) for rec, ruta, f in con_id(B["guid"], ids_de_a)]
    nuevos = cruzados()[antes:]
    inexistentes = [(rec, ruta, f(cab(B["bearer"]))) for rec, ruta, f in con_id(B["guid"], ids_falsos)]
    cuerpos_de_rechazo += [r.text for _, _, r in ajenas]
    rev("las 10 rutas con un id de A y el tenant de B dan 404", all(r.status_code == 404 for _, _, r in ajenas), str([(ruta, r.status_code) for _, ruta, r in ajenas if r.status_code != 404]))
    rev("y cada respuesta es IDÉNTICA a la de un id que no existe (mismo estado, mismo cuerpo)",
        all((a.status_code, a.text) == (i.status_code, i.text) for (_, _, a), (_, _, i) in zip(ajenas, inexistentes)),
        str([(ruta, a.text, i.text) for (_, ruta, a), (_, _, i) in zip(ajenas, inexistentes) if (a.status_code, a.text) != (i.status_code, i.text)][:2]))
    rev("y el texto del 404 no insinúa que el recurso exista en otra organización (CA-4)",
        not any(x in r.text.lower() for _, _, r in ajenas for x in ("otro cliente", "otra organiz", "de otro", "ajeno")), str([r.text for _, _, r in ajenas][:3]))
    rev("quedó un evento por cada intento con un id de A (6 de webhook, 2 de llave, 2 de bandeja)",
        sorted(e["recurso"] for e in nuevos) == sorted(["webhook"] * 6 + ["llave"] * 2 + ["bandeja"] * 2), str([e["recurso"] for e in nuevos]))
    rev("con el id que se pidió, quién lo pidió, la organización de quien pidió y que se detectó por el id",
        all(e["via"] == "id" and e["usuarioGuid"] == B["admin_guid"] and e["tenantsDelActor"] == [B["guid"]] and e["recursoId"] in (A["webhook"], A["llave"], A["entrada"]) and e["en"] for e in nuevos), str(nuevos[:1]))
    rev("los ids inventados NO dejaron ningún evento (no son un acceso cruzado)", len(cruzados()) == antes + len(nuevos))
    hooks_a = c.get("/webhooks/", params={"tenant": A["guid"]}, headers=cab(A["bearer"])).json()["webhooks"]
    rev("y lo de A sigue intacto: webhook activo, llave viva y entrada pendiente",
        [(w["id"], w["estado"]) for w in hooks_a] == [(A["webhook"], "activo")]
        and c.get("/llaves/", params={"tenant": A["guid"]}, headers=cab(A["bearer"])).json()["llaves"][0]["revocadaEn"] is None
        and [e["id"] for e in c.get("/bandeja/", params={"tenant": A["guid"]}, headers=cab(A["bearer"])).json()["entradas"]] == [A["entrada"]])

    titulo("5 · CA-2: los archivos de una organización no se leen desde otra (y un 404 no confirma que existan)")
    MIME = {"mime": "application/pdf"}
    faltante_de_b = f"{B['guid']}/00/00/{'0' * 64}"
    antes = len(cruzados())
    r_ajeno = c.get(f"/archivos/{A['ruta']}", params=MIME, headers=cab(B["bearer"]))
    r_falta = c.get(f"/archivos/{faltante_de_b}", params=MIME, headers=cab(B["bearer"]))
    cuerpos_de_rechazo.append(r_ajeno.text)
    rev("B pide el archivo de A por su ruta: 404, IDÉNTICO a pedir uno suyo que no existe", r_ajeno.status_code == 404 and (r_ajeno.status_code, r_ajeno.text) == (r_falta.status_code, r_falta.text), f"{r_ajeno.text} / {r_falta.text}")
    ev = cruzados()[antes:]
    rev("y queda anotado: un evento, por la ruta, con la ruta pedida y la organización a la que pertenece",
        len(ev) == 1 and ev[0]["via"] == "ruta" and ev[0]["recurso"] == "archivo" and ev[0]["recursoId"] == A["ruta"] and ev[0]["tenantPedido"] == A["guid"] and ev[0]["usuarioGuid"] == B["admin_guid"], str(ev))
    traversal = c.get(f"/archivos/{B['guid']}/../{A['guid']}/{A['sha'][:2]}/{A['sha'][2:4]}/{A['sha']}", params=MIME, headers=cab(B["bearer"]))
    rev("un `..` para salirse del prefijo de B no entrega el archivo de A", traversal.status_code != 200 and b"%PDF" not in traversal.content, str(traversal.status_code))
    # El espacio heredado.
    antes = len(cruzados())
    r = c.get(f"/archivos/{heredado['rutaRelativa']}", params=MIME, headers=cab(B["bearer"]))
    rev("el prefijo heredado `csi`: a quien pertenece a una organización se le dice que no existe...", r.status_code == 404, str(r.status_code))
    rev("...SIN anotarlo como intento cruzado (es un apuntador viejo suyo, no un ataque)", len(cruzados()) == antes)
    r = c.get(f"/archivos/{heredado['rutaRelativa']}", params=MIME, headers=cab(bearer_super))
    rev("y el administrador de plataforma sí lo lee", r.status_code == 200 and r.content.startswith(b"%PDF"), str(r.status_code))
    r = c.get(f"/archivos/{A['ruta']}", params=MIME, headers=cab(bearer_super))
    rev("pero el administrador de plataforma tampoco lee el de una organización por esta ruta: 404 y anotado",
        r.status_code == 404 and cruzados()[-1]["tenantsDelActor"] == [] and cruzados()[-1]["recursoId"] == A["ruta"], str(r.status_code))
    r = c.post("/archivos/", data={"tenant": "csi"}, files={"archivo": ("x.pdf", b"%PDF-1.4 x", "application/pdf")}, headers=cab(B["bearer"]))
    rev("B tampoco puede subir al prefijo heredado `csi`: 403 y anotado", r.status_code == 403 and cruzados()[-1]["tenantPedido"] == "csi", str(r.status_code))

    titulo("6 · CA-3: el administrador de plataforma opera en el espacio de plataforma, no en el de una organización")
    r = c.get("/webhooks/", params={"tenant": "demo"}, headers=cab(bearer_super))
    r2 = c.post("/webhooks/", json={"tenant": "demo", "url": "https://api.plataforma.com/x", "eventos": ["documento.completado"]}, headers=cab(bearer_super))
    rev("en un tenant que no es de ninguna organización (`demo`) sí: lista y registra", r.status_code == 200 and r2.status_code == 201, f"{r.status_code} {r2.status_code}")
    antes = len(cruzados())
    r = c.get("/webhooks/", params={"tenant": A["guid"]}, headers=cab(bearer_super))
    r2 = c.get("/bandeja/", params={"tenant": A["guid"]}, headers=cab(bearer_super))
    cuerpos_de_rechazo += [r.text, r2.text]
    rev("en el de una organización no: 403, y queda anotado sin organizaciones propias",
        r.status_code == 403 and r2.status_code == 403 and len(cruzados()) == antes + 2 and cruzados()[-1]["tenantsDelActor"] == [], f"{r.status_code} {r2.status_code}")
    # El almacén recorta el tenant antes de usarlo y un montaje que no distingue mayúsculas lleva `ABC` al directorio de `abc`: escribir el
    # guid de una organización con un espacio o en mayúsculas no puede servirle al administrador de plataforma para entrar a ella.
    antes = len(cruzados())
    variantes = [f" {A['guid']}", f"{A['guid']} ", A["guid"].upper()]
    rechazos = [c.get("/webhooks/", params={"tenant": v}, headers=cab(bearer_super)) for v in variantes] + [c.get("/bandeja/", params={"tenant": v}, headers=cab(bearer_super)) for v in variantes]
    rev("y escribir el guid de la organización con un espacio, o en mayúsculas, tampoco abre la puerta: 403 y anotado",
        all(x.status_code == 403 for x in rechazos) and len(cruzados()) == antes + len(rechazos), str([x.status_code for x in rechazos]))

    titulo("7 · Sesión cerrada, cuenta desactivada y llaves de cliente")
    extra = entrar(B["admin_correo"], "Rumbo-Sereno-Azul-47!")
    usuarios.revocar_sesiones(B["admin_guid"], auth.verificar_acceso(extra["accessToken"])["sid"], "PRUEBA")
    r = c.get("/bandeja/", params={"tenant": B["guid"]}, headers=cab(extra["accessToken"]))
    rev("con una sesión cerrada (el JWT todavía vale): 401 sesion_cerrada", r.status_code == 401 and r.json()["detail"]["codigo"] == "sesion_cerrada", r.text[:120])
    mara_b = entrar(B["usuario_correo"], B["usuario_temp"])["accessToken"]
    r = c.get("/webhooks/", params={"tenant": B["guid"]}, headers=cab(mara_b))
    rev("un usuario de B (no administrador) ve lo de SU organización", r.status_code == 200, str(r.status_code))
    antes = len(cruzados())
    r = c.get("/webhooks/", params={"tenant": A["guid"]}, headers=cab(mara_b))
    rev("y no lo de A: 403 y anotado a su nombre", r.status_code == 403 and cruzados()[-1]["usuarioGuid"] == B["usuario"] and len(cruzados()) == antes + 1, str(r.status_code))
    usuarios.cambiar_estado(B["usuario"], False, "prueba")
    r = c.get("/webhooks/", params={"tenant": B["guid"]}, headers=cab(mara_b))
    rev("desactivado: 403 desactivada", r.status_code == 403 and r.json()["detail"]["codigo"] == "desactivada", r.text[:120])
    usuarios.cambiar_estado(B["usuario"], True)
    r = c.post("/bandeja/", files={"archivo": ("p.pdf", b"%PDF-1.4 por la api de A", "application/pdf")}, headers={"x-api-key": A["llave_secreta"]})
    rev("la API Key de A sube a la bandeja de A SIN sesión de nadie (es el flujo de los clientes)", r.status_code == 201, r.text[:160])
    entradas_a = c.get("/bandeja/", params={"tenant": A["guid"]}, headers=cab(A["bearer"])).json()["entradas"]
    rev("y aparece en la bandeja de A, no en la de B",
        len(entradas_a) == 2 and len(c.get("/bandeja/", params={"tenant": B["guid"]}, headers=cab(B["bearer"])).json()["entradas"]) == 1)
    r = c.post("/bandeja/", data={"tenant": B["guid"]}, files={"archivo": ("p.pdf", b"%PDF-1.4 otra", "application/pdf")}, headers={"x-api-key": A["llave_secreta"]})
    rev("y con ella no se puede escribir en B aunque se diga (403, como antes)", r.status_code == 403, str(r.status_code))

    titulo("8 · CA-5 y CA-6: un usuario de otra organización se contesta como uno que no existe, y queda anotado")
    falso = "00000000-0000-4000-8000-000000000000"

    def sobre_usuario(g: str) -> list[tuple[str, object]]:
        return [
            ("POST /usuarios/{guid}", lambda h: c.post(f"/usuarios/{g}", json={"nombre": "Otro", "rol": "VIEWER"}, headers=h)),
            ("POST /usuarios/{guid}/estado", lambda h: c.post(f"/usuarios/{g}/estado", json={"activo": False, "motivo": "intento"}, headers=h)),
            ("POST /usuarios/{guid}/cerrar-sesion", lambda h: c.post(f"/usuarios/{g}/cerrar-sesion", json={"motivo": "intento"}, headers=h)),
            ("POST /usuarios/{guid}/reenviar-invitacion", lambda h: c.post(f"/usuarios/{g}/reenviar-invitacion", headers=h)),
        ]

    antes = len(cruzados())
    de_a = [(n, f(cab(B["bearer"]))) for n, f in sobre_usuario(A["usuario"])]
    de_nadie = [(n, f(cab(B["bearer"]))) for n, f in sobre_usuario(falso)]
    cuerpos_de_rechazo += [r.text for _, r in de_a]
    rev("las 4 rutas sobre un usuario de A, con la sesión del administrador de B: 404, idéntico al de un usuario que no existe",
        all(a.status_code == 404 and (a.status_code, a.text) == (b.status_code, b.text) for (_, a), (_, b) in zip(de_a, de_nadie)), str([(n, r.status_code) for n, r in de_a]))
    ev = cruzados()[antes:]
    rev("cuatro eventos, uno por intento, sobre `usuario` con el guid pedido; el usuario inventado no deja ninguno",
        len(ev) == 4 and all(e["recurso"] == "usuario" and e["recursoId"] == A["usuario"] and e["via"] == "id" and e["usuarioGuid"] == B["admin_guid"] for e in ev), str(ev[:1]))
    rev("y el usuario de A no sufrió nada: sigue activo, con su rol y su sesión",
        usuarios.por_guid(A["usuario"])["activo"] is True and usuarios.por_guid(A["usuario"])["membresias"][0]["rol"] == "ANALISTA")
    r = c.get("/usuarios/", headers=cab(B["bearer"]))
    rev("el listado de B solo trae a los de B", {u["email"] for u in r.json()["usuarios"]} == {B["admin_correo"], B["usuario_correo"]}, r.text[:200])

    titulo("9 · CA-4: ningún rechazo cuenta nada de la otra organización, ni rutas del servidor")
    junto = "\n".join(cuerpos_de_rechazo)
    rev(f"las {len(cuerpos_de_rechazo)} respuestas de rechazo no llevan NINGÚN dato de A: ni su guid, nombre, slug o código, ni sus ids, su archivo, su hash ni sus correos",
        not [x for x in SECRETOS_DE_A if x in junto], str([x for x in SECRETOS_DE_A if x in junto]))
    from servicios import tenants_registro
    from servicios.almacen import ErrorAlmacen

    ruta_secreta = r"Z:\NAS\nexus\.registro\usuarios.jsonl"
    mensaje_del_sistema = f"No se pudo leer el registro usuarios: [Errno 13] Permission denied: '{ruta_secreta}'"

    def falla(*args, **kwargs):
        raise ErrorAlmacen(mensaje_del_sistema)

    cuerpos_503 = {}
    originales = (usuarios.de_tenant, tenants_registro.listar, usuarios.para_login, usuarios.actualizar_perfil)
    try:
        usuarios.de_tenant = falla
        cuerpos_503["GET /usuarios/"] = c.get("/usuarios/", headers=cab(B["bearer"]))
        usuarios.de_tenant = originales[0]
        tenants_registro.listar = falla
        cuerpos_503["GET /organizaciones/"] = c.get("/organizaciones/", headers=cab(bearer_super))
        tenants_registro.listar = originales[1]
        usuarios.para_login = falla
        cuerpos_503["POST /auth/login"] = c.post("/auth/login", json={"email": "nadie@ejemplo.com", "password": "x", "ip": "127.0.0.1"}, headers=cab())
        usuarios.para_login = originales[2]
        usuarios.actualizar_perfil = falla
        cuerpos_503["POST /auth/perfil"] = c.post("/auth/perfil", json={"nombre": "Ada"}, headers=cab(B["bearer"]))
    finally:
        usuarios.de_tenant, tenants_registro.listar, usuarios.para_login, usuarios.actualizar_perfil = originales
    rev("cuando el almacén falla, usuarios, organizaciones, login y perfil responden 503",
        all(r.status_code == 503 for r in cuerpos_503.values()), str({k: r.status_code for k, r in cuerpos_503.items()}))
    rev("con un mensaje FIJO: ni la ruta del servidor, ni el nombre del registro, ni el texto del sistema operativo",
        not any(x in r.text for r in cuerpos_503.values() for x in ("Z:", "NAS", "jsonl", "Errno", "Permission", "registro usuarios")), str([r.text[:160] for r in cuerpos_503.values()][:2]))
finally:
    c.close()
    shutil.rmtree(RAIZ, ignore_errors=True)

print("\n" + "=" * 72)
print(f"{fallos} FALLARON" if fallos else "todas las comprobaciones pasaron")
raise SystemExit(1 if fallos else 0)
