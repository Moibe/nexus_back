"""Registro de los TIPOS DOCUMENTALES de cada organización — registro PROVISIONAL.

## Qué cambia (2026-10-15)

Hasta hoy el catálogo de tipos documentales (nombre, campos, estado, el procesador de Document AI de cada uno, el historial de
versiones, los ejemplos recortados) vivía en el `localStorage` de CADA navegador. Eso producía tres males:

- cada computadora tenía su propia biblioteca, y dos personas de la misma organización no se veían;
- cada navegador inventaba sus ids, así que el mismo "INE" dado de alta en dos máquinas creaba dos extractores en Google;
- el clasificador (que es UNO para todo el sistema) se sincronizaba con la lista de tipos del navegador que lo tocara
  último, y los tipos de otra computadora dejaban de clasificarse.

Ahora el catálogo vive AQUÍ, por organización. El navegador lo lee y lo escribe a través del BFF; el clasificador se
sincroniza desde este registro (`tipos_activos`), no desde lo que mande el front.

## Qué es un tipo

El back lo trata casi como una caja: `datos` es el objeto que el front ya manejaba (campos, pasos, historial, recortes…) y
se guarda tal cual. Solo se EXIGE y se lee lo que el back necesita para algo:

- `nombre` y `descripcion` — son las categorías del clasificador (la descripción es prompt real para el modelo);
- `estado` — `borrador`, `activo` o `archivado`: solo los `activo` entran al clasificador;
- `procesadorId` — para saber qué procesadores de Google tienen dueño (`servicios/procesadores_huerfanos.py`).

Lo que el servidor pone y el cliente no puede escribir: `revision`, `creadoPor`, `creadoEn`, `modificadoPor`, `modificadoEn`
y `tenant`. Se guardan junto al tipo, nunca dentro de `datos`.

## Revisión (dos personas editando lo mismo)

Cada tipo lleva un número de `revision`. Para guardar hay que decir sobre cuál se editó: si en el ínterin otra persona lo
guardó, es un 409 con la revisión vigente, y NO se pisa nada en silencio. `0` significa «es nuevo».

## Ids

Los inventa el cliente (`tipo-` + 32 hexadecimales de un UUID) y el servidor exige que no se repitan EN NINGUNA organización:
el clasificador es global y nombra cada categoría con el id, así que dos tipos con el mismo id, de organizaciones distintas,
serían la misma categoría. Un id que ya existe en OTRA organización se contesta igual que uno repetido en la propia, sin
nombrar de quién es.

## Cómo se guarda

Un solo archivo de solo-agregar (`tipos-documentales.jsonl`, ver `servicios/registro.py`), con tres clases de evento:
`guardar` (el tipo COMPLETO, con su revisión nueva), `eliminar` y `orden` (el orden de la biblioteca de una organización).
El estado vigente se arma doblando el archivo de principio a fin.

⚠️ Cada guardado escribe el tipo entero: un tipo con muchos recortes pesa decenas de KB y el archivo crece con cada edición.
Con el uso de hoy basta; si crece, la salida es compactar (reescribir solo el último evento de cada tipo) o la base.
"""

import json
import logging
import re
from datetime import datetime, timezone

from servicios import registro

logger = logging.getLogger(__name__)

ARCHIVO = "tipos-documentales.jsonl"

ESTADOS = ("borrador", "activo", "archivado")

# El id lo usa el clasificador (`normalizar_nombre`, hasta 64 caracteres, [a-z0-9_-]) y Google nombra procesadores con él:
# solo minúsculas, dígitos y guiones, y que empiece con `tipo-`.
_RE_ID = re.compile(r"^tipo-[a-z0-9-]{6,58}$")

#: Tope del documento de un tipo, ya serializado. Un tipo normal pesa unos KB; con muchos ejemplos recortados, decenas.
TOPE_BYTES = 1_000_000

#: Lo que NO se acepta dentro de `datos`: lo pone el servidor.
_RESERVADAS = frozenset({"revision", "creadoPor", "creadoEn", "modificadoPor", "modificadoEn", "tenant", "meta"})


class TipoInvalido(ValueError):
    """Lo que llegó no es un tipo documental guardable."""


class NoEncontrado(LookupError):
    """No hay tal tipo EN ESA organización (o no existe: no se distingue)."""


class Conflicto(Exception):
    """Alguien lo guardó antes: `revision_actual` es la vigente, o `None` si el id ya es de otro."""

    def __init__(self, revision_actual: int | None):
        super().__init__("conflicto de revisión")
        self.revision_actual = revision_actual


def _ahora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _doblar() -> tuple[dict[str, dict], dict[str, list[str]]]:
    """El estado vigente: `{id: tipo}` y `{tenant: [ids en el orden pedido]}`. Se llama con el candado puesto."""
    tipos: dict[str, dict] = {}
    ordenes: dict[str, list[str]] = {}
    for e in registro.eventos(ARCHIVO):
        op = e.get("op")
        if op == "guardar" and isinstance(e.get("id"), str) and isinstance(e.get("datos"), dict):
            previo = tipos.get(e["id"])
            tipos[e["id"]] = {
                "id": e["id"],
                "tenant": e.get("tenant", ""),
                "revision": int(e.get("revision", 1)),
                "datos": e["datos"],
                "creadoPor": previo["creadoPor"] if previo else e.get("por"),
                "creadoEn": previo["creadoEn"] if previo else e.get("en"),
                "modificadoPor": e.get("por"),
                "modificadoEn": e.get("en"),
            }
        elif op == "eliminar":
            tipos.pop(e.get("id"), None)
        elif op == "orden" and isinstance(e.get("ids"), list):
            ordenes[e.get("tenant", "")] = [i for i in e["ids"] if isinstance(i, str)]
    return tipos, ordenes


