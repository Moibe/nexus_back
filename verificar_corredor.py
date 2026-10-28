"""¿El corredor de migraciones (`python -m migraciones`) se porta como promete, y se NIEGA cuando debe?

    .venv/Scripts/python verificar_corredor.py

OFFLINE: no toca ninguna base. Usa una base en memoria que cumple el mismo contrato que `BaseSql` (la que habla con SQL Server) y comprueba la lógica: la suma de
comprobación, el partir en lotes por `GO`, los archivos, el orden, la simulación que se deshace, una migración por transacción, la que falla sin dejar nada a
medias, la que cambió después de aplicarse, `marcar`, la cadena dev → staging → prod y las banderas de la línea de comandos. Lo que SÍ habla con SQL Server
(el DDL real, el candado, la tabla de control) lo prueba `verificar_migraciones_sql.py`, que necesita Docker.
"""

import contextlib
import io
import os
import shutil
import tempfile
from pathlib import Path

from migraciones import corredor
from migraciones.corredor import ErrorMigracion, Migracion, descubrir, ejecutar, estado, normalizar, partir_en_lotes, suma_de

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + extra if extra and not ok else ''}")
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


def niega(llamada, trozo: str) -> bool:
    try:
        llamada()
    except ErrorMigracion as exc:
        return trozo in str(exc)
    return False


class BaseEnMemoria:
    """Cumple el contrato de `corredor.Base`. Una transacción es un buffer que se confirma o se descarta."""

    def __init__(self, nombre: str, falla_en: str | None = None):
        self.nombre = nombre
        self.tabla: dict[int, dict] = {}
        self.pendiente: dict[int, dict] = {}
        self.ejecutados: list[str] = []  # los lotes que SÍ se mandaron a la base (en cualquier transacción)
        self.falla_en = falla_en
        self.candado_tomado = False
        self.confirmaciones = 0

    def preparar(self) -> None:
        self.candado_tomado = True

    def aplicadas(self) -> dict[int, dict]:
        return {v: dict(f) for v, f in self.tabla.items()}

    def correr(self, m: Migracion, marcar: bool) -> int:
        if not marcar:
            for lote in m.lotes:
                if self.falla_en and self.falla_en in lote:
                    raise RuntimeError(f"falla en «{self.falla_en}»")
                self.ejecutados.append(lote)
        self.pendiente[m.version] = {"nombre": m.nombre, "suma": m.suma, "modo": "marcada" if marcar else "ejecutada"}
        return 1

    def confirmar(self) -> None:
        self.tabla.update(self.pendiente)
        self.pendiente = {}
        self.confirmaciones += 1

    def deshacer(self) -> None:
        self.pendiente = {}

    def cerrar(self) -> None:
        pass


def migracion(version: int, nombre: str, sql: str) -> Migracion:
    return Migracion(version, nombre, sql)


M1 = migracion(1, "linea_base", "CREATE TABLE a (id int);\nGO\nCREATE TABLE b (id int);\n")
M2 = migracion(2, "agrega_c", "CREATE TABLE c (id int);\nGO\n")
M3 = migracion(3, "agrega_d", "CREATE TABLE d (id int);\nGO\nSELECT 1/0 AS exploto;\n")
TODAS = [M1, M2, M3]

