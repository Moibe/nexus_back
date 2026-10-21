"""Configuración central leída del .env.

Sigue el patrón de geospace_nucleo: constantes a nivel de módulo, sin clases ni
pydantic-settings. Se importa como `from config import ALGO`.
"""

import ipaddress
import os

from dotenv import load_dotenv

load_dotenv()


def _numero(nombre: str, default: float) -> float:
    """Lee una variable numérica del entorno tolerando que esté presente pero vacía.

    `os.getenv(x, default)` solo aplica el default cuando la variable NO existe.
    Un renglón `PORT=` en el .env devuelve `''`, y `int('')` truena con un
    ValueError durante el import de este módulo — o sea, antes de que exista la
    app, con un traceback que ni menciona el .env. Ese caso es fácil de provocar
    escribiendo el .env de producción a mano.
    """
    crudo = os.getenv(nombre)
    if crudo is None or not crudo.strip():
        return default
    try:
        return float(crudo)
    except ValueError as exc:
        raise RuntimeError(
            f"{nombre}={crudo!r} en el .env no es un número válido."
        ) from exc


# ── Ambiente ──────────────────────────────────────────────────────────────────
ENVIRONMENT = os.getenv("ENVIRONMENT", "local")
# Ojo: en el server el puerto real lo fija el `--port` de la línea de comandos de
# uvicorn que pm2 guardó; esta variable solo la usa el bloque __main__ de app.py.
PORT = int(_numero("PORT", 8083))

# ── CORS ──────────────────────────────────────────────────────────────────────
# El front (SvelteKit) llama a esta API desde su capa server, no desde el
# navegador, así que en producción CORS casi no importa. Se deja configurable
# por si algún día se consume directo desde el browser.
_origins_env = os.getenv("CORS_ALLOWED_ORIGINS", "http://localhost:3400,http://127.0.0.1:3400")
CORS_ALLOWED_ORIGINS = [o.strip() for o in _origins_env.split(",") if o.strip()]

# ── Dominio público ───────────────────────────────────────────────────────────
# Los nombres con los que esta API se publica hacia clientes: el reverse proxy
# de Soporte TI reenvía por ellos. Lo que llega por uno de estos nombres solo
# puede usar lo de la documentación pública (mandar un documento); ver
# superficie_publica.py. Varios, separados por coma. Una variable presente pero
# VACÍA vale lo mismo que ausente: un renglón en blanco en el .env no debe
# apagar la restricción sin que nadie se entere.
_hosts_env = os.getenv("NEXUS_HOSTS_PUBLICOS", "").strip() or "nexus-doc-api.buzzword.com.mx"
HOSTS_PUBLICOS = frozenset(h.strip().lower() for h in _hosts_env.split(",") if h.strip())

# ── SQL Server ────────────────────────────────────────────────────────────────
# La base la diseña y mantiene el DBA; aquí solo se consumen sus stored
# procedures. No hay ORM ni migraciones de este lado a propósito.
# Llave compartida con el front (ver seguridad.py). Vacía = la API falla
# cerrada en los endpoints protegidos, nunca abierta.
NEXUS_API_KEY = os.getenv("NEXUS_API_KEY", "")

# Llave Fernet con la que se cifran los secrets de firma de los webhooks (ver
# servicios/webhooks_cliente.py). Sin ella el alta de webhooks responde 503: un
# secret que no se sabe cifrar no se guarda. NO se cambia a la ligera: lo que se
# cifró con la anterior ya no se puede descifrar.
WEBHOOKS_CLAVE_CIFRADO = os.getenv("WEBHOOKS_CLAVE_CIFRADO", "").strip()


def _redes(nombre: str, default: str) -> list:
    """Una lista de redes separadas por coma. Vacía o ausente = el default: una
    guarda de seguridad no se apaga por dejar el renglón en blanco."""
    crudo = (os.getenv(nombre) or "").strip() or default
    redes = []
    for parte in crudo.split(","):
        parte = parte.strip()
        if not parte:
            continue
        try:
            redes.append(ipaddress.ip_network(parte, strict=False))
        except ValueError as exc:
            raise RuntimeError(f"{nombre}: {parte!r} no es una red válida (ej. 172.10.0.0/16).") from exc
    return redes


