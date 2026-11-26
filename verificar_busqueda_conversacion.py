"""¿La búsqueda como conversación (NEX-611) hace lo que dice el ticket?

    .venv/Scripts/python verificar_busqueda_conversacion.py

Corre contra el SQL Server desechable de `pruebas_sql.py` (sin Docker se salta y lo dice), con todas las migraciones (la 0019 incluida) y el login de solo
lectura que hizo el DBA (aquí lo crea la prueba). El modelo y el de vectores se sustituyen por un doble que anota el texto que le llegaría y contesta lo
que dice el guion: NO se llama a Google. Lo que se prueba:

1. la primera pregunta empieza una conversación (id con su forma, pregunta 1) y el modelo recibe la regla de la primera pregunta, sin conversación;
2. la de seguimiento: misma conversación, pregunta 2; al modelo le llegan la pregunta anterior, cómo se entendió y su consulta, y NUNCA un renglón de la
   respuesta ni un dato extraído; «¿y cuántas de esas tienen campos corregidos?» corre la consulta que conserva lo de antes (lo mismo que a mano), la
   explicación dice qué se tomó, y si el modelo no lo dijo se le antepone;
3. las sugerencias: hasta tres, sin repetidas ni la pregunta misma, sin las largas; se guardan, y una respuesta reusada trae las suyas;
4. con preguntas anteriores no se reusa nada por la pregunta sola; sin ellas sí; lo que tomó algo de su conversación no se reusa suelto, no sale en «Tus
   preguntas recientes», no llega a «Por revisar» y no se aprueba como ejemplo (409), aunque sí se puede valorar;
5. «Nueva conversación»: «¿y de esas?» sin anteriores → no se puede, con la conversación en el 422;
6. nunca se mezclan: el id de la conversación de otra persona o de otra organización (o uno sin forma) empieza una nueva, sin nada de la otra;
7. lo que no salió bien (tardó, sin consulta segura, el modelo falló, el tope) no se le cuenta al modelo, pero sí cuenta como pregunta; «no se puede»
   sí, con su motivo; solo van las últimas cinco;
8. la base: los CHECK de la 0019 muerden.
"""

import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
from datetime import date
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-busqueda-conversacion-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["CORREO_MODO"] = "silencio"
os.environ["DOCAI_PROJECT_ID"] = "proyecto-de-prueba"

import verificacion_comun  # noqa: E402,F401  (fija AUTH_JWT_SECRET)
import pruebas_sql  # noqa: E402

BASE = pruebas_sql.preparar("nexus-busqueda-conversacion", 14346)

DB = BASE["base"]
LECTURA = "usrNexusLectura"
CLAVE = "Lec-" + secrets.token_urlsafe(12) + "-9!"
sa = BASE["conectar"]("sa", BASE["sa"], DB)
k = sa.cursor()
m = BASE["conectar"]("sa", BASE["sa"])
m.cursor().execute(f"IF SUSER_ID('{LECTURA}') IS NULL CREATE LOGIN {LECTURA} WITH PASSWORD = '{CLAVE}', CHECK_POLICY = ON ELSE ALTER LOGIN {LECTURA} WITH PASSWORD = '{CLAVE}'")
k.execute(f"CREATE USER {LECTURA} FOR LOGIN {LECTURA}")
k.execute(f"GRANT SELECT ON SCHEMA::consulta TO {LECTURA}")
carpeta = Path(tempfile.mkdtemp(prefix="busqueda-conversacion-lectura-"))
ARCHIVO_LECTURA = carpeta / "lectura.env"
ARCHIVO_LECTURA.write_text(f"SQLSERVER_USER={LECTURA}\nSQLSERVER_PASSWORD='{CLAVE}'\n", encoding="utf-8")
os.environ["NEXUS_LECTURA_ENV"] = str(ARCHIVO_LECTURA)

from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
from app import app  # noqa: E402
from db import consulta, tablas  # noqa: E402
from servicios import auth, busqueda, gemini, usuarios  # noqa: E402
from servicios import busqueda_bitacora as bitacora  # noqa: E402

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(("OK   " if ok else "FALLA") + " " + descripcion + ("" if ok or not extra else f"\n      {extra}"))
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n[{t}]")


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
ORG_A, a_admin = crear_organizacion(bearer_super, "Alfa", "rita@ejemplo.com", "Rumbo-Sereno-Azul-47!")
ORG_B, b_admin = crear_organizacion(bearer_super, "Beta", "beto@ejemplo.com", "Rumbo-Sereno-Rojo-48!")
a_viewer = sumar_persona(a_admin, "vera@ejemplo.com", "VIEWER", "Cielo-Abierto-Azul-55!")


