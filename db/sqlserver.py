"""Acceso a SQL Server vía stored procedures — el diccionario VIEJO.

Regla del proyecto: esta API es el ÚNICO consumidor de la base. El front nunca
habla con SQL Server directo — pasa por aquí.

Lo que este módulo toca son los esquemas `documents`, `security` y `reference`,
y los toca SOLO por los stored procedures que dejó el DBA: ahí no se escribe SQL
de tablas. Desde la decisión del 2026-10-21 («Nexus sin stored procedures») esos
SPs quedaron CONGELADOS como especificación del dominio, así que tampoco se piden
nuevos: lo que nace de hoy en adelante vive en el esquema `nexus`, con su esquema
versionado en `migraciones/`, y se escribe con SQL directo desde `db/tablas.py`.
Este módulo se queda para lo que ya existía, y aporta además `version_de_esquema()`,
que es cómo `/health/db` sabe si la base va al día con el código.

pyodbc es síncrono; los endpoints de FastAPI que lo usan se declaran con `def`
(no `async def`) para que FastAPI los corra en su threadpool y no bloquee el
event loop.
"""

import re

import pyodbc

from errores import ConfiguracionIncompleta

from config import (
    SQLSERVER_DB,
    SQLSERVER_DRIVER,
    SQLSERVER_ENCRYPT,
    SQLSERVER_HOST,
    SQLSERVER_PASSWORD,
    SQLSERVER_PORT,
    SQLSERVER_TRUST_CERT,
    SQLSERVER_USER,
)

# Solo letras, números, guión bajo, punto y corchetes: suficiente para
# `dbo.sp_Nombre` o `[esquema].[sp_Nombre]`. El nombre del SP se interpola en el
# string de SQL (no puede ir como parámetro), así que se valida para que nunca
# pueda llegar algo arbitrario desde una request.
_NOMBRE_SP_VALIDO = re.compile(r"^[A-Za-z0-9_.\[\]]+$")


def _cadena_conexion() -> str:
    faltantes = [
        nombre
        for nombre, valor in (
            ("SQLSERVER_HOST", SQLSERVER_HOST),
            ("SQLSERVER_DB", SQLSERVER_DB),
            ("SQLSERVER_USER", SQLSERVER_USER),
            ("SQLSERVER_PASSWORD", SQLSERVER_PASSWORD),
        )
        if not valor
    ]
    if faltantes:
        raise ConfiguracionIncompleta(
            f"Faltan estas variables en el .env: {', '.join(faltantes)}. "
            "Revisa .env.example."
        )
    return (
        f"DRIVER={{{SQLSERVER_DRIVER}}};"
        f"SERVER={SQLSERVER_HOST},{SQLSERVER_PORT};"
        f"DATABASE={SQLSERVER_DB};"
        f"UID={SQLSERVER_USER};"
        f"PWD={SQLSERVER_PASSWORD};"
        f"TrustServerCertificate={SQLSERVER_TRUST_CERT};"
        f"Encrypt={SQLSERVER_ENCRYPT};"
    )


def obtener_conexion() -> pyodbc.Connection:
    """Abre una conexión. pyodbc reutiliza conexiones por debajo (pooling del
    driver manager ODBC), así que abrir/cerrar por operación es barato y evita
    tener que manejar un pool a mano."""
    return pyodbc.connect(_cadena_conexion())


def _filas_a_dicts(cursor: pyodbc.Cursor) -> list[dict]:
    """Convierte el result set actual del cursor a lista de dicts."""
    if cursor.description is None:
        return []
    columnas = [col[0] for col in cursor.description]
    return [dict(zip(columnas, fila)) for fila in cursor.fetchall()]


def ejecutar_sp(nombre: str, params: dict | None = None, commit: bool = False) -> list[dict]:
    """Ejecuta un stored procedure y devuelve su primer result set.

    Se usan parámetros nombrados (`EXEC sp @param=?`) en vez de posicionales
    para que el orden del dict no importe y para que el call se lea igual que la
    firma que documenta el DBA.

    `commit=True` para SPs que escriben (INSERT/UPDATE/DELETE); sin eso los
    cambios se revierten al cerrar la conexión.
    """
    if not _NOMBRE_SP_VALIDO.match(nombre):
        raise ValueError(f"Nombre de stored procedure inválido: {nombre!r}")

    params = params or {}
    if params:
        marcadores = ", ".join(f"@{clave}=?" for clave in params)
        sql = f"EXEC {nombre} {marcadores}"
    else:
        sql = f"EXEC {nombre}"

    conexion = obtener_conexion()
    try:
        cursor = conexion.cursor()
        cursor.execute(sql, *params.values())
        resultado = _filas_a_dicts(cursor)
        if commit:
            conexion.commit()
        return resultado
    finally:
        conexion.close()


def ejecutar_sp_multiple(nombre: str, params: dict | None = None, commit: bool = False) -> list[list[dict]]:
    """Igual que `ejecutar_sp` pero devuelve TODOS los result sets.

    Aplica cuando un SP hace varios SELECT (ej. devolver el registro creado y
    además un conteo). `ejecutar_sp` se quedaría solo con el primero.
    """
    if not _NOMBRE_SP_VALIDO.match(nombre):
        raise ValueError(f"Nombre de stored procedure inválido: {nombre!r}")

    params = params or {}
    if params:
        marcadores = ", ".join(f"@{clave}=?" for clave in params)
        sql = f"EXEC {nombre} {marcadores}"
    else:
        sql = f"EXEC {nombre}"

    conexion = obtener_conexion()
    try:
        cursor = conexion.cursor()
        cursor.execute(sql, *params.values())
        sets: list[list[dict]] = [_filas_a_dicts(cursor)]
        while cursor.nextset():
            sets.append(_filas_a_dicts(cursor))
        if commit:
            conexion.commit()
        return sets
    finally:
        conexion.close()


def version_de_esquema() -> int | None:
    """La última migración aplicada a ESTA base (`nexus.schemaMigrations`, ver `migraciones/`), o `None` si la tabla de control todavía no existe.
    Solo lee, con el usuario de la API (`db_datareader`)."""
    conexion = obtener_conexion()
    try:
        cursor = conexion.cursor()
        cursor.execute("SELECT OBJECT_ID('nexus.schemaMigrations', 'U')")
        if cursor.fetchone()[0] is None:
            return None
        cursor.execute("SELECT MAX(version) FROM nexus.schemaMigrations")
        fila = cursor.fetchone()
        return None if fila is None or fila[0] is None else int(fila[0])
    finally:
        conexion.close()


def probar_conexion() -> dict:
    """Diagnóstico: confirma que la API alcanza SQL Server. Lo usa /health/db."""
    conexion = obtener_conexion()
    try:
        cursor = conexion.cursor()
        cursor.execute("SELECT @@VERSION AS version, DB_NAME() AS base")
        fila = _filas_a_dicts(cursor)
        return fila[0] if fila else {}
    finally:
        conexion.close()
