"""Dominio: tipos de expediente — la clase de la que salen los expedientes (ver `servicios/tipos_expediente.py`).

«Expediente médico» y «expediente de un choque» son TIPOS; el expediente de UN paciente o de UN choque es una instancia de su tipo
(`/expedientes`) y guarda los documentos de ese caso. El tipo no guarda documentos.

Todas las rutas van detrás de la llave de servicio y exigen TAMBIÉN el JWT de la persona (HU11): el tenant lo manda la petición y se
comprueba contra el registro, igual que en tipos documentales, procesos y expedientes.

## Quién puede qué

- **Leer** (listar, ver un tipo): cualquiera que opere en la organización, porque quien organiza expedientes necesita elegir el tipo.
- **Crear** un tipo y **definir su esquema**: `roles.PUEDEN_ADMINISTRAR` (Administrador y Supervisor), igual que la biblioteca de tipos
  documentales y los procesos: un tipo define cómo se arman los expedientes de la organización, no es trabajo de cada expediente. Propuesta de
  producto, pendiente de confirmar con César; los demás roles reciben 403 `sin_permiso`.

## El esquema describe, no restringe

Un tipo de expediente puede llevar tipos documentales de la misma organización que describen lo que se espera de sus expedientes. NO limita
lo que un expediente recibe: cualquier documento entra a cualquier expediente, y los que no encajan quedan «sin tipo documental» (hoy todos los
documentos están así: ninguno guarda aún su tipo documental).

## Qué se anota (auditoría)

`CASE_TYPE_CREATED` al crear un tipo y `CASE_TYPE_SCHEMA_CHANGED` al cambiar su esquema (con el esquema `antes` y `despues`), con quién lo hizo.
Definir el mismo esquema que ya tenía no repite el rastro.

## Lo que NO hace todavía

No renombra ni borra tipos. No asigna un tipo documental a cada documento (ni hay cómo saber qué falta de un esquema en un expediente): eso
viene después, con la clasificación guardada en el documento.
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


def _con_total(tipo: dict, cuenta: dict[str, int], nombres: dict[str, str | None]) -> dict:
    return {**tipos_expediente.con_esquema_resuelto(tipo, nombres), "totalExpedientes": cuenta.get(tipo["id"], 0)}


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
        "Crea un tipo con ese nombre. El nombre es único en la organización, sin distinguir mayúsculas ni espacios de más: uno repetido "
        "responde 409 `ya_existe`."
    ),
)
def crear(entrada: NuevoTipo, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_administrar(authorization, entrada.tenant, "tipo de expediente")
    try:
        creado = tipos_expediente.crear(entrada.tenant, entrada.nombre, actor.guid)
    except tipos_expediente.NombreRepetido as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"codigo": "ya_existe", "mensaje": "Ya hay un tipo de expediente con ese nombre."},
        ) from exc
    except tipos_expediente.TipoInvalido as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    _anotar(auditoria.CASE_TYPE_CREATED, actor, entrada.tenant, tipoId=creado["id"], nombre=creado["nombre"])
    return _con_total(creado, {}, {})


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
