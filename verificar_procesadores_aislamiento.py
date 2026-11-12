"""¿Un procesador de Document AI solo lo toca quien administra la organización DUEÑA de su tipo documental?

    .venv/Scripts/python verificar_procesadores_aislamiento.py

OFFLINE y sin tocar nada tuyo, como los demás `verificar_*`: Document AI se sustituye por dobles que anotan lo que les llegaría.

Nació de la auditoría de seguridad de código del 2026-10-30 (NEX-550, hallazgo NX-21): `activar`, `consultar` y `eliminar` un procesador
respondían a CUALQUIER petición con la llave de servicio, sin saber quién pedía ni de qué organización era el procesador, y el procesador
lo manda el navegador. Con una cuenta cualquiera se podía borrar el de otra organización (con su dataset y su esquema) o gastar sus
extracciones. Lo que se vigila:

- consultar y borrar piden sesión y rol de quien administra, y que el procesador sea de un tipo de ESA organización: el de otra se
  contesta como si no existiera (404, sin decir de quién es) y deja `CROSS_TENANT_ACCESS`; si ni siquiera lo conoce ningún tipo, igual;
- pedir el tenant de otra organización es 403 `sin_acceso`, y un rol que no administra, 403 `sin_permiso`;
- activar pide la organización en el cuerpo y las mismas dos cosas, y no se activa un tipo cuyo procesador ya es de otra;
- extraer con `tenant` solo usa procesadores de esa organización (aunque no guarde nada).
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-proc-aisl-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
import pruebas_sql  # noqa: E402  (desde el 2026-11-11 todo se guarda en SQL Server: esta prueba corre contra uno desechable)

BASE_SQL = pruebas_sql.preparar()

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from routers import ia as router_ia  # noqa: E402
from routers import procesadores as router  # noqa: E402
from servicios import auth, usuarios  # noqa: E402
from servicios import tipos_documentales as registro_tipos  # noqa: E402

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + extra if extra and not ok else ''}")
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


def cab(bearer: str | None = None, servicio: bool = True) -> dict:
    return {**({"x-api-key": SERVICIO} if servicio else {}), **({"Authorization": f"Bearer {bearer}"} if bearer else {})}


c = TestClient(app)


def entrar(correo: str, contrasena: str) -> dict:
    r = c.post("/auth/login", json={"email": correo, "password": contrasena, "ip": "127.0.0.1", "userAgent": "verificar"}, headers=cab())
    assert r.status_code == 200, r.text
    return r.json()


def con_primer_acceso(correo: str, temporal: str, definitiva: str) -> str:
    s = entrar(correo, temporal)
    r = c.post("/auth/contrasena", json={"nueva": definitiva, "confirmacion": definitiva}, headers=cab(s["accessToken"]))
    assert r.status_code == 200, r.text
    return entrar(correo, definitiva)["accessToken"]


def crear_organizacion(bearer_super: str, nombre: str, correo: str, contrasena: str) -> tuple[str, str]:
    r = c.post("/organizaciones/", json={"nombre": nombre, "adminNombre": "Admin", "adminApellidoPaterno": nombre, "adminEmail": correo}, headers=cab(bearer_super))
    assert r.status_code == 201, r.text
    return r.json()["organizacion"]["guid"], con_primer_acceso(correo, r.json()["contrasenaTemporal"], contrasena)


def auditoria(accion: str) -> list[dict]:
    from servicios import auditoria as bitacora

    return bitacora.eventos({accion})


# Los dobles de Document AI: anotan a qué procesadores se les llamó.
llamadas: list[tuple[str, str]] = []


async def consultar_falso(pid):
    llamadas.append(("consultar", pid))
    return {"procesadorId": pid, "procesadorDisplayName": "nexusdoc--x", "versionDefault": "v1", "estado": "ENABLED"}


async def eliminar_falso(pid):
    llamadas.append(("eliminar", pid))


async def activar_falso(id_tipo, nombre, esquema, version_actual=0):
    llamadas.append(("activar", id_tipo))
    return {"procesadorId": f"proc-de-{id_tipo}", "procesadorNombre": "projects/p/locations/us/processors/x", "procesadorDisplayName": "nexusdoc--x", "versionDefault": "pretrained"}


async def extraer_falso(procesador, contenido, mime, version):
    llamadas.append(("extraer", procesador))
    return {"procesador": procesador, "campos": {}}


router.consultar_procesador = consultar_falso  # type: ignore[assignment]
router.eliminar_procesador = eliminar_falso  # type: ignore[assignment]
router.activar_tipo_documental = activar_falso  # type: ignore[assignment]
router_ia.ia.extraer_con_procesador = extraer_falso  # type: ignore[assignment]

try:
    titulo("0 · Montaje: dos organizaciones, un operador en la primera, y un tipo con procesador en cada una")
    temporal = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal))
    super_ = con_primer_acceso("super@ejemplo.com", temporal, "Cobre-Lento-Azul-47!")
    org_a, a_admin = crear_organizacion(super_, "Alfa", "rita@ejemplo.com", "Rumbo-Sereno-Azul-47!")
    org_b, b_admin = crear_organizacion(super_, "Beta", "beto@ejemplo.com", "Rumbo-Sereno-Rojo-48!")
    r = c.post("/usuarios/", json={"nombre": "Olga", "apellidoPaterno": "Operadora", "email": "olga@ejemplo.com", "rol": "OPERADOR"}, headers=cab(a_admin))
    assert r.status_code == 201, r.text
    a_operador = con_primer_acceso("olga@ejemplo.com", r.json()["contrasenaTemporal"], "Cielo-Abierto-Verde-52!")
    for tenant, tipo, proc in ((org_a, "tipo-aaaaaa", "proc-a"), (org_b, "tipo-bbbbbb", "proc-b")):
        registro_tipos.guardar(tenant, tipo, {"nombre": f"Tipo {tipo}", "estado": "activo", "procesadorId": proc, "campos": []}, 0, "verificar")
    rev("hay dos organizaciones distintas con su administrador y un operador en la primera", org_a != org_b and a_admin and b_admin and a_operador)

    titulo("1 · Consultar un procesador")
    llamadas.clear()
    r = c.get("/procesadores/proc-a", params={"tenant": org_a}, headers=cab(a_admin))
    rev("el administrador consulta el procesador de SU organización: 200", r.status_code == 200 and ("consultar", "proc-a") in llamadas, r.text[:160])
    antes = len(auditoria("CROSS_TENANT_ACCESS"))
    llamadas.clear()
    r = c.get("/procesadores/proc-b", params={"tenant": org_a}, headers=cab(a_admin))
    rev("el procesador de OTRA organización se contesta como si no existiera: 404, sin llamar a Google", r.status_code == 404 and not llamadas, r.text[:160])
    rev("…sin decir de quién es", org_b not in r.text and "proc-b" not in r.text and "tipo-bbbbbb" not in r.text, r.text[:160])
    rev("…y queda CROSS_TENANT_ACCESS en la bitácora", len(auditoria("CROSS_TENANT_ACCESS")) == antes + 1)
    llamadas.clear()
    r = c.get("/procesadores/proc-ninguno", params={"tenant": org_a}, headers=cab(a_admin))
    rev("uno que ningún tipo conoce (como el heredado del .env) también es 404 y no llega a Google", r.status_code == 404 and not llamadas, r.text[:160])
    r = c.get("/procesadores/proc-b", params={"tenant": org_b}, headers=cab(a_admin))
    rev("pedir el tenant de OTRA organización: 403 sin_acceso", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso", r.text[:160])
    r = c.get("/procesadores/proc-a", params={"tenant": org_a}, headers=cab(a_operador))
    rev("un rol que no administra: 403 sin_permiso", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso", r.text[:160])
    r = c.get("/procesadores/proc-a", params={"tenant": org_a}, headers=cab(None))
    rev("sin sesión (solo la llave de servicio): 401", r.status_code == 401, r.text[:160])
    r = c.get("/procesadores/proc-a", headers=cab(a_admin))
    rev("sin decir la organización: 422", r.status_code == 422, r.text[:160])

    titulo("2 · Borrar un procesador")
    llamadas.clear()
    r = c.delete("/procesadores/proc-b", params={"tenant": org_a}, headers=cab(a_admin))
    rev("borrar el de OTRA organización: 404 y NO se borra nada en Google", r.status_code == 404 and not llamadas, r.text[:160])
    r = c.delete("/procesadores/proc-b", params={"tenant": org_b}, headers=cab(a_admin))
    rev("borrarlo diciendo la organización de ella: 403 sin_acceso y nada se borra", r.status_code == 403 and not llamadas, r.text[:160])
    r = c.delete("/procesadores/proc-a", params={"tenant": org_a}, headers=cab(a_operador))
    rev("un rol que no administra no borra: 403 sin_permiso y nada se borra", r.status_code == 403 and not llamadas, r.text[:160])
    r = c.delete("/procesadores/proc-ninguno", params={"tenant": org_a}, headers=cab(a_admin))
    rev("el que ningún tipo conoce no se borra: 404", r.status_code == 404 and not llamadas, r.text[:160])
    r = c.delete("/procesadores/proc-a", params={"tenant": org_a}, headers=cab(a_admin))
    rev("el administrador borra el de SU organización: 200 y sí se borra", r.status_code == 200 and llamadas == [("eliminar", "proc-a")], r.text[:160])

    titulo("3 · Activar un tipo")
    cuerpo = {"tenant": org_a, "id": "tipo-nuevo1", "nombre": "Nuevo", "campos": [{"nombre": "Campo", "tipoDato": "texto"}]}
    llamadas.clear()
    r = c.post("/procesadores/activar", json=cuerpo, headers=cab(a_admin))
    rev("el administrador activa un tipo de SU organización: 200", r.status_code == 200 and ("activar", "tipo-nuevo1") in llamadas, r.text[:160])
    llamadas.clear()
    r = c.post("/procesadores/activar", json={**cuerpo, "tenant": org_b}, headers=cab(a_admin))
    rev("a nombre de OTRA organización: 403 sin_acceso y no se llama a Google", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso" and not llamadas, r.text[:160])
    r = c.post("/procesadores/activar", json=cuerpo, headers=cab(a_operador))
    rev("un rol que no administra: 403 sin_permiso y no se llama a Google", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso" and not llamadas, r.text[:160])
    r = c.post("/procesadores/activar", json=cuerpo, headers=cab(None))
    rev("sin sesión: 401", r.status_code == 401 and not llamadas, r.text[:160])
    sin_tenant = {k: v for k, v in cuerpo.items() if k != "tenant"}
    r = c.post("/procesadores/activar", json=sin_tenant, headers=cab(a_admin))
    rev("sin decir la organización: 422", r.status_code == 422 and not llamadas, r.text[:160])
    r = c.post("/procesadores/activar", json={**cuerpo, "id": "tipo-bbbbbb"}, headers=cab(a_admin))
    rev("activar un tipo cuyo procesador ya es de OTRA organización: 404 y no se llama a Google", r.status_code == 404 and not llamadas, r.text[:160])

    titulo("4 · Extraer con un procesador")
    archivo = {"imagen": ("doc.pdf", b"%PDF-1.4\n%x", "application/pdf")}
    llamadas.clear()
    r = c.post("/ia/extraer", data={"procesador": "proc-a", "tenant": org_a}, files=archivo, headers=cab(a_admin))
    rev("con el procesador de su organización: 200", r.status_code == 200 and ("extraer", "proc-a") in llamadas, r.text[:160])
    llamadas.clear()
    r = c.post("/ia/extraer", data={"procesador": "proc-b", "tenant": org_a}, files=archivo, headers=cab(a_admin))
    rev("con el procesador de OTRA organización: 404 y no se le paga a Google", r.status_code == 404 and not llamadas, r.text[:160])
    r = c.post("/ia/extraer", data={"procesador": "proc-a", "tenant": org_b}, files=archivo, headers=cab(a_admin))
    rev("a nombre de otra organización: 403 sin_acceso y no se le paga a Google", r.status_code == 403 and not llamadas, r.text[:160])
    r = c.post("/ia/extraer", data={"procesador": "proc-a", "tenant": org_a}, files=archivo, headers=cab(None))
    rev("sin sesión pero con tenant: 401", r.status_code == 401 and not llamadas, r.text[:160])
    llamadas.clear()
    r = c.post("/ia/extraer", data={"procesador": "proc-b"}, files=archivo, headers=cab())
    rev("sin tenant (Swagger y las pruebas, solo con la llave de servicio) se conserva lo de siempre: 200", r.status_code == 200 and ("extraer", "proc-b") in llamadas, r.text[:160])
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
