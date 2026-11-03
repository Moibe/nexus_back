"""Dominio: tipos de expediente VIRTUAL — la clase de la que salen los expedientes virtuales (ver `servicios/tipos_expediente.py`).

Desde el 2026-10-29 el expediente no tiene tipo: es un receptáculo de documentos. Lo que es instancia de un tipo es el EXPEDIENTE VIRTUAL
(`/expedientes/{id}/virtuales`), un subconjunto de los documentos de un expediente: la «Solicitud de crédito» de Juan Pérez. La ruta sigue
siendo `/tipos-expediente` y los ids `tipo-expediente-…`; donde abajo dice «expediente» debe leerse «expediente virtual».
`totalExpedientesVirtuales` es cuántos expedientes virtuales tiene cada tipo (antes, `totalExpedientes`).

Todas las rutas van detrás de la llave de servicio y exigen TAMBIÉN el JWT de la persona (HU11): el tenant lo manda la petición y se
comprueba contra el registro, igual que en tipos documentales, procesos y expedientes.

## Quién puede qué

- **Leer** (listar, ver un tipo y ver sus expedientes virtuales, `GET /{id}/virtuales`): cualquiera que opere en la organización, porque quien
  organiza expedientes necesita elegir el tipo.
- **Crear**, **editar** (su nombre y su esquema) y **borrar** un tipo: `roles.PUEDEN_ADMINISTRAR` (Administrador y Supervisor), igual que la
  biblioteca de tipos documentales y los procesos: un tipo define cómo se arman los expedientes de la organización, no es trabajo de cada
  expediente. Propuesta de producto, pendiente de confirmar con César; los demás roles reciben 403 `sin_permiso`.

## Editar y borrar

Editar (`PATCH /{id}`) cambia el nombre, el esquema o los dos a la vez, y deja el `id`: los expedientes ven el nombre nuevo solos. El nombre sigue
siendo único (otro tipo con ese nombre: 409 `ya_existe`; las mayúsculas o los espacios del propio nombre sí se pueden cambiar). Todo se comprueba
antes de escribir: si el nombre se repite o un tipo documental no existe, no cambia ni lo uno ni lo otro. Borrar solo se puede si el tipo NO
tiene expedientes: con expedientes, 409 `con_expedientes` (con cuántos), porque todo expediente es instancia de un tipo y no puede quedar sin él.
Un tipo borrado responde 404 como uno que nunca existió, y su nombre queda libre.

## El esquema describe, no restringe

Un tipo de expediente puede tener VARIOS tipos documentales de la misma organización, y ese conjunto es su esquema: describe lo que se espera de
sus expedientes. NO limita lo que un expediente recibe: cualquier documento entra a cualquier expediente, y los que no encajan quedan «sin tipo
documental» (hoy todos los documentos están así: ninguno guarda aún su tipo documental).

**Se define al crear el tipo** (`POST /` lo exige: al menos un tipo documental, 400 si la lista viene vacía, repetida, con uno que no existe o de
otra organización; sin el campo, 422). Después se reemplaza completo con `PATCH /{id}` (con o sin el nombre) o con `PUT /{id}/esquema`, y los dos
piden lo mismo que al crear: una lista vacía responde 400, así que un tipo que ya tiene esquema nunca se queda sin él. (Los tipos anteriores a la
regla, sin esquema, siguen siéndolo hasta que alguien les ponga uno; renombrarlos no lo exige.)

## Qué se anota (auditoría)

`CASE_TYPE_CREATED` al crear un tipo (con su esquema en `tiposDocumentales`; el esquema inicial NO deja además un `CASE_TYPE_SCHEMA_CHANGED`),
`CASE_TYPE_SCHEMA_CHANGED` al cambiar su esquema después (con el esquema `antes` y `despues`), `CASE_TYPE_RENAMED` al renombrarlo (con el nombre
`antes` y `despues`) y `CASE_TYPE_DELETED` al borrarlo, con quién lo hizo. Una edición que cambia las dos cosas deja las dos anotaciones; repetir
el mismo esquema o el mismo nombre no repite el rastro.

## Lo que NO hace todavía

No borra un tipo que tiene expedientes (no hay cómo borrar ni mover expedientes). No asigna un tipo documental a cada documento (ni hay cómo
saber qué falta de un esquema en un expediente): eso viene después, con la clasificación guardada en el documento.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from routers.errores import almacen_no_disponible
from servicios import aislamiento, auditoria, expedientes_virtuales, tipos_expediente
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_EXISTE = "Ese tipo de expediente no existe."


class NuevoTipo(BaseModel):
    tenant: str = Field(..., description="La organización a la que pertenece el tipo")
    nombre: str = Field(..., description="Cómo se llama el tipo de expediente, por ejemplo «Expediente médico» (hasta 200 caracteres)")
    tiposDocumentales: list[str] = Field(
        ...,
        description=(
            "Los ids de los tipos documentales (de la biblioteca de la misma organización) que forman el esquema del tipo, en el orden en que "
            "se muestran. Es obligatorio: al menos uno, sin repetidos y hasta 50."
        ),
    )


def _anotar(accion: str, actor: aislamiento.Actor, tenant: str, **datos) -> None:
    """El rastro nunca cambia la respuesta: si la bitácora falla, se avisa en el log y la operación sigue."""
    try:
        auditoria.registrar(accion, usuario_guid=actor.guid, sesionGuid=actor.sesion, tenant=tenant, **datos)
    except (ErrorAlmacen, OSError) as exc:
        logger.warning("No se pudo anotar %s en la bitácora de auditoría: %s", accion, exc)


class NuevoEsquema(BaseModel):
    tenant: str = Field(..., description="La organización a la que pertenece el tipo")
    tiposDocumentales: list[str] = Field(
        ...,
        description=(
            "Los ids de los tipos documentales (de la biblioteca de la misma organización) que describen el esquema del tipo de expediente, "
            "en el orden en que se muestran. Reemplaza al esquema anterior; como al crear, al menos uno (una lista vacía responde 400)."
        ),
    )


class Edicion(BaseModel):
    tenant: str = Field(..., description="La organización a la que pertenece el tipo")
    nombre: str | None = Field(default=None, description="El nombre nuevo del tipo de expediente (hasta 200 caracteres); sin él, el nombre no se toca")
    tiposDocumentales: list[str] | None = Field(
        default=None,
        description=(
            "El esquema nuevo: los ids de los tipos documentales (de la biblioteca de la misma organización), en el orden en que se muestran. "
            "Reemplaza al anterior y, como al crear, pide al menos uno, sin repetidos y hasta 50; sin él, el esquema no se toca."
        ),
    )


def _con_total(tipo: dict, cuenta: dict[str, int], nombres: dict[str, str | None]) -> dict:
    return {**tipos_expediente.con_esquema_resuelto(tipo, nombres), "totalExpedientesVirtuales": cuenta.get(tipo["id"], 0)}


def _nombre_repetido() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"codigo": "ya_existe", "mensaje": "Ya hay un tipo de expediente con ese nombre."})


def _editar(tenant: str, tipo_id: str, nombre: str | None, tipos_documentales: list[str] | None, authorization: str | None) -> dict:
    """Lo que comparten el PUT del esquema y el PATCH: la edición, el rastro de lo que SÍ cambió y la respuesta."""
    actor = aislamiento.exigir_administrar(authorization, tenant, "tipo de expediente")
    try:
        tipo, nombre_anterior, esquema_anterior = tipos_expediente.editar(tenant, tipo_id, nombre, tipos_documentales, actor.guid)
        cuenta = expedientes_virtuales.contar_por_tipo(tenant)
        nombres = tipos_expediente.nombres_de_tipos_documentales(tenant)
    except tipos_expediente.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except tipos_expediente.NombreRepetido as exc:
        raise _nombre_repetido() from exc
    except tipos_expediente.TipoInvalido as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    nombre_cambiado = tipo["nombre"] != nombre_anterior
    esquema_cambiado = tipo["tiposDocumentales"] != esquema_anterior
    if nombre_cambiado:
        _anotar(auditoria.CASE_TYPE_RENAMED, actor, tenant, tipoId=tipo_id, antes=nombre_anterior, despues=tipo["nombre"])
    if esquema_cambiado:
        _anotar(auditoria.CASE_TYPE_SCHEMA_CHANGED, actor, tenant, tipoId=tipo_id, nombre=tipo["nombre"], antes=esquema_anterior, despues=tipo["tiposDocumentales"])
    return {
        "cambiado": nombre_cambiado or esquema_cambiado,
        "nombreCambiado": nombre_cambiado,
        "esquemaCambiado": esquema_cambiado,
        "tipo": _con_total(tipo, cuenta, nombres),
    }


@router.get(
    "/",
    tags=["Tipos de expediente"],
    summary="Listar los tipos de expediente de una organización",
    description="Todos los tipos de la organización, en el orden de su creación, cada uno con su esquema y cuántos expedientes tiene.",
)
def listar(tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "tipo de expediente")
    try:
        cuenta = expedientes_virtuales.contar_por_tipo(tenant)
        nombres = tipos_expediente.nombres_de_tipos_documentales(tenant)
        return {"tipos": [_con_total(t, cuenta, nombres) for t in tipos_expediente.listar(tenant)]}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    tags=["Tipos de expediente"],
    summary="Crear un tipo de expediente",
    description=(
        "Crea un tipo con ese nombre y su esquema: todo tipo se crea con al menos un tipo documental de la misma organización (sin repetidos, "
        "hasta 50); sin ninguno responde 400. El esquema es DESCRIPTIVO: no limita los archivos que sus expedientes pueden recibir. El nombre es "
        "único en la organización, sin distinguir mayúsculas ni espacios de más: uno repetido responde 409 `ya_existe`."
    ),
)
def crear(entrada: NuevoTipo, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, entrada.tenant, "tipo de expediente")
    try:
        creado = tipos_expediente.crear(entrada.tenant, entrada.nombre, entrada.tiposDocumentales, actor.guid)
        nombres = tipos_expediente.nombres_de_tipos_documentales(entrada.tenant)
    except tipos_expediente.NombreRepetido as exc:
        raise _nombre_repetido() from exc
    except tipos_expediente.TipoInvalido as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    _anotar(auditoria.CASE_TYPE_CREATED, actor, entrada.tenant, tipoId=creado["id"], nombre=creado["nombre"], tiposDocumentales=creado["tiposDocumentales"])
    return _con_total(creado, {}, nombres)


@router.get(
    "/{tipo_id}",
    tags=["Tipos de expediente"],
    summary="Ver un tipo de expediente",
    description="El tipo, su esquema (los tipos documentales que lo describen) y cuántos expedientes tiene.",
)
def ver(tipo_id: str, tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "tipo de expediente")
    try:
        tipo = tipos_expediente.obtener(tenant, tipo_id)
        cuenta = expedientes_virtuales.contar_por_tipo(tenant) if tipo else {}
        nombres = tipos_expediente.nombres_de_tipos_documentales(tenant) if tipo else {}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    if tipo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE)
    return _con_total(tipo, cuenta, nombres)


@router.get(
    "/{tipo_id}/virtuales",
    tags=["Tipos de expediente"],
    summary="Listar los expedientes virtuales de un tipo",
    description=(
        "Los expedientes virtuales de ese tipo en TODA la organización (cada uno vive dentro de un expediente), los más recientes primero. Cada "
        "uno trae su expediente (con cuántos documentos tiene) y cuántos de esos documentos lo forman. Es lo que hay detrás de la cuenta "
        "`totalExpedientesVirtuales`. Lo lee cualquiera que opere en la organización. Sin base, la lista viene vacía."
    ),
)
def listar_virtuales(tipo_id: str, tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "tipo de expediente")
    try:
        tipo = tipos_expediente.obtener(tenant, tipo_id)
        virtuales = expedientes_virtuales.listar_de_tipo(tenant, tipo_id) if tipo else []
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    if tipo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE)
    return {"tipo": {"id": tipo["id"], "nombre": tipo["nombre"]}, "expedientesVirtuales": virtuales}


@router.put(
    "/{tipo_id}/esquema",
    tags=["Tipos de expediente"],
    summary="Definir el esquema de un tipo de expediente",
    description=(
        "Reemplaza el esquema del tipo por esa lista de tipos documentales, en ese orden. Es DESCRIPTIVO: no limita los archivos que sus "
        "expedientes pueden recibir; los que no encajan quedan «sin tipo documental». Como al crear, pide al menos uno: una lista vacía "
        "responde 400 y el esquema anterior se queda como estaba. Idempotente: con la misma lista responde `cambiado: false` y no hace nada. "
        "(`PATCH /{id}` hace lo mismo y además puede cambiar el nombre en la misma llamada.)"
    ),
)
def definir_esquema(tipo_id: str, entrada: NuevoEsquema, authorization: str | None = Header(default=None)):
    return _editar(entrada.tenant, tipo_id, None, entrada.tiposDocumentales, authorization)


@router.patch(
    "/{tipo_id}",
    tags=["Tipos de expediente"],
    summary="Editar un tipo de expediente (nombre y/o esquema)",
    description=(
        "Cambia el nombre del tipo, su esquema o los dos en una llamada; lo que no se manda no se toca, y al menos uno tiene que venir (si no, 400). "
        "El `id` no cambia y sus expedientes ven el nombre nuevo. El nombre sigue siendo único en la organización, sin distinguir mayúsculas ni "
        "espacios de más: otro tipo con ese nombre responde 409 `ya_existe` (cambiar solo las mayúsculas del propio nombre sí se puede). El "
        "esquema REEMPLAZA al anterior y pide lo mismo que al crear: al menos un tipo documental de la organización, sin repetidos y hasta 50; "
        "una lista vacía responde 400. Si algo falla no cambia nada, ni el nombre ni el esquema. Idempotente: lo que ya estaba igual no se "
        "escribe, y la respuesta dice qué cambió (`nombreCambiado`, `esquemaCambiado`; `cambiado` es uno u otro)."
    ),
)
def editar(tipo_id: str, entrada: Edicion, authorization: str | None = Header(default=None)):
    return _editar(entrada.tenant, tipo_id, entrada.nombre, entrada.tiposDocumentales, authorization)


@router.delete(
    "/{tipo_id}",
    tags=["Tipos de expediente"],
    summary="Borrar un tipo de expediente",
    description=(
        "Borra el tipo y libera su nombre. Solo se puede si NO tiene expedientes: todo expediente es instancia de un tipo y no puede quedar sin "
        "él; con expedientes responde 409 `con_expedientes` y cuántos tiene. Un tipo borrado responde 404 como uno que nunca existió."
    ),
)
def borrar(tipo_id: str, tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, tenant, "tipo de expediente")
    try:
        borrado = tipos_expediente.borrar(tenant, tipo_id, actor.guid)
    except tipos_expediente.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except tipos_expediente.TipoConExpedientes as exc:
        cuantos = f"{exc.total} expediente virtual" if exc.total == 1 else f"{exc.total} expedientes virtuales"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"codigo": "con_expedientes", "mensaje": f"Este tipo de expediente virtual tiene {cuantos}: no se puede borrar mientras los tenga.", "total": exc.total},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    _anotar(auditoria.CASE_TYPE_DELETED, actor, tenant, tipoId=tipo_id, nombre=borrado["nombre"])
    return {"borrado": True}
