"""¿Ya sacó Charlie el prefijo del código del SP y lo lee de la tabla?

Corre en el SERVER, que es donde el `.env` tiene la base:

    cd /home/mbriseno/code/nexus_back && venv/bin/python verificar_secuencia_tenant.py

QUÉ SE LE PIDIÓ. Que `[security].[uspCreateTenant]` obtenga `prefix` y
`lastSequence` de `[security].[tenantSequence]`, tomando la fila con
`isActive = 1`, en vez de traer el prefijo escrito también en el cuerpo del SP.

POR QUÉ SE PUEDE VERIFICAR SIN PREGUNTARLE. Desde el 2026-08-31 existe
`GRANT VIEW DEFINITION ON SCHEMA::security TO usrNexus`, así que
`OBJECT_DEFINITION` devuelve el cuerpo real de los SPs en vez de NULL (ver
docs/solicitudes-dba.md). Ese cuerpo es la única prueba que no depende de que
alguien diga "ya quedó" — que es justo la convención de ese documento: ✅ se
marca cuando la aplicación lo pudo comprobar, no cuando se acordó.

TODO ES DE SOLO LECTURA. Se leen catálogos del sistema (`sys.sql_modules`,
`sys.columns`) y la tabla de configuración. **No crea tenants**: la prueba de
comportamiento —crear uno y ver qué código sale— es de `verificar_tenants.py`,
y esa sí escribe.

LOS INDICIOS NO SON LA PRUEBA. El veredicto sale de buscar palabras en el
cuerpo del SP, y eso puede engañar: Charlie puede escribirlo de una forma que
estos patrones no contemplen. Por eso **siempre se imprimen las líneas
relevantes del SP**, para que la decisión la tome quien lee. Si el veredicto y
las líneas se contradicen, gana lo que se lee.

    venv/bin/python verificar_secuencia_tenant.py --autoprueba

corre la detección contra cuerpos de SP inventados, SIN tocar la base. Vale la
pena correrla primero: este script se usa en el server, donde nadie va a estar
depurando expresiones regulares. Ya cazó un falso positivo real — el `'00000'`
del relleno se leía como si fuera el prefijo.
"""

import re
import sys

try:
    from db.sqlserver import obtener_conexion
except Exception as exc:  # pragma: no cover - solo pasa sin pyodbc instalado
    print(f"No se pudo importar el acceso a SQL Server: {exc}")
    print("¿Estás corriendo esto en el server, con el venv del proyecto?")
    raise SystemExit(2)

from errores import ConfiguracionIncompleta

TABLA = "tenantSequence"
ESQUEMA = "security"


def titulo(texto: str) -> None:
    print()
    print("=" * 72)
    print(texto)
    print("=" * 72)


def _sin_comentarios(cuerpo: str) -> str:
    """Quita comentarios de línea y de bloque antes de buscar palabras.

    Sin esto, un SP que dejara el prefijo viejo comentado (`-- SET @prefix =
    'NEX'`) se reportaría como si todavía lo tuviera escrito en el código, que
    es exactamente el falso positivo que haría desconfiar del script."""
    sin_bloque = re.sub(r"/\*.*?\*/", " ", cuerpo, flags=re.S)
    return re.sub(r"--[^\n]*", " ", sin_bloque)


def _lineas_con(cuerpo: str, patron: str) -> list[tuple[int, str]]:
    rx = re.compile(patron, re.I)
    return [
        (n, linea.rstrip())
        for n, linea in enumerate(cuerpo.splitlines(), 1)
        if rx.search(linea)
    ]


def revisar_sp(cur, nombre: str) -> str | None:
    """Devuelve el cuerpo del SP, o None si no se puede leer."""
    cur.execute("SELECT OBJECT_DEFINITION(OBJECT_ID(?))", f"[{ESQUEMA}].[{nombre}]")
    fila = cur.fetchone()
    return fila[0] if fila else None


def evaluar_cuerpo(cuerpo: str) -> dict:
    """Las señales que se buscan en el cuerpo del SP, en un solo lugar.

    Está aparte de `main` para que se pueda probar SIN base: ver
    `--autoprueba`. Este script se corre en el server, donde nadie va a estar
    depurando regex, así que conviene que llegue ya probado."""
    limpio = _sin_comentarios(cuerpo)

    # La señal de que el prefijo sigue en el código es un literal ASIGNADO o
    # COMPARADO contra `prefix`, o concatenado directo para armar el código.
    # NO basta con que un literal ande cerca de la palabra "prefix": la
    # primera versión de esto marcaba como prefijo el `'00000'` del relleno en
    #     @code = @prefix + RIGHT('00000' + CAST(@next AS varchar(5)), 5)
    # o sea daba "todavía no" sobre un SP que ya estaba bien. Lo cazó la
    # autoprueba antes de que llegara al server.
    literales = re.findall(r"@?prefix\b[^=\n]{0,30}=\s*'([^']{1,10})'", limpio, re.I)
    # Y el prefijo metido directo en la concatenación: 'NEX' + RIGHT(...).
    # Solo letras, para no volver a confundirlo con un relleno de ceros.
    literales += re.findall(r"'([A-Za-z]{2,5})'\s*\+", limpio)

    lee_tabla = re.search(TABLA, limpio, re.I) is not None
    filtra_activa = re.search(r"isActive\s*=\s*1", limpio, re.I) is not None
    lee_secuencia = re.search(r"lastSequence", limpio, re.I) is not None
    return {
        "lee_tabla": lee_tabla,
        "filtra_activa": filtra_activa,
        "lee_secuencia": lee_secuencia,
        "literales": sorted(set(literales)),
        "lee_configuracion": lee_tabla and filtra_activa and lee_secuencia,
    }


