"""Organizaciones, clasificadores, bandeja de entrada y auditoría guardados en SQL Server (migración `0006`).

Hace lo mismo que el JSONL de `tenants_registro.py`, `clasificadores.py`, `bandeja.py` y `auditoria.py` —mismas firmas, mismos errores,
misma forma de salida—, y son esos módulos los que eligen: SQL si el `.env` trae `SQLSERVER_HOST`, el almacén si no (la laptop y las
pruebas offline). Antes de leer o escribir cada registro se asegura que su JSONL ya se haya importado una vez en esta base
(`servicios/importacion_json.py`).

Las fechas se guardan en UTC sin zona y se devuelven con la forma que tenían en el almacén (`2026-10-29T18:04:05+00:00`).
"""

import json
import uuid
from datetime import datetime, timezone

from db import tablas
from servicios import importacion_json


def _iso(valor) -> str | None:
    return valor.replace(tzinfo=timezone.utc).isoformat(timespec="seconds") if valor else None


def _ahora() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ── Organizaciones ──────────────────────────────────────────────────────────────────────────────────────────────────────

_COLUMNAS_ORG = "guid, name, slug, code, status, adminGuid, recoveryJson, createdAt"


def _a_org(f: dict) -> dict:
    return {
        "guid": f["guid"],
        "nombre": f["name"],
        "slug": f["slug"],
        "codigo": f["code"],
        "estado": f["status"],
        "adminGuid": f["adminGuid"],
        "recuperacion": json.loads(f["recoveryJson"] or "{}"),
        "creadaEn": _iso(f["createdAt"]),
    }


def orgs_listar() -> list[dict]:
    """De la más nueva a la más vieja; dos del mismo instante, la que se escribió después primero (igual que el almacén)."""
    importacion_json.asegurar("organizaciones")
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS_ORG} FROM nexus.tenants ORDER BY createdAt DESC, tenantId DESC")
        return [_a_org(f) for f in tablas.filas(cur)]


def orgs_por_guid(guid: str) -> dict | None:
    importacion_json.asegurar("organizaciones")
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS_ORG} FROM nexus.tenants WHERE guid = ?", guid)
        f = tablas.una(cur)
    return _a_org(f) if f else None


def orgs_slugs() -> set[str]:
    importacion_json.asegurar("organizaciones")
    with tablas.lectura() as cur:
        cur.execute("SELECT slug FROM nexus.tenants")
        return {f[0] for f in cur.fetchall()}


def orgs_crear(nombre: str, base: str, admin_guid: str, recuperacion: dict, slug_libre, prefijo: str) -> str:
    """Registra la organización y devuelve su guid. El slug libre y el código siguiente se calculan con la tabla BLOQUEADA, para que dos
    altas a la vez no se lleven el mismo."""
    importacion_json.asegurar("organizaciones")
    guid = str(uuid.uuid4())
    with tablas.transaccion() as cur:
        cur.execute("SELECT slug, code FROM nexus.tenants WITH (TABLOCKX, HOLDLOCK)")
        filas = cur.fetchall()
        slug = slug_libre(base, {f[0] for f in filas})
        numeros = [int(c.split("-", 1)[1]) for _, c in filas if c.startswith(f"{prefijo}-") and c.split("-", 1)[1].isdigit()]
        codigo = f"{prefijo}-{max(numeros, default=0) + 1:05d}"
        cur.execute(
            "INSERT INTO nexus.tenants (guid, name, slug, code, adminGuid, recoveryJson, createdAt) VALUES (?, ?, ?, ?, ?, ?, ?)",
            guid, nombre, slug, codigo, admin_guid, json.dumps(recuperacion or {}, ensure_ascii=False), _ahora().replace(microsecond=0),
        )
    return guid


def orgs_cambiar_estado(guid: str, estado: str) -> bool:
    importacion_json.asegurar("organizaciones")
    with tablas.transaccion() as cur:
        cur.execute("UPDATE nexus.tenants SET status = ? WHERE guid = ?", estado, guid)
        return cur.rowcount > 0


# ── Clasificadores ──────────────────────────────────────────────────────────────────────────────────────────────────────


def clasificador_de(tenant: str) -> str | None:
    importacion_json.asegurar("clasificadores")
    with tablas.lectura() as cur:
        cur.execute("SELECT processorId FROM nexus.tenantClassifiers WHERE tenant = ?", tenant)
        f = tablas.una(cur)
    return f["processorId"] if f else None


def clasificador_asignar(tenant: str, procesador_id: str) -> None:
    importacion_json.asegurar("clasificadores")
    with tablas.transaccion() as cur:
        cur.execute(
            "UPDATE nexus.tenantClassifiers WITH (UPDLOCK, HOLDLOCK) SET processorId = ?, assignedAt = sysutcdatetime() WHERE tenant = ? AND processorId <> ?",
            procesador_id, tenant, procesador_id,
        )
        cur.execute("SELECT 1 AS hay FROM nexus.tenantClassifiers WHERE tenant = ?", tenant)
        if tablas.una(cur) is None:
            cur.execute("INSERT INTO nexus.tenantClassifiers (tenant, processorId) VALUES (?, ?)", tenant, procesador_id)


# ── Bandeja de entrada ──────────────────────────────────────────────────────────────────────────────────────────────────

_COLUMNAS_ENTRADA = "publicId, tenant, documentPublicId, relativePath, sha256, sizeBytes, mime, originalName, channel, keyId, receivedAt, removedAt, removalReason, removalDetail"


