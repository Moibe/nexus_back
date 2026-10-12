"""Los textos de los correos de la recuperación de contraseña (HU10).

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


def _html(titulo: str, parrafos: list[str], enlace: str | None, boton: str | None, cierre: list[str]) -> str:
    """Una plantilla sencilla, con estilos en línea: los clientes de correo no
    respetan hojas de estilo externas ni casi nada de CSS moderno."""
    partes = [
        '<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;color:#1f2937;line-height:1.5">',
        f'<h1 style="font-size:24px;margin:24px 0 8px">{escape(titulo)}</h1>',
        f'<p style="color:#6b7280;margin:0 0 24px">{escape(_ENCABEZADO)}</p>',
    ]
    for p in parrafos:
        partes.append(f'<p style="margin:0 0 16px">{escape(p)}</p>')
    if enlace and boton:
        partes.append(
            f'<p style="margin:24px 0"><a href="{escape(enlace, quote=True)}" '
            'style="background:#2563eb;color:#ffffff;text-decoration:none;padding:12px 24px;'
            f'border-radius:8px;display:inline-block;font-weight:bold">{escape(boton)}</a></p>'
        )
        partes.append(
            '<p style="font-size:12px;color:#6b7280;margin:0 0 16px">Si el botón no abre, copia y pega '
            f'este enlace en tu navegador:<br><span style="word-break:break-all">{escape(enlace)}</span></p>'
        )
    for p in cierre:
        partes.append(f'<p style="margin:0 0 16px">{escape(p)}</p>')
    soporte = _soporte()
    if soporte:
        partes.append('<p style="margin:0 0 16px">' + "<br>".join(escape(s) for s in soporte) + "</p>")
    partes.append('<p style="margin:24px 0 0">' + "<br>".join(escape(s) for s in _FIRMA) + "</p>")
    partes.append(
        '<hr style="border:none;border-top:1px solid #e5e7eb;margin:24px 0">'
        '<p style="font-size:12px;color:#6b7280;margin:0 0 8px">' + "<br>".join(escape(s) for s in _SEGURIDAD) + "</p>"
        f'<p style="font-size:12px;color:#6b7280;margin:0 0 8px">{escape(_AUTOMATICO)}</p>'
        f'<p style="font-size:12px;color:#6b7280;margin:0">{escape(_DERECHOS)}</p>'
    )
    partes.append("</div>")
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
