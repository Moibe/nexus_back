"""Corredor de migraciones de Nexus.

Nexus ya no usa stored procedures (decisión del 2026-10-21): el esquema de la base se versiona AQUÍ, en archivos numerados, y se aplica con este
corredor y el usuario de migraciones `usrNexusDDL`. Nada se cambia en la base desde una consola ni al desplegar la aplicación.

    python -m migraciones estado  --base IA_Nexus_Dev
    python -m migraciones aplicar --base IA_Nexus_Dev                 (simulación: lo hace todo en una transacción y la deshace)
    python -m migraciones aplicar --base IA_Nexus_Dev --confirmar
    python -m migraciones marcar  --base IA_Nexus_Dev --version 1 --confirmar     (da por aplicada una migración que la base YA tiene)
    python -m migraciones nueva   "agrega la tabla de tipos documentales"          (crea el siguiente archivo)

## Los archivos

`migraciones/NNNN_nombre.sql`, con versiones consecutivas desde 0001 (sin huecos ni repetidas). Se parten en lotes por las líneas `GO`. Una vez
aplicada en CUALQUIER base, una migración NO se edita: su suma de comprobación (sha256 del texto, con los saltos de línea normalizados) queda
anotada, y si el archivo cambia el corredor se niega. Se corrige con una migración nueva.

## La tabla de control

`nexus.schemaMigrations` (la crea el propio corredor, dentro de la transacción de la primera migración: una simulación no deja ni la tabla): versión, nombre, suma, cuándo, quién, `modo` (`ejecutada` o `marcada`) y cuánto tardó.
`marcada` es para una base que ya tenía el esquema de antes de que existiera el corredor (la línea base 0001 se MARCA en IA_Nexus, IA_Nexus_Dev
e IA_Nexus_Staging; solo se EJECUTA en una base vacía).

## Las reglas que protegen

- `--base` es obligatorio y solo acepta las tres bases de Nexus; el corredor comprueba que de verdad quedó conectado a esa (`DB_NAME()`).
- Sin `--confirmar` es una SIMULACIÓN: se ejecuta todo en una sola transacción y se deshace al final (el DDL de SQL Server es transaccional).
- **La cadena dev → staging → prod:** una migración solo se aplica (o se marca) en `IA_Nexus_Staging` si ya está en `IA_Nexus_Dev` CON LA MISMA
  SUMA, y en `IA_Nexus` (producción) si ya está en staging. No hay forma de saltársela.
- Producción (`IA_Nexus`) pide además `--produccion` junto a `--confirmar`.
- Cada migración corre en su PROPIA transacción: si la 3 falla, la 1 y la 2 quedan aplicadas y la 3 no deja nada a medias.
- Un candado de aplicación (`sp_getapplock`) impide que dos corredores trabajen a la vez sobre la misma base.
- Las credenciales salen de `~/.nexus-sql-ddl.env` (o de la ruta de `NEXUS_DDL_ENV`), NUNCA del `.env` del servidor ni del de la API: la contraseña
  de migraciones vive solo en la laptop de quien migra, y las migraciones no corren solas al desplegar.
"""

import argparse
import hashlib
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

CARPETA = Path(__file__).parent
# Las tres bases de Nexus y, para cada una, la que tiene que ir ANTES en la cadena (`None` = la primera).
CADENA: dict[str, str | None] = {"IA_Nexus_Dev": None, "IA_Nexus_Staging": "IA_Nexus_Dev", "IA_Nexus": "IA_Nexus_Staging"}
PRODUCCION = "IA_Nexus"
TABLA_CONTROL = "nexus.schemaMigrations"

_RE_NOMBRE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
# Una línea que solo dice GO (con espacios y, si acaso, un comentario). Es la convención de sqlcmd/SSMS; pyodbc no la entiende.
_RE_GO = re.compile(r"^[ \t]*GO[ \t]*(?:--.*)?$", re.IGNORECASE | re.MULTILINE)


class ErrorMigracion(Exception):
    """Algo que el corredor se niega a hacer. Su texto se puede mostrar tal cual."""


def normalizar(sql: str) -> str:
    """El texto con saltos de línea `\\n`, sin BOM y con UN salto al final: así la suma no cambia entre Windows y Linux ni por el `autocrlf` de git."""
    texto = sql.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    return texto.rstrip("\n") + "\n"


