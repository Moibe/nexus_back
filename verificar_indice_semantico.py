"""¿El índice y la búsqueda en el texto de los documentos hacen lo que piden HU-094 a HU-099 (épica 14, migración 0022)?

    .venv/Scripts/python verificar_indice_semantico.py

Corre contra el SQL Server desechable de `pruebas_sql.py` (sin Docker se salta y lo dice), con todas las migraciones y el login de solo lectura que
hizo el DBA (aquí lo crea la prueba). Google se sustituye por un doble: los vectores salen de las palabras del texto (dos textos que comparten
palabras se parecen), y se puede hacer fallar a voluntad. El trabajador está apagado: la prueba indexa a mano con `procesar_vencidos`.

1. la capa OCR: cada extracción guarda sus líneas (texto, página, caja) y cada dato extraído queda ligado a su línea; una fallida no guarda nada;
2. indexar (HU-094): la extracción deja el documento INDEX_PENDING; los fragmentos de texto no cruzan de página y se enciman; hay uno de campos por
   página con los nombres del tipo documental; el documento queda INDEXED con su cuenta; los vectores se piden como RETRIEVAL_DOCUMENT;
3. corregir un dato (HU-094 CA-3): solo se vuelve a pedir el fragmento de campos de esa página y el vector anterior desaparece;
4. Google no responde (HU-094 CA-2): INDEX_PENDING con reintento programado; al tercer fallo ya no se programa y queda SEMANTIC_INDEX_ALERT; una
   extracción nueva vuelve a empezar la cuenta; la extracción misma nunca falla por el índice;
5. re-extraer (HU-099 CA-1): los fragmentos viejos dejan de salir EN EL ACTO (antes de reindexar); al reindexar se reemplazan en una transacción y
   SEMANTIC_REINDEXED lleva las dos extracciones; lo que no cambió no se vuelve a pedir;
6. borrar un documento (HU-099 CA-2) se lleva sus fragmentos y su estado;
7. el aislamiento (HU-095): sin organización la vista no devuelve nada; con Beta fijada no ve lo de Alfa; el usuario de lectura no lee [nexus];
   la búsqueda de Beta nunca trae un documento de Alfa; con la organización de otro en el cuerpo, 403 y CROSS_TENANT_ACCESS;
8. buscar (HU-096 a HU-098): los más parecidos primero, con score, pasaje resaltado, página, tipo, fecha, expediente y datos de la página; el
   mínimo de la organización (0.65 por omisión) y el mensaje sin resultados; filtros de tipo y fechas; orden por fecha; paginar sin volver a pedir
   el vector; validaciones (400); SEMANTIC_SEARCH en la bitácora; el estado del índice;
9. el barrido recoge lo que se quedó sin programar (documentos extraídos antes de la 0022) y lo atascado;
10. los CHECK de las tablas nuevas muerden.
"""

import asyncio
import hashlib
import json
import math
import os
import re
import secrets
import sys
import tempfile
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-indice-semantico-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["CORREO_MODO"] = "silencio"
os.environ["DOCAI_PROJECT_ID"] = "proyecto-de-prueba"
os.environ["INDICE_SEMANTICO"] = "off"
os.environ["ENTREGAS_WEBHOOKS"] = "off"

import verificacion_comun  # noqa: E402,F401  (fija AUTH_JWT_SECRET)
import pruebas_sql  # noqa: E402

BASE = pruebas_sql.preparar("nexus-indice-semantico", 14348)

DB = BASE["base"]
LECTURA = "usrNexusLectura"
CLAVE = "Lec-" + secrets.token_urlsafe(12) + "-9!"
sa = BASE["conectar"]("sa", BASE["sa"], DB)
k = sa.cursor()
m = BASE["conectar"]("sa", BASE["sa"])
m.cursor().execute(f"IF SUSER_ID('{LECTURA}') IS NULL CREATE LOGIN {LECTURA} WITH PASSWORD = '{CLAVE}', CHECK_POLICY = ON ELSE ALTER LOGIN {LECTURA} WITH PASSWORD = '{CLAVE}'")
k.execute(f"CREATE USER {LECTURA} FOR LOGIN {LECTURA}")
k.execute(f"GRANT SELECT ON SCHEMA::consulta TO {LECTURA}")
carpeta = Path(tempfile.mkdtemp(prefix="indice-semantico-lectura-"))
ARCHIVO_LECTURA = carpeta / "lectura.env"
ARCHIVO_LECTURA.write_text(f"SQLSERVER_USER={LECTURA}\nSQLSERVER_PASSWORD='{CLAVE}'\n", encoding="utf-8")
os.environ["NEXUS_LECTURA_ENV"] = str(ARCHIVO_LECTURA)

import pyodbc  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
from app import app  # noqa: E402
from db import consulta, tablas  # noqa: E402
from servicios import auditoria, auth, busqueda_documentos, datos_extraidos, embeddings, fragmentos, indice_semantico, usuarios  # noqa: E402

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(("OK   " if ok else "FALLA") + " " + descripcion + ("" if ok or not extra else f"\n      {extra}"))
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n[{t}]")


# ── El doble de Google ──────────────────────────────────────────────────────────────────────────────────────────────────
LLAMADAS: list[tuple[str, list[str]]] = []
FALLAR = {"n": 0}
DIM = config.EMBEDDINGS_DIMENSIONES


def _plano(texto: str) -> str:
    return "".join(unicodedata.normalize("NFD", c)[0] for c in texto).lower()


def _vector(texto: str) -> list[float]:
    v = [0.0] * DIM
    for palabra in re.findall(r"\w+", _plano(texto)):
        if len(palabra) < 3:
            continue
        v[int(hashlib.sha256(palabra[:5].encode()).hexdigest(), 16) % DIM] += 1.0
    norma = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / norma for x in v]


