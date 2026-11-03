"""Tipos de expediente virtual, documentos y expedientes guardados en SQL Server (migraciones `0003` y `0004`).

Es el segundo paso de la decisión del 2026-10-28 («todo lo nuevo directo a SQL; lo que ya está en JSON se migra cuando lo nuevo lo
toca»). Las tres entidades se mudaron juntas porque se referencian entre sí: el esquema de un tipo apunta a tipos documentales
(`nexus.documentTypes`, `0002`) y la pertenencia une expedientes con documentos.

**Desde la `0004` (2026-10-29) el expediente NO tiene tipo:** es un receptáculo de documentos. Los tipos (`nexus.virtualCaseTypes`) son de
EXPEDIENTE VIRTUAL, y sus instancias —subconjuntos de los documentos de un expediente— viven en `servicios/expedientes_virtuales.py`.

Hace exactamente lo mismo que el JSONL de `tipos_expediente.py`, `documentos.py` y `expedientes.py` —mismas firmas, mismos errores,
misma forma de salida—, y son esos tres módulos los que eligen: SQL si el `.env` trae `SQLSERVER_HOST`, el almacén si no (la laptop y
las pruebas offline). Ellos validan lo que llega (nombre limpio, esquema sin repetidos…) ANTES de llamar aquí; aquí solo se guarda y se lee.

## Qué gana al estar en la base

- El esquema de un tipo apunta a tipos documentales con una llave foránea REAL: borrar un tipo documental lo quita de los esquemas
  (en el almacén se quedaba como un id sin nombre, «Tipo documental eliminado»).
- Un tipo con expedientes virtuales no se puede borrar aunque alguien lo intente por fuera del servicio: lo impide `FK_virtualCases_virtualCaseType`.
- Un documento no se mete dos veces al mismo expediente (`UQ_caseDocuments_case_document`), y el nombre de un tipo no se repite en su
  organización (`UQ_virtualCaseTypes_tenant_name`; la base no distingue mayúsculas, que es justo lo que pide el producto).
- Todo «leer y luego escribir» va en una TRANSACCIÓN con el renglón bloqueado (`UPDLOCK, HOLDLOCK`), en lugar del candado de proceso
  del almacén, que dejaba de alcanzar con más de un worker de uvicorn.
- El total de documentos y el peso de un expediente siguen sin guardarse: se calculan en la consulta.
"""

import uuid
from datetime import datetime, timezone

from db import tablas
from servicios.tipos_documentales_comun import momento

# ── Tipos de expediente virtual ────────────────────────────────────────────────────────────────────────────────────────────


def _esquemas(cur, tenant: str, tipo_ids: list[int] | None = None) -> dict[int, list[str]]:
    """`{virtualCaseTypeId: [ids de tipos documentales, en su orden]}` de una organización (o solo de los tipos indicados)."""
    sql = (
        "SELECT s.virtualCaseTypeId, s.documentTypePublicId FROM nexus.virtualCaseTypeDocumentTypes s "
        "JOIN nexus.virtualCaseTypes t ON t.virtualCaseTypeId = s.virtualCaseTypeId WHERE t.tenant = ?"
    )
    parametros: list = [tenant]
    if tipo_ids is not None:
        if not tipo_ids:
            return {}
        sql += f" AND s.virtualCaseTypeId IN ({', '.join('?' * len(tipo_ids))})"
        parametros += tipo_ids
    cur.execute(sql + " ORDER BY s.virtualCaseTypeId, s.position", *parametros)
    esquemas: dict[int, list[str]] = {}
    for f in tablas.filas(cur):
        esquemas.setdefault(f["virtualCaseTypeId"], []).append(f["documentTypePublicId"])
    return esquemas


def _a_tipo(f: dict, esquema: list[str]) -> dict:
    return {"id": f["publicId"], "nombre": f["name"], "tiposDocumentales": list(esquema), "creadoPor": f["createdBy"], "creadoEn": momento(f["createdAt"])}


_COLUMNAS_TIPO = "virtualCaseTypeId, publicId, name, createdBy, createdAt"


