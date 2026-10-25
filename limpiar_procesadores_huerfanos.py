"""Lista (y, si se pide, borra) los procesadores de Document AI que ningún tipo documental señala.

    .venv/Scripts/python limpiar_procesadores_huerfanos.py              # solo LISTA, no toca nada
    .venv/Scripts/python limpiar_procesadores_huerfanos.py --borrar     # los borra, pidiendo confirmación
    .venv/Scripts/python limpiar_procesadores_huerfanos.py --borrar --antes-de 2026-10-15
                                                                         # borra solo los huérfanos creados ANTES de esa fecha

`--antes-de AAAA-MM-DD` es el corte de seguridad: un huérfano más reciente se lista pero no se borra. Úsalo cuando el registro que
lee el script pueda no ser el de todos los ambientes (dev y prod) que comparten el proyecto de Google: lo creado antes de que el
catálogo pasara al servidor (2026-10-15) no puede tener dueño en ningún registro.

Por qué existe: ver `servicios/procesadores_huerfanos.py`. Es el paso 5 de la centralización de tipos documentales
(2026-10-15): al pasar el catálogo al servidor sin migrar lo que había en cada computadora, los extractores que esas pruebas
crearon en Google quedaron sin dueño.

Sin `--borrar` es de SOLO LECTURA (`processors.list` no cobra). Con `--borrar`, borra ÚNICAMENTE los huérfanos, después de
mostrarlos y de que se escriba BORRAR; es IRREVERSIBLE (se va el procesador, su dataset y su esquema). Los protegidos y los que
tienen dueño no se tocan nunca.

Hay que correrlo donde estén las dos cosas: las credenciales de Google y el almacén con el registro (el servidor). Lee el mismo
`.env` que la API.
"""

import argparse
import asyncio
import sys

from dotenv import load_dotenv

load_dotenv()

from config import DOCAI_CLASIFICADOR_ID, DOCAI_PROCESADOR_INE  # noqa: E402
from servicios import procesadores, procesadores_huerfanos, tipos_documentales  # noqa: E402
from servicios.almacen import ErrorAlmacen  # noqa: E402


def _linea(p: dict) -> str:
    return f"  {p['id']}  {p['creado'][:10]}  {p['displayName']}"


async def main() -> int:
    parser = argparse.ArgumentParser(description="Lista o borra los procesadores de Document AI sin dueño.")
    parser.add_argument("--borrar", action="store_true", help="Borra los huérfanos (pide confirmación).")
    parser.add_argument(
        "--antes-de",
        metavar="AAAA-MM-DD",
        type=procesadores_huerfanos.fecha_de_corte,
        help="Solo borra los huérfanos creados ANTES de esa fecha; los más recientes se listan y no se tocan.",
    )
    args = parser.parse_args()

    try:
        con_dueno = tipos_documentales.procesadores_con_dueno()
    except ErrorAlmacen as exc:
        print(f"No se pudo leer el registro de tipos documentales: {exc}", file=sys.stderr)
        return 1
    try:
        en_google = await procesadores.listar_procesadores_nexusdoc()
    except (procesadores.DocumentAIError, RuntimeError) as exc:
        print(f"No se pudo listar los procesadores en Document AI: {exc}", file=sys.stderr)
        return 1

    protegidos = {i for i in (DOCAI_CLASIFICADOR_ID, DOCAI_PROCESADOR_INE) if i}
    reparto = procesadores_huerfanos.clasificar(en_google, con_dueno, protegidos)

    print(f"Procesadores nexusdoc en Google: {len(en_google)}")
    print(f"\nCon dueño ({len(reparto['con_dueno'])}) — no se tocan:")
    for p in reparto["con_dueno"]:
        print(_linea(p) + f"   ← tipo {p['dueno']['tipo']}")
    print(f"\nProtegidos ({len(reparto['protegidos'])}) — no se tocan:")
    for p in reparto["protegidos"]:
        print(_linea(p))
    a_borrar = reparto["huerfanos"]
    if args.antes_de:
        a_borrar, recientes = procesadores_huerfanos.anteriores_a(reparto["huerfanos"], args.antes_de)
        print(f"\nHuérfanos creados antes de {args.antes_de} ({len(a_borrar)}) — los que se borrarían:")
        for p in a_borrar:
            print(_linea(p))
        print(f"\nHuérfanos de {args.antes_de} en adelante ({len(recientes)}) — NO se tocan (revísalos a mano):")
        for p in recientes:
            print(_linea(p))
    else:
        print(f"\nHuérfanos ({len(a_borrar)}) — nadie los señala:")
        for p in a_borrar:
            print(_linea(p))

    # Los que el registro dice tener y Google no: el tipo apunta a algo que ya no existe.
    en_google_ids = {p["id"] for p in en_google}
    sueltos = {i: d for i, d in con_dueno.items() if i not in en_google_ids}
    if sueltos:
        print(f"\nAviso: {len(sueltos)} procesador(es) que el registro señala ya no existen en Google:")
        for i, d in sueltos.items():
            print(f"  {i}   ← tipo {d['tipo']}")

    if not args.borrar:
        print("\n(Solo se listó. Con --borrar se borran los huérfanos.)")
        return 0
    if not a_borrar:
        print("\nNo hay huérfanos que borrar.")
        return 0
    if input(f"\nSe van a BORRAR {len(a_borrar)} procesadores, sin vuelta atrás. Escribe BORRAR para continuar: ").strip() != "BORRAR":
        print("Cancelado: no se borró nada.")
        return 0
    fallos = 0
    for p in a_borrar:
        try:
            await procesadores.eliminar_procesador(p["id"])
            print(f"  borrado  {p['id']}")
        except procesadores.DocumentAIError as exc:
            if exc.status_code == 404:
                print(f"  ya no existía  {p['id']}")
                continue
            fallos += 1
            print(f"  FALLÓ  {p['id']}: {exc}", file=sys.stderr)
    return 1 if fallos else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
