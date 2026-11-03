"""¿Los GRUPOS funcionan de punta a punta? (migración `0010`, `servicios/grupos.py`)

    .venv/Scripts/python verificar_grupos.py
    .venv/Scripts/python verificar_grupos.py --dejar     (deja vivo el contenedor para investigar)

Los grupos SOLO viven en SQL (decisión del 2026-10-28: «todo lo nuevo directo a SQL»), así que esta prueba corre siempre contra el SQL Server
desechable de `pruebas_sql.py`; sin Docker se salta y lo dice. El almacén (bandeja, usuarios, auditoría) es una carpeta temporal.

Lo que se prueba (decisión de producto de Moibe, 2026-11-03: varios expedientes viven en un grupo, un solo nivel; el grupo se crea antes y se elige
al crear el expediente; los que ya existían se asignan después):

- un grupo nace con su nombre limpio, vacío y con un id que pone el servidor; el nombre es único por organización sin distinguir mayúsculas ni espacios
  de más (otra organización sí puede repetirlo) y los nombres que no sirven se rechazan;
- listar y ver enseñan cuántos expedientes tiene cada grupo; lo de otra organización contesta 404 (o 403 con su tenant);
- un expediente NUEVO exige grupo (400 sin él, 404 con uno que no existe o es de otra organización, y entonces no se crea nada) y lo trae en la lista y en
  el detalle; uno de ANTES de la migración no tiene (`grupo: null`) y se le asigna después; asignar es idempotente, pasa a otro grupo y nunca lo deja sin;
- renombrar deja el id, los expedientes ven el nombre nuevo, solo cambiar las mayúsculas del propio nombre se puede y chocar con otro grupo no;
- borrar solo se puede sin expedientes (409 con cuántos) y libera el nombre; borrar mientras se crean expedientes nunca deja uno huérfano;
- la BASE cuida lo que el servicio ya comprueba: un expediente no puede apuntar a un grupo de otra organización, y los nombres no se repiten ni por debajo;
- Administrador y Supervisor crean, renombran y borran; Analista y Operador organizan (crean expedientes y los asignan) pero no tocan grupos; Viewer solo lee;
- cada cosa deja su rastro en la auditoría.
"""

import os
import shutil
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pruebas_sql  # noqa: E402

BASE = pruebas_sql.preparar("nexus-grupos-prueba", 14336, dejar="--dejar" in sys.argv)

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-grupos-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"

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


def grupo(bearer: str, tenant: str, nombre: str):
    return c.post("/grupos/", json={"tenant": tenant, "nombre": nombre}, headers=cab(bearer))


def grupos_de(bearer: str, tenant: str):
    return c.get("/grupos/", params={"tenant": tenant}, headers=cab(bearer))


def ver_grupo(bearer: str, tenant: str, g: str):
    return c.get(f"/grupos/{g}", params={"tenant": tenant}, headers=cab(bearer))


def renombrar(bearer: str, tenant: str, g: str, nombre: str):
    return c.patch(f"/grupos/{g}", json={"tenant": tenant, "nombre": nombre}, headers=cab(bearer))


def borrar(bearer: str, tenant: str, g: str):
    return c.delete(f"/grupos/{g}", params={"tenant": tenant}, headers=cab(bearer))


def expediente(bearer: str, tenant: str, nombre: str, g: str | None):
    cuerpo = {"tenant": tenant, "nombre": nombre, **({"grupoId": g} if g is not None else {})}
    return c.post("/expedientes/", json=cuerpo, headers=cab(bearer))


def asignar(bearer: str, tenant: str, exp: str, g: str):
    return c.put(f"/expedientes/{exp}/grupo", json={"tenant": tenant, "grupoId": g}, headers=cab(bearer))


def ver_exp(bearer: str, tenant: str, exp: str):
    return c.get(f"/expedientes/{exp}", params={"tenant": tenant}, headers=cab(bearer))


def lista_exp(bearer: str, tenant: str) -> list[dict]:
    return c.get("/expedientes/", params={"tenant": tenant}, headers=cab(bearer)).json()["expedientes"]


