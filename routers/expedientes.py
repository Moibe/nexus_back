"""Dominio: expedientes — conjuntos de documentos de una organización (ver `servicios/expedientes.py`).

Un expediente es un RECEPTÁCULO de documentos (desde el 2026-10-29 no tiene tipo): la carpeta de un cliente o de un caso. Tiene un nombre y
reúne documentos; un documento puede estar en ninguno (el «void»), en uno o en varios. Los documentos NACEN al entrar al sistema
(`servicios/documentos.py`), no aquí: este router solo los organiza.

Cada expediente vive en un GRUPO (`/grupos`, ver `servicios/grupos.py`): el nivel de arriba, que cada organización usa como quiere (áreas, departamentos,
trimestres…). El grupo se elige AL CREAR el expediente y es obligatorio (400 sin él; 404 si no es de la organización); los expedientes anteriores a
los grupos no tienen y se les asigna después con `PUT /{id}/grupo`, que también pasa un expediente de un grupo a otro pero nunca lo deja sin grupo.
Los grupos solo viven en SQL: sin base, las rutas que los usan responden 503 y los expedientes se crean sin grupo.

Dentro de un expediente viven sus EXPEDIENTES VIRTUALES (`/expedientes/{id}/virtuales`, ver `servicios/expedientes_virtuales.py`): grupitos
de sus documentos, cada uno instancia de un tipo de expediente virtual (`/tipos-expediente`), que son lo que usará el motor de reglas. Solo
viven en SQL: sin base, sus rutas responden 503.

Todas las rutas van detrás de la llave de servicio y exigen TAMBIÉN el JWT de la persona (HU11): el tenant lo manda la petición y se
comprueba contra el registro, igual que en tipos documentales, procesos y bandeja.

## Quién puede qué

- **Leer** (listar, ver un expediente, listar documentos): cualquiera que opere en la organización.
- **Organizar** (crear un expediente, asignarle grupo, meter o sacar un documento, o SUBIRLE un archivo directo; crear, llenar y borrar sus expedientes
  virtuales): `roles.PUEDEN_ORGANIZAR`
  (Administrador, Supervisor, Analista y Operador); Compliance, Auditor y Viewer reciben 403 `sin_permiso`. Es una propuesta de producto
  pendiente de confirmar con César.

## Qué se anota (auditoría)

`CASE_CREATED` (con el grupo en `grupoId` y `grupoNombre`), `CASE_GROUP_ASSIGNED` (al asignar o cambiar de grupo; no se anota si ya estaba en ese),
`CASE_DOCUMENT_ADDED` (también al subir un archivo directo a un expediente, con `subida: true`) y `CASE_DOCUMENT_REMOVED`,
con quién lo hizo; de los virtuales, `VIRTUAL_CASE_CREATED`,
`VIRTUAL_CASE_DOCUMENT_ADDED`, `VIRTUAL_CASE_DOCUMENT_REMOVED` y `VIRTUAL_CASE_DELETED`. Meter un documento que ya estaba no repite el rastro.
Lo que mete SOLO el clasificador a un virtual también deja `VIRTUAL_CASE_DOCUMENT_ADDED`, con `origen: "clasificador"`, el tipo y la confianza;
«quién» es la persona que provocó la clasificación (subió el archivo, lo clasificó, lo metió al expediente o creó el virtual).

## El clasificador y los virtuales (Moibe, 2026-11-02)

Subir un archivo a un expediente lo CLASIFICA en la misma llamada (`clasificacion` en la respuesta): se gasta lo que cuestan las dos primeras
páginas en Document AI, pero el archivo ya queda con su tipo documental. Si el clasificador lo reconoce como de un tipo (sin confianza mínima), entra
solo a los expedientes virtuales del expediente que esperan ese tipo, también a los dos cuando hay dos del mismo tipo
(`expedientes_virtuales.colocar_clasificados`). Lo mismo pasa cuando un documento YA clasificado se mete a un expediente, cuando se clasifica
uno que ya está en uno (`/ia/clasificar` con `documento`) y cuando se crea un virtual (se llena con lo que su expediente ya tiene clasificado).
Nada de eso puede tumbar lo que lo provocó: sin clasificador, si Document AI falla o tarda, o si la colocación falla, el archivo se sube igual,
sin tipo, y la respuesta dice qué pasó.

## Lo que NO hace todavía

No renombra ni borra expedientes, no tiene estado ni folio, y un documento no se descarta desde aquí. Se agregan cuando haga falta.
"""

