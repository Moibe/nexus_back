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
    avisos_de_produccion,
)
from logging_config import configurar_logging
from servicios import almacen as almacen_servicio

# Antes de importar los routers, para que cualquier log que ocurra durante los
# imports (por ejemplo el aviso de que falta el driver ODBC) ya salga con formato.
configurar_logging()
logger = logging.getLogger(__name__)

# Variables que se quedaron en un default que no sirve fuera de `local` — ver
# `config.avisos_de_produccion`. Se evalúa UNA vez, aquí, porque el .env solo se
# lee al arrancar: revisarlo por request sería gastar en algo que no cambia.
#
# Se loguea en el momento correcto (ya con formato y timestamp) y el CONTEO se
# publica en /health. El detalle NO: este puerto lo alcanza cualquiera en la red
# de CSI, y una lista de "qué está mal configurado" es justamente lo que no
# conviene regalar. El conteo basta para saber que hay que ir al log.
AVISOS_CONFIGURACION = avisos_de_produccion()
for _aviso in AVISOS_CONFIGURACION:
    logger.warning("Configuración de producción — %s", _aviso)

# El orden de esta lista define el orden de los grupos en Swagger, y cada
# entrada le pone descripción al encabezado del grupo.
TAGS = [
    {"name": "Utilidad", "description": "Health checks y diagnóstico."},
    {
        "name": "Bandeja",
        "description": (
            "**Para clientes.** Mandar documentos a la bandeja de preparación. "
            "Subir acepta una API Key de cliente (`nxdoc_live_…`); listar y "
            "retirar son solo del front."
        ),
    },
    {
        "name": "Llaves",
        "description": "Emitir, listar y revocar API Keys de cliente. Solo el front.",
    },
    {
        "name": "Tipos documentales",
        "description": "El catálogo de tipos documentales de cada organización. Solo el front.",
    },
    {
        "name": "Documentos",
        "description": "Ingesta, bandeja de preparación y pipeline documental.",
    },
    {
        "name": "IA",
        "description": "Endpoints que delegan en Document AI (OCR, extracción, clasificación).",
    },
]