# La guarda contra SSRF de los webhooks (servicios/entrega_webhooks.py) ya
# rechaza toda dirección que no sea pública. Pero la red interna de CSI usa
# 172.10.x.x, que NO es privada según el estándar (las privadas son 172.16 a
# 172.31): para Python es "pública" y pasaría. Por eso se bloquea aquí, de forma
# explícita. Varias, separadas por coma.
WEBHOOKS_REDES_BLOQUEADAS = _redes("WEBHOOKS_REDES_BLOQUEADAS", "172.10.0.0/16")

# SOLO para desarrollo: deja validar webhooks que apuntan a localhost. En el
# server NUNCA: abriría la puerta a que una URL le pegue a los servicios de la
# propia máquina (los hooks de despliegue no piden token).
WEBHOOKS_PERMITIR_LOCAL = (os.getenv("WEBHOOKS_PERMITIR_LOCAL") or "").strip().lower() in {"1", "true", "si", "sí", "yes"}

# Cuánto se espera al endpoint del cliente al validar (y, cuando exista, al
# entregar).
WEBHOOKS_TIMEOUT_S = _numero("WEBHOOKS_TIMEOUT_S", 10)

# Apaga el trabajador que entrega y reintenta los avisos de webhook
# (servicios/entregas_webhooks.py). Es para un entorno que COMPARTE el almacén
# con otro: el trabajador reconstruye sus pendientes leyendo el registro del
# NAS, y con el registro compartido reintentaría las entregas del otro entorno
# hacia los endpoints reales de los clientes. Vacía o ausente = encendido, o
# sea que producción no cambia; solo lo apagan los valores off/0/no/false.
ENTREGAS_WEBHOOKS_ACTIVAS = (os.getenv("ENTREGAS_WEBHOOKS") or "").strip().lower() not in {"off", "0", "no", "false"}

SQLSERVER_HOST = os.getenv("SQLSERVER_HOST", "")
SQLSERVER_PORT = os.getenv("SQLSERVER_PORT", "1433")
SQLSERVER_DB = os.getenv("SQLSERVER_DB", "")
SQLSERVER_USER = os.getenv("SQLSERVER_USER", "")
SQLSERVER_PASSWORD = os.getenv("SQLSERVER_PASSWORD", "")
# El nombre del driver ODBC tiene que coincidir EXACTO con el instalado en el SO
# (en Ubuntu: `odbcinst -q -d` lo lista).
SQLSERVER_DRIVER = os.getenv("SQLSERVER_DRIVER", "ODBC Driver 18 for SQL Server")
# Un SQL Server interno normalmente trae certificado autofirmado; con Driver 18
# el default es Encrypt=yes y truena si no se confía en el cert.
SQLSERVER_TRUST_CERT = os.getenv("SQLSERVER_TRUST_CERT", "yes")
# El Driver 18 cambió el default a Encrypt=yes (el 17 era "no"), y contra un SQL
# Server viejo eso puede tumbar la conexión ANTES del login: OpenSSL 3 (Ubuntu
# 22.04+) rechaza TLS 1.0/1.1 y protocolos que ese SQL Server quizá sea el único
# que ofrece. El síntoma engaña: TCP abre bien y el error se ve como problema de
# conexión o de credenciales, no de cifrado.
#
# Se deja en "yes" a propósito — cifrar es lo correcto. Ponerlo en "no" es un
# escape documentado para una red interna donde el servidor no puede negociar
# TLS moderno, no la configuración recomendada.
SQLSERVER_ENCRYPT = os.getenv("SQLSERVER_ENCRYPT", "yes")

# ── Google Document AI ────────────────────────────────────────────────────────
# Se llaman directo los procesadores de Document AI (no vía el proyecto
# hermano `document_ai`). Los IDs van aquí y no hardcodeados en el código, para
# poder apuntar a procesadores distintos por ambiente sin tocar fuentes.
#
# La credencial la resuelve google-auth sola leyendo GOOGLE_APPLICATION_CREDENTIALS
# del entorno (ruta al JSON de la cuenta de servicio), por eso no se lee aquí.
DOCAI_PROJECT_ID = os.getenv("DOCAI_PROJECT_ID", "")
DOCAI_LOCATION = os.getenv("DOCAI_LOCATION", "us")
DOCAI_PROCESADOR_INE = os.getenv("DOCAI_PROCESADOR_INE", "")
# El ÚNICO Custom Document Classifier del sistema (ver servicios/procesadores.py
# sincronizar_clasificador). A diferencia de DOCAI_PROCESADOR_INE, este no
# identifica un tipo documental sino el paso previo: a cuál tipo pertenece un
# documento entrante, antes de decidir a qué extractor mandarlo.
DOCAI_CLASIFICADOR_ID = os.getenv("DOCAI_CLASIFICADOR_ID", "")
# Misma razón que DOCAI_VERSION_INE: sin fijarla, Google puede promover otra
# versión a "default" sin aviso y la clasificación deja de ser reproducible.
DOCAI_VERSION_CLASIFICADOR = os.getenv("DOCAI_VERSION_CLASIFICADOR", "")

