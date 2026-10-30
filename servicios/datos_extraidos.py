"""Lo que se sabe de cada documento: su CLASIFICACIÓN y sus DATOS EXTRAÍDOS (entity_fact), guardados en SQL (migración `0007`).

«Una cosa es el file y otra los datos que se extrajeron» (Moibe, 2026-10-29). El documento (`servicios/documentos.py`) son los bytes y de
dónde vinieron; aquí vive lo que Document AI dijo de él:

- **clasificación**: cada vez que el clasificador opina, una fila (categoría, el tipo documental que le corresponde, confianza);
- **corrida de extracción** (`extraction_run` del diccionario): cada extracción, con el tipo documental, la revisión de su configuración,
  el procesador y su versión, cómo terminó y sus dos confianzas resumen. También las que fallan: son historia;
- **datos** (`entity_fact`): un renglón por campo, por corrida, con valor crudo y normalizado, confianza 0-100 + la original + su método,
  y página. Lo obligatorio que no apareció (`_metadata.campos_ausentes`) también tiene renglón, con `nullReason = not_present`;
- **correcciones** (`field_correction`): una persona corrige un valor y queda como fila nueva; el dato original nunca se toca.

Todo es de solo agregar. Lo vigente de un documento lo da la vista `nexus.vEntityFactCurrent` (su última corrida que terminó, con la última
corrección de cada campo): es lo que leerá el motor de reglas.

Quien escribe es el SERVIDOR, no el navegador: `/ia/clasificar` y `/ia/extraer` guardan lo que Google les contestó cuando la petición dice
de qué documento se trata. Así nadie puede guardar datos que Document AI no produjo.

Solo SQL (decisión del 2026-10-28): sin base, escribir levanta `SinBase` y leer devuelve «nada todavía».
"""

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from config import SQLSERVER_HOST
from servicios.bloques import es_campo
from servicios.tipos_documentales_comun import momento

#: Las llaves de la respuesta de una extracción que NO son campos.
_NO_SON_CAMPOS = frozenset({"confianza_promedio", "confianza_minima", "ocr", "_metadata"})
_RE_DOCUMENTO = re.compile(r"^documento-[0-9a-f]{32}$")
_RE_INDICE = re.compile(r"\[(\d+)\]")
_MAX_DETALLE = 1000
_MAX_MOTIVO = 500
_MAX_VALOR = 4000

#: El motor que produjo los datos, en `extractionRuns.engine`.
MOTOR_DOCUMENT_AI = "document_ai_extractor"


class SinBase(RuntimeError):
    """Los datos extraídos solo viven en SQL Server, y esta instancia no tiene base."""


class DocumentoNoEncontrado(LookupError):
    """No hay tal documento EN ESA organización."""


class TipoNoEncontrado(LookupError):
    """No hay tal tipo documental en esa organización, o su procesador no es el que se usó."""


class CampoNoEncontrado(LookupError):
    """El documento no tiene ese campo en su extracción vigente."""


class CorreccionInvalida(ValueError):
    """El valor corregido no se puede guardar."""


def _tablas():
    if not SQLSERVER_HOST:
        raise SinBase("Los datos extraídos necesitan la base de datos, y esta instancia no tiene.")
    from db import tablas

    return tablas


def _ahora() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _texto(valor: Any) -> str | None:
    """Un valor de Document AI como texto para la base: las cadenas tal cual, lo demás en JSON (números, booleanos)."""
    if valor is None:
        return None
    texto = valor if isinstance(valor, str) else json.dumps(valor, ensure_ascii=False)
    return texto[:_MAX_VALOR]


def _confianza(valor: Any) -> float | None:
    return round(float(valor), 2) if isinstance(valor, (int, float)) and 0 <= valor <= 100 else None


# ── De la respuesta de Document AI a renglones ──────────────────────────────────────────────────────────────────────────


