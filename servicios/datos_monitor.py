"""Los datos extraídos de TODAS las organizaciones, de solo lectura, para la sección «Datos» de Nexus Monitor (Moibe, 2026-11-11).

Monitor no se conecta a SQL Server: le pregunta a este back con la llave de servicio, igual que con `/esquema` y `/duenos-procesadores`.
A diferencia de esos dos, aquí SÍ salen datos de negocio (lo que Document AI leyó de cada documento). Quien decide cuánto se enseña es
Monitor: en producción enmascara los valores antes de mandarlos al navegador.

Lee lo mismo que `servicios/datos_extraidos.py` (la vista `vEntityFactCurrent` y `extractionRuns`), pero cruzando organizaciones:

- `panorama()`: los documentos con al menos una extracción (la más reciente primero) y, por tipo documental, cómo le va a cada campo
  (cuántas veces salió, cuántas faltó, cuántas se corrigió a mano y su confianza);
- `documento(id)`: la ficha de un documento (sus campos vigentes, `datos_extraidos.de_documento`) y todas sus corridas.

Sin base, todo viene vacío con `sinBase: true`.
"""

import re

from config import SQLSERVER_HOST
from servicios import datos_extraidos
from servicios.tipos_documentales_comun import momento

#: Tope del listado: Monitor es para mirar, no para exportar.
LIMITE_DOCUMENTOS = 300
_RE_INDICE = re.compile(r"\[\d+\]")
_RE_DOCUMENTO = re.compile(r"^documento-[0-9a-f]{32}$")


class DocumentoNoEncontrado(LookupError):
    """No hay tal documento en este ambiente."""


def _numero(valor) -> float | None:
    return round(float(valor), 2) if valor is not None else None


def _organizaciones() -> dict[str, str]:
    """`{guid: nombre}`. Si el registro no se puede leer, los documentos salen igual, solo con el guid."""
    try:
        from servicios import tenants_registro

        return {t["guid"]: t["nombre"] for t in tenants_registro.listar()}
    except Exception:  # noqa: BLE001 — el nombre es adorno; no tumba el panorama
        return {}


def panorama() -> dict:
    if not SQLSERVER_HOST:
        return {"sinBase": True, "documentos": [], "tipos": []}
    from db import tablas

    nombres = _organizaciones()
    with tablas.lectura() as cur:
        cur.execute(
            f"SELECT TOP ({LIMITE_DOCUMENTOS}) d.publicId, d.tenant, d.originalName, d.mime, d.sizeBytes, d.receivedAt, "
            "r.publicId AS corridaId, r.documentTypePublicId AS tipoId, COALESCE(t.name, r.documentTypeName) AS tipoNombre, "
            "r.documentTypeRevision, r.status, r.errorDetail, r.confidenceAverage, r.confidenceMinimum, r.pageCount, r.finishedAt, "
            "(SELECT count(*) FROM nexus.extractionRuns x WHERE x.documentId = d.documentId) AS corridas, "
            "v.campos, v.conValor, v.ausentes, v.corregidos "
            "FROM nexus.documents d "
            "CROSS APPLY (SELECT TOP (1) r0.* FROM nexus.extractionRuns r0 WHERE r0.documentId = d.documentId ORDER BY r0.runId DESC) r "
            "LEFT JOIN nexus.documentTypes t ON t.documentTypeId = r.documentTypeId "
            "OUTER APPLY (SELECT count(*) AS campos, "
            "  sum(CASE WHEN f.valueNormalized IS NOT NULL THEN 1 ELSE 0 END) AS conValor, "
            "  sum(CASE WHEN f.nullReason IS NOT NULL THEN 1 ELSE 0 END) AS ausentes, "
            "  sum(CAST(f.isCorrected AS int)) AS corregidos "
            "  FROM nexus.vEntityFactCurrent f WHERE f.documentId = d.documentId) v "
            "ORDER BY r.runId DESC"
        )
        documentos = tablas.filas(cur)
        cur.execute(
            "SELECT d.tenant, f.documentId, f.documentTypePublicId AS tipoId, COALESCE(t.name, r.documentTypeName) AS tipoNombre, "
            "f.fieldPath, f.fieldName, CASE WHEN f.valueNormalized IS NULL THEN 0 ELSE 1 END AS conValor, f.nullReason, f.confidence, "
            "CAST(f.isCorrected AS int) AS corregido "
            "FROM nexus.vEntityFactCurrent f JOIN nexus.documents d ON d.documentId = f.documentId "
            "JOIN nexus.extractionRuns r ON r.runId = f.runId LEFT JOIN nexus.documentTypes t ON t.documentTypeId = f.documentTypeId"
        )
        hechos = tablas.filas(cur)

    def org(guid: str) -> dict:
        return {"guid": guid, "nombre": nombres.get(guid)}

    # Por tipo documental (y organización: dos organizaciones pueden tener tipos con el mismo nombre), cómo le va a cada campo. Las
    # filas de lo que se repite (`socios[2].nombre`) cuentan como el mismo campo (`socios[].nombre`).
    tipos: dict[tuple, dict] = {}
    for h in hechos:
        clave = (h["tenant"], h["tipoId"])
        tipo = tipos.setdefault(
            clave, {"id": h["tipoId"], "nombre": h["tipoNombre"], "organizacion": org(h["tenant"]), "_docs": set(), "_campos": {}}
        )
        tipo["_docs"].add(h["documentId"])
        ruta = _RE_INDICE.sub("[]", h["fieldPath"])
        campo = tipo["_campos"].setdefault(
            ruta, {"ruta": ruta, "nombre": h["fieldName"], "veces": 0, "conValor": 0, "ausentes": 0, "corregidos": 0, "_conf": []}
        )
        campo["veces"] += 1
        campo["conValor"] += h["conValor"]
        campo["ausentes"] += 1 if h["nullReason"] else 0
        campo["corregidos"] += h["corregido"]
        if h["confidence"] is not None:
            campo["_conf"].append(float(h["confidence"]))

    salida_tipos = []
    for tipo in tipos.values():
        campos = []
        todas: list[float] = []
        for c in tipo.pop("_campos").values():
            conf = c.pop("_conf")
            todas += conf
            c["confianzaPromedio"] = round(sum(conf) / len(conf), 2) if conf else None
            c["confianzaMinima"] = round(min(conf), 2) if conf else None
            campos.append(c)
        # Los que más fallan arriba: lo que falta y lo de menor confianza es lo que hay que afinar.
        campos.sort(key=lambda c: (-c["ausentes"], c["confianzaPromedio"] if c["confianzaPromedio"] is not None else -1, c["ruta"]))
        tipo["documentos"] = len(tipo.pop("_docs"))
        tipo["confianzaPromedio"] = round(sum(todas) / len(todas), 2) if todas else None
        tipo["campos"] = campos
        salida_tipos.append(tipo)
    salida_tipos.sort(key=lambda t: (-t["documentos"], (t["nombre"] or "").lower()))

    return {
        "sinBase": False,
        "limite": LIMITE_DOCUMENTOS,
        "documentos": [
            {
                "id": d["publicId"],
                "organizacion": org(d["tenant"]),
                "archivo": d["originalName"],
                "mime": d["mime"],
                "bytes": d["sizeBytes"],
                "recibidoEn": momento(d["receivedAt"]),
                "tipoDocumental": {"id": d["tipoId"], "nombre": d["tipoNombre"], "revision": d["documentTypeRevision"]},
                "estado": d["status"],
                "error": d["errorDetail"],
                "confianzaPromedio": _numero(d["confidenceAverage"]),
                "confianzaMinima": _numero(d["confidenceMinimum"]),
                "paginas": d["pageCount"],
                "extraidoEn": momento(d["finishedAt"]),
                "corridas": d["corridas"],
                "campos": d["campos"] or 0,
                "conValor": d["conValor"] or 0,
                "ausentes": d["ausentes"] or 0,
                "corregidos": d["corregidos"] or 0,
            }
            for d in documentos
        ],
        "tipos": salida_tipos,
    }