import asyncio
import logging

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from config import SQLSERVER_HOST
from errores import SinClasificador
from routers.archivos import guardar_subida
from routers.bandeja import MIME_BANDEJA
from routers.errores import almacen_no_disponible
from servicios import aislamiento, auditoria, clasificadores, datos_extraidos, documentos, expedientes, expedientes_virtuales, grupos, ia
from servicios.almacen import ErrorAlmacen
from servicios.subidas import normalizar_tipo

logger = logging.getLogger(__name__)

router = APIRouter()

#: Cuánto se espera al clasificador al subir un archivo (segundos). Pasado ese tiempo el archivo ya está subido y se queda sin clasificar: subir
#: nunca se cuelga esperando a Google (el BFF espera 130 s y Document AI suele contestar en pocos segundos).
ESPERA_CLASIFICAR_AL_SUBIR = 60

_NO_EXISTE = "Ese expediente no existe."
_VIRTUAL_NO_EXISTE = "Ese expediente virtual no existe."
_TIPO_NO_EXISTE = "Ese tipo de expediente virtual no existe."
_GRUPO_NO_EXISTE = "Ese grupo no existe."
_DOCUMENTO_NO_EXISTE = "Ese documento no existe."
_DOCUMENTO_NO_ESTA = "Ese documento no está en el expediente."
_DOCUMENTO_NO_ESTA_EN_VIRTUAL = "Ese documento no está en el expediente virtual."


class NuevoExpediente(BaseModel):
    tenant: str = Field(..., description="La organización a la que pertenece el expediente")
    nombre: str = Field(..., description="Cómo se llama el expediente (hasta 200 caracteres)")
    grupoId: str | None = Field(
        default=None,
        description="El grupo en que vive el expediente (`/grupos`), de la misma organización. Obligatorio donde hay base: sin él, 400.",
    )


class AsignacionDeGrupo(BaseModel):
    tenant: str = Field(..., description="La organización")
    grupoId: str = Field(..., description="El grupo al que pasa el expediente (`/grupos`), de la misma organización")


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


def colocar_clasificados(tenant: str, **filtros) -> list[dict]:
    """Mete a los expedientes virtuales lo que el clasificador reconoció (ver `expedientes_virtuales.colocar_clasificados`; los `filtros` son
    los de allá). NUNCA levanta: es una consecuencia de lo que la provocó (subir, clasificar, meter, crear) y no puede tumbarla; si falla,
    queda en el log y el documento se queda donde estaba, para que una persona lo meta a mano."""
    try:
        return expedientes_virtuales.colocar_clasificados(tenant, **filtros)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo meter a los expedientes virtuales lo que el clasificador reconoció (%s)", filtros)
        return []


def anotar_colocados(actor: aislamiento.Actor, tenant: str, colocados: list[dict]) -> None:
    """El rastro de cada documento que el clasificador metió a un virtual: el mismo `VIRTUAL_CASE_DOCUMENT_ADDED` de siempre, con el origen,
    el tipo y la confianza. `actor` es quien provocó la clasificación, no el clasificador."""
    for c in colocados:
        tipo = c.get("tipoDocumental") or {}
        _anotar(
            auditoria.VIRTUAL_CASE_DOCUMENT_ADDED,
            actor,
            tenant,
            expedienteVirtualId=c["expedienteVirtual"]["id"],
            nombre=c["expedienteVirtual"]["nombre"],
            expedienteId=c["expediente"]["id"],
            documentoId=c["documentoId"],
            nombreOriginal=c.get("nombreOriginal"),
            origen="clasificador",
            tipoDocumentalId=tipo.get("id"),
            tipoDocumentalNombre=tipo.get("nombre"),
            confianza=c.get("confianza"),
        )


def _virtuales_de(colocados: list[dict]) -> list[dict]:
    """A cuáles virtuales entró el documento (sin repetir), para la respuesta: `[{id, nombre, expedienteId}]`."""
    vistos: dict[str, dict] = {}
    for c in colocados:
        v = c["expedienteVirtual"]
        vistos.setdefault(v["id"], {"id": v["id"], "nombre": v["nombre"], "expedienteId": c["expediente"]["id"]})
    return list(vistos.values())


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
    description=(
        "Crea un expediente vacío con ese nombre, dentro del grupo `grupoId` (obligatorio donde hay base: sin él, 400; uno que no es de la "
        "organización, 404). Dos expedientes pueden llamarse igual. No tiene tipo: lo tienen sus expedientes virtuales."
    ),
)
def crear(entrada: NuevoExpediente, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_organizar(authorization, entrada.tenant, "expediente")
    try:
        creado = expedientes.crear(entrada.tenant, entrada.nombre, actor.guid, entrada.grupoId)
    except expedientes.ExpedienteInvalido as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except expedientes.GrupoNoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_GRUPO_NO_EXISTE) from exc
    except grupos.SinBase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    grupo = creado.get("grupo") or {}
    _anotar(auditoria.CASE_CREATED, actor, entrada.tenant, expedienteId=creado["id"], nombre=creado["nombre"], grupoId=grupo.get("id"), grupoNombre=grupo.get("nombre"))
    return creado


