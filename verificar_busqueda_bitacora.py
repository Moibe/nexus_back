"""¿La bitácora, los topes, «¿Te sirvió?», la misma pregunta con la misma consulta y los ejemplos aprobados de la búsqueda (NEX-609) hacen lo que dice el
ticket?

    .venv/Scripts/python verificar_busqueda_bitacora.py

Corre contra el SQL Server desechable de `pruebas_sql.py` (sin Docker se salta y lo dice), con todas las migraciones (la 0018 incluida) y el login de solo
lectura que hizo el DBA (aquí lo crea la prueba). El modelo y el de vectores se sustituyen por un doble que anota lo que le llegaría: NO se llama a Google.
Los vectores del doble son de juguete (cuántas veces sale cada palabra): dos preguntas con las mismas palabras quedan juntas. Lo que se prueba:

1. la bitácora: TODA pregunta con organización y texto válidos deja su renglón (respondida, no se puede, sin consulta segura, tardó, el modelo falló o
   no contestó JSON, sin configurar), con quién, la consulta, de dónde salió, cuántos renglones, cuánto tardó, los tokens y el costo; nunca los renglones
   de la respuesta; lo que no llega (vacía, otra organización) no deja nada;
2. la misma pregunta usa la misma consulta sin llamar al modelo (aunque cambien mayúsculas, acentos y signos), también para otra persona de la
   organización, con los datos de hoy; NO entre organizaciones, ni si el catálogo cambió, ni si la última respuesta «no sirvió»; si ya no corre, se le
   pregunta al modelo;
3. «¿Te sirvió?»: solo lo propio, solo lo que salió bien, se puede cambiar;
4. los ejemplos: «Por revisar», aprobar (Administrador y Supervisor sí; Operador y Viewer no; otra organización no), la misma pregunta usa el ejemplo, una
   parecida lo recibe marcado como aprobado, la otra organización nunca lo ve, descartar, quitar, la auditoría;
5. los topes: por persona por hora y por organización por día, con cuándo se puede volver a preguntar, sin llegar al modelo, y que el tope no cuente;
6. «Tus preguntas recientes»;
7. la elección de ejemplos por significado y lo que pasa si el modelo de vectores falla;
8. lo que Nexus Monitor lee: solo números, con la llave de servicio.
"""

import hashlib
import json
import os
import re
import secrets
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-busqueda-bitacora-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["CORREO_MODO"] = "silencio"
os.environ["DOCAI_PROJECT_ID"] = "proyecto-de-prueba"

import verificacion_comun  # noqa: E402,F401  (fija AUTH_JWT_SECRET)
import pruebas_sql  # noqa: E402

BASE = pruebas_sql.preparar("nexus-busqueda-bitacora", 14345)

DB = BASE["base"]
LECTURA = "usrNexusLectura"
CLAVE = "Lec-" + secrets.token_urlsafe(12) + "-9!"
sa = BASE["conectar"]("sa", BASE["sa"], DB)
k = sa.cursor()
m = BASE["conectar"]("sa", BASE["sa"])
m.cursor().execute(f"IF SUSER_ID('{LECTURA}') IS NULL CREATE LOGIN {LECTURA} WITH PASSWORD = '{CLAVE}', CHECK_POLICY = ON ELSE ALTER LOGIN {LECTURA} WITH PASSWORD = '{CLAVE}'")
k.execute(f"CREATE USER {LECTURA} FOR LOGIN {LECTURA}")
k.execute(f"GRANT SELECT ON SCHEMA::consulta TO {LECTURA}")
carpeta = Path(tempfile.mkdtemp(prefix="busqueda-bitacora-lectura-"))
ARCHIVO_LECTURA = carpeta / "lectura.env"
ARCHIVO_LECTURA.write_text(f"SQLSERVER_USER={LECTURA}\nSQLSERVER_PASSWORD='{CLAVE}'\n", encoding="utf-8")
os.environ["NEXUS_LECTURA_ENV"] = str(ARCHIVO_LECTURA)

from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
from app import app  # noqa: E402
from db import consulta, tablas  # noqa: E402
from servicios import auth, busqueda, embeddings, gemini, usuarios  # noqa: E402
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
a_super = sumar_persona(a_admin, "sara@ejemplo.com", "SUPERVISOR", "Cielo-Abierto-Verde-53!")
a_operador = sumar_persona(a_admin, "olga@ejemplo.com", "OPERADOR", "Cielo-Abierto-Rojo-54!")
a_viewer = sumar_persona(a_admin, "vera@ejemplo.com", "VIEWER", "Cielo-Abierto-Azul-55!")
a_analista = sumar_persona(a_admin, "ana@ejemplo.com", "ANALISTA", "Cielo-Abierto-Gris-56!")


def guid_de(correo: str) -> str:
    return k.execute("SELECT guid FROM nexus.users WHERE email = ?", correo).fetchone()[0]


RITA, VERA, ANA, SARA = guid_de("rita@ejemplo.com"), guid_de("vera@ejemplo.com"), guid_de("ana@ejemplo.com"), guid_de("sara@ejemplo.com")


def campo(nombre: str, tipo_dato: str = "texto", cardinalidad: str = "unico", subcampos: list | None = None) -> dict:
    return {"nombre": nombre, "tipoDato": tipo_dato, "descripcion": "", "obligatorio": False, "cardinalidad": cardinalidad, "valoresLista": [], "subcampos": subcampos or []}


def guardar_tipo(bearer: str, tenant: str, tipo_id: str, nombre: str, campos: list) -> None:
    datos = {"nombre": nombre, "descripcion": f"{nombre} de prueba.", "estado": "activo", "procesadorId": "proc-" + tipo_id[-6:], "campos": campos}
    r = c.put(f"/tipos-documentales/{tipo_id}", json={"tenant": tenant, "revision": 0, "datos": datos}, headers=cab(bearer))
    assert r.status_code == 200, r.text


guardar_tipo(a_admin, ORG_A, "tipo-prueba-ine-01", "INE", [campo("Nombre"), campo("CURP"), campo("Vigencia", "fecha")])
guardar_tipo(a_admin, ORG_A, "tipo-prueba-acta-01", "Acta constitutiva", [campo("Razón social"), campo("Socios", "grupo", "multiple", [campo("Nombre")])])
guardar_tipo(b_admin, ORG_B, "tipo-prueba-pasaporte-01", "Pasaporte", [campo("Número de pasaporte")])