def campo(nombre: str, tipo_dato: str = "texto") -> dict:
    return {"nombre": nombre, "tipoDato": tipo_dato, "descripcion": "", "obligatorio": False, "cardinalidad": "unico", "valoresLista": [], "subcampos": []}


def guardar_tipo(bearer: str, tenant: str, tipo_id: str, nombre: str, campos: list) -> None:
    datos = {"nombre": nombre, "descripcion": f"{nombre} de prueba.", "estado": "activo", "procesadorId": "proc-" + tipo_id[-6:], "campos": campos}
    r = c.put(f"/tipos-documentales/{tipo_id}", json={"tenant": tenant, "revision": 0, "datos": datos}, headers=cab(bearer))
    assert r.status_code == 200, r.text


guardar_tipo(a_admin, ORG_A, "tipo-prueba-ine-01", "INE", [campo("Nombre"), campo("CURP"), campo("Vigencia", "fecha")])
guardar_tipo(a_admin, ORG_A, "tipo-prueba-acta-01", "Acta constitutiva", [campo("Razón social")])
guardar_tipo(b_admin, ORG_B, "tipo-prueba-pasaporte-01", "Pasaporte", [campo("Número de pasaporte")])


def documento(cur, tenant: str, nombre: str, recibido: str | None = None) -> int:
    cur.execute(
        "INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel) OUTPUT INSERTED.documentId VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        f"documento-{secrets.token_hex(16)}", tenant, secrets.token_hex(32), f"{tenant}/aa/aa/{secrets.token_hex(32)}", 1234, "application/pdf", nombre, "MANUAL",
    )
    doc = cur.fetchone()[0]
    if recibido:
        cur.execute("UPDATE nexus.documents SET receivedAt = ? WHERE documentId = ?", recibido, doc)
    return doc


def corrida(cur, doc: int, tipo: str, nombre: str) -> int:
    cur.execute(
        "INSERT INTO nexus.extractionRuns (publicId, documentId, documentTypeId, documentTypePublicId, documentTypeName, documentTypeRevision, engine, processorId, "
        "processorVersion, status, errorDetail, confidenceAverage, confidenceMinimum, pageCount, metadataJson, startedAt, createdBy) "
        "OUTPUT INSERTED.runId VALUES (?, ?, NULL, ?, ?, 1, 'document_ai_extractor', 'proc123', NULL, 'ok', NULL, 90, 80, 1, NULL, SYSUTCDATETIME(), 'prueba')",
        f"extraccion-{secrets.token_hex(16)}", doc, tipo, nombre,
    )
    return cur.fetchone()[0]


def dato(cur, run: int, ruta: str, valor: str | None) -> int:
    cur.execute(
        "INSERT INTO nexus.entityFacts (runId, fieldPath, fieldName, recordIndex, valueRaw, valueNormalized, confidence, nullReason, pageNumber) "
        "OUTPUT INSERTED.factId VALUES (?, ?, ?, NULL, ?, ?, 95, ?, 1)",
        run, ruta, ruta, valor, valor, None if valor is not None else "not_present",
    )
    return cur.fetchone()[0]


ESTE_ANO = date.today().year


def ine(tenant: str, archivo: str, nombre: str, curp: str | None, recibido: str | None = None, corregir_nombre: str | None = None) -> None:
    with tablas.transaccion() as cur:
        r = corrida(cur, documento(cur, tenant, archivo, recibido), "tipo-prueba-ine-01", "INE")
        f = dato(cur, r, "nombre", nombre)
        dato(cur, r, "curp", curp)
        dato(cur, r, "vigencia", f"{ESTE_ANO}-12-31")
        if corregir_nombre:
            cur.execute("INSERT INTO nexus.fieldCorrections (factId, valueNormalized, reason, correctedBy) VALUES (?, ?, ?, ?)", f, corregir_nombre, "acento", "prueba")