async def doble(textos, tarea=embeddings.TAREA):
    LLAMADAS.append((tarea, list(textos)))
    if FALLAR["n"] > 0:
        FALLAR["n"] -= 1
        raise embeddings.SinEmbeddings("Google no respondió (doble)")
    return [_vector(t) for t in textos], sum(max(1, len(t) // 4) for t in textos)


embeddings.vectores = doble

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


temporal_super = auth.contrasena_temporal()
usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal_super))
bearer_super = con_primer_acceso("super@ejemplo.com", temporal_super, "Cobre-Lento-Azul-47!")
ORG_A, a_admin = crear_organizacion(bearer_super, "Alfa", "rita@ejemplo.com", "Rumbo-Sereno-Azul-47!")
ORG_B, b_admin = crear_organizacion(bearer_super, "Beta", "beto@ejemplo.com", "Rumbo-Sereno-Rojo-48!")
RITA = k.execute("SELECT guid FROM nexus.users WHERE email = 'rita@ejemplo.com'").fetchone()[0]


def campo(nombre: str, tipo_dato: str = "texto", subcampos: list | None = None) -> dict:
    return {"nombre": nombre, "tipoDato": tipo_dato, "descripcion": "", "obligatorio": False, "cardinalidad": "unico", "valoresLista": [], "subcampos": subcampos or []}


def guardar_tipo(bearer: str, tenant: str, tipo_id: str, nombre: str, campos: list) -> str:
    procesador = "proc-" + tipo_id[-6:]
    datos = {"nombre": nombre, "descripcion": f"{nombre} de prueba.", "estado": "activo", "procesadorId": procesador, "campos": campos}
    r = c.put(f"/tipos-documentales/{tipo_id}", json={"tenant": tenant, "revision": 0, "datos": datos}, headers=cab(bearer))
    assert r.status_code == 200, r.text
    return procesador


T_INE, T_ACTA, T_PAS = "tipo-prueba-ine-01", "tipo-prueba-acta-01", "tipo-prueba-pasaporte-01"
P_INE = guardar_tipo(a_admin, ORG_A, T_INE, "INE", [campo("Nombre"), campo("CURP"), campo("Año de registro")])
P_ACTA = guardar_tipo(a_admin, ORG_A, T_ACTA, "Acta constitutiva", [campo("Razón social"), campo("Socios", "grupo", [campo("Nombre")])])
P_PAS = guardar_tipo(b_admin, ORG_B, T_PAS, "Pasaporte", [campo("Número de pasaporte")])

r = c.post("/expedientes/", json={"tenant": ORG_A, "nombre": "Expediente de Ana"}, headers=cab(a_admin))
assert r.status_code == 201, r.text
E_ANA = r.json().get("id") or r.json()["expediente"]["id"]


def nuevo_documento(tenant: str, archivo: str, caso: str | None = None) -> tuple[str, int]:
    with tablas.transaccion() as cur:
        publico = f"documento-{secrets.token_hex(16)}"
        cur.execute(
            "INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel) OUTPUT INSERTED.documentId "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            publico, tenant, secrets.token_hex(32), f"{tenant}/aa/aa/{secrets.token_hex(32)}", 1234, "application/pdf", archivo, "MANUAL",
        )
        doc = cur.fetchone()[0]
        if caso:
            cur.execute("SELECT caseId FROM nexus.cases WHERE publicId = ?", caso)
            cur.execute("INSERT INTO nexus.caseDocuments (caseId, documentId, addedBy) VALUES (?, ?, ?)", cur.fetchone()[0], doc, RITA)
    return publico, doc


def hoja(valor: str, pagina: int, bloque: int | None) -> dict:
    return {"value_raw": valor, "value_normalized": valor, "confianza": 93.5, "confianza_cruda": 0.935, "metodo_confianza": "extractor_confidence",
            "page_number": pagina, "bloque_indice": bloque}


def capa(paginas: dict[int, list[str]]) -> dict:
    bloques = []
    for pagina in sorted(paginas):
        for i, linea in enumerate(paginas[pagina]):
            bloques.append({"indice": len(bloques), "page_number": pagina, "block_type": "line", "texto": linea,
                            "bbox": {"x": 0.1, "y": round(0.05 + i * 0.03, 6), "ancho": 0.8, "alto": 0.025}, "confianza": 98.0,
                            "orientacion": "PAGE_UP", "orientacion_grados": 0})
    return {"engine": "document_ai", "engine_version": "pretrained-v1", "page_count": len(paginas), "language": "es",
            "paginas": [{"page_number": p, "ancho": 1000, "alto": 1400, "unidad": "pixels", "idioma": "es"} for p in sorted(paginas)], "bloques": bloques}


def extraer(tenant: str, publico: str, tipo: str, procesador: str, datos: dict, paginas: dict | None, error: str | None = None) -> str:
    resultado = None if error else {"confianza_promedio": 90, "confianza_minima": 80, "_metadata": {}, **datos, **({"ocr": capa(paginas)} if paginas else {})}
    return datos_extraidos.registrar_extraccion(tenant, publico, tipo, procesador, "v1", datetime.now(timezone.utc).replace(tzinfo=None), "prueba",
                                                resultado=resultado, error=error)


def indice(doc: int) -> dict:
    fila = k.execute("SELECT status, runId, chunkCount, attempts, nextAttemptAt, alertedAt, lastError FROM nexus.documentIndexes WHERE documentId = ?", doc).fetchone()
    return dict(zip(["status", "runId", "chunkCount", "attempts", "nextAttemptAt", "alertedAt", "lastError"], fila)) if fila else {}


def fragmentos_de(doc: int) -> list[dict]:
    k.execute("SELECT chunkId, runId, kind, pageNumber, chunkIndex, content, tokenCount, DATALENGTH(embedding) AS bytes, embeddingModel FROM nexus.documentChunks "
              "WHERE documentId = ? ORDER BY kind DESC, chunkIndex", doc)
    cols = [d[0] for d in k.description]
    return [dict(zip(cols, f)) for f in k.fetchall()]


