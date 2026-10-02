"""¿Funciona el registro de webhooks de punta a punta?

    venv/bin/python verificar_webhooks.py

OFFLINE y sin tocar nada tuyo, igual que `verificar_llaves_cliente.py`: levanta
la app contra un `ALMACEN_RUTA` temporal, con una llave de servicio y una de
cifrado inventadas.

Lo que importa no es que se registre un webhook: es que el secret salga UNA
vez, que no quede escrito en claro en ningún lado, que el servidor SÍ pueda
recuperarlo para firmar (si no, guardarlo no serviría de nada), que sin llave
de cifrado no se guarde nada, y que cada quien vea y toque solo lo suyo.

Y de la validación de conexión: que el aviso de prueba vaya firmado como pide
Standard Webhooks —verificado con una implementación escrita aparte y con el
vector de prueba publicado—, que solo un 2xx valide, y que la guarda contra
SSRF rechace la red interna de CSI SIN llegar a conectar. El endpoint de cliente
es un servidor HTTP de verdad en 127.0.0.1.

Y de los avisos de eventos: que solo se acepten de entradas reales que pasaron
al pipeline, un resultado final por entrada; que cada entrega llegue firmada y
con el MISMO webhook-id en todos sus reintentos; que el calendario completo se
cumpla (con un reloj de mentira, sin esperar horas); que desactivar o eliminar
cancele lo pendiente; que un reinicio no pierda nada; y las métricas.
"""

import base64
import contextlib
import hashlib
import hmac
import http.server
import ipaddress
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography.fernet import Fernet

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-webhooks-"))
SERVICIO = "llave-de-servicio-solo-local"
CLAVE = Fernet.generate_key().decode()
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["WEBHOOKS_CLAVE_CIFRADO"] = CLAVE
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
from app import app  # noqa: E402
from servicios import almacen, entrega_webhooks, entregas_webhooks, webhooks_cliente  # noqa: E402

(RAIZ / almacen.CENTINELA).touch()
# Validar reintenta con esperas de 1, 2, 4 y 8 s: aquí sin esperar, o cada
# validación fallida tardaría 15 s.
entregas_webhooks.ESPERAS_VALIDACION_S = (0, 0, 0, 0)
REGISTRO = RAIZ / ".registro" / "webhooks.jsonl"
FRONT = Path(r"C:\Moibe\code\nexus_poc_svelte")
URL = "https://api.empresa.com/webhooks/nexusdoc"

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


def alta(c, url=URL, eventos=("documento.completado",), tenant="demo", llave=SERVICIO):
    return c.post(
        "/webhooks/", json={"tenant": tenant, "url": url, "eventos": list(eventos)}, headers={"X-API-Key": llave}
    )


def listado(c, tenant="demo"):
    return c.get("/webhooks/", params={"tenant": tenant}, headers=srv()).json()["webhooks"]


def lineas() -> int:
    return len(REGISTRO.read_text(encoding="utf-8").splitlines()) if REGISTRO.exists() else 0