def suma_de(sql: str) -> str:
    return hashlib.sha256(normalizar(sql).encode("utf-8")).hexdigest()


def partir_en_lotes(sql: str) -> list[str]:
    """Los lotes de un script, separados por líneas `GO`. Los lotes vacíos se descartan."""
    return [lote.strip("\n") for lote in _RE_GO.split(normalizar(sql)) if lote.strip()]


@dataclass(frozen=True)
class Migracion:
    version: int
    nombre: str
    sql: str

    @property
    def suma(self) -> str:
        return suma_de(self.sql)

    @property
    def lotes(self) -> list[str]:
        return partir_en_lotes(self.sql)

    @property
    def archivo(self) -> str:
        return f"{self.version:04d}_{self.nombre}.sql"


def descubrir(carpeta: Path | None = None) -> list[Migracion]:
    """Las migraciones de la carpeta, en orden. Se niega ante un nombre que parece migración pero está mal escrito, un hueco o una versión repetida."""
    carpeta = carpeta or CARPETA  # al llamar, no al definir: así se puede apuntar a otra carpeta en las pruebas
    encontradas: list[Migracion] = []
    for ruta in sorted(carpeta.glob("*.sql")):
        m = _RE_NOMBRE.match(ruta.name)
        if not m:
            raise ErrorMigracion(f"«{ruta.name}» no se llama NNNN_nombre.sql (cuatro dígitos, guion bajo, minúsculas, dígitos o guiones bajos).")
        encontradas.append(Migracion(int(m.group(1)), m.group(2), ruta.read_text(encoding="utf-8")))
    versiones = [m.version for m in encontradas]
    if len(set(versiones)) != len(versiones):
        raise ErrorMigracion(f"Hay versiones repetidas: {sorted(v for v in set(versiones) if versiones.count(v) > 1)}.")
    if versiones != list(range(1, len(versiones) + 1)):
        raise ErrorMigracion(f"Las versiones tienen que ser consecutivas desde 0001; hay {versiones}.")
    return encontradas


class Base(Protocol):
    """Lo que el corredor necesita de una base. La real habla con SQL Server (`BaseSql`); las pruebas usan una en memoria."""

    nombre: str

    def preparar(self) -> None: ...  # toma el candado (no escribe nada)
    def aplicadas(self) -> dict[int, dict]: ...  # versión → {nombre, suma, modo}; vacío si la tabla de control no existe
    def correr(self, m: Migracion, marcar: bool) -> int: ...  # ejecuta (o solo anota) dentro de la transacción abierta; milisegundos
    def confirmar(self) -> None: ...
    def deshacer(self) -> None: ...
    def cerrar(self) -> None: ...


@dataclass(frozen=True)
class Fila:
    version: int
    nombre: str
    estado: str  # aplicada · marcada · pendiente · cambiada
    detalle: str = ""


def _revisar(base: Base, migraciones: list[Migracion]) -> tuple[list[Fila], dict[int, dict]]:
    aplicadas = base.aplicadas()
    conocidas = {m.version for m in migraciones}
    sueltas = sorted(set(aplicadas) - conocidas)
    if sueltas:
        raise ErrorMigracion(f"{base.nombre} tiene aplicadas las versiones {sueltas}, que no están en la carpeta de migraciones: esta copia del repositorio está atrasada o falta un archivo.")
    filas: list[Fila] = []
    for m in migraciones:
        previa = aplicadas.get(m.version)
        if previa is None:
            filas.append(Fila(m.version, m.nombre, "pendiente"))
        elif previa["suma"] != m.suma:
            filas.append(Fila(m.version, m.nombre, "cambiada", "el archivo cambió DESPUÉS de aplicarse: una migración aplicada no se edita, se corrige con otra"))
        else:
            filas.append(Fila(m.version, m.nombre, "marcada" if previa.get("modo") == "marcada" else "aplicada"))
    cambiadas = [f for f in filas if f.estado == "cambiada"]
    if cambiadas:
        raise ErrorMigracion(f"{base.nombre}: " + "; ".join(f"{f.version:04d}_{f.nombre} {f.detalle}" for f in cambiadas))
    huecos = [f.version for f in filas if f.estado == "pendiente"]
    aplicadas_despues = [f.version for f in filas if f.estado != "pendiente" and huecos and f.version > min(huecos)]
    if aplicadas_despues:
        raise ErrorMigracion(f"{base.nombre} tiene aplicadas {aplicadas_despues} pero le falta {min(huecos)}: las migraciones se aplican en orden.")
    return filas, aplicadas


