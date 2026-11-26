"""¿Preguntarle al expediente (NEX-612) hace lo que dice el ticket?

    .venv/Scripts/python verificar_busqueda_expediente.py

Corre contra el SQL Server desechable de `pruebas_sql.py` (sin Docker se salta y lo dice), con todas las migraciones (la 0020 incluida) y el login de solo
lectura que hizo el DBA (aquí lo crea la prueba). El modelo se sustituye por un doble que anota el texto que le llegaría y contesta lo del guion: NO se
llama a Google. Se siembra la organización Alfa con dos procesos (uno activo que usa INE y Comprobante de domicilio, otro desactivado que usa Acta), tres
expedientes (Juan: con su INE corregida y dos ejecuciones, la última en «Requiere revisión»; Ana: con los dos documentos y sin ejecuciones; uno vacío) y
la organización Beta con lo suyo. Lo que se prueba:

1. las vistas nuevas contra lo sembrado: expedientes, sus documentos, lo que exige cada proceso (y si está activo), lo que le falta a cada expediente,
   las ejecuciones (cuál es la última), sus reglas con lo que compararon, su evidencia, sus faltantes y las correcciones con quién y cuándo;
2. el aislamiento: sin organización, nada; con un expediente fijado, TODAS las vistas (también las dos de documentos) solo traen lo suyo; no se puede
   cambiar a media conexión (15664); con la organización ajena fijada, nada aunque se fije un expediente de Alfa; el usuario de lectura no lee [nexus];
3. preguntar desde un expediente: 404 con uno ajeno o sin forma (sin anotarlo); «todos los expedientes» solo trae el abierto; al modelo se le dice desde
   dónde se pregunta y nunca el nombre del expediente; un expediente sin ejecuciones o sin documentos lo dice en `aviso`;
4. las cuatro preguntas del ticket con la consulta de su ejemplo dan lo sembrado; todos los ejemplos corren en los dos ámbitos;
5. la conversación, el reuso y «Tus preguntas recientes» no mezclan el Módulo de uso con los expedientes;
6. la bitácora guarda desde qué expediente se preguntó, y su CHECK muerde.
"""

import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-busqueda-expediente-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["CORREO_MODO"] = "silencio"
os.environ["DOCAI_PROJECT_ID"] = "proyecto-de-prueba"

import verificacion_comun  # noqa: E402,F401  (fija AUTH_JWT_SECRET)
import pruebas_sql  # noqa: E402

BASE = pruebas_sql.preparar("nexus-busqueda-expediente", 14347)

DB = BASE["base"]
LECTURA = "usrNexusLectura"
CLAVE = "Lec-" + secrets.token_urlsafe(12) + "-9!"
sa = BASE["conectar"]("sa", BASE["sa"], DB)
k = sa.cursor()
m = BASE["conectar"]("sa", BASE["sa"])
m.cursor().execute(f"IF SUSER_ID('{LECTURA}') IS NULL CREATE LOGIN {LECTURA} WITH PASSWORD = '{CLAVE}', CHECK_POLICY = ON ELSE ALTER LOGIN {LECTURA} WITH PASSWORD = '{CLAVE}'")
k.execute(f"CREATE USER {LECTURA} FOR LOGIN {LECTURA}")
k.execute(f"GRANT SELECT ON SCHEMA::consulta TO {LECTURA}")
carpeta = Path(tempfile.mkdtemp(prefix="busqueda-expediente-lectura-"))
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


temporal_super = auth.contrasena_temporal()
usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal_super))
bearer_super = con_primer_acceso("super@ejemplo.com", temporal_super, "Cobre-Lento-Azul-47!")
ORG_A, a_admin = crear_organizacion(bearer_super, "Alfa", "rita@ejemplo.com", "Rumbo-Sereno-Azul-47!")
ORG_B, b_admin = crear_organizacion(bearer_super, "Beta", "beto@ejemplo.com", "Rumbo-Sereno-Rojo-48!")
RITA = k.execute("SELECT guid FROM nexus.users WHERE email = 'rita@ejemplo.com'").fetchone()[0]


def campo(nombre: str, tipo_dato: str = "texto") -> dict:
    return {"nombre": nombre, "tipoDato": tipo_dato, "descripcion": "", "obligatorio": False, "cardinalidad": "unico", "valoresLista": [], "subcampos": []}


def guardar_tipo(bearer: str, tenant: str, tipo_id: str, nombre: str, campos: list) -> None:
    datos = {"nombre": nombre, "descripcion": f"{nombre} de prueba.", "estado": "activo", "procesadorId": "proc-" + tipo_id[-6:], "campos": campos}
    r = c.put(f"/tipos-documentales/{tipo_id}", json={"tenant": tenant, "revision": 0, "datos": datos}, headers=cab(bearer))
    assert r.status_code == 200, r.text


T_INE, T_COMP, T_ACTA, T_PAS = "tipo-prueba-ine-01", "tipo-prueba-comprobante-01", "tipo-prueba-acta-01", "tipo-prueba-pasaporte-01"
guardar_tipo(a_admin, ORG_A, T_INE, "INE", [campo("Nombre"), campo("CURP")])
guardar_tipo(a_admin, ORG_A, T_COMP, "Comprobante de domicilio", [campo("Domicilio")])
guardar_tipo(a_admin, ORG_A, T_ACTA, "Acta constitutiva", [campo("Razón social")])
guardar_tipo(b_admin, ORG_B, T_PAS, "Pasaporte", [campo("Número de pasaporte")])


def expediente(bearer: str, tenant: str, nombre: str) -> str:
    r = c.post("/expedientes/", json={"tenant": tenant, "nombre": nombre}, headers=cab(bearer))
    assert r.status_code == 201, r.text
    cuerpo = r.json()
    return cuerpo.get("id") or cuerpo["expediente"]["id"]


