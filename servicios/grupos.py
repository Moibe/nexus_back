"""GRUPOS: el nivel de arriba de los expedientes (migración `0010`).

## Qué es (decisión de producto, Moibe, 2026-11-03)

Varios expedientes viven juntos en un grupo. Cada organización lo usa como quiere —áreas, departamentos, trimestres del año…—, así que el nombre
genérico es «Grupo». Es UN SOLO nivel: un grupo tiene expedientes y nada más. Los expedientes no se anidan dentro de expedientes y los grupos
no se anidan dentro de grupos.

- El grupo se crea ANTES (módulo de configuración) y al crear un expediente se elige a cuál pertenece (`servicios/expedientes.py`: sin grupo no se
  crea). Los expedientes que ya existían no tienen grupo y se les asigna después (`expedientes.asignar_grupo`); uno que ya tiene grupo se puede
  pasar a otro, pero no quedarse sin él.
- Solo tiene un `nombre`, único por organización sin distinguir mayúsculas ni espacios de más (la base es CI: lo cuida `UQ_caseGroups_tenant_name`).
  Se puede RENOMBRAR (el id no cambia, así que los expedientes ven el nombre nuevo solos) y BORRAR, pero solo si no tiene expedientes: un
  expediente con grupo no puede quedarse sin él, así que hay que pasarlos a otro antes (`GrupoConExpedientes`, con cuántos tiene).
- Que un expediente solo pueda ir en un grupo de SU organización lo cuida la base: la llave foránea `FK_cases_caseGroup` es compuesta
  (organización + grupo).

## Solo en SQL

Por la decisión del 2026-10-28 («todo lo nuevo directo a SQL»), esta entidad NO tiene gemelo en JSON: donde no hay base (la laptop, las pruebas
offline sin `--sql`) cada operación levanta `SinBase` y el router contesta 503 diciéndolo. Allí los expedientes se crean sin grupo (no podría
haber uno que elegir) y se ven con `grupo: null`.

## Errores

`GrupoInvalido` (nombre que no sirve), `NoEncontrado` (no hay tal grupo en esa organización, o no existe: no se distingue), `NombreRepetido` (con el
id del que ya se llama así) y `GrupoConExpedientes` (con cuántos tiene).
"""

import re
import uuid

from config import SQLSERVER_HOST
from servicios.tipos_documentales_comun import momento

MAX_NOMBRE = 200

RE_ID = re.compile(r"^grupo-[0-9a-f]{32}$")


class SinBase(RuntimeError):
    """Los grupos solo viven en SQL Server, y esta instancia no tiene base."""


class GrupoInvalido(ValueError):
    """Lo que llegó no se puede guardar (nombre vacío o demasiado largo)."""


class NoEncontrado(LookupError):
    """No hay tal grupo EN ESA organización (o no existe: no se distingue)."""


class NombreRepetido(Exception):
    """Ya hay un grupo con ese nombre en la organización. `existente` es su id."""

    def __init__(self, existente: str):
        super().__init__("nombre repetido")
        self.existente = existente


class GrupoConExpedientes(Exception):
    """El grupo tiene expedientes y por eso no se puede borrar. `total` es cuántos tiene."""

    def __init__(self, total: int):
        super().__init__("grupo con expedientes")
        self.total = total


def _tablas():
    if not SQLSERVER_HOST:
        raise SinBase("Los grupos necesitan la base de datos, y esta instancia no tiene.")
    from db import tablas

    return tablas


def limpiar_nombre(nombre: object) -> str:
    if not isinstance(nombre, str):
        raise GrupoInvalido("El nombre del grupo no es válido.")
    limpio = " ".join(nombre.split())
    if not limpio or len(limpio) > MAX_NOMBRE or not limpio.isprintable():
        raise GrupoInvalido("El nombre del grupo no es válido.")
    return limpio


def _tenant(tenant: str) -> str:
    # La MISMA regla que el almacén para el nombre de una organización: un tenant raro se rechaza antes de llegar a la base.
    from servicios import almacen

    return almacen._validar_tenant(tenant)


def _id_valido(grupo_id: object) -> str:
    if not isinstance(grupo_id, str) or not RE_ID.match(grupo_id):
        raise NoEncontrado(grupo_id)
    return grupo_id


# Cada grupo con cuántos expedientes tiene: se calcula en la consulta, no se guarda.
_RESUMEN = (
    "SELECT g.caseGroupId, g.publicId, g.name, g.createdBy, g.createdAt, g.updatedAt, count(c.caseId) AS total "
    "FROM nexus.caseGroups g LEFT JOIN nexus.cases c ON c.caseGroupId = g.caseGroupId "
    "WHERE g.tenant = ? {filtro} GROUP BY g.caseGroupId, g.publicId, g.name, g.createdBy, g.createdAt, g.updatedAt ORDER BY g.caseGroupId"
)


def _a_grupo(f: dict) -> dict:
    return {
        "id": f["publicId"],
        "nombre": f["name"],
        "totalExpedientes": int(f["total"]),
        "creadoPor": f["createdBy"],
        "creadoEn": momento(f["createdAt"]),
        "actualizadoEn": momento(f["updatedAt"]) if f.get("updatedAt") else None,
    }


