"""Los textos de los correos: la recuperación de contraseña (HU10) y la bienvenida con accesos temporales
(HU01, HU02 y HU06; ver `bienvenida` al final).

`servicios/correo.py` solo transporta; aquí viven los textos, porque cada
correo trae su propio asunto, su destinatario y sus datos.

Los textos son LITERALES del diseño (Figma, página Sprint 1, sección HU010,
plantilla `Mail | Ne | P1`, node `303:4601`). Tres desviaciones conscientes:

1. **La URL visible lleva el token.** El mockup muestra
   `https://app.nexusdoc.com/restablecer-contrasena` (un dominio que no es el
   nuestro y sin token) sobre un botón que sí lo lleva. Enseñar una URL y
   abrir otra es justo el patrón que los filtros antiphishing castigan. Aquí la
   línea visible es el enlace real, para copiar y pegar si el botón no abre.
2. **El bloque de soporte es opcional.** El mockup trae
   `soporte@nexusdoc.com` y un teléfono de ejemplo: datos que no son nuestros.
   Solo se imprime si `CORREO_SOPORTE` está configurado.
3. **La vigencia no se dice en número.** El diseño dice "por un tiempo
   limitado" y se respeta; la vigencia real (1 hora) la aplica
   `servicios/tokens.py`.

El nombre de la persona y el enlace se escapan en la versión HTML: el nombre
lo escribió un administrador y no debe poder meter marcado en un correo.
"""

from html import escape

import config

ASUNTO_RECUPERACION = "Restablece tu contraseña"
ASUNTO_PRIMER_ACCESO = "Tu primer acceso a NexusDoc sigue pendiente"
ASUNTO_BIENVENIDA = "Bienvenido a NexusDoc"

_ENCABEZADO = "En NexusDoc nos preocupamos por tu seguridad y por brindarte la mejor experiencia en todo momento."
_CIERRE = (
    "En NexusDoc trabajamos para brindarte una experiencia segura, ágil y eficiente en la gestión documental, "
    "protegiendo en todo momento la información de tus procesos y expedientes."
)
_FIRMA = ("Atentamente,", "Equipo NexusDoc", "Transformando la gestión documental con inteligencia y seguridad.")
_SEGURIDAD = (
    "Por tu seguridad, no compartas este correo con nadie.",
    "NexusDoc nunca te pedirá tu contraseña por correo o mensaje.",
)
_AUTOMATICO = "Este correo fue generado automáticamente por NexusDoc. Por favor, no respondas a este mensaje."
_DERECHOS = "© NexusDoc. Todos los derechos reservados."


def _soporte() -> list[str]:
    """El bloque de contacto, o nada si no está configurado."""
    if not config.CORREO_SOPORTE:
        return []
    lineas = ["Contactar soporte", config.CORREO_SOPORTE]
    if config.CORREO_SOPORTE_TELEFONO:
        lineas.append(config.CORREO_SOPORTE_TELEFONO)
    return lineas


# Colores de la plantilla `Mail | Ne | P1` del Figma, medidos de la captura que compartió QA (2026-10-20). Los clientes de
# correo no entienden variables CSS ni casi nada moderno: todo va en línea y la estructura es una tabla de ancho fijo.
_AZUL_MARCA = "#4268fb"
_MARINO = "#03031f"


def _abrir_html(titulo: str, subtitulo: str) -> str:
    """El encabezado del diseño (franja marino con el logo, el título en blanco y el subtítulo), la franja azul que
    hace de la fotografía de carpetas y la tarjeta blanca donde empieza el texto. `_pie_html` cierra todo."""
    return (
        '<div style="background:#f1f4f9;padding:24px 12px;font-family:Arial,Helvetica,sans-serif;color:#1f2937;'
        'line-height:1.5">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" border="0" '
        'style="max-width:640px;margin:0 auto"><tr><td>'
        f'<div style="background:{_MARINO};padding:28px 32px 32px;text-align:center;border-radius:12px 12px 0 0">'
        '<p style="margin:0 0 24px"><span style="display:inline-block;width:30px;height:30px;line-height:30px;'
        f'border-radius:8px;background:{_AZUL_MARCA};color:#ffffff;font-weight:bold;font-size:16px;'
        'vertical-align:middle">N</span><span style="color:#ffffff;font-size:20px;font-weight:bold;'
        'vertical-align:middle;margin-left:8px">NexusDoc</span></p>'
        f'<h1 style="font-size:28px;line-height:1.25;margin:0 0 12px;color:#ffffff">{escape(titulo)}</h1>'
        f'<p style="font-size:14px;margin:0;color:#d5dcec">{escape(subtitulo)}</p></div>'
        # La fotografía del diseño no es nuestra: una franja con el azul de la marca ocupa su lugar.
        f'<div style="height:96px;background:{_AZUL_MARCA};'
        'background:linear-gradient(100deg,#c6dcf7 0%,#6f98ee 45%,#1c3fd2 100%)"></div>'
        '<div style="background:#ffffff;padding:32px 32px 8px">'
    )


