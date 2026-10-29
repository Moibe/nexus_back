"""Endpoints del ciclo de vida de procesadores: el "Activar" del front.

El front manda el tipo documental COMPLETO (nombre, descripción y campos tal
como los capturó el wizard) al activarlo. Cuando exista la tabla
`config_version`, este endpoint pasará a recibir solo el id y a leer de ahí.

El CLASIFICADOR, en cambio, ya no recibe nada del front (2026-10-15): se arma con
los tipos ACTIVOS de la organización, leídos del registro del servidor
(`servicios/tipos_documentales.py`). Antes los mandaba cada navegador desde su
`localStorage`, y el último que sincronizaba pisaba a los demás.

Desde el 2026-10-26 cada organización tiene SU clasificador (`servicios/clasificadores.py`): estas rutas piden `tenant` y el
JWT de la persona (HU11), igual que tipos documentales, y solo tocan el de esa organización.
"""

import logging

import httpx
from fastapi import APIRouter, File, Form, Header, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from errores import SinClasificador
from routers.ia import _leer_subida
from servicios import aislamiento
from servicios import tipos_documentales as registro_tipos
from servicios.almacen import ErrorAlmacen
from servicios.esquema import CATEGORIA_OTRO, TIPO_GRUPO, esquema_clasificador_desde_tipos, esquema_desde_campos
from servicios.procesadores import (
    DocumentAIError,
    activar_tipo_documental,
    consultar_esquema_clasificador,
    consultar_procesador,
    eliminar_procesador,
    generar_campos as _generar_campos,
    sincronizar_clasificador as _sincronizar_clasificador,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class SubcampoEntrada(BaseModel):
    """Un campo DENTRO de un grupo (accionista → nombre, acciones, monto). Mismo molde que un campo suelto, sin cardinalidad
    (dentro de una fila cada subcampo aparece una vez) y sin poder ser, a su vez, otro grupo."""

    nombre: str = Field(min_length=1, max_length=200)
    tipoDato: str = "texto"
    descripcion: str = ""
    valorEstructura: str = ""
    obligatorio: bool = False
    valoresLista: list[str] = []


class CampoEntrada(BaseModel):
    nombre: str = Field(min_length=1, max_length=200)
    tipoDato: str = "texto"
    descripcion: str = ""
    valorEstructura: str = ""
    obligatorio: bool = False
    cardinalidad: str = "unico"
    valoresLista: list[str] = []
    # Solo con `tipoDato == "grupo"` (2026-10-16): los campos de cada fila. Un grupo sin ninguno no se puede activar.
    subcampos: list[SubcampoEntrada] = []


class TipoDocumentalEntrada(BaseModel):
    id: str = Field(min_length=1, max_length=100)
    nombre: str = Field(min_length=1, max_length=200)
    descripcion: str = ""
    campos: list[CampoEntrada]
    # La versión YA publicada (0 = nunca activado). "Activar" siempre manda 0;
    # "Crear nueva versión" manda la actual, para que el back publique la
    # SIGUIENTE como procesador nuevo en vez de adoptar el vigente — ver
    # `activar_tipo_documental`.
    version: int = Field(default=0, ge=0)


def _mensaje_para(exc: DocumentAIError, *, mensaje_4xx: str) -> str:
    """Traduce un DocumentAIError al mensaje que ve el usuario.

    El branch de 5xx SÍ es idéntico para activar y eliminar: en los dos casos
    significa lo mismo ("Document AI no respondió; no es algo que se arregle
    cambiando nada, solo reintentar"). El de 4xx NO puede compartirse: para
    activar, un 4xx casi siempre es el esquema mal formado, y "revisa los
    campos" es cierto y accionable. Para eliminar, un DELETE no tiene campos
    que revisar — decir eso ahí sería tan confuso como el texto técnico crudo
    que este mensaje existe para evitar. Cada endpoint manda SU frase.
    """
    if 400 <= exc.status_code < 500:
        return mensaje_4xx
    return "No se pudo contactar a Document AI en este momento. Intenta de nuevo en unos minutos."


@router.post(
    "/activar",
    tags=["Procesadores"],
    summary="Activar un tipo documental",
    description=(
        "Crea (o adopta, si un intento anterior ya lo dejó a medias) el Custom "
        "Extractor de Document AI para la SIGUIENTE versión del tipo documental "
        "(`version` + 1) y le sube el esquema armado desde sus campos. Una "
        "versión nunca reutiliza el procesador de la anterior — esa se queda "
        "viva sin tocarse. El procesador nace en modalidad zero-shot: extrae "
        "leyendo solo el esquema, sin entrenamiento. Devuelve `procesadorId` y "
        "`versionDefault`, que el front debe guardar junto al tipo — la versión "
        "se fija en cada extracción para que el resultado sea reproducible — "
        "más `procesadorDisplayName`, que es el nombre VISIBLE del procesador "
        "en la consola de GCP (algo como `nexusdoc--tipo-abc--v2--ine`): sirve "
        "para que una persona pueda casar el tipo documental de NexusDoc con "
        "el procesador que ve en Document AI."
    ),
)
async def activar(tipo: TipoDocumentalEntrada):
    # Sin campos no hay nada que extraer: un procesador con esquema vacío se
    # crearía bien y fallaría después, en la primera extracción, lejos de la
    # causa. Mejor el error aquí, donde todavía se llama por su nombre.
    if not tipo.campos:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="El tipo documental no tiene campos de extracción; agrega al menos uno antes de activar.",
        )

    # Un grupo es un campo que trae campos: sin ninguno, o con otro grupo adentro, no hay un esquema válido que armar. Se dice
    # aquí, con el nombre del grupo, y no se deja que el generador lo omita en silencio (el tipo se activaría sin ese dato).
    for campo in tipo.campos:
        if campo.tipoDato != TIPO_GRUPO:
            continue
        if not campo.subcampos:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"El grupo «{campo.nombre}» no tiene campos; agrega al menos uno antes de activar.",
            )
        if any(sub.tipoDato == TIPO_GRUPO for sub in campo.subcampos):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"El grupo «{campo.nombre}» tiene un grupo adentro; los grupos solo pueden tener campos sueltos.",
            )

    esquema = esquema_desde_campos(
        tipo.nombre, tipo.descripcion, [c.model_dump() for c in tipo.campos]
    )
    try:
        resultado = await activar_tipo_documental(tipo.id, tipo.nombre, esquema, tipo.version)
    except DocumentAIError as exc:
        # El texto técnico completo (status HTTP, ruta, el JSON de error de
        # Google) va al log, no al usuario: un "Invalid JSON payload received
        # at process_options.schema_override..." no le dice nada a quien está
        # configurando un tipo documental, y en el peor caso expone detalles
        # internos (nombres de recurso de GCP) sin necesidad.
        logger.exception("Falló la activación del tipo %s", tipo.id)
        mensaje = _mensaje_para(
            exc,
            mensaje_4xx=(
                "Document AI rechazó la configuración de este modelo. Revisa "
                "los nombres y tipos de los campos, y vuelve a intentar la "
                "activación."
            ),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=mensaje) from exc
    except RuntimeError as exc:
        # Fallas que NO vienen de una respuesta HTTP de Google (falta
        # DOCAI_PROJECT_ID en el .env, o el polling de la operación se agotó a
        # los 90s): no hay un status code que triar, así que van a un mensaje
        # genérico pero honesto — nunca el texto técnico crudo.
        logger.exception("Falló la activación del tipo %s", tipo.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo completar la activación. Intenta de nuevo; si el problema persiste, avisa al equipo técnico.",
        ) from exc

    return resultado


