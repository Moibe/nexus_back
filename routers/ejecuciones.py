"""Dominio: ejecuciones — correr un proceso del motor de reglas sobre un EXPEDIENTE, y su historial (ver `servicios/ejecuciones.py`).

Una ejecución junta UN expediente con UN proceso (Moibe, 2026-11-05: se quitaron los expedientes virtuales, así que el proceso corre sobre el expediente
COMPLETO y sobre cualquiera de la organización; los tipos documentales de su lienzo declaran qué documentos toma de él). El back no corre las reglas (eso
vive en `reglas.ts`, que el servidor del front ejecuta con los datos reales): aquí se PREPARA lo que la ejecución necesita y se GUARDA lo que salió. El
historial es una foto inmutable: no hay rutas que lo editen o lo borren.

Todas las rutas van detrás de la llave de servicio y exigen TAMBIÉN el JWT de la persona (HU11), igual que procesos y expedientes.

## Quién puede qué

- **Ver el historial** (listar y abrir una ejecución) y **los procesos que se ofrecen para ejecutar** (`GET /procesos`): cualquiera que opere en la
  organización, de cualquier rol. Esto último es el mismo nivel de exposición que tenía «los procesos que le aplican» dentro de un expediente virtual:
  solo nombre, si está activo y si se puede ejecutar; el dibujo del proceso sigue siendo de `/procesos`, que solo leen Administrador y Supervisor.
- **Ejecutar** (preparar y guardar): `roles.PUEDEN_ORGANIZAR` (Administrador, Supervisor, Analista y Operador), igual que subir archivos a un expediente;
  Compliance, Auditor y Viewer reciben 403 `sin_permiso`. Confirmado por Moibe el 2026-11-03 («no hace falta confirmarlo con César, así está ok»).

## Qué se anota

`PROCESS_RUN_RECORDED` cuando una ejecución queda en el historial, con quién, qué proceso, sobre qué expediente y cómo terminó (sin expediente virtual:
ya no lo hay). Preparar, o una ejecución que no se corrió por duplicados, no dejan rastro.

## Las ejecuciones de antes del 2026-11-05

Las que se corrieron sobre un expediente virtual conservan su virtual y salen con `virtual: {id, nombre, tipoNombre}`; las nuevas, con `virtual: null`. El
filtro `virtual` del historial se quitó.
"""

import logging
from datetime import datetime

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from servicios import aislamiento, auditoria, ejecuciones

logger = logging.getLogger(__name__)

router = APIRouter()

_MENSAJES_NO_EXISTE = {
    "expediente": "Ese expediente no existe.",
    "proceso": "Ese proceso no existe.",
    "ejecucion": "Esa ejecución no existe.",
}


class NuevaEjecucion(BaseModel):
    tenant: str = Field(..., description="La organización")
    expedienteId: str = Field(..., description="El expediente sobre el que se corrió (el completo)")
    procesoId: str = Field(..., description="El proceso que se corrió")
    procesoRevision: int = Field(..., ge=1, description="La revisión del proceso que `/ejecuciones/preparar` entregó y con la que se corrió")
    resultado: dict = Field(..., description="Con qué palabra terminó: `{caso: si|no|sin, texto}`; el texto tiene que ser el que el proceso tiene para ese caso")
    valoresFinales: list[dict] = Field(default_factory=list, description="La lista de valores finales que entregó, uno por cada nodo «Valor final» del proceso")
    detalle: dict = Field(..., description="La foto de cómo salió cada regla y de qué documentos y campos se tomó cada dato")


def _anotar(accion: str, actor: aislamiento.Actor, tenant: str, **datos) -> None:
    """El rastro nunca cambia la respuesta: si la bitácora falla, se avisa en el log y la ejecución ya guardada se entrega igual."""
    try:
        auditoria.registrar(accion, usuario_guid=actor.guid, sesionGuid=actor.sesion, tenant=tenant, **datos)
    except Exception as exc:  # noqa: BLE001
        logger.warning("No se pudo anotar %s en la bitácora de auditoría: %s", accion, exc)


def _no_existe(exc: ejecuciones.NoEncontrado) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"codigo": f"{exc.que}_no_existe", "mensaje": _MENSAJES_NO_EXISTE.get(exc.que, "No existe.")},
    )


def _sin_base(exc: ejecuciones.SinBase) -> HTTPException:
    return HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))


