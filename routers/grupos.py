"""Dominio: grupos — el nivel de arriba de los expedientes (ver `servicios/grupos.py`).

Varios expedientes viven juntos en un grupo. Cada organización lo usa como quiere (áreas, departamentos, trimestres del año…), así que el nombre
genérico es «Grupo». Es UN SOLO nivel: un grupo tiene expedientes y nada más. El grupo se crea ANTES, en el módulo de configuración, y al crear un
expediente (`POST /expedientes`) se elige a cuál pertenece; los expedientes anteriores a los grupos se asignan después con `PUT /expedientes/{id}/grupo`.

Todas las rutas van detrás de la llave de servicio y exigen TAMBIÉN el JWT de la persona (HU11): el tenant lo manda la petición y se comprueba contra
el registro, igual que en tipos de expediente, procesos y expedientes. Solo viven en SQL: sin base, las rutas responden 503.

## Quién puede qué

- **Leer** (listar, ver un grupo): cualquiera que opere en la organización, porque quien organiza expedientes necesita elegir el grupo.
- **Crear**, **renombrar** y **borrar**: `roles.PUEDEN_ADMINISTRAR` (Administrador y Supervisor), igual que los tipos de expediente virtual: el grupo define cómo
  se ordenan los expedientes de toda la organización. Propuesta de producto, pendiente de confirmar con César; los demás roles reciben 403 `sin_permiso`.

## Nombre, renombrar y borrar

El nombre es único en la organización, sin distinguir mayúsculas ni espacios de más (otro con ese nombre: 409 `ya_existe`; cambiar solo las mayúsculas del
propio nombre sí se puede). Renombrar deja el `id`: los expedientes ven el nombre nuevo solos. Borrar solo se puede si el grupo NO tiene expedientes: un
expediente con grupo no puede quedarse sin él, así que hay que pasarlos a otro antes (409 `con_expedientes`, con cuántos tiene). Un grupo borrado
responde 404 como uno que nunca existió, y su nombre queda libre.

## Qué se anota (auditoría)

`CASE_GROUP_CREATED`, `CASE_GROUP_RENAMED` (con el nombre `antes` y `despues`; no se repite si el nombre era el mismo) y `CASE_GROUP_DELETED`, con quién lo hizo.
Asignar un expediente a un grupo (`CASE_GROUP_ASSIGNED`) se anota desde `routers/expedientes.py`.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from routers.errores import almacen_no_disponible
from servicios import aislamiento, auditoria, grupos
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_EXISTE = "Ese grupo no existe."


class NuevoGrupo(BaseModel):
    tenant: str = Field(..., description="La organización a la que pertenece el grupo")
    nombre: str = Field(..., description="Cómo se llama el grupo, por ejemplo «Ventas» o «T1 2026» (hasta 200 caracteres)")


class Renombrado(BaseModel):
    tenant: str = Field(..., description="La organización a la que pertenece el grupo")
    nombre: str = Field(..., description="El nombre nuevo del grupo (hasta 200 caracteres)")


def _anotar(accion: str, actor: aislamiento.Actor, tenant: str, **datos) -> None:
    """El rastro nunca cambia la respuesta: si la bitácora falla, se avisa en el log y la operación sigue."""
    try:
        auditoria.registrar(accion, usuario_guid=actor.guid, sesionGuid=actor.sesion, tenant=tenant, **datos)
    except (ErrorAlmacen, OSError) as exc:
        logger.warning("No se pudo anotar %s en la bitácora de auditoría: %s", accion, exc)


def _sin_base(exc: grupos.SinBase) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


def _nombre_repetido() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"codigo": "ya_existe", "mensaje": "Ya hay un grupo con ese nombre."})


@router.get(
    "/",
    tags=["Grupos"],
    summary="Listar los grupos de una organización",
    description="Todos los grupos de la organización, en el orden de su creación, cada uno con cuántos expedientes tiene (`totalExpedientes`).",
)
def listar(tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "grupo")
    try:
        return {"grupos": grupos.listar(tenant)}
    except grupos.SinBase as exc:
        raise _sin_base(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    tags=["Grupos"],
    summary="Crear un grupo",
    description=(
        "Crea un grupo vacío con ese nombre. El nombre es único en la organización, sin distinguir mayúsculas ni espacios de más: uno repetido responde "
        "409 `ya_existe`. Los expedientes se meten al grupo al crearlos (`POST /expedientes` con `grupoId`)."
    ),
)
def crear(entrada: NuevoGrupo, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, entrada.tenant, "grupo")
    try:
        creado = grupos.crear(entrada.tenant, entrada.nombre, actor.guid)
    except grupos.SinBase as exc:
        raise _sin_base(exc) from exc
    except grupos.NombreRepetido as exc:
        raise _nombre_repetido() from exc
    except grupos.GrupoInvalido as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    _anotar(auditoria.CASE_GROUP_CREATED, actor, entrada.tenant, grupoId=creado["id"], nombre=creado["nombre"])
    return creado


@router.get(
    "/{grupo_id}",
    tags=["Grupos"],
    summary="Ver un grupo",
    description="El grupo y cuántos expedientes tiene.",
)
def ver(grupo_id: str, tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "grupo")
    try:
        grupo = grupos.obtener(tenant, grupo_id)
    except grupos.SinBase as exc:
        raise _sin_base(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    if grupo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE)
    return grupo


@router.patch(
    "/{grupo_id}",
    tags=["Grupos"],
    summary="Renombrar un grupo",
    description=(
        "Le pone otro nombre; el `id` no cambia y sus expedientes ven el nombre nuevo. El nombre sigue siendo único en la organización, sin distinguir "
        "mayúsculas ni espacios de más: otro grupo con ese nombre responde 409 `ya_existe` (cambiar solo las mayúsculas del propio nombre sí se puede). "
        "Idempotente: con el mismo nombre responde `cambiado: false` y no escribe nada."
    ),
)
def renombrar(grupo_id: str, entrada: Renombrado, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, entrada.tenant, "grupo")
    try:
        grupo, anterior = grupos.renombrar(entrada.tenant, grupo_id, entrada.nombre, actor.guid)
    except grupos.SinBase as exc:
        raise _sin_base(exc) from exc
    except grupos.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except grupos.NombreRepetido as exc:
        raise _nombre_repetido() from exc
    except grupos.GrupoInvalido as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    cambiado = grupo["nombre"] != anterior
    if cambiado:
        _anotar(auditoria.CASE_GROUP_RENAMED, actor, entrada.tenant, grupoId=grupo_id, antes=anterior, despues=grupo["nombre"])
    return {"cambiado": cambiado, "grupo": grupo}


@router.delete(
    "/{grupo_id}",
    tags=["Grupos"],
    summary="Borrar un grupo",
    description=(
        "Borra el grupo y libera su nombre. Solo se puede si NO tiene expedientes: un expediente con grupo no puede quedarse sin él; con expedientes "
        "responde 409 `con_expedientes` y cuántos tiene. Un grupo borrado responde 404 como uno que nunca existió."
    ),
)
def borrar(grupo_id: str, tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, tenant, "grupo")
    try:
        borrado = grupos.borrar(tenant, grupo_id)
    except grupos.SinBase as exc:
        raise _sin_base(exc) from exc
    except grupos.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except grupos.GrupoConExpedientes as exc:
        cuantos = f"{exc.total} expediente" if exc.total == 1 else f"{exc.total} expedientes"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"codigo": "con_expedientes", "mensaje": f"Este grupo tiene {cuantos}: no se puede borrar mientras los tenga.", "total": exc.total},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    _anotar(auditoria.CASE_GROUP_DELETED, actor, tenant, grupoId=borrado["id"], nombre=borrado["nombre"])
    return {"borrado": True}
