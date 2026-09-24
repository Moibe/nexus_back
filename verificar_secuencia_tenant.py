"""¿Ya quedó lo que se le pidió a Charlie sobre el código de tenant?

Corre en el SERVER, que es donde el `.env` tiene la base:

    cd /home/mbriseno/code/nexus_back && venv/bin/python verificar_secuencia_tenant.py

CONTESTA DOS PREGUNTAS, por separado — una puede estar resuelta y la otra no:

  A. ¿El SP saca `prefix` y `lastSequence` de la tabla de configuración?
  B. ¿El código completo CABE en `tenantCode`, o se trunca?

QUÉ SE LE PIDIÓ (A). Que `[security].[uspCreateTenant]` obtenga `prefix` y
`lastSequence` de `[security].[tenantSequence]`, tomando la fila con
`isActive = 1`, en vez de traer el prefijo escrito también en el cuerpo del SP.

QUÉ SE LE PIDIÓ (B). Que `tenantCode` quepa. `docs/solicitudes-dba.md` razonó
el prefijo dando por hecho que el código era `NEX00001` —3 letras + 5 dígitos,
8 justos en un `varchar(8)`—, pero si el SP mete un separador son 9 y **SQL
Server trunca en silencio**: no hay error al asignar a una variable corta, solo
códigos recortados. Con `NEX-00001` cortado a `NEX-0000`, cada bloque de diez
tenants comparte código. Esta parte NO se deduce: se mide el ancho declarado en
`sys.columns` y se lee del SP cómo arma el código de verdad.

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

# El acceso a SQL Server se importa DENTRO de `main()`, no aquí: al nivel del
# módulo, un `pyodbc` ausente mataba el script antes de poder llegar a
# `--autoprueba`, que justamente no toca la base. O sea que la autoprueba solo
# corría donde menos falta hacía (el server) y no en la máquina donde se
# escriben las expresiones regulares.

TABLA = "tenantSequence"
ESQUEMA = "security"
TABLA_TENANTS = "tenants"
COLUMNA_CODIGO = "tenantCode"


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
    # Los corchetes son OPCIONALES y no decorativos: Charlie escribe los
    # identificadores delimitados (`[isActive] = 1`), y la primera versión de
    # esto buscaba `isActive\s*=\s*1`, que el `]` de en medio rompe. Resultado:
    # dio "TODAVÍA NO" sobre un SP que ya estaba correcto, el 2026-09-24. Es
    # exactamente el falso negativo contra el que advierte el docstring —
    # las líneas del SP decían lo contrario que el veredicto.
    filtra_activa = re.search(r"\[?\s*isActive\s*\]?\s*=\s*1", limpio, re.I) is not None
    lee_secuencia = re.search(r"\[?\s*lastSequence\s*\]?", limpio, re.I) is not None
    return {
        "lee_tabla": lee_tabla,
        "filtra_activa": filtra_activa,
        "lee_secuencia": lee_secuencia,
        "literales": sorted(set(literales)),
        "lee_configuracion": lee_tabla and filtra_activa and lee_secuencia,
    }


def evaluar_ancho(cuerpo: str) -> dict:
    """Cómo arma el SP el código, en caracteres: prefijo + separador + dígitos.

    Se saca del cuerpo real y no se asume, porque la suposición ya falló una
    vez: `docs/solicitudes-dba.md` razonó el prefijo dando por hecho que el
    código era `NEX00001` (8 justos), pero si el SP mete un guion son 9 y
    `tenantCode varchar(8)` los trunca EN SILENCIO — SQL Server no avisa al
    truncar en una asignación a variable. Con todos los códigos recortados al
    mismo largo, dos tenants distintos pueden terminar con el mismo código.

    Devuelve `digitos=None` si no se reconoció el armado; entonces no se
    inventa un veredicto, se dice que no se pudo leer.
    """
    limpio = _sin_comentarios(cuerpo)

    # El separador: lo que va entre el prefijo y el resto de la concatenación.
    # Se acepta vacío (pegados) y de hasta 3 caracteres.
    sep = re.search(r"@prefix\s*\+\s*'([^']{0,3})'\s*\+", limpio, re.I)
    if sep:
        separador = sep.group(1)
    elif re.search(r"@prefix\s*\+\s*(RIGHT|CAST|CONVERT|FORMAT)", limpio, re.I):
        separador = ""  # van pegados, sin nada en medio
    else:
        separador = None

    # Los dígitos del consecutivo: el segundo argumento de RIGHT(...), que es
    # el ancho al que se rellena. Si no hay RIGHT, se intenta con el relleno.
    digitos = None
    m = re.search(r"RIGHT\s*\([^,]+,\s*(\d+)\s*\)", limpio, re.I)
    if m:
        digitos = int(m.group(1))
    else:
        relleno = re.search(r"'(0{2,10})'\s*\+", limpio)
        if relleno:
            digitos = len(relleno.group(1))

    return {"separador": separador, "digitos": digitos}


def largo_necesario(prefijo: str, separador: str, digitos: int) -> int:
    return len(prefijo) + len(separador) + digitos


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
    # El caso REAL de Charlie, que la primera versión no reconocía: todos los
    # identificadores entre corchetes. Es el que produjo el falso negativo del
    # 2026-09-24, así que se queda como caso fijo para que no vuelva.
    ("nuevo, con identificadores entre corchetes",
     """CREATE PROCEDURE [security].[uspCreateTenant] AS
