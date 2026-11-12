"""¿Entrega `/monitor-datos` lo que la sección «Datos» de Nexus Monitor necesita? (`servicios/datos_monitor.py`)

    .venv/Scripts/python verificar_datos_monitor.py

Corre contra el SQL Server desechable de `pruebas_sql.py` (sin Docker se salta y lo dice). Los datos se siembran directo en las tablas de
la migración `0007`, sin pasar por Document AI.

Lo que se prueba:
- sin la llave de servicio: 401; con ella, el panorama cruza organizaciones (dos tenants);
- cada documento trae su ÚLTIMA corrida y los conteos de sus campos VIGENTES (con valor, ausentes, corregidos);
- una última corrida que falló se enseña como `error`, pero los conteos siguen siendo los de la corrida anterior que terminó;
- por tipo documental, las filas de lo que se repite (`socios[1].nombre`, `socios[2].nombre`) cuentan como un solo campo, y la confianza
  promedio y mínima salen bien; los campos que más faltan van primero;
- la ficha de un documento trae sus campos vigentes (con la corrección aplicada) y TODAS sus corridas; un id que no existe: 404.
"""

import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pruebas_sql  # noqa: E402

pruebas_sql.preparar("nexus-datos-monitor-prueba", 14341)

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-datos-monitor-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from db import tablas  # noqa: E402

fallos = 0


def ok(condicion: bool, texto: str) -> None:
    global fallos
    print(("OK   " if condicion else "FALLA") + " " + texto)
    if not condicion:
        fallos += 1


def documento(cur, tenant: str, nombre: str) -> tuple[int, str]:
    publico = f"documento-{uuid.uuid4().hex}"
    cur.execute(
        "INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel) "
        "OUTPUT INSERTED.documentId VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        publico, tenant, "a" * 64, f"{tenant}/aa/aa/{'a' * 64}", 1234, "application/pdf", nombre, "MANUAL",
    )
    return cur.fetchone()[0], publico


def corrida(cur, doc: int, tipo: str, nombre: str, estado: str = "ok", promedio: float | None = 90, minima: float | None = 80) -> int:
    cur.execute(
        "INSERT INTO nexus.extractionRuns (publicId, documentId, documentTypeId, documentTypePublicId, documentTypeName, documentTypeRevision, "
        "engine, processorId, processorVersion, status, errorDetail, confidenceAverage, confidenceMinimum, pageCount, metadataJson, startedAt, createdBy) "
        "OUTPUT INSERTED.runId VALUES (?, ?, NULL, ?, ?, 1, 'document_ai_extractor', 'proc123', NULL, ?, ?, ?, ?, 1, NULL, ?, 'prueba')",
        f"extraccion-{uuid.uuid4().hex}", doc, tipo, nombre, estado, "falló" if estado == "error" else None,
        promedio if estado != "error" else None, minima if estado != "error" else None, datetime.utcnow(),
    )
    return cur.fetchone()[0]


def dato(cur, run: int, ruta: str, nombre: str, valor: str | None, confianza: float | None, indice: int | None = None) -> int:
    cur.execute(
        "INSERT INTO nexus.entityFacts (runId, fieldPath, fieldName, recordIndex, valueRaw, valueNormalized, confidence, nullReason) "
        "OUTPUT INSERTED.factId VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        run, ruta, nombre, indice, valor, valor, confianza, None if valor is not None else "not_present",
    )
    return cur.fetchone()[0]


ORG_A = "11111111-1111-4111-8111-111111111111"
ORG_B = "22222222-2222-4222-8222-222222222222"