def ya_toca(doc: int) -> None:
    """Adelanta el siguiente intento programado (en vez de esperar ESPERAS_S)."""
    k.execute("UPDATE nexus.documentIndexes SET nextAttemptAt = DATEADD(second, -1, SYSUTCDATETIME()) WHERE documentId = ? AND nextAttemptAt IS NOT NULL", doc)
    sa.commit()


def eventos(accion: str) -> list[dict]:
    return [e for e in auditoria.eventos({accion})]


# ── 1. La capa OCR ──────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("1. La capa OCR")
LARGA = [f"Cláusula {i}: la sociedad se obliga a conservar sus libros contables y actas de asamblea durante diez años." for i in range(1, 61)]
D_ACTA, ID_ACTA = nuevo_documento(ORG_A, "acta-constitutiva-dorama.pdf", E_ANA)
datos_acta = {"razon_social": hoja("DORAMA SERVICIOS SA DE CV", 1, 0), "socios": [{"nombre": hoja("ANA LÓPEZ RUIZ", 2, None)}, {"nombre": hoja("LUIS PÉREZ", 2, None)}]}
paginas_acta = {1: ["DORAMA SERVICIOS SA DE CV", "ESCRITURA PÚBLICA NÚMERO 4521", "Ante mí, notario público número 17 de la Ciudad de México"],
                2: ["Comparecen los socios fundadores", "Ana López Ruiz suscribe cincuenta acciones", "Luis Pérez suscribe cincuenta acciones"],
                3: LARGA}
LLAMADAS.clear()
R_ACTA_1 = extraer(ORG_A, D_ACTA, T_ACTA, P_ACTA, datos_acta, paginas_acta)
k.execute("SELECT o.ocrResultId, o.engine, o.engineVersion, o.pageCount, o.language FROM nexus.ocrResults o JOIN nexus.extractionRuns r ON r.runId = o.runId WHERE r.publicId = ?", R_ACTA_1)
ocr = k.fetchone()
rev("la extracción guardó su lectura (motor, versión, páginas, idioma)", ocr is not None and tuple(ocr[1:]) == ("document_ai", "pretrained-v1", 3, "es"), str(ocr))
lineas = k.execute("SELECT COUNT(*), MIN(pageNumber), MAX(pageNumber), MAX(LEN(text)) FROM nexus.ocrBlocks WHERE ocrResultId = ?", ocr[0]).fetchone()
rev("…y cada línea con su página (66 líneas en 3 páginas)", tuple(lineas[:3]) == (66, 1, 3), str(lineas))
k.execute("SELECT f.fieldPath, b.text FROM nexus.entityFacts f JOIN nexus.extractionRuns r ON r.runId = f.runId LEFT JOIN nexus.ocrBlocks b ON b.ocrBlockId = f.ocrBlockId "
          "WHERE r.publicId = ? ORDER BY f.fieldPath", R_ACTA_1)
ligados = dict(k.fetchall())
rev("cada dato extraído queda ligado a su línea (razón social → la línea 0); los que no traen línea, sin liga", ligados.get("razon_social") == "DORAMA SERVICIOS SA DE CV" and ligados.get("socios[1].nombre") is None, str(ligados))
rev("la extracción no llamó a Google para el índice (eso es del trabajador)", LLAMADAS == [], str(LLAMADAS))
D_MAL, ID_MAL = nuevo_documento(ORG_A, "falla.pdf")
extraer(ORG_A, D_MAL, T_INE, P_INE, {}, None, error="Document AI no respondió")
rev("una extracción fallida no guarda lectura ni programa índice",
    k.execute("SELECT COUNT(*) FROM nexus.ocrResults o JOIN nexus.extractionRuns r ON r.runId = o.runId WHERE r.documentId = ?", ID_MAL).fetchone()[0] == 0 and indice(ID_MAL) == {})

# ── 2. Indexar ──────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("2. Indexar (HU-094)")
rev("la extracción dejó el documento INDEX_PENDING para ya", indice(ID_ACTA).get("status") == "INDEX_PENDING" and indice(ID_ACTA).get("nextAttemptAt") is not None, str(indice(ID_ACTA)))
salida = indice_semantico.procesar_vencidos()
rev("el trabajador lo tomó y lo indexó", (ID_ACTA, "indexado") in salida, str(salida))
fr = fragmentos_de(ID_ACTA)
texto_fr = [f for f in fr if f["kind"] == "texto"]
campos_fr = [f for f in fr if f["kind"] == "campos"]
rev("hay fragmentos de texto de las tres páginas, ninguno cruza de página", sorted({f["pageNumber"] for f in texto_fr}) == [1, 2, 3]
    and not any("Cláusula" in f["content"] for f in texto_fr if f["pageNumber"] != 3) and not any("Comparecen" in f["content"] for f in texto_fr if f["pageNumber"] != 2),
    str([(f["pageNumber"], f["tokenCount"]) for f in texto_fr]))
p3 = [f for f in texto_fr if f["pageNumber"] == 3]
rev("la página 3 (larga) se partió en varios de hasta 512 tokens", len(p3) >= 2 and all(f["tokenCount"] <= 512 for f in p3), str([f["tokenCount"] for f in p3]))
encimados = len(p3) >= 2 and " ".join(p3[1]["content"].split()[:6]) in " ".join(p3[0]["content"].split())
rev("…y el segundo empieza con el final del primero (solapamiento)", bool(encimados), p3[1]["content"][:120] if len(p3) > 1 else "")
rev("ningún fragmento de la página 1 trae texto de la 2", all("Comparecen" not in f["content"] for f in texto_fr if f["pageNumber"] == 1))
rev("un fragmento de campos por página con datos (1 y 2), con los nombres del tipo documental",
    sorted(f["pageNumber"] for f in campos_fr) == [1, 2] and any("Razón social: DORAMA SERVICIOS SA DE CV" in f["content"] for f in campos_fr)
    and any("Socios 1 › Nombre: ANA LÓPEZ RUIZ" in f["content"] for f in campos_fr) and all(f["content"].startswith("Acta constitutiva · datos extraídos") for f in campos_fr),
    str([f["content"] for f in campos_fr]))
