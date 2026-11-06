"""Dominio: expedientes — conjuntos de documentos de una organización (ver `servicios/expedientes.py`).

Un expediente es un RECEPTÁCULO de documentos (desde el 2026-10-29 no tiene tipo): la carpeta de un cliente o de un caso. Tiene un nombre y
reúne documentos; un documento puede estar en ninguno (el «void»), en uno o en varios. Los documentos NACEN al entrar al sistema
(`servicios/documentos.py`), no aquí: este router solo los organiza.

**Sin grupos (Moibe, 2026-11-06: «en aras de la simplicidad por el momento también quitaremos grupos, léase que los expedientes no se agruparán en
Grupos y no se requiere el menú»).** Entre el 2026-11-03 y ese día cada expediente vivía en un GRUPO (`/grupos`: áreas, departamentos, trimestres…, que se
elegía al crearlo). Se quitaron: `POST /` pide solo `{tenant, nombre}` (si un cliente viejo todavía manda `grupoId`, se IGNORA en silencio: no falla ni se
guarda), el expediente ya no trae `grupo` en ninguna respuesta, y `/grupos…` y `PUT /{id}/grupo` ya no existen (404). Un expediente nuevo no depende de
nada más que de su nombre.

**Sin expedientes virtuales (Moibe, 2026-11-05: «voy a quitar los expedientes virtuales, ahora podremos meter el expediente completo a la ejecución de
un proceso»).** Entre el 2026-10-29 y ese día un expediente tenía dentro EXPEDIENTES VIRTUALES (`/expedientes/{id}/virtuales…`): grupitos de sus documentos,
instancias de un tipo de expediente virtual (`/tipos-expediente…`), que elegían qué documentos alimentaban a un proceso. Se quitaron las dos familias de
rutas y el clasificador dejó de meter documentos a ningún lado: un proceso corre sobre el expediente COMPLETO (`/ejecuciones`) y los tipos documentales que
dibuja en su lienzo declaran qué necesita. Un cliente que todavía llame a una de esas rutas recibe 404.

Todas las rutas van detrás de la llave de servicio y exigen TAMBIÉN el JWT de la persona (HU11): el tenant lo manda la petición y se
comprueba contra el registro, igual que en tipos documentales, procesos y bandeja.

## Quién puede qué

- **Leer** (listar, ver un expediente, listar documentos): cualquiera que opere en la organización.
- **Organizar** (crear un expediente, meter o sacar un documento, o SUBIRLE un archivo directo): `roles.PUEDEN_ORGANIZAR`
  (Administrador, Supervisor, Analista y Operador); Compliance, Auditor y Viewer reciben 403 `sin_permiso`. Es una propuesta de producto
  pendiente de confirmar con César.

## Qué se anota (auditoría)

`CASE_CREATED`, `CASE_DOCUMENT_ADDED` (también al subir un archivo directo a un expediente, con `subida: true`) y `CASE_DOCUMENT_REMOVED`,
con quién lo hizo. Meter un documento que ya estaba no repite el rastro. (Las acciones `VIRTUAL_CASE_*`, `CASE_TYPE_*` y `CASE_GROUP_*` de antes ya no se
escriben; las que quedaron en la bitácora se siguen leyendo, y los `CASE_CREATED` del 2026-11-03 al 2026-11-06 conservan su `grupoId` y `grupoNombre`.)

## El clasificador al subir

Subir un archivo a un expediente lo CLASIFICA en la misma llamada (`clasificacion` en la respuesta): se gasta lo que cuestan las dos primeras
páginas en Document AI, pero el archivo ya queda con su tipo documental (la clasificación se guarda y es lo que dice de qué tipo es cada documento: el
detalle del expediente lo muestra en `tipoDocumental`). Desde el 2026-11-05 clasificar ya NO mete el documento a ningún lado; lo mismo pasa con
`/ia/clasificar` con `documento`. Nada de eso puede tumbar la subida: sin clasificador, si Document AI falla o tarda, el archivo se sube igual, sin tipo,
y la respuesta dice qué pasó.

## La extracción al subir (Moibe, 2026-11-06)

Justo después de clasificar, si el clasificador reconoció un tipo, se EXTRAEN los datos del archivo (`extraccion` en la respuesta) con el procesador de ese
tipo y se guardan igual que desde `/ia/extraer` (`datos_extraidos.registrar_extraccion`): es lo que leen los procesos, que antes no encontraban nada en
los archivos subidos directo a un expediente (solo el pipeline de las bandejas extraía; Moibe: «cuando corremos los archivos en las bandejas sí se
extraen los datos, pero en expedientes no»). Las condiciones: el clasificador reconoció un tipo, y ese tipo está ACTIVO y tiene procesador; nada más. Desde
el 2026-11-06 el paso «Calibrar» de los tipos documentales ya no existe (Moibe decidió quitarlo), así que un tipo activo con procesador extrae en cuanto está
activo, y el `calibradoEn` que un tipo viejo todavía traiga en su caja se ignora. Cuesta una
llamada más a Document AI por archivo reconocido. Tampoco puede tumbar la subida: si no se puede, el archivo queda subido con su tipo y `extraccion.estado`
dice por qué (`sin_procesador`, `error`, `no_extraido`…). Los archivos subidos ANTES de este cambio no se extraen solos.

## Duplicados (Moibe, 2026-11-03; sobre el expediente entero desde el 2026-11-05)

Dos o más documentos del mismo tipo documental en un expediente son un DUPLICADO: no se sabe cuál es «el INE», así que un proceso que usa ese tipo no
corre sobre el expediente hasta que quede uno. `GET /{id}` los marca: cada documento trae `duplicado` y el expediente trae `duplicados` (por tipo, con
sus documentos). Es solo una marca informativa que se calcula al leer (`expedientes.duplicados`), no se guarda: sacar uno la quita sola.

## Lo que NO hace todavía

No renombra ni borra expedientes, no tiene estado ni folio, y un documento no se descarta desde aquí. Se agregan cuando haga falta.
"""

