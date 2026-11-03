"""Registro de los EXPEDIENTES de cada organización.

## Qué es un expediente

Desde el 2026-10-29 (decisión de producto de Moibe) un expediente es solo un RECEPTÁCULO de documentos: la carpeta de un cliente o de un caso,
donde cae todo lo suyo. NO tiene tipo (antes era instancia de un tipo de expediente). Lo que tiene esquema son sus EXPEDIENTES VIRTUALES
(`servicios/expedientes_virtuales.py`): subconjuntos de sus documentos, cada uno instancia de un tipo de expediente virtual
(`servicios/tipos_expediente.py`), que son los grupitos que usará el motor de reglas. Un expediente solo tiene un `nombre`; estado, folio o
una referencia del cliente se agregan cuando haya una necesidad real, no antes.

## Documentos y expedientes: muchos a muchos

Un documento puede estar en ningún expediente, en uno o en varios (`servicios/documentos.py`; el documento no sabe de ellos). El «void» NO
es un lugar ni una entidad: es un documento sin ninguna pertenencia, y `listar_documentos(sin_expediente=True)` lo encuentra. Sacar un
documento de su último expediente lo devuelve solo al void; nunca se borra un documento por salir de un expediente. Sacarlo de un expediente
lo saca también de los expedientes virtuales de ese expediente (lo hace la base).

La pertenencia es una entidad propia —en el modelo de tablas, `expediente_documento`— y vive en este mismo registro como dos eventos,
`agregar` y `quitar`, para que meter un documento sea UNA sola escritura. Nada de esto toca al documento.

## Lo que decide el servidor

El `id` (`expediente-` + 32 hexadecimales), `creadoPor` y `creadoEn`. El nombre se limpia (espacios de más) y tiene tope de 200
caracteres. Dos expedientes pueden llamarse igual: un mismo cliente puede tener dos solicitudes.

## Lo que se calcula al consultar

`totalDocumentos` (cuántos tiene) y `tamanoBytes` (cuánto pesan, la suma de sus documentos) no se guardan: salen de doblar el registro y
de leer los documentos cada vez. Un documento que viaja de un expediente a otro, o sale, no obliga a reescribir nada.

## Cómo se guarda

Desde el 2026-10-29, donde hay base, en `nexus.cases` y la pertenencia en `nexus.caseDocuments` (migraciones `0003` y `0004`, ver
`servicios/expedientes_sql.py`). Este módulo valida igual en los dos casos y solo cambia dónde se escribe; se elige por
`SQLSERVER_HOST` en cada llamada. Lo de abajo es el almacén, lo que corre donde no hay base (la laptop, las pruebas offline):

Un archivo de solo agregar por organización, `expedientes-{tenant}.jsonl` (ver `servicios/registro.py`), con tres eventos: `crear`,
`agregar` y `quitar`. El estado vigente se arma doblando el archivo de principio a fin. Los ids son de esa organización: uno ajeno no
existe en su archivo y se contesta como un id que no existe, sin nombrar de quién es. Los `crear` de antes del 2026-10-29 traen un `tipo`
que ya no se lee. En el almacén no hay expedientes virtuales (solo viven en SQL): el detalle trae su lista vacía.
"""

import re
import uuid
from datetime import datetime, timezone

from config import SQLSERVER_HOST
from servicios import almacen, documentos, registro


def _sql():
    """El módulo de SQL si el `.env` dice que hay base; `None` si no. Se importa tarde: sin base, `pyodbc` ni se carga."""
    if not SQLSERVER_HOST:
        return None
    from servicios import expedientes_sql

    return expedientes_sql


MAX_NOMBRE = 200
#: Tope de documentos por expediente: un expediente normal tiene unas decenas; esto es para que un cliente descontrolado no lo infle.
MAX_DOCUMENTOS = 1000

_RE_ID = re.compile(r"^expediente-[0-9a-f]{32}$")


class ExpedienteInvalido(ValueError):
    """Lo que llegó no se puede guardar (nombre vacío, expediente lleno…)."""


class NoEncontrado(LookupError):
    """No hay tal expediente EN ESA organización (o no existe: no se distingue)."""


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
            expedientes[e["id"]] = {"id": e["id"], "nombre": e["nombre"], "creadoPor": e.get("por"), "creadoEn": e.get("en"), "documentos": []}
        elif op in ("agregar", "quitar"):
            exp, doc = expedientes.get(e.get("expediente")), e.get("documento")
            if exp is None or not isinstance(doc, str):
                continue
            if op == "agregar" and doc not in exp["documentos"]:
                exp["documentos"].append(doc)
            elif op == "quitar" and doc in exp["documentos"]:
                exp["documentos"].remove(doc)
    return expedientes