def documento(cur, tenant: str, nombre: str) -> int:
    cur.execute(
        "INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel) OUTPUT INSERTED.documentId VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        f"documento-{secrets.token_hex(16)}", tenant, secrets.token_hex(32), f"{tenant}/aa/aa/{secrets.token_hex(32)}", 1234, "application/pdf", nombre, "MANUAL",
    )
    return cur.fetchone()[0]


def corrida(cur, doc: int, tipo: str, nombre: str) -> int:
    cur.execute(
        "INSERT INTO nexus.extractionRuns (publicId, documentId, documentTypeId, documentTypePublicId, documentTypeName, documentTypeRevision, engine, processorId, "
        "processorVersion, status, errorDetail, confidenceAverage, confidenceMinimum, pageCount, metadataJson, startedAt, createdBy) "
        "OUTPUT INSERTED.runId VALUES (?, ?, NULL, ?, ?, 1, 'document_ai_extractor', 'proc123', NULL, 'ok', NULL, 90, 80, 1, NULL, SYSUTCDATETIME(), 'prueba')",
        f"extraccion-{secrets.token_hex(16)}", doc, tipo, nombre,
    )
    return cur.fetchone()[0]


def dato(cur, run: int, ruta: str, valor: str | None) -> None:
    cur.execute(
        "INSERT INTO nexus.entityFacts (runId, fieldPath, fieldName, recordIndex, valueRaw, valueNormalized, confidence, nullReason, pageNumber) VALUES (?, ?, ?, NULL, ?, ?, 95, ?, 1)",
        run, ruta, ruta, valor, valor, None if valor is not None else "not_present",
    )


ESTE_ANO = date.today().year


def ine(tenant: str, archivo: str, nombre: str, curp: str | None, vigencia: str) -> None:
    with tablas.transaccion() as cur:
        r = corrida(cur, documento(cur, tenant, archivo), "tipo-prueba-ine-01", "INE")
        dato(cur, r, "nombre", nombre)
        dato(cur, r, "curp", curp)
        dato(cur, r, "vigencia", vigencia)


ine(ORG_A, "ine-ana.pdf", "ANA PRUEBA LOPEZ", "PRLA900101MDFXXX01", f"{ESTE_ANO}-12-31")
ine(ORG_A, "ine-juan.pdf", "JUAN PEREZ", None, "2031-06-30")
with tablas.transaccion() as cur:
    r = corrida(cur, documento(cur, ORG_B, "pasaporte-maria.pdf"), "tipo-prueba-pasaporte-01", "Pasaporte")
    dato(cur, r, "numero_de_pasaporte", "G12345678")
VALORES_EXTRAIDOS = ("ANA PRUEBA", "PRLA900101", "JUAN PEREZ", "G12345678", "MARIA")


# ── El doble de Google ────────────────────────────────────────────────────────────────────────────────────────────────────────────────
llamadas: list[str] = []  # el texto de cada llamada al modelo que escribe consultas
vectorizados: list[list[str]] = []  # lo que se le mandó al modelo de vectores, por llamada
guion: list = []
estado = {"vectores_fallan": False}
USO = {"promptTokenCount": 9000, "cachedContentTokenCount": 4000, "candidatesTokenCount": 150, "thoughtsTokenCount": 50, "totalTokenCount": 9200}


def vector_falso(texto: str, dimensiones: int) -> list[float]:
    v = [0.0] * dimensiones
    for palabra in bitacora.clave_de(texto).split():
        v[int(hashlib.sha256(palabra.encode()).hexdigest(), 16) % dimensiones] += 1.0
    return v


async def llamar_falso(url, headers, cuerpo, timeout):
    if url.endswith(":predict"):
        textos = [i["content"] for i in cuerpo["instances"]]
        vectorizados.append(textos)
        if estado["vectores_fallan"]:
            return 500, {"error": {"status": "INTERNAL", "message": "falla de prueba"}}
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


def sql(consulta_sql: str, explicacion: str = "Entendí la pregunta.") -> dict:
    return {"consulta": consulta_sql, "explicacion": explicacion, "noSePuede": None}


def preguntar(bearer: str, tenant: str, pregunta, *respuestas):
    guion[:] = list(respuestas)
    return c.post("/busqueda/preguntar", json={"tenant": tenant, "pregunta": pregunta}, headers=cab(bearer))


def renglon(publico: str) -> dict:
    with tablas.lectura() as cur:
        cur.execute("SELECT * FROM nexus.searchQuestions WHERE publicId = ?", publico)
        return tablas.una(cur) or {}


def ultimo(tenant: str = ORG_A) -> dict:
    with tablas.lectura() as cur:
        cur.execute("SELECT TOP (1) * FROM nexus.searchQuestions WHERE tenant = ? ORDER BY questionId DESC", tenant)
        return tablas.una(cur) or {}


def cuantos(tenant: str | None = None) -> int:
    if tenant:
        return k.execute("SELECT COUNT(*) FROM nexus.searchQuestions WHERE tenant = ?", tenant).fetchone()[0]
    return k.execute("SELECT COUNT(*) FROM nexus.searchQuestions").fetchone()[0]


VENCEN = "SELECT COUNT(DISTINCT documento) AS [INE que vencen] FROM consulta.vDatoExtraido WHERE tipoDocumental = 'INE' AND campo = 'vigencia' AND YEAR(valorFecha) = YEAR(GETDATE())"
POR_TIPO = "SELECT tipoDocumental AS [Tipo documental], COUNT(*) AS [Documentos] FROM consulta.vDocumentoExtraido GROUP BY tipoDocumental ORDER BY tipoDocumental"
CORREGIDOS = "SELECT documento, archivo AS [Archivo] FROM consulta.vDocumentoExtraido WHERE camposCorregidos > 0"

