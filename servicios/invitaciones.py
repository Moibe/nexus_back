"""La invitación: el correo de bienvenida con los accesos temporales (HU01, HU02, HU06).

Quien da de alta a alguien (un administrador, el administrador de plataforma, el script de bootstrap) no sabe
de plantillas ni de canales: llama a `enviar_bienvenida` y recibe una de tres respuestas.

    ENVIADO    el correo salió de verdad y la persona ya tiene sus accesos en su bandeja: NO hay que
               enseñarle la contraseña temporal a quien la dio de alta (viaja solo por correo).
    SIN_CANAL  este ambiente no manda correo de verdad (`archivo`, `silencio`): los accesos se entregan en
               pantalla, como antes. No se escribe un `.eml` con la contraseña: en un servidor sería una
               contraseña en claro, guardada, en el NAS.
    FALLO      había canal y no se pudo (el servicio no contestó, rechazó el envío...): igual que SIN_CANAL,
               los accesos se entregan en pantalla, y la pantalla avisa que el correo no salió.

Se manda EN LÍNEA, no en segundo plano, a propósito: hace falta saber si salió para decidir si se muestra la
contraseña. Para que una caída del servicio no deje a nadie esperando medio minuto, la espera se acota.
"""

import logging

import config
from servicios import auditoria, auth, correo, plantillas_correo, roles, tenants_registro, usuarios
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

ENVIADO = "enviado"
SIN_CANAL = "sin_canal"
FALLO = "fallo"

#: Cuánto espera, como máximo, CADA llamada al servicio de correo (pide token y manda: dos llamadas).
ESPERA_MAX_S = 10


def enlace_de_acceso() -> str:
    """La pantalla de inicio de sesión del front de este ambiente (`FRONT_URL`)."""
    return f"{config.FRONT_URL}/acceso/iniciar-sesion"


def enviar_bienvenida(email: str, nombre: str, contrasena: str, perfil: str, organizacion: str | None = None) -> str:
    """Manda la bienvenida con los accesos temporales. Devuelve `ENVIADO`, `SIN_CANAL` o `FALLO`; nunca
    levanta. En el log queda a quién y el resultado, jamás el cuerpo (lleva la contraseña)."""
    if not correo.canal_real():
        logger.info("Bienvenida a %s: sin canal de correo real (modo %s); los accesos se entregan en pantalla", email, correo.modo())
        return SIN_CANAL
    asunto, texto, html = plantillas_correo.bienvenida(nombre, email, contrasena, enlace_de_acceso(), perfil, organizacion)
    try:
        correo.enviar(email, asunto, texto, html, timeout=ESPERA_MAX_S)
    except correo.ErrorCorreo as exc:
        logger.warning("No se pudo mandar la bienvenida a %s: %s", email, exc)
        return FALLO
    logger.info("Bienvenida enviada a %s (perfil %s)", email, perfil)
    return ENVIADO


def auditar_alta(actor_guid: str | None, usuario_guid: str, rol: str, tenant_guid: str | None, resultado: str) -> None:
    """Deja el evento `USER_CREATED` (HU06 CA-1 y CA-7): el usuario creado, su rol, quién lo dio de alta y
    qué pasó con su correo. Va DESPUÉS de crear y sin poder romper el alta: si la bitácora falla se avisa en
    el log y se sigue (un 503 aquí dejaría una cuenta creada y a su administrador creyendo que no)."""
    try:
        auditoria.registrar(
            auditoria.USER_CREATED,
            usuario_guid=usuario_guid,
            rol=rol,
            tenantGuid=tenant_guid,
            actor=actor_guid,
            correo=resultado,
        )
    except (ErrorAlmacen, OSError) as exc:
        logger.warning("No se pudo registrar USER_CREATED en la bitácora de auditoría: %s", exc)


def para_respuesta(resultado: str, contrasena: str) -> dict:
    """Lo que el alta le devuelve a quien la pidió: si el correo salió, y la contraseña temporal SOLO si no
    salió (si salió, la persona la tiene en su bandeja y quien la dio de alta no tiene por qué verla)."""
    salio = resultado == ENVIADO
    cuerpo: dict = {"correoEnviado": salio, "correoMotivo": None if salio else resultado}
    if not salio:
        cuerpo["contrasenaTemporal"] = contrasena
    return cuerpo


def perfil_de(u: dict) -> tuple[str, str | None]:
    """El perfil de bienvenida que le toca a una cuenta (`plantillas_correo.PERFILES_BIENVENIDA`) y, si es el
    administrador de una organización, el nombre de esa organización."""
    if u["esAdminPlataforma"]:
        return "super_admin", None
    propia = next((m for m in u["membresias"] if m["rol"] == roles.ADMIN), None)
    if propia is None:
        return "usuario", None
    try:
        return "admin_organizacion", tenants_registro.por_guid(propia["tenantGuid"])["nombre"]
    except tenants_registro.NoEncontrado:
        return "admin_organizacion", None


def por_que_no_reenviar(objetivo: dict) -> tuple[str, str] | None:
    """`(codigo, mensaje)` si a esta cuenta NO se le puede reenviar la invitación, o `None` si se puede. `objetivo` es
    lo que devuelve `usuarios.por_guid`. Solo tiene invitación que reenviar quien sigue en su primer acceso, y a una
    cuenta desactivada no se le manda nada hasta que se reactive."""
    if not objetivo["activo"]:
        return "desactivada", "La cuenta está desactivada: reactívala antes de reenviar su invitación."
    if not objetivo["debeCambiarContrasena"]:
        return "ya_activa", "Este usuario ya hizo su primer acceso: no hay invitación que reenviar."
    return None


def reenviar(guid: str, actor_guid: str | None) -> tuple[str, str]:
    """Le genera a una cuenta que sigue en su primer acceso una contraseña temporal NUEVA, invalida la anterior
    (y las sesiones que se hubieran abierto con ella), empieza otra vez las horas de vigencia y le manda la
    bienvenida. Devuelve `(resultado, contrasena)`: el resultado es el de `enviar_bienvenida`, y la contraseña
    la usa quien llama para entregarla a mano si el correo no salió.

    Es lo que hace falta cuando la contraseña caducó (HU06 CA-5) o se perdió, y el núcleo del reenvío de la
    invitación (CA-6). Levanta `usuarios.NoEncontrado` o `usuarios.NoPendiente` (ya hizo su primer acceso)."""
    temporal = auth.contrasena_temporal()
    nueva = usuarios.emitir_temporal(guid, auth.hash_de(temporal))
    perfil, organizacion = perfil_de(nueva)
    resultado = enviar_bienvenida(nueva["email"], nueva["nombre"], temporal, perfil, organizacion)
    try:
        auditoria.registrar(auditoria.INVITATION_REISSUED, usuario_guid=guid, actor=actor_guid, correo=resultado)
    except (ErrorAlmacen, OSError) as exc:
        logger.warning("No se pudo registrar INVITATION_REISSUED en la bitácora de auditoría: %s", exc)
    return resultado, temporal