# Tres INE de este año (una con un campo corregido) y una del año pasado, también corregida: «las de este año con campos corregidos» es UNA; «todos los
# documentos con campos corregidos», DOS.
ine(ORG_A, "ine-ana.pdf", "ANA PRUEBA LOPEZ", "PRLA900101MDFXXX01")
ine(ORG_A, "ine-juan.pdf", "JUAN PEREZ", None, corregir_nombre="JUAN PÉREZ")
ine(ORG_A, "ine-luz.pdf", "LUZ PRUEBA", "PRLU900101MDFXXX02")
ine(ORG_A, "ine-vieja.pdf", "PEDRO VIEJO", "VIPE800101HDFXXX03", recibido=f"{ESTE_ANO - 1}-05-10", corregir_nombre="PEDRO VIEJO R")
with tablas.transaccion() as cur:
    r = corrida(cur, documento(cur, ORG_B, "pasaporte-maria.pdf"), "tipo-prueba-pasaporte-01", "Pasaporte")
    dato(cur, r, "numero_de_pasaporte", "G12345678")
VALORES_EXTRAIDOS = ("ANA PRUEBA", "PRLA900101", "JUAN PEREZ", "JUAN PÉREZ", "LUZ PRUEBA", "PEDRO VIEJO", "G12345678")


# ── El doble de Google ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
llamadas: list[str] = []
guion: list = []
USO = {"promptTokenCount": 9000, "cachedContentTokenCount": 4000, "candidatesTokenCount": 150, "thoughtsTokenCount": 50, "totalTokenCount": 9200}


def vector_falso(texto: str, dimensiones: int) -> list[float]:
    v = [0.0] * dimensiones
    for palabra in bitacora.clave_de(texto).split():
        v[int(hashlib.sha256(palabra.encode()).hexdigest(), 16) % dimensiones] += 1.0
    return v


vectorizados: list[list[str]] = []


async def llamar_falso(url, headers, cuerpo, timeout):
    if url.endswith(":predict"):
        textos = [i["content"] for i in cuerpo["instances"]]
        vectorizados.append(textos)
        d = cuerpo["parameters"]["outputDimensionality"]
        return 200, {"predictions": [{"embeddings": {"values": vector_falso(t, d), "statistics": {"token_count": 5}}} for t in textos]}
    texto = cuerpo["contents"][0]["parts"][0]["text"]
    llamadas.append(texto)
    respuesta = guion.pop(0) if guion else {"consulta": None, "noSePuede": "sin guion"}
    if isinstance(respuesta, int):
        return respuesta, {"error": {"status": "X", "message": "falla de prueba"}}
    salida = respuesta if isinstance(respuesta, str) else json.dumps(respuesta, ensure_ascii=False)
    return 200, {"candidates": [{"content": {"parts": [{"text": salida}]}, "finishReason": "STOP"}], "usageMetadata": USO}


gemini._llamar = llamar_falso
gemini._token = lambda: "token-falso"
config.EMBEDDINGS_DIMENSIONES = 256
config.BUSQUEDA_TOPE_PERSONA_POR_HORA = config.BUSQUEDA_TOPE_ORGANIZACION_POR_DIA = 10_000


def sql(consulta_sql: str, explicacion: str = "Entendí la pregunta.", tomado: str | None = None, sugerencias: list | None = None) -> dict:
    return {"consulta": consulta_sql, "explicacion": explicacion, "noSePuede": None, "tomadoDeAntes": tomado, "sugerencias": sugerencias or []}


def preguntar(bearer: str, tenant: str, pregunta, conversacion=None, *respuestas):
    guion[:] = list(respuestas)
    cuerpo = {"tenant": tenant, "pregunta": pregunta}
    if conversacion is not None:
        cuerpo["conversacion"] = conversacion
    return c.post("/busqueda/preguntar", json=cuerpo, headers=cab(bearer))


def renglon(publico: str) -> dict:
    with tablas.lectura() as cur:
        cur.execute("SELECT * FROM nexus.searchQuestions WHERE publicId = ?", publico)
        return tablas.una(cur) or {}


def ultimo(tenant: str = ORG_A) -> dict:
    with tablas.lectura() as cur:
        cur.execute("SELECT TOP (1) * FROM nexus.searchQuestions WHERE tenant = ? ORDER BY questionId DESC", tenant)
        return tablas.una(cur) or {}


def a_mano(consulta_sql: str, tenant: str = ORG_A) -> list:
    return [list(f) for f in consulta.consultar(tenant, consulta_sql).filas]


def seccion_conversacion(prompt: str) -> str:
    """Lo que el prompt cuenta de la conversación (entre los ejemplos y la pregunta)."""
    inicio = prompt.find("Conversación: antes")
    return prompt[inicio:prompt.rfind("Pregunta: «")] if inicio >= 0 else ""