import asyncio
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from config import SQLSERVER_HOST
from errores import SinClasificador
from routers.archivos import guardar_subida
from routers.bandeja import MIME_BANDEJA
from routers.errores import almacen_no_disponible
from servicios import aislamiento, auditoria, clasificadores, datos_extraidos, documentos, expedientes, ia, tipos_documentales
from servicios.almacen import ErrorAlmacen
from servicios.subidas import normalizar_tipo

logger = logging.getLogger(__name__)

router = APIRouter()

#: Cuánto se espera al clasificador al subir un archivo (segundos). Pasado ese tiempo el archivo ya está subido y se queda sin clasificar: subir
#: nunca se cuelga esperando a Google (el BFF espera 130 s y Document AI suele contestar en pocos segundos).
ESPERA_CLASIFICAR_AL_SUBIR = 60
# Y lo que se espera a que EXTRAIGA (2026-11-06). Clasificar + extraer tienen que caber en el tiempo de espera del puente del front (130 s).
ESPERA_EXTRAER_AL_SUBIR = 60

_NO_EXISTE = "Ese expediente no existe."
_DOCUMENTO_NO_EXISTE = "Ese documento no existe."
_DOCUMENTO_NO_ESTA = "Ese documento no está en el expediente."


class NuevoExpediente(BaseModel):
    # Solo estos dos campos. Un cliente viejo que todavía manda `grupoId` (los grupos se quitaron el 2026-11-06) no falla: pydantic ignora lo que el
    # modelo no declara, y así el front de antes sigue creando expedientes unos segundos entre el despliegue de este back y el del front nuevo.
    tenant: str = Field(..., description="La organización a la que pertenece el expediente")
    nombre: str = Field(..., description="Cómo se llama el expediente (hasta 200 caracteres)")


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
    description=(
        "Crea un expediente vacío con ese nombre. Dos expedientes pueden llamarse igual. No tiene tipo ni grupo (los grupos se quitaron el 2026-11-06: "
        "si el cuerpo trae `grupoId`, se ignora)."
    ),
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
    summary="Ver un expediente con sus documentos",
    description=(
        "El expediente y sus documentos completos, en el orden en que se agregaron. Cada documento trae su `tipoDocumental` (el de su última extracción "
        "o, si no se ha extraído, el de su última clasificación; `null` si no se sabe) y `duplicado`: hay OTRO documento del mismo tipo en ESTE expediente. "
        "El expediente trae `duplicados`: los tipos que tienen dos o más documentos, con cuáles son (`[{tipoId, tipoNombre, documentos: [{id, nombre}]}]`). "
        "Son marcas informativas que se calculan al leer: no se guardan y sacar un documento las quita."
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
    # Duplicados (Moibe, 2026-11-03, sobre el expediente entero desde el 2026-11-05): dos documentos del mismo tipo y no se sabe cuál es «el INE».
    # Cada documento dice si es uno de ellos y el expediente dice cuáles tipos tiene repetidos. Se calcula al leer: no se guarda nada.
    repetidos = expedientes.duplicados(docs)
    marcados = {d["id"] for grupo in repetidos for d in grupo["documentos"]}
    return {**detalle, "documentos": [{**d, "duplicado": d["id"] in marcados} for d in docs], "duplicados": repetidos}


@router.put(
    "/{expediente_id}/documentos/{documento_id}",
    tags=["Expedientes"],
    summary="Meter un documento a un expediente",
    description=(
        "Idempotente: si el documento ya estaba, responde `agregado: false` y no hace nada. Un documento puede estar en varios "
        "expedientes a la vez. Devuelve `{agregado, expediente}`."
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


async def _clasificar_al_subir(actor: aislamiento.Actor, tenant: str, documento: dict, archivo: UploadFile) -> dict:
    """Clasifica el archivo recién subido y anota lo que dijo el clasificador: esa clasificación es lo que dice de qué tipo es el documento. Devuelve
    `clasificacion`; NUNCA levanta: el archivo ya está subido y esto es una consecuencia, así que cualquier falla (sin clasificador, Document AI caído
    o lento, la base) solo deja el documento sin tipo y se dice en `estado`: `clasificado`, `sin_tipo` (el clasificador dijo que no es ninguno de los
    tipos), `sin_clasificador` (la organización todavía no tiene ningún tipo documental activo) o `no_clasificado` (se intentó y no se pudo, o esta
    instalación no tiene base). Desde el 2026-11-05 no mete el documento a ningún lado (ya no hay expedientes virtuales)."""
    def resultado(estado: str, tipo: dict | None = None, confianza: float | None = None) -> dict:
        return {"estado": estado, "tipo": tipo, "confianza": confianza}

    # Sin base no hay dónde anotar la clasificación: no se gasta en preguntarle a Google.
    if not SQLSERVER_HOST:
        return resultado("no_clasificado")
    try:
        contenido = await run_in_threadpool(_releer, archivo)
        respuesta = await asyncio.wait_for(ia.clasificar_documento(contenido, documento["mime"], tenant), timeout=ESPERA_CLASIFICAR_AL_SUBIR)
    except SinClasificador:
        return resultado("sin_clasificador")
    except asyncio.TimeoutError:
        logger.warning("El clasificador tardó más de %s s con el documento %s: queda subido y sin tipo", ESPERA_CLASIFICAR_AL_SUBIR, documento["id"])
        return resultado("no_clasificado")
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo clasificar al subir el documento %s", documento["id"])
        return resultado("no_clasificado")
    try:
        anotado = await run_in_threadpool(
            datos_extraidos.registrar_clasificacion, tenant, documento["id"], respuesta, clasificadores.id_de(tenant) or "", actor.guid
        )
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo guardar la clasificación del documento %s", documento["id"])
        return resultado("no_clasificado")
    if not anotado["tipoId"]:
        return resultado("sin_tipo", confianza=anotado["confianza"])
    return resultado("clasificado", {"id": anotado["tipoId"], "nombre": anotado["tipoNombre"]}, anotado["confianza"])


async def _extraer_al_subir(actor: aislamiento.Actor, tenant: str, documento: dict, archivo: UploadFile, clasificacion: dict) -> dict:
    """Extrae los datos del archivo recién subido, DESPUÉS de clasificarlo (Moibe, 2026-11-06: «cuando corremos los archivos en las bandejas sí se
    extraen los datos, pero en expedientes no»). Es la MISMA extracción del pipeline (`ia.extraer_con_procesador`) y se hace si el clasificador reconoció un
    tipo y ese tipo está ACTIVO y tiene su procesador: nada más. Desde el 2026-11-06 no existe el paso «Calibrar» de los tipos documentales (antes se exigía
    además `calibradoEn`, la marca que ponía), así que un tipo activo con procesador extrae en cuanto está activo, y el `calibradoEn` que un tipo viejo
    todavía traiga en su caja no se mira. La corrida y sus datos se guardan igual que desde `/ia/extraer` (`registrar_extraccion`), y es lo que luego
    leen los procesos.
    Devuelve `extraccion`; NUNCA levanta: el archivo ya está subido y esto es una consecuencia, así que cualquier falla solo deja el documento sin datos
    y se dice en `estado`: `extraido`, `no_reconocido` (Document AI no reconoció los campos: la corrida se guarda con esa marca), `sin_tipo` (no se
    clasificó, o el tipo ya no está activo, así que no hay con qué extraer), `sin_procesador` (el tipo está activo pero sin procesador: lo que falta es
    trabajo de configuración, no reintentar), `error` (Document AI falló: la corrida fallida queda anotada) o `no_extraido` (tardó demasiado o no se pudo guardar)."""
    def resultado(estado: str, **mas) -> dict:
        return {"estado": estado, **mas}

    if clasificacion.get("estado") != "clasificado" or not (clasificacion.get("tipo") or {}).get("id"):
        return resultado("sin_tipo")
    tipo_id = clasificacion["tipo"]["id"]
    try:
        tipo = await run_in_threadpool(tipos_documentales.obtener, tenant, tipo_id)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo leer el tipo documental %s para extraer al subir", tipo_id)
        return resultado("no_extraido")
    datos = (tipo or {}).get("datos") or {}
    if not tipo or datos.get("estado") != "activo":
        return resultado("sin_tipo")
    procesador = str(datos.get("procesadorId") or "").strip()
    if not procesador:
        return resultado("sin_procesador")
    version = str(datos.get("procesadorVersion") or "").strip()

    iniciada = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        contenido = await run_in_threadpool(_releer, archivo)
        extraido = await asyncio.wait_for(ia.extraer_con_procesador(procesador, contenido, documento["mime"], version), timeout=ESPERA_EXTRAER_AL_SUBIR)
    except asyncio.TimeoutError:
        logger.warning("La extracción tardó más de %s s con el documento %s: queda subido y sin datos", ESPERA_EXTRAER_AL_SUBIR, documento["id"])
        return resultado("no_extraido")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Falló la extracción al subir el documento %s", documento["id"])
        try:
            await run_in_threadpool(
                datos_extraidos.registrar_extraccion, tenant, documento["id"], tipo_id, procesador, version, iniciada, actor.guid, None, str(exc)[:500] or "Falló la extracción."
            )
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo anotar la extracción fallida del documento %s", documento["id"])
        return resultado("error")
    try:
        corrida = await run_in_threadpool(
            datos_extraidos.registrar_extraccion, tenant, documento["id"], tipo_id, procesador, version, iniciada, actor.guid, extraido, None
        )
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo guardar la extracción del documento %s", documento["id"])
        return resultado("no_extraido")
    reconocido = not ((extraido or {}).get("_metadata") or {}).get("quality_alert")
    return resultado("extraido" if reconocido else "no_reconocido", extraccionId=corrida)


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
        "`sin_tipo`, `sin_clasificador` o `no_clasificado`), el `tipo` y la `confianza` (0-100) si lo reconoció: esa clasificación se guarda y es lo "
        "que dice de qué tipo es el documento. Clasificar nunca tumba la subida: si no se puede, el archivo queda subido y sin tipo documental. "
        "Si el clasificador reconoció un tipo activo y con procesador, EXTRAE sus datos con ese procesador, igual que el pipeline de las "
        "bandejas (lo que luego leen los procesos): `extraccion.estado` es `extraido`, `no_reconocido`, `sin_tipo`, `sin_procesador`, "
        "`error` o `no_extraido` (y `extraccionId` si se guardó la corrida). Tampoco tumba la subida. "
        "Devuelve `{agregado, documento, clasificacion, extraccion}`."
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
    clasificacion = await _clasificar_al_subir(actor, tenant, documento, archivo)
    extraccion = await _extraer_al_subir(actor, tenant, documento, archivo, clasificacion)
    return {"agregado": True, "documento": documento, "clasificacion": clasificacion, "extraccion": extraccion}


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