def aplanar(resultado: dict[str, Any]) -> list[dict[str, Any]]:
    """Los renglones de `entityFacts` de una extracción: uno por campo hoja, y uno por cada campo obligatorio ausente.

    La ruta conserva la jerarquía de la respuesta: `rfc`, `domicilio.calle`, `accionistas[2].nombre`, `telefonos[1]` (las filas cuentan desde
    1, como en `campos_ausentes`). `recordIndex` es el índice de la fila más cercana al campo; NULL si nada de su camino se repite.
    """
    renglones: list[dict[str, Any]] = []

    def recorrer(nodo: Any, ruta: str, nombre: str, indice: int | None) -> None:
        if es_campo(nodo):
            normalizado = nodo.get("value_normalized")
            if normalizado is None:
                normalizado = nodo.get("value_raw")
            if normalizado is None:
                return
            renglones.append(
                {
                    "fieldPath": ruta,
                    "fieldName": nombre,
                    "recordIndex": indice,
                    "valueRaw": _texto(nodo.get("value_raw")),
                    "valueNormalized": _texto(normalizado),
                    "confidence": _confianza(nodo.get("confianza")),
                    "confidenceRaw": float(nodo["confianza_cruda"]) if isinstance(nodo.get("confianza_cruda"), (int, float)) else None,
                    "confidenceMethod": nodo.get("metodo_confianza") if nodo.get("metodo_confianza") in ("extractor_confidence", "logprob", "composite") else None,
                    "pageNumber": nodo["page_number"] if isinstance(nodo.get("page_number"), int) and nodo["page_number"] >= 1 else None,
                    "nullReason": None,
                }
            )
        elif isinstance(nodo, list):
            for i, elemento in enumerate(nodo, start=1):
                recorrer(elemento, f"{ruta}[{i}]", nombre, i)
        elif isinstance(nodo, dict):
            for llave, hijo in nodo.items():
                recorrer(hijo, f"{ruta}.{llave}" if ruta else llave, llave, indice)

    recorrer({k: v for k, v in resultado.items() if k not in _NO_SON_CAMPOS}, "", "", None)

    presentes = {r["fieldPath"] for r in renglones}
    for ruta in (resultado.get("_metadata") or {}).get("campos_ausentes") or []:
        if not isinstance(ruta, str) or not ruta or ruta in presentes:
            continue
        indices = _RE_INDICE.findall(ruta)
        renglones.append(
            {
                "fieldPath": ruta[:400],
                "fieldName": _RE_INDICE.sub("", ruta.rsplit(".", 1)[-1])[:200],
                "recordIndex": int(indices[-1]) if indices else None,
                "valueRaw": None,
                "valueNormalized": None,
                "confidence": None,
                "confidenceRaw": None,
                "confidenceMethod": None,
                "pageNumber": None,
                "nullReason": "not_present",
            }
        )
        presentes.add(ruta)
    return renglones


# ── Escribir ────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _documento(cur, tenant: str, documento_id: object) -> int:
    if not isinstance(documento_id, str) or not _RE_DOCUMENTO.match(documento_id):
        raise DocumentoNoEncontrado(documento_id)
    cur.execute("SELECT documentId FROM nexus.documents WHERE publicId = ? AND tenant = ?", documento_id, tenant)
    fila = cur.fetchone()
    if fila is None:
        raise DocumentoNoEncontrado(documento_id)
    return fila[0]


def comprobar(tenant: str, documento_id: object, tipo_id: object, procesador_id: str) -> None:
    """Lo que `registrar_extraccion` va a exigir, comprobado ANTES de pagarle a Google: que el documento sea de la organización y que el tipo
    documental también, con ese procesador. Levanta lo mismo que ella."""
    tablas = _tablas()
    with tablas.lectura() as cur:
        _documento(cur, tenant, documento_id)
        cur.execute("SELECT processorId FROM nexus.documentTypes WHERE publicId = ? AND tenant = ?", tipo_id, tenant)
        fila = cur.fetchone()
    if fila is None or (fila[0] or "") != procesador_id:
        raise TipoNoEncontrado(tipo_id)


