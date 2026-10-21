"""Qué procesadores de Document AI NO tienen dueño en ningún tipo documental.

Hasta el 2026-10-15 cada navegador inventaba sus ids de tipo, así que el mismo «INE» dado de alta en varias computadoras creó
varios Custom Extractor en Google, y los tipos viven ahora en el registro del servidor (`servicios/tipos_documentales.py`)
SIN ninguno de esos: quedaron procesadores que nadie apunta. Este módulo es la parte PURA de decidir cuáles; la que habla con
Google y borra es `limpiar_procesadores_huerfanos.py`.

## Qué cuenta como huérfano

Un procesador `nexusdoc--…` cuyo id no está en ningún tipo del registro: ni como procesador vigente, ni en su historial de
versiones, ni como la versión que se está editando. Nunca lo son:

- el clasificador (`nexusdoc-clasificador`) y el procesador cuyo id está en `protegidos` (el clasificador del `.env`, el
  extractor de INE legado): sin ellos el sistema deja de funcionar;
- cualquier procesador cuyo nombre NO empiece con `nexusdoc--`: no es de la aplicación.
"""

PREFIJO_EXTRACTOR = "nexusdoc--"


def clasificar(procesadores: list[dict], con_dueno: dict[str, dict], protegidos: set[str]) -> dict[str, list[dict]]:
    """Reparte los procesadores en `con_dueno`, `huerfanos` y `protegidos`. Un procesador está en UNA sola lista."""
    reparto: dict[str, list[dict]] = {"con_dueno": [], "huerfanos": [], "protegidos": []}
    for p in procesadores:
        if p["id"] in protegidos or not p["displayName"].startswith(PREFIJO_EXTRACTOR):
            reparto["protegidos"].append(p)
        elif p["id"] in con_dueno:
            reparto["con_dueno"].append({**p, "dueno": con_dueno[p["id"]]})
        else:
            reparto["huerfanos"].append(p)
    return reparto