def _validar(tipo_id: str, datos: object) -> dict:
    if not isinstance(tipo_id, str) or not _RE_ID.match(tipo_id):
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
    limpio = {k: v for k, v in datos.items() if k not in _RESERVADAS and k != "id"}
    if len(json.dumps(limpio, ensure_ascii=False).encode("utf-8")) > TOPE_BYTES:
        raise TipoInvalido("El tipo documental es demasiado grande para guardarse.")
    return limpio


def _publico(t: dict) -> dict:
    return {
        "id": t["id"],
        "revision": t["revision"],
        "datos": {**t["datos"], "id": t["id"]},
        "creadoPor": t["creadoPor"],
        "creadoEn": t["creadoEn"],
        "modificadoPor": t["modificadoPor"],
        "modificadoEn": t["modificadoEn"],
    }


def listar(tenant: str) -> list[dict]:
    """Los tipos de UNA organización, en el orden que ella les dio (los nuevos al final, en el de su creación)."""
    with registro.candado:
        tipos, ordenes = _doblar()
    suyos = [t for t in tipos.values() if t["tenant"] == tenant]
    pedido = {tipo_id: i for i, tipo_id in enumerate(ordenes.get(tenant, []))}
    # `sorted` es estable: los que no están en el orden pedido conservan el de creación entre sí y van al final.
    suyos.sort(key=lambda t: pedido.get(t["id"], len(pedido)))
    return [_publico(t) for t in suyos]


def obtener(tenant: str, tipo_id: str) -> dict | None:
    with registro.candado:
        tipos, _ = _doblar()
    t = tipos.get(tipo_id)
    return _publico(t) if t and t["tenant"] == tenant else None


def guardar(tenant: str, tipo_id: str, datos: object, revision: int, por: str) -> tuple[dict, dict | None]:
    """Crea (`revision == 0`) o actualiza (`revision` = la que se editó) un tipo.

    Devuelve `(el tipo como quedó, el tipo ANTERIOR o None)`; el anterior es para que quien llama anote el cambio de estado.
    Levanta `TipoInvalido`, `NoEncontrado` (actualizar uno que no es de esta organización) o `Conflicto`.
    """
    limpio = _validar(tipo_id, datos)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise TipoInvalido("La revisión no es válida.")
    with registro.candado:
        tipos, _ = _doblar()
        actual = tipos.get(tipo_id)
        if revision == 0:
            if actual is not None:
                # Repetido en esta organización: se dice cuál es la vigente. En OTRA: no se nombra de quién es.
                raise Conflicto(actual["revision"] if actual["tenant"] == tenant else None)
            nueva = 1
        else:
            if actual is None or actual["tenant"] != tenant:
                raise NoEncontrado(tipo_id)
            if actual["revision"] != revision:
                raise Conflicto(actual["revision"])
            nueva = revision + 1
        registro.agregar(
            ARCHIVO,
            {"op": "guardar", "id": tipo_id, "tenant": tenant, "revision": nueva, "datos": limpio, "por": por, "en": _ahora()},
        )
        tipos, _ = _doblar()
        return _publico(tipos[tipo_id]), _publico(actual) if actual else None


def eliminar(tenant: str, tipo_id: str, por: str) -> dict:
    """Quita un tipo de la organización. Devuelve el que se quitó. `NoEncontrado` si no es de ella."""
    with registro.candado:
        tipos, _ = _doblar()
        actual = tipos.get(tipo_id)
        if actual is None or actual["tenant"] != tenant:
            raise NoEncontrado(tipo_id)
        registro.agregar(ARCHIVO, {"op": "eliminar", "id": tipo_id, "tenant": tenant, "por": por, "en": _ahora()})
        return _publico(actual)


def ordenar(tenant: str, ids: list[str], por: str) -> None:
    """Fija el orden de la biblioteca de una organización. Solo cuentan los ids que son SUYOS: el resto se ignora."""
    with registro.candado:
        tipos, _ = _doblar()
        propios = [i for i in ids if isinstance(i, str) and i in tipos and tipos[i]["tenant"] == tenant]
        # Sin repetidos, conservando el orden en que llegaron.
        unicos = list(dict.fromkeys(propios))
        registro.agregar(ARCHIVO, {"op": "orden", "tenant": tenant, "ids": unicos, "por": por, "en": _ahora()})


def tipos_activos() -> list[dict]:
    """Los tipos ACTIVOS de TODAS las organizaciones, con lo que el clasificador necesita de cada uno.

    El clasificador es único para el sistema, así que arma sus categorías con los de todas. El orden es el de creación.
    """
    with registro.candado:
        tipos, _ = _doblar()
    return [
        {"id": t["id"], "nombre": t["datos"]["nombre"].strip(), "descripcion": (t["datos"].get("descripcion") or "").strip()}
        for t in tipos.values()
        if t["datos"].get("estado") == "activo"
    ]


def procesadores_con_dueno() -> dict[str, dict]:
    """`{procesadorId: {tenant, tipo, version}}` de cada procesador que algún tipo conoce, vigente o de su historial.

    Es lo que usa la limpieza de procesadores huérfanos: lo que NO está aquí no tiene dueño en ninguna organización.
    """
    with registro.candado:
        tipos, _ = _doblar()
    dueños: dict[str, dict] = {}
    for t in tipos.values():
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