rev("cada vector mide 768 × 4 bytes y lleva la etiqueta del modelo", all(f["bytes"] == DIM * 4 and f["embeddingModel"] == embeddings.etiqueta() for f in fr))
est = indice(ID_ACTA)
rev("el documento quedó INDEXED con su extracción y su cuenta", est["status"] == "INDEXED" and est["chunkCount"] == len(fr) and est["attempts"] == 0 and est["nextAttemptAt"] is None, str(est))
rev("los vectores se pidieron como RETRIEVAL_DOCUMENT, uno por fragmento distinto", len(LLAMADAS) == 1 and LLAMADAS[0][0] == "RETRIEVAL_DOCUMENT" and len(LLAMADAS[0][1]) == len({f["content"] for f in fr}), str([(t, len(x)) for t, x in LLAMADAS]))
ind = eventos(auditoria.SEMANTIC_INDEXED)
rev("SEMANTIC_INDEXED en la bitácora con el documento, la extracción y cuántos fragmentos", any(e.get("documento") == D_ACTA and e.get("corrida") == R_ACTA_1 and e.get("fragmentos") == len(fr) for e in ind), str(ind[-1:]))
rev("no queda nada más por hacer", indice_semantico.procesar_vencidos() == [])

# Más documentos para buscar.
D_INE_ANA, ID_INE_ANA = nuevo_documento(ORG_A, "ine-ana.pdf", E_ANA)
extraer(ORG_A, D_INE_ANA, T_INE, P_INE, {"nombre": hoja("ANA LÓPEZ RUIZ", 1, 1), "curp": hoja("LORA900101MDFPZN09", 1, 2), "ano_de_registro": hoja("2009", 1, 3)},
        {1: ["INSTITUTO NACIONAL ELECTORAL", "ANA LÓPEZ RUIZ", "CURP LORA900101MDFPZN09", "AÑO DE REGISTRO 2009", "CREDENCIAL PARA VOTAR VIGENCIA 2030"]})
D_INE_LUIS, ID_INE_LUIS = nuevo_documento(ORG_A, "ine-luis.pdf")
extraer(ORG_A, D_INE_LUIS, T_INE, P_INE, {"nombre": hoja("LUIS PÉREZ", 1, 1)}, {1: ["INSTITUTO NACIONAL ELECTORAL", "LUIS PÉREZ", "CREDENCIAL PARA VOTAR VIGENCIA 2024"]})
D_PAS, ID_PAS = nuevo_documento(ORG_B, "pasaporte-beto.pdf")
extraer(ORG_B, D_PAS, T_PAS, P_PAS, {"numero_de_pasaporte": hoja("G12345678", 1, 1)},
        {1: ["PASAPORTE MEXICANO", "CREDENCIAL PARA VOTAR NO APLICA", "ANA LÓPEZ RUIZ TESTIGO"]})
indice_semantico.procesar_vencidos(limite=10)
rev("los cuatro documentos quedaron INDEXED", all(indice(d).get("status") == "INDEXED" for d in (ID_ACTA, ID_INE_ANA, ID_INE_LUIS, ID_PAS)))

# ── 3. Corregir un dato ─────────────────────────────────────────────────────────────────────────────────────────────────
titulo("3. Corregir un dato (HU-094 CA-3)")
antes = {f["chunkId"]: f for f in fragmentos_de(ID_INE_ANA)}
campos_antes = [f for f in antes.values() if f["kind"] == "campos"]
LLAMADAS.clear()
datos_extraidos.corregir(ORG_A, D_INE_ANA, "nombre", "ANA LOPEZ RUIZ DE LA O", "Faltaba el segundo apellido", RITA)
rev("la corrección dejó el documento INDEX_PENDING", indice(ID_INE_ANA).get("status") == "INDEX_PENDING")
indice_semantico.procesar_vencidos()
despues = fragmentos_de(ID_INE_ANA)
rev("solo se pidió UN vector: el del fragmento de campos de su página", len(LLAMADAS) == 1 and len(LLAMADAS[0][1]) == 1 and "ANA LOPEZ RUIZ DE LA O" in LLAMADAS[0][1][0], str(LLAMADAS))
rev("el fragmento de campos trae el valor corregido y el anterior ya no existe",
    any("Nombre: ANA LOPEZ RUIZ DE LA O" in f["content"] for f in despues if f["kind"] == "campos") and not any(f["content"] == campos_antes[0]["content"] for f in despues),
    str([f["content"] for f in despues if f["kind"] == "campos"]))
rev("el texto leído no cambió: su fragmento se conservó tal cual (mismo contenido, mismo vector)",
    [f["content"] for f in despues if f["kind"] == "texto"] == [f["content"] for f in antes.values() if f["kind"] == "texto"])

# ── 4. Google no responde ───────────────────────────────────────────────────────────────────────────────────────────────
titulo("4. Google no responde (HU-094 CA-2)")
D_FALLA, ID_FALLA = nuevo_documento(ORG_A, "comprobante-falla.pdf")
FALLAR["n"] = 10
R_FALLA = extraer(ORG_A, D_FALLA, T_INE, P_INE, {"nombre": hoja("MARTA DIAZ", 1, 0)}, {1: ["MARTA DIAZ", "DOMICILIO CONOCIDO"]})
rev("la extracción se guardó aunque Google no responda", isinstance(R_FALLA, str) and R_FALLA.startswith("extraccion-"))
indice_semantico.procesar_vencidos()
e1 = indice(ID_FALLA)
rev("primer fallo: INDEX_PENDING, 1 intento, el siguiente en ~1 minuto, con el motivo", e1["status"] == "INDEX_PENDING" and e1["attempts"] == 1 and e1["nextAttemptAt"] is not None
    and "vectores" in (e1["lastError"] or ""), str(e1))