with tablas.transaccion() as cur:
    # Las dos organizaciones registradas: `documents.tenant` tiene llave foránea a `tenants` (FK_documents_organization).
    cur.execute("INSERT INTO nexus.tenants (guid, name, slug, code) VALUES (?, ?, ?, ?)", ORG_A, "Dorama Prueba", "dorama-prueba", "NEX-09001")
    cur.execute("INSERT INTO nexus.tenants (guid, name, slug, code) VALUES (?, ?, ?, ?)", ORG_B, "Otra Prueba", "otra-prueba", "NEX-09002")
    # Organización A: una INE con una corrida buena y luego una que falló; un acta con socios que se repiten.
    ine, ine_id = documento(cur, ORG_A, "ine juan.pdf")
    r1 = corrida(cur, ine, "tipo-ine", "INE")
    nombre_fact = dato(cur, r1, "nombre", "nombre", "JUAN PEREZ", 98)
    dato(cur, r1, "curp", "curp", None, None)
    cur.execute("INSERT INTO nexus.fieldCorrections (factId, valueNormalized, reason, correctedBy) VALUES (?, ?, ?, ?)", nombre_fact, "JUAN PÉREZ", "acento", "prueba")
    corrida(cur, ine, "tipo-ine", "INE", estado="error")

    acta, acta_id = documento(cur, ORG_A, "acta.pdf")
    r2 = corrida(cur, acta, "tipo-acta", "Acta constitutiva", promedio=70, minima=40)
    dato(cur, r2, "razon_social", "razon_social", "ACME SA", 95)
    dato(cur, r2, "socios[1].nombre", "nombre", "ANA", 60, 1)
    dato(cur, r2, "socios[2].nombre", "nombre", "LUIS", 40, 2)

    # Organización B: otra INE, con la CURP presente.
    ine_b, ine_b_id = documento(cur, ORG_B, "ine.pdf")
    r3 = corrida(cur, ine_b, "tipo-ine-b", "INE")
    dato(cur, r3, "nombre", "nombre", "MARIA", 90)
    dato(cur, r3, "curp", "curp", "MARIA000000", 70)
    dato(cur, r3, "fecha_nacimiento", "fecha_nacimiento", "1990-05-17", 95)
    dato(cur, r3, "seccion", "seccion", "2265", 99)
    dato(cur, r3, "vigencia", "vigencia", "2024-2034", 97)

cliente = TestClient(app)
llave = {"X-API-Key": SERVICIO}

ok(cliente.get("/monitor-datos/").status_code == 401, "sin llave de servicio: 401")

r = cliente.get("/monitor-datos/", headers=llave)
ok(r.status_code == 200, f"panorama: 200 ({r.status_code})")
p = r.json()
docs = {d["id"]: d for d in p["documentos"]}
ok(p["sinBase"] is False and len(docs) == 3, "tres documentos de dos organizaciones")
ok({d["organizacion"]["guid"] for d in docs.values()} == {ORG_A, ORG_B}, "cruza organizaciones")

d = docs[ine_id]
ok(d["estado"] == "error" and d["corridas"] == 2, "la INE enseña su última corrida (la que falló) y que tuvo 2")
ok((d["campos"], d["conValor"], d["ausentes"], d["corregidos"]) == (2, 1, 1, 1), f"…pero sus conteos son los de la corrida buena: {d}")
ok(p["documentos"][0]["id"] == ine_b_id, "el más reciente primero")

tipos = {(t["organizacion"]["guid"], t["id"]): t for t in p["tipos"]}
acta_t = tipos[(ORG_A, "tipo-acta")]
socios = next(c for c in acta_t["campos"] if c["ruta"] == "socios[].nombre")
ok(socios["veces"] == 2 and socios["confianzaPromedio"] == 50 and socios["confianzaMinima"] == 40, f"las filas que se repiten son un campo: {socios}")
ok(acta_t["documentos"] == 1 and acta_t["nombre"] == "Acta constitutiva", "el tipo trae su nombre y cuántos documentos")
ine_t = tipos[(ORG_A, "tipo-ine")]
ok(ine_t["campos"][0]["ruta"] == "curp" and ine_t["campos"][0]["ausentes"] == 1, "lo que más falta va primero")
ok(next(c for c in ine_t["campos"] if c["ruta"] == "nombre")["corregidos"] == 1, "cuenta las correcciones")

