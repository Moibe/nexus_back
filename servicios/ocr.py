"""Capa de OCR de la respuesta de Document AI.

Vive aparte de `servicios.ia` porque en el modelo de datos son cosas distintas:
`ocr_result` + `ocr_block` (lo que se LEYÓ del documento) contra
`extraction_run` + `entity_fact` (lo que se ENTENDIÓ de esa lectura). Un
documento puede releerse con otro motor sin volver a extraer, y viceversa.

`ocr_block` es, según el diccionario, "la moneda de la trazabilidad": es lo que
permite que al hacer clic en un campo extraído se resalte la región exacta de la
imagen de donde salió. Sin esta capa, `entity_fact.ocr_block_id` no tiene a qué
apuntar y esa función no se puede construir.
"""

from typing import Any

# Granularidad que se publica como ocr_block. Se eligió `lines` tras medir una
# INE real: 13 blocks / 22 paragraphs / 23 lines / 57 tokens por página. Las
# líneas son el punto dulce — cada entidad extraída solapa exactamente una, y
# el volumen es una fracción del de tokens (el diccionario advierte que
# ocr_block será "la tabla más grande del sistema por órdenes de magnitud").
# Document AI expone las cuatro; cambiar aquí cambia toda la capa.
GRANULARIDAD = "lines"
BLOCK_TYPE = "line"  # valor del enum block_type del diccionario

# Document AI describe la orientación como "cuánto hay que girar la cabeza en
# sentido horario para leer el texto".
_GRADOS_POR_ORIENTACION = {
    "PAGE_UP": 0,
    "PAGE_RIGHT": 90,
    "PAGE_DOWN": 180,
    "PAGE_LEFT": 270,
}


def _offsets(layout: dict[str, Any]) -> list[tuple[int, int]]:
    """Tramos [inicio, fin) que este layout ocupa dentro de `document.text`.
    Es la llave para ligar una entidad con el bloque del que salió."""
    tramos = []
    for s in layout.get("textAnchor", {}).get("textSegments", []):
        tramos.append((int(s.get("startIndex", 0)), int(s.get("endIndex", 0))))
    return tramos


def _bbox(layout: dict[str, Any]) -> dict[str, float] | None:
    """Caja normalizada 0-1 del bloque, en el sistema que fija el diccionario:
    origen en la esquina superior izquierda, fracciones del ancho/alto de página.

    OJO — los bloques de OCR, a diferencia de las entidades, SÍ vienen con
    polígonos rotados de verdad (medido en una INE real: las líneas llegan con
    `orientation: PAGE_RIGHT` y los cuatro vértices variando en ambos ejes). Al
    colapsarlos a min/max se obtiene la caja envolvente alineada a los ejes, que
    es la forma que pide `ocr_block.bbox_*`, pero es MÁS GRANDE que la extensión
    real del texto rotado. Para resaltar en pantalla alcanza; si algún día se
    necesita el contorno exacto, hay que guardar los cuatro vértices."""
    vertices = layout.get("boundingPoly", {}).get("normalizedVertices")
    if not vertices:
        return None
    xs = [v.get("x", 0.0) for v in vertices]
    ys = [v.get("y", 0.0) for v in vertices]
    x, y = min(xs), min(ys)
    return {
        "x": round(x, 6),
        "y": round(y, 6),
        "ancho": round(max(xs) - x, 6),
        "alto": round(max(ys) - y, 6),
    }


def _a_cien(valor: Any) -> float | None:
    """Document AI reporta confianza 0-1; el diccionario la quiere 0-100
    (`numeric(5,2)`). La conversión se hace aquí, en la frontera."""
    if not isinstance(valor, (int, float)):
        return None
    return round(float(valor) * 100, 2)


def _idioma(pagina: dict[str, Any]) -> str | None:
    """Idioma dominante de la página (BCP-47), para `ocr_result.language`."""
    idiomas = pagina.get("detectedLanguages") or []
    if not idiomas:
        return None
    return max(idiomas, key=lambda i: i.get("confidence", 0)).get("languageCode")


