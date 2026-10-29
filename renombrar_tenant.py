"""Renombra un tenant que NO es una organización —el espacio de plataforma: `demo` → `plataforma`— CONSERVANDO todos sus datos.

    .venv/Scripts/python renombrar_tenant.py                                 (simulación de demo → plataforma)
    .venv/Scripts/python renombrar_tenant.py --confirmar
    .venv/Scripts/python renombrar_tenant.py --de plataforma --a demo        (se deshace con el mismo script, al revés)

Sin `--confirmar` NO hace nada: dice qué se movería. Con `--confirmar` copia primero `.registro/` a `.respaldo-renombre-<fecha>/` (dentro del
almacén) y luego:

  - reescribe, en los registros que guardan el tenant, los renglones de ESE tenant (`tenant`, `tenantGuid`, `tenantPedido`) y las rutas
    de archivo que llevan su nombre delante (`demo/ab/cd/<sha256>` → `plataforma/ab/cd/<sha256>`: las de la bandeja, los documentos y los
    ejemplos de cada tipo documental);
  - renombra sus archivos de registro propios (`bandeja-demo.jsonl`, `documentos-demo.jsonl`, `expedientes-demo.jsonl`,
    `tipos-expediente-demo.jsonl`) y la carpeta de documentos `{almacén}/demo/`;
  - deja un evento `PLATFORM_TENANT_RENAMED` en la bitácora de auditoría. La bitácora NO se reescribe: es evidencia de solo agregar, y
    lo que pasó en el tenant antes del cambio sigue diciendo `demo`.

Nada de Google depende del nombre del tenant (los procesadores se llaman por el id del tipo), así que los procesadores y el clasificador
del espacio de plataforma siguen siendo los mismos.

Se niega si alguno de los dos nombres es el guid de una organización, si son iguales, o si el destino ya tiene algo (un registro, una
carpeta o renglones con ese tenant): mezclar dos espacios no se deshace. Lo que escribe un renglón nuevo es solo lo que cambió; el
resto de las líneas se conserva byte por byte, y una línea ilegible se deja tal cual.

Lee el mismo `.env` que la API (`ALMACEN_RUTA`). Córrelo sin tráfico en ese espacio: entre leer y reemplazar un archivo, una escritura
de la API se perdería. Lo que cambia de nombre aquí tiene que cambiar también en el front (`NEXUS_TENANT`, que por omisión ya es
`plataforma`), o el administrador de plataforma entraría a un espacio vacío.
"""

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from servicios import almacen, registro  # noqa: E402
from servicios.aislamiento import es_organizacion  # noqa: E402
from servicios.almacen import ErrorAlmacen  # noqa: E402

# Registros compartidos donde cada renglón dice de qué tenant es. NO está `auditoria.jsonl` a propósito (ver arriba).
REGISTROS_COMPARTIDOS = (
    "tipos-documentales.jsonl",
    "procesos.jsonl",
    "llaves.jsonl",
    "webhooks.jsonl",
    "entregas-webhooks.jsonl",
    "clasificadores.jsonl",
)
PREFIJOS_POR_TENANT = ("bandeja", "documentos", "expedientes", "tipos-expediente")
CLAVES_DE_TENANT = ("tenant", "tenantGuid", "tenantPedido")


def _patron_de_ruta(tenant: str) -> re.Pattern:
    """`tenant/ab/cd/<sha256>` con el tenant al inicio (sin letras, números, `_` o `-` pegados delante). Es la ÚNICA forma de ruta de objeto."""
    return re.compile(r"(?<![A-Za-z0-9_-])" + re.escape(tenant) + r"/([0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64})")