PRIMERA = "Esta es la primera pregunta de una conversación"
INE_ANO = "SELECT COUNT(*) AS [INE de este año] FROM consulta.vDocumentoExtraido WHERE tipoDocumental = 'INE' AND YEAR(recibidoEn) = YEAR(GETDATE())"
INE_ANO_CORREGIDAS = (
    "SELECT COUNT(*) AS [INE de este año con campos corregidos] FROM consulta.vDocumentoExtraido "
    "WHERE tipoDocumental = 'INE' AND YEAR(recibidoEn) = YEAR(GETDATE()) AND camposCorregidos > 0"
)
CORREGIDOS_TODOS = "SELECT COUNT(*) AS [Documentos con campos corregidos] FROM consulta.vDocumentoExtraido WHERE camposCorregidos > 0"
LISTA_INE = "SELECT TOP (200) documento, archivo AS [Archivo] FROM consulta.vDocumentoExtraido WHERE tipoDocumental = 'INE' AND YEAR(recibidoEn) = YEAR(GETDATE()) ORDER BY archivo"
SUGERIDAS = ["¿Y cuántas de esas tienen campos corregidos?", "¿Y por mes?", "¿Cuáles son?"]

# ── [1] La primera pregunta ───────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("1 · la primera pregunta empieza una conversación")
r = preguntar(a_admin, ORG_A, "¿Cuántas INE llegaron este año?", None,
              sql(INE_ANO, "Conté las INE que llegaron este año.", "algo que no hay", SUGERIDAS + ["¿Cuántas INE llegaron este año?", "¿y por mes?", "x" * 130, "", 7, "¿Y en total?"]))
uno = r.json() if r.status_code == 200 else {}
C1 = uno.get("conversacion") or ""
rev("200 con una conversación nueva (`conversacion-` + 32 hexadecimales) y la pregunta 1", r.status_code == 200 and re.fullmatch(r"conversacion-[0-9a-f]{32}", C1) is not None and uno.get("turno") == 1, r.text[:300])
rev("…la tabla es la de siempre: 3 INE de este año", uno.get("filas") == [[3]] and uno.get("filas") == a_mano(INE_ANO), str(uno.get("filas")))
rev("…sin preguntas anteriores no se tomó nada, diga lo que diga el modelo, y la explicación queda como la escribió",
    uno.get("usaAnteriores") is False and uno.get("explicacion") == "Conté las INE que llegaron este año.", str({x: uno.get(x) for x in ("usaAnteriores", "explicacion")}))
rev("…las sugerencias: hasta tres, sin la pregunta misma, sin repetidas (sin fijarse en mayúsculas ni signos), sin las vacías, sin las largas, sin lo que no es texto",
    uno.get("sugerencias") == SUGERIDAS, str(uno.get("sugerencias")))
P1 = llamadas[-1]
rev("…el modelo recibió la regla de la primera pregunta y ninguna conversación", PRIMERA in P1 and "Conversación: antes" not in P1)
rev("…y el JSON que se le pide trae tomadoDeAntes y sugerencias", '"tomadoDeAntes"' in P1 and '"sugerencias"' in P1)
g = renglon(uno.get("preguntaId") or "")
rev("la bitácora: su conversación, pregunta 1, sin «tomó de antes» (NULL: no había anteriores) y las sugerencias guardadas",
    g.get("conversationId") == C1 and g.get("turn") == 1 and g.get("usesContext") is None and json.loads(g.get("suggestionsJson") or "null") == SUGERIDAS,
    str({x: g.get(x) for x in ("conversationId", "turn", "usesContext", "suggestionsJson")}))

# ── [2] La de seguimiento ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("2 · la pregunta de seguimiento se entiende con la anterior")
n_vectores = len(vectorizados)
r = preguntar(a_admin, ORG_A, "¿y cuántas de esas tienen campos corregidos?", C1,
              sql(INE_ANO_CORREGIDAS, "Tomé las INE de este año de tu pregunta anterior y conté las que tienen campos corregidos.", "las INE de este año", ["¿Cuáles son?"]))
dos = r.json() if r.status_code == 200 else {}
rev("200 en la MISMA conversación, pregunta 2", r.status_code == 200 and dos.get("conversacion") == C1 and dos.get("turno") == 2, r.text[:300])
rev("…responde sobre las INE de este año (1), no sobre todos los documentos corregidos (2): lo mismo que a mano",
    dos.get("filas") == [[1]] and dos.get("filas") == a_mano(INE_ANO_CORREGIDAS) and a_mano(CORREGIDOS_TODOS) == [[2]], str(dos.get("filas")))
