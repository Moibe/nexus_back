"""Retira una cuenta: BORRADO LÓGICO. Su correo queda libre para darse de alta otra vez.

    .venv/Scripts/python retirar_cuenta.py --email a@b.com --email c@d.com --motivo "Cuenta de prueba"            (simulación)
    .venv/Scripts/python retirar_cuenta.py --email a@b.com --email c@d.com --motivo "Cuenta de prueba" --confirmar

Sin `--confirmar` NO hace nada: solo dice qué cuenta es y qué pasaría. Con `--confirmar`, a cada cuenta se le cierran
todas las sesiones, se apagan sus enlaces pendientes, deja de existir para el login, los listados y los JWT, y queda
el evento `USER_DELETED` en la bitácora de auditoría (con el motivo). El rastro de lo que hizo se queda: el registro
es de solo agregar y aquí no se borra ningún renglón.

Se niega a retirar al ÚLTIMO administrador de plataforma, y al administrador de una organización salvo con `--forzar`
(la organización queda sin administrador). No hay forma de deshacerlo desde aquí: para volver a tener la cuenta se da
de alta otra vez, como una cuenta nueva.

Lee el mismo `.env` que la API (`ALMACEN_RUTA`). Código de salida: 0 si todo salió, 2 si alguna cuenta no existe o se negó.
"""

import argparse
import sys

from dotenv import load_dotenv

load_dotenv()

from servicios import auditoria, tenants_registro, tokens, usuarios  # noqa: E402
from servicios.almacen import ErrorAlmacen  # noqa: E402


def _describir(cuenta: dict) -> str:
    partes = [f"{cuenta['nombre']} {cuenta['apellidos']}".strip() or "(sin nombre)"]
    if cuenta["esAdminPlataforma"]:
        partes.append("administrador de plataforma")
    for m in cuenta["membresias"]:
        try:
            organizacion = tenants_registro.por_guid(m["tenantGuid"])["nombre"]
        except tenants_registro.NoEncontrado:
            organizacion = m["tenantGuid"]
        partes.append(f"{m['rol']} de {organizacion}")
    partes.append("activa" if cuenta["activo"] else "DESACTIVADA")
    partes.append("sin hacer su primer acceso" if cuenta["debeCambiarContrasena"] else "con su primer acceso hecho")
    return " · ".join(partes)


def main() -> int:
    parser = argparse.ArgumentParser(description="Retira una o varias cuentas (borrado lógico). Sin --confirmar solo simula.")
    parser.add_argument("--email", action="append", required=True, help="Se puede repetir.")
    parser.add_argument("--motivo", required=True, help="Por qué se retira; queda en la bitácora.")
    parser.add_argument("--confirmar", action="store_true", help="Hacerlo de verdad. Sin esto solo se simula.")
    parser.add_argument("--forzar", action="store_true", help="Permite retirar al administrador de una organización.")
    args = parser.parse_args()

    motivo = args.motivo.strip()
    if not motivo:
        print("El motivo es obligatorio.", file=sys.stderr)
        return 2
    if not args.confirmar:
        print("SIMULACIÓN (no se cambia nada; agrega --confirmar para hacerlo):")
    problemas = 0
    for correo in args.email:
        correo = correo.strip().lower()
        try:
            cuenta = usuarios.para_login(correo)
            if cuenta is None:
                print(f"  {correo}: NO EXISTE (o ya se retiró).")
                problemas += 1
                continue
            descripcion = _describir(cuenta)
            if not args.confirmar:
                try:
                    usuarios.puede_eliminar(cuenta["guid"], forzar=args.forzar)
                    print(f"  {correo}: se retiraría → {descripcion}")
                except usuarios.NoSePuedeEliminar as exc:
                    print(f"  {correo}: SE NEGARÍA ({exc}) → {descripcion}" + ("  (con --forzar sí)" if exc.motivo == "admin_de_organizacion" else ""))
                    problemas += 1
                continue
            try:
                usuarios.eliminar_usuario(cuenta["guid"], motivo, por="CLI", forzar=args.forzar)
            except usuarios.NoSePuedeEliminar as exc:
                print(f"  {correo}: NO se retiró: {exc}" + ("  (con --forzar sí)" if exc.motivo == "admin_de_organizacion" else ""))
                problemas += 1
                continue
            tokens.invalidar_de(cuenta["guid"], motivo="CUENTA_RETIRADA")
            auditoria.registrar(
                auditoria.USER_DELETED,
                usuario_guid=cuenta["guid"],
                email=correo,
                motivo=motivo,
                actor="CLI",
                esAdminPlataforma=cuenta["esAdminPlataforma"],
                membresias=len(cuenta["membresias"]),
            )
            print(f"  {correo}: RETIRADA → {descripcion}. Su correo ya puede darse de alta otra vez.")
        except ErrorAlmacen as exc:
            print(f"  {correo}: no se pudo escribir el registro: {exc}", file=sys.stderr)
            return 1
    return 2 if problemas else 0


if __name__ == "__main__":
    sys.exit(main())
