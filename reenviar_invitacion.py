"""HU06 CA-5/CA-6 — Le genera una contraseña temporal NUEVA a una cuenta que sigue en su primer acceso.

    .venv/Scripts/python reenviar_invitacion.py --email alguien@grupocsi.com

La contraseña temporal del primer acceso caduca (`TOKEN_ACTIVACION_HORAS`, 48 por default) y quien no entró a tiempo
se queda sin forma de entrar: su correo ya está registrado, así que no se le puede dar de alta otra vez. Este script
le pone una contraseña nueva, invalida la anterior, empieza otra vez las horas de vigencia y le manda la bienvenida
(si este ambiente manda correo de verdad). La contraseña se imprime SIEMPRE, salga o no el correo: es el respaldo de
quien lo corre.

Solo sirve para una cuenta que NO ha hecho su primer acceso. Si ya lo hizo, la salida es "Olvidé mi contraseña".
Lee el mismo `.env` que la API (`ALMACEN_RUTA`).
"""

import argparse
import sys

from dotenv import load_dotenv

load_dotenv()

from servicios import invitaciones, usuarios  # noqa: E402
from servicios.almacen import ErrorAlmacen  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Genera una contraseña temporal nueva para una cuenta que no ha hecho su primer acceso.")
    parser.add_argument("--email", required=True)
    args = parser.parse_args()

    try:
        cuenta = usuarios.para_login(args.email)
        if cuenta is None:
            print(f"No existe ninguna cuenta con el correo {args.email.strip().lower()}.", file=sys.stderr)
            return 2
        resultado, temporal = invitaciones.reenviar(cuenta["guid"], actor_guid=None)
    except usuarios.NoPendiente:
        print("Esa cuenta ya hizo su primer acceso: para recuperarla, que use «Olvidé mi contraseña».", file=sys.stderr)
        return 2
    except ErrorAlmacen as exc:
        print(f"No se pudo escribir el registro: {exc}", file=sys.stderr)
        return 1

    print("Contraseña temporal NUEVA generada (la anterior ya no sirve).")
    print(f"  Usuario:             {cuenta['email']}")
    print(f"  Contraseña temporal: {temporal}")
    print("  (se muestra solo esta vez; al entrar se pedirá cambiarla)")
    if resultado == invitaciones.ENVIADO:
        print(f"  Correo de bienvenida: enviado a {cuenta['email']}.")
    elif resultado == invitaciones.SIN_CANAL:
        print("  Correo de bienvenida: NO se mandó (este ambiente no manda correo de verdad); entrega tú la contraseña.")
    else:
        print("  Correo de bienvenida: NO SE PUDO ENVIAR (el motivo está en el log); entrega tú la contraseña.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