def main() -> int:
    c = TestClient(app)

    titulo("1 · Registrar: el secret sale UNA vez, cifrado en el registro")
    r = alta(c, eventos=["documento.fallido", "documento.completado"])
    rev("responde 201", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
    cuerpo = r.json()
    secret, webhook = cuerpo["secret"], cuerpo["webhook"]
    rev("el secret es whsec_ + 32 caracteres base64 (24 bytes)",
        re.fullmatch(r"whsec_[A-Za-z0-9+/]{32}", secret) is not None and len(base64.b64decode(secret[6:])) == 24, secret[:12])
    rev("la respuesta pide no guardarse (no-store)", r.headers.get("cache-control") == "no-store")
    rev("el webhook no trae el secret ni su cifrado",
        set(webhook) == {"id", "url", "eventos", "estado", "creadoEn", "validadoEn", "fallidaEn"}, str(set(webhook)))
    rev("nace activo", webhook["estado"] == "activo")
    rev("los eventos salen en el orden del diseño, no en el pedido",
        webhook["eventos"] == ["documento.completado", "documento.fallido"], str(webhook["eventos"]))
    contenido = REGISTRO.read_text(encoding="utf-8")
    rev("en el registro NO está el secret en claro", secret not in contenido)
    rev("ni su parte aleatoria", secret.removeprefix("whsec_") not in contenido)
    cifrado = next(
        (l for l in contenido.splitlines() if webhook["id"] in l and '"registrado"' in l), ""
    )
    token = re.search(r'"secret_cifrado": "([^"]+)"', cifrado)
    rev("está cifrado, y la llave lo abre", bool(token) and Fernet(CLAVE).decrypt(token.group(1).encode()).decode() == secret)
    rev("el servidor lo recupera para firmar", webhooks_cliente.secret_para_firmar(webhook["id"]) == secret)
    otro = alta(c, url="https://api.empresa.com/webhooks/otro").json()
    rev("cada webhook tiene su propio secret", otro["secret"] != secret)

    titulo("2 · Registrar rechaza lo inválido (la regla del front, repetida aquí)")
    casos = [
        ("http:// a un dominio", "http://api.empresa.com/hook", 400),
        ("sin esquema", "api.empresa.com/hook", 400),
        ("ftp://", "ftp://api.empresa.com/hook", 400),
        ("con usuario y contraseña", "https://yo:secreto@api.empresa.com/hook", 400),
        ("con fragmento", "https://api.empresa.com/hook#algo", 400),
        ("más de 2048 caracteres", "https://api.empresa.com/" + "a" * 2048, 400),
        ("vacía", "   ", 400),
    ]
    for nombre, url, esperado in casos:
        r = alta(c, url=url)
        rev(f"{nombre} da {esperado}", r.status_code == esperado, f"{r.status_code} {r.text[:120]}")
    r = alta(c, url="http://localhost:3000/hook")
    rev("http://localhost sí se acepta (para probar en local)", r.status_code == 201, r.text[:120])
    rev("sin eventos da 400", alta(c, url="https://x.empresa.com/a", eventos=[]).status_code == 400)
    r = alta(c, url="https://x.empresa.com/b", eventos=["documento.completado", "documento.archivado"])
    rev("un evento desconocido da 400 y lo nombra", r.status_code == 400 and "documento.archivado" in r.text, r.text[:160])
    rev("un tenant con ../ da 400", alta(c, url="https://x.empresa.com/c", tenant="../otro").status_code == 400)
    rev("sin llave de servicio da 401", alta(c, url="https://x.empresa.com/d", llave="").status_code == 401)
    llave_cliente = c.post(
        "/llaves/", json={"tenant": "demo", "nombre": "Cliente", "descripcion": "Prueba", "dias": 1}, headers=srv()
    ).json()["secret"]
    rev("con una llave de CLIENTE da 401 (un cliente no administra webhooks)",
        alta(c, url="https://x.empresa.com/e", llave=llave_cliente).status_code == 401)

    titulo("3 · Repetidos: la misma dirección escrita de otra forma es la misma")
    r = alta(c, url="HTTPS://API.Empresa.com:443/webhooks/nexusdoc")
    rev("mayúsculas y el puerto de siempre: 409", r.status_code == 409, f"{r.status_code} {r.text[:120]}")
    rev("con el mensaje del front", "Ya tienes un webhook con esa URL" in r.text)
    rev("la ruta SÍ distingue mayúsculas", alta(c, url="https://api.empresa.com/Webhooks/NexusDoc").status_code == 201)
    rev("otro puerto es otra dirección", alta(c, url="https://api.empresa.com:8443/webhooks/nexusdoc").status_code == 201)
    r = alta(c, url="https://API.empresa.com")
    rev("sin ruta se guarda con /", r.status_code == 201 and r.json()["webhook"]["url"] == "https://api.empresa.com/", r.text[:160])
    rev("la misma URL en OTRO cliente sí se puede", alta(c, tenant="acme").status_code == 201)

    titulo("4 · Listar: sin secrets, del más nuevo al más viejo, cada quien lo suyo")
    lista = listado(c)
    rev("trae los del cliente", len(lista) >= 6, str(len(lista)))
    rev("ninguno trae secret ni cifrado", all(set(w) == {"id", "url", "eventos", "estado", "creadoEn", "validadoEn", "fallidaEn"} for w in lista))
    rev("el primero es el más nuevo, el último el más viejo",
        lista[0]["url"] == "https://api.empresa.com/" and lista[-1]["id"] == webhook["id"],
        str([w["url"] for w in lista]))
    rev("no trae los de otro cliente", all(w["url"] != URL or w["id"] == webhook["id"] for w in lista))
    rev("el otro cliente ve solo el suyo", [w["url"] for w in listado(c, "acme")] == [URL])

    titulo("5 · Activar y desactivar")
    ruta = f"/webhooks/{webhook['id']}/estado"
    r = c.post(ruta, json={"tenant": "demo", "estado": "inactivo"}, headers=srv())
    rev("desactivar responde 200 con el webhook", r.status_code == 200 and r.json()["webhook"]["estado"] == "inactivo", r.text[:160])
    rev("el listado lo dice", next(w for w in listado(c) if w["id"] == webhook["id"])["estado"] == "inactivo")
    antes = lineas()
    r = c.post(ruta, json={"tenant": "demo", "estado": "inactivo"}, headers=srv())
    rev("pedir el mismo estado no es error y no escribe", r.status_code == 200 and lineas() == antes)
    c.post(ruta, json={"tenant": "demo", "estado": "activo"}, headers=srv())
    rev("se vuelve a activar", next(w for w in listado(c) if w["id"] == webhook["id"])["estado"] == "activo")
    rev("desde otro cliente da 404", c.post(ruta, json={"tenant": "acme", "estado": "inactivo"}, headers=srv()).status_code == 404)
    rev("uno que no existe da 404",
        c.post("/webhooks/wh_noexiste/estado", json={"tenant": "demo", "estado": "activo"}, headers=srv()).status_code == 404)
    rev("un estado inventado da 422", c.post(ruta, json={"tenant": "demo", "estado": "pausado"}, headers=srv()).status_code == 422)
    rev("un estado ilegible en el registro se lee INACTIVO, nunca activo", _estado_ilegible_es_inactivo(c))

    titulo("6 · Eliminar")
    baja = f"/webhooks/{webhook['id']}/eliminar"
    rev("desde otro cliente da 404 y no lo toca",
        c.post(baja, json={"tenant": "acme"}, headers=srv()).status_code == 404
        and any(w["id"] == webhook["id"] for w in listado(c)))
    r = c.post(baja, json={"tenant": "demo"}, headers=srv())
    rev("eliminar responde 200", r.status_code == 200 and r.json() == {"eliminado": True}, r.text[:120])
    rev("ya no sale en el listado", all(w["id"] != webhook["id"] for w in listado(c)))
    rev("eliminarlo otra vez no es error (idempotente)", c.post(baja, json={"tenant": "demo"}, headers=srv()).status_code == 200)
    rev("su secret ya no se entrega para firmar", webhooks_cliente.secret_para_firmar(webhook["id"]) is None)
    rev("ni se puede activar", c.post(ruta, json={"tenant": "demo", "estado": "activo"}, headers=srv()).status_code == 404)
    r = alta(c)
    rev("la misma URL se puede volver a registrar, con secret NUEVO",
        r.status_code == 201 and r.json()["secret"] != secret, r.text[:120])

    titulo("7 · Sin llave de cifrado no se guarda nada")
    original = config.WEBHOOKS_CLAVE_CIFRADO
    try:
        antes = lineas()
        config.WEBHOOKS_CLAVE_CIFRADO = ""
        r = alta(c, url="https://sin.empresa.com/hook")
        rev("sin llave: 503 que dice qué falta", r.status_code == 503 and "WEBHOOKS_CLAVE_CIFRADO" in r.text, r.text[:200])
        config.WEBHOOKS_CLAVE_CIFRADO = "no-es-una-llave"
        r = alta(c, url="https://sin.empresa.com/hook")
        rev("con una llave inválida: 503", r.status_code == 503, r.text[:160])
        rev("y en los dos casos no se escribió nada", lineas() == antes)
        rev("listar sigue sirviendo sin la llave", c.get("/webhooks/", params={"tenant": "demo"}, headers=srv()).status_code == 200)
        config.WEBHOOKS_CLAVE_CIFRADO = Fernet.generate_key().decode()
        vivo = listado(c)[0]["id"]
        try:
            webhooks_cliente.secret_para_firmar(vivo)
            rev("con OTRA llave el secret no se abre, y lo dice", False, "lo abrió")
        except webhooks_cliente.SinCifrado as exc:
            rev("con OTRA llave el secret no se abre, y lo dice", "WEBHOOKS_CLAVE_CIFRADO" in str(exc), str(exc))
    finally:
        config.WEBHOOKS_CLAVE_CIFRADO = original

    titulo("8 · Por el dominio público no se alcanza")
    publico = {"Host": "nexus-doc-api.buzzword.com.mx"}
    r = c.get("/webhooks/", params={"tenant": "demo"}, headers=srv(publico))
    rev("listar por el nombre público da 404, aun con la llave de servicio", r.status_code == 404, str(r.status_code))
    r = c.post("/webhooks/", json={"tenant": "demo", "url": URL + "2", "eventos": ["documento.completado"]}, headers=srv(publico))
    rev("registrar tampoco", r.status_code == 404, str(r.status_code))

    titulo("9 · Los eventos son los mismos que ofrece el front")
    fuente = (FRONT / "src" / "lib" / "state" / "webhooks.svelte.ts").read_text(encoding="utf-8")
    bloque = fuente[fuente.index("export const EVENTOS_WEBHOOK"):]
    bloque = bloque[: bloque.index("] as const")]
    del_front = tuple(re.findall(r"valor: '([^']+)'", bloque))
    rev("misma lista y mismo orden", del_front == webhooks_cliente.EVENTOS, str(del_front))

    titulo("10 · Validar: un aviso de prueba firmado, y solo vale un 2xx")
    entrega_webhooks.limite_validaciones.reiniciar()
    receptor = Receptor.iniciar()
    url_local = f"http://127.0.0.1:{receptor.puerto}/hooks/nexus"
    config.WEBHOOKS_PERMITIR_LOCAL = False
    nuevo = alta(c, url=url_local).json()
    wid, wsecret = nuevo["webhook"]["id"], nuevo["secret"]
    rev("nace sin validar", nuevo["webhook"]["validadoEn"] is None)
    rev("y así lo lista", next(w for w in listado(c) if w["id"] == wid)["validadoEn"] is None)
    v = validar(c, wid)
    rev("sin WEBHOOKS_PERMITIR_LOCAL, localhost se rechaza como dirección interna",
        v.status_code == 200 and v.json()["validado"] is False and "interna" in v.json()["motivo"], v.text[:200])
    rev("y no se le mandó nada", receptor.recibidos == [])
    config.WEBHOOKS_PERMITIR_LOCAL = True
    entrega_webhooks.limite_validaciones.reiniciar()
    receptor.responder(200)
    v = validar(c, wid)
    cuerpo_v = v.json()
    rev("responde 2xx: queda validado", v.status_code == 200 and cuerpo_v.get("validado") is True, v.text[:200])
    rev("trae validadoEn, el código y los ms",
        bool(cuerpo_v.get("webhook", {}).get("validadoEn")) and cuerpo_v.get("codigo") == 200 and isinstance(cuerpo_v.get("ms"), int))
    rev("el listado lo dice", next(w for w in listado(c) if w["id"] == wid)["validadoEn"] == cuerpo_v["webhook"]["validadoEn"])
    rev("llegó UN aviso", len(receptor.recibidos) == 1, str(len(receptor.recibidos)))
    aviso = receptor.recibidos[-1]
    cab = {k.lower(): val for k, val in aviso["cabeceras"].items()}
    rev("un POST a la ruta registrada", aviso["metodo"] == "POST" and aviso["ruta"] == "/hooks/nexus", f"{aviso['metodo']} {aviso['ruta']}")
    rev("con las tres cabeceras de Standard Webhooks", all(k in cab for k in ("webhook-id", "webhook-timestamp", "webhook-signature")))
    rev("la firma se verifica como lo haría una biblioteca de Standard Webhooks", _firma_valida(wsecret, cab, aviso["cuerpo"]))
    rev("y NO se verifica con otro secret", not _firma_valida("whsec_" + base64.b64encode(b"x" * 24).decode(), cab, aviso["cuerpo"]))
    rev("la marca de tiempo es de ahora", abs(int(cab["webhook-timestamp"]) - time.time()) < 60)
    datos = json.loads(aviso["cuerpo"])
    rev("el cuerpo dice qué es y de qué webhook",
        datos.get("type") == "webhook.validacion" and datos.get("data", {}).get("webhookId") == wid, str(datos)[:160])
    rev("es JSON", cab.get("content-type") == "application/json")
    rev("el secret NO viaja en el aviso", wsecret not in aviso["cuerpo"].decode() and all(wsecret not in str(x) for x in cab.values()))
    rev("la firma coincide con el vector de prueba publicado por Standard Webhooks",
        entrega_webhooks.firmar("whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", "msg_p5jXN8AQM9LWM0D4loKWxJek", "1614265330",
                                b'{"test": 2432232314}') == "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE=")

    titulo("11 · Lo que NO valida")
    for codigo, texto in ((500, "respondió 500"), (404, "respondió 404"), (302, "redirección")):
        entrega_webhooks.limite_validaciones.reiniciar()
        receptor.responder(codigo, cabeceras={"Location": "http://10.0.0.1/"} if codigo == 302 else None)
        i = alta(c, url=f"{url_local}/{codigo}").json()["webhook"]["id"]
        v = validar(c, i).json()
        rev(f"{codigo}: no se valida, y dice por qué", v.get("validado") is False and texto in v.get("motivo", ""), str(v)[:200])
        rev(f"{codigo}: y con qué código respondió", v.get("codigo") == codigo, str(v.get("codigo")))
        rev(f"{codigo}: tras 5 intentos", v.get("intentos") == 5, str(v.get("intentos")))
        rev(f"{codigo}: sigue pendiente", next(w for w in listado(c) if w["id"] == i)["validadoEn"] is None)
    entrega_webhooks.limite_validaciones.reiniciar()
    config.WEBHOOKS_TIMEOUT_S = 1
    receptor.responder(200, retraso=2.5)
    i = alta(c, url=f"{url_local}/lento").json()["webhook"]["id"]
    v = validar(c, i).json()
    rev("si no responde a tiempo: no se valida, y lo dice", v.get("validado") is False and "no respondió" in v.get("motivo", ""), str(v)[:200])
    rev("y sin código: no se llegó a recibir respuesta", "codigo" in v and v["codigo"] is None, str(v))
    config.WEBHOOKS_TIMEOUT_S = 10
    receptor.responder(200)
    rev("validar uno de otro cliente da 404", c.post(f"/webhooks/{wid}/validar", json={"tenant": "acme"}, headers=srv()).status_code == 404)
    borrado = alta(c, url=f"{url_local}/borrado").json()["webhook"]["id"]
    c.post(f"/webhooks/{borrado}/eliminar", json={"tenant": "demo"}, headers=srv())
    rev("validar uno eliminado da 404", validar(c, borrado).status_code == 404)
    entrega_webhooks.limite_validaciones.reiniciar()
    validar(c, wid)
    v = validar(c, wid)
    rev("dos seguidas del mismo: la segunda espera (429)", v.status_code == 429 and "Espera" in v.text, f"{v.status_code} {v.text[:120]}")
    entrega_webhooks.limite_validaciones.reiniciar()
    rev("sin llave de servicio da 401", c.post(f"/webhooks/{wid}/validar", json={"tenant": "demo"}).status_code == 401)

    titulo("12 · La guarda contra la red interna, al ENTREGAR (sin llegar a conectar)")
    config.WEBHOOKS_PERMITIR_LOCAL = False
    internas = {
        "el server de CSI, 172.10.30.15": "https://172.10.30.15/hook",
        "el NAS de CSI, 172.10.30.58": "https://172.10.30.58/hook",
        "una privada, 10.1.2.3": "https://10.1.2.3/hook",
        "la de metadatos de nube, 169.254.169.254": "https://169.254.169.254/latest",
        "CGNAT, 100.64.1.1": "https://100.64.1.1/hook",
        "loopback": "https://127.0.0.1/hook",
        "loopback IPv6": "https://[::1]/hook",
        "una privada escondida en IPv6": "https://[::ffff:10.0.0.1]/hook",
        "0.0.0.0": "https://0.0.0.0/hook",
    }
    for nombre, url in internas.items():
        rev(f"{nombre}: rechazada", _rechazo(url, "interna"))
    with dns_falso({"interno.prueba": ["172.10.30.15"], "mixto.prueba": ["93.184.216.34", "10.0.0.5"],
                    "publico.prueba": ["93.184.216.34"]}):
        rev("un nombre que resuelve a CSI: rechazado", _rechazo("https://interno.prueba/hook", "interna"))
        rev("un nombre con UNA dirección privada entre públicas: rechazado", _rechazo("https://mixto.prueba/hook", "interna"))
        rev("http:// a una dirección pública: rechazado (solo https)", _rechazo("http://publico.prueba/hook", "https"))
    with dns_falso({}):
        rev("un nombre que no resuelve: lo dice", _rechazo("https://noexiste.prueba/hook", "resolver"))
    config.WEBHOOKS_PERMITIR_LOCAL = True
    with dns_falso({"receptor.prueba": ["127.0.0.1"]}):
        receptor.responder(200)
        antes = len(receptor.recibidos)
        entrega_webhooks.entregar(f"http://receptor.prueba:{receptor.puerto}/fijada", wsecret, "webhook.validacion", {})
        ultimo = {k.lower(): val for k, val in receptor.recibidos[-1]["cabeceras"].items()}
        rev("se conecta a la IP revisada, con el nombre original en Host",
            len(receptor.recibidos) == antes + 1 and ultimo.get("host") == f"receptor.prueba:{receptor.puerto}",
            str(ultimo.get("host")))
    config.WEBHOOKS_PERMITIR_LOCAL = False
    receptor.detener()

    titulo("13 · Avisos de eventos: solo de entradas reales que pasaron al pipeline")
    entregas_webhooks._pendientes.clear()
    entregas_webhooks._cargado = True
    entrega_webhooks.limite_validaciones.reiniciar()
    config.WEBHOOKS_PERMITIR_LOCAL = True
    rx = Receptor.iniciar()
    rx.responder(200)
    base_rx = f"http://127.0.0.1:{rx.puerto}"
    # Las secciones anteriores dejaron webhooks validados y suscritos en "demo":
    # recibirían estos avisos también (es lo correcto), así que se parte de cero.
    for w in listado(c):
        c.post(f"/webhooks/{w['id']}/eliminar", json={"tenant": "demo"}, headers=srv())
    entregas_webhooks._pendientes.clear()

    def webhook_validado(ruta, eventos, tenant="demo"):
        r = alta(c, url=f"{base_rx}/{ruta}", eventos=eventos, tenant=tenant).json()
        entrega_webhooks.limite_validaciones.reiniciar()
        v = validar(c, r["webhook"]["id"], tenant=tenant).json()
        assert v.get("validado"), v
        return r["webhook"]["id"], r["secret"]

    wa, secret_a = webhook_validado("a", ["documento.completado", "documento.rechazado"])
    wb, _ = webhook_validado("b", ["documento.fallido"])
    wc = alta(c, url=f"{base_rx}/c-sin-validar", eventos=["documento.completado"]).json()["webhook"]["id"]
    wd, _ = webhook_validado("d-inactivo", ["documento.completado"])
    c.post(f"/webhooks/{wd}/estado", json={"tenant": "demo", "estado": "inactivo"}, headers=srv())
    webhook_validado("e-otro-cliente", ["documento.completado"], tenant="acme")
    rx.recibidos.clear()

    e1 = entrada_al_pipeline(c)
    e_pendiente = subir_entrada(c)
    e_descartada = subir_entrada(c)
    c.post(f"/bandeja/{e_descartada}/retirar", data={"tenant": "demo", "motivo": "descartado"}, headers=srv())

    rev("una entrada que no existe: 404", avisar_evento(c, "documento.completado", "noexiste").status_code == 404)
    rev("una que sigue en la bandeja: 404", avisar_evento(c, "documento.completado", e_pendiente).status_code == 404)
    rev("una descartada: 404", avisar_evento(c, "documento.completado", e_descartada).status_code == 404)
    rev("la de otro cliente: 404", avisar_evento(c, "documento.completado", e1, tenant="acme").status_code == 404)
    rev("un tipo inventado: 400", avisar_evento(c, "documento.archivado", e1).status_code == 400)
    r = avisar_evento(c, "expediente.completado", e1)
    rev("expediente.completado: 400 (el expediente no existe)", r.status_code == 400 and "expediente" in r.text.lower(), r.text[:120])
    rev("rechazado con un motivo fuera de la lista: 400", avisar_evento(c, "documento.rechazado", e1, motivo="porque si").status_code == 400)
    rev("completado CON motivo: 400", avisar_evento(c, "documento.completado", e1, motivo="error_del_servicio").status_code == 400)
    rev("sin llave de servicio: 401",
        c.post("/webhooks/eventos", json={"tenant": "demo", "tipo": "documento.completado", "entradaId": e1}).status_code == 401)
    r = avisar_evento(c, "documento.completado", e1, tipo_documental="INE")
    rev("un aviso válido programa UNA entrega: solo el validado, activo y suscrito",
        r.status_code == 200 and r.json() == {"programadas": 1, "duplicado": False}, r.text[:160])
    r = avisar_evento(c, "documento.completado", e1, tipo_documental="INE")
    rev("repetirlo no programa nada (duplicado)", r.status_code == 200 and r.json() == {"programadas": 0, "duplicado": True}, r.text[:160])
    r = avisar_evento(c, "documento.rechazado", e1, motivo="tipo_no_identificado")
    rev("otro resultado final para la misma entrada: 409", r.status_code == 409, r.text[:160])
    e2 = entrada_al_pipeline(c)
    rev("fallido le llega al suscrito a fallido",
        avisar_evento(c, "documento.fallido", e2, motivo="error_del_servicio").json() == {"programadas": 1, "duplicado": False})
    rev("fallido NO es final: después puede completarse",
        avisar_evento(c, "documento.completado", e2).json() == {"programadas": 1, "duplicado": False})

    titulo("14 · Entregas: firmadas, con reintentos, y que sobreviven a un reinicio")
    entregadas = entregas_webhooks.procesar_vencidas()
    rev("se intentaron las tres programadas", entregadas == 3, str(entregadas))
    rev("y llegaron tres", len(rx.recibidos) == 3, str(len(rx.recibidos)))
    rev("ninguna al webhook sin validar, al inactivo ni al de otro cliente",
        all(x["ruta"] in ("/a", "/b") for x in rx.recibidos), str([x["ruta"] for x in rx.recibidos]))
    al_a = next(x for x in rx.recibidos if x["ruta"] == "/a" and json.loads(x["cuerpo"])["data"]["entradaId"] == e1)
    cab = {k.lower(): v for k, v in al_a["cabeceras"].items()}
    datos = json.loads(al_a["cuerpo"])
    rev("firmada: la verifica una implementación aparte de Standard Webhooks", _firma_valida(secret_a, cab, al_a["cuerpo"]))
    rev("type documento.completado", datos["type"] == "documento.completado")
    rev("data: entradaId, estado, tipoDocumental, sin motivo",
        datos["data"]["entradaId"] == e1 and datos["data"]["estado"] == "completado"
        and datos["data"]["tipoDocumental"] == "INE" and datos["data"]["motivo"] is None, str(datos["data"]))
    rev("data trae cuándo se recibió y cuándo terminó", bool(datos["data"].get("recibidoEn")) and bool(datos["data"].get("terminadoEn")))
    rev("el cuerpo no trae el nombre del archivo ni datos extraídos", "nombreOriginal" not in al_a["cuerpo"].decode())
    rev("no queda nada pendiente", entregas_webhooks.pendientes() == 0)

    reloj = [datetime.now(timezone.utc)]
    entregas_webhooks._ahora = lambda: reloj[0]
    try:
        rx.recibidos.clear()
        rx.responder(500)
        e3 = entrada_al_pipeline(c)
        avisar_evento(c, "documento.completado", e3)
        entregas_webhooks.procesar_vencidas()
        rev("1er intento: 500, queda pendiente", entregas_webhooks.pendientes() == 1 and len(rx.recibidos) == 1)
        rev("al momento no se reintenta", entregas_webhooks.procesar_vencidas() == 0)
        for espera in entregas_webhooks.CALENDARIO_S[1:]:
            reloj[0] += timedelta(seconds=espera)
            entregas_webhooks.procesar_vencidas()
        rev("seis intentos en total, según el calendario", len(rx.recibidos) == 6, str(len(rx.recibidos)))
        ids = {x["cabeceras"].get("webhook-id") for x in rx.recibidos}
        rev("el MISMO webhook-id en todos (para que el cliente no repita)", len(ids) == 1, str(ids))
        rev("el mismo cuerpo en todos", len({x["cuerpo"] for x in rx.recibidos}) == 1)
        rev("agotada: ya no queda pendiente", entregas_webhooks.pendientes() == 0)
        reloj[0] += timedelta(days=1)
        rev("y no se vuelve a intentar", entregas_webhooks.procesar_vencidas() == 0 and len(rx.recibidos) == 6)
        rev("quedó anotada como agotada", '"evento": "agotada"' in (RAIZ / ".registro" / "entregas-webhooks.jsonl").read_text(encoding="utf-8"))

        rx.recibidos.clear()
        rx.responder(500)
        e4 = entrada_al_pipeline(c)
        avisar_evento(c, "documento.completado", e4)
        entregas_webhooks.procesar_vencidas()
        rx.responder(200)
        reloj[0] += timedelta(seconds=5)
        entregas_webhooks.procesar_vencidas()
        rev("falla y al reintentar entra: dos intentos y listo", len(rx.recibidos) == 2 and entregas_webhooks.pendientes() == 0)

        rx.recibidos.clear()
        rx.responder(500)
        e5 = entrada_al_pipeline(c)
        avisar_evento(c, "documento.completado", e5)
        entregas_webhooks.procesar_vencidas()
        c.post(f"/webhooks/{wa}/estado", json={"tenant": "demo", "estado": "inactivo"}, headers=srv())
        rev("desactivar el webhook cancela lo pendiente", entregas_webhooks.pendientes() == 0)
        reloj[0] += timedelta(hours=10)
        entregas_webhooks.procesar_vencidas()
        rev("y ya no se le manda nada", len(rx.recibidos) == 1)
        c.post(f"/webhooks/{wa}/estado", json={"tenant": "demo", "estado": "activo"}, headers=srv())

        rx.recibidos.clear()
        rx.responder(200)
        e6 = entrada_al_pipeline(c)
        avisar_evento(c, "documento.completado", e6)
        # Un reinicio: lo pendiente se va de la memoria y se recupera del registro.
        entregas_webhooks._pendientes.clear()
        entregas_webhooks._cargado = False
        entregas_webhooks.procesar_vencidas()
        rev("tras un reinicio, lo pendiente se recupera del registro y se entrega",
            len(rx.recibidos) == 1 and json.loads(rx.recibidos[0]["cuerpo"])["data"]["entradaId"] == e6)

        rx.recibidos.clear()
        e7 = entrada_al_pipeline(c)
        avisar_evento(c, "documento.completado", e7)
        c.post(f"/webhooks/{wa}/eliminar", json={"tenant": "demo"}, headers=srv())
        rev("eliminar el webhook también cancela lo pendiente", entregas_webhooks.pendientes() == 0)
    finally:
        entregas_webhooks._ahora = lambda: datetime.now(timezone.utc)

    titulo("15 · Métricas de entregas")
    wm, _ = webhook_validado("metricas", ["documento.completado"])
    entregas_webhooks._pendientes.clear()
    rx.recibidos.clear()
    for codigo in (200, 200, 500):
        rx.responder(codigo)
        en = entrada_al_pipeline(c)
        avisar_evento(c, "documento.completado", en)
        entregas_webhooks.procesar_vencidas()
    entregas_webhooks._pendientes.clear()
    ahora = datetime.now(timezone.utc)
    q = {"tenant": "demo", "desde": (ahora - timedelta(hours=1)).isoformat(), "hasta": (ahora + timedelta(hours=1)).isoformat()}
    r = c.get(f"/webhooks/{wm}/metricas", params=q, headers=srv())
    m = r.json()
    rev("responde 200 con actual y anterior", r.status_code == 200 and {"actual", "anterior"} <= set(m), r.text[:200])
    act = m.get("actual", {})
    rev("cuenta los tres intentos", act.get("solicitudes") == 3, str(act))
    rev("uno con error: 33.3 %", act.get("errores") == 1 and act.get("tasaError") == 33.3, str(act))
    rev("trae P50, P90 y P99", all(isinstance(act.get(k), int) for k in ("p50Ms", "p90Ms", "p99Ms")), str(act))
    rev("P50 ≤ P90 ≤ P99", act["p50Ms"] <= act["p90Ms"] <= act["p99Ms"])
    rev("el periodo anterior, vacío", m["anterior"]["solicitudes"] == 0 and m["anterior"]["tasaError"] is None)
    rev("de otro cliente: 404", c.get(f"/webhooks/{wm}/metricas", params={**q, "tenant": "acme"}, headers=srv()).status_code == 404)
    rev("sin zona horaria: 400",
        c.get(f"/webhooks/{wm}/metricas", params={**q, "desde": "2026-10-01T00:00:00"}, headers=srv()).status_code == 400)
    rev("percentil de rango más cercano", entregas_webhooks._percentil([10, 20, 30, 40], 50) == 20
        and entregas_webhooks._percentil([10, 20, 30, 40], 99) == 40 and entregas_webhooks._percentil([], 50) is None)
    config.WEBHOOKS_PERMITIR_LOCAL = False
    rx.detener()

    titulo('16 · Validar con reintentos, "Con fallos" y el historial de intentos')
    config.WEBHOOKS_PERMITIR_LOCAL = True
    rx = Receptor.iniciar()
    base_rx = f"http://127.0.0.1:{rx.puerto}"
    entrega_webhooks.limite_validaciones.reiniciar()
    rx.responder(500)
    nuevo = alta(c, url=f"{base_rx}/siempre-500").json()
    w500 = nuevo["webhook"]["id"]
    rev("nace sin fallas (fallidaEn vacío)", nuevo["webhook"]["fallidaEn"] is None)
    v = validar(c, w500).json()
    rev("500 en todos: no se valida, tras 5 intentos", v.get("validado") is False and v.get("intentos") == 5, str(v)[:200])
    rev("al endpoint le llegaron los 5", len(rx.recibidos) == 5, str(len(rx.recibidos)))
    rev("los 5 con el MISMO webhook-id", len({x["cabeceras"].get("webhook-id") for x in rx.recibidos}) == 1)
    rev('queda "Con fallos": fallidaEn, y sigue sin validar',
        bool(v.get("webhook", {}).get("fallidaEn")) and v["webhook"]["validadoEn"] is None, str(v.get("webhook")))
    en_lista = next(w for w in listado(c) if w["id"] == w500)
    rev("el listado lo dice", bool(en_lista["fallidaEn"]) and en_lista["validadoEn"] is None)
    h = c.get(f"/webhooks/{w500}/intentos", params={"tenant": "demo"}, headers=srv())
    hist = h.json().get("intentos", [])
    rev("el historial trae los 5 intentos", h.status_code == 200 and len(hist) == 5, h.text[:200])
    rev("del más reciente al más viejo (5, 4, 3, 2, 1)", [x["n"] for x in hist] == [5, 4, 3, 2, 1], str([x["n"] for x in hist]))
    rev("cada uno con su hora, su código y su motivo",
        all(x["en"] and x["codigo"] == 500 and x["ok"] is False and "500" in (x["motivo"] or "") for x in hist))
    rev("de tipo webhook.validacion", all(x["tipo"] == "webhook.validacion" for x in hist))
    ahora = datetime.now(timezone.utc)
    q = {"tenant": "demo", "desde": (ahora - timedelta(hours=1)).isoformat(), "hasta": (ahora + timedelta(hours=1)).isoformat()}
    rev("las validaciones NO cuentan en las métricas (son pruebas, no avisos)",
        c.get(f"/webhooks/{w500}/metricas", params=q, headers=srv()).json()["actual"]["solicitudes"] == 0)

    rx.recibidos.clear()
    rx.secuencia([500, 500, 200])
    entrega_webhooks.limite_validaciones.reiniciar()
    v = validar(c, w500).json()
    rev("500, 500 y 200: se valida al tercer intento",
        v.get("validado") is True and v.get("intentos") == 3 and len(rx.recibidos) == 3, str(v)[:200])
    rev("validado gana a la falla anterior", bool(v["webhook"]["validadoEn"]))

    entrega_webhooks.limite_validaciones.reiniciar()
    config.WEBHOOKS_PERMITIR_LOCAL = False
    interna = alta(c, url="https://172.10.30.15/historial").json()["webhook"]["id"]
    v = validar(c, interna).json()
    rev("una dirección interna NO se reintenta: un solo intento",
        v.get("validado") is False and v.get("intentos") == 1 and "interna" in v.get("motivo", ""), str(v)[:200])
    hist = c.get(f"/webhooks/{interna}/intentos", params={"tenant": "demo"}, headers=srv()).json()["intentos"]
    rev("y en el historial, sin código ni tiempo (no llegó a conectar)",
        len(hist) == 1 and hist[0]["codigo"] is None and hist[0]["ms"] is None, str(hist))
    config.WEBHOOKS_PERMITIR_LOCAL = True

    entrega_webhooks.limite_validaciones.reiniciar()
    entregas_webhooks._validando.add(w500)
    r = validar(c, w500)
    rev("si ese webhook ya se está validando: 429", r.status_code == 429 and "ya se está validando" in r.text, r.text[:160])
    entregas_webhooks._validando.discard(w500)
    tomados = [entregas_webhooks._cupo_validacion.acquire(blocking=False) for _ in range(entregas_webhooks.MAX_VALIDACIONES_SIMULTANEAS)]
    entrega_webhooks.limite_validaciones.reiniciar()
    r = validar(c, interna)
    rev("con el cupo de validaciones simultáneas lleno: 429", r.status_code == 429 and "otras validaciones" in r.text, r.text[:160])
    for tomado in tomados:
        if tomado:
            entregas_webhooks._cupo_validacion.release()

    rev("historial de otro cliente: 404",
        c.get(f"/webhooks/{w500}/intentos", params={"tenant": "acme"}, headers=srv()).status_code == 404)
    rev("el historial respeta el límite",
        len(c.get(f"/webhooks/{w500}/intentos", params={"tenant": "demo", "limite": 2}, headers=srv()).json()["intentos"]) == 2)
    hm = c.get(f"/webhooks/{wm}/intentos", params={"tenant": "demo"}, headers=srv()).json()["intentos"]
    de_avisos = [x for x in hm if x["tipo"] == "documento.completado"]
    rev("trae también las entregas de avisos, cada una con su entradaId",
        len(de_avisos) >= 3 and all(x["entradaId"] for x in de_avisos), str(hm[:2])[:200])
    rev("y la validación con la que se dio de alta", any(x["tipo"] == "webhook.validacion" for x in hm))
    config.WEBHOOKS_PERMITIR_LOCAL = False
    rx.detener()

    titulo("17 · Editar: nueva URL y eventos, y vuelve a quedar sin validar")
    entrega_webhooks.limite_validaciones.reiniciar()
    config.WEBHOOKS_PERMITIR_LOCAL = False
    ed = alta(c, url="https://172.10.30.15/para-editar").json()
    eid, esecret = ed["webhook"]["id"], ed["secret"]
    validar(c, eid)
    rev("antes de editar está Con fallos", bool(next(w for w in listado(c) if w["id"] == eid)["fallidaEn"]))
    r = c.post(f"/webhooks/{eid}/editar", json={"tenant": "demo", "url": "HTTPS://Nuevo.Empresa.com/hook",
                                               "eventos": ["documento.fallido"]}, headers=srv())
    w = r.json().get("webhook", {})
    rev("responde 200 con el webhook editado y la URL normalizada",
        r.status_code == 200 and w.get("url") == "https://nuevo.empresa.com/hook" and w.get("eventos") == ["documento.fallido"], r.text[:200])
    rev("vuelve a quedar sin validar y sin fallas", w.get("validadoEn") is None and w.get("fallidaEn") is None)
    en_lista = next(x for x in listado(c) if x["id"] == eid)
    rev("el listado lo dice", en_lista["url"] == "https://nuevo.empresa.com/hook" and en_lista["fallidaEn"] is None)
    rev("conserva su secret", webhooks_cliente.secret_para_firmar(eid) == esecret)
    alta(c, url="https://rival.empresa.com/hook")
    rev("a la URL de otro webhook suyo: 409",
        c.post(f"/webhooks/{eid}/editar", json={"tenant": "demo", "url": "https://rival.empresa.com/hook",
                                                "eventos": ["documento.completado"]}, headers=srv()).status_code == 409)
    rev("a su propia URL (solo cambia eventos): 200",
        c.post(f"/webhooks/{eid}/editar", json={"tenant": "demo", "url": "https://nuevo.empresa.com/hook",
                                                "eventos": ["documento.completado"]}, headers=srv()).status_code == 200)
    rev("sin eventos: 400",
        c.post(f"/webhooks/{eid}/editar", json={"tenant": "demo", "url": "https://x.empresa.com/", "eventos": []}, headers=srv()).status_code == 400)
    rev("http:// a un dominio: 400",
        c.post(f"/webhooks/{eid}/editar", json={"tenant": "demo", "url": "http://x.empresa.com/", "eventos": ["documento.completado"]}, headers=srv()).status_code == 400)
    rev("de otro cliente: 404",
        c.post(f"/webhooks/{eid}/editar", json={"tenant": "acme", "url": "https://y.empresa.com/", "eventos": ["documento.completado"]}, headers=srv()).status_code == 404)

    print("\n" + "=" * 72)
    print(f"{fallos} FALLARON" if fallos else "todas las comprobaciones pasaron")
    return 1 if fallos else 0


PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)


