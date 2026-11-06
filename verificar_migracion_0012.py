"""¿La migración 0012 (se borran las tablas de los expedientes virtuales) cae bien sobre una base que YA tiene virtuales, como dev?

    .venv/Scripts/python verificar_migracion_0012.py

Levanta el SQL Server desechable de `pruebas_sql.py` SOLO hasta la 0010 y le arma lo que dev ya tenía: un tipo documental, un tipo de expediente virtual con su
esquema, un expediente con dos documentos, un expediente virtual con uno de ellos, un proceso ligado a ese tipo y una ejecución vieja que dice sobre qué
virtual se corrió. Aplica el resto (la 0011 y la 0012) con el corredor real —el mismo usuario de DDL de dev, para que también se pruebe que puede borrar lo
que creó— y comprueba que:

- las cuatro tablas de los virtuales y la columna con que un proceso se ligaba a su tipo ya no existen, y con ellas se fueron sus llaves foráneas;
- todo lo demás sigue ahí tal cual: el expediente, sus documentos y la pertenencia, el tipo documental, el proceso (con su dibujo y su revisión) y la
  ejecución vieja con la foto de su virtual;
- `usrNexus`, el usuario de la API, sigue leyendo y escribiendo lo que queda (un proceso sin liga, sacar un documento del expediente, una ejecución
  nueva sin virtual).

Sin Docker, se salta y lo dice: no cuenta como pasada.
"""

import sys

sys.stdout.reconfigure(encoding="utf-8")

import pruebas_sql  # noqa: E402

BASE = pruebas_sql.preparar("nexus-migracion-0012", 14339, dejar="--dejar" in sys.argv, hasta=10)
fallos: list[str] = []


def rev(que: str, ok: bool, extra: str = "") -> None:
    print(f"  {'OK   ' if ok else 'FALLA'}  {que}{(' — ' + extra) if extra and not ok else ''}")
    if not ok:
        fallos.append(que)


TD = "tipo-" + "1" * 32
TE = "tipo-expediente-" + "2" * 32
EXP = "expediente-" + "3" * 32
VC = "expediente-virtual-" + "7" * 32
PROCESO = "proceso-prueba-0012"
EJECUCION = "ejecucion-" + "9" * 32
D1, D2 = ("documento-" + c * 32 for c in "45")
DIBUJO = '{"nombre":"Revisar solicitud","descripcion":"","nodos":[],"aristas":[],"tipoExpedienteVirtualId":null}'

TABLAS_DE_VIRTUALES = ("virtualCases", "virtualCaseDocuments", "virtualCaseTypes", "virtualCaseTypeDocumentTypes")

print("\n=== 1 · Lo que dev ya tenía con la 0010 ===")
con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
k = con.cursor()
k.execute("INSERT INTO nexus.documentTypes (publicId, tenant, revision, dataJson, createdBy) VALUES (?, 'org', 1, ?, 'x')", TD, '{"nombre":"INE","estado":"activo"}')
k.execute("INSERT INTO nexus.virtualCaseTypes (publicId, tenant, name, createdBy) VALUES (?, 'org', N'Solicitud de crédito', 'x')", TE)
id_te = k.execute("SELECT virtualCaseTypeId FROM nexus.virtualCaseTypes WHERE publicId = ?", TE).fetchone()[0]
k.execute("INSERT INTO nexus.virtualCaseTypeDocumentTypes (virtualCaseTypeId, documentTypePublicId, position) VALUES (?, ?, 0)", id_te, TD)
k.execute("INSERT INTO nexus.cases (publicId, tenant, name, createdBy) VALUES (?, 'org', N'Juan Pérez', 'x')", EXP)
id_exp = k.execute("SELECT caseId FROM nexus.cases WHERE publicId = ?", EXP).fetchone()[0]
ids_doc = {}
for d in (D1, D2):
    k.execute(
        "INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel) VALUES (?, 'org', ?, 'org/x', 10, 'application/pdf', 'x.pdf', 'API')",
        d, "a" * 64,
    )
    ids_doc[d] = k.execute("SELECT documentId FROM nexus.documents WHERE publicId = ?", d).fetchone()[0]
    k.execute("INSERT INTO nexus.caseDocuments (caseId, documentId, addedBy) VALUES (?, ?, 'x')", id_exp, ids_doc[d])
k.execute("INSERT INTO nexus.virtualCases (publicId, tenant, caseId, virtualCaseTypeId, name, createdBy) VALUES (?, 'org', ?, ?, N'Solicitud de Juan', 'x')", VC, id_exp, id_te)
id_vc = k.execute("SELECT virtualCaseId FROM nexus.virtualCases WHERE publicId = ?", VC).fetchone()[0]
k.execute("INSERT INTO nexus.virtualCaseDocuments (virtualCaseId, caseId, documentId, addedBy) VALUES (?, ?, ?, 'x')", id_vc, id_exp, ids_doc[D1])
k.execute("INSERT INTO nexus.processes (publicId, tenant, revision, dataJson, virtualCaseTypePublicId, createdBy) VALUES (?, 'org', 3, ?, ?, 'x')", PROCESO, DIBUJO, TE)
k.execute(
    "INSERT INTO nexus.processRuns (publicId, tenant, caseId, caseName, virtualCaseId, virtualCaseName, virtualCaseTypeName, processId, processName, processRevision, "
    "outcomeCase, outcomeText, status, detailJson, definitionJson, executedBy) VALUES (?, 'org', ?, N'Juan Pérez', ?, N'Solicitud de Juan', N'Solicitud de crédito', ?, "
    "N'Revisar solicitud', 3, 'si', N'Aprobado', 'completa', '{}', ?, 'x')",
    EJECUCION, EXP, VC, PROCESO, DIBUJO,
)
rev(
    "hay un mundo con virtuales: la liga del proceso, el virtual con su documento y la ejecución que lo nombra",
    k.execute("SELECT count(*) FROM nexus.virtualCaseDocuments").fetchone()[0] == 1
    and k.execute("SELECT virtualCaseTypePublicId FROM nexus.processes WHERE publicId = ?", PROCESO).fetchone()[0] == TE,
)
con.close()

