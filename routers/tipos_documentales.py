"""Dominio: tipos documentales — el catálogo de cada organización (ver `servicios/tipos_documentales.py`).

Lo lee y lo escribe la pantalla de Configuración a través del BFF. Todas las rutas van detrás de la llave de servicio y
exigen TAMBIÉN el JWT de la persona (HU11): el tenant lo manda la petición y se comprueba contra el registro, igual que en
llaves, webhooks y bandeja.

## Qué se anota (auditoría)

Crear, borrar y cambiar de estado (borrador ↔ activo ↔ archivado) un tipo dejan su rastro en la bitácora, con quién
lo hizo. Guardar a secas (editar un campo, subir un ejemplo) no: son muchos guardados y no cambian lo que el tipo ES. Hasta el 2026-11-06 también se
anotaba CALIBRAR un tipo (`DOCUMENT_TYPE_CALIBRATED`); el paso «Calibrar» se quitó, así que ya no se escribe (lo que quedó en la bitácora se sigue leyendo).

## Sugerir una descripción (2026-11-05)

`POST /sugerir-descripcion` le pide a Gemini (Vertex AI, ver `servicios/gemini.py`) una descripción para el tipo que se está capturando, a partir
de su nombre y de los nombres de los demás tipos de la organización. No guarda nada: devuelve el texto y la persona lo revisa. Lo puede pedir
quien puede editar tipos (el mismo rol que guardarlos), porque cada llamada cuesta (centavos).

## Lo que NO hace todavía

- **No distingue roles.** Cualquier persona que pueda operar en la organización lee y escribe sus tipos, como en llaves y
  webhooks. Cuando haya un rol que configure (hoy el catálogo de HU07 no tiene uno), se exige aquí.
- **No toca Google.** Guardar un tipo no crea ni borra procesadores: eso lo hacen `/procesadores/*`, que el front llama aparte.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from routers.errores import almacen_no_disponible
from servicios import aislamiento, auditoria, gemini, tipos_documentales as registro_tipos
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


class PedidoSugerencia(BaseModel):
    tenant: str = Field(..., description="La organización")
    nombre: str = Field(..., max_length=200, description="El nombre del tipo documental que se está capturando")
    tipoId: str | None = Field(None, description="El tipo que se está editando, si ya existe: no cuenta entre «los otros tipos»")


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
    # LEER la biblioteca sigue abierto a todo el que opera en la organización (el pipeline la necesita para clasificar); lo que pide
    # el rol de administración son los cambios (NEX-472).
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
    actor = aislamiento.exigir_administrar(authorization, entrada.tenant, "tipo_documental")
    try:
        registro_tipos.ordenar(entrada.tenant, entrada.ids, actor.guid)
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    return {"ok": True}


@router.post(
    "/sugerir-descripcion",
    tags=["Tipos documentales"],
    summary="Sugerir la descripción de un tipo documental (Gemini)",
    description=(
        "A partir del nombre del tipo y de los nombres de los demás tipos de la organización (los archivados y el propio `tipoId` no cuentan), "
        "Gemini propone una descripción para el clasificador: qué documento es y cómo distinguirlo. Devuelve `{descripcion, modelo}`. No guarda "
        "nada. Lo puede pedir quien puede editar tipos. 400 si el nombre está en blanco; 503 si el servidor no tiene acceso a Gemini; 429 si "
        "Google pide esperar; 502 si falla, tarda o no devuelve texto."
    ),
)
async def sugerir_descripcion(entrada: PedidoSugerencia, authorization: str | None = Header(default=None)):
    aislamiento.exigir_administrar(authorization, entrada.tenant, "tipo_documental")
    nombre = " ".join(entrada.nombre.split())
    if not nombre:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Escribe primero el nombre del tipo documental.")
    try:
        tipos = await run_in_threadpool(registro_tipos.listar, entrada.tenant)
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    # Los «otros»: los que la organización ve (no archivados) y que no son este mismo tipo.
    otros = [t["datos"].get("nombre", "") for t in tipos if t["id"] != entrada.tipoId and t["datos"].get("estado") != "archivado"]
    try:
        return await gemini.sugerir_descripcion(nombre, otros)
    except gemini.ErrorSugerencia as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc


@router.put(
    "/{tipo_id}",
    tags=["Tipos documentales"],
    summary="Crear o guardar un tipo documental",
    description=(
        "Con `revision: 0` crea el tipo; con otra, lo actualiza SI esa sigue siendo la revisión vigente. Si alguien lo guardó "
        "antes, responde 409 `conflicto` con `revisionActual`: hay que volver a leerlo y no se pisa nada. El nombre es único en la "
        "organización (sin distinguir mayúsculas ni espacios de más; los archivados no cuentan): crear, renombrar o desarchivar un "
        "tipo con el nombre de otro responde 409 `ya_existe`. Guardar sin tocar el nombre no choca, aunque ya hubiera un duplicado "
        "de antes de esta regla. Devuelve el tipo con su revisión nueva."
    ),
)
def guardar(tipo_id: str, entrada: Guardado, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, entrada.tenant, "tipo_documental")
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
    except registro_tipos.NombreRepetido as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"codigo": "ya_existe", "mensaje": "Ya hay un tipo documental con ese nombre."},
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
        # Ya no se anota `DOCUMENT_TYPE_CALIBRATED` (2026-11-06): el paso «Calibrar» se quitó y un tipo activo con procesador extrae sin él. Si una caja
        # vieja todavía trae `calibradoEn` y cambia, es un guardado a secas.
    return tipo


@router.delete(
    "/{tipo_id}",
    tags=["Tipos documentales"],
    summary="Borrar un tipo documental",
    description="Lo quita de la organización. No borra su procesador en Document AI: eso se hace aparte, ANTES, con `/procesadores`.",
)
def borrar(tipo_id: str, tenant: str = Query(...), authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, tenant, "tipo_documental")
    try:
        quitado = registro_tipos.eliminar(tenant, tipo_id, actor.guid)
    except registro_tipos.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    _anotar(auditoria.DOCUMENT_TYPE_DELETED, actor, tenant, tipo_id, nombre=quitado["datos"]["nombre"], estado=quitado["datos"]["estado"])
    return {"eliminado": True}
