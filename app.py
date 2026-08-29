"""NexusDoc AI — API.

Backend único del proyecto: es el dueño exclusivo de SQL Server (el front nunca
toca la base) y además orquesta las llamadas a los servicios de IA.

Arranque local:  .venv/Scripts/python.exe -m uvicorn app:app --reload --port 8083
"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from errores import ConfiguracionIncompleta
from config import (
    CORS_ALLOWED_ORIGINS,
    ENVIRONMENT,
    MAX_SUBIDA_BYTES,
    MAX_SUBIDA_MB,
    PORT,
    SQLSERVER_HOST,
)
from logging_config import configurar_logging

# Antes de importar los routers, para que cualquier log que ocurra durante los
# imports (por ejemplo el aviso de que falta el driver ODBC) ya salga con formato.
configurar_logging()
logger = logging.getLogger(__name__)

# El orden de esta lista define el orden de los grupos en Swagger, y cada
# entrada le pone descripción al encabezado del grupo.
TAGS = [
    {"name": "Utilidad", "description": "Health checks y diagnóstico."},
    {
        "name": "Documentos",
        "description": "Ingesta, bandeja de preparación y pipeline documental.",
    },
    {
        "name": "IA",
        "description": "Endpoints que delegan en Document AI (OCR, extracción, clasificación).",
    },
]

app = FastAPI(
    title="NexusDoc AI · API",
    description="Ingesta, procesamiento y configuración documental para NexusDoc AI.",
    version="0.0.1",
    openapi_tags=TAGS,
)

@app.middleware("http")
async def limitar_tamano_subida(request: Request, call_next):
    """Rechaza cuerpos demasiado grandes ANTES de que Starlette parsee el
    multipart.

    Sin esto, Starlette vuelca el archivo completo a un temporal en disco y luego
    el endpoint lo carga a memoria y lo pasa a base64 (~1.33x) — una subida
    grande basta para tumbar el worker, y pm2 lo reinicia perdiendo las requests
    en vuelo. En el server de CSI no hay nginx delante, así que este es el único
    lugar donde se puede poner el tope.

    Se valida `Content-Length` porque es lo que mandan curl y los navegadores en
    un multipart. Una subida con `Transfer-Encoding: chunked` no trae ese header
    y se cuela por aquí — para ese caso el router de IA tiene su propio tope
    (routers/ia.py), que sí mide el contenido real.
    """
    largo = request.headers.get("content-length")
    if largo is not None:
        try:
            if int(largo) > MAX_SUBIDA_BYTES:
                return JSONResponse(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    content={"detail": f"El archivo excede el límite de {MAX_SUBIDA_MB:g} MB."},
                )
        except ValueError:
            # Content-Length no numérico: lo deja pasar y que falle el parseo
            # normal de Starlette, que da un error más preciso que uno inventado aquí.
            pass
    return await call_next(request)


# CORS se registra AL FINAL a propósito. Starlette mete cada middleware en el
# índice 0, así que el último registrado queda como el MÁS EXTERNO: de este modo
# el 413 que devuelve `limitar_tamano_subida` sí pasa por CORS y sale con sus
# cabeceras. Al revés, un navegador vería un error de CORS opaco en lugar del 413.
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=False,  # el front llama desde su capa server, no manda cookies
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.get(
    "/health",
    tags=["Utilidad"],
    summary="Health Check",
    description="Verifica que el servidor esté en línea. No toca la base.",
)
def health():
    # `documentos_disponible` en True significa que hay SQLSERVER_HOST configurado
    # y que el router cargó. NO significa que la base responda — para eso está
    # /health/db. Se expone aquí porque el deploy.sh de webhook-central no hace
    # healthcheck, así que es la forma de ver el estado con un solo curl.
    return {
        "status": "ok",
        "environment": ENVIRONMENT,
        "documentos_disponible": DOCUMENTOS_DISPONIBLE,
    }


# Qué significa cada SQLSTATE que devuelve pyodbc cuando falla la conexión. El
# CÓDIGO es seguro de publicar (no trae host, usuario ni contraseña) y es la
# diferencia entre "no sé qué pasó" y saber exactamente cuál de los tres
# eslabones se rompió: driver, red o credenciales.
_PISTAS_SQLSTATE = {
    "HYT00": (
        "Se agotó el tiempo de espera al conectar. El driver está bien y el "
        "intento salió: no hubo respuesta del otro lado. Casi siempre es "
        "firewall o ruteo hacia el host/puerto de SQL Server, no credenciales."
    ),
    "HYT01": "Se agotó el tiempo de espera de la conexión ya establecida.",
    "IM002": (
        "El driver ODBC no está instalado o su nombre no coincide con "
        "SQLSERVER_DRIVER. En el server: `odbcinst -q -d` lista los instalados."
    ),
    "IM003": "El driver está registrado pero no se pudo cargar (biblioteca faltante).",
    "08001": (
        "No se alcanzó el servidor: host/puerto equivocados, o el firewall no "
        "deja pasar. El driver SÍ está bien si llegaste a este error."
    ),
    "08S01": "La conexión se cayó a medio camino (red inestable o TLS rechazado).",
    "28000": "Login rechazado: usuario o contraseña incorrectos.",
    # 42000 está SOBRECARGADO: lo usa tanto un problema de permisos como un
    # RAISERROR/THROW dentro de un stored procedure. Comprobado el 2026-08-26
    # con `[security].[uspCreateTenant]`, que devolvió 42000 con el mensaje
    # "The tenant sequence is not configured. (50006)" — o sea el SP corrió
    # bien y falló su propia validación. La primera versión de esta pista solo
    # decía "no hay permiso", que habría mandado a buscar el problema al lugar
    # equivocado justo cuando el SP estaba funcionando.
    "42000": (
        "Dos causas posibles. (a) El login no tiene permiso sobre la base o el "
        "objeto. (b) Un stored procedure se ejecutó y lanzó su propio error con "
        "RAISERROR/THROW — si el mensaje trae un número >= 50000, es este caso: "
        "el permiso está bien y lo que falló es una validación de negocio."
    ),
}


@app.get(
    "/health/db",
    tags=["Utilidad"],
    summary="Health Check de SQL Server",
    description="Confirma que la API alcanza SQL Server y devuelve versión y base conectada.",
)
def health_db():
    try:
        # El import va DENTRO del try: si falta el runtime de ODBC truena con
        # ImportError, y este endpoint tiene que reportarlo como estado, no
        # devolver un 500 pelón.
        from db.sqlserver import probar_conexion

        return {"status": "ok", **probar_conexion()}
    except ConfiguracionIncompleta as exc:
        # Este mensaje lo escribimos nosotros y solo nombra variables que faltan
        # — sin valores. Se puede mostrar tal cual, y es lo que hace la
        # diferencia entre "RuntimeError" y saber qué línea agregarle al .env.
        return {"status": "sin_configurar", "detalle": str(exc)}
    except Exception as exc:  # noqa: BLE001
        # El texto de una excepción de pyodbc trae host, driver y usuario. En el
        # server de CSI cualquier miembro de la empresa alcanza este puerto por
        # IP, así que el detalle se va al log y al cliente solo el tipo de error
        # más el SQLSTATE, que es diagnóstico pero no confidencial.
        logger.exception("Falló el health check de SQL Server")
        respuesta = {"status": "error", "error": type(exc).__name__}

        # El SQLSTATE se publica SIEMPRE que exista, esté o no en la tabla de
        # pistas. La primera versión de esto solo lo mostraba si lo tenía
        # mapeado, y el resultado fue una respuesta sin ninguna información
        # útil justo en el caso más probable (HYT00, que no estaba en la
        # tabla) — o sea el diagnóstico fallaba precisamente cuando más se
        # necesitaba. El código en sí no es confidencial: son cinco caracteres
        # del estándar ODBC, sin host, usuario ni contraseña.
        codigo = exc.args[0] if exc.args and isinstance(exc.args[0], str) else None
        if codigo:
            respuesta["sqlstate"] = codigo
            pista = _PISTAS_SQLSTATE.get(codigo)
            respuesta["pista"] = pista or (
                f"SQLSTATE {codigo} sin pista registrada. El detalle completo "
                "está en el log: `pm2 logs nexus-back-api --lines 40`."
            )
        return respuesta


# ── Registro de routers ───────────────────────────────────────────────────────

from fastapi import Depends  # noqa: E402

from routers.ia import router as ia_router  # noqa: E402
from seguridad import exigir_llave  # noqa: E402

# La llave va en el include_router y no dentro de cada endpoint: así un router
# nuevo que se registre aquí decide EXPLÍCITAMENTE si queda protegido, y no
# puede quedar abierto por olvidar un decorador adentro.
app.include_router(ia_router, prefix="/ia", dependencies=[Depends(exigir_llave)])

from routers.procesadores import router as procesadores_router  # noqa: E402

app.include_router(
    procesadores_router, prefix="/procesadores", dependencies=[Depends(exigir_llave)]
)

# El grupo Documentos se registra solo si hay una base configurada.
#
# Mientras el DBA no entregue SQL Server, publicar esos endpoints sería publicar
# un 503: aparecerían en Swagger apuntando a una base que no existe, y en un
# server que alcanza cualquier miembro de la empresa por IP. Con esto, Swagger
# muestra únicamente lo que de verdad funciona, y `documentos_disponible` de
# /health significa algo útil en lugar de ser un falso positivo.
#
# Efecto secundario deseable: sin SQLSERVER_HOST ni se importa pyodbc, porque el
# import vive en la cadena del router.
DOCUMENTOS_DISPONIBLE = False

if not SQLSERVER_HOST:
    logger.warning(
        "SQLSERVER_HOST está vacío: la API arranca SIN el grupo /documentos/*. "
        "Es lo esperado mientras el DBA no entregue la base; /health y /ia/* "
        "funcionan normal."
    )
else:
    # El router arrastra pyodbc de forma TRANSITIVA:
    #   routers.documentos → repositorios.documentos → db.sqlserver → import pyodbc
    #
    # Si el runtime de unixODBC no está en el SO, ese import truena y sin este
    # try/except se caería la app COMPLETA: ni /health ni /ia/ine responderían y
    # pm2 entraría en ciclo de restart, mientras el deploy.sh de webhook-central
    # reporta el despliegue como exitoso porque no hace healthcheck.
    #
    # El except NO atrapa cualquier ImportError: un typo en un import de la cadena
    # (`from repositorios import documentoss`) también lanza ImportError, y ese sí
    # es un bug de verdad que debe tumbar el arranque en lugar de quedar enterrado
    # en un WARNING. Solo se perdona el fallo que viene de pyodbc.
    try:
        from routers.documentos import router as documentos_router  # noqa: E402
    except ImportError as exc:
        # `exc.name` es 'pyodbc' cuando falta el paquete; cuando el paquete está
        # pero no carga su librería nativa, el mensaje trae "libodbc.so...". Se
        # revisan los dos, y cualquier otra cosa se re-lanza.
        _raiz = (exc.name or "").split(".")[0]
        if _raiz != "pyodbc" and "odbc" not in str(exc).lower():
            raise
        logger.warning(
            "No se pudo importar el router de documentos: falta el runtime de "
            "ODBC en este SO (%s). La API arranca SIN /documentos/*.",
            exc,
        )
    else:
        app.include_router(
            documentos_router, prefix="/documentos", dependencies=[Depends(exigir_llave)]
        )
        DOCUMENTOS_DISPONIBLE = True


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