rev("…y nada se toma antes de su hora", indice_semantico.procesar_vencidos() == [])
ya_toca(ID_FALLA)
indice_semantico.procesar_vencidos()
ya_toca(ID_FALLA)
indice_semantico.procesar_vencidos()
e3 = indice(ID_FALLA)
rev("tercer fallo: ya no se programa otro intento y queda la alerta", e3["attempts"] == 3 and e3["nextAttemptAt"] is None and e3["alertedAt"] is not None, str(e3))
alertas = [e for e in eventos(auditoria.SEMANTIC_INDEX_ALERT) if e.get("documento") == D_FALLA]
rev("SEMANTIC_INDEX_ALERT en la bitácora con el documento y los intentos", len(alertas) == 1 and alertas[0].get("intentos") == 3, str(alertas))
rev("el barrido NO lo vuelve a programar (ya se avisó)", indice_semantico.barrer() == 0 and indice(ID_FALLA)["nextAttemptAt"] is None)
FALLAR["n"] = 0
extraer(ORG_A, D_FALLA, T_INE, P_INE, {"nombre": hoja("MARTA DIAZ", 1, 0)}, {1: ["MARTA DIAZ", "DOMICILIO CONOCIDO"]})
rev("una extracción nueva vuelve a empezar la cuenta", indice(ID_FALLA)["attempts"] == 0 and indice(ID_FALLA)["alertedAt"] is None)
indice_semantico.procesar_vencidos()
rev("…y con Google de vuelta queda INDEXED", indice(ID_FALLA)["status"] == "INDEXED")

# ── 5. Re-extraer ───────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("5. Re-extraer: nunca la versión vieja (HU-099 CA-1)")


def vista_como(tenant: str | None, sql: str, *params) -> list:
    con = consulta._conectar()
    try:
        cur = con.cursor()
        if tenant:
            cur.execute("EXEC sys.sp_set_session_context @key = N'organizacion', @value = ?, @read_only = 1", tenant)
        cur.execute(sql, *params)
        return cur.fetchall()
    finally:
        con.close()


n_vista = lambda doc: vista_como(ORG_A, "SELECT COUNT(*) FROM consulta.vFragmento WHERE documento = ?", doc)[0][0]  # noqa: E731
rev("antes: la vista enseña los fragmentos de la INE de Luis", n_vista(D_INE_LUIS) > 0)
R_LUIS_2 = extraer(ORG_A, D_INE_LUIS, T_INE, P_INE, {"nombre": hoja("LUIS PÉREZ GÓMEZ", 1, 1)},
                   {1: ["INSTITUTO NACIONAL ELECTORAL", "LUIS PÉREZ GÓMEZ", "CREDENCIAL PARA VOTAR VIGENCIA 2034"]})
rev("re-extraída y sin reindexar todavía: la vista ya NO enseña los fragmentos viejos", n_vista(D_INE_LUIS) == 0 and k.execute(
    "SELECT COUNT(*) FROM nexus.documentChunks WHERE documentId = ?", ID_INE_LUIS).fetchone()[0] > 0)
LLAMADAS.clear()
indice_semantico.procesar_vencidos()
fr_luis = fragmentos_de(ID_INE_LUIS)
rev("al reindexar: todos los fragmentos son de la extracción nueva", fr_luis and len({f["runId"] for f in fr_luis}) == 1 and
    fr_luis[0]["runId"] == k.execute("SELECT runId FROM nexus.extractionRuns WHERE publicId = ?", R_LUIS_2).fetchone()[0])
rev("…la vista los enseña y traen el texto nuevo", n_vista(D_INE_LUIS) == len(fr_luis) and any("VIGENCIA 2034" in f["content"] for f in fr_luis))
reind = [e for e in eventos(auditoria.SEMANTIC_REINDEXED) if e.get("documento") == D_INE_LUIS]
rev("SEMANTIC_REINDEXED lleva la extracción nueva y la anterior", len(reind) == 1 and reind[0].get("corrida") == R_LUIS_2 and len(reind[0].get("corridasAnteriores") or []) == 1, str(reind))
LLAMADAS.clear()
extraer(ORG_A, D_INE_LUIS, T_INE, P_INE, {"nombre": hoja("LUIS PÉREZ GÓMEZ", 1, 1)},
        {1: ["INSTITUTO NACIONAL ELECTORAL", "LUIS PÉREZ GÓMEZ", "CREDENCIAL PARA VOTAR VIGENCIA 2034"]})
indice_semantico.procesar_vencidos()
rev("re-extraer un documento que no cambió no pide ningún vector", LLAMADAS == [] and indice(ID_INE_LUIS)["status"] == "INDEXED", str(LLAMADAS))

# ── 6. Borrar un documento ──────────────────────────────────────────────────────────────────────────────────────────────
titulo("6. Borrar un documento (HU-099 CA-2)")
D_BORRAR, ID_BORRAR = nuevo_documento(ORG_A, "temporal.pdf")
extraer(ORG_A, D_BORRAR, T_INE, P_INE, {"nombre": hoja("TEMPORAL", 1, 0)}, {1: ["DOCUMENTO TEMPORAL"]})
indice_semantico.procesar_vencidos()
rev("indexado antes de borrar", indice(ID_BORRAR).get("status") == "INDEXED" and len(fragmentos_de(ID_BORRAR)) > 0)
k.execute("DELETE FROM nexus.documents WHERE documentId = ?", ID_BORRAR)
sa.commit()
rev("al borrarlo se van sus fragmentos y su estado", k.execute("SELECT COUNT(*) FROM nexus.documentChunks WHERE documentId = ?", ID_BORRAR).fetchone()[0] == 0 and indice(ID_BORRAR) == {})

