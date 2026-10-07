"""¿Funciona el alta de organizaciones (HU02) de punta a punta?

    .venv/Scripts/python verificar_organizaciones.py

OFFLINE y sin tocar nada tuyo, como `verificar_auth.py`.

Lo que importa: que SOLO un administrador de plataforma pueda entrar (un
usuario normal con sesión válida no); que al crear una organización nazca
también su administrador con contraseña temporal, devuelta UNA vez; que el
correo repetido se rechace con el texto del diseño y NO deje la organización a
medias; que el nombre repetido no se bloquee sino que reciba un "ID asignado"
distinto (`-1`), que es lo que anuncia el aviso de coincidencias; y que el
administrador nuevo pueda entrar y le toque cambiar su contraseña.
"""

import os
import shutil
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-orgs-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import auth, tenants_registro, usuarios  # noqa: E402

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + extra if extra and not ok else ''}")
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


def srv() -> dict:
    return {"x-api-key": SERVICIO}


c = TestClient(app)
try:
    titulo("0 · Quién puede")
    temporal_admin = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal_admin))
    rev("sin llave de servicio: 401", c.get("/organizaciones/").status_code == 401)
    rev("con llave pero sin sesión: 401", c.get("/organizaciones/", headers=srv()).status_code == 401)
    r = c.post("/auth/login", json={"email": "super@ejemplo.com", "password": temporal_admin}, headers=srv())
    token_super = r.json()["accessToken"]
    bearer = {**srv(), "Authorization": f"Bearer {token_super}"}
    rev("el super admin sí entra", c.get("/organizaciones/", headers=bearer).status_code == 200)

    titulo("1 · Crear la primera organización")
    cuerpo = {
        "nombre": "Seguros Monterrey",
        "adminNombre": "Benjamin",
        "adminApellidos": "Leon Galvez",
        "adminTelefono": "+52 55 1020 3040",
        "adminEmail": "benjamin.lg@ejemplo.com",
        "recuperacion": {"telefono": "+52 55 1478 8523", "email": "respaldo@ejemplo.com"},
    }
    r = c.post("/organizaciones/", json=cuerpo, headers=bearer)
    rev("201 con la organización, su admin y la contraseña temporal",
        r.status_code == 201 and {"organizacion", "admin", "contrasenaTemporal"} <= set(r.json()), r.text[:200])
    primera = r.json()
    rev("el slug sale del nombre", primera["organizacion"]["slug"] == "seguros-monterrey", primera["organizacion"]["slug"])
    rev("y trae su código", primera["organizacion"]["codigo"] == "NEX-00001", primera["organizacion"]["codigo"])
    rev("la tarjeta trae los datos del administrador",
        primera["organizacion"]["admin"]["email"] == "benjamin.lg@ejemplo.com"
        and primera["organizacion"]["admin"]["telefono"] == "+52 55 1020 3040"
        and primera["organizacion"]["admin"]["nombre"] == "Benjamin Leon Galvez", str(primera["organizacion"]["admin"]))
    rev("y los datos de recuperación", primera["organizacion"]["recuperacion"]["telefono"] == "+52 55 1478 8523")
    rev("no-store en la respuesta (trae la contraseña)", r.headers.get("cache-control") == "no-store")
    registro_txt = (RAIZ / ".registro" / "usuarios.jsonl").read_text(encoding="utf-8")
    rev("la contraseña temporal NO queda escrita", primera["contrasenaTemporal"] not in registro_txt)

    titulo("2 · Coincidencias de nombre")
    r = c.get("/organizaciones/coincidencias", params={"nombre": "Seguros Monterrey"}, headers=bearer)
    datos = r.json()
    rev("avisa la coincidencia con su ID encontrado",
        len(datos["coincidencias"]) == 1 and datos["coincidencias"][0]["slug"] == "seguros-monterrey", r.text[:200])
    rev("y propone el ID asignado", datos["slugPropuesto"] == "seguros-monterrey-1", datos["slugPropuesto"])
    r = c.get("/organizaciones/coincidencias", params={"nombre": "Nova seguros"}, headers=bearer)
    rev("un nombre nuevo no tiene coincidencias", r.json()["coincidencias"] == [] and r.json()["slugPropuesto"] == "nova-seguros")
    rev("compara sin acentos ni mayúsculas",
        len(c.get("/organizaciones/coincidencias", params={"nombre": "  SEGUROS   MONTERREY "}, headers=bearer).json()["coincidencias"]) == 1)

    titulo("3 · Segunda con el mismo nombre: se permite, con otro ID")
    r = c.post("/organizaciones/", json={**cuerpo, "adminEmail": "otro@ejemplo.com"}, headers=bearer)
    segunda = r.json()
    rev("201 y el slug lleva -1", r.status_code == 201 and segunda["organizacion"]["slug"] == "seguros-monterrey-1", r.text[:200])
    rev("el nombre se conserva tal cual", segunda["organizacion"]["nombre"] == "Seguros Monterrey")
    rev("y el código avanza", segunda["organizacion"]["codigo"] == "NEX-00002", segunda["organizacion"]["codigo"])

    titulo("4 · Correo repetido")
    antes = len(c.get("/organizaciones/", headers=bearer).json()["organizaciones"])
    r = c.post("/organizaciones/", json={**cuerpo, "nombre": "Zurich seguros"}, headers=bearer)
    rev("409 con el texto del diseño",
        r.status_code == 409 and r.json()["detail"]["codigo"] == "correo_registrado"
        and "ya se encuentra registrado" in r.json()["detail"]["mensaje"], r.text[:200])
    despues = c.get("/organizaciones/", headers=bearer).json()["organizaciones"]
    rev("y NO se creó la organización a medias", len(despues) == antes, f"{antes} -> {len(despues)}")
    rev("correo inválido: 400", c.post("/organizaciones/", json={**cuerpo, "nombre": "X", "adminEmail": "no-es"}, headers=bearer).status_code == 400)
    rev("nombre vacío: 400", c.post("/organizaciones/", json={**cuerpo, "nombre": "   ", "adminEmail": "z@ejemplo.com"}, headers=bearer).status_code == 400)

    titulo("5 · Listado")
    lista = c.get("/organizaciones/", headers=bearer).json()["organizaciones"]
    rev("están las dos, de la más nueva a la más vieja", len(lista) == 2 and lista[0]["slug"] == "seguros-monterrey-1", str([t["slug"] for t in lista]))
    r = c.get(f"/organizaciones/{primera['organizacion']['guid']}/usuarios", headers=bearer)
    rev("sus usuarios: por ahora solo su administrador",
        r.status_code == 200 and [u["email"] for u in r.json()["usuarios"]] == ["benjamin.lg@ejemplo.com"], r.text[:200])
    rev("una organización que no existe: 404", c.get("/organizaciones/no-existe/usuarios", headers=bearer).status_code == 404)

    titulo("6 · El administrador nuevo entra, pero no puede crear organizaciones")
    r = c.post("/auth/login", json={"email": "benjamin.lg@ejemplo.com", "password": primera["contrasenaTemporal"]}, headers=srv())
    rev("entra con su contraseña temporal", r.status_code == 200, r.text[:200])
    rev("y le toca cambiarla", r.json()["usuario"]["debeCambiarContrasena"] is True)
    rev("su membresía dice de qué organización es",
        r.json()["usuario"]["membresias"] == [{"tenantGuid": primera["organizacion"]["guid"], "rol": "ADMIN"}], str(r.json()["usuario"]["membresias"]))
    suyo = {**srv(), "Authorization": f"Bearer {r.json()['accessToken']}"}
    r = c.get("/organizaciones/", headers=suyo)
    rev("no puede listar organizaciones: 403 sin_permiso",
        r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso", r.text[:200])
    rev("ni crearlas", c.post("/organizaciones/", json={**cuerpo, "nombre": "Nova", "adminEmail": "n@ejemplo.com"}, headers=suyo).status_code == 403)

    titulo("7 · Un super admin desactivado deja de poder")
    usuarios.cambiar_estado(usuarios.para_login("super@ejemplo.com")["guid"], False)
    rev("403 desactivada", c.get("/organizaciones/", headers=bearer).status_code == 403)
finally:
    c.close()
    shutil.rmtree(RAIZ, ignore_errors=True)

print("\n" + "=" * 72)
print(f"{fallos} FALLARON" if fallos else "todas las comprobaciones pasaron")
raise SystemExit(1 if fallos else 0)
