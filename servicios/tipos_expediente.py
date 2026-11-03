"""Registro de los TIPOS DE EXPEDIENTE VIRTUAL de cada organización.

## Qué es un tipo de expediente virtual (desde el 2026-10-29)

Hasta el 2026-10-29 era el «tipo de expediente»: la clase de la que salían los expedientes. Ese día cambió el modelo (decisión de producto de
Moibe): el expediente es solo un receptáculo de documentos, sin tipo, y los tipos son de EXPEDIENTE VIRTUAL (`servicios/expedientes_virtuales.py`):
un subconjunto de los documentos de un expediente, por ejemplo la «Solicitud de crédito» de Juan Pérez con su INE, su comprobante y su estado de
cuenta. El tipo sigue siendo lo mismo —un `nombre` y un esquema de tipos documentales—, así que el módulo, el id (`tipo-expediente-…`), la ruta
(`/tipos-expediente`) y lo ya dado de alta se conservan; lo que cambió es de QUÉ es instancia. Lo de abajo dice «expediente» donde hoy debe
leerse «expediente virtual».

## Esquema: qué documentos describe (sin restringir)

Un tipo de expediente puede tener VARIOS TIPOS DOCUMENTALES de su misma organización (`servicios/tipos_documentales.py`), y ese conjunto es su
esquema: «un expediente médico lleva una receta, un estudio de laboratorio…». Es una lista ordenada, sin repetidos, y es DESCRIPTIVA: **no limita
los archivos que un expediente puede recibir**. Cualquier documento entra a cualquier expediente; los que no encajan con el esquema simplemente
quedan «sin tipo documental». Hoy ningún documento guarda su tipo documental (la clasificación se hace en el navegador y no se persiste), así
que todos están en ese caso: asignarle uno a cada documento será otra entidad y otro paso. El esquema se reemplaza completo (`editar`).
Un tipo documental que se borra de la biblioteca después queda en el esquema como un id sin nombre, a la vista, hasta que alguien lo quite.

**El esquema se define AL CREAR el tipo** (pedido de producto, 2026-10-25): `crear` exige al menos un tipo documental, así que ningún tipo nuevo
nace sin esquema (sin repetidos, hasta `MAX_ESQUEMA`, todos de esa organización). El tipo y su esquema se guardan en UN solo evento `crear`: o
queda el tipo con su esquema o no queda nada, nunca un tipo a medias. **Y se puede cambiar después** (`editar`, también del mismo día): la lista
nueva reemplaza a la anterior y pide lo mismo que al crear, de modo que un esquema NUNCA se deja vacío. Los tipos creados antes de la regla, sin
esquema, siguen como estaban —su evento `crear` no trae `tiposDocumentales` y se lee como una lista vacía— hasta que alguien les ponga uno.

## Nombre

Único por organización, sin distinguir mayúsculas ni espacios de más: un catálogo con dos «Expediente médico» no sirve. Es una propuesta mía; si
no se quiere, se quita la comprobación.

## Editar y borrar

**Editar** cambia el nombre y/o el esquema en una sola llamada (`editar`) y deja el `id`: los expedientes guardan el id de su tipo, así que ven el
nombre nuevo sin tocarlos, y cambiar el esquema no toca los documentos de sus expedientes (describe, no restringe). El nombre sigue siendo único:
otro tipo no puede llamarse igual, pero el tipo SÍ puede cambiarse solo las mayúsculas o los espacios de su propio nombre (no se repite consigo
mismo). Todo se comprueba antes de escribir: si algo falla, no cambia ni el nombre ni el esquema. Lo que, ya limpio, es lo que había, no escribe nada.
Si cambian los dos van DOS eventos seguidos, con el candado puesto: una caída justo entre ellos dejaría el nombre nuevo con el esquema anterior
(un tipo válido) y repetir la edición lo completa; a diferencia de `crear`, ahí no hay un estado que la regla prohíba.

**Borrar** solo se puede si el tipo NO tiene expedientes virtuales (`TipoConExpedientes`): todo expediente virtual es instancia de un tipo. Para
borrarlo hay que borrar antes sus expedientes virtuales (eso sí se puede, y no toca documentos). En SQL la cuenta y el borrado van con el tipo
bloqueado, el mismo bloqueo con que se crea un expediente virtual, así que uno de ese tipo no se cuela entre una cosa y la otra. Borrar libera el
nombre. (En el almacén no hay expedientes virtuales: ahí siempre se puede.)

## Qué decide el servidor

El `id` (`tipo-expediente-` + 32 hexadecimales), `creadoPor` y `creadoEn`. El nombre se limpia (espacios de más) y tiene tope de 200 caracteres.

## Cómo se guarda

Desde el 2026-10-29, donde hay base, en `nexus.virtualCaseTypes` y su esquema en `nexus.virtualCaseTypeDocumentTypes` (migraciones `0003` y `0004`, ver
`servicios/expedientes_sql.py`), con una llave foránea REAL a los tipos documentales: allí borrar un tipo documental lo QUITA de los
esquemas, en vez de dejarlo como un id sin nombre. Este módulo valida igual en los dos casos y solo cambia dónde se escribe; se elige por
`SQLSERVER_HOST` en cada llamada. Lo de abajo es el almacén, lo que corre donde no hay base (la laptop, las pruebas offline):

Un archivo de solo agregar por organización, `tipos-expediente-{tenant}.jsonl` (ver `servicios/registro.py`), con cuatro eventos: `crear` (con el
esquema inicial en `tiposDocumentales`), `esquema` (la lista completa de tipos documentales, que reemplaza a la anterior), `renombrar` (el nombre
nuevo) y `borrar`. El estado vigente se arma doblando el archivo. Un id de otra organización no existe en su archivo y se contesta como uno que no
existe.
"""