def registrar_clasificacion(tenant: str, documento_id: object, respuesta: dict[str, Any], procesador_id: str, por: str) -> None:
    """Anota lo que dijo el clasificador. La categoría se traduce al tipo documental de la organización con la misma regla con que se
    armó el clasificador (`esquema.normalizar_nombre` del id); `otro`, o una categoría que ya no corresponde a ningún tipo, quedan sin tipo."""
    from servicios.esquema import normalizar_nombre

    tablas = _tablas()
    categoria = respuesta.get("categoria") if isinstance(respuesta.get("categoria"), str) else None
    with tablas.transaccion() as cur:
        documento = _documento(cur, tenant, documento_id)
        tipo = None
        if categoria:
            cur.execute("SELECT documentTypeId, publicId FROM nexus.documentTypes WHERE tenant = ?", tenant)
            tipo = next((f[0] for f in cur.fetchall() if normalizar_nombre(f[1]) == categoria), None)
        cur.execute(
            "INSERT INTO nexus.documentClassifications (documentId, category, documentTypeId, confidence, processorId, classifiedBy) VALUES (?, ?, ?, ?, ?, ?)",
            documento, categoria[:120] if categoria else None, tipo, _confianza(respuesta.get("confianza")), procesador_id[:120], por,
        )


def registrar_extraccion(
    tenant: str,
    documento_id: object,
    tipo_id: object,
    procesador_id: str,
    version: str,
    iniciada: datetime,
    por: str,
    resultado: dict[str, Any] | None = None,
    error: str | None = None,
) -> str:
    """Guarda UNA corrida con sus datos, todo o nada, y devuelve su id (`extraccion-…`). Con `error` y sin `resultado` es una corrida fallida.

    El tipo documental tiene que ser de la organización y su procesador el que se usó: así una corrida nunca queda atribuida a un tipo que
    no la produjo."""
    tablas = _tablas()
    if not isinstance(tipo_id, str) or not tipo_id:
        raise TipoNoEncontrado(tipo_id)
    metadata = (resultado or {}).get("_metadata") or {}
    if error is not None:
        estado, renglones = "error", []
    elif metadata.get("quality_alert"):
        estado, renglones = "no_reconocido", aplanar(resultado or {})
    else:
        estado, renglones = "ok", aplanar(resultado or {})
    paginas = metadata.get("paginas_totales")
    if not isinstance(paginas, int):
        # Un documento procesado entero no trae `paginas_totales`: las cuenta su capa de OCR.
        ocr = (resultado or {}).get("ocr")
        paginas = len(ocr["paginas"]) if isinstance(ocr, dict) and isinstance(ocr.get("paginas"), list) else None
    publico = f"extraccion-{uuid.uuid4().hex}"
    with tablas.transaccion() as cur:
        documento = _documento(cur, tenant, documento_id)
        cur.execute(
            "SELECT documentTypeId, publicId, name, revision, processorId FROM nexus.documentTypes WHERE publicId = ? AND tenant = ?", tipo_id, tenant
        )
        tipo = tablas.una(cur)
        if tipo is None or (tipo["processorId"] or "") != procesador_id:
            raise TipoNoEncontrado(tipo_id)
        cur.execute(
            "INSERT INTO nexus.extractionRuns (publicId, documentId, documentTypeId, documentTypePublicId, documentTypeName, documentTypeRevision, engine, "
            "processorId, processorVersion, status, errorDetail, confidenceAverage, confidenceMinimum, pageCount, metadataJson, startedAt, finishedAt, createdBy) "
            "OUTPUT INSERTED.runId VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            publico, documento, tipo["documentTypeId"], tipo["publicId"], tipo["name"], tipo["revision"], MOTOR_DOCUMENT_AI,
            procesador_id[:120], (version or None) and version[:120], estado, error[:_MAX_DETALLE] if error is not None else None,
            _confianza((resultado or {}).get("confianza_promedio")), _confianza((resultado or {}).get("confianza_minima")),
            paginas if isinstance(paginas, int) and paginas > 0 else None,
            json.dumps(metadata, ensure_ascii=False, default=str) if metadata else None, iniciada, _ahora(), por,
        )
        corrida = cur.fetchone()[0]
        if renglones:
            columnas = list(renglones[0].keys())
            cur.fast_executemany = True
            cur.executemany(
                f"INSERT INTO nexus.entityFacts (runId, {', '.join(columnas)}) VALUES (?, {', '.join('?' * len(columnas))})",
                [(corrida, *[r[c] for c in columnas]) for r in renglones],
            )
    return publico


