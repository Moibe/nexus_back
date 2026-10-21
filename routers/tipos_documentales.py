"""Dominio: tipos documentales — el catálogo de cada organización (ver `servicios/tipos_documentales.py`).

Lo lee y lo escribe la pantalla de Configuración a través del BFF. Todas las rutas van detrás de la llave de servicio y
exigen TAMBIÉN el JWT de la persona (HU11): el tenant lo manda la petición y se comprueba contra el registro, igual que en
llaves, webhooks y bandeja.

## Qué se anota (auditoría)

Crear, borrar, cambiar de estado (borrador ↔ activo ↔ archivado) y CALIBRAR un tipo dejan su rastro en la bitácora, con quién
lo hizo. Guardar a secas (editar un campo, subir un ejemplo) no: son muchos guardados y no cambian lo que el tipo ES.

## Lo que NO hace todavía

- **No distingue roles.** Cualquier persona que pueda operar en la organización lee y escribe sus tipos, como en llaves y
  webhooks. Cuando haya un rol que configure (hoy el catálogo de HU07 no tiene uno), se exige aquí.
- **No toca Google.** Guardar un tipo no crea ni borra procesadores: eso lo hacen `/procesadores/*`, que el front llama aparte.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from routers.errores import almacen_no_disponible
from servicios import aislamiento, auditoria, tipos_documentales as registro_tipos
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_EXISTE = "Ese tipo documental no existe."


class Guardado(BaseModel):
    tenant: str = Field(..., description="La organización a la que pertenece el tipo")
    revision: int = Field(..., ge=0, description="La revisión sobre la que se editó; 0 si el tipo es nuevo")
    datos: dict = Field(..., description="El tipo documental completo, tal como lo maneja la pantalla de Configuración")


class Orden(BaseModel):
    tenant: str
    ids: list[str] = Field(..., max_length=500)


def _anotar(accion: str, actor: aislamiento.Actor, tenant: str, tipo_id: str, **datos) -> None:
    """El rastro nunca cambia la respuesta: si la bitácora falla, se avisa en el log y la operación sigue."""
    try:
        auditoria.registrar(accion, usuario_guid=actor.guid, sesionGuid=actor.sesion, tenant=tenant, tipoId=tipo_id, **datos)
    except (ErrorAlmacen, OSError) as exc:
        logger.warning("No se pudo anotar %s en la bitácora de auditoría: %s", accion, exc)


@router.get(
    "/",
    tags=["Tipos documentales"],
    summary="Listar los tipos documentales de una organización",
    description="Todos los de la organización —borradores, activos y archivados—, en el orden que ella les dio.",
)
def listar(tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "tipo_documental")
    try:
        return {"tipos": registro_tipos.listar(tenant)}
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc


@router.put(
    "/orden",
    tags=["Tipos documentales"],
    summary="Fijar el orden de la biblioteca",
    description="Guarda el orden en que la organización quiere ver sus tipos. Los ids que no son suyos se ignoran.",
)
def ordenar(entrada: Orden, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir(authorization, entrada.tenant, "tipo_documental")
    try:
        registro_tipos.ordenar(entrada.tenant, entrada.ids, actor.guid)
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    return {"ok": True}


@router.put(
    "/{tipo_id}",
    tags=["Tipos documentales"],
    summary="Crear o guardar un tipo documental",
    description=(
        "Con `revision: 0` crea el tipo; con otra, lo actualiza SI esa sigue siendo la revisión vigente. Si alguien lo guardó "
        "antes, responde 409 con `revisionActual`: hay que volver a leerlo y no se pisa nada. Devuelve el tipo con su "
        "revisión nueva."
    ),
)
def guardar(tipo_id: str, entrada: Guardado, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir(authorization, entrada.tenant, "tipo_documental")
    try:
        tipo, previo = registro_tipos.guardar(entrada.tenant, tipo_id, entrada.datos, entrada.revision, actor.guid)
    except registro_tipos.TipoInvalido as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except registro_tipos.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except registro_tipos.Conflicto as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "codigo": "conflicto",
                "mensaje": "Otra persona guardó este tipo documental antes que tú.",
                "revisionActual": exc.revision_actual,
            },
        ) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc

    nuevo = tipo["datos"]
    if previo is None:
        _anotar(auditoria.DOCUMENT_TYPE_CREATED, actor, entrada.tenant, tipo_id, nombre=nuevo["nombre"], estado=nuevo["estado"])
    else:
        antes = previo["datos"]
        if antes["estado"] != nuevo["estado"]:
            _anotar(auditoria.DOCUMENT_TYPE_STATE_CHANGED, actor, entrada.tenant, tipo_id, nombre=nuevo["nombre"], de=antes["estado"], a=nuevo["estado"])
        # Calibrar es lo que permite que un tipo extraiga: quién y cuándo importa.
        if nuevo.get("calibradoEn") and nuevo.get("calibradoEn") != antes.get("calibradoEn"):
            _anotar(auditoria.DOCUMENT_TYPE_CALIBRATED, actor, entrada.tenant, tipo_id, nombre=nuevo["nombre"], version=nuevo.get("version"))
    return tipo


@router.delete(
    "/{tipo_id}",
    tags=["Tipos documentales"],
    summary="Borrar un tipo documental",
    description="Lo quita de la organización. No borra su procesador en Document AI: eso se hace aparte, ANTES, con `/procesadores`.",
)
def borrar(tipo_id: str, tenant: str = Query(...), authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir(authorization, tenant, "tipo_documental")
    try:
        quitado = registro_tipos.eliminar(tenant, tipo_id, actor.guid)
    except registro_tipos.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    _anotar(auditoria.DOCUMENT_TYPE_DELETED, actor, tenant, tipo_id, nombre=quitado["datos"]["nombre"], estado=quitado["datos"]["estado"])
    return {"eliminado": True}
