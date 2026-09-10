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
import re
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

import httpx
from google.auth import default
from google.auth.transport.requests import Request

from config import (
    DOCAI_CLASIFICADOR_ID,
    DOCAI_LOCATION,
    DOCAI_PROCESADOR_INE,
    DOCAI_PROJECT_ID,
    DOCAI_VERSION_CLASIFICADOR,
    DOCAI_VERSION_INE,
    IA_TIMEOUT,
)
from errores import ErrorDocumentAI
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


def _motivo_de(status_code: int, detalle: str) -> str:
    """Clasifica el error de Document AI en una causa accionable.

    Los textos NO son inventados: se midieron mandándole entradas rotas a la
    API real (2026-09-06). Verbatim de cada uno:

      - límite: "Document pages in non-imageless mode exceed the limit: 15 got
        28. Try using imageless mode to increase the limit to 30."
      - cuota:  "Quota exceeded for quota metric 'Number of online process
        document pages (US)' and limit '... per minute ...'"
      - PDF:    "PDF document could not be opened or is corrupted (possible
        reasons: corrupt document structure, damaged cross-reference table,
        truncated file, or unsupported encryption)."
      - imagen: "Invalid image content"

    Se busca por SUBCADENA y no por igualdad porque los mensajes traen números
    y nombres de cuota variables. Si Google reescribe estos textos, la
    consecuencia es que el motivo cae en `desconocido` y el usuario ve el
    mensaje genérico de siempre — se degrada, no se rompe.
    """
    t = (detalle or "").lower()
    if status_code == 429 or "quota exceeded" in t:
        return "cuota"
    if "exceed the limit" in t and "pages" in t:
        return "limite_paginas"
    if "could not be opened or is corrupted" in t or "invalid image content" in t:
        return "archivo_ilegible"
    if status_code >= 500:
        return "servicio"
    return "desconocido"


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
    procesador_id: str,
    contenido: bytes,
    mime_type: str,
    version: str = "",
    opciones: dict[str, Any] | None = None,
) -> dict:
    """Manda el archivo a un procesador de Document AI y devuelve su JSON crudo.

    `opciones` se mezcla en el cuerpo del request tal cual (`imagelessMode`,
    `processOptions`, etc. — todos campos de `ProcessRequest`). Se deja
    genérico en vez de un parámetro por opción porque quien sabe qué opciones
    necesita es cada función de dominio, no esta.
    """
    if not version:
        logger.warning(
            "Se está llamando al procesador %s SIN fijar versión: Google usará la "
            "default, que puede cambiar sin aviso. La extracción deja de ser "
            "reproducible y engine_version no se puede registrar con certeza.",
            procesador_id,
        )
    cuerpo: dict[str, Any] = {
        "rawDocument": {
            "mimeType": mime_type,
            "content": base64.b64encode(contenido).decode("utf-8"),
        }
    }
    if opciones:
        cuerpo.update(opciones)
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
            # así que se registra en el log pero NO se propaga al cliente: viaja
            # dentro de la excepción para que el router pueda clasificarlo, y es
            # el router quien decide qué texto ve el usuario.
            try:
                detalle = respuesta.json().get("error", {}).get("message", "")
            except Exception:  # noqa: BLE001
                detalle = respuesta.text[:500]
            logger.error(
                "Document AI respondió %s: %s", respuesta.status_code, respuesta.text
            )
            raise ErrorDocumentAI(
                f"Document AI respondió {respuesta.status_code}: {detalle}",
                status_code=respuesta.status_code,
                motivo=_motivo_de(respuesta.status_code, detalle),
            )
        return respuesta.json()


# ── Parseo de la respuesta ────────────────────────────────────────────────────


def _a_cien(valor: Any) -> float | None:
    """Document AI reporta la confianza 0-1; el diccionario la quiere 0-100
    (`entity_fact.confidence numeric(5,2)`). Se convierte aquí, en la frontera."""
    if not isinstance(valor, (int, float)):
        return None
    return round(float(valor) * 100, 2)


