"""Ciclo de vida de procesadores de Document AI: lo que hay detrás de "Activar".

Activar un tipo documental crea (o adopta) su Custom Extractor y le sube el
esquema armado desde el wizard. Son tres llamadas repartidas en DOS versiones
de la API — y eso no es un descuido nuestro, es la superficie real de Google:

  1. POST  v1       .../processors                  crear (síncrono)
  2. PATCH v1beta3  .../processors/{id}/dataset     inicializar (LRO, se pollea)
  3. PATCH v1beta3  .../dataset/datasetSchema       subir el esquema (síncrono)

El recurso `dataset` y su esquema NO existen en v1: solo en v1beta3. El
`:process` de siempre sigue en v1. Mismo httpx + google-auth de siempre; el
cambio de versión es solo un segmento de la URL.

No hay paso de "entrenar" ni de "desplegar": el procesador nace con las
versiones `pretrained-foundation-model-*` ya utilizables (zero-shot). Está
verificado en este mismo proyecto — el procesador de INE nunca se entrenó y
extrae con confianza >0.99.

Sobre la idempotencia, que aquí es una trampa real: `processors.create` NO es
idempotente (no acepta requestId) y `processors.list` no tiene filtro. Un
doble clic en "Activar" crearía dos procesadores. El guardia es el
displayName: se nombra `nexusdoc--{id_del_tipo}` y ANTES de crear se lista y
se busca ese nombre — si ya existe, se adopta en lugar de duplicar.
"""

import asyncio
import logging
from typing import Any

import httpx

from config import DOCAI_LOCATION, DOCAI_PROJECT_ID
from servicios.ia import SCOPES, _token  # misma credencial cacheada que /ia

logger = logging.getLogger(__name__)

_PADRE = f"projects/{DOCAI_PROJECT_ID}/locations/{DOCAI_LOCATION}"


def _url(api: str, ruta: str) -> str:
    return f"https://{DOCAI_LOCATION}-documentai.googleapis.com/{api}/{ruta}"


def _prefijo_display(id_tipo: str) -> str:
    # Doble guion como separador: un displayName con guiones simples adentro
    # (el propio id) no se confunde con el prefijo.
    return f"nexusdoc--{id_tipo}"


async def _pedir(
    cliente: httpx.AsyncClient, metodo: str, api: str, ruta: str, **kwargs: Any
) -> dict:
    r = await cliente.request(
        metodo,
        _url(api, ruta),
        headers={"Authorization": f"Bearer {_token()}"},
        timeout=60,
        **kwargs,
    )
    if r.status_code >= 400:
        detalle = ""
        try:
            detalle = r.json().get("error", {}).get("message", "")
        except Exception:
            detalle = r.text[:300]
        raise RuntimeError(f"Document AI respondió {r.status_code} en {metodo} {ruta}: {detalle}")
    return r.json() if r.content else {}


async def _esperar_operacion(cliente: httpx.AsyncClient, nombre_op: str) -> dict:
    """Pollea una google.longrunning.Operation hasta que termine.

    El único paso lento de la activación es inicializar el dataset. Google no
    publica cuánto tarda; medido aquí anda en segundos. El tope de ~90s existe
    para que el spinner del front ("Validando configuración...") nunca quede
    colgado para siempre: mejor un error que explica, que una espera infinita.
    """
    for _ in range(30):
        op = await _pedir(cliente, "GET", "v1beta3", nombre_op)
        if op.get("done"):
            if "error" in op:
                raise RuntimeError(f"La operación {nombre_op} falló: {op['error']}")
            return op
        await asyncio.sleep(3)
    raise RuntimeError(f"La operación {nombre_op} no terminó en 90s.")


async def _buscar_por_display(cliente: httpx.AsyncClient, display: str) -> dict | None:
    """Busca un procesador por displayName paginando `list` (no hay filtro)."""
    ruta = f"{_PADRE}/processors"
    token = ""
    while True:
        params = {"pageSize": 100}
        if token:
            params["pageToken"] = token
        pagina = await _pedir(cliente, "GET", "v1", ruta, params=params)
        for p in pagina.get("processors", []):
            if p.get("displayName") == display:
                return p
        token = pagina.get("nextPageToken", "")
        if not token:
            return None


