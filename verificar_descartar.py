"""¿Descartar un documento de la bandeja de preparación funciona, lo puede hacer quien debe y deja rastro?

    .venv/Scripts/python verificar_descartar.py

OFFLINE y sin tocar nada tuyo, como los demás `verificar_*`.

«Descartar» es sacar la entrada de la bandeja con `POST /bandeja/{id}/retirar` y motivo `descartado`. Lo que se prueba:

- sale de la bandeja y NO se borra el archivo del almacén (la deduplicación por contenido lo vuelve peligroso);
- el motivo que escribe la persona (`detalle`) queda en el registro, con su tope;
- solo lo pueden hacer los roles que trabajan la bandeja (administrador, supervisor, analista y operador) y el administrador de
  plataforma; compliance, auditor y viewer reciben 403 y NO se toca nada;
- cada descarte deja `DOCUMENT_DISCARDED` (quién, desde qué sesión, qué documento, de qué organización, por qué);
- descartar dos veces, o algo que no existe, es 404 y no deja un segundo rastro.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-descartar-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

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


def descartes() -> list[dict]:
    ruta = RAIZ / ".registro" / "auditoria.jsonl"
    if not ruta.exists():
        return []
    return [e for e in (json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip()) if e.get("accion") == "DOCUMENT_DISCARDED"]


def retiros(tenant: str) -> list[dict]:
    ruta = RAIZ / ".registro" / f"bandeja-{tenant}.jsonl"
    return [e for e in (json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip()) if e.get("evento") == "retiro"]


try:
    titulo("0 · Montaje: una organización con un usuario de cada rol, y el administrador de plataforma")
    temporal_super = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal_super))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal_super, "Cobre-Lento-Azul-47!")
    r = c.post("/organizaciones/", json={"nombre": "Seguros Alfa", "adminNombre": "Rita", "adminApellidoPaterno": "Zeta", "adminEmail": "rita@ejemplo.com"}, headers=cab(bearer_super))
    assert r.status_code == 201, r.text
    org = r.json()["organizacion"]["guid"]
    bearers = {"ADMIN": con_primer_acceso("rita@ejemplo.com", r.json()["contrasenaTemporal"], "Rumbo-Sereno-Azul-47!")}
    ROLES = ["SUPERVISOR", "ANALISTA", "OPERADOR", "COMPLIANCE_OFFICER", "AUDITOR", "VIEWER"]
    for rol in ROLES:
        correo = f"{rol.lower()}@ejemplo.com"
        r = c.post("/usuarios/", json={"nombre": rol.title(), "apellidoPaterno": "Prueba", "email": correo, "rol": rol}, headers=cab(bearers["ADMIN"]))
        assert r.status_code == 201, r.text
        bearers[rol] = con_primer_acceso(correo, r.json()["contrasenaTemporal"], "Cielo-Abierto-Verde-52!")
    rev("el administrador y los seis roles tienen sesión", len(bearers) == 7)

    def nueva_entrada(tenant: str, nombre: str, contenido: bytes | None = None) -> dict:
        g = almacen.guardar(tenant, contenido or f"%PDF-1.4 {nombre}".encode())
        e = bandeja.registrar_entrada(tenant, g, "application/pdf", nombre, "API")
        e["_ruta"], e["_sha"] = g["rutaRelativa"], g["hash"]
        return e

    def descartar(bearer: str, tenant: str, entrada_id: str, detalle: str | None = None, motivo: str = "descartado"):
        datos = {"tenant": tenant, "motivo": motivo, **({"detalle": detalle} if detalle is not None else {})}
        return c.post(f"/bandeja/{entrada_id}/retirar", data=datos, headers=cab(bearer))

    def pendientes(tenant: str, bearer: str) -> list[str]:
        return [e["id"] for e in c.get("/bandeja/", params={"tenant": tenant}, headers=cab(bearer)).json()["entradas"]]

    titulo("1 · El administrador descarta: sale de la bandeja, el archivo se queda y queda el rastro")
    e1 = nueva_entrada(org, "factura-duplicada.pdf")
    rev("la entrada está pendiente", e1["id"] in pendientes(org, bearers["ADMIN"]))
    r = descartar(bearers["ADMIN"], org, e1["id"], "Es un duplicado de la factura 1234")
    rev("responde 200", r.status_code == 200 and r.json() == {"retirada": True}, r.text[:160])
    rev("ya no está en la bandeja", e1["id"] not in pendientes(org, bearers["ADMIN"]))
    rev("pero el archivo SIGUE en el almacén (descartar no borra bytes)", almacen.existe(e1["_ruta"]))
    rt = retiros(org)[-1]
    rev("el retiro guarda el motivo y lo que escribió la persona", rt["motivo"] == "descartado" and rt.get("detalle") == "Es un duplicado de la factura 1234", str(rt))
    ev = descartes()
    rev("deja UN evento DOCUMENT_DISCARDED", len(ev) == 1, str(len(ev)))
    e = ev[0] if ev else {}
    rev("con quién, desde qué sesión, de qué organización y qué documento",
        e.get("usuarioGuid") == usuarios.para_login("rita@ejemplo.com")["guid"] and e.get("sesionGuid") and e.get("tenant") == org and e.get("entradaId") == e1["id"]
        and e.get("nombreOriginal") == "factura-duplicada.pdf" and e.get("sha256") == e1["_sha"] and e.get("canal") == "API", str(e))
    rev("y por qué", e.get("detalle") == "Es un duplicado de la factura 1234", str(e))

    titulo("1b · Lo descartado se puede consultar (el filtro «Descartado» de la bandeja)")
    r = c.get("/bandeja/", params={"tenant": org, "estado": "descartados"}, headers=cab(bearers["ADMIN"]))
    lista = r.json()["entradas"] if r.status_code == 200 else []
    d1 = next((x for x in lista if x["id"] == e1["id"]), {})
    rev("`estado=descartados` trae la entrada descartada, con cuándo y con su motivo",
        r.status_code == 200 and d1.get("nombreOriginal") == "factura-duplicada.pdf" and d1.get("descartadaEn") and d1.get("detalle") == "Es un duplicado de la factura 1234", str(d1))
    rev("y con lo que se necesita para pintarla (tamaño, tipo, canal, huella)", all(k in d1 for k in ("tamanoBytes", "mime", "canal", "sha256", "recibidoEn")), str(d1))
    rev("la lista de pendientes ya no la trae (sigue igual que antes)", e1["id"] not in pendientes(org, bearers["ADMIN"]))
    e_otra = nueva_entrada(org, "sigue-pendiente.pdf")
    rev("y una entrada pendiente NO sale entre los descartados", e_otra["id"] not in [x["id"] for x in c.get("/bandeja/", params={"tenant": org, "estado": "descartados"}, headers=cab(bearers["ADMIN"])).json()["entradas"]])
    rev("quien solo consulta (AUDITOR) también la ve: ver no destruye nada",
        c.get("/bandeja/", params={"tenant": org, "estado": "descartados"}, headers=cab(bearers["AUDITOR"])).status_code == 200)
    rev("un estado que no existe: 422", c.get("/bandeja/", params={"tenant": org, "estado": "otro"}, headers=cab(bearers["ADMIN"])).status_code == 422)
    rev("otra organización no ve los descartados de esta (403 sin_acceso, como el resto)",
        c.get("/bandeja/", params={"tenant": "demo", "estado": "descartados"}, headers=cab(bearers["ADMIN"])).status_code == 403)
    rev("el administrador de plataforma en su espacio ve su lista vacía, no la de la organización",
        c.get("/bandeja/", params={"tenant": "demo", "estado": "descartados"}, headers=cab(bearer_super)).json()["entradas"] == [])
    c.post(f"/bandeja/{e_otra['id']}/retirar", data={"tenant": org, "motivo": "descartado"}, headers=cab(bearers["ADMIN"]))

    titulo("2 · Descartar otra vez, o algo que no existe: 404 y un solo rastro")
    antes = len(descartes())
    r = descartar(bearers["ADMIN"], org, e1["id"])
    r2 = descartar(bearers["ADMIN"], org, "f" * 32)
    rev("ya descartada: 404", r.status_code == 404, r.text[:120])
    rev("inexistente: 404", r2.status_code == 404, r2.text[:120])
    rev("ninguno dejó un segundo DOCUMENT_DISCARDED", len(descartes()) == antes)

    titulo("3 · El motivo es opcional y tiene tope")
    e2 = nueva_entrada(org, "sin-motivo.pdf")
    r = descartar(bearers["ADMIN"], org, e2["id"])
    rev("sin motivo se descarta igual", r.status_code == 200, r.text[:120])
    rev("y el rastro lo dice (sin detalle)", descartes()[-1].get("detalle") in (None, ""), str(descartes()[-1]))
    e3 = nueva_entrada(org, "motivo-largo.pdf")
    r = descartar(bearers["ADMIN"], org, e3["id"], "x" * 501)
    rev("un motivo de más de 500 caracteres se rechaza (422)", r.status_code == 422, str(r.status_code))
    rev("y no se descartó nada", e3["id"] in pendientes(org, bearers["ADMIN"]))
    r = descartar(bearers["ADMIN"], org, e3["id"], "  espacios   de más  ")
    rev("el motivo se guarda sin espacios sobrantes", r.status_code == 200 and descartes()[-1]["detalle"] == "espacios de más", str(descartes()[-1]))

    titulo("4 · Quién puede")
    puede = {"SUPERVISOR": True, "ANALISTA": True, "OPERADOR": True, "COMPLIANCE_OFFICER": False, "AUDITOR": False, "VIEWER": False}
    for rol, debe in puede.items():
        ent = nueva_entrada(org, f"de-{rol.lower()}.pdf")
        antes = len(descartes())
        r = descartar(bearers[rol], org, ent["id"], f"lo descarta {rol}")
        if debe:
            rev(f"{rol}: puede (200) y queda su rastro", r.status_code == 200 and len(descartes()) == antes + 1 and ent["id"] not in pendientes(org, bearers["ADMIN"]), f"{r.status_code} {r.text[:100]}")
        else:
            rev(f"{rol}: 403 sin_permiso, la entrada sigue pendiente y NO queda rastro de descarte",
                r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso" and ent["id"] in pendientes(org, bearers["ADMIN"]) and len(descartes()) == antes,
                f"{r.status_code} {r.text[:100]}")
    rev("el mensaje del 403 no nombra ningún rol ni dato de nadie", "no puede descartar" in r.json()["detail"]["mensaje"].lower() and org not in r.text)
    r = descartar(bearers["VIEWER"], org, "f" * 32)
    rev("y un VIEWER que descarta algo inexistente recibe el mismo 403 (no se entera de si existe)", r.status_code == 403, str(r.status_code))
    # Mandarla al pipeline no es descartar: esta historia solo cierra lo que destruye el trabajo de otro (queda dicho en el README).
    ent = nueva_entrada(org, "para-pipeline.pdf")
    antes = len(descartes())
    r = descartar(bearers["VIEWER"], org, ent["id"], motivo="pipeline")
    rev("retirar por «pipeline» no es un descarte: sigue igual que antes y no deja DOCUMENT_DISCARDED", r.status_code == 200 and len(descartes()) == antes, f"{r.status_code} {r.text[:100]}")

    titulo("5 · El administrador de plataforma descarta en su espacio")
    ent = nueva_entrada("demo", "de-plataforma.pdf")
    r = descartar(bearer_super, "demo", ent["id"], "limpieza")
    rev("200 y su rastro, sin organizaciones propias", r.status_code == 200 and descartes()[-1]["tenant"] == "demo" and descartes()[-1]["usuarioGuid"], f"{r.status_code} {r.text[:100]}")

    titulo("6 · Un descarte con el almacén caído no se traga el rastro en silencio")
    ent = nueva_entrada(org, "con-bitacora-caida.pdf")
    from servicios import auditoria
    from servicios.almacen import ErrorAlmacen

    original = auditoria.registrar

    def falla(*a, **k):
        raise ErrorAlmacen("No se pudo escribir en Z:\\NAS\\nexus\\.registro\\auditoria.jsonl")

    auditoria.registrar = falla
    try:
        r = descartar(bearers["ADMIN"], org, ent["id"], "prueba")
    finally:
        auditoria.registrar = original
    rev("si la bitácora falla el descarte se completa (ya estaba hecho) y no hay 500 ni rutas del servidor en la respuesta",
        r.status_code == 200 and "Z:" not in r.text and "jsonl" not in r.text, f"{r.status_code} {r.text[:120]}")
finally:
    c.close()
    shutil.rmtree(RAIZ, ignore_errors=True)

print("\n" + "=" * 72)
print(f"{fallos} FALLARON" if fallos else "todas las comprobaciones pasaron")
raise SystemExit(1 if fallos else 0)
