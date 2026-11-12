"""«Parecido a» del motor de reglas: `POST /procesos/parecido` y el `minimo` del nodo (2026-11-10).

    .venv/Scripts/python verificar_parecido.py

OFFLINE: Gemini se sustituye por un doble (`servicios.gemini._llamar`), así que no sale nada a Google ni cuesta. Levanta la app contra un
`ALMACEN_RUTA` temporal, como los demás `verificar_*`. Lo que prueba:

1. Quién puede: los roles que organizan (Administrador, Supervisor, Analista, Operador); Compliance, Auditor y Viewer reciben 403; otra organización, 403.
2. La respuesta: un `{porcentaje, razon}` por par y en orden; el porcentaje se recorta a 0-100 y se redondea; un par por llamada a Gemini.
3. La caché: el mismo par (en cualquier orden y con espacios de más) no vuelve a preguntarle a Gemini; dos veces el mismo par en una petición, una pregunta.
8. La forma de escribir no cuenta (2026-11-10, «KEYSI» ~ «KEISY» no pasaba y «Keysi» ~ «KEISY» sí): Gemini recibe los textos en mayúsculas y sin
   acentos, las variantes comparten la caché, lo que solo se escribe distinto es 100 sin preguntar, y si una llamada falla lo que sí salió se guarda.
4. Lo que se rechaza: sin pares, más de 20, un texto vacío o de más de 500 letras.
5. Los fallos de Gemini con su mensaje para la persona: 429 → 429, 403/404 → 503, 500 → 502, respuesta que no se entiende → 502; sin proyecto → 503.
6. El tope por persona (pares por minuto) → 429.
7. El dibujo: un «Parecido a» guarda su `minimo` (90 si no trae), lo rechaza fuera de 1-100 o si no es entero; los otros operadores no lo llevan.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-parecido-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
import pruebas_sql  # noqa: E402  (desde el 2026-11-11 todo se guarda en SQL Server: esta prueba corre contra uno desechable)

BASE_SQL = pruebas_sql.preparar()

from cryptography.fernet import Fernet  # noqa: E402

os.environ["WEBHOOKS_CLAVE_CIFRADO"] = Fernet.generate_key().decode()

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import auth, gemini, parecido, procesos, usuarios  # noqa: E402

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

# ── El doble de Gemini ───────────────────────────────────────────────────────
LLAMADAS: list[dict] = []
RESPUESTA = {"estado": 200, "cuerpo": None, "texto": None}


async def falso_llamar(url, headers, cuerpo, timeout):
    LLAMADAS.append(cuerpo)
    if RESPUESTA["estado"] != 200 or "FALLA-AQUI" in cuerpo["contents"][0]["parts"][0]["text"]:
        return (RESPUESTA["estado"] if RESPUESTA["estado"] != 200 else 500), {"error": {"status": "X", "message": "fallo de prueba"}}
    if RESPUESTA["texto"] is not None:
        texto = RESPUESTA["texto"]
    else:
        # Contesta según los pares que trae el prompt: 100 si los textos son iguales sin mayúsculas, 30 si no; el par «PORCENTAJE-RARO» devuelve 140.5.
        prompt = cuerpo["contents"][0]["parts"][0]["text"]
        renglones = [r for r in prompt.split("Pares:\n", 1)[1].splitlines() if r.strip()]
        salida = []
        for r in renglones:
            i, resto = r.split(". A: ", 1)
            a, b = resto.split(" | B: ")
            a, b = json.loads(a), json.loads(b)
            p = 140.5 if "PORCENTAJE-RARO" in a else -3 if "NEGATIVO" in a else 100 if a.casefold() == b.casefold() else 30
            salida.append({"indice": int(i), "porcentaje": p, "razon": f"razón de {a}"})
        texto = json.dumps(salida, ensure_ascii=False)
    return 200, {"candidates": [{"content": {"parts": [{"text": texto}]}, "finishReason": "STOP"}]}


gemini._llamar = falso_llamar
parecido._token = lambda: "token-de-prueba"
parecido.DOCAI_PROJECT_ID = "proyecto-de-prueba"


def entrar(correo: str, contrasena: str) -> dict:
    r = c.post("/auth/login", json={"email": correo, "password": contrasena, "ip": "127.0.0.1", "userAgent": "verificar"}, headers=cab())
    assert r.status_code == 200, r.text
    return r.json()


def con_primer_acceso(correo: str, temporal: str, definitiva: str) -> str:
    s = entrar(correo, temporal)
    r = c.post("/auth/contrasena", json={"nueva": definitiva, "confirmacion": definitiva}, headers=cab(s["accessToken"]))
    assert r.status_code == 200, r.text
    return entrar(correo, definitiva)["accessToken"]


def medir(bearer: str, tenant: str, pares: list) -> object:
    return c.post("/procesos/parecido", json={"tenant": tenant, "pares": pares}, headers=cab(bearer))


try:
    titulo("0 · Montaje: una organización con una persona de cada rol y otra organización ajena")
    temporal_super = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal_super))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal_super, "Cobre-Lento-Azul-47!")

    def crear_org(nombre: str, correo: str) -> dict:
        r = c.post("/organizaciones/", json={"nombre": nombre, "adminNombre": "Rita", "adminApellidos": "Zeta", "adminEmail": correo}, headers=cab(bearer_super))
        assert r.status_code == 201, r.text
        cuerpo = r.json()
        return {"guid": cuerpo["organizacion"]["guid"], "ADMIN": con_primer_acceso(correo, cuerpo["contrasenaTemporal"], "Rumbo-Sereno-Azul-47!")}

    O = crear_org("Seguros Alfa Omega", "rita.alfa@ejemplo.com")
    AJENA = crear_org("Textiles Beta Sigma", "tomas.beta@ejemplo.com")
    for rol in ("SUPERVISOR", "ANALISTA", "OPERADOR", "COMPLIANCE_OFFICER", "AUDITOR", "VIEWER"):
        correo = f"{rol.lower()}@ejemplo.com"
        r = c.post("/usuarios/", json={"nombre": rol.title(), "email": correo, "rol": rol}, headers=cab(O["ADMIN"]))
        assert r.status_code == 201, r.text
        O[rol] = con_primer_acceso(correo, r.json()["contrasenaTemporal"], "Rumbo-Sereno-Azul-47!")
    rev("una persona activa por cada rol", all(O.get(r) for r in ("ADMIN", "SUPERVISOR", "ANALISTA", "OPERADOR", "COMPLIANCE_OFFICER", "AUDITOR", "VIEWER")))

    titulo("1 · Quién puede pedirlo")
    for i, rol in enumerate(("ADMIN", "SUPERVISOR", "ANALISTA", "OPERADOR")):
        r = medir(O[rol], O["guid"], [{"a": f"Texto {rol}", "b": f"texto {rol}"}])
        rev(f"{rol}: 200", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    for rol in ("COMPLIANCE_OFFICER", "AUDITOR", "VIEWER"):
        antes = len(LLAMADAS)
        r = medir(O[rol], O["guid"], [{"a": "x", "b": "y"}])
        rev(f"{rol}: 403 sin_permiso y no se le pregunta a Gemini", r.status_code == 403 and len(LLAMADAS) == antes, f"{r.status_code} {r.text[:120]}")
    r = medir(AJENA["ADMIN"], O["guid"], [{"a": "x", "b": "y"}])
    rev("el Administrador de OTRA organización: 403 sin_acceso", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso", r.text[:120])
    r = c.post("/procesos/parecido", json={"tenant": O["guid"], "pares": [{"a": "x", "b": "y"}]}, headers=cab())
    rev("sin sesión: 401", r.status_code == 401, str(r.status_code))

    titulo("2 · La respuesta, en orden y recortada a 0-100")
    LLAMADAS.clear()
    r = medir(O["ADMIN"], O["guid"], [{"a": "ACME", "b": "acme"}, {"a": "ACME", "b": "Otra cosa"}, {"a": "PORCENTAJE-RARO", "b": "z"}, {"a": "NEGATIVO", "b": "z"}])
    res = r.json().get("resultados", [])
    rev("200 con un resultado por par", r.status_code == 200 and len(res) == 4, r.text[:200])
    rev("en el mismo orden (100 y luego 30)", [x["porcentaje"] for x in res[:2]] == [100, 30], str(res))
    rev("un porcentaje de 140.5 se recorta a 100 y uno negativo a 0", res[2]["porcentaje"] == 100 and res[3]["porcentaje"] == 0, str(res[2:]))
    rev("cada uno trae su razón", all(x["razon"] for x in res), str(res))
    pares_por_llamada = [x["contents"][0]["parts"][0]["text"].split("Pares:\n", 1)[1].count(". A: ") for x in LLAMADAS]
    rev("una llamada a Gemini POR PAR (tres), y «ACME» ~ «acme» ni pregunta: es el mismo texto", pares_por_llamada == [1, 1, 1], str(pares_por_llamada))
    cfg = LLAMADAS[0]["generationConfig"]
    rev("con temperatura 0 y respuesta JSON", cfg.get("temperature") == 0 and cfg.get("responseMimeType") == "application/json", str(cfg))
    rev("y dice qué modelo contestó", bool(r.json().get("modelo")))

    titulo("3 · La caché")
    LLAMADAS.clear()
    r = medir(O["ADMIN"], O["guid"], [{"a": "acme", "b": "ACME"}, {"a": "  ACME ", "b": "Otra   cosa"}])
    rev("el mismo par al revés y con espacios de más: sale de la caché, sin llamar a Gemini", r.status_code == 200 and len(LLAMADAS) == 0 and [x["porcentaje"] for x in r.json()["resultados"]] == [100, 30], f"{len(LLAMADAS)} {r.text[:150]}")
    r = medir(O["ADMIN"], O["guid"], [{"a": "Nuevo uno", "b": "Otro dos"}, {"a": "otro DOS", "b": "nuevo uno"}])
    pregunta = LLAMADAS[0]["contents"][0]["parts"][0]["text"] if LLAMADAS else ""
    rev("dos veces el mismo par en una petición: se pregunta una sola vez", len(LLAMADAS) == 1 and pregunta.count("NUEVO UNO") == 1 and pregunta.count("OTRO DOS") == 1 and len(r.json()["resultados"]) == 2, pregunta[-200:])

    titulo("4 · Lo que se rechaza")
    rev("sin pares: 422", medir(O["ADMIN"], O["guid"], []).status_code == 422)
    rev("más de 20 pares: 422", medir(O["ADMIN"], O["guid"], [{"a": f"a{i}", "b": "b"} for i in range(21)]).status_code == 422)
    rev("un texto vacío: 422", medir(O["ADMIN"], O["guid"], [{"a": "", "b": "b"}]).status_code == 422)
    r = medir(O["ADMIN"], O["guid"], [{"a": "   ", "b": "b"}])
    rev("un texto de puros espacios: 400", r.status_code == 400, f"{r.status_code} {r.text[:120]}")
    rev("un texto de más de 500 letras: 422", medir(O["ADMIN"], O["guid"], [{"a": "x" * 501, "b": "b"}]).status_code == 422)

    titulo("5 · Los fallos de Gemini, con su mensaje")
    for estado_g, esperado in ((429, 429), (403, 503), (404, 503), (500, 502)):
        RESPUESTA["estado"] = estado_g
        r = medir(O["SUPERVISOR"], O["guid"], [{"a": f"fallo {estado_g}", "b": "zz"}])
        rev(f"Gemini {estado_g} → {esperado} con un mensaje para la persona", r.status_code == esperado and isinstance(r.json().get("detail"), str) and "Gemini" not in r.json()["detail"], f"{r.status_code} {r.text[:150]}")
    RESPUESTA["estado"] = 200
    RESPUESTA["texto"] = "esto no es JSON"
    r = medir(O["SUPERVISOR"], O["guid"], [{"a": "no se entiende", "b": "zz"}])
    rev("una respuesta que no se entiende → 502", r.status_code == 502, f"{r.status_code} {r.text[:120]}")
    RESPUESTA["texto"] = json.dumps([{"indice": 5, "porcentaje": 50, "razon": "x"}])
    r = medir(O["SUPERVISOR"], O["guid"], [{"a": "falta su indice", "b": "zz"}])
    rev("una respuesta a la que le falta un par → 502 (no se inventa)", r.status_code == 502, f"{r.status_code} {r.text[:120]}")
    RESPUESTA["texto"] = None
    r = medir(O["SUPERVISOR"], O["guid"], [{"a": "falta su indice", "b": "zz"}])
    rev("lo que falló NO quedó en la caché: al volver a pedirlo se pregunta y sale bien", r.status_code == 200, f"{r.status_code} {r.text[:120]}")
    parecido.DOCAI_PROJECT_ID = ""
    r = medir(O["SUPERVISOR"], O["guid"], [{"a": "sin proyecto", "b": "zz"}])
    rev("sin proyecto de Google en el servidor → 503", r.status_code == 503, f"{r.status_code} {r.text[:120]}")
    parecido.DOCAI_PROJECT_ID = "proyecto-de-prueba"

    titulo("6 · El tope por persona")
    tope = parecido.MAX_PARES_POR_MINUTO
    parecido._PEDIDOS.clear()
    parecido.MAX_PARES_POR_MINUTO = 5
    ok = [medir(O["OPERADOR"], O["guid"], [{"a": f"tope {i}", "b": "zz"}]).status_code for i in range(5)]
    r = medir(O["OPERADOR"], O["guid"], [{"a": "tope 6", "b": "zz"}])
    rev("5 pares por minuto pasan y el sexto da 429", ok == [200] * 5 and r.status_code == 429, f"{ok} {r.status_code}")
    rev("y el tope es por persona: otra persona sí puede", medir(O["ANALISTA"], O["guid"], [{"a": "tope otra", "b": "zz"}]).status_code == 200)
    parecido.MAX_PARES_POR_MINUTO = tope

    titulo("7 · El mínimo del nodo en el dibujo")

    def nodo(datos: dict) -> dict:
        return {"id": "operador-1", "type": "operador", "position": {"x": 0, "y": 0}, "data": datos}

    limpio = procesos._nodo(nodo({"operador": "parecido", "a": "x", "b": "y"}))
    rev("sin `minimo`: queda 90", limpio["data"].get("minimo") == 90, str(limpio["data"]))
    rev("con 75: queda 75", procesos._nodo(nodo({"operador": "parecido", "minimo": 75}))["data"]["minimo"] == 75)
    for malo in (0, 101, 90.5, "90", True, None):
        try:
            procesos._nodo(nodo({"operador": "parecido", "minimo": malo}))
            rev(f"`minimo` = {malo!r} se rechaza", False)
        except procesos.ProcesoInvalido:
            rev(f"`minimo` = {malo!r} se rechaza", True)
    rev("los otros operadores no llevan `minimo` (aunque lo traigan)", "minimo" not in procesos._nodo(nodo({"operador": "igual", "minimo": 50}))["data"])

    titulo("8 · La forma de escribir no cuenta")
    rev("comparable: mayúsculas, sin acentos ni tilde de la ñ, sin espacios de más", parecido.comparable("  Pérez   lópez ") == "PEREZ LOPEZ" and parecido.comparable("Peña") == "PENA" and parecido.comparable("Straße") == "STRASSE")
    LLAMADAS.clear()
    r = medir(O["ANALISTA"], O["guid"], [{"a": "KEYSI", "b": "KEISY"}])
    primero = r.json()["resultados"][0]["porcentaje"] if r.status_code == 200 else None
    pregunta = LLAMADAS[0]["contents"][0]["parts"][0]["text"].split("Pares:\n", 1)[1] if LLAMADAS else ""
    rev("«KEYSI» ~ «KEISY»: se le pregunta a Gemini una vez", r.status_code == 200 and len(LLAMADAS) == 1, f"{r.status_code} {len(LLAMADAS)}")
    for a, b in (("Keysi", "KEISY"), ("keysi", "keisy"), ("Keisy", "Keysi")):
        r = medir(O["ANALISTA"], O["guid"], [{"a": a, "b": b}])
        rev(f"«{a}» ~ «{b}»: la misma respuesta y SIN volver a preguntar", r.status_code == 200 and r.json()["resultados"][0]["porcentaje"] == primero and len(LLAMADAS) == 1, f"{r.text[:120]} llamadas {len(LLAMADAS)}")
    LLAMADAS.clear()
    medir(O["ANALISTA"], O["guid"], [{"a": "José Pérez", "b": "JOSE PERES"}])
    pregunta = LLAMADAS[0]["contents"][0]["parts"][0]["text"].split("Pares:\n", 1)[1] if LLAMADAS else ""
    rev("Gemini recibe los textos ya en mayúsculas y sin acentos", '"JOSE PEREZ"' in pregunta and '"JOSE PERES"' in pregunta and "é" not in pregunta, pregunta)
    LLAMADAS.clear()
    r = medir(O["ANALISTA"], O["guid"], [{"a": "Peña  Núñez", "b": "PENA NUNEZ"}, {"a": "acme", "b": "ACME"}])
    res = r.json().get("resultados", [])
    rev("lo que solo se escribe distinto: 100, «es el mismo texto», y sin preguntarle a Gemini", r.status_code == 200 and [x["porcentaje"] for x in res] == [100, 100] and all(x["razon"] == parecido.RAZON_MISMO_TEXTO for x in res) and not LLAMADAS, f"{r.text[:160]} llamadas {len(LLAMADAS)}")
    LLAMADAS.clear()
    r = medir(O["ANALISTA"], O["guid"], [{"a": "sale bien", "b": "zz"}, {"a": "FALLA-AQUI", "b": "zz"}])
    rev("si la llamada de un par falla, la petición falla (502): no se devuelve una medición a medias", r.status_code == 502 and len(LLAMADAS) == 2, f"{r.status_code} {len(LLAMADAS)}")
    LLAMADAS.clear()
    r = medir(O["ANALISTA"], O["guid"], [{"a": "SALE BIEN", "b": "ZZ"}])
    rev("…pero el par que sí salió quedó en la caché", r.status_code == 200 and not LLAMADAS, f"{r.status_code} {len(LLAMADAS)}")
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print("\n" + ("TODO BIEN" if fallos == 0 else f"{fallos} FALLAS"))
raise SystemExit(0 if fallos == 0 else 1)
