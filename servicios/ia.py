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


def _valor_normalizado(entidad: dict[str, Any]) -> Any:
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


def _extraer_entidades(entidades: list[dict[str, Any]]) -> dict[str, Any]:
    """Convierte las entidades de Document AI en un diccionario, CONSERVANDO la
    jerarquía: una entidad con sub-propiedades (ej. 'domicilio') queda como
    sub-diccionario, para que no choquen llaves repetidas con el nivel raíz
    (ej. 'estado' dentro de domicilio vs. 'estado' suelto)."""
    resultado: dict[str, Any] = {}
    for entidad in entidades:
        nombre = entidad.get("type")
        if not nombre:
            continue
        if entidad.get("properties"):
            resultado[nombre] = _extraer_entidades(entidad["properties"])
        else:
            valor = _valor_normalizado(entidad)
            if valor is not None:
                resultado[nombre] = valor
    return resultado


def _limpiar_ine(datos: dict[str, Any]) -> dict[str, Any]:
    """Quita artefactos de puntuación que el OCR arrastra del renglón de
    domicilio impreso en la credencial: el estado trae punto final ('SON.') y la
    localidad trae coma final ('HUATABAMPO,')."""
    domicilio = datos.get("domicilio")
    if isinstance(domicilio, dict):
        if isinstance(domicilio.get("estado"), str):
            domicilio["estado"] = domicilio["estado"].rstrip(".").strip()
        if isinstance(domicilio.get("localidad"), str):
            domicilio["localidad"] = domicilio["localidad"].rstrip(",").strip()
    return datos


# ── Operaciones de dominio ────────────────────────────────────────────────────


async def extraer_ine(contenido: bytes, mime_type: str = "image/jpeg") -> dict[str, Any]:
    """Extrae los datos de una credencial INE."""
    crudo = await _procesar(DOCAI_PROCESADOR_INE, contenido, mime_type)
    entidades = crudo.get("document", {}).get("entities")
    if entidades is None:
        logger.error("La respuesta de Document AI no trae document.entities")
        raise RuntimeError("Respuesta inesperada de Document AI")
    return _limpiar_ine(_extraer_entidades(entidades))