# ── 7. Aislamiento ──────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("7. Aislamiento por organización (HU-095)")
rev("sin organización fijada, la vista no devuelve nada", vista_como(None, "SELECT COUNT(*) FROM consulta.vFragmento")[0][0] == 0)
rev("con Beta fijada, solo lo de Beta", {f[0] for f in vista_como(ORG_B, "SELECT DISTINCT documento FROM consulta.vFragmento")} == {D_PAS})
rev("con Alfa fijada, nada de Beta", D_PAS not in {f[0] for f in vista_como(ORG_A, "SELECT DISTINCT documento FROM consulta.vFragmento")})
try:
    vista_como(ORG_A, "SELECT TOP (1) chunkId FROM nexus.documentChunks")
    lee_nexus = True
except pyodbc.Error:
    lee_nexus = False
rev("el usuario de lectura no lee nexus.documentChunks", not lee_nexus)
try:
    vista_como(ORG_A, "EXEC sys.sp_set_session_context @key = N'organizacion', @value = ?; SELECT 1", ORG_B)
    cambia = True
except pyodbc.Error:
    cambia = False
rev("no se puede cambiar de organización a media conexión", not cambia)

k.execute("INSERT INTO nexus.semanticSearchSettings (tenant, chunkTokens, overlapTokens, minScore, updatedBy) VALUES (?, 512, 64, 0.05, 'prueba')", ORG_A)
k.execute("INSERT INTO nexus.semanticSearchSettings (tenant, chunkTokens, overlapTokens, minScore, updatedBy) VALUES (?, 512, 64, 0.05, 'prueba')", ORG_B)
sa.commit()
busqueda_documentos.olvidar()


def buscar(bearer: str, tenant: str, **cuerpo):
    return c.post("/busqueda/documentos", json={"tenant": tenant, **cuerpo}, headers=cab(bearer))


rb = buscar(b_admin, ORG_B, consulta="Ana López Ruiz credencial para votar", limite=20)
rev("la búsqueda de Beta encuentra lo suyo y nunca un documento de Alfa", rb.status_code == 200 and rb.json()["total"] >= 1 and
    {x["documento"] for x in rb.json()["resultados"]} == {D_PAS}, rb.text[:300])
cruzado = buscar(b_admin, ORG_A, consulta="Ana López")
rev("Beta pidiendo la organización de Alfa: 403", cruzado.status_code == 403, cruzado.text[:200])
rev("…y queda CROSS_TENANT_ACCESS", any(e.get("tenantPedido") == ORG_A for e in eventos(auditoria.CROSS_TENANT_ACCESS)))

# ── 8. Buscar ───────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("8. Buscar (HU-096 a HU-098)")
LLAMADAS.clear()
r1 = buscar(a_admin, ORG_A, consulta="socios que suscriben acciones Ana López", limite=3)
j1 = r1.json()
rev("200 con resultados ordenados por parecido", r1.status_code == 200 and j1["total"] >= 2 and [x["score"] for x in j1["resultados"]] == sorted([x["score"] for x in j1["resultados"]], reverse=True), r1.text[:300])
primero = j1["resultados"][0] if j1.get("resultados") else {}
rev("el más parecido es la página 2 del acta", primero.get("documento") == D_ACTA and primero.get("pagina") == 2, json.dumps(primero, ensure_ascii=False)[:300])
rev("trae archivo, tipo, fecha, clase, score con 4 decimales", primero.get("archivo") == "acta-constitutiva-dorama.pdf" and primero.get("tipoDocumental") == "Acta constitutiva"
    and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", primero.get("procesadoEn") or "") is not None and primero.get("clase") in ("texto", "campos")
    and 0 < primero.get("score", 0) <= 1)
marcas = [primero["pasaje"][a:b] for a, b in primero.get("resaltado", [])]
rev("el pasaje resalta las palabras de la consulta (sin distinguir acentos)", any(_plano(x) in ("ana", "lopez", "suscribe", "acciones", "socios") for x in marcas), str(marcas))
rev("el expediente del documento", primero.get("expedientes") == [{"id": E_ANA, "nombre": "Expediente de Ana"}], str(primero.get("expedientes")))
rev("los datos extraídos de esa página, con su nombre en pantalla y su confianza",
    any(x["nombre"] == "Socios 1 › Nombre" and x["valor"] == "ANA LÓPEZ RUIZ" and x["confianza"] == 93.5 for x in primero.get("campos", [])), str(primero.get("campos")))
rev("un solo resultado por documento, página y clase", len({(x["documento"], x["pagina"], x["clase"]) for x in j1["resultados"]}) == len(j1["resultados"]))
rev("el vector de la consulta se pidió como RETRIEVAL_QUERY", LLAMADAS and LLAMADAS[-1][0] == "RETRIEVAL_QUERY" and j1["vectorReusado"] is False, str(LLAMADAS))

LLAMADAS.clear()
r2 = buscar(a_admin, ORG_A, consulta="socios que suscriben acciones Ana López", limite=3, inicio=3)
rev("la página siguiente no vuelve a pedir el vector y sigue donde quedó", r2.status_code == 200 and LLAMADAS == [] and r2.json()["vectorReusado"] is True
    and r2.json()["inicio"] == 3 and not ({x["fragmento"] for x in r2.json()["resultados"]} & {x["fragmento"] for x in j1["resultados"]}), r2.text[:300])
