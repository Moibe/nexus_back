"""¿Usuarios, contraseñas, intentos de inicio de sesión, sesiones y membresías se comportan IGUAL en SQL (migración 0014) que en el almacén, y la
importación única desde `usuarios.jsonl` funciona?

    .venv/Scripts/python verificar_usuarios_sql.py [--dejar]

Levanta el SQL Server desechable de `pruebas_sql.py` hasta la 0013, siembra `tenants.jsonl`, `usuarios.jsonl` y `tokens.jsonl` como los tiene un
ambiente real (con lo de antes del 2026-10-14 y con lo que NO se puede importar: un guid que no sirve, dos cuentas vigentes con el mismo correo,
una membresía de una organización que no existe, una sesión sin refresh legible, un enlace de una cuenta que no existe, líneas rotas), aplica la
0014 con el corredor real, y luego:

1. la 0014: versión, tablas y la llave foránea nueva de los enlaces (sin revisar lo de antes);
2. importa una sola vez y revisa qué entró, qué se saltó y que importar otra vez no duplica;
3. compara cada lectura con lo CONGELADO (`verificar_usuarios_sql.congelado.json`): hasta el 2026-11-11 se comparaba contra el mismo módulo
   leyendo el JSONL, y en el commit 61aea2b las dos salidas eran idénticas; al quitar ese camino se congelaron (ver `pruebas_sql.congelado`);
4. hace las escrituras (altas, membresías, intentos, sesiones, contraseñas, administración, retiro) y compara lo que devuelven con lo congelado;
5. lo que solo la base cuida: correo único, llaves foráneas, restricciones, y la bitácora de lo que un administrador hizo;
6. hilos a la vez: un refresh se canjea UNA vez, un correo se da de alta UNA vez, los intentos se cuentan bien, un restablecimiento o un cierre
   forzado no dejan viva una sesión que nació a la mitad, dos retiros no dejan a la plataforma sin administradores;
7. la API de punta a punta contra la base: primer acceso, alta de organización y de usuario, desactivar, cierre forzado, renovar, cerrar sesión,
   recuperar la contraseña (el enlace con su llave foránea), bloqueo por intentos y Nexus Monitor.

Sin Docker, se salta y lo dice.
"""

import json
import os
import re
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pruebas_sql  # noqa: E402

BASE = pruebas_sql.preparar("nexus-usuarios-prueba", 14345, dejar="--dejar" in sys.argv, hasta=13)
RAIZ = Path(tempfile.mkdtemp(prefix="verificar-usuarios-sql-"))
(RAIZ / ".nexus-almacen").touch()
(RAIZ / ".registro").mkdir()
SERVICIO = "llave-de-servicio-solo-local"
os.environ.update(
    ALMACEN_RUTA=str(RAIZ), NEXUS_API_KEY=SERVICIO, AUTH_JWT_SECRET="secreto-de-prueba-de-al-menos-32-caracteres-xx",
    ENTREGAS_WEBHOOKS="off", CORREO_MODO="archivo",
)

import pyodbc  # noqa: E402

fallos: list[str] = []


def rev(que: str, ok: bool, extra: str = "") -> None:
    print(f"  {'OK   ' if ok else 'FALLA'}  {que}{(' — ' + str(extra)) if extra and not ok else ''}")
    if not ok:
        fallos.append(que)


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


AHORA = datetime.now(timezone.utc).replace(microsecond=0)


def hace(minutos: float) -> str:
    return (AHORA - timedelta(minutes=minutos)).isoformat(timespec="seconds")


def luego(horas: float) -> str:
    return (AHORA + timedelta(hours=horas)).isoformat(timespec="seconds")


def escribir(nombre: str, eventos: list) -> None:
    lineas = [e if isinstance(e, str) else json.dumps(e, ensure_ascii=False) for e in eventos]
    (RAIZ / ".registro" / nombre).write_text("".join(l + "\n" for l in lineas), encoding="utf-8")


def g(n: int) -> str:
    return f"a0000000-0000-4000-8000-{n:012x}"


def sid(n: int) -> str:
    return f"5e510000-0000-4000-8000-{n:012x}"


def r(n: int) -> str:
    """Un refresh «hasheado» legible: 64 hexadecimales."""
    return f"{n:064x}"


ORG_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
ORG_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
ORG_X = "cccccccc-3333-4333-8333-cccccccccccc"  # no existe
SUPER, SUPER2, ADMV, ANA, BETO, CARLA, DANI, DANI2, EVA1, EVA2, FER, GABY, HUGO, IVAN, KARLA, JUAN = (g(i) for i in range(1, 17))
RARO = "no-es-un-guid"
CORREO = {
    SUPER: "super@plataforma.mx", SUPER2: "super2@plataforma.mx", ADMV: "rodrigo@alfa.mx", ANA: "ana@alfa.mx", BETO: "beto@alfa.mx",
    CARLA: "carla@alfa.mx", DANI: "dani@beta.mx", DANI2: "dani@beta.mx", EVA1: "eva@beta.mx", EVA2: "eva@beta.mx", FER: "fer@demo.mx",
    GABY: "gaby@alfa.mx", HUGO: "hugo@alfa.mx", IVAN: "ivan@alfa.mx", KARLA: "karla@alfa.mx", JUAN: "juan@alfa.mx",
}
R_EXP = r(77)  # la sesión con vencimiento ilegible
R_BAD = "h" * 64  # un refresh que no es hexadecimal

