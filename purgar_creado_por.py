"""Purga de VERDAD lo que crearon unos administradores de plataforma que ya se retiraron.

    .venv/Scripts/python purgar_creado_por.py --listar-retirados
    .venv/Scripts/python purgar_creado_por.py --listar-organizaciones
    .venv/Scripts/python purgar_creado_por.py --todas --conservar GUID --conservar GUID            (simulación)
    .venv/Scripts/python purgar_creado_por.py --email a@yopmail.com --email b@hotmail.com            (simulación)
    .venv/Scripts/python purgar_creado_por.py --email a@yopmail.com --email b@hotmail.com --confirmar

Sin `--confirmar` NO hace nada: lista lo que se borraría. Con `--confirmar` copia primero `.registro/` a
`.respaldo-purga-<fecha>/` (dentro del almacén) y luego borra.

A diferencia de `retirar_cuenta.py` (borrado lógico), esto REESCRIBE los registros de solo agregar y borra los archivos de
las organizaciones. No hay forma de deshacerlo salvo el respaldo de `.registro/` (los documentos NO se respaldan).

## Qué se considera «lo que crearon»

La bitácora guarda en cada `USER_CREATED` quién dio de alta a quién (`actor`) y en qué organización (`tenantGuid`). De ahí:

  - Organizaciones: aquellas cuyo administrador dio de alta uno de los retirados (o con `--tenant`).
  - Personas: las que ellos dieron de alta, TODAS las que tengan membresía en esas organizaciones, y los propios retirados.
  - De cada organización: tipos documentales, tipos de expediente, expedientes, documentos y su bandeja, procesos, API Keys,
    webhooks (y sus entregas), el uso de las llaves, los enlaces de un solo uso de esas personas, la carpeta de archivos
    `{almacén}/{organización}/` y sus renglones de la bitácora de auditoría.

Con `--todas` se purgan TODAS las organizaciones menos las que se indiquen con `--conservar GUID` (míralas con
`--listar-organizaciones`), con todas sus personas; los administradores de plataforma vivos nunca se tocan.

Se niega si alguno de los retirados SIGUE existiendo (retíralo antes), si alguna de las personas a borrar es un administrador de
plataforma que sigue vivo, o si una persona a borrar tiene membresía en una organización que NO se purga (avisa; con `--forzar`
se borra igual).

Lo que NO toca: los procesadores de Document AI en Google (ver `limpiar_procesadores_huerfanos.py`) y los renglones sueltos
sin dueño (por ejemplo, los intentos de acceso por IP).

Lee el mismo `.env` que la API (`ALMACEN_RUTA`). Con la API prendida funciona (su caché se invalida sola al cambiar el
tamaño del archivo), pero lo más limpio es correrlo sin tráfico.
"""

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from servicios import almacen, registro  # noqa: E402
from servicios.almacen import ErrorAlmacen  # noqa: E402

# Los registros por organización: `{prefijo}-{tenant}.jsonl`. Se borran enteros.
PREFIJOS_POR_ORGANIZACION = ("bandeja", "documentos", "expedientes", "tipos-expediente")
CLAVES_DE_ORGANIZACION = ("tenant", "tenantGuid", "tenantPedido")


