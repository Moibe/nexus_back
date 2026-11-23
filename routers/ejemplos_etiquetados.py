"""Dominio: ejemplos etiquetados de un tipo documental y la medición de su precisión (2026-11-23; ver `servicios/ejemplos_etiquetados.py`).

Lo usa la pantalla «Ejemplo documental» de Configuración a través del BFF. Va bajo `/tipos-documentales`, detrás de la llave de servicio Y del
JWT de la persona (HU11), como los tipos. Leer un ejemplo, etiquetarlo y medir lo puede quien administra tipos documentales (leer y medir
cuestan: son extracciones de Document AI); ver las etiquetas y el historial, quien opera en la organización.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from routers.errores import almacen_no_disponible
from servicios import aislamiento
from servicios import ejemplos_etiquetados as etiquetado
from servicios.almacen import ErrorAlmacen
from servicios.tipos_documentales_comun import NoEncontrado as TipoNoEncontrado

logger = logging.getLogger(__name__)

router = APIRouter()


class Pedido(BaseModel):
    tenant: str = Field(..., description="La organización")


class Etiquetas(BaseModel):
    tenant: str = Field(..., description="La organización")
    campos: list = Field(..., description="`[{ruta, valor (null = no aparece), pagina, caja, origen}]`")
    base: str | None = Field(None, max_length=400, description="Lo que devolvió `leer` en `base`; vacío si no se volvió a leer")


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, (etiquetado.NoEncontrado, TipoNoEncontrado)):
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc) or "Ese tipo documental no existe.")
    if isinstance(exc, etiquetado.Invalido):
        return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if isinstance(exc, etiquetado.SinLectura):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    if isinstance(exc, ErrorAlmacen):
        return almacen_no_disponible(exc)
    raise exc


@router.post(
    "/{tipo_id}/ejemplos/{ejemplo_id}/leer",
    summary="Leer un documento de ejemplo para etiquetarlo",
    description=(
        "Extrae el documento de ejemplo con la definición ACTUAL del tipo (su procesador si está activo; si no, el compartido, con el esquema del "
        "tipo) y devuelve `{ejemplo, revision, procesador, base, campos: [{ruta, campo, etiqueta, grupo, fila, tipoDato, valor, confianza, pagina, "
        "caja}]}`. Un campo que no apareció viene con `valor` null; algo que se repite sin ninguna fila, con `sinFilas`. `base` es lo que devolvió "
        "Google, guardado en el almacén: se manda de vuelta al guardar las etiquetas. Cuesta una extracción. Pide el rol que administra tipos."
    ),
)
async def leer(tipo_id: str, ejemplo_id: str, entrada: Pedido, authorization: str | None = Header(default=None)):
    aislamiento.exigir_administrar(authorization, entrada.tenant, "tipo_documental")
    try:
        return await etiquetado.leer(entrada.tenant, tipo_id, ejemplo_id)
    except (etiquetado.NoEncontrado, TipoNoEncontrado, etiquetado.Invalido, etiquetado.SinLectura, ErrorAlmacen) as exc:
        raise _error(exc) from exc


@router.put(
    "/{tipo_id}/ejemplos/{ejemplo_id}/etiquetas",
    summary="Guardar las etiquetas de un documento de ejemplo",
    description=(
        "La «respuesta buena» del documento: por campo, el valor correcto o null si no aparece en el documento, con su página y su caja (0 a 1000). "
        "Reemplaza las que tuviera. 400 si una ruta no es un campo del tipo. Pide el rol que administra tipos."
    ),
)
def guardar(tipo_id: str, ejemplo_id: str, entrada: Etiquetas, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, entrada.tenant, "tipo_documental")
    try:
        return etiquetado.guardar_etiquetas(entrada.tenant, tipo_id, ejemplo_id, entrada.campos, entrada.base, actor.guid)
    except (etiquetado.NoEncontrado, TipoNoEncontrado, etiquetado.Invalido, ErrorAlmacen) as exc:
        raise _error(exc) from exc


@router.delete(
    "/{tipo_id}/ejemplos/{ejemplo_id}/etiquetas",
    summary="Quitar las etiquetas de un documento de ejemplo",
    description="Al quitar el documento de ejemplo, o para empezar de nuevo. Pide el rol que administra tipos.",
)
def quitar(tipo_id: str, ejemplo_id: str, tenant: str = Query(...), authorization: str | None = Header(default=None)):
    aislamiento.exigir_administrar(authorization, tenant, "tipo_documental")
    try:
        return {"quitadas": etiquetado.borrar_etiquetas(tenant, tipo_id, ejemplo_id)}
    except (etiquetado.NoEncontrado, TipoNoEncontrado, etiquetado.Invalido, ErrorAlmacen) as exc:
        raise _error(exc) from exc


@router.get(
    "/{tipo_id}/etiquetas",
    summary="Las etiquetas de los documentos de ejemplo de un tipo",
    description="`{etiquetas: [{ejemplo, revision, campos, conBase, guardadoEn, guardadoPor}]}`, solo de los documentos que el tipo todavía tiene.",
)
def listar(tipo_id: str, tenant: str = Query(...), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "tipo_documental")
    try:
        return {"etiquetas": etiquetado.etiquetas(tenant, tipo_id)}
    except (etiquetado.NoEncontrado, TipoNoEncontrado, ErrorAlmacen) as exc:
        raise _error(exc) from exc


@router.post(
    "/{tipo_id}/mediciones",
    summary="Medir la precisión del tipo con sus documentos etiquetados",
    description=(
        f"Vuelve a extraer los documentos etiquetados (hasta {etiquetado.MAX_EJEMPLOS_POR_MEDICION}, los más recientes) con la definición actual del "
        "tipo y compara campo por campo contra sus etiquetas. Guarda la medición y la devuelve con su detalle (`detalle.campos`: aciertos, errores, "
        "faltaron y sobraron por campo; `detalle.documentos`: cada comparación). Cuesta una extracción por documento. 400 si no hay ninguno "
        "etiquetado. Pide el rol que administra tipos."
    ),
)
async def medir(tipo_id: str, entrada: Pedido, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, entrada.tenant, "tipo_documental")
    try:
        return await etiquetado.medir(entrada.tenant, tipo_id, actor.guid)
    except (etiquetado.NoEncontrado, TipoNoEncontrado, etiquetado.Invalido, ErrorAlmacen) as exc:
        raise _error(exc) from exc


@router.get(
    "/{tipo_id}/mediciones",
    summary="El historial de mediciones de un tipo",
    description="De la más reciente a la más antigua (hasta `limite`, 10 por omisión); la más reciente trae su detalle.",
)
async def historial(tipo_id: str, tenant: str = Query(...), limite: int = Query(10, ge=1, le=50), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "tipo_documental")
    try:
        return await run_in_threadpool(etiquetado.mediciones, tenant, tipo_id, limite)
    except (etiquetado.NoEncontrado, TipoNoEncontrado, ErrorAlmacen) as exc:
        raise _error(exc) from exc
