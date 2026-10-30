"""¿Enlaces de un solo uso, API keys, su uso, webhooks y sus entregas se comportan IGUAL en SQL (migración 0008) que en el almacén, y la importación
única desde sus JSONL funciona?

    .venv/Scripts/python verificar_integraciones_sql.py [--dejar]

Levanta el SQL Server desechable de `pruebas_sql.py` hasta la 0007, le mete lo que dev ya tenía (un documento con una llave que aún no existe en
SQL, otro con una que sí existirá), siembra los JSONL como los tiene dev (con lo que NO se puede importar: una llave de una organización que no
existe, un webhook repetido, una entrega pendiente, miles de líneas de uso), aplica la 0008 con el corredor real, y luego:

1. importa una sola vez y compara cada lectura contra el MISMO módulo leyendo el JSONL (se cambia `SQLSERVER_HOST` del módulo para elegir);
2. hace las mismas escrituras en los dos y compara lo que devuelven, incluido el trabajador de entregas con su reloj y su reinicio;
3. comprueba lo que solo la base cuida: llaves foráneas, que `a7K9` y `A7k9` sean dos llaves, una URL vigente por organización, el secreto que
   desaparece al eliminar;
4. lanza hilos a la vez: un enlace se gasta UNA vez, una URL se registra UNA vez, un aviso se programa UNA vez, los ids de llave no se repiten;
5. recorre la API de punta a punta: emitir una llave, subir con ella, ver su uso, revocarla, y un webhook que recibe su aviso.

Sin Docker, se salta y lo dice.
"""

import json
import os
import shutil
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

import pruebas_sql  # noqa: E402

import pyodbc  # noqa: E402
import seguridad_llaves  # noqa: E402

BASE = pruebas_sql.preparar("nexus-integraciones-prueba", 14341, dejar="--dejar" in sys.argv, hasta=7)
RAIZ = Path(tempfile.mkdtemp(prefix="verificar-integraciones-"))
(RAIZ / ".nexus-almacen").touch()
(RAIZ / ".registro").mkdir()

from cryptography.fernet import Fernet  # noqa: E402

CLAVE_CIFRADO = Fernet.generate_key().decode()
SERVICIO = "llave-de-servicio-solo-local"
os.environ.update(
    ALMACEN_RUTA=str(RAIZ), NEXUS_API_KEY=SERVICIO, AUTH_JWT_SECRET="secreto-de-prueba-de-al-menos-32-caracteres-xx",
    WEBHOOKS_CLAVE_CIFRADO=CLAVE_CIFRADO, ENTREGAS_WEBHOOKS="off",
)

fallos: list[str] = []


def rev(que: str, ok: bool, extra: str = "") -> None:
    print(f"  {'OK   ' if ok else 'FALLA'}  {que}{(' — ' + str(extra)) if extra and not ok else ''}")
    if not ok:
        fallos.append(que)


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


ORG_A = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"
ORG_B = "bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb"
NO_EXISTE = "dddddddd-4444-4444-8444-dddddddddddd"
AHORA = datetime.now(timezone.utc).replace(microsecond=0)
E1 = "1" * 32


def hace(minutos: float, ms: bool = False) -> str:
    return (AHORA - timedelta(minutes=minutos)).isoformat(timespec="milliseconds" if ms else "seconds")


def luego(horas: float) -> str:
    return (AHORA + timedelta(hours=horas)).isoformat(timespec="seconds")


def escribir(nombre: str, eventos: list) -> None:
    lineas = [e if isinstance(e, str) else json.dumps(e, ensure_ascii=False) for e in eventos]
    (RAIZ / ".registro" / nombre).write_text("".join(l + "\n" for l in lineas), encoding="utf-8")


def cuerpo_de(mensaje: str, tipo: str = "documento.completado") -> str:
    return json.dumps({"type": tipo, "timestamp": hace(90), "data": {"entradaId": E1, "estado": "completado"}}, separators=(",", ":"))