def estado(base: Base, migraciones: list[Migracion]) -> list[Fila]:
    """El estado de cada migración en esa base. Solo lee."""
    return _revisar(base, migraciones)[0]


def _exigir_cadena(destino: str, anterior: Base | None, objetivos: list[Migracion]) -> None:
    if anterior is None:
        return
    en_anterior = anterior.aplicadas()
    for m in objetivos:
        previa = en_anterior.get(m.version)
        if previa is None:
            raise ErrorMigracion(f"La migración {m.archivo} no está aplicada en {anterior.nombre}: va primero ahí (dev → staging → prod) y se prueba antes de pasar a {destino}.")
        if previa["suma"] != m.suma:
            raise ErrorMigracion(f"La migración {m.archivo} en {anterior.nombre} tiene OTRA suma que este archivo: no es la que se probó allá. Se corrige con una migración nueva, no editando la aplicada.")


def ejecutar(
    base: Base,
    migraciones: list[Migracion],
    *,
    accion: str,
    hasta: int | None = None,
    version: int | None = None,
    confirmar: bool = False,
    anterior: Base | None = None,
) -> list[str]:
    """`aplicar` (todas las pendientes, o hasta `hasta`) o `marcar` (exactamente `version`, que debe ser la siguiente pendiente).

    Sin `confirmar` es una simulación: todo en UNA transacción que se deshace al final. Devuelve las líneas que se muestran al usuario."""
    if accion not in ("aplicar", "marcar"):
        raise ValueError(accion)
    base.preparar()
    filas, _ = _revisar(base, migraciones)
    pendientes = [m for m, f in zip(migraciones, filas) if f.estado == "pendiente"]
    if accion == "marcar":
        if version is None:
            raise ErrorMigracion("`marcar` necesita --version.")
        if not pendientes or pendientes[0].version != version:
            siguiente = pendientes[0].version if pendientes else None
            raise ErrorMigracion(
                f"La versión {version} no es la siguiente pendiente de {base.nombre} (la siguiente es {siguiente}): solo se marca la que sigue, en orden."
                if siguiente
                else f"{base.nombre} no tiene nada pendiente."
            )
        objetivos = [pendientes[0]]
    else:
        objetivos = [m for m in pendientes if hasta is None or m.version <= hasta]
    if not objetivos:
        return [f"{base.nombre}: no hay migraciones pendientes."]
    _exigir_cadena(base.nombre, anterior, objetivos)

    verbo = "marcada" if accion == "marcar" else "aplicada"
    lineas: list[str] = []
    try:
        for m in objetivos:
            ms = base.correr(m, marcar=accion == "marcar")
            lineas.append(f"  {m.archivo}: {verbo} ({ms} ms)")
            if confirmar:
                base.confirmar()  # cada migración, en su propia transacción
        if not confirmar:
            base.deshacer()
            lineas.append(f"SIMULACIÓN en {base.nombre}: todo corrió bien y se DESHIZO. Agrega --confirmar para hacerlo de verdad.")
        else:
            lineas.append(f"Listo: {len(objetivos)} migración(es) {verbo}(s) en {base.nombre}.")
    except Exception:
        base.deshacer()
        raise
    return lineas


# ── La base real ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _credenciales() -> dict[str, str]:
    from dotenv import dotenv_values

    ruta = Path(os.environ.get("NEXUS_DDL_ENV") or Path.home() / ".nexus-sql-ddl.env")
    if not ruta.is_file():
        raise ErrorMigracion(f"No encuentro las credenciales de migraciones en {ruta}. Va fuera de los repos; ver el README (sección de migraciones).")
    valores = {k: v for k, v in dotenv_values(ruta).items() if v}
    faltan = [k for k in ("SQLSERVER_HOST", "SQLSERVER_USER", "SQLSERVER_PASSWORD") if not valores.get(k)]
    if faltan:
        raise ErrorMigracion(f"A {ruta.name} le faltan: {', '.join(faltan)}.")
    if valores["SQLSERVER_USER"] != "usrNexusDDL":
        raise ErrorMigracion(f"{ruta.name} no es del usuario de migraciones (usrNexusDDL): el corredor no corre con otro usuario.")
    return valores


