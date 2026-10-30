"""¿Las copias de las reglas de roles dicen lo mismo que nexus_back?

    .venv/Scripts/python verificar_roles_copias.py

Las reglas de quién puede qué viven en TRES lugares y hay que cambiarlas en los tres:

- `servicios/roles.py` (este repo), que es quien MANDA: `PUEDEN_ADMINISTRAR`, `PUEDEN_ORGANIZAR`, `PUEDEN_DESCARTAR`. El servidor responde
  403 `sin_permiso` aunque una pantalla le ofrezca algo a quien no le toca.
- `src/lib/usuarios/permisos.ts` (nexus_poc_svelte): decide qué se le OFRECE a la persona (el engrane, los botones).
- `src/lib/roles.ts` (nexus_monitor): la tabla de la sección «Roles», que es lo que se le enseña a César para confirmar las reglas.

Una tabla de roles que miente es peor que no tenerla, así que esta prueba lee los tres archivos y falla si dicen cosas distintas: los conjuntos
de roles, el catálogo de roles con sus nombres y su descripción del diseño, y qué opciones del engrane ve cada quien. Es OFFLINE; los otros
dos proyectos se buscan al lado de este (`../nexus_poc_svelte`, `../nexus_monitor`) y, si alguno no está en esta máquina, esa parte se SALTA y lo
dice (no cuenta como pasada).
"""

import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from servicios import roles  # noqa: E402

AQUI = Path(__file__).resolve().parent
FRONT = AQUI.parent / "nexus_poc_svelte" / "src" / "lib" / "usuarios" / "permisos.ts"
MONITOR = AQUI.parent / "nexus_monitor" / "src" / "lib" / "roles.ts"

fallos = 0
saltadas = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + extra if extra and not ok else ''}")
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


def _codigos(texto: str) -> set[str]:
    return set(re.findall(r"'([A-Z_]+)'", texto))


def conjunto_ts(codigo: str, nombre: str) -> set[str] | None:
    """`const NOMBRE = new Set(['A', 'B'])` en el front."""
    m = re.search(rf"const {nombre}\s*=\s*new Set\(\[([^\]]*)\]\)", codigo)
    return _codigos(m.group(1)) if m else None


def lista_ts(codigo: str, clave: str) -> set[str] | None:
    """`clave: ['A', 'B'],` en el Monitor (una línea por conjunto, como dice su nota)."""
    m = re.search(rf"^\s*{clave}:\s*\[([^\]]*)\]", codigo, re.M)
    return _codigos(m.group(1)) if m else None


def mostrar(c: set[str] | None) -> str:
    return "no se pudo leer" if c is None else str(sorted(c))


titulo("El front (permisos.ts) dice lo mismo que el back")
if not FRONT.exists():
    print(f"  SE SALTA: no está {FRONT}")
    saltadas += 1
else:
    front = FRONT.read_text(encoding="utf-8")
    administran, organizan = conjunto_ts(front, "ROLES_QUE_ADMINISTRAN"), conjunto_ts(front, "ROLES_QUE_ORGANIZAN")
    rev("quién ADMINISTRA: ROLES_QUE_ADMINISTRAN = roles.PUEDEN_ADMINISTRAR", administran == set(roles.PUEDEN_ADMINISTRAR), f"front={mostrar(administran)} back={sorted(roles.PUEDEN_ADMINISTRAR)}")
    rev("quién ORGANIZA: ROLES_QUE_ORGANIZAN = roles.PUEDEN_ORGANIZAR", organizan == set(roles.PUEDEN_ORGANIZAR), f"front={mostrar(organizan)} back={sorted(roles.PUEDEN_ORGANIZAR)}")
    rev("organizar y descartar son hoy el mismo conjunto (si se separan, la tabla de Roles necesita una fila con su propio conjunto)", roles.PUEDEN_ORGANIZAR == roles.PUEDEN_DESCARTAR)
    m = re.search(r"export function puedeVerOpcionDelEngrane\(.*?\{\s*return (.*?);\s*\}", front, re.S)
    regla = re.sub(r"\s+", " ", m.group(1)) if m else ""
    rev("el engrane: «modulo-de-uso» lo ve quien `puedeUsar` y todo lo demás quien `puedeAdministrar`",
        regla == "opcion === 'modulo-de-uso' ? puedeUsar(usuario) : puedeAdministrar(usuario)", regla or "no se pudo leer la función")
    m = re.search(r"export function puedeUsar\(.*?\{(.*?)\n\}", front, re.S)
    cuerpo = re.sub(r"\s+", " ", m.group(1)) if m else ""
    rev("`puedeUsar` es TODO el que tiene un rol (o es administrador de plataforma), no un subconjunto", "usuario.rol !== null" in cuerpo and "esAdminPlataforma" in cuerpo, cuerpo)
    tipo = re.search(r"export type OpcionDelEngrane =(.*?);", front, re.S)
    opciones_front = re.findall(r"'([a-z-]+)'", tipo.group(1)) if tipo else []