def _leer(nombre: str) -> list[tuple[bytes, dict | None]]:
    """Las líneas CRUDAS del registro con su evento (o `None` si no se pudo leer: esas se conservan tal cual)."""
    origen = registro.ruta(nombre)
    if not origen.exists():
        return []
    salida = []
    with open(origen, "rb") as f:
        for cruda in f:
            if not cruda.strip():
                continue
            try:
                e = json.loads(cruda.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                e = None
            salida.append((cruda if cruda.endswith(b"\n") else cruda + b"\n", e if isinstance(e, dict) else None))
    return salida


def _normalizar(correo: str) -> str:
    return correo.strip().lower()


class Plan:
    """Lo que se va a borrar, calculado sin tocar nada."""

    def __init__(self, retirados: set[str], tenants: set[str]):
        self.retirados = retirados
        self.tenants = tenants
        self.usuarios: set[str] = set()
        self.avisos: list[str] = []
        self.por_registro: dict[str, int] = {}
        self.archivos_por_organizacion: list[Path] = []
        self.carpetas: list[tuple[Path, int, int]] = []


def _estado_de_cuentas() -> tuple[dict[str, dict], set[str]]:
    """(personas que alguna vez existieron, por guid; guids con evento `eliminado`)."""
    personas: dict[str, dict] = {}
    eliminadas: set[str] = set()
    for _, e in _leer("usuarios.jsonl"):
        if not e:
            continue
        guid = e.get("guid")
        if not isinstance(guid, str):
            continue
        if e.get("evento") == "creado":
            personas.setdefault(guid, e)
        elif e.get("evento") == "eliminado":
            eliminadas.add(guid)
    return personas, eliminadas


def listar_retirados() -> int:
    personas, eliminadas = _estado_de_cuentas()
    hay = False
    for guid in eliminadas:
        p = personas.get(guid)
        if p and p.get("esAdminPlataforma"):
            hay = True
            print(f"  {p.get('email')}  ({p.get('nombre', '')} {p.get('apellidos', '')}, alta {p.get('en')})  guid={guid}")
    if not hay:
        print("  No hay administradores de plataforma retirados en el registro.")
    return 0


def listar_organizaciones() -> int:
    personas, _ = _estado_de_cuentas()
    miembros: dict[str, list[str]] = {}
    for _, e in _leer("usuarios.jsonl"):
        if e and e.get("evento") == "membresia" and isinstance(e.get("guid"), str) and isinstance(e.get("tenantGuid"), str):
            if e["guid"] not in miembros.setdefault(e["tenantGuid"], []):
                miembros[e["tenantGuid"]].append(e["guid"])
    hay = False
    for _, e in _leer("tenants.jsonl"):
        if e and e.get("evento") == "creada":
            hay = True
            admin = personas.get(e.get("adminGuid") or "", {}).get("email", "(sin administrador)")
            print(f"  {e.get('codigo', ''):10} {e.get('nombre', ''):32} admin: {admin:40} personas: {len(miembros.get(e['guid'], [])):3}  {e['guid']}")
    if not hay:
        print("  No hay organizaciones en el registro.")
    return 0


def construir_plan(correos: list[str], tenants_extra: list[str], forzar: bool, todas: bool = False, conservar: tuple = ()) -> Plan:
    personas, eliminadas = _estado_de_cuentas()
    por_correo = {_normalizar(p.get("email", "")): g for g, p in personas.items()}

    retirados: set[str] = set()
    for correo in correos:
        guid = por_correo.get(_normalizar(correo))
        if guid is None:
            raise SystemExit(f"{correo}: no aparece en el registro de usuarios.")
        if guid not in eliminadas:
            raise SystemExit(f"{correo}: SIGUE existiendo. Retíralo primero (Monitor o retirar_cuenta.py).")
        retirados.add(guid)

    # Quién dio de alta a quién y en qué organización.
    altas = [e for _, e in _leer("auditoria.jsonl") if e and e.get("accion") == "USER_CREATED"]
    tenants = {t for t in tenants_extra}
    if todas:
        creadas = [e for _, e in _leer("tenants.jsonl") if e and e.get("evento") == "creada" and isinstance(e.get("guid"), str)]
        existentes = {e["guid"] for e in creadas}
        por_codigo = {str(e.get("codigo", "")).upper(): e["guid"] for e in creadas}
        guardar = set()
        for g in conservar:
            # Se acepta el guid o el código (NEX-00004).
            destino = g if g in existentes else por_codigo.get(g.strip().upper())
            if destino is None:
                raise SystemExit(f"--conservar {g}: no es una organización del registro (míralas con --listar-organizaciones).")
            guardar.add(destino)
        tenants |= existentes - guardar
        altas = []
    for e in altas:
        if e.get("actor") in retirados and isinstance(e.get("tenantGuid"), str):
            tenants.add(e["tenantGuid"])
    # Los administradores de esas organizaciones los pudo dar de alta un retirado sin que la organización quede anotada.
    for _, e in _leer("tenants.jsonl"):
        if e and e.get("evento") == "creada" and e.get("adminGuid"):
            for alta in altas:
                if alta.get("usuarioGuid") == e["adminGuid"] and alta.get("actor") in retirados:
                    tenants.add(e["guid"])

    plan = Plan(retirados, tenants)

    # Personas: las que dieron de alta, las que pertenecen a esas organizaciones y los retirados.
    usuarios = set(retirados)
    for e in altas:
        if e.get("actor") in retirados and isinstance(e.get("usuarioGuid"), str):
            usuarios.add(e["usuarioGuid"])
    membresias: dict[str, set[str]] = {}
    for _, e in _leer("usuarios.jsonl"):
        if e and e.get("evento") == "membresia" and isinstance(e.get("guid"), str) and isinstance(e.get("tenantGuid"), str):
            membresias.setdefault(e["guid"], set()).add(e["tenantGuid"])
            if e["tenantGuid"] in tenants:
                usuarios.add(e["guid"])
    plan.usuarios = usuarios

    # Candados.
    vivos = {g for g in usuarios if g in personas and g not in eliminadas}
    for g in sorted(vivos):
        if personas[g].get("esAdminPlataforma"):
            raise SystemExit(f"Se negaría: {personas[g].get('email')} es administrador de plataforma y sigue vivo; la purga lo alcanzaría.")
    for g in sorted(usuarios):
        fuera = membresias.get(g, set()) - tenants
        if fuera and g not in retirados:
            aviso = f"{personas.get(g, {}).get('email', g)} también es miembro de organizaciones que NO se purgan ({len(fuera)})."
            if not forzar:
                raise SystemExit(aviso + " Agrega --forzar si aun así quieres borrarlo.")
            plan.avisos.append(aviso + " (se borra por --forzar)")

    # Cuántos renglones se irían de cada registro compartido (se calcula con el mismo filtro que el borrado).
    for nombre in REGISTROS_COMPARTIDOS:
        conservadas, quitadas = _filtrar(nombre, plan)
        plan.por_registro[nombre] = len(quitadas)

    raiz = almacen._raiz()
    for t in sorted(tenants):
        for prefijo in PREFIJOS_POR_ORGANIZACION:
            destino = registro.ruta(f"{prefijo}-{t}.jsonl")
            if destino.exists():
                plan.archivos_por_organizacion.append(destino)
        carpeta = raiz / t
        if carpeta.is_dir():
            n = peso = 0
            for base, _, nombres in os.walk(carpeta):
                for nombre in nombres:
                    n += 1
                    try:
                        peso += (Path(base) / nombre).stat().st_size
                    except OSError:
                        pass
            plan.carpetas.append((carpeta, n, peso))
    return plan


REGISTROS_COMPARTIDOS = (
    "usuarios.jsonl",
    "tenants.jsonl",
    "tokens.jsonl",
    "auditoria.jsonl",
    "tipos-documentales.jsonl",
    "procesos.jsonl",
    "llaves.jsonl",
    "webhooks.jsonl",
    "entregas-webhooks.jsonl",
    "uso-llaves.jsonl",
)


def _filtrar(nombre: str, plan: Plan) -> tuple[list[bytes], list[bytes]]:
    """(líneas que se quedan, líneas que se van) del registro compartido `nombre`."""
    lineas = _leer(nombre)
    T, U, R = plan.tenants, plan.usuarios, plan.retirados

    def de_organizacion(e: dict) -> bool:
        return any(e.get(c) in T for c in CLAVES_DE_ORGANIZACION)

    # Registros cuyos eventos posteriores solo traen el `id` de lo creado: se sacan los ids de lo que era de una organización.
    ids: set[str] = set()
    if nombre in ("llaves.jsonl", "webhooks.jsonl", "entregas-webhooks.jsonl"):
        for _, e in lineas:
            if e and de_organizacion(e):
                for clave in ("id", "webhook"):
                    if isinstance(e.get(clave), str):
                        ids.add(e[clave])
    hashes: set[str] = set()
    if nombre == "tokens.jsonl":
        for _, e in lineas:
            if e and e.get("usuarioGuid") in U and isinstance(e.get("hash"), str):
                hashes.add(e["hash"])

    def se_va(e: dict) -> bool:
        if nombre == "usuarios.jsonl":
            return e.get("guid") in U
        if nombre == "tenants.jsonl":
            return e.get("guid") in T
        if nombre == "tokens.jsonl":
            return e.get("usuarioGuid") in U or e.get("hash") in hashes
        if nombre == "auditoria.jsonl":
            return de_organizacion(e) or e.get("usuarioGuid") in U or e.get("actor") in U or e.get("actor") in R
        if nombre in ("tipos-documentales.jsonl", "procesos.jsonl"):
            return de_organizacion(e)
        if nombre in ("llaves.jsonl", "webhooks.jsonl", "entregas-webhooks.jsonl"):
            return de_organizacion(e) or e.get("id") in ids or e.get("webhook") in ids
        if nombre == "uso-llaves.jsonl":
            return e.get("llave") in plan_llaves(plan)
        return False

    quedan, van = [], []
    for cruda, e in lineas:
        (van if e is not None and se_va(e) else quedan).append(cruda)
    return quedan, van


_LLAVES_CACHE: dict[int, set[str]] = {}


def plan_llaves(plan: Plan) -> set[str]:
    """Los ids de las llaves que eran de las organizaciones purgadas (para el uso de llaves)."""
    clave = id(plan)
    if clave not in _LLAVES_CACHE:
        _LLAVES_CACHE[clave] = {
            e["id"] for _, e in _leer("llaves.jsonl") if e and e.get("tenant") in plan.tenants and isinstance(e.get("id"), str)
        }
    return _LLAVES_CACHE[clave]


def describir(plan: Plan) -> None:
    personas, _ = _estado_de_cuentas()
    print(f"\nRetirados: {len(plan.retirados)} · Organizaciones: {len(plan.tenants)} · Personas: {len(plan.usuarios)}")
    nombres = {e["guid"]: e.get("nombre") for _, e in _leer("tenants.jsonl") if e and e.get("evento") == "creada"}
    for t in sorted(plan.tenants):
        print(f"  organización {nombres.get(t, '(sin nombre en el registro)')}  [{t}]")
    for g in sorted(plan.usuarios, key=lambda x: personas.get(x, {}).get("email", "")):
        p = personas.get(g, {})
        print(f"  persona {p.get('email', '(sin correo)')}{'  (retirado)' if g in plan.retirados else ''}")
    print("\nRenglones que se quitarían de los registros compartidos:")
    for nombre, n in plan.por_registro.items():
        print(f"  {nombre:28} {n}")
    print("\nArchivos de registro por organización que se borrarían:")
    for r in plan.archivos_por_organizacion:
        print(f"  {r.name}")
    print("\nCarpetas de documentos que se borrarían:")
    total = 0
    for carpeta, n, peso in plan.carpetas:
        total += n
        print(f"  {carpeta.name}  ({n} archivos, {peso / 1024:.0f} KB)")
    print(f"  total: {total} archivos")
    for a in plan.avisos:
        print(f"\nAVISO: {a}")


def _reescribir(nombre: str, lineas: list[bytes]) -> None:
    destino = registro.ruta(nombre)
    temporal = destino.with_name(destino.name + ".purga.tmp")
    with open(temporal, "wb") as f:
        f.writelines(lineas)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporal, destino)


