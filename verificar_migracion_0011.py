"""¿La migración 0011 (las ejecuciones ya no corren sobre un expediente virtual) cae bien sobre una base que YA tiene ejecuciones con virtual, como dev?

    .venv/Scripts/python verificar_migracion_0011.py
    .venv/Scripts/python verificar_migracion_0011.py --dejar     (deja vivo el contenedor para investigar)

Levanta el SQL Server desechable de `pruebas_sql.py` SOLO hasta la 0010 y le arma, por SQL directo, lo que dev ya tenía: un expediente con dos
documentos, un tipo de expediente virtual y un expediente virtual, un proceso ligado a ese tipo y TRES ejecuciones corridas sobre ese virtual (una sin
el nombre del tipo, que la 0009 dejó opcional; una con sus valores finales). Aplica SOLO la 0011 con el corredor real y comprueba que:

- antes de ella una ejecución sin virtual no cabe (las dos columnas eran NOT NULL) y existía `IX_processRuns_virtualCase`;
- después, `virtualCaseId` y `virtualCaseName` admiten NULL (sin cambiar de tipo ni de largo), el índice ya no está, los demás sí, y la regla
  `CK_processRuns_virtual` existe y está confiada (se validó contra las filas que ya había);
- las ejecuciones de antes se CONSERVAN idénticas, fila por fila (también sus valores finales), y se leen con su virtual completo, también la que
  no traía el nombre de su tipo; las filtra el expediente igual que antes;
- una ejecución nueva, guardada por el servicio (`registrar`), queda con las tres columnas de virtual en NULL y se lee con `virtual: null`, junto a las viejas;
- la base rechaza un virtual a medias (el id sin el nombre, el nombre sin el id, el nombre del tipo sin virtual) y acepta el completo y el que no existe;
- `usrNexus`, el usuario de la API, lee y escribe sin problema.

Después aplica TODO lo que siga en la carpeta de migraciones (la 0012 borra las tablas de los virtuales; la 0013, los grupos) y comprueba que las ejecuciones viejas
siguen idénticas y siguen leyéndose con su virtual (son una foto: no dependen de esas tablas), que el proceso, el expediente y sus documentos siguen ahí
(la 0012 no se los lleva), que las tablas de los virtuales y la liga del proceso ya no existen (si la carpeta trae la 0012) y que la base queda en la
última versión.

Sin Docker, se salta y lo dice: no cuenta como pasada.
"""

import json
import os
import shutil
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pruebas_sql  # noqa: E402

import pyodbc  # noqa: E402

BASE = pruebas_sql.preparar("nexus-migracion-0011", 14343, dejar="--dejar" in sys.argv, hasta=10)

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-migracion-0011-"))
(RAIZ / ".nexus-almacen").touch()
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"

# DESPUÉS de `preparar`: `config.py` lee el entorno una sola vez, y es `preparar` quien deja SQLSERVER_* apuntando al contenedor.
from migraciones.corredor import descubrir  # noqa: E402
from servicios import ejecuciones  # noqa: E402

fallos: list[str] = []


def rev(que: str, ok: bool, extra: str = "") -> None:
    print(f"  {'OK   ' if ok else 'FALLA'}  {que}{(' — ' + str(extra)) if extra and not ok else ''}")
    if not ok:
        fallos.append(que)


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


def terminar() -> None:
    shutil.rmtree(RAIZ, ignore_errors=True)
    print("\nTodo bien." if not fallos else f"\nFALLARON {len(fallos)}:\n  - " + "\n  - ".join(fallos))
    raise SystemExit(1 if fallos else 0)


def aplicar(hasta: int | None) -> tuple[bool, str]:
    """El corredor de verdad. Si no puede aplicar, `pruebas_sql` termina con el motivo: aquí se vuelve un fallo de la prueba y no una excepción."""
    try:
        BASE["aplicar"](hasta)
        return True, ""
    except SystemExit as exc:
        return False, str(exc)


def intenta(que: str, funcion):
    """Corre `funcion`; si truena, lo anota como fallo de `que` (con el error) y devuelve `None`: una migración mal hecha tiene que fallar con su
    aserción y no con un traceback."""
    try:
        return funcion()
    except Exception as exc:  # noqa: BLE001
        rev(que, False, f"{type(exc).__name__}: {str(exc)[:300]}")
        return None