# "AÑO DE REGISTRO" es UN campo impreso en la credencial cuyo valor trae dos
# datos pegados: el año en que la persona se registró en el padrón y el número
# de emisión de la credencial. Medido sobre 44 INEs reales: 42 vienen como
# "2024 00" y 2 con basura de OCR en el separador ("2018.01", "2010, 01"), de ahí
# el `\D{0,3}` en vez de un espacio literal.
#
# Que el segundo número es el número de emisión quedó comprobado, no supuesto:
# con `00` el hueco entre año de emisión y año de registro es 0 en 19 de 19
# casos (o sea es la credencial original), y crece de forma monótona con el
# contador — 01 → 5.6 años, 02 → 12.0, 03 → 21.3. Y no puede ser el mes porque
# `00` no existe como mes.
_RE_ANIO_REGISTRO = re.compile(r"^\s*(\d{4})\D{0,3}(\d{2})\s*$")


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


def _descomponer_anio_registro(datos: dict[str, Any]) -> dict[str, Any]:
    """Parte `fecha_registro` ("2024 00") en sus dos datos reales.

    Se hace AQUÍ y no en el procesador de Document AI a propósito: en la
    credencial es un solo campo impreso, con una sola etiqueta. Pedirle al
    modelo que lo divida sería inventar una región que no existe, además de
    encargarle a un modelo generativo una partición de cadena que en código es
    determinista — el mismo razonamiento por el que `edad` se calcula y no se
    extrae.

    Cómo queda:
      - `value_raw` conserva "2024 00" ÍNTEGRO. Es lo que dice el documento y es
        lo que permite auditar la partición después.
      - `value_normalized` pasa a ser solo el año. El campo se llama
        `fecha_registro`; que su valor normalizado sea una fecha y no una cadena
        con dos cosas pegadas es lo que su nombre promete.
      - `numero_emision` aparece como llave nueva del mismo campo, en vez de
        como campo hermano, para no mostrar el mismo dato dos veces en la UI.

    Cuando exista `extractor_field_map` (sección 2.6), esta partición se declara
    ahí como configuración versionada y esta función desaparece. Mientras no
    exista, vive aquí junto a las otras limpiezas específicas de INE.
    """
    campo = datos.get("fecha_registro")
    if not isinstance(campo, dict):
        return datos
    crudo = campo.get("value_raw")
    if not isinstance(crudo, str):
        return datos
    coincidencia = _RE_ANIO_REGISTRO.match(crudo)
    if not coincidencia:
        # No se fuerza: si el OCR devolvió algo que no cuadra con el patrón, se
        # deja tal cual. Un `value_normalized` a medias sería peor que el crudo.
        return datos
    anio, emision = coincidencia.groups()
    campo["value_normalized"] = anio
    campo["numero_emision"] = emision
    return datos


def _confianzas(datos: dict[str, Any]) -> list[float]:
    """Las `confianza` (escala 0-100) de TODOS los campos extraídos, recorriendo
    también los anidados como 'domicilio'.

    Se recolectan ANTES de agregar la capa de OCR al resultado, a propósito: los
    bloques de OCR también traen `confianza`, y esa es la confianza de LECTURA,
    no de extracción. Mezclarlas daría un número que no significa nada.

    Lista vacía si no se extrajo ningún campo (Document AI puede responder 200
    con una lista de entidades vacía si la imagen no es legible)."""
    confianzas: list[float] = []

    def recorrer(nodo: dict[str, Any]) -> None:
        for valor in nodo.values():
            if not isinstance(valor, dict):
                continue
            if "confianza" in valor:
                # Se filtran los None: `_a_cien` devuelve None cuando Document AI
                # omite `confidence` en una entidad, y en Python 3 comparar None
                # con un float dentro de min() lanza TypeError. Eso convertiría
                # una extracción PERFECTA en un 502 "No se pudo procesar la
                # credencial" — el peor tipo de falla, porque el mensaje culpa al
                # documento cuando el problema es nuestro.
                if isinstance(valor["confianza"], (int, float)):
                    confianzas.append(float(valor["confianza"]))
            else:
                recorrer(valor)

    recorrer(datos)
    return confianzas


