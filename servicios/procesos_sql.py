"""Procesos del motor de reglas guardados en SQL Server, en `nexus.processes` (migración `0005`).

Hace exactamente lo mismo que el JSONL de `servicios/procesos.py` —mismas firmas, mismos errores, misma forma de salida—, y es ese módulo
el que elige (SQL si hay `SQLSERVER_HOST`) y el que valida lo que llega antes de llamar aquí.

La liga con el tipo de expediente virtual (`datos.tipoExpedienteVirtualId`) NO se guarda dentro de `dataJson`: va en su columna, con una
llave foránea real a `virtualCaseTypes`, y se vuelve a poner en `datos` al leer. Si el tipo se borra, la base deja la liga en NULL
(ON DELETE SET NULL) y el proceso se lee sin tipo.
"""

import json

from db import tablas
from servicios.tipos_documentales_comun import momento

_COLUMNAS = "publicId, tenant, revision, dataJson, virtualCaseTypePublicId, createdAt, createdBy, updatedAt, updatedBy"


def _a_proceso(f: dict) -> dict:
    datos = json.loads(f["dataJson"])
    datos["tipoExpedienteVirtualId"] = f["virtualCaseTypePublicId"]
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


def guardar(tenant: str, proceso_id: str, limpio: dict, revision: int, por: str) -> tuple[dict, dict | None]:
    """`limpio` ya viene validado. En una transacción, con el renglón bloqueado: dos guardados a la vez no se pisan (409)."""
    from servicios.procesos import Conflicto, NoEncontrado, ProcesoInvalido

    tipo = limpio.get("tipoExpedienteVirtualId")
    caja = {k: v for k, v in limpio.items() if k != "tipoExpedienteVirtualId"}
    serializado = json.dumps(caja, ensure_ascii=False)
    with tablas.transaccion() as cur:
        if tipo is not None:
            # Con candado compartido hasta el final: el tipo no puede borrarse entre la comprobación y la escritura.
            cur.execute("SELECT 1 AS hay FROM nexus.virtualCaseTypes WITH (HOLDLOCK) WHERE publicId = ? AND tenant = ?", tipo, tenant)
            if tablas.una(cur) is None:
                raise ProcesoInvalido("Ese tipo de expediente virtual no existe en esta organización.")
        cur.execute(f"SELECT {_COLUMNAS} FROM nexus.processes WITH (UPDLOCK, HOLDLOCK) WHERE publicId = ?", proceso_id)
        fila = tablas.una(cur)
        actual = _a_proceso(fila) if fila else None
        if revision == 0:
            if actual is not None:
                raise Conflicto(actual["revision"] if actual["tenant"] == tenant else None)
            cur.execute(
                "INSERT INTO nexus.processes (publicId, tenant, revision, dataJson, virtualCaseTypePublicId, createdBy) VALUES (?, ?, 1, ?, ?, ?)",
                proceso_id, tenant, serializado, tipo, por,
            )
        else:
            if actual is None or actual["tenant"] != tenant:
                raise NoEncontrado(proceso_id)
            if actual["revision"] != revision:
                raise Conflicto(actual["revision"])
            cur.execute(
                "UPDATE nexus.processes SET dataJson = ?, virtualCaseTypePublicId = ?, revision = ?, updatedAt = sysutcdatetime(), updatedBy = ? "
                "WHERE publicId = ? AND revision = ?",
                serializado, tipo, revision + 1, por, proceso_id, revision,
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


def de_tipo(tenant: str, tipo_id: str) -> list[dict]:
    """Los procesos ligados a un tipo de expediente virtual, en el orden de su creación: `{id, nombre, activo}`."""
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT publicId, name, JSON_VALUE(dataJson, '$.activo') AS activo FROM nexus.processes "
            "WHERE tenant = ? AND virtualCaseTypePublicId = ? ORDER BY processId",
            tenant, tipo_id,
        )
        return [{"id": f["publicId"], "nombre": f["name"], "activo": f["activo"] != "false"} for f in tablas.filas(cur)]
