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

#: Largo máximo de las «instrucciones para la extracción» de un tipo (`datos.instrucciones`, opcionales). Viajan a Google como el
#: `documentPrompt` del extractor (ver `esquema.esquema_desde_campos`): un párrafo de contexto, no un manual.
MAX_INSTRUCCIONES = 1000

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


class NombreRepetido(Exception):
    """Ya hay OTRO tipo documental con ese nombre en la organización. Ver `exigir_nombre_libre`."""

    def __init__(self):
        super().__init__("nombre repetido")


def clave_de_nombre(nombre: object) -> str:
    """Con qué se compara un nombre para saber si choca con otro: sin espacios de más y sin distinguir mayúsculas.

    Es la misma regla que ya usan los procesos. «Cédula  profesional » y «CÉDULA PROFESIONAL» chocan;
    «Cedula profesional» (sin acento) no: es otra palabra.
    """
    return " ".join(str(nombre).split()).casefold()


def exigir_nombre_libre(nuevo: dict, actual: dict | None, otros: list[dict]) -> None:
    """`NombreRepetido` si al guardar `nuevo` el tipo pasa a OCUPAR un nombre que ya ocupa otro tipo de la organización.

    La regla (Moibe, 2026-11-04): el nombre de un tipo documental es único en su organización. Dos tipos con el mismo nombre son dos
    categorías iguales para el clasificador (el nombre es su `displayName`, y la descripción suele ser parecida) y no se distinguen
    en ningún lado donde se listan por nombre: la Biblioteca, los menús del motor de reglas o la columna «Tipo documental» de un
    expediente.

    - Un tipo ARCHIVADO no ocupa nombre: no se lista ni entra al clasificador, así que su nombre se puede volver a usar. Guardar uno
      archivado nunca choca, y los archivados de `otros` no cuentan.
    - Solo se revisa cuando el tipo ENTRA a un nombre: al crearse, al cambiar de nombre (más allá de mayúsculas o espacios) o al
      dejar de estar archivado. Guardar sin tocar el nombre —activarlo, subirle un ejemplo— no puede fallar por un
      duplicado que ya existía antes de esta regla (el mismo criterio que en procesos); esos se resuelven renombrando uno.

    `nuevo` y `actual` son los `datos` (el que llega y el vigente, o `None` si el tipo es nuevo); `otros`, los `datos` de los DEMÁS
    tipos de la organización (al menos `nombre` y `estado`). Quien llama sabe si hace falta leer `otros`: `entra_a_un_nombre`.
    """
    if not entra_a_un_nombre(nuevo, actual):
        return
    clave = clave_de_nombre(nuevo.get("nombre", ""))
    if any(o.get("estado") != "archivado" and clave_de_nombre(o.get("nombre", "")) == clave for o in otros):
        raise NombreRepetido()


def entra_a_un_nombre(nuevo: dict, actual: dict | None) -> bool:
    """Si este guardado hace que el tipo pase a ocupar un nombre que antes no ocupaba (ver `exigir_nombre_libre`).

    Separado para que el almacenamiento en SQL no lea —ni bloquee— los demás tipos de la organización en cada guardado: la mayoría
    (el asistente guarda con cada tecla) no tocan el nombre.
    """
    if nuevo.get("estado") == "archivado":
        return False
    if actual is None or actual.get("estado") == "archivado":
        return True
    return clave_de_nombre(actual.get("nombre", "")) != clave_de_nombre(nuevo.get("nombre", ""))


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
    instrucciones = datos.get("instrucciones", "")
    if not isinstance(instrucciones, str) or len(instrucciones) > MAX_INSTRUCCIONES:
        raise TipoInvalido(f"Las instrucciones para la extracción deben ser texto de hasta {MAX_INSTRUCCIONES} caracteres.")
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
