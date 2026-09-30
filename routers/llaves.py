"""Dominio: llaves — emitir, listar y revocar las API Keys de cliente.

Lo usa SOLO el front (módulo "API Key"), con su llave de servicio: el router
entero va detrás de `exigir_llave` en `app.py`. Un cliente no administra
llaves; usa la suya para subir a la bandeja.

El secret se genera AQUÍ, en el servidor, y viaja una sola vez: en la
respuesta de la emisión. No se guarda, no se registra en logs, y la respuesta
lleva `Cache-Control: no-store` para que ningún intermediario la conserve.

Registro provisional en el NAS: ver `servicios/llaves_cliente.py`.

Los handlers son `def`: el registro es I/O síncrono y debe ir al threadpool.
"""

import logging

from datetime import date

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from servicios import llaves_cliente, uso_llaves
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

router = APIRouter()


class Emision(BaseModel):
    tenant: str = Field(..., description="Cliente al que pertenece la llave")
    nombre: str = Field(..., description="Para reconocerla en el listado")
    descripcion: str = Field(..., description="Para qué se usa")
    dias: int = Field(..., description="Vigencia: 1, 7, 30 o 90 días")


class Revocacion(BaseModel):
    tenant: str


def _no_disponible(exc: Exception, que: str) -> HTTPException:
    logger.exception("Falló el registro de llaves (%s)", que)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="El registro de llaves no está disponible en este momento. Intenta de nuevo en unos minutos.",
    )


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    tags=["Llaves"],
    summary="Emitir una API Key de cliente",
    description=(
        "Genera la llave, guarda SOLO el hash de su secret y devuelve el secret "
        "UNA vez. Esa llave abre únicamente `POST /bandeja/`, y lo que suba se "
        "va al tenant de la llave."
    ),
)
def emitir(datos: Emision, response: Response):
    try:
        secret, llave = llaves_cliente.emitir(datos.tenant, datos.nombre, datos.descripcion, datos.dias)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        # Si falló la escritura, la llave NO existe: el secret nunca salió de
        # aquí, así que no hay nada que se pierda ni que revocar.
        raise _no_disponible(exc, f"emitir, tenant={datos.tenant}") from exc
    response.headers["Cache-Control"] = "no-store"
    logger.info("API Key emitida (id=%s, tenant=%s)", llave["id"], llave["tenant"])
    return {"secret": secret, "llave": llave}


@router.get(
    "/",
    tags=["Llaves"],
    summary="Listar las API Keys de un cliente",
    description="De la más nueva a la más vieja. Nunca trae el secret ni su hash.",
)
def listar(tenant: str = Query(...)):
    try:
        llaves = llaves_cliente.listar(tenant)
        # "Último uso" en cada tarjeta, como pide el diseño.
        ultimos = uso_llaves.ultimos_usos([l["id"] for l in llaves])
        for l in llaves:
            l["ultimoUso"] = ultimos.get(l["id"])
        return {"llaves": llaves}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise _no_disponible(exc, f"listar, tenant={tenant}") from exc


@router.post(
    "/{identificador}/revocar",
    tags=["Llaves"],
    summary="Revocar una API Key",
    description=(
        "Deja de servir de inmediato y no se puede reactivar. Revocar una que ya "
        "estaba revocada no es error: responde igual, con la fecha de la primera vez."
    ),
)
def revocar(identificador: str, datos: Revocacion):
    try:
        revocada_en = llaves_cliente.revocar(datos.tenant, identificador)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise _no_disponible(exc, f"revocar, tenant={datos.tenant}") from exc
    if revocada_en is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Esa API Key no existe o es de otro cliente.",
        )
    logger.info("API Key revocada (id=%s, tenant=%s)", identificador, datos.tenant)
    return {"revocada": True, "revocadaEn": revocada_en}


@router.get(
    "/{identificador}/metricas",
    tags=["Llaves"],
    summary="Métricas de consumo de una API Key",
    description=(
        "Solicitudes, éxito, errores y latencia del periodo, comparados con el "
        "periodo anterior de la misma duración; consumo de la semana contra el "
        "tope; y último uso. Fechas en ISO (AAAA-MM-DD), días completos en UTC."
    ),
)
def metricas(identificador: str, tenant: str = Query(...), desde: date = Query(...), hasta: date = Query(...)):
    try:
        # Solo las llaves del tenant: que no se puedan leer métricas ajenas.
        if not any(l["id"] == identificador for l in llaves_cliente.listar(tenant)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Esa API Key no existe o es de otro cliente.")
        return uso_llaves.metricas(identificador, desde, hasta)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise _no_disponible(exc, f"metricas, tenant={tenant}") from exc