# ── [1] La bitácora ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("1 · la bitácora: toda pregunta deja su renglón")
r = preguntar(a_admin, ORG_A, "¿Cuántas INE vencen este año?", sql(VENCEN, "Conté las INE que vencen este año."))
cuerpo = r.json() if r.status_code == 200 else {}
rev("respondida: 200 con preguntaId, origen modelo, sin reuso", r.status_code == 200 and re.fullmatch(r"pregunta-[0-9a-f]{32}", cuerpo.get("preguntaId") or "") and cuerpo.get("origen") == "modelo" and cuerpo.get("reusoDe") is None and cuerpo.get("filas") == [[1]], r.text[:300])
P_VENCEN = cuerpo.get("preguntaId")
g = renglon(P_VENCEN)
rev("…su renglón: quién, la pregunta, ok, la consulta, de dónde salió, cuántos renglones, intentos, tiempo, modelo",
    g.get("tenant") == ORG_A and g.get("userGuid") == RITA and g.get("question") == "¿Cuántas INE vencen este año?" and g.get("questionKey") == "cuantas ine vencen este ano"
    and g.get("outcome") == "ok" and g.get("sqlText") == VENCEN and g.get("origin") == "modelo" and g.get("resultRows") == 1 and g.get("hasMore") is False
    and g.get("attempts") == 1 and g.get("durationMs", -1) >= 0 and g.get("databaseMs") is not None and g.get("model") == config.GEMINI_MODELO
    and g.get("explanation") == "Conté las INE que vencen este año.", str({x: g.get(x) for x in ("outcome", "origin", "resultRows", "attempts", "model")}))
esperado = round(bitacora.costo(9000, 4000, 200, g.get("embeddingTokens") or 0), 6)
rev(f"…los tokens (entrada 9000, de caché 4000, salida 150 + 50 que pensó) y el costo estimado ({esperado} USD)",
    g.get("inputTokens") == 9000 and g.get("cachedTokens") == 4000 and g.get("outputTokens") == 200 and (g.get("embeddingTokens") or 0) > 0
    and float(g.get("costUsd")) == esperado, str({x: g.get(x) for x in ("inputTokens", "cachedTokens", "outputTokens", "embeddingTokens", "costUsd")}))
ejemplos_dados = json.loads(g.get("examplesJson") or "null")
rev(f"…qué ejemplos se le dieron (los {config.BUSQUEDA_EJEMPLOS_POR_PREGUNTA} más parecidos) y la huella del catálogo",
    isinstance(ejemplos_dados, list) and len(ejemplos_dados) == config.BUSQUEDA_EJEMPLOS_POR_PREGUNTA and ejemplos_dados[-1] == "comun-04"
    and re.fullmatch(r"[0-9a-f]{64}", g.get("catalogHash") or ""), str(ejemplos_dados))
fila_texto = json.dumps({x: str(v) for x, v in g.items()}, ensure_ascii=False)
rev("…y NINGÚN renglón de la respuesta ni dato extraído", not any(v in fila_texto for v in VALORES_EXTRAIDOS), fila_texto[:300])

antes = cuantos()
r = preguntar(a_admin, ORG_A, "¿Qué hora es en Tokio?", {"consulta": None, "explicacion": "", "noSePuede": "No tiene que ver con tus documentos."})
g = ultimo()
rev("no se puede: 422 y su renglón con el motivo, sin consulta", r.status_code == 422 and g.get("outcome") == "no_se_puede" and g.get("detail") == "No tiene que ver con tus documentos." and g.get("sqlText") is None and g.get("inputTokens") == 9000, str(g.get("outcome")))
r = preguntar(a_admin, ORG_A, "dame la columna x", sql("SELECT x FROM consulta.vDocumentoExtraido"), sql("SELECT y FROM consulta.vDocumentoExtraido"))
g = ultimo()
rev("sin consulta segura (la base la rechazó dos veces): su renglón con 2 intentos, la última consulta intentada y el doble de tokens",
    r.status_code == 422 and g.get("outcome") == "consulta_invalida" and g.get("attempts") == 2 and g.get("sqlText") == "SELECT y FROM consulta.vDocumentoExtraido"
    and g.get("inputTokens") == 18000 and "Invalid column name" in (g.get("detail") or ""), str({x: g.get(x) for x in ("outcome", "attempts", "detail")}))
r = preguntar(a_admin, ORG_A, "falla el modelo", 500)
g = ultimo()
rev("el modelo falló: 502 y su renglón «modelo», sin tokens del modelo", r.status_code == 502 and g.get("outcome") == "modelo" and not g.get("inputTokens") and not g.get("outputTokens"), str({x: g.get(x) for x in ("outcome", "inputTokens")}))
r = preguntar(a_admin, ORG_A, "contesta texto", "esto no es JSON")
g = ultimo()
rev("el modelo no contestó JSON: 502, «modelo» y SÍ con sus tokens (se pagaron)", r.status_code == 502 and g.get("outcome") == "modelo" and g.get("inputTokens") == 9000, str(g.get("outcome")))
os.environ["NEXUS_LECTURA_ENV"] = str(carpeta / "no-existe.env")
r = preguntar(a_admin, ORG_A, "sin conexión de lectura", sql(POR_TIPO))
os.environ["NEXUS_LECTURA_ENV"] = str(ARCHIVO_LECTURA)
g = ultimo()
rev("sin la conexión de solo lectura: 503 y su renglón «sin_configurar»", r.status_code == 503 and g.get("outcome") == "sin_configurar", str(g.get("outcome")))
tiempo = consulta.config.CONSULTA_TIEMPO_MAXIMO_S
consulta.config.CONSULTA_TIEMPO_MAXIMO_S = 1
pesada = ("SELECT COUNT_BIG(*) AS n FROM consulta.vDatoExtraido a CROSS JOIN consulta.vDatoExtraido b CROSS JOIN consulta.vDatoExtraido c CROSS JOIN consulta.vDatoExtraido d "
          "CROSS JOIN consulta.vDatoExtraido e CROSS JOIN consulta.vDatoExtraido f CROSS JOIN consulta.vDatoExtraido g CROSS JOIN consulta.vDatoExtraido h WHERE CHECKSUM(a.valor, b.valor, c.valor, d.valor, e.valor, f.valor, g.valor, h.valor) = 7")