r3 = buscar(a_admin, ORG_A, consulta="socios que suscriben acciones Ana López", limite=20, orden="fecha")
fechas = [x["procesadoEn"] for x in r3.json()["resultados"]]
rev("orden por fecha de procesamiento: lo más reciente primero, sin pedir el vector", r3.status_code == 200 and fechas == sorted(fechas, reverse=True) and LLAMADAS == [], str(fechas))
r4 = buscar(a_admin, ORG_A, consulta="credencial para votar vigencia", tipos=[T_INE], limite=20)
rev("filtro por tipo documental: solo INE", r4.status_code == 200 and r4.json()["total"] >= 1 and {x["tipoDocumentalId"] for x in r4.json()["resultados"]} == {T_INE}, r4.text[:300])
manana = (date.today() + timedelta(days=1)).isoformat()
r5 = buscar(a_admin, ORG_A, consulta="credencial para votar vigencia", desde=manana)
rev("filtro de fechas sin coincidencias: lista vacía con el mensaje", r5.status_code == 200 and r5.json()["total"] == 0 and r5.json()["mensaje"] == busqueda_documentos.SIN_RESULTADOS, r5.text[:200])
r6 = buscar(a_admin, ORG_A, consulta="credencial para votar vigencia", desde=date.today().isoformat(), hasta=date.today().isoformat(), limite=20)
rev("hasta incluye el día", r6.status_code == 200 and r6.json()["total"] >= 1)
k.execute("UPDATE nexus.semanticSearchSettings SET minScore = 0.70 WHERE tenant = ?", ORG_A)
sa.commit()
r7 = buscar(a_admin, ORG_A, consulta="tortuga marina en peligro")
rev("con el mínimo en 0.70 y nada parecido: sin resultados, con el mensaje y sin texto generado", r7.status_code == 200 and r7.json()["resultados"] == []
    and r7.json()["mensaje"] == "Sin resultados relevantes para tu consulta" and r7.json()["minimo"] == 0.7, r7.text[:200])
k.execute("DELETE FROM nexus.semanticSearchSettings WHERE tenant = ?", ORG_A)
sa.commit()
rev("sin renglón de ajustes, el mínimo es 0.65", buscar(a_admin, ORG_A, consulta="tortuga").json()["minimo"] == 0.65)
k.execute("INSERT INTO nexus.semanticSearchSettings (tenant, chunkTokens, overlapTokens, minScore, updatedBy) VALUES (?, 512, 64, 0.05, 'prueba')", ORG_A)
sa.commit()
for cuerpo, que in [({"consulta": "  "}, "vacía"), ({"consulta": "x" * 501}, "larga"), ({"consulta": "ine", "orden": "azar"}, "orden raro"),
                    ({"consulta": "ine", "limite": 21}, "límite > 20"), ({"consulta": "ine", "desde": "2026-12-01", "hasta": "2026-11-01"}, "fechas al revés")]:
    rr = buscar(a_admin, ORG_A, **cuerpo)
    rev(f"400/422 con consulta {que}", rr.status_code in (400, 422), f"{rr.status_code} {rr.text[:150]}")
sin_sesion = c.post("/busqueda/documentos", json={"tenant": ORG_A, "consulta": "ine"}, headers={"x-api-key": SERVICIO})
rev("sin sesión: no pasa", sin_sesion.status_code in (401, 403), str(sin_sesion.status_code))
busq = [e for e in eventos(auditoria.SEMANTIC_SEARCH) if e.get("tenant") == ORG_A]
rev("SEMANTIC_SEARCH con la consulta, los filtros, el total y quién", busq and busq[0].get("consulta") == "socios que suscriben acciones Ana López"
    and busq[0].get("usuarioGuid") == RITA and "filtros" in busq[0] and isinstance(busq[0].get("total"), int), str(busq[:1]))
est = c.get(f"/busqueda/documentos/estado?tenant={ORG_A}", headers=cab(a_admin))
rev("el estado del índice de la organización", est.status_code == 200 and est.json()["indexados"] == 4 and est.json()["pendientes"] == 0 and est.json()["fragmentos"] > 0, est.text)
rev("…y otra organización no puede verlo", c.get(f"/busqueda/documentos/estado?tenant={ORG_A}", headers=cab(b_admin)).status_code == 403)
# Una matriz por organización, y se recarga sola cuando cambia el índice.
total_antes = buscar(a_admin, ORG_A, consulta="escritura pública notario", limite=20).json()["total"]
D_NUEVO, ID_NUEVO = nuevo_documento(ORG_A, "poder-notarial.pdf")
extraer(ORG_A, D_NUEVO, T_ACTA, P_ACTA, {"razon_social": hoja("PODER NOTARIAL", 1, 0)}, {1: ["PODER NOTARIAL", "ESCRITURA PÚBLICA NÚMERO 9988 ante notario"]})
indice_semantico.procesar_vencidos()
rnuevo = buscar(a_admin, ORG_A, consulta="escritura pública notario", limite=20).json()
rev("un documento recién indexado ya sale (la matriz se recargó sola)", rnuevo["total"] > total_antes and D_NUEVO in {x["documento"] for x in rnuevo["resultados"]})
rev("cada organización tiene su propia matriz en memoria", set(busqueda_documentos._matrices) == {ORG_A, ORG_B})

# ── 9. El barrido ───────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("9. El barrido")
D_VIEJO, ID_VIEJO = nuevo_documento(ORG_A, "anterior-a-la-0022.pdf")
with tablas.transaccion() as cur:
    cur.execute(
        "INSERT INTO nexus.extractionRuns (publicId, documentId, documentTypeId, documentTypePublicId, documentTypeName, documentTypeRevision, engine, processorId, "
        "status, startedAt, createdBy) OUTPUT INSERTED.runId SELECT ?, ?, documentTypeId, publicId, name, 1, 'document_ai_extractor', processorId, 'ok', SYSUTCDATETIME(), 'antes' "
        "FROM nexus.documentTypes WHERE publicId = ? AND tenant = ?",
        f"extraccion-{secrets.token_hex(16)}", ID_VIEJO, T_INE, ORG_A,
    )
    corrida_vieja = cur.fetchone()[0]
    cur.execute("INSERT INTO nexus.entityFacts (runId, fieldPath, fieldName, valueRaw, valueNormalized, confidence, pageNumber) VALUES (?, 'nombre', 'nombre', 'JULIA SOTO', 'JULIA SOTO', 90, 1)", corrida_vieja)