@router.put(
    "/{expediente_id}/grupo",
    tags=["Expedientes"],
    summary="Asignar un expediente a un grupo (o pasarlo a otro)",
    description=(
        "Los expedientes anteriores a los grupos no tienen y se les asigna aquí; uno que ya tiene grupo se puede pasar a otro, pero nunca queda sin "
        "grupo. Idempotente: si ya estaba en ese grupo responde `cambiado: false` y no hace nada. Devuelve el expediente como queda y el grupo en "
        "que estaba (`grupoAnterior`, `null` si no tenía). Un grupo o un expediente que no son de la organización: 404. Sin base, 503."
    ),
)
def asignar_grupo(expediente_id: str, entrada: AsignacionDeGrupo, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_organizar(authorization, entrada.tenant, "expediente")
    try:
        expediente, anterior = expedientes.asignar_grupo(entrada.tenant, expediente_id, entrada.grupoId, actor.guid)
    except expedientes.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except expedientes.GrupoNoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_GRUPO_NO_EXISTE) from exc
    except grupos.SinBase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    cambiado = anterior is None or anterior["id"] != entrada.grupoId
    if cambiado:
        grupo = expediente["grupo"] or {}
        _anotar(
            auditoria.CASE_GROUP_ASSIGNED,
            actor,
            entrada.tenant,
            expedienteId=expediente_id,
            nombre=expediente["nombre"],
            grupoId=grupo.get("id"),
            grupoNombre=grupo.get("nombre"),
            grupoAnteriorId=(anterior or {}).get("id"),
        )
    return {"cambiado": cambiado, "expediente": expediente, "grupoAnterior": anterior}


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
        return {"documentos": _con_tipo(tenant, expedientes.listar_documentos(tenant, sin_expediente))}
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc


def _con_tipo(tenant: str, docs: list[dict]) -> list[dict]:
    """Cada documento con `tipoDocumental`: el de su última extracción o, si no se ha extraído, el de su última clasificación (`null` si no
    se sabe). Si la lectura falla, los documentos se entregan igual, sin él."""
    try:
        tipos = datos_extraidos.tipos_de(tenant, [d["id"] for d in docs])
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo leer el tipo documental de los documentos")
        tipos = {}
    return [{**d, "tipoDocumental": tipos.get(d["id"])} for d in docs]


class Correccion(BaseModel):
    tenant: str = Field(..., description="La organización")
    ruta: str = Field(..., description="El campo, como lo da `campos[].ruta` (`rfc`, `domicilio.calle`, `accionistas[2].nombre`)")
    valor: str = Field(..., description="El valor correcto")
    motivo: str | None = Field(None, description="Opcional. Por qué se corrige (hasta 500 caracteres)")


@router.get(
    "/documentos/{documento_id}/datos",
    tags=["Expedientes"],
    summary="Lo que se sabe de un documento: su clasificación y sus datos extraídos",
    description=(
        "`clasificacion`: lo último que dijo el clasificador (categoría, tipo documental, confianza). `extraccion`: la última corrida de "
        "extracción, aunque haya fallado (tipo documental y la revisión de su configuración, procesador y versión, estado, confianzas, "
        "cuántas corridas lleva). `campos`: los VIGENTES, uno por campo, de la última corrida que terminó; cada uno con `valor` (el "
        "normalizado o su última corrección), `valorCrudo`, `confianza` 0-100, `pagina`, y `motivoSinValor` cuando el campo se pedía y no "
        "apareció. Todo `null`/vacío si el documento no se ha procesado desde que se guardan (migración 0007)."
    ),
)
def datos_de_documento(documento_id: str, tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "documento")
    try:
        return datos_extraidos.de_documento(tenant, documento_id)
    except datos_extraidos.DocumentoNoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_DOCUMENTO_NO_EXISTE) from exc