rev("…dice que tomó algo de la anterior y la explicación dice qué",
    dos.get("usaAnteriores") is True and dos.get("explicacion", "").startswith("Tomé las INE de este año de tu pregunta anterior"), str({x: dos.get(x) for x in ("usaAnteriores", "explicacion")}))
P2 = llamadas[-1]
conv = seccion_conversacion(P2)
rev("al modelo le llegó la pregunta anterior, cómo se entendió y su consulta",
    "1. «¿Cuántas INE llegaron este año?»" in conv and "Se entendió así: Conté las INE que llegaron este año." in conv and f"Su consulta: {INE_ANO}" in conv and PRIMERA not in P2, conv[:600])
rev("…y NINGÚN renglón de la respuesta ni dato extraído (ni en todo el texto)", not any(v in P2 for v in VALORES_EXTRAIDOS) and "[[3]]" not in P2)
rev("…la pregunta nueva va al final, sola", P2.rstrip().endswith("Pregunta: «¿y cuántas de esas tienen campos corregidos?»"))
rev("los ejemplos se eligieron con la anterior y la nueva juntas (sola, «¿y de esas?» no dice nada)",
    len(vectorizados) > n_vectores and vectorizados[n_vectores][0] == "¿Cuántas INE llegaron este año? ¿y cuántas de esas tienen campos corregidos?", str(vectorizados[n_vectores][:1] if len(vectorizados) > n_vectores else None))
g = renglon(dos.get("preguntaId") or "")
rev("la bitácora: misma conversación, pregunta 2, «tomó de antes» = 1", g.get("conversationId") == C1 and g.get("turn") == 2 and g.get("usesContext") is True, str({x: g.get(x) for x in ("conversationId", "turn", "usesContext")}))
P_DEPENDE = dos.get("preguntaId")

r = preguntar(a_admin, ORG_A, "¿cuáles son?", C1, sql(LISTA_INE, "Listé las INE de este año.", "las INE de este año"))
tres = r.json() if r.status_code == 200 else {}
rev("si el modelo tomó algo pero no lo dijo en la explicación, se le antepone «Tomé … de tu pregunta anterior.»",
    tres.get("explicacion") == "Tomé las INE de este año de tu pregunta anterior. Listé las INE de este año." and tres.get("turno") == 3, str(tres.get("explicacion")))
conv = seccion_conversacion(llamadas[-1])
rev("…y a la tercera le llegan las dos anteriores, en orden", conv.find("1. «¿Cuántas INE llegaron este año?»") >= 0 and conv.find("2. «¿y cuántas de esas tienen campos corregidos?»") > conv.find("1. «"), conv[:500])
rev("…tampoco ahí ningún dato extraído", not any(v in llamadas[-1] for v in VALORES_EXTRAIDOS))

r = preguntar(a_admin, ORG_A, "¿Cuántos documentos hay por tipo documental?", C1,
              sql("SELECT tipoDocumental AS [Tipo documental], COUNT(*) AS [Documentos] FROM consulta.vDocumentoExtraido GROUP BY tipoDocumental", "Conté por tipo.", None))
cuatro = r.json() if r.status_code == 200 else {}
g = renglon(cuatro.get("preguntaId") or "")
rev("una pregunta que no toma nada de antes, dentro de la conversación: usaAnteriores false y en la bitácora 0 (la contestó sola)",
    r.status_code == 200 and cuatro.get("usaAnteriores") is False and cuatro.get("turno") == 4 and g.get("usesContext") is False, str({x: g.get(x) for x in ("usesContext", "turn")}))

# ── [3] / [4] Reusar, recientes, por revisar, ejemplos ───────────────────────────────────────────────────────────────────────────────
titulo("3-4 · con anteriores no se reusa; lo que depende de la conversación no se reusa suelto, no se repite ni se aprueba")
n = len(llamadas)
r = preguntar(a_admin, ORG_A, "¿Cuántas INE llegaron este año?", C1, sql(INE_ANO, "Conté las INE de este año.", None, ["¿Y por mes?"]))
cinco = r.json() if r.status_code == 200 else {}
rev("la MISMA pregunta que ya salió bien, pero dentro de una conversación con anteriores: se le pregunta al modelo (origen modelo, sin reuso)",
    r.status_code == 200 and cinco.get("origen") == "modelo" and cinco.get("reusoDe") is None and len(llamadas) == n + 1, r.text[:200])
