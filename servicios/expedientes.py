"""Registro de los EXPEDIENTES de cada organización — registro PROVISIONAL, en JSON y con forma de tabla.

## Qué es un expediente

Un expediente es un conjunto de documentos (el motor de reglas corre un proceso sobre uno: `servicios/procesos.py`) y es una INSTANCIA de un
tipo de expediente (`servicios/tipos_expediente.py`): el tipo «expediente médico» es la clase y el expediente de UN paciente es la carpeta
con SUS documentos. Todo expediente nace de un tipo de su misma organización. Además del tipo solo tiene un `nombre`; estado, folio o una
referencia del cliente se agregan cuando haya una necesidad real, no antes.

## Documentos y expedientes: muchos a muchos

Un documento puede estar en ningún expediente, en uno o en varios (`servicios/documentos.py`; el documento no sabe de ellos). El «void» NO
es un lugar ni una entidad: es un documento sin ninguna pertenencia, y `listar_documentos(sin_expediente=True)` lo encuentra. Sacar un
documento de su último expediente lo devuelve solo al void; nunca se borra un documento por salir de un expediente.

La pertenencia es una entidad propia —en el modelo de tablas, `expediente_documento`— y vive en este mismo registro como dos eventos,
`agregar` y `quitar`, para que meter un documento sea UNA sola escritura. Nada de esto toca al documento.

## Lo que decide el servidor

El `id` (`expediente-` + 32 hexadecimales), `creadoPor` y `creadoEn`. El nombre se limpia (espacios de más) y tiene tope de 200
caracteres. Dos expedientes pueden llamarse igual: un mismo cliente puede tener dos solicitudes.

## Cómo se guarda

Un archivo de solo agregar por organización, `expedientes-{tenant}.jsonl` (ver `servicios/registro.py`), con tres eventos: `crear`,
`agregar` y `quitar`. El estado vigente se arma doblando el archivo de principio a fin. Los ids son de esa organización: uno ajeno no
existe en su archivo y se contesta como un id que no existe, sin nombrar de quién es.
"""

import re
import uuid
from datetime import datetime, timezone

from servicios import almacen, documentos, registro, tipos_expediente

MAX_NOMBRE = 200
#: Tope de documentos por expediente: un expediente normal tiene unas decenas; esto es para que un cliente descontrolado no lo infle.
MAX_DOCUMENTOS = 1000

_RE_ID = re.compile(r"^expediente-[0-9a-f]{32}$")


class ExpedienteInvalido(ValueError):
    """Lo que llegó no se puede guardar (nombre vacío, expediente lleno…)."""


class NoEncontrado(LookupError):
    """No hay tal expediente EN ESA organización (o no existe: no se distingue)."""


class TipoNoEncontrado(LookupError):
    """No hay tal tipo de expediente EN ESA organización (o no existe: no se distingue)."""


class DocumentoNoEncontrado(LookupError):
    """El documento no existe en esa organización, o no está en ese expediente."""


def _nombre(tenant: str) -> str:
    # Valida el tenant con la MISMA regla que el almacén: es también la defensa contra un `../` en el nombre del archivo de registro.
    return f"expedientes-{almacen._validar_tenant(tenant)}.jsonl"


def _ahora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _limpiar_nombre(nombre: object) -> str:
    if not isinstance(nombre, str):
        raise ExpedienteInvalido("El nombre del expediente no es válido.")
    limpio = " ".join(nombre.split())
    if not limpio or len(limpio) > MAX_NOMBRE or not limpio.isprintable():
        raise ExpedienteInvalido("El nombre del expediente no es válido.")
    return limpio


def _doblar(tenant: str) -> dict[str, dict]:
    """El estado vigente: `{id: expediente}`, cada uno con sus `documentos` (ids, en el orden en que se agregaron). Se llama con el
    candado puesto."""
    expedientes: dict[str, dict] = {}
    for e in registro.eventos(_nombre(tenant)):
        op = e.get("op")
        if op == "crear" and isinstance(e.get("id"), str) and isinstance(e.get("nombre"), str):
            expedientes[e["id"]] = {"id": e["id"], "nombre": e["nombre"], "tipo": e.get("tipo"), "creadoPor": e.get("por"), "creadoEn": e.get("en"), "documentos": []}
        elif op in ("agregar", "quitar"):
            exp, doc = expedientes.get(e.get("expediente")), e.get("documento")
            if exp is None or not isinstance(doc, str):
                continue
            if op == "agregar" and doc not in exp["documentos"]:
                exp["documentos"].append(doc)
            elif op == "quitar" and doc in exp["documentos"]:
                exp["documentos"].remove(doc)
    return expedientes


def _tipos(tenant: str) -> dict[str, dict]:
    return {t["id"]: t for t in tipos_expediente.listar(tenant)}


def _tipo_de(e: dict, tipos: dict[str, dict]) -> dict | None:
    """El tipo del expediente como lo ve quien consulta: su `id` y su `nombre`. `None` si el expediente no lleva tipo."""
    tipo_id = e.get("tipo")
    if tipo_id is None:
        return None
    tipo = tipos.get(tipo_id)
    return {"id": tipo_id, "nombre": tipo["nombre"] if tipo else None}


def _resumen(e: dict, tipos: dict[str, dict]) -> dict:
    return {
        "id": e["id"],
        "nombre": e["nombre"],
        "tipo": _tipo_de(e, tipos),
        "creadoPor": e["creadoPor"],
        "creadoEn": e["creadoEn"],
        "totalDocumentos": len(e["documentos"]),
    }


