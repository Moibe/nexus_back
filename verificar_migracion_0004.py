"""¿La migración 0004 (expedientes virtuales) cae bien sobre una base que YA tiene datos de la 0003, como dev?

    .venv/Scripts/python verificar_migracion_0004.py

Levanta el SQL Server desechable de `pruebas_sql.py` SOLO hasta la 0003, le mete lo que dev ya tenía (un tipo documental, un tipo de expediente
con su esquema, un expediente de ese tipo con dos documentos), aplica la 0004 con el corredor real y comprueba que:

- el expediente sigue ahí, con sus documentos, y ya sin tipo;
- el tipo de expediente pasó a ser tipo de expediente VIRTUAL con su mismo id, su nombre y su esquema;
- un expediente virtual solo puede tener documentos de SU expediente (lo impide la base), y sacar un documento del expediente lo saca también
  del virtual; borrar el virtual se lleva sus renglones; un tipo con virtuales no se puede borrar;
- `usrNexus`, el usuario de la API, lee y escribe las tablas renombradas y las nuevas.

Sin Docker, se salta y lo dice: no cuenta como pasada.
"""

import sys

sys.stdout.reconfigure(encoding="utf-8")

import pruebas_sql  # noqa: E402

import pyodbc  # noqa: E402

BASE = pruebas_sql.preparar("nexus-migracion-0004", 14334, dejar="--dejar" in sys.argv, hasta=3)
fallos: list[str] = []


def rev(que: str, ok: bool, extra: str = "") -> None:
    print(f"  {'OK   ' if ok else 'FALLA'}  {que}{(' — ' + extra) if extra and not ok else ''}")
    if not ok:
        fallos.append(que)


def rechaza(cur, sql: str, *params) -> bool:
    try:
        cur.execute(sql, *params)
        return False
    except pyodbc.Error:
        return True


TD = "tipo-" + "1" * 32
TE = "tipo-expediente-" + "2" * 32
EXP = "expediente-" + "3" * 32
D1, D2, D3 = ("documento-" + c * 32 for c in "456")

print("\n=== 1 · Lo que dev ya tenía con la 0003 ===")
con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
k = con.cursor()
k.execute("INSERT INTO nexus.documentTypes (publicId, tenant, revision, dataJson, createdBy) VALUES (?, 'org', 1, ?, 'x')", TD, '{"nombre":"INE","estado":"activo"}')
k.execute("INSERT INTO nexus.caseTypes (publicId, tenant, name, createdBy) VALUES (?, 'org', N'Expediente médico', 'x')", TE)
id_te = k.execute("SELECT caseTypeId FROM nexus.caseTypes WHERE publicId = ?", TE).fetchone()[0]
k.execute("INSERT INTO nexus.caseTypeDocumentTypes (caseTypeId, documentTypePublicId, position) VALUES (?, ?, 0)", id_te, TD)
k.execute("INSERT INTO nexus.cases (publicId, tenant, caseTypeId, name, createdBy) VALUES (?, 'org', ?, N'Juan Pérez', 'x')", EXP, id_te)
for d in (D1, D2, D3):
    k.execute(
        "INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel) VALUES (?, 'org', ?, 'org/x', 10, 'application/pdf', 'x.pdf', 'API')",
        d, "a" * 64,
    )
id_exp = k.execute("SELECT caseId FROM nexus.cases WHERE publicId = ?", EXP).fetchone()[0]
ids_doc = {d: k.execute("SELECT documentId FROM nexus.documents WHERE publicId = ?", d).fetchone()[0] for d in (D1, D2, D3)}
for d in (D1, D2):
    k.execute("INSERT INTO nexus.caseDocuments (caseId, documentId, addedBy) VALUES (?, ?, 'x')", id_exp, ids_doc[d])
con.close()
rev("se armó lo de la 0003: un tipo con esquema y un expediente de ese tipo con dos documentos", True)

print("\n=== 2 · La 0004, con el corredor real ===")
BASE["aplicar"](4)
con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
k = con.cursor()
rev("la base dice versión 4", k.execute("SELECT MAX(version) FROM nexus.schemaMigrations").fetchone()[0] == 4)
rev("el expediente sigue ahí, con su nombre", k.execute("SELECT name FROM nexus.cases WHERE publicId = ?", EXP).fetchone()[0] == "Juan Pérez")
rev("…y con sus dos documentos", k.execute("SELECT count(*) FROM nexus.caseDocuments WHERE caseId = ?", id_exp).fetchone()[0] == 2)
rev("el expediente ya no tiene columna de tipo", k.execute("SELECT COL_LENGTH('nexus.cases', 'caseTypeId')").fetchone()[0] is None)
rev("las tablas viejas de tipos ya no existen con ese nombre", tuple(k.execute("SELECT OBJECT_ID('nexus.caseTypes'), OBJECT_ID('nexus.caseTypeDocumentTypes')").fetchone()) == (None, None))
fila = k.execute("SELECT virtualCaseTypeId, name FROM nexus.virtualCaseTypes WHERE publicId = ?", TE).fetchone()
rev("el tipo es ahora tipo de expediente VIRTUAL, con su mismo id y su nombre", fila is not None and fila[1] == "Expediente médico", str(fila))
id_tv = fila[0]
rev("…y conserva su esquema", [r[0] for r in k.execute("SELECT documentTypePublicId FROM nexus.virtualCaseTypeDocumentTypes WHERE virtualCaseTypeId = ?", id_tv).fetchall()] == [TD])
rev("los nombres de las restricciones también se renombraron", k.execute("SELECT count(*) FROM sys.objects WHERE schema_id = SCHEMA_ID('nexus') AND name LIKE '%caseType%' AND name NOT LIKE '%virtualCaseType%'").fetchone()[0] == 0)