# Versión del modelo con la que se llama al procesador. Si se deja vacía, Google
# usa la "default" del procesador — y esa la puede cambiar él, sin avisar, el
# día que promueva otra a estable. Eso rompería la reproducibilidad que exige el
# diccionario de datos ("Modelo y versión exactos") y haría que
# `extraction_run.engine_version` guardara una suposición en vez de un hecho.
# Fijarla es la diferencia entre saber y creer con qué modelo se extrajo.
DOCAI_VERSION_INE = os.getenv("DOCAI_VERSION_INE", "")
IA_TIMEOUT = _numero("IA_TIMEOUT", 120)

# ── Documentos largos (más de 15 páginas) ─────────────────────────────────────
# El procesamiento EN LÍNEA de Document AI rechaza un documento de más de 15 páginas ("Document pages exceed the limit: 15 got
# 20"), y un acta constitutiva ronda las 20. Cuando eso pasa, la extracción se hace por BLOQUES de páginas y se unen los
# resultados (servicios/bloques.py). Un documento de 15 páginas o menos nunca pasa por aquí.
#
# EXTRACCION_MAX_PAGINAS: lo más largo que se acepta. Por encima, se rechaza SIN procesar nada (ni cobrar) con el número de
#   páginas en el mensaje. Es una política de negocio: 20 es lo que se acordó para las actas (2026-10-16); se sube aquí si
#   algún día se aceptan más. Cada página extra cuesta lo mismo (US$0.03) y alarga el trabajo, no lo vuelve más arriesgado.
#   No puede ser menor que 15, porque lo de 15 o menos ni siquiera llega a medirse.
# EXTRACCION_PAGINAS_POR_BLOQUE: tope de páginas de un bloque (15 es lo que admite Google). Más chico = más bloques y más
#   cortes, pero cada llamada ve menos páginas a la vez.
# EXTRACCION_SOLAPE_PAGINAS: páginas que un bloque repite del anterior para que lo que cruza el corte salga completo en uno.
#   Cada página repetida se cobra dos veces.
EXTRACCION_MAX_PAGINAS = max(15, int(_numero("EXTRACCION_MAX_PAGINAS", 20)))
EXTRACCION_PAGINAS_POR_BLOQUE = max(1, min(15, int(_numero("EXTRACCION_PAGINAS_POR_BLOQUE", 15))))
EXTRACCION_SOLAPE_PAGINAS = max(0, int(_numero("EXTRACCION_SOLAPE_PAGINAS", 1)))

# ── Límite de subida ──────────────────────────────────────────────────────────
# En el server de CSI la API se expone IP:puerto directo, sin nginx delante (los
# dominios y el proxy solo se usan en el droplet de DigitalOcean). O sea: no hay
# `client_max_body_size` que nos proteja, el límite tiene que vivir aquí.
#
# Importa porque `/ia/ine` codifica el archivo a base64 para mandarlo a Document
# AI, y eso infla el contenido ~1.33x en memoria.
# 20 MB y no 10: es el mismo número que promete el dropzone del front
# ("PDF, DOCX, XLSX, JPG, JPEG, TIFF | Max 20 MB", que viene de Figma) y que
# valida su capa BFF. Con 10 quedaba una franja de 10-20 MB que la UI
# aceptaba y esta API rechazaba, o sea un error que el usuario no podía
# prever. 20 MB es además el tope de Document AI para procesamiento en
# línea, así que subir más no serviría de nada.
MAX_SUBIDA_MB = _numero("MAX_SUBIDA_MB", 20)
if MAX_SUBIDA_MB <= 0:
    raise RuntimeError(f"MAX_SUBIDA_MB tiene que ser mayor que 0 (llegó {MAX_SUBIDA_MB}).")
MAX_SUBIDA_BYTES = int(MAX_SUBIDA_MB * 1024 * 1024)

