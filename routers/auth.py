"""Acceso: login con JWT, renovación, cierre de sesión y cambio de contraseña.

Sprint 1 — HU01 (bootstrap del super admin: `bootstrap_admin.py`, no un
endpoint), HU03 (login), HU04 (cambio obligatorio en el primer acceso) y HU10
(recuperar la contraseña, al final de este archivo). Los
textos de error vienen con un `codigo` estable para que el front elija la
pantalla o el mensaje del diseño; el `detail` es para humanos y logs.

Quién llama: SOLO la capa server del front (BFF), con la llave de servicio
como todo lo demás. El navegador nunca pega aquí: las cookies las pone el
front. Por eso `ip` y `userAgent` llegan en el cuerpo: son los del navegador,
que el BFF conoce y nosotros no.

Lo que el diseño pide y aquí se cumple:

- Mensajes distintos para "el correo no existe" y "la contraseña es
  incorrecta" (sí, eso permite enumerar correos; es lo que dibujó UX y aquí
  los usuarios los crea un administrador, no cualquiera).
- Aviso de cuántos intentos quedan antes del bloqueo (`intentosRestantes`).
- Bloqueo de 15 minutos con `hastaEn`, para el contador de la pantalla.
- Pantalla propia para cuenta desactivada (`codigo: desactivada`).
- En el primer acceso el login SÍ entra, pero con `debeCambiarContrasena`:
  el front solo deja llegar a "Configura tu nueva contraseña". Al cambiarla se
  cierran todas las sesiones y hay que volver a entrar (así lo dibuja el
  flujo: "Contraseña actualizada → Iniciar sesión").
"""

import logging
import re
from datetime import datetime, timezone
from typing import NoReturn

from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

import config
from routers.errores import almacen_no_disponible
from servicios import auditoria, auth, correo, plantillas_correo, tokens, usuarios
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)
router = APIRouter()


def _bloqueada(hasta_en: str) -> HTTPException:
    """La única pantalla de bloqueo del diseño, la dispare la cuenta o el
    navegador: para quien la ve es el mismo contador de cinco intentos."""
    return _error(
        status.HTTP_423_LOCKED,
        "bloqueada",
        "Tu cuenta ha sido bloqueada temporalmente por exceder el número máximo de intentos permitidos.",
        hastaEn=hasta_en,
        segundosRestantes=_segundos_hasta(hasta_en),
    )


class Login(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)
    ip: str | None = Field(default=None, max_length=45)
    userAgent: str | None = Field(default=None, max_length=500)
    #: Identificador anónimo del navegador (cookie `nx_navegador` del front).
    #: Contra él se cuentan los intentos fallidos de correo Y de contraseña.
    origen: str | None = Field(default=None, max_length=64)


class Refresh(BaseModel):
    refreshToken: str = Field(max_length=128)
    ip: str | None = Field(default=None, max_length=45)
    userAgent: str | None = Field(default=None, max_length=500)


class Logout(BaseModel):
    refreshToken: str | None = Field(default=None, max_length=128)
    #: Los del navegador que cierra, para el rastro. Opcionales: un cliente que
    #: no los mande cierra igual.
    ip: str | None = Field(default=None, max_length=45)
    userAgent: str | None = Field(default=None, max_length=500)


class RecuperacionPerfil(BaseModel):
    email: str | None = Field(default=None, max_length=254)
    telefono: str | None = Field(default=None, max_length=25)


class Perfil(BaseModel):
    nombre: str = Field(max_length=200)
    apellidoPaterno: str = Field(default="", max_length=200)
    apellidoMaterno: str = Field(default="", max_length=200)
    telefono: str | None = Field(default=None, max_length=25)
    recuperacion: RecuperacionPerfil | None = None


class CambioContrasena(BaseModel):
    actual: str | None = Field(default=None, max_length=256)
    nueva: str = Field(max_length=256)
    confirmacion: str = Field(max_length=256)


class SolicitudRecuperacion(BaseModel):
    email: str = Field(max_length=254)
    ip: str | None = Field(default=None, max_length=45)
    userAgent: str | None = Field(default=None, max_length=500)
    #: El identificador anónimo del navegador (cookie `nx_navegador` del front).
    origen: str | None = Field(default=None, max_length=64)


class RevisionEnlace(BaseModel):
    token: str = Field(max_length=128)


class ConfirmacionRecuperacion(BaseModel):
    token: str = Field(max_length=128)
    nueva: str = Field(max_length=256)
    confirmacion: str = Field(max_length=256)
    ip: str | None = Field(default=None, max_length=45)
    origen: str | None = Field(default=None, max_length=64)


def _error(status_code: int, codigo: str, detail: str, **extra) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"codigo": codigo, "mensaje": detail, **extra})


def _sin_configurar() -> HTTPException:
    return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "sin_configurar", "El acceso no está configurado en el servidor (AUTH_JWT_SECRET).")


def _activacion_vencida() -> HTTPException:
    """La contraseña temporal caducó (HU06 CA-5). El texto es el de la pantalla del diseño; la pantalla lleva el suyo."""
    return _error(
        status.HTTP_403_FORBIDDEN,
        "activacion_vencida",
        "El enlace utilizado para acceder a la plataforma ha vencido. Los enlaces de primer acceso son válidos "
        "únicamente durante 48 horas a partir de su envío. Contacta al administrador de tu organización para "
        "generar un nuevo acceso.",
    )