titulo("El Monitor (la tabla de Roles) dice lo mismo que el back")
if not MONITOR.exists():
    print(f"  SE SALTA: no está {MONITOR}")
    saltadas += 1
else:
    mon = MONITOR.read_text(encoding="utf-8")
    esperado = {
        "todos": set(roles.VALIDOS),
        "administran": set(roles.PUEDEN_ADMINISTRAR),
        "organizan": set(roles.PUEDEN_ORGANIZAR) | set(roles.PUEDEN_DESCARTAR),
        "soloAdmin": {roles.ADMIN},
        "ninguno": set(),
    }
    for clave, debe in esperado.items():
        leido = lista_ts(mon, clave)
        rev(f"QUIENES.{clave} = {sorted(debe)}", leido == debe, f"monitor={mostrar(leido)}")

    # El catálogo: los mismos siete códigos, con el nombre y la descripción del diseño que tiene el back.
    filas = re.findall(r"codigo: '([A-Z_]+)',\s*nombre: '([^']+)',\s*corto: '[^']*',\s*diseno:\s*'((?:[^'\\]|\\.)*)'", mon)
    rev("la tabla trae los mismos roles que el back, ni uno más ni uno menos", {f[0] for f in filas} == set(roles.VALIDOS) and len(filas) == len(roles.VALIDOS), str([f[0] for f in filas]))
    catalogo = {r["codigo"]: r for r in roles.CATALOGO}
    for codigo, nombre, diseno in filas:
        rev(f"{codigo}: se llama «{roles.nombre_de(codigo)}»", nombre == roles.nombre_de(codigo), f"monitor={nombre!r}")
        if codigo in catalogo:
            rev(f"{codigo}: su descripción del diseño es la del back (HU07)", diseno == catalogo[codigo]["descripcion"], f"monitor={diseno[:60]!r}…")

    # Las opciones del engrane: la tabla las marca con «(engrane)» y las reglas tienen que ser las del front.
    engrane = re.findall(r"nombre: '([^']*\(engrane\))',(?:\s*detalle: '[^']*',)?\s*quienes: '(\w+)'", mon)
    por_regla = {nombre: quienes for nombre, quienes in engrane}
    rev("la tabla marca como (engrane) tantas opciones como tiene el front", FRONT.exists() and len(engrane) == len(opciones_front), f"monitor={len(engrane)} front={len(opciones_front) if FRONT.exists() else '?'}")
    uso = [q for n, q in engrane if n.startswith("Módulo de uso")]
    resto = [q for n, q in engrane if not n.startswith("Módulo de uso")]
    rev("«Módulo de uso» es de `todos` en la tabla", uso == ["todos"], str(uso))
    rev("todas las demás opciones del engrane son de `administran` en la tabla", bool(resto) and all(q == "administran" for q in resto), str(por_regla))

print(f"\n{'TODO BIEN' if fallos == 0 else f'{fallos} FALLA(S)'}{f' ({saltadas} parte(s) saltada(s): falta un proyecto hermano)' if saltadas else ''}")
sys.exit(1 if fallos else 0)
