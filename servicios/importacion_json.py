"""Importar UNA vez, a SQL Server, lo que un registro JSON del almacén ya traía (migración `0006`).

Las tablas de la `0006` (organizaciones, clasificadores, bandeja y auditoría) no pueden arrancar vacías como las de antes: sin las
organizaciones, ninguna cuenta tendría a qué pertenecer; sin los clasificadores, cada organización buscaría otra vez su procesador en
Google; sin la bandeja, lo pendiente desaparecería; sin la auditoría, se perdería el rastro y los límites de la recuperación de contraseña
contarían desde cero. Así que cada registro se importa la PRIMERA vez que se usa en cada ambiente, sin que nadie tenga que correr nada en el
servidor, y queda anotado en `nexus.jsonImports`: nunca se importa dos veces, aunque el JSONL siga creciendo (los ambientes que todavía no
migran siguen escribiendo ahí; lo de después ya no es de esta base).

Va dentro de una transacción con un candado de aplicación (`sp_getapplock`): dos procesos que arrancan a la vez no importan dos veces. Si el
almacén no se puede leer (el NAS caído), falla sin anotar nada y se reintenta en la siguiente llamada. Lo que no se puede importar (un evento
roto, un slug repetido) se salta y se cuenta en `rowsSkipped`, con aviso en el log: una línea mala no deja a todo el producto sin datos.

El orden importa: las organizaciones van primero, porque la bandeja y los clasificadores las referencian con llave foránea.

Desde la `0014` (2026-11-11) se importa también el último registro, `usuarios.jsonl`: cuentas (también las retiradas), membresías, sesiones,
intentos de inicio de sesión, el contador de cada navegador y lo que un administrador le hizo a cada cuenta. Va después de las organizaciones
(las membresías las referencian) y ANTES de los enlaces de un solo uso, que desde la 0014 tienen llave foránea a la cuenta.
"""

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone

from servicios import json_heredado as heredado

logger = logging.getLogger(__name__)

#: Qué otro registro tiene que estar importado antes (llaves foráneas): las organizaciones van primero, y lo que cuelga de una llave o de un
#: webhook, después de ella.
_DEPENDE_DE = {
    "bandeja": "organizaciones",
    "clasificadores": "organizaciones",
    "llaves": "organizaciones",
    "webhooks": "organizaciones",
    "uso-llaves": "llaves",
    "entregas-webhooks": "webhooks",
    "usuarios": "organizaciones",
    "tokens": "usuarios",
}
_RE_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RE_ID = re.compile(r"^[A-Za-z0-9_]+$")
_RE_GUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

#: Los que ya se sabe que están importados en esta base, para no preguntarle a SQL en cada llamada.
_hechos: set[str] = set()
_candado = threading.Lock()


def _fecha(valor) -> datetime:
    """Una fecha del JSONL (`2026-10-29T18:04:05+00:00` o con `Z`) como datetime UTC sin zona, que es como guarda la base. Una ilegible
    cae en «ahora»: mejor un renglón con fecha aproximada que perderlo."""
    try:
        d = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        return (d.astimezone(timezone.utc) if d.tzinfo else d).replace(tzinfo=None, microsecond=0)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).replace(tzinfo=None, microsecond=0)


def asegurar(nombre: str) -> None:
    """Importa el registro `nombre` si todavía no se importó en esta base. Barato después de la primera vez (queda en memoria)."""
    if nombre in _hechos:
        return
    if nombre in _DEPENDE_DE:
        asegurar(_DEPENDE_DE[nombre])
    from db import tablas

    with _candado:
        if nombre in _hechos:
            return
        with tablas.transaccion() as cur:
            cur.execute("DECLARE @r int; EXEC @r = sp_getapplock @Resource = ?, @LockMode = 'Exclusive', @LockOwner = 'Transaction', @LockTimeout = 60000; SELECT @r AS r", f"nexus-import-{nombre}")
            if tablas.una(cur)["r"] < 0:
                raise RuntimeError(f"No se pudo tomar el candado para importar {nombre}.")
            cur.execute("SELECT rowsImported FROM nexus.jsonImports WHERE registryName = ?", nombre)
            if tablas.una(cur) is None:
                importados, saltados = _IMPORTADORES[nombre](cur)
                cur.execute("INSERT INTO nexus.jsonImports (registryName, rowsImported, rowsSkipped) VALUES (?, ?, ?)", nombre, importados, saltados)
                logger.info("Importado %s desde el almacén: %s renglones, %s saltados.", nombre, importados, saltados)
        _hechos.add(nombre)


def asegurar_todo() -> None:
    """Todos, en orden. Lo llama el arranque de la app (si falla, no la tumba: se reintenta al usar cada uno)."""
    for nombre in _IMPORTADORES:
        asegurar(nombre)