def total(bearer: str, tenant: str, g: str) -> int:
    return ver_grupo(bearer, tenant, g).json()["totalExpedientes"]


def auditoria(accion: str) -> list[dict]:
    # Por el servicio y no leyendo el JSONL: con SQL la bitácora vive en nexus.auditEvents (migración 0006).
    from servicios import auditoria as bitacora

    return bitacora.eventos({accion})


def sql_sa(consulta: str, *parametros):
    """Una instrucción directa a la base, con el usuario de la API (el que tiene los permisos que tiene la app) o con `sa`."""
    con = BASE["conectar"]("sa", BASE["sa"], BASE["base"])
    try:
        cur = con.cursor()
        cur.execute(consulta, *parametros)
        return cur.fetchall() if cur.description else None
    finally:
        con.close()


def expediente_de_antes(tenant: str, nombre: str) -> str:
    """Un expediente como los que había ANTES de la migración 0010: sin grupo. No hay forma de crearlo por la API (ya lo exige), así que va directo a la base."""
    pid = f"expediente-{uuid.uuid4().hex}"
    sql_sa("INSERT INTO nexus.cases (publicId, tenant, name, createdBy) VALUES (?, ?, ?, ?)", pid, tenant, nombre, "guid-de-antes")
    return pid


try:
    titulo("0 · Montaje: dos organizaciones y una persona de cada rol")
    temporal = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal, "Cobre-Lento-Azul-47!")
    org, adm = organizacion(bearer_super, "Alfa", "rita@ejemplo.com")
    otra, adm_b = organizacion(bearer_super, "Beta", "beto@ejemplo.com")
    guid_rita = usuarios.para_login("rita@ejemplo.com")["guid"]
    P = {}
    for rol in ("SUPERVISOR", "ANALISTA", "OPERADOR", "VIEWER"):
        correo = f"{rol.lower()}@ejemplo.com"
        r = c.post("/usuarios/", json={"nombre": rol.title(), "apellidoPaterno": "Prueba", "email": correo, "rol": rol}, headers=cab(adm))
        assert r.status_code == 201, r.text
        P[rol] = con_primer_acceso(correo, r.json()["contrasenaTemporal"], "Cielo-Abierto-Verde-52!")
    rev("una organización recién creada no tiene grupos", grupos_de(adm, org).json() == {"grupos": []})

    titulo("1 · Crear un grupo")
    r = grupo(adm, org, "  Ventas   Norte ")
    rev("crear: 201", r.status_code == 201, r.text[:250])
    ventas = r.json()
    rev("su id lo pone el servidor y el nombre sale limpio", ventas["id"].startswith("grupo-") and len(ventas["id"]) == 6 + 32 and ventas["nombre"] == "Ventas Norte", str(ventas))
    rev("nace vacío, con quién lo creó y cuándo", ventas["totalExpedientes"] == 0 and ventas["creadoPor"] == guid_rita and ventas["creadoEn"] and ventas["actualizadoEn"] is None, str(ventas))
    ventas = ventas["id"]
    for repetido, que in (("ventas norte", "las mismas palabras en minúsculas"), ("VENTAS  NORTE", "en mayúsculas y con espacios de más")):
        r = grupo(adm, org, repetido)
        rev(f"otro con el mismo nombre ({que}): 409 `ya_existe`", r.status_code == 409 and r.json()["detail"]["codigo"] == "ya_existe", r.text[:200])
    rev("otra organización SÍ puede llamar así a uno suyo", grupo(adm_b, otra, "Ventas Norte").status_code == 201)
    for malo, que in (("", "vacío"), ("   ", "solo espacios"), ("x" * 201, "de 201 caracteres"), ("a\u0000b", "con un carácter de control")):
        rev(f"un nombre {que}: 400", grupo(adm, org, malo).status_code == 400)
    rev("uno de 200 caracteres sí", grupo(adm, org, "y" * 200).status_code == 201)
    rev("sin decir el nombre: 422", c.post("/grupos/", json={"tenant": org}, headers=cab(adm)).status_code == 422)
    rh = grupo(adm, org, "T1 2026")
    rev("el segundo grupo, también", rh.status_code == 201)
    t1 = rh.json()["id"]

    titulo("2 · Listar y ver")
    lista = grupos_de(adm, org).json()["grupos"]
    rev("la lista trae los de la organización, en el orden de su creación", [g["nombre"] for g in lista] == ["Ventas Norte", "y" * 200, "T1 2026"], str([g["nombre"] for g in lista]))
    de_beta = grupos_de(adm_b, otra).json()["grupos"]
    rev("no trae los de la otra organización (y ella solo ve el suyo)", [g["nombre"] for g in de_beta] == ["Ventas Norte"] and ventas not in {g["id"] for g in de_beta} and all(g["id"] != de_beta[0]["id"] for g in lista))
    rev("ver uno: 200 con su cuenta", ver_grupo(adm, org, ventas).status_code == 200 and ver_grupo(adm, org, ventas).json()["nombre"] == "Ventas Norte")
    rev("uno que no existe: 404", ver_grupo(adm, org, "grupo-" + "0" * 32).status_code == 404)
    rev("un id que ni tiene la forma de uno: 404", ver_grupo(adm, org, "no-es-un-grupo").status_code == 404)
    rev("el de OTRA organización se contesta igual que uno que no existe: 404", ver_grupo(adm, org, grupos_de(adm_b, otra).json()["grupos"][0]["id"]).status_code == 404)
    rev("pedir los de una organización ajena con tu tenant: se rechaza (403)", grupos_de(adm, otra).status_code in (401, 403, 404))
    rev("sin sesión: 401", c.get("/grupos/", params={"tenant": org}, headers=cab()).status_code == 401)
    rev("sin la llave de servicio: se rechaza", c.get("/grupos/", params={"tenant": org}, headers={"Authorization": f"Bearer {adm}"}).status_code in (401, 403))
    rev("sin decir el tenant: 422", c.get("/grupos/", headers=cab(adm)).status_code == 422)
    rev("todos los roles leen", all(grupos_de(P[rol], org).status_code == 200 for rol in P))

    titulo("3 · El expediente nuevo nace EN un grupo")
    antes = len(lista_exp(adm, org))
    r = expediente(adm, org, "Juan Pérez", None)
    rev("sin grupo: 400, con el motivo", r.status_code == 400 and "grupo" in r.json()["detail"].lower(), r.text[:200])
    r = expediente(adm, org, "Juan Pérez", "grupo-" + "0" * 32)
    rev("con un grupo que no existe: 404", r.status_code == 404 and "grupo" in r.json()["detail"].lower(), r.text[:200])
    r = expediente(adm, org, "Juan Pérez", grupos_de(adm_b, otra).json()["grupos"][0]["id"])
    rev("con un grupo de OTRA organización: 404", r.status_code == 404)
    r = expediente(adm, org, "Juan Pérez", "esto-no-es-un-id")
    rev("con algo que ni parece un id de grupo: 404", r.status_code == 404)
    rev("nada de eso creó un expediente", len(lista_exp(adm, org)) == antes)
    r = expediente(adm, org, "  Juan   Pérez ", ventas)
    rev("con un grupo de la organización: 201", r.status_code == 201, r.text[:250])
    juan = r.json()
    rev("la respuesta trae su grupo", juan["grupo"] == {"id": ventas, "nombre": "Ventas Norte"} and juan["nombre"] == "Juan Pérez", str(juan))
    juan = juan["id"]
    rev("la lista lo enseña con su grupo", [e["grupo"] for e in lista_exp(adm, org) if e["id"] == juan] == [{"id": ventas, "nombre": "Ventas Norte"}])
    rev("el detalle, también", ver_exp(adm, org, juan).json()["grupo"] == {"id": ventas, "nombre": "Ventas Norte"})
    rev("el grupo cuenta su expediente", total(adm, org, ventas) == 1)
    rev("dos expedientes pueden llamarse igual, también en el mismo grupo", expediente(adm, org, "Juan Pérez", ventas).status_code == 201 and total(adm, org, ventas) == 2)

    titulo("4 · Los expedientes de ANTES no tienen grupo y se les asigna después")
    viejo = expediente_de_antes(org, "Expediente de antes")
    rev("sale en la lista con `grupo: null`", [e["grupo"] for e in lista_exp(adm, org) if e["id"] == viejo] == [None])
    rev("y en su detalle", ver_exp(adm, org, viejo).json()["grupo"] is None)
    r = asignar(adm, org, viejo, t1)
    rev("asignarle uno: 200, cambiado, sin grupo anterior", r.status_code == 200 and r.json()["cambiado"] is True and r.json()["grupoAnterior"] is None, r.text[:250])
    rev("el expediente de la respuesta ya trae su grupo", r.json()["expediente"]["grupo"] == {"id": t1, "nombre": "T1 2026"} and r.json()["expediente"]["nombre"] == "Expediente de antes")
    rev("el grupo lo cuenta", total(adm, org, t1) == 1)
    r = asignar(adm, org, viejo, t1)
    rev("asignar el mismo otra vez: cambiado false y no hace nada", r.status_code == 200 and r.json()["cambiado"] is False and r.json()["grupoAnterior"] == {"id": t1, "nombre": "T1 2026"}, r.text[:250])
    r = asignar(adm, org, viejo, ventas)
    rev("pasarlo a OTRO grupo: cambiado, y dice de cuál venía", r.status_code == 200 and r.json()["cambiado"] is True and r.json()["grupoAnterior"] == {"id": t1, "nombre": "T1 2026"} and r.json()["expediente"]["grupo"]["id"] == ventas, r.text[:250])
    rev("cada grupo cuenta los suyos", total(adm, org, t1) == 0 and total(adm, org, ventas) == 3)
    rev("a un grupo que no existe: 404, y no se mueve", asignar(adm, org, viejo, "grupo-" + "0" * 32).status_code == 404 and ver_exp(adm, org, viejo).json()["grupo"]["id"] == ventas)
    rev("a un grupo de otra organización: 404", asignar(adm, org, viejo, grupos_de(adm_b, otra).json()["grupos"][0]["id"]).status_code == 404 and ver_exp(adm, org, viejo).json()["grupo"]["id"] == ventas)
    rev("un expediente que no existe: 404", asignar(adm, org, "expediente-" + "0" * 32, t1).status_code == 404)
    rev("uno de otra organización se contesta igual: 404", asignar(adm_b, otra, juan, grupos_de(adm_b, otra).json()["grupos"][0]["id"]).status_code == 404 and ver_exp(adm, org, juan).json()["grupo"]["id"] == ventas)
    rev("sin grupoId: 422 (no hay forma de dejarlo sin grupo)", c.put(f"/expedientes/{viejo}/grupo", json={"tenant": org}, headers=cab(adm)).status_code == 422)
    rev("con `grupoId` nulo: 422, tampoco", c.put(f"/expedientes/{viejo}/grupo", json={"tenant": org, "grupoId": None}, headers=cab(adm)).status_code == 422)

    titulo("5 · Renombrar")
    r = renombrar(adm, org, ventas, "  Ventas   Sur ")
    rev("renombrar: 200, cambiado, con el nombre limpio y el mismo id", r.status_code == 200 and r.json()["cambiado"] is True and r.json()["grupo"]["id"] == ventas and r.json()["grupo"]["nombre"] == "Ventas Sur", r.text[:250])
    rev("trae la fecha del cambio y conserva su cuenta", r.json()["grupo"]["actualizadoEn"] and r.json()["grupo"]["totalExpedientes"] == 3)
    rev("sus expedientes ven el nombre nuevo sin tocarlos", ver_exp(adm, org, juan).json()["grupo"] == {"id": ventas, "nombre": "Ventas Sur"})
    r = renombrar(adm, org, ventas, "ventas sur")
    rev("cambiar solo las mayúsculas del PROPIO nombre se puede", r.status_code == 200 and r.json()["cambiado"] is True and r.json()["grupo"]["nombre"] == "ventas sur", r.text[:200])
    r = renombrar(adm, org, ventas, " ventas   sur ")
    rev("el mismo nombre, ya limpio: cambiado false, no escribe", r.status_code == 200 and r.json()["cambiado"] is False)
    r = renombrar(adm, org, ventas, "t1 2026")
    rev("chocar con OTRO grupo: 409 `ya_existe`, y no cambia", r.status_code == 409 and r.json()["detail"]["codigo"] == "ya_existe" and ver_grupo(adm, org, ventas).json()["nombre"] == "ventas sur", r.text[:200])
    rev("un nombre malo: 400", renombrar(adm, org, ventas, "  ").status_code == 400 and renombrar(adm, org, ventas, "z" * 201).status_code == 400)
    rev("uno que no existe: 404", renombrar(adm, org, "grupo-" + "0" * 32, "Algo").status_code == 404)
    rev("uno de otra organización: 404", renombrar(adm, org, grupos_de(adm_b, otra).json()["grupos"][0]["id"], "Robado").status_code == 404)
    renombrar(adm, org, ventas, "Ventas Sur")

    titulo("6 · Borrar")
    r = borrar(adm, org, ventas)
    rev("con expedientes: 409 `con_expedientes` y cuántos", r.status_code == 409 and r.json()["detail"]["codigo"] == "con_expedientes" and r.json()["detail"]["total"] == 3, r.text[:250])
    rev("el mensaje habla en singular y plural", "3 expedientes" in r.json()["detail"]["mensaje"])
    rev("no se borró", ver_grupo(adm, org, ventas).status_code == 200)
    vacio = grupo(adm, org, "Sin nadie").json()["id"]
    r = borrar(adm, org, vacio)
    rev("sin expedientes: se borra", r.status_code == 200 and r.json() == {"borrado": True}, r.text[:200])
    rev("y responde 404 como uno que nunca existió", ver_grupo(adm, org, vacio).status_code == 404 and borrar(adm, org, vacio).status_code == 404)
    rev("su nombre queda libre", grupo(adm, org, "Sin nadie").status_code == 201)
    uno = grupo(adm, org, "Con uno").json()["id"]
    solo = expediente(adm, org, "El único", uno).json()["id"]
    r = borrar(adm, org, uno)
    rev("con UN expediente dice «1 expediente»", r.status_code == 409 and "1 expediente:" in r.json()["detail"]["mensaje"], r.text[:200])
    asignar(adm, org, solo, t1)
    rev("pasado ese expediente a otro grupo, se puede borrar", borrar(adm, org, uno).status_code == 200 and total(adm, org, t1) == 1)
    rev("el expediente sigue y conserva su nuevo grupo", ver_exp(adm, org, solo).json()["grupo"]["id"] == t1)
    rev("borrar uno de otra organización: 404 y no se borra", borrar(adm, org, grupos_de(adm_b, otra).json()["grupos"][0]["id"]).status_code == 404 and len(grupos_de(adm_b, otra).json()["grupos"]) == 1)

    titulo("7 · La base cuida lo que el servicio ya comprueba")
    gb = grupos_de(adm_b, otra).json()["grupos"][0]["id"]

    try:
        sql_sa(
            "INSERT INTO nexus.cases (publicId, tenant, name, caseGroupId, createdBy) "
            "SELECT ?, ?, 'Intruso', caseGroupId, 'x' FROM nexus.caseGroups WHERE publicId = ?",
            f"expediente-{uuid.uuid4().hex}", org, gb,
        )
        rev("un expediente de Alfa NO puede apuntar a un grupo de Beta (llave foránea compuesta)", False, "la base lo dejó pasar")
    except Exception as exc:  # noqa: BLE001
        rev("un expediente de Alfa NO puede apuntar a un grupo de Beta (llave foránea compuesta)", "FK_cases_caseGroup" in str(exc), str(exc)[:200])
    try:
        sql_sa("INSERT INTO nexus.caseGroups (publicId, tenant, name, createdBy) VALUES (?, ?, 'VENTAS SUR', 'x')", f"grupo-{uuid.uuid4().hex}", org)
        rev("por debajo tampoco se repite un nombre (la base no distingue mayúsculas)", False, "la base lo dejó pasar")
    except Exception as exc:  # noqa: BLE001
        rev("por debajo tampoco se repite un nombre (la base no distingue mayúsculas)", "UQ_caseGroups_tenant_name" in str(exc), str(exc)[:200])
    try:
        sql_sa("INSERT INTO nexus.caseGroups (publicId, tenant, name, createdBy) VALUES ('grupo-MAYUSCULAS000000000000000000000', ?, 'X', 'x')", org)
        rev("el id público tiene que ser `grupo-` + 32 hexadecimales en minúsculas", False, "la base lo dejó pasar")
    except Exception as exc:  # noqa: BLE001
        rev("el id público tiene que ser `grupo-` + 32 hexadecimales en minúsculas", "CK_caseGroups_publicId" in str(exc), str(exc)[:200])
    try:
        sql_sa("DELETE FROM nexus.caseGroups WHERE publicId = ?", ventas)
        rev("un grupo con expedientes no se borra por debajo (sin ON DELETE)", False, "la base lo dejó pasar")
    except Exception as exc:  # noqa: BLE001
        rev("un grupo con expedientes no se borra por debajo (sin ON DELETE)", "FK_cases_caseGroup" in str(exc), str(exc)[:200])

    titulo("8 · Quién puede qué")
    nuevo_sup = grupo(P["SUPERVISOR"], org, "Del supervisor")
    rev("el SUPERVISOR crea, renombra y borra grupos", nuevo_sup.status_code == 201 and renombrar(P["SUPERVISOR"], org, nuevo_sup.json()["id"], "Del supervisor 2").status_code == 200 and borrar(P["SUPERVISOR"], org, nuevo_sup.json()["id"]).status_code == 200)
    for rol in ("ANALISTA", "OPERADOR", "VIEWER"):
        malas = [grupo(P[rol], org, f"De {rol}").status_code, renombrar(P[rol], org, t1, "Robado").status_code, borrar(P[rol], org, t1).status_code]
        rev(f"el {rol} NO toca grupos (403 en crear, renombrar y borrar)", malas == [403, 403, 403], str(malas))
    rev("y lo que intentaron no cambió nada", ver_grupo(adm, org, t1).json()["nombre"] == "T1 2026" and not [g for g in grupos_de(adm, org).json()["grupos"] if g["nombre"].startswith("De ")])
    for rol in ("ANALISTA", "OPERADOR"):
        e = expediente(P[rol], org, f"De {rol}", t1)
        rev(f"el {rol} SÍ crea expedientes en un grupo y los asigna", e.status_code == 201 and asignar(P[rol], org, e.json()["id"], ventas).status_code == 200, e.text[:200])
    rev("el VIEWER no crea expedientes ni asigna (403)", expediente(P["VIEWER"], org, "No", t1).status_code == 403 and asignar(P["VIEWER"], org, juan, t1).status_code == 403)
    rev("el VIEWER ve el grupo del expediente", ver_exp(P["VIEWER"], org, juan).json()["grupo"]["id"] == ventas)

    titulo("9 · Borrar un grupo mientras se crean expedientes en él: nunca queda uno huérfano")
    from db import tablas
    from servicios import grupos as servicio_grupos

    # (a) Determinista: el grupo se bloquea a propósito, como lo hace un alta a medias. El borrado tiene que ESPERAR, y al soltar tiene que ver el
    # expediente que se coló en medio y negarse (409) en vez de borrar un grupo con un expediente adentro.
    g = grupo(adm, org, "Bloqueado a propósito").json()["id"]
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with tablas.transaccion() as cur:
            rev("el grupo se puede bloquear desde fuera (como un alta a medias)", servicio_grupos.bloqueado(cur, org, g) is not None)
            f = pool.submit(borrar, adm, org, g)
            time.sleep(1.5)
            rev("mientras está bloqueado, el borrado ESPERA (no pasa de largo)", not f.done())
            cur.execute(
                "INSERT INTO nexus.cases (publicId, tenant, name, caseGroupId, createdBy) SELECT ?, ?, 'Se coló en medio', caseGroupId, 'x' FROM nexus.caseGroups WHERE publicId = ?",
                f"expediente-{uuid.uuid4().hex}", org, g,
            )
        r = f.result(timeout=30)
    finally:
        pool.shutdown(wait=True)
    rev("al soltarlo, el borrado ve el expediente que se coló y se niega: 409 con 1", r.status_code == 409 and r.json()["detail"]["total"] == 1, r.text[:200])
    rev("el grupo sigue y cuenta ese expediente", total(adm, org, g) == 1)

    # (b) Al azar: las altas salen primero y el borrado detrás; sea quien sea el que gane, o se borró y NINGUNA alta quedó, o no se borró y todas entraron.
    resultados = {"borrado": 0, "no borrado": 0}
    for ronda in range(8):
        g = grupo(adm, org, f"Carrera {ronda}").json()["id"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            crean = [pool.submit(expediente, adm, org, f"Carrera {ronda}.{i}", g) for i in range(6)]
            borra = pool.submit(borrar, adm, org, g)
            r_crean = [f.result() for f in crean]
            r_borra = borra.result()
        creados = sum(1 for x in r_crean if x.status_code == 201)
        resultados["borrado" if r_borra.status_code == 200 else "no borrado"] += 1
        rev(
            f"ronda {ronda}: o se borró y NINGÚN alta quedó (404), o no se borró (409) y la cuenta es la de los que entraron",
            (r_borra.status_code == 200 and creados == 0 or r_borra.status_code == 409 and creados > 0 and ver_grupo(adm, org, g).json()["totalExpedientes"] == creados)
            and all(x.status_code in (201, 404) for x in r_crean),
            f"borrar={r_borra.status_code} altas={[x.status_code for x in r_crean]}",
        )
        huerfanos = sql_sa("SELECT count(*) FROM nexus.cases c WHERE c.caseGroupId IS NOT NULL AND NOT EXISTS (SELECT 1 FROM nexus.caseGroups g WHERE g.caseGroupId = c.caseGroupId)")[0][0]
        rev(f"ronda {ronda}: ningún expediente apunta a un grupo que ya no existe", huerfanos == 0)
    print(f"  (así cayeron las rondas: {resultados})")

    titulo("10 · Rastro en la auditoría")
    rev("crear un grupo deja su rastro con quién y cuál", any(e.get("grupoId") == ventas and e.get("nombre") == "Ventas Norte" for e in auditoria("CASE_GROUP_CREATED")))
    rev("renombrar deja el nombre antes y después", any(e.get("grupoId") == ventas and e.get("antes") == "Ventas Norte" and e.get("despues") == "Ventas Sur" for e in auditoria("CASE_GROUP_RENAMED")))
    rev("renombrar con el mismo nombre NO repite el rastro", len([e for e in auditoria("CASE_GROUP_RENAMED") if e.get("grupoId") == ventas and e.get("antes") == "ventas sur" and e.get("despues") == "Ventas Sur"]) == 1)
    rev("borrar deja su rastro", any(e.get("grupoId") == vacio for e in auditoria("CASE_GROUP_DELETED")))
    rev("asignar deja el expediente, el grupo y el que tenía (nulo si no tenía)",
        any(e.get("expedienteId") == viejo and e.get("grupoId") == t1 and e.get("grupoAnteriorId") is None for e in auditoria("CASE_GROUP_ASSIGNED"))
        and any(e.get("expedienteId") == viejo and e.get("grupoId") == ventas and e.get("grupoAnteriorId") == t1 for e in auditoria("CASE_GROUP_ASSIGNED")))
    rev("asignar al mismo grupo NO repite el rastro", len([e for e in auditoria("CASE_GROUP_ASSIGNED") if e.get("expedienteId") == viejo and e.get("grupoId") == t1]) == 1)
    rev("crear un expediente deja su grupo en el rastro", any(e.get("expedienteId") == juan and e.get("grupoId") == ventas and e.get("grupoNombre") == "Ventas Norte" for e in auditoria("CASE_CREATED")))
    rev("lo que se rechazó no dejó rastro de alta", not any(e.get("nombre") == "Intruso" for e in auditoria("CASE_CREATED")))
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