import re
import uuid
from datetime import datetime, timezone

from config import SQLSERVER_HOST
from servicios import almacen, registro, tipos_documentales


def _sql():
    """El módulo de SQL si el `.env` dice que hay base; `None` si no. Se importa tarde: sin base, `pyodbc` ni se carga."""
    if not SQLSERVER_HOST:
        return None
    from servicios import expedientes_sql

    return expedientes_sql


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


class TipoConExpedientes(Exception):
    """El tipo tiene expedientes virtuales y por eso no se puede borrar. `total` es cuántos tiene."""

    def __init__(self, total: int):
        super().__init__("tipo con expedientes")
        self.total = total


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
            # Los tipos creados antes de que el esquema se pidiera al crear no traen `tiposDocumentales`: se leen sin esquema.
            esquema = e.get("tiposDocumentales")
            tipos[e["id"]] = {
                "id": e["id"],
                "nombre": e["nombre"],
                "tiposDocumentales": [i for i in esquema if isinstance(i, str)] if isinstance(esquema, list) else [],
                "creadoPor": e.get("por"),
                "creadoEn": e.get("en"),
            }
        elif op == "esquema" and e.get("id") in tipos and isinstance(e.get("tiposDocumentales"), list):
            tipos[e["id"]]["tiposDocumentales"] = [i for i in e["tiposDocumentales"] if isinstance(i, str)]
        elif op == "renombrar" and isinstance(e.get("id"), str) and e["id"] in tipos and isinstance(e.get("nombre"), str):
            tipos[e["id"]]["nombre"] = e["nombre"]
        elif op == "borrar" and isinstance(e.get("id"), str):
            tipos.pop(e["id"], None)
    return tipos


def _clave(nombre: str) -> str:
    # Los nombres ya se guardan limpios (`_limpiar_nombre`): para compararlos solo falta no distinguir mayúsculas.
    return nombre.casefold()


def _validar_esquema(ids: object) -> list[str]:
    """El esquema como lista de ids. `TipoInvalido` si no es una lista de textos, si trae de más o si trae repetidos."""
    if not isinstance(ids, list) or any(not isinstance(i, str) for i in ids):
        raise TipoInvalido("El esquema debe ser una lista de tipos documentales.")
    if len(ids) > MAX_ESQUEMA:
        raise TipoInvalido(f"Un tipo de expediente admite hasta {MAX_ESQUEMA} tipos documentales.")
    if len(set(ids)) != len(ids):
        raise TipoInvalido("Hay un tipo documental repetido en el esquema.")
    return list(ids)