def rechaza(cur, sql: str, *params) -> str | None:
    """El texto del error si la base rechaza la sentencia; `None` si la acepta."""
    try:
        cur.execute(sql, *params)
        return None
    except pyodbc.Error as exc:
        return str(exc)


ORG = "org-0011"
TD = "tipo-" + "1" * 32
TE = "tipo-expediente-" + "2" * 32
EXP = "expediente-" + "3" * 32
VC = "expediente-virtual-" + "7" * 32
PROCESO = "proceso-prueba-0011"
D1, D2 = ("documento-" + c * 32 for c in "45")
A, B, C = ("ejecucion-" + c * 32 for c in "abc")
DIBUJO = json.dumps(
    {
        "nombre": "Revisar solicitud", "descripcion": "", "activo": True,
        "nodos": [
            {"id": "tipo-ine", "type": "tipo-documental", "position": {"x": 0, "y": 0}, "data": {"tipoId": TD}},
            {"id": "operador-1", "type": "operador", "position": {"x": 300, "y": 0}, "data": {"operador": "existe", "a": "si", "b": ""}},
            {"id": "valor-final-1", "type": "valor-final", "position": {"x": 700, "y": 0}, "data": {"nombre": "Hay INE", "a": ""}},
            {"id": "resultado-final", "type": "resultado-final", "position": {"x": 1000, "y": 0}, "data": {"si": "Aprobado", "no": "Rechazado", "sin": "Pendiente"}},
        ],
        "aristas": [
            {"id": "c1", "source": "tipo-ine", "sourceHandle": "@documento", "target": "operador-1", "targetHandle": "a"},
            {"id": "c2", "source": "operador-1", "sourceHandle": "resultado", "target": "valor-final-1", "targetHandle": "a"},
            {"id": "c3", "source": "operador-1", "sourceHandle": "resultado", "target": "resultado-final", "targetHandle": "a"},
        ],
    },
    ensure_ascii=False,
)
VIRTUAL = {"id": VC, "nombre": "Solicitud de Juan", "tipoNombre": "Solicitud de crédito"}

sa = BASE["conectar"]("sa", BASE["sa"], BASE["base"]).cursor()  # el catálogo se lee con el administrador; los datos, con el usuario de la API


def columnas() -> dict[str, tuple]:
    """`{columna: (tipo, largo en caracteres, ¿admite NULL?)}` de `nexus.processRuns`, en el orden de la tabla."""
    filas = sa.execute(
        "SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = 'nexus' AND TABLE_NAME = 'processRuns' ORDER BY ORDINAL_POSITION"
    ).fetchall()
    return {f[0]: (f[1], f[2], f[3] == "YES") for f in filas}


def indices() -> set[str]:
    return {f[0] for f in sa.execute("SELECT name FROM sys.indexes WHERE object_id = OBJECT_ID('nexus.processRuns') AND name IS NOT NULL").fetchall()}


def regla():
    return sa.execute("SELECT is_not_trusted, is_disabled FROM sys.check_constraints WHERE name = 'CK_processRuns_virtual' AND parent_object_id = OBJECT_ID('nexus.processRuns')").fetchone()


def version() -> int:
    return sa.execute("SELECT MAX(version) FROM nexus.schemaMigrations").fetchone()[0]


titulo("1 · Lo que dev ya tenía con la 0010: ejecuciones corridas sobre un expediente virtual")
con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
k = con.cursor()
k.execute("INSERT INTO nexus.documentTypes (publicId, tenant, revision, dataJson, createdBy) VALUES (?, ?, 1, ?, 'x')", TD, ORG, '{"nombre":"INE","estado":"activo"}')
k.execute("INSERT INTO nexus.virtualCaseTypes (publicId, tenant, name, createdBy) VALUES (?, ?, N'Solicitud de crédito', 'x')", TE, ORG)
id_te = k.execute("SELECT virtualCaseTypeId FROM nexus.virtualCaseTypes WHERE publicId = ?", TE).fetchone()[0]
k.execute("INSERT INTO nexus.virtualCaseTypeDocumentTypes (virtualCaseTypeId, documentTypePublicId, position) VALUES (?, ?, 0)", id_te, TD)
k.execute("INSERT INTO nexus.cases (publicId, tenant, name, createdBy) VALUES (?, ?, N'Juan Pérez', 'x')", EXP, ORG)
id_exp = k.execute("SELECT caseId FROM nexus.cases WHERE publicId = ?", EXP).fetchone()[0]
ids_doc = {}
for d in (D1, D2):
    k.execute("INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel) VALUES (?, ?, ?, 'org/x', 10, 'application/pdf', 'x.pdf', 'API')", d, ORG, "a" * 64)
    ids_doc[d] = k.execute("SELECT documentId FROM nexus.documents WHERE publicId = ?", d).fetchone()[0]
    k.execute("INSERT INTO nexus.caseDocuments (caseId, documentId, addedBy) VALUES (?, ?, 'x')", id_exp, ids_doc[d])
