"""Cliente para los endpoints de IA externos a esta API.

Aquí vive la segunda responsabilidad del back: además de hablar con SQL Server,
esta API orquesta llamadas a servicios de IA (OCR, extracción de campos,
clasificación documental) que corren en otro lado.

Se usa httpx en vez de requests porque soporta async nativo — estas llamadas
pueden tardar decenas de segundos y conviene no amarrar un hilo del threadpool
mientras se espera al modelo.
"""

import httpx

from config import IA_API_KEY, IA_BASE_URL, IA_TIMEOUT


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if IA_API_KEY:
        headers["Authorization"] = f"Bearer {IA_API_KEY}"
    return headers


async def llamar(ruta: str, payload: dict) -> dict:
    """POST genérico a un endpoint de IA. `ruta` es relativa a IA_BASE_URL.

    No atrapa excepciones a propósito: el router que la llama decide qué código
    HTTP devolverle al front (por convención del proyecto, 502 cuando falla un
    servicio upstream).
    """
    if not IA_BASE_URL:
        raise RuntimeError("Falta IA_BASE_URL en el .env — revisa .env.example")

    url = f"{IA_BASE_URL.rstrip('/')}/{ruta.lstrip('/')}"
    async with httpx.AsyncClient(timeout=IA_TIMEOUT) as cliente:
        respuesta = await cliente.post(url, json=payload, headers=_headers())
        respuesta.raise_for_status()
        return respuesta.json()