n = len(llamadas)
r = preguntar(a_admin, ORG_A, "cuantas INE llegaron este ano", None)
seis = r.json() if r.status_code == 200 else {}
rev("…en una conversación nueva sí se reusa la más reciente que se contestó sola (la de recién, sin llamar al modelo) y trae las sugerencias que se guardaron con ella",
    r.status_code == 200 and seis.get("origen") == "anterior" and seis.get("reusoDe") == cinco.get("preguntaId") and len(llamadas) == n
    and seis.get("sugerencias") == ["¿Y por mes?"] and seis.get("turno") == 1 and seis.get("conversacion") not in (None, C1), r.text[:300])
n = len(llamadas)
r = preguntar(a_viewer, ORG_A, "¿y cuántas de esas tienen campos corregidos?", None,
              {"consulta": None, "explicacion": "", "noSePuede": "No hay una pregunta anterior a la cual referirse: escríbela completa.", "tomadoDeAntes": None, "sugerencias": ["¿Algo?"]})
rev("lo que tomó algo de su conversación NO se reusa suelto: la misma frase en una conversación nueva va al modelo",
    len(llamadas) == n + 1 and r.status_code == 422, r.text[:200])
rec = c.get(f"/busqueda/preguntas?tenant={ORG_A}&limite=20", headers=cab(a_admin)).json().get("preguntas", [])
textos = [x["pregunta"] for x in rec]
rev("«Tus preguntas recientes» no trae las que tomaron algo de su conversación («¿y de esas?» no se repite sola), sí las sueltas",
    "¿y cuántas de esas tienen campos corregidos?" not in textos and "¿cuáles son?" not in textos and any("INE llegaron" in t for t in textos)
    and "¿Cuántos documentos hay por tipo documental?" in textos, str(textos))
r = c.post(f"/busqueda/preguntas/{P_DEPENDE}/valoracion", json={"tenant": ORG_A, "util": True}, headers=cab(a_admin))
rev("una respuesta que depende de la conversación sí se puede valorar", r.status_code == 200, r.text[:200])
c.post(f"/busqueda/preguntas/{uno.get('preguntaId')}/valoracion", json={"tenant": ORG_A, "util": True}, headers=cab(a_admin))
revisar = c.get(f"/busqueda/por-revisar?tenant={ORG_A}", headers=cab(a_admin)).json().get("preguntas", [])
rev("…pero no llega a «Por revisar»; la suelta marcada útil sí", [x["pregunta"] for x in revisar] == ["¿Cuántas INE llegaron este año?"], str([x["pregunta"] for x in revisar]))
r = c.post("/busqueda/ejemplos", json={"tenant": ORG_A, "preguntaId": P_DEPENDE}, headers=cab(a_admin))
rev("…y aprobarla como ejemplo da 409 depende_de_la_conversacion", r.status_code == 409 and r.json().get("detail", {}).get("codigo") == "depende_de_la_conversacion", r.text[:200])
r = c.post("/busqueda/ejemplos", json={"tenant": ORG_A, "preguntaId": uno.get("preguntaId")}, headers=cab(a_admin))
EJ = r.json().get("id") if r.status_code == 201 else None
n = len(llamadas)
r = preguntar(a_viewer, ORG_A, "¿Cuántas INE llegaron este año?", None)
siete = r.json() if r.status_code == 200 else {}
rev("un ejemplo aprobado, reusado en una conversación nueva, trae las sugerencias de la respuesta de la que salió",
    r.status_code == 200 and siete.get("origen") == "ejemplo" and siete.get("reusoDe") == EJ and siete.get("sugerencias") == SUGERIDAS and len(llamadas) == n, r.text[:300])

# ── [5] Nueva conversación ───────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("5 · «Nueva conversación»: «¿y de esas?» ya no tiene a qué referirse")
r = preguntar(a_admin, ORG_A, "¿y de esas?", None,
              {"consulta": None, "explicacion": "", "noSePuede": "No hay una pregunta anterior en esta conversación a la cual referirse. Escribe la pregunta completa.", "tomadoDeAntes": None, "sugerencias": []})
d = r.json().get("detail", {}) if r.status_code == 422 else {}
rev("422 no_se_puede con el motivo, y la conversación nueva en el detalle (pregunta 1)",
    r.status_code == 422 and d.get("codigo") == "no_se_puede" and "pregunta anterior" in d.get("mensaje", "") and re.fullmatch(r"conversacion-[0-9a-f]{32}", d.get("conversacion") or "")
    and d.get("conversacion") != C1 and d.get("turno") == 1, r.text[:300])