def subir_entrada(c, tenant="demo") -> str:
    """Una entrada nueva en la bandeja, como la subiría el front. Cada una con
    bytes distintos: el almacén deduplica por contenido."""
    contenido = PNG + secrets_token()
    r = c.post("/bandeja/", files={"archivo": ("ine.png", contenido, "image/png")}, data={"tenant": tenant}, headers=srv())
    assert r.status_code == 201, r.text
    return r.json()["id"]


def secrets_token() -> bytes:
    return os.urandom(8)


def entrada_al_pipeline(c, tenant="demo") -> str:
    """Una entrada que ya pasó al pipeline: la que sí puede avisar."""
    i = subir_entrada(c, tenant)
    r = c.post(f"/bandeja/{i}/retirar", data={"tenant": tenant, "motivo": "pipeline"}, headers=srv())
    assert r.status_code == 200, r.text
    return i


def avisar_evento(c, tipo, entrada, motivo=None, tipo_documental=None, tenant="demo"):
    return c.post(
        "/webhooks/eventos",
        json={"tenant": tenant, "tipo": tipo, "entradaId": entrada, "motivo": motivo, "tipoDocumental": tipo_documental},
        headers=srv(),
    )


def validar(c, identificador, tenant="demo"):
    return c.post(f"/webhooks/{identificador}/validar", json={"tenant": tenant}, headers=srv())