@router.post(
    "/generar-campos",
    tags=["Procesadores"],
    summary="Proponer campos a partir de un documento de ejemplo",
    description=(
        "«Generar a partir de un documento» del paso 2 del alta de un tipo documental: Document AI lee el documento y propone "
        "los campos a extraer (nombre, tipo de dato, descripción, si se repite, y los campos de cada grupo). No guarda nada ni "
        "crea procesadores: devuelve `campos` para que la persona los revise y agregue los que quiera. Tarda unos segundos. Lo "
        "propuesto puede variar un poco de una vez a otra. Pide el rol que administra tipos documentales."
    ),
)
async def generar_campos(
    archivo: UploadFile = File(..., description="El documento de ejemplo (PDF o imagen)"),
    tenant: str = Form(..., description="La organización"),
    nombre: str = Form("", max_length=200, description="El nombre del tipo documental, si ya se capturó: viaja como contexto"),
    descripcion: str = Form("", max_length=2000, description="Su descripción, si ya se capturó: viaja como contexto"),
    authorization: str | None = Header(default=None),
):
    aislamiento.exigir_administrar(authorization, tenant, "tipo_documental")
    contenido = await _leer_subida(archivo)
    try:
        campos = await _generar_campos(contenido, archivo.content_type or "application/pdf", nombre, descripcion)
    except DocumentAIError as exc:
        logger.exception("Falló la generación de campos a partir de un documento")
        if 400 <= exc.status_code < 500:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    "Document AI no pudo leer este documento para proponer campos. Revisa que esté completo, que no tenga "
                    "contraseña y que no sea demasiado largo, o prueba con otro ejemplo."
                ),
            ) from exc
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=_mensaje_para(exc, mensaje_4xx="")) from exc
    except httpx.TimeoutException as exc:
        logger.exception("Document AI no respondió a tiempo al generar campos")
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Document AI tardó demasiado en proponer los campos. Intenta de nuevo o prueba con un documento más corto.",
        ) from exc
    except RuntimeError as exc:
        logger.exception("Falló la generación de campos a partir de un documento")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudieron proponer campos. Intenta de nuevo; si el problema persiste, avisa al equipo técnico.",
        ) from exc
    return {"campos": campos}