def documento(tenant: str, caso: str, archivo: str, tipo: str, nombre_tipo: str, datos: dict) -> tuple[str, dict]:
    """Un documento extraído dentro de un expediente. Devuelve su id público y el factId de cada campo."""
    with tablas.transaccion() as cur:
        publico = f"documento-{secrets.token_hex(16)}"
        cur.execute(
            "INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel) OUTPUT INSERTED.documentId VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            publico, tenant, secrets.token_hex(32), f"{tenant}/aa/aa/{secrets.token_hex(32)}", 1234, "application/pdf", archivo, "MANUAL",
        )
        doc = cur.fetchone()[0]
        cur.execute(
            "INSERT INTO nexus.extractionRuns (publicId, documentId, documentTypeId, documentTypePublicId, documentTypeName, documentTypeRevision, engine, processorId, "
            "processorVersion, status, errorDetail, confidenceAverage, confidenceMinimum, pageCount, metadataJson, startedAt, createdBy) "
            "OUTPUT INSERTED.runId VALUES (?, ?, NULL, ?, ?, 1, 'document_ai_extractor', 'proc123', NULL, 'ok', NULL, 90, 80, 1, NULL, SYSUTCDATETIME(), 'prueba')",
            f"extraccion-{secrets.token_hex(16)}", doc, tipo, nombre_tipo,
        )
        corrida = cur.fetchone()[0]
        hechos = {}
        for ruta, valor in datos.items():
            cur.execute(
                "INSERT INTO nexus.entityFacts (runId, fieldPath, fieldName, recordIndex, valueRaw, valueNormalized, confidence, nullReason, pageNumber) "
                "OUTPUT INSERTED.factId VALUES (?, ?, ?, NULL, ?, ?, 95, ?, 2)",
                corrida, ruta, ruta, valor, valor, None if valor is not None else "not_present",
            )
            hechos[ruta] = cur.fetchone()[0]
        cur.execute("SELECT caseId FROM nexus.cases WHERE publicId = ?", caso)
        cur.execute("INSERT INTO nexus.caseDocuments (caseId, documentId, addedBy) VALUES (?, ?, ?)", cur.fetchone()[0], doc, RITA)
    return publico, hechos


def proceso(tenant: str, nombre: str, tipos: list[str], activo: bool = True, finales: tuple = (), palabras: dict | None = None) -> str:
    """Un proceso con un nodo por tipo documental (cada uno conectado a una regla «Igual a») y su Resultado conectado. `finales`: los nombres de sus
    nodos «Valor final» ('' = sin nombre); `palabras`: las de su Resultado."""
    nodos = [{"id": f"t{i}", "type": "tipo-documental", "position": {"x": 0, "y": i * 100}, "data": {"tipoId": t}} for i, t in enumerate(tipos)]
    nodos += [{"id": "op1", "type": "operador", "position": {"x": 300, "y": 0}, "data": {"operador": "igual"}},
              {"id": "res", "type": "resultado-final", "position": {"x": 600, "y": 0}, "data": palabras or {"si": "Aprobado", "no": "Requiere revisión", "sin": "Sin datos"}}]
    nodos += [{"id": f"valor-final-{i}", "type": "valor-final", "position": {"x": 600, "y": 100 * i}, "data": {"nombre": f} if f else {}} for i, f in enumerate(finales, 1)]
    aristas = [{"id": f"a{i}", "source": f"t{i}", "sourceHandle": "nombre", "target": "op1", "targetHandle": "a" if i == 0 else "b"} for i in range(len(tipos))]
    aristas.append({"id": "ar", "source": "op1", "sourceHandle": None, "target": "res", "targetHandle": "a"})
    publico = f"proceso-{secrets.token_hex(16)}"
    datos = {"nombre": nombre, "descripcion": "", "nodos": nodos, "aristas": aristas, **({} if activo else {"activo": False})}
    with tablas.transaccion() as cur:
        cur.execute("INSERT INTO nexus.processes (publicId, tenant, revision, dataJson, createdBy) VALUES (?, ?, 1, ?, ?)", publico, tenant, json.dumps(datos, ensure_ascii=False), RITA)
    return publico


def ejecucion(tenant: str, caso: str, nombre_caso: str, proc: str, nombre_proc: str, caso_resultado: str, texto: str, detalle: dict, hace_min: int) -> str:
    publico = f"ejecucion-{secrets.token_hex(16)}"
    with tablas.transaccion() as cur:
        cur.execute(
            "INSERT INTO nexus.processRuns (publicId, tenant, caseId, caseName, processId, processName, processRevision, outcomeCase, outcomeText, status, "
            "detailJson, definitionJson, executedAt, executedBy) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, '{}', DATEADD(minute, ?, SYSUTCDATETIME()), ?)",
            publico, tenant, caso, nombre_caso, proc, nombre_proc, caso_resultado, texto, "incompleta" if caso_resultado == "sin" else "completa",
            json.dumps(detalle, ensure_ascii=False), -hace_min, RITA,
        )
    return publico


# ── La siembra ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
E_JUAN = expediente(a_admin, ORG_A, "Expediente de Juan Secreto")
E_ANA = expediente(a_admin, ORG_A, "Expediente de Ana")
E_VACIO = expediente(a_admin, ORG_A, "Expediente vacío")
E_BETA = expediente(b_admin, ORG_B, "Expediente de Beta")
D_INE_JUAN, hechos_juan = documento(ORG_A, E_JUAN, "ine-juan.pdf", T_INE, "INE", {"nombre": "JUAN PEREZ", "curp": "PEJJ900101HDFXXX01"})
with tablas.transaccion() as cur:
    cur.execute("INSERT INTO nexus.fieldCorrections (factId, valueNormalized, reason, correctedBy) VALUES (?, ?, ?, ?)", hechos_juan["nombre"], "JUAN PÉREZ", "acento", RITA)