class Receptor(http.server.BaseHTTPRequestHandler):
    """Un endpoint de cliente de verdad, en 127.0.0.1: guarda lo que recibe y
    contesta lo que se le pida."""

    recibidos: list = []
    respuesta = {"codigo": 200, "retraso": 0.0, "cabeceras": None}
    pendientes_de_secuencia: list = []

    def do_POST(self):  # noqa: N802 (nombre de http.server)
        largo = int(self.headers.get("Content-Length") or 0)
        Receptor.recibidos.append(
            {"metodo": "POST", "ruta": self.path, "cabeceras": dict(self.headers.items()), "cuerpo": self.rfile.read(largo)}
        )
        r = Receptor.respuesta
        time.sleep(r["retraso"])
        codigo = Receptor.pendientes_de_secuencia.pop(0) if Receptor.pendientes_de_secuencia else r["codigo"]
        self.send_response(codigo)
        for k, val in (r["cabeceras"] or {}).items():
            self.send_header(k, val)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):  # sin ruido en la consola
        pass

    @classmethod
    def iniciar(cls):
        cls.recibidos = []
        servidor = http.server.ThreadingHTTPServer(("127.0.0.1", 0), cls)
        threading.Thread(target=servidor.serve_forever, daemon=True).start()

        class Control:
            puerto = servidor.server_address[1]

            @property
            def recibidos(self):
                return cls.recibidos

            def responder(self, codigo, retraso=0.0, cabeceras=None):
                cls.respuesta = {"codigo": codigo, "retraso": retraso, "cabeceras": cabeceras}
                cls.pendientes_de_secuencia = []

            def secuencia(self, codigos):
                cls.pendientes_de_secuencia = list(codigos)

            def detener(self):
                servidor.shutdown()

        return Control()