def tipo_bloqueado(cur, tenant: str, tipo_id: str) -> dict | None:
    """La fila del tipo, bloqueada hasta el final de la transacción (nadie lo edita, lo borra ni le cuelga un expediente virtual en medio).
    También la usa `expedientes_virtuales.crear`."""
    cur.execute(f"SELECT {_COLUMNAS_TIPO} FROM nexus.virtualCaseTypes WITH (UPDLOCK, HOLDLOCK) WHERE publicId = ? AND tenant = ?", tipo_id, tenant)
    return tablas.una(cur)


def _nombre_ocupado(cur, tenant: str, nombre: str, salvo: int | None = None) -> str | None:
    """El id del tipo que ya se llama así en esa organización (sin distinguir mayúsculas: la base es CI), o `None`."""
    cur.execute("SELECT virtualCaseTypeId, publicId FROM nexus.virtualCaseTypes WITH (UPDLOCK, HOLDLOCK) WHERE tenant = ? AND name = ?", tenant, nombre)
    for f in tablas.filas(cur):
        if f["virtualCaseTypeId"] != salvo:
            return f["publicId"]
    return None


def _comprobar_tipos_documentales(cur, tenant: str, ids: list[str]) -> None:
    """`TipoInvalido` si alguno no es un tipo documental de esa organización (uno de otra se contesta igual que uno que no existe).
    Se lee con candado compartido hasta el final, para que no lo borren entre la comprobación y la escritura."""
    from servicios.tipos_expediente import TipoInvalido

    cur.execute(
        f"SELECT publicId FROM nexus.documentTypes WITH (HOLDLOCK) WHERE tenant = ? AND publicId IN ({', '.join('?' * len(ids))})", tenant, *ids
    )
    if len({f["publicId"] for f in tablas.filas(cur)}) != len(set(ids)):
        raise TipoInvalido("Uno de los tipos documentales no existe en esta organización.")


def _escribir_esquema(cur, tipo_id: int, ids: list[str]) -> None:
    cur.execute("DELETE FROM nexus.virtualCaseTypeDocumentTypes WHERE virtualCaseTypeId = ?", tipo_id)
    cur.executemany(
        "INSERT INTO nexus.virtualCaseTypeDocumentTypes (virtualCaseTypeId, documentTypePublicId, position) VALUES (?, ?, ?)",
        [(tipo_id, i, n) for n, i in enumerate(ids)],
    )


def tipos_listar(tenant: str) -> list[dict]:
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS_TIPO} FROM nexus.virtualCaseTypes WHERE tenant = ? ORDER BY virtualCaseTypeId", tenant)
        filas = tablas.filas(cur)
        esquemas = _esquemas(cur, tenant)
    return [_a_tipo(f, esquemas.get(f["virtualCaseTypeId"], [])) for f in filas]


def tipos_obtener(tenant: str, tipo_id: str) -> dict | None:
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS_TIPO} FROM nexus.virtualCaseTypes WHERE publicId = ? AND tenant = ?", tipo_id, tenant)
        f = tablas.una(cur)
        if f is None:
            return None
        return _a_tipo(f, _esquemas(cur, tenant, [f["virtualCaseTypeId"]]).get(f["virtualCaseTypeId"], []))


def tipos_crear(tenant: str, nombre: str, ids: list[str], por: str) -> dict:
    """El tipo y su esquema en UNA transacción: o quedan los dos o no queda nada."""
    from servicios.tipos_expediente import NombreRepetido

    tipo_id = f"tipo-expediente-{uuid.uuid4().hex}"
    with tablas.transaccion() as cur:
        ocupado = _nombre_ocupado(cur, tenant, nombre)
        if ocupado:
            raise NombreRepetido(ocupado)
        _comprobar_tipos_documentales(cur, tenant, ids)
        cur.execute(
            "INSERT INTO nexus.virtualCaseTypes (publicId, tenant, name, createdBy) OUTPUT INSERTED.virtualCaseTypeId, INSERTED.createdAt VALUES (?, ?, ?, ?)",
            tipo_id, tenant, nombre, por,
        )
        nuevo = tablas.una(cur)
        _escribir_esquema(cur, nuevo["virtualCaseTypeId"], ids)
    return {"id": tipo_id, "nombre": nombre, "tiposDocumentales": list(ids), "creadoPor": por, "creadoEn": momento(nuevo["createdAt"])}