def _parrafo_html(texto: str) -> str:
    return f'<p style="margin:0 0 16px;font-size:14px">{escape(texto)}</p>'


def _boton_html(enlace: str, boton: str) -> str:
    return (
        f'<p style="margin:24px 0"><a href="{escape(enlace, quote=True)}" '
        f'style="background:{_AZUL_MARCA};color:#ffffff;text-decoration:none;padding:16px 24px;'
        f'border-radius:8px;display:inline-block;font-weight:bold;font-size:16px">{escape(boton)} &#8599;</a></p>'
        '<p style="font-size:12px;color:#6b7280;margin:0 0 16px">Si el botón no abre, copia y pega '
        f'este enlace en tu navegador:<br><span style="word-break:break-all">{escape(enlace)}</span></p>'
    )


def _pie_html() -> str:
    """Soporte (si hay) y firma, y el pie del diseño: aviso de seguridad sobre marino y la pleca azul con los derechos.
    Cierra los elementos que abre `_abrir_html`."""
    partes = []
    soporte = _soporte()
    if soporte:
        partes.append('<p style="margin:0 0 16px;font-size:14px">' + "<br>".join(escape(s) for s in soporte) + "</p>")
    partes.append('<p style="margin:24px 0 24px;font-size:14px">' + "<br>".join(escape(s) for s in _FIRMA) + "</p>")
    partes.append(
        "</div>"  # la tarjeta blanca
        f'<div style="background:{_MARINO};padding:28px 32px;color:#ffffff;font-size:12px">'
        '<p style="margin:0 0 12px;color:#ffffff">' + "<br>".join(escape(s) for s in _SEGURIDAD) + "</p>"
        f'<p style="margin:0;color:#d5dcec">{escape(_AUTOMATICO)}</p></div>'
        f'<div style="background:{_AZUL_MARCA};padding:16px 32px;color:#ffffff;font-size:12px;'
        f'border-radius:0 0 12px 12px">{escape(_DERECHOS)}</div>'
        "</td></tr></table></div>"
    )
    return "".join(partes)


def _html(titulo: str, parrafos: list[str], enlace: str | None, boton: str | None, cierre: list[str]) -> str:
    """Una plantilla sencilla, con estilos en línea: los clientes de correo no
    respetan hojas de estilo externas ni casi nada de CSS moderno."""
    partes = [_abrir_html(titulo, _ENCABEZADO)]
    partes.extend(_parrafo_html(p) for p in parrafos)
    if enlace and boton:
        partes.append(_boton_html(enlace, boton))
    partes.extend(_parrafo_html(p) for p in cierre)
    partes.append(_pie_html())
    return "".join(partes)


def _texto(titulo: str, parrafos: list[str], enlace: str | None, boton: str | None, cierre: list[str]) -> str:
    bloques = [titulo, _ENCABEZADO, *parrafos]
    if enlace and boton:
        bloques.append(f"{boton}:\n{enlace}")
    bloques.extend(cierre)
    soporte = _soporte()
    if soporte:
        bloques.append("\n".join(soporte))
    bloques.append("\n".join(_FIRMA))
    bloques.append("\n".join(_SEGURIDAD))
    bloques.append(_AUTOMATICO)
    bloques.append(_DERECHOS)
    return "\n\n".join(bloques) + "\n"


