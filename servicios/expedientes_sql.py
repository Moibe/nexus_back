"""Documentos y expedientes guardados en SQL Server (migraciones `0003` y `0004`).

Es el segundo paso de la decisión del 2026-10-28 («todo lo nuevo directo a SQL; lo que ya está en JSON se migra cuando lo nuevo lo
toca»). Las dos entidades se mudaron juntas porque la pertenencia (`nexus.caseDocuments`) une expedientes con documentos.

**El expediente NO tiene tipo** (desde la `0004`, 2026-10-29): es un receptáculo de documentos. Los expedientes virtuales y sus tipos que
la `0004` le agregó se quitaron el 2026-11-05 (Moibe: «voy a quitar los expedientes virtuales»): sus tablas las borra la migración `0012`, y un
proceso corre sobre el expediente completo (`servicios/ejecuciones.py`).

**Tampoco vive en un grupo**: los grupos de expedientes (migración `0010`, 2026-11-03) se quitaron el 2026-11-06 (Moibe: «los expedientes no se
agruparán en Grupos y no se requiere el menú»). La migración `0013` borra `nexus.caseGroups` y `nexus.cases.caseGroupId`; este módulo ya no
los lee ni los escribe.

Hace exactamente lo mismo que el JSONL de `documentos.py` y `expedientes.py` —mismas firmas, mismos errores, misma forma de salida—, y son esos
dos módulos los que eligen: SQL si el `.env` trae `SQLSERVER_HOST`, el almacén si no (la laptop y las pruebas offline). Ellos validan lo que llega
(nombre limpio…) ANTES de llamar aquí; aquí solo se guarda y se lee.

## Qué gana al estar en la base

- Un documento no se mete dos veces al mismo expediente (`UQ_caseDocuments_case_document`).
- Todo «leer y luego escribir» va en una TRANSACCIÓN con el renglón bloqueado (`UPDLOCK, HOLDLOCK`), en lugar del candado de proceso
  del almacén, que dejaba de alcanzar con más de un worker de uvicorn.
- El total de documentos y el peso de un expediente siguen sin guardarse: se calculan en la consulta.
"""

import uuid
from datetime import datetime, timezone

from db import tablas
from servicios.tipos_documentales_comun import momento

# ── Documentos ─────────────────────────────────────────────────────────────────────────────────────────────────────────────

COLUMNAS_DOC = "publicId, tenant, sha256, relativePath, sizeBytes, mime, originalName, channel, keyId, receivedAt"


def _recibido(valor) -> str:
    """La fecha con la misma forma que en el almacén: `2026-10-29T18:04:05+00:00`."""
    return valor.replace(tzinfo=timezone.utc).isoformat(timespec="seconds")


def a_documento(f: dict) -> dict:
    return {
        "id": f["publicId"],
        "tenant": f["tenant"],
        "sha256": f["sha256"],
        "rutaRelativa": f["relativePath"],
        "tamanoBytes": int(f["sizeBytes"]),
        "mime": f["mime"],
        "nombreOriginal": f["originalName"],
        "canal": f["channel"],
        "llaveId": f["keyId"],
        "recibidoEn": _recibido(f["receivedAt"]),
    }


def columnas_doc(alias: str) -> str:
    """Las columnas del documento con el alias de su tabla (`d.publicId, d.tenant…`), para las consultas que la juntan con otra."""
    return ", ".join(f"{alias}.{c.strip()}" for c in COLUMNAS_DOC.split(","))


def documentos_nacer(tenant: str, guardado: dict, mime: str, nombre_original: str, canal: str, llave_id: str | None) -> dict:
    recibido = datetime.now(timezone.utc).replace(microsecond=0, tzinfo=None)
    fila = {
        "publicId": f"documento-{uuid.uuid4().hex}",
        "tenant": tenant,
        "sha256": guardado["hash"],
        "relativePath": guardado["rutaRelativa"],
        "sizeBytes": guardado["bytes"],
        "mime": mime,
        "originalName": nombre_original,
        "channel": canal,
        "keyId": llave_id,
        "receivedAt": recibido,
    }
    with tablas.transaccion() as cur:
        cur.execute(f"INSERT INTO nexus.documents ({COLUMNAS_DOC}) VALUES ({', '.join('?' * len(fila))})", *fila.values())
    return a_documento(fila)


