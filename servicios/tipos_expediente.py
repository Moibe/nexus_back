"""Registro de los TIPOS DE EXPEDIENTE de cada organización — registro PROVISIONAL, en JSON y con forma de tabla.

## Qué es un tipo de expediente

Es la CLASE de la que salen los expedientes: «expediente médico», «expediente de un choque». Se crea una vez; cada expediente de ese tipo es una
INSTANCIA: la carpeta de UN paciente, de UN choque, con SUS documentos (`servicios/expedientes.py`). El tipo no guarda documentos: los guardan
sus expedientes. Por ahora solo tiene un `nombre`; lo que contiene un tipo (qué tipos documentales espera, qué procesos le corren) se agrega
cuando haga falta, no antes.

## Esquema: qué documentos describe (sin restringir)

A un tipo de expediente se le pueden asignar TIPOS DOCUMENTALES de su misma organización (`servicios/tipos_documentales.py`) para describir su
esquema: «un expediente médico lleva una receta, un estudio de laboratorio…». Es una lista ordenada, sin repetidos, y es DESCRIPTIVA: **no limita
los archivos que un expediente puede recibir**. Cualquier documento entra a cualquier expediente; los que no encajan con el esquema simplemente
quedan «sin tipo documental». Hoy ningún documento guarda su tipo documental (la clasificación se hace en el navegador y no se persiste), así
que todos están en ese caso: asignarle uno a cada documento será otra entidad y otro paso. El esquema se reemplaza completo (`definir_esquema`).
Un tipo documental que se borra de la biblioteca después queda en el esquema como un id sin nombre, a la vista, hasta que alguien lo quite.

## Nombre

Único por organización, sin distinguir mayúsculas ni espacios de más: un catálogo con dos «Expediente médico» no sirve, y como todavía no se
pueden renombrar ni borrar, un repetido por error no tendría arreglo. Es una propuesta mía; si no se quiere, se quita la comprobación.

## Qué decide el servidor

El `id` (`tipo-expediente-` + 32 hexadecimales), `creadoPor` y `creadoEn`. El nombre se limpia (espacios de más) y tiene tope de 200 caracteres.

## Cómo se guarda

Un archivo de solo agregar por organización, `tipos-expediente-{tenant}.jsonl` (ver `servicios/registro.py`), con dos eventos: `crear` y `esquema`
(la lista completa de tipos documentales, que reemplaza a la anterior). El estado vigente se arma doblando el archivo. Un id de otra organización
no existe en su archivo y se contesta como uno que no existe.
"""

import re
import uuid
from datetime import datetime, timezone

from servicios import almacen, registro, tipos_documentales

MAX_NOMBRE = 200
#: Tope de tipos documentales en un esquema: un tipo de expediente normal describe unas decenas como mucho.
MAX_ESQUEMA = 50

_RE_ID = re.compile(r"^tipo-expediente-[0-9a-f]{32}$")


class TipoInvalido(ValueError):
    """Lo que llegó no se puede guardar (nombre vacío, esquema con repetidos…)."""


class NoEncontrado(LookupError):
    """No hay tal tipo de expediente EN ESA organización (o no existe: no se distingue)."""


class NombreRepetido(Exception):
    """Ya hay un tipo con ese nombre en la organización. `existente` es su id."""

    def __init__(self, existente: str):
        super().__init__("nombre repetido")
        self.existente = existente


def _nombre(tenant: str) -> str:
    # Valida el tenant con la MISMA regla que el almacén: es también la defensa contra un `../` en el nombre del archivo de registro.
    return f"tipos-expediente-{almacen._validar_tenant(tenant)}.jsonl"


def _ahora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _limpiar_nombre(nombre: object) -> str:
    if not isinstance(nombre, str):
        raise TipoInvalido("El nombre del tipo de expediente no es válido.")
    limpio = " ".join(nombre.split())
    if not limpio or len(limpio) > MAX_NOMBRE or not limpio.isprintable():
        raise TipoInvalido("El nombre del tipo de expediente no es válido.")
    return limpio