try:
    titulo("0 · Lo que dev ya tenía: SQL hasta la 0007 y los JSONL del almacén")
    import hashlib

    hash_de = lambda texto: hashlib.sha256(texto.encode()).hexdigest()  # noqa: E731
    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    k = con.cursor()
    for n, llave in ((1, "aB12"), (2, "orfa")):
        k.execute(
            "INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel, keyId) VALUES (?, 'plataforma', ?, 'x', 1, 'x', 'x', 'API', ?)",
            "documento-" + str(n) * 32, str(n) * 64, llave,
        )
    con.close()

    escribir("tenants.jsonl", [
        {"evento": "creada", "guid": ORG_A, "nombre": "Alfa", "slug": "alfa", "codigo": "NEX-00001", "adminGuid": "admin-a", "recuperacion": {}, "en": hace(1000)},
        {"evento": "creada", "guid": ORG_B, "nombre": "Beta", "slug": "beta", "codigo": "NEX-00002", "adminGuid": "admin-b", "recuperacion": {}, "en": hace(999)},
    ])
    escribir("tokens.jsonl", [
        {"evento": "creado", "guid": "00000000-0000-4000-8000-000000000001", "hash": hash_de("tok-uno"), "usuarioGuid": "u1", "tipo": "ACTIVACION", "expira": luego(48), "en": hace(60)},
        {"evento": "creado", "guid": "00000000-0000-4000-8000-000000000002", "hash": hash_de("tok-dos"), "usuarioGuid": "u1", "tipo": "RECUPERACION", "expira": luego(1), "en": hace(50)},
        {"evento": "usado", "hash": hash_de("tok-dos"), "en": hace(40)},
        {"evento": "creado", "guid": "00000000-0000-4000-8000-000000000003", "hash": hash_de("tok-tres"), "usuarioGuid": "u2", "tipo": "ACTIVACION", "expira": luego(48), "en": hace(30)},
        {"evento": "invalidado", "hash": hash_de("tok-tres"), "motivo": "REEMPLAZADO", "en": hace(20)},
        {"evento": "creado", "guid": "00000000-0000-4000-8000-000000000004", "hash": hash_de("tok-cuatro"), "usuarioGuid": "u2", "tipo": "RECUPERACION", "expira": hace(10), "en": hace(70)},
        {"evento": "creado", "guid": "00000000-0000-4000-8000-000000000005", "hash": "no-es-un-hash", "usuarioGuid": "u3", "tipo": "ACTIVACION", "expira": luego(1), "en": hace(5)},
    ])
    def armar(ident: str, relleno: str) -> str:
        """Una llave de cliente de verdad (formato y checksum) con ese identificador."""
        cuerpo = f"nxdoc_live_{ident}_sk_" + (relleno * 26)[:26]
        return cuerpo + seguridad_llaves.checksum_de(cuerpo)

    SECRETOS = {"aB12": armar("aB12", "a"), "AB12": armar("AB12", "b"), "pl01": armar("pl01", "c"), "ex01": armar("ex01", "e")}
    llave = lambda ident, tenant, secreto, **extra: {"evento": "emitida", "id": ident, "tenant": tenant, "secret_hash": hash_de(secreto), "nombre": f"Llave {ident}", "descripcion": "de prueba", "creadaEn": hace(100), "expiraEn": luego(24 * 30), **extra}  # noqa: E731
    escribir("llaves.jsonl", [
        llave("aB12", ORG_A, SECRETOS["aB12"]),
        llave("AB12", ORG_A, SECRETOS["AB12"]),
        llave("pl01", "plataforma", SECRETOS["pl01"]),
        llave("ex01", ORG_B, SECRETOS["ex01"], expiraEn="ayer por la tarde"),
        llave("xx99", NO_EXISTE, "secreto-5"),
        llave("zz_9", ORG_A, "secreto-6"),
        {"evento": "revocada", "id": "AB12", "en": hace(30)},
    ])
    uso = lambda llave_id, minutos, status=200: {"en": hace(minutos, ms=True), "llave": llave_id, "ruta": "/bandeja/", "metodo": "POST", "status": status, "ms": 10 + int(minutos) % 40, "bytes": 1000 + int(minutos)}  # noqa: E731
    escribir("uso-llaves.jsonl", (
        [uso("aB12", 1 + (i % 170) + i / 10000, 200 if i % 7 else 500) for i in range(1500)]
        + [uso("AB12", 5), uso("AB12", 6), uso("AB12", 7, 401), uso("nope", 5), uso("nope", 6), "esto no es un evento{"]
    ))
    secreto_a1 = "whsec_" + "QUJD" * 8
    cifrar = lambda secreto: Fernet(CLAVE_CIFRADO.encode()).encrypt(secreto.encode()).decode()  # noqa: E731
    wh = lambda ident, tenant, url, eventos, minutos, secreto="whsec_x": {"evento": "registrado", "id": ident, "tenant": tenant, "url": url, "eventos": eventos, "secret_cifrado": cifrar(secreto), "creadoEn": hace(minutos)}  # noqa: E731
    escribir("webhooks.jsonl", [
        wh("wh_a1", ORG_A, "https://a.example/hook", ["documento.completado", "documento.fallido"], 500, secreto_a1),
        {"evento": "validacion_fallida", "id": "wh_a1", "en": hace(450), "intentos": 5, "codigo": 500, "motivo": "boom"},
        {"evento": "validado", "id": "wh_a1", "en": hace(400), "codigo": 200, "ms": 12},
        wh("wh_a2", ORG_A, "https://a.example/2", ["documento.rechazado"], 300),
        {"evento": "estado", "id": "wh_a2", "estado": "inactivo", "en": hace(290)},
        {"evento": "validacion_fallida", "id": "wh_a2", "en": hace(200), "intentos": 5, "codigo": None, "motivo": "sin respuesta"},
        {"evento": "editado", "id": "wh_a2", "url": "https://a.example/2", "eventos": ["documento.rechazado", "documento.fallido"], "urlCambio": False, "en": hace(190)},
        wh("wh_a3", ORG_A, "https://a.example/3", ["documento.completado"], 250),
        {"evento": "eliminado", "id": "wh_a3", "en": hace(100)},
        wh("wh_a4", ORG_A, "https://a.example/hook", ["documento.completado"], 10),
        wh("wh_b1", ORG_B, "https://b.example/", ["documento.completado"], 200),
        {"evento": "validado", "id": "wh_b1", "en": hace(150), "codigo": 200, "ms": 9},
        wh("wh_x", NO_EXISTE, "https://x.example/", ["documento.completado"], 100),
    ])
    intento = lambda mensaje, n, minutos, ok, codigo, ms, motivo=None, tipo="documento.completado": {"evento": "intento", "id": mensaje, "webhook": "wh_a1", "tenant": ORG_A, "tipo": tipo, "n": n, "en": hace(minutos), "ok": ok, "codigo": codigo, "ms": ms, "motivo": motivo}  # noqa: E731
    programada = lambda mensaje, webhook="wh_a1", tipo="documento.completado": {"evento": "programada", "id": mensaje, "webhook": webhook, "tenant": ORG_A, "tipo": tipo, "cuerpo": cuerpo_de(mensaje, tipo), "en": hace(90)}  # noqa: E731
    escribir("entregas-webhooks.jsonl", (
        [intento("msg_val", 1 + i % 5, 120 - (i % 60) / 10, False, None, None, "sin respuesta", "webhook.validacion") for i in range(1100)]
        + [intento("msg_val", 6, 119, True, 200, 7, None, "webhook.validacion")]
        + [{"evento": "aviso", "tenant": ORG_A, "entradaId": E1, "tipo": "documento.completado", "en": hace(90), "destinos": 4}]
        + [programada("msg_pend"), programada("msg_ok"), programada("msg_ago"), programada("msg_can"), programada("msg_huerfana", webhook="wh_nope")]
        + [intento("msg_pend", 1, 80, False, 500, 30, "HTTP 500"), intento("msg_ok", 1, 79, True, 200, 12)]
        + [intento("msg_ago", n, 70 - n, False, 503, 40, "HTTP 503") for n in range(1, 7)]
        + [{"evento": "agotada", "id": "msg_ago", "webhook": "wh_a1", "en": hace(60), "motivo": "HTTP 503"}]
        + [{"evento": "cancelada", "id": "msg_can", "webhook": "wh_a1", "en": hace(55), "motivo": "El webhook se desactivó."}]
    ))
    rev("se sembraron los JSONL (con lo que no se puede importar) y dos documentos con llave antes de la 0008", True)

    titulo("1 · La 0008, con el corredor real, sobre una base con filas")
    BASE["aplicar"](8)
    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    k = con.cursor()
    rev("la base dice versión 8", k.execute("SELECT MAX(version) FROM nexus.schemaMigrations").fetchone()[0] == 8)
    rev("los dos documentos con llave siguen ahí (la llave foránea nueva no se les exige: WITH NOCHECK)", k.execute("SELECT count(*) FROM nexus.documents WHERE keyId IS NOT NULL").fetchone()[0] == 2)
    con.close()

    from servicios import bandeja, entrega_webhooks, entregas_webhooks, importacion_json, llaves_cliente, tokens, uso_llaves, webhooks_cliente  # noqa: E402

    MODULOS = (tokens, llaves_cliente, uso_llaves, webhooks_cliente, entregas_webhooks)
    REAL = tokens.SQLSERVER_HOST

    def modo(sql: bool) -> None:
        for m in MODULOS:
            m.SQLSERVER_HOST = REAL if sql else ""
        uso_llaves._indice.clear()
        uso_llaves._indice_listo = False

    def ambos(fn):
        modo(False)
        a = fn()
        modo(True)
        s = fn()
        return a, s

    def probar(fn, *args, **kwargs):
        try:
            return ("ok", fn(*args, **kwargs))
        except Exception as e:  # noqa: BLE001
            return ("error", type(e).__name__, getattr(e, "codigo", None))

    titulo("2 · La importación")
    modo(True)
    importacion_json.asegurar_todo()
    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    k = con.cursor()
    hechos = {r[0]: (r[1], r[2]) for r in k.execute("SELECT registryName, rowsImported, rowsSkipped FROM nexus.jsonImports").fetchall()}
    esperado = {
        "tokens": (4, 1), "llaves": (4, 2), "uso-llaves": (1503, 2), "webhooks": (4, 2),
        # 1 aviso + 4 entregas (la de un webhook que no existe, saltada) + 1109 intentos
        "entregas-webhooks": (1 + 4 + 1109, 1),
    }
    rev("se importaron los cinco y se anotó cuántos y cuántos se saltaron", all(hechos.get(n) == v for n, v in esperado.items()), f"{ {n: hechos.get(n) for n in esperado} }")
    rev("las llaves saltadas fueron la de una organización que no existe y la de un id raro", k.execute("SELECT count(*) FROM nexus.apiKeys WHERE publicId IN ('xx99', 'zz_9')").fetchone()[0] == 0)
    rev("`aB12` y `AB12` son DOS llaves distintas (la base distingue mayúsculas)", k.execute("SELECT count(*) FROM nexus.apiKeys WHERE publicId IN ('aB12', 'AB12')").fetchone()[0] == 2)
    rev("el webhook repetido (misma URL vigente) y el de la organización que no existe NO entraron", k.execute("SELECT count(*) FROM nexus.webhooks WHERE publicId IN ('wh_a4', 'wh_x')").fetchone()[0] == 0)
    rev("el eliminado entró SIN secreto cifrado (y con su fecha de baja); los demás, con él",
        tuple(k.execute("SELECT count(*), sum(CASE WHEN encryptedSecret IS NULL THEN 1 ELSE 0 END) FROM nexus.webhooks WHERE deletedAt IS NOT NULL").fetchone()) == (1, 1)
        and k.execute("SELECT count(*) FROM nexus.webhooks WHERE deletedAt IS NULL AND encryptedSecret IS NOT NULL").fetchone()[0] == 3)
    estados = {r[0]: r[1] for r in k.execute("SELECT publicId, status FROM nexus.webhookDeliveries").fetchall()}
    rev("entregas: la entregada, la agotada y la cancelada conservan su estado; la que estaba PENDIENTE entra cancelada",
        estados == {"msg_pend": "cancelada", "msg_ok": "entregada", "msg_ago": "agotada", "msg_can": "cancelada"}, str(estados))
    rev("ninguna entrega quedó pendiente (así ningún ambiente reenvía lo de otro)", k.execute("SELECT count(*) FROM nexus.webhookDeliveries WHERE status = 'pendiente'").fetchone()[0] == 0)
    rev("la llave con la expiración ilegible entró YA VENCIDA", k.execute("SELECT expiresAt FROM nexus.apiKeys WHERE publicId = 'ex01'").fetchone()[0] <= AHORA.replace(tzinfo=None))
    importacion_json.olvidar()
    importacion_json.asegurar_todo()
    rev("importar otra vez no duplica nada", k.execute("SELECT (SELECT count(*) FROM nexus.accessTokens) * 1000000 + (SELECT count(*) FROM nexus.apiKeys) * 100000 + (SELECT count(*) FROM nexus.webhooks)").fetchone()[0] == 4 * 1000000 + 4 * 100000 + 4)
    con.close()

    titulo("3 · Las lecturas, iguales en los dos")

    def igual(que: str, fn) -> None:
        a, s = ambos(fn)
        rev(que, a == s, f"almacén={str(a)[:300]!r}\n          sql={str(s)[:300]!r}")

    for etiqueta, token, tipo in (
        ("vigente", "tok-uno", "ACTIVACION"), ("ya usado", "tok-dos", "RECUPERACION"), ("apagado", "tok-tres", "ACTIVACION"),
        ("vencido", "tok-cuatro", "RECUPERACION"), ("de otro tipo", "tok-uno", "RECUPERACION"), ("que no existe", "tok-nada", "ACTIVACION"),
    ):
        igual(f"tokens.revisar, {etiqueta}", lambda token=token, tipo=tipo: probar(tokens.revisar, token, tipo))
    igual("tokens.pendientes_de (la de u1 es la activación vigente; u2 no tiene)", lambda: ([t["guid"] for t in tokens.pendientes_de("u1")], tokens.pendientes_de("u2")))
    modo(False)
    archivo_llaves = [l for l in llaves_cliente.listar(ORG_A) if l["id"] != "zz_9"]
    modo(True)
    rev("llaves.listar: del más nuevo al más viejo, con las del mismo instante en el orden en que se emitieron (la de id raro no está en SQL)",
        archivo_llaves == llaves_cliente.listar(ORG_A) and [l["id"] for l in archivo_llaves] == ["aB12", "AB12"], f"{archivo_llaves}\n          {llaves_cliente.listar(ORG_A)}")
    igual("llaves.listar de plataforma", lambda: llaves_cliente.listar("plataforma"))
    igual("llaves.buscar_por_id", lambda: [llaves_cliente.buscar_por_id(i) for i in ("aB12", "AB12", "pl01", "nada")])
    igual("llaves.existe_en_otro_tenant", lambda: (llaves_cliente.existe_en_otro_tenant(ORG_B, "aB12"), llaves_cliente.existe_en_otro_tenant(ORG_A, "aB12"), llaves_cliente.existe_en_otro_tenant(ORG_A, "nada")))

    def verifica(secreto: str, ident: str) -> bool:
        fila = llaves_cliente.buscar_por_id(ident)
        return fila is not None and seguridad_llaves.verificar(secreto, lambda _i: fila)

    a, s = ambos(lambda: [verifica(SECRETOS[i], i) for i in ("aB12", "AB12", "pl01", "ex01")])
    rev("verificar una llave de verdad: la vigente pasa; la revocada y la de expiración ilegible NO (igual en el almacén y en SQL)", a == s == [True, False, True, False], f"{a} | {s}")
    desde, hasta = AHORA - timedelta(hours=4), AHORA + timedelta(hours=1)
    igual("uso.metricas: solicitudes, exitosas, p50, bytes, por día, tope semanal y último uso", lambda: uso_llaves.metricas("aB12", desde, hasta))
    igual("uso.metricas de una llave con otro estado", lambda: uso_llaves.metricas("AB12", desde, hasta))
    igual("uso.ultimos_usos y consumo semanal", lambda: (uso_llaves.ultimos_usos(["aB12", "AB12", "pl01", "nada"]), uso_llaves.consumo_semanal("aB12")))
    igual("uso.excede_limite", lambda: uso_llaves.excede_limite("aB12"))
    modo(False)
    archivo_wh = {w["id"]: w for w in webhooks_cliente.listar(ORG_A) if w["id"] != "wh_a4"}
    modo(True)
    sql_wh = {w["id"]: w for w in webhooks_cliente.listar(ORG_A)}
    rev("webhooks.listar: lo mismo (el repetido no está en SQL, el eliminado tampoco), con su estado, su validación y su fallo", archivo_wh == sql_wh and sorted(sql_wh) == ["wh_a1", "wh_a2"], f"{archivo_wh}\n          {sql_wh}")
    igual("webhooks.listar de la otra organización", lambda: webhooks_cliente.listar(ORG_B))
    igual("webhooks.suscritos (vigentes, validados, activos y suscritos)", lambda: [webhooks_cliente.suscritos(ORG_A, t) for t in webhooks_cliente.EVENTOS])
    igual("webhooks.para_entregar: el validado sí; el no validado, el eliminado y el que no existe, no", lambda: [webhooks_cliente.para_entregar(i) for i in ("wh_a1", "wh_a2", "wh_a3", "wh_nope")])
    igual("webhooks.secret_para_firmar: se descifra con la llave del .env; el eliminado, no hay", lambda: [webhooks_cliente.secret_para_firmar(i) for i in ("wh_a1", "wh_a3", "wh_nope")])
    igual("webhooks.existe_en_otro_tenant", lambda: (webhooks_cliente.existe_en_otro_tenant(ORG_B, "wh_a1"), webhooks_cliente.existe_en_otro_tenant(ORG_A, "wh_a1"), webhooks_cliente.existe_en_otro_tenant(ORG_B, "wh_a3")))
    igual("entregas.historial: los 50 más recientes, con el documento de cada entrega", lambda: entregas_webhooks.historial(ORG_A, "wh_a1"))
    igual("entregas.historial de otra organización: vacío", lambda: entregas_webhooks.historial(ORG_B, "wh_a1"))
    igual("entregas.metricas: intentos, errores y percentiles (los de validar no cuentan)", lambda: entregas_webhooks.metricas(ORG_A, "wh_a1", desde, hasta))
    rev("el secret que sale de SQL es el que se cifró", (modo(True), webhooks_cliente.secret_para_firmar("wh_a1"))[1] == secreto_a1)

    # Para que el almacén no reintente lo que SQL ya cerró, se cierra también ahí (así los dos arrancan igual los escenarios de abajo).
    with open(RAIZ / ".registro" / "entregas-webhooks.jsonl", "a", encoding="utf-8") as f:
        for mensaje in ("msg_pend", "msg_huerfana"):
            f.write(json.dumps({"evento": "cancelada", "id": mensaje, "webhook": "wh_a1", "en": hace(1), "motivo": "migración"}) + "\n")

    titulo("4 · Las escrituras, iguales en los dos")

    def escenario_tokens():
        salida = []
        t1, _ = tokens.crear("u-w1", tokens.ACTIVACION)
        t2, _ = tokens.crear("u-w1", tokens.ACTIVACION)
        salida.append(probar(tokens.revisar, t1, "ACTIVACION")[:2])
        salida.append(tokens.revisar(t2, "ACTIVACION")["motivo"])
        salida.append(probar(tokens.consumir, t2, "RECUPERACION")[:2])
        salida.append(tokens.consumir(t2, "ACTIVACION")["usuarioGuid"])
        salida.append(probar(tokens.consumir, t2, "ACTIVACION")[:2])
        tokens.crear("u-w1", tokens.RECUPERACION)
        tokens.crear("u-w1", tokens.ACTIVACION)
        salida.append(sorted(t["tipo"] for t in tokens.pendientes_de("u-w1")))
        salida.append(tokens.invalidar_de("u-w1", tokens.RECUPERACION, motivo="X"))
        salida.append(sorted(t["tipo"] for t in tokens.pendientes_de("u-w1")))
        salida.append(tokens.invalidar_de("u-w1", motivo="RESET"))
        salida.append(tokens.pendientes_de("u-w1"))
        salida.append(probar(tokens.crear, "u-w1", "NOPE")[:2])
        return salida

    a, s = ambos(escenario_tokens)
    rev("tokens: crear apaga el anterior, consumir gasta una vez, invalidar_de cuenta, un tipo desconocido se rechaza", a == s, f"{a}\n          {s}")

    def escenario_llaves():
        secreto, llave = llaves_cliente.emitir(ORG_A, "  Mi llave ", "para pruebas", 7)
        ident = llave["id"]
        salida = [llave["nombre"], llave["descripcion"], llave["revocadaEn"], seguridad_llaves.id_de(secreto) == ident, set(llave) == {"id", "tenant", "nombre", "descripcion", "creadaEn", "expiraEn", "revocadaEn"}]
        fila = llaves_cliente.buscar_por_id(ident)
        salida.append(seguridad_llaves.verificar(secreto, lambda _i: fila))
        primera = llaves_cliente.revocar(ORG_A, ident)
        salida.append((primera is not None, llaves_cliente.revocar(ORG_A, ident) == primera, llaves_cliente.revocar(ORG_B, ident), llaves_cliente.revocar(ORG_A, "nada")))
        salida.append(llaves_cliente.existe_en_otro_tenant(ORG_B, ident))
        fila = llaves_cliente.buscar_por_id(ident)
        salida.append(seguridad_llaves.verificar(secreto, lambda _i: fila))
        salida += [probar(llaves_cliente.emitir, ORG_A, "", "d", 7)[:2], probar(llaves_cliente.emitir, ORG_A, "n", "d", 5)[:2], probar(llaves_cliente.emitir, "no valido!", "n", "d", 7)[:2]]
        salida.append([l["id"] for l in llaves_cliente.listar(ORG_A)][:1] == [ident] or "orden")
        return salida

    a, s = ambos(escenario_llaves)
    rev("llaves: emitir, verificar, revocar (idempotente y solo en su organización), validaciones y el listado", a == s, f"{a}\n          {s}")

    def escenario_webhooks():
        url = "https://w.example/Hook?x=1"
        secreto, w = webhooks_cliente.registrar(ORG_A, url, ["documento.fallido", "documento.completado"])
        wid = w["id"]
        salida = [w["url"], w["eventos"], w["estado"], w["validadoEn"], w["fallidaEn"], secreto.startswith("whsec_"), set(w) == {"id", "url", "eventos", "estado", "creadoEn", "validadoEn", "fallidaEn"}]
        salida.append(probar(webhooks_cliente.registrar, ORG_A, url, ["documento.completado"])[:2])
        salida.append(probar(webhooks_cliente.registrar, ORG_A, "http://no-es-https.example/", ["documento.completado"])[:2])
        salida.append(probar(webhooks_cliente.registrar, ORG_A, "https://otra.example/", [])[:2])
        salida.append(webhooks_cliente.registrar(ORG_B, url, ["documento.completado"])[1]["url"])  # la misma URL en OTRA organización sí
        salida.append(webhooks_cliente.secret_para_firmar(wid) == secreto)
        f = webhooks_cliente.marcar_fallida(ORG_A, wid, 5, 500, "x")
        salida.append((f["validadoEn"], f["fallidaEn"] is not None))
        salida.append(webhooks_cliente.suscritos(ORG_A, "documento.fallido") == [])
        v = webhooks_cliente.marcar_validado(ORG_A, wid, 200, 5)
        salida.append((v["validadoEn"] is not None, v["fallidaEn"] is not None))
        salida.append(webhooks_cliente.suscritos(ORG_A, "documento.fallido") == [wid])
        salida.append(webhooks_cliente.cambiar_estado(ORG_A, wid, "inactivo")["estado"])
        salida.append((webhooks_cliente.suscritos(ORG_A, "documento.fallido"), webhooks_cliente.para_entregar(wid)))
        salida.append(webhooks_cliente.cambiar_estado(ORG_A, wid, "activo")["estado"])
        salida.append(webhooks_cliente.cambiar_estado(ORG_B, wid, "inactivo"))
        salida.append(probar(webhooks_cliente.cambiar_estado, ORG_A, wid, "dormido")[:2])
        e1 = webhooks_cliente.editar(ORG_A, wid, url, ["documento.completado"])
        salida.append((e1["validadoEn"] is not None, e1["eventos"]))
        e2 = webhooks_cliente.editar(ORG_A, wid, "https://w.example/otra", ["documento.rechazado", "documento.fallido"])
        salida.append((e2["url"], e2["eventos"], e2["validadoEn"], e2["fallidaEn"]))
        salida.append(probar(webhooks_cliente.editar, ORG_A, wid, "https://a.example/hook", ["documento.completado"])[:2])
        salida.append(webhooks_cliente.editar(ORG_B, wid, "https://nuevo.example/", ["documento.completado"]))
        salida.append(webhooks_cliente.para_validar(ORG_A, wid) is not None and webhooks_cliente.para_validar(ORG_A, wid)[0])
        salida.append((webhooks_cliente.eliminar(ORG_B, "wh_a1"), webhooks_cliente.eliminar(ORG_A, wid), webhooks_cliente.eliminar(ORG_A, wid), webhooks_cliente.eliminar(ORG_A, "wh_nope")))
        salida.append((webhooks_cliente.secret_para_firmar(wid), webhooks_cliente.para_validar(ORG_A, wid), wid in [w["id"] for w in webhooks_cliente.listar(ORG_A)]))
        salida.append(webhooks_cliente.registrar(ORG_A, "https://w.example/otra", ["documento.completado"])[1]["url"])  # su URL quedó libre
        return salida

    a, s = ambos(escenario_webhooks)
    rev("webhooks: registrar, duplicado, validar, activar, editar (la URL nueva pide validar de nuevo), eliminar (el secreto se va, la URL queda libre)", a == s, f"{a}\n          {s}")

    # Las entregas, con reloj propio y un endpoint de mentiras.
    reloj = {"t": datetime(2030, 1, 1, tzinfo=timezone.utc)}
    entregas_webhooks._ahora = lambda: reloj["t"]
    bandeja_real = bandeja.entrada_en_pipeline
    bandeja.entrada_en_pipeline = lambda tenant, entrada: {"recibidoEn": "2026-10-30T00:00:00+00:00"} if str(entrada).startswith("e") else None
    llamadas: dict[str, int] = {}

    def enviar_de_mentiras(url, secret, cuerpo, id_mensaje):
        tipo = json.loads(cuerpo)["type"]
        llamadas[id_mensaje] = llamadas.get(id_mensaje, 0) + 1
        if tipo == "documento.fallido" or llamadas[id_mensaje] == 1:
            raise entrega_webhooks.Rechazo("HTTP 503" if tipo == "documento.fallido" else "HTTP 500", 503 if tipo == "documento.fallido" else 500, 40 if tipo == "documento.fallido" else 30)
        return {"codigo": 200, "ms": 11}

    entrega_webhooks.enviar = enviar_de_mentiras

    def escenario_entregas():
        llamadas.clear()
        reloj["t"] = datetime(2030, 1, 1, tzinfo=timezone.utc)
        entregas_webhooks._pendientes.clear()
        entregas_webhooks._cargado = True
        _, w = webhooks_cliente.registrar(ORG_A, "https://e.example/hook", ["documento.completado", "documento.fallido"])
        wid = w["id"]
        webhooks_cliente.marcar_validado(ORG_A, wid, 200, 3)
        salida = [entregas_webhooks.avisar(ORG_A, "documento.completado", "e" * 32, "INE", None)]
        salida.append(entregas_webhooks.avisar(ORG_A, "documento.completado", "e" * 32, "INE", None))
        salida.append(probar(entregas_webhooks.avisar, ORG_A, "documento.rechazado", "e" * 32, None, "formato_no_soportado"))
        salida.append(entregas_webhooks.avisar(ORG_A, "documento.fallido", "e" * 31 + "4", None, "error_del_servicio"))  # otra entrada: la primera ya tiene su resultado final
        salida.append(probar(entregas_webhooks.avisar, ORG_A, "documento.completado", "f" * 32, None, None))
        salida.append(probar(entregas_webhooks.avisar, ORG_A, "expediente.completado", "e" * 32, None, None))
        salida.append(probar(entregas_webhooks.avisar, ORG_A, "documento.completado", "e" * 31 + "2", None, "motivo"))
        salida.append(entregas_webhooks.pendientes())
        entregas_webhooks.procesar_vencidas()
        salida.append(entregas_webhooks.pendientes())
        # Un reinicio: lo pendiente se va de la memoria y se recupera de donde se guardó.
        entregas_webhooks._pendientes.clear()
        entregas_webhooks._cargado = False
        reloj["t"] += timedelta(seconds=6)
        entregas_webhooks.procesar_vencidas()
        salida.append(entregas_webhooks.pendientes())
        for _ in range(6):
            reloj["t"] += timedelta(hours=6)
            entregas_webhooks.procesar_vencidas()
        salida.append(entregas_webhooks.pendientes())
        ids: dict[str, int] = {}
        historial = [
            (h["en"], h["tipo"], h["n"], h["ok"], h["codigo"], h["ms"], h["motivo"], ids.setdefault(h["mensajeId"], len(ids)), h["entradaId"])
            for h in entregas_webhooks.historial(ORG_A, wid, 100)
        ]
        salida.append(historial)
        metricas = entregas_webhooks.metricas(ORG_A, wid, datetime(2029, 12, 31, tzinfo=timezone.utc), datetime(2031, 1, 1, tzinfo=timezone.utc))
        salida.append((metricas["actual"], metricas["anterior"]))
        # Cancelar lo pendiente de un webhook que se desactiva.
        salida.append(entregas_webhooks.avisar(ORG_A, "documento.completado", "e" * 31 + "3", "INE", None))
        webhooks_cliente.cambiar_estado(ORG_A, wid, "inactivo")
        salida.append(entregas_webhooks.cancelar_de(wid, "Se desactivó."))
        salida.append(entregas_webhooks.pendientes())
        salida.append(len(entregas_webhooks.historial(ORG_A, wid, 100)))
        return salida

    a, s = ambos(escenario_entregas)
    rev("entregas: avisar (duplicado, resultado final repetido, entrada ajena, tipo que no existe), reintentos con el calendario, reinicio, agotada, historial, métricas y cancelar", a == s, f"{a}\n          {s}")
    # Cada aviso va a DOS webhooks: el que crea el escenario y wh_a1, que ya estaba validado, activo y suscrito.
    rev("…y lo que pasó fue lo esperado: 4 entregas programadas, 4 tras el primer intento, 2 tras el reinicio (las de «fallido» siguen), 0 al agotarse; al desactivar, 1 cancelada y 1 (la de wh_a1) sigue",
        s[7:11] == [4, 4, 2, 0] and s[-3:] == [1, 1, 8], str(s[7:11] + s[-3:]))

    titulo("5 · Lo que solo la base cuida")
    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    k = con.cursor()

    def rechaza(sql: str, *p) -> bool:
        try:
            k.execute(sql, *p)
            return False
        except pyodbc.Error:
            return True

    llave_nueva = "INSERT INTO nexus.apiKeys (publicId, tenant, secretHash, name, description, createdAt, expiresAt) VALUES (?, ?, ?, 'n', 'd', sysutcdatetime(), sysutcdatetime())"
    rev("una llave con el mismo id (exacto) NO entra; con otra capitalización SÍ", rechaza(llave_nueva, "aB12", "plataforma", "a" * 64) and not rechaza(llave_nueva, "Ab12", "plataforma", "b" * 64))
    rev("una llave de una organización que no existe NO entra (llave foránea); una de plataforma sí", rechaza(llave_nueva, "qq01", NO_EXISTE, "a" * 64) and not rechaza(llave_nueva, "qq02", "plataforma", "a" * 64))
    rev("un id con guion bajo o un hash que no es hexadecimal NO entran", rechaza(llave_nueva, "a_b1", "plataforma", "a" * 64) and rechaza(llave_nueva, "qq03", "plataforma", "Z" * 64))
    doc = "INSERT INTO nexus.documents (publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel, keyId) VALUES (?, 'plataforma', ?, 'x', 1, 'x', 'x', 'API', ?)"
    rev("un documento subido con una llave que no existe NO entra; con una que sí, sí; y distingue mayúsculas",
        rechaza(doc, "documento-" + "a" * 32, "a" * 64, "nada") and not rechaza(doc, "documento-" + "b" * 32, "b" * 64, "aB12") and rechaza(doc, "documento-" + "c" * 32, "c" * 64, "ab12"))
    rev("la bandeja igual: una entrada con una llave inexistente NO entra", rechaza(
        "INSERT INTO nexus.inboxEntries (publicId, tenant, relativePath, sha256, sizeBytes, mime, originalName, channel, keyId, receivedAt) VALUES (?, 'plataforma', 'x', ?, 1, 'x', 'x', 'API', 'nada', sysutcdatetime())", "9" * 32, "9" * 64))
    rev("el uso de una llave que no existe NO entra", rechaza("INSERT INTO nexus.apiKeyUsage (keyId, occurredAt, route, method, status, durationMs, bytes) VALUES (-1, sysutcdatetime(), 'x', 'GET', 200, 1, 1)"))
    rev("un enlace de un tipo inventado o con un hash mayúscula NO entra", rechaza(
        "INSERT INTO nexus.accessTokens (guid, tokenHash, userGuid, kind, expiresAt, createdAt) VALUES (?, ?, 'u', 'OTRO', sysutcdatetime(), sysutcdatetime())", "1" * 36, "a" * 64)
        and rechaza("INSERT INTO nexus.accessTokens (guid, tokenHash, userGuid, kind, expiresAt, createdAt) VALUES (?, ?, 'u', 'ACTIVACION', sysutcdatetime(), sysutcdatetime())", "2" * 36, "A" * 64))
    webhook = "INSERT INTO nexus.webhooks (publicId, tenant, url, encryptedSecret, createdAt, deletedAt) VALUES (?, ?, ?, ?, sysutcdatetime(), ?)"
    ahora_sql = AHORA.replace(tzinfo=None)
    rev("dos webhooks vigentes de la misma organización con la misma URL: el segundo NO entra", rechaza(webhook, "wh_dup1", ORG_A, "https://a.example/hook", "x", None))
    rev("…pero sí en otra organización, o si el primero ya se eliminó", not rechaza(webhook, "wh_dup2", ORG_B, "https://a.example/hook", "x", None) and not rechaza(webhook, "wh_dup3", ORG_A, "https://a.example/3", "x", None))
    rev("un webhook vigente SIN secreto, o uno eliminado CON secreto, NO entran", rechaza(webhook, "wh_s1", ORG_A, "https://s.example/1", None, None) and rechaza(webhook, "wh_s2", ORG_A, "https://s.example/2", "x", ahora_sql))
    rev("un estado inventado NO entra", rechaza("UPDATE nexus.webhooks SET status = 'dormido' WHERE publicId = 'wh_a1'"))
    k.execute("INSERT INTO nexus.webhooks (publicId, tenant, url, encryptedSecret, createdAt) VALUES ('wh_tmp', 'plataforma', 'https://t.example/', 'x', sysutcdatetime())")
    k.execute("INSERT INTO nexus.webhookSubscriptions (webhookId, eventType) SELECT webhookId, 'documento.completado' FROM nexus.webhooks WHERE publicId = 'wh_tmp'")
    rev("una suscripción a un evento que no existe NO entra", rechaza("INSERT INTO nexus.webhookSubscriptions (webhookId, eventType) SELECT webhookId, 'documento.perdido' FROM nexus.webhooks WHERE publicId = 'wh_tmp'"))
    k.execute("DELETE FROM nexus.webhooks WHERE publicId = 'wh_tmp'")
    rev("borrar un webhook se lleva sus suscripciones", k.execute("SELECT count(*) FROM nexus.webhookSubscriptions s LEFT JOIN nexus.webhooks w ON w.webhookId = s.webhookId WHERE w.webhookId IS NULL").fetchone()[0] == 0)
    entrega = "INSERT INTO nexus.webhookDeliveries (publicId, webhookId, tenant, type, bodyJson, createdAt, status, closedAt) SELECT ?, webhookId, 'plataforma', 't', ?, sysutcdatetime(), ?, ? FROM nexus.webhooks WHERE publicId = 'wh_a1'"
    rev("una entrega pendiente con fecha de cierre, o cerrada sin ella, NO entra", rechaza(entrega, "msg_c1", "{}", "pendiente", ahora_sql) and rechaza(entrega, "msg_c2", "{}", "entregada", None))
    rev("una entrega con un cuerpo que no es JSON NO entra", rechaza(entrega, "msg_c3", "no es json", "pendiente", None))
    rev("el mismo aviso (organización, entrada y tipo) dos veces NO entra", rechaza(
        "INSERT INTO nexus.webhookNotices (tenant, entryId, type, occurredAt, destinations) VALUES (?, ?, 'documento.completado', sysutcdatetime(), 0)", ORG_A, E1))
    con.close()

    titulo("6 · Hilos a la vez: lo que el candado del archivo daba a un solo proceso, la base lo da a todos")
    modo(True)
    entregas_webhooks._ahora = lambda: datetime.now(timezone.utc)
    entrega_webhooks.enviar = enviar_de_mentiras

    def en_paralelo(fn, n=8):
        with ThreadPoolExecutor(max_workers=n) as pool:
            return list(pool.map(lambda _: probar(fn), range(n)))

    token, _ = tokens.crear("u-hilos", tokens.RECUPERACION)
    r = en_paralelo(lambda: tokens.consumir(token, tokens.RECUPERACION))
    rev("un enlace consumido por 8 hilos a la vez: UNO gana y los demás lo ven ya usado", sum(1 for x in r if x[0] == "ok") == 1 and sum(1 for x in r if x[1:2] == ("YaUsado",)) == 7, str(r))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: tokens.crear("u-hilos2", tokens.ACTIVACION), range(8)))
    rev("8 enlaces nuevos a la vez para la misma persona: queda UNO solo vigente", len(tokens.pendientes_de("u-hilos2", tokens.ACTIVACION)) == 1)
    r = en_paralelo(lambda: webhooks_cliente.registrar(ORG_A, "https://hilos.example/", ["documento.completado"]))
    rev("la misma URL registrada por 8 hilos a la vez: UNA se registra y las demás son duplicado", sum(1 for x in r if x[0] == "ok") == 1 and sum(1 for x in r if x[1:2] == ("Duplicado",)) == 7, str(r))
    ids = [x[1][1]["id"] for x in en_paralelo(lambda: llaves_cliente.emitir(ORG_A, "n", "d", 7), 10) if x[0] == "ok"]
    rev("10 llaves emitidas a la vez: 10 ids distintos", len(ids) == 10 and len(set(ids)) == 10, str(ids))
    _, wh_hilos = webhooks_cliente.registrar(ORG_A, "https://hilos-aviso.example/", ["documento.completado"])
    webhooks_cliente.marcar_validado(ORG_A, wh_hilos["id"], 200, 1)
    entregas_webhooks._pendientes.clear()
    suscritos = len(webhooks_cliente.suscritos(ORG_A, "documento.completado"))
    r = en_paralelo(lambda: entregas_webhooks.avisar(ORG_A, "documento.completado", "e" * 31 + "9", None, None))
    nuevas = [x[1]["programadas"] for x in r if x[0] == "ok" and not x[1]["duplicado"]]
    rev("el mismo aviso recibido 8 veces a la vez: se programa UNA vez (una entrega por webhook suscrito, no ocho) y los otros 7 son duplicado",
        all(x[0] == "ok" for x in r) and nuevas == [suscritos] and suscritos >= 2, str(r))
    por_webhook = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"]).cursor().execute(
        "SELECT w.publicId, count(*) FROM nexus.webhookDeliveries d JOIN nexus.webhooks w ON w.webhookId = d.webhookId WHERE d.entryId = ? GROUP BY w.publicId", "e" * 31 + "9").fetchall()
    rev("…y no quedó más de una entrega por webhook para ese aviso", len(por_webhook) == suscritos and all(n == 1 for _, n in por_webhook), str(por_webhook))
    bandeja.entrada_en_pipeline = bandeja_real

    titulo("7 · La API de punta a punta con SQL: emitir una llave, subir con ella, ver su uso, revocarla; y un webhook que recibe su aviso")
    modo(True)
    entregas_webhooks._pendientes.clear()
    entregas_webhooks._cargado = True
    from fastapi.testclient import TestClient  # noqa: E402

    from app import app  # noqa: E402
    from servicios import auth, usuarios  # noqa: E402

    c = TestClient(app)
    PDF = b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"

    def cab(bearer=None, llave=None) -> dict:
        return {"x-api-key": llave or SERVICIO, **({"Authorization": f"Bearer {bearer}"} if bearer else {})}

    def entrar(correo, contrasena):
        r = c.post("/auth/login", json={"email": correo, "password": contrasena, "ip": "127.0.0.1", "userAgent": "verificar"}, headers=cab())
        assert r.status_code == 200, r.text
        return r.json()["accessToken"]

    temporal = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal))
    r = c.post("/auth/contrasena", json={"nueva": "Cobre-Lento-Azul-47!", "confirmacion": "Cobre-Lento-Azul-47!"}, headers=cab(entrar("super@ejemplo.com", temporal)))
    assert r.status_code == 200, r.text
    super_token = entrar("super@ejemplo.com", "Cobre-Lento-Azul-47!")
    r = c.post("/organizaciones/", json={"nombre": "Gamma", "adminNombre": "Admin", "adminApellidoPaterno": "Gamma", "adminEmail": "rita@ejemplo.com"}, headers=cab(super_token))
    assert r.status_code == 201, r.text
    org = r.json()["organizacion"]["guid"]
    c.post("/auth/contrasena", json={"nueva": "Rumbo-Sereno-Azul-47!", "confirmacion": "Rumbo-Sereno-Azul-47!"}, headers=cab(entrar("rita@ejemplo.com", r.json()["contrasenaTemporal"])))
    admin = entrar("rita@ejemplo.com", "Rumbo-Sereno-Azul-47!")

    r = c.post("/llaves/", json={"tenant": org, "nombre": "Integración", "descripcion": "e2e", "dias": 7}, headers=cab(admin))
    rev("POST /llaves: 201, con el secret UNA vez", r.status_code == 201 and r.json()["secret"].startswith("nxdoc_live_"), r.text)
    secreto, ident = r.json()["secret"], r.json()["llave"]["id"]
    r = c.post("/bandeja/", files={"archivo": ("a.pdf", PDF, "application/pdf")}, data={"tenant": org}, headers=cab(llave=secreto))
    rev("POST /bandeja con la llave de cliente: 201 (la verificó SQL)", r.status_code == 201, r.text)
    entrada = r.json()["id"]
    con = BASE["conectar"]("usrNexus", BASE["app"], BASE["base"])
    k = con.cursor()
    rev("la entrada y su documento quedaron con la llave (llave foránea real)", k.execute("SELECT count(*) FROM nexus.inboxEntries WHERE keyId = ?", ident).fetchone()[0] == 1 and k.execute("SELECT count(*) FROM nexus.documents WHERE keyId = ?", ident).fetchone()[0] == 1)
    rev("el uso quedó en la tabla", k.execute("SELECT count(*) FROM nexus.apiKeyUsage u JOIN nexus.apiKeys a ON a.keyId = u.keyId WHERE a.publicId = ? AND u.status = 201", ident).fetchone()[0] == 1)
    r = c.get(f"/llaves/{ident}/metricas", params={"tenant": org, "desde": (AHORA - timedelta(hours=1)).isoformat(), "hasta": (AHORA + timedelta(hours=1)).isoformat()}, headers=cab(admin))
    rev("GET /llaves/{id}/metricas: cuenta la solicitud", r.status_code == 200 and r.json()["actual"]["solicitudes"] == 1 and r.json()["limiteSemanal"]["consumo"] == 1, r.text)
    r = c.get("/llaves/", params={"tenant": org}, headers=cab(admin))
    rev("GET /llaves: la lista trae su último uso", r.status_code == 200 and [l["ultimoUso"] is not None for l in r.json()["llaves"]] == [True], r.text)
    rev("otra organización no ve la llave ni sus métricas (404)", c.get(f"/llaves/{ident}/metricas", params={"tenant": ORG_A, "desde": hace(60), "hasta": luego(1)}, headers=cab(admin)).status_code in (403, 404))
    r = c.post(f"/llaves/{ident}/revocar", json={"tenant": org}, headers=cab(admin))
    rev("POST /llaves/{id}/revocar: 200", r.status_code == 200 and r.json()["revocada"] is True, r.text)
    r = c.post("/bandeja/", files={"archivo": ("b.pdf", PDF + b"2", "application/pdf")}, data={"tenant": org}, headers=cab(llave=secreto))
    rev("con la llave revocada: 401, y esa llamada también cuenta en su uso", r.status_code == 401 and k.execute("SELECT count(*) FROM nexus.apiKeyUsage u JOIN nexus.apiKeys a ON a.keyId = u.keyId WHERE a.publicId = ? AND u.status = 401", ident).fetchone()[0] == 1, r.text)

    r = c.post("/webhooks/", json={"tenant": org, "url": "https://cliente.example/hook", "eventos": ["documento.completado"]}, headers=cab(admin))
    rev("POST /webhooks: 201, con su secret una vez", r.status_code == 201 and r.json()["secret"].startswith("whsec_"), r.text)
    wid = r.json()["webhook"]["id"]
    r = c.post("/webhooks/", json={"tenant": org, "url": "https://cliente.example/hook", "eventos": ["documento.completado"]}, headers=cab(admin))
    rev("la misma URL otra vez: 409", r.status_code == 409, r.text)
    entrega_webhooks.enviar = lambda url, secret, cuerpo, id_mensaje: {"codigo": 200, "ms": 6}
    r = c.post(f"/webhooks/{wid}/validar", json={"tenant": org}, headers=cab(admin))
    rev("POST /webhooks/{id}/validar: validado", r.status_code == 200 and r.json()["validado"] is True and r.json()["webhook"]["validadoEn"], r.text)
    rev("la entrada de la bandeja todavía NO pasó al pipeline: el aviso se rechaza (404)",
        c.post("/webhooks/eventos", json={"tenant": org, "tipo": "documento.completado", "entradaId": entrada, "tipoDocumental": "INE"}, headers=cab(admin)).status_code == 404)
    rev("pasa al pipeline (se retira de la bandeja)", bandeja.retirar(org, entrada, "pipeline"))
    r = c.post("/webhooks/eventos", json={"tenant": org, "tipo": "documento.completado", "entradaId": entrada, "tipoDocumental": "INE"}, headers=cab(admin))
    rev("POST /webhooks/eventos: acepta y programa UNA entrega", r.status_code == 200 and r.json() == {"programadas": 1, "duplicado": False}, r.text)
    entregas_webhooks.procesar_vencidas()
    r = c.get(f"/webhooks/{wid}/intentos", params={"tenant": org}, headers=cab(admin))
    lista = r.json().get("intentos", []) if r.status_code == 200 else []
    rev("el historial trae la validación y la entrega (con la entrada que avisaba)", r.status_code == 200 and len(lista) == 2 and lista[0]["tipo"] == "documento.completado" and lista[0]["ok"] and lista[0]["entradaId"] == entrada and lista[1]["tipo"] == "webhook.validacion", r.text[:300])
    r = c.get(f"/webhooks/{wid}/metricas", params={"tenant": org, "desde": (AHORA - timedelta(hours=1)).isoformat(), "hasta": (AHORA + timedelta(hours=1)).isoformat()}, headers=cab(admin))
    rev("las métricas cuentan la entrega (la validación no)", r.status_code == 200 and r.json()["actual"]["solicitudes"] == 1 and r.json()["actual"]["errores"] == 0, r.text)
    r = c.post(f"/webhooks/{wid}/eliminar", json={"tenant": org}, headers=cab(admin))
    rev("POST /webhooks/{id}/eliminar: 200, y el secreto cifrado se borró de la fila", r.status_code == 200 and k.execute("SELECT encryptedSecret FROM nexus.webhooks WHERE publicId = ?", wid).fetchone()[0] is None, r.text)
    rev("el listado ya no lo trae", c.get("/webhooks/", params={"tenant": org}, headers=cab(admin)).json()["webhooks"] == [])
    con.close()
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print("\nTodo bien." if not fallos else f"\nFALLARON {len(fallos)}:\n  - " + "\n  - ".join(fallos))
raise SystemExit(1 if fallos else 0)