print("\n=== 2 · La 0011 y la 0012, con el corredor real ===")
BASE["aplicar"](None)
con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
k = con.cursor()
rev("la base dice versión 12", k.execute("SELECT MAX(version) FROM nexus.schemaMigrations").fetchone()[0] == 12)
fila = k.execute("SELECT modo FROM nexus.schemaMigrations WHERE version = 12").fetchone()
rev("la 0012 quedó como ejecutada (no solo marcada)", fila is not None and fila[0] == "ejecutada")

print("\n=== 3 · Lo que se borró ===")
for tabla in TABLAS_DE_VIRTUALES:
    rev(f"la tabla {tabla} ya no existe", k.execute("SELECT OBJECT_ID(?, 'U')", f"nexus.{tabla}").fetchone()[0] is None)
rev("el proceso ya no tiene la columna de su liga con un tipo", k.execute("SELECT COL_LENGTH('nexus.processes', 'virtualCaseTypePublicId')").fetchone()[0] is None)
rev("ni el índice de esa liga", k.execute("SELECT count(*) FROM sys.indexes WHERE name = 'IX_processes_virtualCaseType'").fetchone()[0] == 0)
rev(
    "no queda ninguna llave foránea que hable de virtuales",
    k.execute("SELECT count(*) FROM sys.foreign_keys WHERE name LIKE '%virtual%' OR OBJECT_NAME(parent_object_id) LIKE 'virtual%' OR OBJECT_NAME(referenced_object_id) LIKE 'virtual%'").fetchone()[0] == 0,
)

print("\n=== 4 · Lo que NO se borró ===")
rev("el expediente sigue ahí, con su nombre", k.execute("SELECT name FROM nexus.cases WHERE publicId = ?", EXP).fetchone()[0] == "Juan Pérez")
rev("…con sus dos documentos", k.execute("SELECT count(*) FROM nexus.caseDocuments WHERE caseId = ?", id_exp).fetchone()[0] == 2)
rev("los documentos siguen existiendo", k.execute("SELECT count(*) FROM nexus.documents WHERE publicId IN (?, ?)", D1, D2).fetchone()[0] == 2)
rev("el tipo documental sigue ahí", k.execute("SELECT count(*) FROM nexus.documentTypes WHERE publicId = ?", TD).fetchone()[0] == 1)
fila = k.execute("SELECT revision, name, dataJson FROM nexus.processes WHERE publicId = ?", PROCESO).fetchone()
rev("el proceso sigue ahí, con su revisión y su dibujo", fila is not None and fila[0] == 3 and fila[1] == "Revisar solicitud" and fila[2] == DIBUJO)
fila = k.execute("SELECT virtualCaseId, virtualCaseName, virtualCaseTypeName, processName FROM nexus.processRuns WHERE publicId = ?", EJECUCION).fetchone()
rev(
    "la ejecución vieja sigue diciendo sobre qué virtual se corrió (es una foto, no depende de las tablas)",
    fila is not None and fila[0] == VC and fila[1] == "Solicitud de Juan" and fila[2] == "Solicitud de crédito" and fila[3] == "Revisar solicitud",
)

print("\n=== 5 · Lo que sigue funcionando con el usuario de la API ===")
k.execute("INSERT INTO nexus.processes (publicId, tenant, revision, dataJson, createdBy) VALUES ('proceso-nuevo-0012', 'org', 1, ?, 'x')", DIBUJO.replace("Revisar solicitud", "Otro"))
rev("un proceso nuevo se guarda sin ligarse a ningún tipo", k.execute("SELECT count(*) FROM nexus.processes WHERE publicId = 'proceso-nuevo-0012'").fetchone()[0] == 1)
k.execute("DELETE FROM nexus.caseDocuments WHERE caseId = ? AND documentId = ?", id_exp, ids_doc[D1])
rev("sacar un documento del expediente ya no arrastra nada de virtuales", k.execute("SELECT count(*) FROM nexus.caseDocuments WHERE caseId = ?", id_exp).fetchone()[0] == 1)
k.execute(
    "INSERT INTO nexus.processRuns (publicId, tenant, caseId, caseName, processId, processName, processRevision, outcomeCase, outcomeText, status, detailJson, definitionJson, executedBy) "
    "VALUES (?, 'org', ?, N'Juan Pérez', ?, N'Revisar solicitud', 3, 'no', N'Rechazado', 'completa', '{}', ?, 'x')",
    "ejecucion-" + "8" * 32, EXP, PROCESO, DIBUJO,
)
rev("una ejecución nueva, sin virtual, se guarda", k.execute("SELECT virtualCaseId FROM nexus.processRuns WHERE publicId = ?", "ejecucion-" + "8" * 32).fetchone()[0] is None)
con.close()

print()
if fallos:
    print(f"{len(fallos)} FALLA(S):")
    for f in fallos:
        print("  -", f)
    sys.exit(1)
print("Todo bien.")
