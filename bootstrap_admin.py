"""HU01 — Alta de administradores de plataforma.

    .venv/Scripts/python bootstrap_admin.py --email alguien@grupocsi.com --nombre Moisés --apellidos "Briseño Estrello"

Crea un super admin con una contraseña temporal que cumple las reglas y
la imprime UNA vez en la consola. No hay correo en este sprint (decisión del
2026-10-07): el correo de bienvenida del diseño se sustituye por esta salida,
que quien corre el comando le pasa al usuario por el canal que sea.

Puede haber varios super admins (decisión del 2026-10-09): cada corrida con
otro correo crea otro; con un correo ya registrado no hace nada y lo dice. Al entrar con la contraseña temporal, la app obliga a cambiarla (HU04).

Lee el mismo `.env` que la API (`ALMACEN_RUTA`): escribe en el registro
`usuarios.jsonl` del NAS, que es donde la API lo va a buscar.
"""

import argparse
import sys

from dotenv import load_dotenv

load_dotenv()

from servicios import auth, usuarios  # noqa: E402
from servicios.almacen import ErrorAlmacen  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Crea el primer administrador de plataforma.")
    parser.add_argument("--email", required=True)
    parser.add_argument("--nombre", required=True)
    parser.add_argument("--apellidos", required=True)
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
