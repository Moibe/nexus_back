"""Dominio: webhooks — registrar, listar, activar/desactivar y eliminar los
webhooks de un cliente.

Lo usa SOLO el front (módulo "Webhooks"), con su llave de servicio: el router
entero va detrás de `exigir_llave` en `app.py`, y por el dominio público no se
alcanza (`superficie_publica.py` solo deja pasar tres rutas).

TODAVÍA NO SE ENVÍA NINGÚN AVISO: esto es el registro. El secret de firma se
genera AQUÍ, en el servidor, y viaja una sola vez: en la respuesta del alta,
con `Cache-Control: no-store`. Se guarda cifrado y no se escribe en logs; la URL
tampoco se loguea, porque hay endpoints que llevan un token en la consulta.

Registro provisional en el NAS: ver `servicios/webhooks_cliente.py`.

Los handlers son `def`: el registro es I/O síncrono y debe ir al threadpool.
"""

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from servicios import webhooks_cliente
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_EXISTE = "Ese webhook no existe o es de otro cliente."


class Alta(BaseModel):
    tenant: str = Field(..., description="Cliente al que pertenece el webhook")
    url: str = Field(..., description="A dónde se avisará: https://, o http:// solo hacia localhost")
    eventos: list[str] = Field(
        ...,
        description="Uno o más de: documento.completado, documento.fallido, documento.rechazado, expediente.completado",
    )


class CambioDeEstado(BaseModel):
    tenant: str
    estado: Literal["activo", "inactivo"]


class Baja(BaseModel):
    tenant: str


def _no_disponible(exc: Exception, que: str) -> HTTPException:
    logger.exception("Falló el registro de webhooks (%s)", que)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="El registro de webhooks no está disponible en este momento. Intenta de nuevo en unos minutos.",
    )


def _sin_cifrado(exc: Exception) -> HTTPException:
    # Es configuración del servidor, no un error del usuario: se dice qué falta
    # para que quien administra lo arregle sin buscar en los logs.
    logger.error("Webhooks sin cifrado: %s", exc)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail=f"El servidor no puede guardar el secret del webhook: {exc}",
    )


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    tags=["Webhooks"],
    summary="Registrar un webhook",
    description=(
        "Lo registra ACTIVO, genera su secret de firma (`whsec_...`), lo guarda "
        "cifrado y lo devuelve UNA vez. Todavía no se envía ningún aviso."
    ),
)
def registrar(datos: Alta, response: Response):
    try:
        secret, webhook = webhooks_cliente.registrar(datos.tenant, datos.url, datos.eventos)
    except webhooks_cliente.Duplicado as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except webhooks_cliente.SinCifrado as exc:
        # El secret ni siquiera se generó: no hay nada que se pierda.
        raise _sin_cifrado(exc) from exc
    except ErrorAlmacen as exc:
        # Si falló la escritura, el webhook NO existe: el secret nunca salió
        # de aquí, así que no hay nada que se pierda ni que eliminar.
        raise _no_disponible(exc, f"registrar, tenant={datos.tenant}") from exc
    response.headers["Cache-Control"] = "no-store"
    logger.info("Webhook registrado (id=%s, tenant=%s)", webhook["id"], datos.tenant)
    return {"secret": secret, "webhook": webhook}


@router.get(
    "/",
    tags=["Webhooks"],
    summary="Listar los webhooks de un cliente",
    description="Del más nuevo al más viejo. Nunca trae el secret ni su cifrado.",
)
def listar(tenant: str = Query(...)):
    try:
        return {"webhooks": webhooks_cliente.listar(tenant)}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise _no_disponible(exc, f"listar, tenant={tenant}") from exc


@router.post(
    "/{identificador}/estado",
    tags=["Webhooks"],
    summary="Activar o desactivar un webhook",
    description="Pedir el estado que ya tiene no es error: responde igual y no escribe nada.",
)
def cambiar_estado(identificador: str, datos: CambioDeEstado):
    try:
        webhook = webhooks_cliente.cambiar_estado(datos.tenant, identificador, datos.estado)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise _no_disponible(exc, f"estado, tenant={datos.tenant}") from exc
    if webhook is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE)
    logger.info("Webhook %s (id=%s, tenant=%s)", datos.estado, identificador, datos.tenant)
    return {"webhook": webhook}


@router.post(
    "/{identificador}/eliminar",
    tags=["Webhooks"],
    summary="Eliminar un webhook",
    description=(
        "Deja de aparecer en el listado y su secret deja de usarse. Eliminar uno "
        "que ya estaba eliminado no es error."
    ),
)
def eliminar(identificador: str, datos: Baja):
    try:
        existia = webhooks_cliente.eliminar(datos.tenant, identificador)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise _no_disponible(exc, f"eliminar, tenant={datos.tenant}") from exc
    if not existia:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE)
    logger.info("Webhook eliminado (id=%s, tenant=%s)", identificador, datos.tenant)
    return {"eliminado": True}
