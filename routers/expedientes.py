"""Dominio: expedientes — conjuntos de documentos de una organización (ver `servicios/expedientes.py`).

Un expediente es un RECEPTÁCULO de documentos (desde el 2026-10-29 no tiene tipo): la carpeta de un cliente o de un caso. Tiene un nombre y
reúne documentos; un documento puede estar en ninguno (el «void»), en uno o en varios. Los documentos NACEN al entrar al sistema
(`servicios/documentos.py`), no aquí: este router solo los organiza.

Dentro de un expediente viven sus EXPEDIENTES VIRTUALES (`/expedientes/{id}/virtuales`, ver `servicios/expedientes_virtuales.py`): grupitos
de sus documentos, cada uno instancia de un tipo de expediente virtual (`/tipos-expediente`), que son lo que usará el motor de reglas. Solo
viven en SQL: sin base, sus rutas responden 503.

Todas las rutas van detrás de la llave de servicio y exigen TAMBIÉN el JWT de la persona (HU11): el tenant lo manda la petición y se
comprueba contra el registro, igual que en tipos documentales, procesos y bandeja.

## Quién puede qué

- **Leer** (listar, ver un expediente, listar documentos): cualquiera que opere en la organización.
- **Organizar** (crear un expediente, meter o sacar un documento; crear, llenar y borrar sus expedientes virtuales): `roles.PUEDEN_ORGANIZAR`
  (Administrador, Supervisor, Analista y Operador); Compliance, Auditor y Viewer reciben 403 `sin_permiso`. Es una propuesta de producto
  pendiente de confirmar con César.

## Qué se anota (auditoría)

`CASE_CREATED`, `CASE_DOCUMENT_ADDED` y `CASE_DOCUMENT_REMOVED`, con quién lo hizo; de los virtuales, `VIRTUAL_CASE_CREATED`,
`VIRTUAL_CASE_DOCUMENT_ADDED`, `VIRTUAL_CASE_DOCUMENT_REMOVED` y `VIRTUAL_CASE_DELETED`. Meter un documento que ya estaba no repite el rastro.

## Lo que NO hace todavía

No renombra ni borra expedientes, no tiene estado ni folio, y un documento no se descarta desde aquí. Se agregan cuando haga falta.
"""

import logging

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from routers.archivos import guardar_subida
from routers.bandeja import MIME_BANDEJA
from routers.errores import almacen_no_disponible
from servicios import aislamiento, auditoria, documentos, expedientes, expedientes_virtuales
from servicios.almacen import ErrorAlmacen
from servicios.subidas import normalizar_tipo

logger = logging.getLogger(__name__)

router = APIRouter()

_NO_EXISTE = "Ese expediente no existe."
_VIRTUAL_NO_EXISTE = "Ese expediente virtual no existe."
_TIPO_NO_EXISTE = "Ese tipo de expediente virtual no existe."
_DOCUMENTO_NO_EXISTE = "Ese documento no existe."
_DOCUMENTO_NO_ESTA = "Ese documento no está en el expediente."
_DOCUMENTO_NO_ESTA_EN_VIRTUAL = "Ese documento no está en el expediente virtual."


class NuevoExpediente(BaseModel):
    tenant: str = Field(..., description="La organización a la que pertenece el expediente")
    nombre: str = Field(..., description="Cómo se llama el expediente (hasta 200 caracteres)")