r = preguntar(a_admin, ORG_A, "todo contra todo", sql(pesada))
consulta.config.CONSULTA_TIEMPO_MAXIMO_S = tiempo
g = ultimo()
rev("tardó demasiado: 422 y su renglón «tardo»", r.status_code == 422 and g.get("outcome") == "tardo", str(g.get("outcome")))
rev("cada una dejó exactamente un renglón", cuantos() == antes + 6, f"{cuantos() - antes}")
antes = cuantos()
preguntar(a_admin, ORG_A, "   ")
preguntar(a_admin, ORG_B, "¿Cuántos documentos hay?")
preguntar(bearer_super, "plataforma", "¿Cuántos documentos hay?")
rev("lo que no llega (pregunta vacía, otra organización, el espacio de plataforma) no deja renglón", cuantos() == antes, f"{cuantos() - antes}")

# ── [2] La misma pregunta, la misma consulta ─────────────────────────────────────────────────────────────────────────────────────────
titulo("2 · la misma pregunta usa la misma consulta")
n_modelo = len(llamadas)
r = preguntar(a_admin, ORG_A, "  cuantas INE vencen  este AÑO ")
cuerpo = r.json() if r.status_code == 200 else {}
rev("la misma (otras mayúsculas, sin acentos ni signos): 200 con la MISMA consulta, origen «anterior», sin llamar al modelo ni sacar vectores",
    r.status_code == 200 and cuerpo.get("consulta") == VENCEN and cuerpo.get("origen") == "anterior" and cuerpo.get("reusoDe") == P_VENCEN
    and cuerpo.get("intentos") == 0 and cuerpo.get("filas") == [[1]] and len(llamadas) == n_modelo, r.text[:300])
g = renglon(cuerpo.get("preguntaId") or "")
rev("…la bitácora dice que se reusó (de cuál), sin tokens y costo 0", g.get("origin") == "anterior" and g.get("reusedFrom") == P_VENCEN and g.get("attempts") == 0 and g.get("inputTokens") is None and float(g.get("costUsd") or 0) == 0, str({x: g.get(x) for x in ("origin", "reusedFrom", "costUsd")}))
ine(ORG_A, "ine-luisa.pdf", "LUISA PRUEBA", "PRLU900101MDFXXX02", f"{ESTE_ANO}-03-01")
r = preguntar(a_viewer, ORG_A, "¿Cuántas INE vencen este año?")
rev("otra persona de la organización (Viewer) también la reusa, con los datos de HOY (ya son 2)", r.status_code == 200 and r.json()["origen"] == "anterior" and r.json()["filas"] == [[2]] and len(llamadas) == n_modelo, r.text[:300])
P_VENCEN_VERA = r.json().get("preguntaId")
r = preguntar(b_admin, ORG_B, "¿Cuántas INE vencen este año?", sql(VENCEN))
rev("otra organización NO reusa la de A: se le pregunta al modelo", r.status_code == 200 and r.json()["origen"] == "modelo" and len(llamadas) == n_modelo + 1 and r.json()["filas"] == [[0]], r.text[:300])
rv = c.post(f"/busqueda/preguntas/{P_VENCEN_VERA}/valoracion", json={"tenant": ORG_A, "util": False}, headers=cab(a_viewer))
r = preguntar(a_admin, ORG_A, "¿cuántas INE vencen este año?", sql(VENCEN, "Otra vez."))
rev("si la última respuesta a esa pregunta «no sirvió», se le vuelve a preguntar al modelo", rv.status_code == 200 and r.status_code == 200 and r.json()["origen"] == "modelo" and len(llamadas) == n_modelo + 2, r.text[:200])
r = preguntar(a_admin, ORG_A, "¿Cuántas INE vencen este año?")
rev("…y la nueva (que salió bien) ya se reusa", r.status_code == 200 and r.json()["origen"] == "anterior" and len(llamadas) == n_modelo + 2, r.text[:200])
guardar_tipo(a_admin, ORG_A, "tipo-prueba-poder-01", "Carta poder", [campo("Otorgante")])
r = preguntar(a_admin, ORG_A, "¿Cuántas INE vencen este año?", sql(VENCEN, "Con el catálogo nuevo."))
rev("si el catálogo cambió (un tipo nuevo), NO se reusa: se le pregunta al modelo con el catálogo de hoy", r.status_code == 200 and r.json()["origen"] == "modelo" and "«Carta poder»" in llamadas[-1], r.text[:200])
P_NUEVA = r.json().get("preguntaId")
k.execute("UPDATE nexus.searchQuestions SET sqlText = 'SELECT columnaQueYaNoExiste FROM consulta.vDocumentoExtraido' WHERE publicId = ?", P_NUEVA)
r = preguntar(a_admin, ORG_A, "¿Cuántas INE vencen este año?", sql(VENCEN, "Corregida."))
rev("si la consulta que se reusaría ya no corre, se le pregunta al modelo (y sale bien)", r.status_code == 200 and r.json()["origen"] == "modelo" and r.json()["filas"] == [[2]], r.text[:200])

# ── [3] ¿Te sirvió? ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("3 · «¿Te sirvió?»")
r = preguntar(a_admin, ORG_A, "¿Cuántos documentos hay por tipo?", sql(POR_TIPO, "Conté por tipo."))
P_TIPO = r.json()["preguntaId"]


def valorar(bearer: str, tenant: str, pregunta_id: str, util):
    return c.post(f"/busqueda/preguntas/{pregunta_id}/valoracion", json={"tenant": tenant, "util": util}, headers=cab(bearer))


rv = valorar(a_admin, ORG_A, P_TIPO, True)
rev("la propia: 200 y queda marcada como útil", rv.status_code == 200 and rv.json() == {"preguntaId": P_TIPO, "util": True} and renglon(P_TIPO).get("useful") is True and renglon(P_TIPO).get("ratedAt") is not None, rv.text)
rv = valorar(a_admin, ORG_A, P_TIPO, False)
rev("se puede cambiar (vale la última)", rv.status_code == 200 and renglon(P_TIPO).get("useful") is False)
valorar(a_admin, ORG_A, P_TIPO, True)
rv = valorar(a_viewer, ORG_A, P_TIPO, False)
rev("la de otra persona: 404, y no cambia", rv.status_code == 404 and rv.json()["detail"]["codigo"] == "pregunta_no_existe" and renglon(P_TIPO).get("useful") is True, rv.text)
rv = valorar(b_admin, ORG_A, P_TIPO, False)
rev("desde otra organización: 403 sin_acceso", rv.status_code == 403 and rv.json()["detail"]["codigo"] == "sin_acceso", rv.text)
P_TOKIO = k.execute("SELECT TOP (1) publicId FROM nexus.searchQuestions WHERE outcome = 'no_se_puede' AND userGuid = ?", RITA).fetchone()[0]
rv = valorar(a_admin, ORG_A, P_TOKIO, True)
rev("una que no salió bien: 409 no_valorable", rv.status_code == 409 and rv.json()["detail"]["codigo"] == "no_valorable", rv.text)
rv = valorar(a_admin, ORG_A, "pregunta-" + "0" * 32, True)
rev("una que no existe: 404", rv.status_code == 404)

