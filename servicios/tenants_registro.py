"""Organizaciones (tenants).

Viven en `nexus.tenants` (migración `0006`, `servicios/registros_sql.py`), importadas una vez desde el viejo `tenants.jsonl`
(`servicios/importacion_json.py`). Desde el 2026-11-11 no hay otro almacenamiento. El espacio de plataforma (`plataforma`, `csi`, `demo`)
NO es una organización y no se registra aquí (decisión de Moibe: «plataforma se trata aparte»).

## El nombre, el slug y el código

El diseño (HU02) pide avisar cuando el nombre ya existe: *"Detectamos
coincidencias con el nombre ingresado. Al continuar, se asignará
automáticamente un ID único para diferenciar este registro"*, y muestra dos
chips, **ID encontrado** e **ID asignado**. Eso es exactamente el `slug` de la
tabla: el nombre se puede repetir, el slug no. Al segundo "Seguros Monterrey"
le toca `seguros-monterrey-1`.

El `tenantCode` (`NEX-00001`) se asigna con un contador sobre los códigos ya
emitidos (`registros_sql.orgs_crear`).
"""

import re
import unicodedata

from servicios import registros_sql

PREFIJO_CODIGO = "NEX"
_LARGO_NOMBRE = 200
_LARGO_SLUG = 63


class Duplicado(Exception):
    """Ya existe una organización con ese slug exacto."""


class NoEncontrado(Exception):
    pass


def slug_de(nombre: str) -> str:
    """`"Seguros Monterrey"` → `"seguros-monterrey"`. Sin acentos ni signos, que
    es lo que acepta la columna (`varchar(63)`)."""
    sin_acentos = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    limpio = re.sub(r"[^a-zA-Z0-9]+", "-", sin_acentos).strip("-").lower()
    return limpio[:_LARGO_SLUG].strip("-")


def _todas() -> list[dict]:
    """Todas, en el orden en que se crearon."""
    return list(reversed(registros_sql.orgs_listar()))


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
        for t in _todas()
        if t["slug"] == base or t["slug"].startswith(base + "-")
    ]


def slug_propuesto(nombre: str) -> str:
    """El "ID asignado" que tendría el nombre si se guardara ahora."""
    base = slug_de(nombre)
    if not base:
        return ""
    return _slug_libre(base, {t["slug"] for t in _todas()})


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
    return por_guid(registros_sql.orgs_crear(nombre, base, admin_guid, recuperacion or {}, _slug_libre, PREFIJO_CODIGO))


def listar() -> list[dict]:
    """Todas, de la más nueva a la más vieja (el listado las muestra así)."""
    return registros_sql.orgs_listar()


def por_guid(guid: str) -> dict:
    t = registros_sql.orgs_por_guid(guid) if isinstance(guid, str) else None
    if not t:
        raise NoEncontrado(guid)
    return t


def cambiar_estado(guid: str, estado: str) -> dict:
    if estado not in {"ACTIVE", "SUSPENDED", "CLOSED"}:
        raise ValueError(f"Estado inválido: {estado}")
    if not registros_sql.orgs_cambiar_estado(guid, estado):
        raise NoEncontrado(guid)
    return por_guid(guid)