D_INE_ANA, _ = documento(ORG_A, E_ANA, "ine-ana.pdf", T_INE, "INE", {"nombre": "ANA LOPEZ", "curp": None})
D_COMP_ANA, _ = documento(ORG_A, E_ANA, "comprobante-ana.pdf", T_COMP, "Comprobante de domicilio", {"domicilio": "CALLE 1"})
D_PAS, _ = documento(ORG_B, E_BETA, "pasaporte-beto.pdf", T_PAS, "Pasaporte", {"numero_de_pasaporte": "G123"})
P_ALTA = proceso(ORG_A, "Alta de cliente", [T_INE, T_COMP], finales=("Monto solicitado", ""))
P_ACTA = proceso(ORG_A, "Revisión de acta", [T_ACTA], activo=False, palabras={"si": "Acta válida", "no": "  ", "sin": None})
P_BETA = proceso(ORG_B, "Proceso de Beta", [T_PAS], finales=("Número secreto de Beta",))
REGLAS = [
    {"id": "op1", "etiqueta": "Igual a #1", "operador": "igual", "estado": "no_pasa", "valor": None, "texto": "Los valores no son iguales.",
     "entradas": [{"etiqueta": "Entrada A", "origen": "Nombre · INE", "valor": "JUAN PÉREZ"}, {"etiqueta": "Entrada B", "origen": "Nombre · Comprobante de domicilio", "valor": None}]},
    {"id": "op2", "etiqueta": "Existe #2", "operador": "existe", "estado": "pasa", "valor": None, "texto": "Sí existe.", "entradas": [{"etiqueta": "Valor", "origen": "CURP · INE", "valor": "PEJJ900101HDFXXX01"}]},
]
EVIDENCIA = [
    {"tipoId": T_INE, "tipoNombre": "INE", "documentoId": D_INE_JUAN, "documentoNombre": "ine-juan.pdf", "origenTipo": "extraccion",
     "campos": [{"campo": "nombre", "forma": "valor", "valores": ["JUAN PÉREZ"], "confianza": 95, "pagina": 2}, {"campo": "curp", "forma": "valor", "valores": ["PEJJ900101HDFXXX01"], "confianza": 95, "pagina": 2}]},
    {"tipoId": T_COMP, "tipoNombre": "Comprobante de domicilio", "documentoId": None, "documentoNombre": None, "origenTipo": None, "campos": []},
]
FALTA = "Comprobante de domicilio: el expediente no trae el documento"
R_VIEJA = ejecucion(ORG_A, E_JUAN, "Expediente de Juan Secreto", P_ALTA, "Alta de cliente", "si", "Aprobado", {"reglas": [], "evidencia": [], "condicion": "pasa", "faltantes": []}, 120)
R_NUEVA = ejecucion(ORG_A, E_JUAN, "Expediente de Juan Secreto", P_ALTA, "Alta de cliente", "no", "Requiere revisión",
                    {"reglas": REGLAS, "evidencia": EVIDENCIA, "condicion": "no_pasa", "faltantes": [FALTA]}, 5)
R_BETA = ejecucion(ORG_B, E_BETA, "Expediente de Beta", P_BETA, "Proceso de Beta", "no", "Requiere revisión", {"reglas": REGLAS, "evidencia": [], "condicion": "no_pasa", "faltantes": [FALTA]}, 5)


def valores_finales(ejecucion_id: str, valores: list[tuple]) -> None:
    """(nombre, forma, familia, estado, texto, lista, origen) por cada valor final, en orden."""
    with tablas.transaccion() as cur:
        cur.execute("SELECT processRunId FROM nexus.processRuns WHERE publicId = ?", ejecucion_id)
        corrida = cur.fetchone()[0]
        for i, (nombre, forma, familia, estado, texto, lista, origen) in enumerate(valores, 1):
            cur.execute(
                "INSERT INTO nexus.processRunFinalValues (processRunId, position, nodeId, name, kind, family, status, valueText, valueJson, origin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                corrida, i, f"valor-final-{i}", nombre, forma, familia, estado, texto, json.dumps(lista, ensure_ascii=False) if lista is not None else None, origen,
            )


# NEX-612 (0021): lo que entregó cada ejecución.
valores_finales(R_NUEVA, [
    ("Nombre del cliente", "valor", "texto", "listo", "JUAN PÉREZ", None, "Nombre · INE"),
    ("Monto solicitado", "valor", "numero", "listo", "150000", None, "Valor fijo #1"),
    ("Vigencia", "valor", "fecha", "listo", "2031-06-30", None, "Vigencia · INE"),
    ("Revisión del nombre", "veredicto", None, "listo", "No pasa", None, "Igual a #1"),
    ("Domicilio", "valor", "texto", "sin_valores", None, None, "Domicilio · Comprobante de domicilio"),
    ("Socios", "lista", "texto", "listo", "ANA, LUIS", ["ANA", "LUIS"], "Nombre · Acta constitutiva"),
    ("Suelto", "valor", None, "sin_conexion", None, None, None),
])
valores_finales(R_VIEJA, [("Monto solicitado", "valor", "numero", "listo", "90000", None, "Valor fijo #1")])
valores_finales(R_BETA, [("Monto solicitado", "valor", "numero", "listo", "999999", None, "Valor fijo #1")])


def lee(sql: str, organizacion: str | None = ORG_A, expediente_id: str | None = None) -> list:
    """Lo que ve `usrNexusLectura` con esa organización (y ese expediente) fijados."""
    if organizacion is None:
        con = consulta._conectar()
        try:
            cur = con.cursor()
            cur.execute(sql)
            return [list(f) for f in cur.fetchall()]
        finally:
            con.close()
    return [list(f) for f in consulta.consultar(organizacion, sql, expediente=expediente_id).filas]