def corregir(tenant: str, documento_id: object, ruta: object, valor: object, motivo: object, por: str) -> dict:
    """Corrige a mano el valor VIGENTE de un campo: una fila nueva en `fieldCorrections`; el dato extraído no se toca. Devuelve el campo
    como queda."""
    tablas = _tablas()
    if not isinstance(valor, str) or not valor.strip() or len(valor) > _MAX_VALOR:
        raise CorreccionInvalida("El valor corregido no es válido.")
    if motivo is not None and (not isinstance(motivo, str) or len(motivo) > _MAX_MOTIVO):
        raise CorreccionInvalida("El motivo de la corrección no es válido.")
    with tablas.transaccion() as cur:
        documento = _documento(cur, tenant, documento_id)
        cur.execute("SELECT factId FROM nexus.vEntityFactCurrent WHERE documentId = ? AND fieldPath = ?", documento, ruta)
        fila = cur.fetchone()
        if fila is None:
            raise CampoNoEncontrado(ruta)
        cur.execute(
            "INSERT INTO nexus.fieldCorrections (factId, valueNormalized, reason, correctedBy) VALUES (?, ?, ?, ?)",
            fila[0], valor.strip(), (motivo or "").strip() or None, por,
        )
    campo = next((c for c in de_documento(tenant, documento_id)["campos"] if c["ruta"] == ruta), None)
    return campo or {}


# ── Leer ────────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _numero(valor) -> float | None:
    return float(valor) if valor is not None else None


def de_documento(tenant: str, documento_id: object) -> dict:
    """Lo que se sabe hoy de un documento: su última clasificación, su última corrida (aunque haya fallado) y los campos VIGENTES.

    Sin base, o de un documento sin nada todavía, todo viene vacío. Un documento que no es de la organización: `DocumentoNoEncontrado`."""
    vacio = {"clasificacion": None, "extraccion": None, "campos": []}
    if not SQLSERVER_HOST:
        return vacio
    tablas = _tablas()
    with tablas.lectura() as cur:
        documento = _documento(cur, tenant, documento_id)
        cur.execute(
            "SELECT TOP (1) c.category, c.confidence, c.classifiedAt, c.classifiedBy, t.publicId AS tipoId, t.name AS tipoNombre "
            "FROM nexus.documentClassifications c LEFT JOIN nexus.documentTypes t ON t.documentTypeId = c.documentTypeId "
            "WHERE c.documentId = ? ORDER BY c.classificationId DESC",
            documento,
        )
        clasificacion = tablas.una(cur)
        cur.execute(
            "SELECT TOP (1) r.publicId, r.documentTypePublicId, COALESCE(t.name, r.documentTypeName) AS tipoNombre, r.documentTypeRevision, r.processorId, "
            "r.processorVersion, r.status, r.errorDetail, r.confidenceAverage, r.confidenceMinimum, r.pageCount, r.startedAt, r.finishedAt, r.createdBy, "
            "(SELECT count(*) FROM nexus.extractionRuns x WHERE x.documentId = r.documentId) AS corridas "
            "FROM nexus.extractionRuns r LEFT JOIN nexus.documentTypes t ON t.documentTypeId = r.documentTypeId WHERE r.documentId = ? ORDER BY r.runId DESC",
            documento,
        )
        corrida = tablas.una(cur)
        cur.execute(
            "SELECT v.runId, r.publicId AS corridaId, v.fieldPath, v.fieldName, v.recordIndex, v.valueRaw, v.valueNormalized, v.valueExtracted, v.nullReason, "
            "v.confidence, v.pageNumber, v.isCorrected, v.correctedAt, v.correctedBy "
            "FROM nexus.vEntityFactCurrent v JOIN nexus.extractionRuns r ON r.runId = v.runId WHERE v.documentId = ? ORDER BY v.factId",
            documento,
        )
        campos = tablas.filas(cur)
    return {
        "clasificacion": clasificacion
        and {
            "categoria": clasificacion["category"],
            "tipoDocumental": clasificacion["tipoId"] and {"id": clasificacion["tipoId"], "nombre": clasificacion["tipoNombre"]},
            "confianza": _numero(clasificacion["confidence"]),
            "clasificadoEn": momento(clasificacion["classifiedAt"]),
            "clasificadoPor": clasificacion["classifiedBy"],
        },
        "extraccion": corrida
        and {
            "id": corrida["publicId"],
            "tipoDocumental": {"id": corrida["documentTypePublicId"], "nombre": corrida["tipoNombre"], "revision": corrida["documentTypeRevision"]},
            "procesador": {"id": corrida["processorId"], "version": corrida["processorVersion"]},
            "estado": corrida["status"],
            "error": corrida["errorDetail"],
            "confianzaPromedio": _numero(corrida["confidenceAverage"]),
            "confianzaMinima": _numero(corrida["confidenceMinimum"]),
            "paginas": corrida["pageCount"],
            "iniciadaEn": momento(corrida["startedAt"]),
            "terminadaEn": momento(corrida["finishedAt"]),
            "extraidoPor": corrida["createdBy"],
            "totalCorridas": corrida["corridas"],
        },
        # Los campos vigentes son los de la última corrida que TERMINÓ: si la última falló, `extraccion` la enseña y los campos son de antes.
        "campos": [
            {
                "ruta": c["fieldPath"],
                "nombre": c["fieldName"],
                "registro": c["recordIndex"],
                "valor": c["valueNormalized"],
                "valorCrudo": c["valueRaw"],
                "valorExtraido": c["valueExtracted"],
                "motivoSinValor": c["nullReason"],
                "confianza": _numero(c["confidence"]),
                "pagina": c["pageNumber"],
                "corregido": bool(c["isCorrected"]),
                "corregidoEn": momento(c["correctedAt"]) if c["correctedAt"] else None,
                "corregidoPor": c["correctedBy"],
                "corridaId": c["corridaId"],
            }
            for c in campos
        ],
    }


