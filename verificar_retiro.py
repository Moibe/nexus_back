"""¿Funciona retirar una cuenta (borrado lógico, `retirar_cuenta.py`)?

    .venv/Scripts/python verificar_retiro.py

OFFLINE y sin tocar nada tuyo, como los demás `verificar_*`. Lo que importa: que la cuenta deje de existir para
TODO (login, listados, JWT, refresh, recuperación) y su correo quede libre; que no se pueda dejar a la plataforma
sin administrador ni a una organización sin el suyo por accidente; que sin `--confirmar` no se toque nada; y que
el rastro (el registro de solo agregar y la bitácora) se conserve.
"""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-retiro-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ["CORREO_MODO"] = "archivo"
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import auth, tokens, usuarios  # noqa: E402

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


def entrar(c: TestClient, email: str, password: str):
    return c.post("/auth/login", json={"email": email, "password": password}, headers=srv())


def eventos_de_auditoria(accion: str) -> list[dict]:
    ruta = RAIZ / ".registro" / "auditoria.jsonl"
    if not ruta.exists():
        return []
    lineas = (json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip())
    return [e for e in lineas if e.get("accion") == accion]


c = TestClient(app)
try:
    titulo("0 · Preparar: dos superadmins, una organización con su administrador y dos usuarios")
    pwd_a, pwd_b = auth.contrasena_temporal(), auth.contrasena_temporal()
    usuarios.bootstrap_admin("super.a@ejemplo.com", "Ada", "Lovelace", auth.hash_de(pwd_a))
    usuarios.bootstrap_admin("super.b@ejemplo.com", "Beto", "Babbage", auth.hash_de(pwd_b))
    sa = entrar(c, "super.a@ejemplo.com", pwd_a).json()
    bearer_a = {**srv(), "Authorization": f"Bearer {sa['accessToken']}"}
    r = c.post("/organizaciones/", json={"nombre": "Retiro SA", "adminNombre": "Olga", "adminApellidos": "Org", "adminEmail": "olga@ejemplo.com"}, headers=bearer_a)
    pwd_olga = r.json()["contrasenaTemporal"]
    org_guid, olga_guid = r.json()["organizacion"]["guid"], r.json()["admin"]["guid"]
    # Olga hace su primer acceso para poder administrar.
    o = entrar(c, "olga@ejemplo.com", pwd_olga).json()
    c.post("/auth/contrasena", json={"nueva": "Rumbo-Definitiva-123!", "confirmacion": "Rumbo-Definitiva-123!"}, headers={**srv(), "Authorization": f"Bearer {o['accessToken']}"})
    o = entrar(c, "olga@ejemplo.com", "Rumbo-Definitiva-123!").json()
    bearer_o = {**srv(), "Authorization": f"Bearer {o['accessToken']}"}
    r = c.post("/usuarios/", json={"nombre": "Camila", "email": "camila@ejemplo.com", "rol": "ANALISTA"}, headers=bearer_o)
    pwd_camila, camila_guid = r.json()["contrasenaTemporal"], r.json()["usuario"]["guid"]
    r = c.post("/usuarios/", json={"nombre": "Diego", "email": "diego@ejemplo.com", "rol": "VIEWER"}, headers=bearer_o)
    pwd_diego, diego_guid = r.json()["contrasenaTemporal"], r.json()["usuario"]["guid"]
    cam = entrar(c, "camila@ejemplo.com", pwd_camila).json()
    bearer_cam = {**srv(), "Authorization": f"Bearer {cam['accessToken']}"}
    rev("(montaje) Camila tiene una sesión abierta y sale en el listado de su organización",
        len([u for u in c.get("/usuarios/", headers=bearer_o).json()["usuarios"] if u["email"] == "camila@ejemplo.com"]) == 1)

    titulo("1 · Retirar a un usuario: deja de existir para TODO")
    resumen = usuarios.eliminar_usuario(camila_guid, "Cuenta de prueba", por="CLI")
    rev("devuelve cómo era la cuenta, sin su hash", resumen["email"] == "camila@ejemplo.com" and "hash" not in resumen)
    rev("ya no se encuentra por correo (es lo que usa el login)", usuarios.para_login("camila@ejemplo.com") is None)
    try:
        usuarios.por_guid(camila_guid)
        rev("por_guid: NoEncontrado", False)
    except usuarios.NoEncontrado:
        rev("por_guid: NoEncontrado", True)
    rev("no sale en el listado de su organización", all(u["email"] != "camila@ejemplo.com" for u in c.get("/usuarios/", headers=bearer_o).json()["usuarios"]))
    rev("ni en el que ve el administrador de plataforma",
        all(u["email"] != "camila@ejemplo.com" for u in c.get(f"/organizaciones/{org_guid}/usuarios", headers=bearer_a).json()["usuarios"]))
    r = entrar(c, "camila@ejemplo.com", pwd_camila)
    rev("entrar con su contraseña: 401 correo_no_existe", r.status_code == 401 and r.json()["detail"]["codigo"] == "correo_no_existe", r.text[:160])
    r = c.get("/auth/sesion", headers={"Authorization": f"Bearer {cam['accessToken']}", **srv()})
    rev("su JWT, que todavía no vence, ya no sirve: el estado de su sesión es inexistente", r.status_code == 200 and r.json()["estado"] == "inexistente", r.text[:160])
    r = c.get("/auth/yo", headers=bearer_cam)
    rev("y /auth/yo lo rechaza (401)", r.status_code == 401, r.text[:120])
    rev("su refresh ya no se puede canjear", c.post("/auth/refresh", json={"refreshToken": cam["refreshToken"]}, headers=srv()).status_code == 401)
    rev("su alta y todo lo demás siguen en el registro (solo agregar)", "camila@ejemplo.com" in (RAIZ / ".registro" / "usuarios.jsonl").read_text(encoding="utf-8"))

    titulo("2 · Su correo queda libre")
    r = c.post("/usuarios/", json={"nombre": "Camila Dos", "email": "camila@ejemplo.com", "rol": "SUPERVISOR"}, headers=bearer_o)
    rev("se puede dar de alta otra vez: 201 y es OTRA cuenta (otro guid)", r.status_code == 201 and r.json()["usuario"]["guid"] != camila_guid, r.text[:160])
    nueva_pwd = r.json()["contrasenaTemporal"]
    rev("la cuenta nueva entra con SU contraseña y no tiene nada de la anterior",
        entrar(c, "camila@ejemplo.com", nueva_pwd).status_code == 200
        and usuarios.para_login("camila@ejemplo.com")["nombre"] == "Camila" and usuarios.para_login("camila@ejemplo.com")["apellidos"] == "Dos")
    rev("la contraseña de la cuenta retirada no sirve con la nueva", entrar(c, "camila@ejemplo.com", pwd_camila).status_code == 401)
    try:
        usuarios.eliminar_usuario("no-existe", "x")
        rev("retirar una cuenta que no existe: NoEncontrado", False)
    except usuarios.NoEncontrado:
        rev("retirar una cuenta que no existe: NoEncontrado", True)
    try:
        usuarios.eliminar_usuario(diego_guid, "   ")
        rev("sin motivo se niega (ValueError)", False)
    except ValueError:
        rev("sin motivo se niega (ValueError)", True)
    rev("y con la cuenta intacta", usuarios.para_login("diego@ejemplo.com") is not None)

    titulo("3 · Lo que NO se puede retirar por accidente")
    try:
        usuarios.eliminar_usuario(olga_guid, "prueba")
        rev("al administrador de una organización: se niega", False)
    except usuarios.NoSePuedeEliminar as exc:
        rev("al administrador de una organización: se niega (admin_de_organizacion)", exc.motivo == "admin_de_organizacion")
    rev("y no se tocó: sigue entrando", entrar(c, "olga@ejemplo.com", "Rumbo-Definitiva-123!").status_code == 200)
    guid_a = usuarios.para_login("super.a@ejemplo.com")["guid"]
    guid_b = usuarios.para_login("super.b@ejemplo.com")["guid"]
    usuarios.cambiar_estado(guid_b, False, "prueba")
    try:
        usuarios.eliminar_usuario(guid_a, "prueba")
        rev("al ÚLTIMO superadmin activo (el otro está desactivado): se niega", False)
    except usuarios.NoSePuedeEliminar as exc:
        rev("al ÚLTIMO superadmin activo (el otro está desactivado): se niega (ultimo_superadmin)", exc.motivo == "ultimo_superadmin")
    usuarios.cambiar_estado(guid_b, True)
    usuarios.eliminar_usuario(guid_b, "ya hay otro superadmin activo")
    rev("con otro superadmin activo, sí se puede retirar a uno", usuarios.para_login("super.b@ejemplo.com") is None)
    try:
        usuarios.eliminar_usuario(guid_a, "prueba")
        rev("y entonces el que queda ya es el último: se niega", False)
    except usuarios.NoSePuedeEliminar:
        rev("y entonces el que queda ya es el último: se niega", True)
    # Con --forzar se puede retirar al administrador de una organización, y la organización lo tolera.
    usuarios.eliminar_usuario(olga_guid, "prueba forzada", forzar=True)
    r = c.get("/organizaciones/", headers=bearer_a)
    org = next((x for x in r.json()["organizaciones"] if x["guid"] == org_guid), None)
    rev("con forzar se retira al administrador, y el listado de organizaciones lo tolera (sin administrador, no truena)",
        r.status_code == 200 and org is not None and org["admin"] is None, r.text[:200])

    titulo("4 · El script: sin --confirmar solo simula; con --confirmar lo hace, apaga sus enlaces y lo deja en la bitácora")
    import retirar_cuenta

    def correr(*argumentos: str) -> tuple[int, str]:
        previos = sys.argv
        sys.argv = ["retirar_cuenta.py", *argumentos]
        salida, errores = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(errores):
                rc = retirar_cuenta.main()
        finally:
            sys.argv = previos
        return rc, salida.getvalue() + errores.getvalue()

    tokens.crear(diego_guid, tokens.RECUPERACION)
    rc, out = correr("--email", "diego@ejemplo.com", "--email", "nadie@ejemplo.com", "--motivo", "Cuentas de prueba")
    rev("sin --confirmar: SIMULACIÓN, describe la cuenta y NO la toca",
        "SIMULACIÓN" in out and "se retiraría" in out and "VIEWER de Retiro SA" in out and usuarios.para_login("diego@ejemplo.com") is not None, out[:300])
    rev("y avisa de la que no existe (código 2)", "nadie@ejemplo.com: NO EXISTE" in out and rc == 2)
    rev("ni siquiera deja evento de auditoría", not eventos_de_auditoria("USER_DELETED"))
    rc, out = correr("--email", "camila@ejemplo.com", "--email", "nadie@ejemplo.com", "--motivo", "Cuentas de prueba", "--confirmar")
    rev("con --confirmar: retira a la cuenta que existe (la nueva Camila) y avisa de la que no", "camila@ejemplo.com: RETIRADA" in out and usuarios.para_login("camila@ejemplo.com") is None and rc == 2, out[:300])
    rc, out = correr("--email", "diego@ejemplo.com", "--motivo", "Cuenta de prueba", "--confirmar")
    rev("retira a Diego: dice que su correo ya puede darse de alta otra vez (código 0)", rc == 0 and "ya puede darse de alta otra vez" in out and usuarios.para_login("diego@ejemplo.com") is None, out[:300])
    rev("sus enlaces de recuperación pendientes se apagaron", tokens.pendientes_de(diego_guid) == [])
    auditoria_diego = [e for e in eventos_de_auditoria("USER_DELETED") if e.get("usuarioGuid") == diego_guid]
    rev("quedó USER_DELETED con el correo, el motivo y quién lo hizo, sin contraseña",
        len(auditoria_diego) == 1 and auditoria_diego[0]["email"] == "diego@ejemplo.com" and auditoria_diego[0]["motivo"] == "Cuenta de prueba"
        and auditoria_diego[0]["actor"] == "CLI" and pwd_diego not in (RAIZ / ".registro" / "auditoria.jsonl").read_text(encoding="utf-8"), str(auditoria_diego))
    r = c.post("/organizaciones/", json={"nombre": "Otra SA", "adminNombre": "Pedro", "adminApellidos": "P", "adminEmail": "pedro@ejemplo.com"}, headers=bearer_a)
    rc, out = correr("--email", "pedro@ejemplo.com", "--motivo", "prueba", "--confirmar")
    rev("el script se niega con el administrador de una organización y sugiere --forzar",
        rc == 2 and "NO se retiró" in out and "--forzar" in out and usuarios.para_login("pedro@ejemplo.com") is not None, out[:300])
    rc, out = correr("--email", "pedro@ejemplo.com", "--motivo", "prueba", "--confirmar", "--forzar")
    rev("con --forzar lo retira", rc == 0 and "RETIRADA" in out and usuarios.para_login("pedro@ejemplo.com") is None, out[:300])
    rc, out = correr("--email", "super.a@ejemplo.com", "--motivo", "prueba")
    rev("la simulación también avisa que se NEGARÍA con el último superadmin (código 2)", rc == 2 and "SE NEGARÍA" in out, out[:200])
    rc, out = correr("--email", "super.a@ejemplo.com", "--motivo", "prueba", "--confirmar")
    rev("al último superadmin el script se niega y lo dice (código 2)", rc == 2 and "NO se retiró" in out and usuarios.para_login("super.a@ejemplo.com") is not None, out[:300])
    rc, out = correr("--email", "diego@ejemplo.com", "--motivo", "   ", "--confirmar")
    rev("sin motivo se niega", rc == 2 and "motivo es obligatorio" in out)
    rc, out = correr("--email", "diego@ejemplo.com", "--motivo", "otra vez", "--confirmar")
    rev("retirar dos veces la misma: la segunda dice que no existe", "NO EXISTE" in out and rc == 2)

finally:
    c.close()
    shutil.rmtree(RAIZ, ignore_errors=True)

print("\n" + "=" * 72)
print(f"{fallos} FALLARON" if fallos else "todas las comprobaciones pasaron")
raise SystemExit(1 if fallos else 0)
