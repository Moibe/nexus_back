"""Servicio de IA: llamadas directas a Google Document AI.

Este archivo es el único que sabe de Document AI (autenticación, URLs de
procesador, forma de la respuesta). Los routers solo llaman funciones de dominio
como `extraer_ine` y reciben un diccionario ya limpio.

Se usa HTTP crudo con httpx + google-auth para el token, en vez del cliente
oficial `google-cloud-documentai`, para no arrastrar grpc y por consistencia con
cómo ya se llamaba Document AI en el proyecto `document_ai`.
"""

import asyncio
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
    DOCAI_LOCATION,
    DOCAI_PROCESADOR_INE,
    DOCAI_PROJECT_ID,
    DOCAI_VERSION_CLASIFICADOR,
    DOCAI_VERSION_INE,
    EXTRACCION_MAX_PAGINAS,
    EXTRACCION_PAGINAS_POR_BLOQUE,
    EXTRACCION_SOLAPE_PAGINAS,
    IA_TIMEOUT,
)
from errores import ErrorDocumentAI, SinClasificador
from servicios import clasificadores
from servicios.bloques import Parcial, combinar_datos, es_campo, planear_bloques, remapear_bloques, unir_capas_ocr
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


def _motivo_de(status_code: int, detalle: str, reason: str = "") -> str:
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

    Y SÍ los reescribe: el del límite de páginas ya cambió desde que se midió.
    Hoy (2026-09-21) responde "Document pages exceed the limit: 15 got 20", sin
    la parte de imageless mode. La subcadena que se busca aquí sobrevivió el
    cambio, pero fue suerte, no diseño.

    Por eso `reason` manda sobre el texto cuando viene: la respuesta de error
    trae un bloque `error.details[]` con un campo estable, que es parte del
    contrato de la API y no depende de cómo esté redactado el mensaje:

        {"reason": "PAGE_LIMIT_EXCEEDED",
         "metadata": {"page_limit": "15", "pages": "20"}}

    De ese bloque solo está CONFIRMADO contra la API real
    `PAGE_LIMIT_EXCEEDED`; los demás se emparejan por subcadena
    (`RATE_LIMIT`/`QUOTA`) a modo de apuesta barata, porque si no coinciden cae
    igual en el texto de respaldo y no se pierde nada.
    """
    r = (reason or "").upper()
    if r == "PAGE_LIMIT_EXCEEDED":
        return "limite_paginas"
    if "RATE_LIMIT" in r or "QUOTA" in r:
        return "cuota"

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


_RE_LIMITE_PAGINAS = re.compile(r"limit:\s*(\d+)\s+got\s+(\d+)", re.IGNORECASE)


def _paginas_del_error(detalles: list[dict[str, Any]], mensaje: str) -> tuple[int | None, int | None]:
    """`(páginas del documento, tope que aplicó)` de un error de límite de páginas, o `(None, None)` si Google no los dijo.

    Se leen de `details[].metadata` (`{"page_limit": "15", "pages": "20"}`, MEDIDO el 2026-10-16 contra la API real), que es
    parte del contrato del error; si no vinieran, del texto («Document pages exceed the limit: 15 got 20»), que Google ya
    reescribió una vez (ver `_motivo_de`). Con ellas la extracción sabe cuántas páginas tiene un documento sin abrirlo."""
    for detalle in detalles:
        metadata = detalle.get("metadata") or {}
        try:
            return int(metadata["pages"]), int(metadata["page_limit"])
        except (KeyError, TypeError, ValueError):
            continue
    encontrado = _RE_LIMITE_PAGINAS.search(mensaje or "")
    if encontrado:
        return int(encontrado.group(2)), int(encontrado.group(1))
    return None, None


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
                error = respuesta.json().get("error", {})
                detalle = error.get("message", "")
                detalles = error.get("details", [])
                # `details[].reason` es estable aunque Google reescriba el
                # mensaje; ver `_motivo_de`.
                razon = next((d["reason"] for d in detalles if d.get("reason")), "")
            except Exception:  # noqa: BLE001
                detalle = respuesta.text[:500]
                detalles = []
                razon = ""
            logger.error(
                "Document AI respondió %s: %s", respuesta.status_code, respuesta.text
            )
            motivo = _motivo_de(respuesta.status_code, detalle, razon)
            # Un documento demasiado largo trae cuántas páginas tiene: con eso se sabe en cuántos bloques partirlo.
            paginas, limite = _paginas_del_error(detalles, detalle) if motivo == "limite_paginas" else (None, None)
            raise ErrorDocumentAI(
                f"Document AI respondió {respuesta.status_code}: {detalle}",
                status_code=respuesta.status_code,
                motivo=motivo,
                paginas=paginas,
                limite=limite,
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


def _campo(
    entidad: dict[str, Any], offsets_ocr: list[list[tuple[int, int]]], paginas: list[int] | None = None
) -> dict[str, Any]:
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
        "page_number": _pagina_de(entidad, paginas),
        "bloque_indice": bloque_de(offsets_ocr, _tramos(entidad)),
        "posicion": _rectangulo(entidad),
    }


def _pagina_de(entidad: dict[str, Any], paginas: list[int] | None = None) -> int | None:
    """Página donde cae la entidad (1-based). El diccionario la guarda en
    `entity_fact.page_number` aunque sea redundante con el bloque, porque hace
    falta cuando `ocr_block_id` viene en NULL.

    `page` es un ÍNDICE dentro de `document.pages`, no un número de página (medido el 2026-10-16 pidiendo las páginas 11 a 20
    de un PDF de 20: las entidades traen 0…9 mientras `pages[].pageNumber` trae 11…20). En un documento procesado entero
    coinciden (índice + 1); en un bloque no, y `paginas` —los números de página del documento entero, en el orden de
    `document.pages`— hace la traducción."""
    refs = entidad.get("pageAnchor", {}).get("pageRefs")
    if not refs:
        return None
    # Document AI omite `page` cuando es la primera página.
    indice = int(refs[0].get("page", 0))
    if paginas is not None and 0 <= indice < len(paginas):
        return paginas[indice]
    return indice + 1


def _es_campo(valor: Any) -> bool:
    """¿Es un campo hoja ya empaquetado por `_campo`? (y no un grupo, que es un diccionario de campos)."""
    return es_campo(valor)


def _el_mejor(elementos: list[Any]) -> Any:
    """De varias apariciones de un campo que el esquema declara ÚNICO, la de mayor confianza.

    Antes se quedaba la ÚLTIMA, que era un accidente de cómo se llenaba el diccionario: un dato que el modelo encontró dos veces
    (la fecha del encabezado y la de las firmas, por ejemplo) quedaba con el que apareciera después, no con el más seguro. En un
    grupo único (un domicilio) no hay con qué comparar: se queda el primero."""
    if len(elementos) == 1:
        return elementos[0]
    hojas = [e for e in elementos if _es_campo(e)]
    if not hojas:
        return elementos[0]
    return max(hojas, key=lambda c: c["confianza"] if isinstance(c.get("confianza"), (int, float)) else -1.0)


def _extraer_entidades(
    entidades: list[dict[str, Any]],
    offsets_ocr: list[list[tuple[int, int]]],
    estructura: dict[str, dict] | None = None,
    paginas: list[int] | None = None,
) -> dict[str, Any]:
    """Convierte las entidades de Document AI en un diccionario, CONSERVANDO la
    jerarquía: una entidad con sub-propiedades (ej. 'domicilio') queda como
    sub-diccionario de campos, para que no choquen llaves repetidas con el nivel
    raíz (ej. 'estado' dentro de domicilio vs. 'estado' suelto). Cada campo hoja
    trae la forma de `entity_fact` — ver `_campo`.

    Lo que se REPITE (2026-10-16). Antes el diccionario se llenaba por nombre, así
    que de varias entidades del mismo tipo solo sobrevivía la última: de tres socios
    quedaba uno. Ahora un campo que el esquema declara MÚLTIPLE sale SIEMPRE como
    lista, aunque haya aparecido una sola vez, en el orden del documento:

      - campo hoja múltiple  → `[campo, campo, …]`
      - grupo múltiple       → `[{subcampo: campo, …}, {…}]`, una fila por instancia,
                               con los subcampos en el orden del esquema (Google los
                               devuelve en orden alfabético)
      - campo o grupo único  → como siempre: un `campo` o un diccionario

    `estructura` (`esquema.estructura_desde_esquema`) es lo que dice qué se repite: la
    respuesta de Google no lo dice. Sin ella —no se pudo leer el esquema— se aplana
    por lo que llegó: un tipo que aparece más de una vez es lista.

    `paginas` (los números de página del documento entero, en el orden de `document.pages`) solo hace falta cuando lo que se
    procesó es un BLOQUE de un documento largo: ver `_pagina_de`.
    """
    por_tipo: dict[str, list[dict[str, Any]]] = {}
    for entidad in entidades:
        nombre = entidad.get("type")
        if nombre:
            por_tipo.setdefault(nombre, []).append(entidad)

    resultado: dict[str, Any] = {}
    for nombre, ocurrencias in por_tipo.items():
        definicion = (estructura or {}).get(nombre)
        multiple = definicion["multiple"] if definicion else len(ocurrencias) > 1
        hijos_definidos = definicion.get("hijos") if definicion else None
        elementos: list[Any] = []
        for entidad in ocurrencias:
            if entidad.get("properties"):
                hijos = _extraer_entidades(entidad["properties"], offsets_ocr, hijos_definidos, paginas)
                if hijos and multiple and hijos_definidos:
                    # Una fila de una tabla: sus columnas en el orden del esquema, no en el alfabético de Google.
                    hijos = {**{h: hijos[h] for h in hijos_definidos if h in hijos}, **{h: v for h, v in hijos.items() if h not in hijos_definidos}}
                if hijos:
                    elementos.append(hijos)
            elif hijos_definidos:
                continue  # un grupo sin ninguna hija no dice nada: no se vuelve un «campo» vacío
            else:
                campo = _campo(entidad, offsets_ocr, paginas)
                if campo["value_normalized"] is not None:
                    elementos.append(campo)
        if not elementos:
            continue
        resultado[nombre] = elementos if multiple else _el_mejor(elementos)
    return resultado


def _campos_ausentes(estructura: dict[str, dict], datos: dict[str, Any]) -> list[str]:
    """Lo OBLIGATORIO que el esquema pide y la extracción no trae.

    Antes un campo que Document AI no devolvía simplemente no aparecía, y nada lo distinguía de un campo que nadie pidió. Es
    una lista de rutas: `capital_social` (un campo que faltó), `accionistas` (un grupo obligatorio sin ninguna fila) o
    `accionistas[2].nombre` (la fila 2 del grupo salió sin un subcampo obligatorio: casi siempre una instancia espuria que el
    modelo sacó de otra parte del documento, y por eso se señala en vez de esconderla)."""
    ausentes: list[str] = []
    for nombre, definicion in estructura.items():
        valor = datos.get(nombre)
        if definicion["obligatorio"] and (valor is None or valor == []):
            ausentes.append(nombre)
            continue
        hijos = definicion.get("hijos")
        if not hijos or not valor:
            continue
        filas = valor if isinstance(valor, list) else [valor]
        for i, fila in enumerate(filas, start=1):
            for hijo, def_hijo in hijos.items():
                if def_hijo["obligatorio"] and hijo not in fila:
                    ausentes.append(f"{nombre}[{i}].{hijo}" if isinstance(valor, list) else f"{nombre}.{hijo}")
    return ausentes


async def _estructura_de(procesador_id: str) -> dict[str, dict] | None:
    """La estructura del esquema del procesador (ver `procesadores.estructura_del_procesador`), o `None` si no se pudo leer.

    Nunca levanta: sin ella la extracción sigue, solo que aplanando por lo que llegó y sin poder decir qué campo obligatorio
    faltó. Fallar la extracción entera por no poder leer un dato accesorio sería peor que perder esa precisión."""
    from servicios import procesadores  # import tardío: `procesadores` importa a este módulo

    try:
        return await procesadores.estructura_del_procesador(procesador_id) or None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "No se pudo leer la estructura del procesador %s; se aplana por lo que devolvió Document AI: %s",
            procesador_id,
            exc,
        )
        return None


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
    también los anidados como 'domicilio' y las listas (campos múltiples y filas de
    grupos).

    Se recolectan ANTES de agregar la capa de OCR al resultado, a propósito: los
    bloques de OCR también traen `confianza`, y esa es la confianza de LECTURA,
    no de extracción. Mezclarlas daría un número que no significa nada.

    Lista vacía si no se extrajo ningún campo (Document AI puede responder 200
    con una lista de entidades vacía si la imagen no es legible)."""
    confianzas: list[float] = []

    def recorrer(nodo: Any) -> None:
        if isinstance(nodo, list):
            for elemento in nodo:
                recorrer(elemento)
        elif isinstance(nodo, dict):
            if _es_campo(nodo):
                # Se filtran los None: `_a_cien` devuelve None cuando Document AI
                # omite `confidence` en una entidad, y en Python 3 comparar None
                # con un float dentro de min() lanza TypeError. Eso convertiría
                # una extracción PERFECTA en un 502 "No se pudo procesar la
                # credencial" — el peor tipo de falla, porque el mensaje culpa al
                # documento cuando el problema es nuestro.
                if isinstance(nodo.get("confianza"), (int, float)):
                    confianzas.append(float(nodo["confianza"]))
            else:
                for valor in nodo.values():
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


