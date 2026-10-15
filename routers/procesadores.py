"""Endpoints del ciclo de vida de procesadores: el "Activar" del front.

El front manda el tipo documental COMPLETO (nombre, descripción y campos tal
como los capturó el wizard) al activarlo. Cuando exista la tabla
`config_version`, este endpoint pasará a recibir solo el id y a leer de ahí.

El CLASIFICADOR, en cambio, ya no recibe nada del front (2026-10-15): se arma con
los tipos ACTIVOS de TODAS las organizaciones, leídos del registro del servidor
(`servicios/tipos_documentales.py`). Antes los mandaba cada navegador desde su
`localStorage`, y el último que sincronizaba pisaba a los demás.
"""

import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from servicios import tipos_documentales as registro_tipos
from servicios.almacen import ErrorAlmacen
from servicios.esquema import CATEGORIA_OTRO, esquema_clasificador_desde_tipos, esquema_desde_campos
from servicios.procesadores import (
    DocumentAIError,
    activar_tipo_documental,
    consultar_esquema_clasificador,
    consultar_procesador,
    eliminar_procesador,
    sincronizar_clasificador as _sincronizar_clasificador,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class CampoEntrada(BaseModel):
    nombre: str = Field(min_length=1, max_length=200)
    tipoDato: str = "texto"
    descripcion: str = ""
    valorEstructura: str = ""
    obligatorio: bool = False
    cardinalidad: str = "unico"
    valoresLista: list[str] = []


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


def _tipos_activos() -> list[dict]:
    """Los tipos activos de TODAS las organizaciones, del registro del servidor. 503 si el registro no se puede leer."""
    try:
        return registro_tipos.tipos_activos()
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


async def _sincronizar(tipos: list[dict]) -> dict:
    try:
        return await _sincronizar_clasificador(tipos)
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
    summary="Sincronizar el Classifier con los tipos documentales activos",
    description=(
        "Crea (la primera vez) o actualiza el ÚNICO Custom Document Classifier "
        "del sistema, con un EntityType por cada tipo documental ACTIVO de TODAS "
        "las organizaciones. **No recibe cuerpo**: la lista sale del registro de "
        "tipos documentales del servidor, así que lo que se haya guardado "
        "(activar, archivar o borrar un tipo) tiene que estar guardado ANTES de "
        "llamar. Devuelve `procesadorId`."
    ),
)
async def sincronizar_clasificador():
    tipos = _tipos_activos()
    # Sin tipos no hay nada que distinguir: un Classifier con cero categorías
    # se crearía bien y fallaría después, al clasificar, lejos de la causa —
    # mismo criterio que el guardia de "sin campos" en `activar`.
    if not tipos:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No hay tipos documentales activos con los que armar el clasificador.",
        )
    return await _sincronizar(tipos)


@router.post(
    "/clasificador/asegurar",
    tags=["Procesadores"],
    summary="Comprobar que el Classifier está al día y, si no, sincronizarlo",
    description=(
        "Compara las categorías que el Classifier tiene AHORA en Document AI "
        "(nombre y descripción, que es prompt real) con los tipos activos del "
        "registro, y lo sincroniza solo si difieren. La lectura no cobra. El "
        "front lo llama una vez por sesión, antes de clasificar: cubre el "
        "desfase que dejan una sincronización fallida o un tipo tocado a mano. "
        "Devuelve `sincronizado` (true si hubo que corregirlo)."
    ),
)
async def asegurar_clasificador():
    tipos = _tipos_activos()
    if not tipos:
        # Nada que exigirle; el back rechazaría esa lista vacía de todos modos.
        return {"sincronizado": False, "motivo": "sin_tipos"}
    try:
        vigente = await consultar_esquema_clasificador()
    except (DocumentAIError, RuntimeError):
        # Sin poder leerlo no se puede comparar (aún no se sincronizó nunca, o Google no responde): se sincroniza, que es
        # idempotente y lo crea si falta. Si Google está caído, falla igual y el error llega con su mensaje.
        logger.warning("No se pudo leer el clasificador para compararlo; se sincroniza directamente.")
        await _sincronizar(tipos)
        return {"sincronizado": True, "motivo": "ilegible"}

    en_google = sorted(
        (c["name"], (c.get("description") or "").strip()) for c in vigente["categorias"] if c.get("name") and c["name"] != CATEGORIA_OTRO
    )
    if en_google == _categorias_esperadas(tipos):
        return {"sincronizado": False, "motivo": "al_dia"}
    logger.warning("El clasificador estaba desfasado y se resincroniza. En Google: %s · En el registro: %s", en_google, _categorias_esperadas(tipos))
    await _sincronizar(tipos)
    return {"sincronizado": True, "motivo": "desfasado"}


@router.get(
    "/clasificador",
    tags=["Procesadores"],
    summary="Ver el esquema VIGENTE del Classifier",
    description=(
        "Diagnóstico: devuelve las categorías que el Custom Document "
        "Classifier tiene AHORA MISMO en Document AI (`name`, `displayName` y "
        "`description` de cada una), leídas de Google y no reconstruidas de "
        "este lado — que es lo único que sirve para detectar un desfase entre "
        "lo que creemos haber subido y lo que quedó. Incluye también la "
        "versión default del procesador y la que de verdad se usa al "
        "clasificar (`versionEnUso`, fijada en el `.env`), para poder ver si "
        "divergieron. Lectura pura: no cobra y no modifica nada."
    ),
)
async def ver_clasificador():
    try:
        return await consultar_esquema_clasificador()
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