def documentos_listar(tenant: str) -> list[dict]:
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {COLUMNAS_DOC} FROM nexus.documents WHERE tenant = ? ORDER BY documentId", tenant)
        return [a_documento(f) for f in tablas.filas(cur)]


def documentos_obtener(tenant: str, documento_id: str) -> dict | None:
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {COLUMNAS_DOC} FROM nexus.documents WHERE publicId = ? AND tenant = ?", documento_id, tenant)
        f = tablas.una(cur)
    return a_documento(f) if f else None


# ── Expedientes ────────────────────────────────────────────────────────────────────────────────────────────────────────────

# Cada expediente con cuántos documentos tiene y cuánto pesan: se calcula en la consulta, no se guarda.
_RESUMEN = (
    "SELECT c.caseId, c.publicId, c.name, c.createdBy, c.createdAt, count(d.documentId) AS total, coalesce(sum(d.sizeBytes), 0) AS peso "
    "FROM nexus.cases c LEFT JOIN nexus.caseDocuments cd ON cd.caseId = c.caseId LEFT JOIN nexus.documents d ON d.documentId = cd.documentId "
    "WHERE c.tenant = ? {filtro} GROUP BY c.caseId, c.publicId, c.name, c.createdBy, c.createdAt ORDER BY c.caseId"
)


def _a_resumen(f: dict) -> dict:
    return {
        "id": f["publicId"],
        "nombre": f["name"],
        "creadoPor": f["createdBy"],
        "creadoEn": momento(f["createdAt"]),
        "totalDocumentos": int(f["total"]),
        "tamanoBytes": int(f["peso"]),
    }


def expediente_bloqueado(cur, tenant: str, expediente_id: str) -> dict | None:
    """La fila del expediente, bloqueada hasta el final de la transacción: nadie lo borra ni le mete o le saca un documento en medio."""
    cur.execute("SELECT caseId, name FROM nexus.cases WITH (UPDLOCK, HOLDLOCK) WHERE publicId = ? AND tenant = ?", expediente_id, tenant)
    return tablas.una(cur)


def expedientes_crear(tenant: str, nombre: str, por: str) -> dict:
    expediente_id = f"expediente-{uuid.uuid4().hex}"
    with tablas.transaccion() as cur:
        cur.execute(
            "INSERT INTO nexus.cases (publicId, tenant, name, createdBy) OUTPUT INSERTED.createdAt VALUES (?, ?, ?, ?)",
            expediente_id, tenant, nombre, por,
        )
        creado = tablas.una(cur)["createdAt"]
    return {"id": expediente_id, "nombre": nombre, "creadoPor": por, "creadoEn": momento(creado), "totalDocumentos": 0, "tamanoBytes": 0}


def expedientes_listar(tenant: str) -> list[dict]:
    with tablas.lectura() as cur:
        cur.execute(_RESUMEN.format(filtro=""), tenant)
        return [_a_resumen(f) for f in tablas.filas(cur)]


def expedientes_obtener(tenant: str, expediente_id: str) -> dict | None:
    """El expediente con sus documentos, en el orden en que se agregaron."""
    with tablas.lectura() as cur:
        cur.execute(_RESUMEN.format(filtro="AND c.publicId = ?"), tenant, expediente_id)
        f = tablas.una(cur)
        if f is None:
            return None
        cur.execute(
            f"SELECT {columnas_doc('d')} FROM nexus.caseDocuments cd "
            "JOIN nexus.documents d ON d.documentId = cd.documentId WHERE cd.caseId = ? ORDER BY cd.caseDocumentId",
            f["caseId"],
        )
        docs = [a_documento(x) for x in tablas.filas(cur)]
    return {**_a_resumen(f), "documentos": docs}


