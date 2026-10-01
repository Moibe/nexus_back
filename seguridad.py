"""Las dos llaves que acepta esta API, las dos en el header `X-API-Key`.

Contexto: el server de CSI expone el puerto 8083 a toda la intranet, y no hay
nginx delante. Cualquiera que conociera la IP podía pegarle a `/ia/ine` — y
cada llamada a Document AI cuesta dinero. Por eso nada de negocio queda
abierto.

1. **La llave de servicio** (`exigir_llave`): UNA, compartida con el front (su
   capa server, nunca el navegador), en el `.env`. Abre todo. Es autenticación
   de servicio a servicio, no de usuarios.
2. **Las API Keys de cliente** (`exigir_llave_o_cliente`, desde el
   2026-09-30): muchas, las emite el módulo "API Key" del front para que un
   cliente final mande documentos. Abren SOLO `POST /bandeja/`, y el tenant sale
   de la llave. Formato y verificación en `seguridad_llaves.py`; dónde viven,
   en `servicios/llaves_cliente.py`.

Qué se protege y qué no:
- Todo lo de negocio: protegido. Solo `POST /bandeja/` acepta llaves de cliente.
- `/health` y `/health/db`: abiertos. Son diagnósticos, el deploy y pm2 los
  usan sin credenciales, y no revelan más que el estado de la infraestructura.
"""

import logging
import secrets
from dataclasses import dataclass
from typing import Literal

from fastapi import HTTPException, Request, Security, status
from fastapi.security import APIKeyHeader

import seguridad_llaves
from config import NEXUS_API_KEY
from servicios import llaves_cliente
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

# La llave se declara como ESQUEMA DE SEGURIDAD en vez de leer el header a mano.
# Para quien llama no cambia nada —el mismo header `X-API-Key`—, pero así
# Swagger (`/docs`) sabe que existe: muestra el botón "Authorize", marca con
# candado los endpoints protegidos y manda la llave en cada "Try it out".
# Leyéndola a mano, Swagger no se enteraba y toda prueba desde ahí daba 401.
#
# `auto_error=False` a propósito: con `True`, FastAPI contestaría él mismo un
# 403 "Not authenticated" cuando falta el header, en vez del 401 de abajo, que
# dice qué header poner. Los mensajes y códigos se quedan como estaban.
_esquema_llave = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
    description=(
        "Una API Key de cliente (`nxdoc_live_…`, emitida en el módulo API Key del "
        "front), que solo abre `POST /bandeja/`; o la llave de servicio del front "
        "(`NEXUS_API_KEY` en el .env del server), que abre todo."
    ),
)

_NO_AUTORIZADO = "Falta la llave de API o no es la correcta (header X-API-Key)."


def _comprobar_llave_de_servicio(recibida: str) -> None:
    """La llave compartida del front. Sin llave configurada, la API FALLA
    CERRADA (503) en vez de quedar abierta: un deploy al que se le olvidó la
    variable debe romperse de forma visible y explicarse solo, no convertirse
    en un endpoint público por accidente."""
    if not NEXUS_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "La API no tiene NEXUS_API_KEY configurada. Agrégala al .env "
                "de nexus_back (la misma que usa el front) y reinicia: "
                "pm2 restart nexus-back-api --update-env"
            ),
        )
    # compare_digest y no `==`: compara en tiempo constante, así el tiempo de
    # respuesta no filtra cuántos caracteres del intento iban bien.
    if not secrets.compare_digest(recibida, NEXUS_API_KEY):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_NO_AUTORIZADO)


async def exigir_llave(llave: str | None = Security(_esquema_llave)) -> None:
    """Dependencia de FastAPI: SOLO la llave de servicio del front.

    Es la de todo lo que cuesta dinero o administra algo (`/ia`, `/procesadores`,
    `/archivos`, `/llaves`, listar y retirar de la bandeja). Una llave de
    cliente aquí es un 401, aunque sea válida: un cliente solo puede subir.
    """
    _comprobar_llave_de_servicio(llave or "")


@dataclass(frozen=True)
class Identidad:
    """Quién hizo la petición.

    `servicio` es el front (su llave compartida); dice él con qué tenant opera.
    `cliente` es una API Key de cliente, y su tenant sale de la LLAVE, no de lo
    que la petición afirme — eso es lo que impide que un cliente escriba en el
    prefijo de otro cambiando un campo del formulario.
    """

    tipo: Literal["servicio", "cliente"]
    tenant: str | None = None
    llave_id: str | None = None