# La documentación automática se apaga (`docs_url`, `redoc_url`, `openapi_url` en
# None) porque la sirve `documentacion.py` en dos versiones: `/docs`, la PÚBLICA,
# solo con lo que un cliente puede usar, y `/docs-interno` con todo. Ver su
# docstring.
app = FastAPI(
    title="NexusDoc AI · API interna",
    description="Ingesta, procesamiento y configuración documental para NexusDoc AI.",
    version="0.0.1",
    openapi_tags=TAGS,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

def _trae_cuerpo(request: Request) -> bool:
    """¿Esta petición viene con cuerpo? Es lo que decide si vale la pena
    cortar antes de leerlo."""
    if "chunked" in request.headers.get("transfer-encoding", "").lower():
        return True
    try:
        return int(request.headers.get("content-length") or 0) > 0
    except ValueError:
        return True  # ilegible: trátala como si trajera cuerpo


@app.middleware("http")
async def autenticar_y_medir_las_llaves(request: Request, call_next):
    """Resuelve la llave ANTES de leer el cuerpo, aplica el tope semanal y
    registra el uso de las llaves de cliente.

    Son tres cosas en un mismo lugar porque dependen del mismo dato —quién
    manda esta petición— y resolverlo cuesta una lectura del registro de
    llaves: se hace UNA vez y se comparte con la dependencia del endpoint por
    `request.state` (ver `seguridad.analisis_de`).

    ## Por qué corta aquí y no en la dependencia del endpoint (2026-10-01)

    FastAPI parsea el `multipart` ANTES de resolver la dependencia que exige la
    llave, así que una petición sin llave válida se subía ENTERA —hasta el tope
    de 20 MiB— y recién entonces recibía su 401. Medido: 19 MB a 4 MB/s
    tardaban 4.5 s con todos los bytes enviados. Daba igual mientras el puerto
    8083 solo se alcanzara desde la intranet; desde el 2026-10-01 la API está
    publicada en internet por `biblioratum-api.buzzword.com.mx`, y eso convierte
    el detalle en un desperdicio de red y disco que cualquiera puede provocar.
    Ahora el 401 sale sin leer un solo byte (el mismo corte temprano que ya
    hacía el 413 de `limitar_tamano_subida`: medido, 5 ms y 0 bytes subidos).

    SOLO corta cuando la petición TRAE CUERPO. Una petición sin cuerpo sigue su
    camino y la contesta el endpoint como siempre: así `/health` y `/docs`
    —abiertos a propósito— no empiezan a pedir llave porque alguien mande un
    header raro. Esto se apoya en que NINGUNA ruta abierta recibe cuerpo, y lo
    comprueba `verificar_llaves_cliente.py` recorriendo el OpenAPI: si algún
    día se agrega una ruta pública con cuerpo, esa prueba falla y hay que
    revisar esta regla.

    ## Qué uso se anota, y qué no

    Se anota CADA llamada de una llave que EXISTE, aceptada o rechazada: una
    métrica de errores que no cuenta los rechazos no sirve, y a quien tiene una
    llave revocada que sigue llamando hay que poder mostrárselo.

    Lo que ya NO se anota es un identificador que no existe. Antes se anotaba
    cualquier cosa con forma de llave, así que una inundación de llaves
    inventadas hacía crecer sin tope el índice en memoria y el archivo del NAS
    —dos recursos del servidor— sin que hubiera nunca un cliente detrás.
    """
    import time

    from starlette.concurrency import run_in_threadpool

    import seguridad
    import seguridad_llaves
    from servicios import uso_llaves

    llave = request.headers.get("x-api-key", "")
    trae_cuerpo = _trae_cuerpo(request)
    # Sin cuerpo que cortar y sin llave de cliente que medir, no hay nada que
    # hacer aquí: ni se lee el registro.
    if not trae_cuerpo and seguridad_llaves.partes_de(llave) is None:
        return await call_next(request)

    try:
        analisis = await run_in_threadpool(seguridad.analisis_de, request, llave)
    except Exception:  # noqa: BLE001
        # Un fallo inesperado al resolver la llave no puede tumbar la petición
        # desde aquí: sigue su curso y la dependencia del endpoint decide.
        logger.exception("No se pudo resolver la llave en el middleware")
        return await call_next(request)

    inicio = time.perf_counter()
    respuesta = None
    if analisis.rechazo is not None and trae_cuerpo:
        # El 401 (o el 503 si el registro no se pudo leer), sin leer el cuerpo.
        respuesta = JSONResponse(
            status_code=analisis.rechazo.status_code, content={"detail": analisis.rechazo.detail}
        )
    elif analisis.identidad is not None and analisis.identidad.tipo == "cliente":
        # El tope se consulta en el threadpool: lee el registro del NAS. Si el
        # registro no se puede leer, la llamada PASA — el tope es una cortesía
        # de cuota, no una defensa, y no debe tumbar el servicio.
        try:
            excedida = await run_in_threadpool(uso_llaves.excede_limite, analisis.llave_id)
        except Exception:  # noqa: BLE001
            excedida = False
        if excedida:
            respuesta = JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "detail": (
                        f"Esta API Key alcanzó su límite semanal de {uso_llaves.LIMITE_SEMANAL:,} "
                        "solicitudes. Se reinicia el lunes."
                    )
                },
            )
    if respuesta is None:
        respuesta = await call_next(request)

    if analisis.llave_id and analisis.conocida:
        ms = int((time.perf_counter() - inicio) * 1000)
        try:
            # Lo que la petición DIJO traer: si se cortó antes, esos bytes no
            # se leyeron, pero es el tamaño que el cliente intentó mandar.
            bytes_ = int(request.headers.get("content-length") or 0)
        except ValueError:
            bytes_ = 0
        await run_in_threadpool(
            uso_llaves.registrar, analisis.llave_id, request.url.path, request.method, respuesta.status_code, ms, bytes_
        )
    return respuesta


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