# ── [4] Ejemplos ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("4 · ejemplos aprobados por la organización")
r = preguntar(a_viewer, ORG_A, "¿cuántos documentos hay por tipo?")  # reusa la de Rita: otra persona a la que le sirve
P_TIPO_VERA = r.json()["preguntaId"]
valorar(a_viewer, ORG_A, P_TIPO_VERA, True)
r = preguntar(a_admin, ORG_A, "¿Qué documentos tienen campos corregidos?", sql(CORREGIDOS, "Busqué los corregidos."))
P_CORR = r.json()["preguntaId"]
valorar(a_admin, ORG_A, P_CORR, True)
rr = c.get(f"/busqueda/por-revisar?tenant={ORG_A}", headers=cab(a_admin))
lista = rr.json().get("preguntas", []) if rr.status_code == 200 else []
por_clave = {bitacora.clave_de(x["pregunta"]): x for x in lista}
tipo = por_clave.get("cuantos documentos hay por tipo", {})
rev("«Por revisar» (Administrador): una por pregunta, la más reciente, con cuántas veces se hizo, a cuántas personas les sirvió y quién",
    rr.status_code == 200 and len(lista) == 2 and tipo.get("id") == P_TIPO_VERA and tipo.get("personasUtil") == 2 and tipo.get("veces") == 2
    and tipo.get("consulta") == POR_TIPO and (tipo.get("autor") or {}).get("nombre") == "Viewer Prueba" and "cuantas ine vencen este ano" not in por_clave, json.dumps(lista, ensure_ascii=False)[:400])
for quien, bearer in (("Operador", a_operador), ("Viewer", a_viewer), ("Analista", a_analista)):
    rr = c.get(f"/busqueda/por-revisar?tenant={ORG_A}", headers=cab(bearer))
    rev(f"«Por revisar» como {quien}: 403 sin_permiso", rr.status_code == 403 and rr.json()["detail"]["codigo"] == "sin_permiso", rr.text[:200])
rr = c.get(f"/busqueda/por-revisar?tenant={ORG_A}", headers=cab(b_admin))
rev("…como Administrador de OTRA organización: 403 sin_acceso", rr.status_code == 403 and rr.json()["detail"]["codigo"] == "sin_acceso")
negadas = k.execute("SELECT COUNT(*) FROM nexus.auditEvents WHERE action = 'ROLE_DENIED' AND tenant = ? AND dataJson LIKE '%busqueda_ejemplos%'", ORG_A).fetchone()[0]
rev("…y los rechazos por rol quedan en la auditoría (ROLE_DENIED)", negadas >= 3, str(negadas))

ra = c.post("/busqueda/ejemplos", json={"tenant": ORG_A, "preguntaId": P_TIPO_VERA}, headers=cab(a_operador))
rev("aprobar como Operador: 403 sin_permiso, y no se crea", ra.status_code == 403 and k.execute("SELECT COUNT(*) FROM nexus.searchExamples").fetchone()[0] == 0, ra.text[:200])
ra = c.post("/busqueda/ejemplos", json={"tenant": ORG_B, "preguntaId": P_TIPO_VERA}, headers=cab(b_admin))
rev("aprobar una pregunta de A desde B: 404", ra.status_code == 404)
ra = c.post("/busqueda/ejemplos", json={"tenant": ORG_A, "preguntaId": P_TOKIO}, headers=cab(a_admin))
rev("aprobar una que no salió bien: 409 no_valorable", ra.status_code == 409 and ra.json()["detail"]["codigo"] == "no_valorable")
n_vect = len(vectorizados)
ra = c.post("/busqueda/ejemplos", json={"tenant": ORG_A, "preguntaId": P_TIPO_VERA}, headers=cab(a_super))
ej = ra.json() if ra.status_code == 201 else {}
E_TIPO = ej.get("id")
fila = k.execute("SELECT tenant, question, sqlText, approvedBy, embeddingJson, embeddingModel FROM nexus.searchExamples WHERE publicId = ?", E_TIPO or "").fetchone()
rev("aprobar como Supervisor: 201 con su id, la pregunta y la consulta; queda con quién lo aprobó y su vector",
    ra.status_code == 201 and re.fullmatch(r"ejemplo-[0-9a-f]{32}", E_TIPO or "") and ej.get("consulta") == POR_TIPO and fila is not None
    and fila[0] == ORG_A and fila[3] == SARA and fila[4] and fila[5] == embeddings.etiqueta() and len(vectorizados) == n_vect + 1, ra.text[:300])
audit = k.execute("SELECT COUNT(*) FROM nexus.auditEvents WHERE action = 'SEARCH_EXAMPLE_APPROVED' AND tenant = ? AND userGuid = ?", ORG_A, SARA).fetchone()[0]
rev("…y queda en la auditoría (SEARCH_EXAMPLE_APPROVED)", audit == 1)
ra = c.post("/busqueda/ejemplos", json={"tenant": ORG_A, "preguntaId": P_TIPO}, headers=cab(a_admin))
rev("aprobar otra vez la misma pregunta (otra vez que se hizo): 409 ya_es_ejemplo, con el id del que ya existe", ra.status_code == 409 and ra.json()["detail"]["codigo"] == "ya_es_ejemplo" and ra.json()["detail"]["ejemploId"] == E_TIPO, ra.text[:200])
lista = c.get(f"/busqueda/por-revisar?tenant={ORG_A}", headers=cab(a_admin)).json()["preguntas"]
rev("ya no sale en «Por revisar»", [bitacora.clave_de(x["pregunta"]) for x in lista] == ["que documentos tienen campos corregidos"], str([x["pregunta"] for x in lista]))