try:
    titulo("1 · La suma de comprobación no depende del sistema operativo")
    rev("los saltos de línea de Windows y de Linux dan la MISMA suma", suma_de("a\r\nb\r\n") == suma_de("a\nb\n") == suma_de("a\nb"))
    rev("un BOM al inicio no la cambia", suma_de("﻿a\n") == suma_de("a\n"))
    rev("un texto distinto sí", suma_de("a\n") != suma_de("b\n"))
    rev("es un sha256 (64 hexadecimales)", len(suma_de("x")) == 64 and all(c in "0123456789abcdef" for c in suma_de("x")))
    rev("normalizar deja un solo salto al final", normalizar("x\n\n\n") == "x\n" and normalizar("x") == "x\n")

    titulo("2 · Partir en lotes por `GO`")
    rev("dos lotes separados por GO", partir_en_lotes("A;\nGO\nB;\n") == ["A;", "B;"])
    rev("GO en minúsculas, con espacios y con un comentario detrás", partir_en_lotes("A;\n  go  \nB;\ngO -- fin\nC;") == ["A;", "B;", "C;"])
    rev("lotes vacíos se descartan (GO seguidos, GO al inicio y al final)", partir_en_lotes("GO\nA;\nGO\nGO\n\nGO\n") == ["A;"])
    rev("una línea que CONTIENE go pero no es solo GO no parte (GOTO, `-- GO`, un nombre)", partir_en_lotes("GOTO fin;\n-- GO\nSELECT 'go' AS ago;\ngoal:\n") == ["GOTO fin;\n-- GO\nSELECT 'go' AS ago;\ngoal:"])
    rev("un script sin GO es un solo lote", partir_en_lotes("SELECT 1;\nSELECT 2;") == ["SELECT 1;\nSELECT 2;"])
    rev("con saltos de Windows también parte", partir_en_lotes("A;\r\nGO\r\nB;\r\n") == ["A;", "B;"])

    titulo("3 · Los archivos de la carpeta")
    carpeta = Path(tempfile.mkdtemp(prefix="verificar-corredor-"))
    try:
        (carpeta / "0001_linea_base.sql").write_text("SELECT 1;\n", encoding="utf-8")
        (carpeta / "0002_agrega_c.sql").write_text("SELECT 2;\r\n", encoding="utf-8")
        (carpeta / "leeme.txt").write_text("no es migración", encoding="utf-8")
        encontradas = descubrir(carpeta)
        rev("las encuentra en orden y ignora lo que no es .sql", [(m.version, m.nombre) for m in encontradas] == [(1, "linea_base"), (2, "agrega_c")])
        rev("el nombre de archivo se reconstruye igual", [m.archivo for m in encontradas] == ["0001_linea_base.sql", "0002_agrega_c.sql"])
        (carpeta / "0004_salto.sql").write_text("x", encoding="utf-8")
        rev("un hueco en las versiones: se niega", niega(lambda: descubrir(carpeta), "consecutivas"))
        (carpeta / "0004_salto.sql").rename(carpeta / "0003_ok.sql")
        (carpeta / "0003_otra.sql").write_text("x", encoding="utf-8")
        rev("dos archivos con la misma versión: se niega", niega(lambda: descubrir(carpeta), "repetidas"))
        (carpeta / "0003_otra.sql").unlink()
        (carpeta / "Agrega Tabla.sql").write_text("x", encoding="utf-8")
        rev("un .sql que no se llama NNNN_nombre.sql (un typo): se niega en vez de ignorarlo", niega(lambda: descubrir(carpeta), "no se llama"))
        (carpeta / "Agrega Tabla.sql").unlink()
        (carpeta / "0003_Mayusculas.sql").write_text("x", encoding="utf-8")
        rev("nombre con mayúsculas: se niega", niega(lambda: descubrir(carpeta), "no se llama"))
        rev("la carpeta real del repositorio se lee sin errores", bool(descubrir()) and descubrir()[0].nombre == "linea_base")
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)

    titulo("4 · Estado")
    b = BaseEnMemoria("IA_Nexus_Dev")
    rev("una base virgen: todo pendiente", [f.estado for f in estado(b, TODAS)] == ["pendiente"] * 3)

    titulo("5 · Simulación: todo en una transacción que se deshace")
    b = BaseEnMemoria("IA_Nexus_Dev")
    lineas = ejecutar(b, [M1, M2], accion="aplicar", confirmar=False)
    rev("corrió los lotes de las dos migraciones", b.ejecutados == ["CREATE TABLE a (id int);", "CREATE TABLE b (id int);", "CREATE TABLE c (id int);"], str(b.ejecutados))
    rev("y NO quedó nada anotado (se deshizo)", b.tabla == {} and b.pendiente == {} and b.confirmaciones == 0)
    rev("y lo dice: SIMULACIÓN, con la bandera para hacerlo", any("SIMULACIÓN" in x and "--confirmar" in x for x in lineas), str(lineas))
    rev("tomó el candado de la base", b.candado_tomado)

    titulo("6 · Aplicar de verdad: cada migración en SU transacción")
    b = BaseEnMemoria("IA_Nexus_Dev")
    ejecutar(b, [M1, M2], accion="aplicar", confirmar=True)
    rev("quedaron las dos, con su suma y en modo `ejecutada`", sorted(b.tabla) == [1, 2] and b.tabla[1]["suma"] == M1.suma and b.tabla[2]["modo"] == "ejecutada")
    rev("se confirmó DOS veces (una por migración), no una al final", b.confirmaciones == 2)
    rev("el estado ahora dice aplicada", [f.estado for f in estado(b, [M1, M2])] == ["aplicada", "aplicada"])
    lineas = ejecutar(b, [M1, M2], accion="aplicar", confirmar=True)
    rev("repetir no hace nada y lo dice", "no hay migraciones pendientes" in lineas[0] and b.confirmaciones == 2, str(lineas))
    b2 = BaseEnMemoria("IA_Nexus_Dev")
    ejecutar(b2, TODAS[:3], accion="aplicar", hasta=1, confirmar=True)
    rev("`hasta` aplica solo hasta esa versión", sorted(b2.tabla) == [1])

    titulo("7 · Una migración que falla no deja nada a medias, y las anteriores se quedan")
    b = BaseEnMemoria("IA_Nexus_Dev", falla_en="1/0")
    try:
        ejecutar(b, TODAS, accion="aplicar", confirmar=True)
        rev("la excepción sube", False)
    except RuntimeError as exc:
        rev("la excepción sube (no se traga)", "1/0" in str(exc))
    rev("la 1 y la 2 quedaron aplicadas; la 3 NO queda anotada (que sus lotes se deshacen en SQL Server lo prueba verificar_migraciones_sql.py)", sorted(b.tabla) == [1, 2] and b.pendiente == {})
    b = BaseEnMemoria("IA_Nexus_Dev", falla_en="CREATE TABLE b")
    try:
        ejecutar(b, TODAS, accion="aplicar", confirmar=False)
    except RuntimeError:
        pass
    rev("fallar en una simulación tampoco deja nada", b.tabla == {} and b.pendiente == {})

    titulo("8 · Una migración aplicada NO se edita")
    b = BaseEnMemoria("IA_Nexus_Dev")
    ejecutar(b, [M1, M2], accion="aplicar", confirmar=True)
    editada = migracion(2, "agrega_c", "CREATE TABLE c (id int, de_mas int);\nGO\n")
    rev("si el archivo cambió después de aplicarse: se niega, y dice cuál", niega(lambda: ejecutar(b, [M1, editada], accion="aplicar", confirmar=True), "agrega_c"))
    rev("`estado` también se niega", niega(lambda: estado(b, [M1, editada]), "cambió"))
    rev("una versión aplicada en la base que no está en la carpeta (copia atrasada): se niega", niega(lambda: estado(b, [M1]), "no están en la carpeta"))
    b = BaseEnMemoria("IA_Nexus_Dev")
    b.tabla[2] = {"nombre": "agrega_c", "suma": M2.suma, "modo": "ejecutada"}
    rev("aplicada la 2 pero falta la 1: se niega (en orden)", niega(lambda: estado(b, [M1, M2]), "en orden"))

    titulo("9 · Marcar: dar por aplicada una migración que la base YA tiene")
    b = BaseEnMemoria("IA_Nexus_Dev")
    ejecutar(b, [M1, M2], accion="marcar", version=1, confirmar=True)
    rev("queda en modo `marcada`, con su suma", b.tabla[1]["modo"] == "marcada" and b.tabla[1]["suma"] == M1.suma)
    rev("y NO ejecutó ningún lote", b.ejecutados == [])
    rev("el estado dice marcada, y la 2 sigue pendiente", [f.estado for f in estado(b, [M1, M2])] == ["marcada", "pendiente"])
    rev("marcar una que no es la SIGUIENTE: se niega", niega(lambda: ejecutar(b, [M1, M2, M3], accion="marcar", version=3, confirmar=True), "siguiente pendiente"))
    rev("marcar sin --version: se niega", niega(lambda: ejecutar(b, [M1, M2], accion="marcar", confirmar=True), "--version"))
    b = BaseEnMemoria("IA_Nexus_Dev")
    ejecutar(b, [M1], accion="marcar", version=1, confirmar=False)
    rev("marcar sin --confirmar es simulación: no queda nada", b.tabla == {} and b.pendiente == {})
    b = BaseEnMemoria("IA_Nexus_Dev")
    ejecutar(b, [M1, M2], accion="marcar", version=1, confirmar=True)
    ejecutar(b, [M1, M2], accion="aplicar", confirmar=True)
    rev("después de marcar la 1, `aplicar` ejecuta SOLO la 2", b.ejecutados == ["CREATE TABLE c (id int);"], str(b.ejecutados))

    titulo("10 · La cadena dev → staging → prod")
    dev = BaseEnMemoria("IA_Nexus_Dev")
    staging = BaseEnMemoria("IA_Nexus_Staging")
    prod = BaseEnMemoria("IA_Nexus")
    rev("en staging, sin que esté en dev: se niega y dice adónde ir primero", niega(lambda: ejecutar(staging, [M1], accion="aplicar", confirmar=True, anterior=dev), "va primero ahí"))
    rev("y no dejó nada en staging", staging.tabla == {} and staging.ejecutados == [])
    ejecutar(dev, [M1], accion="aplicar", confirmar=True)
    ejecutar(staging, [M1], accion="aplicar", confirmar=True, anterior=dev)
    rev("estando en dev con la misma suma, staging sí", sorted(staging.tabla) == [1])
    rev("la simulación TAMBIÉN respeta la cadena (no deja simular un salto)", niega(lambda: ejecutar(prod, [M1], accion="aplicar", confirmar=False, anterior=BaseEnMemoria("IA_Nexus_Staging")), "va primero ahí"))
    ejecutar(prod, [M1], accion="marcar", version=1, confirmar=True, anterior=staging)
    rev("prod, estando en staging", sorted(prod.tabla) == [1])
    dev2, staging2 = BaseEnMemoria("IA_Nexus_Dev"), BaseEnMemoria("IA_Nexus_Staging")
    ejecutar(dev2, [M1, M2], accion="aplicar", confirmar=True)
    cambiada = migracion(2, "agrega_c", "CREATE TABLE c (id int, otra int);\nGO\n")
    rev("la misma versión con OTRA suma que la probada en dev: se niega", niega(lambda: ejecutar(staging2, [M1, cambiada], accion="aplicar", confirmar=True, anterior=dev2), "OTRA suma"))
    rev("la cadena de las tres bases es dev → staging → prod, y dev es la primera", corredor.CADENA == {"IA_Nexus_Dev": None, "IA_Nexus_Staging": "IA_Nexus_Dev", "IA_Nexus": "IA_Nexus_Staging"})

    titulo("11 · El candado de la base")
    class Ocupada(BaseEnMemoria):
        def preparar(self) -> None:
            raise ErrorMigracion("Otro corredor está trabajando sobre esta base; espera a que termine.")

    rev("si otro corredor tiene el candado, se niega sin tocar nada", niega(lambda: ejecutar(Ocupada("IA_Nexus_Dev"), [M1], accion="aplicar", confirmar=True), "Otro corredor"))

    titulo("12 · La línea de comandos")

    def correr_cli(*argumentos: str) -> tuple[int, str]:
        salida = io.StringIO()
        try:
            with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(salida):
                codigo = corredor.main(list(argumentos))
        except SystemExit as exc:
            codigo = int(exc.code or 0)
        return codigo, salida.getvalue()

    codigo, texto = correr_cli("aplicar")
    rev("sin --base: el analizador de argumentos lo exige (no hay valor por omisión)", codigo == 2 and "--base" in texto)
    codigo, texto = correr_cli("aplicar", "--base", "IA_Otra")
    rev("una base que no es de Nexus: se niega", codigo == 2 and "invalid choice" in texto)
    anterior_env = os.environ.get("NEXUS_DDL_ENV")
    os.environ["NEXUS_DDL_ENV"] = str(Path(tempfile.gettempdir()) / "no-existe-este-archivo.env")
    try:
        codigo, texto = correr_cli("aplicar", "--base", "IA_Nexus", "--confirmar")
        rev("producción con --confirmar pero SIN --produccion: se niega ANTES de buscar credenciales", codigo == 2 and "--produccion" in texto and "credenciales" not in texto, texto)
        codigo, texto = correr_cli("estado", "--base", "IA_Nexus_Dev")
        rev("sin archivo de credenciales: lo dice y dice dónde buscó", codigo == 2 and "no-existe-este-archivo.env" in texto, texto)
        ajeno = Path(tempfile.mkdtemp(prefix="verificar-corredor-env-")) / "otro.env"
        ajeno.write_text("SQLSERVER_HOST=h\nSQLSERVER_USER=usrNexus\nSQLSERVER_PASSWORD=x\n", encoding="utf-8")
        os.environ["NEXUS_DDL_ENV"] = str(ajeno)
        codigo, texto = correr_cli("estado", "--base", "IA_Nexus_Dev")
        rev("credenciales de OTRO usuario (usrNexus): se niega, el corredor solo corre con usrNexusDDL", codigo == 2 and "usrNexusDDL" in texto, texto)
        ajeno.write_text("SQLSERVER_HOST=h\nSQLSERVER_USER=usrNexusDDL\n", encoding="utf-8")
        codigo, texto = correr_cli("estado", "--base", "IA_Nexus_Dev")
        rev("credenciales incompletas: dice cuáles faltan", codigo == 2 and "SQLSERVER_PASSWORD" in texto, texto)
        shutil.rmtree(ajeno.parent, ignore_errors=True)
    finally:
        if anterior_env is None:
            os.environ.pop("NEXUS_DDL_ENV", None)
        else:
            os.environ["NEXUS_DDL_ENV"] = anterior_env

    titulo("12b · Dentro del deploy: `--esperar-env` solo deja migrar la base que ESE back usa")
    anterior_env = os.environ.get("NEXUS_DDL_ENV")
    os.environ["NEXUS_DDL_ENV"] = str(Path(tempfile.gettempdir()) / "no-existe-este-archivo.env")
    carpeta_back = Path(tempfile.mkdtemp(prefix="verificar-corredor-back-"))
    try:
        del_back = carpeta_back / ".env"
        del_back.write_text("SQLSERVER_HOST=h\nSQLSERVER_DB=IA_Nexus_Staging\n", encoding="utf-8")
        codigo, texto = correr_cli("aplicar", "--base", "IA_Nexus_Dev", "--confirmar", "--esperar-env", str(del_back))
        rev("el .env del back apunta a staging y el deploy pide migrar dev: se niega, y dice a cuál apunta", codigo == 2 and "IA_Nexus_Staging" in texto and "ese back usa" in texto.lower(), texto)
        rev("y se niega ANTES de buscar credenciales (no llegó a tocar nada)", "credenciales" not in texto, texto)
        codigo, texto = correr_cli("aplicar", "--base", "IA_Nexus_Staging", "--confirmar", "--esperar-env", str(del_back))
        rev("si coincide, la guarda deja pasar (y entonces sí busca las credenciales, que aquí no existen a propósito)", codigo == 2 and "credenciales" in texto, texto)
        codigo, texto = correr_cli("marcar", "--base", "IA_Nexus_Dev", "--version", "1", "--confirmar", "--esperar-env", str(del_back))
        rev("la guarda también protege a `marcar`", codigo == 2 and "ese back usa" in texto.lower(), texto)
        codigo, texto = correr_cli("aplicar", "--base", "IA_Nexus_Dev", "--esperar-env", str(carpeta_back / "no-hay.env"))
        rev("un .env que no existe: se niega en vez de seguir sin comprobar", codigo == 2 and "No encuentro" in texto, texto)
        del_back.write_text("SQLSERVER_HOST=h\n", encoding="utf-8")
        codigo, texto = correr_cli("aplicar", "--base", "IA_Nexus_Dev", "--esperar-env", str(del_back))
        rev("un .env sin SQLSERVER_DB: se niega", codigo == 2 and "sin SQLSERVER_DB" in texto, texto)
        codigo, texto = correr_cli("estado", "--base", "IA_Nexus_Dev")
        rev("`estado` no pide la guarda (solo lee)", "esperar" not in texto, texto)
    finally:
        shutil.rmtree(carpeta_back, ignore_errors=True)
        if anterior_env is None:
            os.environ.pop("NEXUS_DDL_ENV", None)
        else:
            os.environ["NEXUS_DDL_ENV"] = anterior_env

    titulo("13 · `nueva` crea el siguiente archivo")
    temporal = Path(tempfile.mkdtemp(prefix="verificar-corredor-nueva-"))
    original = corredor.CARPETA
    try:
        corredor.CARPETA = temporal
        (temporal / "0001_linea_base.sql").write_text("x", encoding="utf-8")
        codigo, texto = correr_cli("nueva", "Agrega la tabla de Tipos Documentales (piloto)")
        creados = sorted(p.name for p in temporal.glob("*.sql"))
        rev("crea 0002 con un nombre limpio (minúsculas, sin acentos ni signos)", codigo == 0 and creados == ["0001_linea_base.sql", "0002_agrega_la_tabla_de_tipos_documentales_piloto.sql"], str(creados) + texto)
        rev("trae un encabezado que dice que no se edita una vez aplicada", "NO se edita" in (temporal / creados[1]).read_text(encoding="utf-8"))
        rev("y la carpeta sigue siendo válida para el corredor", len(descubrir(temporal)) == 2)
    finally:
        corredor.CARPETA = original
        shutil.rmtree(temporal, ignore_errors=True)
finally:
    pass

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