BEGIN
    DECLARE @prefix VARCHAR(5), @sequenceNumber INT;
    UPDATE TOP (1) [security].[tenantSequence] WITH (UPDLOCK, HOLDLOCK)
       SET @prefix = [prefix],
           @sequenceNumber = [lastSequence] + 1,
           [lastSequence] = [lastSequence] + 1
     WHERE [isActive] = 1;
    IF @@ROWCOUNT = 0 THROW 50006, 'There is no active row in tenantSequence.', 1;
    SET @tenantCode = @prefix + '-' + RIGHT('00000' + CAST(@sequenceNumber AS VARCHAR(5)), 5);
END""", True),
]


# Casos para `evaluar_ancho`. El que importa es el del guion: es el que
# convierte un `varchar(8)` correcto para `NEX00001` en uno que trunca.
_CASOS_ANCHO = [
    ("pegado, 5 digitos", _CONCAT, "", 5),
    (
        "con guion, 5 digitos",
        "    SET @code = @prefix + '-' + RIGHT('00000' + CAST(@next AS varchar(5)), 5);",
        "-",
        5,
    ),
    (
        "con guion bajo, 4 digitos",
        "    SET @code = @prefix + '_' + RIGHT('0000' + CAST(@next AS varchar(4)), 4);",
        "_",
        4,
    ),
    (
        "sin RIGHT: se deduce del relleno",
        "    SET @code = @prefix + '-' + '000' + CAST(@next AS varchar(3));",
        "-",
        3,
    ),
    (
        "el guion comentado no cuenta",
        "    -- antes: SET @code = @prefix + '-' + RIGHT(...)\n"
        "    SET @code = @prefix + RIGHT('00000' + CAST(@next AS varchar(5)), 5);",
        "",
        5,
    ),
]


def autoprueba() -> int:
    """Comprueba la detección contra cuerpos de SP inventados. Sin base."""
    print("Autoprueba de la detección (no toca la base)\n")
    fallos = 0

    print("  -- ancho del código (separador y dígitos) --")
    for nombre, cuerpo, sep_esperado, dig_esperados in _CASOS_ANCHO:
        r = evaluar_ancho(cuerpo)
        ok = r["separador"] == sep_esperado and r["digitos"] == dig_esperados
        if not ok:
            fallos += 1
        print(
            f"  {'OK   ' if ok else 'FALLA'} {nombre:38} -> "
            f"separador={r['separador']!r} digitos={r['digitos']} "
            f"(esperado {sep_esperado!r}/{dig_esperados})"
        )
    print()

    print("  -- de dónde saca el prefijo --")
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
    total = len(_CASOS) + len(_CASOS_ANCHO)
    print(f"{fallos} FALLARON" if fallos else f"los {total} casos pasaron")
    return 1 if fallos else 0


def main() -> int:
    try:
        from db.sqlserver import obtener_conexion
        from errores import ConfiguracionIncompleta
    except Exception as exc:  # pragma: no cover - solo pasa sin pyodbc instalado
        print(f"No se pudo importar el acceso a SQL Server: {exc}")
        print("¿Estás corriendo esto en el server, con el venv del proyecto?")
        return 2

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
    # El prefijo REAL que se va a usar, leído de la fila activa. Si no hay
    # fila, más abajo se razona con el propuesto ('NEX') y se dice que es una
    # suposición — no es lo mismo medir lo que hay que lo que se planea.
    prefijo_activo: str | None = None
    veredicto_cabe: bool | None = None

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
        if activas and "prefix" in columnas:
            valor = activas[0][columnas.index("prefix")]
            if valor is not None:
                prefijo_activo = str(valor).strip()
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

    # ── 4. ¿Cabe el código en tenantCode? ────────────────────────────────────
    titulo(f"4 · ¿cabe el código completo en [{ESQUEMA}].[{TABLA_TENANTS}].[{COLUMNA_CODIGO}]?")
    print("  SQL Server TRUNCA EN SILENCIO al asignar a una variable corta: si no")
    print("  cabe, no hay error, hay códigos recortados — y dos tenants distintos")
    print("  pueden acabar con el mismo.\n")
    declarado = None
    try:
        cur.execute(
            """
            SELECT TYPE_NAME(c.user_type_id) AS tipo, c.max_length
            FROM sys.columns c
            JOIN sys.tables t ON t.object_id = c.object_id
            WHERE SCHEMA_NAME(t.schema_id) = ? AND t.name = ? AND c.name = ?
            """,
            ESQUEMA,
            TABLA_TENANTS,
            COLUMNA_CODIGO,
        )
        fila = cur.fetchone()
        if not fila:
            print(f"  no se encontró la columna (¿otro nombre, u otro esquema?)")
        else:
            tipo, max_length = str(fila[0]), int(fila[1])
            # `max_length` viene en BYTES. En nvarchar/nchar cada carácter son
            # dos, así que el ancho útil es la mitad; -1 es MAX (sin tope real).
            if max_length == -1:
                declarado = None
                print(f"  {COLUMNA_CODIGO} es {tipo}(MAX): no hay tope, no puede truncar")
                veredicto_cabe = True
            else:
                declarado = max_length // 2 if tipo.lower().startswith("n") else max_length
                print(f"  declarada: {tipo}({declarado}) caracteres")
    except Exception as exc:
        print(f"  no se pudo leer la columna: {exc}")

    if cuerpo_leido and declarado is not None:
        forma = evaluar_ancho(cuerpo)
        sep, dig = forma["separador"], forma["digitos"]
        if sep is None or dig is None:
            print("\n  No se reconoció cómo arma el SP el código; no se opina.")
            print("  Léelo a mano en las líneas de la sección 2.")
        else:
            usado = prefijo_activo if prefijo_activo else "NEX"
            de_donde = "la fila activa" if prefijo_activo else "la propuesta (no hay fila activa)"
            necesario = largo_necesario(usado, sep, dig)
            ejemplo = f"{usado}{sep}{'0' * (dig - 1)}1"
            print(f"\n  separador entre prefijo y consecutivo: {sep!r}")
            print(f"  dígitos del consecutivo:                {dig}")
            print(f"  prefijo:                                {usado!r}  (de {de_donde})")
            print(f"  código que saldría:                     {ejemplo}  -> {necesario} caracteres")
            veredicto_cabe = necesario <= declarado
            if veredicto_cabe:
                print(f"\n  OK    {necesario} <= {declarado}: cabe completo.")
            else:
                print(f"\n  OJO   {necesario} > {declarado}: SE TRUNCA a {ejemplo[:declarado]!r}.")
                print(f"        Haría falta {COLUMNA_CODIGO} de al menos {necesario}")
                print(f"        (o un prefijo de {declarado - len(sep) - dig} letras, o un dígito menos).")

        # El síntoma del truncamiento depende de esto: sin índice único, dos
        # tenants distintos pueden quedar con el MISMO código y nadie se
        # entera; con índice único, el insert revienta. Malo en los dos casos,
        # pero se diagnostican distinto, así que conviene saber cuál toca.
        if veredicto_cabe is False:
            try:
                cur.execute(
                    """
                    SELECT i.name, i.is_unique
                    FROM sys.indexes i
                    JOIN sys.index_columns ic ON ic.object_id = i.object_id
                                             AND ic.index_id = i.index_id
                    JOIN sys.columns c ON c.object_id = ic.object_id
                                      AND c.column_id = ic.column_id
                    JOIN sys.tables t ON t.object_id = i.object_id
                    WHERE SCHEMA_NAME(t.schema_id) = ? AND t.name = ? AND c.name = ?
                    """,
                    ESQUEMA,
                    TABLA_TENANTS,
                    COLUMNA_CODIGO,
                )
                indices = cur.fetchall()
                unicos = [i for i in indices if i[1]]
                if unicos:
                    print(f"\n        Hay índice ÚNICO ({unicos[0][0]}): al truncarse, el segundo")
                    print("        tenant del mismo bloque va a FALLAR al insertarse.")
                else:
                    print("\n        NO hay índice único en esa columna: los códigos truncados")
                    print("        se van a repetir en silencio, sin error.")
            except Exception as exc:
                print(f"\n        (no se pudo revisar si hay índice único: {exc})")

        print("\n  --- líneas del SP donde se arma el código ---")
        for n, linea in _lineas_con(cuerpo, r"@code|tenantCode|RIGHT\s*\("):
            print(f"  {n:>4} | {linea}")

    conexion.close()

    # ── Veredicto ────────────────────────────────────────────────────────────
    titulo("VEREDICTO")
    if not cuerpo_leido:
        print("  INDETERMINADO — no se pudo leer el cuerpo del SP.")
        return 2

    # Son DOS preguntas distintas y se contestan por separado: una puede estar
    # resuelta y la otra no, y juntarlas en un solo sí/no esconde cuál falta.
    prefijo_ok = veredicto_lee_tabla and veredicto_sin_literal
    print("  A · ¿el SP saca prefix y lastSequence de la tabla activa?")
    if prefijo_ok:
        print("      PARECE QUE SÍ: lee la tabla, filtra por isActive y no le quedó")
        print("      ningún prefijo escrito en el código.")
    else:
        print("      PARECE QUE TODAVÍA NO:")
        if not veredicto_lee_tabla:
            print("       - no lee la configuración de la tabla como se pidió")
        if not veredicto_sin_literal:
            print("       - todavía hay un prefijo escrito en el cuerpo del SP")

    print()
    print(f"  B · ¿el código completo cabe en {COLUMNA_CODIGO} sin truncarse?")
    if veredicto_cabe is None:
        print("      INDETERMINADO — no se pudo medir (ver la sección 4).")
    elif veredicto_cabe:
        print("      SÍ: el código que arma el SP cabe completo con el prefijo vigente.")
    else:
        print("      NO: SE TRUNCA. Es el más grave de los dos — no da error, y deja")
        print("      códigos recortados que pueden repetirse entre tenants distintos.")

    print("\n  Confírmalo leyendo las líneas del SP de arriba antes de marcar nada ✅.")
    if prefijo_ok and veredicto_cabe:
        return 0
    if veredicto_cabe is None:
        return 2
    return 1


if __name__ == "__main__":
    if "--autoprueba" in sys.argv:
        sys.exit(autoprueba())
    sys.exit(main())
