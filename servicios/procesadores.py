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
displayName: se nombra `nexusdoc--{id_del_tipo}--v{version}` y ANTES de crear
se lista y se busca ese nombre exacto — si ya existe, se adopta en lugar de
duplicar.

La versión no es cosmética: "Crear nueva versión" del front (2026-09-02)
pide una y otra vez un procesador GENUINAMENTE NUEVO para el mismo tipo
documental, no una adopción del que ya estaba activo — el viejo se queda
vivo tal cual en Document AI, sin tocarlo, como historial. Por eso la
búsqueda de idempotencia va ESCOPEADA a la versión que se está publicando
(`v{n}`), no al tipo completo: dos publicaciones del mismo tipo en momentos
distintos deben crear procesadores DISTINTOS, pero dos clics de la MISMA
publicación (un doble clic accidental) deben adoptarse entre sí.

`sincronizar_clasificador` (2026-09-06) es un ciclo de vida DISTINTO sobre el
mismo mecanismo: un Custom Document Classifier (no Extractor) que distingue
ENTRE los tipos documentales activos, para el paso "identificar qué tipo de
documento es" entre la Bandeja de preparación y el pipeline. A diferencia del
Extractor, aquí hay UN procesador por ORGANIZACIÓN (desde el 2026-10-26; antes,
uno para todo el sistema — ver `servicios/clasificadores.py`), no uno por tipo
ni por versión, y su esquema se REEMPLAZA completo en cada sincronización en
vez de crear uno nuevo — ver el docstring de esa función para el detalle.
"""

import asyncio
import logging
import re
import time
import unicodedata
from typing import Any

import httpx

from config import (
    DOCAI_LOCATION,
    DOCAI_PROJECT_ID,
    DOCAI_VERSION_CLASIFICADOR,
)
from errores import SinClasificador
from servicios import clasificadores
from servicios.esquema import esquema_clasificador_desde_tipos, estructura_desde_esquema
from servicios.ia import SCOPES, _token  # misma credencial cacheada que /ia

logger = logging.getLogger(__name__)

_PADRE = f"projects/{DOCAI_PROJECT_ID}/locations/{DOCAI_LOCATION}"


def _url(api: str, ruta: str) -> str:
    return f"https://{DOCAI_LOCATION}-documentai.googleapis.com/{api}/{ruta}"


_PALABRAS_VACIAS = {
    "de", "del", "la", "el", "los", "las", "un", "una", "unos", "unas",
    "y", "e", "o", "u", "en", "para", "por", "con", "a", "al",
}


def _slug(texto: str) -> str:
    """"Póliza de seguro de auto" -> "poliza-seguro-auto".

    Sin acentos ni espacios: Document AI no documenta ninguna restricción de
    charset para `displayName` (se verificó contra el discovery document), pero
    tampoco confirma que acepte Unicode sin sorpresas — ASCII plano es la
    apuesta segura para un campo que solo existe para leerse en la consola.

    Se quitan artículos/preposiciones sueltos porque no aportan nada a algo
    que solo se lee de un vistazo — si el texto es SOLO palabras vacías (caso
    raro), se conservan todas para no devolver un slug vacío.
    """
    sin_acentos = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    palabras = re.findall(r"[a-z0-9]+", sin_acentos.lower())
    significativas = [p for p in palabras if p not in _PALABRAS_VACIAS] or palabras
    return "-".join(significativas)[:60]


def _prefijo_display(id_tipo: str, version: int) -> str:
    """El PREFIJO estable que identifica UNA VERSIÓN de un tipo documental en
    Document AI.

    Solo esto se usa para BUSCAR (ver `_buscar_por_prefijo`): el id interno y
    la versión no cambian nunca para una publicación dada, a diferencia del
    nombre que el usuario le puso, que sí puede editarse. Si la búsqueda
    dependiera del nombre completo, renombrar un tipo documental y volver a
    activarlo (Publicar cambios) crearía un procesador DUPLICADO en vez de
    adoptar el existente — Document AI no tiene forma de renombrar un
    procesador ya creado ni de filtrar `processors.list` por nada que no sea
    id/parent, así que esto tiene que resolverse de este lado.

    La versión SÍ forma parte del prefijo (a diferencia de antes de
    "Crear nueva versión"): cada publicación es un procesador propio, y
    escopear la búsqueda por versión es lo que permite que dos publicaciones
    del mismo tipo, en momentos distintos, nunca se confundan entre sí ni se
    adopten por error.
    """
    return f"nexusdoc--{id_tipo}--v{version}"


def _display_completo(id_tipo: str, version: int, nombre: str) -> str:
    """El displayName real que se manda a crear: prefijo estable (con
    versión) + nombre legible, para que la consola de GCP diga algo como
    "nexusdoc--tipo-mtaq965y-1--v2--ine" en vez de "tipo-mtaq965y-1" a secas.

    Si el tipo se renombra después de activado, este texto se queda con el
    nombre VIEJO — Document AI no ofrece un `patch` para actualizarlo (se
    verificó: no existe en la API, en ninguna versión). Es un costo cosmético
    que se acepta a cambio de no duplicar procesadores; ver `_prefijo_display`.
    """
    slug = _slug(nombre)
    prefijo = _prefijo_display(id_tipo, version)
    return f"{prefijo}--{slug}" if slug else prefijo


class DocumentAIError(RuntimeError):
    """Google respondió con un error a una llamada concreta.

    Lleva `status_code` aparte del mensaje (que sigue siendo el texto técnico
    completo, para los logs) porque el router necesita DISTINGUIR dos casos que
    para el usuario significan cosas completamente distintas: un 4xx es Google
    rechazando ESTA configuración (algo que sí puede corregir); un 5xx o un
    timeout es que Document AI no respondió (nada que revisar, solo reintentar).
    Sin este campo, el router tendría que volver a parsear el mensaje para
    saberlo — fresco de una excepción cuyo `str()` ya se pensó para logs, no
    para bifurcar lógica.
    """

    def __init__(self, mensaje: str, status_code: int):
        super().__init__(mensaje)
        self.status_code = status_code


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
        raise DocumentAIError(
            f"Document AI respondió {r.status_code} en {metodo} {ruta}: {detalle}",
            status_code=r.status_code,
        )
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


async def _buscar_por_prefijo(cliente: httpx.AsyncClient, prefijo: str) -> dict | None:
    """Busca un procesador cuyo displayName EMPIECE con `prefijo`, paginando
    `list` (Document AI no tiene filtro server-side por nada que no sea
    id/parent — se verificó contra la API real).

    Por prefijo y no por igualdad exacta: el displayName completo incluye el
    nombre legible del tipo documental (ver `_display_completo`), que puede
    cambiar; el prefijo (`_prefijo_display`, basado en el id interno) no
    cambia nunca. Buscar por igualdad exacta perdería el procesador existente
    en cuanto el nombre visible cambiara, y crearía uno duplicado.

    El corte tiene que caer justo en el separador "--": comparar con
    `str.startswith(prefijo)` a secas confundiría, por ejemplo, la versión
    "v1" con "v10" (el segundo también empieza con el texto del primero como
    substring plano) — mismo problema que ya existía con el id del tipo, y
    que la versión hereda al vivir en el mismo prefijo.
    """
    ruta = f"{_PADRE}/processors"
    token = ""
    while True:
        params = {"pageSize": 100}
        if token:
            params["pageToken"] = token
        pagina = await _pedir(cliente, "GET", "v1", ruta, params=params)
        for p in pagina.get("processors", []):
            display = p.get("displayName", "")
            if display == prefijo or display.startswith(f"{prefijo}--"):
                return p
        token = pagina.get("nextPageToken", "")
        if not token:
            return None


async def _buscar_exacto(cliente: httpx.AsyncClient, display_name: str) -> dict | None:
    """Busca el procesador cuyo displayName es EXACTAMENTE `display_name`, paginando `list`.

    Para los clasificadores, no `_buscar_por_prefijo`: el del espacio de plataforma se llama `nexusdoc-clasificador` y el de cada
    organización `nexusdoc-clasificador--{guid}`, así que buscar el primero POR PREFIJO encontraría el de cualquier organización."""
    ruta = f"{_PADRE}/processors"
    token = ""
    while True:
        params = {"pageSize": 100}
        if token:
            params["pageToken"] = token
        pagina = await _pedir(cliente, "GET", "v1", ruta, params=params)
        for p in pagina.get("processors", []):
            if p.get("displayName", "") == display_name:
                return p
        token = pagina.get("nextPageToken", "")
        if not token:
            return None


async def activar_tipo_documental(
    id_tipo: str, nombre: str, esquema: dict, version_actual: int = 0
) -> dict:
    """Crea (o adopta) el procesador de ESTA versión del tipo documental y le
    sube su esquema.

    `version_actual` es la versión YA publicada según el front (0 si nunca se
    activó). Esta llamada publica la SIGUIENTE (`version_actual + 1`) — nunca
    reutiliza el procesador de una versión anterior, aunque exista: cada
    versión es un Custom Extractor propio, y la anterior se queda viva sin
    tocarse (Document AI no ofrece disable/patch más barato que eso, y no se
    pidió borrarla). Ver el docstring del módulo.

    Devuelve lo que el front debe persistir junto al tipo documental:
    `procesadorId`, `procesadorNombre` (la ruta completa del recurso),
    `versionDefault` (la foundation con la que nace, para poder FIJARLA en cada
    `:process` — la reproducibilidad ya nos mordió una vez), y si el procesador
    se creó o ya existía.
    """
    if not DOCAI_PROJECT_ID:
        raise RuntimeError("Falta DOCAI_PROJECT_ID en el .env — revisa .env.example")

    version_a_publicar = version_actual + 1
    prefijo = _prefijo_display(id_tipo, version_a_publicar)
    async with httpx.AsyncClient() as cliente:
        existente = await _buscar_por_prefijo(cliente, prefijo)
        creado = existente is None

        if existente is None:
            procesador = await _pedir(
                cliente,
                "POST",
                "v1",
                f"{_PADRE}/processors",
                json={
                    "type": "CUSTOM_EXTRACTION_PROCESSOR",
                    "displayName": _display_completo(id_tipo, version_a_publicar, nombre),
                },
            )
            logger.info(
                "Procesador creado para el tipo %s v%s: %s",
                id_tipo, version_a_publicar, procesador.get("name"),
            )
        else:
            procesador = existente
            logger.info(
                "Procesador ya existía para el tipo %s v%s: se adopta (doble clic).",
                id_tipo, version_a_publicar,
            )

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
        # Un esquema nuevo (también el de un «Activar» repetido, que adopta el procesador y lo vuelve a subir) hace vieja la
        # estructura que se tenía en memoria de este procesador.
        _CACHE_ESTRUCTURA.pop(nombre_recurso.split("/")[-1], None)

        # Se relee el procesador para conocer su versión default REAL (la
        # foundation vigente al momento de crear), en vez de suponerla: es lo
        # que el front debe fijar en cada :process.
        procesador = await _pedir(cliente, "GET", "v1", nombre_recurso)

    version = (procesador.get("defaultProcessorVersion") or "").split("/")[-1]
    return {
        "procesadorId": nombre_recurso.split("/")[-1],
        "procesadorNombre": nombre_recurso,
        # El nombre VISIBLE en la consola de GCP (`_display_completo`, algo
        # como "nexusdoc--tipo-mtaq965y-1--v2--ine"), aparte del `name` de
        # arriba, que es la ruta del recurso. Se devuelve desde el 2026-09-08
        # a pedido explícito: el front lo guarda y lo muestra en la ficha del
        # tipo documental para que una persona pueda casar lo que ve en
        # NexusDoc con lo que ve en la consola de Document AI. Se lee del
        # procesador releído, no se recalcula, justamente para que sea lo que
        # Google TIENE y no lo que nosotros creemos que le pusimos — para un
        # procesador adoptado (doble clic en "Activar") pueden diferir si el
        # tipo se renombró, porque Document AI no deja renombrar un procesador
        # ya creado.
        "procesadorDisplayName": procesador.get("displayName", ""),
        "versionDefault": version,
        "creado": creado,
        "camposEnEsquema": len(esquema["entityTypes"][0]["properties"]),
    }


# A diferencia de `_prefijo_display` (una entrada por VERSIÓN de UN tipo
# documental), el Classifier es UN SOLO procesador para TODO el sistema: no
# lleva id ni versión en el nombre, solo este prefijo fijo. Localizarlo así
# (en vez de, por ejemplo, guardar su id en el `.env`) reutiliza la MISMA
# idempotencia por displayName que ya resuelve el doble-clic de "Activar" —
# aquí resuelve, además, que "sincronizar" se pueda llamar tantas veces como
# cambien los tipos activos sin crear un segundo Classifier por accidente.
_PREFIJO_CLASIFICADOR = "nexusdoc-clasificador"


# Un candado por tenant: dos sincronizaciones de la MISMA organización a la vez (dos administradores guardando, o un doble clic) no
# deben crear dos clasificadores. Es de proceso, como `registro.candado`: la API corre un solo uvicorn.
_CANDADOS_CLASIFICADOR: dict[str, asyncio.Lock] = {}


async def sincronizar_clasificador(tipos: list[dict], tenant: str) -> dict:
    """Crea (la primera vez) o actualiza el Custom Document Classifier de ESTE tenant, con un EntityType por cada tipo
    documental de `tipos`.

    A diferencia de `activar_tipo_documental` (un Extractor NUEVO por cada versión que se publica, y el viejo se queda vivo como
    historial), aquí hay UN procesador por organización: la lista de categorías cambia con el tiempo, pero el processor es el
    mismo, y su `datasetSchema` se REEMPLAZA por completo en cada llamada (Document AI no ofrece un PATCH incremental de
    entityTypes, solo reemplazo total del esquema), así que un tipo renombrado o eliminado desaparece solo del esquema en la
    siguiente sincronización — no hace falta borrarlo a mano.

    Cuál es su procesador lo dice `servicios/clasificadores.py`: el id guardado de la organización (o el del `.env` si el tenant es
    el espacio de plataforma). Si no hay, o Google ya no lo tiene (se borró a mano), se busca por su nombre EXACTO —para adoptar
    el que un intento anterior dejó creado a medias— y solo si tampoco está se crea.

    `tipos` es la lista COMPLETA de tipos documentales ACTIVOS de ese tenant (`[{"id": ..., "nombre": ...}, ...]`).
    """
    if not DOCAI_PROJECT_ID:
        raise RuntimeError("Falta DOCAI_PROJECT_ID en el .env — revisa .env.example")

    nombre_visible = clasificadores.nombre_visible(tenant)
    candado = _CANDADOS_CLASIFICADOR.setdefault(tenant, asyncio.Lock())
    async with candado, httpx.AsyncClient() as cliente:
        procesador = None
        conocido = clasificadores.id_de(tenant)
        if conocido:
            try:
                procesador = await _pedir(cliente, "GET", "v1", f"{_PADRE}/processors/{conocido}")
            except DocumentAIError as exc:
                # Un 404 es «ese procesador ya no existe» (se borró en la consola): se sigue como si no hubiera. Cualquier otro
                # error es de Google y se deja subir.
                if exc.status_code != 404:
                    raise
                logger.warning("El clasificador %s de %s ya no existe en Google; se vuelve a buscar o a crear.", conocido, tenant)
        if procesador is None:
            procesador = await _buscar_exacto(cliente, nombre_visible)
        creado = procesador is None

        if procesador is None:
            procesador = await _pedir(
                cliente,
                "POST",
                "v1",
                f"{_PADRE}/processors",
                json={
                    "type": "CUSTOM_CLASSIFICATION_PROCESSOR",
                    "displayName": nombre_visible,
                },
            )
            logger.info("Classifier creado para %s: %s", tenant, procesador.get("name"))
        else:
            logger.info("Classifier de %s ya existía: se reutiliza para sincronizar.", tenant)

        nombre_recurso = procesador["name"]  # projects/.../processors/{id}
        # Se anota ANTES de subir el esquema: si lo que sigue falla, el procesador ya creado no se pierde y el siguiente intento lo
        # reutiliza sin tener que buscarlo por nombre.
        clasificadores.asignar(tenant, nombre_recurso.split("/")[-1])

        # Mismo dataset `unmanaged` que el Extractor, y el mismo trago de
        # "already initialized" cuando el processor ya se había sincronizado
        # antes — ver el comentario gemelo en `activar_tipo_documental`.
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

        esquema = esquema_clasificador_desde_tipos(tipos)
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

        procesador = await _pedir(cliente, "GET", "v1", nombre_recurso)

    version = (procesador.get("defaultProcessorVersion") or "").split("/")[-1]
    return {
        "procesadorId": nombre_recurso.split("/")[-1],
        "procesadorNombre": nombre_recurso,
        "versionDefault": version,
        "creado": creado,
        "categorias": len(esquema["entityTypes"]),
    }


async def consultar_esquema_clasificador(tenant: str) -> dict:
    """Lee el esquema VIGENTE del Classifier de ESTE tenant directo de Document AI: qué
    categorías tiene ahora mismo, tal como Google las guardó. Levanta `SinClasificador` si todavía no tiene.

    Es una herramienta de DIAGNÓSTICO (2026-09-08), no parte del flujo: cuando
    el clasificador no reconoce algo, la primera pregunta es "¿de verdad tiene
    la categoría que creemos que tiene?", y hasta ahora la única forma de
    responderla era abrir la consola de GCP. Se lee de la fuente en vez de
    reconstruirlo con `esquema_clasificador_desde_tipos`, que diría lo que
    DEBERÍA haberse subido — justo lo que no sirve para encontrar un desfase.

    El `datasetSchema` vive en v1beta3, igual que al subirlo. Lectura pura, no
    cobra y no modifica nada.
    """
    procesador_id = clasificadores.id_de(tenant)
    if not procesador_id:
        raise SinClasificador("Esta organización todavía no tiene clasificador: se crea al activar su primer tipo documental.")

    recurso = f"{_PADRE}/processors/{procesador_id}"
    async with httpx.AsyncClient() as cliente:
        procesador = await _pedir(cliente, "GET", "v1", recurso)
        esquema = await _pedir(cliente, "GET", "v1beta3", f"{recurso}/dataset/datasetSchema")

    tipos = (esquema.get("documentSchema") or {}).get("entityTypes") or []
    return {
        "procesadorId": procesador_id,
        "procesadorDisplayName": procesador.get("displayName", ""),
        "estado": procesador.get("state", ""),
        "versionDefault": (procesador.get("defaultProcessorVersion") or "").split("/")[-1],
        # La versión que de verdad se usa al clasificar puede NO ser la default:
        # se fija en el .env justo para que no cambie sin aviso. Se devuelven
        # las dos para poder ver si divergieron.
        "versionEnUso": DOCAI_VERSION_CLASIFICADOR or None,
        "categorias": [
            {
                "name": t.get("name", ""),
                "displayName": t.get("displayName", ""),
                "description": t.get("description", ""),
            }
            for t in tipos
        ],
    }


# La estructura de un procesador (qué es obligatorio, qué se repite, qué es grupo) solo cambia cuando se le sube un esquema
# nuevo, y eso lo invalida `activar_tipo_documental`. El tiempo es la red de seguridad para lo que cambie por fuera (alguien
# lo edita en la consola de Google): a lo más 5 minutos de estructura vieja.
_CACHE_ESTRUCTURA: dict[str, tuple[float, dict]] = {}
TTL_ESTRUCTURA_S = 300


async def estructura_del_procesador(procesador_id: str) -> dict:
    """La estructura (`esquema.estructura_desde_esquema`) del esquema que el procesador TIENE en Google, con memoria de 5 minutos.

    Se lee del procesador y no del registro de tipos documentales a propósito: el esquema de Google es lo que el modelo usa de
    verdad (un tipo en edición puede ir adelante de él), y sirve igual para el procesador de INE, que no tiene tipo en el
    registro. Es lectura pura: no cobra y no modifica nada. Devuelve `{}` si el procesador no tiene esquema."""
    ahora = time.monotonic()
    guardada = _CACHE_ESTRUCTURA.get(procesador_id)
    if guardada and ahora - guardada[0] < TTL_ESTRUCTURA_S:
        return guardada[1]
    async with httpx.AsyncClient() as cliente:
        respuesta = await _pedir(cliente, "GET", "v1beta3", f"{_PADRE}/processors/{procesador_id}/dataset/datasetSchema")
    estructura = estructura_desde_esquema(respuesta.get("documentSchema") or {})
    _CACHE_ESTRUCTURA[procesador_id] = (ahora, estructura)
    return estructura


async def consultar_procesador(procesador_id: str) -> dict:
    """Lee un procesador de Document AI: su nombre visible y su versión
    default, sin tocarlo.

    Existe (2026-09-08) porque el nombre visible solo se conocía en el momento
    de ACTIVAR, y los tipos documentales activados antes de esa fecha se
    quedaron sin él. Obligar a republicar solo para verlo sería carísimo: cada
    publicación crea un Custom Extractor nuevo. Con esto el front lo puede
    rellenar leyendo, que es gratis y además dice la VERDAD de Google, no una
    copia local que puede haber quedado vieja.

    Es de lectura pura: `processors.get` no cobra (la facturación es por página
    procesada) y no modifica nada.
    """
    async with httpx.AsyncClient() as cliente:
        procesador = await _pedir(
            cliente, "GET", "v1", f"{_PADRE}/processors/{procesador_id}"
        )
    return {
        "procesadorId": procesador_id,
        "procesadorDisplayName": procesador.get("displayName", ""),
        "versionDefault": (procesador.get("defaultProcessorVersion") or "").split("/")[-1],
        "estado": procesador.get("state", ""),
    }


async def eliminar_procesador(procesador_id: str) -> None:
    """Borra un procesador: lo usa el "Borrar" del front cuando el tipo
    documental ya tenía un Custom Extractor creado. Es IRREVERSIBLE: borra
    también su dataset y su esquema."""
    async with httpx.AsyncClient() as cliente:
        op = await _pedir(
            cliente, "DELETE", "v1", f"{_PADRE}/processors/{procesador_id}"
        )
        if op.get("name") and not op.get("done"):
            # delete devuelve una Operation de v1; el poller pega a v1beta3,
            # que también la sirve — es el mismo espacio de operaciones.
            await _esperar_operacion(cliente, op["name"])


async def listar_procesadores_nexusdoc() -> list[dict]:
    """TODOS los procesadores del proyecto cuyo nombre visible empieza con `nexusdoc`: los extractores
    (`nexusdoc--{id}--v{n}--{nombre}`) y los clasificadores (`nexusdoc-clasificador` y `nexusdoc-clasificador--{organización}`). Lectura pura: no cobra y no cambia nada.

    Existe para la limpieza de procesadores huérfanos (`limpiar_procesadores_huerfanos.py`): Document AI no ofrece
    filtro, así que se pagina `processors.list` completo."""
    ruta = f"{_PADRE}/processors"
    encontrados: list[dict] = []
    token = ""
    async with httpx.AsyncClient() as cliente:
        while True:
            params: dict[str, Any] = {"pageSize": 100}
            if token:
                params["pageToken"] = token
            pagina = await _pedir(cliente, "GET", "v1", ruta, params=params)
            for p in pagina.get("processors", []):
                nombre = p.get("displayName", "")
                if nombre.startswith("nexusdoc"):
                    encontrados.append(
                        {
                            "id": p.get("name", "").split("/")[-1],
                            "displayName": nombre,
                            "tipo": p.get("type", ""),
                            "creado": p.get("createTime", ""),
                        }
                    )
            token = pagina.get("nextPageToken", "")
            if not token:
                return encontrados