n_modelo = len(llamadas)
r = preguntar(a_analista, ORG_A, "Cuántos documentos hay por tipo")
rev("la misma pregunta usa el EJEMPLO: origen «ejemplo», de cuál, sin llamar al modelo", r.status_code == 200 and r.json()["origen"] == "ejemplo" and r.json()["reusoDe"] == E_TIPO and len(llamadas) == n_modelo and r.json()["consulta"] == POR_TIPO, r.text[:300])
rev("…la bitácora dice que se reusó el ejemplo", renglon(r.json()["preguntaId"]).get("reusedFrom") == E_TIPO)
r = preguntar(a_analista, ORG_A, "¿cuántos documentos hay por tipo este mes?", sql(POR_TIPO + " ", "Por tipo, este mes."))
texto = llamadas[-1] if len(llamadas) > n_modelo else ""
rev("una PARECIDA va al modelo y recibe el ejemplo aprobado, marcado como tal y al final (el más parecido)",
    r.status_code == 200 and "- [aprobado por la organización] ¿cuántos documentos hay por tipo?" in texto
    and texto.split("\n\nPregunta:")[0].rstrip().split("\n- ")[-1].startswith("[aprobado por la organización]"), texto[texto.find("Ejemplos de preguntas"):][-700:])
rev("…y la bitácora anota que ese ejemplo se le dio", E_TIPO in json.loads(renglon(r.json()["preguntaId"]).get("examplesJson") or "[]"))
r = preguntar(b_admin, ORG_B, "¿cuántos documentos hay por tipo este mes?", sql(POR_TIPO))
rev("en OTRA organización la misma pregunta parecida NO recibe el ejemplo de A, ni lo reusa",
    r.status_code == 200 and r.json()["origen"] == "modelo" and "[aprobado por la organización]" not in llamadas[-1] and E_TIPO not in (ultimo(ORG_B).get("examplesJson") or ""), r.text[:200])

re_ = c.get(f"/busqueda/ejemplos?tenant={ORG_A}", headers=cab(a_admin))
lista_ej = re_.json().get("ejemplos", []) if re_.status_code == 200 else []
rev("la lista de ejemplos (Administrador): con quién lo aprobó y cuántas veces se reusó su consulta",
    re_.status_code == 200 and len(lista_ej) == 1 and lista_ej[0]["id"] == E_TIPO and lista_ej[0]["reusos"] == 1 and (lista_ej[0]["aprobadoPor"] or {}).get("guid") == SARA, re_.text[:300])
rev("…como Viewer: 403", c.get(f"/busqueda/ejemplos?tenant={ORG_A}", headers=cab(a_viewer)).status_code == 403)
rev("…de B: vacía", c.get(f"/busqueda/ejemplos?tenant={ORG_B}", headers=cab(b_admin)).json() == {"ejemplos": []})

rd = c.post(f"/busqueda/preguntas/{P_CORR}/descartar", json={"tenant": ORG_A}, headers=cab(a_viewer))
rev("descartar como Viewer: 403", rd.status_code == 403)
rd = c.post(f"/busqueda/preguntas/{P_CORR}/descartar", json={"tenant": ORG_A}, headers=cab(a_admin))
rev("descartar (Administrador): sale de «Por revisar»", rd.status_code == 200 and rd.json()["descartadas"] == 1 and c.get(f"/busqueda/por-revisar?tenant={ORG_A}", headers=cab(a_admin)).json()["preguntas"] == [], rd.text)
r = preguntar(a_viewer, ORG_A, "¿Qué documentos tienen campos corregidos?")
valorar(a_viewer, ORG_A, r.json()["preguntaId"], True)
lista = c.get(f"/busqueda/por-revisar?tenant={ORG_A}", headers=cab(a_admin)).json()["preguntas"]
rev("…vuelve si alguien más la marca útil después", len(lista) == 1 and lista[0]["id"] == r.json()["preguntaId"] and lista[0]["personasUtil"] == 1, str(lista))

rq = c.delete(f"/busqueda/ejemplos/{E_TIPO}?tenant={ORG_A}", headers=cab(a_operador))
rev("quitar un ejemplo como Operador: 403", rq.status_code == 403)
rq = c.delete(f"/busqueda/ejemplos/{E_TIPO}?tenant={ORG_B}", headers=cab(b_admin))
rev("quitar el de A desde B: 404", rq.status_code == 404)
rq = c.delete(f"/busqueda/ejemplos/{E_TIPO}?tenant={ORG_A}", headers=cab(a_admin))
rev("quitarlo (Administrador): 200; ya no está y queda en la auditoría (SEARCH_EXAMPLE_REMOVED)",
    rq.status_code == 200 and k.execute("SELECT COUNT(*) FROM nexus.searchExamples WHERE publicId = ?", E_TIPO).fetchone()[0] == 0
    and k.execute("SELECT COUNT(*) FROM nexus.auditEvents WHERE action = 'SEARCH_EXAMPLE_REMOVED' AND tenant = ?", ORG_A).fetchone()[0] == 1, rq.text)
rev("…y su pregunta no regresa sola a «Por revisar»", all(bitacora.clave_de(x["pregunta"]) != "cuantos documentos hay por tipo" for x in c.get(f"/busqueda/por-revisar?tenant={ORG_A}", headers=cab(a_admin)).json()["preguntas"]))
n_modelo = len(llamadas)
r = preguntar(a_analista, ORG_A, "¿Cuántos documentos hay por tipo este mes, otra vez?", sql(POR_TIPO))
rev("…y ya no se le da al modelo", r.status_code == 200 and "[aprobado por la organización]" not in llamadas[-1] and E_TIPO not in (ultimo().get("examplesJson") or ""))
rev("quitar uno que no existe: 404", c.delete(f"/busqueda/ejemplos/ejemplo-{'0' * 32}?tenant={ORG_A}", headers=cab(a_admin)).status_code == 404)