def ejecutar(plan: Plan, motivo: str) -> None:
    raiz = almacen._raiz()
    almacen._exigir_raiz_montada()
    sello = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    respaldo = raiz / f".respaldo-purga-{sello}"
    with registro.candado:
        shutil.copytree(raiz / ".registro", respaldo)
        print(f"\nRespaldo de .registro/ en {respaldo}")
        for nombre in REGISTROS_COMPARTIDOS:
            quedan, van = _filtrar(nombre, plan)
            if van:
                _reescribir(nombre, quedan)
                print(f"  {nombre}: -{len(van)}")
        for r in plan.archivos_por_organizacion:
            r.unlink()
            print(f"  borrado {r.name}")
        for carpeta, n, _ in plan.carpetas:
            # Solo carpetas cuyo nombre es el guid de una organización purgada, directamente bajo la raíz.
            if carpeta.parent.resolve() == raiz.resolve() and carpeta.name in plan.tenants:
                shutil.rmtree(carpeta)
                print(f"  borrada la carpeta {carpeta.name} ({n} archivos)")
        registro.agregar(
            "auditoria.jsonl",
            {
                "evento": "auditoria",
                "accion": "PLATFORM_PURGE",
                "usuarioGuid": None,
                "en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "actor": "CLI",
                "motivo": motivo,
                "retirados": len(plan.retirados),
                "organizaciones": len(plan.tenants),
                "personas": len(plan.usuarios),
                "respaldo": respaldo.name,
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Purga lo que crearon administradores de plataforma ya retirados. Sin --confirmar solo simula.")
    parser.add_argument("--listar-retirados", action="store_true", help="Muestra los administradores de plataforma retirados y sale.")
    parser.add_argument("--listar-organizaciones", action="store_true", help="Muestra las organizaciones (código, nombre, administrador, personas, guid) y sale.")
    parser.add_argument("--todas", action="store_true", help="Purga TODAS las organizaciones menos las de --conservar.")
    parser.add_argument("--conservar", action="append", default=[], help="Guid o código (NEX-00004) de una organización que NO se purga con --todas. Se puede repetir.")
    parser.add_argument("--email", action="append", default=[], help="Correo de un administrador YA retirado. Se puede repetir.")
    parser.add_argument("--tenant", action="append", default=[], help="Guid de una organización más a purgar (si no quedó atribuida). Se puede repetir.")
    parser.add_argument("--motivo", default="Depuración de datos de prueba", help="Queda en la bitácora.")
    parser.add_argument("--confirmar", action="store_true", help="Hacerlo de verdad. Sin esto solo se simula.")
    parser.add_argument("--forzar", action="store_true", help="Borra también a quien es miembro de organizaciones que no se purgan.")
    args = parser.parse_args()
    try:
        almacen._exigir_raiz_montada()
        if args.listar_retirados:
            return listar_retirados()
        if args.listar_organizaciones:
            return listar_organizaciones()
        if args.conservar and not args.todas:
            print("--conservar solo se usa con --todas.", file=sys.stderr)
            return 2
        if not args.email and not args.todas:
            print("Falta --email, --todas o --listar-retirados / --listar-organizaciones.", file=sys.stderr)
            return 2
        plan = construir_plan(args.email, args.tenant, args.forzar, args.todas, tuple(args.conservar))
    except ErrorAlmacen as exc:
        print(f"Almacén no disponible: {exc}", file=sys.stderr)
        return 2
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    describir(plan)
    if not args.confirmar:
        print("\nSIMULACIÓN: no se borró nada. Agrega --confirmar para hacerlo.")
        return 0
    ejecutar(plan, args.motivo.strip() or "Depuración de datos de prueba")
    print("\nListo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
