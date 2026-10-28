"""Tipos documentales guardados en SQL Server, en `nexus.documentTypes` (migración `0002`).

Es el PILOTO de la decisión del 2026-10-21 («Nexus sin stored procedures»): la primera entidad del producto que deja el almacén
y pasa a una tabla cuyo esquema se versiona en `migraciones/` y se aplica dentro del deploy. Se escribe SQL directo, no SPs,
porque el esquema y este código viajan en el mismo commit; ver `db/tablas.py`.

Hace exactamente lo mismo que `tipos_documentales_archivo.py` —mismas firmas, mismos errores, misma forma de salida—, así que
`servicios/tipos_documentales.py` puede elegir uno u otro sin que el router se entere.

## Qué gana al estar en la base

- La regla de que un id no se repite en NINGUNA organización ya no depende de Python: la cuida `UQ_documentTypes_publicId`.
- Nombre, estado y procesador se calculan del JSON y se guardan solos (columnas PERSISTED): no pueden desfasarse.
- Guardar, borrar y reordenar pasan por una TRANSACCIÓN con el renglón bloqueado (`UPDLOCK`), así que dos personas guardando a
  la vez ya no dependen de un candado de proceso (el del almacén dejaba de alcanzar con más de un worker de uvicorn).

## Lo que NO cambió

El tipo sigue siendo una caja: `dataJson` guarda el objeto que maneja el front (campos, pasos, historial, recortes) tal cual.
Desarmarlo en tablas es otra conversación; esto es el piloto del camino, no el rediseño del dominio.
"""

import json

from db import tablas
from servicios.tipos_documentales_comun import (
    Conflicto,
    NoEncontrado,
    activos,
    dueños_de_procesadores,
    momento,
    publico,
    validar,
    validar_revision,
)

_COLUMNAS = "publicId, tenant, revision, dataJson, createdAt, createdBy, updatedAt, updatedBy"

# Los que tienen orden primero, en su orden; los que no, al final por fecha de creación. Es lo mismo que hacía el almacén
# doblando sus eventos. `documentTypeId` desempata para que dos tipos creados en el mismo instante salgan siempre igual.
_ORDEN = "ORDER BY CASE WHEN orderIndex IS NULL THEN 1 ELSE 0 END, orderIndex, createdAt, documentTypeId"


def _a_tipo(f: dict) -> dict:
    """Una fila de la tabla con la forma que esperan `publico()` y los ayudantes comunes."""
    return {
        "id": f["publicId"],
        "tenant": f["tenant"],
        "revision": int(f["revision"]),
        "datos": json.loads(f["dataJson"]),
        "creadoPor": f["createdBy"],
        "creadoEn": momento(f["createdAt"]),
        # Un tipo recién creado todavía no se ha editado: su «modificado» es el de su creación, como en el almacén.
        "modificadoPor": f["updatedBy"] if f["updatedAt"] else f["createdBy"],
        "modificadoEn": momento(f["updatedAt"] or f["createdAt"]),
    }


def listar(tenant: str) -> list[dict]:
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS} FROM nexus.documentTypes WHERE tenant = ? {_ORDEN}", tenant)
        return [publico(_a_tipo(f)) for f in tablas.filas(cur)]


def obtener(tenant: str, tipo_id: str) -> dict | None:
    with tablas.lectura() as cur:
        cur.execute(f"SELECT {_COLUMNAS} FROM nexus.documentTypes WHERE publicId = ? AND tenant = ?", tipo_id, tenant)
        f = tablas.una(cur)
        return publico(_a_tipo(f)) if f else None


