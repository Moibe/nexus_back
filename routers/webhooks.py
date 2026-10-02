"""Dominio: webhooks — registrar, listar, activar/desactivar y eliminar los
webhooks de un cliente.

Lo usa SOLO el front (módulo "Webhooks"), con su llave de servicio: el router
entero va detrás de `exigir_llave` en `app.py`, y por el dominio público no se
alcanza (`superficie_publica.py` solo deja pasar tres rutas).

Además del registro: el aviso de prueba con el que se valida la conexión
(`/validar`), la recepción de los avisos de eventos que manda el front cuando
un documento termina (`/eventos`, que los entrega y reintenta en segundo plano:
ver `servicios/entregas_webhooks.py`) y las métricas de esas entregas. El
secret de firma se
genera AQUÍ, en el servidor, y viaja una sola vez: en la respuesta del alta,
con `Cache-Control: no-store`. Se guarda cifrado y no se escribe en logs; la URL
tampoco se loguea, porque hay endpoints que llevan un token en la consulta.

Registro provisional en el NAS: ver `servicios/webhooks_cliente.py`.

Los handlers son `def`: el registro es I/O síncrono y debe ir al threadpool.
"""

import logging
from datetime import datetime
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from servicios import entrega_webhooks, entregas_webhooks, webhooks_cliente
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


class Validacion(BaseModel):
    tenant: str


class Aviso(BaseModel):
    tenant: str
    tipo: str = Field(..., description="documento.completado, documento.fallido o documento.rechazado")
    entradaId: str = Field(..., description="El id de la entrada de la bandeja: el que recibió el cliente al subir")
    tipoDocumental: str | None = Field(None, description="El tipo que identificó el clasificador, si lo hubo")
    motivo: str | None = Field(None, description="Código del motivo, para fallido y rechazado")


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
    if datos.estado == "inactivo":
        entregas_webhooks.cancelar_de(identificador, "El webhook se desactivó.")
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
    entregas_webhooks.cancelar_de(identificador, "El webhook se eliminó.")
    logger.info("Webhook eliminado (id=%s, tenant=%s)", identificador, datos.tenant)
    return {"eliminado": True}


@router.post(
    "/{identificador}/validar",
    tags=["Webhooks"],
    summary="Validar la conexión de un webhook",
    description=(
        "Le manda al endpoint un aviso de prueba firmado (`webhook.validacion`, "
        "Standard Webhooks) y lo da por validado si responde 2xx en el tiempo "
        "límite. No sigue redirecciones y solo llega a direcciones públicas. "
        "Un endpoint que no responde bien NO es un error de esta llamada: responde "
        "200 con `validado: false` y el motivo."
    ),
)
def validar(identificador: str, datos: Validacion):
    try:
        objetivo = webhooks_cliente.para_validar(datos.tenant, identificador)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except webhooks_cliente.SinCifrado as exc:
        raise _sin_cifrado(exc) from exc
    except ErrorAlmacen as exc:
        raise _no_disponible(exc, f"validar, tenant={datos.tenant}") from exc
    if objetivo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE)

    espera = entrega_webhooks.limite_validaciones.motivo_para_esperar(identificador)
    if espera:
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=espera)

    url, secret = objetivo
    try:
        resultado = entrega_webhooks.entregar(
            url,
            secret,
            "webhook.validacion",
            {
                "webhookId": identificador,
                "mensaje": "Aviso de prueba de NexusDoc. Responde con un código 2xx para validar este webhook.",
            },
        )
    except entrega_webhooks.Rechazo as exc:
        logger.info("Validación fallida (id=%s, tenant=%s): %s", identificador, datos.tenant, exc)
        return {"validado": False, "motivo": str(exc)}
    try:
        webhook = webhooks_cliente.marcar_validado(datos.tenant, identificador, resultado["codigo"], resultado["ms"])
    except ErrorAlmacen as exc:
        raise _no_disponible(exc, f"marcar validado, tenant={datos.tenant}") from exc
    if webhook is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE)
    logger.info("Webhook validado (id=%s, tenant=%s, codigo=%s, ms=%s)", identificador, datos.tenant, resultado["codigo"], resultado["ms"])
    return {"validado": True, "webhook": webhook, **resultado}


@router.post(
    "/eventos",
    tags=["Webhooks"],
    summary="Avisar que un documento terminó",
    description=(
        "Lo llama el front cuando un documento que llegó por la API llega a su "
        "resultado. Se acepta solo si la entrada existe, es del cliente y pasó al "
        "pipeline; se acepta UN resultado final por entrada, y repetir el mismo "
        "aviso no programa nada (`duplicado: true`). Programa una entrega por cada "
        "webhook validado, activo y suscrito, y las entrega y reintenta en segundo plano."
    ),
)
def recibir_aviso(datos: Aviso):
    try:
        resultado = entregas_webhooks.avisar(datos.tenant, datos.tipo, datos.entradaId, datos.tipoDocumental, datos.motivo)
    except entregas_webhooks.AvisoRechazado as exc:
        raise HTTPException(status_code=exc.codigo, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise _no_disponible(exc, f"aviso, tenant={datos.tenant}") from exc
    if not resultado["duplicado"]:
        logger.info(
            "Aviso %s de la entrada %s (tenant=%s): %s entrega(s)",
            datos.tipo, datos.entradaId, datos.tenant, resultado["programadas"],
        )
    return resultado


@router.get(
    "/{identificador}/metricas",
    tags=["Webhooks"],
    summary="Métricas de entregas de un webhook",
    description=(
        "Solicitudes, errores, tasa de error y latencia P50/P90/P99 de las entregas "
        "del periodo, comparadas con el periodo anterior de la misma duración. Cada "
        "INTENTO es una solicitud. El periodo son dos instantes ISO 8601 CON zona, "
        "`[desde, hasta)`, igual que en las métricas de las API Keys."
    ),
)
def metricas(identificador: str, tenant: str = Query(...), desde: datetime = Query(...), hasta: datetime = Query(...)):
    try:
        if not any(w["id"] == identificador for w in webhooks_cliente.listar(tenant)):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE)
        return entregas_webhooks.metricas(tenant, identificador, desde, hasta)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise _no_disponible(exc, f"metricas, tenant={tenant}") from exc
