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
    IA_TIMEOUT,
)

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


def _url_procesador(procesador_id: str) -> str:
    if not DOCAI_PROJECT_ID or not procesador_id:
        raise RuntimeError(
            "Faltan DOCAI_PROJECT_ID o el ID del procesador en el .env — revisa .env.example"
        )
    return (
        f"https://{DOCAI_LOCATION}-documentai.googleapis.com/v1"
        f"/projects/{DOCAI_PROJECT_ID}/locations/{DOCAI_LOCATION}"
        f"/processors/{procesador_id}:process"
    )


async def _procesar(procesador_id: str, contenido: bytes, mime_type: str) -> dict:
    """Manda el archivo a un procesador de Document AI y devuelve su JSON crudo."""
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
            _url_procesador(procesador_id), headers=cabeceras, json=cuerpo
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


def _valor(entidad: dict[str, Any]) -> Any:
    """Valor de una entidad.

    Las fechas completas se normalizan a ISO (YYYY-MM-DD); todo lo demás se deja
    como el `mentionText` crudo que detectó Document AI, para no perder los ceros
    a la izquierda de códigos como localidad '0181' o municipio '064'.
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

    Los 4 vértices del polígono se colapsan a un rectángulo (min/max) porque en
    la práctica este procesador siempre los entrega alineados a los ejes, y un
    rectángulo es lo que un front consume directo (left/top/width/height)."""
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


def _campo(entidad: dict[str, Any]) -> dict[str, Any]:
    """Empaqueta una entidad hoja con su confianza y posición, para que quien
    consuma la API pueda decidir si un campo necesita revisión humana
    (confianza baja) o resaltarlo sobre la imagen original (posición)."""
    return {
        "valor": _valor(entidad),
        "confianza": entidad.get("confidence"),
        "posicion": _rectangulo(entidad),
    }


def _extraer_entidades(entidades: list[dict[str, Any]]) -> dict[str, Any]:
    """Convierte las entidades de Document AI en un diccionario, CONSERVANDO la
    jerarquía: una entidad con sub-propiedades (ej. 'domicilio') queda como
    sub-diccionario de campos, para que no choquen llaves repetidas con el nivel
    raíz (ej. 'estado' dentro de domicilio vs. 'estado' suelto). Cada campo hoja
    trae su valor, confianza y posición — ver `_campo`."""
    resultado: dict[str, Any] = {}
    for entidad in entidades:
        nombre = entidad.get("type")
        if not nombre:
            continue
        if entidad.get("properties"):
            resultado[nombre] = _extraer_entidades(entidad["properties"])
        else:
            campo = _campo(entidad)
            if campo["valor"] is not None:
                resultado[nombre] = campo
    return resultado


def _limpiar_ine(datos: dict[str, Any]) -> dict[str, Any]:
    """Quita artefactos de puntuación que el OCR arrastra del renglón de
    domicilio impreso en la credencial: el estado trae punto final ('SON.') y la
    localidad trae coma final ('HUATABAMPO,')."""
    domicilio = datos.get("domicilio")
    if isinstance(domicilio, dict):
        estado = domicilio.get("estado")
        if isinstance(estado, dict) and isinstance(estado.get("valor"), str):
            estado["valor"] = estado["valor"].rstrip(".").strip()
        localidad = domicilio.get("localidad")
        if isinstance(localidad, dict) and isinstance(localidad.get("valor"), str):
            localidad["valor"] = localidad["valor"].rstrip(",").strip()
    return datos


def _confianza_minima(datos: dict[str, Any]) -> float | None:
    """La menor `confianza` entre TODOS los campos extraídos, recorriendo
    también los de 'domicilio'. Sirve de semáforo de un vistazo: un solo campo
    mal leído (ej. la CURP) puede pasar desapercibido en un promedio si el
    resto de la credencial salió perfecto; el mínimo no lo deja esconderse.

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


async def extraer_ine(contenido: bytes, mime_type: str = "image/jpeg") -> dict[str, Any]:
    """Extrae los datos de una credencial INE.

    Cada campo hoja llega como `{"valor", "confianza", "posicion"}` (ver
    `_campo`), para que quien consuma la API pueda decidir si un campo de baja
    confianza necesita revisión humana, o resaltarlo sobre la imagen original.
    `confianza_minima`, a nivel raíz, resume eso en un solo número — ver
    `_confianza_minima`. `_metadata.procesado_en` es la fecha/hora (UTC) en que
    ESTE llamado a Document AI terminó — no confundir con `fecha_registro`, que
    es un campo de la credencial (la fecha impresa en la INE)."""
    crudo = await _procesar(DOCAI_PROCESADOR_INE, contenido, mime_type)
    # Se captura aquí, justo al terminar la llamada real a Document AI, porque
    # es el ÚNICO lugar donde este dato existe: Google no manda un timestamp de
    # procesamiento en su respuesta (document.keys() no trae ninguno). Si en vez
    # de esto se generara más tarde -p.ej. cuando el front guarde los datos en
    # SQL Server-, dejaría de significar "cuándo procesó Document AI" y pasaría
    # a significar "cuándo se guardó", que puede ser minutos u horas después si
    # alguien revisa campos de baja confianza antes de confirmar.
    procesado_en = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entidades = crudo.get("document", {}).get("entities")
    if entidades is None:
        logger.error("La respuesta de Document AI no trae document.entities")
        raise RuntimeError("Respuesta inesperada de Document AI")
    datos = _limpiar_ine(_extraer_entidades(entidades))
    datos["confianza_minima"] = _confianza_minima(datos)
    # Bajo su propia llave y no como hermano de los campos del documento: esto
    # no es un dato DE la credencial, es del request. El front debe cargarlo tal
    # cual hasta que se guarde en SQL Server, sin regenerarlo en ese momento.
    datos["_metadata"] = {"procesado_en": procesado_en}
    return datos
