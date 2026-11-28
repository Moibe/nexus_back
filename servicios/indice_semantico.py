"""El índice de la búsqueda en el texto de los documentos: guardar lo que se leyó, partirlo en fragmentos y sacarles su vector (épica 14,
HU-094, HU-095 y HU-099; migración 0022; la decisión, en docs/adr-indice-semantico.md).

## Cuándo se indexa

Cada extracción que no falla deja el documento `INDEX_PENDING` (`programar`, desde `datos_extraidos.registrar_extraccion`) y un trabajador
(un hilo que arranca con la app, como el de los webhooks) lo indexa en segundos. Una corrección a mano lo programa igual: su fragmento de
`campos` cambia. La extracción nunca espera al índice ni falla por él (HU-094 CA-2).

Además, cada `BARRIDO_CADA_S` el trabajador busca lo que se haya quedado sin programar: documentos con extracción vigente sin índice, con un
índice de otra extracción, atascados (sin intento programado y sin alerta) o con vectores de otro modelo. Así arrancan también los documentos
extraídos antes de la 0022: su texto no se guardó, así que entran solo con su fragmento de `campos`.

## Qué hace indexar

1. Lee la extracción VIGENTE del documento (la última que no falló), las líneas de su capa OCR y sus datos vigentes (con las correcciones).
2. Arma los fragmentos (`servicios/fragmentos.py`): los de texto por página y los de campos.
3. Reusa el vector de cualquier fragmento del documento con el MISMO contenido y el mismo modelo: una corrección solo vuelve a pedir el
   fragmento de campos de su página; re-extraer un documento que no cambió no le cuesta nada a nadie.
4. Pide a Google los vectores que falten (`RETRIEVAL_DOCUMENT`).
5. En UNA transacción: comprueba que la extracción vigente sigue siendo la misma, borra todos los fragmentos del documento y escribe los
   nuevos, y deja el documento `INDEXED`. Nunca quedan mezclados fragmentos de dos extracciones (HU-099).

## Cuando falla

- Google no responde (o no hay credencial): sigue `INDEX_PENDING` y se reintenta tras `ESPERAS_S`.
- La escritura en la base falló: `INDEX_INCONSISTENT` y se reintenta igual. Mientras tanto la búsqueda no enseña fragmentos de otra
  extracción, porque `consulta.vFragmento` solo devuelve los de la vigente.
- Al `MAX_INTENTOS`-ésimo fallo seguido ya no se programa otro: se anota `SEMANTIC_INDEX_ALERT` en la bitácora de auditoría y un error en el log
  (la «alerta operativa» de HU-094 CA-2 y HU-099 CA-3). Una extracción o corrección nueva vuelve a empezar la cuenta.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import threading
from array import array
from datetime import datetime, timedelta, timezone
from typing import Any

import config
from servicios import auditoria, embeddings, fragmentos

logger = logging.getLogger(__name__)

PENDIENTE, INDEXADO, INCONSISTENTE = "INDEX_PENDING", "INDEXED", "INDEX_INCONSISTENT"
MOTOR_OCR = "document_ai"

#: Intentos seguidos antes de dejar de reintentar y avisar. Lo pide HU-094 CA-2 («máximo 3 intentos»).
MAX_INTENTOS = 3
#: Espera antes del 2.º y del 3.er intento.
ESPERAS_S = (60, 600)
#: Tope de fragmentos por documento (un documento de ~1,000 páginas): más allá se indexa lo primero y se avisa en el log.
MAX_FRAGMENTOS = 2000
#: Documentos por vuelta del trabajador.
LOTE_TRABAJO = 5
BARRIDO_CADA_S = 600

TAMANO_POR_OMISION = fragmentos.TAMANO
SOLAPE_POR_OMISION = fragmentos.SOLAPE
#: HU-096 pedía 0.70. Medido el 2026-11-28 con gemini-embedding-001 real (scratchpad lectura/vectores_reales.py): lo relevante dio de 0.68 a 0.82
#: y lo que no tiene que ver hasta 0.61; con 0.70, «¿quiénes son los accionistas?» (0.679) no encontraba el acta. Configurable por organización.
MINIMO_POR_OMISION = 0.65

_MAX_ERROR = 1000
_MAX_LINEA = 4000


class _Desfasado(Exception):
    """Llegó otra extracción mientras se indexaba: lo hecho ya no sirve y se programa de nuevo, sin contar como fallo."""


def _tablas():
    from db import tablas

    return tablas


def _ahora() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def activo() -> bool:
    """¿Este proceso indexa? Necesita la base; `INDICE_SEMANTICO=off` lo apaga (las pruebas indexan a mano con `procesar_vencidos`)."""
    return bool(config.SQLSERVER_HOST) and config.INDICE_SEMANTICO_ACTIVO


# ── Vectores ↔ bytes ────────────────────────────────────────────────────────────────────────────────────────────────────


def a_bytes(vector: list[float]) -> bytes:
    """float32, little-endian: lo que guarda `documentChunks.embedding`."""
    a = array("f", vector)
    if sys.byteorder != "little":
        a.byteswap()
    return a.tobytes()


def de_bytes(datos: bytes) -> list[float]:
    a = array("f")
    a.frombytes(bytes(datos))
    if sys.byteorder != "little":
        a.byteswap()
    return a.tolist()


# ── La capa OCR (dentro de la transacción de `registrar_extraccion`) ─────────────────────────────────────────────────────


def _caja(bloque: dict[str, Any]) -> tuple[float, float, float, float] | None:
    caja = bloque.get("bbox")
    if not isinstance(caja, dict):
        return None
    try:
        x, y, w, h = (float(caja[k]) for k in ("x", "y", "ancho", "alto"))
    except (KeyError, TypeError, ValueError):
        return None
    limitar = lambda v: round(min(1.0, max(0.0, v)), 6)  # noqa: E731 — Document AI a veces se sale una milésima del borde
    return limitar(x), limitar(y), limitar(w), limitar(h)


def guardar_capa_ocr(cur, corrida: int, capa: Any) -> dict[int, int]:
    """Guarda lo que se LEYÓ en una extracción (`ocrResults` + una fila de `ocrBlocks` por línea) y devuelve `índice del bloque → ocrBlockId`, para ligar
    cada dato extraído con su línea. Sin capa (una extracción fallida, un doble de pruebas) no guarda nada."""
    if not isinstance(capa, dict):
        return {}
    paginas = capa.get("paginas") if isinstance(capa.get("paginas"), list) else []
    total = capa.get("page_count")
    total = total if isinstance(total, int) and total >= 0 else len(paginas)
    idioma = capa.get("language") if isinstance(capa.get("language"), str) else None
    version = capa.get("engine_version") if isinstance(capa.get("engine_version"), str) else None
    motor = capa.get("engine") if capa.get("engine") in ("document_ai", "azure_ocr", "native_pdf") else MOTOR_OCR
    cur.execute(
        "INSERT INTO nexus.ocrResults (runId, engine, engineVersion, pageCount, language, pagesJson) OUTPUT INSERTED.ocrResultId VALUES (?, ?, ?, ?, ?, ?)",
        corrida, motor, version[:120] if version else None, total, idioma[:20] if idioma else None,
        json.dumps(paginas, ensure_ascii=False, default=str) if paginas else None,
    )
    resultado = cur.fetchone()[0]
    filas = []
    vistos: set[int] = set()
    for bloque in capa.get("bloques") or []:
        if not isinstance(bloque, dict):
            continue
        indice, pagina, texto = bloque.get("indice"), bloque.get("page_number"), bloque.get("texto")
        caja = _caja(bloque)
        if not isinstance(indice, int) or indice < 0 or indice in vistos or not isinstance(pagina, int) or pagina < 1 or caja is None:
            continue
        if not isinstance(texto, str):
            texto = ""
        confianza = bloque.get("confianza")
        confianza = round(float(confianza), 2) if isinstance(confianza, (int, float)) and 0 <= confianza <= 100 else None
        rotacion = bloque.get("orientacion_grados") if bloque.get("orientacion_grados") in (0, 90, 180, 270) else 0
        vistos.add(indice)
        filas.append((resultado, indice, pagina, "line", texto[:_MAX_LINEA], *caja, confianza, rotacion))
    if filas:
        cur.fast_executemany = True
        cur.executemany(
            "INSERT INTO nexus.ocrBlocks (ocrResultId, blockIndex, pageNumber, blockType, text, bboxX, bboxY, bboxW, bboxH, confidence, rotation) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            filas,
        )
        cur.fast_executemany = False
        cur.execute("SELECT blockIndex, ocrBlockId FROM nexus.ocrBlocks WHERE ocrResultId = ?", resultado)
        return {indice: bloque for indice, bloque in cur.fetchall()}
    return {}


# ── Lo configurable por organización ────────────────────────────────────────────────────────────────────────────────────


def ajustes(tenant: str) -> dict[str, Any]:
    """`{tamano, solape, minimo}` de la organización: su renglón de `semanticSearchSettings` o lo de siempre (512, 64, 0.65)."""
    tablas = _tablas()
    with tablas.lectura() as cur:
        cur.execute("SELECT chunkTokens, overlapTokens, minScore FROM nexus.semanticSearchSettings WHERE tenant = ?", tenant)
        fila = cur.fetchone()
    if fila is None:
        return {"tamano": TAMANO_POR_OMISION, "solape": SOLAPE_POR_OMISION, "minimo": MINIMO_POR_OMISION}
    return {"tamano": int(fila[0]), "solape": int(fila[1]), "minimo": float(fila[2])}


# ── Programar ───────────────────────────────────────────────────────────────────────────────────────────────────────────


def programar(documento: int, tenant: str, corrida: int | None = None) -> None:
    """Deja el documento `INDEX_PENDING` para ya, con la cuenta de intentos en cero. `corrida`: la extracción nueva (sin ella, la que ya tenía).

    Quien llama ya guardó lo suyo: si esto falla se avisa en el log y el barrido lo recoge después."""
    tablas = _tablas()
    with tablas.transaccion() as cur:
        cur.execute(
            "UPDATE nexus.documentIndexes SET status = ?, runId = COALESCE(?, runId), attempts = 0, nextAttemptAt = SYSUTCDATETIME(), lastError = NULL, "
            "alertedAt = NULL, updatedAt = SYSUTCDATETIME() WHERE documentId = ?",
            PENDIENTE, corrida, documento,
        )
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO nexus.documentIndexes (documentId, tenant, status, runId, attempts, nextAttemptAt) VALUES (?, ?, ?, ?, 0, SYSUTCDATETIME())",
                documento, tenant, PENDIENTE, corrida,
            )
    _despertar.set()


def programar_seguro(documento: int, tenant: str, corrida: int | None = None) -> None:
    """`programar` sin que un error llegue a quien llama (una extracción o una corrección ya guardadas no fallan por el índice)."""
    if not config.SQLSERVER_HOST:
        return
    try:
        programar(documento, tenant, corrida)
    except Exception:  # noqa: BLE001
        logger.warning("No se pudo programar el índice del documento %s; el barrido lo recogerá.", documento, exc_info=True)


def barrer(limite: int = 500) -> int:
    """Programa lo que se quedó sin programar (ver el encabezado). Devuelve cuántos documentos programó."""
    tablas = _tablas()
    with tablas.transaccion() as cur:
        cur.execute(
            "SELECT TOP (?) d.documentId, d.tenant, v.runId, i.documentId AS conIndice "
            "FROM nexus.documents d "
            "CROSS APPLY (SELECT MAX(r.runId) AS runId FROM nexus.extractionRuns r WHERE r.documentId = d.documentId AND r.status <> 'error') v "
            "LEFT JOIN nexus.documentIndexes i ON i.documentId = d.documentId "
            "WHERE v.runId IS NOT NULL AND ("
            "  i.documentId IS NULL"
            "  OR (i.nextAttemptAt IS NULL AND i.alertedAt IS NULL AND (i.status <> ? OR i.runId <> v.runId))"
            "  OR (i.status = ? AND i.nextAttemptAt IS NULL AND EXISTS ("
            "      SELECT 1 FROM nexus.documentChunks c WHERE c.documentId = d.documentId AND c.embeddingModel <> ?))"
            ") ORDER BY d.documentId",
            limite, INDEXADO, INDEXADO, embeddings.etiqueta(),
        )
        candidatos = cur.fetchall()
        for documento, tenant, corrida, con_indice in candidatos:
            if con_indice is None:
                cur.execute(
                    "INSERT INTO nexus.documentIndexes (documentId, tenant, status, runId, attempts, nextAttemptAt) VALUES (?, ?, ?, ?, 0, SYSUTCDATETIME())",
                    documento, tenant, PENDIENTE, corrida,
                )
            else:
                cur.execute(
                    "UPDATE nexus.documentIndexes SET status = CASE WHEN status = ? THEN ? ELSE status END, runId = ?, nextAttemptAt = SYSUTCDATETIME(), "
                    "updatedAt = SYSUTCDATETIME() WHERE documentId = ?",
                    INDEXADO, PENDIENTE, corrida, documento,
                )
    if candidatos:
        logger.info("Índice semántico: el barrido programó %s documento(s).", len(candidatos))
    return len(candidatos)


# ── Indexar un documento ────────────────────────────────────────────────────────────────────────────────────────────────


def _nombres_en_pantalla(data_json: str | None) -> dict[str, str]:
    """Ruta en lo extraído sin filas (`ano_de_registro`, `socios.nombre`) → el nombre que se le dio en el tipo documental."""
    from servicios.esquema import TIPO_GRUPO, nombres_en_google

    try:
        campos = (json.loads(data_json) if data_json else {}).get("campos") or []
    except (ValueError, AttributeError):
        return {}
    campos = [c for c in campos if isinstance(c, dict)] if isinstance(campos, list) else []
    salida: dict[str, str] = {}
    grupos: dict[str, str] = {}
    for campo in campos:
        if campo.get("tipoDato") == TIPO_GRUPO:
            grupos[campo.get("nombre", "")] = campo.get("nombre", "")
    for pantalla, (grupo, nombre) in nombres_en_google(campos).items():
        if grupo is None:
            salida[nombre] = pantalla
        else:
            prefijo = next((g for g in grupos if pantalla.startswith(g + ".")), None)
            salida[f"{grupo}.{nombre}"] = pantalla[len(prefijo) + 1 :] if prefijo else pantalla
    return salida


def _deseados(tenant: str, lineas: list[tuple[int, str]], hechos: list[dict[str, Any]], tipo: str | None, data_json: str | None) -> list[dict[str, Any]]:
    a = ajustes(tenant)
    por_pagina: dict[int, list[str]] = {}
    for pagina, texto in lineas:
        por_pagina.setdefault(int(pagina), []).append(texto or "")
    texto = [{**f, "clase": "texto"} for f in fragmentos.fragmentos_de_texto(por_pagina, a["tamano"], a["solape"])]
    campos = [{**f, "clase": "campos"} for f in fragmentos.fragmentos_de_campos(hechos, _nombres_en_pantalla(data_json), tipo, a["tamano"], a["solape"])]
    if len(texto) + len(campos) > MAX_FRAGMENTOS:
        logger.warning("Índice semántico: %s fragmentos en un documento; se indexan los primeros %s.", len(texto) + len(campos), MAX_FRAGMENTOS)
        texto = texto[: max(0, MAX_FRAGMENTOS - len(campos))]
        campos = campos[:MAX_FRAGMENTOS]
    return texto + campos


def indexar(documento: int) -> dict[str, Any]:
    """Indexa UN documento (ver el encabezado). Levanta `embeddings.SinEmbeddings` si Google falla, `_Desfasado` si llegó otra extracción, y los
    errores de la base tal cual: quien llama (`_procesar`) decide qué estado deja."""
    tablas = _tablas()
    etiqueta = embeddings.etiqueta()
    with tablas.lectura() as cur:
        cur.execute("SELECT tenant, publicId FROM nexus.documents WHERE documentId = ?", documento)
        doc = tablas.una(cur)
        if doc is None:
            return {"estado": "sin_documento"}
        cur.execute(
            "SELECT TOP (1) r.runId, COALESCE(t.name, r.documentTypeName) AS tipo, t.dataJson FROM nexus.extractionRuns r "
            "LEFT JOIN nexus.documentTypes t ON t.documentTypeId = r.documentTypeId WHERE r.documentId = ? AND r.status <> 'error' ORDER BY r.runId DESC",
            documento,
        )
        corrida = tablas.una(cur)
        if corrida is not None:
            cur.execute(
                "SELECT b.pageNumber, b.text FROM nexus.ocrBlocks b JOIN nexus.ocrResults o ON o.ocrResultId = b.ocrResultId WHERE o.runId = ? "
                "ORDER BY b.pageNumber, b.blockIndex",
                corrida["runId"],
            )
            lineas = cur.fetchall()
            cur.execute(
                "SELECT fieldPath AS ruta, valueNormalized AS valor, pageNumber AS pagina FROM nexus.vEntityFactCurrent "
                "WHERE documentId = ? AND valueNormalized IS NOT NULL ORDER BY factId",
                documento,
            )
            hechos = tablas.filas(cur)
            cur.execute("SELECT content, embedding FROM nexus.documentChunks WHERE documentId = ? AND embeddingModel = ?", documento, etiqueta)
            ya = {contenido: bytes(vector) for contenido, vector in cur.fetchall()}
        cur.execute("SELECT runId FROM nexus.documentIndexes WHERE documentId = ?", documento)
        fila = cur.fetchone()
        anterior = fila[0] if fila else None
        cur.execute("SELECT DISTINCT runId FROM nexus.documentChunks WHERE documentId = ?", documento)
        corridas_previas = [r[0] for r in cur.fetchall()]

    if corrida is None:
        # Sin extracción vigente no hay nada que buscar: se va lo que hubiera.
        with tablas.transaccion() as cur:
            cur.execute("DELETE FROM nexus.documentChunks WHERE documentId = ?", documento)
            cur.execute("DELETE FROM nexus.documentIndexes WHERE documentId = ?", documento)
        return {"estado": "sin_extraccion"}

    deseados = _deseados(doc["tenant"], lineas, hechos, corrida["tipo"], corrida["dataJson"])
    faltan = list(dict.fromkeys(f["texto"] for f in deseados if f["texto"] not in ya))
    tokens = 0
    if faltan:
        vectores, tokens = asyncio.run(embeddings.vectores(faltan, embeddings.TAREA_DOCUMENTO))
        ya.update({texto: a_bytes(v) for texto, v in zip(faltan, vectores)})

    orden = {"texto": 0, "campos": 0}
    filas = []
    for f in deseados:
        filas.append((doc["tenant"], documento, corrida["runId"], f["clase"], f["pagina"], orden[f["clase"]], f["texto"], f["tokens"], ya[f["texto"]], etiqueta))
        orden[f["clase"]] += 1

    with tablas.transaccion() as cur:
        cur.execute("SELECT MAX(runId) FROM nexus.extractionRuns WITH (UPDLOCK, HOLDLOCK) WHERE documentId = ? AND status <> 'error'", documento)
        if cur.fetchone()[0] != corrida["runId"]:
            raise _Desfasado()
        cur.execute("DELETE FROM nexus.documentChunks WHERE documentId = ?", documento)
        if filas:
            cur.executemany(
                "INSERT INTO nexus.documentChunks (tenant, documentId, runId, kind, pageNumber, chunkIndex, content, tokenCount, embedding, embeddingModel) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                filas,
            )
        cur.execute(
            "UPDATE nexus.documentIndexes SET status = ?, runId = ?, chunkCount = ?, attempts = 0, nextAttemptAt = NULL, lastError = NULL, alertedAt = NULL, "
            "indexedAt = SYSUTCDATETIME(), updatedAt = SYSUTCDATETIME() WHERE documentId = ?",
            INDEXADO, corrida["runId"], len(filas), documento,
        )
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO nexus.documentIndexes (documentId, tenant, status, runId, chunkCount, attempts, indexedAt) VALUES (?, ?, ?, ?, ?, 0, SYSUTCDATETIME())",
                documento, doc["tenant"], INDEXADO, corrida["runId"], len(filas),
            )

    otras = sorted({r for r in corridas_previas if r != corrida["runId"]} | ({anterior} if anterior not in (None, corrida["runId"]) else set()))
    resumen = {
        "estado": "indexado",
        "documento": doc["publicId"],
        "fragmentos": len(filas),
        "deTexto": orden["texto"],
        "deCampos": orden["campos"],
        "vectoresNuevos": len(faltan),
        "reusados": len(filas) - sum(1 for f in deseados if f["texto"] in faltan),
        "tokens": tokens,
    }
    _anotar(
        auditoria.SEMANTIC_REINDEXED if otras else auditoria.SEMANTIC_INDEXED,
        tenant=doc["tenant"], documento=doc["publicId"], corrida=_publico_de_corrida(corrida["runId"]),
        **({"corridasAnteriores": [_publico_de_corrida(r) for r in otras]} if otras else {}),
        fragmentos=len(filas), vectoresNuevos=len(faltan),
    )
    return resumen


def _publico_de_corrida(run_id: int) -> str | None:
    tablas = _tablas()
    try:
        with tablas.lectura() as cur:
            cur.execute("SELECT publicId FROM nexus.extractionRuns WHERE runId = ?", run_id)
            fila = cur.fetchone()
        return fila[0] if fila else None
    except Exception:  # noqa: BLE001
        return None


def _anotar(accion: str, **datos) -> None:
    try:
        auditoria.registrar(accion, **datos)
    except Exception as exc:  # noqa: BLE001 — el rastro nunca tumba el índice
        logger.warning("No se pudo anotar %s en la bitácora de auditoría: %s", accion, exc)


def _fallo(documento: int, estado: str, error: str) -> None:
    """Cuenta un intento fallido: programa el siguiente o, al `MAX_INTENTOS`-ésimo, deja de intentar y avisa."""
    tablas = _tablas()
    with tablas.transaccion() as cur:
        cur.execute("SELECT tenant, attempts FROM nexus.documentIndexes WHERE documentId = ?", documento)
        fila = cur.fetchone()
        if fila is None:
            return
        tenant, intentos = fila[0], int(fila[1]) + 1
        rendirse = intentos >= MAX_INTENTOS
        siguiente = None if rendirse else _ahora() + timedelta(seconds=ESPERAS_S[min(intentos, len(ESPERAS_S)) - 1])
        cur.execute(
            "UPDATE nexus.documentIndexes SET status = ?, attempts = ?, nextAttemptAt = ?, lastError = ?, alertedAt = ?, updatedAt = SYSUTCDATETIME() "
            "WHERE documentId = ?",
            estado, intentos, siguiente, error[:_MAX_ERROR], _ahora() if rendirse else None, documento,
        )
        cur.execute("SELECT publicId FROM nexus.documents WHERE documentId = ?", documento)
        publico = (cur.fetchone() or [None])[0]
    if rendirse:
        logger.error("Índice semántico: el documento %s falló %s veces seguidas (%s) y ya no se reintenta solo: %s", publico, intentos, estado, error)
        _anotar(auditoria.SEMANTIC_INDEX_ALERT, tenant=tenant, documento=publico, estado=estado, intentos=intentos, error=error[:300])
    else:
        logger.warning("Índice semántico: falló el documento %s (intento %s de %s, %s): %s", publico, intentos, MAX_INTENTOS, estado, error)


def _procesar(documento: int) -> str:
    """Indexa uno y deja su estado según cómo salió. Devuelve `indexado`, `desfasado`, `pendiente` o `inconsistente`."""
    try:
        indexar(documento)
        return "indexado"
    except _Desfasado:
        programar_seguro(documento, _tenant_de(documento) or "", None)
        return "desfasado"
    except embeddings.SinEmbeddings as exc:
        _fallo(documento, PENDIENTE, f"vectores: {exc}")
        return "pendiente"
    except Exception as exc:  # noqa: BLE001 — la base u otra cosa: los fragmentos pueden no corresponder a la extracción vigente
        logger.exception("Índice semántico: falló al escribir el documento %s", documento)
        try:
            _fallo(documento, INCONSISTENTE, f"{type(exc).__name__}: {exc}")
        except Exception:  # noqa: BLE001
            logger.exception("Índice semántico: tampoco se pudo anotar el fallo del documento %s", documento)
        return "inconsistente"


def _tenant_de(documento: int) -> str | None:
    tablas = _tablas()
    with tablas.lectura() as cur:
        cur.execute("SELECT tenant FROM nexus.documents WHERE documentId = ?", documento)
        fila = cur.fetchone()
    return fila[0] if fila else None


def procesar_vencidos(limite: int = LOTE_TRABAJO) -> list[tuple[int, str]]:
    """Toma hasta `limite` documentos a los que ya les toca y los indexa. Devuelve `(documentId, cómo salió)` de cada uno."""
    tablas = _tablas()
    with tablas.transaccion() as cur:
        cur.execute(
            "UPDATE TOP (?) nexus.documentIndexes SET nextAttemptAt = NULL, updatedAt = SYSUTCDATETIME() OUTPUT INSERTED.documentId "
            "WHERE nextAttemptAt IS NOT NULL AND nextAttemptAt <= SYSUTCDATETIME()",
            limite,
        )
        tomados = [f[0] for f in cur.fetchall()]
    return [(d, _procesar(d)) for d in tomados]


# ── El trabajador ───────────────────────────────────────────────────────────────────────────────────────────────────────

_hilo: threading.Thread | None = None
_detener = threading.Event()
_despertar = threading.Event()


def _bucle() -> None:
    ultimo_barrido = 0.0
    import time

    while not _detener.is_set():
        try:
            if time.monotonic() - ultimo_barrido >= BARRIDO_CADA_S:
                ultimo_barrido = time.monotonic()
                barrer()
            while procesar_vencidos() and not _detener.is_set():
                pass
        except Exception:  # noqa: BLE001 — una vuelta que falla no debe matar al trabajador
            logger.exception("Falló una vuelta del trabajador del índice semántico")
        _despertar.wait(timeout=2.0)
        _despertar.clear()


def iniciar() -> None:
    """Arranca el trabajador. La llama `app.py` al arrancar."""
    global _hilo
    if not activo():
        logger.warning("Índice semántico APAGADO (sin base o INDICE_SEMANTICO=off): este proceso no indexa documentos.")
        return
    if _hilo is not None and _hilo.is_alive():
        return
    _detener.clear()
    _hilo = threading.Thread(target=_bucle, name="indice-semantico", daemon=True)
    _hilo.start()


def detener() -> None:
    _detener.set()
    _despertar.set()
    if _hilo is not None:
        _hilo.join(timeout=15)


# ── Cómo va el índice de una organización ───────────────────────────────────────────────────────────────────────────────


def estado(tenant: str) -> dict[str, Any]:
    """Cuántos documentos de la organización están indexados, pendientes, inconsistentes o con alerta, y cuántos fragmentos hay."""
    tablas = _tablas()
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT status, COUNT(*), SUM(CASE WHEN alertedAt IS NOT NULL THEN 1 ELSE 0 END), SUM(COALESCE(chunkCount, 0)) "
            "FROM nexus.documentIndexes WHERE tenant = ? GROUP BY status",
            tenant,
        )
        filas = cur.fetchall()
    salida = {"indexados": 0, "pendientes": 0, "inconsistentes": 0, "conAlerta": 0, "fragmentos": 0}
    for status, n, alertas, chunks in filas:
        if status == INDEXADO:
            salida["indexados"] += int(n)
            salida["fragmentos"] += int(chunks or 0)
        elif status == PENDIENTE:
            salida["pendientes"] += int(n)
        else:
            salida["inconsistentes"] += int(n)
        salida["conAlerta"] += int(alertas or 0)
    return salida