class BaseSql:
    """Una base de SQL Server, abierta con `usrNexusDDL`. Nunca confirma sola: `confirmar()` y `deshacer()` los pide el corredor."""

    def __init__(self, nombre: str, valores: dict[str, str] | None = None):
        import pyodbc

        # Sin el pool de conexiones del driver: con él, `close()` deja la conexión viva en el pool y el candado de SESIÓN (sp_getapplock) con ella,
        # y la siguiente corrida del mismo proceso se encontraría con «otro corredor trabajando».
        pyodbc.pooling = False

        if nombre not in CADENA:
            raise ErrorMigracion(f"«{nombre}» no es una base de Nexus ({', '.join(CADENA)}).")
        v = valores or _credenciales()
        driver = v.get("SQLSERVER_DRIVER") or "ODBC Driver 17 for SQL Server"
        self.nombre = nombre
        self._candado = False  # el candado de aplicación es REENTRANTE: se toma una vez por conexión y se suelta una vez
        self._con = pyodbc.connect(
            f"DRIVER={{{driver}}};SERVER={v['SQLSERVER_HOST']},{v.get('SQLSERVER_PORT') or '1433'};DATABASE={nombre};UID={v['SQLSERVER_USER']};"
            f"PWD={v['SQLSERVER_PASSWORD']};TrustServerCertificate=yes;Encrypt=yes",
            timeout=20,
            autocommit=False,
        )
        cur = self._con.cursor()
        cur.execute("SELECT DB_NAME(), SUSER_NAME()")
        base_real, usuario = cur.fetchone()
        self._con.rollback()
        if base_real != nombre or usuario != "usrNexusDDL":
            self._con.close()
            raise ErrorMigracion(f"Quedé conectado a {base_real} como {usuario}, no a {nombre} como usrNexusDDL: no hago nada.")

    def _uno(self, sql: str, *args):
        cur = self._con.cursor()
        cur.execute(sql, *args)
        fila = cur.fetchone()
        self._con.rollback()
        return fila[0] if fila else None

    def preparar(self) -> None:
        if self._candado:
            return
        cur = self._con.cursor()
        # El candado es de la SESIÓN: dura mientras el corredor esté conectado y evita que dos corran a la vez sobre la misma base.
        cur.execute("DECLARE @r int; EXEC @r = sp_getapplock @Resource = N'nexus.migraciones', @LockMode = 'Exclusive', @LockOwner = 'Session', @LockTimeout = 0; SELECT @r")
        if cur.fetchone()[0] < 0:
            self._con.rollback()
            raise ErrorMigracion(f"Otro corredor está trabajando sobre {self.nombre}; espera a que termine.")
        self._candado = True
        self._con.commit()

    def _crear_control_si_falta(self, cur) -> None:
        cur.execute(
            f"""IF OBJECT_ID('{TABLA_CONTROL}', 'U') IS NULL
                CREATE TABLE {TABLA_CONTROL} (
                    version int NOT NULL CONSTRAINT PK_schemaMigrations PRIMARY KEY,
                    nombre nvarchar(200) NOT NULL,
                    suma char(64) NOT NULL,
                    modo varchar(10) NOT NULL CONSTRAINT CK_schemaMigrations_modo CHECK (modo IN ('ejecutada', 'marcada')),
                    aplicadaEn datetime2 NOT NULL CONSTRAINT DF_schemaMigrations_aplicadaEn DEFAULT SYSUTCDATETIME(),
                    aplicadaPor sysname NOT NULL CONSTRAINT DF_schemaMigrations_aplicadaPor DEFAULT SUSER_NAME(),
                    duracionMs int NOT NULL
                )"""
        )

    def aplicadas(self) -> dict[int, dict]:
        if self._uno(f"SELECT OBJECT_ID('{TABLA_CONTROL}', 'U')") is None:
            return {}
        cur = self._con.cursor()
        cur.execute(f"SELECT version, nombre, suma, modo FROM {TABLA_CONTROL} ORDER BY version")
        filas = {r[0]: {"nombre": r[1], "suma": r[2].strip(), "modo": r[3]} for r in cur.fetchall()}
        self._con.rollback()
        return filas

    def correr(self, m: Migracion, marcar: bool) -> int:
        inicio = time.monotonic()
        cur = self._con.cursor()
        self._crear_control_si_falta(cur)  # dentro de la misma transacción: si se deshace, no queda
        if not marcar:
            for lote in m.lotes:
                cur.execute(lote)
                while cur.nextset():  # un error a media pasada de un lote sale aquí
                    pass
        ms = int((time.monotonic() - inicio) * 1000)
        cur.execute(
            f"INSERT INTO {TABLA_CONTROL} (version, nombre, suma, modo, duracionMs) VALUES (?, ?, ?, ?, ?)",
            m.version, m.nombre, m.suma, "marcada" if marcar else "ejecutada", ms,
        )
        return ms

    def confirmar(self) -> None:
        self._con.commit()

    def deshacer(self) -> None:
        self._con.rollback()

    def cerrar(self) -> None:
        try:
            try:
                self._con.rollback()
                if self._candado:
                    self._con.cursor().execute("EXEC sp_releaseapplock @Resource = N'nexus.migraciones', @LockOwner = 'Session'")
                    self._con.commit()
                    self._candado = False
            except Exception:  # noqa: BLE001  (si la conexión ya murió, da igual: al morir se suelta)
                pass
            self._con.close()
        except Exception:  # noqa: BLE001
            pass


