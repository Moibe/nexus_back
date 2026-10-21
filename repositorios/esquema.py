"""Repositorio del dominio esquema: el inventario de tablas de la base.

A diferencia del resto de los repositorios, esto NO pasa por stored
procedures, y es a propósito: lee las vistas de catálogo (`sys.*`), que son
METADATOS, no datos de negocio. Es exactamente la consulta que hoy se hace a
mano cada vez que se audita el esquema (ver `docs/solicitudes-dba.md`,
secciones 0c y 4.0, las dos marcadas "leído, no supuesto"), solo que servida
por la API para que no haya que volver a pedírsela a nadie.

DOS DECISIONES QUE IMPORTAN:

1. Los conteos salen de `sys.partitions`, NO de un `COUNT(*)` por tabla. Así
   esta consulta no necesita permiso de SELECT sobre cada tabla y, sobre todo,
   **no puede devolver datos de un cliente aunque alguien se equivoque**: solo
   ve nombres y números de fila. El costo es que el conteo es el del motor y
   puede ir unas filas atrás del valor exacto; para un inventario de
   referencia eso sobra, pero no lo uses para cuadrar nada.

2. Se filtra `is_ms_shipped = 0` para no listar las tablas internas de SQL
   Server, y se excluye el esquema `sys`.
"""

from db.sqlserver import obtener_conexion

# `index_id IN (0, 1)` = heap (0) o índice agrupado (1). Sin ese filtro, una
# tabla con índices no agrupados suma sus filas una vez por índice y el conteo
# sale multiplicado.
_SQL_TABLAS = """
SELECT
    s.name AS esquema,
    t.name AS tabla,
    SUM(CASE WHEN p.index_id IN (0, 1) THEN p.rows ELSE 0 END) AS filas
FROM sys.tables AS t
JOIN sys.schemas AS s ON s.schema_id = t.schema_id
LEFT JOIN sys.partitions AS p ON p.object_id = t.object_id
WHERE t.is_ms_shipped = 0 AND s.name <> 'sys'
GROUP BY s.name, t.name
ORDER BY s.name, t.name
"""


def listar_tablas() -> list[dict]:
    """Todas las tablas de la base con su esquema y su número de filas.

    Devuelve `[{"esquema": "documents", "tabla": "files", "filas": 0}, ...]`.
    """
    with obtener_conexion() as conexion:
        cursor = conexion.cursor()
        cursor.execute(_SQL_TABLAS)
        if cursor.description is None:
            return []
        columnas = [col[0] for col in cursor.description]
        return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]