# Cuerpos sintéticos para la autoprueba. El del relleno `'00000'` está en TODOS
# a propósito: es el falso positivo que ya se coló una vez.
_CONCAT = "    SET @code = @prefix + RIGHT('00000' + CAST(@next AS varchar(5)), 5);"
_NUEVO = f"""CREATE PROCEDURE [security].[uspCreateTenant] AS
BEGIN
    DECLARE @prefix varchar(5), @next int, @seqId int;
    SELECT TOP 1 @seqId = tenantSequenceId, @prefix = prefix, @next = lastSequence + 1
      FROM [security].[tenantSequence] WITH (UPDLOCK) WHERE isActive = 1;
    IF @prefix IS NULL RAISERROR('The tenant sequence is not configured', 16, 1);
    UPDATE [security].[tenantSequence] SET lastSequence = @next WHERE tenantSequenceId = @seqId;
{_CONCAT}
END"""
_CASOS = [
    ("viejo: DECLARE @prefix ... = 'NEX'",
     f"CREATE PROCEDURE x AS BEGIN\n    DECLARE @prefix varchar(5) = 'NEX';\n{_CONCAT}\nEND", False),
    ("viejo: SET @prefix = 'NEX'",
     f"CREATE PROCEDURE x AS BEGIN\n    SET @prefix = 'NEX';\n{_CONCAT}\nEND", False),
    ("viejo: 'NEX' + RIGHT(...) inline",
     "CREATE PROCEDURE x AS BEGIN\n    SET @code = 'NEX' + RIGHT('00000' + CAST(@n AS varchar(5)), 5);\nEND", False),
    ("viejo: lee la tabla pero SIN isActive",
     _NUEVO.replace("WHERE isActive = 1", "ORDER BY tenantSequenceId"), False),
    ("nuevo: lee la tabla con isActive = 1", _NUEVO, True),
    ("nuevo + el viejo comentado con --",
     _NUEVO.replace("BEGIN", "BEGIN\n    -- antes: SET @prefix = 'NEX';"), True),
    ("nuevo + el viejo en /* bloque */",
     _NUEVO.replace("BEGIN", "BEGIN\n    /* viejo:\n       SET @prefix = 'NEX';\n    */"), True),
]


def autoprueba() -> int:
    """Comprueba la detección contra cuerpos de SP inventados. Sin base."""
    print("Autoprueba de la detección (no toca la base)\n")
    fallos = 0
    for nombre, cuerpo, esperado in _CASOS:
        s = evaluar_cuerpo(cuerpo)
        obtenido = s["lee_configuracion"] and not s["literales"]
        marca = "OK   " if obtenido == esperado else "FALLA"
        if obtenido != esperado:
            fallos += 1
        print(
            f"  {marca} {nombre:38} -> "
            f"{'YA ESTÁ' if obtenido else 'TODAVÍA NO':10} literales={s['literales']}"
        )
    print()
    print(f"{fallos} FALLARON" if fallos else f"los {len(_CASOS)} casos pasaron")
    return 1 if fallos else 0