def _reemplazar(valor, de: str, a: str, patron: re.Pattern, cuenta: list[int]):
    """Una copia de `valor` (JSON) con el tenant y las rutas cambiados. `cuenta[0]` suma los cambios hechos."""
    if isinstance(valor, dict):
        nuevo = {}
        for k, v in valor.items():
            if k in CLAVES_DE_TENANT and v == de:
                nuevo[k] = a
                cuenta[0] += 1
            else:
                nuevo[k] = _reemplazar(v, de, a, patron, cuenta)
        return nuevo
    if isinstance(valor, list):
        return [_reemplazar(v, de, a, patron, cuenta) for v in valor]
    if isinstance(valor, str):
        nuevo, n = patron.subn(lambda m: f"{a}/{m.group(1)}", valor)
        cuenta[0] += n
        return nuevo
    return valor


def _leer(nombre: str) -> list[tuple[bytes, dict | None]]:
    """Las líneas CRUDAS con su evento (o `None` si no se pudo leer: esas se conservan tal cual)."""
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


def _reescribir_lineas(nombre: str, de: str, a: str) -> tuple[list[bytes], int, int]:
    """(las líneas del registro ya con el cambio, cuántos renglones cambiaron, cuántos valores se tocaron)."""
    patron = _patron_de_ruta(de)
    lineas, renglones, valores = [], 0, 0
    for cruda, e in _leer(nombre):
        if e is None:
            lineas.append(cruda)
            continue
        cuenta = [0]
        nuevo = _reemplazar(e, de, a, patron, cuenta)
        if cuenta[0]:
            renglones += 1
            valores += cuenta[0]
            lineas.append((json.dumps(nuevo, ensure_ascii=False) + "\n").encode("utf-8"))
        else:
            lineas.append(cruda)
    return lineas, renglones, valores


def _menciona(nombre: str, tenant: str) -> bool:
    return any(any(e.get(c) == tenant for c in CLAVES_DE_TENANT) for _, e in _leer(nombre) if e)


class Plan:
    def __init__(self, de: str, a: str):
        self.de, self.a = de, a
        self.compartidos: dict[str, tuple[int, int]] = {}
        self.archivos: list[tuple[Path, Path, int, int]] = []  # (origen, destino, renglones, valores)
        self.carpeta: tuple[Path, Path, int, int] | None = None  # (origen, destino, archivos, bytes)


def construir_plan(de: str, a: str) -> Plan:
    de, a = almacen._validar_tenant(de), almacen._validar_tenant(a)
    if de == a:
        raise SystemExit("El nombre de origen y el de destino son el mismo.")
    for t in (de, a):
        if es_organizacion(t):
            raise SystemExit(f"«{t}» es el guid de una organización: esto solo renombra el espacio de plataforma, no organizaciones.")
    raiz = almacen._raiz()
    plan = Plan(de, a)

    # El destino no puede tener nada: dos espacios mezclados no se separan después.
    ocupado = []
    for p in PREFIJOS_POR_TENANT:
        if registro.ruta(f"{p}-{a}.jsonl").exists():
            ocupado.append(f"{p}-{a}.jsonl")
    if (raiz / a).exists():
        ocupado.append(f"la carpeta {a}/")
    for nombre in REGISTROS_COMPARTIDOS:
        if _menciona(nombre, a):
            ocupado.append(f"renglones de {nombre} con el tenant «{a}»")
    if ocupado:
        raise SystemExit(f"El destino «{a}» ya tiene datos ({'; '.join(ocupado)}). Se niega para no mezclar dos espacios.")

    for nombre in REGISTROS_COMPARTIDOS:
        _, renglones, valores = _reescribir_lineas(nombre, de, a)
        plan.compartidos[nombre] = (renglones, valores)
    for p in PREFIJOS_POR_TENANT:
        origen = registro.ruta(f"{p}-{de}.jsonl")
        if origen.exists():
            _, renglones, valores = _reescribir_lineas(f"{p}-{de}.jsonl", de, a)
            plan.archivos.append((origen, registro.ruta(f"{p}-{a}.jsonl"), renglones, valores))
    carpeta = raiz / de
    if carpeta.is_dir():
        n = peso = 0
        for base, _, nombres in os.walk(carpeta):
            for nombre in nombres:
                n += 1
                try:
                    peso += (Path(base) / nombre).stat().st_size
                except OSError:
                    pass
        plan.carpeta = (carpeta, raiz / a, n, peso)
    return plan


