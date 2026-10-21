"""Organizaciones (tenants) — registro PROVISIONAL.

Lo definitivo es `security.tenants` con `uspCreateTenant`, que YA EXISTEN
(ver `repositorios/tenants.py`). No se usan todavía por una razón práctica: el
ambiente **dev no tiene base de datos** (`SQLSERVER_HOST` vacío a propósito,
ver `nexus_back_dev.conf`), así que contra SQL esta pantalla no se podría ni
probar. Mientras tanto vive en `tenants.jsonl` como las llaves, los webhooks y
los usuarios; cuando haya base dev, cambia ESTE módulo y nada más.

El contrato imita al de la base para que el cambio sea mecánico:

    crear      ↔ security.uspCreateTenant(@name, @slug, @settingsJson)
    listar     ↔ (falta un SP de listado; se le pedirá al DBA)
    por_guid   ↔ security.uspGetTenant(@tenantGuid)

## El nombre, el slug y el código

El diseño (HU02) pide avisar cuando el nombre ya existe: *"Detectamos
coincidencias con el nombre ingresado. Al continuar, se asignará
automáticamente un ID único para diferenciar este registro"*, y muestra dos
chips, **ID encontrado** e **ID asignado**. Eso es exactamente el `slug` de la
tabla: el nombre se puede repetir, el slug no. Al segundo "Seguros Monterrey"
le toca `seguros-monterrey-1`.

El `tenantCode` (`NEX-00001`) lo genera la base con su propia secuencia. Aquí
se imita con un contador sobre los códigos ya emitidos.
"""

import re
import unicodedata
import uuid
from datetime import datetime, timezone

from servicios import registro

ARCHIVO = "tenants.jsonl"

PREFIJO_CODIGO = "NEX"
_LARGO_NOMBRE = 200
_LARGO_SLUG = 63


class Duplicado(Exception):
    """Ya existe una organización con ese slug exacto."""


class NoEncontrado(Exception):
    pass


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def slug_de(nombre: str) -> str:
    """`"Seguros Monterrey"` → `"seguros-monterrey"`. Sin acentos ni signos, que
    es lo que acepta la columna (`varchar(63)`)."""
    sin_acentos = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    limpio = re.sub(r"[^a-zA-Z0-9]+", "-", sin_acentos).strip("-").lower()
    return limpio[:_LARGO_SLUG].strip("-")


def _indice() -> dict[str, dict]:
    """Las organizaciones vivas, por guid, en el orden en que se crearon."""
    tenants: dict[str, dict] = {}
    for e in registro.eventos(ARCHIVO):
        tipo = e.get("evento")
        guid = e.get("guid")
        if not isinstance(guid, str):
            continue
        if tipo == "creada":
            tenants.setdefault(
                guid,
                {
                    "guid": guid,
                    "nombre": e.get("nombre", ""),
                    "slug": e.get("slug", ""),
                    "codigo": e.get("codigo", ""),
                    "estado": "ACTIVE",
                    "adminGuid": e.get("adminGuid"),
                    "recuperacion": e.get("recuperacion") or {},
                    "creadaEn": e.get("en"),
                },
            )
        elif tipo == "estado" and guid in tenants:
            tenants[guid]["estado"] = e.get("estado", "ACTIVE")
    return tenants


def _siguiente_codigo(tenants: dict[str, dict]) -> str:
    usados = [t["codigo"] for t in tenants.values() if t["codigo"].startswith(f"{PREFIJO_CODIGO}-")]
    numeros = [int(c.split("-", 1)[1]) for c in usados if c.split("-", 1)[1].isdigit()]
    return f"{PREFIJO_CODIGO}-{max(numeros, default=0) + 1:05d}"


def _slug_libre(base: str, tomados: set[str]) -> str:
    """El slug del nombre, con `-1`, `-2`… si ya está tomado. Es el "ID
    asignado" que muestra el diseño."""
    if base not in tomados:
        return base
    for n in range(1, 1000):
        sufijo = f"-{n}"
        candidato = base[: _LARGO_SLUG - len(sufijo)] + sufijo
        if candidato not in tomados:
            return candidato
    raise Duplicado("Demasiadas organizaciones con ese nombre.")


def coincidencias(nombre: str) -> list[dict]:
    """Las organizaciones cuyo nombre se parece al que se está escribiendo, para
    el aviso del diseño. Compara por slug: así "Seguros Monterrey" y "seguros
    monterrey" son la misma coincidencia."""
    base = slug_de(nombre)
    if not base:
        return []
    return [
        {"guid": t["guid"], "nombre": t["nombre"], "slug": t["slug"], "codigo": t["codigo"]}
        for t in _indice().values()
        if t["slug"] == base or t["slug"].startswith(base + "-")
    ]


def slug_propuesto(nombre: str) -> str:
    """El "ID asignado" que tendría el nombre si se guardara ahora."""
    base = slug_de(nombre)
    if not base:
        return ""
    return _slug_libre(base, {t["slug"] for t in _indice().values()})


def crear(nombre: str, admin_guid: str, recuperacion: dict | None = None) -> dict:
    """Registra la organización y devuelve cómo quedó. El slug se deriva del
    nombre y se hace único; el código lo asigna la secuencia."""
    nombre = " ".join(nombre.split())
    if not nombre:
        raise ValueError("El nombre de la organización es obligatorio.")
    if len(nombre) > _LARGO_NOMBRE:
        raise ValueError(f"El nombre no puede pasar de {_LARGO_NOMBRE} caracteres.")
    base = slug_de(nombre)
    if not base:
        raise ValueError("El nombre debe tener al menos una letra o un número.")
    with registro.candado:
        tenants = _indice()
        slug = _slug_libre(base, {t["slug"] for t in tenants.values()})
        guid = str(uuid.uuid4())
        registro.agregar(
            ARCHIVO,
            {
                "evento": "creada",
                "guid": guid,
                "nombre": nombre,
                "slug": slug,
                "codigo": _siguiente_codigo(tenants),
                "adminGuid": admin_guid,
                "recuperacion": recuperacion or {},
                "en": _ahora().isoformat(timespec="seconds"),
            },
        )
    return por_guid(guid)


def listar() -> list[dict]:
    """Todas, de la más nueva a la más vieja (el listado las muestra así)."""
    # El registro guarda la hora al segundo: dos altas en el mismo segundo
    # empatan, y entonces manda el orden en que se escribieron.
    orden = list(_indice().values())
    return [t for _, t in sorted(enumerate(orden), key=lambda x: (x[1]["creadaEn"] or "", x[0]), reverse=True)]


def por_guid(guid: str) -> dict:
    t = _indice().get(guid)
    if not t:
        raise NoEncontrado(guid)
    return t


def cambiar_estado(guid: str, estado: str) -> dict:
    if estado not in {"ACTIVE", "SUSPENDED", "CLOSED"}:
        raise ValueError(f"Estado inválido: {estado}")
    with registro.candado:
        if guid not in _indice():
            raise NoEncontrado(guid)
        registro.agregar(
            ARCHIVO,
            {"evento": "estado", "guid": guid, "estado": estado, "en": _ahora().isoformat(timespec="seconds")},
        )
    return por_guid(guid)
