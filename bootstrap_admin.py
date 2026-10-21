"""HU01 — Alta de administradores de plataforma.

    .venv/Scripts/python bootstrap_admin.py --email alguien@grupocsi.com --nombre Moisés --apellidos "Briseño Estrello"

Crea un super admin con una contraseña temporal que cumple las reglas y
la imprime UNA vez en la consola. Además le manda por correo la bienvenida del
diseño, con su usuario y esa contraseña, si este ambiente manda correo de
verdad (`CORREO_MODO=api` o `smtp`); con `--sin-correo` no se manda. La consola
la imprime SIEMPRE, salga o no el correo: es el respaldo de quien corre el
comando, y si el correo no salió es la única forma de que la persona entre.

Puede haber varios super admins (decisión del 2026-10-09): cada corrida con
otro correo crea otro; con un correo ya registrado no hace nada y lo dice. Al entrar con la contraseña temporal, la app obliga a cambiarla (HU04).

Lee el mismo `.env` que la API (`ALMACEN_RUTA`): escribe en el registro
`usuarios.jsonl` del NAS, que es donde la API lo va a buscar.
"""

import argparse
import sys

from dotenv import load_dotenv

load_dotenv()

from servicios import auth, invitaciones, usuarios  # noqa: E402
from servicios.almacen import ErrorAlmacen  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Crea un administrador de plataforma con contraseña temporal. "
            "Puede correrse varias veces: cada corrida con otro correo crea otro. "
            "Un correo ya registrado se rechaza y no crea nada."
        )
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--nombre", required=True)
    parser.add_argument("--apellidos", required=True)
    parser.add_argument("--sin-correo", action="store_true", help="No mandar el correo de bienvenida.")
    args = parser.parse_args()

    temporal = auth.contrasena_temporal()
    try:
        u = usuarios.bootstrap_admin(args.email, args.nombre, args.apellidos, auth.hash_de(temporal))
    except usuarios.YaExiste as exc:
        print(f"No se creó nada: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Datos inválidos: {exc}", file=sys.stderr)
        return 2
    except ErrorAlmacen as exc:
        print(f"No se pudo escribir el registro: {exc}", file=sys.stderr)
        return 1

    print("Administrador de plataforma creado.")
    print(f"  Usuario:             {u['email']}")
    print(f"  Contraseña temporal: {temporal}")
    print("  (se muestra solo esta vez; al entrar se pedirá cambiarla)")
    if args.sin_correo:
        print("  Correo de bienvenida: omitido (--sin-correo).")
        return 0
    resultado = invitaciones.enviar_bienvenida(u["email"], u["nombre"], temporal, "super_admin")
    if resultado == invitaciones.ENVIADO:
        print(f"  Correo de bienvenida: enviado a {u['email']}.")
    elif resultado == invitaciones.SIN_CANAL:
        print("  Correo de bienvenida: NO se mandó (este ambiente no manda correo de verdad); entrega tú la contraseña.")
    else:
        print("  Correo de bienvenida: NO SE PUDO ENVIAR (el motivo está en el log); entrega tú la contraseña.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