def olvidar() -> None:
    """Para las pruebas: que la siguiente llamada vuelva a preguntarle a la base."""
    _hechos.clear()


# ── Cada registro ───────────────────────────────────────────────────────────────────────────────────────────────────────


def _organizaciones(cur) -> tuple[int, int]:
    from servicios import tenants_registro

    vistas = list(heredado.tenants().values())
    guids, slugs, codigos = set(), set(), set()
    filas, saltados = [], 0
    # Una organización NUNCA se pierde por un slug o un código repetido (tres ambientes escribiendo el mismo JSONL pudieron empatar): sus
    # cuentas se quedarían sin organización. Se le da un slug libre o el código siguiente, con aviso. Solo se salta un guid repetido o vacío.
    numeros = [int(t["codigo"].split("-", 1)[1]) for t in vistas if str(t.get("codigo", "")).startswith(f"{tenants_registro.PREFIJO_CODIGO}-") and t["codigo"].split("-", 1)[1].isdigit()]
    siguiente = max(numeros, default=0) + 1
    for t in vistas:
        guid = str(t["guid"]).strip().lower()
        if guid in guids or len(guid) != 36 or not str(t.get("nombre", "")).strip():
            logger.warning("Organización %s no se importa: repetida o incompleta.", guid)
            saltados += 1
            continue
        slug = t["slug"] or tenants_registro.slug_de(t["nombre"]) or "organizacion"
        if slug in slugs:
            nuevo = tenants_registro._slug_libre(slug, slugs)
            logger.warning("Organización %s: su slug %s ya lo tenía otra; se importa como %s.", guid, slug, nuevo)
            slug = nuevo
        codigo = t["codigo"]
        if not codigo or codigo in codigos:
            codigo = f"{tenants_registro.PREFIJO_CODIGO}-{siguiente:05d}"
            siguiente += 1
            logger.warning("Organización %s: su código ya lo tenía otra; se importa como %s.", guid, codigo)
        guids.add(guid)
        slugs.add(slug)
        codigos.add(codigo)
        filas.append((guid, t["nombre"], slug, codigo, t["estado"], t.get("adminGuid"), json.dumps(t.get("recuperacion") or {}, ensure_ascii=False), _fecha(t.get("creadaEn"))))
    if filas:
        cur.executemany(
            "INSERT INTO nexus.tenants (guid, name, slug, code, status, adminGuid, recoveryJson, createdAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", filas
        )
    return len(filas), saltados


def _clasificadores(cur) -> tuple[int, int]:
    cur.execute("SELECT guid FROM nexus.tenants")
    existentes = {f[0] for f in cur.fetchall()}
    filas, saltados = [], 0
    for tenant, procesador in heredado.clasificadores().items():
        if tenant not in existentes:
            saltados += 1
            continue
        filas.append((tenant, procesador))
    if filas:
        cur.executemany("INSERT INTO nexus.tenantClassifiers (tenant, processorId) VALUES (?, ?)", filas)
    return len(filas), saltados


def _bandeja(cur) -> tuple[int, int]:
    cur.execute("SELECT guid FROM nexus.tenants")
    existentes = {f[0] for f in cur.fetchall()}
    filas, saltados, vistos = [], 0, set()
    for archivo in sorted(heredado.ruta("bandeja-x.jsonl").parent.glob("bandeja-*.jsonl")):
        eventos = heredado.eventos(archivo.name)
        retiros = {e.get("id"): e for e in eventos if e.get("evento") == "retiro"}
        for e in eventos:
            if e.get("evento") != "entrada":
                continue
            ident, tenant = e.get("id"), e.get("tenant")
            organizacion = tenant if isinstance(tenant, str) and len(tenant) == 36 and tenant.count("-") == 4 else None
            if (
                not isinstance(ident, str) or len(ident) != 32 or ident in vistos or not isinstance(tenant, str)
                or e.get("canal") not in ("API", "MANUAL") or (organizacion and organizacion not in existentes)
            ):
                saltados += 1
                continue
            vistos.add(ident)
            r = retiros.get(ident)
            motivo = r.get("motivo") if r and r.get("motivo") in ("pipeline", "descartado") else None
            filas.append((
                ident, tenant, e.get("documentoId"), e.get("rutaRelativa") or "", e.get("sha256") or "0" * 64, int(e.get("tamanoBytes") or 0),
                e.get("mime") or "", e.get("nombreOriginal") or "", e["canal"], e.get("llaveId"), _fecha(e.get("recibidoEn")),
                _fecha(r.get("en")) if motivo else None, motivo, (r.get("detalle") if motivo else None),
            ))
    if filas:
        cur.executemany(
            "INSERT INTO nexus.inboxEntries (publicId, tenant, documentPublicId, relativePath, sha256, sizeBytes, mime, originalName, channel, keyId, "
            "receivedAt, removedAt, removalReason, removalDetail) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            filas,
        )
    return len(filas), saltados