class NuevoVirtual(BaseModel):
    tenant: str = Field(..., description="La organización")
    tipoId: str = Field(..., description="El tipo de expediente virtual del que es instancia (`/tipos-expediente`), de la misma organización")
    nombre: str = Field(..., description="Cómo se llama (hasta 200 caracteres); dos pueden llamarse igual")
    documentos: list[str] = Field(
        default_factory=list,
        description="Los ids de los documentos DEL EXPEDIENTE con que nace (pueden ser ninguno). Uno que no esté en el expediente: 404 y no se crea nada.",
    )


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
        "Todos los expedientes de la organización, en el orden de su creación, cada uno con cuántos documentos tiene "
        "(`totalDocumentos`) y cuánto pesan en total (`tamanoBytes`)."
    ),
)
def listar(tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "expediente")
    try:
        return {"expedientes": expedientes.listar(tenant)}
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    tags=["Expedientes"],
    summary="Crear un expediente",
    description="Crea un expediente vacío con ese nombre. Dos expedientes pueden llamarse igual. No tiene tipo: lo tienen sus expedientes virtuales.",
)
def crear(entrada: NuevoExpediente, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_organizar(authorization, entrada.tenant, "expediente")
    try:
        creado = expedientes.crear(entrada.tenant, entrada.nombre, actor.guid)
    except expedientes.ExpedienteInvalido as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    _anotar(auditoria.CASE_CREATED, actor, entrada.tenant, expedienteId=creado["id"], nombre=creado["nombre"])
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


@router.post(
    "/documentos",
    status_code=status.HTTP_201_CREATED,
    tags=["Expedientes"],
    summary="Registrar el documento de una carga manual",
    description=(
        "Lo que la persona carga a mano en el Pipeline del front también es un documento de su organización, igual que lo que un "
        "cliente manda por la API: guarda los bytes en el almacén y anota el documento con `canal: MANUAL`, que es lo que lo hace "
        "aparecer en «Archivos procesados». Mismo formato que acepta la bandeja (PDF, JPEG, PNG y TIFF) y mismos límites de tamaño. "
        "Subir dos veces el mismo archivo crea DOS documentos que comparten los bytes, como por la API."
    ),
)
def registrar_documento_manual(
    archivo: UploadFile = File(...),
    tenant: str = Form(..., description="La organización de quien carga"),
    sha256: str | None = Form(None, description="Opcional. El SHA-256 del archivo, para verificar la transferencia.", examples=[""]),
    authorization: str | None = Header(default=None),
):
    # Quien carga a mano es una persona de la organización, no un cliente con API Key: se le exige la sesión, igual que a `/archivos`.
    aislamiento.exigir(authorization, tenant, "documento")
    mime = normalizar_tipo(archivo.content_type)
    if mime not in MIME_BANDEJA:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Formato no admitido. Se aceptan PDF, JPEG, PNG y TIFF.",
        )
    guardado = guardar_subida(archivo, tenant, sha256)
    try:
        return documentos.nacer(tenant, guardado, mime, archivo.filename or "", "MANUAL", None)
    except ErrorAlmacen as exc:
        # Los bytes ya quedaron en el almacén; sin registro son un objeto suelto, barato y deduplicado: reintentar lo registra.
        raise almacen_no_disponible(exc) from exc


@router.get(
    "/{expediente_id}",
    tags=["Expedientes"],
    summary="Ver un expediente con sus documentos y sus expedientes virtuales",
    description=(
        "El expediente y sus documentos completos, en el orden en que se agregaron, y `expedientesVirtuales`: cada uno con su tipo y los ids "
        "de sus documentos (vacío donde no hay base)."
    ),
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
    description="El documento NO se borra: si era su único expediente, vuelve al void. Sale también de los expedientes virtuales de ese expediente.",
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


# ── Expedientes virtuales ─────────────────────────────────────────────────────────────────────────────────────────────────────


def _sin_base(exc: expedientes_virtuales.SinBase) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


def _virtual_o_404(tenant: str, expediente_id: str, virtual_id: str) -> dict:
    virtual = expedientes_virtuales.obtener(tenant, expediente_id, virtual_id)
    if virtual is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_VIRTUAL_NO_EXISTE)
    return virtual


@router.post(
    "/{expediente_id}/virtuales",
    status_code=status.HTTP_201_CREATED,
    tags=["Expedientes"],
    summary="Crear un expediente virtual dentro de un expediente",
    description=(
        "Un expediente virtual es un grupito de documentos DE ESTE EXPEDIENTE, instancia de un tipo de expediente virtual (`tipoId`, de "
        "`/tipos-expediente`): por ejemplo, la «Solicitud de crédito» de Juan Pérez. Nace con los `documentos` elegidos (pueden ser ninguno); "
        "uno que no esté en el expediente responde 404 y no se crea nada. Un expediente puede tener varios, del mismo tipo o de distintos. "
        "Solo vive en SQL: sin base responde 503."
    ),
)
def crear_virtual(expediente_id: str, entrada: NuevoVirtual, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_organizar(authorization, entrada.tenant, "expediente")
    try:
        creado = expedientes_virtuales.crear(entrada.tenant, expediente_id, entrada.tipoId, entrada.nombre, entrada.documentos, actor.guid)
    except expedientes_virtuales.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except expedientes_virtuales.TipoNoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_TIPO_NO_EXISTE) from exc
    except expedientes_virtuales.DocumentoNoEnExpediente as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_DOCUMENTO_NO_ESTA) from exc
    except expedientes_virtuales.VirtualInvalido as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except expedientes_virtuales.SinBase as exc:
        raise _sin_base(exc) from exc
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    _anotar(
        auditoria.VIRTUAL_CASE_CREATED,
        actor,
        entrada.tenant,
        expedienteVirtualId=creado["id"],
        nombre=creado["nombre"],
        expedienteId=expediente_id,
        tipoId=creado["tipo"]["id"],
        tipoNombre=creado["tipo"]["nombre"],
        documentos=[d["id"] for d in creado["documentos"]],
    )
    return creado