def _comprobar_que_existen(tenant: str, ids: list[str]) -> None:
    """`TipoInvalido` si alguno no es un tipo documental de esa organización (uno de otra se contesta igual que uno que no existe)."""
    existentes = {t["id"] for t in tipos_documentales.listar(tenant)}
    if any(i not in existentes for i in ids):
        raise TipoInvalido("Uno de los tipos documentales no existe en esta organización.")


def crear(tenant: str, nombre: object, tipos_documentales_ids: object, por: str) -> dict:
    """Crea un tipo de expediente CON su esquema y lo devuelve.

    Todo tipo nace con los tipos documentales que lo describen: al menos uno, sin repetidos, hasta `MAX_ESQUEMA` y todos de esa organización
    (`TipoInvalido` si no es así; una lista vacía también se rechaza). `TipoInvalido` si el nombre no sirve y `NombreRepetido` si ya hay uno igual.
    El tipo y su esquema van en UN solo evento: no queda un tipo sin esquema por una caída entre dos escrituras."""
    limpio = _limpiar_nombre(nombre)
    ids = _validar_esquema(tipos_documentales_ids)
    if not ids:
        raise TipoInvalido("Un tipo de expediente necesita al menos un tipo documental en su esquema.")
    if sql := _sql():
        return sql.tipos_crear(almacen._validar_tenant(tenant), limpio, ids, por)
    tipo_id = f"tipo-expediente-{uuid.uuid4().hex}"
    en = _ahora()
    with registro.candado:
        for t in _doblar(tenant).values():
            if _clave(t["nombre"]) == _clave(limpio):
                raise NombreRepetido(t["id"])
        _comprobar_que_existen(tenant, ids)
        registro.agregar(_nombre(tenant), {"op": "crear", "id": tipo_id, "tenant": tenant, "nombre": limpio, "tiposDocumentales": ids, "por": por, "en": en})
    return {"id": tipo_id, "nombre": limpio, "tiposDocumentales": list(ids), "creadoPor": por, "creadoEn": en}


def editar(tenant: str, tipo_id: object, nombre: object, tipos_documentales_ids: object, por: str) -> tuple[dict, str, list[str]]:
    """Cambia el nombre y/o el esquema de un tipo en una sola llamada y devuelve `(el tipo como quedó, el nombre ANTERIOR, el esquema ANTERIOR)`.

    Lo que viene como `None` no se toca, y al menos uno de los dos tiene que venir (`TipoInvalido`). El nombre se limpia y no puede repetir el de
    OTRO tipo (`NombreRepetido`; cambiarse solo las mayúsculas o los espacios del propio nombre sí se puede: el tipo no se repite consigo mismo).
    El esquema REEMPLAZA al anterior y pide, como al crear, al menos un tipo documental, sin repetidos, hasta `MAX_ESQUEMA` y todos de esa
    organización (`TipoInvalido`; uno de otra se contesta igual que uno que no existe). `NoEncontrado` si el tipo no es de esa organización.

    TODO se comprueba antes de escribir nada, con el candado puesto: si el nombre repite el de otro tipo o un tipo documental no existe, no cambia
    ni lo uno ni lo otro. Lo que ya estaba igual no escribe nada (el nombre ya limpio, o la misma lista en el mismo orden). Si cambian los dos
    son dos eventos seguidos (`renombrar` y luego `esquema`)."""
    if nombre is None and tipos_documentales_ids is None:
        raise TipoInvalido("Indica qué cambiar: el nombre o los tipos documentales.")
    limpio = _limpiar_nombre(nombre) if nombre is not None else None
    ids = _validar_esquema(tipos_documentales_ids) if tipos_documentales_ids is not None else None
    if ids is not None and not ids:
        raise TipoInvalido("Un tipo de expediente necesita al menos un tipo documental en su esquema.")
    if sql := _sql():
        if not isinstance(tipo_id, str) or not _RE_ID.match(tipo_id):
            raise NoEncontrado(tipo_id)
        return sql.tipos_editar(almacen._validar_tenant(tenant), tipo_id, limpio, ids, por)
    with registro.candado:
        tipos = _doblar(tenant)
        actual = tipos.get(tipo_id) if isinstance(tipo_id, str) and _RE_ID.match(tipo_id) else None
        if actual is None:
            raise NoEncontrado(tipo_id)
        if limpio is not None:
            for t in tipos.values():
                if t["id"] != actual["id"] and _clave(t["nombre"]) == _clave(limpio):
                    raise NombreRepetido(t["id"])
        if ids is not None:
            _comprobar_que_existen(tenant, ids)
        nombre_anterior = actual["nombre"]
        esquema_anterior = list(actual["tiposDocumentales"])
        en = _ahora()
        if limpio is not None and limpio != nombre_anterior:
            registro.agregar(_nombre(tenant), {"op": "renombrar", "id": tipo_id, "tenant": tenant, "nombre": limpio, "por": por, "en": en})
        if ids is not None and ids != esquema_anterior:
            registro.agregar(_nombre(tenant), {"op": "esquema", "id": tipo_id, "tenant": tenant, "tiposDocumentales": ids, "por": por, "en": en})
        return (
            {**actual, "nombre": nombre_anterior if limpio is None else limpio, "tiposDocumentales": esquema_anterior if ids is None else list(ids)},
            nombre_anterior,
            esquema_anterior,
        )