@contextlib.contextmanager
def dns_falso(tabla: dict):
    """`getaddrinfo` de mentira para los nombres de la tabla; cualquier otro
    NOMBRE no resuelve. Las IPs literales pasan al de verdad, que no sale a la red."""
    original = socket.getaddrinfo

    def falso(host, puerto, *args, **kwargs):
        if host in tabla:
            return [
                (socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                 (ip, puerto, 0, 0) if ":" in ip else (ip, puerto))
                for ip in tabla[host]
            ]
        try:
            ipaddress.ip_address(host)
        except ValueError:
            raise socket.gaierror(socket.EAI_NONAME, "nombre desconocido") from None
        return original(host, puerto, *args, **kwargs)

    entrega_webhooks.socket.getaddrinfo = falso
    try:
        yield
    finally:
        entrega_webhooks.socket.getaddrinfo = original


def _rechazo(url: str, texto: str) -> bool:
    """¿`entregar` rechaza esta URL con un motivo que dice `texto`, SIN haber
    llegado a abrir una conexión?"""
    original = entrega_webhooks.httpx.Client

    def prohibido(*args, **kwargs):
        raise AssertionError("intentó conectar")

    entrega_webhooks.httpx.Client = prohibido
    try:
        entrega_webhooks.entregar(url, "whsec_" + base64.b64encode(b"k" * 24).decode(), "webhook.validacion", {})
    except entrega_webhooks.Rechazo as exc:
        return texto in str(exc)
    except AssertionError:
        return False
    finally:
        entrega_webhooks.httpx.Client = original
    return False