def describir(plan: Plan) -> None:
    print(f"\nRenombrar el tenant «{plan.de}» → «{plan.a}»")
    print("\nRenglones que cambian en los registros compartidos:")
    for nombre, (renglones, valores) in plan.compartidos.items():
        print(f"  {nombre:28} {renglones} renglones ({valores} valores)")
    print("\nArchivos de registro propios que cambian de nombre:")
    if not plan.archivos:
        print("  (ninguno)")
    for origen, destino, renglones, valores in plan.archivos:
        print(f"  {origen.name} → {destino.name}   ({renglones} renglones con cambios, {valores} valores)")
    print("\nCarpeta de documentos:")
    if plan.carpeta:
        origen, destino, n, peso = plan.carpeta
        print(f"  {origen.name}/ → {destino.name}/   ({n} archivos, {peso / 1024:.0f} KB)")
    else:
        print("  (no hay carpeta de documentos de ese tenant)")


def _escribir(destino: Path, lineas: list[bytes]) -> None:
    temporal = destino.with_name(destino.name + ".renombre.tmp")
    with open(temporal, "wb") as f:
        f.writelines(lineas)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temporal, destino)


def ejecutar(plan: Plan) -> None:
    raiz = almacen._raiz()
    almacen._exigir_raiz_montada()
    sello = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    respaldo = raiz / f".respaldo-renombre-{sello}"
    n = 1
    while respaldo.exists():
        n += 1
        respaldo = raiz / f".respaldo-renombre-{sello}-{n}"
    with registro.candado:
        shutil.copytree(raiz / ".registro", respaldo)
        print(f"\nRespaldo de .registro/ en {respaldo}")
        for nombre, (renglones, _) in plan.compartidos.items():
            if renglones:
                lineas, _, _ = _reescribir_lineas(nombre, plan.de, plan.a)
                _escribir(registro.ruta(nombre), lineas)
                print(f"  {nombre}: {renglones} renglones")
        for origen, destino, _, _ in plan.archivos:
            lineas, _, _ = _reescribir_lineas(origen.name, plan.de, plan.a)
            _escribir(destino, lineas)
            origen.unlink()
            print(f"  {origen.name} → {destino.name}")
        if plan.carpeta:
            origen, destino, n_archivos, _ = plan.carpeta
            # Solo una carpeta directamente bajo la raíz y con el nombre exacto del tenant.
            if origen.parent.resolve() == raiz.resolve() and origen.name == plan.de:
                os.rename(origen, destino)
                print(f"  carpeta {origen.name}/ → {destino.name}/ ({n_archivos} archivos)")
        registro.agregar(
            "auditoria.jsonl",
            {
                "evento": "auditoria",
                "accion": "PLATFORM_TENANT_RENAMED",
                "usuarioGuid": None,
                "en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "actor": "CLI",
                "de": plan.de,
                "a": plan.a,
                "respaldo": respaldo.name,
            },
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Renombra el espacio de plataforma conservando sus datos. Sin --confirmar solo simula.")
    parser.add_argument("--de", default="demo", help="Nombre actual del tenant (por omisión: demo).")
    parser.add_argument("--a", default="plataforma", help="Nombre nuevo (por omisión: plataforma).")
    parser.add_argument("--confirmar", action="store_true", help="Hacerlo de verdad. Sin esto solo se simula.")
    args = parser.parse_args()
    try:
        almacen._exigir_raiz_montada()
        plan = construir_plan(args.de, args.a)
    except ErrorAlmacen as exc:
        print(f"Almacén no disponible: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    except SystemExit as exc:
        print(exc, file=sys.stderr)
        return 2
    describir(plan)
    if not args.confirmar:
        print("\nSIMULACIÓN: no se cambió nada. Agrega --confirmar para hacerlo.")
        return 0
    ejecutar(plan)
    print("\nListo.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