print("\n=== 3 · El subconjunto lo cuida la base ===")
VC = "expediente-virtual-" + "7" * 32
k.execute("INSERT INTO nexus.virtualCases (publicId, tenant, caseId, virtualCaseTypeId, name, createdBy) VALUES (?, 'org', ?, ?, N'Solicitud de crédito', 'x')", VC, id_exp, id_tv)
id_vc = k.execute("SELECT virtualCaseId FROM nexus.virtualCases WHERE publicId = ?", VC).fetchone()[0]
rev("usrNexus crea un expediente virtual", True)
rev("meterle un documento DE SU expediente se puede", not rechaza(k, "INSERT INTO nexus.virtualCaseDocuments (virtualCaseId, caseId, documentId, addedBy) VALUES (?, ?, ?, 'x')", id_vc, id_exp, ids_doc[D1]))
rev("uno que NO está en su expediente, no (llave foránea)", rechaza(k, "INSERT INTO nexus.virtualCaseDocuments (virtualCaseId, caseId, documentId, addedBy) VALUES (?, ?, ?, 'x')", id_vc, id_exp, ids_doc[D3]))
k.execute("INSERT INTO nexus.cases (publicId, tenant, name, createdBy) VALUES (?, 'org', N'Otro', 'x')", "expediente-" + "8" * 32)
id_otro = k.execute("SELECT caseId FROM nexus.cases WHERE publicId = ?", "expediente-" + "8" * 32).fetchone()[0]
k.execute("INSERT INTO nexus.caseDocuments (caseId, documentId, addedBy) VALUES (?, ?, 'x')", id_otro, ids_doc[D3])
rev("ni haciéndolo pasar por OTRO expediente donde sí está (el virtual va amarrado a su expediente)", rechaza(k, "INSERT INTO nexus.virtualCaseDocuments (virtualCaseId, caseId, documentId, addedBy) VALUES (?, ?, ?, 'x')", id_vc, id_otro, ids_doc[D3]))
rev("el mismo documento dos veces en el mismo virtual, no", rechaza(k, "INSERT INTO nexus.virtualCaseDocuments (virtualCaseId, caseId, documentId, addedBy) VALUES (?, ?, ?, 'x')", id_vc, id_exp, ids_doc[D1]))
k.execute("DELETE FROM nexus.caseDocuments WHERE caseId = ? AND documentId = ?", id_exp, ids_doc[D1])
rev("sacar el documento del expediente lo saca también del virtual", k.execute("SELECT count(*) FROM nexus.virtualCaseDocuments WHERE virtualCaseId = ?", id_vc).fetchone()[0] == 0)
rev("borrar un tipo con expedientes virtuales lo impide la base", rechaza(k, "DELETE FROM nexus.virtualCaseTypes WHERE virtualCaseTypeId = ?", id_tv))
rev("…y borrar el expediente que tiene virtuales, también", rechaza(k, "DELETE FROM nexus.cases WHERE caseId = ?", id_exp))
k.execute("INSERT INTO nexus.virtualCaseDocuments (virtualCaseId, caseId, documentId, addedBy) VALUES (?, ?, ?, 'x')", id_vc, id_exp, ids_doc[D2])
k.execute("DELETE FROM nexus.virtualCases WHERE virtualCaseId = ?", id_vc)
rev("borrar el expediente virtual se lleva sus renglones, y el documento sigue en el expediente",
    k.execute("SELECT count(*) FROM nexus.virtualCaseDocuments").fetchone()[0] == 0
    and k.execute("SELECT count(*) FROM nexus.caseDocuments WHERE caseId = ? AND documentId = ?", id_exp, ids_doc[D2]).fetchone()[0] == 1)
rev("un id de expediente virtual mal formado no entra", rechaza(k, "INSERT INTO nexus.virtualCases (publicId, tenant, caseId, virtualCaseTypeId, name, createdBy) VALUES (?, 'org', ?, ?, 'x', 'x')", "expediente-" + "9" * 32, id_exp, id_tv))
con.close()

print("\nTodo bien." if not fallos else f"\nFALLARON {len(fallos)}:\n  - " + "\n  - ".join(fallos))
raise SystemExit(1 if fallos else 0)