def borrar(tenant: str, tipo_id: object, por: str) -> dict:
    """Borra un tipo SIN expedientes virtuales y lo devuelve como estaba (para anotar qué se borró).

    `NoEncontrado` si no es de esa organización; `TipoConExpedientes` si todavía tiene: todo expediente virtual es instancia de un tipo, así que
    uno con expedientes virtuales no puede desaparecer. En SQL la cuenta y el borrado van en la misma transacción, con el tipo bloqueado. En el
    almacén no hay expedientes virtuales (solo viven en SQL), así que ahí siempre se puede."""
    if sql := _sql():
        if not isinstance(tipo_id, str) or not _RE_ID.match(tipo_id):
            raise NoEncontrado(tipo_id)
        return sql.tipos_borrar(almacen._validar_tenant(tenant), tipo_id)
    with registro.candado:
        actual = _doblar(tenant).get(tipo_id) if isinstance(tipo_id, str) and _RE_ID.match(tipo_id) else None
        if actual is None:
            raise NoEncontrado(tipo_id)
        registro.agregar(_nombre(tenant), {"op": "borrar", "id": tipo_id, "tenant": tenant, "por": por, "en": _ahora()})
        return actual


def nombres_de_tipos_documentales(tenant: str) -> dict[str, str | None]:
    """`{id: nombre}` de los tipos documentales de esa organización, para mostrar un esquema."""
    return {t["id"]: t["datos"].get("nombre") for t in tipos_documentales.listar(tenant)}


def con_esquema_resuelto(tipo: dict, nombres: dict[str, str | None]) -> dict:
    """El tipo como lo ve quien consulta: su esquema con `id` y `nombre` de cada tipo documental (`nombre: None` si ya no existe)."""
    return {**tipo, "tiposDocumentales": [{"id": i, "nombre": nombres.get(i)} for i in tipo.get("tiposDocumentales", [])]}


def listar(tenant: str) -> list[dict]:
    """Los tipos de UNA organización, en el orden de su creación."""
    if sql := _sql():
        return sql.tipos_listar(almacen._validar_tenant(tenant))
    with registro.candado:
        return list(_doblar(tenant).values())


def obtener(tenant: str, tipo_id: object) -> dict | None:
    """El tipo de esa organización, o `None` si no existe (o es de otra: no se distingue)."""
    if not isinstance(tipo_id, str) or not _RE_ID.match(tipo_id):
        return None
    if sql := _sql():
        return sql.tipos_obtener(almacen._validar_tenant(tenant), tipo_id)
    with registro.candado:
        return _doblar(tenant).get(tipo_id)