def documento(documento_id: str) -> dict:
    if not isinstance(documento_id, str) or not _RE_DOCUMENTO.match(documento_id):
        raise DocumentoNoEncontrado(documento_id)
    if not SQLSERVER_HOST:
        raise DocumentoNoEncontrado(documento_id)
    from db import tablas

    with tablas.lectura() as cur:
        cur.execute(
            "SELECT documentId, tenant, originalName, mime, sizeBytes, receivedAt, channel FROM nexus.documents WHERE publicId = ?", documento_id
        )
        doc = tablas.una(cur)
        if doc is None:
            raise DocumentoNoEncontrado(documento_id)
        cur.execute(
            "SELECT r.publicId, r.documentTypePublicId, COALESCE(t.name, r.documentTypeName) AS tipoNombre, r.documentTypeRevision, r.processorId, "
            "r.processorVersion, r.status, r.errorDetail, r.confidenceAverage, r.confidenceMinimum, r.pageCount, r.finishedAt, r.createdBy, "
            "(SELECT count(*) FROM nexus.entityFacts f WHERE f.runId = r.runId) AS campos "
            "FROM nexus.extractionRuns r LEFT JOIN nexus.documentTypes t ON t.documentTypeId = r.documentTypeId "
            "WHERE r.documentId = ? ORDER BY r.runId DESC",
            doc["documentId"],
        )
        corridas = tablas.filas(cur)

    ficha = datos_extraidos.de_documento(doc["tenant"], documento_id)
    return {
        "id": documento_id,
        "organizacion": {"guid": doc["tenant"], "nombre": _organizaciones().get(doc["tenant"])},
        "archivo": doc["originalName"],
        "mime": doc["mime"],
        "bytes": doc["sizeBytes"],
        "canal": doc["channel"],
        "recibidoEn": momento(doc["receivedAt"]),
        **ficha,
        "corridas": [
            {
                "id": c["publicId"],
                "tipoDocumental": {"id": c["documentTypePublicId"], "nombre": c["tipoNombre"], "revision": c["documentTypeRevision"]},
                "procesador": {"id": c["processorId"], "version": c["processorVersion"]},
                "estado": c["status"],
                "error": c["errorDetail"],
                "confianzaPromedio": _numero(c["confidenceAverage"]),
                "confianzaMinima": _numero(c["confidenceMinimum"]),
                "paginas": c["pageCount"],
                "terminadaEn": momento(c["finishedAt"]),
                "extraidoPor": c["createdBy"],
                "campos": c["campos"],
            }
            for c in corridas
        ],
    }