def _tipos_activos(tenant: str) -> list[dict]:
    """Los tipos activos de ESTA organización, del registro del servidor. 503 si el registro no se puede leer."""
    try:
        return registro_tipos.tipos_activos(tenant)
    except ErrorAlmacen as exc:
        logger.exception("No se pudo leer el registro de tipos documentales")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="El servicio no está disponible en este momento. Intenta de nuevo en unos minutos.",
        ) from exc


def _categorias_esperadas(tipos: list[dict]) -> list[tuple[str, str]]:
    """Lo que el clasificador DEBE tener en Google, como pares `(categoría, descripción)` ordenados y sin `otro`.

    Se arma con el MISMO código que arma el esquema que se sube, no con una copia de sus reglas: así no pueden divergir."""
    esquema = esquema_clasificador_desde_tipos(tipos)
    pares = [(t["name"], (t.get("description") or "").strip()) for t in esquema["entityTypes"] if t["name"] != CATEGORIA_OTRO]
    return sorted(pares)


async def _sincronizar(tipos: list[dict], tenant: str) -> dict:
    try:
        return await _sincronizar_clasificador(tipos, tenant)
    except DocumentAIError as exc:
        logger.exception("Falló la sincronización del clasificador")
        mensaje = _mensaje_para(
            exc,
            mensaje_4xx=(
                "Document AI rechazó la configuración del clasificador. Revisa "
                "los tipos documentales activos y vuelve a intentar."
            ),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=mensaje) from exc
    except RuntimeError as exc:
        logger.exception("Falló la sincronización del clasificador")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo sincronizar el clasificador. Intenta de nuevo; si el problema persiste, avisa al equipo técnico.",
        ) from exc


@router.post(
    "/clasificador/sincronizar",
    tags=["Procesadores"],
    summary="Sincronizar el Classifier de una organización con sus tipos documentales activos",
    description=(
        "Crea (la primera vez) o actualiza el Custom Document Classifier de la "
        "organización `tenant`, con un EntityType por cada tipo documental ACTIVO "
        "de ESA organización. **No recibe cuerpo**: la lista sale del registro de "
        "tipos documentales del servidor, así que lo que se haya guardado "
        "(activar, archivar o borrar un tipo) tiene que estar guardado ANTES de "
        "llamar. Pide el JWT de quien administra esa organización (o el del "
        "administrador de plataforma, en su espacio). Devuelve `procesadorId`."
    ),
)
async def sincronizar_clasificador(
    tenant: str = Query(..., description="La organización"),
    authorization: str | None = Header(default=None),
):
    aislamiento.exigir_administrar(authorization, tenant, "clasificador")
    tipos = _tipos_activos(tenant)
    # Sin tipos no hay nada que distinguir: un Classifier con cero categorías
    # se crearía bien y fallaría después, al clasificar, lejos de la causa —
    # mismo criterio que el guardia de "sin campos" en `activar`.
    if not tipos:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No hay tipos documentales activos con los que armar el clasificador.",
        )
    return await _sincronizar(tipos, tenant)