def _documentos_por_id(tenant: str) -> dict[str, dict]:
    return {d["id"]: d for d in documentos.listar(tenant)}


def _resumen(e: dict, por_id: dict[str, dict]) -> dict:
    return {
        "id": e["id"],
        "nombre": e["nombre"],
        "creadoPor": e["creadoPor"],
        "creadoEn": e["creadoEn"],
        "totalDocumentos": len(e["documentos"]),
        # Se calcula al consultar, no se guarda: un documento cambia de expediente sin que el expediente se reescriba.
        "tamanoBytes": sum(por_id[i].get("tamanoBytes") or 0 for i in e["documentos"] if i in por_id),
    }


def _detalle(e: dict, por_id: dict[str, dict]) -> dict:
    return {**_resumen(e, por_id), "documentos": [por_id[i] for i in e["documentos"] if i in por_id], "expedientesVirtuales": []}


def crear(tenant: str, nombre: object, por: str) -> dict:
    """Crea un expediente vacío y lo devuelve. `ExpedienteInvalido` si el nombre no sirve."""
    limpio = _limpiar_nombre(nombre)
    if sql := _sql():
        return sql.expedientes_crear(almacen._validar_tenant(tenant), limpio, por)
    expediente_id = f"expediente-{uuid.uuid4().hex}"
    en = _ahora()
    with registro.candado:
        registro.agregar(_nombre(tenant), {"op": "crear", "id": expediente_id, "tenant": tenant, "nombre": limpio, "por": por, "en": en})
    return {"id": expediente_id, "nombre": limpio, "creadoPor": por, "creadoEn": en, "totalDocumentos": 0, "tamanoBytes": 0}


def listar(tenant: str) -> list[dict]:
    """Los expedientes de UNA organización, en el orden de su creación, cada uno con cuántos documentos tiene y cuánto pesan en total."""
    if sql := _sql():
        return sql.expedientes_listar(almacen._validar_tenant(tenant))
    with registro.candado:
        expedientes = _doblar(tenant)
        por_id = _documentos_por_id(tenant)
    return [_resumen(e, por_id) for e in expedientes.values()]


def obtener(tenant: str, expediente_id: str) -> dict | None:
    """El expediente con sus documentos completos (en el orden en que se agregaron) y sus expedientes virtuales, o `None` si no es de esa
    organización."""
    if not isinstance(expediente_id, str) or not _RE_ID.match(expediente_id):
        return None
    if sql := _sql():
        return sql.expedientes_obtener(almacen._validar_tenant(tenant), expediente_id)
    with registro.candado:
        e = _doblar(tenant).get(expediente_id)
        return _detalle(e, _documentos_por_id(tenant)) if e else None


def agregar_documento(tenant: str, expediente_id: str, documento_id: str, por: str) -> bool:
    """Mete un documento a un expediente. Devuelve `True` si lo agregó y `False` si YA estaba (no hace nada y no repite el evento).

    `NoEncontrado` si el expediente no es de esa organización, `DocumentoNoEncontrado` si el documento no lo es, y
    `ExpedienteInvalido` si el expediente ya tiene el máximo."""
    if sql := _sql():
        if not isinstance(expediente_id, str) or not _RE_ID.match(expediente_id):
            raise NoEncontrado(expediente_id)
        if not isinstance(documento_id, str):
            raise DocumentoNoEncontrado(documento_id)
        return sql.expedientes_agregar_documento(almacen._validar_tenant(tenant), expediente_id, documento_id, por, MAX_DOCUMENTOS)
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
    """Saca un documento de un expediente (el documento no se borra: si era su único expediente, vuelve al void). En SQL lo saca también de
    los expedientes virtuales de ese expediente.

    `NoEncontrado` si el expediente no es de esa organización; `DocumentoNoEncontrado` si el documento no estaba en él."""
    if sql := _sql():
        if not isinstance(expediente_id, str) or not _RE_ID.match(expediente_id):
            raise NoEncontrado(expediente_id)
        if not isinstance(documento_id, str):
            raise DocumentoNoEncontrado(documento_id)
        return sql.expedientes_quitar_documento(almacen._validar_tenant(tenant), expediente_id, documento_id)
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
    if sql := _sql():
        return sql.expedientes_listar_documentos(almacen._validar_tenant(tenant), sin_expediente)
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