def _auditoria(cur) -> tuple[int, int]:
    filas, saltados = [], 0
    for e in heredado.eventos(heredado.AUDITORIA):
        accion = e.get("accion")
        if not isinstance(accion, str) or not accion:
            saltados += 1
            continue
        tenant = e.get("tenant") if isinstance(e.get("tenant"), str) else None
        filas.append((accion[:60], e.get("usuarioGuid") if isinstance(e.get("usuarioGuid"), str) else None, tenant[:63] if tenant else None, _fecha(e.get("en")), json.dumps(e, ensure_ascii=False)))
    if filas:
        cur.executemany("INSERT INTO nexus.auditEvents (action, userGuid, tenant, occurredAt, dataJson) VALUES (?, ?, ?, ?, ?)", filas)
    return len(filas), saltados


# ── Migración 0008: enlaces, llaves, uso, webhooks y entregas ──────────────────────────────────────────────────────────


def _utc(momento: datetime, milisegundos: bool = False) -> datetime:
    utc = momento.astimezone(timezone.utc).replace(tzinfo=None)
    return utc.replace(microsecond=(utc.microsecond // 1000) * 1000) if milisegundos else utc.replace(microsecond=0)


def _organizaciones_existentes(cur) -> set[str]:
    cur.execute("SELECT guid FROM nexus.tenants")
    return {f[0] for f in cur.fetchall()}


def _tenant_importable(tenant, existentes: set[str]) -> bool:
    """Un tenant que se puede importar: texto sin espacios de hasta 63 caracteres y, si tiene forma de organización (guid), que exista."""
    if not isinstance(tenant, str) or not tenant or len(tenant) > 63 or " " in tenant:
        return False
    return not (len(tenant) == 36 and tenant.count("-") == 4) or tenant in existentes


def _insertar_lote(cur, sql: str, filas: list) -> None:
    """Un lote grande va con `fast_executemany` (cientos de miles de líneas de uso); uno chico, normal."""
    if not filas:
        return
    if len(filas) > 1000:
        cur.fast_executemany = True
        try:
            for i in range(0, len(filas), 20000):
                cur.executemany(sql, filas[i : i + 20000])
        finally:
            cur.fast_executemany = False
    else:
        cur.executemany(sql, filas)


def _entero(valor) -> int | None:
    return int(valor) if isinstance(valor, (int, float)) and not isinstance(valor, bool) else None


def _tokens(cur) -> tuple[int, int]:
    from servicios import tokens

    # Desde la 0014 cada enlace apunta (llave foránea) a una cuenta que exista en `nexus.users`, que se importa antes.
    cur.execute("SELECT guid FROM nexus.users")
    cuentas = {f[0] for f in cur.fetchall()}
    filas, saltados, vistos, guids = [], 0, set(), set()
    for t in heredado.tokens().values():
        clave, usuario = t.get("hash"), t.get("usuarioGuid")
        if (
            not isinstance(clave, str) or not _RE_HEX64.match(clave) or clave in vistos or t.get("tipo") not in tokens.TIPOS
            or not isinstance(usuario, str) or usuario not in cuentas or heredado.fecha(t.get("expira")) is None
        ):
            saltados += 1
            continue
        guid = t["guid"] if isinstance(t.get("guid"), str) and len(t["guid"]) == 36 and t["guid"] not in guids else str(uuid.uuid4())
        vistos.add(clave)
        guids.add(guid)
        filas.append((
            guid, clave, usuario, t["tipo"], _fecha(t["expira"]), _fecha(t.get("creadoEn")),
            _fecha(t["usadoEn"]) if t.get("usadoEn") else None, _fecha(t["invalidadoEn"]) if t.get("invalidadoEn") else None,
            str(t["motivo"])[:100] if t.get("motivo") else None,
        ))
    _insertar_lote(
        cur,
        "INSERT INTO nexus.accessTokens (guid, tokenHash, userGuid, kind, expiresAt, createdAt, usedAt, invalidatedAt, invalidationReason) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        filas,
    )
    return len(filas), saltados


def _llaves(cur) -> tuple[int, int]:
    existentes = _organizaciones_existentes(cur)
    emitidas, revocadas = heredado.llaves()
    filas, saltados = [], 0
    for ident, e in emitidas.items():
        tenant, secreto = e.get("tenant"), e.get("secret_hash")
        if (
            not _RE_ID.match(ident) or "_" in ident or len(ident) > 100 or not _tenant_importable(tenant, existentes)
            or not isinstance(secreto, str) or not _RE_HEX64.match(secreto)
        ):
            logger.warning("Llave %s no se importa: incompleta, de una organización que no existe o con un identificador raro.", ident)
            saltados += 1
            continue
        creada = _fecha(e.get("creadaEn"))
        # Una expiración ilegible se importa como YA VENCIDA: ante la duda, la llave no pasa (igual que `verificar`).
        expira = _fecha(e["expiraEn"]) if heredado.fecha(e.get("expiraEn")) else creada
        revocada = _fecha(revocadas[ident]) if ident in revocadas else None
        filas.append((ident, tenant, secreto, str(e.get("nombre") or "")[:100], str(e.get("descripcion") or "")[:500], creada, expira, revocada))
    _insertar_lote(
        cur,
        "INSERT INTO nexus.apiKeys (publicId, tenant, secretHash, name, description, createdAt, expiresAt, revokedAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        filas,
    )
    return len(filas), saltados


def _uso_llaves(cur) -> tuple[int, int]:
    cur.execute("SELECT publicId, keyId FROM nexus.apiKeys")
    llaves = {f[0]: f[1] for f in cur.fetchall()}
    filas, saltados = [], 0
    for e in heredado.eventos(heredado.USO_LLAVES):
        llave, cuando = e.get("llave"), heredado.fecha_de_uso(e)
        try:
            status, ms, bytes_ = int(e.get("status", 0)), int(e.get("ms", 0)), int(e.get("bytes", 0))
        except (TypeError, ValueError):
            saltados += 1
            continue
        if not isinstance(llave, str) or llave not in llaves or cuando is None:
            saltados += 1
            continue
        filas.append((llaves[llave], _utc(cuando, milisegundos=True), str(e.get("ruta") or "")[:400], str(e.get("metodo") or "")[:10], status, ms, bytes_))
    _insertar_lote(
        cur, "INSERT INTO nexus.apiKeyUsage (keyId, occurredAt, route, method, status, durationMs, bytes) VALUES (?, ?, ?, ?, ?, ?, ?)", filas
    )
    return len(filas), saltados


def _webhooks(cur) -> tuple[int, int]:
    from servicios import webhooks_cliente

    existentes = _organizaciones_existentes(cur)
    registrados, estados, eliminados, validados, fallidas = heredado.webhooks()
    eliminado_en = {e.get("id"): e.get("en") for e in heredado.eventos(heredado.WEBHOOKS) if e.get("evento") == "eliminado"}
    importados, saltados, urls = 0, 0, set()
    for ident, w in registrados.items():
        tenant, url, cifrado = w.get("tenant"), w.get("url"), w.get("secret_cifrado")
        borrado = ident in eliminados
        if (
            not _RE_ID.match(ident) or len(ident) > 40 or not _tenant_importable(tenant, existentes)
            or not isinstance(url, str) or not url or len(url) > 2048
            or (not borrado and (not isinstance(cifrado, str) or not cifrado or len(cifrado) > 600))
        ):
            logger.warning("Webhook %s no se importa: incompleto o de una organización que no existe.", ident)
            saltados += 1
            continue
        if not borrado:
            # Tres ambientes escribiendo el mismo archivo pudieron registrar la misma URL dos veces: la base solo deja una vigente.
            if (tenant.lower(), url) in urls:
                logger.warning("Webhook %s no se importa: su organización ya tiene otro vigente con esa URL.", ident)
                saltados += 1
                continue
            urls.add((tenant.lower(), url))
        cur.execute(
            "INSERT INTO nexus.webhooks (publicId, tenant, url, encryptedSecret, status, createdAt, validatedAt, validationFailedAt, deletedAt) "
            "OUTPUT INSERTED.webhookId VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ident, tenant, url, None if borrado else cifrado, estados.get(ident, "activo"), _fecha(w.get("creadoEn")),
            _fecha(validados[ident]) if ident in validados else None,
            _fecha(fallidas[ident]["en"]) if ident in fallidas else None,
            _fecha(eliminado_en.get(ident)) if borrado else None,
        )
        nuevo = cur.fetchone()[0]
        eventos = [e for e in webhooks_cliente.EVENTOS if e in (w.get("eventos") or [])]
        _insertar_lote(cur, "INSERT INTO nexus.webhookSubscriptions (webhookId, eventType) VALUES (?, ?)", [(nuevo, e) for e in eventos])
        importados += 1
    return importados, saltados


def _entregas_webhooks(cur) -> tuple[int, int]:
    """Avisos, entregas e intentos. Lo que seguía PENDIENTE de reintento se importa CANCELADO: los tres ambientes compartían el archivo, y si cada
    base se quedara con las pendientes, cada trabajador las reenviaría. Una entrega que ya había respondido 2xx, o que ya se había agotado o
    cancelado, conserva su estado."""
    from servicios import entregas_webhooks as ent

    cur.execute("SELECT publicId, webhookId, tenant FROM nexus.webhooks")
    webhooks = {f[0]: (f[1], f[2]) for f in cur.fetchall()}
    ahora = _fecha(None)
    avisos, programadas, intentos, cerradas, en_orden = [], {}, {}, {}, []
    for e in heredado.eventos(heredado.ENTREGAS_WEBHOOKS):
        tipo, ident = e.get("evento"), e.get("id")
        if tipo == "aviso":
            avisos.append(e)
        elif tipo == "programada" and isinstance(ident, str):
            programadas.setdefault(ident, e)
        elif tipo == "intento" and isinstance(ident, str):
            intentos.setdefault(ident, []).append(e)
            en_orden.append(e)
        elif tipo in ("agotada", "cancelada") and isinstance(ident, str):
            cerradas.setdefault(ident, e)

    importados = saltados = 0
    vistos, filas = set(), []
    for e in avisos:
        tenant, entrada, tipo = e.get("tenant"), e.get("entradaId"), e.get("tipo")
        if not (isinstance(tenant, str) and tenant and len(tenant) <= 63 and isinstance(entrada, str) and entrada and len(entrada) <= 64 and isinstance(tipo, str) and tipo and len(tipo) <= 40):
            saltados += 1
            continue
        if (tenant.lower(), entrada.lower(), tipo) in vistos:
            saltados += 1
            continue
        vistos.add((tenant.lower(), entrada.lower(), tipo))
        filas.append((tenant, entrada, tipo, _fecha(e.get("en")), max(0, _entero(e.get("destinos")) or 0)))
    _insertar_lote(cur, "INSERT INTO nexus.webhookNotices (tenant, entryId, type, occurredAt, destinations) VALUES (?, ?, ?, ?, ?)", filas)
    importados += len(filas)

    filas = []
    for ident, e in programadas.items():
        destino, tenant, cuerpo = webhooks.get(e.get("webhook")), e.get("tenant"), e.get("cuerpo")
        try:
            datos = json.loads(cuerpo).get("data", {}) if isinstance(cuerpo, str) else None
        except (ValueError, AttributeError):
            datos = None
        if (
            not _RE_ID.match(ident) or len(ident) > 60 or destino is None or not isinstance(tenant, str) or not tenant or len(tenant) > 63
            or datos is None or not isinstance(e.get("tipo"), str) or len(e["tipo"]) > 40
        ):
            saltados += 1
            continue
        hechos = intentos.get(ident, [])
        cerrada = cerradas.get(ident)
        exitosos = [x for x in hechos if x.get("ok")]
        if cerrada:
            estado, cierre, motivo = cerrada["evento"], _fecha(cerrada.get("en")), cerrada.get("motivo")
        elif exitosos:
            estado, cierre, motivo = "entregada", _fecha(exitosos[0].get("en")), None
        elif len(hechos) >= len(ent.CALENDARIO_S):
            estado, cierre, motivo = "agotada", _fecha(hechos[-1].get("en")), "Se agotó el calendario de reintentos."
        else:
            estado, cierre, motivo = "cancelada", ahora, "Estaba pendiente al migrar a SQL: no se reenvía (otro ambiente pudo haberla entregado)."
        ultimo = max((_fecha(x.get("en")) for x in hechos), default=None)
        entrada = datos.get("entradaId") if isinstance(datos, dict) else None
        filas.append((
            ident, destino[0], tenant, e["tipo"], entrada[:64] if isinstance(entrada, str) else None, cuerpo, _fecha(e.get("en")),
            estado, len(hechos), ultimo, cierre, str(motivo)[:1000] if motivo else None,
        ))
    _insertar_lote(
        cur,
        "INSERT INTO nexus.webhookDeliveries (publicId, webhookId, tenant, type, entryId, bodyJson, createdAt, status, attempts, lastAttemptAt, closedAt, closeReason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        filas,
    )
    importados += len(filas)

    # En el orden en que se escribieron: el historial se ordena por él.
    filas = []
    for x in en_orden:
        ident, destino = x["id"], webhooks.get(x.get("webhook"))
        tenant = x.get("tenant") if isinstance(x.get("tenant"), str) and x.get("tenant") else (destino[1] if destino else None)
        if destino is None or not tenant or len(tenant) > 63 or not _RE_ID.match(ident) or len(ident) > 60:
            saltados += 1
            continue
        filas.append((
            destino[0], tenant, ident, str(x.get("tipo") or "")[:40], _entero(x.get("n")) or 0, _fecha(x.get("en")), 1 if x.get("ok") else 0,
            _entero(x.get("codigo")), _entero(x.get("ms")), str(x["motivo"])[:1000] if x.get("motivo") else None,
        ))
    _insertar_lote(
        cur,
        "INSERT INTO nexus.webhookAttempts (webhookId, tenant, messageId, type, attemptNumber, occurredAt, ok, httpCode, durationMs, reason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        filas,
    )
    importados += len(filas)
    return importados, saltados


# ── Migración 0014: usuarios ────────────────────────────────────────────────────────────────────────────────────────────


def _fecha_legible(valor) -> datetime | None:
    """Como `_fecha`, pero `None` si no se puede leer (en vez de «ahora»): para decidir, no para rellenar."""
    try:
        d = datetime.fromisoformat(str(valor).replace("Z", "+00:00")) if isinstance(valor, str) and valor else None
    except ValueError:
        return None
    return (d.astimezone(timezone.utc) if d.tzinfo else d).replace(tzinfo=None, microsecond=0) if d else None


def _texto(valor, largo: int) -> str | None:
    """Texto recortado a `largo` caracteres; lo que no es texto, o queda vacío, es `None`. Las columnas nvarchar de la 0014 tienen el doble
    (se miden en unidades de UTF-16, y un emoji ocupa dos), así que lo recortado siempre cabe."""
    return valor[:largo] or None if isinstance(valor, str) else None


def _cabe(texto: str, unidades: int) -> bool:
    """¿Cabe en un nvarchar de `unidades`? (Se mide en UTF-16: un emoji ocupa dos.)"""
    return len(texto.encode("utf-16-le")) // 2 <= unidades


def _forma_de_organizacion(tenant: str) -> bool:
    """La misma prueba que la columna calculada `organizationGuid` (`LIKE '________-____-____-____-____________'`): si la cumple, la base exige
    que la organización exista."""
    return len(tenant) == 36 and tenant[8] == tenant[13] == tenant[18] == tenant[23] == "-"


def _usuarios(cur) -> tuple[int, int]:
    """Cuentas, membresías, sesiones, intentos de inicio de sesión, el contador de cada navegador y lo que un administrador le hizo a cada
    cuenta (migración 0014).

    Las cuentas salen del MISMO repaso que usa el JSONL (`json_heredado.usuarios_indice`), así que entran como el producto las veía, con su
    contraseña, sus intentos fallidos, su bloqueo, si están activas y su orden. Las retiradas también entran (con `deletedAt`), porque su rastro
    las sigue apuntando. Las sesiones entran todas, abiertas y cerradas: nadie tiene que volver a iniciar sesión por la mudanza.

    Lo que no se puede importar se salta y se cuenta, y NUNCA tumba la importación entera (sin ella nadie podría entrar): una cuenta con un guid o
    un correo que no sirven (y lo que cuelga de ella), una membresía de una organización que no existe, una sesión sin un refresh legible. Si dos
    cuentas vigentes comparten correo (tres ambientes escribían el mismo archivo), la primera se queda con él y la otra entra RETIRADA, con su
    motivo: en el JSONL tampoco podía entrar, porque el correo apuntaba a la primera."""
    import pyodbc

    import config
    from servicios import roles, usuarios

    eventos = heredado.eventos(heredado.USUARIOS)
    retirados: dict[str, dict] = {}
    vivos, _, sesiones = heredado.usuarios_indice(eventos, retirados=retirados)
    importados = saltados = 0
    ahora = _fecha(None)

    # El orden de alta (para que `userId` lo siga, como el orden de los listados) y cuándo se asignó cada membresía.
    orden: dict[str, int] = {}
    asignada: dict[tuple, str] = {}
    for i, e in enumerate(eventos):
        guid = e.get("guid")
        if isinstance(guid, str) and e.get("evento") == "creado":
            orden.setdefault(guid, i)
        elif isinstance(guid, str) and e.get("evento") == "membresia" and isinstance(e.get("tenantGuid"), str):
            asignada[(guid, e["tenantGuid"])] = e.get("en")

    # La recuperación que un administrador de antes del 2026-10-14 heredaba de su organización (lo hacía el viejo índice de `usuarios.py` al leerlo) entra ya
    # resuelta: la de una organización no se edita después de crearla, así que leerla una vez o en cada lectura da lo mismo.
    cur.execute("SELECT adminGuid, recoveryJson FROM nexus.tenants WHERE adminGuid IS NOT NULL ORDER BY tenantId")
    de_su_organizacion: dict[str, dict] = {}
    for admin, texto in cur.fetchall():
        try:
            recuperacion = json.loads(texto or "{}")
        except ValueError:
            continue
        if isinstance(recuperacion, dict) and recuperacion:
            de_su_organizacion[admin] = recuperacion
    for u in vivos.values():
        if not u["recuperacion"] and not u.get("recuperacionPropia") and any(m.get("rol") == roles.ADMIN for m in u["membresias"]):
            u["recuperacion"] = dict(de_su_organizacion.get(u["guid"], {}))

    # ── Cuentas, en orden de alta. Una por una: un correo que la base considere repetido no puede tumbar a las demás.
    cuentas = [(u, False) for u in vivos.values()] + [(u, True) for g, u in retirados.items() if g not in vivos]
    cuentas.sort(key=lambda par: orden.get(par[0]["guid"], len(eventos)))
    insertar_cuenta = (
        "INSERT INTO nexus.users (guid, email, firstName, lastName, secondLastName, phone, recoveryJson, isPlatformAdmin, passwordHash, "
        "mustChangePassword, passwordChangedAt, temporaryIssuedAt, failedAttempts, lockedUntil, lastLoginAt, isActive, createdAt, deletedAt) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
    )
    importadas: set[str] = set()
    correos: set[str] = set()
    retiradas_al_importar: list[str] = []
    for u, retirada in cuentas:
        guid, email = u.get("guid"), u.get("email")
        if not isinstance(guid, str) or not _RE_GUID.match(guid) or not isinstance(email, str) or not email or " " in email or not _cabe(email, 254):
            logger.warning("La cuenta %s no se importa: su guid o su correo no sirven.", guid)
            saltados += 1
            continue
        borrada = (_fecha_legible(u.get("eliminadoEn")) or ahora) if retirada else None
        if borrada is None and email.lower() in correos:
            logger.warning("La cuenta %s entra retirada: otra cuenta vigente ya tenía su correo.", guid)
            borrada = ahora
            retiradas_al_importar.append(guid)
        recuperacion = u.get("recuperacion") if isinstance(u.get("recuperacion"), dict) else {}
        telefono = u.get("telefono")[:25] if isinstance(u.get("telefono"), str) else None  # un "" se queda "", como en el JSONL
        fila = [
            guid, email, str(u.get("nombre") or "")[:200], str(u.get("apellidoPaterno") or "")[:200], str(u.get("apellidoMaterno") or "")[:200],
            telefono, json.dumps(recuperacion, ensure_ascii=False), bool(u.get("esAdminPlataforma")),
            u["hash"] if isinstance(u.get("hash"), str) and len(u["hash"]) <= 500 else None, bool(u.get("debeCambiar")),
            _fecha_legible(u.get("contrasenaCambiadaEn")), _fecha_legible(u.get("temporalEmitidaEn")),
            max(0, int(u.get("fallidos") or 0)), _fecha_legible(u.get("bloqueadoHasta")), _fecha_legible(u.get("ultimoLoginEn")),
            bool(u.get("activo", True)), _fecha(u.get("creadoEn")), borrada,
        ]
        try:
            cur.execute(insertar_cuenta, *fila)
        except pyodbc.Error as exc:
            # Un error de UNA fila (el correo repetido, o algo que no se previó) no deshace la transacción: se decide y se sigue.
            if borrada is not None or not isinstance(exc, pyodbc.IntegrityError) or "UX_users_email" not in str(exc):
                logger.warning("La cuenta %s no se importa: la base no la aceptó (%s).", guid, exc)
                saltados += 1
                continue
            # La base considera el correo igual al de otra cuenta vigente (otra forma de escribirlo): entra retirada.
            logger.warning("La cuenta %s entra retirada: la base considera su correo igual al de otra cuenta vigente.", guid)
            fila[-1] = ahora
            retiradas_al_importar.append(guid)
            cur.execute(insertar_cuenta, *fila)
        if fila[-1] is None:
            correos.add(email.lower())
        importadas.add(guid)
        importados += 1

    # ── Membresías, en el orden de cada cuenta (la primera es la que va en el JWT).
    existentes = _organizaciones_existentes(cur)
    filas = []
    for u, _ in cuentas:
        if u.get("guid") not in importadas:
            continue
        vistas: set[str] = set()
        for m in u.get("membresias") or []:
            tenant, rol = m.get("tenantGuid"), m.get("rol")
            if (
                not isinstance(tenant, str) or not tenant or len(tenant) > 63 or " " in tenant or not tenant.isascii() or tenant.lower() in vistas
                or (_forma_de_organizacion(tenant) and tenant not in existentes)
                or not isinstance(rol, str) or not rol.strip() or len(rol) > 40 or not rol.isascii()
            ):
                logger.warning("Una membresía de la cuenta %s no se importa: su organización no existe o le falta el rol.", u["guid"])
                saltados += 1
                continue
            vistas.add(tenant.lower())
            filas.append((u["guid"], tenant, rol, _fecha(asignada.get((u["guid"], tenant)) or u.get("creadoEn"))))
    _insertar_lote(cur, "INSERT INTO nexus.userMemberships (userGuid, tenant, role, assignedAt) VALUES (?, ?, ?, ?)", filas)
    importados += len(filas)

    # ── Sesiones, abiertas y cerradas. Una sin vencimiento legible entra ya vencida (el JSONL tampoco la dejaba renovar).
    filas, guids_sesion, hashes = [], set(), set()
    for s in sesiones.values():
        dueno, sesion, clave = s.get("guid"), s.get("sesionGuid"), s.get("refreshHash")
        if (
            dueno not in importadas or not isinstance(sesion, str) or not sesion or " " in sesion or len(sesion) > 63 or not sesion.isascii()
            or sesion.lower() in guids_sesion or not isinstance(clave, str) or not _RE_HEX64.match(clave) or clave in hashes
        ):
            saltados += 1
            continue
        guids_sesion.add(sesion.lower())
        hashes.add(clave)
        creada = _fecha(s.get("creadaEn"))
        revocada = _fecha(s["revocadaEn"]) if s.get("revocadaEn") else None
        filas.append((
            sesion, dueno, clave, _fecha_legible(s.get("expira")) or creada, creada, _texto(s.get("ip"), 45), _texto(s.get("userAgent"), 500),
            revocada, _texto(s.get("revocadaPor"), 40) if revocada else None,
        ))
    _insertar_lote(
        cur,
        "INSERT INTO nexus.userSessions (guid, userGuid, refreshHash, expiresAt, createdAt, ip, userAgent, revokedAt, revocationReason) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        filas,
    )
    importados += len(filas)

    # ── Intentos de inicio de sesión (todos) y el contador de cada navegador, con la MISMA regla que `usuarios.estado_origen`.
    filas, origenes = [], {}
    for e in eventos:
        tipo, origen = e.get("evento"), e.get("origen")
        if tipo not in ("intento", "origen_reiniciado"):
            continue
        valido = isinstance(origen, str) and origen and origen == origen.strip() and len(origen) <= 100
        en = _fecha(e.get("en"))
        if tipo == "intento":
            exitoso = bool(e.get("exitoso"))
            cuenta = e.get("guid")
            filas.append((
                cuenta if isinstance(cuenta, str) and cuenta in importadas else None, str(e.get("email") or "")[:320], exitoso,
                None if exitoso else _texto(e.get("motivo"), 20), _texto(e.get("ip"), 45), _texto(e.get("userAgent"), 500),
                origen if valido else None, en,
            ))
        if valido:
            fallidos, hasta, _ = origenes.get(origen, (0, None, None))
            if tipo == "origen_reiniciado":
                fallidos, hasta = 0, None
            else:
                fallidos, hasta = usuarios.plegar_intento_origen(fallidos, hasta, bool(e.get("exitoso")), e.get("motivo"), en)
            origenes[origen] = (fallidos, hasta, en)
    _insertar_lote(
        cur,
        "INSERT INTO nexus.loginAttempts (userGuid, email, succeeded, reason, ip, userAgent, originId, occurredAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        filas,
    )
    importados += len(filas)
    filas = [(origen, f, h, en) for origen, (f, h, en) in origenes.items() if f or h]
    _insertar_lote(cur, "INSERT INTO nexus.loginOrigins (originId, failedAttempts, lockedUntil, updatedAt) VALUES (?, ?, ?, ?)", filas)
    importados += len(filas)

    # ── Lo que un administrador le hizo a cada cuenta, con su motivo.
    tipos = {"eliminado": "DELETED", "cierre_forzado": "SESSIONS_CLOSED"}
    filas = []
    for e in eventos:
        tipo, guid = e.get("evento"), e.get("guid")
        if tipo == "estado":
            clase = "REACTIVATED" if e.get("activo") else "DEACTIVATED"
        elif tipo in tipos:
            clase = tipos[tipo]
        else:
            continue
        if not isinstance(guid, str) or guid not in importadas:
            saltados += 1
            continue
        filas.append((guid, clase, _texto(e.get("motivo"), 500), _texto(e.get("por"), 63), _fecha(e.get("en"))))
    filas += [(g, "DELETED", "Importada como retirada: otra cuenta vigente ya tenía ese correo.", "importacion", ahora) for g in retiradas_al_importar]
    _insertar_lote(cur, "INSERT INTO nexus.userAdminEvents (userGuid, kind, reason, byWhom, occurredAt) VALUES (?, ?, ?, ?, ?)", filas)
    importados += len(filas)
    logger.info(
        "Usuarios importados: %s cuentas (%s retiradas al importar), con la política de %s intentos y %s minutos.",
        len(importadas), len(retiradas_al_importar), config.AUTH_MAX_INTENTOS, config.AUTH_BLOQUEO_MIN,
    )
    return importados, saltados


_IMPORTADORES = {
    "organizaciones": _organizaciones,
    "clasificadores": _clasificadores,
    "bandeja": _bandeja,
    "auditoria": _auditoria,
    "usuarios": _usuarios,
    "tokens": _tokens,
    "llaves": _llaves,
    "uso-llaves": _uso_llaves,
    "webhooks": _webhooks,
    "entregas-webhooks": _entregas_webhooks,
}