def _a_entrada(f: dict) -> dict:
    return {
        "id": f["publicId"],
        "documentoId": f["documentPublicId"],
        "tenant": f["tenant"],
        "rutaRelativa": f["relativePath"],
        "sha256": f["sha256"],
        "tamanoBytes": int(f["sizeBytes"]),
        "mime": f["mime"],
        "nombreOriginal": f["originalName"],
        "canal": f["channel"],
        "llaveId": f["keyId"],
        "recibidoEn": _iso(f["receivedAt"]),
    }


def bandeja_entrar(tenant: str, documento_id: str, guardado: dict, mime: str, nombre_original: str, canal: str, llave_id: str | None) -> dict:
    importacion_json.asegurar("bandeja")
    fila = {
        "publicId": uuid.uuid4().hex,
        "tenant": tenant,
        "documentPublicId": documento_id,
        "relativePath": guardado["rutaRelativa"],
        "sha256": guardado["hash"],
        "sizeBytes": guardado["bytes"],
        "mime": mime,
        "originalName": nombre_original,
        "channel": canal,
        "keyId": llave_id,
        "receivedAt": _ahora().replace(microsecond=0),
        "removedAt": None,
        "removalReason": None,
        "removalDetail": None,
    }
    with tablas.transaccion() as cur:
        cur.execute(f"INSERT INTO nexus.inboxEntries ({_COLUMNAS_ENTRADA}) VALUES ({', '.join('?' * len(fila))})", *fila.values())
    return _a_entrada(fila)


def bandeja_pendientes(tenant: str) -> list[dict]:
    importacion_json.asegurar("bandeja")
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS_ENTRADA} FROM nexus.inboxEntries WHERE tenant = ? AND removedAt IS NULL ORDER BY entryId", tenant)
        return [_a_entrada(f) for f in tablas.filas(cur)]


def bandeja_entrada(tenant: str, id_entrada: str) -> dict | None:
    """La fila cruda (con su retiro), o `None`."""
    importacion_json.asegurar("bandeja")
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS_ENTRADA} FROM nexus.inboxEntries WHERE publicId = ? AND tenant = ?", id_entrada, tenant)
        return tablas.una(cur)


def bandeja_descartadas(tenant: str) -> list[dict]:
    importacion_json.asegurar("bandeja")
    with tablas.lectura() as cur:
        cur.execute(
            f"SELECT {_COLUMNAS_ENTRADA} FROM nexus.inboxEntries WHERE tenant = ? AND removalReason = 'descartado' ORDER BY removedAt DESC, entryId DESC",
            tenant,
        )
        return [
            {**_a_entrada(f), "descartadaEn": _iso(f["removedAt"]), **({"detalle": f["removalDetail"]} if f["removalDetail"] else {})}
            for f in tablas.filas(cur)
        ]


def bandeja_retirar(tenant: str, id_entrada: str, motivo: str, detalle: str | None) -> bool:
    """`False` si no existe en ese tenant o ya había salido. Un UPDATE condicionado: dos retiros a la vez no salen los dos."""
    importacion_json.asegurar("bandeja")
    with tablas.transaccion() as cur:
        cur.execute(
            "UPDATE nexus.inboxEntries SET removedAt = sysutcdatetime(), removalReason = ?, removalDetail = ? WHERE publicId = ? AND tenant = ? AND removedAt IS NULL",
            motivo, detalle, id_entrada, tenant,
        )
        return cur.rowcount > 0


def bandeja_en_otro_tenant(tenant: str, id_entrada: str) -> bool:
    importacion_json.asegurar("bandeja")
    with tablas.lectura() as cur:
        cur.execute("SELECT 1 AS hay FROM nexus.inboxEntries WHERE publicId = ? AND tenant <> ?", id_entrada, tenant)
        return tablas.una(cur) is not None


# ── Auditoría ───────────────────────────────────────────────────────────────────────────────────────────────────────────


def auditoria_registrar(evento: dict) -> None:
    importacion_json.asegurar("auditoria")
    tenant = evento.get("tenant") if isinstance(evento.get("tenant"), str) else None
    with tablas.transaccion() as cur:
        cur.execute(
            "INSERT INTO nexus.auditEvents (action, userGuid, tenant, occurredAt, dataJson) VALUES (?, ?, ?, sysutcdatetime(), ?)",
            evento["accion"], evento.get("usuarioGuid"), tenant[:63] if tenant else None, json.dumps(evento, ensure_ascii=False),
        )


def auditoria_eventos(acciones: frozenset[str] | set[str] | None = None, desde: datetime | None = None) -> list[dict]:
    """Los eventos en el orden en que se escribieron; con `acciones`, solo esas; con `desde` (UTC), solo los posteriores."""
    importacion_json.asegurar("auditoria")
    sql, parametros = "SELECT dataJson FROM nexus.auditEvents WHERE 1 = 1", []
    if acciones is not None:
        lista = sorted(acciones)
        if not lista:
            return []
        sql += f" AND action IN ({', '.join('?' * len(lista))})"
        parametros += lista
    if desde is not None:
        sql += " AND occurredAt >= ?"
        parametros.append(desde.astimezone(timezone.utc).replace(tzinfo=None) if desde.tzinfo else desde)
    with tablas.lectura() as cur:
        cur.execute(sql + " ORDER BY eventId", *parametros)
        return [json.loads(f[0]) for f in cur.fetchall()]