def _doblar(tenant: str) -> dict[str, dict]:
    """El estado vigente: `{id: tipo}`. Se llama con el candado puesto."""
    tipos: dict[str, dict] = {}
    for e in registro.eventos(_nombre(tenant)):
        op = e.get("op")
        if op == "crear" and isinstance(e.get("id"), str) and isinstance(e.get("nombre"), str):
            tipos[e["id"]] = {"id": e["id"], "nombre": e["nombre"], "tiposDocumentales": [], "creadoPor": e.get("por"), "creadoEn": e.get("en")}
        elif op == "esquema" and e.get("id") in tipos and isinstance(e.get("tiposDocumentales"), list):
            tipos[e["id"]]["tiposDocumentales"] = [i for i in e["tiposDocumentales"] if isinstance(i, str)]
    return tipos


def _clave(nombre: str) -> str:
    # Los nombres ya se guardan limpios (`_limpiar_nombre`): para compararlos solo falta no distinguir mayúsculas.
    return nombre.casefold()


def crear(tenant: str, nombre: object, por: str) -> dict:
    """Crea un tipo de expediente y lo devuelve. `TipoInvalido` si el nombre no sirve, `NombreRepetido` si ya hay uno igual."""
    limpio = _limpiar_nombre(nombre)
    tipo_id = f"tipo-expediente-{uuid.uuid4().hex}"
    en = _ahora()
    with registro.candado:
        for t in _doblar(tenant).values():
            if _clave(t["nombre"]) == _clave(limpio):
                raise NombreRepetido(t["id"])
        registro.agregar(_nombre(tenant), {"op": "crear", "id": tipo_id, "tenant": tenant, "nombre": limpio, "por": por, "en": en})
    return {"id": tipo_id, "nombre": limpio, "tiposDocumentales": [], "creadoPor": por, "creadoEn": en}


def definir_esquema(tenant: str, tipo_id: object, tipos_documentales_ids: object, por: str) -> tuple[dict, list[str]]:
    """Reemplaza el esquema de un tipo por esa lista de tipos documentales (ids, en ese orden). Devuelve `(el tipo como quedó, el esquema
    ANTERIOR)`; si la lista es la misma que ya tenía, no escribe nada.

    `NoEncontrado` si el tipo no es de esa organización; `TipoInvalido` si no es una lista de textos, si trae repetidos o de más, o si alguno no
    es un tipo documental de esa organización (uno de otra se contesta igual que uno que no existe)."""
    ids = tipos_documentales_ids
    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
        raise TipoInvalido("El esquema debe ser una lista de tipos documentales.")
    if len(ids) > MAX_ESQUEMA:
        raise TipoInvalido(f"Un tipo de expediente admite hasta {MAX_ESQUEMA} tipos documentales.")
    if len(set(ids)) != len(ids):
        raise TipoInvalido("Hay un tipo documental repetido en el esquema.")
    with registro.candado:
        actual = _doblar(tenant).get(tipo_id) if isinstance(tipo_id, str) and _RE_ID.match(tipo_id) else None
        if actual is None:
            raise NoEncontrado(tipo_id)
        existentes = {t["id"] for t in tipos_documentales.listar(tenant)}
        if any(i not in existentes for i in ids):
            raise TipoInvalido("Uno de los tipos documentales no existe en esta organización.")
        anterior = list(actual["tiposDocumentales"])
        if ids != anterior:
            registro.agregar(_nombre(tenant), {"op": "esquema", "id": tipo_id, "tenant": tenant, "tiposDocumentales": ids, "por": por, "en": _ahora()})
        return {**actual, "tiposDocumentales": list(ids)}, anterior


def nombres_de_tipos_documentales(tenant: str) -> dict[str, str | None]:
    """`{id: nombre}` de los tipos documentales de esa organización, para mostrar un esquema."""
    return {t["id"]: t["datos"].get("nombre") for t in tipos_documentales.listar(tenant)}


def con_esquema_resuelto(tipo: dict, nombres: dict[str, str | None]) -> dict:
    """El tipo como lo ve quien consulta: su esquema con `id` y `nombre` de cada tipo documental (`nombre: None` si ya no existe)."""
    return {**tipo, "tiposDocumentales": [{"id": i, "nombre": nombres.get(i)} for i in tipo.get("tiposDocumentales", [])]}


def listar(tenant: str) -> list[dict]:
    """Los tipos de UNA organización, en el orden de su creación."""
    with registro.candado:
        return list(_doblar(tenant).values())


def obtener(tenant: str, tipo_id: object) -> dict | None:
    """El tipo de esa organización, o `None` si no existe (o es de otra: no se distingue)."""
    if not isinstance(tipo_id, str) or not _RE_ID.match(tipo_id):
        return None
    with registro.candado:
        return _doblar(tenant).get(tipo_id)