f = cliente.get(f"/monitor-datos/{ine_id}", headers=llave)
ok(f.status_code == 200, "ficha: 200")
f = f.json()
nombre = next(c for c in f["campos"] if c["ruta"] == "nombre")
ok(nombre["valor"] == "JUAN PÉREZ" and nombre["valorExtraido"] == "JUAN PEREZ" and nombre["corregido"], "la ficha trae el valor corregido y el extraído")
ok(len(f["corridas"]) == 2 and f["corridas"][0]["estado"] == "error" and f["corridas"][1]["campos"] == 2, "y todas las corridas, la última primero")
ok(f["archivo"] == "ine juan.pdf" and f["organizacion"]["guid"] == ORG_A, "con su archivo y su organización")
ok(cliente.get(f"/monitor-datos/documento-{'0' * 32}", headers=llave).status_code == 404, "un documento que no existe: 404")
ok(cliente.get("/monitor-datos/cualquier-cosa", headers=llave).status_code == 404, "un id mal formado: 404")
ok(all(d["tipoDocumental"]["existe"] is False for d in p["documentos"]) and all(t["existe"] is False for t in p["tipos"]) and f["corridas"][0]["tipoDocumental"]["existe"] is False, "un tipo que ya no está en documentTypes sale con existe: false (aquí se sembraron sin tipo)")
ok(docs[ine_id]["organizacion"]["nombre"] == "Dorama Prueba" and docs[ine_b_id]["organizacion"]["nombre"] == "Otra Prueba", "cada documento trae el nombre de su organización")

# ── Migración 0015: las vistas para consultar en lenguaje natural, leídas con el usuario de la API ──────────────────────────
with tablas.lectura() as cur:
    cur.execute("SELECT * FROM nexus.vDocumentoExtraido")
    vdocs = {v["documento"]: v for v in tablas.filas(cur)}
    cur.execute("SELECT * FROM nexus.vDatoExtraido")
    vdatos = tablas.filas(cur)

ok(set(vdocs) == {ine_id, acta_id, ine_b_id}, "vDocumentoExtraido: un renglón por documento extraído")
v = vdocs[ine_id]
ok(v["estado"] == "ok" and v["extracciones"] == 2, "…con su extracción VIGENTE (la que terminó, no la que falló) y cuántas tuvo")
ok((v["campos"], v["camposConValor"], v["camposFaltantes"], v["camposCorregidos"]) == (2, 1, 1, 1), f"…y los conteos vigentes: {v}")
ok(v["organizacion"] == "Dorama Prueba" and v["tipoDocumental"] == "INE" and v["archivo"] == "ine juan.pdf", "…con organización, tipo y archivo")
ok(vdocs[ine_b_id]["organizacion"] == "Otra Prueba" and float(vdocs[acta_id]["confianzaMinima"]) == 40, "organización de cada uno; confianzas tal cual")

por = {(d["documento"], d["campo"]): d for d in vdatos}
ok(len(vdatos) == 2 + 3 + 5, f"vDatoExtraido: un renglón por campo vigente ({len(vdatos)})")
nom = por[(ine_id, "nombre")]
ok(nom["valor"] == "JUAN PÉREZ" and nom["corregido"] and nom["organizacion"] == "Dorama Prueba", "…con el valor ya corregido")
ok(por[(ine_id, "curp")]["valor"] is None and por[(ine_id, "curp")]["motivoSinValor"] == "not_present", "…lo que faltó, con su motivo")
ok(str(por[(ine_b_id, "fecha_nacimiento")]["valorFecha"]) == "1990-05-17", "valorFecha convierte las fechas ISO")
ok(por[(ine_b_id, "vigencia")]["valorFecha"] is None and por[(ine_b_id, "vigencia")]["valorNumero"] is None, "…y deja NULL lo que no es fecha ni número (2024-2034)")
ok(float(por[(ine_b_id, "seccion")]["valorNumero"]) == 2265 and por[(ine_b_id, "curp")]["valorNumero"] is None, "valorNumero convierte los números y solo ellos")
s2 = por[(acta_id, "socios[2].nombre")]
ok(s2["fila"] == 2 and s2["nombreCampo"] == "nombre" and s2["tipoDocumental"] == "Acta constitutiva", "lo que se repite trae su fila y el nombre del campo hoja")

shutil.rmtree(RAIZ, ignore_errors=True)
print()
print("Todo bien." if not fallos else f"{fallos} fallas.")
sys.exit(1 if fallos else 0)
