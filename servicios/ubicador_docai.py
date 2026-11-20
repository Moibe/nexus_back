"""Ubicar los campos propuestos sobre un documento de ejemplo CON DOCUMENT AI (2026-11-20, Moibe: «¿no puede ser Document AI el que nos la regrese?»
→ «sí, hazlo con Document AI»).

Hasta hoy las cajas de «Generar a partir de un documento» las adivinaba Gemini (`servicios/ubicar_campos.ubicar`): lee bien el valor, pero la caja
es una estimación, y en dev una foto de pasaporte salió con 12 de 15 cajas corridas ~40 % del alto hacia la otra página. Aquí la caja sale del
mismo extractor que usará el tipo documental al activarse: un Custom Extractor recibe en el `:process` el esquema de los campos propuestos
(`processOptions.schemaOverride`, solo v1beta3: la Property de v1 no tiene `description`, ver `esquema.esquema_desde_campos`) y devuelve cada
valor con su `pageAnchor` (el polígono de dónde lo leyó). Medido con ese pasaporte: 15 de 15 con caja, todas en su lugar, 4.1 s. De paso se ve
lo que de verdad extraerá (ahí mismo leyó «HTSMRNO9» con O en la CURP).

El extractor es UNO para toda la aplicación, `biblioratum-ubicador-campos`: se busca por nombre exacto y se crea una sola vez (sin dataset: el
esquema viaja en cada llamada). No empieza con `biblioratum--`, así que la limpieza de procesadores huérfanos lo trata como ajeno y no lo toca
(`servicios/procesadores_huerfanos.py`). Cobra como cualquier extracción (~US$0.03 por página, medido con actas); solo se lee hasta
`MAX_PAGINAS` páginas de un PDF.

Lo de aquí NO decide nada ni guarda nada: si falla, quien llama cae a Gemini.
"""

import asyncio
import base64
import logging
from typing import Any

import httpx

from servicios import procesadores
from servicios.esquema import TIPO_GRUPO, esquema_desde_campos, nombres_en_google

logger = logging.getLogger(__name__)

NOMBRE_UBICADOR = "biblioratum-ubicador-campos"
#: Lo más que se lee de un PDF de ejemplo (el `:process` síncrono de un extractor admite 15 páginas). Lo de más allá no se marca.
MAX_PAGINAS = 15
TIMEOUT = 120
MOTOR = "document-ai"

_ubicador: list[str] = []  # el id del procesador, una vez encontrado o creado
_candado = asyncio.Lock()


class UbicadorNoDisponible(Exception):
    """Document AI no pudo ubicar (sin credencial, sin procesador, error de Google). Quien llama cae a Gemini."""


def campos_para_esquema(campos: list[dict]) -> list[dict]:
    """Los campos pedidos, en la forma que entiende `esquema_desde_campos`. Todo campo suelto se pide como TEXTO y opcional: aquí solo importa
    DÓNDE está; una fecha pedida como fecha o una lista con sus valores fijos podrían hacer que el extractor la calle si no coincide. Un grupo
    se queda grupo (con sus subcampos), para marcar el bloque entero, y lo que se repite se pide repetido."""
    salida: list[dict] = []
    for c in campos:
        multiple = "multiple" if c.get("cardinalidad") == "multiple" else "unico"
        if c.get("tipoDato") == TIPO_GRUPO and c.get("subcampos"):
            salida.append({
                "nombre": c["nombre"], "descripcion": c.get("descripcion", ""), "tipoDato": TIPO_GRUPO, "cardinalidad": multiple,
                "subcampos": [{"nombre": s["nombre"], "descripcion": s.get("descripcion", ""), "tipoDato": "texto"} for s in c["subcampos"]],
            })
        else:
            salida.append({"nombre": c["nombre"], "descripcion": c.get("descripcion", ""), "tipoDato": "texto", "cardinalidad": multiple})
    return salida


def _caja_de(entidad: dict) -> tuple[int, list[float]] | None:
    """`(página desde 1, [ymin, xmin, ymax, xmax] de 0 a 1)` de una entidad, o None si no trae polígono. Una entidad de grupo puede no traer el
    suyo: entonces es la unión de sus hijas (en la página de la primera que lo tenga)."""
    for ref in (entidad.get("pageAnchor") or {}).get("pageRefs") or []:
        vertices = (ref.get("boundingPoly") or {}).get("normalizedVertices") or []
        if vertices:
            xs = [float(v.get("x", 0.0)) for v in vertices]
            ys = [float(v.get("y", 0.0)) for v in vertices]
            return int(ref.get("page", 0) or 0) + 1, [min(ys), min(xs), max(ys), max(xs)]
    hijas = [c for c in (_caja_de(h) for h in entidad.get("properties") or []) if c]
    if not hijas:
        return None
    pagina = hijas[0][0]
    return pagina, _union([c for p, c in hijas if p == pagina])