k.execute("INSERT INTO nexus.virtualCases (publicId, tenant, caseId, virtualCaseTypeId, name, createdBy) VALUES (?, ?, ?, ?, N'Solicitud de Juan', 'x')", VC, ORG, id_exp, id_te)
id_vc = k.execute("SELECT virtualCaseId FROM nexus.virtualCases WHERE publicId = ?", VC).fetchone()[0]
k.execute("INSERT INTO nexus.virtualCaseDocuments (virtualCaseId, caseId, documentId, addedBy) VALUES (?, ?, ?, 'x')", id_vc, id_exp, ids_doc[D1])
k.execute("INSERT INTO nexus.processes (publicId, tenant, revision, dataJson, virtualCaseTypePublicId, createdBy) VALUES (?, ?, 3, ?, ?, 'x')", PROCESO, ORG, DIBUJO, TE)
for n, (publico, caso, texto, estado, tipo_virtual) in enumerate(
    ((A, "si", "Aprobado", "completa", "Solicitud de crédito"), (B, "no", "Rechazado", "completa", None), (C, "sin", "Pendiente", "incompleta", "Solicitud de crédito"))
):
    k.execute(
        "INSERT INTO nexus.processRuns (publicId, tenant, caseId, caseName, virtualCaseId, virtualCaseName, virtualCaseTypeName, processId, processName, processRevision, "
        "outcomeCase, outcomeText, status, detailJson, definitionJson, executedAt, executedBy) VALUES (?, ?, ?, N'Juan Pérez', ?, N'Solicitud de Juan', ?, ?, N'Revisar solicitud', 3, "
        "?, ?, ?, '{\"reglas\":[]}', ?, ?, 'persona-de-antes')",
        publico, ORG, EXP, VC, tipo_virtual, PROCESO, caso, texto, estado, DIBUJO, datetime(2026, 11, 1, 10, n, 0),
    )
id_a = k.execute("SELECT processRunId FROM nexus.processRuns WHERE publicId = ?", A).fetchone()[0]
k.execute(
    "INSERT INTO nexus.processRunFinalValues (processRunId, position, nodeId, name, kind, family, status, valueText, valueJson, origin) VALUES (?, 1, 'valor-final-1', N'Hay INE', 'veredicto', NULL, 'listo', N'Pasa', NULL, N'Existe documento #1')",
    id_a,
)


def corridas_de_antes() -> list[tuple]:
    return [tuple(f) for f in k.execute("SELECT * FROM nexus.processRuns WHERE publicId IN (?, ?, ?) ORDER BY processRunId", A, B, C).fetchall()]


def valores_de_antes() -> list[tuple]:
    """Los valores finales de la ejecución de antes (la nueva, que se guarda más abajo, también trae los suyos y no cuenta aquí)."""
    return [tuple(f) for f in k.execute("SELECT * FROM nexus.processRunFinalValues WHERE processRunId = ? ORDER BY finalValueId", id_a).fetchall()]


antes_corridas, antes_valores = corridas_de_antes(), valores_de_antes()
antes_columnas = columnas()
antes_indices = indices()
rev("hay un mundo de antes: tres ejecuciones sobre un virtual (una sin el nombre de su tipo) y una con un valor final", len(antes_corridas) == 3 and len(antes_valores) == 1)
rev("las dos columnas del virtual eran NOT NULL (`virtualCaseTypeName` ya admitía NULL)", antes_columnas["virtualCaseId"][2] is False and antes_columnas["virtualCaseName"][2] is False and antes_columnas["virtualCaseTypeName"][2] is True, str({c_: antes_columnas[c_] for c_ in antes_columnas if c_.startswith("virtualCase")}))
rev("y existía el índice por virtual", "IX_processRuns_virtualCase" in antes_indices, str(sorted(antes_indices)))
sin_virtual = dict(
    publicId="ejecucion-" + uuid.uuid4().hex, tenant=ORG, caseId=EXP, caseName="Juan Pérez", processId=PROCESO, processName="Revisar solicitud", processRevision=3,
    outcomeCase="si", outcomeText="Aprobado", status="completa", detailJson="{}", definitionJson=DIBUJO, executedBy="prueba",
)


