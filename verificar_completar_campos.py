"""¿«El recuadro completa el campo» y «Dibujar crea el campo» (Gemini) hacen lo que deben? (Moibe, 2026-11-12: «quiero que ese etiquetado sí sirva
para definir el schema»; eligió esas dos.)

    POST /procesadores/describir-campos   varios campos, cada uno con su recuadro → descripción, tipo, ejemplo inventado de la forma y valor leído
    POST /procesadores/proponer-campo     un recuadro dibujado donde no había campo → el campo que sería, con su nombre

Lo pide solo quien administra tipos; el documento va primero; lo que vuelve no lleva datos de ESE documento (ni en la descripción ni en el ejemplo,
que terminan en el esquema que se sube a Google); un campo que no se pudo leer no vuelve; nada se guarda; los fallos no filtran detalles de Google.

    .venv/Scripts/python verificar_completar_campos.py

OFFLINE como los demás `verificar_*`: SQL Server desechable (`pruebas_sql`) y Gemini sustituido por un doble. La llamada real se probó aparte, a mano.
"""

import base64
import json
import os
import shutil
import tempfile
from pathlib import Path

import httpx

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-completar-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
import pruebas_sql  # noqa: E402  (todo se guarda en SQL Server: esta prueba corre contra uno desechable)

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

PNG = b"\x89PNG\r\n\x1a\n" + b"imagen de una credencial"
PDF = b"%PDF-1.4\n%ejemplo\n"
CAMPOS = [
    {"nombre": "Fecha de nacimiento", "tipoDato": "texto", "descripcion": "La fecha en que nació la persona", "pagina": 1, "caja": [460, 321, 486, 432]},
    {"nombre": "CURP", "tipoDato": "texto", "descripcion": "", "pagina": 1, "caja": [560, 321, 586, 535]},
    {"nombre": "Domicilio", "tipoDato": "grupo", "descripcion": "Calle, colonia y municipio", "pagina": 2, "caja": [100, 50, 220, 600]},
    {"nombre": "Sexo", "tipoDato": "lista", "descripcion": "", "pagina": 1, "caja": [460, 621, 486, 635]},
]


def pedir_campos(bearer, tenant, campos=CAMPOS, archivo=("c.png", PNG, "image/png"), servicio=True, tipo=("Credencial", "Credencial para votar"), crudo=None):
    datos = {"tenant": tenant, "campos": crudo if crudo is not None else json.dumps(campos, ensure_ascii=False)}
    if tipo:
        datos["nombreTipo"], datos["descripcionTipo"] = tipo
    return c.post("/procesadores/describir-campos", data=datos, files={"archivo": archivo}, headers=cab(bearer, servicio))


def pedir_propuesta(bearer, tenant, caja=(700, 520, 726, 640), pagina=1, existentes=("CURP", "Sexo"), archivo=("c.png", PNG, "image/png"), servicio=True,
                    tipo=("Credencial", ""), crudo=None):
    datos = {
        "tenant": tenant,
        "pagina": str(pagina),
        "caja": crudo["caja"] if crudo and "caja" in crudo else json.dumps(list(caja)),
        "existentes": crudo["existentes"] if crudo and "existentes" in crudo else json.dumps(list(existentes), ensure_ascii=False),
    }
    if tipo:
        datos["nombreTipo"], datos["descripcionTipo"] = tipo
    return c.post("/procesadores/proponer-campo", data=datos, files={"archivo": archivo}, headers=cab(bearer, servicio))


def sin_filtrar(r) -> bool:
    return "proyecto-de-prueba" not in r.text and "modelo-de-prueba" not in r.text and "detalle de Google" not in r.text


def texto_de_la_ultima() -> str:
    return llamadas[-1]["cuerpo"]["contents"][0]["parts"][1]["text"]