# ── [5] Topes ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("5 · topes de uso")
config.BUSQUEDA_TOPE_PERSONA_POR_HORA = 3
hechas = k.execute("SELECT COUNT(*) FROM nexus.searchQuestions WHERE userGuid = ? AND outcome <> 'tope' AND createdAt > DATEADD(minute, -60, SYSUTCDATETIME())", ANA).fetchone()[0]
for i in range(max(0, 3 - hechas)):
    preguntar(a_analista, ORG_A, f"pregunta de relleno {i}", sql(POR_TIPO))
ahora = datetime.now(timezone.utc).replace(tzinfo=None)
ids = [f[0] for f in k.execute("SELECT questionId FROM nexus.searchQuestions WHERE userGuid = ? AND outcome <> 'tope' ORDER BY questionId", ANA).fetchall()]
for qid, minutos in zip(ids[-3:], (50, 40, 30)):
    k.execute("UPDATE nexus.searchQuestions SET createdAt = ? WHERE questionId = ?", ahora - timedelta(minutes=minutos), qid)
for qid in ids[:-3]:
    k.execute("UPDATE nexus.searchQuestions SET createdAt = ? WHERE questionId = ?", ahora - timedelta(minutes=120), qid)
n_modelo = len(llamadas)
r = preguntar(a_analista, ORG_A, "una más", sql(POR_TIPO))
d = r.json().get("detail", {}) if r.status_code == 429 else {}
reintentar = datetime.strptime(d.get("reintentarEn", "2000-01-01T00:00:00Z"), "%Y-%m-%dT%H:%M:%SZ")
rev("la cuarta en una hora (tope 3): 429 tope por persona, por hora, y NO llega al modelo",
    r.status_code == 429 and d.get("codigo") == "tope" and d.get("alcance") == "persona" and d.get("tope") == 3 and d.get("ventana") == "hora"
    and d.get("mensaje") == "Llegaste al tope de 3 preguntas por hora." and len(llamadas) == n_modelo, r.text[:300])
rev(f"…dice cuándo se puede volver a preguntar: cuando la más vieja salga de la hora (≈ en 10 min; dijo {d.get('reintentarEn')})", abs((reintentar - (ahora + timedelta(minutes=10))).total_seconds()) < 5)
rev("…y queda en la bitácora como «tope»", ultimo().get("outcome") == "tope" and ultimo().get("userGuid") == ANA and ultimo().get("sqlText") is None)
r2 = preguntar(a_analista, ORG_A, "y otra", sql(POR_TIPO))
rev("insistir no alarga la espera (lo que frenó el tope no cuenta)", r2.status_code == 429 and r2.json()["detail"]["reintentarEn"] == d.get("reintentarEn"), r2.text[:200])
r = preguntar(a_super, ORG_A, "¿Cuántas INE vencen este año?")
rev("otra persona de la organización sí puede preguntar", r.status_code == 200, r.text[:200])
k.execute("UPDATE nexus.searchQuestions SET createdAt = ? WHERE questionId = ?", ahora - timedelta(minutes=61), ids[-3])
r = preguntar(a_analista, ORG_A, "ahora sí", sql(POR_TIPO))
rev("cuando la más vieja sale de la hora, ya puede", r.status_code == 200, r.text[:200])
config.BUSQUEDA_TOPE_PERSONA_POR_HORA = 10_000
hechas_org = k.execute("SELECT COUNT(*) FROM nexus.searchQuestions WHERE tenant = ? AND outcome <> 'tope' AND createdAt > DATEADD(minute, -1440, SYSUTCDATETIME())", ORG_A).fetchone()[0]
config.BUSQUEDA_TOPE_ORGANIZACION_POR_DIA = hechas_org
n_modelo = len(llamadas)
r = preguntar(a_admin, ORG_A, "¿y por organización?", sql(POR_TIPO))
d = r.json().get("detail", {}) if r.status_code == 429 else {}
rev(f"la organización llegó a su tope por día ({hechas_org}): 429 por organización, por día, sin llegar al modelo",
    r.status_code == 429 and d.get("alcance") == "organizacion" and d.get("ventana") == "dia" and d.get("mensaje") == f"Tu organización llegó al tope de {hechas_org} preguntas por día." and len(llamadas) == n_modelo, r.text[:300])
r = preguntar(b_admin, ORG_B, "¿Cuántos documentos hay por tipo?", sql(POR_TIPO))
rev("…la otra organización sigue preguntando", r.status_code == 200, r.text[:200])
config.BUSQUEDA_TOPE_ORGANIZACION_POR_DIA = 10_000

# ── [6] Tus preguntas recientes ───────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("6 · tus preguntas recientes")
rr = c.get(f"/busqueda/preguntas?tenant={ORG_A}&limite=20", headers=cab(a_analista))
mias = rr.json().get("preguntas", []) if rr.status_code == 200 else []
textos = [x["pregunta"] for x in mias]
rev("las de quien pide, distintas, la más reciente primero, sin las que frenó el tope",
    rr.status_code == 200 and textos[0] == "ahora sí" and "una más" not in textos and "y otra" not in textos and len(textos) == len({bitacora.clave_de(t) for t in textos})
    and all(re.fullmatch(r"pregunta-[0-9a-f]{32}", x["id"]) and x["en"] for x in mias), str(textos))
rev("…nunca las de otra persona", "¿Qué hora es en Tokio?" not in textos)
rr = c.get(f"/busqueda/preguntas?tenant={ORG_A}&limite=2", headers=cab(a_analista))
rev("…con límite", len(rr.json()["preguntas"]) == 2)
rev("…desde otra organización: 403", c.get(f"/busqueda/preguntas?tenant={ORG_A}", headers=cab(b_admin)).status_code == 403)

# ── [7] Los ejemplos por significado ──────────────────────────────────────────────────────────────────────────────────────────────────
titulo("7 · los ejemplos más parecidos por significado")
r = preguntar(a_admin, ORG_A, "dame la lista de las INE con nombre y CURP", sql("SELECT documento, archivo AS [Archivo] FROM consulta.vDocumentoExtraido WHERE tipoDocumental = 'INE'"))
dados = json.loads(ultimo().get("examplesJson") or "[]")
texto = llamadas[-1]
rev(f"van {config.BUSQUEDA_EJEMPLOS_POR_PREGUNTA} (no los 21) y el más parecido al final, junto a la pregunta",
    len(dados) == config.BUSQUEDA_EJEMPLOS_POR_PREGUNTA and dados[-1] == "comun-10"
    and texto.split("Ejemplos de preguntas")[1].split("\n\nPregunta:")[0].count("\n- ") == config.BUSQUEDA_EJEMPLOS_POR_PREGUNTA, str(dados))
