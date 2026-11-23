"""¿Los ejemplos etiquetados y la medición de precisión hacen lo que deben? (2026-11-23, migración 0016, `servicios/ejemplos_etiquetados.py`)

    .venv/Scripts/python verificar_ejemplos_etiquetados.py

OFFLINE como los demás `verificar_*`: SQL Server desechable en Docker (`pruebas_sql`), almacén en una carpeta temporal y Document AI sustituido
por un doble. La llamada real se prueba aparte (`--real` en el script de la sesión).

1. La definición del tipo: nombres de Google, rótulos, qué ruta es de un campo vigente.
2. Lo que ve la persona para etiquetar (campos sueltos, grupos, filas, «sin filas») y sus cajas.
3. Comparar y resumir: acierto (sin fijarse en mayúsculas ni espacios), error, faltó, sobró, filas de más, marcadores, campos sin etiqueta.
4. Validar las etiquetas que manda la pantalla.
5. Las rutas: leer (con qué procesador y qué esquema), guardar, listar, quitar, medir e historial; roles, otra organización, un ejemplo que
   apunta al almacén de otra organización, fallas de Google sin filtrar el detalle, y que borrar el tipo se lleva todo.
"""

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-etiquetados-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
import pruebas_sql  # noqa: E402

BASE_SQL = pruebas_sql.preparar()

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from db import tablas  # noqa: E402
from errores import ErrorDocumentAI  # noqa: E402
from servicios import almacen, auth, ia, ubicador_docai, usuarios  # noqa: E402
from servicios import ejemplos_etiquetados as et  # noqa: E402

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + str(extra)[:400] if extra and not ok else ''}")
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


# ── Un tipo de prueba ──
CAMPOS = [
    {"nombre": "Número pasaporte", "tipoDato": "texto", "descripcion": "Una letra y 8 dígitos", "obligatorio": True, "cardinalidad": "unico"},
    {"nombre": "CURP", "tipoDato": "texto", "descripcion": "", "obligatorio": True, "cardinalidad": "unico"},
    {"nombre": "Fecha nacimiento", "tipoDato": "fecha", "descripcion": "", "obligatorio": False, "cardinalidad": "unico"},
    {"nombre": "Teléfonos", "tipoDato": "texto", "descripcion": "", "obligatorio": False, "cardinalidad": "multiple"},
    {"nombre": "Socios", "tipoDato": "grupo", "descripcion": "", "obligatorio": False, "cardinalidad": "multiple",
     "subcampos": [{"nombre": "Nombre", "tipoDato": "texto", "descripcion": "", "obligatorio": True}, {"nombre": "Acciones", "tipoDato": "numero", "descripcion": "", "obligatorio": False}]},
    {"nombre": "Domicilio", "tipoDato": "grupo", "descripcion": "", "obligatorio": False, "cardinalidad": "unico",
     "subcampos": [{"nombre": "Calle", "tipoDato": "texto", "descripcion": "", "obligatorio": False}]},
]


def campo(valor: str, y: float = 0.5, pagina: int = 1) -> dict:
    """Un campo hoja como lo deja `ia._campo`."""
    return {"value_raw": valor, "value_normalized": valor, "confianza": 98.5, "confianza_cruda": 0.985, "metodo_confianza": "extractor_confidence",
            "page_number": pagina, "bloque_indice": None, "posicion": {"x": 0.1, "y": y, "ancho": 0.3, "alto": 0.02}}


def resultado(**campos) -> dict:
    return {**campos, "confianza_promedio": 98.5, "confianza_minima": 98.5, "ocr": {}, "_metadata": {}}


