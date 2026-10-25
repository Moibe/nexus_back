"""¿Cada rol hace solo lo que le toca? (NEX-472, hallazgo de la auditoría de QA)

    .venv/Scripts/python verificar_roles.py

OFFLINE y sin tocar nada tuyo, como los demás `verificar_*`: levanta la app contra un `ALMACEN_RUTA` temporal.

Hasta el 2026-10-22 las rutas de API Keys, webhooks, procesos y tipos documentales solo comprobaban que la persona perteneciera a la
organización: un Auditor podía leer y CREAR API Keys y webhooks. Ahora piden un rol que administre (`roles.PUEDEN_ADMINISTRAR`:
Administrador y Supervisor; el administrador de plataforma siempre, en su espacio). Aquí hay UNA organización con una persona de cada
rol y cada una prueba, con petición directa (sin pasar por la pantalla), todas las rutas:

- Administrador y Supervisor: todo sigue funcionando (CA2).
- Analista, Operador, Compliance Officer, Auditor y Viewer: 403 `sin_permiso` en cada ruta de administración, también en las de
  solo lectura, y NADA cambia en el servidor (CA1 y CA4).
- Lo que opera el pipeline queda abierto a todos los roles: leer la biblioteca de tipos documentales y el aviso `/webhooks/eventos`
  (CA3, regresión).
- Cada rechazo deja `ROLE_DENIED` en la bitácora, y el cuerpo del rechazo no revela qué rol sí puede.
- Quien NO es de la organización sigue recibiendo `sin_acceso` (el aislamiento va primero), no `sin_permiso`.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-roles-"))
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
from servicios import auth, bandeja, almacen, usuarios  # noqa: E402

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


def auditoria(accion: str) -> list[dict]:
    ruta = RAIZ / ".registro" / "auditoria.jsonl"
    if not ruta.exists():
        return []
    return [e for e in (json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip()) if e.get("accion") == accion]


ROLES_QUE_ADMINISTRAN = ["ADMIN", "SUPERVISOR"]
ROLES_SIN_ADMINISTRAR = ["ANALISTA", "OPERADOR", "COMPLIANCE_OFFICER", "AUDITOR", "VIEWER"]

try:
    titulo("0 · Montaje: una organización con una persona de cada rol, y una segunda organización ajena")
    temporal_super = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal_super))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal_super, "Cobre-Lento-Azul-47!")

    def crear_org(nombre: str, correo: str) -> dict:
        r = c.post("/organizaciones/", json={"nombre": nombre, "adminNombre": "Rita", "adminApellidos": "Zeta", "adminEmail": correo}, headers=cab(bearer_super))
        assert r.status_code == 201, r.text
        cuerpo = r.json()
        return {"guid": cuerpo["organizacion"]["guid"], "slug": cuerpo["organizacion"]["slug"],
                "ADMIN": con_primer_acceso(correo, cuerpo["contrasenaTemporal"], "Rumbo-Sereno-Azul-47!")}

    O = crear_org("Seguros Alfa Omega", "rita.alfa@ejemplo.com")
    AJENA = crear_org("Textiles Beta Sigma", "tomas.beta@ejemplo.com")
    for rol in ["SUPERVISOR"] + ROLES_SIN_ADMINISTRAR:
        correo = f"{rol.lower()}@ejemplo.com"
        r = c.post("/usuarios/", json={"nombre": rol.title(), "email": correo, "rol": rol}, headers=cab(O["ADMIN"]))
        assert r.status_code == 201, r.text
        O[rol] = con_primer_acceso(correo, r.json()["contrasenaTemporal"], "Rumbo-Sereno-Azul-47!")
    rev("la organización tiene una persona activa por cada uno de los 7 roles", all(O.get(r) for r in ROLES_QUE_ADMINISTRAN + ROLES_SIN_ADMINISTRAR))

    # Lo que existe de antemano, para que las rutas con identificador tengan sobre qué actuar.
    r = c.post("/webhooks/", json={"tenant": O["guid"], "url": "https://api.alfa.com/avisos", "eventos": ["documento.completado"]}, headers=cab(O["ADMIN"]))
    assert r.status_code == 201, r.text
    WEBHOOK = r.json()["webhook"]["id"]
    r = c.post("/llaves/", json={"tenant": O["guid"], "nombre": "Llave de base", "descripcion": "x", "dias": 30}, headers=cab(O["ADMIN"]))
    assert r.status_code == 201, r.text
    LLAVE = r.json()["llave"]["id"]
    g = almacen.guardar(O["guid"], b"%PDF-1.4 contenido propio")
    ENTRADA = bandeja.registrar_entrada(O["guid"], g, "application/pdf", "alfa.pdf", "API")["id"]
    DATOS_TIPO = {"nombre": "Tipo de prueba", "estado": "borrador"}
    PROCESO = {"nombre": "Proceso de prueba", "nodos": [], "aristas": []}

    periodo = {"desde": "2026-10-01T00:00:00-06:00", "hasta": "2026-10-02T00:00:00-06:00"}

    def rutas_de_administracion(t: str, h: dict) -> list[tuple[str, object]]:
        """Cada ruta que pide el rol de administración, ejecutada con las cabeceras `h` (los idempotentes y los que no cambian nada
        van primero; los que sí cambian el servidor se prueban aparte, con su antes y después)."""
        return [
            ("GET /llaves/", lambda: c.get("/llaves/", params={"tenant": t}, headers=h)),
            ("GET /llaves/{id}/metricas", lambda: c.get(f"/llaves/{LLAVE}/metricas", params={"tenant": t, **periodo}, headers=h)),
            ("GET /webhooks/", lambda: c.get("/webhooks/", params={"tenant": t}, headers=h)),
            ("GET /webhooks/{id}/metricas", lambda: c.get(f"/webhooks/{WEBHOOK}/metricas", params={"tenant": t, **periodo}, headers=h)),
            ("GET /webhooks/{id}/intentos", lambda: c.get(f"/webhooks/{WEBHOOK}/intentos", params={"tenant": t}, headers=h)),
            ("GET /procesos/", lambda: c.get("/procesos/", params={"tenant": t}, headers=h)),
        ]

    def rutas_que_cambian(t: str, h: dict) -> list[tuple[str, object]]:
        return [
            ("POST /llaves/", lambda: c.post("/llaves/", json={"tenant": t, "nombre": "Intrusa", "descripcion": "x", "dias": 7}, headers=h)),
            ("POST /llaves/{id}/revocar", lambda: c.post(f"/llaves/{LLAVE}/revocar", json={"tenant": t}, headers=h)),
            ("POST /webhooks/", lambda: c.post("/webhooks/", json={"tenant": t, "url": "https://api.intrusa.com/x", "eventos": ["documento.completado"]}, headers=h)),
            ("POST /webhooks/{id}/estado", lambda: c.post(f"/webhooks/{WEBHOOK}/estado", json={"tenant": t, "estado": "inactivo"}, headers=h)),
            ("POST /webhooks/{id}/validar", lambda: c.post(f"/webhooks/{WEBHOOK}/validar", json={"tenant": t}, headers=h)),
            ("POST /webhooks/{id}/editar", lambda: c.post(f"/webhooks/{WEBHOOK}/editar", json={"tenant": t, "url": "https://api.intrusa.com/y", "eventos": ["documento.completado"]}, headers=h)),
            ("POST /webhooks/{id}/eliminar", lambda: c.post(f"/webhooks/{WEBHOOK}/eliminar", json={"tenant": t}, headers=h)),
            ("PUT /procesos/{id}", lambda: c.put("/procesos/proceso-intruso", json={"tenant": t, "revision": 0, "datos": PROCESO}, headers=h)),
            ("DELETE /procesos/{id}", lambda: c.delete("/procesos/proceso-intruso", params={"tenant": t}, headers=h)),
            ("PUT /tipos-documentales/{id}", lambda: c.put("/tipos-documentales/tipo-intruso", json={"tenant": t, "revision": 0, "datos": DATOS_TIPO}, headers=h)),
            ("PUT /tipos-documentales/orden", lambda: c.put("/tipos-documentales/orden", json={"tenant": t, "ids": []}, headers=h)),
            ("DELETE /tipos-documentales/{id}", lambda: c.delete("/tipos-documentales/tipo-intruso", params={"tenant": t}, headers=h)),
        ]

    def estado_del_servidor() -> tuple:
        """Lo que NADA debe cambiar cuando se rechaza: las llaves, los webhooks y los procesos de la organización, y los tipos."""
        hs = cab(O["ADMIN"])
        llaves = c.get("/llaves/", params={"tenant": O["guid"]}, headers=hs).json()["llaves"]
        hooks = c.get("/webhooks/", params={"tenant": O["guid"]}, headers=hs).json()["webhooks"]
        procesos = c.get("/procesos/", params={"tenant": O["guid"]}, headers=hs).json()
        tipos = c.get("/tipos-documentales/", params={"tenant": O["guid"]}, headers=hs).json()
        return (json.dumps(llaves, sort_keys=True), json.dumps(hooks, sort_keys=True), json.dumps(procesos, sort_keys=True), json.dumps(tipos, sort_keys=True))

    titulo("1 · CA1 y CA4: quien no administra recibe 403 sin_permiso en CADA ruta de administración, por petición directa")
    antes = estado_del_servidor()
    rechazos_esperados = 0
    for rol in ROLES_SIN_ADMINISTRAR:
        h = cab(O[rol])
        todas = rutas_de_administracion(O["guid"], h) + rutas_que_cambian(O["guid"], h)
        malas = []
        cuerpos = []
        for nombre, llamar in todas:
            r = llamar()
            rechazos_esperados += 1
            cuerpos.append(r.text)
            detalle = r.json().get("detail") if r.headers.get("content-type", "").startswith("application/json") else None
            if not (r.status_code == 403 and isinstance(detalle, dict) and detalle.get("codigo") == "sin_permiso"):
                malas.append((nombre, r.status_code))
        rev(f"{rol}: las {len(todas)} rutas de administración dan 403 sin_permiso", not malas, str(malas))
        revela = [t for t in cuerpos if "SUPERVISOR" in t or "ADMIN" in t or "Supervisor" in t or "Administrador" in t]
        rev(f"{rol}: ningún rechazo nombra qué rol sí puede", not revela, str(revela[:1]))
    rev("tras todos los rechazos NADA cambió en el servidor (llaves, webhooks, procesos y tipos idénticos)", estado_del_servidor() == antes)
    rev("cada rechazo dejó su ROLE_DENIED en la bitácora", len(auditoria("ROLE_DENIED")) == rechazos_esperados, f"{len(auditoria('ROLE_DENIED'))} de {rechazos_esperados}")
    ev = auditoria("ROLE_DENIED")[-1]
    rev("el ROLE_DENIED lleva quién, de qué organización, qué intentó y con qué rol", all(ev.get(k) for k in ("usuarioGuid", "tenant", "recurso", "roles")), str(ev))

    titulo("2 · CA2: Administrador y Supervisor siguen pudiendo todo")
    for rol in ROLES_QUE_ADMINISTRAN:
        h = cab(O[rol])
        malas = [(n, r.status_code) for n, f in rutas_de_administracion(O["guid"], h) for r in [f()] if r.status_code != 200]
        rev(f"{rol}: las rutas de lectura responden 200", not malas, str(malas))
        r = c.post("/llaves/", json={"tenant": O["guid"], "nombre": f"Llave de {rol}", "descripcion": "x", "dias": 7}, headers=h)
        rev(f"{rol}: crea una API Key (201)", r.status_code == 201, r.text[:120])
        r = c.post("/webhooks/", json={"tenant": O["guid"], "url": f"https://api.{rol.lower()}.com/x", "eventos": ["documento.completado"]}, headers=h)
        rev(f"{rol}: crea un webhook (201)", r.status_code == 201, r.text[:120])
        r = c.put(f"/procesos/proceso-{rol.lower()}-ok", json={"tenant": O["guid"], "revision": 0, "datos": PROCESO}, headers=h)
        rev(f"{rol}: guarda un proceso (200)", r.status_code in (200, 201), f"{r.status_code} {r.text[:120]}")
        r = c.put(f"/tipos-documentales/tipo-{rol.lower()}-ok", json={"tenant": O["guid"], "revision": 0, "datos": DATOS_TIPO}, headers=h)
        rev(f"{rol}: guarda un tipo documental (200)", r.status_code in (200, 201), f"{r.status_code} {r.text[:120]}")
    r = c.post("/webhooks/", json={"tenant": "demo", "url": "https://api.plataforma.com/x", "eventos": ["documento.completado"]}, headers=cab(bearer_super))
    rev("el administrador de plataforma, en su espacio, también (201)", r.status_code == 201, f"{r.status_code} {r.text[:120]}")

    titulo("3 · CA3 (regresión): lo que opera el pipeline sigue abierto a TODOS los roles de la organización")
    for rol in ROLES_QUE_ADMINISTRAN + ROLES_SIN_ADMINISTRAR:
        r = c.get("/tipos-documentales/", params={"tenant": O["guid"]}, headers=cab(O[rol]))
        rev(f"{rol}: lee la biblioteca de tipos documentales (200)", r.status_code == 200, f"{r.status_code} {r.text[:100]}")
        r = c.post("/webhooks/eventos", json={"tenant": O["guid"], "tipo": "documento.completado", "entradaId": "no-existe"}, headers=cab(O[rol]))
        rev(f"{rol}: el aviso /webhooks/eventos llega al negocio (404 de «no existe», no 403)", r.status_code == 404, f"{r.status_code} {r.text[:100]}")
        r = c.get("/bandeja/", params={"tenant": O["guid"]}, headers=cab(O[rol]))
        rev(f"{rol}: lee la bandeja (200)", r.status_code == 200, f"{r.status_code} {r.text[:100]}")

    titulo("4 · El aislamiento va primero: quien no es de la organización recibe sin_acceso, no sin_permiso")
    r = c.get("/llaves/", params={"tenant": O["guid"]}, headers=cab(AJENA["ADMIN"]))
    d = r.json().get("detail", {})
    rev("el Administrador de OTRA organización: 403 sin_acceso", r.status_code == 403 and d.get("codigo") == "sin_acceso", f"{r.status_code} {d}")
    r = c.get("/webhooks/", params={"tenant": O["guid"]}, headers=cab(bearer_super))
    rev("el administrador de plataforma NO entra por estas rutas a una organización (sin_acceso)", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso", f"{r.status_code} {r.text[:100]}")

finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print("\n" + ("TODO BIEN" if fallos == 0 else f"{fallos} FALLAS"))
raise SystemExit(0 if fallos == 0 else 1)