# Un ejemplo aprobado SIN vector (Google no contestó al aprobar) se vectoriza solo en la siguiente pregunta y se guarda.
r = preguntar(a_admin, ORG_A, "¿Cuántos socios tiene cada acta?", sql("SELECT documento, archivo AS [Archivo] FROM consulta.vDocumentoExtraido WHERE tipoDocumental = 'Acta constitutiva'"))
P_SOCIOS = r.json()["preguntaId"]
estado["vectores_fallan"] = True
ra = c.post("/busqueda/ejemplos", json={"tenant": ORG_A, "preguntaId": P_SOCIOS}, headers=cab(a_admin))
E_SOCIOS = ra.json().get("id") if ra.status_code == 201 else None
rev("aprobar cuando el modelo de vectores falla: se aprueba igual (201), sin vector por ahora",
    ra.status_code == 201 and k.execute("SELECT embeddingJson FROM nexus.searchExamples WHERE publicId = ?", E_SOCIOS or "").fetchone()[0] is None, ra.text[:200])
r = preguntar(a_admin, ORG_A, "¿qué INE ya vencieron?", sql(VENCEN))
dados = json.loads(ultimo().get("examplesJson") or "[]")
rev(f"si los vectores fallan al preguntar, la pregunta sale igual con los {len(busqueda.EJEMPLOS)} comunes y los aprobados recientes",
    r.status_code == 200 and len(dados) == len(busqueda.EJEMPLOS) + 1 and dados[-1] == E_SOCIOS and (ultimo().get("embeddingTokens") or 0) == 0, str(dados[-3:]))
estado["vectores_fallan"] = False
n_vect = len(vectorizados)
r = preguntar(a_admin, ORG_A, "¿cuántos socios tiene cada acta hoy?", sql("SELECT documento FROM consulta.vDocumentoExtraido"))
dados = json.loads(ultimo().get("examplesJson") or "[]")
guardado = k.execute("SELECT embeddingModel FROM nexus.searchExamples WHERE publicId = ?", E_SOCIOS).fetchone()[0]
rev("cuando vuelven, el ejemplo sin vector se vectoriza en esa misma llamada, se guarda y sale elegido (es el más parecido)",
    r.status_code == 200 and guardado == embeddings.etiqueta() and dados[-1] == E_SOCIOS and any("Cuántos socios tiene cada acta" in t for t in sum(vectorizados[n_vect:], [])), str(dados[-3:]))
n_vect = len(vectorizados)
preguntar(a_admin, ORG_A, "¿cuántos socios hay por acta?", sql("SELECT documento FROM consulta.vDocumentoExtraido"))
rev("…y ya no se vuelve a sacar (solo el de la pregunta)", [len(x) for x in vectorizados[n_vect:]] == [1], str(vectorizados[n_vect:]))

# ── [8] Nexus Monitor ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("8 · lo que lee Nexus Monitor")
rm = c.get("/monitor-busqueda/?dias=30", headers={"x-api-key": SERVICIO})
p = rm.json() if rm.status_code == 200 else {}
orgs = {o["guid"]: o for o in p.get("organizaciones", [])}
a = orgs.get(ORG_A, {})


def contar(where: str, *args) -> int:
    return k.execute(f"SELECT COUNT(*) FROM nexus.searchQuestions WHERE tenant = ? AND {where}", ORG_A, *args).fetchone()[0]


costo_a = float(k.execute("SELECT SUM(costUsd) FROM nexus.searchQuestions WHERE tenant = ?", ORG_A).fetchone()[0])
rev("con solo la llave de servicio: 200, por organización, con sus números igual que la bitácora",
    rm.status_code == 200 and a.get("nombre") == "Alfa" and a.get("preguntas") == cuantos(ORG_A) and a.get("respondidas") == contar("outcome = 'ok'")
    and a.get("fallidas") == contar("outcome IN ('consulta_invalida', 'tardo', 'modelo', 'sin_configurar', 'error')") and a.get("noSePuede") == contar("outcome = 'no_se_puede'")
    and a.get("topadas") == contar("outcome = 'tope'") and a.get("reusadas") == contar("outcome = 'ok' AND origin IN ('ejemplo', 'anterior')")
    and abs(a.get("costoUsd", -1) - costo_a) < 1e-6 and a.get("personas") == k.execute("SELECT COUNT(DISTINCT userGuid) FROM nexus.searchQuestions WHERE tenant = ?", ORG_A).fetchone()[0]
    and a.get("ejemplos") == 1 and a.get("msPromedio") is not None,
    json.dumps(a, ensure_ascii=False)[:500])
rev("…y la otra organización con lo suyo", orgs.get(ORG_B, {}).get("preguntas") == cuantos(ORG_B))
rev("…totales, desglose por resultado, topes y precios", p.get("totales", {}).get("preguntas") == cuantos() and sum(p.get("porResultado", {}).values()) == cuantos()
    and p.get("topes") == {"personaPorHora": 10_000, "organizacionPorDia": 10_000} and p.get("precios", {}).get("entradaUsdMillon") == config.GEMINI_PRECIO_ENTRADA_USD_MILLON)
rev("…solo números: ni el texto de las preguntas ni sus consultas", "Tokio" not in rm.text and "vencen" not in rm.text and "SELECT" not in rm.text)
rev("sin la llave de servicio: 401", c.get("/monitor-busqueda/").status_code == 401)
k.execute("UPDATE nexus.searchQuestions SET createdAt = DATEADD(day, -40, createdAt) WHERE tenant = ?", ORG_B)
rev("con `dias`: lo de hace 40 días no cuenta en los últimos 30", ORG_B not in {o["guid"] for o in c.get("/monitor-busqueda/?dias=30", headers={"x-api-key": SERVICIO}).json()["organizaciones"]})

ARCHIVO_LECTURA.unlink()
os.rmdir(carpeta)
print("\n" + ("TODO EN VERDE" if not fallos else f"{fallos} FALLA(S)"))
sys.exit(1 if fallos else 0)
