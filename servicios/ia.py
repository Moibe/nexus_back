"""Servicio de IA: llamadas directas a Google Document AI.

Este archivo es el único que sabe de Document AI (autenticación, URLs de
procesador, forma de la respuesta). Los routers solo llaman funciones de dominio
como `extraer_ine` y reciben un diccionario ya limpio.

Se usa HTTP crudo con httpx + google-auth para el token, en vez del cliente
oficial `google-cloud-documentai`, para no arrastrar grpc y por consistencia con
cómo ya se llamaba Document AI en el proyecto `document_ai`.
"""

import base64
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from google.auth import default
from google.auth.transport.requests import Request

from config import (
    DOCAI_LOCATION,
    DOCAI_PROCESADOR_INE,
    DOCAI_PROJECT_ID,
    DOCAI_VERSION_INE,
    IA_TIMEOUT,
)
from servicios.ocr import bloque_de, extraer_capa_ocr

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]

# Las credenciales se cachean a nivel módulo: `default()` lee el JSON de la
# cuenta de servicio una sola vez y el token solo se renueva cuando expira.
# (Refrescar en cada request agregaría una vuelta de red a Google por documento.)
_credenciales = None


def _token() -> str:
    global _credenciales
    if _credenciales is None:
        _credenciales, _ = default(scopes=SCOPES)
    if not _credenciales.valid:
        _credenciales.refresh(Request())
    return _credenciales.token


def _url_procesador(procesador_id: str, version: str = "") -> str:
    """URL del `:process`. Con `version`, se llama a esa versión exacta del
    modelo; sin ella, a la que Google tenga como default en ese momento."""
    if not DOCAI_PROJECT_ID or not procesador_id:
        raise RuntimeError(
            "Faltan DOCAI_PROJECT_ID o el ID del procesador en el .env — revisa .env.example"
        )
    ruta = (
        f"https://{DOCAI_LOCATION}-documentai.googleapis.com/v1"
        f"/projects/{DOCAI_PROJECT_ID}/locations/{DOCAI_LOCATION}"
        f"/processors/{procesador_id}"
    )
    if version:
        ruta += f"/processorVersions/{version}"
    return ruta + ":process"


async def _procesar(
    procesador_id: str, contenido: bytes, mime_type: str, version: str = ""
) -> dict:
    """Manda el archivo a un procesador de Document AI y devuelve su JSON crudo."""
    if not version:
        logger.warning(
            "Se está llamando al procesador %s SIN fijar versión: Google usará la "
            "default, que puede cambiar sin aviso. La extracción deja de ser "
            "reproducible y engine_version no se puede registrar con certeza.",
            procesador_id,
        )
    cuerpo = {
        "rawDocument": {
            "mimeType": mime_type,
            "content": base64.b64encode(contenido).decode("utf-8"),
        }
    }
    cabeceras = {
        "Authorization": f"Bearer {_token()}",
        "Content-Type": "application/json; charset=utf-8",
    }

    async with httpx.AsyncClient(timeout=IA_TIMEOUT) as cliente:
        respuesta = await cliente.post(
            _url_procesador(procesador_id, version), headers=cabeceras, json=cuerpo
        )
        if respuesta.status_code >= 400:
            # El detalle crudo de Google puede traer IDs de proyecto/procesador,
            # así que se registra en el log pero NO se propaga al cliente.
            logger.error(
                "Document AI respondió %s: %s", respuesta.status_code, respuesta.text
            )
            raise RuntimeError(f"Document AI respondió {respuesta.status_code}")
        return respuesta.json()


# ── Parseo de la respuesta ────────────────────────────────────────────────────


def _a_cien(valor: Any) -> float | None:
    """Document AI reporta la confianza 0-1; el diccionario la quiere 0-100
    (`entity_fact.confidence numeric(5,2)`). Se convierte aquí, en la frontera."""
    if not isinstance(valor, (int, float)):
        return None
    return round(float(valor) * 100, 2)


