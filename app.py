"""NexusDoc AI — API.

Backend único del proyecto: es el dueño exclusivo de SQL Server (el front nunca
toca la base) y además orquesta las llamadas a los servicios de IA.

Arranque local:  .venv/Scripts/python.exe -m uvicorn app:app --reload --port 8083
"""

import logging

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import (
    CORS_ALLOWED_ORIGINS,
    ENVIRONMENT,
    MAX_SUBIDA_BYTES,
    MAX_SUBIDA_MB,
    PORT,
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
    # `documentos_disponible` en False significa que el router de documentos no
    # cargó por falta del runtime de ODBC. Se expone aquí porque el deploy.sh de
    # webhook-central no hace healthcheck: es la forma de verlo sin entrar a leer
    # los logs de pm2.
    return {
        "status": "ok",
        "environment": ENVIRONMENT,
        "documentos_disponible": DOCUMENTOS_DISPONIBLE,
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
    except Exception as exc:  # noqa: BLE001
        # El texto de una excepción de pyodbc trae host, driver y usuario. En el
        # server de CSI cualquier miembro de la empresa alcanza este puerto por
        # IP, así que el detalle se va al log y al cliente solo el tipo de error.
        logger.exception("Falló el health check de SQL Server")
        return {"status": "error", "error": type(exc).__name__}


# ── Registro de routers ───────────────────────────────────────────────────────

from routers.ia import router as ia_router  # noqa: E402

app.include_router(ia_router, prefix="/ia")

# El router de documentos arrastra pyodbc de forma TRANSITIVA:
#   routers.documentos → repositorios.documentos → db.sqlserver → import pyodbc
#
# Si el runtime de unixODBC no está instalado en el SO, ese import truena y sin
# este try/except se cae la app COMPLETA: ni /health ni /ia/ine responderían y
# pm2 entraría en ciclo de restart — mientras el deploy.sh de webhook-central
# reporta el despliegue como exitoso, porque no hace healthcheck.
#
# Con esto la API arranca sin el grupo Documentos y todo lo demás sigue vivo.
#
# El except NO atrapa cualquier ImportError: un typo en un import de la cadena
# (`from repositorios import documentoss`) también lanza ImportError, y ese sí es
# un bug de verdad que debe tumbar el arranque en lugar de quedar enterrado en un
# WARNING. Solo se perdona el fallo que viene de pyodbc.
DOCUMENTOS_DISPONIBLE = False
try:
    from routers.documentos import router as documentos_router  # noqa: E402
except ImportError as exc:
    # `exc.name` es 'pyodbc' cuando falta el paquete; cuando el paquete está pero
    # no carga su librería nativa el mensaje trae "libodbc.so...". Se revisan los
    # dos, y cualquier otra cosa se re-lanza.
    _raiz = (exc.name or "").split(".")[0]
    if _raiz != "pyodbc" and "odbc" not in str(exc).lower():
        raise
    logger.warning(
        "No se pudo importar el router de documentos: falta el runtime de ODBC "
        "en este SO (%s). La API arranca SIN /documentos/*; /health y /ia/* "
        "siguen disponibles.",
        exc,
    )
else:
    app.include_router(documentos_router, prefix="/documentos")
    DOCUMENTOS_DISPONIBLE = True


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
