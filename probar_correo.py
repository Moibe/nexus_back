"""Manda UN correo de prueba por el canal que dice el .env (`CORREO_MODO`), para comprobar de verdad el servicio.

    .venv/Scripts/python probar_correo.py tu.correo@grupocsi.com

Lee el mismo .env que la app (corre en el servidor, o en tu máquina con las variables puestas). En modo
`api` pide un token y manda el correo por el servicio de ART Broker; en `archivo` lo deja en
`<ALMACEN_RUTA>/correos/`; en `smtp`, por el relay. Imprime si salió y por dónde, o por qué no: nunca el
usuario, la contraseña ni el token.

UN correo por corrida, a un buzón TUYO: no lo metas en un ciclo ni lo apuntes a otra persona. El servicio
tiene límites de envío que todavía no conocemos.
"""

import sys

import config  # noqa: F401  (carga el .env)
from servicios import correo


def main() -> int:
    if len(sys.argv) != 2 or "@" not in sys.argv[1]:
        print("Uso: python probar_correo.py destinatario@ejemplo.com")
        return 2
    destino = sys.argv[1].strip()
    print(f"Modo: {correo.modo()} · configurado: {'sí' if correo.esta_configurado() else 'NO'}")
    if not correo.esta_configurado():
        print("Falta configuración: ver la sección Correo de .env.example.")
        return 1
    try:
        donde = correo.enviar(
            destino,
            "Prueba de NexusDoc",
            "Este es un correo de prueba de NexusDoc. Si lo lees, el canal de correo funciona.",
            "<p>Este es un correo de <b>prueba</b> de NexusDoc.</p><p>Si lo lees, el canal de correo funciona.</p>",
        )
    except correo.ErrorCorreo as exc:
        print(f"NO salió: {exc}")
        return 1
    print(f"Salió por: {donde}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
