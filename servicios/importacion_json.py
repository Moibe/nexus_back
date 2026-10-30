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
import threading
from datetime import datetime, timezone

from servicios import registro

logger = logging.getLogger(__name__)

#: Los que ya se sabe que están importados en esta base, para no preguntarle a SQL en cada llamada.
_hechos: set[str] = set()
_candado = threading.Lock()


def _fecha(valor) -> datetime:
    """Una fecha del JSONL (`2026-10-29T18:04:05+00:00` o con `Z`) como datetime UTC sin zona, que es como guarda la base. Una ilegible
    cae en «ahora»: mejor un renglón con fecha aproximada que perderlo."""
    try:
        d = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        return (d.astimezone(timezone.utc) if d.tzinfo else d).replace(tzinfo=None)
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).replace(tzinfo=None)


def asegurar(nombre: str) -> None:
    """Importa el registro `nombre` si todavía no se importó en esta base. Barato después de la primera vez (queda en memoria)."""
    if nombre in _hechos:
        return
    if nombre in ("bandeja", "clasificadores"):
        asegurar("organizaciones")
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
    """Los cuatro, en orden. Lo llama el arranque de la app (si falla, no la tumba: se reintenta al usar cada uno)."""
    for nombre in ("organizaciones", "clasificadores", "bandeja", "auditoria"):
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


_IMPORTADORES = {"organizaciones": _organizaciones, "clasificadores": _clasificadores, "bandeja": _bandeja, "auditoria": _auditoria}
