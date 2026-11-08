"""¿«Sugerir descripción» (Gemini por Vertex AI) hace lo que debe: lo pide solo quien edita tipos, le cuenta al modelo lo correcto, no guarda nada
y sus fallos no filtran detalles de Google?

    .venv/Scripts/python verificar_sugerir_descripcion.py

OFFLINE y sin tocar nada tuyo, como los demás `verificar_*`: el almacén es una carpeta temporal y Gemini se sustituye por un doble que anota lo que
le llegaría y contesta lo que cada caso necesita. NO se llama a Google (la llamada real se probó aparte, a mano, el 2026-11-05).

Lo que se prueba:

- el rol: Administrador y Supervisor sí; Operador, 403 `sin_permiso` (con `ROLE_DENIED`); otra organización, 403 `sin_acceso`; sin sesión o sin llave, no;
- a qué se llama: la URL (proyecto, región y modelo del `.env`), la credencial y la cabecera `x-goog-user-project`, y un tope de tokens holgado
  (los modelos que «piensan» se cortan con pocos);
- qué se le cuenta al modelo: el nombre y los OTROS tipos de la organización, sin el propio, sin los archivados y sin los de otra organización;
  con tope de cuántos y de cuán largos, sin repetidos y sin las comillas angulares con que se acota el nombre;
- lo que se devuelve: un párrafo limpio (sin rótulo, viñeta, negritas ni comillas), con techo de largo, y una respuesta cortada por tokens no se
  entrega a medias;
- que no guarda nada (los tipos quedan igual) y que un nombre en blanco se rechaza sin llamar a Google;
- CON documentos de ejemplo (2026-11-07): de 1 a 3, en PDF, JPG o PNG, adjuntos a la petición como `inlineData` ANTES del texto; el prompt lo dice y pide no copiar datos
  de las personas; el TIFF, un cuarto archivo, un archivo vacío, un peso excesivo o un nombre en blanco se rechazan SIN llamar a Gemini; mismos roles y mismos fallos;
- los fallos: sin proyecto o credencial 503, Google 401/403/404 503, 429 429, 5xx, tiempo agotado, red, vacía o bloqueada 502; y el mensaje nunca trae el
  proyecto, el modelo ni lo que dijo Google.
"""

import base64
import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import httpx

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-sugerir-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import auth, gemini, usuarios  # noqa: E402

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


def nuevo_id() -> str:
    return "tipo-" + uuid.uuid4().hex


def guardar_tipo(bearer: str, tenant: str, tipo_id: str, nombre: str, estado: str = "borrador") -> None:
    r = c.put(
        f"/tipos-documentales/{tipo_id}",
        json={"tenant": tenant, "revision": 0, "datos": {"nombre": nombre, "descripcion": f"D de {nombre}", "estado": estado, "campos": [], "procesadorId": ""}},
        headers=cab(bearer),
    )
    assert r.status_code == 200, r.text


def tipos_de(bearer: str, tenant: str) -> str:
    return json.dumps(c.get("/tipos-documentales/", params={"tenant": tenant}, headers=cab(bearer)).json(), sort_keys=True)


def auditoria(accion: str) -> list[dict]:
    ruta = RAIZ / ".registro" / "auditoria.jsonl"
    if not ruta.exists():
        return []
    return [e for e in (json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip()) if e.get("accion") == accion]


# ── El doble de Gemini ──────────────────────────────────────────────────────────────────────────────────────────────────────
llamadas: list[dict] = []
guion: dict = {"respuesta": (200, {"candidates": [{"content": {"parts": [{"text": "Es un documento de prueba."}]}, "finishReason": "STOP"}]}), "falla": None}


async def _llamar_falso(url, headers, cuerpo, timeout):
    llamadas.append({"url": url, "headers": headers, "cuerpo": cuerpo, "timeout": timeout})
    if guion["falla"] is not None:
        raise guion["falla"]
    return guion["respuesta"]


def contestar(texto: str, fin: str = "STOP", estado: int = 200) -> None:
    guion["falla"] = None
    guion["respuesta"] = (estado, {"candidates": [{"content": {"parts": [{"text": texto}]}, "finishReason": fin}]})


def prompt_enviado() -> str:
    return llamadas[-1]["cuerpo"]["contents"][0]["parts"][0]["text"]