rev("…el modelo no recibió ninguna pregunta de la conversación vieja, solo la regla de la primera", PRIMERA in llamadas[-1] and "Conversación: antes" not in llamadas[-1] and "INE llegaron" not in seccion_conversacion(llamadas[-1]))
C5 = d.get("conversacion")
r = preguntar(a_admin, ORG_A, "me refiero a las INE de este año", C5, sql(INE_ANO, "Conté las INE de este año."))
conv = seccion_conversacion(llamadas[-1])
rev("lo que no se pudo responder SÍ se le cuenta al modelo (con su motivo) y la conversación sigue: pregunta 2",
    r.status_code == 200 and r.json().get("turno") == 2 and "1. «¿y de esas?»" in conv and "No se pudo responder: No hay una pregunta anterior" in conv, conv[:400])

# ── [6] Nunca se mezclan ─────────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("6 · nunca se mezclan personas ni organizaciones")
r = preguntar(a_viewer, ORG_A, "¿y de esas?", C1, {"consulta": None, "explicacion": "", "noSePuede": "No hay pregunta anterior.", "tomadoDeAntes": None, "sugerencias": []})
d = r.json().get("detail", {}) if r.status_code == 422 else {}
rev("otra persona de la MISMA organización con la conversación de Rita: empieza una nueva, sin ninguna pregunta de la de Rita",
    d.get("conversacion") not in (None, C1) and d.get("turno") == 1 and PRIMERA in llamadas[-1] and "Conversación: antes" not in llamadas[-1]
    and "¿cuáles son?" not in llamadas[-1], r.text[:300])
r = preguntar(b_admin, ORG_B, "¿y cuántas de esas tienen campos corregidos?", C1, {"consulta": None, "explicacion": "", "noSePuede": "No hay pregunta anterior.", "tomadoDeAntes": None, "sugerencias": []})
d = r.json().get("detail", {}) if r.status_code == 422 else {}
rev("otra ORGANIZACIÓN con el id de la conversación de Alfa: empieza una nueva y al modelo no le llega nada de Alfa (ni su conversación ni su ejemplo)",
    d.get("conversacion") not in (None, C1) and d.get("turno") == 1 and "Conversación: antes" not in llamadas[-1] and "INE llegaron" not in llamadas[-1]
    and "ine-ana" not in llamadas[-1], r.text[:300])
with tablas.lectura() as cur:
    cur.execute("SELECT COUNT(*) FROM nexus.searchQuestions WHERE conversationId = ? AND tenant <> ?", C1, ORG_A)
    ajenas = cur.fetchone()[0]
rev("…en la bitácora, la conversación de Alfa solo tiene preguntas de Alfa", ajenas == 0, str(ajenas))
for malo in ("conversacion-XYZ", "otra-cosa", "conversacion-" + "A" * 32):
    r = preguntar(a_admin, ORG_A, "¿Cuántos documentos hay?", malo, sql("SELECT COUNT(*) AS [Documentos] FROM consulta.vDocumentoExtraido"))
    rev(f"un id sin la forma ({malo[:20]}…): empieza una conversación nueva, sin error", r.status_code == 200 and r.json().get("turno") == 1 and r.json().get("conversacion") != malo, r.text[:200])
r = c.post("/busqueda/preguntar", json={"tenant": ORG_A, "pregunta": "x", "conversacion": "c" * 61}, headers=cab(a_admin))
rev("…y uno de más de 60 caracteres no pasa del modelo de entrada (422 de validación)", r.status_code == 422, r.text[:200])

# ── [7] Qué se le cuenta al modelo ───────────────────────────────────────────────────────────────────────────────────────────────────
titulo("7 · lo que falló no se le cuenta al modelo; solo las últimas cinco")
r = preguntar(a_admin, ORG_A, "Pregunta base número uno", None, sql(INE_ANO, "Base uno."))
C7 = r.json().get("conversacion")
r = preguntar(a_admin, ORG_A, "la que falla en el modelo", C7, 500)
d = r.json().get("detail", {}) if r.status_code == 502 else {}
rev("el modelo falló: 502 con la conversación y la pregunta 2 en el detalle", r.status_code == 502 and d.get("conversacion") == C7 and d.get("turno") == 2, r.text[:200])
r = preguntar(a_admin, ORG_A, "la que no tiene consulta segura", C7, sql("SELECT x FROM consulta.vDocumentoExtraido"), sql("SELECT y FROM consulta.vDocumentoExtraido"))
d = r.json().get("detail", {}) if r.status_code == 422 else {}
rev("sin consulta segura: 422 consulta_invalida con la conversación y la pregunta 3", d.get("codigo") == "consulta_invalida" and d.get("conversacion") == C7 and d.get("turno") == 3, r.text[:200])
tiempo = consulta.config.CONSULTA_TIEMPO_MAXIMO_S
consulta.config.CONSULTA_TIEMPO_MAXIMO_S = 1
pesada = ("SELECT COUNT_BIG(*) AS n FROM consulta.vDatoExtraido a CROSS JOIN consulta.vDatoExtraido b CROSS JOIN consulta.vDatoExtraido c CROSS JOIN consulta.vDatoExtraido d "
          "CROSS JOIN consulta.vDatoExtraido e CROSS JOIN consulta.vDatoExtraido f CROSS JOIN consulta.vDatoExtraido g CROSS JOIN consulta.vDatoExtraido h WHERE CHECKSUM(a.valor, b.valor, c.valor, d.valor, e.valor, f.valor, g.valor, h.valor) = 7")
