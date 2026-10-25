"""Dominio: procesos — el motor de reglas de cada organización (ver `servicios/procesos.py`).

Un proceso es un conjunto de reglas que corre sobre un expediente: se arma en el lienzo del front con los tipos documentales de la
organización como entradas y operadores (igual, distinto, contiene, mayor, menor, listas, existe, y, o, no, implica) como reglas. Aquí solo se GUARDA ese
dibujo: todavía no hay un motor que corra los procesos sobre un expediente de verdad.

Todas las rutas van detrás de la llave de servicio y exigen TAMBIÉN el JWT de la persona (HU11): el tenant lo manda la petición y se
comprueba contra el registro, igual que en tipos documentales, llaves, webhooks y bandeja.

## Qué se anota (auditoría)

Crear y borrar un proceso dejan su rastro en la bitácora, con quién lo hizo. Guardar a secas (mover un nodo, escribir un valor de
ejemplo) no: son muchos guardados y no cambian lo que el proceso ES.

## Lo que NO hace todavía

- **Solo los roles que administran** (`roles.PUEDEN_ADMINISTRAR`: Administrador y Supervisor, más el administrador de plataforma) leen y
  escriben procesos; cualquier otro rol de la organización recibe 403 `sin_permiso` (NEX-472, 2026-10-22).
- **No interpreta las reglas** ni las corre: las guarda tal cual, comprobando solo que el dibujo esté entero.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from routers.errores import almacen_no_disponible
from servicios import aislamiento, auditoria, procesos as registro_procesos
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_EXISTE = "Ese proceso no existe."


class Guardado(BaseModel):
    tenant: str = Field(..., description="La organización a la que pertenece el proceso")
    revision: int = Field(..., ge=0, description="La revisión sobre la que se editó; 0 si el proceso es nuevo")
    datos: dict = Field(..., description="El proceso completo: nombre, descripción, nodos y cables, tal como los maneja el lienzo")


def _anotar(accion: str, actor: aislamiento.Actor, tenant: str, proceso_id: str, **datos) -> None:
    """El rastro nunca cambia la respuesta: si la bitácora falla, se avisa en el log y la operación sigue."""
    try:
        auditoria.registrar(accion, usuario_guid=actor.guid, sesionGuid=actor.sesion, tenant=tenant, procesoId=proceso_id, **datos)
    except (ErrorAlmacen, OSError) as exc:
        logger.warning("No se pudo anotar %s en la bitácora de auditoría: %s", accion, exc)


@router.get(
    "/",
    tags=["Procesos"],
    summary="Listar los procesos de una organización",
    description="Todos los procesos de la organización, en el orden de su creación.",
)
def listar(tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir_administrar(authorization, tenant, "proceso")
    try:
        return {"procesos": registro_procesos.listar(tenant)}
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc


@router.put(
    "/{proceso_id}",
    tags=["Procesos"],
    summary="Crear o guardar un proceso",
    description=(
        "Con `revision: 0` crea el proceso; con otra, lo actualiza SI esa sigue siendo la revisión vigente. Si alguien lo guardó "
        "antes, responde 409 con `revisionActual`: hay que volver a leerlo y no se pisa nada. Devuelve el proceso con su "
        "revisión nueva."
    ),
)
def guardar(proceso_id: str, entrada: Guardado, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, entrada.tenant, "proceso")
    try:
        proceso, previo = registro_procesos.guardar(entrada.tenant, proceso_id, entrada.datos, entrada.revision, actor.guid)
    except registro_procesos.ProcesoInvalido as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except registro_procesos.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except registro_procesos.Conflicto as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": "conflicto",
                "mensaje": "Otra persona guardó este proceso antes que tú.",
                "revisionActual": exc.revision_actual,
            },
        ) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc

    if previo is None:
        _anotar(auditoria.PROCESS_CREATED, actor, entrada.tenant, proceso_id, nombre=proceso["datos"]["nombre"])
    return proceso


@router.delete(
    "/{proceso_id}",
    tags=["Procesos"],
    summary="Borrar un proceso",
    description="Lo quita de la organización.",
)
def borrar(proceso_id: str, tenant: str = Query(...), authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, tenant, "proceso")
    try:
        quitado = registro_procesos.eliminar(tenant, proceso_id, actor.guid)
    except registro_procesos.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    _anotar(auditoria.PROCESS_DELETED, actor, tenant, proceso_id, nombre=quitado["datos"]["nombre"])
    return {"eliminado": True}