def insertar(**cambios) -> str | None:
    """Mete una ejecución con `usrNexus`; el texto del error si la base la rechaza, `None` si entra. Parte de una SIN virtual."""
    valores = {**sin_virtual, "publicId": "ejecucion-" + uuid.uuid4().hex, **cambios}
    return rechaza(k, f"INSERT INTO nexus.processRuns ({', '.join(valores)}) VALUES ({', '.join('?' * len(valores))})", *valores.values())


rev("antes de la 0011 una ejecución SIN virtual no cabe (la base la rechaza)", insertar() is not None)
con.close()

titulo("2 · La 0011, con el corredor real (solo ella)")
ok, motivo = aplicar(11)
rev("el corredor aplica la 0011", ok, motivo)
if not ok:
    terminar()
sa.execute("SELECT 1").fetchone()
rev("la base dice versión 11 y la anotó como ejecutada, con su nombre", sa.execute("SELECT MAX(version) FROM nexus.schemaMigrations").fetchone()[0] == 11
    and tuple(sa.execute("SELECT nombre, modo FROM nexus.schemaMigrations WHERE version = 11").fetchone()) == ("ejecuciones_sin_expediente_virtual", "ejecutada"))
despues_columnas = columnas()
rev("`virtualCaseId` y `virtualCaseName` admiten NULL, como `virtualCaseTypeName`", all(despues_columnas[c_][2] for c_ in ("virtualCaseId", "virtualCaseName", "virtualCaseTypeName")), str({c_: despues_columnas[c_] for c_ in despues_columnas if c_.startswith("virtualCase")}))
rev("sin cambiar de tipo ni de largo (varchar(63), nvarchar(200), nvarchar(200))", [despues_columnas[c_][:2] for c_ in ("virtualCaseId", "virtualCaseName", "virtualCaseTypeName")] == [("varchar", 63), ("nvarchar", 200), ("nvarchar", 200)], str(despues_columnas))
rev("la tabla conserva TODAS sus columnas, en el mismo orden, y solo cambió la nulabilidad de esas dos",
    list(despues_columnas) == list(antes_columnas) and {c_ for c_ in antes_columnas if antes_columnas[c_] != despues_columnas[c_]} == {"virtualCaseId", "virtualCaseName"}, str({c_ for c_ in antes_columnas if antes_columnas[c_] != despues_columnas.get(c_)}))
despues_indices = indices()
rev("`IX_processRuns_virtualCase` ya no existe y NO se recreó", "IX_processRuns_virtualCase" not in despues_indices, str(sorted(despues_indices)))
rev("los demás índices siguen (el de la organización y el del proceso), junto con la llave y el id público", {"IX_processRuns_tenant", "IX_processRuns_process", "PK_processRuns", "UQ_processRuns_publicId"} <= despues_indices and despues_indices == antes_indices - {"IX_processRuns_virtualCase"}, str(sorted(despues_indices)))
estado_regla = regla()
rev("existe `CK_processRuns_virtual`, activa y CONFIADA (se validó contra lo que ya había)", estado_regla is not None and tuple(estado_regla) == (False, False), str(estado_regla))

titulo("3 · Lo de antes se conserva, y se lee con su virtual")
con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
k = con.cursor()
rev("las tres ejecuciones de antes siguen idénticas, fila por fila (todas las columnas, también cuándo y quién)", corridas_de_antes() == antes_corridas)
rev("y su valor final también", valores_de_antes() == antes_valores)
rev("con su virtual a la vista en la tabla", [(f[0], f[1], f[2]) for f in k.execute("SELECT virtualCaseId, virtualCaseName, virtualCaseTypeName FROM nexus.processRuns WHERE publicId IN (?, ?, ?) ORDER BY processRunId", A, B, C).fetchall()]
    == [(VC, "Solicitud de Juan", "Solicitud de crédito"), (VC, "Solicitud de Juan", None), (VC, "Solicitud de Juan", "Solicitud de crédito")])