@router.get(
    "/{expediente_id}/virtuales/{virtual_id}",
    tags=["Expedientes"],
    summary="Ver un expediente virtual",
    description="El expediente virtual con su tipo (y el esquema del tipo), su expediente y sus documentos completos, en el orden en que se agregaron.",
)
def ver_virtual(expediente_id: str, virtual_id: str, tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "expediente")
    try:
        return _virtual_o_404(tenant, expediente_id, virtual_id)
    except expedientes_virtuales.SinBase as exc:
        raise _sin_base(exc) from exc
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc


@router.put(
    "/{expediente_id}/virtuales/{virtual_id}/documentos/{documento_id}",
    tags=["Expedientes"],
    summary="Meter un documento del expediente a uno de sus expedientes virtuales",
    description=(
        "El documento tiene que estar en el expediente (si no, 404). Idempotente: si ya estaba en el virtual, responde `agregado: false` y no "
        "hace nada."
    ),
)
def agregar_documento_virtual(
    expediente_id: str,
    virtual_id: str,
    documento_id: str,
    tenant: str = Query(..., description="La organización"),
    authorization: str | None = Header(default=None),
):
    actor = aislamiento.exigir_organizar(authorization, tenant, "expediente")
    try:
        agregado = expedientes_virtuales.agregar_documento(tenant, expediente_id, virtual_id, documento_id, actor.guid)
        virtual = _virtual_o_404(tenant, expediente_id, virtual_id)
    except expedientes_virtuales.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_VIRTUAL_NO_EXISTE) from exc
    except expedientes_virtuales.DocumentoNoEnExpediente as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_DOCUMENTO_NO_ESTA) from exc
    except expedientes_virtuales.SinBase as exc:
        raise _sin_base(exc) from exc
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    if agregado:
        documento = next((d for d in virtual["documentos"] if d["id"] == documento_id), None)
        _anotar(
            auditoria.VIRTUAL_CASE_DOCUMENT_ADDED,
            actor,
            tenant,
            expedienteVirtualId=virtual_id,
            nombre=virtual["nombre"],
            expedienteId=expediente_id,
            documentoId=documento_id,
            nombreOriginal=documento["nombreOriginal"] if documento else None,
        )
    return {"agregado": agregado, "expedienteVirtual": virtual}


@router.delete(
    "/{expediente_id}/virtuales/{virtual_id}/documentos/{documento_id}",
    tags=["Expedientes"],
    summary="Sacar un documento de un expediente virtual",
    description="El documento sigue en el expediente: solo deja de ser parte de ese grupito.",
)
def quitar_documento_virtual(
    expediente_id: str,
    virtual_id: str,
    documento_id: str,
    tenant: str = Query(..., description="La organización"),
    authorization: str | None = Header(default=None),
):
    actor = aislamiento.exigir_organizar(authorization, tenant, "expediente")
    try:
        antes = _virtual_o_404(tenant, expediente_id, virtual_id)
        expedientes_virtuales.quitar_documento(tenant, expediente_id, virtual_id, documento_id)
    except expedientes_virtuales.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_VIRTUAL_NO_EXISTE) from exc
    except expedientes_virtuales.DocumentoNoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_DOCUMENTO_NO_ESTA_EN_VIRTUAL) from exc
    except expedientes_virtuales.SinBase as exc:
        raise _sin_base(exc) from exc
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    documento = next((d for d in antes["documentos"] if d["id"] == documento_id), None)
    _anotar(
        auditoria.VIRTUAL_CASE_DOCUMENT_REMOVED,
        actor,
        tenant,
        expedienteVirtualId=virtual_id,
        nombre=antes["nombre"],
        expedienteId=expediente_id,
        documentoId=documento_id,
        nombreOriginal=documento["nombreOriginal"] if documento else None,
    )
    return {"quitado": True}


@router.delete(
    "/{expediente_id}/virtuales/{virtual_id}",
    tags=["Expedientes"],
    summary="Borrar un expediente virtual",
    description="Borra el grupito, NO sus documentos: siguen en el expediente. Uno que no existe responde 404.",
)
def borrar_virtual(expediente_id: str, virtual_id: str, tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_organizar(authorization, tenant, "expediente")
    try:
        borrado = expedientes_virtuales.borrar(tenant, expediente_id, virtual_id)
    except expedientes_virtuales.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_VIRTUAL_NO_EXISTE) from exc
    except expedientes_virtuales.SinBase as exc:
        raise _sin_base(exc) from exc
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    _anotar(auditoria.VIRTUAL_CASE_DELETED, actor, tenant, expedienteVirtualId=virtual_id, nombre=borrado["nombre"], expedienteId=expediente_id)
    return {"borrado": True}
