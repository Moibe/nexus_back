"""Acceso: login con JWT, renovación, cierre de sesión y cambio de contraseña.

Sprint 1 — HU01 (bootstrap del super admin: `bootstrap_admin.py`, no un
endpoint), HU03 (login) y HU04 (cambio obligatorio en el primer acceso). Los
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
from datetime import datetime, timezone

from fastapi import APIRouter, Header, HTTPException, Response, status
from pydantic import BaseModel, Field

import config
from servicios import auth, usuarios
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)
router = APIRouter()


class Login(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)
    ip: str | None = Field(default=None, max_length=45)
    userAgent: str | None = Field(default=None, max_length=500)


class Refresh(BaseModel):
    refreshToken: str = Field(max_length=128)
    ip: str | None = Field(default=None, max_length=45)
    userAgent: str | None = Field(default=None, max_length=500)


class Logout(BaseModel):
    refreshToken: str | None = Field(default=None, max_length=128)


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


def _error(status_code: int, codigo: str, detail: str, **extra) -> HTTPException:
    return HTTPException(status_code=status_code, detail={"codigo": codigo, "mensaje": detail, **extra})


def _sin_configurar() -> HTTPException:
    return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "sin_configurar", "El acceso no está configurado en el servidor (AUTH_JWT_SECRET).")


def _sesion_nueva(usuario: dict, ip: str | None, user_agent: str | None) -> dict:
    refresh, refresh_hash, vence_refresh = auth.nuevo_refresh()
    sesion_guid = usuarios.crear_sesion(usuario["guid"], refresh_hash, vence_refresh, ip, user_agent)
    acceso, vence_acceso = auth.emitir_acceso(usuario, sesion_guid)
    return {
        "accessToken": acceso,
        "accessExpiraEn": vence_acceso.isoformat(timespec="seconds"),
        "refreshToken": refresh,
        "refreshExpiraEn": vence_refresh.isoformat(timespec="seconds"),
        "usuario": usuario,
    }


def _segundos_hasta(iso: str) -> int:
    hasta = datetime.fromisoformat(iso)
    return max(0, int((hasta - datetime.now(timezone.utc)).total_seconds()))


@router.post(
    "/login",
    summary="Iniciar sesión",
    description=(
        "Correo y contraseña → JWT de acceso + refresh token. Errores con `codigo`: "
        "`correo_invalido` (400), `correo_no_existe` y `credenciales` (401, con `intentosRestantes`), "
        "`bloqueada` (423, con `hastaEn` y `segundosRestantes`), `desactivada` (403)."
    ),
)
def login(datos: Login, response: Response) -> dict:
    if not config.AUTH_JWT_SECRET:
        raise _sin_configurar()
    response.headers["Cache-Control"] = "no-store"
    email = datos.email.strip().lower()
    if not usuarios.correo_valido(email):
        raise _error(status.HTTP_400_BAD_REQUEST, "correo_invalido", "Por favor, ingresa un correo electrónico válido.")
    try:
        u = usuarios.para_login(email)
        if u is None:
            usuarios.registrar_resultado_login(None, email, False, "UNKNOWN_EMAIL", datos.ip, datos.userAgent)
            raise _error(status.HTTP_401_UNAUTHORIZED, "correo_no_existe", "El correo electrónico no existe; por favor ingresa un correo electrónico válido.")
        if u["bloqueadoHasta"]:
            usuarios.registrar_resultado_login(u["guid"], email, False, "LOCKED", datos.ip, datos.userAgent)
            raise _error(
                status.HTTP_423_LOCKED, "bloqueada",
                "Tu cuenta ha sido bloqueada temporalmente por exceder el número máximo de intentos permitidos.",
                hastaEn=u["bloqueadoHasta"], segundosRestantes=_segundos_hasta(u["bloqueadoHasta"]),
            )
        if not u["activo"]:
            usuarios.registrar_resultado_login(u["guid"], email, False, "INACTIVE", datos.ip, datos.userAgent)
            raise _error(status.HTTP_403_FORBIDDEN, "desactivada", "La cuenta con la que intentas ingresar ha sido desactivada.")
        if not auth.verificar_contrasena(u["hash"], datos.password):
            fallidos, bloqueado_hasta = usuarios.registrar_resultado_login(u["guid"], email, False, "BAD_PASSWORD", datos.ip, datos.userAgent)
            if bloqueado_hasta:
                raise _error(
                    status.HTTP_423_LOCKED, "bloqueada",
                    "Tu cuenta ha sido bloqueada temporalmente por exceder el número máximo de intentos permitidos.",
                    hastaEn=bloqueado_hasta, segundosRestantes=_segundos_hasta(bloqueado_hasta),
                )
            raise _error(
                status.HTTP_401_UNAUTHORIZED, "credenciales",
                "La contraseña es incorrecta; por favor intenta de nuevo.",
                intentosRestantes=config.AUTH_MAX_INTENTOS - fallidos,
            )
        usuarios.registrar_resultado_login(u["guid"], email, True, None, datos.ip, datos.userAgent)
        publico = {k: v for k, v in u.items() if k not in ("hash", "fallidos", "bloqueadoHasta")}
        publico["ultimoLoginEn"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        logger.info("Login correcto (guid=%s)", u["guid"])
        return _sesion_nueva(publico, datos.ip, datos.userAgent)
    except ErrorAlmacen as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
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
        s = usuarios.sesion_por_hash(auth.hash_refresh(datos.refreshToken))
        if s is None or s["revocadaEn"] or not s["usuario"] or not s["usuario"]["activo"]:
            raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_invalida", "La sesión ya no es válida. Inicia sesión de nuevo.")
        if datetime.fromisoformat(s["expira"]) <= datetime.now(timezone.utc):
            raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_vencida", "La sesión venció. Inicia sesión de nuevo.")
        usuarios.revocar_sesiones(s["guid"], s["sesionGuid"], "ROTATED")
        return _sesion_nueva(s["usuario"], datos.ip, datos.userAgent)
    except ErrorAlmacen as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    except auth.SinConfigurar as exc:
        raise _sin_configurar() from exc


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, summary="Cerrar sesión")
def logout(datos: Logout) -> Response:
    """Revoca la sesión del refresh token. Sin token, o con uno que ya no
    existe, responde 204 igual: cerrar sesión nunca falla hacia el usuario."""
    if datos.refreshToken:
        try:
            s = usuarios.sesion_por_hash(auth.hash_refresh(datos.refreshToken))
            if s and not s["revocadaEn"]:
                usuarios.revocar_sesiones(s["guid"], s["sesionGuid"], "LOGOUT")
        except ErrorAlmacen as exc:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)


def _usuario_del_bearer(authorization: str | None) -> tuple[dict, dict]:
    """(claims, usuario) del JWT de acceso, o 401."""
    if not authorization or not authorization.lower().startswith("bearer "):
        raise _error(status.HTTP_401_UNAUTHORIZED, "sin_sesion", "Falta el token de acceso.")
    try:
        claims = auth.verificar_acceso(authorization[7:].strip())
    except auth.SinConfigurar as exc:
        raise _sin_configurar() from exc
    if not claims:
        raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_invalida", "El token de acceso no es válido o venció.")
    try:
        u = usuarios.por_guid(claims["sub"])
    except usuarios.NoEncontrado as exc:
        raise _error(status.HTTP_401_UNAUTHORIZED, "sesion_invalida", "El usuario ya no existe.") from exc
    if not u["activo"]:
        raise _error(status.HTTP_403_FORBIDDEN, "desactivada", "La cuenta con la que intentas ingresar ha sido desactivada.")
    return claims, u


@router.get("/yo", summary="Quién soy")
def yo(authorization: str | None = Header(default=None)) -> dict:
    _, u = _usuario_del_bearer(authorization)
    return {"usuario": u}


@router.post(
    "/perfil",
    summary="Actualizar mi perfil (HU12)",
    description=(
        "Nombre, apellidos, teléfono y datos de recuperación de QUIEN LLAMA. El correo con el "
        "que inicia sesión y su rol no se tocan aquí: esos los mueve quien administra."
    ),
)
def actualizar_perfil(datos: Perfil, authorization: str | None = Header(default=None)) -> dict:
    _, u = _usuario_del_bearer(authorization)
    if not datos.nombre.strip():
        raise _error(status.HTTP_400_BAD_REQUEST, "nombre_requerido", "El nombre es obligatorio.")
    recuperacion = datos.recuperacion.model_dump() if datos.recuperacion else {}
    correo_rec = (recuperacion.get("email") or "").strip()
    if correo_rec and not usuarios.correo_valido(correo_rec):
        raise _error(
            status.HTTP_400_BAD_REQUEST,
            "correo_recuperacion_invalido",
            "El correo de recuperación no es válido.",
        )
    try:
        actualizado = usuarios.actualizar_perfil(
            u["guid"], datos.nombre, datos.apellidoPaterno, datos.apellidoMaterno, datos.telefono, recuperacion
        )
        logger.info("Perfil actualizado (guid=%s)", u["guid"])
        return {"usuario": actualizado}
    except ValueError as exc:
        raise _error(status.HTTP_400_BAD_REQUEST, "datos_invalidos", str(exc)) from exc
    except ErrorAlmacen as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc


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
    _, u = _usuario_del_bearer(authorization)
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
            motivo = "FIRST_LOGIN"
        else:
            if not datos.actual or not auth.verificar_contrasena(completo["hash"], datos.actual):
                raise _error(status.HTTP_401_UNAUTHORIZED, "actual_incorrecta", "La contraseña actual es incorrecta.")
            motivo = "CHANGE"
        if auth.verificar_contrasena(completo["hash"], datos.nueva):
            raise _error(status.HTTP_400_BAD_REQUEST, "misma", "La nueva contraseña no puede ser igual a la anterior.")
        actualizado = usuarios.fijar_contrasena(u["guid"], auth.hash_de(datos.nueva), motivo)
        logger.info("Contraseña cambiada (guid=%s, motivo=%s)", u["guid"], motivo)
        return {"ok": True, "motivo": motivo, "sesionesCerradas": motivo in usuarios.MOTIVOS_QUE_CIERRAN_SESIONES, "usuario": actualizado}
    except ErrorAlmacen as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)) from exc
