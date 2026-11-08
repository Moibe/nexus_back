"""Qué clasificador de Document AI usa cada organización — registro PROVISIONAL.

Hasta el 2026-10-26 había UN solo clasificador para todo el sistema, armado con los tipos activos de TODAS las organizaciones.
Eso mezclaba las categorías de unas con las de otras (un documento de B podía caer en un tipo que solo existe en A), hacía que lo
que una organización guardaba cambiara el clasificador de las demás, y dejaba los nombres y las descripciones de los tipos de cada
cliente a la vista de todos en el esquema. Ahora cada organización tiene el suyo, con SOLO sus tipos activos.

## Dónde vive cada uno

- **Una organización** (el tenant es el guid de una de `tenants_registro`): su clasificador se llama `nexusdoc-clasificador--{guid}`
  en Google y su id queda en `clasificadores.jsonl`, un registro de solo agregar como los demás. Se crea la PRIMERA vez que se
  sincroniza (al activar su primer tipo documental), no al darla de alta: una organización sin tipos no gasta un procesador.
- **El espacio de plataforma** (cualquier tenant que no es una organización: `demo`, lo que opera el administrador de plataforma):
  sigue usando el clasificador de siempre, el del `.env` (`DOCAI_CLASIFICADOR_ID`, `nexusdoc-clasificador`). Así no hubo que migrar
  nada y ese procesador no se recreó.

El id se guarda aquí y no se busca por nombre en cada llamada porque Document AI no filtra `processors.list`: encontrar uno por su
nombre es paginar TODOS los procesadores del proyecto. Por nombre solo se busca la primera vez (o si el procesador se borró a mano),
para adoptar el que un intento anterior dejó creado a medias en vez de duplicarlo.

**Desde el 2026-10-29 (migración `0006`), donde hay base, el id vive en `nexus.tenantClassifiers`** (una fila por organización, con llave
foránea a `nexus.tenants`), importado una vez desde `clasificadores.jsonl` (`servicios/importacion_json.py`). Este módulo elige por
`SQLSERVER_HOST` en cada llamada; el JSONL es lo que corre donde no hay base.
"""

from datetime import datetime, timezone

from config import DOCAI_CLASIFICADOR_ID, SQLSERVER_HOST
from servicios import almacen, registro


def _sql():
    """El módulo de SQL si el `.env` dice que hay base; `None` si no. Se importa tarde: sin base, `pyodbc` ni se carga."""
    if not SQLSERVER_HOST:
        return None
    from servicios import registros_sql

    return registros_sql
from servicios.aislamiento import es_organizacion

ARCHIVO = "clasificadores.jsonl"

# El del espacio de plataforma conserva el nombre de siempre; los de las organizaciones llevan su guid detrás de `--`.
NOMBRE_PLATAFORMA = "nexusdoc-clasificador"
PREFIJO_ORGANIZACION = f"{NOMBRE_PLATAFORMA}--"


def _ahora() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def nombre_visible(tenant: str) -> str:
    """El `displayName` con que se crea y se busca el clasificador de este tenant en Google."""
    tenant = almacen._validar_tenant(tenant)
    return f"{PREFIJO_ORGANIZACION}{tenant}" if es_organizacion(tenant) else NOMBRE_PLATAFORMA


def _asignados() -> dict[str, str]:
    """`{tenant: procesadorId}` del registro. Si una organización tiene varios eventos gana el último (se reasigna cuando el
    procesador se borró a mano y hubo que crear otro)."""
    asignados: dict[str, str] = {}
    for e in registro.eventos(ARCHIVO):
        if e.get("evento") == "asignado" and isinstance(e.get("tenant"), str) and isinstance(e.get("procesadorId"), str):
            asignados[e["tenant"]] = e["procesadorId"]
    return asignados


def asignados_archivo() -> dict[str, str]:
    """Lo que dice el JSONL, aunque haya base: es lo que importa `servicios/importacion_json.py`."""
    return _asignados()


def id_de(tenant: str) -> str | None:
    """El id del clasificador de este tenant, o `None` si todavía no tiene (aún no se sincroniza, o el espacio de plataforma
    sin `DOCAI_CLASIFICADOR_ID` en el `.env`)."""
    tenant = almacen._validar_tenant(tenant)
    if not es_organizacion(tenant):
        return DOCAI_CLASIFICADOR_ID or None
    if sql := _sql():
        return sql.clasificador_de(tenant)
    return _asignados().get(tenant)


def asignar(tenant: str, procesador_id: str) -> None:
    """Anota cuál es el clasificador de una ORGANIZACIÓN. No hace nada para el espacio de plataforma (su id está en el `.env`) ni
    si ya era ese mismo."""
    tenant = almacen._validar_tenant(tenant)
    if not es_organizacion(tenant):
        return
    if sql := _sql():
        return sql.clasificador_asignar(tenant, procesador_id)
    with registro.candado:
        if _asignados().get(tenant) == procesador_id:
            return
        registro.agregar(ARCHIVO, {"evento": "asignado", "tenant": tenant, "procesadorId": procesador_id, "en": _ahora()})