# ── Almacén de documentos ─────────────────────────────────────────────────────
# Dónde se guardan los BYTES de un archivo subido (ver servicios/almacen.py).
#
# VACÍA A PROPÓSITO por ahora. El almacén está construido y probado pero NADIE
# lo llama todavía: se dejó listo para el día que exista la tabla `file` en SQL
# Server, sin cambiar en nada el comportamiento actual de Nexus. Mientras esté
# vacía, `almacen.esta_configurado()` responde False y cualquier intento de
# escribir levanta un error explícito en vez de inventarse una carpeta.
#
# Va a ser el punto de montaje del NAS de infraestructura de CSI. Se configura
# aquí y no se hardcodea porque el montaje cambia por ambiente (y podría
# cambiar en el server sin que cambie el código): en la base se guardan rutas
# RELATIVAS justamente para que mover el montaje no obligue a migrar datos.
#
# Local (Windows):  C:/Moibe/almacen-nexus
# Server de CSI:    /mnt/nas/nexus/documentos
ALMACEN_RUTA = os.getenv("ALMACEN_RUTA", "")

# ── Acceso (Sprint 1: login con JWT) ─────────────────────────────────────────
# El secreto firma los JWT de acceso; el front lo comparte para verificarlos
# (y pregunta aparte, en GET /auth/sesion, si la sesión sigue viva). Sin él,
# /auth/* responde 503. Mínimo 32
# caracteres: `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
AUTH_JWT_SECRET = os.getenv("AUTH_JWT_SECRET", "").strip()
# Vida del JWT de acceso (minutos) y del refresh token (días).
AUTH_ACCESO_MIN = int(_numero("AUTH_ACCESO_MIN", 15))
AUTH_REFRESH_DIAS = int(_numero("AUTH_REFRESH_DIAS", 7))
# La política del diseño: 5 intentos fallidos → 15 minutos de bloqueo. Cuando
# existan los SPs de NEX-319, vivirá en `uspRecordLoginResult` y esto sobra.
AUTH_MAX_INTENTOS = int(_numero("AUTH_MAX_INTENTOS", 5))
AUTH_BLOQUEO_MIN = int(_numero("AUTH_BLOQUEO_MIN", 15))

# ── Correo (Sprint 1: activación y recuperación de contraseña) ───────────────
# El canal real depende de Soporte TI (qué relay, qué remitente, si la red deja
# salir el puerto). Para no quedarnos parados, el envío tiene modos:
#
#   archivo   (default) deja el correo en `<ALMACEN_RUTA>/correos/`; no toca la
#             red. Es con lo que se desarrolla y lo que leen las pruebas.
#   smtp      lo manda de verdad. Necesita al menos CORREO_SMTP_HOST y
#             CORREO_REMITENTE.
#   silencio  lo descarta.
#   api       lo manda por el servicio HTTP de notificaciones de ART Broker (pide
#             un token y manda el correo). Necesita CORREO_API_URL (solo el host,
#             en https), CORREO_API_USUARIO y CORREO_API_CONTRASENA. El remitente
#             lo decide ESE servicio, no CORREO_REMITENTE.
#
# El día que TI conteste, esto es lo único que cambia.
CORREO_MODO = (os.getenv("CORREO_MODO") or "archivo").strip().lower()
CORREO_REMITENTE = (os.getenv("CORREO_REMITENTE") or "").strip()
CORREO_SMTP_HOST = (os.getenv("CORREO_SMTP_HOST") or "").strip()
CORREO_SMTP_PUERTO = int(_numero("CORREO_SMTP_PUERTO", 587))
CORREO_SMTP_USUARIO = (os.getenv("CORREO_SMTP_USUARIO") or "").strip()
CORREO_SMTP_CONTRASENA = os.getenv("CORREO_SMTP_CONTRASENA") or ""
# STARTTLS sobre el puerto normal (587) o SSL directo (465). No los dos.
CORREO_SMTP_TLS = (os.getenv("CORREO_SMTP_TLS") or "1").strip().lower() in {"1", "true", "si", "sí", "yes"}
CORREO_SMTP_SSL = (os.getenv("CORREO_SMTP_SSL") or "").strip().lower() in {"1", "true", "si", "sí", "yes"}
CORREO_TIMEOUT = int(_numero("CORREO_TIMEOUT", 20))
# Servicio HTTP de notificaciones (modo `api`). Las rutas van fijas en
# `servicios/correo.py`: entre ambientes solo cambia el host. Credenciales: SOLO
# en el .env del servidor, nunca en el repo, la documentación ni el log.
CORREO_API_URL = (os.getenv("CORREO_API_URL") or "").strip().rstrip("/")
CORREO_API_USUARIO = (os.getenv("CORREO_API_USUARIO") or "").strip()
CORREO_API_CONTRASENA = os.getenv("CORREO_API_CONTRASENA") or ""

