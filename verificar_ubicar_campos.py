"""¿«Ubicar campos» (Gemini) hace lo que debe: lo pide solo quien administra tipos, manda el documento y los campos, devuelve solo cajas válidas con
el nombre tal como se pidió, no guarda nada y sus fallos no filtran detalles de Google?

    .venv/Scripts/python verificar_ubicar_campos.py

OFFLINE como los demás `verificar_*`: almacén en una carpeta temporal y Gemini sustituido por un doble. La llamada real se probó aparte, a mano, el
2026-11-11 (pasaporte sintético: 12 de 12 campos bien ubicados).
"""

import base64
import json
import os
import shutil
import tempfile
from pathlib import Path

import httpx

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-ubicar-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

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


def contestar(campos: list[dict] | str, estado: int = 200) -> None:
    texto = campos if isinstance(campos, str) else json.dumps({"campos": campos}, ensure_ascii=False)
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
CAMPOS = [{"nombre": "Número pasaporte", "descripcion": "El número impreso arriba a la derecha"}, {"nombre": "Apellidos"}, {"nombre": "Autoridad", "descripcion": ""}]


def pedir(bearer, tenant, campos=CAMPOS, archivo=("p.png", PNG, "image/png"), servicio=True, crudo: str | None = None):
    datos = {"tenant": tenant, "campos": crudo if crudo is not None else json.dumps(campos, ensure_ascii=False)}
    return c.post("/procesadores/ubicar-campos", data=datos, files={"archivo": archivo}, headers=cab(bearer, servicio))