# ── Documentos largos ─────────────────────────────────────────────────────────

# Cuántos bloques de un mismo documento se mandan a Google a la vez.
BLOQUES_EN_PARALELO = 3
# Cuánto se espera antes de repetir un bloque que falló por algo pasajero.
PAUSA_REINTENTO_BLOQUE_S = 3.0
# Un bloque se repite UNA vez si falló por algo pasajero (Google caído un momento, cuota por minuto). Lo que es del documento
# (archivo ilegible, otro tope) saldría igual y no se repite.
_MOTIVOS_PASAJEROS = ("servicio", "cuota")


def _paginas_absolutas(documento: dict[str, Any], desde: int, hasta: int) -> list[int]:
    """Los números de página DEL DOCUMENTO ENTERO de las páginas que Document AI devolvió para un bloque, en el orden en que las
    devolvió (el índice al que apuntan las entidades; ver `_pagina_de`).

    Medido el 2026-10-16 pidiendo las páginas 11 a 20 de un PDF de 20: `pages[].pageNumber` viene ABSOLUTO (11…20). Se usa eso
    mientras cuadre con lo pedido; si no cuadra (otra convención en una versión futura de Google), se confía en lo que se pidió."""
    devueltas = [p.get("pageNumber") for p in documento.get("pages") or []]
    if devueltas and all(isinstance(n, int) and desde <= n <= hasta for n in devueltas):
        return devueltas
    return list(range(desde, hasta + 1))


