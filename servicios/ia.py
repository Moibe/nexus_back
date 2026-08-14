"""Servicio de IA: TODO lo que llama a endpoints de IA externos.

Convención de capas: este archivo es el único que sabe de httpx y de las rutas
del servicio de IA. Los routers nunca llaman httpx directo — llaman funciones de
aquí, con nombres de dominio (`extraer_campos`, no `post_a_extraccion`).

httpx en vez de requests porque estas llamadas pueden tardar decenas de segundos
y conviene no amarrar un hilo del threadpool mientras se espera al modelo.
"""

import httpx

from config import IA_API_KEY, IA_BASE_URL, IA_TIMEOUT


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if IA_API_KEY:
        headers["Authorization"] = f"Bearer {IA_API_KEY}"
    return headers


async def llamar(ruta: str, payload: dict) -> dict:
    """POST genérico al servicio de IA. `ruta` es relativa a IA_BASE_URL.

    No atrapa excepciones a propósito: el router decide qué código HTTP
    devolverle al front (por convención del proyecto, 502 cuando falla un
    servicio upstream).
    """
    if not IA_BASE_URL:
        raise RuntimeError("Falta IA_BASE_URL en el .env — revisa .env.example")

    url = f"{IA_BASE_URL.rstrip('/')}/{ruta.lstrip('/')}"
    async with httpx.AsyncClient(timeout=IA_TIMEOUT) as cliente:
        respuesta = await cliente.post(url, json=payload, headers=_headers())
        respuesta.raise_for_status()
        return respuesta.json()


# ── Operaciones de dominio ────────────────────────────────────────────────────
# ⚠️ PLACEHOLDER: las rutas reales se ajustan cuando existan los endpoints de IA.


async def evaluar_calidad_ocr(documento_id: int) -> dict:
    """HU032 — evalúa qué tan confiable quedó el OCR de un documento."""
    return await llamar("/ocr/calidad", {"documento_id": documento_id})


async def extraer_campos(documento_id: int, tipo_documental_id: int) -> dict:
    """Extrae los campos configurados para ese tipo documental."""
    return await llamar(
        "/extraccion", {"documento_id": documento_id, "tipo_documental_id": tipo_documental_id}
    )