def _contrasena_incorrecta(u: dict, email: str, datos: "Login", origen: str | None) -> NoReturn:
    """Anota la contraseña mala y responde: 423 si con esta se llegó al tope de intentos, o 401
    con los que quedan. No devuelve nunca."""
    fallidos, bloqueado_hasta = usuarios.registrar_resultado_login(
        u["guid"], email, False, "BAD_PASSWORD", datos.ip, datos.userAgent, origen
    )
    fallidos_nav, bloqueo_navegador = usuarios.estado_origen(origen)
    if bloqueado_hasta or bloqueo_navegador:
        raise _bloqueada(bloqueado_hasta or bloqueo_navegador)
    # El aviso dice la verdad: manda el contador que esté más cerca.
    raise _error(
        status.HTTP_401_UNAUTHORIZED, "credenciales",
        "La contraseña es incorrecta; por favor intenta de nuevo.",
        intentosRestantes=config.AUTH_MAX_INTENTOS - max(fallidos, fallidos_nav),
    )


def _sesion_nueva(usuario: dict, ip: str | None, user_agent: str | None, hash_verificado: str | None = None) -> dict:
    refresh, refresh_hash, vence_refresh = auth.nuevo_refresh()
    sesion_guid = usuarios.crear_sesion(usuario["guid"], refresh_hash, vence_refresh, ip, user_agent, hash_verificado)
    return _respuesta_de_sesion(usuario, sesion_guid, refresh, vence_refresh)


def _respuesta_de_sesion(usuario: dict, sesion_guid: str, refresh: str, vence_refresh: datetime) -> dict:
    acceso, vence_acceso = auth.emitir_acceso(usuario, sesion_guid)
    return {
        "accessToken": acceso,
        "accessExpiraEn": vence_acceso.isoformat(timespec="seconds"),
        "refreshToken": refresh,
        "refreshExpiraEn": vence_refresh.isoformat(timespec="seconds"),
        "usuario": usuario,
    }


# ── Recuperación de contraseña (HU10) ───────────────────────────────────────

#: Literal del criterio CA-2. Es lo ÚNICO que responde "solicitar" cuando la
#: solicitud es válida, exista la cuenta o no.
MENSAJE_NEUTRO = (
    "Si existe una cuenta asociada a ese correo electrónico, recibirás un enlace de recuperación en los "
    "próximos minutos."
)

#: Literal del Figma (pantalla "Enlace de recuperación expirado"). Es UNO solo
#: para enlace inexistente, usado, vencido o de una cuenta que ya no sirve: el
#: diseño tiene una sola pantalla, y distinguirlos le diría a un tercero qué
#: tokens existieron alguna vez.
_MENSAJE_ENLACE = (
    "Por motivos de seguridad, este enlace de restablecimiento ya no se encuentra disponible. Solicita un nuevo "
    "correo para continuar con el proceso."
)

#: La forma de un token que nosotros emitimos (`secrets.token_urlsafe`). Se
#: comprueba ANTES de tocar el registro: leerlo entero por cada basura que
#: alguien mande a la URL sería regalarle un amplificador de carga.
_RE_TOKEN = re.compile(r"[A-Za-z0-9_-]{20,128}")


def _enlace_no_valido(codigo: str) -> HTTPException:
    return _error(status.HTTP_410_GONE, codigo, _MENSAJE_ENLACE)


def _validar_enlace(token: str) -> tuple[dict, dict]:
    """Comprueba que el enlace sirva SIN gastarlo. Devuelve `(token, usuario)`.

    Un enlace emitido para una cuenta que después se desactivó, o que volvió a
    estar pendiente de su primer acceso, deja de servir: no tiene caso dejar
    que alguien llene el formulario para rechazarlo al final."""
    if not _RE_TOKEN.fullmatch(token or ""):
        raise _enlace_no_valido("enlace_invalido")
    try:
        t = tokens.revisar(token, tokens.RECUPERACION)
    except tokens.TokenInvalido as exc:
        raise _enlace_no_valido("enlace_invalido") from exc
    except tokens.YaUsado as exc:
        raise _enlace_no_valido("enlace_usado") from exc
    except tokens.Vencido as exc:
        raise _enlace_no_valido("enlace_vencido") from exc
    try:
        u = usuarios.por_guid(t["usuarioGuid"])
    except usuarios.NoEncontrado as exc:
        raise _enlace_no_valido("enlace_invalido") from exc
    if not u["activo"] or u["debeCambiarContrasena"]:
        raise _enlace_no_valido("enlace_invalido")
    return t, u