lista = intenta("el servicio lee el historial después de la 0011", lambda: ejecuciones.listar(ORG))
por_id = {e["id"]: e for e in (lista or {"ejecuciones": []})["ejecuciones"]}
rev("el servicio las lista (y solo esas tres, de la más reciente a la más antigua)", lista is not None and [e["id"] for e in lista["ejecuciones"]] == [C, B, A] and lista["total"] == 3, str(lista)[:300])
rev("cada una sale con su virtual completo: id, nombre y nombre del tipo", all(por_id.get(x, {}).get("virtual") == VIRTUAL for x in (A, C)), str({x: por_id.get(x, {}).get("virtual") for x in (A, C)}))
rev("la que no traía el nombre de su tipo sale con `tipoNombre: null` (sigue siendo un virtual)", por_id.get(B, {}).get("virtual") == {**VIRTUAL, "tipoNombre": None}, str(por_id.get(B, {}).get("virtual")))
abierta = intenta("el servicio abre una ejecución de antes", lambda: ejecuciones.obtener(ORG, A))
rev("abrir una de antes: el mismo virtual, su expediente, sus valores finales y la foto del proceso",
    abierta is not None and abierta["virtual"] == VIRTUAL and abierta["expediente"] == {"id": EXP, "nombre": "Juan Pérez"} and abierta["proceso"] == {"id": PROCESO, "nombre": "Revisar solicitud", "revision": 3}
    and [v["nombre"] for v in abierta["valoresFinales"]] == ["Hay INE"] and abierta["definicion"]["nombre"] == "Revisar solicitud", str(abierta)[:300])
rev("filtrar por el expediente trae las de antes igual que siempre", intenta("filtrar por expediente", lambda: ejecuciones.listar(ORG, expediente=EXP)["total"]) == 3)

titulo("4 · Una ejecución NUEVA no lleva virtual (la guarda el servicio de verdad)")
entrada = {
    "expedienteId": EXP, "procesoId": PROCESO, "procesoRevision": 3, "resultado": {"caso": "si", "texto": "Aprobado"},
    "valoresFinales": [{"nodoId": "valor-final-1", "nombre": "Hay INE", "forma": "veredicto", "familia": None, "estado": "listo", "valor": "Pasa", "lista": None, "origen": "Existe documento #1"}],
    "detalle": {"reglas": []},
}
nueva = intenta("el servicio guarda una ejecución sin virtual después de la 0011", lambda: ejecuciones.registrar(ORG, "usuario-de-prueba", entrada))
rev("`registrar` la guarda y la devuelve con `virtual: null` y su expediente", nueva is not None and nueva["virtual"] is None and nueva["expediente"] == {"id": EXP, "nombre": "Juan Pérez"} and nueva["resultado"] == {"caso": "si", "texto": "Aprobado"}, str(nueva)[:300])
if nueva is not None:
    fila = k.execute("SELECT virtualCaseId, virtualCaseName, virtualCaseTypeName FROM nexus.processRuns WHERE publicId = ?", nueva["id"]).fetchone()
    rev("en la tabla, las tres columnas de virtual de la nueva quedaron en NULL", tuple(fila) == (None, None, None), str(tuple(fila)))
    lista = intenta("el servicio lee el historial con viejas y nueva", lambda: ejecuciones.listar(ORG))
    rev("el historial junta las cuatro: la nueva primero, sin virtual, y las de antes con el suyo",
        lista is not None and [e["id"] for e in lista["ejecuciones"]] == [nueva["id"], C, B, A] and lista["ejecuciones"][0]["virtual"] is None and [e["virtual"] is not None for e in lista["ejecuciones"][1:]] == [True, True, True], str(lista)[:300])
    rev("y se lee al abrirla, igual", (intenta("abrir la nueva", lambda: ejecuciones.obtener(ORG, nueva["id"])) or {}).get("virtual", "falta") is None)