def expedientes_agregar_documento(tenant: str, expediente_id: str, documento_id: str, por: str, maximo: int) -> bool:
    from servicios.expedientes import DocumentoNoEncontrado, ExpedienteInvalido, NoEncontrado

    with tablas.transaccion() as cur:
        e = expediente_bloqueado(cur, tenant, expediente_id)
        if e is None:
            raise NoEncontrado(expediente_id)
        cur.execute("SELECT documentId FROM nexus.documents WHERE publicId = ? AND tenant = ?", documento_id, tenant)
        d = tablas.una(cur)
        if d is None:
            raise DocumentoNoEncontrado(documento_id)
        cur.execute(
            "SELECT count(*) AS n, sum(CASE WHEN documentId = ? THEN 1 ELSE 0 END) AS ya FROM nexus.caseDocuments WHERE caseId = ?",
            d["documentId"], e["caseId"],
        )
        cuenta = tablas.una(cur)
        if cuenta["ya"]:
            return False
        if cuenta["n"] >= maximo:
            raise ExpedienteInvalido(f"El expediente ya tiene el máximo de documentos ({maximo}).")
        cur.execute("INSERT INTO nexus.caseDocuments (caseId, documentId, addedBy) VALUES (?, ?, ?)", e["caseId"], d["documentId"], por)
    return True


def expedientes_quitar_documento(tenant: str, expediente_id: str, documento_id: str) -> None:
    """Saca el documento del expediente; el documento no se borra (si era su único expediente, vuelve al void)."""
    from servicios.expedientes import DocumentoNoEncontrado, NoEncontrado

    with tablas.transaccion() as cur:
        e = expediente_bloqueado(cur, tenant, expediente_id)
        if e is None:
            raise NoEncontrado(expediente_id)
        cur.execute(
            "DELETE cd FROM nexus.caseDocuments cd JOIN nexus.documents d ON d.documentId = cd.documentId "
            "WHERE cd.caseId = ? AND d.publicId = ?",
            e["caseId"], documento_id,
        )
        if cur.rowcount == 0:
            raise DocumentoNoEncontrado(documento_id)


def expedientes_borrar(tenant: str, expediente_id: str) -> dict:
    """Borra el expediente; sus pertenencias se van con él (`FK_caseDocuments_case` es ON DELETE CASCADE) y los documentos se quedan."""
    from servicios.expedientes import NoEncontrado

    with tablas.transaccion() as cur:
        e = expediente_bloqueado(cur, tenant, expediente_id)
        if e is None:
            raise NoEncontrado(expediente_id)
        cur.execute("SELECT count(*) AS n FROM nexus.caseDocuments WHERE caseId = ?", e["caseId"])
        total = int(tablas.una(cur)["n"])
        cur.execute("DELETE FROM nexus.cases WHERE caseId = ?", e["caseId"])
    return {"nombre": e["name"], "totalDocumentos": total}


def expedientes_listar_documentos(tenant: str, sin_expediente: bool | None) -> list[dict]:
    with tablas.lectura() as cur:
        cur.execute(f"SELECT documentId, {COLUMNAS_DOC} FROM nexus.documents WHERE tenant = ? ORDER BY documentId", tenant)
        docs = tablas.filas(cur)
        cur.execute(
            "SELECT cd.documentId, c.publicId, c.name FROM nexus.caseDocuments cd JOIN nexus.cases c ON c.caseId = cd.caseId "
            "WHERE c.tenant = ? ORDER BY c.caseId",
            tenant,
        )
        en: dict[int, list[dict]] = {}
        for f in tablas.filas(cur):
            en.setdefault(f["documentId"], []).append({"id": f["publicId"], "nombre": f["name"]})
    resultado = [{**a_documento(d), "expedientes": en.get(d["documentId"], [])} for d in docs]
    if sin_expediente is True:
        resultado = [d for d in resultado if not d["expedientes"]]
    elif sin_expediente is False:
        resultado = [d for d in resultado if d["expedientes"]]
    return resultado
