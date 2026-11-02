"""Dominio: ejecuciones — correr un proceso del motor de reglas sobre un expediente virtual, y su historial (ver `servicios/ejecuciones.py`).

Una ejecución junta UN expediente virtual con UN proceso. El back no corre las reglas (eso vive en `reglas.ts`, que el servidor del front ejecuta con los
datos reales): aquí se PREPARA lo que la ejecución necesita y se GUARDA lo que salió. El historial es una foto inmutable: no hay rutas que lo editen o
lo borren.

Todas las rutas van detrás de la llave de servicio y exigen TAMBIÉN el JWT de la persona (HU11), igual que procesos y expedientes.

## Quién puede qué

- **Ver el historial** (listar y abrir una ejecución): cualquiera que opere en la organización, de cualquier rol.
- **Ejecutar** (preparar y guardar): `roles.PUEDEN_ORGANIZAR` (Administrador, Supervisor, Analista y Operador), igual que subir archivos a un expediente;
  Compliance, Auditor y Viewer reciben 403 `sin_permiso`. Es una propuesta de producto pendiente de confirmar con César.

## Qué se anota

`PROCESS_RUN_RECORDED` cuando una ejecución queda en el historial, con quién, qué proceso, sobre qué expediente virtual y cómo terminó. Preparar, o
una ejecución que no se corrió por duplicados, no dejan rastro.
"""

import logging

from fastapi import APIRouter, Header, HTTPException, Query, status
from pydantic import BaseModel, Field

from servicios import aislamiento, auditoria, ejecuciones

logger = logging.getLogger(__name__)

router = APIRouter()

_MENSAJES_NO_EXISTE = {
    "virtual": "Ese expediente virtual no existe.",
    "proceso": "Ese proceso no existe.",
    "ejecucion": "Esa ejecución no existe.",
}


class NuevaEjecucion(BaseModel):
    tenant: str = Field(..., description="La organización")
    expedienteId: str = Field(..., description="El expediente al que pertenece el expediente virtual")
    virtualId: str = Field(..., description="El expediente virtual sobre el que se corrió")
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
    summary="Preparar una ejecución: el proceso, sus tipos documentales y los datos reales del expediente virtual",
    description=(
        "Todo lo que una ejecución necesita en UNA lectura: `proceso` (con su dibujo y su revisión), `tipos` (los tipos documentales que usa, con sus "
        "campos), `documentos` (por cada tipo: los documentos del expediente virtual que lo cubren y, si es uno solo, sus valores VIGENTES por el nombre "
        "del campo en la biblioteca) y `duplicados` (los tipos que el proceso usa y el expediente virtual trae repetidos: con alguno, la ejecución no "
        "debe seguir). 409 con `codigo` si el proceso no se puede ejecutar: `proceso_inactivo`, `no_corre_aqui`, `sin_resultado` o `resultado_sin_regla`."
    ),
)
def preparar(
    tenant: str = Query(..., description="La organización"),
    expediente: str = Query(..., description="El expediente"),
    virtual: str = Query(..., description="El expediente virtual sobre el que se va a correr"),
    proceso: str = Query(..., description="El proceso que se va a correr"),
    authorization: str | None = Header(default=None),
):
    aislamiento.exigir_organizar(authorization, tenant, "ejecucion")
    try:
        return ejecuciones.preparar(tenant, expediente, virtual, proceso)
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
        "Guarda lo que salió de correr la lógica del lienzo con los datos que entregó `/ejecuciones/preparar`. Los nombres y el dibujo del proceso los toma el "
        "servidor, no la petición. 400 si algo no cuadra con el proceso (una palabra del resultado que no es la suya, un valor final que no es de él); "
        "409 `proceso_cambio` si el proceso se guardó otra vez desde que se preparó (hay que volver a ejecutar). Es historial: una vez guardada no se edita ni se borra."
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
        expedienteVirtualId=ejecucion["virtual"]["id"],
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
        "De la más reciente a la más antigua. Filtros opcionales por `expediente`, `virtual` y `proceso` (sus ids). `limite` (1 a 200, por omisión 50) "
        "dice cuántas entrega y `antes` es el id de la última que ya se vio, para pedir las siguientes. Devuelve `ejecuciones`, `total` (de las que cumplen "
        "los filtros) y `hayMas`. Sin base, vacío."
    ),
)
def listar(
    tenant: str = Query(..., description="La organización"),
    expediente: str | None = Query(None, description="Solo las de este expediente"),
    virtual: str | None = Query(None, description="Solo las de este expediente virtual"),
    proceso: str | None = Query(None, description="Solo las de este proceso"),
    limite: int = Query(ejecuciones.LIMITE_POR_OMISION, ge=1, le=ejecuciones.LIMITE_MAXIMO, description="Cuántas entregar"),
    antes: str | None = Query(None, description="El id de la última ejecución que ya se vio"),
    authorization: str | None = Header(default=None),
):
    aislamiento.exigir(authorization, tenant, "ejecucion")
    try:
        return ejecuciones.listar(tenant, expediente, virtual, proceso, limite, antes)
    except ejecuciones.SinBase as exc:
        raise _sin_base(exc) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


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