def _union(cajas: list[list[float]]) -> list[float]:
    return [min(c[0] for c in cajas), min(c[1] for c in cajas), max(c[2] for c in cajas), max(c[3] for c in cajas)]


def _a_mil(caja: list[float]) -> list[int] | None:
    """La caja de 0 a 1 llevada a 0-1000 (la convención del front), o None si no tiene área."""
    ymin, xmin, ymax, xmax = (max(0, min(1000, round(n * 1000))) for n in caja)
    if ymax <= ymin:
        ymax = min(1000, ymin + 1)
    if xmax <= xmin:
        xmax = min(1000, xmin + 1)
    if ymax <= ymin or xmax <= xmin:
        return None
    return [ymin, xmin, ymax, xmax]


def ubicaciones_de(documento: dict, campos: list[dict], max_valor: int = 200) -> list[dict]:
    """De la respuesta de `:process` (su `document`), dónde está cada campo pedido: `[{campo, pagina, caja, valor}]` con el nombre TAL COMO LO
    PIDIÓ el front. Lo que se repite (o un grupo con varias filas) se marca como UN bloque: la unión de sus apariciones en la página de la
    primera. El valor es lo que leyó el extractor (de un grupo, el de su primera fila, con sus partes separadas por « · »). Un campo que no
    vino, o vino sin polígono, no se marca."""
    google_a_nombre = {google: nombre for nombre, (grupo, google) in nombres_en_google(campos_para_esquema(campos)).items() if grupo is None}
    por_campo: dict[str, list[dict]] = {}
    for e in documento.get("entities") or []:
        nombre = google_a_nombre.get(e.get("type", ""))
        if nombre is not None:
            por_campo.setdefault(nombre, []).append(e)
    salida: list[dict] = []
    for c in campos:
        entidades = por_campo.get(c["nombre"]) or []
        cajas = [x for x in (_caja_de(e) for e in entidades) if x]
        if not cajas:
            continue
        pagina = cajas[0][0]
        caja = _a_mil(_union([cj for p, cj in cajas if p == pagina]))
        if caja is None:
            continue
        primera = entidades[0]
        valor = (primera.get("mentionText") or "").strip()
        if not valor and primera.get("properties"):
            valor = " · ".join((h.get("mentionText") or "").strip() for h in primera["properties"] if (h.get("mentionText") or "").strip())
        salida.append({"campo": c["nombre"], "pagina": pagina, "caja": caja, "valor": " ".join(valor.split())[:max_valor]})
    return salida


async def _id_del_ubicador(cliente: httpx.AsyncClient) -> str:
    """El id del extractor que ubica: lo busca por nombre exacto y, si no existe, lo crea UNA vez (con candado: dos peticiones a la vez no
    crean dos)."""
    async with _candado:
        if _ubicador:
            return _ubicador[0]
        p = await procesadores._buscar_exacto(cliente, NOMBRE_UBICADOR)
        if p is None:
            p = await procesadores._pedir(
                cliente, "POST", "v1", f"{procesadores._PADRE}/processors",
                json={"type": "CUSTOM_EXTRACTION_PROCESSOR", "displayName": NOMBRE_UBICADOR},
            )
            logger.info("Ubicar con Document AI: se creó el extractor %s (%s).", NOMBRE_UBICADOR, p.get("name"))
        _ubicador.append(p["name"].rsplit("/", 1)[-1])
        return _ubicador[0]


async def ubicar(contenido: bytes, mime: str, campos: list[dict]) -> list[dict]:
    """Dónde está cada campo en el documento, según Document AI. `UbicadorNoDisponible` si no se pudo (quien llama cae a Gemini)."""
    if not procesadores.DOCAI_PROJECT_ID:
        raise UbicadorNoDisponible("falta DOCAI_PROJECT_ID")
    esquema = esquema_desde_campos("Documento de ejemplo", "", campos_para_esquema(campos))
    opciones: dict[str, Any] = {"schemaOverride": esquema}
    if mime == "application/pdf":
        opciones["fromStart"] = MAX_PAGINAS
    cuerpo = {"rawDocument": {"content": base64.b64encode(contenido).decode("ascii"), "mimeType": mime}, "processOptions": opciones}
    try:
        async with httpx.AsyncClient() as cliente:
            pid = await _id_del_ubicador(cliente)
            respuesta = await procesadores._pedir(
                cliente, "POST", "v1beta3", f"{procesadores._PADRE}/processors/{pid}:process", json=cuerpo, timeout=TIMEOUT
            )
    except Exception as exc:  # noqa: BLE001 — error de Google, sin red, sin credencial, respuesta rara: todo cae a Gemini
        raise UbicadorNoDisponible(f"{type(exc).__name__}: {str(exc)[:300]}") from exc
    return ubicaciones_de(respuesta.get("document") or {}, campos)