def _valor_normalizado(entidad: dict[str, Any]) -> Any:
    """Valor tras normalizar, para `entity_fact.value_normalized`.

    Las fechas COMPLETAS se pasan a ISO (YYYY-MM-DD); todo lo demás se deja como
    el `mentionText` crudo, para no perder los ceros a la izquierda de códigos
    como localidad '0181' o municipio '064'. El texto original nunca se pierde:
    va aparte en `value_raw`.
    """
    nv = entidad.get("normalizedValue")
    if nv and "dateValue" in nv:
        dv = nv["dateValue"]
        anio, mes, dia = dv.get("year"), dv.get("month"), dv.get("day")
        if anio and mes and dia:
            return f"{anio:04d}-{mes:02d}-{dia:02d}"
    return entidad.get("mentionText")


def _rectangulo(entidad: dict[str, Any]) -> dict[str, float] | None:
    """Caja delimitadora normalizada (fracciones 0-1 del ancho/alto de la
    página, listas para posicionar con CSS en %) de dónde Document AI encontró
    el texto de esta entidad. None si la entidad no tiene posición propia — es
    el caso de una entidad compuesta como 'domicilio', que solo agrupa a otras.

    Los 4 vértices del polígono se colapsan a un rectángulo (min/max), que es lo
    que un front consume directo (left/top/width/height). Medido en una INE real:
    las ENTIDADES sí llegan alineadas a los ejes, así que aquí el colapso no
    pierde nada. No vale asumir lo mismo de los bloques de OCR — esos sí vienen
    rotados; ver la nota en `servicios.ocr._bbox`."""
    refs = entidad.get("pageAnchor", {}).get("pageRefs")
    if not refs:
        return None
    vertices = refs[0].get("boundingPoly", {}).get("normalizedVertices")
    if not vertices:
        return None
    xs = [v.get("x", 0.0) for v in vertices]
    ys = [v.get("y", 0.0) for v in vertices]
    x, y = min(xs), min(ys)
    # redondeo a 6 decimales: de sobra para posicionar en UI, y evita el ruido
    # de punto flotante que deja la resta (ej. 0.12000000000000002)
    return {
        "x": round(x, 6),
        "y": round(y, 6),
        "ancho": round(max(xs) - x, 6),
        "alto": round(max(ys) - y, 6),
    }


def _tramos(entidad: dict[str, Any]) -> list[tuple[int, int]]:
    """Tramos [inicio, fin) que ocupa la entidad dentro de `document.text`."""
    segmentos = entidad.get("textAnchor", {}).get("textSegments", [])
    return [(int(s.get("startIndex", 0)), int(s.get("endIndex", 0))) for s in segmentos]


def _campo(entidad: dict[str, Any], offsets_ocr: list[list[tuple[int, int]]]) -> dict[str, Any]:
    """Empaqueta una entidad hoja con la forma que pide `entity_fact`.

    Puntos donde esto se apega al diccionario y no a lo que da Document AI:
      - `value_raw` y `value_normalized` son DOS columnas distintas. El crudo es
        el texto tal como se leyó; el normalizado, el resultado de aplicar la
        regla del campo (hoy: fechas completas a ISO). Nunca se pisan.
      - `confianza` va 0-100 (`numeric(5,2)`), no 0-1 como la reporta Google.
        El valor original se conserva en `confianza_cruda` porque el diccionario
        lo pide explícitamente: "permite recalibrar sin re-extraer".
      - `metodo_confianza` es `extractor_confidence` — de los tres del enum, es
        el que aplica: el número lo da el extractor, no se derivó de logprobs
        ni se compuso de varias señales.
      - `bloque_indice` apunta al bloque de OCR del que salió el valor; es el
        precursor de `entity_fact.ocr_block_id`.
    """
    crudo = entidad.get("mentionText")
    return {
        "value_raw": crudo,
        "value_normalized": _valor_normalizado(entidad),
        "confianza": _a_cien(entidad.get("confidence")),
        "confianza_cruda": entidad.get("confidence"),
        "metodo_confianza": "extractor_confidence",
        "page_number": _pagina_de(entidad),
        "bloque_indice": bloque_de(offsets_ocr, _tramos(entidad)),
        "posicion": _rectangulo(entidad),
    }


def _pagina_de(entidad: dict[str, Any]) -> int | None:
    """Página donde cae la entidad (1-based). El diccionario la guarda en
    `entity_fact.page_number` aunque sea redundante con el bloque, porque hace
    falta cuando `ocr_block_id` viene en NULL."""
    refs = entidad.get("pageAnchor", {}).get("pageRefs")
    if not refs:
        return None
    # Document AI omite `page` cuando es la primera página.
    return int(refs[0].get("page", 0)) + 1