@dataclass(frozen=True)
class Analisis:
    """Lo que se sabe de la llave que trae una petición, resuelto UNA vez.

    Existe porque la misma pregunta se hace en dos lugares: el middleware que
    corta antes de leer el cuerpo (`app.py`) y la dependencia del endpoint. Sin
    esto, cada petición leería el registro de llaves dos veces.

    `conocida` es la pieza que no se puede deducir del rechazo: dice si la
    llave EXISTE en el registro, aunque esté revocada o vencida. El registro de
    uso la necesita para no anotar identificadores inventados — ver
    `servicios/uso_llaves.py`.
    """

    identidad: Identidad | None
    #: El 401 o 503 que hay que contestar, ya armado; `None` si la llave sirve.
    rechazo: HTTPException | None
    llave_id: str | None = None
    conocida: bool = False


def analizar_llave(recibida: str) -> Analisis:
    """Resuelve la llave sin contestar todavía: la llave de servicio, una API
    Key de cliente, o el rechazo que corresponde.

    No levanta: devuelve el rechazo dentro del `Analisis`, para que quien
    llame decida CUÁNDO contestarlo. El middleware lo contesta antes de leer el
    cuerpo; la dependencia lo levanta como siempre.

    Todos los rechazos dicen lo mismo —401 con el mensaje genérico—: decir
    "existe pero está revocada" le confirmaría a quien anda probando llaves
    que acertó una. El motivo real va al log, con el identificador público
    (que no es secreto) y nunca con la llave.
    """
    if seguridad_llaves.partes_de(recibida) is None:
        # No tiene forma de llave de cliente: es la del servicio, o nada.
        try:
            _comprobar_llave_de_servicio(recibida)
        except HTTPException as exc:
            return Analisis(identidad=None, rechazo=exc)
        return Analisis(identidad=Identidad("servicio"), rechazo=None)

    identificador = seguridad_llaves.id_de(recibida)
    try:
        fila = llaves_cliente.buscar_por_id(identificador) if identificador else None
    except ErrorAlmacen:
        logger.exception("No se pudo leer el registro de llaves")
        return Analisis(
            identidad=None,
            llave_id=identificador,
            rechazo=HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="No se pudo verificar la llave en este momento. Intenta de nuevo en unos minutos.",
            ),
        )
    # La fila se busca UNA vez y se le pasa a `verificar` ya resuelta, para no
    # leer el registro dos veces por petición.
    if fila is None or not seguridad_llaves.verificar(recibida, lambda _id: fila):
        logger.info("Llave de cliente rechazada (id=%s)", identificador)
        return Analisis(
            identidad=None,
            llave_id=identificador,
            # Que la fila exista es lo que separa una llave revocada o vencida
            # —uso real de un cliente, que sus métricas deben contar— de un
            # identificador inventado, que no debe dejar rastro.
            conocida=fila is not None,
            rechazo=HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_NO_AUTORIZADO),
        )
    return Analisis(
        identidad=Identidad("cliente", tenant=fila["tenant"], llave_id=identificador),
        rechazo=None,
        llave_id=identificador,
        conocida=True,
    )


def analisis_de(request: Request, recibida: str) -> Analisis:
    """El análisis de esta petición, calculado una sola vez y guardado en
    `request.state`. Lo comparten el middleware de `app.py` y la dependencia."""
    guardado = getattr(request.state, "analisis_de_llave", None)
    if isinstance(guardado, Analisis):
        return guardado
    analisis = analizar_llave(recibida)
    request.state.analisis_de_llave = analisis
    return analisis


def exigir_llave_o_cliente(request: Request, llave: str | None = Security(_esquema_llave)) -> Identidad:
    """Dependencia de FastAPI: la llave de servicio O una API Key de cliente.

    Es `def` y no `async def` a propósito: verificar una llave de cliente lee
    el registro en el NAS, que es I/O síncrono y debe ir al threadpool.

    Normalmente el trabajo ya está hecho: el middleware de `app.py` resolvió la
    llave antes de leer el cuerpo y dejó el resultado en `request.state`. Si no
    (una petición sin cuerpo), se resuelve aquí.
    """
    analisis = analisis_de(request, llave or "")
    if analisis.rechazo is not None:
        raise analisis.rechazo
    if analisis.identidad is None:  # defensivo: no debería pasar
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_NO_AUTORIZADO)
    return analisis.identidad