try:
    from servicios import auth  # noqa: E402

    H_SEMILLA = auth.hash_de("Semilla-Temporal-2026!")
    H_DEFINITIVA = auth.hash_de("Clave-Definitiva-2026!")

    def creado(guid, nombre, apellidos="", minutos=0, **extra):
        return {"evento": "creado", "guid": guid, "email": CORREO.get(guid, extra.pop("email", "x@x.mx")), "nombre": nombre, "apellidos": apellidos,
                "esAdminPlataforma": False, "hash": H_SEMILLA, "debeCambiar": True, "en": hace(minutos), **extra}

    def nuevo(guid, nombre, paterno, materno, minutos, **extra):
        """Un alta con el formato de hoy (apellidos por separado y su recuperación propia)."""
        return creado(guid, nombre, " ".join(x for x in (paterno, materno) if x), minutos, apellidoPaterno=paterno, apellidoMaterno=materno,
                      telefono="+52 55 1234 5678", recuperacion={}, **extra)

    def membresia(guid, tenant, rol, minutos):
        return {"evento": "membresia", "guid": guid, "tenantGuid": tenant, "rol": rol, "en": hace(minutos)}

    def contrasena(guid, minutos):
        return {"evento": "contrasena", "guid": guid, "hash": H_DEFINITIVA, "motivo": "FIRST_LOGIN", "en": hace(minutos)}

    def sesion(guid, n, refresh, minutos, expira=None):
        return {"evento": "sesion", "sesionGuid": sid(n), "guid": guid, "refreshHash": refresh, "expira": expira or luego(24 * 7), "ip": "10.0.0.5",
                "userAgent": "Firefox", "en": hace(minutos)}

    def revocada(guid, n, motivo, minutos):
        return {"evento": "sesion_revocada", "guid": guid, "sesionGuid": sid(n) if n is not None else None, "motivo": motivo, "en": hace(minutos)}

    def intento(guid, email, exitoso, motivo, minutos, origen=None):
        return {"evento": "intento", "guid": guid, "email": email, "exitoso": exitoso, "motivo": None if exitoso else motivo, "ip": "10.0.0.9",
                "userAgent": "Chrome", "origen": origen, "en": hace(minutos)}

    titulo("0 · Lo que un ambiente ya tenía: SQL hasta la 0013 y los JSONL del almacén")
    escribir("tenants.jsonl", [
        {"evento": "creada", "guid": ORG_A, "nombre": "Alfa", "slug": "alfa", "codigo": "NEX-00001", "adminGuid": ADMV,
         "recuperacion": {"email": "recupera@alfa.mx", "telefono": "5511112222"}, "en": hace(9000)},
        {"evento": "creada", "guid": ORG_B, "nombre": "Beta", "slug": "beta", "codigo": "NEX-00002", "adminGuid": DANI2, "recuperacion": {}, "en": hace(8900)},
    ])
    escribir("usuarios.jsonl", [
        # Un superadmin del bootstrap de antes (nombre y apellidos juntos), con su primer acceso hecho, y otro que nunca entró.
        {"evento": "creado", "guid": SUPER, "email": CORREO[SUPER], "nombre": "Ada", "apellidos": "Lovelace Byron", "esAdminPlataforma": True,
         "hash": H_SEMILLA, "debeCambiar": True, "en": hace(8800)},
        contrasena(SUPER, 8790),
        intento(SUPER, CORREO[SUPER], True, None, 8780, origen="nav-super"),
        {"evento": "creado", "guid": SUPER2, "email": CORREO[SUPER2], "nombre": "Grace", "apellidos": "Hopper", "esAdminPlataforma": True,
         "hash": H_SEMILLA, "debeCambiar": True, "en": hace(8700)},
        # El administrador de Alfa, de antes del 2026-10-14: el nombre completo junto y SIN recuperación propia (la hereda de su organización).
        creado(ADMV, "Rodrigo Alberto De la Garza Treviño", "", 8600, telefono="+52 55 0000 0000"),
        membresia(ADMV, ORG_A, "ADMIN", 8600),
        contrasena(ADMV, 8590),
        # Ana: formato de hoy, recuperación propia, una sesión renovada (la vieja ROTATED), una viva, una vencida sin cerrar, una con un
        # refresh que no es hexadecimal y otra con un vencimiento ilegible; tres contraseñas malas desde su navegador.
        nuevo(ANA, "Ana", "López", "Ruiz", 8000),
        membresia(ANA, ORG_A, "ANALISTA", 8000),
        contrasena(ANA, 7990),
        {"evento": "datos", "guid": ANA, "nombre": "Ana", "apellidos": "López Ruiz", "apellidoPaterno": "López", "apellidoMaterno": "Ruiz",
         "telefono": "+52 55 1234 5678", "recuperacion": {"email": "ana.personal@correo.mx", "telefono": None}, "en": hace(7980)},
        sesion(ANA, 1, r(1), 3000, luego(24 * 6)),
        revocada(ANA, 1, "ROTATED", 2000),
        sesion(ANA, 2, r(2), 2000),
        sesion(ANA, 3, r(3), 3500, hace(10)),
        sesion(ANA, 4, R_BAD, 1900),
        sesion(ANA, 5, R_EXP, 1800, "mañana temprano"),
        *[intento(ANA, CORREO[ANA], False, "BAD_PASSWORD", 100 - i, origen="nav-1") for i in range(3)],
        # Beto: bloqueado por cinco contraseñas malas (y su navegador también).
        nuevo(BETO, "Beto", "Gómez", "", 7900),
        membresia(BETO, ORG_A, "OPERADOR", 7900),
        contrasena(BETO, 7890),
        *[intento(BETO, CORREO[BETO], False, "BAD_PASSWORD", 5 - i, origen="nav-2") for i in range(5)],
        # Carla: desactivada, con su sesión cerrada y el motivo.
        nuevo(CARLA, "Carla", "Mena", "Ortiz", 7800),
        membresia(CARLA, ORG_A, "VIEWER", 7800),
        contrasena(CARLA, 7790),
        sesion(CARLA, 6, r(6), 1000),
        revocada(CARLA, None, "DEACTIVATED", 500),
        {"evento": "estado", "guid": CARLA, "activo": False, "motivo": "Dejó la empresa", "por": ADMV, "en": hace(500)},
        # Dani: retirada (con su sesión cerrada) y dada de alta otra vez con el MISMO correo.
        nuevo(DANI, "Daniela", "Paz", "", 7700),
        membresia(DANI, ORG_B, "SUPERVISOR", 7700),
        sesion(DANI, 7, r(7), 900),
        revocada(DANI, None, "DELETED", 800),
        {"evento": "eliminado", "guid": DANI, "motivo": "Cuenta duplicada", "por": "CLI", "en": hace(800)},
        nuevo(DANI2, "Daniela", "Paz", "Luna", 700),
        membresia(DANI2, ORG_B, "ADMIN", 700),
        # Dos cuentas VIGENTES con el mismo correo (tres ambientes escribían el mismo archivo): la segunda no podía entrar.
        nuevo(EVA1, "Eva", "Rocha", "", 600),
        membresia(EVA1, ORG_B, "AUDITOR", 600),
        nuevo(EVA2, "Eva", "Rocha", "Segunda", 590),
        membresia(EVA2, ORG_B, "AUDITOR", 590),
        # Fer, en el espacio `demo` (no es una organización: sin llave foránea), con un nombre de 200 caracteres que son emojis (400 unidades de UTF-16).
        nuevo(FER, "Fer" + "🌵" * 197, "Díaz", "", 580),
        membresia(FER, "demo", "SUPERVISOR", 580),
        # Gaby: cierre forzado con motivo.
        nuevo(GABY, "Gaby", "Solís", "", 575),
        membresia(GABY, ORG_A, "ANALISTA", 575),
        contrasena(GABY, 574),
        sesion(GABY, 8, r(8), 400),
        {"evento": "cierre_forzado", "guid": GABY, "motivo": "Sesión robada", "por": ADMV, "en": hace(300)},
        revocada(GABY, None, "FORCED_LOGOUT", 300),
        # Hugo: alta y edición de antes del 2026-10-14 (el nombre completo en `nombre`).
        creado(HUGO, "Hugo Sánchez Márquez", "", 570),
        membresia(HUGO, ORG_A, "OPERADOR", 570),
        {"evento": "datos", "guid": HUGO, "nombre": "Hugo Alberto Sánchez Márquez", "apellidos": "", "telefono": None, "en": hace(560)},
        # Textos al límite de lo que acepta la API, hechos de emojis (cada uno ocupa dos unidades en un nvarchar): no tumban la importación.
        {**sesion(HUGO, 10, r(10), 520), "userAgent": "🦊" * 500, "ip": "🌐" * 45},
        {**intento(None, "nadie2@alfa.mx", False, "UNKNOWN_EMAIL", 41, origen="nav-5"), "userAgent": "🦊" * 500},
        # Iván: pendiente desde hace días, con la invitación reemitida hace una hora; Karla: pendiente y caducada.
        nuevo(IVAN, "Iván", "Cruz", "", 7200),
        membresia(IVAN, ORG_A, "ANALISTA", 7200),
        {"evento": "invitacion", "guid": IVAN, "hash": H_SEMILLA, "en": hace(60)},
        {"evento": "cierre_forzado", "guid": IVAN, "motivo": "🧯" * 500, "por": ADMV, "en": hace(59)},
        nuevo(KARLA, "Karla", "Vega", "", 7100),
        membresia(KARLA, ORG_A, "ANALISTA", 7100),
        # Juan: una membresía de una organización que NO existe (no entra), y desactivado y reactivado con motivo.
        nuevo(JUAN, "Juan", "Pérez", "", 550),
        membresia(JUAN, ORG_A, "ANALISTA", 550),
        membresia(JUAN, ORG_X, "ANALISTA", 549),
        {"evento": "estado", "guid": JUAN, "activo": False, "motivo": "Vacaciones", "por": ADMV, "en": hace(400)},
        {"evento": "estado", "guid": JUAN, "activo": True, "motivo": "Regresó", "por": ADMV, "en": hace(390)},
        # Una cuenta con un guid que no sirve (y lo que cuelga de ella).
        {"evento": "creado", "guid": RARO, "email": "raro@alfa.mx", "nombre": "Raro", "apellidos": "", "esAdminPlataforma": False, "hash": H_SEMILLA,
         "debeCambiar": True, "en": hace(540)},
        membresia(RARO, ORG_A, "ANALISTA", 540),
        sesion(RARO, 9, r(9), 530),
        # Un navegador que se bloqueó por correos que no existen y luego se restableció; otro que acertó después de fallar.
        *[intento(None, "nadie@alfa.mx", False, "UNKNOWN_EMAIL", 50 - i, origen="nav-3") for i in range(5)],
        {"evento": "origen_reiniciado", "origen": "nav-3", "en": hace(45)},
        intento(None, "otro@alfa.mx", False, "UNKNOWN_EMAIL", 44, origen="nav-4"),
        intento(None, "otro2@alfa.mx", False, "UNKNOWN_EMAIL", 43, origen="nav-4"),
        intento(SUPER, CORREO[SUPER], True, None, 42, origen="nav-4"),
        "esto no es un evento{",
        "[1, 2, 3]",
    ])
    escribir("tokens.jsonl", [
        {"evento": "creado", "guid": "00000000-0000-4000-8000-000000000001", "hash": r(501), "usuarioGuid": ANA, "tipo": "RECUPERACION", "expira": luego(1), "en": hace(5)},
        {"evento": "creado", "guid": "00000000-0000-4000-8000-000000000002", "hash": r(502), "usuarioGuid": EVA2, "tipo": "ACTIVACION", "expira": luego(48), "en": hace(5)},
        {"evento": "creado", "guid": "00000000-0000-4000-8000-000000000003", "hash": r(503), "usuarioGuid": g(999), "tipo": "ACTIVACION", "expira": luego(48), "en": hace(5)},
    ])
    rev("se sembraron los JSONL (con lo viejo, lo raro y lo que no se puede importar)", True)

    titulo("1 · La 0014, con el corredor real")
    BASE["aplicar"](14)
    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    k = con.cursor()
    rev("la base dice versión 14", k.execute("SELECT MAX(version) FROM nexus.schemaMigrations").fetchone()[0] == 14)
    tablas_nuevas = {f[0] for f in k.execute("SELECT name FROM sys.tables WHERE schema_id = SCHEMA_ID('nexus')").fetchall()}
    rev("existen las seis tablas nuevas",
        {"users", "userMemberships", "userSessions", "loginAttempts", "loginOrigins", "userAdminEvents"} <= tablas_nuevas, str(sorted(tablas_nuevas)))
    fk = k.execute("SELECT is_not_trusted, is_disabled FROM sys.foreign_keys WHERE name = 'FK_accessTokens_user'").fetchone()
    rev("los enlaces tienen su llave foránea a la cuenta, activa y sin revisar lo de antes (WITH NOCHECK)", fk is not None and tuple(fk) == (True, False), str(fk))
    con.close()

    from servicios import importacion_json, json_heredado, tokens, usuarios  # noqa: E402
    from servicios.almacen import ErrorAlmacen  # noqa: E402

    def probar(fn, *args, **kwargs):
        try:
            return ("ok", fn(*args, **kwargs))
        except Exception as e:  # noqa: BLE001
            return ("error", type(e).__name__, getattr(e, "motivo", None) or (str(e) if isinstance(e, (usuarios.YaExiste, ValueError)) else None))

    def igual(que: str, fn, ajustar=None) -> None:
        """Lo que contesta SQL, contra lo congelado (lo que el JSONL y SQL contestaban igual el 2026-11-11)."""
        ok, detalle = pruebas_sql.congelado(__file__, que, fn())
        rev(que, ok, detalle)

    titulo("2 · La importación")
    importacion_json.asegurar_todo()
    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    k = con.cursor()
    hechos = {f[0]: (f[1], f[2]) for f in k.execute("SELECT registryName, rowsImported, rowsSkipped FROM nexus.jsonImports").fetchall()}
    cuenta = lambda sql: k.execute(sql).fetchone()[0]  # noqa: E731
    por_tabla = {t: cuenta(f"SELECT count(*) FROM nexus.{t}") for t in ("users", "userMemberships", "userSessions", "loginAttempts", "loginOrigins", "userAdminEvents")}
    rev("se importó: 16 cuentas, 14 membresías, 8 sesiones, 18 intentos, 3 navegadores y 7 acciones de administración",
        por_tabla == {"users": 16, "userMemberships": 14, "userSessions": 8, "loginAttempts": 18, "loginOrigins": 3, "userAdminEvents": 7}, str(por_tabla))
    rev("y se anotó así en jsonImports (66 renglones, 4 saltados)", hechos.get("usuarios") == (66, 4), str(hechos.get("usuarios")))
    rev("los textos de emojis al límite entraron completos (nombre de 400 unidades, user agent de 1000, motivo de 1000)",
        k.execute(f"SELECT firstName FROM nexus.users WHERE guid = '{FER}'").fetchone()[0] == "Fer" + "🌵" * 197
        and k.execute(f"SELECT userAgent FROM nexus.userSessions WHERE guid = '{sid(10)}'").fetchone()[0] == "🦊" * 500
        and k.execute(f"SELECT reason FROM nexus.userAdminEvents WHERE userGuid = '{IVAN}'").fetchone()[0] == "🧯" * 500)
    rev("la cuenta con un guid que no sirve no entró, ni su sesión", cuenta("SELECT count(*) FROM nexus.users WHERE email = 'raro@alfa.mx'") == 0
        and cuenta(f"SELECT count(*) FROM nexus.userSessions WHERE guid = '{sid(9)}'") == 0)
    rev("Dani retirada entró CON su fecha de retiro, y su nuevo alta con el mismo correo, vigente",
        cuenta(f"SELECT count(*) FROM nexus.users WHERE guid = '{DANI}' AND deletedAt IS NOT NULL") == 1
        and cuenta(f"SELECT count(*) FROM nexus.users WHERE guid = '{DANI2}' AND deletedAt IS NULL") == 1)
    rev("de las dos Evas vigentes con el mismo correo, la segunda entró RETIRADA y con su motivo",
        cuenta(f"SELECT count(*) FROM nexus.users WHERE guid = '{EVA2}' AND deletedAt IS NOT NULL") == 1
        and cuenta(f"SELECT count(*) FROM nexus.userAdminEvents WHERE userGuid = '{EVA2}' AND kind = 'DELETED' AND byWhom = 'importacion'") == 1)
    rev("la membresía de la organización que no existe no entró (la de `demo` sí)",
        cuenta(f"SELECT count(*) FROM nexus.userMemberships WHERE tenant = '{ORG_X}'") == 0
        and cuenta("SELECT count(*) FROM nexus.userMemberships WHERE tenant = 'demo'") == 1)
    rev("el administrador de antes del 2026-10-14 trae YA la recuperación de su organización",
        json.loads(k.execute(f"SELECT recoveryJson FROM nexus.users WHERE guid = '{ADMV}'").fetchone()[0]) == {"email": "recupera@alfa.mx", "telefono": "5511112222"})
    rev("la sesión con un refresh ilegible no entró; la del vencimiento ilegible entró ya vencida",
        cuenta(f"SELECT count(*) FROM nexus.userSessions WHERE guid = '{sid(4)}'") == 0
        and cuenta(f"SELECT count(*) FROM nexus.userSessions WHERE guid = '{sid(5)}' AND expiresAt = createdAt") == 1)
    rev("los navegadores: el de Ana con 3 errores, el de Beto bloqueado; el restablecido y el que acertó, no hacen falta",
        {f[0]: (f[1], f[2] is not None) for f in k.execute("SELECT originId, failedAttempts, lockedUntil FROM nexus.loginOrigins").fetchall()}
        == {"nav-1": (3, False), "nav-2": (0, True), "nav-5": (1, False)})
    rev("la bitácora de administración: desactivar, retirar, cierres forzados, y Juan desactivado y reactivado",
        sorted((f[0], f[1], f[2]) for f in k.execute("SELECT kind, reason, byWhom FROM nexus.userAdminEvents WHERE byWhom <> 'importacion'").fetchall())
        == sorted([("DEACTIVATED", "Dejó la empresa", ADMV), ("DELETED", "Cuenta duplicada", "CLI"), ("SESSIONS_CLOSED", "Sesión robada", ADMV),
                   ("SESSIONS_CLOSED", "🧯" * 500, ADMV), ("DEACTIVATED", "Vacaciones", ADMV), ("REACTIVATED", "Regresó", ADMV)]))
    rev("los enlaces: el de una cuenta que no existe se saltó; el de la Eva retirada sí entró (su fila existe)",
        hechos.get("tokens") == (2, 1) and cuenta(f"SELECT count(*) FROM nexus.accessTokens WHERE userGuid = '{EVA2}'") == 1, str(hechos.get("tokens")))
    importacion_json.olvidar()
    importacion_json.asegurar_todo()
    rev("importar otra vez no duplica nada", cuenta("SELECT count(*) FROM nexus.users") == 16 and cuenta("SELECT count(*) FROM nexus.loginAttempts") == 18)
    con.close()

    titulo("3 · Las lecturas, iguales en los dos")
    COMPARABLES = [SUPER, SUPER2, ADMV, ANA, BETO, CARLA, DANI2, EVA1, FER, GABY, HUGO, IVAN, KARLA]
    for guid in COMPARABLES:
        igual(f"para_login de {CORREO[guid]} (con el hash, los intentos, el bloqueo, la recuperación y las membresías)", lambda guid=guid: usuarios.para_login(CORREO[guid]))
    igual("por_guid de todas", lambda: [usuarios.por_guid(x) for x in COMPARABLES])
    igual("la retirada no existe para nadie", lambda: probar(usuarios.por_guid, DANI)[:2])
    vivos_jsonl, _, sesiones_jsonl = json_heredado.usuarios_indice(json_heredado.eventos("usuarios.jsonl"))
    rev("diferencia esperada: la segunda Eva sí existía en el JSONL (sin poder entrar) y en SQL quedó retirada",
        EVA2 in vivos_jsonl and probar(usuarios.por_guid, EVA2)[:2] == ("error", "NoEncontrado"))
    rev("diferencia esperada: Juan pierde la membresía de la organización que no existe",
        [m["tenantGuid"] for m in vivos_jsonl[JUAN]["membresias"]] == [ORG_A, ORG_X] and [m["tenantGuid"] for m in usuarios.por_guid(JUAN)["membresias"]] == [ORG_A])

    def sin_lo_que_no_entro(lista):
        return [{**u, "membresias": [m for m in u["membresias"] if m["tenantGuid"] != ORG_X]} for u in lista if u["guid"] not in (EVA2, RARO)]

    for tenant in (ORG_A, ORG_B, "demo"):
        igual(f"de_tenant({tenant[:8]}…): mismas personas, mismo orden", lambda tenant=tenant: usuarios.de_tenant(tenant), sin_lo_que_no_entro)
    igual("administradores_de_plataforma", usuarios.administradores_de_plataforma)
    igual("para_listado de Alfa (rol, número, sesión activa, acceso vencido)",
          lambda: [usuarios.para_listado(u, ORG_A, i + 1) for i, u in enumerate([x for x in usuarios.de_tenant(ORG_A) if x["guid"] != RARO])])
    igual("tiene_sesion de cada una", lambda: [usuarios.tiene_sesion(x) for x in COMPARABLES + [JUAN]])
    igual("por_guid_y_sesion: viva, renovada, vencida sin cerrar, ajena, cerrada a la fuerza, desactivada, inexistente",
          lambda: [usuarios.por_guid_y_sesion(a, b)[1] for a, b in ((ANA, sid(2)), (ANA, sid(1)), (ANA, sid(3)), (ANA, sid(8)), (GABY, sid(8)), (CARLA, sid(6)), (ANA, "nope"))])
    igual("por_guid_y_sesion de una retirada: no existe", lambda: probar(usuarios.por_guid_y_sesion, DANI, sid(7))[:2])
    igual("sesion_por_hash: viva, renovada, vencida, desactivada, de una retirada (sin cuenta), cerrada a la fuerza, inexistente",
          lambda: [usuarios.sesion_por_hash(x) for x in (r(2), r(1), r(3), r(6), r(7), r(8), r(12345))])
    igual("estado_origen de cada navegador", lambda: [usuarios.estado_origen(o) for o in ("nav-1", "nav-2", "nav-3", "nav-4", "nav-5", "nav-super", "nuevo", None)])
    igual("acceso vencido: Karla sí, Iván (reemitido) no, el superadmin pendiente nunca",
          lambda: [usuarios.acceso_vencido(usuarios.para_login(CORREO[x])) for x in (KARLA, IVAN, SUPER2, ANA)])
    igual("puede_eliminar: superadmin (hay otro), administrador de organización, una analista",
          lambda: [probar(usuarios.puede_eliminar, x)[:3] for x in (SUPER, ADMV, ANA)])
    rev("diferencia esperada: la sesión con un refresh ilegible ya no existe en SQL",
        any(s.get("refreshHash") == R_BAD for s in sesiones_jsonl.values()) and usuarios.sesion_por_hash(R_BAD) is None)

    titulo("4 · Las escrituras, iguales en los dos")
    DINAMICOS = {"guid", "sesionGuid", "creadoEn", "ultimoLoginEn", "temporalEmitidaEn", "expira", "creadaEn", "revocadaEn", "bloqueadoHasta", "refreshHash"}

    def normal(x):
        if isinstance(x, dict):
            return {k: ("<dinámico>" if k in DINAMICOS and v is not None else normal(v)) for k, v in x.items()}
        if isinstance(x, (list, tuple)):
            return type(x)(normal(v) for v in x)
        return x

    def bloqueo(par):
        return (par[0], par[1] is not None)

    SIETE_DIAS = AHORA + timedelta(days=7)

    def escenario_altas():
        s = []
        u = usuarios.crear_usuario("nora@alfa.mx", "Nora", "", "h-nora", "+52 55 9999 0000", ORG_A, "ANALISTA", apellido_paterno="Núñez",
                                   apellido_materno="Ortiz", recuperacion={"email": "nora@correo.mx", "telefono": None})
        s.append(normal(u))
        s.append(probar(usuarios.crear_usuario, "  NORA@alfa.mx ", "Otra", "", "h"))
        v = usuarios.crear_usuario("vale@alfa.mx", "María de los Ángeles De la Cruz Pérez", "", "h-vale", None, ORG_A, "VIEWER")
        s.append(normal(v))
        s.append(normal(usuarios.bootstrap_admin("plataforma3@x.mx", "Grace", "Hopper Murray", "h-p3")))
        s.append(probar(usuarios.bootstrap_admin, "plataforma3@x.mx", "G", "H", "h"))
        s.append(probar(usuarios.crear_usuario, "no es correo", "X", "", "h"))
        s.append(probar(usuarios.crear_usuario, CORREO[DANI2], "X", "", "h"))
        s.append(normal(usuarios.agregar_membresia(u["guid"], ORG_B, "SUPERVISOR"))["membresias"])
        s.append(normal(usuarios.agregar_membresia(u["guid"], ORG_A, "AUDITOR"))["membresias"])
        s.append(probar(usuarios.agregar_membresia, g(999), ORG_A, "ANALISTA")[:2])
        s.append(normal(usuarios.actualizar_datos(v["guid"], "María de los Ángeles De la Cruz Pérez", "", "5511223344")))
        s.append(normal(usuarios.actualizar_datos(v["guid"], "Ana Lucía Gómez Soto", "", None)))
        s.append(normal(usuarios.actualizar_datos(HUGO, "Hugo Alberto Sánchez Márquez", "", "5500001111")))
        s.append(normal(usuarios.actualizar_perfil(u["guid"], "Nora Elena", "Núñez", "", "+52 55 1111 0000", {"email": "otro@correo.mx", "telefono": "5500000000"})))
        s.append(normal(usuarios.actualizar_perfil(ADMV, "Rodrigo", "De la Garza", "Treviño", None, {})))
        s.append(probar(usuarios.actualizar_perfil, g(999), "X", "", "", None, None)[:2])
        s.append(probar(usuarios.actualizar_datos, g(999), "X")[:2])
        s.append([x["email"] for x in usuarios.de_tenant(ORG_A) if x["guid"] != RARO])
        return s

    ok, detalle = pruebas_sql.congelado(__file__, "altas, correo repetido, bootstrap, membresías (la reasignada se va al final), datos del administrador y perfil", escenario_altas())
    rev("altas, correo repetido, bootstrap, membresías (la reasignada se va al final), datos del administrador y perfil", ok, detalle)

    def escenario_intentos():
        s = []
        u = usuarios.crear_usuario("intentos@alfa.mx", "Iñaki", "Torres", "h", None, ORG_A, "ANALISTA")
        for _ in range(4):
            s.append(bloqueo(usuarios.registrar_resultado_login(u["guid"], "intentos@alfa.mx", False, "BAD_PASSWORD", "1.1.1.1", "UA", "nav-i")))
        s.append(bloqueo(usuarios.registrar_resultado_login(u["guid"], "intentos@alfa.mx", False, "BAD_PASSWORD", "1.1.1.1", "UA", "nav-i")))
        s.append(bloqueo(usuarios.registrar_resultado_login(u["guid"], "intentos@alfa.mx", False, "LOCKED", "1.1.1.1", "UA", "nav-i")))
        p = usuarios.para_login("intentos@alfa.mx")
        s.append((p["fallidos"], p["bloqueadoHasta"] is not None))
        s.append(bloqueo(usuarios.estado_origen("nav-i")))
        for _ in range(5):
            usuarios.registrar_resultado_login(None, "nadie@alfa.mx", False, "UNKNOWN_EMAIL", None, None, "nav-n")
        s.append(bloqueo(usuarios.estado_origen("nav-n")))
        usuarios.reiniciar_intentos_de_origen("nav-n")
        s.append(usuarios.estado_origen("nav-n"))
        s.append(usuarios.registrar_resultado_login(u["guid"], "intentos@alfa.mx", True, None, "1.1.1.1", "UA", "nav-i"))
        p = usuarios.para_login("intentos@alfa.mx")
        s.append((p["fallidos"], p["bloqueadoHasta"], p["ultimoLoginEn"] is not None))
        s.append(usuarios.estado_origen("nav-i"))
        # Ana traía 3 errores importados: dos más la bloquean, a ella y a su navegador.
        s.append(bloqueo(usuarios.registrar_resultado_login(ANA, CORREO[ANA], False, "BAD_PASSWORD", None, None, "nav-1")))
        s.append(bloqueo(usuarios.registrar_resultado_login(ANA, CORREO[ANA], False, "BAD_PASSWORD", None, None, "nav-1")))
        s.append(bloqueo(usuarios.estado_origen("nav-1")))
        s.append(bloqueo(usuarios.registrar_resultado_login(None, "x@alfa.mx", False, "LOCKED", None, None, None)))
        s.append(usuarios.registrar_resultado_login(g(999), "fantasma@alfa.mx", False, "BAD_PASSWORD", None, None, None))
        return s

    ok, detalle = pruebas_sql.congelado(__file__, "intentos: la quinta bloquea, lo bloqueado no cuenta, el navegador cuenta correos que no existen, restablecer y acertar ponen en cero", escenario_intentos())
    rev("intentos: la quinta bloquea, lo bloqueado no cuenta, el navegador cuenta correos que no existen, restablecer y acertar ponen en cero", ok, detalle)

    def escenario_sesiones():
        s = []
        u = usuarios.crear_usuario("sesiones@alfa.mx", "Sergio", "Ramos", "h-ses", None, ORG_A, "ANALISTA")
        gu = u["guid"]
        s1 = usuarios.crear_sesion(gu, r(1001), SIETE_DIAS, "1.1.1.1", "UA")
        s.append((usuarios.tiene_sesion(gu), usuarios.por_guid_y_sesion(gu, s1)[1]))
        s2, publico = usuarios.rotar_sesion(r(1001), r(1002), SIETE_DIAS, "1.1.1.1", "UA")
        s.append(normal(publico))
        s.append(probar(usuarios.rotar_sesion, r(1001), r(1003), SIETE_DIAS, None, None)[:3])
        s.append((usuarios.por_guid_y_sesion(gu, s1)[1], usuarios.por_guid_y_sesion(gu, s2)[1]))
        s.append(normal(usuarios.sesion_por_hash(r(1001))))
        s.append((usuarios.revocar_sesiones(gu, s2, "LOGOUT"), usuarios.revocar_sesiones(gu, s2, "LOGOUT"), usuarios.tiene_sesion(gu)))
        usuarios.crear_sesion(gu, r(1004), AHORA - timedelta(hours=1), None, None)
        s.append(usuarios.tiene_sesion(gu))  # una sesión abierta pero ya vencida no cuenta
        s.append(probar(usuarios.rotar_sesion, r(1004), r(1005), SIETE_DIAS, None, None)[:3])
        s.append(probar(usuarios.crear_sesion, gu, r(1006), SIETE_DIAS, None, None, "otro-hash")[:3])
        usuarios.cambiar_estado(gu, False, "prueba", ADMV)
        s.append(probar(usuarios.crear_sesion, gu, r(1007), SIETE_DIAS, None, None, "h-ses")[:3])
        usuarios.cambiar_estado(gu, True, "regresa", ADMV)
        s.append(probar(usuarios.crear_sesion, gu, r(1008), SIETE_DIAS, None, None, "h-ses")[0])
        s.append(probar(usuarios.crear_sesion, g(999), r(1009), SIETE_DIAS, None, None)[:2])
        # Las sesiones importadas: la viva se renueva; la vencida, la renovada, la del vencimiento ilegible y la desactivada, no.
        s.append(normal(usuarios.rotar_sesion(r(2), r(1010), SIETE_DIAS, None, None)[1]))
        s.append([probar(usuarios.rotar_sesion, x, r(1011 + i), SIETE_DIAS, None, None)[:3] for i, x in enumerate((r(3), r(1), R_EXP, r(6), r(7), r(54321)))])
        s.append(usuarios.tiene_sesion(ANA))
        return s

    ok, detalle = pruebas_sql.congelado(__file__, "sesiones: abrir, renovar una vez, cerrar, vencida, contraseña que cambió, desactivada; las importadas se renuevan o no como antes", escenario_sesiones())
    rev("sesiones: abrir, renovar una vez, cerrar, vencida, contraseña que cambió, desactivada; las importadas se renuevan o no como antes", ok, detalle)

    def escenario_contrasenas():
        s = []
        u = usuarios.crear_usuario("claves@alfa.mx", "Clara", "Llaves", "h-0", None, ORG_A, "ANALISTA")
        gu = u["guid"]
        for n in (2001, 2002, 2003):
            usuarios.crear_sesion(gu, r(n), SIETE_DIAS, None, None)
        publico, cerradas = usuarios.fijar_contrasena_y_cerrar(gu, "h-1", "FIRST_LOGIN")
        s.append((normal(publico), cerradas))
        a_ = usuarios.crear_sesion(gu, r(2004), SIETE_DIAS, None, None)
        b_ = usuarios.crear_sesion(gu, r(2005), SIETE_DIAS, None, None)
        s.append(usuarios.fijar_contrasena_y_cerrar(gu, "h-2", "CHANGE", sesion_guid=a_, hash_verificado="h-1")[1])
        s.append((usuarios.por_guid_y_sesion(gu, a_)[1], usuarios.por_guid_y_sesion(gu, b_)[1]))
        s.append(probar(usuarios.fijar_contrasena_y_cerrar, gu, "h-3", "CHANGE", sesion_guid=b_, hash_verificado="h-2")[:3])
        s.append(probar(usuarios.fijar_contrasena_y_cerrar, gu, "h-3", "CHANGE", sesion_guid=a_, hash_verificado="h-1")[:3])
        s.append(usuarios.fijar_contrasena_y_cerrar(gu, "h-4", "RESET")[1])
        s.append(probar(usuarios.fijar_contrasena, gu, "h", "OTRO")[:3])
        s.append(probar(usuarios.fijar_contrasena, g(999), "h", "RESET")[:2])
        s.append(usuarios.para_login("claves@alfa.mx")["hash"])
        p = usuarios.crear_usuario("pendiente@alfa.mx", "Pedro", "Espera", "t-1", None, ORG_A, "ANALISTA")
        usuarios.crear_sesion(p["guid"], r(2006), SIETE_DIAS, None, None)
        usuarios.registrar_resultado_login(p["guid"], "pendiente@alfa.mx", False, "BAD_PASSWORD", None, None, None)
        s.append(normal(usuarios.emitir_temporal(p["guid"], "t-2")))
        s.append((usuarios.tiene_sesion(p["guid"]), usuarios.para_login("pendiente@alfa.mx")["fallidos"], usuarios.para_login("pendiente@alfa.mx")["hash"]))
        s.append(probar(usuarios.emitir_temporal, gu, "x")[:2])
        s.append(probar(usuarios.emitir_temporal, g(999), "x")[:2])
        s.append(normal(usuarios.emitir_temporal(IVAN, "t-ivan")))
        return s

    ok, detalle = pruebas_sql.congelado(__file__, "contraseñas: primer acceso y restablecer cierran todas, cambiar cierra las otras, sesión cerrada o contraseña vieja se rechazan, temporal nueva", escenario_contrasenas())
    rev("contraseñas: primer acceso y restablecer cierran todas, cambiar cierra las otras, sesión cerrada o contraseña vieja se rechazan, temporal nueva", ok, detalle)

    def escenario_administracion():
        s = []
        u = usuarios.crear_usuario("admin.prueba@alfa.mx", "Alma", "Prueba", "h", None, ORG_A, "SUPERVISOR")
        gu = u["guid"]
        for n in (3001, 3002):
            usuarios.crear_sesion(gu, r(n), SIETE_DIAS, None, None)
        s.append(normal(usuarios.cambiar_estado(gu, False, "Licencia", ADMV)))
        s.append(usuarios.tiene_sesion(gu))
        s.append(normal(usuarios.cambiar_estado(gu, True, "Regresó", ADMV))["activo"])
        for n in (3003, 3004):
            usuarios.crear_sesion(gu, r(n), SIETE_DIAS, None, None)
        s.append(usuarios.forzar_cierre(gu, "Robo", ADMV, ORG_A, usuarios.ACTOR_ADMIN_ORGANIZACION))
        s.append(usuarios.cerrar_sesiones(gu, "otra vez", ADMV))
        s.append(probar(usuarios.cambiar_estado, g(999), False)[:2])
        s.append(probar(usuarios.cerrar_sesiones, g(999), "x")[:2])
        s.append(probar(usuarios.eliminar_usuario, ADMV, "x")[:3])
        s.append(probar(usuarios.eliminar_usuario, gu, "  ")[:3])
        s.append(probar(usuarios.eliminar_usuario, g(999), "x")[:2])
        s.append(normal(usuarios.eliminar_usuario(gu, "Ya no trabaja aquí", por=ADMV)))
        s.append((probar(usuarios.por_guid, gu)[:2], usuarios.para_login("admin.prueba@alfa.mx")))
        s.append(normal(usuarios.crear_usuario("admin.prueba@alfa.mx", "Nuevo", "Dueño", "h", None, ORG_A, "VIEWER"))["email"])
        s.append(normal(usuarios.eliminar_usuario(ADMV, "Se forzó", por="CLI", forzar=True))["email"])
        # Superadmins: SUPER, SUPER2 y el de las altas. Se retiran dos; el último ya no.
        s.append(probar(usuarios.eliminar_usuario, SUPER2, "nunca entró", por="monitor")[0])
        otro = next(x["guid"] for x in usuarios.administradores_de_plataforma() if x["email"] == "plataforma3@x.mx")
        s.append(probar(usuarios.eliminar_usuario, otro, "prueba", por="monitor")[0])
        s.append(probar(usuarios.eliminar_usuario, SUPER, "el último", por="monitor")[:3])
        s.append(probar(usuarios.puede_eliminar, SUPER)[:3])
        s.append([x["email"] for x in usuarios.administradores_de_plataforma()])
        s.append([normal(usuarios.para_listado(x, ORG_A, i + 1)) for i, x in enumerate([y for y in usuarios.de_tenant(ORG_A) if y["guid"] != RARO])])
        return s

    ok, detalle = pruebas_sql.congelado(__file__, "administración: desactivar cierra sesiones, cierre forzado, retiro (con sus negativas), el correo queda libre, el último superadmin no se va", escenario_administracion())
    rev("administración: desactivar cierra sesiones, cierre forzado, retiro (con sus negativas), el correo queda libre, el último superadmin no se va", ok, detalle)

    titulo("5 · Lo que solo la base cuida")
    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    k = con.cursor()

    def falla(sql: str, *parametros) -> bool:
        try:
            k.execute(sql, *parametros)
        except pyodbc.Error:
            return True
        return False

    alma = k.execute("SELECT guid FROM nexus.users WHERE email = 'admin.prueba@alfa.mx' AND deletedAt IS NOT NULL").fetchone()[0]
    eventos_alma = sorted((f[0], f[1], f[2]) for f in k.execute("SELECT kind, reason, byWhom FROM nexus.userAdminEvents WHERE userGuid = ?", alma).fetchall())
    rev("la bitácora guardó cada acción con su motivo y quién: desactivar, reactivar, dos cierres forzados y el retiro",
        eventos_alma == sorted([("DEACTIVATED", "Licencia", ADMV), ("REACTIVATED", "Regresó", ADMV), ("SESSIONS_CLOSED", "Robo", ADMV),
                                ("SESSIONS_CLOSED", "otra vez", ADMV), ("DELETED", "Ya no trabaja aquí", ADMV)]), str(eventos_alma))
    insertar = "INSERT INTO nexus.users (guid, email, firstName, lastName, secondLastName, isPlatformAdmin, mustChangePassword, createdAt) VALUES (?, ?, 'X', '', '', 0, 1, SYSUTCDATETIME())"
    rev("dos cuentas vigentes con el mismo correo: la base no deja", falla(insertar, g(500), "ana@alfa.mx"))
    rev("un guid que no tiene forma de guid: la base no deja", falla(insertar, "NO-ES-GUID", "nuevo1@alfa.mx"))
    rev("una membresía de una organización que no existe: la base no deja",
        falla("INSERT INTO nexus.userMemberships (userGuid, tenant, role, assignedAt) VALUES (?, ?, 'ANALISTA', SYSUTCDATETIME())", ANA, ORG_X))
    rev("una sesión de una cuenta que no existe: la base no deja",
        falla("INSERT INTO nexus.userSessions (guid, userGuid, refreshHash, expiresAt, createdAt) VALUES ('s-x', ?, ?, SYSUTCDATETIME(), SYSUTCDATETIME())", g(999), r(9999)))
    rev("un refresh que no es hexadecimal: la base no deja",
        falla("INSERT INTO nexus.userSessions (guid, userGuid, refreshHash, expiresAt, createdAt) VALUES ('s-y', ?, ?, SYSUTCDATETIME(), SYSUTCDATETIME())", ANA, R_BAD))
    con.close()
    rev("un enlace para una cuenta que no existe: la base no deja (503 de almacén, no un enlace huérfano)", probar(tokens.crear, g(999), tokens.ACTIVACION)[1] == "ErrorAlmacen")
    rev("y para una que sí, se emite", probar(tokens.crear, ANA, tokens.RECUPERACION)[0] == "ok")

    titulo("6 · A la vez")

    def a_la_vez(n: int, fn):
        barrera = threading.Barrier(n)

        def uno(i):
            barrera.wait()
            return probar(fn, i)

        with ThreadPoolExecutor(n) as ex:
            return list(ex.map(uno, range(n)))

    def vivas(guid: str) -> int:
        con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
        try:
            return con.cursor().execute("SELECT count(*) FROM nexus.userSessions WHERE userGuid = ? AND revokedAt IS NULL", guid).fetchone()[0]
        finally:
            con.close()

    u = usuarios.crear_usuario("carrera.refresh@alfa.mx", "Rita", "Rápida", "h-c", None, ORG_A, "ANALISTA")
    usuarios.crear_sesion(u["guid"], r(4000), SIETE_DIAS, None, None)
    res = a_la_vez(8, lambda i: usuarios.rotar_sesion(r(4000), r(4100 + i), SIETE_DIAS, None, None))
    rev("ocho renovaciones con el MISMO refresh: una gana y siete se rechazan", [x[0] for x in res].count("ok") == 1
        and all(x[2] == "invalida" for x in res if x[0] == "error"), str([x[:3] if x[0] == "error" else "ok" for x in res]))
    rev("y queda UNA sola sesión viva", vivas(u["guid"]) == 1)
    res = a_la_vez(8, lambda i: usuarios.crear_usuario("carrera.alta@alfa.mx", f"Persona {i}", "Alta", "h", None, ORG_A, "ANALISTA"))
    rev("ocho altas con el mismo correo: una entra y siete reciben «ya registrado»",
        [x[0] for x in res].count("ok") == 1 and all(x[1] == "YaExiste" for x in res if x[0] == "error"), str([x[:2] for x in res]))
    u = usuarios.crear_usuario("carrera.intentos@alfa.mx", "Iris", "Intentos", "h", None, ORG_A, "ANALISTA")
    res = a_la_vez(10, lambda i: usuarios.registrar_resultado_login(u["guid"], "carrera.intentos@alfa.mx", False, "BAD_PASSWORD", None, None, "nav-carrera"))
    # Una por una darían 1-4 sin bloqueo, la quinta bloquea (0), 1-4 con el bloqueo vigente, y la décima bloquea otra vez (0).
    rev("diez contraseñas malas a la vez: se cuentan todas (dos bloqueos, ninguna perdida)",
        sorted(x[1][0] for x in res) == [0, 0, 1, 1, 2, 2, 3, 3, 4, 4] and sum(1 for x in res if not x[1][1]) == 4, str([x[1] for x in res]))
    rev("y el navegador, igual: cinco y cinco, bloqueado", usuarios.estado_origen("nav-carrera")[0] == 0 and usuarios.estado_origen("nav-carrera")[1] is not None)
    for vuelta in range(3):
        u = usuarios.crear_usuario(f"carrera.reset{vuelta}@alfa.mx", "Rosa", "Reset", "viejo", None, ORG_A, "ANALISTA")
        res = a_la_vez(9, lambda i, u=u, vuelta=vuelta: usuarios.fijar_contrasena(u["guid"], "nuevo", "RESET") if i == 0
                       else usuarios.crear_sesion(u["guid"], r(5000 + vuelta * 100 + i), SIETE_DIAS, None, None, "viejo"))
        rev(f"restablecer la contraseña mientras ocho logins con la vieja abren sesión: ninguna queda viva (vuelta {vuelta + 1})",
            vivas(u["guid"]) == 0, str([x[:3] for x in res]))
    for vuelta in range(3):
        u = usuarios.crear_usuario(f"carrera.cierre{vuelta}@alfa.mx", "Carmen", "Cierre", "h", None, ORG_A, "ANALISTA")
        usuarios.crear_sesion(u["guid"], r(6000 + vuelta * 10), SIETE_DIAS, None, None)
        res = a_la_vez(2, lambda i, u=u, vuelta=vuelta: usuarios.cerrar_sesiones(u["guid"], "carrera", ADMV) if i == 0
                       else usuarios.rotar_sesion(r(6000 + vuelta * 10), r(6001 + vuelta * 10), SIETE_DIAS, None, None))
        rev(f"un cierre forzado y una renovación a la vez: no queda viva ninguna (vuelta {vuelta + 1})", vivas(u["guid"]) == 0, str([x[:3] for x in res]))
    activos = [x["guid"] for x in usuarios.administradores_de_plataforma()]
    nuevos = [usuarios.bootstrap_admin(f"ultimo{i}@x.mx", "Último", "Admin", "h")["guid"] for i in range(2)]
    for x in activos:
        usuarios.eliminar_usuario(x, "dejar solo dos", por="monitor")
    res = a_la_vez(2, lambda i: usuarios.eliminar_usuario(nuevos[i], "a la vez", por="monitor"))
    rev("dos retiros a la vez de los dos últimos superadmins: uno se va y el otro se queda (último administrador)",
        sorted(x[0] for x in res) == ["error", "ok"] and any(x[2] == "ultimo_superadmin" for x in res if x[0] == "error"), str([x[:3] for x in res]))
    rev("y la plataforma conserva uno", len(usuarios.administradores_de_plataforma()) == 1)

    titulo("7 · La API de punta a punta, contra la base")
    import email as correo_lib  # noqa: E402
    import email.policy  # noqa: E402

    from fastapi.testclient import TestClient  # noqa: E402

    from app import app  # noqa: E402

    c = TestClient(app)
    srv = {"x-api-key": SERVICIO}

    def entrar(correo: str, clave: str, origen: str | None = None):
        return c.post("/auth/login", json={"email": correo, "password": clave, "ip": "10.1.1.1", "userAgent": "verificar", **({"origen": origen} if origen else {})}, headers=srv)

    def con_bearer(token: str) -> dict:
        return {**srv, "Authorization": f"Bearer {token}"}

    def primer_acceso(correo: str, temporal: str, definitiva: str) -> dict:
        r1 = entrar(correo, temporal)
        assert r1.status_code == 200, r1.text
        r2 = c.post("/auth/contrasena", json={"nueva": definitiva, "confirmacion": definitiva}, headers=con_bearer(r1.json()["accessToken"]))
        assert r2.status_code == 200, r2.text
        r3 = entrar(correo, definitiva)
        assert r3.status_code == 200, r3.text
        return r3.json()

    temporal = auth.contrasena_temporal()
    usuarios.bootstrap_admin("plataforma.api@x.mx", "Ada", "Api", auth.hash_de(temporal))
    r1 = entrar("plataforma.api@x.mx", temporal)
    rev("el superadmin entra con su temporal y se le pide cambiarla", r1.status_code == 200 and r1.json()["usuario"]["debeCambiarContrasena"] is True, r1.text[:200])
    plataforma = primer_acceso("plataforma.api@x.mx", temporal, "Cobre-Lento-Azul-47!")
    rev("…la cambia y vuelve a entrar con la definitiva", plataforma["usuario"]["debeCambiarContrasena"] is False)
    rev("GET /auth/sesion: viva", c.get("/auth/sesion", headers=con_bearer(plataforma["accessToken"])).json().get("estado") == "viva")
    r2 = c.post("/organizaciones/", json={"nombre": "Gamma API", "adminNombre": "Gil", "adminApellidos": "Gamma", "adminEmail": "gil@gamma.mx"},
                headers=con_bearer(plataforma["accessToken"]))
    rev("alta de una organización con su administrador: 201", r2.status_code == 201, r2.text[:200])
    org = r2.json()["organizacion"]["guid"]
    admin = primer_acceso("gil@gamma.mx", r2.json()["contrasenaTemporal"], "Nexus-Rumbo-A-2026!")
    rev("el JWT del administrador lleva su organización y su rol", auth.verificar_acceso(admin["accessToken"])["tnt"] == org
        and auth.verificar_acceso(admin["accessToken"])["rol"] == "ADMIN")
    r3 = c.post("/usuarios/", json={"nombre": "Lía", "apellidos": "Gamma Ortiz", "email": "lia@gamma.mx", "telefono": "+52 55 3333 4444", "rol": "ANALISTA"},
                headers=con_bearer(admin["accessToken"]))
    rev("el administrador da de alta a una analista: 201", r3.status_code == 201, r3.text[:200])
    lia_guid = r3.json()["usuario"]["guid"]
    lia = primer_acceso("lia@gamma.mx", r3.json()["contrasenaTemporal"], "Sauce-Verde-Lento-31!")
    lista = c.get("/usuarios/", headers=con_bearer(admin["accessToken"])).json()["usuarios"]
    rev("el listado la muestra con su rol, su número y su sesión abierta",
        [(x["numero"], x["rol"], x["sesionActiva"]) for x in lista] == [("001", "ADMIN", True), ("002", "ANALISTA", True)], str(lista)[:300])
    r4 = c.post(f"/usuarios/{lia_guid}/estado", json={"activo": False, "motivo": "Auditoría interna"}, headers=con_bearer(admin["accessToken"]))
    rev("desactivarla: 200", r4.status_code == 200, r4.text[:200])
    rev("su sesión dice «desactivada» y ya no puede entrar (403)",
        c.get("/auth/sesion", headers=con_bearer(lia["accessToken"])).json().get("estado") == "desactivada" and entrar("lia@gamma.mx", "Sauce-Verde-Lento-31!").status_code == 403)
    c.post(f"/usuarios/{lia_guid}/estado", json={"activo": True, "motivo": "Se aclaró"}, headers=con_bearer(admin["accessToken"]))
    lia = entrar("lia@gamma.mx", "Sauce-Verde-Lento-31!").json()
    r5 = c.post(f"/usuarios/{lia_guid}/cerrar-sesion", json={"motivo": "Dispositivo perdido"}, headers=con_bearer(admin["accessToken"]))
    rev("reactivada entra; el cierre forzado responde 200", r5.status_code == 200 and "accessToken" in lia, r5.text[:200])
    rev("…y su JWT ya no vale (sesión cerrada) ni su refresh renueva",
        c.get("/auth/sesion", headers=con_bearer(lia["accessToken"])).json().get("estado") == "cerrada"
        and c.post("/auth/refresh", json={"refreshToken": lia["refreshToken"]}, headers=srv).status_code == 401)
    lia = entrar("lia@gamma.mx", "Sauce-Verde-Lento-31!").json()
    r6 = c.post("/auth/refresh", json={"refreshToken": lia["refreshToken"]}, headers=srv)
    rev("renovar: 200 con un refresh nuevo; el viejo ya no sirve (un solo uso)",
        r6.status_code == 200 and c.post("/auth/refresh", json={"refreshToken": lia["refreshToken"]}, headers=srv).status_code == 401, r6.text[:200])
    rev("cerrar sesión: 204, y ese refresh ya no renueva",
        c.post("/auth/logout", json={"refreshToken": r6.json()["refreshToken"]}, headers=srv).status_code == 204
        and c.post("/auth/refresh", json={"refreshToken": r6.json()["refreshToken"]}, headers=srv).status_code == 401)
    for f in (RAIZ / "correos").glob("*.eml") if (RAIZ / "correos").exists() else []:
        f.unlink()
    r7 = c.post("/auth/recuperacion/solicitar", json={"email": "lia@gamma.mx", "ip": "10.0.0.1", "userAgent": "verificar"}, headers=srv)
    mandados = sorted((RAIZ / "correos").glob("*.eml")) if (RAIZ / "correos").exists() else []
    cuerpo = correo_lib.message_from_bytes(mandados[-1].read_bytes(), policy=email.policy.default).get_body(preferencelist=("plain",)).get_content() if mandados else ""
    enlace = re.search(r"\?t=([A-Za-z0-9_-]+)", cuerpo)
    rev("pedir recuperar la contraseña: 200 y el correo trae su enlace", r7.status_code == 200 and enlace is not None, r7.text[:200])
    r8 = c.post("/auth/recuperacion/confirmar", json={"token": enlace.group(1) if enlace else "x", "nueva": "Roble-Firme-Claro-58!", "confirmacion": "Roble-Firme-Claro-58!", "ip": "10.0.0.1"}, headers=srv)
    # Primero la anterior (un intento fallido) y después la nueva, que acierta y pone el contador en cero.
    rev("confirmar con la nueva: 200; no entra con la anterior y sí con ella",
        r8.status_code == 200 and entrar("lia@gamma.mx", "Sauce-Verde-Lento-31!").status_code == 401 and entrar("lia@gamma.mx", "Roble-Firme-Claro-58!").status_code == 200, r8.text[:200])
    resp = [entrar("lia@gamma.mx", "Mal-Mal-Mal-2026!", origen="nav-api").status_code for _ in range(5)]
    rev("cinco contraseñas malas: la quinta bloquea (423) y con la buena sigue bloqueada", resp == [401, 401, 401, 401, 423]
        and entrar("lia@gamma.mx", "Roble-Firme-Claro-58!").status_code == 423, str(resp))
    r9 = c.post("/admins-plataforma/", json={"email": "monitor.api@x.mx", "nombre": "Mona", "apellidos": "Monitor", "enviarCorreo": False}, headers=srv)
    rev("Nexus Monitor da de alta un superadmin: 201", r9.status_code == 201, r9.text[:200])
    r10 = c.delete(f"/admins-plataforma/{r9.json()['administrador']['guid'] if r9.status_code == 201 else 'x'}", params={"motivo": "prueba"}, headers=srv)
    rev("…y lo retira: 200", r10.status_code == 200, r10.text[:200])
    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    k = con.cursor()
    rev("todo quedó en SQL: Lía con su bitácora (desactivar, reactivar, cierre forzado) y su enlace de recuperación ya gastado",
        k.execute("SELECT count(*) FROM nexus.userAdminEvents WHERE userGuid = ?", lia_guid).fetchone()[0] == 3
        and k.execute("SELECT count(*) FROM nexus.accessTokens WHERE userGuid = ? AND kind = 'RECUPERACION' AND usedAt IS NOT NULL", lia_guid).fetchone()[0] == 1)
    con.close()
    rev("y NADA se escribió en usuarios.jsonl desde la importación", (RAIZ / ".registro" / "usuarios.jsonl").read_text(encoding="utf-8").count('"lia@gamma.mx"') == 0)

except Exception as exc:  # noqa: BLE001
    import traceback

    traceback.print_exc()
    fallos.append(f"excepción: {exc!r}")

print()
if fallos:
    print(f"FALLARON {len(fallos)}:")
    for f in fallos:
        print(f"  - {f}")
    sys.exit(1)
print("Todo bien.")