def _extraer_entidades(
    entidades: list[dict[str, Any]], offsets_ocr: list[list[tuple[int, int]]]
) -> dict[str, Any]:
    """Convierte las entidades de Document AI en un diccionario, CONSERVANDO la
    jerarquía: una entidad con sub-propiedades (ej. 'domicilio') queda como
    sub-diccionario de campos, para que no choquen llaves repetidas con el nivel
    raíz (ej. 'estado' dentro de domicilio vs. 'estado' suelto). Cada campo hoja
    trae la forma de `entity_fact` — ver `_campo`."""
    resultado: dict[str, Any] = {}
    for entidad in entidades:
        nombre = entidad.get("type")
        if not nombre:
            continue
        if entidad.get("properties"):
            resultado[nombre] = _extraer_entidades(entidad["properties"], offsets_ocr)
        else:
            campo = _campo(entidad, offsets_ocr)
            if campo["value_normalized"] is not None:
                resultado[nombre] = campo
    return resultado


def _limpiar_ine(datos: dict[str, Any]) -> dict[str, Any]:
    """Quita artefactos de puntuación que el OCR arrastra del renglón de
    domicilio impreso en la credencial: el estado trae punto final ('SON.') y la
    localidad trae coma final ('HUATABAMPO,').

    Se toca SOLO `value_normalized`. El `value_raw` conserva el punto y la coma
    tal como venían — eso es lo que significa "crudo" en el diccionario, y sin
    él no se podría auditar qué leyó realmente el OCR contra qué se guardó."""
    domicilio = datos.get("domicilio")
    if not isinstance(domicilio, dict):
        return datos

    for llave, sobrante in (("estado", "."), ("localidad", ",")):
        campo = domicilio.get(llave)
        if isinstance(campo, dict) and isinstance(campo.get("value_normalized"), str):
            campo["value_normalized"] = campo["value_normalized"].rstrip(sobrante).strip()
    return datos


def _confianza_minima(datos: dict[str, Any]) -> float | None:
    """La menor `confianza` (escala 0-100) entre TODOS los campos extraídos,
    recorriendo también los de 'domicilio'. Sirve de semáforo de un vistazo: un
    solo campo mal leído (ej. la CURP) puede pasar desapercibido en un promedio
    si el resto de la credencial salió perfecto; el mínimo no lo deja esconderse.

    Se calcula ANTES de agregar la capa de OCR al resultado, a propósito: los
    bloques de OCR también traen `confianza`, y son la confianza de LECTURA, no
    de extracción. Mezclarlas daría un mínimo que no significa nada.

    None si no se extrajo ningún campo (Document AI puede responder 200 con
    una lista de entidades vacía si la imagen no es una INE reconocible)."""
    confianzas: list[float] = []

    def recorrer(nodo: dict[str, Any]) -> None:
        for valor in nodo.values():
            if not isinstance(valor, dict):
                continue
            if "confianza" in valor:
                confianzas.append(valor["confianza"])
            else:
                recorrer(valor)

    recorrer(datos)
    return min(confianzas) if confianzas else None


# ── Operaciones de dominio ────────────────────────────────────────────────────


# Texto de negocio para quien revise el documento (front). Deliberadamente
# distinto del detail del 502 en routers/ia.py: ese es para cuando la llamada a
# Document AI MISMA falla (credenciales, cuota, red) — un error de servicio, no
# de calidad del documento. No compartir la misma redacción entre los dos.
_MOTIVO_NO_LEGIBLE = "Calidad del OCR inferior al umbral requerido o nulo"