# ── [1] Las vistas ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("1 · las vistas nuevas contra lo sembrado")
exps = lee("SELECT expediente, nombre, documentos, ejecuciones, ultimoProceso, ultimoResultado, ultimoCaso, creadoPor FROM consulta.vExpediente ORDER BY nombre")
rev("vExpediente: los tres de Alfa (ninguno de Beta), con sus documentos, ejecuciones, el último resultado y quién lo creó (su nombre)",
    exps == [[E_ANA, "Expediente de Ana", 2, 0, None, None, None, "Admin Alfa"], [E_JUAN, "Expediente de Juan Secreto", 1, 2, "Alta de cliente", "Requiere revisión", "no", "Admin Alfa"],
             [E_VACIO, "Expediente vacío", 0, 0, None, None, None, "Admin Alfa"]], str(exps))
docs = lee("SELECT expediente, documento, archivo, tipoDocumental, origenTipo FROM consulta.vDocumentoDeExpediente ORDER BY archivo")
rev("vDocumentoDeExpediente: cada documento con su expediente y su tipo vigente",
    docs == [[E_ANA, D_COMP_ANA, "comprobante-ana.pdf", "Comprobante de domicilio", "extraccion"], [E_ANA, D_INE_ANA, "ine-ana.pdf", "INE", "extraccion"],
             [E_JUAN, D_INE_JUAN, "ine-juan.pdf", "INE", "extraccion"]], str(docs))
req = lee("SELECT nombreProceso, activo, tipoDocumental FROM consulta.vDocumentoRequerido ORDER BY nombreProceso, tipoDocumental")
rev("vDocumentoRequerido: lo que usa cada proceso, y el desactivado como inactivo",
    req == [["Alta de cliente", True, "Comprobante de domicilio"], ["Alta de cliente", True, "INE"], ["Revisión de acta", False, "Acta constitutiva"]], str(req))
falt = lee("SELECT nombreExpediente, nombreProceso, tipoDocumental FROM consulta.vDocumentoFaltante ORDER BY nombreExpediente, tipoDocumental")
rev("vDocumentoFaltante: a Juan le falta el Comprobante; al vacío, los dos; a Ana nada; el proceso inactivo no exige nada",
    falt == [["Expediente de Juan Secreto", "Alta de cliente", "Comprobante de domicilio"], ["Expediente vacío", "Alta de cliente", "Comprobante de domicilio"],
             ["Expediente vacío", "Alta de cliente", "INE"]], str(falt))
ejs = lee("SELECT ejecucion, expediente, resultado, caso, esLaUltima, reglasQueNoPasaron, faltantes, ejecutadaPor FROM consulta.vEjecucion ORDER BY ejecutadaEn")
rev("vEjecucion: las dos de Juan (la de Beta no), la última marcada, con cuántas reglas no pasaron y cuántas cosas faltaron, y quién la corrió",
    ejs == [[R_VIEJA, E_JUAN, "Aprobado", "si", False, 0, 0, "Admin Alfa"], [R_NUEVA, E_JUAN, "Requiere revisión", "no", True, 1, 1, "Admin Alfa"]], str(ejs))
reglas = lee("SELECT ejecucion, orden, regla, estado, resultadoRegla, explicacion, comparo FROM consulta.vReglaDeEjecucion ORDER BY orden")
rev("vReglaDeEjecucion: cada regla con cómo salió, su explicación y lo que comparó (cada entrada con su origen y su valor, en orden)",
    reglas == [[R_NUEVA, 1, "Igual a #1", "no_pasa", "No pasa", "Los valores no son iguales.", "Entrada A (Nombre · INE): JUAN PÉREZ; Entrada B (Nombre · Comprobante de domicilio): —"],
               [R_NUEVA, 2, "Existe #2", "pasa", "Pasa", "Sí existe.", "Valor (CURP · INE): PEJJ900101HDFXXX01"]], str(reglas))
evid = lee("SELECT tipoDocumental, documento, archivo, campo, valor, confianza, pagina FROM consulta.vEvidenciaDeEjecucion ORDER BY tipoDocumental, campo")
rev("vEvidenciaDeEjecucion: de qué documento salió cada campo, con su valor, confianza y página; el tipo que faltó, sin documento",
    [[x[0], x[1], x[2], x[3], x[4], float(x[5]) if x[5] is not None else None, x[6]] for x in evid]
    == [["Comprobante de domicilio", None, None, None, None, None, None], ["INE", D_INE_JUAN, "ine-juan.pdf", "curp", "PEJJ900101HDFXXX01", 95.0, 2],
        ["INE", D_INE_JUAN, "ine-juan.pdf", "nombre", "JUAN PÉREZ", 95.0, 2]], str(evid))
rev("vFaltanteDeEjecucion: lo que le faltó a la última", lee("SELECT ejecucion, faltante, esLaUltima FROM consulta.vFaltanteDeEjecucion") == [[R_NUEVA, FALTA, True]])
corr = lee("SELECT documento, campo, valorExtraido, valorCorregido, motivo, corregidoPor, pagina FROM consulta.vCorreccion")
vf = lee("SELECT ejecucion, esLaUltima, orden, valorFinal, forma, familia, estado, situacion, valor, valorNumero, valorFecha, elementos, origen "
         "FROM consulta.vValorFinalDeEjecucion ORDER BY ejecutadaEn DESC, orden")
