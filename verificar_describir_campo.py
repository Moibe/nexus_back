"""¿«Describir un campo a partir de su recuadro» (Gemini) hace lo que debe: lo pide solo quien administra tipos, manda el documento con la zona
marcada, devuelve una descripción que no lleva el valor de ESE documento, no guarda nada y sus fallos no filtran detalles de Google?

    .venv/Scripts/python verificar_describir_campo.py

OFFLINE como los demás `verificar_*`: almacén en una carpeta temporal y Gemini sustituido por un doble. La llamada real se probó aparte, a mano.
"""

import base64
import json
import os
import shutil
import tempfile
from pathlib import Path

import httpx

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-describir-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
import pruebas_sql  # noqa: E402  (desde el 2026-11-11 todo se guarda en SQL Server: esta prueba corre contra uno desechable)

BASE_SQL = pruebas_sql.preparar()

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import auth, gemini, usuarios  # noqa: E402
from servicios import ubicar_campos as ubicar  # noqa: E402

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


def crear_organizacion(bearer_super: str, nombre: str, correo: str, contrasena: str) -> tuple[str, str]:
    r = c.post("/organizaciones/", json={"nombre": nombre, "adminNombre": "Admin", "adminApellidoPaterno": nombre, "adminEmail": correo}, headers=cab(bearer_super))
    assert r.status_code == 201, r.text
    return r.json()["organizacion"]["guid"], con_primer_acceso(correo, r.json()["contrasenaTemporal"], contrasena)


def sumar_persona(bearer_admin: str, correo: str, rol: str, contrasena: str) -> str:
    r = c.post("/usuarios/", json={"nombre": rol.title(), "apellidoPaterno": "Prueba", "email": correo, "rol": rol}, headers=cab(bearer_admin))
    assert r.status_code == 201, r.text
    return con_primer_acceso(correo, r.json()["contrasenaTemporal"], contrasena)


# ── El doble de Gemini ──
llamadas: list[dict] = []
guion: dict = {"respuesta": (200, {}), "falla": None}


async def _llamar_falso(url, headers, cuerpo, timeout):
    llamadas.append({"url": url, "headers": headers, "cuerpo": cuerpo, "timeout": timeout})
    if guion["falla"] is not None:
        raise guion["falla"]
    return guion["respuesta"]


def contestar(datos: dict | str, estado: int = 200) -> None:
    texto = datos if isinstance(datos, str) else json.dumps(datos, ensure_ascii=False)
    guion["falla"] = None
    guion["respuesta"] = (estado, {"candidates": [{"content": {"parts": [{"text": texto}]}, "finishReason": "STOP"}]})


gemini._llamar = _llamar_falso
ubicar._token = lambda: "token-falso"
ubicar.DOCAI_PROJECT_ID = "proyecto-de-prueba-123"
gemini.DOCAI_PROJECT_ID = "proyecto-de-prueba-123"
gemini.GEMINI_MODELO = "modelo-de-prueba"
ubicar.GEMINI_MODELO = "modelo-de-prueba"
gemini.GEMINI_REGION = "global"

PNG = b"\x89PNG\r\n\x1a\n" + b"imagen de un pasaporte"
PDF = b"%PDF-1.4\n%ejemplo\n"
CAMPO = {"nombre": "Mrz", "tipoDato": "texto", "descripcion": "La zona de lectura mecánica"}
CAJA = [880, 40, 960, 960]


def pedir(bearer, tenant, campo=CAMPO, caja=CAJA, pagina=2, archivo=("p.png", PNG, "image/png"), servicio=True, tipo=("Pasaporte", "Pasaporte mexicano"), crudo=None):
    datos = {
        "tenant": tenant,
        "campo": crudo["campo"] if crudo and "campo" in crudo else json.dumps(campo, ensure_ascii=False),
        "caja": crudo["caja"] if crudo and "caja" in crudo else json.dumps(caja),
        "pagina": str(pagina),
    }
    if tipo:
        datos["nombreTipo"], datos["descripcionTipo"] = tipo
    return c.post("/procesadores/describir-campo", data=datos, files={"archivo": archivo}, headers=cab(bearer, servicio))