try:
    titulo("1 · La definición del tipo")
    esquema, info = et.definicion({"nombre": "Pasaporte", "descripcion": "Pasaporte mexicano", "campos": CAMPOS + [{"nombre": "  "}, "basura"]})
    rev("los campos van por su nombre de Google, en el orden del tipo",
        list(info) == ["numero_pasaporte", "curp", "fecha_nacimiento", "telefonos", "socios", "domicilio"], str(list(info)))
    rev("con su rótulo y su tipo de dato", info["numero_pasaporte"]["etiqueta"] == "Número pasaporte" and info["fecha_nacimiento"]["tipoDato"] == "fecha")
    rev("un grupo trae sus subcampos por nombre de Google", list(info["socios"]["hijos"]) == ["nombre", "acciones"] and info["socios"]["multiple"] and info["socios"]["grupo"])
    rev("el esquema es el mismo que se activaría (con descripciones)", esquema["entityTypes"][0]["properties"][0]["description"] == "Una letra y 8 dígitos")
    rev("rótulos legibles", et.etiqueta_de("socios[2].nombre", info) == "Socios 2 · Nombre" and et.etiqueta_de("telefonos[1]", info) == "Teléfonos 1"
        and et.etiqueta_de("domicilio.calle", info) == "Domicilio · Calle" and et.etiqueta_de("curp", info) == "CURP")
    rev("el campo de una ruta es la ruta sin índices", et.campo_de("socios[2].nombre") == "socios.nombre" and et.campo_de("telefonos[3]") == "telefonos")
    vigentes = {r: et.vigente(r, info) for r in ["curp", "socios[1].nombre", "socios", "telefonos[2]", "telefonos", "domicilio.calle",
                                                 "curp[1]", "socios.nombre", "socios[1].edad", "domicilio[1].calle", "telefonos.x", "rfc", "CURP", "curp; drop"]}
    rev("vigente: solo las rutas con la forma que el tipo tiene hoy",
        [r for r, v in vigentes.items() if v] == ["curp", "socios[1].nombre", "socios", "telefonos[2]", "telefonos", "domicilio.calle"], str(vigentes))

    titulo("2 · Lo que ve la persona para etiquetar")
    leido = resultado(
        numero_pasaporte=campo("G48213977", 0.2), curp=campo("RASL910314MJCMTC07", 0.4),
        telefonos=[campo("555 1"), campo("555 2")],
        domicilio={"calle": campo("Reforma 10")},
        socios=[{"nombre": campo("ANA"), "acciones": campo("10")}, {"nombre": campo("LUIS")}],
    )
    lecturas = et.leidos(leido)
    rev("lo leído, por la misma ruta que los datos extraídos", sorted(l["ruta"] for l in lecturas) == sorted(
        ["numero_pasaporte", "curp", "telefonos[1]", "telefonos[2]", "domicilio.calle", "socios[1].nombre", "socios[1].acciones", "socios[2].nombre"]),
        str([l["ruta"] for l in lecturas]))
    rev("cada uno con su caja en 0-1000", next(l for l in lecturas if l["ruta"] == "curp")["caja"] == [400, 100, 420, 400])
    revisar = et.para_revisar(info, lecturas)
    rutas = [r["ruta"] for r in revisar]
    rev("en el orden del tipo, con lo no leído vacío y cada fila con todos sus subcampos",
        rutas == ["numero_pasaporte", "curp", "fecha_nacimiento", "telefonos[1]", "telefonos[2]", "socios[1].nombre", "socios[1].acciones",
                  "socios[2].nombre", "socios[2].acciones", "domicilio.calle"], str(rutas))
    rev("lo que no apareció viene con valor vacío", next(r for r in revisar if r["ruta"] == "fecha_nacimiento")["valor"] is None
        and next(r for r in revisar if r["ruta"] == "socios[2].acciones")["valor"] is None)
    rev("cada renglón dice su rótulo, su campo, su grupo y su fila", next(r for r in revisar if r["ruta"] == "socios[2].nombre") | {} == {
        "ruta": "socios[2].nombre", "campo": "socios.nombre", "etiqueta": "Socios 2 · Nombre", "grupo": "socios", "fila": 2, "tipoDato": "texto",
        "valor": "LUIS", "confianza": 98.5, "pagina": 1, "caja": [500, 100, 520, 400]}, str(next(r for r in revisar if r["ruta"] == "socios[2].nombre")))
    sin = et.para_revisar(info, [l for l in lecturas if not l["ruta"].startswith(("telefonos", "socios"))])
    rev("lo que se repite sin ninguna fila sale una vez, marcado «sin filas»",
        [r["ruta"] for r in sin if r.get("sinFilas")] == ["telefonos", "socios"], str([r["ruta"] for r in sin]))

    titulo("3 · Comparar y resumir")
    etiquetas = [
        {"ruta": "numero_pasaporte", "valor": "g48213977 "},          # mayúsculas y espacios: acierto
        {"ruta": "curp", "valor": "RASL910314MJCMTC0?"},              # leyó otra cosa: error
        {"ruta": "fecha_nacimiento", "valor": "1991-03-14"},          # no se leyó: faltó
        {"ruta": "domicilio.calle", "valor": None},                   # no está, pero se leyó: sobró
        {"ruta": "telefonos[1]", "valor": "555 1"},                    # acierto; la fila 2 leída nadie la etiquetó: sobró
        {"ruta": "socios[1].nombre", "valor": "ANA"}, {"ruta": "socios[1].acciones", "valor": "10"},
        {"ruta": "rfc", "valor": "XAXX010101000"},                    # ya no es campo del tipo: no cuenta
    ]
    comps = et.comparar(etiquetas, lecturas, info)
    por = {c["ruta"]: c["resultado"] for c in comps}
    rev("acierto sin fijarse en mayúsculas ni en espacios de más", por.get("numero_pasaporte") == "acierto")
    rev("una letra distinta es error", por.get("curp") == "error" and next(c for c in comps if c["ruta"] == "curp")["leido"] == "RASL910314MJCMTC07")
    rev("lo esperado que no se leyó: faltó", por.get("fecha_nacimiento") == "falto")
    rev("lo que no está pero se leyó: sobró", por.get("domicilio.calle") == "sobro")
    rev("una fila leída que nadie etiquetó: sobró (cada subcampo)", por.get("telefonos[2]") == "sobro" and por.get("socios[2].nombre") == "sobro", str(por))
    rev("lo que ya no es campo del tipo no cuenta", "rfc" not in por)
    sin_etiqueta = et.comparar([{"ruta": "curp", "valor": "RASL910314MJCMTC07"}], lecturas, info)
    rev("un campo sin etiqueta en el documento no cuenta (ni sus filas)", [c["ruta"] for c in sin_etiqueta] == ["curp"], str([c["ruta"] for c in sin_etiqueta]))
    marcadores = et.comparar([{"ruta": "telefonos", "valor": None}, {"ruta": "socios", "valor": None}],
                             [l for l in lecturas if not l["ruta"].startswith("socios")], info)
    rev("«no trae filas»: acierto si no se leyó ninguna; cada fila leída, sobró",
        sorted((c["ruta"], c["resultado"]) for c in marcadores) == [("socios", "acierto"), ("telefonos[1]", "sobro"), ("telefonos[2]", "sobro")], str(marcadores))
    resumen = et.resumir([{"comparaciones": comps}], info)
    rev("el resumen va por campo, en el orden del tipo",
        [r["campo"] for r in resumen] == ["numero_pasaporte", "curp", "fecha_nacimiento", "telefonos", "socios.nombre", "socios.acciones", "domicilio.calle"],
        str([r["campo"] for r in resumen]))
    tel = next(r for r in resumen if r["campo"] == "telefonos")
    rev("con sus cuentas", tel == {"campo": "telefonos", "etiqueta": "Teléfonos", "aciertos": 1, "errores": 0, "faltaron": 0, "sobraron": 1, "total": 2}, str(tel))

    titulo("4 · Validar las etiquetas")
    def invalido(campos) -> str:
        try:
            et.validar_campos(campos, info)
        except et.Invalido as exc:
            return str(exc)
        return ""
    rev("sin etiquetas: no", invalido([]) != "" and invalido("x") != "")
    rev("una ruta que no es campo del tipo: no, y lo dice", "no es un campo" in invalido([{"ruta": "rfc", "valor": "x"}]))
    rev("repetida: no", "dos veces" in invalido([{"ruta": "curp", "valor": "a"}, {"ruta": "curp", "valor": "b"}]))
    rev("un valor que no es texto o es larguísimo: no", invalido([{"ruta": "curp", "valor": 5}]) != "" and invalido([{"ruta": "curp", "valor": "x" * 2001}]) != "")
    rev("algo que se repite no se etiqueta con un solo valor", "por filas" in invalido([{"ruta": "socios", "valor": "ANA"}]))
    rev("caja y página imposibles: no", invalido([{"ruta": "curp", "valor": "a", "caja": [5, 5, 1, 9]}]) != ""
        and invalido([{"ruta": "curp", "valor": "a", "pagina": 0}]) != "" and invalido([{"ruta": "curp", "valor": "a", "origen": "inventado"}]) != "")
    limpias = et.validar_campos([{"ruta": "curp", "valor": "  "}, {"ruta": "numero_pasaporte", "valor": " G1 ", "caja": [1.4, 2, 30, 40.6], "pagina": 1}], info)
    rev("un valor en blanco es «no aparece» y el origen se deduce", limpias[0] == {"ruta": "curp", "valor": None, "pagina": None, "caja": None, "origen": "ausente"})
    rev("el valor se limpia y la caja se redondea", limpias[1] == {"ruta": "numero_pasaporte", "valor": "G1", "pagina": 1, "caja": [1, 2, 30, 41], "origen": "confirmado"}, str(limpias[1]))

    titulo("5 · Montaje de las rutas")
    c = TestClient(app)

    def cab(bearer: str | None = None) -> dict:
        return {"x-api-key": SERVICIO, **({"Authorization": f"Bearer {bearer}"} if bearer else {})}

    def entrar(correo: str, contrasena: str) -> str:
        r = c.post("/auth/login", json={"email": correo, "password": contrasena, "ip": "127.0.0.1", "userAgent": "verificar"}, headers=cab())
        assert r.status_code == 200, r.text
        return r.json()["accessToken"]

    def con_primer_acceso(correo: str, temporal: str, definitiva: str) -> str:
        r = c.post("/auth/contrasena", json={"nueva": definitiva, "confirmacion": definitiva}, headers=cab(entrar(correo, temporal)))
        assert r.status_code == 200, r.text
        return entrar(correo, definitiva)

    def crear_organizacion(bearer_super: str, nombre: str, correo: str, contrasena: str) -> tuple[str, str]:
        r = c.post("/organizaciones/", json={"nombre": nombre, "adminNombre": "Admin", "adminApellidoPaterno": nombre, "adminEmail": correo}, headers=cab(bearer_super))
        assert r.status_code == 201, r.text
        return r.json()["organizacion"]["guid"], con_primer_acceso(correo, r.json()["contrasenaTemporal"], contrasena)

    def sumar_persona(bearer_admin: str, correo: str, rol: str, contrasena: str) -> str:
        r = c.post("/usuarios/", json={"nombre": rol.title(), "apellidoPaterno": "Prueba", "email": correo, "rol": rol}, headers=cab(bearer_admin))
        assert r.status_code == 201, r.text
        return con_primer_acceso(correo, r.json()["contrasenaTemporal"], contrasena)

    temporal_super = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal_super))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal_super, "Cobre-Lento-Azul-47!")
    org_a, a_admin = crear_organizacion(bearer_super, "Alfa", "rita@ejemplo.com", "Rumbo-Sereno-Azul-47!")
    org_b, b_admin = crear_organizacion(bearer_super, "Beta", "beto@ejemplo.com", "Rumbo-Sereno-Rojo-48!")
    a_operador = sumar_persona(a_admin, "olga@ejemplo.com", "OPERADOR", "Cielo-Abierto-Verde-53!")

    PNG1 = b"\x89PNG\r\n\x1a\n" + b"pasaporte uno"
    PNG2 = b"\x89PNG\r\n\x1a\n" + b"pasaporte dos"
    ruta1 = almacen.guardar(org_a, PNG1)["rutaRelativa"]
    ruta2 = almacen.guardar(org_a, PNG2)["rutaRelativa"]
    ruta_ajena = almacen.guardar(org_b, b"\x89PNG\r\n\x1a\nde otra organizacion")["rutaRelativa"]
    EJEMPLOS = [
        {"id": "ej-uno", "nombre": "uno.png", "tipo": "PNG", "tamanoBytes": len(PNG1), "rutaRelativa": ruta1, "mime": "image/png"},
        {"id": "ej-dos", "nombre": "dos.png", "tipo": "PNG", "tamanoBytes": len(PNG2), "rutaRelativa": ruta2},  # sin mime: sale de `tipo`
        {"id": "ej-ajeno", "nombre": "ajeno.png", "tipo": "PNG", "tamanoBytes": 10, "rutaRelativa": ruta_ajena, "mime": "image/png"},
    ]
    TIPO = "tipo-prueba-etiquetado-01"
    datos_tipo = {"nombre": "Pasaporte", "descripcion": "Pasaporte mexicano", "estado": "borrador", "procesadorId": "", "campos": CAMPOS, "documentosEjemplo": EJEMPLOS}

    def guardar_tipo(datos: dict, revision: int) -> int:
        r = c.put(f"/tipos-documentales/{TIPO}", json={"tenant": org_a, "revision": revision, "datos": datos}, headers=cab(a_admin))
        assert r.status_code == 200, r.text
        return r.json()["revision"]

    revision = guardar_tipo(datos_tipo, 0)

    # ── El doble de Document AI: contesta según el documento (por sus bytes) y anota cada llamada ──
    llamadas: list[dict] = []
    guion: dict = {}

    async def extraer_falso(procesador, contenido, mime, version="", transformar=None, permitir_bloques=True, esquema=None, documentos_crudos=None):
        llamadas.append({"procesador": procesador, "version": version, "mime": mime, "esquema": esquema, "contenido": contenido})
        respuesta = guion.get(contenido)
        if isinstance(respuesta, Exception):
            raise respuesta
        if documentos_crudos is not None:
            documentos_crudos.append({"text": "texto de prueba", "entities": []})
        return respuesta if respuesta is not None else resultado()

    async def ubicador_falso():
        return "ubicador-falso", "version-del-ubicador"

    ia.extraer_con_procesador = extraer_falso
    ubicador_docai.id_del_ubicador = ubicador_falso

    def ruta_leer(ejemplo: str) -> str:
        return f"/tipos-documentales/{TIPO}/ejemplos/{ejemplo}/leer"

    def ruta_etiquetas(ejemplo: str) -> str:
        return f"/tipos-documentales/{TIPO}/ejemplos/{ejemplo}/etiquetas"

    titulo("6 · Leer un ejemplo")
    guion[PNG1] = leido
    r = c.post(ruta_leer("ej-uno"), json={"tenant": org_a}, headers=cab(a_admin))
    cuerpo = r.json() if r.status_code == 200 else {}
    rev("200 con lo que se leyó, listo para revisar", r.status_code == 200 and [x["ruta"] for x in cuerpo.get("campos", [])] == rutas, r.text[:300])
    rev("un tipo en borrador se lee con el extractor compartido y su versión fijada",
        llamadas[-1]["procesador"] == "ubicador-falso" and llamadas[-1]["version"] == "version-del-ubicador" and cuerpo.get("procesador") == "compartido")
    rev("…con el esquema de la definición ACTUAL del tipo", llamadas[-1]["esquema"] == et.definicion(datos_tipo)[0])
    rev("…y el archivo del ejemplo con su tipo", llamadas[-1]["contenido"] == PNG1 and llamadas[-1]["mime"] == "image/png")
    base = cuerpo.get("base") or ""
    rev("lo que devolvió Google queda en el almacén de la organización", base.startswith(f"{org_a}/") and json.loads(almacen.leer(base))
        == {"procesador": "ubicador-falso", "version": "version-del-ubicador", "documentos": [{"text": "texto de prueba", "entities": []}]}, base)
    rev("dice la revisión del tipo que se leyó", cuerpo.get("revision") == revision)
    c.post(ruta_leer("ej-dos"), json={"tenant": org_a}, headers=cab(a_admin))
    rev("un ejemplo sin mime lo toma de su tipo de archivo", llamadas[-1]["mime"] == "image/png" and llamadas[-1]["contenido"] == PNG2)
    n = len(llamadas)
    r = c.post(ruta_leer("ej-ajeno"), json={"tenant": org_a}, headers=cab(a_admin))
    rev("un ejemplo que apunta al almacén de OTRA organización: 404 sin leer nada", r.status_code == 404 and len(llamadas) == n and "otra" not in r.text.lower(), f"{r.status_code} {r.text}")
    r = c.post(ruta_leer("ej-no-existe"), json={"tenant": org_a}, headers=cab(a_admin))
    rev("un ejemplo que el tipo no tiene: 404", r.status_code == 404, f"{r.status_code}")
    r = c.post(ruta_leer("../x"), json={"tenant": org_a}, headers=cab(a_admin))
    rev("un id de ejemplo raro: 400 o 404, sin leer", r.status_code in (400, 404) and len(llamadas) == n, f"{r.status_code}")
    r = c.post(ruta_leer("ej-uno"), json={"tenant": org_a}, headers=cab(a_operador))
    rev("un operador no puede leer (cuesta): 403", r.status_code == 403 and len(llamadas) == n, f"{r.status_code}")
    r = c.post(ruta_leer("ej-uno"), json={"tenant": org_a}, headers=cab(b_admin))
    rev("el administrador de otra organización tampoco", r.status_code in (401, 403) and len(llamadas) == n, f"{r.status_code}")
    r = c.post(ruta_leer("ej-uno"), json={"tenant": org_b}, headers=cab(b_admin))
    rev("…ni pidiéndolo como suyo: el tipo no existe en su organización", r.status_code == 404 and len(llamadas) == n, f"{r.status_code}")
    guion[PNG1] = ErrorDocumentAI("Document AI respondió 500: projects/123/processors/abc DETALLE-INTERNO", status_code=500, motivo="servicio")
    r = c.post(ruta_leer("ej-uno"), json={"tenant": org_a}, headers=cab(a_admin))
    rev("si Google falla: 502 con un mensaje para la persona, sin el detalle de Google", r.status_code == 502 and "DETALLE" not in r.text and "projects" not in r.text, f"{r.status_code} {r.text}")
    guion[PNG1] = leido

    titulo("7 · Guardar y listar etiquetas")
    etiquetas_uno = [
        {"ruta": "numero_pasaporte", "valor": "G48213977", "pagina": 1, "caja": [200, 100, 220, 400], "origen": "confirmado"},
        {"ruta": "curp", "valor": "RASL910314MJCMTC07", "origen": "confirmado"},
        {"ruta": "fecha_nacimiento", "valor": None, "origen": "ausente"},
        {"ruta": "telefonos[1]", "valor": "555 1"}, {"ruta": "telefonos[2]", "valor": "555 2"},
        {"ruta": "socios[1].nombre", "valor": "ANA"}, {"ruta": "socios[1].acciones", "valor": "10"},
        {"ruta": "socios[2].nombre", "valor": "LUIS"}, {"ruta": "socios[2].acciones", "valor": None},
        {"ruta": "domicilio.calle", "valor": "Reforma 10"},
    ]
    r = c.put(ruta_etiquetas("ej-uno"), json={"tenant": org_a, "campos": etiquetas_uno, "base": base}, headers=cab(a_admin))
    rev("guardar: 200 con lo guardado", r.status_code == 200 and r.json()["ejemplo"] == "ej-uno" and len(r.json()["campos"]) == 10 and r.json()["conBase"], r.text[:300])
    with tablas.lectura() as cur:
        cur.execute("SELECT sha256, relativePath, mime, originalName, documentTypeRevision, baseDocumentPath, baseProcessorId, baseProcessorVersion FROM nexus.exampleLabels WHERE exampleId = 'ej-uno'")
        fila = tablas.una(cur)
    rev("se guarda la foto del ejemplo, la revisión del tipo y la base con su procesador",
        fila["sha256"] == hashlib.sha256(PNG1).hexdigest() and fila["relativePath"] == ruta1 and fila["mime"] == "image/png" and fila["originalName"] == "uno.png"
        and fila["documentTypeRevision"] == revision and fila["baseDocumentPath"] == base and fila["baseProcessorId"] == "ubicador-falso"
        and fila["baseProcessorVersion"] == "version-del-ubicador", str(fila))
    r = c.put(ruta_etiquetas("ej-uno"), json={"tenant": org_a, "campos": [{"ruta": "rfc", "valor": "x"}]}, headers=cab(a_admin))
    rev("una ruta que no es del tipo: 400 con un mensaje claro", r.status_code == 400 and "no es un campo" in r.text, f"{r.status_code} {r.text}")
    lectura_ajena = almacen.guardar(org_b, json.dumps({"procesador": "p", "version": None, "documentos": [{"text": "de Beta"}]}).encode())["rutaRelativa"]
    r = c.put(ruta_etiquetas("ej-uno"), json={"tenant": org_a, "campos": etiquetas_uno, "base": lectura_ajena}, headers=cab(a_admin))
    rev("una lectura (base) del almacén de otra organización: 400", r.status_code == 400, f"{r.status_code}")
    r = c.put(ruta_etiquetas("ej-uno"), json={"tenant": org_a, "campos": etiquetas_uno, "base": ruta1}, headers=cab(a_admin))
    rev("una base que no es una lectura (es el documento): 400", r.status_code == 400, f"{r.status_code}")
    r = c.put(ruta_etiquetas("ej-uno"), json={"tenant": org_a, "campos": etiquetas_uno}, headers=cab(a_operador))
    rev("un operador no puede etiquetar: 403", r.status_code == 403)
    r = c.put(ruta_etiquetas("ej-uno"), json={"tenant": org_a, "campos": etiquetas_uno[:2]}, headers=cab(a_admin))
    rev("volver a guardar reemplaza las etiquetas y, sin base nueva, conserva la de antes", r.status_code == 200 and len(r.json()["campos"]) == 2 and r.json()["conBase"], r.text[:200])
    c.put(ruta_etiquetas("ej-uno"), json={"tenant": org_a, "campos": etiquetas_uno}, headers=cab(a_admin))
    r = c.put(ruta_etiquetas("ej-dos"), json={"tenant": org_a, "campos": [{"ruta": "numero_pasaporte", "valor": "G75104286"}, {"ruta": "curp", "valor": "HEBJ850827HNLRLR04"}]}, headers=cab(a_admin))
    rev("otro documento, sin base (no se leyó en esta sesión): se puede", r.status_code == 200 and not r.json()["conBase"], r.text[:200])
    rev("…y dice qué campos del tipo quedaron sin etiqueta en él",
        r.status_code == 200 and r.json()["camposSinEtiqueta"] == ["Fecha nacimiento", "Teléfonos", "Socios", "Domicilio"], str(r.json().get("camposSinEtiqueta")))
    rev("cada etiqueta guardada trae su rótulo, su campo, su tipo de dato y si sigue vigente",
        r.status_code == 200 and r.json()["campos"][0] | {} == {"ruta": "numero_pasaporte", "valor": "G75104286", "pagina": None, "caja": None, "origen": "confirmado",
                                                              "campo": "numero_pasaporte", "etiqueta": "Número pasaporte", "grupo": "numero_pasaporte",
                                                              "tipoDato": "texto", "vigente": True}, str(r.json()["campos"][0]))
    r = c.get(f"/tipos-documentales/{TIPO}/etiquetas", params={"tenant": org_a}, headers=cab(a_operador))
    rev("listar: cualquiera que opera en la organización, en el orden en que se etiquetaron",
        r.status_code == 200 and [e["ejemplo"] for e in r.json()["etiquetas"]] == ["ej-uno", "ej-dos"], r.text[:200])
    r = c.get(f"/tipos-documentales/{TIPO}/etiquetas", params={"tenant": org_b}, headers=cab(b_admin))
    rev("otra organización no ve el tipo: 404", r.status_code == 404, f"{r.status_code}")

    titulo("8 · Medir")
    # El documento uno se lee con la CURP con una O y sin el teléfono 2; el dos, igual que su etiqueta.
    guion[PNG1] = resultado(
        numero_pasaporte=campo("G48213977"), curp=campo("RASL910314MJCMTC0O"), fecha_nacimiento=campo("1991-03-14"),
        telefonos=[campo("555 1")], domicilio={"calle": campo("Reforma 10")},
        socios=[{"nombre": campo("ANA"), "acciones": campo("10")}, {"nombre": campo("LUIS")}],
    )
    guion[PNG2] = resultado(numero_pasaporte=campo("G75104286"), curp=campo("HEBJ850827HNLRLR04"))
    n = len(llamadas)
    r = c.post(f"/tipos-documentales/{TIPO}/mediciones", json={"tenant": org_a}, headers=cab(a_admin))
    m = r.json() if r.status_code == 200 else {}
    rev("medir: 200 y se leyeron los dos documentos etiquetados con la definición actual",
        r.status_code == 200 and len(llamadas) == n + 2 and all(x["esquema"] == et.definicion(datos_tipo)[0] for x in llamadas[n:]), r.text[:300])
    rev("cuentas: 12 comparaciones, 9 aciertos, completa", (m.get("comparaciones"), m.get("aciertos"), m.get("estado"), m.get("documentosMedidos")) == (12, 9, "completa", 2),
        str({k: m.get(k) for k in ("comparaciones", "aciertos", "estado", "documentosMedidos")}))
    campos_m = {x["campo"]: x for x in m.get("detalle", {}).get("campos", [])}
    rev("por campo: la CURP 1 de 2 con un error", (campos_m.get("curp") or {}).get("aciertos") == 1 and campos_m["curp"]["errores"] == 1)
    rev("la fecha que no estaba y se leyó: sobró; el teléfono 2 que no se leyó: faltó",
        campos_m.get("fecha_nacimiento", {}).get("sobraron") == 1 and campos_m.get("telefonos", {}).get("faltaron") == 1, str(campos_m.get("telefonos")))
    doc_uno = next(d for d in m["detalle"]["documentos"] if d["ejemplo"] == "ej-uno")
    curp = next(x for x in doc_uno["comparaciones"] if x["ruta"] == "curp")
    rev("el detalle dice qué esperaba y qué leyó, y el nombre del archivo",
        curp["esperado"] == "RASL910314MJCMTC07" and curp["leido"] == "RASL910314MJCMTC0O" and doc_uno["nombre"] == "uno.png", str(curp))
    rev("un tipo en borrador se mide con el compartido", m.get("procesador") == "compartido" and m.get("version") == "version-del-ubicador")

    # Activo: se mide con SU procesador y su versión.
    datos_tipo = {**datos_tipo, "estado": "activo", "procesadorId": "procesador-del-tipo", "procesadorVersion": "version-del-tipo"}
    revision = guardar_tipo(datos_tipo, revision)
    guion[PNG2] = ErrorDocumentAI("Document AI respondió 429: projects/123 CUOTA-INTERNA", status_code=429, motivo="cuota")
    n = len(llamadas)
    r = c.post(f"/tipos-documentales/{TIPO}/mediciones", json={"tenant": org_a}, headers=cab(a_admin))
    m2 = r.json() if r.status_code == 200 else {}
    rev("un tipo activo se mide con SU procesador y su versión fijada",
        all(x["procesador"] == "procesador-del-tipo" and x["version"] == "version-del-tipo" for x in llamadas[n:]) and m2.get("procesador") == "tipo", r.text[:200])
    doc_dos = next((d for d in m2.get("detalle", {}).get("documentos", []) if d["ejemplo"] == "ej-dos"), {})
    rev("un documento que Google no pudo leer: parcial, con un mensaje para la persona y sin filtrar el detalle",
        m2.get("estado") == "parcial" and m2.get("documentosMedidos") == 1 and doc_dos.get("estado") == "error" and "CUOTA" not in r.text and "projects" not in r.text
        and "ocupado" in doc_dos.get("mensaje", ""), str(doc_dos))
    rev("…y las cuentas son solo de lo que sí se leyó", m2.get("comparaciones") == 10 and m2.get("revision") == revision, str(m2.get("comparaciones")))
    r = c.post(f"/tipos-documentales/{TIPO}/mediciones", json={"tenant": org_a}, headers=cab(a_operador))
    rev("un operador no puede medir: 403", r.status_code == 403)
    r = c.get(f"/tipos-documentales/{TIPO}/mediciones", params={"tenant": org_a}, headers=cab(a_operador))
    h = r.json().get("mediciones", []) if r.status_code == 200 else []
    rev("el historial: la más reciente primero, solo ella con detalle", r.status_code == 200 and [x["id"] for x in h] == [m2.get("id"), m.get("id")]
        and "detalle" in h[0] and "detalle" not in h[1], r.text[:200])
    r = c.get(f"/tipos-documentales/{TIPO}/mediciones", params={"tenant": org_a, "limite": 1}, headers=cab(a_admin))
    rev("con límite", r.status_code == 200 and len(r.json()["mediciones"]) == 1)

    titulo("9 · Quitar")
    datos_tipo = {**datos_tipo, "documentosEjemplo": [e for e in EJEMPLOS if e["id"] != "ej-dos"]}
    revision = guardar_tipo(datos_tipo, revision)
    r = c.get(f"/tipos-documentales/{TIPO}/etiquetas", params={"tenant": org_a}, headers=cab(a_admin))
    rev("las etiquetas de un ejemplo que el tipo ya no tiene no se listan", [e["ejemplo"] for e in r.json()["etiquetas"]] == ["ej-uno"], r.text[:200])
    r = c.delete(ruta_etiquetas("ej-uno"), params={"tenant": org_a}, headers=cab(a_admin))
    rev("quitar: 200 y dice que había", r.status_code == 200 and r.json() == {"quitadas": True}, r.text)
    r = c.delete(ruta_etiquetas("ej-uno"), params={"tenant": org_a}, headers=cab(a_admin))
    rev("quitar otra vez: no había", r.status_code == 200 and r.json() == {"quitadas": False})
    r = c.post(f"/tipos-documentales/{TIPO}/mediciones", json={"tenant": org_a}, headers=cab(a_admin))
    rev("medir sin documentos etiquetados: 400 que lo explica", r.status_code == 400 and "etiqueta" in r.text, f"{r.status_code} {r.text}")
    c.put(ruta_etiquetas("ej-uno"), json={"tenant": org_a, "campos": etiquetas_uno}, headers=cab(a_admin))
    r = c.delete(f"/tipos-documentales/{TIPO}", params={"tenant": org_a}, headers=cab(a_admin))
    with tablas.lectura() as cur:
        cur.execute("SELECT (SELECT count(*) FROM nexus.exampleLabels) AS e, (SELECT count(*) FROM nexus.extractionMeasurements) AS m")
        cuentas = tablas.una(cur)
    rev("borrar el tipo se lleva sus etiquetas y sus mediciones", r.status_code == 200 and cuentas == {"e": 0, "m": 0}, f"{r.status_code} {cuentas}")
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