@app.middleware("http")
async def solo_lo_publico_por_el_dominio_publico(request: Request, call_next):
    """Si la petición llegó por el dominio público, solo pasa lo de la
    documentación pública: mandar un documento. Lo demás responde 404, incluso
    con la llave de servicio. Ver superficie_publica.py para el porqué.

    Va fuera de los demás middlewares (se define después de ellos) para que lo
    rechazado ni se cuente como uso de una llave ni se procese.
    """
    import superficie_publica

    if superficie_publica.por_nombre_publico(request.headers) and not superficie_publica.permitida(
        request.method, request.url.path
    ):
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": "Not Found"})
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
    #
    # `avisos_configuracion` es un CONTEO, no la lista: el detalle dice qué
    # variable está mal puesta y eso no se publica en un puerto que alcanza
    # cualquiera en la red de CSI. Si no es 0, el detalle está en el log:
    #   pm2 logs nexus-back-api --lines 40
    #
    # `almacen_disponible` SÍ habla del NAS: si el centinela `.nexus-almacen` no está
    # (montaje caído, ruta sin configurar), `status` deja de ser "ok". El 21-oct el
    # NAS no montó tras un reinicio, el back dio 503 a todo y este endpoint seguía
    # diciendo "ok". Es una comprobación barata (un `stat`), no escribe nada.
    # El código HTTP se queda en 200 A PROPÓSITO: Nexus Monitor lee `environment` de
    # aquí y descarta cualquier respuesta que no sea 2xx, así que un 503 lo dejaría
    # sin poder decir de qué ambiente habla justo cuando más falta hace.
    almacen = almacen_servicio.raiz_montada()
    return {
        "status": "ok" if almacen else "almacen_no_disponible",
        "environment": ENVIRONMENT,
        "almacen_disponible": almacen,
        "documentos_disponible": DOCUMENTOS_DISPONIBLE,
        "avisos_configuracion": len(AVISOS_CONFIGURACION),
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
    description=(
        "Confirma que la API alcanza SQL Server y devuelve versión y base conectada, y si el ESQUEMA de esa base está al día con el código: "
        "`esquema` dice la última migración aplicada y la que este código espera. Si a la base le faltan migraciones, `status` es "
        "`esquema_desfasado` (el código usaría algo que la base todavía no tiene). Que la base vaya ADELANTE del código es normal: se migra antes de desplegar."
    ),
)
def health_db():
    try:
        # El import va DENTRO del try: si falta el runtime de ODBC truena con
        # ImportError, y este endpoint tiene que reportarlo como estado, no
        # devolver un 500 pelón.
        from db.sqlserver import probar_conexion, version_de_esquema
        from migraciones.corredor import descubrir

        datos = probar_conexion()
        esperado = len(descubrir())
        aplicado = version_de_esquema()
        al_dia = aplicado is not None and aplicado >= esperado
        respuesta = {"status": "ok" if al_dia else "esquema_desfasado", **datos, "esquema": {"aplicado": aplicado, "esperado": esperado, "al_dia": al_dia}}
        if not al_dia:
            respuesta["pista"] = (
                f"A la base le faltan migraciones: tiene {aplicado if aplicado is not None else 'ninguna anotada'} y este código espera la {esperado}. "
                "Se aplican con `python -m migraciones aplicar --base <esta base> --confirmar` (o con el deploy)."
            )
        return respuesta
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

# Los tipos documentales de cada organización (la biblioteca de Configuración): antes vivían en el localStorage de cada
# navegador. Como llaves y webhooks, piden la llave de servicio Y el JWT de la persona (HU11).
from routers.tipos_documentales import router as tipos_documentales_router  # noqa: E402

app.include_router(
    tipos_documentales_router,
    prefix="/tipos-documentales",
    tags=["Tipos documentales"],
    dependencies=[Depends(exigir_llave)],
)

# Los procesos de cada organización (el motor de reglas: tipos documentales como entradas y operadores como reglas). Aquí solo se
# GUARDA el dibujo del lienzo; no hay motor que los corra todavía. Como los tipos documentales: llave de servicio Y JWT (HU11).
from routers.procesos import router as procesos_router  # noqa: E402

app.include_router(
    procesos_router,
    prefix="/procesos",
    tags=["Procesos"],
    dependencies=[Depends(exigir_llave)],
)

