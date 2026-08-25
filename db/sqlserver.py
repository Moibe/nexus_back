"""Acceso a SQL Server vía stored procedures.

Regla del proyecto: esta API es el ÚNICO consumidor de la base. El front nunca
habla con SQL Server directo — pasa por aquí. Y aquí nunca se escribe SQL de
tablas: el DBA expone stored procedures y este módulo solo los invoca. Si algo
requiere un query nuevo, se le pide un SP, no se escribe el SELECT aquí.

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