@router.post(
    "/documentos/{documento_id}/datos/correcciones",
    status_code=status.HTTP_201_CREATED,
    tags=["Expedientes"],
    summary="Corregir a mano un dato extraído",
    description=(
        "Corrige el valor VIGENTE de un campo del documento. El dato extraído no se toca: la corrección queda aparte, con quién y cuándo, y "
        "desde ese momento es el valor vigente. Devuelve el campo como queda. Solo vive en SQL: sin base responde 503."
    ),
)
def corregir_dato(documento_id: str, entrada: Correccion, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_organizar(authorization, entrada.tenant, "documento")
    try:
        campo = datos_extraidos.corregir(entrada.tenant, documento_id, entrada.ruta, entrada.valor, entrada.motivo, actor.guid)
    except datos_extraidos.SinBase as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except datos_extraidos.DocumentoNoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_DOCUMENTO_NO_EXISTE) from exc
    except datos_extraidos.CampoNoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ese documento no tiene ese campo en su extracción vigente.") from exc
    except datos_extraidos.CorreccionInvalida as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    _anotar(auditoria.FIELD_CORRECTED, actor, entrada.tenant, documentoId=documento_id, ruta=entrada.ruta)
    return campo


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
    docs = _con_tipo(tenant, detalle.get("documentos") or [])
    # Duplicados (Moibe, 2026-11-03): un expediente virtual con dos documentos de un mismo tipo no sabe cuál es «el INE». Cada virtual dice cuáles
    # son sus duplicados y cada documento dice en qué virtuales está duplicado (`duplicadoEn`). Se calcula al leer: no se guarda nada.
    por_id = {d["id"]: d for d in docs}
    marcas: dict[str, list[dict]] = {}
    virtuales = []
    for v in detalle.get("expedientesVirtuales") or []:
        repetidos = expedientes_virtuales.duplicados([por_id[i] for i in v["documentos"] if i in por_id])
        virtuales.append({**v, "duplicados": repetidos})
        for grupo in repetidos:
            for d in grupo["documentos"]:
                marcas.setdefault(d["id"], []).append({"id": v["id"], "nombre": v["nombre"], "tipoNombre": grupo["tipoNombre"]})
    return {
        **detalle,
        "documentos": [{**d, "duplicadoEn": marcas.get(d["id"], [])} for d in docs],
        "expedientesVirtuales": virtuales,
    }


@router.put(
    "/{expediente_id}/documentos/{documento_id}",
    tags=["Expedientes"],
    summary="Meter un documento a un expediente",
    description=(
        "Idempotente: si el documento ya estaba, responde `agregado: false` y no hace nada. Un documento puede estar en varios "
        "expedientes a la vez. Si el documento ya está clasificado con confianza suficiente, entra solo a los expedientes virtuales de ESTE "
        "expediente que esperan su tipo documental (`expedientesVirtuales` dice a cuáles)."
    ),
)
def agregar_documento(
    expediente_id: str,
    documento_id: str,
    tenant: str = Query(..., description="La organización"),
    authorization: str | None = Header(default=None),
):
    actor = aislamiento.exigir_organizar(authorization, tenant, "expediente")
    colocados: list[dict] = []
    try:
        agregado = expedientes.agregar_documento(tenant, expediente_id, documento_id, actor.guid)
        if agregado:
            # ANTES de leer el expediente, para que lo que devuelve ya traiga lo que el clasificador metió a sus virtuales.
            colocados = colocar_clasificados(tenant, documento_id=documento_id, expediente_id=expediente_id)
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
        anotar_colocados(actor, tenant, colocados)
    return {"agregado": agregado, "expediente": detalle, "expedientesVirtuales": _virtuales_de(colocados)}