def _firma_valida(secret: str, cab: dict, cuerpo: bytes) -> bool:
    """Como lo hace una biblioteca de Standard Webhooks, escrita aparte y a
    propósito SIN usar `entrega_webhooks`: si las dos compartieran un error, la
    prueba no lo vería."""
    llave = base64.b64decode(secret.split("_", 1)[1])
    contenido = f"{cab['webhook-id']}.{cab['webhook-timestamp']}.".encode() + cuerpo
    esperada = "v1," + base64.b64encode(hmac.new(llave, contenido, hashlib.sha256).digest()).decode()
    return any(hmac.compare_digest(f, esperada) for f in cab["webhook-signature"].split(" "))


def _estado_ilegible_es_inactivo(c) -> bool:
    nuevo = alta(c, url="https://ilegible.empresa.com/hook").json()["webhook"]["id"]
    with open(REGISTRO, "a", encoding="utf-8") as f:
        f.write('{"evento": "estado", "id": "%s", "estado": "quien-sabe"}\n' % nuevo)
    return next(w for w in listado(c) if w["id"] == nuevo)["estado"] == "inactivo"


if __name__ == "__main__":
    try:
        codigo = main()
    finally:
        shutil.rmtree(RAIZ, ignore_errors=True)
    raise SystemExit(codigo)