def _frenar_recuperacion(email: str, origen: str | None, ip: str | None) -> None:
    """Levanta 429 si esta solicitud rebasa algún límite.

    Cuenta TODAS las solicitudes, existan o no las cuentas (si no, el límite
    delataría cuáles existen), y va ANTES de mirar si la cuenta existe. Tres de
    los cinco límites no dependen de nada que el cliente controle: la cookie
    del navegador se puede no mandar, el correo y la IP del BFF y el total no."""
    ahora = datetime.now(timezone.utc)
    hora = auditoria.recientes(auditoria.PASSWORD_RESET_REQUESTED, 3600)
    demasiadas = (
        sum(1 for _, e in hora if e.get("email") == email) >= config.RECUPERACION_MAX_POR_CORREO_HORA
        or any(
            e.get("email") == email and (ahora - cuando).total_seconds() < config.RECUPERACION_ESPERA_SEG
            for cuando, e in hora
        )
        or (origen and sum(1 for _, e in hora if e.get("origen") == origen) >= config.RECUPERACION_MAX_POR_ORIGEN_HORA)
        or (ip and sum(1 for _, e in hora if e.get("ip") == ip) >= config.RECUPERACION_MAX_POR_IP_HORA)
        or len(hora) >= config.RECUPERACION_MAX_GLOBAL_HORA
    )
    if demasiadas:
        raise _error(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "demasiados_intentos",
            "Has solicitado demasiados enlaces. Espera unos minutos e intenta de nuevo.",
        )


def _enviar_en_segundo_plano(destino: str, asunto: str, texto: str, html: str) -> None:
    """Manda el correo DESPUÉS de haber respondido. Dos razones: el tiempo que
    tarda el servidor de correo no puede delatar si la cuenta existe (con
    cuenta se manda y sin cuenta no, y la diferencia sería de segundos), y un
    fallo de envío no debe cambiar lo que ve la persona. Lo que se registra es
    a quién y con qué asunto; el cuerpo, que lleva el enlace, jamás."""
    try:
        correo.enviar(destino, asunto, texto, html)
    except correo.ErrorCorreo as exc:
        logger.warning("No se pudo mandar el correo de recuperación a %s: %s", destino, exc)


def _segundos_hasta(iso: str) -> int:
    hasta = datetime.fromisoformat(iso)
    return max(0, int((hasta - datetime.now(timezone.utc)).total_seconds()))


