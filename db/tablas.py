"""Acceso DIRECTO a las tablas NUESTRAS, las del esquema `nexus`.

Convive con `db/sqlserver.py` y no lo reemplaza; se reparten el trabajo por decisión del 2026-10-21 («Nexus sin stored procedures»):

- `db/sqlserver.py` habla con el diccionario VIEJO (`documents`, `security`, `reference`) y solo invoca los stored procedures que
  el DBA expuso. Esos SPs quedaron CONGELADOS como especificación: ahí no se escribe SQL de tablas.
- este módulo habla con las tablas que creamos nosotros en el esquema `nexus`, cuyo esquema se versiona en `migraciones/` y se
  aplica dentro del deploy. Aquí SÍ se escribe SQL, porque el esquema y el código viajan juntos en el mismo commit.

Lo que aporta sobre `obtener_conexion()` pelada es la TRANSACCIÓN: un «leer y luego escribir» (guardar comprobando la revisión,
reordenar la biblioteca) tiene que ser indivisible, o dos personas guardando a la vez se pisan. `transaccion()` abre una, la
confirma si todo salió y la deshace ante cualquier error.

pyodbc es síncrono: los endpoints que llegan aquí se declaran con `def`, no `async def`, para que FastAPI los corra en su
threadpool y no bloquee el event loop.
"""

import logging
from contextlib import contextmanager
from typing import Iterator

import pyodbc

from db.sqlserver import obtener_conexion

logger = logging.getLogger(__name__)


@contextmanager
def transaccion() -> Iterator[pyodbc.Cursor]:
    """Un cursor dentro de una transacción: se confirma al salir bien, se deshace ante cualquier error.

    pyodbc abre las conexiones sin autocommit, así que la transacción empieza sola con la primera instrucción.
    """
    conexion = obtener_conexion()
    try:
        cursor = conexion.cursor()
        yield cursor
        conexion.commit()
    except BaseException:
        try:
            conexion.rollback()
        except pyodbc.Error:  # la conexión ya estaba rota: no hay nada que deshacer
            logger.warning("No se pudo deshacer la transacción (la conexión ya no respondía).", exc_info=True)
        raise
    finally:
        conexion.close()


@contextmanager
def lectura() -> Iterator[pyodbc.Cursor]:
    """Un cursor para consultar. Deshace al salir: no escribe nada, pero deja la conexión limpia antes de devolverla al pool."""
    conexion = obtener_conexion()
    try:
        yield conexion.cursor()
    finally:
        try:
            conexion.rollback()
        except pyodbc.Error:
            pass
        conexion.close()


def filas(cursor: pyodbc.Cursor) -> list[dict]:
    """El resultado actual del cursor como lista de diccionarios."""
    if cursor.description is None:
        return []
    columnas = [c[0] for c in cursor.description]
    return [dict(zip(columnas, f)) for f in cursor.fetchall()]


def una(cursor: pyodbc.Cursor) -> dict | None:
    """La primera fila como diccionario, o `None` si no hubo ninguna."""
    todas = filas(cursor)
    return todas[0] if todas else None