def recuperacion(nombre: str, enlace: str) -> tuple[str, str, str]:
    """El correo con el enlace de un solo uso. Devuelve `(asunto, texto, html)`.

    `enlace` ya trae el token: viaja SOLO en el cuerpo de este correo, y el
    cuerpo nunca se registra (`servicios/correo.py`)."""
    parrafos = [
        f"Hola, {nombre}.",
        "Recibimos una solicitud para restablecer la contraseña de tu cuenta en NexusDoc. Para mantener la "
        "seguridad de tu información y garantizar el acceso protegido a la plataforma, puedes crear una nueva "
        "contraseña utilizando el siguiente enlace:",
    ]
    # Lo que va DESPUÉS del botón, en el orden del diseño.
    cierre = [
        "Por motivos de seguridad, este enlace estará disponible por un tiempo limitado. Si no realizaste esta "
        "solicitud, puedes ignorar este mensaje. Tu cuenta y la información almacenada dentro de NexusDoc "
        "permanecerán protegidas y no se realizará ningún cambio. Si necesitas ayuda o presentas algún "
        "inconveniente para acceder a tu cuenta, nuestro equipo estará disponible para apoyarte.",
        _CIERRE,
    ]
    boton = "Restablecer contraseña"
    return (
        ASUNTO_RECUPERACION,
        _texto(ASUNTO_RECUPERACION, parrafos, enlace, boton, cierre),
        _html(ASUNTO_RECUPERACION, parrafos, enlace, boton, cierre),
    )


def primer_acceso_pendiente(nombre: str) -> tuple[str, str, str]:
    """Para quien pide recuperar una cuenta que todavía no ha entrado nunca
    (HU10, CA-9). NO lleva enlace de recuperación: restablecer la contraseña de
    una cuenta sin activar apagaría en silencio la obligación del primer
    acceso. Se manda este aviso en lugar del enlace, y a la pantalla le
    responde lo mismo que a todos, para que ella no delate el estado de la
    cuenta."""
    parrafos = [
        f"Hola, {nombre}.",
        "Recibimos una solicitud para restablecer la contraseña de tu cuenta en NexusDoc, pero tu cuenta todavía "
        "no completa su primer acceso, así que no es posible restablecerla por este medio.",
        "Para continuar, entra con la contraseña temporal que te entregó la persona que dio de alta tu cuenta y "
        "configura tu nueva contraseña. Si no la tienes o ya no funciona, pídesela a esa persona.",
        "Si no realizaste esta solicitud, puedes ignorar este mensaje; no se realizó ningún cambio en tu cuenta.",
    ]
    return (
        ASUNTO_PRIMER_ACCESO,
        _texto(ASUNTO_PRIMER_ACCESO, parrafos, None, None, []),
        _html(ASUNTO_PRIMER_ACCESO, parrafos, None, None, []),
    )


# ── Bienvenida con accesos temporales (HU01, HU02, HU06) ─────────────────────
#
# Texto LITERAL del diseño (Figma, plantilla `Mail | Ne | P1` de las secciones HU001, HU002 y HU006): el
# mismo correo con tres presentaciones, según quién recibe la cuenta. Desviaciones conscientes:
#
# 1. **El enlace es el real.** El mockup trae una URL de ejemplo con parámetros inventados (`session_id`,
#    `ts`...). Aquí la línea visible y el botón llevan la misma dirección: la pantalla de inicio de sesión.
# 2. **El bloque de soporte es opcional**, como en la recuperación (`CORREO_SOPORTE`).
# 3. **La contraseña va en el cuerpo, como dibuja el diseño.** Por eso el cuerpo jamás se registra
#    (`servicios/correo.py`) y por eso la cuenta nace obligada a cambiarla en el primer acceso.