def tipos_de(tenant: str, documento_ids: list[str]) -> dict[str, dict]:
    """El tipo documental vigente de varios documentos a la vez: el de su última corrida que terminó y, si nunca se extrajo, el de su
    última clasificación. Para listados («Archivos procesados», el expediente). Sin base: `{}`."""
    ids = [d for d in documento_ids if isinstance(d, str) and _RE_DOCUMENTO.match(d)]
    if not SQLSERVER_HOST or not ids:
        return {}
    tablas = _tablas()
    resultado: dict[str, dict] = {}
    with tablas.lectura() as cur:
        for i in range(0, len(ids), 500):
            lote = ids[i : i + 500]
            cur.execute(
                "SELECT d.publicId, x.tipoId, x.tipoNombre, x.estado, x.confianza FROM nexus.documents d "
                "OUTER APPLY (SELECT TOP (1) r.documentTypePublicId AS tipoId, COALESCE(t.name, r.documentTypeName) AS tipoNombre, r.status AS estado, "
                "r.confidenceAverage AS confianza FROM nexus.extractionRuns r LEFT JOIN nexus.documentTypes t ON t.documentTypeId = r.documentTypeId "
                "WHERE r.documentId = d.documentId AND r.status <> 'error' ORDER BY r.runId DESC) x "
                f"WHERE d.tenant = ? AND d.publicId IN ({', '.join('?' * len(lote))})",
                tenant, *lote,
            )
            for f in tablas.filas(cur):
                if f["tipoId"]:
                    resultado[f["publicId"]] = {"id": f["tipoId"], "nombre": f["tipoNombre"], "origen": "extraccion", "estado": f["estado"], "confianza": _numero(f["confianza"])}
            faltan = [d for d in lote if d not in resultado]
            if faltan:
                cur.execute(
                    "SELECT d.publicId, x.tipoId, x.tipoNombre, x.confianza FROM nexus.documents d "
                    # La ÚLTIMA clasificación, aunque haya dicho `otro`: si la última opinión es «no es ninguno», no se presume uno viejo.
                    "CROSS APPLY (SELECT TOP (1) t.publicId AS tipoId, t.name AS tipoNombre, c.confidence AS confianza FROM nexus.documentClassifications c "
                    "LEFT JOIN nexus.documentTypes t ON t.documentTypeId = c.documentTypeId WHERE c.documentId = d.documentId ORDER BY c.classificationId DESC) x "
                    f"WHERE d.tenant = ? AND d.publicId IN ({', '.join('?' * len(faltan))})",
                    tenant, *faltan,
                )
                for f in tablas.filas(cur):
                    if f["tipoId"]:
                        resultado[f["publicId"]] = {"id": f["tipoId"], "nombre": f["tipoNombre"], "origen": "clasificacion", "estado": None, "confianza": _numero(f["confianza"])}
    return resultado