def sin_filtrar(r) -> bool:
    return "proyecto-de-prueba" not in r.text and "modelo-de-prueba" not in r.text and "detalle de Google" not in r.text


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

    BUENA = [
        {"campo": "numero pasaporte", "encontrado": True, "valor": "G48213977", "pagina": 1, "caja": [165, 734, 191, 849]},
        {"campo": "Apellidos", "encontrado": True, "valor": "RAMÍREZ SOTO", "pagina": 1, "caja": [261, 321, 287, 475]},
        {"campo": "Autoridad", "encontrado": False},
    ]

    titulo("1 · Quién puede pedirla")
    contestar(BUENA)
    r = pedir(a_admin, org_a)
    rev("el Administrador: 200 con las ubicaciones y el modelo", r.status_code == 200 and r.json()["modelo"] == "modelo-de-prueba" and len(r.json()["ubicaciones"]) == 2, r.text[:300])
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
    rev("el prompt lista cada campo con su descripción (si tiene) y pide cajas de 0 a 1000", "- Número pasaporte: El número impreso arriba a la derecha" in texto and "\n- Apellidos\n" in texto and texto.endswith("- Autoridad") and "0 a 1000" in texto, texto[-300:])
    cfg = ll["cuerpo"]["generationConfig"]
    rev("pide JSON con esquema, temperatura 0 y tokens holgados", cfg["responseMimeType"] == "application/json" and "responseSchema" in cfg and cfg["temperature"] == 0 and cfg["maxOutputTokens"] >= 4000, str(cfg)[:200])
    rev("espera al menos un minuto (ubicar tarda más que sugerir)", ll["timeout"] >= 60)
    contestar(BUENA)
    pedir(a_admin, org_a, archivo=("a.pdf", PDF, "application/pdf"))
    rev("un PDF viaja como PDF", llamadas[-1]["cuerpo"]["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "application/pdf")
    contestar(BUENA)
    pedir(a_admin, org_a, campos=[{"nombre": "  Apellidos  "}, {"nombre": "apellidos"}, {"nombre": ""}, {"nombre": "X" * 300, "descripcion": "d" * 900}])
    t = llamadas[-1]["cuerpo"]["contents"][0]["parts"][1]["text"]
    rev("los campos se limpian: espacios, repetidos (sin importar mayúsculas), vacíos y topes de largo", t.count("Apellidos") == 1 and "X" * 80 in t and "X" * 81 not in t and "d" * 200 in t and "d" * 201 not in t, t[-500:])

    titulo("3 · Lo que se devuelve")
    contestar(BUENA)
    u = pedir(a_admin, org_a).json()["ubicaciones"]
    rev("el nombre vuelve TAL COMO se pidió aunque el modelo lo escriba sin acento ni mayúsculas", u[0] == {"campo": "Número pasaporte", "pagina": 1, "caja": [165, 734, 191, 849], "valor": "G48213977"}, str(u))
    rev("lo no encontrado no se devuelve", all(x["campo"] != "Autoridad" for x in u))
    contestar([
        {"campo": "Apellidos", "encontrado": True, "pagina": 2, "caja": [1200.4, -5, 100, 50]},  # ymax < ymin tras recortar
        {"campo": "Número pasaporte", "encontrado": True, "caja": [10, 20, 30]},  # 3 números
        {"campo": "Autoridad", "encontrado": True, "caja": [10, 20, 30, 40], "pagina": 0, "valor": "V" * 500},
        {"campo": "Inventado", "encontrado": True, "caja": [1, 1, 2, 2]},
        {"campo": "Autoridad", "encontrado": True, "caja": [500, 500, 600, 600]},
    ])
    u = pedir(a_admin, org_a).json()["ubicaciones"]
    rev("se descartan cajas imposibles, cajas incompletas, campos que no se pidieron y repetidos", [x["campo"] for x in u] == ["Autoridad"] and u[0]["caja"] == [10, 20, 30, 40], str(u))
    rev("una página inválida se toma como la 1 y el valor se recorta", u[0]["pagina"] == 1 and len(u[0]["valor"]) == ubicar.MAX_VALOR)
    contestar([{"campo": "Apellidos", "encontrado": True, "pagina": 3, "caja": [-20, 0, 1400, 999.6]}])
    u = pedir(a_admin, org_a).json()["ubicaciones"]
    rev("una caja que se sale se recorta a 0-1000 y la página se respeta", u == [{"campo": "Apellidos", "pagina": 3, "caja": [0, 0, 1000, 1000], "valor": ""}], str(u))
    contestar([])
    r = pedir(a_admin, org_a)
    rev("si no encontró ninguno: 200 con la lista vacía", r.status_code == 200 and r.json()["ubicaciones"] == [], r.text[:200])

    titulo("4 · Lo que se rechaza sin llamar a Gemini")
    n = len(llamadas)
    for desc, kw, esperado in [
        ("un TIFF", {"archivo": ("a.tif", b"II*\x00datos", "image/tiff")}, 400),
        ("campos que no son JSON", {"crudo": "esto no"}, 400),
        ("campos que no son lista", {"crudo": '{"nombre":"x"}'}, 400),
        ("un campo sin nombre", {"campos": [{"descripcion": "x"}]}, 400),
        ("ningún campo con nombre", {"campos": [{"nombre": "  "}]}, 400),
        ("más de 60 campos", {"campos": [{"nombre": f"C{i}"} for i in range(61)]}, 400),
        ("un archivo vacío", {"archivo": ("a.png", b"", "image/png")}, 400),
    ]:
        r = pedir(a_admin, org_a, **kw)
        rev(f"{desc}: {esperado}", r.status_code == esperado and len(llamadas) == n, f"{r.status_code} {r.text[:160]}")
    r = pedir(a_admin, org_a, crudo="no")
    rev("…con un mensaje claro", r.json()["detail"] == "Los campos no son un JSON válido.", r.text)
    r = pedir(a_admin, org_a, archivo=("a.tif", b"II*\x00datos", "image/tiff"))
    rev("…el TIFF dice qué formatos sí", "PDF, JPG o PNG" in r.text, r.text)
    viejo = gemini.MAX_BYTES_DOCUMENTOS
    gemini.MAX_BYTES_DOCUMENTOS = 10
    r = pedir(a_admin, org_a)
    rev("un documento que pesa más de lo que admite Gemini: 413 sin llamarlo", r.status_code == 413 and len(llamadas) == n, f"{r.status_code} {r.text[:160]}")
    gemini.MAX_BYTES_DOCUMENTOS = viejo

    titulo("5 · Los fallos")
    for estado_g, esperado in [(429, 429), (401, 503), (403, 503), (404, 503), (500, 502), (503, 502)]:
        guion["falla"] = None
        guion["respuesta"] = (estado_g, {"error": {"status": "X", "message": "proyecto-de-prueba-123 detalle de Google"}})
        r = pedir(a_admin, org_a)
        rev(f"Gemini contesta {estado_g}: {esperado} sin filtrar nada", r.status_code == esperado and sin_filtrar(r), f"{r.status_code} {r.text[:200]}")
    for desc, falla in [("tiempo agotado", httpx.ReadTimeout("lento")), ("error de red", httpx.ConnectError("sin red"))]:
        guion["falla"] = falla
        r = pedir(a_admin, org_a)
        rev(f"{desc}: 502", r.status_code == 502 and sin_filtrar(r), f"{r.status_code} {r.text[:160]}")
    for desc, texto in [("texto que no es JSON", "aquí tienes las cajas"), ("JSON sin `campos`", '{"otra": 1}'), ("respuesta vacía", "")]:
        contestar(texto)
        r = pedir(a_admin, org_a)
        rev(f"{desc}: 502", r.status_code == 502 and sin_filtrar(r), f"{r.status_code} {r.text[:160]}")
    guion["falla"] = None
    guion["respuesta"] = (200, {"promptFeedback": {"blockReason": "SAFETY"}})
    r = pedir(a_admin, org_a)
    rev("bloqueada por Google: 502", r.status_code == 502, f"{r.status_code}")

    def sin_credencial():
        raise RuntimeError("PERMISO-SECRETO")

    n = len(llamadas)
    ubicar._token = sin_credencial
    r = pedir(a_admin, org_a)
    rev("sin credencial: 503 sin llamar y sin filtrar el motivo", r.status_code == 503 and len(llamadas) == n and "PERMISO" not in r.text, f"{r.status_code} {r.text[:160]}")
    ubicar._token = lambda: "token-falso"
    ubicar.DOCAI_PROJECT_ID = ""
    r = pedir(a_admin, org_a)
    rev("sin proyecto en el .env: 503 sin llamar", r.status_code == 503 and len(llamadas) == n, f"{r.status_code}")
    ubicar.DOCAI_PROJECT_ID = "proyecto-de-prueba-123"
    contestar(BUENA)
    rev("y al volver Google, vuelve a funcionar", pedir(a_admin, org_a).status_code == 200)

    titulo("6 · No guarda nada")
    almacen_despues = sorted(str(p.relative_to(RAIZ)) for p in RAIZ.rglob("*") if p.is_file() and "auditoria" not in p.name)
    rev("el almacén tiene los mismos archivos que antes de ubicar", almacen_antes == almacen_despues, f"{set(almacen_despues) ^ set(almacen_antes)}")
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