nuevos = [[x[2], x[3], x[4], x[5], x[6], x[7], x[8], float(x[9]) if x[9] is not None else None, str(x[10]) if x[10] is not None else None, x[11], x[12]] for x in vf if x[0] == R_NUEVA]
rev("vValorFinalDeEjecucion: los de la última de Juan, en orden, con su forma, su clase, si tienen valor (y por qué no) y de dónde salen",
    nuevos == [
        [1, "Nombre del cliente", "valor", "texto", "listo", "Con valor", "JUAN PÉREZ", None, None, None, "Nombre · INE"],
        [2, "Monto solicitado", "valor", "numero", "listo", "Con valor", "150000", 150000.0, None, None, "Valor fijo #1"],
        [3, "Vigencia", "valor", "fecha", "listo", "Con valor", "2031-06-30", None, "2031-06-30", None, "Vigencia · INE"],
        [4, "Revisión del nombre", "veredicto", None, "listo", "Con valor", "No pasa", None, None, None, "Igual a #1"],
        [5, "Domicilio", "valor", "texto", "sin_valores", "Sin valor (faltó el dato)", None, None, None, None, "Domicilio · Comprobante de domicilio"],
        [6, "Socios", "lista", "texto", "listo", "Con valor", "ANA, LUIS", None, None, 2, "Nombre · Acta constitutiva"],
        [7, "Suelto", "valor", None, "sin_conexion", "Sin conectar", None, None, None, None, None],
    ], str(nuevos))
rev("…con la ejecución vieja (la marca de la última, solo en la nueva) y nada de Beta",
    [x[:2] + [x[3]] for x in vf if x[0] != R_NUEVA] == [[R_VIEJA, False, "Monto solicitado"]] and all(x[1] is True for x in vf if x[0] == R_NUEVA), str([x[:4] for x in vf]))
rev("vCorreccion: el campo corregido, con el valor extraído, el corregido, el motivo, quién (su nombre) y la página",
    corr == [[D_INE_JUAN, "nombre", "JUAN PEREZ", "JUAN PÉREZ", "acento", "Admin Alfa", 2]], str(corr))

# ── [2] El aislamiento ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("2 · el aislamiento: organización y expediente fijados por la base")
NUEVAS = ("vExpediente", "vDocumentoDeExpediente", "vDocumentoRequerido", "vDocumentoFaltante", "vEjecucion", "vReglaDeEjecucion", "vEvidenciaDeEjecucion",
          "vFaltanteDeEjecucion", "vCorreccion", "vDocumentoExtraido", "vDatoExtraido", "vValorFinalDeEjecucion")
cuentas = {v: lee(f"SELECT COUNT(*) FROM consulta.{v}", None)[0][0] for v in NUEVAS}
rev("sin organización fijada: todas dan 0", all(n == 0 for n in cuentas.values()), str(cuentas))
DE_DOCUMENTOS = "SELECT DISTINCT documento FROM consulta.{} ORDER BY documento"
juan = {v: lee(DE_DOCUMENTOS.format(v), ORG_A, E_JUAN) for v in ("vDocumentoExtraido", "vDatoExtraido", "vDocumentoDeExpediente", "vCorreccion")}
rev("con el expediente de Juan fijado, las vistas de documentos (también las dos de la 0017) solo traen su documento",
    all(x == [[D_INE_JUAN]] for x in juan.values()), str(juan))
rev("…vExpediente solo él, vEjecucion solo las suyas, vDocumentoFaltante solo lo suyo",
    lee("SELECT expediente FROM consulta.vExpediente", ORG_A, E_JUAN) == [[E_JUAN]] and len(lee("SELECT 1 FROM consulta.vEjecucion", ORG_A, E_JUAN)) == 2
    and lee("SELECT tipoDocumental FROM consulta.vDocumentoFaltante", ORG_A, E_JUAN) == [["Comprobante de domicilio"]])
ana = {v: lee(f"SELECT COUNT(*) FROM consulta.{v}", ORG_A, E_ANA)[0][0] for v in NUEVAS}
rev("con el de Ana: sus 2 documentos, 0 ejecuciones, 0 reglas, 0 correcciones, 0 faltantes (vDocumentoRequerido sigue siendo de la organización)",
    ana["vDocumentoDeExpediente"] == 2 and ana["vDocumentoExtraido"] == 2 and ana["vEjecucion"] == 0 and ana["vReglaDeEjecucion"] == 0 and ana["vCorreccion"] == 0
    and ana["vDocumentoFaltante"] == 0 and ana["vExpediente"] == 1 and ana["vDocumentoRequerido"] == 3, str(ana))
cruzado = {v: lee(f"SELECT COUNT(*) FROM consulta.{v}", ORG_B, E_JUAN)[0][0] for v in NUEVAS if v != "vDocumentoRequerido"}
rev("con Beta fijada y el expediente de Juan (de Alfa): nada de Alfa (todo da 0)", all(n == 0 for n in cruzado.values()), str(cruzado))
rev("…y lo que exigen los procesos es solo lo de Beta (no depende del expediente)",
    lee("SELECT nombreProceso, tipoDocumental FROM consulta.vDocumentoRequerido", ORG_B, E_JUAN) == [["Proceso de Beta", "Pasaporte"]])
con = consulta.abrir(ORG_A, expediente=E_JUAN)
try:
    con.cursor().execute("EXEC sys.sp_set_session_context @key = N'expediente', @value = ?", E_ANA)
    cambio = "se pudo cambiar"
except Exception as exc:  # noqa: BLE001
    cambio = str(exc)
finally:
    con.close()
rev("cambiar el expediente a media conexión: 15664", "15664" in cambio, cambio[:200])
con = consulta.abrir(ORG_A)
try:
    fijado = con.cursor().execute("SELECT CAST(SESSION_CONTEXT(N'expediente') AS varchar(63))").fetchone()[0]
finally:
    con.close()
rev("sin expediente, la conexión no trae ninguno (el pool no arrastra el de otra pregunta)", fijado is None, str(fijado))
try:
    lee("SELECT COUNT(*) FROM nexus.cases", ORG_A, E_JUAN)
    leyo = "leyó nexus.cases"
except consulta.ConsultaRechazada as exc:
    leyo = f"{exc.numero}: {exc}"
rev("el usuario de lectura no lee [nexus] (229)", leyo.startswith("229:"), leyo[:200])
try:
    consulta.abrir(ORG_A, expediente="expediente-xyz")
    forma = "aceptó"