def tipos_editar(tenant: str, tipo_id: str, nombre: str | None, ids: list[str] | None, por: str) -> tuple[dict, str, list[str]]:
    """Todo se comprueba antes de escribir, dentro de la misma transacción: si algo falla no cambia ni el nombre ni el esquema."""
    from servicios.tipos_expediente import NoEncontrado, NombreRepetido

    with tablas.transaccion() as cur:
        f = tipo_bloqueado(cur, tenant, tipo_id)
        if f is None:
            raise NoEncontrado(tipo_id)
        actual = _a_tipo(f, _esquemas(cur, tenant, [f["virtualCaseTypeId"]]).get(f["virtualCaseTypeId"], []))
        if nombre is not None:
            ocupado = _nombre_ocupado(cur, tenant, nombre, salvo=f["virtualCaseTypeId"])
            if ocupado:
                raise NombreRepetido(ocupado)
        if ids is not None:
            _comprobar_tipos_documentales(cur, tenant, ids)
        nombre_anterior, esquema_anterior = actual["nombre"], list(actual["tiposDocumentales"])
        cambia_nombre = nombre is not None and nombre != nombre_anterior
        cambia_esquema = ids is not None and ids != esquema_anterior
        if cambia_nombre:
            cur.execute("UPDATE nexus.virtualCaseTypes SET name = ? WHERE virtualCaseTypeId = ?", nombre, f["virtualCaseTypeId"])
        if cambia_esquema:
            _escribir_esquema(cur, f["virtualCaseTypeId"], ids)
        if cambia_nombre or cambia_esquema:
            cur.execute("UPDATE nexus.virtualCaseTypes SET updatedAt = sysutcdatetime(), updatedBy = ? WHERE virtualCaseTypeId = ?", por, f["virtualCaseTypeId"])
    return (
        {**actual, "nombre": nombre_anterior if nombre is None else nombre, "tiposDocumentales": esquema_anterior if ids is None else list(ids)},
        nombre_anterior,
        esquema_anterior,
    )


def tipos_borrar(tenant: str, tipo_id: str) -> dict:
    """La cuenta de expedientes virtuales y el borrado van con el tipo bloqueado: `expedientes_virtuales.crear` bloquea la misma fila, así
    que un expediente virtual nuevo no se cuela entre una cosa y la otra."""
    from servicios.tipos_expediente import NoEncontrado, TipoConExpedientes

    with tablas.transaccion() as cur:
        f = tipo_bloqueado(cur, tenant, tipo_id)
        if f is None:
            raise NoEncontrado(tipo_id)
        actual = _a_tipo(f, _esquemas(cur, tenant, [f["virtualCaseTypeId"]]).get(f["virtualCaseTypeId"], []))
        cur.execute("SELECT count(*) AS n FROM nexus.virtualCases WHERE virtualCaseTypeId = ?", f["virtualCaseTypeId"])
        total = tablas.una(cur)["n"]
        if total:
            raise TipoConExpedientes(total)
        # El esquema se va con él (ON DELETE CASCADE).
        cur.execute("DELETE FROM nexus.virtualCaseTypes WHERE virtualCaseTypeId = ?", f["virtualCaseTypeId"])
    return actual


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
    """La fila del expediente, bloqueada hasta el final de la transacción. También la usa `expedientes_virtuales`."""
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
    """El expediente con sus documentos y, desde la `0004`, sus expedientes virtuales (cada uno con su tipo y los ids de sus documentos)."""
    from servicios import expedientes_virtuales

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
        virtuales = expedientes_virtuales.resumenes_de(cur, f["caseId"])
    return {**_a_resumen(f), "documentos": docs, "expedientesVirtuales": virtuales}


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
    """Sacar el documento del expediente lo saca también de sus expedientes virtuales: lo hace la base (ON DELETE CASCADE)."""
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