BUENAS = {"campos": [
    {"campo": "Fecha de nacimiento", "descripcion": "Fecha de nacimiento bajo el rótulo FECHA DE NACIMIENTO, abajo a la izquierda, DD/MM/AAAA.", "tipoDato": "fecha",
     "estructura": "15/03/1990", "valor": "29/10/1996"},
    {"campo": "CURP", "descripcion": "Clave CURP de 18 caracteres junto al rótulo CURP.", "tipoDato": "texto", "estructura": "ABCD010203HDFRRR01", "valor": "FOCM961029HMCLSR09"},
    {"campo": "Domicilio", "descripcion": "Las tres líneas del domicilio bajo el rótulo DOMICILIO.", "tipoDato": "texto", "estructura": "C SIEMPRE VIVA 742", "valor": "C LAURELES 12"},
    {"campo": "Sexo", "descripcion": "H o M, a la derecha del nombre, junto al rótulo SEXO.", "tipoDato": "texto", "estructura": "M", "valor": "H"},
]}

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

    titulo("1 · Quién puede pedirlas")
    for ruta, pedir in (("describir-campos", pedir_campos), ("proponer-campo", pedir_propuesta)):
        contestar(BUENAS if ruta == "describir-campos" else {"nombre": "Sección", "descripcion": "Número de sección electoral bajo el rótulo SECCIÓN.", "tipoDato": "texto", "estructura": "0421", "valor": "1859"})
        r = pedir(a_admin, org_a)
        rev(f"{ruta}: el Administrador, 200", r.status_code == 200, r.text[:300])
        r = pedir(a_supervisor, org_a)
        rev(f"{ruta}: el Supervisor (administra tipos), 200", r.status_code == 200, r.text[:200])
        n = len(llamadas)
        r = pedir(a_operador, org_a)
        rev(f"{ruta}: el Operador, 403 `sin_permiso` sin llamar a Gemini", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso" and len(llamadas) == n, r.text[:200])
        r = pedir(b_admin, org_a)
        rev(f"{ruta}: el administrador de OTRA organización, 403 `sin_acceso` sin llamar a Gemini", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso" and len(llamadas) == n, r.text[:200])
        r = pedir(None, org_a)
        rev(f"{ruta}: sin sesión, no pasa", r.status_code in (401, 403) and len(llamadas) == n, f"{r.status_code}")
        r = pedir(a_admin, org_a, servicio=False)
        rev(f"{ruta}: sin la llave de servicio, no pasa", r.status_code in (401, 403) and len(llamadas) == n, f"{r.status_code}")

    titulo("2 · describir-campos: qué se le manda a Gemini")
    contestar(BUENAS)
    pedir_campos(a_admin, org_a)
    ll = llamadas[-1]
    partes = ll["cuerpo"]["contents"][0]["parts"]
    rev("la URL del proyecto y el modelo, con la credencial y la cabecera del proyecto",
        ll["url"].endswith("/projects/proyecto-de-prueba-123/locations/global/publishers/google/models/modelo-de-prueba:generateContent")
        and ll["headers"] == {"Authorization": "Bearer token-falso", "x-goog-user-project": "proyecto-de-prueba-123"}, ll["url"])
    rev("UNA llamada con el documento primero, en base64 e intacto; el texto al final",
        len(partes) == 2 and partes[0]["inlineData"] == {"mimeType": "image/png", "data": base64.b64encode(PNG).decode()} and "text" in partes[1])
    t = partes[1]["text"]
    rev("cada campo con su tipo actual, su página, su caja y su descripción actual",
        "- «Fecha de nacimiento» (tipo de dato actual: texto): página 1, caja [ymin, xmin, ymax, xmax] = [460, 321, 486, 432]. Su descripción actual: «La fecha en que nació la persona»." in t
        and "- «CURP» (tipo de dato actual: texto): página 1, caja [ymin, xmin, ymax, xmax] = [560, 321, 586, 535]. Su descripción actual: no tiene." in t
        and "- «Domicilio» (tipo de dato actual: grupo de campos): página 2," in t and "- «Sexo» (tipo de dato actual: lista de valores):" in t, t[-900:])
    rev("…el tipo documental, y pide que sirva para CUALQUIER documento", "del tipo «Credencial» (Credencial para votar)" in t and "CUALQUIER documento" in t)
    rev("…el tipo de dato entre los cinco que se deducen, y un ejemplo INVENTADO con la misma forma",
        "uno de: texto, numero, fecha, moneda, booleano" in t and "`estructura`: un ejemplo INVENTADO" in t and "Nunca uses el valor de este documento" in t)
    rev("…y que no copie datos de este documento ni en la descripción ni en el ejemplo", "NO copies el valor" in t and "en `descripcion` ni en `estructura`" in t)
    cfg = ll["cuerpo"]["generationConfig"]
    propiedades = cfg["responseSchema"]["properties"]["campos"]["items"]["properties"]
    rev("JSON con esquema por campo, temperatura 0 y tope de salida holgado",
        cfg["responseMimeType"] == "application/json" and {"campo", "descripcion", "tipoDato", "estructura", "valor"} <= set(propiedades)
        and cfg["temperature"] == 0 and cfg["maxOutputTokens"] == ubicar.MAX_TOKENS_COMPLETAR)
    rev("…y el valor se escribe ANTES que la descripción (`propertyOrdering`: primero lee la caja, luego la describe)",
        cfg["responseSchema"]["properties"]["campos"]["items"]["propertyOrdering"][:3] == ["campo", "valor", "descripcion"])

    titulo("3 · describir-campos: lo que se devuelve")
    contestar(BUENAS)
    r = pedir_campos(a_admin, org_a)
    d = r.json()
    por = {x["nombre"]: x for x in d["campos"]}
    rev("200 con un renglón por campo, en el orden pedido, y el modelo", r.status_code == 200 and [x["nombre"] for x in d["campos"]] == [x["nombre"] for x in CAMPOS] and d["modelo"] == "modelo-de-prueba", r.text[:300])
    rev("cada renglón trae exactamente descripción, tipo, ejemplo de la forma y valor leído", all(set(x) == {"nombre", "descripcion", "tipoDato", "valorEstructura", "valor"} for x in d["campos"]), str(d["campos"][0]))
    rev("un campo de texto que se lee como fecha pasa a `fecha`, con su ejemplo inventado", por["Fecha de nacimiento"]["tipoDato"] == "fecha" and por["Fecha de nacimiento"]["valorEstructura"] == "15/03/1990", str(por["Fecha de nacimiento"]))
    rev("…y el valor leído vuelve tal cual (solo para enseñarlo)", por["Fecha de nacimiento"]["valor"] == "29/10/1996" and por["CURP"]["valor"] == "FOCM961029HMCLSR09")
    rev("a un GRUPO no se le cambia el tipo ni se le pone ejemplo", por["Domicilio"]["tipoDato"] is None and por["Domicilio"]["valorEstructura"] == "", str(por["Domicilio"]))
    rev("a una LISTA no se le cambia el tipo, pero sí lleva su ejemplo", por["Sexo"]["tipoDato"] is None and por["Sexo"]["valorEstructura"] == "M", str(por["Sexo"]))

    contestar({"campos": [
        {"campo": "fecha de nacimiento", "descripcion": "Fecha de nacimiento.", "tipoDato": "Fecha"},
        {"campo": "CURP", "descripcion": "   "},
        {"campo": "Un campo que nadie pidió", "descripcion": "x"},
    ]})
    d = pedir_campos(a_admin, org_a).json()
    rev("el nombre vuelve TAL COMO se pidió aunque el modelo lo escriba distinto; lo que nadie pidió se ignora",
        [x["nombre"] for x in d["campos"]] == ["Fecha de nacimiento"] and d["campos"][0]["tipoDato"] == "fecha", str(d)[:300])
    rev("…y un campo sin descripción, o que no vino, no vuelve (el front lo deja como estaba)", len(d["campos"]) == 1)

    contestar({"campos": [{"campo": "CURP", "descripcion": "La CURP FOCM961029HMCLSR09 junto al rótulo CURP.", "valor": "FOCM961029HMCLSR09", "estructura": "FOCM961029HMCLSR09"}]})
    x = pedir_campos(a_admin, org_a).json()["campos"][0]
    rev("si el modelo copió el valor en la descripción, se quita", x["descripcion"] == "La CURP junto al rótulo CURP.", x["descripcion"])
    rev("…y un «ejemplo» que es el valor mismo se tira", x["valorEstructura"] == "")

    def ejemplo(valor: str, estructura: str, tipo: str = "texto") -> str:
        contestar({"campos": [{"campo": "CURP", "descripcion": "Algo.", "valor": valor, "estructura": estructura, "tipoDato": tipo}]})
        return pedir_campos(a_admin, org_a).json()["campos"][0]["valorEstructura"]

    rev("se tira el valor con otros separadores (29-10-1996 por 29/10/1996)", ejemplo("29/10/1996", "29-10-1996") == "")
    rev("se tira un pedazo del valor (solo el año completo: 1996 de 29/10/1996)", ejemplo("29/10/1996", "1996") == "")
    rev("se tira un ejemplo que repite una palabra del valor (un apellido)", ejemplo("RAMIREZ SOTO", "RAMIREZ PEREZ") == "")
    rev("se tira un código que copia 8 caracteres seguidos del real (la fecha de nacimiento dentro de una CURP)", ejemplo("FOCM961029HMCLSR09", "ABCD961029HMXXXX01") == "")
    rev("SE QUEDA un importe con la misma forma (los ceros y la moneda no son datos de nadie)", ejemplo("$18,500.00 MXN", "$25,000.00 MXN", "moneda") == "$25,000.00 MXN")
    rev("SE QUEDA una razón social distinta con la misma terminación «SA DE CV»", ejemplo("INMOBILIARIA LOS PINOS SA DE CV", "CONSTRUCTORA DEL VALLE SA DE CV") == "CONSTRUCTORA DEL VALLE SA DE CV")
    rev("SE QUEDA una fecha inventada aunque comparta un dígito suelto", ejemplo("29/10/1996", "15/03/1990", "fecha") == "15/03/1990")
    contestar({"campos": [{"campo": "CURP", "descripcion": "Duración del contrato en meses.", "valor": "12 meses", "estructura": "24 meses"}]})
    rev("SE QUEDA un ejemplo que repite una palabra que ya dice la descripción («meses»; con Gemini real se tiraba)",
        pedir_campos(a_admin, org_a).json()["campos"][0]["valorEstructura"] == "24 meses")
    contestar({"campos": [{"campo": "Calle", "descripcion": "La primera línea del domicilio.", "valor": "CALLE LAURELES 12", "estructura": "CALLE PINOS 34"}]})
    x = pedir_campos(a_admin, org_a, campos=[{"nombre": "Calle", "tipoDato": "texto", "descripcion": "", "pagina": 1, "caja": [100, 50, 130, 600]}]).json()["campos"][0]
    rev("…o el nombre del campo, aunque no lo diga la descripción («CALLE» en «Calle»)", x["valorEstructura"] == "CALLE PINOS 34", str(x))
    contestar({"campos": [{"campo": "Calle", "descripcion": "La primera línea del domicilio.", "valor": "CALLE LAURELES 12", "estructura": "CALLE LAURELES 99"}]})
    x = pedir_campos(a_admin, org_a, campos=[{"nombre": "Calle", "tipoDato": "texto", "descripcion": "", "pagina": 1, "caja": [100, 50, 130, 600]}]).json()["campos"][0]
    rev("…pero una palabra del valor que no es genérica («LAURELES») sí lo tira", x["valorEstructura"] == "", str(x))
    contestar({"campos": [{"campo": "Calle", "descripcion": "Número del contrato de arrendamiento.", "valor": "ARRENDAMIENTO 2024-117", "estructura": "ARRENDAMIENTO 2023-045"}]})
    x = pedir_campos(a_admin, org_a, campos=[{"nombre": "Calle", "tipoDato": "texto", "descripcion": "", "pagina": 1, "caja": [100, 50, 130, 600]}]).json()["campos"][0]
    rev("…y una palabra genérica LARGA tampoco cuenta como pedazo de un código (en el navegador se tiraba «Ejemplo de Observaciones»)",
        x["valorEstructura"] == "ARRENDAMIENTO 2023-045", str(x))
    rev(f"un ejemplo larguísimo se recorta a {ubicar.MAX_ESTRUCTURA} caracteres", len(ejemplo("A1", "B" * 500)) == ubicar.MAX_ESTRUCTURA)

    def tipo_de(crudo) -> object:
        contestar({"campos": [{"campo": "CURP", "descripcion": "Algo.", "tipoDato": crudo}]})
        return pedir_campos(a_admin, org_a).json()["campos"][0]["tipoDato"]

    rev("los tipos se entienden con acentos, mayúsculas, en inglés o con sinónimos",
        [tipo_de(t) for t in ("Número", "DATE", "currency", "boolean", " texto ")] == ["numero", "fecha", "moneda", "booleano", "texto"])
    rev("un tipo desconocido, «lista», «grupo» o que no es texto: no se propone (`null`)", [tipo_de(t) for t in ("color", "lista", "grupo", 7)] == [None, None, None, None])

    titulo("4 · describir-campos: lo que se rechaza sin llamar a Gemini")
    n = len(llamadas)
    for desc, kw in [
        ("un TIFF", {"archivo": ("a.tif", b"II*\x00datos", "image/tiff")}),
        ("campos que no son JSON", {"crudo": "no"}),
        ("campos que no son una lista", {"crudo": json.dumps({"nombre": "x"})}),
        ("una lista vacía", {"campos": []}),
        ("un campo sin nombre", {"campos": [{"pagina": 1, "caja": [1, 2, 30, 40]}]}),
        ("un nombre en blanco", {"campos": [{"nombre": "  ", "pagina": 1, "caja": [1, 2, 30, 40]}]}),
        ("un campo sin recuadro", {"campos": [{"nombre": "A", "pagina": 1}]}),
        ("un recuadro al revés", {"campos": [{"nombre": "A", "pagina": 1, "caja": [500, 500, 400, 600]}]}),
        ("la página 0", {"campos": [{"nombre": "A", "pagina": 0, "caja": [1, 2, 30, 40]}]}),
        ("una página que no es número", {"campos": [{"nombre": "A", "pagina": "2", "caja": [1, 2, 30, 40]}]}),
        ("un nombre repetido (sin importar mayúsculas ni acentos)", {"campos": [{"nombre": "Sección", "caja": [1, 2, 30, 40]}, {"nombre": "seccion", "caja": [1, 2, 30, 40]}]}),
        (f"más de {ubicar.MAX_CAMPOS} campos", {"campos": [{"nombre": f"C{i}", "caja": [1, 2, 30, 40]} for i in range(ubicar.MAX_CAMPOS + 1)]}),
        ("un archivo vacío", {"archivo": ("a.png", b"", "image/png")}),
    ]:
        r = pedir_campos(a_admin, org_a, **kw)
        rev(f"{desc}: 400", r.status_code == 400 and len(llamadas) == n, f"{r.status_code} {r.text[:160]}")
    r = pedir_campos(a_admin, org_a, campos=[{"nombre": "Sección", "caja": [1, 2, 30, 40]}, {"nombre": "seccion", "caja": [1, 2, 30, 40]}])
    rev("…con un mensaje que dice cuál se repite", r.json()["detail"] == "El campo «seccion» viene repetido.", r.text)
    viejo = gemini.MAX_BYTES_DOCUMENTOS
    gemini.MAX_BYTES_DOCUMENTOS = 10
    r = pedir_campos(a_admin, org_a)
    rev("un documento que pesa más de lo que admite Gemini: 413 sin llamarlo", r.status_code == 413 and len(llamadas) == n, f"{r.status_code} {r.text[:160]}")
    gemini.MAX_BYTES_DOCUMENTOS = viejo
    contestar(BUENAS)
    r = pedir_campos(a_admin, org_a, tipo=None, archivo=("a.pdf", PDF, "application/pdf"))
    rev("sin tipo documental y con un PDF: funciona, y el PDF viaja como PDF",
        r.status_code == 200 and "del tipo" not in texto_de_la_ultima() and llamadas[-1]["cuerpo"]["contents"][0]["parts"][0]["inlineData"]["mimeType"] == "application/pdf")

    titulo("5 · describir-campos: los fallos")
    for estado_g, esperado in [(429, 429), (401, 503), (403, 503), (404, 503), (500, 502)]:
        guion["falla"] = None
        guion["respuesta"] = (estado_g, {"error": {"status": "X", "message": "proyecto-de-prueba-123 detalle de Google"}})
        r = pedir_campos(a_admin, org_a)
        rev(f"Gemini contesta {estado_g}: {esperado} sin filtrar nada", r.status_code == esperado and sin_filtrar(r), f"{r.status_code} {r.text[:200]}")
    for desc, falla in [("tiempo agotado", httpx.ReadTimeout("lento")), ("error de red", httpx.ConnectError("sin red"))]:
        guion["falla"] = falla
        r = pedir_campos(a_admin, org_a)
        rev(f"{desc}: 502", r.status_code == 502 and sin_filtrar(r), f"{r.status_code} {r.text[:160]}")
    for desc, texto in [
        ("texto que no es JSON", "aquí van los campos"),
        ("JSON sin `campos`", '{"descripcion": "x"}'),
        ("ningún campo usable", '{"campos": [{"campo": "CURP", "descripcion": ""}, {"campo": "Otro", "descripcion": "x"}]}'),
        ("respuesta vacía", ""),
    ]:
        contestar(texto)
        r = pedir_campos(a_admin, org_a)
        rev(f"{desc}: 502 con un mensaje que se puede mostrar", r.status_code == 502 and r.json()["detail"] == ubicar.MENSAJE_COMPLETAR_FALLO and sin_filtrar(r), f"{r.status_code} {r.text[:160]}")
    n = len(llamadas)
    ubicar.DOCAI_PROJECT_ID = ""
    r = pedir_campos(a_admin, org_a)
    rev("sin proyecto en el .env: 503 sin llamar", r.status_code == 503 and len(llamadas) == n and r.json()["detail"] == ubicar.MENSAJE_COMPLETAR_NO_DISPONIBLE, f"{r.status_code} {r.text[:160]}")
    ubicar.DOCAI_PROJECT_ID = "proyecto-de-prueba-123"

    titulo("6 · proponer-campo")
    contestar({"nombre": "«Sección»", "descripcion": "Número de sección electoral 1859, bajo el rótulo SECCIÓN, abajo al centro.", "tipoDato": "Número", "estructura": "0421", "valor": "1859"})
    r = pedir_propuesta(a_admin, org_a)
    d = r.json()
    rev("200 con nombre, descripción, tipo, ejemplo, valor y modelo",
        r.status_code == 200 and set(d) == {"nombre", "descripcion", "tipoDato", "valorEstructura", "valor", "modelo"} and d["modelo"] == "modelo-de-prueba", r.text[:300])
    rev("el nombre sin comillas angulares; el tipo entendido; el ejemplo inventado se queda",
        d["nombre"] == "Sección" and d["tipoDato"] == "numero" and d["valorEstructura"] == "0421", str(d))
    rev("el valor de este documento se quita de la descripción", d["descripcion"] == "Número de sección electoral, bajo el rótulo SECCIÓN, abajo al centro.", d["descripcion"])
    t = texto_de_la_ultima()
    rev("el prompt dice la página y la caja dibujadas, y los campos que ya existen (si es uno de ellos, con su mismo nombre)",
        "página 1, caja [ymin, xmin, ymax, xmax] = [700, 520, 726, 640]" in t and "Estos campos ya existen: «CURP», «Sexo». Si lo que está dentro de la caja es uno de ellos, devuelve ese nombre escrito igual" in t, t[:900])
    rev("…y que describa SIEMPRE lo que está dentro de la caja (con Gemini real, sin esto inventaba otro campo para no repetir)", "Describe SIEMPRE lo que está dentro de la caja, aunque ya exista." in t)
    rev("…pide el nombre (de preferencia el rótulo impreso) y lo mismo que al completar",
        "`nombre`: corto y claro" in t and "rótulo impreso" in t and "`estructura`: un ejemplo INVENTADO" in t and "en `nombre`, `descripcion` ni `estructura`" in t)
    esquema_propuesta = llamadas[-1]["cuerpo"]["generationConfig"]["responseSchema"]
    rev("…con su propio esquema JSON, que empieza por el valor y luego el nombre",
        set(esquema_propuesta["properties"]) == {"nombre", "descripcion", "tipoDato", "estructura", "valor"} and esquema_propuesta["propertyOrdering"][:2] == ["valor", "nombre"])
    contestar({"nombre": "Folio ABC12345", "descripcion": "Folio del documento.", "valor": "ABC12345"})
    rev("si el modelo metió el valor en el nombre, se quita", pedir_propuesta(a_admin, org_a).json()["nombre"] == "Folio")
    contestar({"nombre": "Vigencia", "descripcion": "Años de vigencia.", "valor": "2025 - 2035", "estructura": "2024 - 2034"})
    pedir_propuesta(a_admin, org_a, existentes=())
    rev("sin campos existentes, el prompt no habla de ellos", "ya existen" not in texto_de_la_ultima())
    n = len(llamadas)
    for desc, kw in [
        ("un recuadro que no es JSON", {"crudo": {"caja": "no"}}),
        ("un recuadro de 3 números", {"caja": (1, 2, 3)}),
        ("la página 0", {"pagina": 0}),
        ("existentes que no son JSON", {"crudo": {"existentes": "no"}}),
        ("existentes que no son una lista de nombres", {"crudo": {"existentes": json.dumps([1, 2])}}),
        (f"más de {ubicar.MAX_EXISTENTES} existentes", {"existentes": tuple(f"C{i}" for i in range(ubicar.MAX_EXISTENTES + 1))}),
        ("un TIFF", {"archivo": ("a.tif", b"II*\x00datos", "image/tiff")}),
    ]:
        r = pedir_propuesta(a_admin, org_a, **kw)
        rev(f"{desc}: 400 sin llamar a Gemini", r.status_code == 400 and len(llamadas) == n, f"{r.status_code} {r.text[:160]}")
    for desc, texto in [
        ("sin nombre", '{"descripcion": "x"}'),
        ("con el nombre vacío", '{"nombre": " «» ", "descripcion": "x"}'),
        ("con el nombre que solo era el valor", '{"nombre": "1859", "descripcion": "x", "valor": "1859"}'),
        ("sin descripción", '{"nombre": "Sección", "descripcion": ""}'),
        ("que no es JSON", "Sección"),
    ]:
        contestar(texto)
        r = pedir_propuesta(a_admin, org_a)
        rev(f"una respuesta {desc}: 502 con un mensaje que se puede mostrar", r.status_code == 502 and r.json()["detail"] == ubicar.MENSAJE_PROPONER_FALLO and sin_filtrar(r), f"{r.status_code} {r.text[:160]}")
    guion["falla"] = None
    guion["respuesta"] = (429, {"error": {"status": "RESOURCE_EXHAUSTED", "message": "proyecto-de-prueba-123 detalle de Google"}})
    r = pedir_propuesta(a_admin, org_a)
    rev("Gemini sin cuota: 429 sin filtrar nada", r.status_code == 429 and sin_filtrar(r), f"{r.status_code} {r.text[:160]}")
    n = len(llamadas)
    ubicar.DOCAI_PROJECT_ID = ""
    r = pedir_propuesta(a_admin, org_a)
    rev("sin proyecto en el .env: 503 sin llamar", r.status_code == 503 and len(llamadas) == n and r.json()["detail"] == ubicar.MENSAJE_PROPONER_NO_DISPONIBLE, f"{r.status_code} {r.text[:160]}")
    ubicar.DOCAI_PROJECT_ID = "proyecto-de-prueba-123"

    titulo("7 · No guarda nada")
    almacen_despues = sorted(str(p.relative_to(RAIZ)) for p in RAIZ.rglob("*") if p.is_file() and "auditoria" not in p.name)
    rev("el almacén tiene los mismos archivos que antes", almacen_antes == almacen_despues, f"{set(almacen_despues) ^ set(almacen_antes)}")
    con = BASE_SQL["conectar"]("sa", BASE_SQL["sa"], BASE_SQL["base"])
    tipos = con.cursor().execute("SELECT count(*) FROM nexus.documentTypes").fetchone()[0]
    con.close()
    rev("ni un tipo documental en la base", tipos == 0, str(tipos))
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