# La URL pública del FRONT, para armar los enlaces que van en los correos. Sin
# esto no se puede mandar un enlace que alguien pueda abrir.
#   local: http://localhost:3400 · dev CSI: http://172.10.30.15:3401
FRONT_URL = (os.getenv("FRONT_URL") or "http://localhost:3400").strip().rstrip("/")

# Vigencia de los enlaces de un solo uso, en horas. Las dos salen del diseño:
# 48 h para activar una cuenta nueva (HU06) y 1 h para recuperar la contraseña
# (HU10), que es más corta a propósito porque abre la puerta de una cuenta que
# ya existe.
# Cuánto sirve la contraseña temporal del primer acceso (HU06 CA-5), y cuánto dura un enlace de activación. Puede
# ser fraccionaria (0.01 = menos de un minuto: para probar la caducidad sin esperar dos días); 0 apaga la
# caducidad del primer acceso.
TOKEN_ACTIVACION_HORAS = _numero("TOKEN_ACTIVACION_HORAS", 48)
TOKEN_RECUPERACION_HORAS = int(_numero("TOKEN_RECUPERACION_HORAS", 1))

# Freno a "Olvidé mi contraseña" (HU10). Esa pantalla la abre cualquiera, sin
# sesión, y cada solicitud puede mandar un correo: sin límite sería una forma de
# llenarle el buzón a una persona o de gastar el relay de CSI. Los límites
# cuentan TODAS las solicitudes, existan o no las cuentas (si no, delatarían
# cuáles existen), y se aplican ANTES de mirar si la cuenta existe. Todo en
# SEGUNDOS menos lo que dice "por hora".
RECUPERACION_ESPERA_SEG = int(_numero("RECUPERACION_ESPERA_SEG", 60))          # entre dos solicitudes del MISMO correo
RECUPERACION_MAX_POR_CORREO_HORA = int(_numero("RECUPERACION_MAX_POR_CORREO_HORA", 5))
RECUPERACION_MAX_POR_ORIGEN_HORA = int(_numero("RECUPERACION_MAX_POR_ORIGEN_HORA", 5))   # por navegador (cookie)
# Por IP y global NO dependen de nada que el cliente controle: la cookie del
# navegador se puede no mandar, la IP del BFF y el total no. La IP va holgada
# porque detrás del proxy de TI puede ser la de todos.
RECUPERACION_MAX_POR_IP_HORA = int(_numero("RECUPERACION_MAX_POR_IP_HORA", 30))
RECUPERACION_MAX_GLOBAL_HORA = int(_numero("RECUPERACION_MAX_GLOBAL_HORA", 200))
# ¿La pantalla «Recupera tu acceso» le dice a quien teclea un correo que NO tiene cuenta? Sí (por defecto), como dibuja el
# Figma y como pidió QA el 2026-10-20: «El correo electrónico no existe; por favor ingresa un correo electrónico válido.».
# Es lo que el criterio CA-2 de HU10 prohibía: con esto, esa pantalla deja de ser neutral y cualquiera puede preguntar si
# un correo tiene cuenta (el login ya lo hace desde HU03, y esta ruta sigue frenada por los límites de arriba). Poner
# `RECUPERACION_DICE_SI_EXISTE=0` en el .env devuelve el comportamiento neutral de CA-2 sin tocar código.
RECUPERACION_DICE_SI_EXISTE = (os.getenv("RECUPERACION_DICE_SI_EXISTE") or "1").strip().lower() not in ("0", "false", "no")

# Contacto de soporte que se imprime al pie de los correos. Vacío = no se
# imprime el bloque: el diseño trae un correo y un teléfono de ejemplo que NO
# son nuestros, y mandar datos de contacto falsos en un correo real es peor
# que no mandarlos.
CORREO_SOPORTE = (os.getenv("CORREO_SOPORTE") or "").strip()
CORREO_SOPORTE_TELEFONO = (os.getenv("CORREO_SOPORTE_TELEFONO") or "").strip()