# Los tipos de expediente VIRTUAL de cada organización («Solicitud de crédito»): la clase de la que salen los expedientes virtuales (desde el
# 2026-10-29 el expediente no tiene tipo; la ruta conservó su nombre).
from routers.tipos_expediente import router as tipos_expediente_router  # noqa: E402

app.include_router(
    tipos_expediente_router,
    prefix="/tipos-expediente",
    tags=["Tipos de expediente"],
    dependencies=[Depends(exigir_llave)],
)

# Los expedientes de cada organización: conjuntos de documentos. Un documento puede estar en ninguno (el void), en uno o en varios. Los
# documentos nacen al entrar por la bandeja (`servicios/bandeja.py`); aquí se organizan. Como procesos: llave de servicio Y JWT (HU11).
from routers.expedientes import router as expedientes_router  # noqa: E402

app.include_router(
    expedientes_router,
    prefix="/expedientes",
    tags=["Expedientes"],
    dependencies=[Depends(exigir_llave)],
)

# Administradores de plataforma: crearlos, verlos, regenerarles la contraseña y retirarlos. Lo usa Nexus Monitor (la herramienta del
# operador); antes eran tres scripts en una terminal del servidor. ⚠️ Piden SOLO la llave de servicio: sin llave de administración
# ni JWT (decisión del usuario, 2026-10-20). Ver el docstring de `routers/admins_plataforma.py`.
from routers.admins_plataforma import router as admins_plataforma_router  # noqa: E402

app.include_router(
    admins_plataforma_router,
    prefix="/admins-plataforma",
    tags=["Administradores de plataforma"],
    dependencies=[Depends(exigir_llave)],
)

# Archivos SÍ se registra siempre, a diferencia de Documentos: no depende de la
# base, solo del almacén. Si `ALMACEN_RUTA` está vacía el endpoint responde un
# 503 que NOMBRA la variable que falta — más útil que esconder la ruta y dejar
# a quien la busca preguntándose si existe.
from routers.archivos import router as archivos_router  # noqa: E402

app.include_router(archivos_router, prefix="/archivos", dependencies=[Depends(exigir_llave)])

# La bandeja de entrada: por donde un cliente manda archivos a la bandeja de
# preparación del front. Mismo criterio que Archivos (solo depende del
# almacén). Es el ÚNICO router que acepta API Keys de cliente, y solo para
# subir: el router exige ALGUNA llave válida y las rutas de administración
# (listar, retirar) agregan `exigir_llave` encima — ver `routers/bandeja.py`.
from routers.bandeja import router as bandeja_router  # noqa: E402
from seguridad import exigir_llave_o_cliente  # noqa: E402

app.include_router(
    bandeja_router, prefix="/bandeja", dependencies=[Depends(exigir_llave_o_cliente)]
)

# Emitir, listar y revocar API Keys de cliente. Solo el front, con su llave.
from routers.llaves import router as llaves_router  # noqa: E402

app.include_router(llaves_router, prefix="/llaves", dependencies=[Depends(exigir_llave)])

# Registrar, listar, activar/desactivar y eliminar webhooks de cliente. Solo el
# front, con su llave. Todavía no se envía ningún aviso.
from routers.webhooks import router as webhooks_router  # noqa: E402

app.include_router(webhooks_router, prefix="/webhooks", dependencies=[Depends(exigir_llave)])

# Acceso (Sprint 1): login, refresh, logout, cambio de contraseña. Lo llama SOLO
# la capa server del front, con la llave de servicio; el navegador recibe las
# cookies del front. Queda fuera del dominio público como todo lo que no es
# /bandeja (ver HOSTS_PUBLICOS).
from routers.auth import router as auth_router  # noqa: E402

app.include_router(auth_router, prefix="/auth", tags=["Acceso"], dependencies=[Depends(exigir_llave)])

# Organizaciones (Sprint 1, HU02). Además de la llave de servicio, cada
# endpoint exige que el Bearer sea de un ADMINISTRADOR DE PLATAFORMA: la llave
# la tiene el front entero, así que sola no distingue quién pide.
from routers.organizaciones import router as organizaciones_router  # noqa: E402

