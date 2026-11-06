"""Procesos del motor de reglas guardados en SQL Server, en `nexus.processes` (migración `0005`).

Hace exactamente lo mismo que el JSONL de `servicios/procesos.py` —mismas firmas, mismos errores, misma forma de salida—, y es ese módulo
el que elige (SQL si hay `SQLSERVER_HOST`) y el que valida lo que llega antes de llamar aquí.

Moibe, 2026-11-05: se quitaron los expedientes virtuales y un proceso ya no se liga a ningún tipo. Este módulo dejó de leer y de escribir la
columna `virtualCaseTypePublicId` (la `0005` la creó; la borra la migración `0012`): el proceso es su `dataJson`, sin nada más que dependa de otra
tabla, y `datos.tipoExpedienteVirtualId` no se guarda ni se devuelve.
"""

import json

from db import tablas
from servicios.tipos_documentales_comun import momento

_COLUMNAS = "publicId, tenant, revision, dataJson, createdAt, createdBy, updatedAt, updatedBy"


def _a_proceso(f: dict) -> dict:
    datos = json.loads(f["dataJson"])
    return {
        "id": f["publicId"],
        "tenant": f["tenant"],
        "revision": int(f["revision"]),
        "datos": datos,
        "creadoPor": f["createdBy"],
        "creadoEn": momento(f["createdAt"]),
        "modificadoPor": f["updatedBy"] if f["updatedAt"] else f["createdBy"],
        "modificadoEn": momento(f["updatedAt"] or f["createdAt"]),
    }


def listar(tenant: str) -> list[dict]:
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS} FROM nexus.processes WHERE tenant = ? ORDER BY processId", tenant)
        return [_a_proceso(f) for f in tablas.filas(cur)]


def obtener(tenant: str, proceso_id: str) -> dict | None:
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS} FROM nexus.processes WHERE publicId = ? AND tenant = ?", proceso_id, tenant)
        f = tablas.una(cur)
    return _a_proceso(f) if f else None


def _exigir_nombre_libre(cur, tenant: str, proceso_id: str, nombre: str, exigir) -> None:
    """El nombre es único en la organización. Se leen los de los DEMÁS procesos con candado hasta el final de la transacción: dos guardados a la
    vez con el mismo nombre no pasan los dos. Se compara en Python, con la misma regla que el JSONL (sin espacios de más ni mayúsculas)."""
    cur.execute("SELECT name FROM nexus.processes WITH (UPDLOCK, HOLDLOCK) WHERE tenant = ? AND publicId <> ?", tenant, proceso_id)
    exigir(nombre, [f["name"] for f in tablas.filas(cur)])


def guardar(tenant: str, proceso_id: str, limpio: dict, revision: int, por: str) -> tuple[dict, dict | None]:
    """`limpio` ya viene validado. En una transacción, con el renglón bloqueado: dos guardados a la vez no se pisan (409)."""
    from servicios.procesos import Conflicto, NoEncontrado, clave_de_nombre, exigir_nombre_libre

    serializado = json.dumps(limpio, ensure_ascii=False)
    with tablas.transaccion() as cur:
        cur.execute(f"SELECT {_COLUMNAS} FROM nexus.processes WITH (UPDLOCK, HOLDLOCK) WHERE publicId = ?", proceso_id)
        fila = tablas.una(cur)
        actual = _a_proceso(fila) if fila else None
        if revision == 0:
            if actual is not None:
                raise Conflicto(actual["revision"] if actual["tenant"] == tenant else None)
            _exigir_nombre_libre(cur, tenant, proceso_id, limpio["nombre"], exigir_nombre_libre)
            cur.execute(
                "INSERT INTO nexus.processes (publicId, tenant, revision, dataJson, createdBy) VALUES (?, ?, 1, ?, ?)",
                proceso_id, tenant, serializado, por,
            )
        else:
            if actual is None or actual["tenant"] != tenant:
                raise NoEncontrado(proceso_id)
            if actual["revision"] != revision:
                raise Conflicto(actual["revision"])
            if clave_de_nombre(actual["datos"]["nombre"]) != clave_de_nombre(limpio["nombre"]):
                _exigir_nombre_libre(cur, tenant, proceso_id, limpio["nombre"], exigir_nombre_libre)
            cur.execute(
                "UPDATE nexus.processes SET dataJson = ?, revision = ?, updatedAt = sysutcdatetime(), updatedBy = ? WHERE publicId = ? AND revision = ?",
                serializado, revision + 1, por, proceso_id, revision,
            )
        cur.execute(f"SELECT {_COLUMNAS} FROM nexus.processes WHERE publicId = ?", proceso_id)
        nuevo = _a_proceso(tablas.una(cur))
    return nuevo, actual


def eliminar(tenant: str, proceso_id: str) -> dict:
    from servicios.procesos import NoEncontrado

    with tablas.transaccion() as cur:
        cur.execute(f"SELECT {_COLUMNAS} FROM nexus.processes WITH (UPDLOCK, HOLDLOCK) WHERE publicId = ? AND tenant = ?", proceso_id, tenant)
        fila = tablas.una(cur)
        if fila is None:
            raise NoEncontrado(proceso_id)
        cur.execute("DELETE FROM nexus.processes WHERE publicId = ? AND tenant = ?", proceso_id, tenant)
    return _a_proceso(fila)