# ── Avisos de configuración de producción ─────────────────────────────────────
# Este proyecto ya se tropezó TRES veces con el mismo molde: una variable cuyo
# default es correcto en la laptop y equivocado en el servidor, que falla en
# silencio porque `.env` no viaja por git y `git pull` no la puede traer.
#
#   · BODY_SIZE_LIMIT (en el front) — tumbaba toda subida de más de 512 KB, y el
#     error no mencionaba el tamaño: se diagnosticaba como "archivo corrupto".
#   · DOCAI_VERSION_INE — sin fijar, Google resolvía su versión "default" y el
#     mismo archivo devolvía 19 campos en el server contra 20 en local.
#   · FRONT_URL — su default manda los enlaces de los correos a localhost, o sea
#     a la máquina de quien RECIBE el correo. El síntoma es "llegó el correo pero
#     el link no sirve", que no se lee como un problema de configuración.
#
# De ahí esta función: al arrancar fuera de `local`, revisa las variables donde
# quedarse con el default es un error, y avisa.
#
# Dos decisiones a propósito:
#   1. AVISA, no truena. Una app caída por un detalle de configuración es peor
#      que una app arriba con un WARNING visible. El arranque nunca se bloquea.
#   2. Devuelve datos, no loguea. Cuando `config` se importa, `configurar_logging`
#      todavía no corrió, así que un `logger.warning` aquí se perdería o saldría
#      sin formato. Quien llama (`app.py`) lo loguea en el momento correcto.
def avisos_de_produccion() -> list[str]:
    """Variables que se quedaron en un default que no sirve fuera de `local`.

    Cada aviso dice QUÉ pasa, no solo qué falta: un "FRONT_URL vacío" no le
    dice nada a quien lee el log a las 11 de la noche; "los enlaces de los
    correos apuntan a la máquina de quien los recibe", sí.

    Lista vacía en `ENVIRONMENT=local`: ahí los defaults son justamente los
    correctos y avisar sería ruido que enseña a ignorar los avisos.
    """
    if ENVIRONMENT.strip().lower() == "local":
        return []

    avisos: list[str] = []

    if "localhost" in FRONT_URL or "127.0.0.1" in FRONT_URL:
        avisos.append(
            f"FRONT_URL={FRONT_URL!r} — los enlaces de activación y de "
            "recuperación de contraseña apuntarían a la máquina de quien RECIBE "
            "el correo, no al servidor. Los correos llegan y el link está muerto."
        )

    if CORREO_MODO in {"archivo", "silencio"}:
        avisos.append(
            f"CORREO_MODO={CORREO_MODO!r} — no sale ningún correo de verdad. "
            "La recuperación de contraseña responde bien pero nadie recibe nada."
        )
    elif CORREO_MODO == "api" and not (
        CORREO_API_URL and CORREO_API_USUARIO and CORREO_API_CONTRASENA
    ):
        avisos.append(
            "CORREO_MODO=api pero falta CORREO_API_URL, _USUARIO o _CONTRASENA — "
            "el envío va a fallar en cada intento."
        )
    elif CORREO_MODO == "smtp" and not CORREO_SMTP_HOST:
        avisos.append("CORREO_MODO=smtp pero CORREO_SMTP_HOST está vacío.")

    if not AUTH_JWT_SECRET:
        avisos.append("AUTH_JWT_SECRET vacío — el login responde 503.")

    if not ALMACEN_RUTA:
        avisos.append("ALMACEN_RUTA vacío — el almacén está apagado y el login no funciona.")

    if not NEXUS_API_KEY:
        avisos.append(
            "NEXUS_API_KEY vacía — los endpoints protegidos fallan CERRADOS "
            "(seguro, pero el front no puede llamarlos)."
        )

    if not WEBHOOKS_CLAVE_CIFRADO:
        avisos.append("WEBHOOKS_CLAVE_CIFRADO vacía — el alta de webhooks responde 503.")

    if WEBHOOKS_PERMITIR_LOCAL:
        avisos.append(
            "SEGURIDAD: WEBHOOKS_PERMITIR_LOCAL está encendido fuera de local — "
            "permite registrar webhooks contra localhost del propio servidor."
        )

    if not DOCAI_VERSION_INE:
        avisos.append(
            "DOCAI_VERSION_INE vacía — Google resuelve su versión 'default', que "
            "puede mover sin aviso; la extracción deja de ser reproducible."
        )

    return avisos

