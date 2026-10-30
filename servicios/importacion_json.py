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
"""

import json
import logging
import re
import threading
import uuid
from datetime import datetime, timezone

from servicios import registro

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
}
_RE_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_RE_ID = re.compile(r"^[A-Za-z0-9_]+$")

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

    vistas = list(tenants_registro.indice_archivo().values())
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
    from servicios import clasificadores

    cur.execute("SELECT guid FROM nexus.tenants")
    existentes = {f[0] for f in cur.fetchall()}
    filas, saltados = [], 0
    for tenant, procesador in clasificadores.asignados_archivo().items():
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
    for archivo in sorted(registro.ruta("bandeja-x.jsonl").parent.glob("bandeja-*.jsonl")):
        eventos = registro.eventos(archivo.name)
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
    from servicios import auditoria

    filas, saltados = [], 0
    for e in registro.eventos(auditoria.ARCHIVO):
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

    filas, saltados, vistos, guids = [], 0, set(), set()
    for t in tokens._indice().values():
        clave, usuario = t.get("hash"), t.get("usuarioGuid")
        if (
            not isinstance(clave, str) or not _RE_HEX64.match(clave) or clave in vistos or t.get("tipo") not in tokens.TIPOS
            or not isinstance(usuario, str) or not usuario or len(usuario) > 63 or tokens._fecha(t.get("expira")) is None
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
    from servicios import llaves_cliente

    existentes = _organizaciones_existentes(cur)
    emitidas, revocadas = llaves_cliente._indice()
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
        expira = _fecha(e["expiraEn"]) if llaves_cliente._fecha(e.get("expiraEn")) else creada
        revocada = _fecha(revocadas[ident]) if ident in revocadas else None
        filas.append((ident, tenant, secreto, str(e.get("nombre") or "")[:100], str(e.get("descripcion") or "")[:500], creada, expira, revocada))
    _insertar_lote(
        cur,
        "INSERT INTO nexus.apiKeys (publicId, tenant, secretHash, name, description, createdAt, expiresAt, revokedAt) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        filas,
    )
    return len(filas), saltados


def _uso_llaves(cur) -> tuple[int, int]:
    from servicios import uso_llaves

    cur.execute("SELECT publicId, keyId FROM nexus.apiKeys")
    llaves = {f[0]: f[1] for f in cur.fetchall()}
    filas, saltados = [], 0
    for e in registro.eventos(uso_llaves.ARCHIVO):
        llave, cuando = e.get("llave"), uso_llaves._fecha(e)
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
    registrados, estados, eliminados, validados, fallidas = webhooks_cliente._indice()
    eliminado_en = {e.get("id"): e.get("en") for e in registro.eventos(webhooks_cliente.ARCHIVO) if e.get("evento") == "eliminado"}
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
    for e in registro.eventos(ent.ARCHIVO):
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


_IMPORTADORES = {
    "organizaciones": _organizaciones,
    "clasificadores": _clasificadores,
    "bandeja": _bandeja,
    "auditoria": _auditoria,
    "tokens": _tokens,
    "llaves": _llaves,
    "uso-llaves": _uso_llaves,
    "webhooks": _webhooks,
    "entregas-webhooks": _entregas_webhooks,
}