except ValueError:
    forma = "ValueError"
rev("fijar un expediente sin forma: ValueError antes de conectar", forma == "ValueError")

# ── El doble de Google ───────────────────────────────────────────────────────────────────────────────────────────────────────────────
llamadas: list[str] = []
guion: list = []
USO = {"promptTokenCount": 9000, "cachedContentTokenCount": 0, "candidatesTokenCount": 150, "thoughtsTokenCount": 0, "totalTokenCount": 9150}


async def llamar_falso(url, headers, cuerpo, timeout):
    if url.endswith(":predict"):
        d = cuerpo["parameters"]["outputDimensionality"]
        return 200, {"predictions": [{"embeddings": {"values": [1.0] + [0.0] * (d - 1), "statistics": {"token_count": 5}}} for _ in cuerpo["instances"]]}
    llamadas.append(cuerpo["contents"][0]["parts"][0]["text"])
    respuesta = guion.pop(0) if guion else {"consulta": None, "noSePuede": "sin guion"}
    return 200, {"candidates": [{"content": {"parts": [{"text": json.dumps(respuesta, ensure_ascii=False)}]}, "finishReason": "STOP"}], "usageMetadata": USO}


gemini._llamar = llamar_falso
gemini._token = lambda: "token-falso"
config.EMBEDDINGS_DIMENSIONES = 256
config.BUSQUEDA_TOPE_PERSONA_POR_HORA = config.BUSQUEDA_TOPE_ORGANIZACION_POR_DIA = 10_000


def sql(consulta_sql: str, explicacion: str = "Entendí la pregunta.") -> dict:
    return {"consulta": consulta_sql, "explicacion": explicacion, "noSePuede": None, "tomadoDeAntes": None, "sugerencias": []}


def preguntar(pregunta, expediente_id=None, conversacion=None, *respuestas, bearer=None, tenant=ORG_A):
    guion[:] = list(respuestas)
    cuerpo = {"tenant": tenant, "pregunta": pregunta, **({"expediente": expediente_id} if expediente_id else {}), **({"conversacion": conversacion} if conversacion else {})}
    return c.post("/busqueda/preguntar", json=cuerpo, headers=cab(bearer or a_admin))


def cuantas_preguntas() -> int:
    return k.execute("SELECT COUNT(*) FROM nexus.searchQuestions").fetchone()[0]


# ── [3] Preguntar desde un expediente ────────────────────────────────────────────────────────────────────────────────────────────────
titulo("3 · preguntar desde un expediente")
antes = cuantas_preguntas()
r = preguntar("¿Cuántos documentos tiene?", E_BETA)
rev("con un expediente de OTRA organización: 404 expediente_no_existe", r.status_code == 404 and r.json()["detail"]["codigo"] == "expediente_no_existe", r.text[:200])
r = preguntar("¿Cuántos documentos tiene?", "expediente-noexiste")
rev("…con uno sin forma, también 404", r.status_code == 404, r.text[:200])
rev("…y no se anota nada ni se le pregunta al modelo", cuantas_preguntas() == antes and not llamadas)
r = preguntar("Dame todos los expedientes con sus documentos", E_JUAN, None,
              sql("SELECT expediente, nombre AS [Expediente], documentos AS [Documentos] FROM consulta.vExpediente ORDER BY nombre"))
cuerpo = r.json() if r.status_code == 200 else {}
rev("«todos los expedientes» desde el de Juan: solo trae el de Juan (la base lo acota, la consulta no filtra)",
    r.status_code == 200 and cuerpo.get("filas") == [[E_JUAN, "Expediente de Juan Secreto", 1]] and cuerpo.get("expediente") == E_JUAN and cuerpo.get("aviso") is None, r.text[:300])
prompt = llamadas[-1]
rev("al modelo se le dijo que se pregunta DESDE UN EXPEDIENTE, sin su nombre ni su id", "DESDE UN EXPEDIENTE" in prompt and "Juan Secreto" not in prompt and E_JUAN not in prompt)
rev("…y las vistas nuevas están en lo que se le cuenta", all(f"consulta.{v}" in prompt for v in NUEVAS))
rev("el catálogo trae los PROCESOS: nombre, si está activo, las palabras de su Resultado, sus valores finales (sin nombre: «Valor final #N») y lo que usa",
    "- «Alta de cliente» (activo): termina en «Aprobado» si se cumple, «Requiere revisión» si no se cumple y «Sin datos» si faltan datos; "
    "valores finales: «Monto solicitado», «Valor final #2»; usa: INE, Comprobante de domicilio" in prompt, prompt[prompt.find("Procesos del motor"):][:600])
rev("…el desactivado como inactivo, y una palabra de Resultado vacía con la de siempre",
    "- «Revisión de acta» (inactivo): termina en «Acta válida» si se cumple, «Rechazado» si no se cumple y «Sin datos» si faltan datos; usa: Acta constitutiva" in prompt,
    prompt[prompt.find("Procesos del motor"):][:600])
rev("…y nada de los procesos de la otra organización", "Proceso de Beta" not in prompt and "Número secreto de Beta" not in prompt)
huella_antes = k.execute("SELECT TOP (1) catalogHash FROM nexus.searchQuestions ORDER BY questionId DESC").fetchone()[0]
rev("sin procesos, el catálogo lo dice", busqueda._procesos_en_texto([]) == "La organización todavía no tiene procesos del motor de reglas."
    and "todavía sin Resultado" in busqueda._procesos_en_texto([{"nombre": "X", "activo": False, "resultados": None, "valoresFinales": [], "tipos": []}]))
g = k.execute("SELECT TOP (1) caseId FROM nexus.searchQuestions ORDER BY questionId DESC").fetchone()[0]
rev("la bitácora guarda desde qué expediente se preguntó", g == E_JUAN, str(g))
with tablas.transaccion() as cur:
    cur.execute("UPDATE nexus.processes SET dataJson = JSON_MODIFY(dataJson, '$.nombre', N'Alta de cliente renombrado') WHERE publicId = ?", P_ALTA)