_SUBTITULO_BIENVENIDA = (
    "Nos complace darte la bienvenida a la plataforma inteligente diseñada para centralizar, validar, gestionar "
    "y automatizar de forma eficiente expedientes digitales."
)
_PORTAL = "Te damos la bienvenida al Portal Administrativo NexusDoc."
_PRESENTACION_SUPER_ADMIN = (
    f"{_PORTAL} Como Super Administrador, ahora cuentas con acceso al entorno de administración principal para "
    "supervisar operaciones, usuarios y flujos documentales de forma segura y eficiente."
)
_PRESENTACION_USUARIO = (
    f"{_PORTAL} Tu cuenta ha sido creada exitosamente por el administrador de tu organización y ya cuentas con "
    "acceso a la plataforma. A partir de este momento podrás ingresar y utilizar las funcionalidades disponibles "
    "de acuerdo con los permisos y responsabilidades asignados a tu perfil."
)
PERFILES_BIENVENIDA = ("super_admin", "admin_organizacion", "usuario")


def _presentacion(perfil: str, organizacion: str | None) -> str:
    if perfil == "super_admin":
        return _PRESENTACION_SUPER_ADMIN
    if perfil == "admin_organizacion":
        return (
            f"{_PORTAL} Tu cuenta como administrador de {organizacion or 'tu organización'} ha sido creada "
            "correctamente y ya cuentas con acceso a las herramientas necesarias para la gestión, revisión y "
            "seguimiento de expedientes dentro de la plataforma."
        )
    if perfil == "usuario":
        return _PRESENTACION_USUARIO
    raise ValueError(f"Perfil de bienvenida desconocido: {perfil!r}")


def bienvenida(
    nombre: str, usuario: str, contrasena: str, enlace: str, perfil: str, organizacion: str | None = None
) -> tuple[str, str, str]:
    """El correo de bienvenida con los accesos temporales. Devuelve `(asunto, texto, html)`.

    `perfil` es uno de `PERFILES_BIENVENIDA`; `organizacion` solo cambia el texto del administrador de una
    organización. `contrasena` viaja SOLO en el cuerpo de este correo. Todo lo que escribió una persona
    (nombre, organización, correo) se escapa en el HTML; la contraseña también, porque su alfabeto incluye
    `&`."""
    presentacion = _presentacion(perfil, organizacion)
    saludo = f"Hola, {nombre}."
    pide = "A continuación, encontrarás tus datos de acceso:"
    recomienda = "Por seguridad, te recomendamos cambiar tu contraseña después de iniciar sesión por primera vez."
    enlace_intro = "Puedes iniciar sesión en el siguiente enlace:"
    apoyo = "Si requieres apoyo adicional, nuestro equipo estará disponible para acompañarte en cualquier paso que des."
    boton = "Ir a portal Administrador"
    subtitulo_accesos = "Tus accesos temporales"

    caja = (
        '<div style="background:#f3f4f6;border-radius:8px;padding:16px;margin:0 0 16px">'
        f'<p style="margin:0 0 8px">Usuario: <strong>{escape(usuario)}</strong></p>'
        '<p style="margin:0">Contraseña temporal: <strong style="font-family:Consolas,Menlo,monospace;'
        f'font-size:16px;letter-spacing:.5px">{escape(contrasena)}</strong></p></div>'
    )
    html = "".join(
        [
            _abrir_html(ASUNTO_BIENVENIDA, _SUBTITULO_BIENVENIDA),
            _parrafo_html(saludo),
            _parrafo_html(presentacion),
            f'<h2 style="font-size:18px;margin:24px 0 8px">{escape(subtitulo_accesos)}</h2>',
            _parrafo_html(pide),
            caja,
            _parrafo_html(recomienda),
            _parrafo_html(enlace_intro),
            _boton_html(enlace, boton),
            _parrafo_html(apoyo),
            _parrafo_html(_CIERRE),
            _pie_html(),
        ]
    )
    bloques = [
        ASUNTO_BIENVENIDA,
        _SUBTITULO_BIENVENIDA,
        saludo,
        presentacion,
        subtitulo_accesos,
        pide,
        f"Usuario: {usuario}\nContraseña temporal: {contrasena}",
        recomienda,
        f"{enlace_intro}\n{enlace}",
        apoyo,
        _CIERRE,
    ]
    soporte = _soporte()
    if soporte:
        bloques.append("\n".join(soporte))
    bloques.extend(["\n".join(_FIRMA), "\n".join(_SEGURIDAD), _AUTOMATICO, _DERECHOS])
    return ASUNTO_BIENVENIDA, "\n\n".join(bloques) + "\n", html