def main() -> int:
    try:
        conexion = obtener_conexion()
    except ConfiguracionIncompleta as exc:
        print(f"La base no está configurada en este .env: {exc}")
        print("Este script corre en el SERVER; el .env local no trae SQLSERVER_HOST.")
        return 2

    cur = conexion.cursor()
    veredicto_lee_tabla = False
    veredicto_sin_literal = False
    cuerpo_leido = False

    # ── 1. La tabla de configuración ─────────────────────────────────────────
    titulo(f"1 · [{ESQUEMA}].[{TABLA}] — la fila que el SP tiene que leer")
    try:
        cur.execute(f"SELECT * FROM [{ESQUEMA}].[{TABLA}]")
        columnas = [c[0] for c in cur.description]
        filas = cur.fetchall()
        print(f"  columnas: {', '.join(columnas)}")
        print(f"  filas: {len(filas)}")
        for f in filas:
            print("   ", dict(zip(columnas, [str(v) for v in f])))
        activas = [
            f for f in filas if "isActive" in columnas and f[columnas.index("isActive")]
        ]
        if len(activas) == 1:
            print("  OK    hay EXACTAMENTE una fila con isActive = 1")
        elif not filas:
            print("  OJO   la tabla está VACÍA: el SP no tiene de dónde leer nada")
        elif not activas:
            print("  OJO   hay filas pero NINGUNA activa: el SP no va a encontrar cuál usar")
        else:
            print(f"  OJO   hay {len(activas)} filas activas: el SP tiene que desempatar")
    except Exception as exc:
        print(f"  no se pudo leer la tabla: {exc}")

    # ── 2. El cuerpo del SP ──────────────────────────────────────────────────
    titulo("2 · [security].[uspCreateTenant] — ¿de dónde saca el prefijo?")
    cuerpo = revisar_sp(cur, "uspCreateTenant")
    if not cuerpo:
        print("  OBJECT_DEFINITION devolvió NULL.")
        print("  O se revocó VIEW DEFINITION, o el SP cambió de nombre/esquema.")
        print("  Sin el cuerpo no se puede contestar la pregunta. Ver")
        print("  docs/solicitudes-dba.md, la sección del GRANT.")
    else:
        cuerpo_leido = True
        limpio = _sin_comentarios(cuerpo)

        senales = evaluar_cuerpo(cuerpo)
        lee_tabla = senales["lee_tabla"]
        filtra_activa = senales["filtra_activa"]
        lee_secuencia = senales["lee_secuencia"]
        literales = senales["literales"]
        veredicto_lee_tabla = senales["lee_configuracion"]
        veredicto_sin_literal = not literales

        print(f"  ¿menciona {TABLA}?            {'sí' if lee_tabla else 'NO'}")
        print(f"  ¿filtra por isActive = 1?      {'sí' if filtra_activa else 'NO'}")
        print(f"  ¿usa lastSequence?             {'sí' if lee_secuencia else 'NO'}")
        print(
            "  ¿quedan literales de prefijo?  "
            + (f"SÍ -> {sorted(set(literales))}" if literales else "no se encontró ninguno")
        )

        print("\n  --- líneas del SP que hablan de esto (léelas, no confíes en lo de arriba) ---")
        interesantes = _lineas_con(cuerpo, rf"{TABLA}|isActive|lastSequence|prefix")
        if interesantes:
            for n, linea in interesantes:
                print(f"  {n:>4} | {linea}")
        else:
            print("  (ninguna: el SP no menciona nada de esto)")

    # ── 3. Cualquier otro SP que toque la tabla ──────────────────────────────
    titulo(f"3 · ¿Qué otros módulos tocan {TABLA}?")
    print("  (si el cambio se hizo en otro SP, aparece aquí)")
    try:
        cur.execute(
            """
            SELECT SCHEMA_NAME(o.schema_id) AS esquema, o.name, o.type_desc
            FROM sys.sql_modules m
            JOIN sys.objects o ON o.object_id = m.object_id
            WHERE m.definition LIKE ?
            ORDER BY 1, 2
            """,
            f"%{TABLA}%",
        )
        encontrados = cur.fetchall()
        if encontrados:
            for e in encontrados:
                print(f"    [{e[0]}].[{e[1]}]  ({e[2]})")
        else:
            print("    ninguno visible")
        print(
            "\n  OJO: `sys.sql_modules` solo muestra los objetos sobre los que hay\n"
            "  VIEW DEFINITION. El GRANT es sobre el esquema `security`, así que un\n"
            "  SP en otro esquema sería invisible aquí — ausencia no es prueba."
        )
    except Exception as exc:
        print(f"    no se pudo consultar: {exc}")

    conexion.close()

    # ── Veredicto ────────────────────────────────────────────────────────────
    titulo("VEREDICTO")
    if not cuerpo_leido:
        print("  INDETERMINADO — no se pudo leer el cuerpo del SP.")
        return 2
    if veredicto_lee_tabla and veredicto_sin_literal:
        print("  PARECE QUE SÍ: el SP lee la tabla, filtra por la fila activa y no")
        print("  se le encontró ningún prefijo escrito en el código.")
        print("  Confírmalo leyendo las líneas de arriba antes de marcarlo ✅.")
        return 0
    print("  PARECE QUE TODAVÍA NO. Lo que falta, según lo de arriba:")
    if not veredicto_lee_tabla:
        print("   - no lee la configuración de la tabla como se pidió")
    if not veredicto_sin_literal:
        print("   - todavía hay un prefijo escrito en el cuerpo del SP")
    return 1


if __name__ == "__main__":
    if "--autoprueba" in sys.argv:
        sys.exit(autoprueba())
    sys.exit(main())
