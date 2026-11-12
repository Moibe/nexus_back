"""¿La migración 0013 (se borran los grupos de expedientes) cae bien sobre una base que YA tiene grupos, como dev?

    .venv/Scripts/python verificar_migracion_0013.py
    .venv/Scripts/python verificar_migracion_0013.py --dejar     (deja vivo el contenedor para investigar)

Levanta el SQL Server desechable de `pruebas_sql.py` SOLO hasta la 0012 y le arma, por SQL directo, lo que dev ya tenía: dos organizaciones con grupos (uno con el
mismo nombre en las dos y uno vacío), expedientes con grupo y sin él, documentos en ellos (uno en dos expedientes y uno en el «void»), un tipo documental, un
proceso y una ejecución con un valor final sobre un expediente con grupo. Aplica la 0013 con el corredor real —el mismo usuario de DDL de dev, para que también se
pruebe que puede borrar lo que creó— y comprueba que:

- antes de ella el mundo de verdad tenía grupos (la tabla, la columna, su llave foránea compuesta y su índice) y el servicio de expedientes de ahora ya los lee sin
  `grupo` (el código nuevo no depende de que la migración haya corrido);
- después, la tabla `caseGroups`, la columna `cases.caseGroupId`, `FK_cases_caseGroup` e `IX_cases_caseGroup` ya no existen, y NADA MÁS cambió en el catálogo: las
  demás tablas, columnas, llaves foráneas (con el mismo estado de antes: ninguna se apaga ni deja de ser de confianza), índices, reglas y módulos son exactamente
  los de antes, menos lo de los grupos;
- ninguna fila de ninguna otra tabla cambió ni se perdió (foto de TODAS las tablas de `nexus`, fila por fila), y los expedientes, los que tenían grupo y el que no,
  siguen con su nombre, su organización y sus documentos en el mismo orden;
- `usrNexus`, el usuario de la API, lee y escribe lo que queda (un expediente nuevo, su lista, meterle y sacarle un documento), por SQL directo y por el propio
  servicio de expedientes, y la base ya no acepta un expediente con grupo;
- el corredor dejó la fila de la versión 13 (con su nombre, la suma del archivo y como ejecutada) y volver a aplicar no hace nada: la tabla de control queda idéntica.

Sin Docker, se salta y lo dice: no cuenta como pasada.
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pruebas_sql  # noqa: E402

import pyodbc  # noqa: E402

BASE = pruebas_sql.preparar("nexus-migracion-0013", 14341, dejar="--dejar" in sys.argv, hasta=12)

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-migracion-0013-"))
(RAIZ / ".nexus-almacen").touch()
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"

# DESPUÉS de `preparar`: `config.py` lee el entorno una sola vez, y es `preparar` quien deja SQLSERVER_* apuntando al contenedor.
from migraciones.corredor import descubrir  # noqa: E402
from servicios import expedientes  # noqa: E402

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
    """Corre `funcion`; si truena, lo anota como fallo de `que` (con el error) y devuelve `None`: una migración mal hecha tiene que fallar con su aserción y
    no con un traceback."""
    try:
        return funcion()
    except Exception as exc:  # noqa: BLE001
        rev(que, False, f"{type(exc).__name__}: {str(exc)[:300]}")
        return None


def comprueba(que: str, funcion) -> None:
    """Como `rev`, pero de lo que calcula `funcion` (un `bool`, o `(bool, extra)`): si truena —una tabla que ya no existe, una columna que falta—, es una FALLA de
    `que` con el error y no un traceback que se lleve el resto de la prueba."""
    try:
        resultado = funcion()
    except Exception as exc:  # noqa: BLE001
        rev(que, False, f"{type(exc).__name__}: {str(exc)[:300]}")
        return
    ok, extra = resultado if isinstance(resultado, tuple) else (resultado, "")
    rev(que, bool(ok), extra)


def rechaza(cur, sql: str, *params) -> str | None:
    """El texto del error si la base rechaza la sentencia; `None` si la acepta."""
    try:
        cur.execute(sql, *params)
        return None
    except pyodbc.Error as exc:
        return str(exc)


ORG, OTRA = "org-0013", "otra-0013"
G_VENTAS, G_SOPORTE, G_VACIO, G_VENTAS_OTRA = ("grupo-" + c * 32 for c in "abcd")
E_VENTAS, E_SOPORTE, E_SIN, E_OTRA = ("expediente-" + c * 32 for c in "1234")
D1, D2, D3, D4, D5 = ("documento-" + c * 32 for c in "56789")
TD = "tipo-" + "9" * 32
PROCESO = "proceso-prueba-0013"
EJECUCION = "ejecucion-" + "e" * 32
DIBUJO = '{"nombre":"Revisar solicitud","descripcion":"","nodos":[],"aristas":[]}'
# Cada expediente de antes: (id, organización, nombre, grupo con el que vivía o None).
EXPEDIENTES = (
    (E_VENTAS, ORG, "Juan Pérez", G_VENTAS),
    (E_SOPORTE, ORG, "Ana López", G_SOPORTE),
    (E_SIN, ORG, "Expediente de antes", None),
    (E_OTRA, OTRA, "Caso de otra organización", G_VENTAS_OTRA),
)
# Qué documentos tenía cada uno, en el orden en que se metieron.
DOCUMENTOS_DE = {E_VENTAS: [D1, D2], E_SOPORTE: [D3], E_SIN: [D1], E_OTRA: [D4]}

sa = BASE["conectar"]("sa", BASE["sa"], BASE["base"]).cursor()  # el catálogo y las fotos se leen con el administrador; los datos, con el usuario de la API


def catalogo() -> dict:
    """Todo lo que la base dice de su propia forma: tablas, columnas (en orden), llaves foráneas, índices, reglas y módulos."""
    columnas: dict[str, list[tuple]] = {}
    for tabla, col, tipo, largo, nulo, defecto in sa.execute(
        "SELECT TABLE_NAME, COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH, IS_NULLABLE, COLUMN_DEFAULT FROM INFORMATION_SCHEMA.COLUMNS "
        "WHERE TABLE_SCHEMA = 'nexus' ORDER BY TABLE_NAME, ORDINAL_POSITION"
    ).fetchall():
        columnas.setdefault(tabla, []).append((col, tipo, largo, nulo, defecto))
    return {
        "tablas": sorted(f[0] for f in sa.execute("SELECT s.name + '.' + t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id").fetchall()),
        "columnas": columnas,
        "llaves": sorted(
            tuple(f) for f in sa.execute(
                "SELECT name, OBJECT_NAME(parent_object_id), OBJECT_NAME(referenced_object_id), is_disabled, is_not_trusted, delete_referential_action, update_referential_action "
                "FROM sys.foreign_keys"
            ).fetchall()
        ),
        "indices": sorted(
            tuple(f) for f in sa.execute(
                "SELECT OBJECT_NAME(i.object_id), i.name, i.type_desc, i.is_unique FROM sys.indexes i JOIN sys.tables t ON t.object_id = i.object_id WHERE i.name IS NOT NULL"
            ).fetchall()
        ),
        "reglas": sorted(tuple(f) for f in sa.execute("SELECT OBJECT_NAME(parent_object_id), name, definition FROM sys.check_constraints").fetchall()),
        "modulos": sorted(tuple(f) for f in sa.execute("SELECT type_desc, name FROM sys.objects WHERE is_ms_shipped = 0 AND type IN ('V', 'P', 'FN', 'IF', 'TF', 'TR')").fetchall()),
    }


def llaves_sin_confiar() -> list[str]:
    """Las llaves foráneas apagadas o sin confiar. Seis llegan así de antes (la 0006 puso la llave de organización sin revisar lo que ya había: no es de esta
    prueba ni de esta migración); lo que la 0013 no puede hacer es apagar o dejar sin confiar OTRA."""
    return sorted(f[0] for f in sa.execute("SELECT name FROM sys.foreign_keys WHERE is_disabled = 1 OR is_not_trusted = 1").fetchall())


def foto() -> dict[str, list[tuple]]:
    """Todas las filas de todas las tablas de `nexus` (menos las que la 0013 borra y la tabla de control, que cambia a propósito), sin la columna del grupo."""
    tablas = sorted(
        f[0] for f in sa.execute(
            "SELECT t.name FROM sys.tables t JOIN sys.schemas s ON s.schema_id = t.schema_id WHERE s.name = 'nexus' AND t.name NOT IN ('caseGroups', 'schemaMigrations')"
        ).fetchall()
    )
    resultado = {}
    for t in tablas:
        cur = sa.execute(f"SELECT * FROM nexus.[{t}]")
        nombres = [d[0] for d in cur.description]
        filas = [tuple(v for n, v in zip(nombres, fila) if not (t == "cases" and n == "caseGroupId")) for fila in cur.fetchall()]
        resultado[t] = sorted(filas, key=repr)
    return resultado


def control() -> list[tuple]:
    return [tuple(f) for f in sa.execute("SELECT * FROM nexus.schemaMigrations ORDER BY version").fetchall()]


def hay(sql: str) -> bool:
    """¿La consulta (de una sola columna) devuelve algo que no sea NULL? Sirve igual para `OBJECT_ID(...)` y `COL_LENGTH(...)` (que devuelven NULL si no
    existe) que para un `SELECT … FROM sys.…` (que devuelve cero filas)."""
    fila = sa.execute(sql).fetchone()
    return fila is not None and fila[0] is not None


titulo("1 · Lo que dev ya tenía con la 0010: grupos, expedientes con grupo y sin él, documentos, un proceso y una ejecución")
con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
k = con.cursor()
ids_g = {}
for publico, tenant, nombre in ((G_VENTAS, ORG, "Ventas"), (G_SOPORTE, ORG, "Soporte"), (G_VACIO, ORG, "Sin nadie"), (G_VENTAS_OTRA, OTRA, "Ventas")):
    k.execute("INSERT INTO nexus.caseGroups (publicId, tenant, name, createdBy) VALUES (?, ?, ?, 'x')", publico, tenant, nombre)
    ids_g[publico] = k.execute("SELECT caseGroupId FROM nexus.caseGroups WHERE publicId = ?", publico).fetchone()[0]
ids_e = {}
for publico, tenant, nombre, grupo in EXPEDIENTES:
    k.execute("INSERT INTO nexus.cases (publicId, tenant, name, caseGroupId, createdBy) VALUES (?, ?, ?, ?, 'x')", publico, tenant, nombre, ids_g[grupo] if grupo else None)
    ids_e[publico] = k.execute("SELECT caseId FROM nexus.cases WHERE publicId = ?", publico).fetchone()[0]
ids_d = {}
for publico, tenant in ((D1, ORG), (D2, ORG), (D3, ORG), (D4, OTRA), (D5, ORG)):
    k.execute(
        "INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel) VALUES (?, ?, ?, 'org/x', 10, 'application/pdf', ?, 'API')",
        publico, tenant, "a" * 64, f"{publico[10:14]}.pdf",
    )
    ids_d[publico] = k.execute("SELECT documentId FROM nexus.documents WHERE publicId = ?", publico).fetchone()[0]
for expediente, documentos_ in DOCUMENTOS_DE.items():
    for d in documentos_:
        k.execute("INSERT INTO nexus.caseDocuments (caseId, documentId, addedBy) VALUES (?, ?, 'x')", ids_e[expediente], ids_d[d])
k.execute("INSERT INTO nexus.documentTypes (publicId, tenant, revision, dataJson, createdBy) VALUES (?, ?, 1, ?, 'x')", TD, ORG, '{"nombre":"INE","estado":"activo"}')
k.execute("INSERT INTO nexus.processes (publicId, tenant, revision, dataJson, createdBy) VALUES (?, ?, 3, ?, 'x')", PROCESO, ORG, DIBUJO)
k.execute(
    "INSERT INTO nexus.processRuns (publicId, tenant, caseId, caseName, processId, processName, processRevision, outcomeCase, outcomeText, status, detailJson, definitionJson, executedBy) "
    "VALUES (?, ?, ?, N'Juan Pérez', ?, N'Revisar solicitud', 3, 'si', N'Aprobado', 'completa', '{}', ?, 'x')",
    EJECUCION, ORG, E_VENTAS, PROCESO, DIBUJO,
)
id_run = k.execute("SELECT processRunId FROM nexus.processRuns WHERE publicId = ?", EJECUCION).fetchone()[0]
k.execute(
    "INSERT INTO nexus.processRunFinalValues (processRunId, position, nodeId, name, kind, family, status, valueText, valueJson, origin) VALUES (?, 1, 'valor-final-1', N'Hay INE', 'veredicto', NULL, 'listo', N'Pasa', NULL, N'Existe documento #1')",
    id_run,
)
con.close()

antes_catalogo, antes_foto, antes_control = catalogo(), foto(), control()
rev("la base dice versión 12 (se detuvo ahí a propósito)", sa.execute("SELECT MAX(version) FROM nexus.schemaMigrations").fetchone()[0] == 12 and len(antes_control) == 12)
rev("hay un mundo con grupos: cuatro (uno vacío) y tres expedientes que viven en uno, más uno sin grupo", sa.execute("SELECT count(*) FROM nexus.caseGroups").fetchone()[0] == 4
    and sa.execute("SELECT count(*) FROM nexus.cases WHERE caseGroupId IS NOT NULL").fetchone()[0] == 3 and sa.execute("SELECT count(*) FROM nexus.cases WHERE caseGroupId IS NULL").fetchone()[0] == 1)
rev("existían la tabla de grupos, la columna de los expedientes, su llave foránea compuesta y su índice",
    hay("SELECT OBJECT_ID('nexus.caseGroups', 'U')") and hay("SELECT COL_LENGTH('nexus.cases', 'caseGroupId')")
    and hay("SELECT object_id FROM sys.foreign_keys WHERE name = 'FK_cases_caseGroup'") and hay("SELECT index_id FROM sys.indexes WHERE name = 'IX_cases_caseGroup' AND object_id = OBJECT_ID('nexus.cases')"))
rev("hay datos de verdad en lo que NO se debe tocar: expedientes, documentos, pertenencias, tipo documental, proceso, ejecución y su valor final",
    all(len(antes_foto[t]) >= 1 for t in ("cases", "documents", "caseDocuments", "documentTypes", "processes", "processRuns", "processRunFinalValues"))
    and len(antes_foto["cases"]) == 4 and len(antes_foto["caseDocuments"]) == 5 and len(antes_foto["documents"]) == 5)
antes_sin_confiar = llaves_sin_confiar()
rev("(punto de partida) ninguna llave foránea está apagada; las que llegan sin confiar son las de organización de la 0006 y ninguna toca a los grupos",
    all(not f[3] for f in antes_catalogo["llaves"]) and "FK_cases_caseGroup" not in antes_sin_confiar and len(antes_sin_confiar) == 6, str(antes_sin_confiar))
viejos = intenta("el servicio de expedientes (el de ahora) lee la base de antes, con grupos", lambda: expedientes.listar(ORG))
rev("el código nuevo no depende de la migración: lista los expedientes de antes y NINGUNO trae `grupo`", viejos is not None and [e["id"] for e in viejos] == [E_VENTAS, E_SOPORTE, E_SIN]
    and all("grupo" not in e for e in viejos), str(viejos)[:300])

titulo("2 · La 0013, con el corredor real (solo ella)")
ok, motivo = aplicar(13)
rev("el corredor aplica la 0013", ok, motivo)
if not ok:
    terminar()
sa.execute("SELECT 1").fetchone()
despues_control = control()
rev("la base dice versión 13", sa.execute("SELECT MAX(version) FROM nexus.schemaMigrations").fetchone()[0] == 13 and len(despues_control) == 13)
fila13 = [f for f in despues_control if f[0] == 13]
esperada = [m for m in descubrir() if m.version == 13]
rev("la 0013 quedó anotada con su nombre, la suma del archivo y como ejecutada (no solo marcada)",
    len(fila13) == 1 and len(esperada) == 1 and fila13[0][1] == "borrar_grupos" and fila13[0][2].strip() == esperada[0].suma and fila13[0][3] == "ejecutada", str(fila13))
rev("las doce anotaciones de antes no cambiaron", despues_control[:12] == antes_control)

titulo("3 · Lo que se borró")
rev("la tabla `caseGroups` ya no existe", not hay("SELECT OBJECT_ID('nexus.caseGroups', 'U')"))
rev("`cases.caseGroupId` ya no existe (y `cases.caseId`, de control, sí se ve)", not hay("SELECT COL_LENGTH('nexus.cases', 'caseGroupId')") and hay("SELECT COL_LENGTH('nexus.cases', 'caseId')"))
rev("`FK_cases_caseGroup` ya no existe", not hay("SELECT object_id FROM sys.foreign_keys WHERE name = 'FK_cases_caseGroup'"))
rev("`IX_cases_caseGroup` ya no existe", not hay("SELECT index_id FROM sys.indexes WHERE name = 'IX_cases_caseGroup'"))
rev("no queda ninguna llave foránea, índice ni regla que hable de grupos", sa.execute(
    "SELECT (SELECT count(*) FROM sys.foreign_keys WHERE name LIKE '%caseGroup%' OR OBJECT_NAME(parent_object_id) LIKE 'caseGroup%' OR OBJECT_NAME(referenced_object_id) LIKE 'caseGroup%') "
    "+ (SELECT count(*) FROM sys.indexes WHERE name LIKE '%caseGroup%') + (SELECT count(*) FROM sys.check_constraints WHERE name LIKE '%caseGroup%')"
).fetchone()[0] == 0)

titulo("4 · Lo que NO se borró: el catálogo cambia solo en lo de los grupos")
despues_catalogo = catalogo()
esperado = {
    "tablas": [t for t in antes_catalogo["tablas"] if t != "nexus.caseGroups"],
    "columnas": {t: ([c_ for c_ in cols if c_[0] != "caseGroupId"] if t == "cases" else cols) for t, cols in antes_catalogo["columnas"].items() if t != "caseGroups"},
    "llaves": [f for f in antes_catalogo["llaves"] if f[0] != "FK_cases_caseGroup"],
    "indices": [f for f in antes_catalogo["indices"] if f[0] != "caseGroups" and f[1] != "IX_cases_caseGroup"],
    "reglas": [f for f in antes_catalogo["reglas"] if f[0] != "caseGroups"],
    "modulos": antes_catalogo["modulos"],
}
for clave, que in (
    ("tablas", "las tablas: las mismas, menos `caseGroups`"),
    ("columnas", "las columnas de todas las tablas, en el mismo orden y del mismo tipo: las mismas, y `cases` sin `caseGroupId`"),
    ("llaves", "las llaves foráneas: las mismas, con el mismo estado (apagada, de confianza) y las mismas acciones al borrar y actualizar, menos `FK_cases_caseGroup`"),
    ("indices", "los índices: los mismos, menos los de `caseGroups` e `IX_cases_caseGroup`"),
    ("reglas", "las reglas (CHECK): las mismas, menos las de `caseGroups`"),
    ("modulos", "los módulos (vistas, procedimientos, funciones, disparadores): los mismos"),
):
    dif = ""
    if despues_catalogo[clave] != esperado[clave]:
        a_, b_ = despues_catalogo[clave], esperado[clave]
        dif = str(sorted(set(map(repr, a_.items() if isinstance(a_, dict) else a_)) ^ set(map(repr, b_.items() if isinstance(b_, dict) else b_))))[:300]
    rev(que, despues_catalogo[clave] == esperado[clave], dif)
rev("ninguna otra llave foránea se rompió: no se apagó ni dejó de ser de confianza ninguna más (siguen sin confiar las mismas seis de antes)", llaves_sin_confiar() == antes_sin_confiar, str(llaves_sin_confiar()))
despues_foto = foto()
rev("ninguna otra tabla cambió: la foto de TODAS las de `nexus` (fila por fila, `cases` sin su columna de grupo) es idéntica a la de antes",
    despues_foto == antes_foto, str([t for t in antes_foto if despues_foto.get(t) != antes_foto[t]]))
rev("(la foto no es vacía: sigue habiendo expedientes, documentos, pertenencias, proceso, ejecución y valor final)", len(despues_foto["cases"]) == 4 and len(despues_foto["caseDocuments"]) == 5 and len(despues_foto["processRuns"]) == 1 and len(despues_foto["processRunFinalValues"]) == 1)

con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
k = con.cursor()


def documentos_por_expediente() -> dict[str, list[str]]:
    resultado: dict[str, list[str]] = {}
    for p, d in k.execute(
        "SELECT c.publicId, d.publicId FROM nexus.caseDocuments cd JOIN nexus.cases c ON c.caseId = cd.caseId JOIN nexus.documents d ON d.documentId = cd.documentId ORDER BY cd.caseDocumentId"
    ).fetchall():
        resultado.setdefault(p, []).append(d)
    return resultado


comprueba(
    "los cuatro expedientes siguen con su nombre y su organización, los que tenían grupo y el que no",
    lambda: [tuple(f) for f in k.execute("SELECT publicId, tenant, name FROM nexus.cases ORDER BY caseId").fetchall()] == [(p, t, n) for p, t, n, _ in EXPEDIENTES],
)
comprueba("…cada uno con sus documentos, en el mismo orden (D1 sigue en dos expedientes)", lambda: (documentos_por_expediente() == DOCUMENTOS_DE, str(documentos_por_expediente())))
comprueba(
    "el documento que no estaba en ningún expediente sigue en el void",
    lambda: k.execute("SELECT count(*) FROM nexus.caseDocuments cd JOIN nexus.documents d ON d.documentId = cd.documentId WHERE d.publicId = ?", D5).fetchone()[0] == 0
    and k.execute("SELECT count(*) FROM nexus.documents WHERE publicId = ?", D5).fetchone()[0] == 1,
)
comprueba(
    "el tipo documental, el proceso (con su revisión y su dibujo) y la ejecución con su valor final siguen ahí",
    lambda: k.execute("SELECT count(*) FROM nexus.documentTypes WHERE publicId = ?", TD).fetchone()[0] == 1
    and tuple(k.execute("SELECT revision, dataJson FROM nexus.processes WHERE publicId = ?", PROCESO).fetchone()) == (3, DIBUJO)
    and tuple(k.execute("SELECT caseId, outcomeText, status FROM nexus.processRuns WHERE publicId = ?", EJECUCION).fetchone()) == (E_VENTAS, "Aprobado", "completa")
    and k.execute("SELECT count(*) FROM nexus.processRunFinalValues WHERE processRunId = ?", id_run).fetchone()[0] == 1,
)

titulo("5 · Lo que sigue funcionando con el usuario de la API (usrNexus)")
error = rechaza(k, "INSERT INTO nexus.cases (publicId, tenant, name, caseGroupId, createdBy) VALUES (?, ?, 'x', NULL, 'x')", "expediente-" + "f" * 32, ORG)
rev("la base ya no acepta un expediente con grupo (ni siquiera nulo): la columna no existe", error is not None and "caseGroupId" in error, str(error)[:200])
error = rechaza(k, "INSERT INTO nexus.caseGroups (publicId, tenant, name, createdBy) VALUES (?, ?, 'x', 'x')", "grupo-" + "f" * 32, ORG)
rev("ni un grupo nuevo: la tabla no existe", error is not None and "caseGroups" in error, str(error)[:200])
error = rechaza(k, "INSERT INTO nexus.cases (publicId, tenant, name, createdBy) VALUES (?, ?, N'Directo', 'x')", "expediente-" + "e" * 32, ORG)
rev("un expediente sin grupo, por SQL directo, entra", error is None, str(error)[:200])
rechaza(k, "DELETE FROM nexus.cases WHERE publicId = ?", "expediente-" + "e" * 32)
con.close()

nuevo = intenta("el servicio crea un expediente después de la 0013", lambda: expedientes.crear(ORG, "  Expediente   nuevo ", "persona-de-prueba"))
rev("`expedientes.crear` lo crea, con el nombre limpio y SIN `grupo`", nuevo is not None and nuevo["nombre"] == "Expediente nuevo" and nuevo["id"].startswith("expediente-")
    and sorted(nuevo) == ["creadoEn", "creadoPor", "id", "nombre", "tamanoBytes", "totalDocumentos"], str(nuevo))
lista = intenta("el servicio lista los expedientes después de la 0013", lambda: expedientes.listar(ORG))
rev("`expedientes.listar` trae los tres de antes y el nuevo, en el orden de su creación y ninguno con `grupo`",
    lista is not None and nuevo is not None and [e["id"] for e in lista] == [E_VENTAS, E_SOPORTE, E_SIN, nuevo["id"]] and all("grupo" not in e for e in lista), str(lista)[:300])
rev("el expediente de antes que SÍ tenía grupo se lee igual que el que no: nombre, cuántos documentos y cuánto pesan",
    lista is not None and {e["id"]: (e["nombre"], e["totalDocumentos"], e["tamanoBytes"]) for e in lista if e["id"] in (E_VENTAS, E_SIN)} == {E_VENTAS: ("Juan Pérez", 2, 20), E_SIN: ("Expediente de antes", 1, 10)}, str(lista)[:300])
detalle = intenta("el servicio abre un expediente con grupo de antes", lambda: expedientes.obtener(ORG, E_VENTAS))
rev("`expedientes.obtener` abre el que tenía grupo, con sus dos documentos en orden y sin `grupo`", detalle is not None and "grupo" not in detalle and [d["id"] for d in detalle["documentos"]] == [D1, D2], str(detalle)[:300])
rev("y uno de otra organización no se ve desde esta (404 en el servicio: `None`)", intenta("obtener uno ajeno", lambda: expedientes.obtener(ORG, E_OTRA)) is None)
if nuevo is not None:
    rev("meter un documento al expediente nuevo (el del void): lo agrega", intenta("agregar un documento", lambda: expedientes.agregar_documento(ORG, nuevo["id"], D5, "persona-de-prueba")) is True)
    abierto = intenta("abrir el nuevo", lambda: expedientes.obtener(ORG, nuevo["id"])) or {}
    rev("…y el expediente lo cuenta, con su peso (10 B)", abierto.get("totalDocumentos") == 1 and abierto.get("tamanoBytes") == 10, str(abierto)[:200])
    comprueba("sacarlo lo devuelve al void sin borrarlo", lambda: expedientes.quitar_documento(ORG, nuevo["id"], D5, "persona-de-prueba") is None
              and D5 in [d["id"] for d in expedientes.listar_documentos(ORG, True)])

titulo("6 · Volver a aplicar no hace nada")
antes_de_reaplicar = control()
# Hasta la 13: las que vinieron después (la 0014, usuarios en SQL) no son de esta prueba.
ok, motivo = aplicar(13)
rev("el corredor, otra vez sobre la misma base: termina bien (no hay nada pendiente)", ok, motivo)
rev("la tabla de control queda IDÉNTICA (mismas trece filas, mismas fechas)", control() == antes_de_reaplicar and len(control()) == 13, str(len(control())))
rev("y el catálogo no se movió", catalogo() == despues_catalogo)

terminar()
