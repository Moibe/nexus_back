"""Dominio: expedientes — conjuntos de documentos de una organización (ver `servicios/expedientes.py`).

Un expediente es una INSTANCIA de un tipo de expediente (`/tipos-expediente`): el tipo «expediente médico» es la clase y el expediente de UN
paciente es la carpeta con SUS documentos. Tiene un nombre y reúne documentos; un documento puede estar en ninguno (el «void»), en uno o en
varios. Los documentos NACEN al entrar al sistema (`servicios/documentos.py`), no aquí: este router solo los organiza.

Todas las rutas van detrás de la llave de servicio y exigen TAMBIÉN el JWT de la persona (HU11): el tenant lo manda la petición y se
comprueba contra el registro, igual que en tipos documentales, procesos y bandeja.

## Quién puede qué

- **Leer** (listar, ver un expediente, listar documentos): cualquiera que opere en la organización.
- **Organizar** (crear un expediente, meter o sacar un documento): `roles.PUEDEN_ORGANIZAR` (Administrador, Supervisor, Analista y
  Operador); Compliance, Auditor y Viewer reciben 403 `sin_permiso`. Es una propuesta de producto pendiente de confirmar con César.

## Qué se anota (auditoría)

`CASE_CREATED`, `CASE_DOCUMENT_ADDED` y `CASE_DOCUMENT_REMOVED`, con quién lo hizo. Meter un documento que ya estaba no repite el rastro.

## Lo que NO hace todavía

No renombra ni borra expedientes, no tiene estado ni folio, y un documento no se descarta desde aquí. Se agregan cuando haga falta.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from routers.errores import almacen_no_disponible
from servicios import aislamiento, auditoria, documentos, expedientes
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_EXISTE = "Ese expediente no existe."
_TIPO_NO_EXISTE = "Ese tipo de expediente no existe."
_DOCUMENTO_NO_EXISTE = "Ese documento no existe."
_DOCUMENTO_NO_ESTA = "Ese documento no está en el expediente."


class NuevoExpediente(BaseModel):
    tenant: str = Field(..., description="La organización a la que pertenece el expediente")
    nombre: str = Field(..., description="Cómo se llama el expediente (hasta 200 caracteres)")
    tipoId: str = Field(..., description="El tipo de expediente del que es instancia (`/tipos-expediente`), de la misma organización")


def _anotar(accion: str, actor: aislamiento.Actor, tenant: str, **datos) -> None:
    """El rastro nunca cambia la respuesta: si la bitácora falla, se avisa en el log y la operación sigue."""
    try:
        auditoria.registrar(accion, usuario_guid=actor.guid, sesionGuid=actor.sesion, tenant=tenant, **datos)
    except (ErrorAlmacen, OSError) as exc:
        logger.warning("No se pudo anotar %s en la bitácora de auditoría: %s", accion, exc)


def _tenant_invalido(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.get(
    "/",
    tags=["Expedientes"],
    summary="Listar los expedientes de una organización",
    description=(
        "Todos los expedientes de la organización, en el orden de su creación, cada uno con su tipo, cuántos documentos tiene "
        "(`totalDocumentos`) y cuánto pesan en total (`tamanoBytes`). Con `tipo`, solo los de ese tipo de expediente."
    ),
)
def listar(
    tenant: str = Query(..., description="La organización"),
    tipo: str | None = Query(None, description="El id de un tipo de expediente: solo los expedientes de ese tipo"),
    authorization: str | None = Header(default=None),
):
    aislamiento.exigir(authorization, tenant, "expediente")
    try:
        return {"expedientes": expedientes.listar(tenant, tipo)}
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    tags=["Expedientes"],
    summary="Crear un expediente",
    description=(
        "Crea un expediente vacío con ese nombre, como instancia de un tipo de expediente de la misma organización (`tipoId`). Dos "
        "expedientes pueden llamarse igual."
    ),
)
def crear(entrada: NuevoExpediente, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_organizar(authorization, entrada.tenant, "expediente")
    try:
        creado = expedientes.crear(entrada.tenant, entrada.nombre, actor.guid, entrada.tipoId)
    except expedientes.TipoNoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_TIPO_NO_EXISTE) from exc
    except expedientes.ExpedienteInvalido as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    _anotar(auditoria.CASE_CREATED, actor, entrada.tenant, expedienteId=creado["id"], nombre=creado["nombre"], tipoId=creado["tipo"]["id"], tipoNombre=creado["tipo"]["nombre"])
    return creado


# Va ANTES de `/{expediente_id}`: si no, «documentos» se tomaría por el id de un expediente. Cuelga de /expedientes y no de /documentos
# porque ese prefijo ya es del grupo que habla con SQL Server (`routers/documentos.py`).
@router.get(
    "/documentos",
    tags=["Expedientes"],
    summary="Listar los documentos de una organización y en qué expedientes están",
    description=(
        "Los documentos de la organización, del más viejo al más nuevo, cada uno con los expedientes en que está (`id` y `nombre`). "
        "Con `sin_expediente=true`, solo los que no están en ninguno (el void); con `false`, solo los que están en al menos uno."
    ),
)
def listar_documentos(
    tenant: str = Query(..., description="La organización"),
    sin_expediente: bool | None = Query(None, description="`true` = solo el void; `false` = solo los que están en algún expediente"),
    authorization: str | None = Header(default=None),
):
    aislamiento.exigir(authorization, tenant, "expediente")
    try:
        return {"documentos": expedientes.listar_documentos(tenant, sin_expediente)}
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc


@router.get(
    "/{expediente_id}",
    tags=["Expedientes"],
    summary="Ver un expediente con sus documentos",
    description="El expediente y sus documentos completos, en el orden en que se agregaron.",
)
def ver(expediente_id: str, tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "expediente")
    try:
        detalle = expedientes.obtener(tenant, expediente_id)
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    if detalle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE)
    return detalle


@router.put(
    "/{expediente_id}/documentos/{documento_id}",
    tags=["Expedientes"],
    summary="Meter un documento a un expediente",
    description=(
        "Idempotente: si el documento ya estaba, responde `agregado: false` y no hace nada. Un documento puede estar en varios "
        "expedientes a la vez."
    ),
)
def agregar_documento(
    expediente_id: str,
    documento_id: str,
    tenant: str = Query(..., description="La organización"),
    authorization: str | None = Header(default=None),
):
    actor = aislamiento.exigir_organizar(authorization, tenant, "expediente")
    try:
        agregado = expedientes.agregar_documento(tenant, expediente_id, documento_id, actor.guid)
        detalle = expedientes.obtener(tenant, expediente_id)
        documento = documentos.obtener(tenant, documento_id)
    except expedientes.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except expedientes.DocumentoNoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_DOCUMENTO_NO_EXISTE) from exc
    except expedientes.ExpedienteInvalido as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    if agregado and detalle and documento:
        _anotar(
            auditoria.CASE_DOCUMENT_ADDED,
            actor,
            tenant,
            expedienteId=expediente_id,
            nombre=detalle["nombre"],
            documentoId=documento_id,
            nombreOriginal=documento["nombreOriginal"],
        )
    return {"agregado": agregado, "expediente": detalle}


@router.delete(
    "/{expediente_id}/documentos/{documento_id}",
    tags=["Expedientes"],
    summary="Sacar un documento de un expediente",
    description="El documento NO se borra: si era su único expediente, vuelve al void.",
)
def quitar_documento(
    expediente_id: str,
    documento_id: str,
    tenant: str = Query(..., description="La organización"),
    authorization: str | None = Header(default=None),
):
    actor = aislamiento.exigir_organizar(authorization, tenant, "expediente")
    try:
        antes = expedientes.obtener(tenant, expediente_id)
        documento = documentos.obtener(tenant, documento_id)
        expedientes.quitar_documento(tenant, expediente_id, documento_id, actor.guid)
    except expedientes.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except expedientes.DocumentoNoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_DOCUMENTO_NO_ESTA) from exc
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    if antes and documento:
        _anotar(
            auditoria.CASE_DOCUMENT_REMOVED,
            actor,
            tenant,
            expedienteId=expediente_id,
            nombre=antes["nombre"],
            documentoId=documento_id,
            nombreOriginal=documento["nombreOriginal"],
        )
    return {"quitado": True}
