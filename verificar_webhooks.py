"""¿Funciona el registro de webhooks de punta a punta?

    venv/bin/python verificar_webhooks.py

OFFLINE y sin tocar nada tuyo, igual que `verificar_llaves_cliente.py`: levanta
la app contra un `ALMACEN_RUTA` temporal, con una llave de servicio y una de
cifrado inventadas.

Lo que importa no es que se registre un webhook: es que el secret salga UNA
vez, que no quede escrito en claro en ningún lado, que el servidor SÍ pueda
recuperarlo para firmar (si no, guardarlo no serviría de nada), que sin llave
de cifrado no se guarde nada, y que cada quien vea y toque solo lo suyo.
"""

import os
import re
import shutil
import tempfile
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
from servicios import almacen, webhooks_cliente  # noqa: E402

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
    rev("el secret es whsec_ + 48 hexadecimales", re.fullmatch(r"whsec_[0-9a-f]{48}", secret) is not None, secret[:12])
    rev("la respuesta pide no guardarse (no-store)", r.headers.get("cache-control") == "no-store")
    rev("el webhook no trae el secret ni su cifrado",
        set(webhook) == {"id", "url", "eventos", "estado", "creadoEn"}, str(set(webhook)))
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
    rev("ninguno trae secret ni cifrado", all(set(w) == {"id", "url", "eventos", "estado", "creadoEn"} for w in lista))
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

    print("\n" + "=" * 72)
    print(f"{fallos} FALLARON" if fallos else "todas las comprobaciones pasaron")
    return 1 if fallos else 0


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
