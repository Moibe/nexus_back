"""Conexión de SOLO LECTURA para la búsqueda en lenguaje natural (NEX-605).

La consulta SQL la escribe un modelo de lenguaje, así que la seguridad no puede depender de que acierte. Dos cosas las garantiza la BASE:

- **solo se lee**: se entra con `usrNexusLectura`, que solo tiene SELECT sobre el esquema `[consulta]` (NEX-604). Leer `[nexus]`, escribir, crear
  objetos, ejecutar, `EXECUTE AS` u `OPENROWSET` fallan por permiso;
- **solo se ve la organización de quien pregunta**: las vistas de `[consulta]` (migración 0017) filtran por `SESSION_CONTEXT(N'organizacion')`, y
  aquí se fija ANTES de correr nada, con `@read_only = 1`, así que ni la consulta del modelo puede cambiarla (error 15664). Sin organización, las
  vistas devuelven 0 renglones.

Una conexión por pregunta: se abre, se fija la organización, se corre la consulta y se cierra. Si el pool del driver devolviera una conexión con
la organización de OTRA pregunta todavía puesta, fijar la nueva fallaría con 15664 y aquí se corta (`ConsultaNoDisponible`): nunca se corre
una consulta sin comprobar antes, en esa misma conexión, que la organización fijada es la de quien pregunta.

Topes (config.py): `CONSULTA_TIEMPO_MAXIMO_S` corta la consulta en el servidor (el driver manda la cancelación) y `CONSULTA_TOPE_RENGLONES`
limita lo que se devuelve; si hay más, `hay_mas` lo dice. `SET DEADLOCK_PRIORITY LOW`: si una consulta choca con una escritura, pierde la consulta.

La contraseña NO va en el `.env` de la API: sale de `~/.nexus-lectura.env` (o de la ruta de `NEXUS_LECTURA_ENV`), con `SQLSERVER_USER=usrNexusLectura`
y `SQLSERVER_PASSWORD`. El servidor, el puerto, el driver y la base son los MISMOS del back (`SQLSERVER_*` del `.env`): este usuario lee la base
que usa este back y ninguna otra. Si el archivo trae `SQLSERVER_DB` y no coincide, se niega.

pyodbc es síncrono: quien lo llame desde FastAPI lo hace en un endpoint `def` (threadpool), como el resto de `db/`.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

import config

USUARIO = "usrNexusLectura"
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
# El prefijo que pone pyodbc y el driver a cada mensaje («[42S22] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]») y la cola
# «(208) (SQLExecDirectW)».
_PREFIJO = re.compile(r"^(\[[^\]]*\]\s*)+")
_COLA = re.compile(r"\s*\((\d+)\)\s*\(SQL\w+\)\s*$")


class ConsultaNoDisponible(Exception):
    """La conexión de solo lectura no se pudo preparar: falta el archivo de credenciales, no es el usuario correcto, la base no responde o no
    se pudo fijar la organización. Es un problema NUESTRO, no de la consulta: el mensaje es para el log, no para quien pregunta."""


class ConsultaRechazada(Exception):
    """La base rechazó la consulta (sintaxis, columna inexistente, sin permiso…). `numero` es el error de SQL Server y `mensaje` su texto
    sin el prefijo del driver: sirve para pedirle al modelo que la corrija."""

    def __init__(self, mensaje: str, numero: int | None = None):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.numero = numero


class ConsultaTardo(Exception):
    """La consulta pasó del tiempo máximo y se canceló en el servidor."""

    def __init__(self, segundos: int):
        super().__init__(f"La consulta tardó más de {segundos} s y se canceló.")
        self.segundos = segundos


@dataclass
class Resultado:
    columnas: list[str]
    filas: list[list] = field(default_factory=list)
    hay_mas: bool = False
    tope: int = 0
    milisegundos: int = 0


def ruta_credenciales() -> Path:
    return Path(os.environ.get("NEXUS_LECTURA_ENV") or Path.home() / ".nexus-lectura.env")


def _credenciales() -> dict[str, str]:
    from dotenv import dotenv_values

    ruta = ruta_credenciales()
    if not ruta.is_file():
        raise ConsultaNoDisponible(f"No encuentro las credenciales de solo lectura en {ruta} (van fuera de los repos; ver el README, «Consultas de solo lectura»).")
    v = {k: (x or "").strip() for k, x in dotenv_values(ruta).items()}
    if v.get("SQLSERVER_USER") != USUARIO:
        raise ConsultaNoDisponible(f"{ruta.name} no es del usuario de solo lectura ({USUARIO}): no se corre una consulta con otro usuario.")
    if not v.get("SQLSERVER_PASSWORD"):
        raise ConsultaNoDisponible(f"A {ruta.name} le falta SQLSERVER_PASSWORD.")
    if v.get("SQLSERVER_DB") and v["SQLSERVER_DB"] != config.SQLSERVER_DB:
        raise ConsultaNoDisponible(f"{ruta.name} apunta a {v['SQLSERVER_DB']}, pero este back usa {config.SQLSERVER_DB or '(sin SQLSERVER_DB)'}.")
    if not config.SQLSERVER_HOST or not config.SQLSERVER_DB:
        raise ConsultaNoDisponible("Faltan SQLSERVER_HOST o SQLSERVER_DB en el .env del back.")
    return v


def _llave(valor: str) -> str:
    """Un valor de la cadena de conexión entre llaves (la contraseña puede traer `;`, `=` o `}`)."""
    return "{" + valor.replace("}", "}}") + "}"


def _conectar():
    import pyodbc

    v = _credenciales()
    servidor = config.SQLSERVER_HOST if "," in config.SQLSERVER_HOST else f"{config.SQLSERVER_HOST},{config.SQLSERVER_PORT}"
    cadena = (
        f"DRIVER={{{config.SQLSERVER_DRIVER}}};SERVER={servidor};DATABASE={config.SQLSERVER_DB};UID={USUARIO};PWD={_llave(v['SQLSERVER_PASSWORD'])};"
        f"TrustServerCertificate={config.SQLSERVER_TRUST_CERT};Encrypt={config.SQLSERVER_ENCRYPT};APP=Biblioratum consulta"
    )
    try:
        con = pyodbc.connect(cadena, timeout=15, autocommit=True)
    except pyodbc.Error as exc:
        raise ConsultaNoDisponible(f"No se pudo conectar como {USUARIO}: {_texto(exc)[0]}") from exc
    try:
        usuario, base = con.cursor().execute("SELECT SUSER_SNAME(), DB_NAME()").fetchone()
        if usuario != USUARIO or base != config.SQLSERVER_DB:
            raise ConsultaNoDisponible(f"Quedé conectado a {base} como {usuario}, no a {config.SQLSERVER_DB} como {USUARIO}.")
    except BaseException:
        con.close()
        raise
    return con


def _texto(exc: Exception) -> tuple[str, int | None]:
    """El mensaje de SQL Server sin el prefijo del driver, y su número de error."""
    crudo = str(exc.args[1]) if len(exc.args) > 1 else str(exc)
    partes = [p for p in re.split(r";\s*(?=\[)", crudo) if p.strip()]
    texto = partes[0] if partes else crudo
    numero = None
    m = _COLA.search(texto)
    if m:
        numero = int(m.group(1))
        texto = texto[: m.start()]
    return _PREFIJO.sub("", texto).strip(), numero


def _es_tiempo_agotado(exc: Exception) -> bool:
    codigo = exc.args[0] if exc.args and isinstance(exc.args[0], str) else ""
    return codigo in ("HYT00", "HYT01") or "timeout expired" in str(exc).lower()


def abrir(organizacion: str, tiempo_maximo: int | None = None):
    """Una conexión de solo lectura con la organización YA fijada (y comprobada). Quien la pide la cierra."""
    import pyodbc

    if not isinstance(organizacion, str) or not _GUID.fullmatch(organizacion):
        raise ValueError("La organización debe ser el guid de una organización.")
    con = _conectar()
    try:
        cur = con.cursor()
        cur.execute("SET DEADLOCK_PRIORITY LOW")
        try:
            cur.execute("EXEC sys.sp_set_session_context @key = N'organizacion', @value = ?, @read_only = 1", organizacion)
        except pyodbc.Error as exc:
            texto, numero = _texto(exc)
            raise ConsultaNoDisponible(f"No se pudo fijar la organización en la conexión ({numero}): {texto}") from exc
        fijada = cur.execute("SELECT CAST(SESSION_CONTEXT(N'organizacion') AS varchar(63))").fetchone()[0]
        if (fijada or "").lower() != organizacion.lower():
            raise ConsultaNoDisponible("La organización fijada en la conexión no es la de quien pregunta.")
        con.timeout = int(tiempo_maximo or config.CONSULTA_TIEMPO_MAXIMO_S)
        return con
    except BaseException:
        con.close()
        raise


def consultar(organizacion: str, sql: str, *, tope: int | None = None, tiempo_maximo: int | None = None) -> Resultado:
    """Corre `sql` como `usrNexusLectura` con la organización fijada. Devuelve el PRIMER conjunto de resultados, hasta `tope` renglones.

    Errores: `ValueError` (organización o consulta vacías), `ConsultaNoDisponible` (nuestra conexión), `ConsultaRechazada` (la base no la aceptó),
    `ConsultaTardo` (pasó del tiempo máximo)."""
    import pyodbc

    if not isinstance(sql, str) or not sql.strip():
        raise ValueError("La consulta está vacía.")
    tope = max(1, int(tope or config.CONSULTA_TOPE_RENGLONES))
    segundos = int(tiempo_maximo or config.CONSULTA_TIEMPO_MAXIMO_S)
    con = abrir(organizacion, segundos)
    inicio = time.monotonic()
    try:
        cur = con.cursor()
        try:
            cur.execute(sql)
            while cur.description is None and cur.nextset():
                pass
            if cur.description is None:
                return Resultado(columnas=[], tope=tope, milisegundos=int((time.monotonic() - inicio) * 1000))
            columnas = [d[0] for d in cur.description]
            filas = cur.fetchmany(tope + 1)
        except pyodbc.Error as exc:
            if _es_tiempo_agotado(exc):
                raise ConsultaTardo(segundos) from exc
            texto, numero = _texto(exc)
            raise ConsultaRechazada(texto, numero) from exc
        return Resultado(
            columnas=columnas,
            filas=[list(f) for f in filas[:tope]],
            hay_mas=len(filas) > tope,
            tope=tope,
            milisegundos=int((time.monotonic() - inicio) * 1000),
        )
    finally:
        con.close()


def probar() -> dict:
    """Diagnóstico para `/health/consulta`: ¿hay credenciales, entra como usrNexusLectura y, SIN organización, las vistas dan 0 renglones y
    `[nexus]` está cerrado? No fija organización ni devuelve datos. Lo que se publica es solo el estado: el detalle va al log."""
    import pyodbc

    con = _conectar()
    try:
        cur = con.cursor()
        documentos = cur.execute("SELECT COUNT(*) FROM consulta.vDocumentoExtraido").fetchone()[0]
        datos = cur.execute("SELECT COUNT(*) FROM consulta.vDatoExtraido").fetchone()[0]
        try:
            cur.execute("SELECT TOP (0) 1 FROM nexus.tenants")
            nexus_cerrado = False
        except pyodbc.Error as exc:
            nexus_cerrado = _texto(exc)[1] == 229
        return {"sin_organizacion_vacio": documentos == 0 and datos == 0, "nexus_cerrado": nexus_cerrado}
    finally:
        con.close()