def extraer_capa_ocr(
    documento: dict[str, Any], engine_version: str | None = None
) -> tuple[dict[str, Any], list[list[tuple[int, int]]]]:
    """Convierte la respuesta de Document AI en la capa de OCR.

    Devuelve dos cosas:
      1. el diccionario publicable (cabecera tipo `ocr_result` + sus bloques)
      2. los offsets de cada bloque, en el MISMO orden que `bloques` — esto no
         se publica, solo sirve para que `servicios.ia` ligue cada campo
         extraído con su bloque de origen.
    """
    paginas_crudas = documento.get("pages") or []

    paginas: list[dict[str, Any]] = []
    bloques: list[dict[str, Any]] = []
    offsets_por_bloque: list[list[tuple[int, int]]] = []

    for pagina in paginas_crudas:
        numero = pagina.get("pageNumber", len(paginas) + 1)
        dim = pagina.get("dimension") or {}
        paginas.append(
            {
                "page_number": numero,
                "ancho": dim.get("width"),
                "alto": dim.get("height"),
                "unidad": dim.get("unit"),
                "idioma": _idioma(pagina),
            }
        )

        for nodo in pagina.get(GRANULARIDAD, []):
            layout = nodo.get("layout", {})
            tramos = _offsets(layout)
            caja = _bbox(layout)
            if caja is None:
                continue  # sin posición no sirve para trazabilidad
            orientacion = layout.get("orientation")
            bloques.append(
                {
                    # Índice estable dentro de la respuesta. Es a lo que apunta
                    # cada campo extraído; al persistir se cambia por el id real
                    # de la fila de ocr_block.
                    "indice": len(bloques),
                    "page_number": numero,
                    "block_type": BLOCK_TYPE,
                    "texto": _texto_de(documento, tramos),
                    "bbox": caja,
                    "confianza": _a_cien(layout.get("confidence")),
                    "orientacion": orientacion,
                    "orientacion_grados": _GRADOS_POR_ORIENTACION.get(orientacion),
                }
            )
            offsets_por_bloque.append(tramos)

    capa = {
        "engine": "document_ai",
        # La lectura y la extracción salen de la MISMA llamada, así que
        # comparten versión de modelo. Se repite aquí porque en el diccionario
        # son tablas distintas (`ocr_result` y `extraction_run`), cada una con
        # su propia columna `engine_version`.
        "engine_version": engine_version,
        "page_count": len(paginas_crudas),
        # Idioma del documento = el de la primera página con idioma detectado.
        "language": next((p["idioma"] for p in paginas if p["idioma"]), None),
        "paginas": paginas,
        "bloques": bloques,
    }
    return capa, offsets_por_bloque


def _texto_de(documento: dict[str, Any], tramos: list[tuple[int, int]]) -> str:
    texto = documento.get("text", "")
    return "".join(texto[ini:fin] for ini, fin in tramos)


def bloque_de(offsets_por_bloque: list[list[tuple[int, int]]], tramos_entidad: list[tuple[int, int]]) -> int | None:
    """Índice del bloque de OCR del que salió una entidad, por solapamiento de
    offsets de texto. None si la entidad no trae posición en el texto (es el
    caso de las entidades contenedoras como 'domicilio', que solo agrupan a sus
    hijas y no tienen texto propio).

    Se devuelve el bloque con MAYOR solapamiento: si un valor cruza dos líneas,
    este es el principal — el conjunto completo es lo que el diccionario modela
    con la tabla puente `entity_fact_source`, pendiente de implementar.
    """
    if not tramos_entidad:
        return None

    mejor_indice = None
    mejor_solape = 0
    for indice, tramos_bloque in enumerate(offsets_por_bloque):
        solape = 0
        for ini_e, fin_e in tramos_entidad:
            for ini_b, fin_b in tramos_bloque:
                solape += max(0, min(fin_e, fin_b) - max(ini_e, ini_b))
        if solape > mejor_solape:
            mejor_solape = solape
            mejor_indice = indice
    return mejor_indice