app.include_router(
    organizaciones_router, prefix="/organizaciones", tags=["Organizaciones"], dependencies=[Depends(exigir_llave)]
)

# Usuarios dentro de una organización (Sprint 1, HU06 y HU07). El permiso lo
# da la membresía con rol ADMIN, no la llave: cada administrador opera sobre
# SU organización, la del JWT.
from routers.usuarios import router as usuarios_router  # noqa: E402

app.include_router(
    usuarios_router, prefix="/usuarios", tags=["Usuarios"], dependencies=[Depends(exigir_llave)]
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

    # Esquema (inventario de tablas) va DENTRO de este mismo bloque y no
    # arriba con los demás routers, porque arrastra pyodbc por la misma cadena
    # que documentos: routers.esquema → repositorios.esquema → db.sqlserver.
    # Registrarlo afuera reintroduciría justo el fallo que el try/except de
    # aquí evita — un SO sin runtime de ODBC tumbaría la API COMPLETA — y
    # además publicaría en Swagger un endpoint que, sin SQLSERVER_HOST, solo
    # sabe responder error.
    #
    # Lo consume Nexus Monitor (su pantalla de Tablas). Es de solo lectura y
    # no devuelve datos de negocio: nombres de tabla y conteos, nada más.
    try:
        from routers.esquema import router as esquema_router  # noqa: E402
    except ImportError as exc:
        _raiz = (exc.name or "").split(".")[0]
        if _raiz != "pyodbc" and "odbc" not in str(exc).lower():
            raise
        logger.warning(
            "No se pudo importar el router de esquema: falta el runtime de "
            "ODBC en este SO (%s). La API arranca SIN /esquema/*.",
            exc,
        )
    else:
        app.include_router(
            esquema_router,
            prefix="/esquema",
            tags=["Esquema"],
            dependencies=[Depends(exigir_llave)],
        )


# Al final, con todos los routers ya incluidos: la documentación pública se
# deriva del esquema completo.
import documentacion  # noqa: E402

documentacion.instalar(app)


@app.on_event("startup")
def calentar_uso_de_llaves() -> None:
    """Carga el índice de uso de las llaves ANTES de atender la primera
    petición. Si se calentara en la primera llamada, esa llamada pagaría la
    lectura del registro completo (segundos, con cientos de miles de líneas).
    Si el almacén no está, se deja para la siguiente llamada; no se tumba el
    arranque por esto."""
    from servicios import uso_llaves

    try:
        uso_llaves.calentar()
    except Exception:  # noqa: BLE001
        logger.warning("No se pudo cargar el índice de uso de llaves al arrancar; se intentará al usarse.")


@app.on_event("startup")
def importar_registros_json() -> None:
    """Migraciones `0006` y `0008`: importa UNA vez a SQL lo que traían sus JSONL —las organizaciones, los clasificadores, la bandeja y la
    auditoría; los enlaces de un solo uso, las llaves de cliente, su uso, los webhooks y sus entregas— (`servicios/importacion_json.py`). Se hace
    al arrancar para que ninguna petición escriba en una tabla con llave foránea a organizaciones antes de que existan; si falla (la base o el NAS
    no responden), no tumba la app: se reintenta al usar cada uno."""
    if not SQLSERVER_HOST:
        return
    try:
        from servicios import importacion_json

        importacion_json.asegurar_todo()
    except Exception:  # noqa: BLE001
        logger.warning("No se pudieron importar los registros JSON al arrancar; se intentará al usarse.", exc_info=True)


@app.on_event("startup")
def arrancar_entregas_de_webhooks() -> None:
    """El trabajador que entrega los avisos de webhook y los reintenta (ver
    `servicios/entregas_webhooks.py`). Reconstruye lo pendiente del registro:
    un reinicio no pierde avisos, a lo sumo los retrasa."""
    from servicios import entregas_webhooks

    entregas_webhooks.iniciar()


@app.on_event("shutdown")
def detener_entregas_de_webhooks() -> None:
    from servicios import entregas_webhooks

    entregas_webhooks.detener()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