async def extraer_ine(contenido: bytes, mime_type: str = "image/jpeg") -> dict[str, Any]:
    """Extrae los datos de una credencial INE.

    La respuesta tiene la forma del diccionario de datos v0.4, para que
    guardarla sea mecánico cuando exista SQL Server:

      - cada campo hoja es un `entity_fact` en potencia: `value_raw`,
        `value_normalized`, `confianza` (0-100), `confianza_cruda`,
        `metodo_confianza`, `page_number`, `bloque_indice` y `posicion`.
      - `ocr` es la cabecera tipo `ocr_result` con sus `bloques` (`ocr_block`),
        que es lo que permite resaltar en la imagen la región de cada dato.
        `bloque_indice` de cada campo apunta ahí por posición en la lista.
      - `confianza_minima` (0-100) resume la calidad de la extracción — ver
        `_confianza_minima`.

    Lo que TODAVÍA no cumple del diccionario, porque necesita la base: los
    campos ausentes deberían generar renglón con `null_reason`, y para saber
    cuáles se esperaban hace falta el catálogo `field_definition`. Hoy un campo
    que Document AI no encontró simplemente no aparece. `_metadata.procesado_en` es la fecha/hora (UTC) en que
    ESTE llamado a Document AI terminó — no confundir con `fecha_registro`, que
    es un campo de la credencial (la fecha impresa en la INE).

    `_metadata.quality_alert` es True SOLO cuando Document AI respondió 200
    pero sin ninguna estructura de documento reconocible (la imagen no es una
    INE, o es ilegible al punto de no reconocerse como una) — ahí viene junto
    con `_metadata.motivo`, `confianza_minima` en None y ningún campo del
    documento. En cualquier otro caso, `quality_alert` es False y `motivo` NI
    SIQUIERA aparece en el diccionario.

    OJO: esto es DISTINTO de que la llamada a Document AI misma falle (ver
    `_procesar` — credenciales, cuota, red, Google caído). Ese caso sigue
    siendo un error HTTP real (502, ver routers/ia.py) y NO se convierte en
    quality_alert: ahí el problema es del servicio, no del documento, y
    esconderlo detrás de una bandera de "calidad" ocultaría una falla operativa
    real a cualquier monitoreo que vigile el status code."""
    crudo = await _procesar(DOCAI_PROCESADOR_INE, contenido, mime_type, DOCAI_VERSION_INE)
    # Se captura aquí, justo al terminar la llamada real a Document AI, porque
    # es el ÚNICO lugar donde este dato existe: Google no manda un timestamp de
    # procesamiento en su respuesta (document.keys() no trae ninguno). Si en vez
    # de esto se generara más tarde -p.ej. cuando el front guarde los datos en
    # SQL Server-, dejaría de significar "cuándo procesó Document AI" y pasaría
    # a significar "cuándo se guardó", que puede ser minutos u horas después si
    # alguien revisa campos de baja confianza antes de confirmar.
    procesado_en = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    metadata: dict[str, Any] = {
        "procesado_en": procesado_en,
        "quality_alert": False,
        # Precursores de extraction_run.engine / engine_version. La versión es
        # None cuando no se fijó en el .env: ahí Google usó su default y NO
        # sabemos cuál fue — se prefiere decir "no sé" antes que registrar una
        # suposición en una columna que existe para dar reproducibilidad.
        "engine": "document_ai_extractor",
        "engine_version": DOCAI_VERSION_INE or None,
    }

    entidades = crudo.get("document", {}).get("entities")
    if entidades is None:
        # Ya NO se levanta excepción: esto es un resultado de negocio válido
        # (foto ilegible / no es una INE), no un bug de la app ni una falla del
        # servicio — se loguea como warning, no como error, para no ensuciar los
        # logs de errores reales con cada foto mala que suba alguien.
        logger.warning(
            "La respuesta de Document AI no trae document.entities: se marca "
            "quality_alert (imagen no reconocida como INE)."
        )
        metadata["quality_alert"] = True
        metadata["motivo"] = _MOTIVO_NO_LEGIBLE
        return {"confianza_minima": None, "_metadata": metadata}

    # La capa de OCR se arma ANTES que los campos, porque cada campo necesita
    # saber a qué bloque apuntar. Los offsets no se publican: son solo el
    # cruce interno entidad↔bloque.
    capa_ocr, offsets_ocr = extraer_capa_ocr(
        crudo.get("document", {}), DOCAI_VERSION_INE or None
    )

    datos = _limpiar_ine(_extraer_entidades(entidades, offsets_ocr))
    datos["confianza_minima"] = _confianza_minima(datos)
    datos["ocr"] = capa_ocr
    # Bajo su propia llave y no como hermano de los campos del documento: esto
    # no es un dato DE la credencial, es del request. El front debe cargarlo tal
    # cual hasta que se guarde en SQL Server, sin regenerarlo en ese momento.
    datos["_metadata"] = metadata
    return datos
