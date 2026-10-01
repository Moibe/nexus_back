"""¿Funcionan las API Keys de cliente de punta a punta?

    venv/bin/python verificar_llaves_cliente.py

OFFLINE y sin tocar nada tuyo, igual que `verificar_bandeja.py`: levanta la app
contra un `ALMACEN_RUTA` temporal, con una llave de servicio inventada.

Lo que importa no es que se emita una llave: es que una llave de cliente abra
SOLO lo que debe, que el tenant salga de la llave y no del formulario, que
revocada o vencida deje de servir en el acto, y que el secret no quede escrito
en ningún lado.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-llaves-"))
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

import seguridad_llaves  # noqa: E402
from app import app  # noqa: E402
from servicios import almacen  # noqa: E402

(RAIZ / almacen.CENTINELA).touch()

PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)
FRONT = Path(r"C:\Moibe\code\nexus_poc_svelte")

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    if ok:
        print(f"  OK    {descripcion}")
    else:
        fallos += 1
        print(f"  FALLA {descripcion}" + (f"  -> {extra}" if extra else ""))


def titulo(texto: str) -> None:
    print("\n" + "=" * 72 + "\n" + texto + "\n" + "=" * 72)


def srv(extra=None):
    return {"X-API-Key": SERVICIO, **(extra or {})}


def emitir(c, tenant="demo", nombre="Integración demo", descripcion="Pruebas", dias=30, llave=SERVICIO):
    return c.post(
        "/llaves/",
        json={"tenant": tenant, "nombre": nombre, "descripcion": descripcion, "dias": dias},
        headers={"X-API-Key": llave},
    )


def subir(c, llave, tenant=None, contenido=PNG, nombre="ine.png"):
    datos = {} if tenant is None else {"tenant": tenant}
    return c.post(
        "/bandeja/", files={"archivo": (nombre, contenido, "image/png")}, data=datos, headers={"X-API-Key": llave}
    )


def pendientes(c, tenant="demo"):
    return c.get("/bandeja/", params={"tenant": tenant}, headers=srv()).json()["entradas"]


def main() -> int:
    c = TestClient(app)

    titulo("1 · Emitir: el secret sale UNA vez y no queda escrito")
    r = emitir(c)
    rev("responde 201", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
    cuerpo = r.json()
    secret, llave = cuerpo["secret"], cuerpo["llave"]
    rev("el secret tiene el formato y el checksum del producto", seguridad_llaves.validar_formato(secret), secret[:16])
    rev("el id de la respuesta es el del secret", seguridad_llaves.id_de(secret) == llave["id"])
    rev("la llave no trae hash ni secret", not ({"secret", "secret_hash", "hash"} & set(llave)), str(set(llave)))
    rev("vence a los 30 días",
        abs((datetime.fromisoformat(llave["expiraEn"]) - datetime.fromisoformat(llave["creadaEn"])) - timedelta(days=30)) < timedelta(seconds=2))
    rev("la respuesta pide no guardarse (no-store)", r.headers.get("cache-control") == "no-store", str(r.headers.get("cache-control")))
    contenido = (RAIZ / ".registro" / "llaves.jsonl").read_text(encoding="utf-8")
    rev("en el registro NO está el secret", secret not in contenido)
    rev("sí está su hash", seguridad_llaves.hash_de(secret) in contenido)

    titulo("2 · Emitir rechaza lo inválido")
    rev("sin nombre da 400", emitir(c, nombre="  ").status_code == 400)
    rev("sin descripción da 400", emitir(c, descripcion="").status_code == 400)
    rev("5 días (no es 1, 7, 30 ni 90) da 400", emitir(c, dias=5).status_code == 400)
    rev("un tenant con ../ da 400", emitir(c, tenant="../otro").status_code == 400)
    rev("sin llave de servicio da 401", emitir(c, llave="").status_code == 401)
    rev("con una llave de CLIENTE da 401 (un cliente no emite llaves)", emitir(c, llave=secret).status_code == 401)

    titulo("3 · Listar: sin hashes, y cada quien lo suyo")
    r_acme = emitir(c, tenant="cli-acme", nombre="Acme")
    secret_acme = r_acme.json()["secret"]
    lista = c.get("/llaves/", params={"tenant": "demo"}, headers=srv()).json()["llaves"]
    rev("demo ve su llave", [x["id"] for x in lista] == [llave["id"]], str([x["id"] for x in lista]))
    rev("y no la de acme", r_acme.json()["llave"]["id"] not in [x["id"] for x in lista])
    rev("el listado no trae hashes", "secret_hash" not in json.dumps(lista) and seguridad_llaves.hash_de(secret) not in json.dumps(lista))
    rev("listar con una llave de cliente da 401", c.get("/llaves/", params={"tenant": "demo"}, headers={"X-API-Key": secret}).status_code == 401)

    titulo("4 · Con la llave de cliente se sube, y el tenant sale de la llave")
    r = subir(c, secret)
    rev("sube sin decir tenant (201)", r.status_code == 201, f"{r.status_code} {r.text[:160]}")
    e = r.json()
    rev("quedó en el tenant de la llave", e.get("tenant") == "demo" and e["rutaRelativa"].startswith("demo/"), e.get("rutaRelativa", ""))
    rev("y anota con qué llave entró", e.get("llaveId") == llave["id"])
    rev("aparece en la bandeja de demo", e["id"] in [x["id"] for x in pendientes(c)])
    rev("mandando su PROPIO tenant también pasa", subir(c, secret, tenant="demo", contenido=PNG + b"2").status_code == 201)

    titulo("5 · Una llave de cliente no puede escribir en otro cliente")
    antes = len(pendientes(c, "cli-acme"))
    r = subir(c, secret, tenant="cli-acme", contenido=PNG + b"ajeno")
    rev("pedir otro tenant da 403", r.status_code == 403, f"{r.status_code} {r.text[:120]}")
    rev("y no se subió nada a acme", len(pendientes(c, "cli-acme")) == antes)
    r = subir(c, secret_acme, contenido=PNG + b"acme")
    rev("la llave de acme sube a acme", r.status_code == 201 and r.json()["tenant"] == "cli-acme")

    titulo("6 · Una llave de cliente abre SOLO subir a la bandeja")
    cliente = {"X-API-Key": secret}
    rev("listar la bandeja: 401", c.get("/bandeja/", params={"tenant": "demo"}, headers=cliente).status_code == 401)
    rev("retirar de la bandeja: 401",
        c.post(f"/bandeja/{e['id']}/retirar", data={"tenant": "demo", "motivo": "descartado"}, headers=cliente).status_code == 401)
    rev("subir a /archivos/: 401",
        c.post("/archivos/", files={"archivo": ("x.png", PNG, "image/png")}, data={"tenant": "demo"}, headers=cliente).status_code == 401)
    rev("revocar llaves: 401", c.post(f"/llaves/{llave['id']}/revocar", json={"tenant": "demo"}, headers=cliente).status_code == 401)
    # Se enumeran desde el OpenAPI y no desde `app.routes`: esta versión de
    # FastAPI guarda los routers incluidos agrupados, y `app.routes` no los
    # aplana — la lista salía vacía y la comprobación pasaba sin probar nada.
    rutas = c.get("/openapi-interno.json").json()["paths"]
    probadas, abiertas = 0, []
    for ruta, metodos in rutas.items():
        if not ruta.startswith(("/ia/", "/procesadores/", "/archivos/", "/llaves/")):
            continue
        for metodo in metodos:
            probadas += 1
            rr = c.request(metodo.upper(), ruta.replace("{", "x").replace("}", ""), headers=cliente)
            if rr.status_code != 401:
                abiertas.append(f"{metodo.upper()} {ruta} -> {rr.status_code}")
    rev(f"ninguna de las {probadas} rutas de /ia, /procesadores, /archivos y /llaves la acepta",
        probadas >= 5 and not abiertas, f"probadas={probadas}; " + "; ".join(abiertas))

    titulo("7 · Llaves falsas o alteradas: 401")
    ident, falsa = seguridad_llaves.generar()
    rev("una llave con formato perfecto pero que nadie emitió", subir(c, falsa).status_code == 401)
    cuerpo_malo = secret[:-6][:-1] + ("a" if secret[-7] != "a" else "b")
    alterada = cuerpo_malo + seguridad_llaves.checksum_de(cuerpo_malo)
    rev("la llave buena con UN carácter cambiado (y checksum recalculado)", subir(c, alterada).status_code == 401)
    rev("basura", subir(c, "nxdoc_live_hola").status_code == 401)
    rev("sin llave", subir(c, "").status_code == 401)

    titulo("8 · Revocada deja de servir en el acto")
    r = c.post(f"/llaves/{llave['id']}/revocar", json={"tenant": "demo"}, headers=srv())
    rev("revocar responde 200", r.status_code == 200, r.text[:120])
    rev("la llave revocada ya no sube (401)", subir(c, secret, contenido=PNG + b"tarde").status_code == 401)
    otra_vez = c.post(f"/llaves/{llave['id']}/revocar", json={"tenant": "demo"}, headers=srv())
    rev("revocar otra vez es idempotente: 200 con la fecha de la primera vez",
        otra_vez.status_code == 200 and otra_vez.json().get("revocadaEn") == r.json().get("revocadaEn"), otra_vez.text[:160])
    id_acme = r_acme.json()["llave"]["id"]
    rev("revocar la de acme como si fuera de demo da 404",
        c.post(f"/llaves/{id_acme}/revocar", json={"tenant": "demo"}, headers=srv()).status_code == 404)
    rev("y la de acme sigue sirviendo", subir(c, secret_acme, contenido=PNG + b"acme2").status_code == 201)
    lista = c.get("/llaves/", params={"tenant": "demo"}, headers=srv()).json()["llaves"]
    rev("el listado la muestra revocada", lista[0]["revocadaEn"] is not None)

    titulo("9 · Vencida deja de servir sola")
    ident, vencida = seguridad_llaves.generar()
    hace_un_rato = datetime.now(timezone.utc) - timedelta(minutes=1)
    with open(RAIZ / ".registro" / "llaves.jsonl", "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "evento": "emitida", "id": ident, "tenant": "demo",
            "secret_hash": seguridad_llaves.hash_de(vencida), "nombre": "vieja", "descripcion": "x",
            "creadaEn": (hace_un_rato - timedelta(days=1)).isoformat(), "expiraEn": hace_un_rato.isoformat(),
        }) + "\n")
    rev("una llave que venció hace un minuto: 401", subir(c, vencida).status_code == 401)

    titulo("10 · La llave de servicio sigue igual")
    rev("sube diciendo el tenant", subir(c, SERVICIO, tenant="demo", contenido=PNG + b"srv").status_code == 201)
    rev("sin tenant da 400", subir(c, SERVICIO, contenido=PNG + b"srv2").status_code == 400)
    rev("lista la bandeja", c.get("/bandeja/", params={"tenant": "demo"}, headers=srv()).status_code == 200)

    titulo("11 · Veinte emisiones al mismo tiempo: veinte ids distintos")
    ids, errores = [], []

    def una(i):
        rr = emitir(c, nombre=f"lote {i}")
        (ids.append(rr.json()["llave"]["id"]) if rr.status_code == 201 else errores.append(rr.status_code))

    hilos = [threading.Thread(target=una, args=(i,)) for i in range(20)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    rev("las 20 respondieron 201", len(ids) == 20, str(errores))
    rev("con 20 ids distintos", len(set(ids)) == 20)

    titulo("12 · Si el registro no está, 503 y no se deja pasar a nadie")
    (RAIZ / almacen.CENTINELA).unlink()
    try:
        rev("una llave de cliente: 503 (no se pudo verificar), no 201", subir(c, secret_acme, contenido=PNG + b"caido").status_code == 503)
        rev("emitir: 503", emitir(c).status_code == 503)
    finally:
        (RAIZ / almacen.CENTINELA).touch()

    titulo("12b · Los registros internos NO se pueden leer por /archivos/")
    for ruta in (".registro/llaves.jsonl", ".registro/bandeja-demo.jsonl", ".nexus-almacen"):
        rr = c.get(f"/archivos/{ruta}", params={"mime": "application/pdf"}, headers=srv())
        rev(f"GET /archivos/{ruta} con la llave de servicio: no lo sirve ({rr.status_code})",
            rr.status_code in (400, 404) and "secret_hash" not in rr.text, rr.text[:120])

    titulo("12c · Un acento cortado a media escritura no tumba el registro")
    registro_llaves = RAIZ / ".registro" / "llaves.jsonl"
    with open(registro_llaves, "ab") as f:
        f.write('{"evento": "emitida", "id": "Zz9z", "nombre": "Juá'.encode("utf-8")[:-1])  # la "á" a la mitad
    rev("la llave de acme sigue sirviendo", subir(c, secret_acme, contenido=PNG + b"utf8").status_code == 201)
    rev("se sigue pudiendo listar", c.get("/llaves/", params={"tenant": "demo"}, headers=srv()).status_code == 200)
    rev("y emitir", emitir(c, nombre="después del corte").status_code == 201)

    titulo("12d · Uso y métricas por llave")
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    from servicios import uso_llaves
    r_m = emitir(c, nombre="con métricas")
    sec_m, id_m = r_m.json()["secret"], r_m.json()["llave"]["id"]
    for i in range(3):
        subir(c, sec_m, contenido=PNG + bytes([100 + i]))
    c.post("/bandeja/", files={"archivo": ("x.zip", b"zz", "application/zip")}, headers={"X-API-Key": sec_m})  # se rechaza: 400
    ahora_utc = _dt.now(_tz.utc)
    p_hoy = {"desde": (ahora_utc - _td(hours=1)).isoformat(), "hasta": (ahora_utc + _td(hours=1)).isoformat()}
    m = c.get(f"/llaves/{id_m}/metricas", params={"tenant": "demo", **p_hoy}, headers=srv())
    rev("métricas responde 200", m.status_code == 200, m.text[:120])
    a = m.json()["actual"]
    rev("cuenta aceptadas Y rechazadas: 4 solicitudes, 3 exitosas, 1 error",
        (a["solicitudes"], a["exitosas"], a["errores"]) == (4, 3, 1), str(a))
    rev("% de éxito 75.0", a["porcentajeExito"] == 75.0)
    rev("latencia P50 es un entero", isinstance(a["latenciaP50Ms"], int))
    rev("el consumo semanal es 4 de 500,000", m.json()["limiteSemanal"]["consumo"] == 4 and m.json()["limiteSemanal"]["limite"] == 500_000)
    rev("trae último uso", m.json()["ultimoUso"] is not None)
    lst = c.get("/llaves/", params={"tenant": "demo"}, headers=srv()).json()["llaves"]
    rev("el listado trae ultimoUso", next(l for l in lst if l["id"] == id_m)["ultimoUso"] is not None)
    rev("métricas de otro tenant: 404", c.get(f"/llaves/{id_m}/metricas", params={"tenant": "cli-acme", **p_hoy}, headers=srv()).status_code == 404)
    rev("con una llave de cliente: 401", c.get(f"/llaves/{id_m}/metricas", params={"tenant": "demo", **p_hoy}, headers={"X-API-Key": sec_m}).status_code == 401)
    rev("periodo al revés: 400", c.get(f"/llaves/{id_m}/metricas", params={"tenant": "demo", "desde": "2026-09-30T00:00:00Z", "hasta": "2026-09-01T00:00:00Z"}, headers=srv()).status_code == 400)
    contenido_uso = (RAIZ / ".registro" / "uso-llaves.jsonl").read_text(encoding="utf-8")
    rev("el registro de uso NO guarda el secret", sec_m not in contenido_uso)
    rev("una llave falsa también se anota (bajo su id), sin abrir nada",
        subir(c, "nxdoc_live_QqQq_sk_" + "b" * 32).status_code == 401 and '"llave": "QqQq"' in (RAIZ / ".registro" / "uso-llaves.jsonl").read_text(encoding="utf-8"))

    titulo("12f · El periodo viaja como instantes: el día es el de quien mira, no el de UTC")
    # Dos eventos de fecha fija, inyectados en el índice (solo en memoria: el
    # servidor de la prueba es este mismo proceso). Uno entra a las 18:30 del
    # 15 de enero en México (UTC-6) y a las 00:30 del 16 en UTC — el caso que
    # fallaba—; el otro a las 23:30 del 14 en México y a las 05:30 del 15 en UTC.
    # 2020 queda lejos de "esta semana": no mueve el tope de 12e.
    tarde_mx = (_dt(2020, 1, 16, 0, 30, tzinfo=_tz.utc), 201, 50, 10)
    noche_mx = (_dt(2020, 1, 15, 5, 30, tzinfo=_tz.utc), 201, 50, 10)
    uso_llaves._eventos_de(id_m)  # calienta el índice si no lo estaba
    uso_llaves._indice.setdefault(id_m, []).extend([tarde_mx, noche_mx])

    def periodo(desde: str, hasta: str):
        return c.get(f"/llaves/{id_m}/metricas", params={"tenant": "demo", "desde": desde, "hasta": hasta}, headers=srv())

    try:
        r = periodo("2020-01-15T00:00:00-06:00", "2020-01-16T00:00:00-06:00")
        rev("el 15 de enero EN MÉXICO incluye lo de las 18:30 (00:30 UTC del 16)", r.status_code == 200 and r.json()["actual"]["solicitudes"] == 1, r.text[:160])
        r = periodo("2020-01-15T00:00:00Z", "2020-01-16T00:00:00Z")
        rev("el 15 de enero EN UTC incluye solo lo de las 05:30 UTC", r.status_code == 200 and r.json()["actual"]["solicitudes"] == 1, r.text[:160])
        r = periodo("2020-01-14T00:00:00-06:00", "2020-01-15T00:00:00-06:00")
        rev("el 14 EN MÉXICO incluye lo de las 23:30 (05:30 UTC del 15)", r.status_code == 200 and r.json()["actual"]["solicitudes"] == 1, r.text[:160])
        r = periodo("2020-01-15T00:00:00-06:00", "2020-01-16T00:00:00-06:00")
        rev("el periodo anterior (el 14 en México) trae la otra solicitud", r.json()["anterior"]["solicitudes"] == 1, r.text[:200])
        r = periodo("2020-01-16T00:00:00+05:30", "2020-01-17T00:00:00+05:30")
        rev("un desfase positivo con minutos (+05:30) también", r.status_code == 200 and r.json()["actual"]["solicitudes"] == 1, r.text[:160])
        r = periodo("2020-01-14T00:00:00-06:00", "2020-01-16T00:00:00-06:00")
        dias = {d["dia"]: d["solicitudes"] for d in r.json()["porDia"]}
        rev("porDia agrupa por el día de quien mira (14 y 15 en México)", dias == {"2020-01-14": 1, "2020-01-15": 1}, str(dias))
        r = periodo("2020-01-14T00:00:00Z", "2020-01-17T00:00:00Z")
        dias = {d["dia"]: d["solicitudes"] for d in r.json()["porDia"]}
        rev("y en UTC, por día UTC (15 y 16)", dias == {"2020-01-15": 1, "2020-01-16": 1}, str(dias))
        r = periodo("2020-01-15T00:00:00-06:00", "2020-01-16T00:00:00-06:00")
        rev("el periodo se devuelve como se pidió", r.json()["desde"].startswith("2020-01-15T00:00:00-06:00"), r.text[:160])

        rev("sin zona horaria: 400 (ambiguo)", periodo("2020-01-15T00:00:00", "2020-01-16T00:00:00").status_code == 400)
        rev("solo fechas, sin hora ni zona: 400", periodo("2020-01-15", "2020-01-16").status_code == 400)
        rev("fin igual al inicio: 400 (periodo vacío)", periodo("2020-01-15T00:00:00Z", "2020-01-15T00:00:00Z").status_code == 400)
        rev("fin antes del inicio: 400", periodo("2020-01-16T00:00:00Z", "2020-01-15T00:00:00Z").status_code == 400)
        rev("basura: 422, no 500", periodo("ayer", "hoy").status_code == 422)
        rr = periodo("0001-01-01T00:00:00Z", "9999-12-31T00:00:00Z")
        rev("un periodo de milenios no revienta (400, no 500)", rr.status_code == 400, f"{rr.status_code} {rr.text[:100]}")
    finally:
        uso_llaves._indice[id_m] = [e for e in uso_llaves._indice.get(id_m, []) if e not in (tarde_mx, noche_mx)]

    titulo("12e · El tope semanal responde 429 y no se sube nada")
    original = uso_llaves.LIMITE_SEMANAL
    uso_llaves.LIMITE_SEMANAL = uso_llaves.consumo_semanal(id_m)  # ya está al tope
    try:
        antes = len(pendientes(c))
        rr = subir(c, sec_m, contenido=PNG + b"tope")
        rev("429 con el mensaje del límite", rr.status_code == 429 and "límite semanal" in rr.text, f"{rr.status_code} {rr.text[:100]}")
        rev("y no entró a la bandeja", len(pendientes(c)) == antes)
        rev("la llave de servicio no tiene tope", subir(c, SERVICIO, tenant="demo", contenido=PNG + b"srv-tope").status_code == 201)
    finally:
        uso_llaves.LIMITE_SEMANAL = original

    titulo("13 · Swagger: la documentación interna tiene todo")
    o = c.get("/openapi-interno.json").json()
    rev("subir a la bandeja lleva candado", o["paths"]["/bandeja/"]["post"].get("security") == [{"APIKeyHeader": []}])
    rev("en el formulario de subir, `tenant` es opcional",
        "tenant" not in (o["components"]["schemas"].get("Body_subir_a_bandeja_bandeja__post", {}).get("required") or []))
    rev("están las rutas de llaves", "/llaves/" in o["paths"] and "/llaves/{identificador}/revocar" in o["paths"])
    rev("/docs-interno se sirve", c.get("/docs-interno").status_code == 200)

    titulo("13b · Swagger: la documentación PÚBLICA trae solo lo del cliente")
    pub = c.get("/openapi.json").json()
    rev("una sola operación: POST /bandeja/", {k: sorted(v) for k, v in pub["paths"].items()} == {"/bandeja/": ["post"]},
        str({k: sorted(v) for k, v in pub["paths"].items()}))
    formulario = pub["components"]["schemas"]["Documento"]
    rev("el formulario ya no pide `tenant`", "tenant" not in formulario["properties"], str(list(formulario["properties"])))
    rev("pide el archivo", "archivo" in (formulario.get("required") or []))
    rev("con candado", pub["paths"]["/bandeja/"]["post"].get("security") == [{"APIKeyHeader": []}])
    texto = json.dumps(pub)
    rev("no menciona la llave de servicio ni rutas internas",
        not any(x in texto for x in ("NEXUS_API_KEY", "/llaves", "/ia/", "/procesadores", "/archivos", "retirar")), "")
    descripcion_publica = pub["info"]["description"]
    rev("el ejemplo de curl usa el dominio público, no un marcador",
        "curl -X POST https://nexus-doc-api.buzzword.com.mx/bandeja/" in descripcion_publica
        and "<servidor>" not in descripcion_publica and "__SERVIDOR__" not in descripcion_publica, descripcion_publica[400:600])
    rev("/docs se sirve", c.get("/docs").status_code == 200)
    rev("/redoc ya no existe", c.get("/redoc").status_code == 404)
    rev("con el formulario PÚBLICO (sin tenant) y una llave de cliente, la subida funciona",
        subir(c, secret_acme, contenido=PNG + b"publico").status_code == 201)

    titulo("13c · Por el DOMINIO PÚBLICO solo sale lo de la documentación pública")
    # Soporte TI reenvió TODO el back por el dominio (se comprobó el 2026-10-01:
    # la documentación interna y /health/db, que da la versión de SQL Server).
    # Esta guarda lo cierra desde aquí: por un nombre público solo pasan tres rutas.
    import config
    from servicios import uso_llaves as _uso
    publico = sorted(config.HOSTS_PUBLICOS)[0]
    rev("el dominio público de fábrica es el de los clientes", publico == "nexus-doc-api.buzzword.com.mx", publico)

    def por(host, metodo, ruta, headers=None, **kw):
        """Una petición como la entrega el proxy: con el nombre público en Host."""
        return c.request(metodo, ruta, headers={"Host": host, **(headers or {})}, **kw)

    rev("lo acordado funciona: GET /docs → 200", por(publico, "GET", "/docs").status_code == 200)
    rev("GET /openapi.json → 200", por(publico, "GET", "/openapi.json").status_code == 200)
    rr = por(publico, "POST", "/bandeja/", headers={"X-API-Key": secret_acme},
             files={"archivo": ("dominio.png", PNG + b"dominio", "image/png")})
    rev("POST /bandeja/ con llave de cliente → 201", rr.status_code == 201, f"{rr.status_code} {rr.text[:100]}")

    cerradas = [
        ("GET", "/docs-interno"), ("GET", "/openapi-interno.json"), ("GET", "/health"), ("GET", "/health/db"),
        ("GET", "/llaves/"), ("POST", "/llaves/"), ("GET", "/bandeja/"), ("POST", "/archivos/"),
        ("GET", "/archivos/demo/aa/bb/x"), ("GET", "/procesadores/clasificador"), ("POST", "/ia/ine"),
        ("POST", "/bandeja/xyz/retirar"), ("POST", "/bandeja"), ("GET", "/redoc"), ("GET", "/"),
    ]
    resultado = [(m, r, por(publico, m, r, headers={"X-API-Key": SERVICIO}).status_code) for m, r in cerradas]
    rev("todo lo demás responde 404, aun con la llave de SERVICIO", all(s == 404 for _, _, s in resultado),
        str([x for x in resultado if x[2] != 404]))
    rev("y el 404 no da pistas", por(publico, "GET", "/docs-interno").json() == {"detail": "Not Found"})

    # Por dentro (IP o localhost, que es como llama el front) nada cambia.
    rev("por la IP interna: /docs-interno → 200", c.get("/docs-interno", headers={"Host": "172.10.30.15:8083"}).status_code == 200)
    rev("por localhost: /health → 200", c.get("/health", headers={"Host": "127.0.0.1:8083"}).status_code == 200)
    rev("y el front puede listar llaves", c.get("/llaves/", params={"tenant": "demo"}, headers=srv({"Host": "172.10.30.15:8083"})).status_code == 200)

    # Un proxy puede no conservar Host y dejar el nombre en X-Forwarded-Host.
    interno = "172.10.30.15:8083"
    variantes = {
        "X-Forwarded-Host con el nombre público": {"Host": interno, "X-Forwarded-Host": publico},
        "X-Forwarded-Host con varios, el público al final": {"Host": interno, "X-Forwarded-Host": f"otro.com, {publico}"},
        "X-Forwarded-Host con varios, el público primero": {"Host": interno, "X-Forwarded-Host": f"{publico}, otro.com"},
        "con puerto en Host": {"Host": f"{publico}:443"},
        "en mayúsculas": {"Host": publico.upper()},
        "Forwarded: host=": {"Host": interno, "Forwarded": f"for=10.0.0.9;host={publico};proto=https"},
    }
    for nombre, cab in variantes.items():
        ok = all(c.get(r, headers=cab).status_code == 404 for r in ("/docs-interno", "/health/db", "/llaves/"))
        rev(f"también se restringe: {nombre}", ok)
    rev("un Host de otro dominio con X-Forwarded-Host ajeno NO se restringe",
        c.get("/docs-interno", headers={"Host": interno, "X-Forwarded-Host": "otro.com"}).status_code == 200)

    id_acme = seguridad_llaves.id_de(secret_acme)
    antes = len(_uso._eventos_de(id_acme))
    por(publico, "GET", "/llaves/", headers={"X-API-Key": secret_acme})
    rev("lo que el dominio rechaza ni se cuenta como uso de la llave", len(_uso._eventos_de(id_acme)) == antes)
    rev("una subida buena por el dominio SÍ cuenta", len(_uso._eventos_de(id_acme)) == antes and
        (por(publico, "POST", "/bandeja/", headers={"X-API-Key": secret_acme}, files={"archivo": ("d2.png", PNG + b"dominio2", "image/png")}).status_code == 201)
        and len(_uso._eventos_de(id_acme)) == antes + 1)

    titulo("14 · El validador del FRONT acepta las llaves que emite el servidor")
    if not (FRONT / "src" / "lib" / "apiKeys" / "formato.ts").exists() or shutil.which("node") is None:
        print("  (se salta: no está el repo del front o no hay node en esta máquina)")
    else:
        llaves = [seguridad_llaves.generar()[1] for _ in range(25)]
        guion = (
            "const m = await import('file:///' + process.argv[1].replace(/\\\\/g, '/'));"
            "const ls = JSON.parse(process.argv[2]);"
            "console.log(JSON.stringify(ls.map((l) => [m.validarFormato(l), m.idDe(l)])));"
        )
        salida = subprocess.run(
            ["node", "--experimental-strip-types", "--no-warnings", "--input-type=module", "-e", guion,
             str(FRONT / "src" / "lib" / "apiKeys" / "formato.ts"), json.dumps(llaves)],
            cwd=FRONT, capture_output=True, text=True, timeout=60,
        )
        if salida.returncode != 0:
            rev("node pudo cargar formato.ts", False, salida.stderr[:300])
        else:
            resultado = json.loads(salida.stdout.strip().splitlines()[-1])
            rev("las 25 pasan validarFormato() del front", all(ok for ok, _ in resultado), str(resultado[:3]))
            rev("y el front les saca el mismo id", [i for _, i in resultado] == [seguridad_llaves.id_de(l) for l in llaves])

    print("\n" + "=" * 72)
    print(f"{fallos} FALLARON" if fallos else "todas las comprobaciones pasaron")
    return 1 if fallos else 0


if __name__ == "__main__":
    try:
        codigo = main()
    finally:
        shutil.rmtree(RAIZ, ignore_errors=True)
    raise SystemExit(codigo)