r2 = preguntar("¿Cuántos procesos hay?", E_JUAN, None, sql("SELECT COUNT(DISTINCT proceso) AS [Procesos] FROM consulta.vDocumentoRequerido"))
huella_despues = k.execute("SELECT TOP (1) catalogHash FROM nexus.searchQuestions ORDER BY questionId DESC").fetchone()[0]
rev("renombrar un proceso cambia lo que el modelo sabe (y la huella del catálogo: la misma pregunta no reusa una consulta de antes del cambio)",
    "«Alta de cliente renombrado»" in llamadas[-1] and huella_antes != huella_despues, f"{huella_antes} {huella_despues}")
with tablas.transaccion() as cur:
    cur.execute("UPDATE nexus.processes SET dataJson = JSON_MODIFY(dataJson, '$.nombre', N'Alta de cliente') WHERE publicId = ?", P_ALTA)
r = preguntar("¿Qué documentos hay?", None, None, sql("SELECT COUNT(*) AS [Documentos] FROM consulta.vDocumentoExtraido"))
rev("desde el Módulo de uso: toda la organización (3 documentos de Alfa) y el modelo lo sabe", r.json().get("filas") == [[3]] and "desde el Módulo de uso" in llamadas[-1], r.text[:200])
rev("…y la bitácora no guarda expediente", k.execute("SELECT TOP (1) caseId FROM nexus.searchQuestions ORDER BY questionId DESC").fetchone()[0] is None)
r = preguntar("¿Por qué quedó así?", E_VACIO, None, sql("SELECT regla FROM consulta.vReglaDeEjecucion WHERE esLaUltima = 1"))
rev("un expediente al que no se le ha ejecutado nada: 0 renglones y el aviso lo dice, sin inventar un resultado",
    r.status_code == 200 and r.json().get("filas") == [] and r.json().get("aviso") == busqueda.AVISO_SIN_EJECUCIONES, r.text[:300])
r = preguntar("¿Qué documentos tiene?", E_VACIO, None, sql("SELECT documento, archivo FROM consulta.vDocumentoDeExpediente"))
rev("…uno sin documentos: el aviso lo dice", r.json().get("filas") == [] and r.json().get("aviso") == busqueda.AVISO_SIN_DOCUMENTOS, r.text[:300])
r = preguntar("¿Qué reglas no pasaron?", E_ANA, None, sql("SELECT regla FROM consulta.vReglaDeEjecucion WHERE estado = 'no_pasa'"))
rev("…Ana tampoco tiene ejecuciones: el mismo aviso", r.json().get("aviso") == busqueda.AVISO_SIN_EJECUCIONES, r.text[:300])
r = preguntar("¿Qué reglas dieron error raro?", E_JUAN, None, sql("SELECT regla FROM consulta.vReglaDeEjecucion WHERE estado = 'xyz'"))
rev("…Juan sí tiene ejecuciones: 0 renglones sin aviso (es «no hay información»)", r.json().get("filas") == [] and r.json().get("aviso") is None, r.text[:300])
r = preguntar("¿Qué valores finales dio?", E_ANA, None, sql("SELECT valorFinal, valor FROM consulta.vValorFinalDeEjecucion WHERE esLaUltima = 1"))
rev("…los valores finales de un expediente sin ejecuciones: el mismo aviso", r.json().get("filas") == [] and r.json().get("aviso") == busqueda.AVISO_SIN_EJECUCIONES, r.text[:300])

# ── [4] Las cuatro del ticket y todos los ejemplos ───────────────────────────────────────────────────────────────────────────────────
titulo("4 · las cuatro preguntas del ticket con la consulta de su ejemplo")
EJ = dict(busqueda.EJEMPLOS)


def con_ejemplo(pregunta: str, expediente_id: str | None) -> dict:
    r = preguntar(pregunta, expediente_id, None, sql(EJ[pregunta]))
    return r.json() if r.status_code == 200 else {"error": r.text}


f = con_ejemplo("¿Qué documentos le faltan a este expediente?", E_JUAN)
rev("«¿Qué documentos le faltan a este expediente?» (Juan): el Comprobante, que exige Alta de cliente", f.get("filas") == [["Alta de cliente", "Comprobante de domicilio"]], str(f)[:300])
f = con_ejemplo("¿Por qué quedó en Requiere revisión?", E_JUAN)
rev("«¿Por qué quedó en Requiere revisión?» (Juan): la regla que no pasó, con lo que comparó, y lo que faltó; con ejecucion y expediente para abrirla",
    f.get("columnas", [])[:2] == ["ejecucion", "expediente"]
    and sorted(tuple(x[2:5]) for x in f.get("filas", [])) == sorted([("Alta de cliente", "Igual a #1", "Los valores no son iguales."), ("Alta de cliente", "Faltó", FALTA)])
    and all(x[0] == R_NUEVA and x[1] == E_JUAN for x in f.get("filas", [])), str(f)[:400])
f = con_ejemplo("Expedientes de este mes que quedaron en Requiere revisión", None)
rev("«Expedientes de este mes que quedaron en Requiere revisión» (Módulo de uso): Juan, por su última ejecución (Beta no)",
    [x[:3] for x in f.get("filas", [])] == [[R_NUEVA, E_JUAN, "Expediente de Juan Secreto"]], str(f)[:300])
f = con_ejemplo("¿Qué datos de este expediente se corrigieron y quién lo hizo?", E_JUAN)
rev("«¿Qué datos de este expediente se corrigieron y quién lo hizo?» (Juan): el nombre, de JUAN PEREZ a JUAN PÉREZ, por acento, Admin Alfa",
    [x[:8] for x in f.get("filas", [])] == [[D_INE_JUAN, "ine-juan.pdf", "INE", "nombre", "JUAN PEREZ", "JUAN PÉREZ", "acento", "Admin Alfa"]], str(f)[:300])