def _detalle(tenant: str, e: dict, tipos: dict[str, dict]) -> dict:
    por_id = {d["id"]: d for d in documentos.listar(tenant)}
    return {**_resumen(e, tipos), "documentos": [por_id[i] for i in e["documentos"] if i in por_id]}


def crear(tenant: str, nombre: object, por: str, tipo_id: object) -> dict:
    """Crea un expediente vacío DE ESE TIPO y lo devuelve. `ExpedienteInvalido` si el nombre no sirve y `TipoNoEncontrado` si el tipo no es de
    esa organización (o no existe)."""
    limpio = _limpiar_nombre(nombre)
    expediente_id = f"expediente-{uuid.uuid4().hex}"
    en = _ahora()
    with registro.candado:
        tipo = tipos_expediente.obtener(tenant, tipo_id)
        if tipo is None:
            raise TipoNoEncontrado(tipo_id)
        registro.agregar(_nombre(tenant), {"op": "crear", "id": expediente_id, "tenant": tenant, "nombre": limpio, "tipo": tipo["id"], "por": por, "en": en})
    return {"id": expediente_id, "nombre": limpio, "tipo": {"id": tipo["id"], "nombre": tipo["nombre"]}, "creadoPor": por, "creadoEn": en, "totalDocumentos": 0}


def listar(tenant: str, tipo_id: str | None = None) -> list[dict]:
    """Los expedientes de UNA organización, en el orden de su creación, cada uno con su tipo y cuántos documentos tiene. Con `tipo_id`,
    solo los de ese tipo (un tipo que no existe no tiene ninguno)."""
    with registro.candado:
        expedientes = _doblar(tenant)
        tipos = _tipos(tenant)
    return [_resumen(e, tipos) for e in expedientes.values() if tipo_id is None or e["tipo"] == tipo_id]


def contar_por_tipo(tenant: str) -> dict[str, int]:
    """Cuántos expedientes tiene cada tipo de esa organización: `{tipo_id: n}` (los tipos sin expedientes no aparecen)."""
    with registro.candado:
        expedientes = _doblar(tenant)
    cuenta: dict[str, int] = {}
    for e in expedientes.values():
        if e["tipo"]:
            cuenta[e["tipo"]] = cuenta.get(e["tipo"], 0) + 1
    return cuenta


def obtener(tenant: str, expediente_id: str) -> dict | None:
    """El expediente con sus documentos completos (en el orden en que se agregaron), o `None` si no es de esa organización."""
    if not isinstance(expediente_id, str) or not _RE_ID.match(expediente_id):
        return None
    with registro.candado:
        e = _doblar(tenant).get(expediente_id)
        return _detalle(tenant, e, _tipos(tenant)) if e else None


def agregar_documento(tenant: str, expediente_id: str, documento_id: str, por: str) -> bool:
    """Mete un documento a un expediente. Devuelve `True` si lo agregó y `False` si YA estaba (no hace nada y no repite el evento).

    `NoEncontrado` si el expediente no es de esa organización, `DocumentoNoEncontrado` si el documento no lo es, y
    `ExpedienteInvalido` si el expediente ya tiene el máximo."""
    with registro.candado:
        e = _doblar(tenant).get(expediente_id) if isinstance(expediente_id, str) and _RE_ID.match(expediente_id) else None
        if e is None:
            raise NoEncontrado(expediente_id)
        if documentos.obtener(tenant, documento_id) is None:
            raise DocumentoNoEncontrado(documento_id)
        if documento_id in e["documentos"]:
            return False
        if len(e["documentos"]) >= MAX_DOCUMENTOS:
            raise ExpedienteInvalido(f"El expediente ya tiene el máximo de documentos ({MAX_DOCUMENTOS}).")
        registro.agregar(_nombre(tenant), {"op": "agregar", "expediente": expediente_id, "documento": documento_id, "tenant": tenant, "por": por, "en": _ahora()})
        return True


def quitar_documento(tenant: str, expediente_id: str, documento_id: str, por: str) -> None:
    """Saca un documento de un expediente (el documento no se borra: si era su único expediente, vuelve al void).

    `NoEncontrado` si el expediente no es de esa organización; `DocumentoNoEncontrado` si el documento no estaba en él."""
    with registro.candado:
        e = _doblar(tenant).get(expediente_id) if isinstance(expediente_id, str) and _RE_ID.match(expediente_id) else None
        if e is None:
            raise NoEncontrado(expediente_id)
        if documento_id not in e["documentos"]:
            raise DocumentoNoEncontrado(documento_id)
        registro.agregar(_nombre(tenant), {"op": "quitar", "expediente": expediente_id, "documento": documento_id, "tenant": tenant, "por": por, "en": _ahora()})


def listar_documentos(tenant: str, sin_expediente: bool | None = None) -> list[dict]:
    """Los documentos de UNA organización, del más viejo al más nuevo, cada uno con los expedientes en que está (`id` y `nombre`).

    `sin_expediente=True` deja solo el void (los que no están en ninguno); `False`, los que están en al menos uno; `None`, todos."""
    with registro.candado:
        expedientes = _doblar(tenant)
        todos = documentos.listar(tenant)
    en: dict[str, list[dict]] = {}
    for e in expedientes.values():
        for doc in e["documentos"]:
            en.setdefault(doc, []).append({"id": e["id"], "nombre": e["nombre"]})
    resultado = [{**d, "expedientes": en.get(d["id"], [])} for d in todos]
    if sin_expediente is True:
        resultado = [d for d in resultado if not d["expedientes"]]
    elif sin_expediente is False:
        resultado = [d for d in resultado if d["expedientes"]]
    return resultado
