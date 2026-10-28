"""Lo que los DOS almacenamientos de tipos documentales comparten: qué es un tipo válido, cómo se ve hacia afuera y sus errores.

Aquí no hay nada que sepa dónde se guarda. Lo usan `tipos_documentales_archivo.py` (el JSONL del almacén) y
`tipos_documentales_sql.py` (la tabla `nexus.documentTypes`), y a través de ellos la puerta pública,
`servicios/tipos_documentales.py`, que es la que documenta el dominio.
"""

import json
import re
from datetime import datetime, timezone

ESTADOS = ("borrador", "activo", "archivado")

# El id lo usa el clasificador (`normalizar_nombre`, hasta 64 caracteres, [a-z0-9_-]) y Google nombra procesadores con él:
# solo minúsculas, dígitos y guiones, y que empiece con `tipo-`. La tabla lo exige también, con su CHECK.
RE_ID = re.compile(r"^tipo-[a-z0-9-]{6,58}$")

#: Tope del documento de un tipo, ya serializado. Un tipo normal pesa unos KB; con muchos ejemplos recortados, decenas.
TOPE_BYTES = 1_000_000

#: Lo que NO se acepta dentro de `datos`: lo pone el servidor.
RESERVADAS = frozenset({"revision", "creadoPor", "creadoEn", "modificadoPor", "modificadoEn", "tenant", "meta"})


class TipoInvalido(ValueError):
    """Lo que llegó no es un tipo documental guardable."""


class NoEncontrado(LookupError):
    """No hay tal tipo EN ESA organización (o no existe: no se distingue)."""


class Conflicto(Exception):
    """Alguien lo guardó antes: `revision_actual` es la vigente, o `None` si el id ya es de otro."""

    def __init__(self, revision_actual: int | None):
        super().__init__("conflicto de revisión")
        self.revision_actual = revision_actual


def ahora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def momento(valor) -> str | None:
    """Una fecha de SQL Server (`datetime`, en UTC porque así se guarda) con la misma forma que las del almacén."""
    if valor is None:
        return None
    if isinstance(valor, str):
        return valor
    return valor.strftime("%Y-%m-%dT%H:%M:%SZ")


def validar(tipo_id: str, datos: object) -> dict:
    """Devuelve los `datos` limpios (sin lo que pone el servidor) o levanta `TipoInvalido`."""
    if not isinstance(tipo_id, str) or not RE_ID.match(tipo_id):
        raise TipoInvalido("El identificador del tipo documental no es válido.")
    if not isinstance(datos, dict):
        raise TipoInvalido("Faltan los datos del tipo documental.")
    nombre = datos.get("nombre")
    if not isinstance(nombre, str) or not nombre.strip() or len(nombre) > 200:
        raise TipoInvalido("El tipo documental necesita un nombre de hasta 200 caracteres.")
    if not isinstance(datos.get("descripcion", ""), str):
        raise TipoInvalido("La descripción del tipo documental debe ser texto.")
    if datos.get("estado") not in ESTADOS:
        raise TipoInvalido("El estado del tipo documental no es válido.")
    if not isinstance(datos.get("procesadorId", ""), str):
        raise TipoInvalido("El procesador del tipo documental debe ser texto.")
    limpio = {k: v for k, v in datos.items() if k not in RESERVADAS and k != "id"}
    if len(json.dumps(limpio, ensure_ascii=False).encode("utf-8")) > TOPE_BYTES:
        raise TipoInvalido("El tipo documental es demasiado grande para guardarse.")
    return limpio


def validar_revision(revision: int) -> None:
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise TipoInvalido("La revisión no es válida.")


def publico(t: dict) -> dict:
    """La forma en que el tipo sale hacia el front. `t` trae id, tenant, revision, datos y las cuatro marcas de quién y cuándo."""
    return {
        "id": t["id"],
        "revision": t["revision"],
        "datos": {**t["datos"], "id": t["id"]},
        "creadoPor": t["creadoPor"],
        "creadoEn": t["creadoEn"],
        "modificadoPor": t["modificadoPor"],
        "modificadoEn": t["modificadoEn"],
    }


def activos(tipos: list[dict], tenant: str) -> list[dict]:
    """Los tipos ACTIVOS de una organización con lo que su clasificador necesita. `tipos` ya viene en orden de creación."""
    return [
        {"id": t["id"], "nombre": t["datos"]["nombre"].strip(), "descripcion": (t["datos"].get("descripcion") or "").strip()}
        for t in tipos
        if t["tenant"] == tenant and t["datos"].get("estado") == "activo"
    ]


def dueños_de_procesadores(tipos: list[dict]) -> dict[str, dict]:
    """`{procesadorId: {tenant, tipo, version}}` de cada procesador que algún tipo conoce, vigente o de su historial.

    Es lo que usa la limpieza de procesadores huérfanos: lo que NO está aquí no tiene dueño en ninguna organización. Por eso
    `tipos` tiene que traer los de TODAS las organizaciones, no los de una.
    """
    dueños: dict[str, dict] = {}
    for t in tipos:
        d = t["datos"]
        if d.get("procesadorId"):
            dueños[d["procesadorId"]] = {"tenant": t["tenant"], "tipo": t["id"], "version": d.get("version")}
        vistas = list(d.get("historialVersiones") or [])
        if isinstance(d.get("versionEnEdicion"), dict):
            vistas.append(d["versionEnEdicion"])
        for v in vistas:
            if isinstance(v, dict) and v.get("procesadorId"):
                dueños.setdefault(v["procesadorId"], {"tenant": t["tenant"], "tipo": t["id"], "version": v.get("version")})
    return dueños