def _confianza_promedio(datos: dict[str, Any]) -> float | None:
    """El PROMEDIO de la confianza de todos los campos extraídos.

    Es lo que la interfaz muestra como "Nivel de confianza obtenida" desde el
    2026-09-10, por pedido explícito. Antes mostraba el mínimo.

    El intercambio, para que quede escrito y nadie lo redescubra: el promedio
    responde "qué tan bien salió esta extracción en conjunto", que es la
    pregunta que se hace quien mira la pantalla. El MÍNIMO respondía otra cosa,
    "cuál es el peor campo", y por eso sigue viajando en la respuesta: un solo
    campo mal leído —la CURP, digamos— se diluye en el promedio cuando el resto
    de la credencial salió perfecta, y ahí es donde el mínimo no lo deja
    esconderse. Los dos números sirven, para preguntas distintas.

    Se redondea a 2 decimales, la misma precisión con la que `_a_cien` convierte
    cada campo de la escala 0-1 de Google: publicar más decimales que los de la
    entrada sería inventar precisión.

    None si no se extrajo ningún campo."""
    confianzas = _confianzas(datos)
    if not confianzas:
        return None
    return round(sum(confianzas) / len(confianzas), 2)


def _confianza_minima(datos: dict[str, Any]) -> float | None:
    """La MENOR confianza entre todos los campos: el eslabón más débil.

    Ya no es lo que se muestra en la interfaz (ver `_confianza_promedio`), pero
    se sigue calculando y publicando: es el número que delata un campo
    concreto mal leído, y es barato conservarlo.

    None si no se extrajo ningún campo."""
    confianzas = _confianzas(datos)
    return min(confianzas) if confianzas else None


# ── Operaciones de dominio ────────────────────────────────────────────────────


# Texto de negocio para quien revise el documento (front). Deliberadamente
# distinto del detail del 502 en routers/ia.py: ese es para cuando la llamada a
# Document AI MISMA falla (credenciales, cuota, red) — un error de servicio, no
# de calidad del documento. No compartir la misma redacción entre los dos.
_MOTIVO_NO_LEGIBLE = "Calidad del OCR inferior al umbral requerido o nulo"


