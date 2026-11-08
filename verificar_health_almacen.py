"""¿`GET /health` avisa cuando el almacén (el NAS) no está montado?

    .venv/Scripts/python verificar_health_almacen.py

OFFLINE: el almacén es una carpeta temporal. Lo que se vigila: con el centinela `.nexus-almacen` en su sitio `status` es `ok` y
`almacen_disponible` es True; si el centinela desaparece (el montaje del NAS se cayó) `status` pasa a `almacen_no_disponible`; con la
ruta sin configurar también; y el código HTTP se queda en 200 siempre, porque Nexus Monitor lee `environment` de esta respuesta y
descarta lo que no sea 2xx. Antes (hasta el 2026-10-28) `/health` decía `ok` con el NAS caído: el 21-oct el back dio 503 a todo
mientras este endpoint seguía en verde.
"""

import os
import shutil
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-health-almacen-"))
os.environ["ALMACEN_RUTA"] = str(RAIZ)

from fastapi.testclient import TestClient  # noqa: E402

import servicios.almacen as almacen  # noqa: E402
from app import app  # noqa: E402

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + extra if extra and not ok else ''}")
    if not ok:
        fallos += 1


c = TestClient(app)
centinela = RAIZ / almacen.CENTINELA
original = almacen.ALMACEN_RUTA

try:
    print("\n=== Montado ===")
    centinela.touch()
    r = c.get("/health")
    rev("200", r.status_code == 200)
    rev("status ok", r.json()["status"] == "ok", str(r.json()))
    rev("almacen_disponible True", r.json()["almacen_disponible"] is True)
    rev("sigue diciendo de qué ambiente habla (lo que lee Nexus Monitor)", "environment" in r.json())

    print("\n=== Montaje caído (el centinela no está) ===")
    centinela.unlink()
    r = c.get("/health")
    rev("sigue siendo 200 (Nexus Monitor descarta lo que no sea 2xx)", r.status_code == 200, str(r.status_code))
    rev("status deja de ser ok", r.json()["status"] == "almacen_no_disponible", str(r.json()))
    rev("almacen_disponible False", r.json()["almacen_disponible"] is False)
    rev("sigue diciendo de qué ambiente habla", "environment" in r.json())

    print("\n=== Vuelve el montaje ===")
    centinela.touch()
    rev("status vuelve a ok sin reiniciar nada", c.get("/health").json()["status"] == "ok")

    print("\n=== Ruta sin configurar ===")
    almacen.ALMACEN_RUTA = ""
    r = c.get("/health")
    rev("sigue siendo 200", r.status_code == 200)
    rev("status almacen_no_disponible", r.json()["status"] == "almacen_no_disponible", str(r.json()))

    print("\n=== Lo demás no cambió ===")
    almacen.ALMACEN_RUTA = original
    cuerpo = c.get("/health").json()
    rev("trae documentos_disponible y avisos_configuracion como antes", {"documentos_disponible", "avisos_configuracion"} <= set(cuerpo))
    rev("no filtra la ruta del almacén", str(RAIZ) not in str(cuerpo))
finally:
    almacen.ALMACEN_RUTA = original
    shutil.rmtree(RAIZ, ignore_errors=True)

print("\nTodo bien." if not fallos else f"\nFALLARON {fallos}.")
raise SystemExit(1 if fallos else 0)