rev("un documento extraído antes de la 0022 no tiene índice", indice(ID_VIEJO) == {})
n = indice_semantico.barrer()
rev("el barrido lo programa", n >= 1 and indice(ID_VIEJO).get("status") == "INDEX_PENDING")
indice_semantico.procesar_vencidos(limite=10)
fv = fragmentos_de(ID_VIEJO)
rev("…y entra solo con su fragmento de campos (su texto no se guardó)", [f["kind"] for f in fv] == ["campos"] and "Nombre: JULIA SOTO" in fv[0]["content"], str(fv))
k.execute("UPDATE nexus.documentIndexes SET status = 'INDEX_PENDING', nextAttemptAt = NULL WHERE documentId = ?", ID_VIEJO)
sa.commit()
rev("uno atascado (pendiente sin intento programado ni alerta) también se recoge", indice_semantico.barrer() >= 1 and indice(ID_VIEJO)["nextAttemptAt"] is not None)
indice_semantico.procesar_vencidos(limite=10)

# ── 10. Los CHECK ───────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("10. Los CHECK de las tablas nuevas")


def muerde(sql: str, *params) -> bool:
    try:
        k.execute(sql, *params)
        sa.rollback()
        return False
    except pyodbc.Error:
        sa.rollback()
        return True


run_acta = k.execute("SELECT runId FROM nexus.extractionRuns WHERE publicId = ?", R_ACTA_1).fetchone()[0]
for sql, params, que in [
    ("INSERT INTO nexus.documentChunks (tenant, documentId, runId, kind, pageNumber, chunkIndex, content, tokenCount, embedding, embeddingModel) VALUES (?, ?, ?, 'otro', 1, 99, 'x', 1, 0x00000000, 'm')", (ORG_A, ID_ACTA, run_acta), "clase desconocida"),
    ("INSERT INTO nexus.documentChunks (tenant, documentId, runId, kind, pageNumber, chunkIndex, content, tokenCount, embedding, embeddingModel) VALUES (?, ?, ?, 'texto', NULL, 99, 'x', 1, 0x00000000, 'm')", (ORG_A, ID_ACTA, run_acta), "texto sin página"),
    ("INSERT INTO nexus.documentChunks (tenant, documentId, runId, kind, pageNumber, chunkIndex, content, tokenCount, embedding, embeddingModel) VALUES (?, ?, ?, 'texto', 1, 99, 'x', 1, 0x000000, 'm')", (ORG_A, ID_ACTA, run_acta), "vector que no es de float32"),
    ("INSERT INTO nexus.documentChunks (tenant, documentId, runId, kind, pageNumber, chunkIndex, content, tokenCount, embedding, embeddingModel) VALUES (?, ?, ?, 'texto', 1, 99, '', 1, 0x00000000, 'm')", (ORG_A, ID_ACTA, run_acta), "contenido vacío"),
    ("UPDATE nexus.documentIndexes SET status = 'LISTO' WHERE documentId = ?", (ID_ACTA,), "estado desconocido"),
    ("UPDATE nexus.documentIndexes SET indexedAt = NULL WHERE documentId = ?", (ID_ACTA,), "INDEXED sin fecha"),
    ("INSERT INTO nexus.semanticSearchSettings (tenant, chunkTokens, overlapTokens, minScore) VALUES (?, 512, 600, 0.7)", (ORG_B + "x",), "solape mayor que el fragmento / organización inexistente"),
    ("UPDATE nexus.semanticSearchSettings SET minScore = 1.5 WHERE tenant = ?", (ORG_B,), "mínimo mayor que 1"),
    ("INSERT INTO nexus.ocrBlocks (ocrResultId, blockIndex, pageNumber, blockType, text, bboxX, bboxY, bboxW, bboxH) VALUES (?, 9999, 1, 'line', 'x', 1.2, 0, 0, 0)", (ocr[0],), "caja fuera de 0-1"),
    ("INSERT INTO nexus.ocrResults (runId, engine, pageCount) VALUES (?, 'tesseract', 1)", (run_acta,), "motor desconocido / segunda lectura de la misma corrida"),
]:
    rev(f"no deja: {que}", muerde(sql, *params))

# ── Lógica pura ─────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("Lógica pura (fragmentos)")
rev("partir respeta el tamaño y avanza siempre", all(fragmentos.estimar_tokens(t) <= 20 for t in fragmentos.partir("uno dos tres " * 40, 20, 5)))
rev("una palabra enorme queda sola", fragmentos.partir("a " + "x" * 400 + " b", 10, 2) == ["a", "x" * 400, "b"] or len(fragmentos.partir("x" * 400, 10, 2)) == 1)
rev("resaltar ignora acentos y mayúsculas", fragmentos.resaltar("Ana LÓPEZ suscribió acciones", "lopez accion") == [[4, 9], [20, 28]],
    str(fragmentos.resaltar("Ana LÓPEZ suscribió acciones", "lopez accion")))
rev("resaltar no marca palabras vacías", fragmentos.resaltar("la casa de la sociedad", "la de") == [])
p = fragmentos.pasaje("palabra " * 200 + "notario " + "palabra " * 200, fragmentos.resaltar("palabra " * 200 + "notario " + "palabra " * 200, "notario"), 100)
rev("el pasaje se centra en la coincidencia y sus tramos siguen apuntando a ella", p["texto"][p["resaltado"][0][0]:p["resaltado"][0][1]] == "notario" and not p["alInicio"] and not p["alFinal"], str(p)[:200])
rev("vectores ida y vuelta por bytes", [round(x, 5) for x in indice_semantico.de_bytes(indice_semantico.a_bytes([0.5, -1.25, 3.0]))] == [0.5, -1.25, 3.0])

print()
print("TODO EN VERDE" if not fallos else f"{fallos} FALLA(S)")
sys.exit(1 if fallos else 0)
