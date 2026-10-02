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
from servicios import almacen, entrega_webhooks, webhooks_cliente  # noqa: E402

(RAIZ / almacen.CENTINELA).touch()
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
        set(webhook) == {"id", "url", "eventos", "estado", "creadoEn", "validadoEn"}, str(set(webhook)))
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
    rev("ninguno trae secret ni cifrado", all(set(w) == {"id", "url", "eventos", "estado", "creadoEn", "validadoEn"} for w in lista))
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
        rev(f"{codigo}: sigue pendiente", next(w for w in listado(c) if w["id"] == i)["validadoEn"] is None)
    entrega_webhooks.limite_validaciones.reiniciar()
    config.WEBHOOKS_TIMEOUT_S = 1
    receptor.responder(200, retraso=2.5)
    i = alta(c, url=f"{url_local}/lento").json()["webhook"]["id"]
    v = validar(c, i).json()
    rev("si no responde a tiempo: no se valida, y lo dice", v.get("validado") is False and "no respondió" in v.get("motivo", ""), str(v)[:200])
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

    print("\n" + "=" * 72)
    print(f"{fallos} FALLARON" if fallos else "todas las comprobaciones pasaron")
    return 1 if fallos else 0


def validar(c, identificador, tenant="demo"):
    return c.post(f"/webhooks/{identificador}/validar", json={"tenant": tenant}, headers=srv())


class Receptor(http.server.BaseHTTPRequestHandler):
    """Un endpoint de cliente de verdad, en 127.0.0.1: guarda lo que recibe y
    contesta lo que se le pida."""

    recibidos: list = []
    respuesta = {"codigo": 200, "retraso": 0.0, "cabeceras": None}

    def do_POST(self):  # noqa: N802 (nombre de http.server)
        largo = int(self.headers.get("Content-Length") or 0)
        Receptor.recibidos.append(
            {"metodo": "POST", "ruta": self.path, "cabeceras": dict(self.headers.items()), "cuerpo": self.rfile.read(largo)}
        )
        r = Receptor.respuesta
        time.sleep(r["retraso"])
        self.send_response(r["codigo"])
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