def _recibir_en_expediente(actor: aislamiento.Actor, tenant: str, expediente_id: str, archivo: UploadFile, sha256: str | None, mime: str) -> dict:
    """La parte de subir un archivo a un expediente que ESCRIBE: guarda los bytes, hace nacer el documento y lo mete al expediente, con todo
    comprobado antes de guardar nada. Es síncrona (disco y SQL) y la ruta la manda a un hilo; devuelve el documento que nació."""
    # El expediente se comprueba ANTES de guardar los bytes: uno que no existe, o que ya está lleno, no puede dejar un documento suelto.
    try:
        detalle = expedientes.obtener(tenant, expediente_id)
    except ValueError as exc:
        raise _tenant_invalido(exc) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    if detalle is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE)
    if len(detalle["documentos"]) >= expedientes.MAX_DOCUMENTOS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"El expediente ya tiene el máximo de documentos ({expedientes.MAX_DOCUMENTOS}).")
    guardado = guardar_subida(archivo, tenant, sha256)
    try:
        documento = documentos.nacer(tenant, guardado, mime, archivo.filename or "", "MANUAL", None)
        expedientes.agregar_documento(tenant, expediente_id, documento["id"], actor.guid)
    except expedientes.NoEncontrado as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=_NO_EXISTE) from exc
    except expedientes.ExpedienteInvalido as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        # Los bytes ya quedaron en el almacén; sin registro son un objeto suelto, barato y deduplicado: reintentar lo registra.
        raise almacen_no_disponible(exc) from exc
    _anotar(
        auditoria.CASE_DOCUMENT_ADDED,
        actor,
        tenant,
        expedienteId=expediente_id,
        nombre=detalle["nombre"],
        documentoId=documento["id"],
        nombreOriginal=documento["nombreOriginal"],
        subida=True,
    )
    return documento


def _releer(archivo: UploadFile) -> bytes:
    """Los bytes del archivo que se acaba de guardar (la subida los leyó una vez; el clasificador los necesita otra)."""
    archivo.file.seek(0)
    return archivo.file.read()


async def _clasificar_al_subir(actor: aislamiento.Actor, tenant: str, expediente_id: str, documento: dict, archivo: UploadFile) -> tuple[dict, list[dict]]:
    """Clasifica el archivo recién subido, anota lo que dijo el clasificador y, si lo reconoció como de un tipo, lo mete a los virtuales
    del expediente que esperan ese tipo. Devuelve `(clasificacion, colocados)`; NUNCA levanta: el archivo ya está subido y esto es una
    consecuencia, así que cualquier falla (sin clasificador, Document AI caído o lento, la base) solo deja el documento sin tipo y se dice en
    `clasificacion.estado`: `clasificado`, `sin_tipo` (el clasificador dijo que no es ninguno de los tipos), `sin_clasificador` (la organización
    todavía no tiene ningún tipo documental activo) o `no_clasificado` (se intentó y no se pudo, o esta instalación no tiene base)."""
    def resultado(estado: str, tipo: dict | None = None, confianza: float | None = None) -> dict:
        return {"estado": estado, "tipo": tipo, "confianza": confianza}

    # Sin base no hay dónde anotar la clasificación ni virtuales a los que meterlo: no se gasta en preguntarle a Google.
    if not SQLSERVER_HOST:
        return resultado("no_clasificado"), []
    try:
        contenido = await run_in_threadpool(_releer, archivo)
        respuesta = await asyncio.wait_for(ia.clasificar_documento(contenido, documento["mime"], tenant), timeout=ESPERA_CLASIFICAR_AL_SUBIR)
    except SinClasificador:
        return resultado("sin_clasificador"), []
    except asyncio.TimeoutError:
        logger.warning("El clasificador tardó más de %s s con el documento %s: queda subido y sin tipo", ESPERA_CLASIFICAR_AL_SUBIR, documento["id"])
        return resultado("no_clasificado"), []
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo clasificar al subir el documento %s", documento["id"])
        return resultado("no_clasificado"), []
    try:
        anotado = await run_in_threadpool(
            datos_extraidos.registrar_clasificacion, tenant, documento["id"], respuesta, clasificadores.id_de(tenant) or "", actor.guid
        )
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo guardar la clasificación del documento %s", documento["id"])
        return resultado("no_clasificado"), []
    if not anotado["tipoId"]:
        return resultado("sin_tipo", confianza=anotado["confianza"]), []
    colocados = await run_in_threadpool(colocar_clasificados, tenant, documento_id=documento["id"], expediente_id=expediente_id)
    return resultado("clasificado", {"id": anotado["tipoId"], "nombre": anotado["tipoNombre"]}, anotado["confianza"]), colocados