def _con_paginas_absolutas(capa: dict[str, Any], paginas: list[int]) -> dict[str, Any]:
    """La capa de OCR de un bloque con los números de página del documento entero (no cambia nada si Google ya los manda así)."""
    mapa = {p["page_number"]: nuevo for p, nuevo in zip(capa["paginas"], paginas)}
    return {
        **capa,
        "paginas": [{**p, "page_number": mapa.get(p["page_number"], p["page_number"])} for p in capa["paginas"]],
        "bloques": [{**b, "page_number": mapa.get(b["page_number"], b["page_number"])} for b in capa["bloques"]],
    }


async def _procesar_bloque(
    procesador_id: str,
    contenido: bytes,
    mime_type: str,
    version: str,
    desde: int,
    hasta: int,
    semaforo: asyncio.Semaphore,
) -> dict:
    """Procesa SOLO las páginas `desde`…`hasta` del documento (`individualPageSelector`: Google cobra solo esas), y repite una vez
    si falla por algo pasajero."""
    opciones = {"processOptions": {"individualPageSelector": {"pages": list(range(desde, hasta + 1))}}}
    async with semaforo:
        for intento in (1, 2):
            try:
                return await _procesar(procesador_id, contenido, mime_type, version, opciones)
            except ErrorDocumentAI as exc:
                if intento == 2 or exc.motivo not in _MOTIVOS_PASAJEROS:
                    raise
                logger.warning(
                    "El bloque de las páginas %s a %s falló (%s); se repite una vez.", desde, hasta, exc.motivo
                )
                await asyncio.sleep(PAUSA_REINTENTO_BLOQUE_S)
    raise AssertionError("inalcanzable: el segundo intento devuelve o levanta")