@router.get(
    "/preparar",
    tags=["Ejecuciones"],
    summary="Preparar una ejecución: el proceso, sus tipos documentales y los datos reales del expediente",
    description=(
        "Todo lo que una ejecución necesita en UNA lectura: `expediente`, `proceso` (con su dibujo y su revisión), `tipos` (los tipos documentales que usa, "
        "con sus campos), `documentos` (por cada tipo: los documentos del EXPEDIENTE completo que lo cubren y, si es uno solo, sus valores VIGENTES por el "
        "nombre del campo en la biblioteca) y `duplicados` (los tipos que el proceso usa y el expediente trae repetidos: con alguno, la ejecución no debe "
        "seguir). Un proceso corre sobre cualquier expediente de la organización. 404 con `codigo` si no existe el expediente (`expediente_no_existe`) o el "
        "proceso (`proceso_no_existe`); 409 con `codigo` si el proceso no se puede ejecutar: `proceso_inactivo`, `sin_resultado` o `resultado_sin_regla`."
    ),
)
def preparar(
    tenant: str = Query(..., description="La organización"),
    expediente: str = Query(..., description="El expediente sobre el que se va a correr (el completo)"),
    proceso: str = Query(..., description="El proceso que se va a correr"),
    authorization: str | None = Header(default=None),
):
    aislamiento.exigir_organizar(authorization, tenant, "ejecucion")
    try:
        return ejecuciones.preparar(tenant, expediente, proceso)
    except ejecuciones.NoEncontrado as exc:
        raise _no_existe(exc) from exc
    except ejecuciones.NoSePuedeEjecutar as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail={"codigo": exc.codigo, "mensaje": str(exc)}) from exc
    except ejecuciones.SinBase as exc:
        raise _sin_base(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    tags=["Ejecuciones"],
    summary="Guardar una ejecución en el historial",
    description=(
        "Guarda lo que salió de correr la lógica del lienzo con los datos que entregó `/ejecuciones/preparar`: `{tenant, expedienteId, procesoId, procesoRevision, "
        "resultado, valoresFinales, detalle}`. Los nombres y el dibujo del proceso los toma el servidor, no la petición. 400 si algo no cuadra con el proceso "
        "(una palabra del resultado que no es la suya, un valor final que no es de él); 404 `expediente_no_existe` o `proceso_no_existe`; 409 `proceso_cambio` si "
        "el proceso se guardó otra vez desde que se preparó (hay que volver a ejecutar). Es historial: una vez guardada no se edita ni se borra."
    ),
)
def guardar(entrada: NuevaEjecucion, authorization: str | None = Header(default=None)):
    actor = aislamiento.exigir_organizar(authorization, entrada.tenant, "ejecucion")
    cuerpo = entrada.model_dump(exclude={"tenant"})
    try:
        ejecucion = ejecuciones.registrar(entrada.tenant, actor.guid, cuerpo)
    except ejecuciones.NoEncontrado as exc:
        raise _no_existe(exc) from exc
    except ejecuciones.Conflicto as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"codigo": "proceso_cambio", "mensaje": "El proceso cambió mientras se ejecutaba: vuelve a ejecutarlo.", "revisionActual": exc.revision_actual},
        ) from exc
    except ejecuciones.EjecucionInvalida as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ejecuciones.SinBase as exc:
        raise _sin_base(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    _anotar(
        auditoria.PROCESS_RUN_RECORDED,
        actor,
        entrada.tenant,
        ejecucionId=ejecucion["id"],
        procesoId=ejecucion["proceso"]["id"],
        nombre=ejecucion["proceso"]["nombre"],
        expedienteId=ejecucion["expediente"]["id"],
        caso=ejecucion["resultado"]["caso"],
        resultado=ejecucion["resultado"]["texto"],
    )
    return ejecucion


@router.get(
    "/",
    tags=["Ejecuciones"],
    summary="El historial de ejecuciones de la organización",
    description=(
        "De la más reciente a la más antigua. Filtros opcionales, que se combinan con Y: `expediente` (un id); `proceso` y `persona` (ids; el "
        "parámetro se repite, `proceso=a&proceso=b`, y basta con que sea de UNO); `caso` (`si`, `no` o `sin`, también repetible: cómo terminó, y `sin` son "
        "las incompletas); y el rango de fechas, `desde` (incluido) y `hasta` (NO incluido), en ISO 8601, sin zona se toma por UTC. `limite` (1 a 200, por "
        "omisión 50) dice cuántas entrega y `antes` es el id de la última que ya se vio, para pedir las siguientes. Devuelve `ejecuciones`, `total` (de las "
        "que cumplen los filtros) y `hayMas`. Cada ejecución trae `virtual`: `null` si es nueva y `{id, nombre, tipoNombre}` si es de las que se corrieron sobre un "
        "expediente virtual antes del 2026-11-05 (el filtro por virtual ya no existe). 400 si un filtro no vale (un caso que no existe, más de 50 valores en uno). "
        "Sin base, vacío."
    ),
)
def listar(
    tenant: str = Query(..., description="La organización"),
    expediente: str | None = Query(None, description="Solo las de este expediente"),
    proceso: list[str] | None = Query(None, description="Solo las de estos procesos (sus ids; el parámetro se repite)"),
    persona: list[str] | None = Query(None, description="Solo las que ejecutaron estas personas (sus ids; el parámetro se repite)"),
    caso: list[str] | None = Query(None, description="Solo las que terminaron así: `si` (se cumplió), `no` (no se cumplió) o `sin` (faltaron datos); se repite"),
    desde: datetime | None = Query(None, description="Solo las ejecutadas desde este instante, él incluido (ISO 8601; sin zona, UTC)"),
    hasta: datetime | None = Query(None, description="Solo las ejecutadas ANTES de este instante, él no incluido (ISO 8601; sin zona, UTC)"),
    limite: int = Query(ejecuciones.LIMITE_POR_OMISION, ge=1, le=ejecuciones.LIMITE_MAXIMO, description="Cuántas entregar"),
    antes: str | None = Query(None, description="El id de la última ejecución que ya se vio"),
    authorization: str | None = Header(default=None),
):
    aislamiento.exigir(authorization, tenant, "ejecucion")
    try:
        return ejecuciones.listar(tenant, expediente, proceso, limite, antes, persona=persona, caso=caso, desde=desde, hasta=hasta)
    except ejecuciones.SinBase as exc:
        raise _sin_base(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.get(
    "/filtros",
    tags=["Ejecuciones"],
    summary="Lo que se puede elegir para filtrar el historial",
    description=(
        "`procesos` (id y nombre) y `personas` (id y nombre, `null` si ya no se puede leer), cada lista por orden de nombre. Salen de lo que está EN el "
        "historial, no del catálogo vivo: un proceso borrado, o alguien que ya no está, siguen ahí y se pueden filtrar. Cada proceso trae el nombre de su "
        "ejecución más reciente. Lo ven todos los roles. Sin base, vacío."
    ),
)
def filtros(tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "ejecucion")
    try:
        return ejecuciones.opciones_de_filtro(tenant)
    except ejecuciones.SinBase as exc:
        raise _sin_base(exc) from exc


# `/procesos` va ANTES de `/{ejecucion_id}`: si no, «procesos» se tomaría por el id de una ejecución.
@router.get(
    "/procesos",
    tags=["Ejecuciones"],
    summary="Los procesos que se ofrecen para ejecutar",
    description=(
        "`{procesos: [{id, nombre, activo, ejecutable}]}`: TODOS los procesos de la organización (activos e inactivos: la pantalla filtra `activo`), en el orden "
        "de su creación. `ejecutable` dice si se puede correr (tiene su nodo de Resultado con una regla conectada); uno que no lo es se ofrece igual y la pantalla "
        "explica por qué no se ejecuta. Un proceso corre sobre cualquier expediente de la organización. Lo ven todos los roles (es solo el nombre, si está activo "
        "y si se puede ejecutar; el dibujo es de `/procesos`, que solo leen Administrador y Supervisor). Sin base, vacío."
    ),
)
def procesos(tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "ejecucion")
    try:
        return {"procesos": ejecuciones.procesos_ofrecidos(tenant)}
    except ejecuciones.SinBase as exc:
        raise _sin_base(exc) from exc


@router.get(
    "/{ejecucion_id}",
    tags=["Ejecuciones"],
    summary="Abrir una ejecución",
    description=(
        "La ejecución completa: su resumen, la lista de valores finales, la foto de cómo salió cada regla y el proceso tal como estaba. 404 si no es de la "
        "organización o no existe (no se distingue)."
    ),
)
def ver(ejecucion_id: str, tenant: str = Query(..., description="La organización"), authorization: str | None = Header(default=None)):
    aislamiento.exigir(authorization, tenant, "ejecucion")
    try:
        ejecucion = ejecuciones.obtener(tenant, ejecucion_id)
    except ejecuciones.SinBase as exc:
        raise _sin_base(exc) from exc
    if ejecucion is None:
        raise _no_existe(ejecuciones.NoEncontrado("ejecucion"))
    return ejecucion