def bloqueado(cur, tenant: str, grupo_id: str) -> dict | None:
    """La fila del grupo, bloqueada hasta el final de la transacción (nadie lo renombra ni lo borra, ni le cuelga un expediente en medio).
    También la usan `expedientes_sql.expedientes_crear` y `expedientes_sql.expedientes_asignar_grupo`."""
    from db import tablas

    cur.execute("SELECT caseGroupId, publicId, name FROM nexus.caseGroups WITH (UPDLOCK, HOLDLOCK) WHERE publicId = ? AND tenant = ?", grupo_id, tenant)
    return tablas.una(cur)


def listar(tenant: str) -> list[dict]:
    """Los grupos de UNA organización, en el orden de su creación, cada uno con cuántos expedientes tiene."""
    tablas = _tablas()
    with tablas.lectura() as cur:
        cur.execute(_RESUMEN.format(filtro=""), _tenant(tenant))
        return [_a_grupo(f) for f in tablas.filas(cur)]


def obtener(tenant: str, grupo_id: str) -> dict | None:
    """El grupo, o `None` si no es de esa organización."""
    tablas = _tablas()
    if not isinstance(grupo_id, str) or not RE_ID.match(grupo_id):
        return None
    with tablas.lectura() as cur:
        cur.execute(_RESUMEN.format(filtro="AND g.publicId = ?"), _tenant(tenant), grupo_id)
        f = tablas.una(cur)
    return _a_grupo(f) if f else None


def crear(tenant: str, nombre: object, por: str) -> dict:
    """Crea un grupo vacío y lo devuelve. `GrupoInvalido` si el nombre no sirve; `NombreRepetido` si ya hay uno con ese nombre."""
    tablas = _tablas()
    limpio = limpiar_nombre(nombre)
    tenant = _tenant(tenant)
    grupo_id = f"grupo-{uuid.uuid4().hex}"
    with tablas.transaccion() as cur:
        cur.execute("SELECT publicId FROM nexus.caseGroups WITH (UPDLOCK, HOLDLOCK) WHERE tenant = ? AND name = ?", tenant, limpio)
        ocupado = tablas.una(cur)
        if ocupado:
            raise NombreRepetido(ocupado["publicId"])
        cur.execute(
            "INSERT INTO nexus.caseGroups (publicId, tenant, name, createdBy) OUTPUT INSERTED.createdAt VALUES (?, ?, ?, ?)",
            grupo_id, tenant, limpio, por,
        )
        creado = tablas.una(cur)["createdAt"]
    return {"id": grupo_id, "nombre": limpio, "totalExpedientes": 0, "creadoPor": por, "creadoEn": momento(creado), "actualizadoEn": None}


def renombrar(tenant: str, grupo_id: str, nombre: object, por: str) -> tuple[dict, str]:
    """Le pone otro nombre. Devuelve `(grupo, nombre_anterior)`; si el nombre, ya limpio, es el que tenía, no escribe nada. Cambiar solo las
    mayúsculas del propio nombre sí se puede (no choca consigo mismo)."""
    tablas = _tablas()
    limpio = limpiar_nombre(nombre)
    tenant = _tenant(tenant)
    _id_valido(grupo_id)
    with tablas.transaccion() as cur:
        f = bloqueado(cur, tenant, grupo_id)
        if f is None:
            raise NoEncontrado(grupo_id)
        cur.execute("SELECT publicId FROM nexus.caseGroups WITH (UPDLOCK, HOLDLOCK) WHERE tenant = ? AND name = ? AND publicId <> ?", tenant, limpio, grupo_id)
        ocupado = tablas.una(cur)
        if ocupado:
            raise NombreRepetido(ocupado["publicId"])
        anterior = f["name"]
        if limpio != anterior:
            cur.execute("UPDATE nexus.caseGroups SET name = ?, updatedAt = sysutcdatetime(), updatedBy = ? WHERE caseGroupId = ?", limpio, por, f["caseGroupId"])
    return obtener(tenant, grupo_id), anterior  # type: ignore[return-value]


def borrar(tenant: str, grupo_id: str) -> dict:
    """Borra el grupo y libera su nombre. `GrupoConExpedientes` si tiene alguno. La cuenta y el borrado van con el grupo bloqueado: un expediente
    nuevo no se cuela entre una cosa y la otra (`expedientes_sql.expedientes_crear` bloquea la misma fila)."""
    tablas = _tablas()
    tenant = _tenant(tenant)
    _id_valido(grupo_id)
    with tablas.transaccion() as cur:
        f = bloqueado(cur, tenant, grupo_id)
        if f is None:
            raise NoEncontrado(grupo_id)
        cur.execute("SELECT count(*) AS n FROM nexus.cases WHERE caseGroupId = ?", f["caseGroupId"])
        total = tablas.una(cur)["n"]
        if total:
            raise GrupoConExpedientes(total)
        cur.execute("DELETE FROM nexus.caseGroups WHERE caseGroupId = ?", f["caseGroupId"])
    return {"id": f["publicId"], "nombre": f["name"]}