r = preguntar(a_admin, ORG_A, "la que tarda", C7, sql(pesada))
consulta.config.CONSULTA_TIEMPO_MAXIMO_S = tiempo
d = r.json().get("detail", {}) if r.status_code == 422 else {}
rev("tardó: 422 tardo con la conversación y la pregunta 4", d.get("codigo") == "tardo" and d.get("conversacion") == C7 and d.get("turno") == 4, r.text[:200])
topes = (config.BUSQUEDA_TOPE_PERSONA_POR_HORA, config.BUSQUEDA_TOPE_ORGANIZACION_POR_DIA)
config.BUSQUEDA_TOPE_PERSONA_POR_HORA = 1
r = preguntar(a_admin, ORG_A, "la que frena el tope", C7)
config.BUSQUEDA_TOPE_PERSONA_POR_HORA, config.BUSQUEDA_TOPE_ORGANIZACION_POR_DIA = topes
d = r.json().get("detail", {}) if r.status_code == 429 else {}
rev("el tope: 429 con la conversación y la pregunta 5 (sin llegar al modelo)", d.get("codigo") == "tope" and d.get("conversacion") == C7 and d.get("turno") == 5, r.text[:200])
r = preguntar(a_admin, ORG_A, "Pregunta base número dos", C7, sql(INE_ANO, "Base dos."))
conv = seccion_conversacion(llamadas[-1])
rev("la siguiente es la pregunta 6, y al modelo solo le llega la base uno: ni la que falló, ni la sin consulta, ni la que tardó, ni la del tope",
    r.json().get("turno") == 6 and "1. «Pregunta base número uno»" in conv and "falla" not in conv and "consulta segura" not in conv and "tarda" not in conv and "tope" not in conv, conv[:500])
for i in range(3, 9):
    preguntar(a_admin, ORG_A, f"Pregunta base número {i}", C7, sql(INE_ANO, f"Base {i}."))
r = preguntar(a_admin, ORG_A, "Pregunta base número nueve", C7, sql(INE_ANO, "Base nueve."))
conv = seccion_conversacion(llamadas[-1])
numeradas = re.findall(r"^\d+\. «([^»]*)»", conv, re.M)
rev("con muchas, solo van las últimas cinco, de la más vieja a la más reciente",
    numeradas == [f"Pregunta base número {i}" for i in (4, 5, 6, 7, 8)], str(numeradas))

# ── [8] La base ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("8 · los CHECK de la 0019 muerden")


def choca(set_sql: str, *params) -> bool:
    try:
        k.execute(f"UPDATE nexus.searchQuestions SET {set_sql} WHERE publicId = ?", *params, uno.get("preguntaId"))
    except Exception as exc:  # noqa: BLE001
        sa.rollback()
        return "CHECK" in str(exc) or "conflicted" in str(exc)
    sa.rollback()
    return False


sa.autocommit = False
rev("conversación sin número", choca("turn = NULL"))
rev("número sin conversación", choca("conversationId = NULL"))
rev("un id sin la forma", choca("conversationId = 'conversacion-xyz'"))
rev("un id con mayúsculas", choca("conversationId = ?", "conversacion-" + "A" * 32))
rev("«tomó de antes» en la pregunta 1", choca("usesContext = 1"))
rev("número 0", choca("turn = 0"))
rev("sugerencias que no son JSON", choca("suggestionsJson = 'no es json'"))
rev("…y lo bueno sí pasa (pregunta 2 que tomó de antes)", not choca("turn = 2, usesContext = 1"))
sa.autocommit = True

print()
print("TODO EN VERDE" if not fallos else f"{fallos} FALLA(S)")
sys.exit(1 if fallos else 0)
