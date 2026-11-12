"""Qué clasificador de Document AI usa cada organización — registro PROVISIONAL.

Hasta el 2026-10-26 había UN solo clasificador para todo el sistema, armado con los tipos activos de TODAS las organizaciones.
Eso mezclaba las categorías de unas con las de otras (un documento de B podía caer en un tipo que solo existe en A), hacía que lo
que una organización guardaba cambiara el clasificador de las demás, y dejaba los nombres y las descripciones de los tipos de cada
cliente a la vista de todos en el esquema. Ahora cada organización tiene el suyo, con SOLO sus tipos activos.

## Dónde vive cada uno

- **Una organización** (el tenant es el guid de una de `tenants_registro`): su clasificador se llama `nexusdoc-clasificador--{guid}`
  en Google y su id queda en la base (abajo). Se crea la PRIMERA vez que se
  sincroniza (al activar su primer tipo documental), no al darla de alta: una organización sin tipos no gasta un procesador.
- **El espacio de plataforma** (cualquier tenant que no es una organización: `demo`, lo que opera el administrador de plataforma):
  sigue usando el clasificador de siempre, el del `.env` (`DOCAI_CLASIFICADOR_ID`, `nexusdoc-clasificador`). Así no hubo que migrar
  nada y ese procesador no se recreó.

El id se guarda aquí y no se busca por nombre en cada llamada porque Document AI no filtra `processors.list`: encontrar uno por su
nombre es paginar TODOS los procesadores del proyecto. Por nombre solo se busca la primera vez (o si el procesador se borró a mano),
para adoptar el que un intento anterior dejó creado a medias en vez de duplicarlo.

**Desde el 2026-10-29 (migración `0006`) el id vive en `nexus.tenantClassifiers`** (una fila por organización, con llave foránea
a `nexus.tenants`), importado una vez desde el viejo `clasificadores.jsonl` (`servicios/importacion_json.py`). Desde el 2026-11-11 no hay
otro almacenamiento: el JSONL ya solo se LEE para esa importación (`servicios/json_heredado.py`).
"""

from config import DOCAI_CLASIFICADOR_ID
from servicios import almacen, registros_sql
from servicios.aislamiento import es_organizacion

# El del espacio de plataforma conserva el nombre de siempre; los de las organizaciones llevan su guid detrás de `--`.
NOMBRE_PLATAFORMA = "nexusdoc-clasificador"
PREFIJO_ORGANIZACION = f"{NOMBRE_PLATAFORMA}--"


def nombre_visible(tenant: str) -> str:
    """El `displayName` con que se crea y se busca el clasificador de este tenant en Google."""
    tenant = almacen._validar_tenant(tenant)
    return f"{PREFIJO_ORGANIZACION}{tenant}" if es_organizacion(tenant) else NOMBRE_PLATAFORMA


def id_de(tenant: str) -> str | None:
    """El id del clasificador de este tenant, o `None` si todavía no tiene (aún no se sincroniza, o el espacio de plataforma
    sin `DOCAI_CLASIFICADOR_ID` en el `.env`)."""
    tenant = almacen._validar_tenant(tenant)
    if not es_organizacion(tenant):
        return DOCAI_CLASIFICADOR_ID or None
    return registros_sql.clasificador_de(tenant)


def asignar(tenant: str, procesador_id: str) -> None:
    """Anota cuál es el clasificador de una ORGANIZACIÓN. No hace nada para el espacio de plataforma (su id está en el `.env`) ni
    si ya era ese mismo."""
    tenant = almacen._validar_tenant(tenant)
    if not es_organizacion(tenant):
        return
    registros_sql.clasificador_asignar(tenant, procesador_id)