gemini._llamar = _llamar_falso
gemini._token = lambda: "token-falso"
gemini.DOCAI_PROJECT_ID = "proyecto-de-prueba-123"
gemini.GEMINI_MODELO = "modelo-de-prueba"
gemini.GEMINI_REGION = "global"
gemini.GEMINI_TIMEOUT = 17


def pedir(bearer: str | None, tenant: str, nombre: str, tipo_id: str | None = None, servicio: bool = True):
    cuerpo = {"tenant": tenant, "nombre": nombre, **({"tipoId": tipo_id} if tipo_id else {})}
    return c.post("/tipos-documentales/sugerir-descripcion", json=cuerpo, headers=cab(bearer, servicio))


try:
    titulo("0 · Montaje: dos organizaciones; la primera con administrador, supervisor y operador")
    temporal_super = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal_super))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal_super, "Cobre-Lento-Azul-47!")
    org_a, a_admin = crear_organizacion(bearer_super, "Alfa", "rita@ejemplo.com", "Rumbo-Sereno-Azul-47!")
    org_b, b_admin = crear_organizacion(bearer_super, "Beta", "beto@ejemplo.com", "Rumbo-Sereno-Rojo-48!")
    a_supervisor = sumar_persona(a_admin, "omar@ejemplo.com", "SUPERVISOR", "Cielo-Abierto-Verde-52!")
    a_operador = sumar_persona(a_admin, "olga@ejemplo.com", "OPERADOR", "Cielo-Abierto-Verde-53!")
    propio = nuevo_id()
    guardar_tipo(a_admin, org_a, nuevo_id(), "INE", "activo")
    guardar_tipo(a_admin, org_a, nuevo_id(), "Pasaporte", "borrador")
    guardar_tipo(a_admin, org_a, nuevo_id(), "Viejo archivado", "archivado")
    guardar_tipo(a_admin, org_a, propio, "Constancia Fiscal", "borrador")
    guardar_tipo(b_admin, org_b, nuevo_id(), "Secreto de Beta", "activo")
    rev("hay dos organizaciones y tres personas con rol distinto en la primera", org_a != org_b and all([a_admin, a_supervisor, a_operador, b_admin]))
    antes = tipos_de(a_admin, org_a)

    titulo("1 · Quién puede pedirla")
    r = pedir(a_admin, org_a, "Acta Constitutiva")
    rev("el Administrador: 200 con la descripción y el modelo", r.status_code == 200 and r.json() == {"descripcion": "Es un documento de prueba.", "modelo": "modelo-de-prueba"}, r.text[:200])
    r = pedir(a_supervisor, org_a, "Acta Constitutiva")
    rev("el Supervisor (también edita tipos): 200", r.status_code == 200, r.text[:200])
    n = len(llamadas)
    r = pedir(a_operador, org_a, "Acta Constitutiva")
    rev("el Operador: 403 `sin_permiso`", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso", r.text[:200])
    rev("…con `ROLE_DENIED` en la bitácora y SIN llamar a Gemini (no gasta)", len(auditoria("ROLE_DENIED")) >= 1 and len(llamadas) == n)
    r = pedir(b_admin, org_a, "Acta Constitutiva")
    rev("el administrador de OTRA organización pidiendo con el tenant ajeno: 403 `sin_acceso`", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso" and len(llamadas) == n, r.text[:200])
    r = pedir(None, org_a, "Acta Constitutiva")
    rev("sin sesión de persona: no pasa y no llama a Gemini", r.status_code in (401, 403) and len(llamadas) == n, f"{r.status_code} {r.text[:120]}")
    r = pedir(a_admin, org_a, "Acta Constitutiva", servicio=False)
    rev("sin la llave de servicio: no pasa y no llama a Gemini", r.status_code in (401, 403) and len(llamadas) == n, f"{r.status_code} {r.text[:120]}")

    titulo("2 · A qué se llama")
    pedir(a_admin, org_a, "Acta Constitutiva")
    ll = llamadas[-1]
    rev("la URL lleva el proyecto, la región y el modelo del .env", ll["url"] == "https://aiplatform.googleapis.com/v1/projects/proyecto-de-prueba-123/locations/global/publishers/google/models/modelo-de-prueba:generateContent", ll["url"])
    rev("con la credencial y la cabecera del proyecto al que se cobra", ll["headers"] == {"Authorization": "Bearer token-falso", "x-goog-user-project": "proyecto-de-prueba-123"}, str(ll["headers"]))
    rev("con el tiempo de espera configurado", ll["timeout"] == 17)
    rev("y un tope de tokens holgado (los modelos que «piensan» se cortan con pocos)", ll["cuerpo"]["generationConfig"]["maxOutputTokens"] >= 1000, str(ll["cuerpo"]["generationConfig"]))
    gemini.GEMINI_REGION = "us-central1"
    pedir(a_admin, org_a, "Acta Constitutiva")
    rev("con una región regional el host cambia", llamadas[-1]["url"].startswith("https://us-central1-aiplatform.googleapis.com/v1/projects/proyecto-de-prueba-123/locations/us-central1/"), llamadas[-1]["url"])
    gemini.GEMINI_REGION = "global"

    titulo("3 · Qué se le cuenta al modelo")
    pedir(a_admin, org_a, "  Acta   Constitutiva ")
    p = prompt_enviado()
    rev("el nombre, sin espacios de más", "Tipo: «Acta Constitutiva»." in p, p[:200])
    rev("los otros tipos de la organización (activos y borradores)", "INE" in p and "Pasaporte" in p and "Constancia Fiscal" in p, p)
    rev("NO los archivados ni los de otra organización", "Viejo archivado" not in p and "Secreto de Beta" not in p, p)
    # El caso real: la persona le está CAMBIANDO el nombre al tipo en el formulario («Constancia Fiscal» → «Constancia Fiscal 2025»). El nombre que
    # el tipo todavía tiene guardado NO es «otro»: sin el `tipoId` aparecería entre los otros, y sin esta distinción el id no serviría de nada.
    pedir(a_admin, org_a, "Constancia Fiscal 2025", propio)
    p = prompt_enviado()
    rev("editando un tipo (aunque le cambie el nombre), él mismo no cuenta entre «los otros»", "Otros tipos que ya existen en la organización: INE, Pasaporte." in p, p)
    pedir(a_admin, org_a, "Constancia Fiscal 2025")
    rev("…y sin decir cuál es, el nombre que tenía guardado sí aparece (es la prueba de que el id es lo que lo excluye)", "Constancia Fiscal" in prompt_enviado().split("Otros tipos")[-1], prompt_enviado())
    pedir(a_admin, org_a, "constancia FISCAL")
    r_sin = prompt_enviado()
    rev("…y el que se pide con otras mayúsculas no se repite entre los otros", r_sin.split("Otros tipos que ya existen en la organización:")[-1].split(".")[0].lower().count("constancia fiscal") == 0, r_sin)
    pedir(b_admin, org_b, "Licencia")
    rev("Beta solo se entera de SUS tipos: ninguno de los de Alfa", "Secreto de Beta" in prompt_enviado() and not any(x in prompt_enviado() for x in ("INE", "Pasaporte", "Constancia Fiscal")), prompt_enviado())
    rev("sin otros tipos, el prompt no trae esa frase de más", "Otros tipos" not in gemini.armar_prompt("Licencia", []) and "Otros tipos" not in gemini.armar_prompt("Licencia", ["licencia", "  "]))
    pedir(a_admin, org_a, "Con «comillas» angulares\ny salto de línea")
    rev("las comillas angulares y los saltos de línea del nombre no rompen el prompt", "Tipo: «Con comillas angulares y salto de línea»." in prompt_enviado(), prompt_enviado())
    rev("el prompt pide no inventar y distinguir de los otros", "No inventes datos" in prompt_enviado() and "distinguirlo de los otros" in prompt_enviado())
    muchos = [f"Tipo número {i}" for i in range(80)] + ["tipo número 1", "  Tipo número 2  ", "A" * 300]
    p = gemini.armar_prompt("Nuevo", muchos)
    lista = p.split("Otros tipos que ya existen en la organización: ")[1].split(". Di qué")[0].split(", ")
    rev("con cientos de tipos, solo se le dicen los primeros 30 y sin repetidos", len(lista) == gemini.MAX_OTROS == 30 and len(set(x.casefold() for x in lista)) == 30, str(len(lista)))
    p3 = gemini.armar_prompt("Nuevo", ["INE", "ine", "  INE ", "Pasaporte", "pasaporte", "Nuevo", "NUEVO"])
    rev("los repetidos (sin distinguir mayúsculas ni espacios de más) y el propio nombre se le cuentan una sola vez o ninguna", "Otros tipos que ya existen en la organización: INE, Pasaporte." in p3, p3)
    p2 = gemini.armar_prompt("Nuevo", ["B" * 300])
    rev("y cada nombre largo se recorta", ("B" * gemini.MAX_NOMBRE) in p2 and ("B" * (gemini.MAX_NOMBRE + 1)) not in p2)

    titulo("4 · Lo que se devuelve")
    for crudo, esperado, que in [
        ("**Descripción:** El INE es la credencial para votar.", "El INE es la credencial para votar.", "rótulo y negritas"),
        ("Sugerencia: «El INE es la credencial para votar.»", "El INE es la credencial para votar.", "rótulo y comillas angulares"),
        ('"Es un documento."', "Es un documento.", "comillas rectas"),
        ("- Primera frase.\n  Segunda frase.", "Primera frase. Segunda frase.", "viñeta y salto de línea en un solo párrafo"),
        ("   Solo espacios alrededor.  \n", "Solo espacios alrededor.", "espacios"),
    ]:
        contestar(crudo)
        r = pedir(a_admin, org_a, "Acta")
        rev(f"limpia: {que}", r.status_code == 200 and r.json()["descripcion"] == esperado, r.text[:200])
    contestar("Frase número uno es larga. " * 60)
    r = pedir(a_admin, org_a, "Acta")
    d = r.json()["descripcion"]
    rev("un texto larguísimo se corta en un punto y no pasa del techo", r.status_code == 200 and len(d) <= gemini.MAX_DESCRIPCION and d.endswith("."), f"{len(d)} {d[-30:]}")
    contestar("Primera frase completa. Segunda frase a medias y se cort", fin="MAX_TOKENS")
    r = pedir(a_admin, org_a, "Acta")
    rev("una respuesta cortada por tokens se entrega solo hasta la última frase completa", r.status_code == 200 and r.json()["descripcion"] == "Primera frase completa.", r.text[:200])
    contestar("Una sola frase sin punto que se cort", fin="MAX_TOKENS")
    r = pedir(a_admin, org_a, "Acta")
    rev("y si ni una frase cerró, no se entrega nada a medias (502)", r.status_code == 502, f"{r.status_code} {r.text[:160]}")
    guion["respuesta"] = (200, {"candidates": [{"content": {"parts": [{"text": "pensando…", "thought": True}, {"text": "La respuesta real."}]}, "finishReason": "STOP"}]})
    r = pedir(a_admin, org_a, "Acta")
    rev("las partes de «pensamiento» no son la respuesta", r.status_code == 200 and r.json()["descripcion"] == "La respuesta real.", r.text[:200])

    titulo("5 · No guarda nada y rechaza lo vacío")
    contestar("Es una descripción cualquiera.")
    rev("después de todo lo anterior, los tipos de la organización quedaron exactamente igual", tipos_de(a_admin, org_a) == antes)
    n = len(llamadas)
    r = pedir(a_admin, org_a, "   ")
    rev("un nombre en blanco: 400, y no se llama a Gemini", r.status_code == 400 and len(llamadas) == n, f"{r.status_code} {r.text[:160]}")
    r = pedir(a_admin, org_a, "X" * 201)
    rev("un nombre de más de 200 caracteres: 422, y no se llama a Gemini", r.status_code == 422 and len(llamadas) == n, f"{r.status_code}")
    r = c.post("/tipos-documentales/sugerir-descripcion", json={"tenant": org_a}, headers=cab(a_admin))
    rev("sin nombre: 422", r.status_code == 422)

    titulo("6 · Los fallos: el estado correcto y un mensaje que no filtra nada")
    secretos = ("proyecto-de-prueba-123", "modelo-de-prueba", "PERMISO-SECRETO", "token-falso")

    def sin_filtrar(r) -> bool:
        return not any(s in r.text for s in secretos)

    casos = [
        ("Google 403 (API sin habilitar o sin permiso)", (403, {"error": {"status": "PERMISSION_DENIED", "message": "PERMISO-SECRETO en proyecto-de-prueba-123"}}), None, 503),
        ("Google 404 (modelo retirado)", (404, {"error": {"status": "NOT_FOUND", "message": "Publisher model modelo-de-prueba was not found"}}), None, 503),
        ("Google 401", (401, {"error": {"message": "PERMISO-SECRETO"}}), None, 503),
        ("Google 429 (cuota)", (429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "cuota de proyecto-de-prueba-123"}}), None, 429),
        ("Google 500", (500, {"error": {"message": "PERMISO-SECRETO interno"}}), None, 502),
        ("Google 400 (petición mal armada)", (400, {"error": {"message": "PERMISO-SECRETO"}}), None, 502),
        ("Google contesta algo que no es JSON", (502, "<html>PERMISO-SECRETO</html>"), None, 502),
        ("tiempo agotado", None, httpx.ReadTimeout("PERMISO-SECRETO"), 502),
        ("error de red", None, httpx.ConnectError("PERMISO-SECRETO"), 502),
        ("sin candidatos (bloqueada)", (200, {"promptFeedback": {"blockReason": "SAFETY"}}), None, 502),
        ("candidato sin texto", (200, {"candidates": [{"content": {"parts": []}, "finishReason": "SAFETY"}]}), None, 502),
        ("texto en blanco", (200, {"candidates": [{"content": {"parts": [{"text": "  \n "}]}, "finishReason": "STOP"}]}), None, 502),
    ]
    for que, respuesta, falla, esperado in casos:
        guion["respuesta"], guion["falla"] = respuesta, falla
        r = pedir(a_admin, org_a, "Acta")
        rev(f"{que}: {esperado} con un mensaje que se puede mostrar y no filtra proyecto, modelo ni lo que dijo Google",
            r.status_code == esperado and isinstance(r.json().get("detail"), str) and len(r.json()["detail"]) > 20 and sin_filtrar(r), f"{r.status_code} {r.text[:200]}")
    guion["falla"] = None
    contestar("Vuelve a funcionar.")
    r = pedir(a_admin, org_a, "Acta")
    rev("y al volver Google, vuelve a funcionar sin reiniciar nada", r.status_code == 200 and r.json()["descripcion"] == "Vuelve a funcionar.")

    gemini.DOCAI_PROJECT_ID = ""
    n = len(llamadas)
    r = pedir(a_admin, org_a, "Acta")
    rev("sin proyecto de Google en el .env: 503, sin llamar a nadie", r.status_code == 503 and len(llamadas) == n and sin_filtrar(r), f"{r.status_code} {r.text[:160]}")
    gemini.DOCAI_PROJECT_ID = "proyecto-de-prueba-123"

    def sin_credencial():
        raise RuntimeError("PERMISO-SECRETO: no hay credencial")

    gemini._token = sin_credencial
    r = pedir(a_admin, org_a, "Acta")
    rev("sin credencial de Google: 503, sin llamar a nadie y sin filtrar el motivo", r.status_code == 503 and len(llamadas) == n and sin_filtrar(r), f"{r.status_code} {r.text[:160]}")
    gemini._token = lambda: "token-falso"
    titulo("10 · Con documentos de ejemplo")
    PDF_1 = b"%PDF-1.4\n%ejemplo uno con datos de Juan Perez Lopez\n"
    PDF_2 = b"%PDF-1.4\n%ejemplo dos\n" + bytes(range(256))
    PNG_1 = b"\x89PNG\r\n\x1a\n" + b"imagen"

    def pedir_con(bearer, tenant, nombre, archivos, tipo_id="", servicio=True):
        datos = {"tenant": tenant, "nombre": nombre, **({"tipoId": tipo_id} if tipo_id else {})}
        return c.post("/tipos-documentales/sugerir-descripcion-con-documentos", data=datos, files=[("archivos", a) for a in archivos], headers=cab(bearer, servicio))

    contestar("Acta que emite un notario y trae los datos de la sociedad.")
    n = len(llamadas)
    r = pedir_con(a_admin, org_a, "Acta Constitutiva", [("a.pdf", PDF_1, "application/pdf"), ("b.png", PNG_1, "image/png")])
    rev("el Administrador, con 2 documentos: 200 con la descripción, el modelo y cuántos documentos se usaron",
        r.status_code == 200 and r.json() == {"descripcion": "Acta que emite un notario y trae los datos de la sociedad.", "modelo": "modelo-de-prueba", "documentos": 2}, r.text[:200])
    partes = llamadas[-1]["cuerpo"]["contents"][0]["parts"]
    rev("a Gemini le llegan 3 partes: los 2 documentos y, AL FINAL, el texto", len(llamadas) == n + 1 and len(partes) == 3 and "inlineData" in partes[0] and "inlineData" in partes[1] and "text" in partes[2], str([list(x) for x in partes]))
    rev("cada documento va en base64 con su tipo y sus bytes intactos",
        partes[0]["inlineData"] == {"mimeType": "application/pdf", "data": base64.b64encode(PDF_1).decode()} and partes[1]["inlineData"] == {"mimeType": "image/png", "data": base64.b64encode(PNG_1).decode()})
    texto = partes[2]["text"]
    rev("el prompt dice que se adjuntan 2 documentos y pide NO copiar datos de las personas", "Se adjuntan 2 documentos de ejemplo" in texto and "NUNCA copies datos concretos" in texto, texto[:300])
    rev("y sigue contando el nombre y los otros tipos de la organización", "Tipo: «Acta Constitutiva»." in texto and "INE" in texto and "Pasaporte" in texto and "Viejo archivado" not in texto and "Secreto de Beta" not in texto, texto)
    rev("el prompt NO trae los bytes ni el contenido de los documentos (solo van como adjuntos)", "Juan Perez" not in texto and "%PDF" not in texto)
    rev("misma URL, credencial, cabecera y tope de tokens que sin documentos", llamadas[-1]["url"].endswith("modelo-de-prueba:generateContent") and llamadas[-1]["headers"]["x-goog-user-project"] == "proyecto-de-prueba-123" and llamadas[-1]["cuerpo"]["generationConfig"]["maxOutputTokens"] >= 1000)

    r = pedir_con(a_admin, org_a, "Constancia Fiscal 2025", [("a.pdf", PDF_1, "application/pdf")], tipo_id=propio)
    texto = llamadas[-1]["cuerpo"]["contents"][0]["parts"][-1]["text"]
    rev("con 1 documento el prompt habla en singular («Se adjunta 1 documento») y editando un tipo él no cuenta entre los otros",
        r.status_code == 200 and "Se adjunta 1 documento de ejemplo" in texto and "descríbelo a partir de lo que ves en él" in texto and "Otros tipos que ya existen en la organización: INE, Pasaporte." in texto, texto)
    rev("sin documentos, el prompt es el de siempre (nada de «adjunta»)", "adjunta" not in gemini.armar_prompt("X", ["INE"]) and gemini.armar_prompt("X", ["INE"]) == gemini.armar_prompt("X", ["INE"], 0))
    r = pedir_con(a_supervisor, org_a, "Acta", [("a.pdf", PDF_1, "application/pdf")])
    rev("el Supervisor (también edita tipos): 200", r.status_code == 200, r.text[:160])
    r = pedir_con(a_admin, org_a, "Acta", [("a.jpg", b"\xff\xd8\xff\xe0jpg", "image/jpeg")])
    rev("un JPG también se lee", r.status_code == 200 and llamadas[-1]["cuerpo"]["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "image/jpeg", r.text[:160])

    n = len(llamadas)
    r = pedir_con(a_operador, org_a, "Acta", [("a.pdf", PDF_1, "application/pdf")])
    rev("el Operador: 403 `sin_permiso`, sin llamar a Gemini (no gasta)", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso" and len(llamadas) == n, r.text[:160])
    r = pedir_con(b_admin, org_a, "Acta", [("a.pdf", PDF_1, "application/pdf")])
    rev("el administrador de OTRA organización con el tenant ajeno: 403 `sin_acceso`, sin llamar", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso" and len(llamadas) == n, r.text[:160])
    r = pedir_con(None, org_a, "Acta", [("a.pdf", PDF_1, "application/pdf")])
    rev("sin sesión de persona: no pasa y no llama", r.status_code in (401, 403) and len(llamadas) == n, f"{r.status_code} {r.text[:120]}")
    r = pedir_con(a_admin, org_a, "Acta", [("a.pdf", PDF_1, "application/pdf")], servicio=False)
    rev("sin la llave de servicio: no pasa y no llama", r.status_code in (401, 403) and len(llamadas) == n, f"{r.status_code} {r.text[:120]}")

    titulo("10b · Lo que se rechaza sin llamar a Gemini")
    n = len(llamadas)
    r = pedir_con(a_admin, org_a, "Acta", [(f"{i}.pdf", PDF_1, "application/pdf") for i in range(4)])
    rev("un cuarto documento: 400 «elige hasta 3»", r.status_code == 400 and "hasta 3" in r.json()["detail"] and len(llamadas) == n, r.text[:160])
    r = pedir_con(a_admin, org_a, "Acta", [("a.tif", b"II*\x00tiff", "image/tiff")])
    rev("un TIFF (que Document AI sí lee, Gemini no): 400 que dice qué formatos usar", r.status_code == 400 and "PDF, JPG o PNG" in r.json()["detail"] and len(llamadas) == n, r.text[:160])
    r = pedir_con(a_admin, org_a, "Acta", [("a.pdf", PDF_1, "application/pdf"), ("notas.txt", b"hola", "text/plain")])
    rev("un archivo de texto entre los buenos: 400 y NO se llama con los otros", r.status_code == 400 and len(llamadas) == n, r.text[:160])
    r = pedir_con(a_admin, org_a, "Acta", [("vacio.pdf", b"", "application/pdf")])
    rev("un archivo vacío: error de cliente, sin llamar", 400 <= r.status_code < 500 and len(llamadas) == n, f"{r.status_code} {r.text[:160]}")
    r = pedir_con(a_admin, org_a, "   ", [("a.pdf", PDF_1, "application/pdf")])
    rev("un nombre en blanco: 400, sin llamar", r.status_code == 400 and len(llamadas) == n, r.text[:160])
    r = c.post("/tipos-documentales/sugerir-descripcion-con-documentos", data={"tenant": org_a, "nombre": "Acta"}, headers=cab(a_admin))
    rev("sin ningún archivo: error de cliente, sin llamar", 400 <= r.status_code < 500 and len(llamadas) == n, f"{r.status_code} {r.text[:160]}")
    pesado = b"%PDF-1.4\n" + b"x" * (7 * 1024 * 1024)
    casi = b"%PDF-1.4\n" + b"x" * int(6.3 * 1024 * 1024)
    r = pedir_con(a_admin, org_a, "Acta", [(f"p{i}.pdf", casi, "application/pdf") for i in range(3)])
    rev("3 documentos de 6.3 MB (casi 19 MB en total, más que el tope de 18): 413 por el peso de todos juntos, sin llamar", r.status_code == 413 and "demasiado" in r.json()["detail"] and "18 MB" in r.json()["detail"] and len(llamadas) == n, f"{r.status_code} {r.text[:160]}")
    r = pedir_con(a_admin, org_a, "Acta", [(f"p{i}.pdf", pesado, "application/pdf") for i in range(3)])
    rev("y 3 de 7 MB (21 MB: pasa del tope de TODA subida) también: 413, sin llamar", r.status_code == 413 and len(llamadas) == n, f"{r.status_code} {r.text[:160]}")
    r = pedir_con(a_admin, org_a, "Acta", [(f"p{i}.pdf", pesado, "application/pdf") for i in range(2)])
    rev("2 de 7 MB (14 MB) sí caben", r.status_code == 200 and len(llamadas) == n + 1, f"{r.status_code} {r.text[:160]}")

    titulo("10c · No guarda nada y sus fallos son los de siempre")
    rev("los tipos de la organización quedan exactamente igual", tipos_de(a_admin, org_a) == antes)
    for estado_g, esperado in ((429, 429), (500, 502), (403, 503)):
        guion["falla"] = None
        guion["respuesta"] = (estado_g, {"error": {"status": "X", "message": "proyecto-de-prueba-123 detalle de Google"}})
        r = pedir_con(a_admin, org_a, "Acta", [("a.pdf", PDF_1, "application/pdf")])
        rev(f"Gemini contesta {estado_g}: {esperado} con un mensaje que se puede mostrar y sin filtrar el proyecto", r.status_code == esperado and "proyecto-de-prueba" not in r.text and "detalle de Google" not in r.text, f"{r.status_code} {r.text[:200]}")
    contestar("Vuelve a funcionar.")
    r = pedir_con(a_admin, org_a, "Acta", [("a.pdf", PDF_1, "application/pdf")])
    rev("y al volver Google, vuelve a funcionar", r.status_code == 200 and r.json()["descripcion"] == "Vuelve a funcionar.")
    gemini.DOCAI_PROJECT_ID = ""
    n = len(llamadas)
    r = pedir_con(a_admin, org_a, "Acta", [("a.pdf", PDF_1, "application/pdf")])
    rev("sin proyecto de Google en el .env: 503, sin llamar a nadie", r.status_code == 503 and len(llamadas) == n, f"{r.status_code} {r.text[:160]}")
    gemini.DOCAI_PROJECT_ID = "proyecto-de-prueba-123"
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