titulo("5 · La base no deja un virtual a medias")
rev("una ejecución sin virtual entra", insertar() is None)
rev("un virtual COMPLETO entra, con el nombre de su tipo", insertar(virtualCaseId=VC, virtualCaseName="Solicitud de Juan", virtualCaseTypeName="Solicitud de crédito") is None)
rev("…y sin el nombre de su tipo (así eran algunas de antes)", insertar(virtualCaseId=VC, virtualCaseName="Solicitud de Juan") is None)
for que, cambios in (
    ("el id del virtual sin su nombre", {"virtualCaseId": VC}),
    ("el nombre del virtual sin su id", {"virtualCaseName": "Solicitud de Juan"}),
    ("el nombre del tipo del virtual sin virtual", {"virtualCaseTypeName": "Solicitud de crédito"}),
    ("el id y el tipo, pero sin nombre", {"virtualCaseId": VC, "virtualCaseTypeName": "Solicitud de crédito"}),
    ("el nombre y el tipo, pero sin id", {"virtualCaseName": "Solicitud de Juan", "virtualCaseTypeName": "Solicitud de crédito"}),
):
    error = insertar(**cambios)
    rev(f"{que}: la base lo rechaza (`CK_processRuns_virtual`)", error is not None and "CK_processRuns_virtual" in error, str(error)[:200])
rev("las demás reglas de la tabla siguen en pie (un caso que no existe)", "CK_processRuns_outcomeCase" in (insertar(outcomeCase="xx") or ""))
con.close()

titulo("6 · Lo que sigue en la carpeta de migraciones (la 0012 borra las tablas de los virtuales; la 0013, los grupos)")
ok, motivo = aplicar(None)
rev("el corredor aplica lo que falte", ok, motivo)
if ok:
    esperadas = len(descubrir())
    rev(f"la base queda en la última versión de la carpeta ({esperadas})", version() == esperadas, str(version()))
    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    k = con.cursor()
    rev("las ejecuciones de antes siguen idénticas, fila por fila", corridas_de_antes() == antes_corridas)
    rev("y su valor final", valores_de_antes() == antes_valores)
    lista = intenta("el servicio lee el historial después de todas las migraciones", lambda: ejecuciones.listar(ORG))
    rev("se siguen leyendo con su virtual (es una foto: no depende de las tablas de los virtuales)",
        lista is not None and {e["id"]: e["virtual"] for e in lista["ejecuciones"] if e["id"] in (A, B, C)} == {A: VIRTUAL, B: {**VIRTUAL, "tipoNombre": None}, C: VIRTUAL}, str(lista)[:300])
    rev("la regla y la nulabilidad siguen donde las dejó la 0011", regla() is not None and tuple(regla()) == (False, False) and all(columnas()[c_][2] for c_ in ("virtualCaseId", "virtualCaseName")))
    proceso_f = k.execute("SELECT revision, name, dataJson FROM nexus.processes WHERE publicId = ?", PROCESO).fetchone()
    rev("el proceso sigue ahí, con su revisión y su dibujo (lo que se borre de los virtuales no se lo lleva)", proceso_f is not None and proceso_f[0] == 3 and proceso_f[1] == "Revisar solicitud" and proceso_f[2] == DIBUJO, str(proceso_f)[:200])
    rev("y el expediente con sus dos documentos, y los documentos mismos",
        k.execute("SELECT name FROM nexus.cases WHERE publicId = ?", EXP).fetchone()[0] == "Juan Pérez" and k.execute("SELECT count(*) FROM nexus.caseDocuments WHERE caseId = ?", id_exp).fetchone()[0] == 2
        and k.execute("SELECT count(*) FROM nexus.documents WHERE publicId IN (?, ?)", D1, D2).fetchone()[0] == 2)
    if any(m.version == 12 for m in descubrir()):
        hay = [sa.execute("SELECT OBJECT_ID(?, 'U')", f"nexus.{t_}").fetchone()[0] is not None for t_ in ("virtualCases", "virtualCaseDocuments", "virtualCaseTypes", "virtualCaseTypeDocumentTypes")]
        liga = sa.execute("SELECT COL_LENGTH('nexus.processes', 'virtualCaseTypePublicId')").fetchone()[0]
        rev("con la 0012 puesta, las cuatro tablas de los virtuales y la liga del proceso con su tipo ya no existen", not any(hay) and liga is None, f"{hay} {liga}")
    rev("el servicio sigue guardando ejecuciones sin virtual", intenta("registrar de nuevo", lambda: ejecuciones.registrar(ORG, "usuario-de-prueba", entrada)) is not None)
    con.close()

terminar()