f = con_ejemplo("¿Qué valores finales dio la última ejecución?", E_JUAN)
rev("«¿Qué valores finales dio la última ejecución?» (Juan): los siete, con el valor o por qué no lo tiene, y con ejecucion y expediente para abrirla",
    [x[3:5] for x in f.get("filas", [])] == [["Nombre del cliente", "JUAN PÉREZ"], ["Monto solicitado", "150000"], ["Vigencia", "2031-06-30"], ["Revisión del nombre", "No pasa"],
                                             ["Domicilio", "Sin valor (faltó el dato)"], ["Socios", "ANA, LUIS"], ["Suelto", "Sin conectar"]]
    and all(x[0] == R_NUEVA and x[1] == E_JUAN for x in f.get("filas", [])), str(f)[:400])
f = con_ejemplo("¿En qué expedientes el valor final Monto pasa de 100,000?", None)
rev("«¿En qué expedientes el valor final Monto pasa de 100,000?» (Módulo de uso): Juan por su última (150,000; la vieja de 90,000 no cuenta, Beta no)",
    [[x[1], float(x[5])] for x in f.get("filas", [])] == [[E_JUAN, 150000.0]], str(f)[:300])
f = con_ejemplo("¿Qué datos de este expediente se corrigieron y quién lo hizo?", E_ANA)
rev("…la misma desde Ana: no hay correcciones (0 renglones, sin aviso: sí tiene documentos)", f.get("filas") == [] and f.get("aviso") is None, str(f)[:200])
malos = []
for pregunta_ej, sql_ej in busqueda.EJEMPLOS:
    for exp in (None, E_JUAN):
        try:
            consulta.consultar(ORG_A, busqueda.revisar(sql_ej), expediente=exp)
        except Exception as exc:  # noqa: BLE001
            malos.append(f"{pregunta_ej} ({exp or 'organización'}): {exc}")
rev(f"los {len(busqueda.EJEMPLOS)} ejemplos pasan la revisión y corren en los dos ámbitos", not malos, "; ".join(malos)[:600])

# ── [5] No se mezclan los ámbitos ────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("5 · conversación, reuso y recientes, cada una en su ámbito")
r = preguntar("¿Cuántos documentos tiene este expediente?", E_JUAN, None, sql("SELECT COUNT(*) AS [Documentos] FROM consulta.vDocumentoDeExpediente"))
C_JUAN = r.json().get("conversacion")
r = preguntar("¿y de qué tipo son?", E_JUAN, C_JUAN, sql("SELECT tipoDocumental FROM consulta.vDocumentoDeExpediente"))
rev("la conversación sigue dentro del MISMO expediente (pregunta 2, con la anterior en el prompt)",
    r.json().get("conversacion") == C_JUAN and r.json().get("turno") == 2 and "Conversación: antes" in llamadas[-1], r.text[:200])
r = preguntar("¿y de qué tipo son?", E_ANA, C_JUAN, sql("SELECT tipoDocumental FROM consulta.vDocumentoDeExpediente"))
rev("…la misma conversación mandada desde OTRO expediente empieza una nueva", r.json().get("conversacion") not in (None, C_JUAN) and r.json().get("turno") == 1, r.text[:200])
r = preguntar("¿y de qué tipo son?", None, C_JUAN, sql("SELECT tipoDocumental FROM consulta.vDocumentoExtraido"))
rev("…y desde el Módulo de uso, también", r.json().get("conversacion") != C_JUAN and r.json().get("turno") == 1, r.text[:200])
n = len(llamadas)
r = preguntar("¿Cuántos documentos tiene este expediente?", E_ANA)
rev("la misma pregunta desde OTRO expediente reusa la consulta (sin llamar al modelo) y la base la acota a ese: Ana tiene 2",
    r.json().get("origen") == "anterior" and r.json().get("filas") == [[2]] and len(llamadas) == n, r.text[:200])
n = len(llamadas)
r = preguntar("¿Cuántos documentos tiene este expediente?", None, None, {"consulta": None, "explicacion": "", "noSePuede": "Para preguntarle a un expediente, ábrelo.", "tomadoDeAntes": None, "sugerencias": []})
rev("…pero desde el Módulo de uso NO se reusa lo de un expediente: se le pregunta al modelo", len(llamadas) == n + 1 and r.status_code == 422, r.text[:200])
rec = [x["pregunta"] for x in c.get(f"/busqueda/preguntas?tenant={ORG_A}&limite=20", headers=cab(a_admin)).json().get("preguntas", [])]
rev("«Tus preguntas recientes» (del Módulo de uso) no trae las que se hicieron desde un expediente",
    "¿Qué documentos hay?" in rec and "Dame todos los expedientes con sus documentos" not in rec and "¿Por qué quedó así?" not in rec and "¿Qué reglas no pasaron?" not in rec, str(rec))

# ── [6] La base ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
titulo("6 · el CHECK de caseId")
pid = k.execute("SELECT TOP (1) publicId FROM nexus.searchQuestions WHERE caseId IS NOT NULL").fetchone()[0]
sa.autocommit = False
for malo in ("expediente-XYZ", "otra-cosa", "expediente-" + "A" * 32):
    try:
        k.execute("UPDATE nexus.searchQuestions SET caseId = ? WHERE publicId = ?", malo, pid)
        mordio = False
    except Exception as exc:  # noqa: BLE001
        mordio = "CHECK" in str(exc)
    sa.rollback()
    rev(f"caseId sin la forma ({malo[:16]}…): lo frena el CHECK", mordio)
sa.autocommit = True

print()
print("TODO EN VERDE" if not fallos else f"{fallos} FALLA(S)")
sys.exit(1 if fallos else 0)