@router.post(
    "/clasificador/asegurar",
    tags=["Procesadores"],
    summary="Comprobar que el Classifier está al día y, si no, sincronizarlo",
    description=(
        "Compara las categorías que el Classifier de la organización tiene AHORA en "
        "Document AI (nombre y descripción, que es prompt real) con sus tipos activos "
        "del registro, y lo sincroniza solo si difieren (o lo crea, si todavía no "
        "tiene). La lectura no cobra. El front lo llama una vez por sesión, antes de "
        "clasificar: cubre el desfase que dejan una sincronización fallida o un tipo "
        "tocado a mano. Pide el JWT de cualquier persona de la organización. "
        "Devuelve `sincronizado` (true si hubo que corregirlo)."
    ),
)
async def asegurar_clasificador(
    tenant: str = Query(..., description="La organización"),
    authorization: str | None = Header(default=None),
):
    aislamiento.exigir(authorization, tenant, "clasificador")
    tipos = _tipos_activos(tenant)
    if not tipos:
        # Nada que exigirle; el back rechazaría esa lista vacía de todos modos.
        return {"sincronizado": False, "motivo": "sin_tipos"}
    try:
        vigente = await consultar_esquema_clasificador(tenant)
    except SinClasificador:
        # Todavía no se creó el de esta organización (tiene tipos activos pero nunca se sincronizó): se crea ahora.
        await _sincronizar(tipos, tenant)
        return {"sincronizado": True, "motivo": "sin_clasificador"}
    except (DocumentAIError, RuntimeError):
        # Sin poder leerlo no se puede comparar (aún no se sincronizó nunca, o Google no responde): se sincroniza, que es
        # idempotente y lo crea si falta. Si Google está caído, falla igual y el error llega con su mensaje.
        logger.warning("No se pudo leer el clasificador para compararlo; se sincroniza directamente.")
        await _sincronizar(tipos, tenant)
        return {"sincronizado": True, "motivo": "ilegible"}

    en_google = sorted(
        (c["name"], (c.get("description") or "").strip()) for c in vigente["categorias"] if c.get("name") and c["name"] != CATEGORIA_OTRO
    )
    if en_google == _categorias_esperadas(tipos):
        return {"sincronizado": False, "motivo": "al_dia"}
    logger.warning("El clasificador estaba desfasado y se resincroniza. En Google: %s · En el registro: %s", en_google, _categorias_esperadas(tipos))
    await _sincronizar(tipos, tenant)
    return {"sincronizado": True, "motivo": "desfasado"}


@router.get(
    "/clasificador",
    tags=["Procesadores"],
    summary="Ver el esquema VIGENTE del Classifier de una organización",
    description=(
        "Diagnóstico: devuelve las categorías que el Custom Document "
        "Classifier de la organización tiene AHORA MISMO en Document AI (`name`, `displayName` y "
        "`description` de cada una), leídas de Google y no reconstruidas de "
        "este lado — que es lo único que sirve para detectar un desfase entre "
        "lo que creemos haber subido y lo que quedó. Incluye también la "
        "versión default del procesador y la que de verdad se usa al "
        "clasificar (`versionEnUso`, fijada en el `.env`), para poder ver si "
        "divergieron. Lectura pura: no cobra y no modifica nada. 404 si la "
        "organización todavía no tiene clasificador. Pide el JWT de quien administra."
    ),
)
async def ver_clasificador(
    tenant: str = Query(..., description="La organización"),
    authorization: str | None = Header(default=None),
):
    aislamiento.exigir_administrar(authorization, tenant, "clasificador")
    try:
        return await consultar_esquema_clasificador(tenant)
    except SinClasificador as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except DocumentAIError as exc:
        logger.exception("Falló la consulta del esquema del clasificador")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_mensaje_para(
                exc,
                mensaje_4xx=(
                    "Document AI no permitió leer el esquema del clasificador. "
                    "Puede que todavía no se haya sincronizado ninguna vez."
                ),
            ),
        ) from exc
    except RuntimeError as exc:
        logger.exception("Falló la consulta del esquema del clasificador")
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc


@router.get(
    "/{procesador_id}",
    tags=["Procesadores"],
    summary="Consultar un Custom Extractor por su id",
    description=(
        "Lee un procesador de Document AI sin tocarlo: devuelve su "
        "`procesadorDisplayName` (el nombre visible en la consola de GCP), su "
        "`versionDefault` y su `estado`. Existe para que el front pueda "
        "rellenar el nombre de tipos documentales activados ANTES de que "
        "`/procesadores/activar` empezara a devolverlo — republicar solo para "
        "eso costaría un Custom Extractor nuevo por tipo. Es lectura pura: "
        "`processors.get` no cobra (la facturación de Document AI es por "
        "página PROCESADA) y no modifica nada."
    ),
)
async def consultar(procesador_id: str):
    try:
        return await consultar_procesador(procesador_id)
    except DocumentAIError as exc:
        if exc.status_code == 404:
            # El procesador que el tipo documental dice tener ya no está en
            # Google (borrado a mano, o de otro proyecto). Se responde 404 tal
            # cual: quien llama tiene que poder distinguir "no existe" de "no
            # se pudo consultar", porque significan cosas muy distintas para
            # el registro local que lo apunta.
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Ese procesador ya no existe en Document AI.",
            ) from exc
        logger.exception("Falló la consulta del procesador %s", procesador_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=_mensaje_para(
                exc,
                mensaje_4xx=(
                    "Document AI no permitió consultar este procesador. "
                    "Intenta de nuevo; si el problema persiste, avisa al "
                    "equipo técnico."
                ),
            ),
        ) from exc
    except RuntimeError as exc:
        logger.exception("Falló la consulta del procesador %s", procesador_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo consultar el procesador en Document AI.",
        ) from exc


@router.delete(
    "/{procesador_id}",
    tags=["Procesadores"],
    summary="Borrar el Custom Extractor de un tipo documental",
    description=(
        "Borra el procesador de Document AI, su dataset y su esquema. "
        "IRREVERSIBLE. Si el procesador ya no existe en Google (borrado a mano, "
        "o nunca se llegó a crear), responde éxito igual: el objetivo es que no "
        "quede ahí, y si ya no está, el objetivo ya se cumplió."
    ),
)
async def eliminar(procesador_id: str):
    try:
        await eliminar_procesador(procesador_id)
    except DocumentAIError as exc:
        if exc.status_code == 404:
            # Ya no existe: exactamente el estado que se pedía. No es un error
            # para quien llamó — insistir en que "falló" cuando el procesador
            # ya no está ahí sería mentir en la dirección contraria.
            logger.info("Procesador %s ya no existía al querer borrarlo.", procesador_id)
            return {"eliminado": True}
        logger.exception("Falló el borrado del procesador %s", procesador_id)
        # A diferencia de activar, un 4xx aquí NO es "revisa tus campos" — un
        # DELETE no tiene campos que revisar. Lo más honesto es admitir que
        # Google no lo permitió, sin inventar una causa que no se conoce.
        mensaje = _mensaje_para(
            exc,
            mensaje_4xx=(
                "Document AI no permitió borrar este procesador en este "
                "momento. Intenta de nuevo; si el problema persiste, avisa "
                "al equipo técnico."
            ),
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=mensaje) from exc
    except RuntimeError as exc:
        logger.exception("Falló el borrado del procesador %s", procesador_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="No se pudo completar el borrado. Intenta de nuevo; si el problema persiste, avisa al equipo técnico.",
        ) from exc

    return {"eliminado": True}