@router.post(
    "/login",
    summary="Iniciar sesión",
    description=(
        "Correo y contraseña → JWT de acceso + refresh token. Errores con `codigo`: "
        "`correo_invalido` (400), `correo_no_existe` y `credenciales` (401, con `intentosRestantes`), "
        "`bloqueada` (423, con `hastaEn` y `segundosRestantes`), `desactivada` (403), `activacion_vencida` (403: la "
        "contraseña temporal era la correcta pero ya pasaron las horas de `TOKEN_ACTIVACION_HORAS`)."
    ),
)
def login(datos: Login, response: Response) -> dict:
    if not config.AUTH_JWT_SECRET:
        raise _sin_configurar()
    response.headers["Cache-Control"] = "no-store"
    email = datos.email.strip().lower()
    if not usuarios.correo_valido(email):
        raise _error(status.HTTP_400_BAD_REQUEST, "correo_invalido", "Por favor, ingresa un correo electrónico válido.")
    origen = (datos.origen or "").strip() or None
    try:
        # El navegador bloqueado no pasa, teclee el correo que teclee: si no, se
        # saldría del bloqueo probando otro correo.
        _, bloqueo_navegador = usuarios.estado_origen(origen)
        if bloqueo_navegador:
            usuarios.registrar_resultado_login(None, email, False, "LOCKED", datos.ip, datos.userAgent, origen)
            raise _bloqueada(bloqueo_navegador)

        u = usuarios.para_login(email)
        if u is None:
            usuarios.registrar_resultado_login(None, email, False, "UNKNOWN_EMAIL", datos.ip, datos.userAgent, origen)
            fallidos_nav, bloqueo_navegador = usuarios.estado_origen(origen)
            if bloqueo_navegador:
                raise _bloqueada(bloqueo_navegador)
            raise _error(
                status.HTTP_401_UNAUTHORIZED, "correo_no_existe",
                "El correo electrónico no existe; por favor ingresa un correo electrónico válido.",
                intentosRestantes=config.AUTH_MAX_INTENTOS - fallidos_nav,
            )
        if u["bloqueadoHasta"]:
            usuarios.registrar_resultado_login(u["guid"], email, False, "LOCKED", datos.ip, datos.userAgent, origen)
            raise _bloqueada(u["bloqueadoHasta"])
        if not u["activo"]:
            usuarios.registrar_resultado_login(u["guid"], email, False, "INACTIVE", datos.ip, datos.userAgent, origen)
            raise _error(status.HTTP_403_FORBIDDEN, "desactivada", "La cuenta con la que intentas ingresar ha sido desactivada.")
        if not auth.verificar_contrasena(u["hash"], datos.password):
            _contrasena_incorrecta(u, email, datos, origen)
        if usuarios.acceso_vencido(u):
            # La contraseña era la correcta pero caducó: no cuenta como intento fallido (no es un error de quien
            # escribe) y se dice DESPUÉS de verificarla, para no contarle esto a quien no la tiene.
            usuarios.registrar_resultado_login(u["guid"], email, False, "EXPIRED", datos.ip, datos.userAgent, origen)
            raise _activacion_vencida()
        publico = {k: v for k, v in u.items() if k not in ("hash", "fallidos", "bloqueadoHasta", "temporalEmitidaEn")}
        publico["ultimoLoginEn"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        try:
            # `hash_verificado`: la contraseña se comprobó fuera del candado; si mientras tanto
            # se restableció o la cuenta se desactivó, la sesión no debe nacer. El acierto se
            # anota DESPUÉS de abrirla: un login que se rechaza aquí no puede quedar como exitoso
            # (reiniciaría los intentos fallidos y fijaría `ultimoLoginEn`).
            sesion = _sesion_nueva(publico, datos.ip, datos.userAgent, hash_verificado=u["hash"])
        except usuarios.SesionNoCanjeable as exc:
            if exc.motivo == "desactivada":
                usuarios.registrar_resultado_login(u["guid"], email, False, "INACTIVE", datos.ip, datos.userAgent, origen)
                raise _error(status.HTTP_403_FORBIDDEN, "desactivada", "La cuenta con la que intentas ingresar ha sido desactivada.") from exc
            # La contraseña que se verificó ya no es la vigente (se restableció a media petición):
            # para efectos del login es una contraseña mala, y se cuenta como tal.
            _contrasena_incorrecta(u, email, datos, origen)
        usuarios.registrar_resultado_login(u["guid"], email, True, None, datos.ip, datos.userAgent, origen)
        logger.info("Login correcto (guid=%s)", u["guid"])
        return sesion
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    except auth.SinConfigurar as exc:
        raise _sin_configurar() from exc


@router.post(
    "/refresh",
    summary="Renovar la sesión",
    description="Cambia un refresh token vigente por un JWT nuevo y un refresh nuevo (el anterior queda revocado).",
)
def refresh(datos: Refresh, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    try:
        nuevo_refresh, nuevo_hash, vence_refresh = auth.nuevo_refresh()
        # Comprobar la sesión, revocarla y crear la sucesora, en UN paso: un refresh es de
        # un solo uso y un cierre forzado no puede dejar una sucesora viva a medias.
        sesion_guid, usuario = usuarios.rotar_sesion(
            auth.hash_refresh(datos.refreshToken), nuevo_hash, vence_refresh, datos.ip, datos.userAgent
        )
        return _respuesta_de_sesion(usuario, sesion_guid, nuevo_refresh, vence_refresh)
    except usuarios.SesionNoCanjeable as exc:
        if exc.motivo == "vencida":
            raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_vencida", "La sesión venció. Inicia sesión de nuevo.") from exc
        raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_invalida", "La sesión ya no es válida. Inicia sesión de nuevo.") from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    except auth.SinConfigurar as exc:
        raise _sin_configurar() from exc


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Cerrar sesión",
    description=(
        "Revoca la sesión de ESE refresh token y solo esa: las de otros dispositivos siguen. Responde 204 aunque "
        "el token no exista o ya estuviera revocado (503 solo si el almacén no responde) y deja un evento `LOGOUT` "
        "en la bitácora de auditoría. El JWT de esa sesión deja de valer EN ESTE BACK al instante (401 "
        "`sesion_cerrada`): los guards que validan un Bearer comprueban que la sesión de su `sid` siga viva. "
        "Las rutas que decide solo el front lo comprueban con `GET /auth/sesion`."
    ),
)
def logout(datos: Logout) -> Response:
    """Revoca la sesión del refresh token. Sin token, o con uno que ya no
    existe, responde 204 igual: cerrar sesión nunca falla hacia el usuario."""
    if datos.refreshToken:
        try:
            # Límite conocido: si una renovación canjea ESTE refresh justo entre la lectura y
            # la revocación, `revocar_sesiones` no encuentra nada vivo y la sesión sucesora sigue
            # abierta (aquí no hay forma de saber cuál es: no se guarda de quién viene). El front
            # comparte una sola renovación por refresh, y la ventana es de milisegundos.
            s = usuarios.sesion_por_hash(auth.hash_refresh(datos.refreshToken))
            if s and not s["revocadaEn"]:
                cerradas = usuarios.revocar_sesiones(s["guid"], s["sesionGuid"], "LOGOUT")
                if cerradas:
                    _auditar_cierre(s["guid"], s["sesionGuid"], datos.ip, datos.userAgent)
        except ErrorAlmacen as exc:
            raise almacen_no_disponible(exc) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _campos_modificados(antes: dict, despues: dict) -> list[str]:
    """Los NOMBRES de los campos del perfil que cambiaron (HU12 CA-6), en el orden de la pantalla. Se compara lo que QUEDÓ
    guardado con lo de antes, no lo que se mandó: el mismo correo escrito con espacios y mayúsculas no es un cambio."""
    rec_antes, rec_despues = antes.get("recuperacion") or {}, despues.get("recuperacion") or {}
    pares = (
        ("nombre", antes.get("nombre"), despues.get("nombre")),
        ("apellidoPaterno", antes.get("apellidoPaterno"), despues.get("apellidoPaterno")),
        ("apellidoMaterno", antes.get("apellidoMaterno"), despues.get("apellidoMaterno")),
        ("telefono", antes.get("telefono"), despues.get("telefono")),
        ("correoRecuperacion", rec_antes.get("email"), rec_despues.get("email")),
        ("telefonoRecuperacion", rec_antes.get("telefono"), rec_despues.get("telefono")),
    )
    return [campo for campo, a, b in pares if (a or None) != (b or None)]


def _auditar_perfil(usuario_guid: str, sesion_guid: str, campos: list[str]) -> None:
    """Deja `PROFILE_UPDATED` si algo cambió. Va DESPUÉS de guardar y sin poder romperlo: los datos ya se guardaron y decirle
    a la persona que no se pudo sería falso (igual que `LOGOUT` y `PASSWORD_CHANGED`)."""
    if not campos:
        return
    try:
        auditoria.registrar(auditoria.PROFILE_UPDATED, usuario_guid=usuario_guid, sesionGuid=sesion_guid, campos=campos)
    except (ErrorAlmacen, OSError) as exc:
        logger.warning("No se pudo registrar el PROFILE_UPDATED en la bitácora de auditoría: %s", exc)


def _auditar_cambio_de_contrasena(accion: str, usuario_guid: str, sesion_guid: str, **datos) -> None:
    """Deja `PASSWORD_CHANGED` (HU13 CA-4: cambio desde una sesión) o `FIRST_LOGIN_PASSWORD_CHANGED` (HU04 CA-5: la
    definitiva del primer acceso). Va DESPUÉS del cambio y sin poder romperlo: la contraseña ya cambió y las sesiones ya
    se cerraron, y decirle a la persona que no se pudo sería falso. El dato también queda en el registro de usuarios
    (`contrasena` con su motivo); si el almacén falla se avisa en el log. `datos`: cuántas sesiones se cerraron."""
    try:
        auditoria.registrar(accion, usuario_guid=usuario_guid, sesionGuid=sesion_guid, **datos)
    except (ErrorAlmacen, OSError) as exc:
        logger.warning("No se pudo registrar el %s en la bitácora de auditoría: %s", accion, exc)


def _auditar_cierre(usuario_guid: str, sesion_guid: str, ip: str | None, user_agent: str | None) -> None:
    """Deja el evento `LOGOUT`. Va DESPUÉS de revocar y sin poder romper el
    cierre: la sesión ya murió y el dato está también en `sesion_revocada`, así
    que si el registro de auditoría falla se avisa en el log y se sigue.
    (El cierre FORZADO hace lo contrario, a propósito: ahí el rastro es lo que
    dice quién lo ordenó y por qué, y sin él no debe cerrarse nada.)"""
    try:
        auditoria.registrar(
            auditoria.LOGOUT,
            usuario_guid=usuario_guid,
            sesionGuid=sesion_guid,
            ip=(ip or "").strip() or None,
            userAgent=(user_agent or "")[:500] or None,
        )
    except (ErrorAlmacen, OSError) as exc:
        logger.warning("No se pudo registrar el LOGOUT en la bitácora de auditoría: %s", exc)


def _claims_del_bearer(authorization: str | None) -> dict:
    """Los claims del JWT de acceso (firma y vigencia comprobadas), o 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _error(status.HTTP_401_UNAUTHORIZED, "sin_sesion", "Falta el token de acceso.")
    try:
        claims = auth.verificar_acceso(authorization[7:].strip())
    except auth.SinConfigurar as exc:
        raise _sin_configurar() from exc
    if not claims:
        raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_invalida", "El token de acceso no es válido o venció.")
    return claims


def _usuario_del_bearer(authorization: str | None) -> tuple[dict, dict]:
    """(claims, usuario) del JWT de acceso, o 401."""
    claims = _claims_del_bearer(authorization)
    try:
        u, viva = usuarios.por_guid_y_sesion(claims["sub"], claims["sid"])
    except usuarios.NoEncontrado as exc:
        raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_invalida", "El usuario ya no existe.") from exc
    if not u["activo"]:
        raise _error(status.HTTP_403_FORBIDDEN, "desactivada", "La cuenta con la que intentas ingresar ha sido desactivada.")
    if not viva:
        # El JWT es válido (firma y vigencia) pero su SESIÓN se cerró: voluntaria,
        # forzada, por restablecer la contraseña... (HU05 CA-2 y CA-6).
        raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_cerrada", "La sesión ya no es válida. Inicia sesión de nuevo.")
    return claims, u


@router.get("/yo", summary="Quién soy")
def yo(authorization: str | None = Header(default=None)) -> dict:
    _, u = _usuario_del_bearer(authorization)
    return {"usuario": u}


@router.get(
    "/sesion",
    summary="¿Sigue viva la sesión de este JWT? (HU05, 2ª etapa)",
    description=(
        "Lo consulta el guard del front (una vez cada pocos segundos por sesión). Un JWT firmado y vigente NO prueba que su sesión siga "
        "abierta: se pudo cerrar (la persona, un administrador, un restablecimiento de contraseña) o la cuenta "
        "desactivarse, y el JWT vale hasta que venza. Las rutas que valida este back ya lo rechazan solas "
        "(`sesion_cerrada`); las que decide SOLO el front (bandeja, llaves, webhooks, archivos, procesadores) no "
        "pasan por aquí, y para ellas existe esta pregunta. "
        "Responde 200 con `estado`: `viva`, `cerrada` (la sesión se cerró, o no existe: ante la duda se rechaza), "
        "`desactivada` o `inexistente` (la persona ya no existe). Las respuestas definitivas son 200 a propósito: "
        "es una CONSULTA, no un intento de acceso. Un 401 (`sin_sesion`, `sesion_invalida`) o un 503 quieren decir "
        "que NO se pudo contestar (también un 503 si el almacén no está disponible), y quien pregunta no debe "
        "tomarlo como que la sesión murió. Hoy contesta `usuarios.por_guid_y_sesion` sobre el registro provisional; "
        "con la base hará falta una consulta por `sessionGuid` que NO está en lo pedido al DBA (ver "
        "docs/solicitudes-dba.md): `uspGetSession` busca por el hash del refresh, no por el `sid` del JWT."
    ),
)
def estado_de_sesion(response: Response, authorization: str | None = Header(default=None)) -> dict:
    response.headers["Cache-Control"] = "no-store"
    claims = _claims_del_bearer(authorization)
    try:
        u, viva = usuarios.por_guid_y_sesion(claims["sub"], claims["sid"])
    except usuarios.NoEncontrado:
        return {"estado": "inexistente"}
    except ErrorAlmacen as exc:
        # El montaje caído NO es una sesión muerta: 503, para que quien pregunta lo trate
        # como "no se pudo contestar" (y el log diga por qué, en vez de un 500 anónimo).
        raise almacen_no_disponible(exc) from exc
    if not u["activo"]:
        return {"estado": "desactivada"}
    return {"estado": "viva" if viva else "cerrada"}


@router.post(
    "/perfil",
    summary="Actualizar mi perfil (HU12)",
    description=(
        "Nombre, apellidos, teléfono y datos de recuperación de QUIEN LLAMA. El correo con el "
        "que inicia sesión y su rol no se tocan aquí: esos los mueve quien administra."
    ),
)
def actualizar_perfil(datos: Perfil, authorization: str | None = Header(default=None)) -> dict:
    claims, u = _usuario_del_bearer(authorization)
    if not datos.nombre.strip():
        raise _error(status.HTTP_400_BAD_REQUEST, "nombre_requerido", "El nombre es obligatorio.")
    try:
        recuperacion = usuarios.normalizar_recuperacion(
            datos.recuperacion.model_dump() if datos.recuperacion else {}
        )
    except ValueError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, "correo_recuperacion_invalido", str(exc)) from exc
    try:
        actualizado = usuarios.actualizar_perfil(
            u["guid"], datos.nombre, datos.apellidoPaterno, datos.apellidoMaterno, datos.telefono, recuperacion
        )
        logger.info("Perfil actualizado (guid=%s)", u["guid"])
        _auditar_perfil(u["guid"], claims["sid"], _campos_modificados(u, actualizado))
        return {"usuario": actualizado}
    except ValueError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, "datos_invalidos", str(exc)) from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc


@router.post(
    "/contrasena",
    summary="Cambiar la contraseña",
    description=(
        "Primer acceso (`debeCambiarContrasena`): solo `nueva` y `confirmacion`; después se cierran TODAS las "
        "sesiones y hay que volver a entrar. Sesión normal: además `actual`. Reglas: 12 caracteres, mayúscula, "
        "minúscula, número y carácter especial (`codigo: reglas`, con `faltan`)."
    ),
)
def cambiar_contrasena(datos: CambioContrasena, authorization: str | None = Header(default=None)) -> dict:
    claims, u = _usuario_del_bearer(authorization)
    if datos.nueva != datos.confirmacion:
        raise _error(status.HTTP_400_BAD_REQUEST, "no_coincide", "La contraseña no coincide; por favor intenta de nuevo.")
    faltan = auth.reglas_incumplidas(datos.nueva)
    if faltan:
        raise _error(status.HTTP_400_BAD_REQUEST, "reglas", "La contraseña no cumple las reglas de seguridad.", faltan=faltan)
    try:
        completo = usuarios.para_login(u["email"])
        if completo is None:
            raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_invalida", "El usuario ya no existe.")
        if u["debeCambiarContrasena"]:
            if usuarios.acceso_vencido(completo):
                # Entró antes de que caducara y se quedó sin terminar: el primer acceso ya no se puede completar.
                usuarios.revocar_sesiones(u["guid"], None, "ACTIVATION_EXPIRED")
                raise _activacion_vencida()
            motivo = "FIRST_LOGIN"
        else:
            if not datos.actual or not auth.verificar_contrasena(completo["hash"], datos.actual):
                raise _error(status.HTTP_401_UNAUTHORIZED, "actual_incorrecta", "La contraseña actual es incorrecta.")
            motivo = "CHANGE"
        if auth.verificar_contrasena(completo["hash"], datos.nueva):
            raise _error(status.HTTP_400_BAD_REQUEST, "misma", "La nueva contraseña no puede ser igual a la anterior.")
        # Las verificaciones de arriba tardaron y se hicieron fuera del candado: `fijar_contrasena`
        # comprueba otra vez, bajo él, que la sesión siga viva y que la contraseña verificada siga siendo
        # la vigente. Sin eso, un restablecimiento a media petición (que cierra TODAS las sesiones) no
        # impedía que la sesión ya cerrada pusiera su contraseña encima de la del dueño.
        actualizado, cerradas = usuarios.fijar_contrasena_y_cerrar(
            u["guid"], auth.hash_de(datos.nueva), motivo, sesion_guid=claims["sid"], hash_verificado=completo["hash"]
        )
        logger.info("Contraseña cambiada (guid=%s, motivo=%s, sesiones cerradas=%s)", u["guid"], motivo, cerradas)
        todas = motivo in usuarios.MOTIVOS_QUE_CIERRAN_SESIONES
        if motivo == "CHANGE":
            _auditar_cambio_de_contrasena(auditoria.PASSWORD_CHANGED, u["guid"], claims["sid"], otrasSesionesCerradas=cerradas)
        elif motivo == "FIRST_LOGIN":
            _auditar_cambio_de_contrasena(auditoria.FIRST_LOGIN_PASSWORD_CHANGED, u["guid"], claims["sid"], sesionesCerradas=cerradas)
        # `sesionesCerradas`: se cerraron TODAS, la de quien lo pidió incluida (primer acceso): hay que volver a entrar.
        # `otrasSesionesCerradas`: las de los OTROS dispositivos (HU13 CA-4); la de este sigue viva.
        return {"ok": True, "motivo": motivo, "sesionesCerradas": todas, "otrasSesionesCerradas": 0 if todas else cerradas, "usuario": actualizado}
    except usuarios.SesionNoCanjeable as exc:
        if exc.motivo == "cerrada":
            raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_cerrada", "La sesión ya no es válida. Inicia sesión de nuevo.") from exc
        # `contrasena`: otro dispositivo la cambió mientras tanto, y la "actual" que se verificó ya no lo es.
        raise _error(status.HTTP_401_UNAUTHORIZED, "actual_incorrecta", "La contraseña actual es incorrecta.") from exc
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc


# ── Recuperar la contraseña (HU10) ──────────────────────────────────────────
#
# Tres pasos, los tres SIN sesión (quien los usa no puede entrar):
#
#   solicitar  → "olvidé mi contraseña": manda el enlace al correo de la cuenta
#   revisar    → la pantalla del enlace pregunta si todavía sirve
#   confirmar  → pone la contraseña nueva y gasta el enlace
#
# Decisiones que se tomaron aquí y vale la pena que Cesar conozca:
#
# 1. **CA-2 gana sobre CA-3 y sobre el Figma.** CA-2 dice que nunca se revele si
#    una cuenta existe; CA-3 pide ofrecer a quien escribe un correo la opción
#    de elegir entre el principal y el de recuperación, ambos enmascarados. Las
#    dos cosas no caben: una pantalla de elección ES la prueba de que la cuenta
#    existe. Gana la que protege. El enlace va al correo PRINCIPAL y la
#    respuesta es siempre la misma. Por la misma razón NO se dibuja el estado
#    "El correo electrónico no existe" del Figma ni se dice "te enviamos un
#    enlace a X": ambos afirman o niegan que la cuenta existe.
#
#    (Ojo: el login sí distingue "el correo no existe" de "contraseña
#    incorrecta", porque así lo dibujó UX para HU03. Mientras eso siga, este
#    endurecimiento no cierra del todo la enumeración; es una decisión de
#    producto que conviene tomar para las dos pantallas juntas.)
#
# 2. **CA-9 se resuelve en el servidor, no en pantalla.** Una cuenta que nunca
#    ha entrado NO recibe enlace de recuperación, pero la pantalla le responde
#    lo mismo que a todos; lo que cambia es el correo, que le dice que termine
#    su primer acceso. Además es lo único correcto: restablecer una contraseña
#    apagaría en silencio `debeCambiar`.
#
# 3. **CA-7 cierra las sesiones, y desde HU05 también el JWT en este back.**
#    `fijar_contrasena` con motivo RESET revoca todas las sesiones: nadie puede
#    renovar y los guards que validan un Bearer rechazan ya el JWT de esas
#    sesiones (`sesion_cerrada`). Lo que decide solo el front (bandeja, llaves,
#    webhooks...) lo rechaza el guard del front, que pregunta por `GET
#    /auth/sesion`: ver README, "El JWT de acceso y la sesión cerrada".
#
# 4. **El correo se manda DESPUÉS de responder** (`BackgroundTasks`), para que
#    el tiempo de envío no delate si la cuenta existe. Queda una diferencia
#    pequeña (con cuenta se crea un token y se escribe un renglón más) que no
#    se pretende eliminar.


@router.post(
    "/recuperacion/solicitar",
    summary="Pedir un enlace para recuperar la contraseña (HU10)",
    description=(
        "Responde lo mismo (`MENSAJE_NEUTRO`) si el correo tiene forma de correo y no le dice nada de la cuenta, salvo que "
        "no exista: ahí contesta `correo_no_existe` (404) mientras `RECUPERACION_DICE_SI_EXISTE` esté encendida (por "
        "defecto; con `=0` vuelve a contestar neutral, CA-2). "
        "Errores: `correo_invalido` (400), `correo_no_existe` (404), `demasiados_intentos` (429). Si la cuenta existe, está activa y ya "
        "completó su primer acceso, manda el enlace al correo principal; si no, no manda enlace."
    ),
)
def solicitar_recuperacion(datos: SolicitudRecuperacion, response: Response, segundo_plano: BackgroundTasks) -> dict:
    response.headers["Cache-Control"] = "no-store"
    email = datos.email.strip().lower()
    if not usuarios.correo_valido(email):
        raise _error(status.HTTP_400_BAD_REQUEST, "correo_invalido", "Por favor, ingresa un correo electrónico válido.")
    origen = (datos.origen or "").strip() or None
    ip = (datos.ip or "").strip() or None
    try:
        _frenar_recuperacion(email, origen, ip)

        u = usuarios.para_login(email)
        guid = u["guid"] if u else None
        envio = None  # (asunto, texto, html) del correo que toque, si toca alguno
        if u is None:
            resultado = "SIN_CUENTA"
        elif not u["activo"]:
            resultado = "DESACTIVADA"
        elif u["debeCambiarContrasena"]:
            # CA-9: nada de enlace; se le avisa que termine su primer acceso.
            resultado = "PENDIENTE_PRIMER_ACCESO"
            envio = plantillas_correo.primer_acceso_pendiente(u["nombre"])
        else:
            resultado = "ENVIADO"
            token, _ = tokens.crear(u["guid"], tokens.RECUPERACION)
            enlace = f"{config.FRONT_URL}/acceso/restablecer-contrasena?t={token}"
            envio = plantillas_correo.recuperacion(u["nombre"], enlace)

        # CA-10: se anota SIEMPRE, exista la cuenta o no. Es también lo que
        # cuentan los límites. Nunca el token: va solo en el correo.
        auditoria.registrar(
            auditoria.PASSWORD_RESET_REQUESTED,
            usuario_guid=guid,
            email=email,
            origen=origen,
            ip=ip,
            resultado=resultado,
        )
        if resultado == "SIN_CUENTA" and config.RECUPERACION_DICE_SI_EXISTE:
            # Se anotó arriba como siempre; solo cambia lo que se le contesta a quien preguntó. Es el estado de error del
            # Figma («Recuperar contraseña 3») y el CA4 de QA: ver `config.RECUPERACION_DICE_SI_EXISTE`.
            logger.info("Recuperación solicitada (resultado=%s)", resultado)
            raise _error(
                status.HTTP_404_NOT_FOUND,
                "correo_no_existe",
                "El correo electrónico no existe; por favor ingresa un correo electrónico válido.",
            )
        if envio is not None:
            segundo_plano.add_task(_enviar_en_segundo_plano, u["email"], *envio)
        logger.info("Recuperación solicitada (resultado=%s)", resultado)
        return {"ok": True, "mensaje": MENSAJE_NEUTRO}
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc


@router.post(
    "/recuperacion/revisar",
    summary="¿Todavía sirve este enlace de recuperación? (HU10)",
    description=(
        "Comprueba el enlace SIN gastarlo: es lo que decide si la pantalla pinta el formulario o el aviso de "
        "enlace vencido. 410 con `enlace_invalido`, `enlace_usado` o `enlace_vencido` (el mismo mensaje para "
        "los tres). No devuelve de quién es el enlace."
    ),
)
def revisar_enlace(datos: RevisionEnlace, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    try:
        _validar_enlace(datos.token)
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
    return {"ok": True}


@router.post(
    "/recuperacion/confirmar",
    summary="Poner la contraseña nueva con un enlace de recuperación (HU10)",
    description=(
        "Valida la contraseña, gasta el enlace, la cambia y cierra TODAS las sesiones. Errores: `no_coincide` y "
        "`reglas` (400, con `faltan`) y los de enlace (410). Las dos validaciones de la contraseña van ANTES de "
        "gastar el enlace: un typo no debe quemarlo."
    ),
)
def confirmar_recuperacion(datos: ConfirmacionRecuperacion, response: Response) -> dict:
    response.headers["Cache-Control"] = "no-store"
    # Primero lo que se puede corregir sin perder el enlace.
    if datos.nueva != datos.confirmacion:
        raise _error(status.HTTP_400_BAD_REQUEST, "no_coincide", "La contraseña no coincide; por favor intenta de nuevo.")
    faltan = auth.reglas_incumplidas(datos.nueva)
    if faltan:
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "reglas",
            "La contraseña no cumple con el formato requerido; por favor intenta de nuevo.",
            faltan=faltan,
        )
    origen = (datos.origen or "").strip() or None
    try:
        _, u = _validar_enlace(datos.token)
        # AHORA sí se gasta. Es atómico: con dos clics, uno gana y el otro
        # recibe `enlace_usado`.
        try:
            t = tokens.consumir(datos.token, tokens.RECUPERACION)
        except tokens.YaUsado as exc:
            raise _enlace_no_valido("enlace_usado") from exc
        except tokens.Vencido as exc:
            raise _enlace_no_valido("enlace_vencido") from exc
        except tokens.TokenInvalido as exc:
            raise _enlace_no_valido("enlace_invalido") from exc

        # CA-6 y CA-7: la contraseña cambia y, con motivo RESET, se revocan TODAS
        # las sesiones.
        usuarios.fijar_contrasena(u["guid"], auth.hash_de(datos.nueva), "RESET")
        # Cualquier otro enlace pendiente de esa persona (de recuperación o de
        # activación) deja de servir: un correo viejo no debe poder abrir nada.
        tokens.invalidar_de(u["guid"], None, "PASSWORD_RESET")
        # Quien agotó sus cinco intentos y por eso vino aquí no debe toparse
        # con "cuenta bloqueada" al intentar entrar con la contraseña nueva.
        usuarios.reiniciar_intentos_de_origen(origen)
        auditoria.registrar(
            auditoria.PASSWORD_RESET_COMPLETED,
            usuario_guid=u["guid"],
            email=u["email"],
            tokenGuid=t["guid"],
            origen=origen,
            ip=(datos.ip or "").strip() or None,
        )
        logger.info("Contraseña restablecida (guid=%s)", u["guid"])
        return {"ok": True, "sesionesCerradas": True}
    except ErrorAlmacen as exc:
        raise almacen_no_disponible(exc) from exc
