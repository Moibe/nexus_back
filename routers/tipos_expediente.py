"""Dominio: tipos de expediente — la clase de la que salen los expedientes (ver `servicios/tipos_expediente.py`).

«Expediente médico» y «expediente de un choque» son TIPOS; el expediente de UN paciente o de UN choque es una instancia de su tipo
(`/expedientes`) y guarda los documentos de ese caso. El tipo no guarda documentos.

Todas las rutas van detrás de la llave de servicio y exigen TAMBIÉN el JWT de la persona (HU11): el tenant lo manda la petición y se
comprueba contra el registro, igual que en tipos documentales, procesos y expedientes.

## Quién puede qué

- **Leer** (listar, ver un tipo): cualquiera que opere en la organización, porque quien organiza expedientes necesita elegir el tipo.
- **Crear**, **renombrar** y **borrar** un tipo y **definir su esquema**: `roles.PUEDEN_ADMINISTRAR` (Administrador y Supervisor), igual que la
  biblioteca de tipos documentales y los procesos: un tipo define cómo se arman los expedientes de la organización, no es trabajo de cada
  expediente. Propuesta de producto, pendiente de confirmar con César; los demás roles reciben 403 `sin_permiso`.

## Renombrar y borrar

Renombrar deja el `id` y los expedientes ven el nombre nuevo solos; el nombre sigue siendo único (otro tipo con ese nombre: 409 `ya_existe`; las
mayúsculas o los espacios del propio nombre sí se pueden cambiar). Borrar solo se puede si el tipo NO tiene expedientes: con expedientes, 409
`con_expedientes` (con cuántos), porque todo expediente es instancia de un tipo y no puede quedar sin él. Un tipo borrado responde 404 como uno
que nunca existió, y su nombre queda libre.

## El esquema describe, no restringe

Un tipo de expediente puede tener VARIOS tipos documentales de la misma organización, y ese conjunto es su esquema: describe lo que se espera de
sus expedientes. NO limita lo que un expediente recibe: cualquier documento entra a cualquier expediente, y los que no encajan quedan «sin tipo
documental» (hoy todos los documentos están así: ninguno guarda aún su tipo documental).

**Se define al crear el tipo** (`POST /` lo exige: al menos un tipo documental, 400 si la lista viene vacía, repetida, con uno que no existe o de
otra organización; sin el campo, 422). Después se reemplaza completo con `PUT /{id}/esquema`, que no cambió y todavía acepta una lista vacía.

## Qué se anota (auditoría)

`CASE_TYPE_CREATED` al crear un tipo (con su esquema en `tiposDocumentales`; el esquema inicial NO deja además un `CASE_TYPE_SCHEMA_CHANGED`),
`CASE_TYPE_SCHEMA_CHANGED` al cambiar su esquema después (con el esquema `antes` y `despues`), `CASE_TYPE_RENAMED` al renombrarlo (con el nombre
`antes` y `despues`) y `CASE_TYPE_DELETED` al borrarlo, con quién lo hizo. Repetir el mismo esquema o el mismo nombre no repite el rastro.

## Lo que NO hace todavía

No borra un tipo que tiene expedientes (no hay cómo borrar ni mover expedientes). No asigna un tipo documental a cada documento (ni hay cómo
saber qué falta de un esquema en un expediente): eso viene después, con la clasificación guardada en el documento.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from routers.errores import almacen_no_disponible
from servicios import aislamiento, auditoria, expedientes, tipos_expediente
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
            "en el orden en que se muestran. Reemplaza al esquema anterior; una lista vacía lo deja sin esquema."
        ),
    )


class NuevoNombre(BaseModel):
    tenant: str = Field(..., description="La organización a la que pertenece el tipo")
    nombre: str = Field(..., description="El nombre nuevo del tipo de expediente (hasta 200 caracteres)")


def _con_total(tipo: dict, cuenta: dict[str, int], nombres: dict[str, str | None]) -> dict:
    return {**tipos_expediente.con_esquema_resuelto(tipo, nombres), "totalExpedientes": cuenta.get(tipo["id"], 0)}


def _nombre_repetido() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"codigo": "ya_existe", "mensaje": "Ya hay un tipo de expediente con ese nombre."})


@router.get(
    "/",
    tags=["Tipos de expediente"],
    summary="Listar los tipos de expediente de una organización",
    description="Todos los tipos de la organización, en el orden de su creación, cada uno con su esquema y cuántos expedientes tiene.",
)
def listar(tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "tipo de expediente")
    try:
        cuenta = expedientes.contar_por_tipo(tenant)
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
        cuenta = expedientes.contar_por_tipo(tenant) if tipo else {}
        nombres = tipos_expediente.nombres_de_tipos_documentales(tenant) if tipo else {}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    if tipo is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE)
    return _con_total(tipo, cuenta, nombres)


@router.put(
    "/{tipo_id}/esquema",
    tags=["Tipos de expediente"],
    summary="Definir el esquema de un tipo de expediente",
    description=(
        "Reemplaza el esquema del tipo por esa lista de tipos documentales, en ese orden. Es DESCRIPTIVO: no limita los archivos que sus "
        "expedientes pueden recibir; los que no encajan quedan «sin tipo documental». Idempotente: con la misma lista responde `cambiado: "
        "false` y no hace nada. Una lista vacía deja el tipo sin esquema."
    ),
)
def definir_esquema(tipo_id: str, entrada: NuevoEsquema, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, entrada.tenant, "tipo de expediente")
    try:
        tipo, anterior = tipos_expediente.definir_esquema(entrada.tenant, tipo_id, entrada.tiposDocumentales, actor.guid)
        cuenta = expedientes.contar_por_tipo(entrada.tenant)
        nombres = tipos_expediente.nombres_de_tipos_documentales(entrada.tenant)
    except tipos_expediente.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except tipos_expediente.TipoInvalido as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    cambiado = tipo["tiposDocumentales"] != anterior
    if cambiado:
        _anotar(auditoria.CASE_TYPE_SCHEMA_CHANGED, actor, entrada.tenant, tipoId=tipo_id, nombre=tipo["nombre"], antes=anterior, despues=tipo["tiposDocumentales"])
    return {"cambiado": cambiado, "tipo": _con_total(tipo, cuenta, nombres)}


@router.patch(
    "/{tipo_id}",
    tags=["Tipos de expediente"],
    summary="Renombrar un tipo de expediente",
    description=(
        "Le cambia el nombre al tipo; su `id` no cambia y sus expedientes ven el nombre nuevo. El nombre sigue siendo único en la organización, "
        "sin distinguir mayúsculas ni espacios de más: otro tipo con ese nombre responde 409 `ya_existe` (cambiar solo las mayúsculas del propio "
        "nombre sí se puede). Idempotente: con el mismo nombre responde `cambiado: false` y no hace nada."
    ),
)
def renombrar(tipo_id: str, entrada: NuevoNombre, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, entrada.tenant, "tipo de expediente")
    try:
        tipo, anterior = tipos_expediente.renombrar(entrada.tenant, tipo_id, entrada.nombre, actor.guid)
        cuenta = expedientes.contar_por_tipo(entrada.tenant)
        nombres = tipos_expediente.nombres_de_tipos_documentales(entrada.tenant)
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
    cambiado = tipo["nombre"] != anterior
    if cambiado:
        _anotar(auditoria.CASE_TYPE_RENAMED, actor, entrada.tenant, tipoId=tipo_id, antes=anterior, despues=tipo["nombre"])
    return {"cambiado": cambiado, "tipo": _con_total(tipo, cuenta, nombres)}


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
        borrado = tipos_expediente.borrar(tenant, tipo_id, actor.guid, lambda: expedientes.contar_por_tipo(tenant).get(tipo_id, 0))
    except tipos_expediente.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except tipos_expediente.TipoConExpedientes as exc:
        cuantos = f"{exc.total} expediente" if exc.total == 1 else f"{exc.total} expedientes"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"codigo": "con_expedientes", "mensaje": f"Este tipo de expediente tiene {cuantos}: no se puede borrar mientras los tenga.", "total": exc.total},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    _anotar(auditoria.CASE_TYPE_DELETED, actor, tenant, tipoId=tipo_id, nombre=borrado["nombre"])
    return {"borrado": True}