async def activar_tipo_documental(
    id_tipo: str, nombre: str, esquema: dict
) -> dict:
    """Crea (o adopta) el procesador del tipo documental y le sube su esquema.

    Devuelve lo que el front debe persistir junto al tipo documental:
    `procesadorId`, `procesadorNombre` (la ruta completa del recurso),
    `versionDefault` (la foundation con la que nace, para poder FIJARLA en cada
    `:process` — la reproducibilidad ya nos mordió una vez), y si el procesador
    se creó o ya existía.
    """
    if not DOCAI_PROJECT_ID:
        raise RuntimeError("Falta DOCAI_PROJECT_ID en el .env — revisa .env.example")

    display = _prefijo_display(id_tipo)
    async with httpx.AsyncClient() as cliente:
        existente = await _buscar_por_display(cliente, display)
        creado = existente is None

        if existente is None:
            procesador = await _pedir(
                cliente,
                "POST",
                "v1",
                f"{_PADRE}/processors",
                json={"type": "CUSTOM_EXTRACTION_PROCESSOR", "displayName": display},
            )
            logger.info("Procesador creado para el tipo %s: %s", id_tipo, procesador.get("name"))
        else:
            procesador = existente
            logger.info("Procesador ya existía para el tipo %s: se adopta.", id_tipo)

        nombre_recurso = procesador["name"]  # projects/.../processors/{id}

        # El dataset es el contenedor del esquema (y, en el futuro, de los
        # documentos de ejemplo del few-shot de HU039-041). `unmanaged` =
        # almacenamiento administrado por Google, sin bucket propio.
        #
        # Reinicializarlo NO es inocuo — Google responde 400 "Dataset is
        # already initialized" (medido en la prueba e2e, no supuesto). Para un
        # procesador adoptado ese error significa "ya está listo", así que se
        # traga; cualquier otro 400 sí es un problema y se propaga.
        try:
            op = await _pedir(
                cliente,
                "PATCH",
                "v1beta3",
                f"{nombre_recurso}/dataset",
                params={"updateMask": "unmanaged_dataset_config"},
                json={"name": f"{nombre_recurso}/dataset", "unmanagedDatasetConfig": {}},
            )
        except RuntimeError as exc:
            if "already initialized" not in str(exc):
                raise
            op = {}
        if op.get("name") and not op.get("done"):
            await _esperar_operacion(cliente, op["name"])

        await _pedir(
            cliente,
            "PATCH",
            "v1beta3",
            f"{nombre_recurso}/dataset/datasetSchema",
            json={
                "name": f"{nombre_recurso}/dataset/datasetSchema",
                "documentSchema": esquema,
            },
        )

        # Se relee el procesador para conocer su versión default REAL (la
        # foundation vigente al momento de crear), en vez de suponerla: es lo
        # que el front debe fijar en cada :process.
        procesador = await _pedir(cliente, "GET", "v1", nombre_recurso)

    version = (procesador.get("defaultProcessorVersion") or "").split("/")[-1]
    return {
        "procesadorId": nombre_recurso.split("/")[-1],
        "procesadorNombre": nombre_recurso,
        "versionDefault": version,
        "creado": creado,
        "camposEnEsquema": len(esquema["entityTypes"][0]["properties"]),
    }


async def eliminar_procesador(procesador_id: str) -> None:
    """Borra un procesador. Existe para las pruebas y para deshacer errores —
    ningún flujo del producto lo llama todavía. Es IRREVERSIBLE: borra también
    su dataset y su esquema."""
    async with httpx.AsyncClient() as cliente:
        op = await _pedir(
            cliente, "DELETE", "v1", f"{_PADRE}/processors/{procesador_id}"
        )
        if op.get("name") and not op.get("done"):
            # delete devuelve una Operation de v1; el poller pega a v1beta3,
            # que también la sirve — es el mismo espacio de operaciones.
            await _esperar_operacion(cliente, op["name"])