def guardar(tenant: str, tipo_id: str, datos: object, revision: int, por: str) -> tuple[dict, dict | None]:
    """Crea (`revision == 0`) o actualiza un tipo. Devuelve `(como quedó, el ANTERIOR o None)`.

    Todo dentro de una transacción, y la lectura con `UPDLOCK, HOLDLOCK`: si dos personas guardan el mismo tipo a la vez, la
    segunda espera y se encuentra la revisión ya cambiada, que es justo lo que dispara el 409. El `HOLDLOCK` cubre el caso de
    crear: bloquea el hueco donde iría ese id, así dos creaciones del mismo id no entran las dos.
    """
    limpio = validar(tipo_id, datos)
    validar_revision(revision)
    serializado = json.dumps(limpio, ensure_ascii=False)
    with tablas.transaccion() as cur:
        cur.execute(f"SELECT {_COLUMNAS} FROM nexus.documentTypes WITH (UPDLOCK, HOLDLOCK) WHERE publicId = ?", tipo_id)
        fila = tablas.una(cur)
        actual = _a_tipo(fila) if fila else None
        if revision == 0:
            if actual is not None:
                # Repetido en esta organización: se dice cuál es la vigente. En OTRA: no se nombra de quién es.
                raise Conflicto(actual["revision"] if actual["tenant"] == tenant else None)
            cur.execute(
                "INSERT INTO nexus.documentTypes (publicId, tenant, revision, dataJson, createdBy) VALUES (?, ?, 1, ?, ?)",
                tipo_id,
                tenant,
                serializado,
                por,
            )
        else:
            if actual is None or actual["tenant"] != tenant:
                raise NoEncontrado(tipo_id)
            if actual["revision"] != revision:
                raise Conflicto(actual["revision"])
            cur.execute(
                "UPDATE nexus.documentTypes SET dataJson = ?, revision = ?, updatedAt = sysutcdatetime(), updatedBy = ? "
                "WHERE publicId = ? AND revision = ?",
                serializado,
                revision + 1,
                por,
                tipo_id,
                revision,
            )
        cur.execute(f"SELECT {_COLUMNAS} FROM nexus.documentTypes WHERE publicId = ?", tipo_id)
        nuevo = _a_tipo(tablas.una(cur))
    return publico(nuevo), publico(actual) if actual else None


def eliminar(tenant: str, tipo_id: str, por: str) -> dict:
    """Quita un tipo de la organización. Devuelve el que se quitó. `NoEncontrado` si no es de ella."""
    with tablas.transaccion() as cur:
        cur.execute(
            f"SELECT {_COLUMNAS} FROM nexus.documentTypes WITH (UPDLOCK, HOLDLOCK) WHERE publicId = ? AND tenant = ?",
            tipo_id,
            tenant,
        )
        fila = tablas.una(cur)
        if fila is None:
            raise NoEncontrado(tipo_id)
        cur.execute("DELETE FROM nexus.documentTypes WHERE publicId = ? AND tenant = ?", tipo_id, tenant)
        return publico(_a_tipo(fila))


def ordenar(tenant: str, ids: list[str], por: str) -> None:
    """Fija el orden de la biblioteca de una organización. Los ids que no son SUYOS no hacen nada: el `WHERE` los descarta.

    Reordenar no cuenta como editar el tipo, así que NO toca `updatedAt` (en el almacén tampoco: el evento `orden` no cambiaba
    el «modificado» de ningún tipo).
    """
    # Sin repetidos, conservando el orden en que llegaron.
    unicos = list(dict.fromkeys(i for i in ids if isinstance(i, str)))
    with tablas.transaccion() as cur:
        cur.execute("UPDATE nexus.documentTypes SET orderIndex = NULL WHERE tenant = ? AND orderIndex IS NOT NULL", tenant)
        if unicos:
            cur.executemany(
                "UPDATE nexus.documentTypes SET orderIndex = ? WHERE publicId = ? AND tenant = ?",
                [(i, tipo_id, tenant) for i, tipo_id in enumerate(unicos)],
            )


def tipos_activos(tenant: str) -> list[dict]:
    """Los tipos ACTIVOS de UNA organización, con lo que su clasificador necesita. En orden de creación."""
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT publicId, tenant, dataJson FROM nexus.documentTypes WHERE tenant = ? AND status = 'activo' "
            "ORDER BY createdAt, documentTypeId",
            tenant,
        )
        tipos = [{"id": f["publicId"], "tenant": f["tenant"], "datos": json.loads(f["dataJson"])} for f in tablas.filas(cur)]
    return activos(tipos, tenant)


def procesadores_con_dueno() -> dict[str, dict]:
    """`{procesadorId: {tenant, tipo, version}}` de TODAS las organizaciones: lo que no está aquí no tiene dueño."""
    with tablas.lectura() as cur:
        cur.execute("SELECT publicId, tenant, dataJson FROM nexus.documentTypes ORDER BY createdAt, documentTypeId")
        tipos = [{"id": f["publicId"], "tenant": f["tenant"], "datos": json.loads(f["dataJson"])} for f in tablas.filas(cur)]
    return dueños_de_procesadores(tipos)
