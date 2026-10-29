"""¿`GET /health/db` avisa cuando a la base le faltan migraciones?

    .venv/Scripts/python verificar_health_esquema.py

OFFLINE: sustituye la conexión por dobles. Lo que se vigila: `status` es `ok` solo si el esquema de la base está AL DÍA con el código (la última migración aplicada es
al menos la que el código espera); si le faltan migraciones —o ni siquiera existe la tabla de control— es `esquema_desfasado`, con una pista de cómo arreglarlo; que
la base vaya ADELANTE del código es normal (se migra antes de desplegar) y no es un aviso; y los estados de antes (`sin_configurar`, `error`) no cambian.
La prueba contra una base de verdad vive en `verificar_migraciones_sql.py`.
"""

from fastapi.testclient import TestClient

import db.sqlserver as dbs
import migraciones.corredor as corredor
from app import app
from errores import ConfiguracionIncompleta

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + extra if extra and not ok else ''}")
    if not ok:
        fallos += 1


c = TestClient(app)
original = (dbs.probar_conexion, dbs.version_de_esquema, corredor.descubrir)


def con(aplicado, esperado: int, conexion=None):
    dbs.probar_conexion = conexion or (lambda: {"version": "SQL Server de mentira", "base": "IA_Nexus_Dev"})
    dbs.version_de_esquema = lambda: aplicado
    corredor.descubrir = lambda carpeta=None: [object()] * esperado
    return c.get("/health/db").json()


try:
    print("\n=== El esquema de la base contra el código ===")
    r = con(1, 1)
    rev("al día (aplicado 1, esperado 1): ok, y dice las dos cifras", r["status"] == "ok" and r["esquema"] == {"aplicado": 1, "esperado": 1, "al_dia": True} and r["base"] == "IA_Nexus_Dev", str(r))
    rev("al día no trae pista", "pista" not in r)
    r = con(3, 1)
    rev("la base va ADELANTE del código (3 contra 1): sigue ok (se migra antes de desplegar)", r["status"] == "ok" and r["esquema"]["al_dia"] is True, str(r))
    r = con(1, 2)
    rev("a la base le falta una (1 contra 2): esquema_desfasado", r["status"] == "esquema_desfasado" and r["esquema"] == {"aplicado": 1, "esperado": 2, "al_dia": False}, str(r))
    rev("y la pista dice qué tiene, qué espera y cómo se arregla", "tiene 1" in r["pista"] and "la 2" in r["pista"] and "python -m migraciones aplicar" in r["pista"], r.get("pista", ""))
    r = con(None, 1)
    rev("sin tabla de control (nunca se anotó nada): esquema_desfasado, y la pista dice «ninguna anotada»", r["status"] == "esquema_desfasado" and r["esquema"]["aplicado"] is None and "ninguna anotada" in r["pista"], str(r))
    rev("siguen viniendo la versión y la base conectada", "version" in r and r["base"] == "IA_Nexus_Dev")

    print("\n=== Los estados de siempre no cambian ===")
    def sin_configurar():
        raise ConfiguracionIncompleta("Faltan estas variables en el .env: SQLSERVER_HOST.")

    r = con(1, 1, conexion=sin_configurar)
    rev("sin configurar: sigue diciendo qué falta (y no toca el esquema)", r["status"] == "sin_configurar" and "SQLSERVER_HOST" in r["detalle"] and "esquema" not in r, str(r))

    class ErrorDeSql(Exception):
        pass

    def revienta():
        raise ErrorDeSql("HYT00", "texto con host y usuario que NO debe salir")

    r = con(1, 1, conexion=revienta)
    rev("un error de SQL: status error, con el tipo, sin el texto del driver", r["status"] == "error" and r["error"] == "ErrorDeSql" and "NO debe salir" not in str(r), str(r))
finally:
    dbs.probar_conexion, dbs.version_de_esquema, corredor.descubrir = original

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