def _cerrar_resultado(
    datos: dict[str, Any],
    estructura: dict[str, dict] | None,
    capa_ocr: dict[str, Any],
    metadata: dict[str, Any],
    transformar: Callable[[dict[str, Any]], dict[str, Any]] | None,
) -> dict[str, Any]:
    """Lo que es igual para un documento procesado entero y para uno procesado por bloques: los campos ausentes, la
    transformación propia del tipo, las dos confianzas resumen, la capa de OCR y `_metadata`."""
    if estructura:
        # Solo cuando se pudo leer el esquema: sin él no se sabe qué era obligatorio, y una lista vacía diría «no faltó nada».
        metadata["campos_ausentes"] = _campos_ausentes(estructura, datos)
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


async def _extraer_por_bloques(
    procesador_id: str,
    contenido: bytes,
    mime_type: str,
    version: str,
    transformar: Callable[[dict[str, Any]], dict[str, Any]] | None,
    rechazo: ErrorDocumentAI,
    estructura_tarea: "asyncio.Future[dict[str, dict] | None]",
) -> dict[str, Any]:
    """Extrae un documento que Document AI rechazó por largo (`rechazo` trae cuántas páginas tiene), partiéndolo en bloques de
    páginas (`servicios.bloques.planear_bloques`), procesándolos a la vez y uniendo el resultado como si hubiera pasado entero.

    Todo o nada: si un bloque falla (después de su reintento), falla la extracción. Entregar el documento sin un pedazo,
    callado, sería peor que no entregarlo: faltaría un dato y nada lo diría."""
    total = rechazo.paginas
    if total > EXTRACCION_MAX_PAGINAS:
        # Se rechaza sin procesar nada (y sin cobrar): el rechazo de Google ya dijo cuántas páginas tiene.
        raise ErrorDocumentAI(
            f"El documento tiene {total} páginas y el máximo que se procesa es {EXTRACCION_MAX_PAGINAS}",
            status_code=422,
            motivo="limite_paginas",
            paginas=total,
            limite=EXTRACCION_MAX_PAGINAS,
        )
    plan = planear_bloques(total, min(EXTRACCION_PAGINAS_POR_BLOQUE, rechazo.limite or 15), EXTRACCION_SOLAPE_PAGINAS)
    logger.info(
        "El documento tiene %s páginas (tope de Document AI en línea: %s): se procesa en %s bloques %s",
        total,
        rechazo.limite,
        len(plan),
        plan,
    )

    semaforo = asyncio.Semaphore(BLOQUES_EN_PARALELO)
    tareas = [
        asyncio.ensure_future(_procesar_bloque(procesador_id, contenido, mime_type, version, desde, hasta, semaforo))
        for desde, hasta in plan
    ]
    try:
        try:
            # Por debajo del tiempo que espera el front (130 s, ver `TIMEOUT_MS` del BFF): mejor decir «tardó demasiado» que
            # que el navegador se rinda con el trabajo todavía corriendo y cobrándose.
            crudos = await asyncio.wait_for(asyncio.gather(*tareas), timeout=IA_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise httpx.ReadTimeout("Los bloques del documento no terminaron a tiempo") from exc
    except BaseException:
        # Un bloque que falla no deja a los demás procesando (y cobrando) un resultado que nadie va a usar.
        for tarea in tareas:
            tarea.cancel()
        await asyncio.gather(*tareas, return_exceptions=True)
        raise

    estructura = await estructura_tarea
    procesado_en = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    metadata: dict[str, Any] = {
        "procesado_en": procesado_en,
        "quality_alert": False,
        "engine": "document_ai_extractor",
        "engine_version": version or None,
        "paginas_totales": total,
        "bloques": [{"desde": desde, "hasta": hasta} for desde, hasta in plan],
        # Lo que Google cobró: más que `paginas_totales` por las páginas que los bloques repiten.
        "paginas_procesadas": sum(hasta - desde + 1 for desde, hasta in plan),
    }

    documentos = [crudo.get("document", {}) for crudo in crudos]
    if all(d.get("entities") is None for d in documentos):
        logger.warning(
            "Ningún bloque de la respuesta de Document AI (procesador %s) trae document.entities: se marca quality_alert.",
            procesador_id,
        )
        metadata["quality_alert"] = True
        metadata["motivo"] = _MOTIVO_NO_LEGIBLE
        return {"confianza_promedio": None, "confianza_minima": None, "_metadata": metadata}

    capas: list[dict[str, Any]] = []
    por_bloque: list[dict[str, Any]] = []
    for (desde, hasta), documento in zip(plan, documentos):
        paginas = _paginas_absolutas(documento, desde, hasta)
        capa, offsets_ocr = extraer_capa_ocr(documento, version or None)
        capas.append(_con_paginas_absolutas(capa, paginas))
        por_bloque.append(_extraer_entidades(documento.get("entities") or [], offsets_ocr, estructura, paginas))

    capa_ocr, mapas = unir_capas_ocr(capas, total)
    parciales = [
        Parcial(desde, hasta, remapear_bloques(datos, mapa))
        for (desde, hasta), datos, mapa in zip(plan, por_bloque, mapas)
    ]
    datos = combinar_datos(parciales, estructura)
    return _cerrar_resultado(datos, estructura, capa_ocr, metadata, transformar)


async def extraer_con_procesador(
    procesador_id: str,
    contenido: bytes,
    mime_type: str,
    version: str = "",
    transformar: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    permitir_bloques: bool = True,
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

    DOCUMENTOS LARGOS (2026-10-16). Document AI rechaza en línea más de 15 páginas. Si el rechazo trae cuántas tiene el
    documento, se procesa por BLOQUES de páginas y se unen los resultados (`_extraer_por_bloques`), hasta
    `EXTRACCION_MAX_PAGINAS`. El resultado tiene la misma forma, con las páginas del documento entero, y `_metadata` agrega
    `paginas_totales`, `bloques` y `paginas_procesadas`. `permitir_bloques=False` lo apaga: una credencial de 20 páginas no es
    una credencial, y no vale la pena cobrarla.
    """
    # La estructura del esquema (qué se repite, qué es obligatorio) se lee MIENTRAS Document AI procesa: casi siempre sale de
    # la memoria, y la primera vez no alarga la extracción porque corren juntas.
    estructura_tarea = asyncio.ensure_future(_estructura_de(procesador_id))
    try:
        try:
            crudo = await _procesar(procesador_id, contenido, mime_type, version)
        except ErrorDocumentAI as exc:
            if not (permitir_bloques and exc.motivo == "limite_paginas" and exc.paginas):
                raise
            return await _extraer_por_bloques(
                procesador_id, contenido, mime_type, version, transformar, exc, estructura_tarea
            )
        estructura = await estructura_tarea
    finally:
        if not estructura_tarea.done():
            estructura_tarea.cancel()
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

    datos = _extraer_entidades(entidades, offsets_ocr, estructura)
    return _cerrar_resultado(datos, estructura, capa_ocr, metadata, transformar)


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
        # Una credencial son una o dos caras: si trae 20 páginas no es una credencial, y no se paga por procesarla.
        permitir_bloques=False,
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


async def clasificar_documento(contenido: bytes, mime_type: str, tenant: str) -> dict[str, Any]:
    """Pregunta al Custom Document Classifier de ESTA organización a cuál de SUS tipos documentales activos
    pertenece un documento entrante — el paso previo a decidir a qué extractor
    mandarlo, entre la Bandeja de preparación y el pipeline. Cada organización tiene su
    clasificador (`servicios/clasificadores.py`), así que un documento nunca cae en un tipo de otra;
    levanta `SinClasificador` si la organización todavía no tiene el suyo.

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
    procesador_id = clasificadores.id_de(tenant)
    if not procesador_id:
        # El clasificador se crea al activar el primer tipo documental (`sincronizar_clasificador`). Quien llega aquí vio el
        # Pipeline, no la configuración, así que el texto le dice QUÉ hacer y DÓNDE, no cómo se llama la pieza interna.
        raise SinClasificador(
            "Todavía no se pueden clasificar documentos: tu organización no tiene ningún tipo documental activo. "
            "Crea y activa al menos uno en Configuración, Tipos documentales, y vuelve a intentarlo."
        )
    crudo = await _procesar(
        procesador_id,
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
