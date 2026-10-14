"""Errores que comparten los routers.

## `almacen_no_disponible` (HU11 CA-4)

Un `ErrorAlmacen` trae el texto del fallo del sistema de archivos: rutas del servidor (el punto de montaje del NAS, la
carpeta de los registros), nombres de archivos de registro —que llevan el guid de la organización— y a veces el nombre
de otra. Hasta el 2026-10-14 varios routers lo devolvían tal cual en el `detail` del 503. Ahora el texto completo va al
log del servidor y a quien pidió se le dice algo que no sirve para enterarse de nada.
"""

import logging

from fastapi import HTTPException, status

logger = logging.getLogger(__name__)

MENSAJE_NO_DISPONIBLE = "El servicio no está disponible en este momento. Intenta de nuevo en unos minutos."


def almacen_no_disponible(exc: Exception) -> HTTPException:
    """El 503 de un fallo del almacén o de los registros. Se llama DENTRO del `except`, para que el log lleve la causa."""
    logger.exception("Falló el almacén o un registro interno: %s", exc)
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=MENSAJE_NO_DISPONIBLE)