@router.post(
    "/{expediente_id}/documentos",
    status_code=status.HTTP_201_CREATED,
    tags=["Expedientes"],
    summary="Subir un archivo directamente a un expediente",
    description=(
        "Lo que la persona sube desde la pantalla de un expediente: guarda los bytes en el almacén, anota el documento con `canal: MANUAL` "
        "(como una carga manual: aparece en «Archivos procesados») y lo mete a ESTE expediente, todo en una llamada. Todo se comprueba "
        "ANTES de guardar nada: el rol (solo quien organiza: Administrador, Supervisor, Analista y Operador), el formato (PDF, JPEG, PNG y "
        "TIFF, con los mismos límites de tamaño que la bandeja) y el expediente (404 si no es de la organización, 400 si ya tiene el "
        "máximo de documentos), así que un rechazo no deja un documento suelto. Subir dos veces el mismo archivo crea DOS documentos que "
        "comparten los bytes, como por la API. "
        "Además lo CLASIFICA (lo que cuestan sus dos primeras páginas en Document AI): `clasificacion` dice el `estado` (`clasificado`, "
        "`sin_tipo`, `sin_clasificador` o `no_clasificado`), el `tipo` y la `confianza` (0-100) si lo reconoció; "
        "`expedientesVirtuales` dice a cuáles virtuales entró solo por ser de ese tipo. Clasificar nunca tumba la "
        "subida: si no se puede, el archivo queda subido y sin tipo documental."
    ),
)
async def subir_documento(
    expediente_id: str,
    archivo: UploadFile = File(...),
    tenant: str = Form(..., description="La organización"),
    sha256: str | None = Form(None, description="Opcional. El SHA-256 del archivo, para verificar la transferencia.", examples=[""]),
    authorization: str | None = Header(default=None),
):
    actor = aislamiento.exigir_organizar(authorization, tenant, "expediente")
    mime = normalizar_tipo(archivo.content_type)
    if mime not in MIME_BANDEJA:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Formato no admitido. Se aceptan PDF, JPEG, PNG y TIFF.")
    documento = await run_in_threadpool(_recibir_en_expediente, actor, tenant, expediente_id, archivo, sha256, mime)
    clasificacion, colocados = await _clasificar_al_subir(actor, tenant, expediente_id, documento, archivo)
    if colocados:
        await run_in_threadpool(anotar_colocados, actor, tenant, colocados)
    return {"agregado": True, "documento": documento, "clasificacion": clasificacion, "expedientesVirtuales": _virtuales_de(colocados)}


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
    docs = _con_tipo(tenant, virtual["documentos"])
    # Ahora que los documentos saben su tipo documental, cada tipo del esquema dice qué documentos lo cubren: uno sin ninguno es lo que FALTA.
    for tipo in virtual["tipo"]["tiposDocumentales"]:
        tipo["documentos"] = [d["id"] for d in docs if (d["tipoDocumental"] or {}).get("id") == tipo["id"]]
    # Duplicados (Moibe, 2026-11-03): dos o más documentos del mismo tipo. Cada documento trae `duplicado` y el virtual, la lista de `duplicados`
    # (por tipo, con los documentos): un proceso que usa ese tipo no se ejecuta sobre este virtual hasta que quede uno.
    repetidos = expedientes_virtuales.duplicados(docs)
    marcados = {d["id"] for grupo in repetidos for d in grupo["documentos"]}
    return {**virtual, "documentos": [{**d, "duplicado": d["id"] in marcados} for d in docs], "duplicados": repetidos}


@router.post(
    "/{expediente_id}/virtuales",
    status_code=status.HTTP_201_CREATED,
    tags=["Expedientes"],
    summary="Crear un expediente virtual dentro de un expediente",
    description=(
        "Un expediente virtual es un grupito de documentos DE ESTE EXPEDIENTE, instancia de un tipo de expediente virtual (`tipoId`, de "
        "`/tipos-expediente`): por ejemplo, la «Solicitud de crédito» de Juan Pérez. Nace con los `documentos` elegidos (pueden ser ninguno); "
        "uno que no esté en el expediente responde 404 y no se crea nada. Además se llena SOLO con lo que su expediente ya tiene clasificado "
        "como alguno de los tipos documentales de su esquema (los que entraron así traen `agregadoPorClasificador`). "
        "Un expediente puede tener varios, del mismo tipo o de distintos. Solo vive en SQL: sin base responde 503."
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
    # Nace con lo que su expediente ya tiene clasificado y su tipo espera (se revisa DESPUÉS de anotar su nacimiento: el rastro cuenta primero que
    # nació y luego lo que el clasificador le metió). Si metió algo, se devuelve el virtual ya con eso.
    colocados = colocar_clasificados(entrada.tenant, virtual_id=creado["id"])
    if colocados:
        anotar_colocados(actor, entrada.tenant, colocados)
        creado = expedientes_virtuales.obtener(entrada.tenant, expediente_id, creado["id"]) or creado
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