async def extraer_con_procesador(
    procesador_id: str,
    contenido: bytes,
    mime_type: str,
    version: str = "",
    transformar: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Extrae los campos de un documento con CUALQUIER Custom Extractor.

    Es el cuerpo genérico que antes vivía dentro de `extraer_ine`, sacado
    aparte el 2026-09-07 para que un tipo documental dado de alta desde el
    wizard pueda extraerse con SU PROPIO procesador (el que "Activar" le creó)
    y no solo con el de INE, que estaba fijo en el `.env`. Sin esto, el
    pipeline podía CLASIFICAR un documento correctamente y aun así no tener a
    dónde mandarlo.

    Todo lo que es específico de INE se quedó fuera, en `extraer_ine`, y llega
    por `transformar`: una función que recibe los campos ya aplanados y
    devuelve los campos a publicar. Se aplica ANTES de calcular
    `confianza_minima` a propósito — es el orden que ya tenía la versión de
    INE, y `_descomponer_anio_registro` parte un campo en dos, así que
    calcular la confianza antes cambiaría el conjunto de campos que se mira.

    La forma de la respuesta es la MISMA para todos los procesadores (ver el
    docstring de `extraer_ine`): campos hoja con `value_raw`/`value_normalized`
    /`confianza`/..., más `ocr`, las dos confianzas resumen y `_metadata`. Un extractor
    genérico no sabe qué campos esperar — eso lo define el esquema con el que
    se creó el procesador — así que aquí no hay ninguna limpieza por nombre de
    campo; publica lo que Document AI haya encontrado.
    """
    crudo = await _procesar(procesador_id, contenido, mime_type, version)
    procesado_en = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    metadata: dict[str, Any] = {
        "procesado_en": procesado_en,
        "quality_alert": False,
        "engine": "document_ai_extractor",
        "engine_version": version or None,
    }

    entidades = crudo.get("document", {}).get("entities")
    if entidades is None:
        logger.warning(
            "La respuesta de Document AI (procesador %s) no trae "
            "document.entities: se marca quality_alert.",
            procesador_id,
        )
        metadata["quality_alert"] = True
        metadata["motivo"] = _MOTIVO_NO_LEGIBLE
        return {"confianza_promedio": None, "confianza_minima": None, "_metadata": metadata}

    capa_ocr, offsets_ocr = extraer_capa_ocr(crudo.get("document", {}), version or None)

    datos = _extraer_entidades(entidades, offsets_ocr)
    if transformar is not None:
        datos = transformar(datos)
    # El PROMEDIO va primero porque es el que la interfaz muestra; el mínimo
    # queda como dato de apoyo. Los dos se calculan sobre `datos` ANTES de
    # meterle la capa `ocr`, que trae confianzas de otra naturaleza.
    datos["confianza_promedio"] = _confianza_promedio(datos)
    datos["confianza_minima"] = _confianza_minima(datos)
    datos["ocr"] = capa_ocr
    datos["_metadata"] = metadata
    return datos


async def extraer_ine(contenido: bytes, mime_type: str = "image/jpeg") -> dict[str, Any]:
    """Extrae los datos de una credencial INE.

    Desde el 2026-09-07 es una capa delgada sobre `extraer_con_procesador`:
    aporta el procesador y la versión fijos del `.env`, más las dos limpiezas
    que solo tienen sentido en una INE (`_limpiar_ine` y
    `_descomponer_anio_registro`). Todo lo demás —la llamada, la capa de OCR,
    `confianza_minima`, `_metadata`, el caso de "no se reconoció nada"— es
    común a cualquier extractor y vive allá.

    La respuesta tiene la forma del diccionario de datos v0.4, para que
    guardarla sea mecánico cuando exista SQL Server:

      - cada campo hoja es un `entity_fact` en potencia: `value_raw`,
        `value_normalized`, `confianza` (0-100), `confianza_cruda`,
        `metodo_confianza`, `page_number`, `bloque_indice` y `posicion`.
      - `ocr` es la cabecera tipo `ocr_result` con sus `bloques` (`ocr_block`),
        que es lo que permite resaltar en la imagen la región de cada dato.
        `bloque_indice` de cada campo apunta ahí por posición en la lista.
      - `confianza_promedio` (0-100) resume la calidad de la extracción y es
        lo que la interfaz muestra — ver `_confianza_promedio`.
      - `confianza_minima` (0-100) es el peor campo, que el promedio esconde.

    Lo que TODAVÍA no cumple del diccionario, porque necesita la base: los
    campos ausentes deberían generar renglón con `null_reason`, y para saber
    cuáles se esperaban hace falta el catálogo `field_definition`. Hoy un campo
    que Document AI no encontró simplemente no aparece. `_metadata.procesado_en` es la fecha/hora (UTC) en que
    ESTE llamado a Document AI terminó — no confundir con `fecha_registro`, que
    es un campo de la credencial (la fecha impresa en la INE).

    `_metadata.quality_alert` es True SOLO cuando Document AI respondió 200
    pero sin ninguna estructura de documento reconocible (la imagen no es una
    INE, o es ilegible al punto de no reconocerse como una) — ahí viene junto
    con `_metadata.motivo`, las dos confianzas resumen en None y ningún campo del
    documento. En cualquier otro caso, `quality_alert` es False y `motivo` NI
    SIQUIERA aparece en el diccionario.

    OJO: esto es DISTINTO de que la llamada a Document AI misma falle (ver
    `_procesar` — credenciales, cuota, red, Google caído). Ese caso sigue
    siendo un error HTTP real (502, ver routers/ia.py) y NO se convierte en
    quality_alert: ahí el problema es del servicio, no del documento, y
    esconderlo detrás de una bandera de "calidad" ocultaría una falla operativa
    real a cualquier monitoreo que vigile el status code.

    Notas que aplican a la parte común, hoy en `extraer_con_procesador`:
    `procesado_en` se captura justo al terminar la llamada real a Document AI
    porque es el ÚNICO lugar donde ese dato existe (Google no manda timestamp
    de procesamiento); generarlo más tarde —p. ej. al guardar en SQL Server—
    lo convertiría en "cuándo se guardó", que puede ser horas después. Y
    `quality_alert` NO levanta excepción: una foto ilegible es un resultado de
    negocio válido, no un bug ni una falla del servicio, así que se loguea
    como warning y no como error."""
    return await extraer_con_procesador(
        DOCAI_PROCESADOR_INE,
        contenido,
        mime_type,
        DOCAI_VERSION_INE,
        transformar=lambda datos: _descomponer_anio_registro(_limpiar_ine(datos)),
    )


# Cuántas páginas del documento ve el clasificador. NO es una optimización
# prematura: resuelve un fallo real y ahorra dinero real.
#
# El fallo: el procesamiento EN LÍNEA de Document AI topa en 15 páginas (30 en
# `imagelessMode`), y un documento más largo se rechaza con un 400 — medido con
# un PDF de 28 páginas: "Document pages in non-imageless mode exceed the limit:
# 15 got 28". Con `fromStart` el tope deja de aplicar: se midió un PDF de 70
# páginas clasificando bien, porque Google solo procesa las primeras N.
#
# El dinero: la facturación es POR PÁGINA PROCESADA. Clasificar un dictamen de
# 70 páginas cobraba 70; así cobra 2.
#
# Y es lo correcto semánticamente: para saber QUÉ es un documento basta el
# principio — el documento completo hace falta para EXTRAERLE datos, que es
# otro procesador y otra llamada. El riesgo que se acepta a cambio: un tipo
# documental que solo se distinga después de la página 2 se clasificaría mal.
# No aplica a ninguno de los tipos de hoy (INE, actas, comprobantes se
# reconocen desde la carátula); si algún día aplica, subir este número.
PAGINAS_PARA_CLASIFICAR = 2


async def clasificar_documento(contenido: bytes, mime_type: str) -> dict[str, Any]:
    """Pregunta al Custom Document Classifier a cuál tipo documental activo
    pertenece un documento entrante — el paso previo a decidir a qué extractor
    mandarlo, entre la Bandeja de preparación y el pipeline.

    A diferencia de `extraer_ine`, aquí NO hay campos que extraer: el
    Classifier solo etiqueta el documento ENTERO con una categoría (ver
    `servicios.procesadores.sincronizar_clasificador`). `categoria` es el
    `name` del EntityType que ganó — hoy eso es literal el `procesadorId` del
    Extractor al que hay que mandar el documento después (ver el docstring de
    `esquema_clasificador_desde_tipos`), EXCEPTO cuando vale `"otro"`: esa es
    la categoría explícita de "no corresponde a ningún tipo configurado", y
    ahí no hay ningún extractor al que seguir — el documento debe quedar
    marcado para revisión/configuración manual, nunca mandarse a un extractor
    de todos modos.

    `categoria` es `None` solo si Document AI respondió sin ninguna entidad —
    caso raro con la etiqueta `otro` ya en el esquema (siempre debería poder
    elegir ALGO), pero se contempla en vez de asumir que `entities` nunca
    viene vacío. El front debe tratar `None` igual que `"otro"`: sin categoría
    reconocida, sin extractor al que mandarlo.
    """
    if not DOCAI_CLASIFICADOR_ID:
        raise RuntimeError(
            "Falta DOCAI_CLASIFICADOR_ID en el .env — revisa .env.example"
        )
    crudo = await _procesar(
        DOCAI_CLASIFICADOR_ID,
        contenido,
        mime_type,
        DOCAI_VERSION_CLASIFICADOR,
        # `imagelessMode` se QUITÓ el 2026-09-08, investigando por qué el
        # clasificador no reconocía una INE. El razonamiento con el que se
        # había puesto era "las imágenes de página no se leen de la respuesta,
        # así que pedirlas es payload que se descarta; de paso sube el tope de
        # 15 a 30 páginas". Los dos motivos se cayeron al revisarlos:
        #
        #   - El tope ya lo resuelve `fromStart`, que procesa solo las
        #     primeras N. El propio comentario admitía que era "irrelevante".
        #   - Lo que el flag hace de verdad NO está documentado. La única
        #     descripción que Google publica —en v1, v1beta3 y el proto— son
        #     ocho palabras: "Optional. Option to remove images from the
        #     document.", y es ambigua entre "quita las imágenes de la
        #     RESPUESTA" y "deja de procesarlas". Las tres páginas del sitio
        #     que lo mencionan hablan solo de límites de páginas; ninguna dice
        #     nada sobre precisión ni sobre qué señales usa el modelo.
        #
        # Con esa ambigüedad, el riesgo es asimétrico: si resulta que el
        # modelo deja de ver la imagen, una credencial —que se reconoce por su
        # LAYOUT y su fotografía, con poco texto— pierde justo la señal que la
        # hace reconocible. A cambio de un ahorro de payload sobre 2 páginas.
        # No vale la pena; si algún día hace falta el tope de 30, se vuelve a
        # evaluar midiendo.
        {"processOptions": {"fromStart": PAGINAS_PARA_CLASIFICAR}},
    )
    entidades = crudo.get("document", {}).get("entities") or []
    if not entidades:
        return {"categoria": None, "confianza": None}
    mejor = max(entidades, key=lambda e: e.get("confidence") or 0)
    return {"categoria": mejor.get("type"), "confianza": _a_cien(mejor.get("confidence"))}