# ── La línea de comandos ────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _nueva(nombre: str) -> Path:
    limpio = re.sub(r"[^a-z0-9]+", "_", nombre.lower().encode("ascii", "ignore").decode() or "migracion").strip("_")
    if not limpio:
        raise ErrorMigracion("El nombre no tiene letras ni números.")
    siguiente = len(descubrir()) + 1
    ruta = CARPETA / f"{siguiente:04d}_{limpio}.sql"
    ruta.write_text(
        f"-- {siguiente:04d} · {nombre}\n--\n-- Una migración aplicada NO se edita: se corrige con otra. Los lotes se separan con una línea que solo diga GO.\n"
        "-- Qué cambia y por qué, en una o dos líneas:\n\n",
        encoding="utf-8",
        newline="\n",
    )
    return ruta


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="python -m migraciones", description="Corredor de migraciones de Nexus (usrNexusDDL).")
    sub = parser.add_subparsers(dest="orden", required=True)
    for nombre in ("estado", "aplicar", "marcar"):
        p = sub.add_parser(nombre)
        p.add_argument("--base", required=True, choices=list(CADENA), help="La base. Obligatoria: no hay valor por omisión.")
        if nombre != "estado":
            p.add_argument("--confirmar", action="store_true", help="Hacerlo de verdad. Sin esto es una simulación que se deshace.")
            p.add_argument("--produccion", action="store_true", help=f"Obligatorio además de --confirmar para {PRODUCCION}.")
        if nombre == "aplicar":
            p.add_argument("--hasta", type=int, help="Aplica solo hasta esa versión.")
        if nombre == "marcar":
            p.add_argument("--version", type=int, required=True)
    p = sub.add_parser("nueva", help="Crea el siguiente archivo de migración.")
    p.add_argument("nombre")
    args = parser.parse_args(argv)

    try:
        if args.orden == "nueva":
            print("Creada:", _nueva(args.nombre))
            return 0
        migraciones = descubrir()
        if args.orden != "estado" and args.confirmar and args.base == PRODUCCION and not args.produccion:
            raise ErrorMigracion(f"{PRODUCCION} es producción: para escribir ahí hay que pasar --produccion además de --confirmar.")
        valores = _credenciales()
        base = BaseSql(args.base, valores)
        anterior = BaseSql(CADENA[args.base], valores) if CADENA[args.base] else None
        try:
            if args.orden == "estado":
                print(f"{args.base}:")
                for f in estado(base, migraciones):
                    print(f"  {f.version:04d}_{f.nombre:40} {f.estado}")
            else:
                for linea in ejecutar(
                    base, migraciones, accion=args.orden, hasta=getattr(args, "hasta", None), version=getattr(args, "version", None), confirmar=args.confirmar, anterior=anterior
                ):
                    print(linea)
        finally:
            base.cerrar()
            if anterior:
                anterior.cerrar()
        return 0
    except ErrorMigracion as exc:
        print(f"NO: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