def sin_filtrar(r) -> bool:
    return "proyecto-de-prueba" not in r.text and "modelo-de-prueba" not in r.text and "detalle de Google" not in r.text


BUENA = {"descripcion": "Las dos líneas de la zona de lectura mecánica, al pie de la página de datos, con letras, números y signos «<».", "valor": "P<MEXRAMIREZ<<LUCIA"}

try:
    titulo("0 · Montaje")
    temporal_super = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal_super))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal_super, "Cobre-Lento-Azul-47!")
    org_a, a_admin = crear_organizacion(bearer_super, "Alfa", "rita@ejemplo.com", "Rumbo-Sereno-Azul-47!")
    org_b, b_admin = crear_organizacion(bearer_super, "Beta", "beto@ejemplo.com", "Rumbo-Sereno-Rojo-48!")
    a_supervisor = sumar_persona(a_admin, "omar@ejemplo.com", "SUPERVISOR", "Cielo-Abierto-Verde-52!")
    a_operador = sumar_persona(a_admin, "olga@ejemplo.com", "OPERADOR", "Cielo-Abierto-Verde-53!")
    almacen_antes = sorted(str(p.relative_to(RAIZ)) for p in RAIZ.rglob("*") if p.is_file() and "auditoria" not in p.name)

    titulo("1 · Quién puede pedirla")
    contestar(BUENA)
    r = pedir(a_admin, org_a)
    rev("el Administrador: 200 con la descripción, el valor leído y el modelo", r.status_code == 200 and r.json() == {**BUENA, "modelo": "modelo-de-prueba"}, r.text[:300])
    r = pedir(a_supervisor, org_a)
    rev("el Supervisor (administra tipos): 200", r.status_code == 200, r.text[:200])
    n = len(llamadas)
    r = pedir(a_operador, org_a)
    rev("el Operador: 403 `sin_permiso` sin llamar a Gemini", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso" and len(llamadas) == n, r.text[:200])
    r = pedir(b_admin, org_a)
    rev("el administrador de OTRA organización: 403 `sin_acceso` sin llamar a Gemini", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso" and len(llamadas) == n, r.text[:200])
    r = pedir(None, org_a)
    rev("sin sesión: no pasa", r.status_code in (401, 403) and len(llamadas) == n, f"{r.status_code}")
    r = pedir(a_admin, org_a, servicio=False)
    rev("sin la llave de servicio: no pasa", r.status_code in (401, 403) and len(llamadas) == n, f"{r.status_code}")

    titulo("2 · Qué se le manda a Gemini")
    contestar(BUENA)
    pedir(a_admin, org_a)
    ll = llamadas[-1]
    partes = ll["cuerpo"]["contents"][0]["parts"]
    rev("la URL del proyecto y el modelo, con la credencial y la cabecera del proyecto",
        ll["url"].endswith("/projects/proyecto-de-prueba-123/locations/global/publishers/google/models/modelo-de-prueba:generateContent")
        and ll["headers"] == {"Authorization": "Bearer token-falso", "x-goog-user-project": "proyecto-de-prueba-123"}, ll["url"])
    rev("el documento va primero, en base64 e intacto; el texto al final", len(partes) == 2 and partes[0]["inlineData"] == {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()} and "text" in partes[1])
    texto = partes[1]["text"]
    rev("el prompt dice el campo, su tipo, la página y la caja marcada", "«Mrz» (tipo de dato: texto)" in texto and "página 2, caja [ymin, xmin, ymax, xmax] = [880, 40, 960, 960]" in texto, texto[:600])
    rev("…la descripción actual y el tipo documental", "Su descripción actual: «La zona de lectura mecánica»" in texto and "del tipo «Pasaporte» (Pasaporte mexicano)" in texto, texto[:400])
    rev("…y pide que sirva para cualquier documento, SIN copiar datos de este", "CUALQUIER documento" in texto and "NO copies el valor" in texto)
    cfg = ll["cuerpo"]["generationConfig"]
    rev("pide JSON con esquema `descripcion` y temperatura 0", cfg["responseMimeType"] == "application/json" and "descripcion" in cfg["responseSchema"]["properties"] and cfg["temperature"] == 0)
    contestar(BUENA)
    pedir(a_admin, org_a, campo={"nombre": "Fecha", "tipoDato": "fecha", "descripcion": ""}, tipo=None, archivo=("a.pdf", PDF, "application/pdf"))
    t = llamadas[-1]["cuerpo"]["contents"][0]["parts"][1]["text"]
    rev("sin tipo documental ni descripción actual, el prompt lo dice sin huecos; un PDF viaja como PDF",
        "Su descripción actual: no tiene." in t and "del tipo" not in t and "(tipo de dato: fecha)" in t
        and llamadas[-1]["cuerpo"]["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "application/pdf", t[:300])
    contestar(BUENA)
    pedir(a_admin, org_a, campo={"nombre": "  Mrz  ", "tipoDato": "inventado", "descripcion": "d" * 900})
    t = llamadas[-1]["cuerpo"]["contents"][0]["parts"][1]["text"]
    rev("se limpian espacios, un tipo desconocido cae a texto y la descripción actual se recorta", "«Mrz» (tipo de dato: texto)" in t and "d" * 200 in t and "d" * 201 not in t)
    contestar(BUENA)
    pedir(a_admin, org_a, caja=[-20, 0, 1400.3, 999.6])
    rev("una caja que se sale se recorta a 0-1000", "= [0, 0, 1000, 1000]" in llamadas[-1]["cuerpo"]["contents"][0]["parts"][1]["text"])

    titulo("3 · Lo que se devuelve")
    contestar({"descripcion": "Número del pasaporte G48213977, arriba a la derecha, junto al rótulo «Pasaporte No.».", "valor": "G48213977"})
    d = pedir(a_admin, org_a).json()
    rev("si el modelo copió el valor de ESTE documento, se quita de la descripción", "G48213977" not in d["descripcion"] and d["descripcion"] == "Número del pasaporte, arriba a la derecha, junto al rótulo «Pasaporte No.».", d["descripcion"])
    contestar({"descripcion": "Folio abc12345 en la esquina superior.", "valor": "ABC12345"})
    d = pedir(a_admin, org_a).json()
    rev("…sin importar mayúsculas", d["descripcion"] == "Folio en la esquina superior.", d["descripcion"])
    contestar({"descripcion": "Sexo del titular, F o M, junto al rótulo Sexo.", "valor": "F"})
    d = pedir(a_admin, org_a).json()
    rev("un valor muy corto (una letra) NO se busca dentro de la frase", d["descripcion"] == "Sexo del titular, F o M, junto al rótulo Sexo.", d["descripcion"])
    contestar({"descripcion": "palabra " * 60, "valor": ""})
    d = pedir(a_admin, org_a).json()
    rev(f"una descripción larga se corta en una palabra, a lo más {ubicar.MAX_DESCRIPCION_PROPUESTA} caracteres", len(d["descripcion"]) <= ubicar.MAX_DESCRIPCION_PROPUESTA and d["descripcion"].endswith("palabra…"), d["descripcion"][-40:])
    contestar({"descripcion": "Sin valor leído."})
    d = pedir(a_admin, org_a).json()
    rev("sin `valor`, vuelve vacío", d["valor"] == "" and d["descripcion"] == "Sin valor leído.", str(d))

    titulo("4 · Lo que se rechaza sin llamar a Gemini")
    n = len(llamadas)
    for desc, kw, esperado in [
        ("un TIFF", {"archivo": ("a.tif", b"II*\x00datos", "image/tiff")}, 400),
        ("un campo que no es JSON", {"crudo": {"campo": "no"}}, 400),
        ("un campo sin nombre", {"campo": {"descripcion": "x"}}, 400),
        ("un nombre en blanco", {"campo": {"nombre": "   "}}, 400),
        ("una caja que no es JSON", {"crudo": {"caja": "no"}}, 400),
        ("una caja de 3 números", {"caja": [1, 2, 3]}, 400),
        ("una caja al revés", {"caja": [500, 500, 400, 600]}, 400),
        ("la página 0", {"pagina": 0}, 400),
        ("un archivo vacío", {"archivo": ("a.png", b"", "image/png")}, 400),
    ]:
        r = pedir(a_admin, org_a, **kw)
        rev(f"{desc}: {esperado}", r.status_code == esperado and len(llamadas) == n, f"{r.status_code} {r.text[:160]}")
    r = pedir(a_admin, org_a, caja=[1, 2, 3])
    rev("…con un mensaje claro", r.json()["detail"] == "El recuadro no es válido.", r.text)
    viejo = gemini.MAX_BYTES_DOCUMENTOS
    gemini.MAX_BYTES_DOCUMENTOS = 10
    r = pedir(a_admin, org_a)
    rev("un documento que pesa más de lo que admite Gemini: 413 sin llamarlo", r.status_code == 413 and len(llamadas) == n, f"{r.status_code} {r.text[:160]}")
    gemini.MAX_BYTES_DOCUMENTOS = viejo

    titulo("5 · Los fallos")
    for estado_g, esperado in [(429, 429), (401, 503), (403, 503), (404, 503), (500, 502)]:
        guion["falla"] = None
        guion["respuesta"] = (estado_g, {"error": {"status": "X", "message": "proyecto-de-prueba-123 detalle de Google"}})
        r = pedir(a_admin, org_a)
        rev(f"Gemini contesta {estado_g}: {esperado} sin filtrar nada", r.status_code == esperado and sin_filtrar(r), f"{r.status_code} {r.text[:200]}")
    for desc, falla in [("tiempo agotado", httpx.ReadTimeout("lento")), ("error de red", httpx.ConnectError("sin red"))]:
        guion["falla"] = falla
        r = pedir(a_admin, org_a)
        rev(f"{desc}: 502", r.status_code == 502 and sin_filtrar(r), f"{r.status_code} {r.text[:160]}")
    for desc, texto in [("texto que no es JSON", "esta es la descripción"), ("JSON sin `descripcion`", '{"valor": "x"}'), ("descripción vacía", '{"descripcion": "   "}'), ("respuesta vacía", "")]:
        contestar(texto)
        r = pedir(a_admin, org_a)
        rev(f"{desc}: 502 con un mensaje que se puede mostrar", r.status_code == 502 and r.json()["detail"] == ubicar.MENSAJE_DESCRIBIR_FALLO and sin_filtrar(r), f"{r.status_code} {r.text[:160]}")
    n = len(llamadas)
    ubicar.DOCAI_PROJECT_ID = ""
    r = pedir(a_admin, org_a)
    rev("sin proyecto en el .env: 503 sin llamar", r.status_code == 503 and len(llamadas) == n and r.json()["detail"] == ubicar.MENSAJE_DESCRIBIR_NO_DISPONIBLE, f"{r.status_code} {r.text[:160]}")
    ubicar.DOCAI_PROJECT_ID = "proyecto-de-prueba-123"
    contestar(BUENA)
    rev("y al volver Google, vuelve a funcionar", pedir(a_admin, org_a).status_code == 200)

    titulo("6 · No guarda nada")
    almacen_despues = sorted(str(p.relative_to(RAIZ)) for p in RAIZ.rglob("*") if p.is_file() and "auditoria" not in p.name)
    rev("el almacén tiene los mismos archivos que antes", almacen_antes == almacen_despues, f"{set(almacen_despues) ^ set(almacen_antes)}")
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
