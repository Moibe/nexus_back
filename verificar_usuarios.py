"""¿Funciona la gestión de usuarios de una organización (HU06, HU07)?

    .venv/Scripts/python verificar_usuarios.py

OFFLINE y sin tocar nada tuyo, como los demás `verificar_*`.

Lo que importa: que solo el ADMINISTRADOR DE LA ORGANIZACIÓN pueda (ni el
super admin de plataforma, que no tiene organización, ni un usuario con otro
rol); que cada quien vea y toque SOLO su organización; que el alta devuelva la
contraseña temporal una vez y el usuario nuevo entre con ella y deba
cambiarla; que el rol se pueda cambiar y viaje en su JWT; y que un
administrador no pueda quitarse a sí mismo el rol y quedar sin quien administre.
"""

import json
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-usuarios-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import auth, registro, roles, usuarios  # noqa: E402

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


def entrar(c: TestClient, email: str, password: str) -> dict:
    r = c.post("/auth/login", json={"email": email, "password": password}, headers=srv())
    assert r.status_code == 200, r.text
    return r.json()


c = TestClient(app)
try:
    titulo("0 · Preparar: super admin y dos organizaciones")
    tmp_super = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(tmp_super))
    s = entrar(c, "super@ejemplo.com", tmp_super)
    bearer_super = {**srv(), "Authorization": f"Bearer {s['accessToken']}"}
    orgs = {}
    for nombre, correo in (("Seguros Monterrey", "admin.sm@ejemplo.com"), ("Zurich seguros", "admin.zs@ejemplo.com")):
        r = c.post(
            "/organizaciones/",
            json={"nombre": nombre, "adminNombre": "Admin", "adminApellidos": nombre, "adminEmail": correo},
            headers=bearer_super,
        )
        assert r.status_code == 201, r.text
        orgs[nombre] = r.json()
    sm = entrar(c, "admin.sm@ejemplo.com", orgs["Seguros Monterrey"]["contrasenaTemporal"])
    bearer_sm = {**srv(), "Authorization": f"Bearer {sm['accessToken']}"}
    zs = entrar(c, "admin.zs@ejemplo.com", orgs["Zurich seguros"]["contrasenaTemporal"])
    bearer_zs = {**srv(), "Authorization": f"Bearer {zs['accessToken']}"}
    rev("el JWT del admin lleva su organización y su rol",
        auth.verificar_acceso(sm["accessToken"])["rol"] == "ADMIN"
        and auth.verificar_acceso(sm["accessToken"])["tnt"] == orgs["Seguros Monterrey"]["organizacion"]["guid"])

    titulo("1 · Quién puede")
    rev("sin sesión: 401", c.get("/usuarios/", headers=srv()).status_code == 401)
    r = c.get("/usuarios/", headers=bearer_super)
    rev("el super admin NO: no tiene organización (403 sin_organizacion)",
        r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_organizacion", r.text[:160])
    r = c.get("/usuarios/", headers=bearer_sm)
    rev("el admin de la organización sí", r.status_code == 200, r.text[:160])
    rev("y ve el nombre de su organización", r.json()["organizacion"]["nombre"] == "Seguros Monterrey")
    rev("con él mismo dentro, numerado y como Administrador",
        [(u["numero"], u["rolNombre"]) for u in r.json()["usuarios"]] == [("001", "Administrador")], str(r.json()["usuarios"]))

    titulo("2 · El catálogo de roles es el del diseño")
    r = c.get("/usuarios/roles", headers=bearer_sm)
    nombres = [x["nombre"] for x in r.json()["roles"]]
    rev("seis roles, en el orden del diseño",
        nombres == ["Supervisor", "Analista", "Operador", "Compliance Officer", "Auditor", "Viewer"], str(nombres))
    rev("cada uno con su descripción", all(len(x["descripcion"]) > 40 for x in r.json()["roles"]))

    titulo("3 · Crear usuarios (HU06)")
    r = c.post(
        "/usuarios/",
        json={"nombre": "Camila Sofía", "apellidos": "Cabrera González", "email": "camila.cg@ejemplo.com",
              "telefono": "+52 55 1111 2222", "rol": "ANALISTA"},
        headers=bearer_sm,
    )
    rev("201 con el usuario y su contraseña temporal",
        r.status_code == 201 and {"usuario", "contrasenaTemporal"} <= set(r.json()), r.text[:200])
    camila = r.json()
    rev("numerado 002 y con su rol", camila["usuario"]["numero"] == "002" and camila["usuario"]["rolNombre"] == "Analista")
    rev("no-store (trae la contraseña)", r.headers.get("cache-control") == "no-store")
    rev("la contraseña NO queda escrita",
        camila["contrasenaTemporal"] not in (RAIZ / ".registro" / "usuarios.jsonl").read_text(encoding="utf-8"))
    r = c.post("/usuarios/", json={"nombre": "X", "email": "camila.cg@ejemplo.com", "rol": "VIEWER"}, headers=bearer_sm)
    rev("correo repetido: 409 con el texto del diseño",
        r.status_code == 409 and "ya se encuentra registrado" in r.json()["detail"]["mensaje"], r.text[:160])
    rev("rol inventado: 400", c.post("/usuarios/", json={"nombre": "X", "email": "x@ejemplo.com", "rol": "JEFE"}, headers=bearer_sm).status_code == 400)
    rev("no se puede asignar ADMIN desde el selector",
        c.post("/usuarios/", json={"nombre": "X", "email": "y@ejemplo.com", "rol": "ADMIN"}, headers=bearer_sm).status_code == 400)
    rev("correo inválido: 400", c.post("/usuarios/", json={"nombre": "X", "email": "no-es", "rol": "VIEWER"}, headers=bearer_sm).status_code == 400)

    titulo("4 · El usuario nuevo entra")
    primera = entrar(c, "camila.cg@ejemplo.com", camila["contrasenaTemporal"])
    rev("entra y le toca cambiar la contraseña", primera["usuario"]["debeCambiarContrasena"] is True)
    # Y la cambia: desactivar/reactivar se prueban con una cuenta ya en uso,
    # que es el caso real.
    nueva_contrasena = "Cobre-Segura-2026!"
    c.post("/auth/contrasena", json={"nueva": nueva_contrasena, "confirmacion": nueva_contrasena},
           headers={**srv(), "Authorization": f"Bearer {primera['accessToken']}"})
    nueva = entrar(c, "camila.cg@ejemplo.com", nueva_contrasena)
    claims = auth.verificar_acceso(nueva["accessToken"])
    rev("su JWT dice su organización y su rol", claims["rol"] == "ANALISTA" and claims["tnt"] == orgs["Seguros Monterrey"]["organizacion"]["guid"])
    suyo = {**srv(), "Authorization": f"Bearer {nueva['accessToken']}"}
    r = c.get("/usuarios/", headers=suyo)
    rev("como Analista NO puede gestionar usuarios: 403",
        r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_organizacion", r.text[:160])

    titulo("5 · Cambiar el rol (HU07)")
    guid = camila["usuario"]["guid"]
    r = c.post(f"/usuarios/{guid}",
               json={"nombre": "Camila Sofía", "apellidos": "Cabrera G.", "telefono": "+52 55 3333 4444", "rol": "SUPERVISOR"},
               headers=bearer_sm)
    rev("200 con el rol y los datos nuevos",
        r.status_code == 200 and r.json()["usuario"]["rolNombre"] == "Supervisor"
        and r.json()["usuario"]["nombre"] == "Camila Sofía Cabrera G.", r.text[:200])
    rev("el cambio se ve en el listado",
        [u["rolNombre"] for u in c.get("/usuarios/", headers=bearer_sm).json()["usuarios"]] == ["Administrador", "Supervisor"])
    r = c.post(f"/usuarios/{guid}", json={"nombre": "C", "rol": "NADA"}, headers=bearer_sm)
    rev("rol inventado al editar: 400", r.status_code == 400)
    yo_guid = sm["usuario"]["guid"]
    r = c.post(f"/usuarios/{yo_guid}", json={"nombre": "Admin SM", "rol": "VIEWER"}, headers=bearer_sm)
    rev("un admin no puede degradarse a sí mismo: 409",
        r.status_code == 409 and r.json()["detail"]["codigo"] == "no_puedes_degradarte", r.text[:160])

    titulo("6 · Cada organización ve solo la suya")
    r = c.get("/usuarios/", headers=bearer_zs)
    rev("la otra organización solo se ve a sí misma",
        [u["email"] for u in r.json()["usuarios"]] == ["admin.zs@ejemplo.com"], str(r.json()["usuarios"]))
    r = c.post(f"/usuarios/{guid}", json={"nombre": "Secuestrada", "rol": "VIEWER"}, headers=bearer_zs)
    rev("no puede editar a alguien de otra organización: 404",
        r.status_code == 404 and r.json()["detail"]["codigo"] == "no_existe", r.text[:160])
    rev("un guid inventado: 404", c.post("/usuarios/nadie", json={"nombre": "X", "rol": "VIEWER"}, headers=bearer_sm).status_code == 404)
    titulo("7 · Desactivar y reactivar (HU08, HU09)")
    r = c.post(f"/usuarios/{guid}/estado", json={"activo": False, "motivo": "   "}, headers=bearer_sm)
    rev("sin motivo: 400", r.status_code == 400 and r.json()["detail"]["codigo"] == "motivo_requerido", r.text[:160])
    r = c.post(f"/usuarios/{yo_guid}/estado", json={"activo": False, "motivo": "x"}, headers=bearer_sm)
    rev("no puedes desactivarte a ti mismo: 409", r.status_code == 409 and r.json()["detail"]["codigo"] == "no_a_ti_mismo", r.text[:160])
    r = c.post(f"/usuarios/{guid}/estado", json={"activo": False, "motivo": "Cambios operativos."}, headers=bearer_sm)
    rev("desactivar: 200 y queda inactivo", r.status_code == 200 and r.json()["usuario"]["activo"] is False, r.text[:160])
    rev("no puede entrar: 403 desactivada",
        c.post("/auth/login", json={"email": "camila.cg@ejemplo.com", "password": nueva_contrasena}, headers=srv()).status_code == 403)
    rev("su refresh quedó revocado",
        c.post("/auth/refresh", json={"refreshToken": nueva["refreshToken"]}, headers=srv()).status_code == 401)
    rev("el listado lo muestra inactivo",
        [u["activo"] for u in c.get("/usuarios/", headers=bearer_sm).json()["usuarios"]] == [True, False])
    r = c.post(f"/usuarios/{guid}/estado", json={"activo": False, "motivo": "otra vez"}, headers=bearer_sm)
    rev("desactivar dos veces: 409 sin_cambio", r.status_code == 409 and r.json()["detail"]["codigo"] == "sin_cambio")
    r = c.post(f"/usuarios/{guid}/estado", json={"activo": True, "motivo": "Regresó al equipo."}, headers=bearer_zs)
    rev("otra organización no puede reactivarlo: 404", r.status_code == 404)
    r = c.post(f"/usuarios/{guid}/estado", json={"activo": True, "motivo": "Regresó al equipo."}, headers=bearer_sm)
    rev("reactivar: 200 y vuelve a estar activo", r.status_code == 200 and r.json()["usuario"]["activo"] is True, r.text[:160])
    sesion_camila = entrar(c, "camila.cg@ejemplo.com", nueva_contrasena)
    rev("y vuelve a entrar con su misma contraseña", bool(sesion_camila["accessToken"]))
    rev("el motivo quedó registrado",
        "Cambios operativos." in (RAIZ / ".registro" / "usuarios.jsonl").read_text(encoding="utf-8"))

    titulo("8 · Cerrar la sesión de alguien (HU05)")
    r = c.post(f"/usuarios/{guid}/cerrar-sesion", json={"motivo": ""}, headers=bearer_sm)
    rev("sin motivo: 400", r.status_code == 400)
    r = c.post(f"/usuarios/{yo_guid}/cerrar-sesion", json={"motivo": "x"}, headers=bearer_sm)
    rev("la tuya no, desde aquí: 409", r.status_code == 409 and r.json()["detail"]["codigo"] == "no_a_ti_mismo")
    def con_sesion(correo: str) -> bool:
        return next(u for u in c.get("/usuarios/", headers=bearer_sm).json()["usuarios"] if u["email"] == correo)["sesionActiva"]

    rev("antes de cerrar, el listado la muestra CON sesión abierta", con_sesion("camila.cg@ejemplo.com") is True)
    r = c.post(f"/usuarios/{guid}/cerrar-sesion", json={"motivo": "Medida preventiva de seguridad."}, headers=bearer_sm)
    rev("cierra sus sesiones: 200 y dice cuántas", r.status_code == 200 and r.json()["cerradas"] >= 1, r.text[:160])
    rev("después de cerrar, el listado la muestra SIN sesión, y su estatus sigue activo",
        con_sesion("camila.cg@ejemplo.com") is False
        and next(u for u in c.get("/usuarios/", headers=bearer_sm).json()["usuarios"] if u["email"] == "camila.cg@ejemplo.com")["activo"] is True)
    rev("su refresh ya no sirve",
        c.post("/auth/refresh", json={"refreshToken": sesion_camila["refreshToken"]}, headers=srv()).status_code == 401)
    rev("pero SIGUE activa: puede volver a entrar",
        c.post("/auth/login", json={"email": "camila.cg@ejemplo.com", "password": nueva_contrasena}, headers=srv()).status_code == 200)
    rev("y al volver a entrar el listado la muestra con sesión otra vez", con_sesion("camila.cg@ejemplo.com") is True)
    rev("una sesión vencida no cuenta: con el reloj 400 días adelante ya no tiene sesión",
        usuarios.tiene_sesion(guid, datetime.now(timezone.utc) + timedelta(days=400)) is False and usuarios.tiene_sesion(guid) is True)
    r = c.post("/usuarios/", json={"nombre": "Sin Fecha", "email": "sin.fecha@ejemplo.com", "rol": "VIEWER"}, headers=bearer_sm)
    guid_sin_fecha = r.json()["usuario"]["guid"]
    rev("quien nunca entró no tiene sesión", usuarios.tiene_sesion(guid_sin_fecha) is False)
    registro.agregar(usuarios.ARCHIVO, {"evento": "sesion", "guid": guid_sin_fecha, "sesionGuid": "sesion-sin-fecha", "refreshHash": "h" * 64,
                                        "expira": "esto-no-es-una-fecha", "en": datetime.now(timezone.utc).isoformat(timespec="seconds")})
    rev("una sesión sin fecha de vencimiento legible se toma por vencida (igual que al canjear el refresh)", usuarios.tiene_sesion(guid_sin_fecha) is False)

    titulo("8b · El cierre forzado deja su evento, y no cualquiera puede hacerlo (HU05 CA-4, CA-5)")
    registro_usuarios = RAIZ / ".registro" / "usuarios.jsonl"
    ruta_auditoria = RAIZ / ".registro" / "auditoria.jsonl"
    forzados = [json.loads(x) for x in ruta_auditoria.read_text(encoding="utf-8").splitlines() if '"FORCED_LOGOUT"' in x]
    rev("quedó UN evento FORCED_LOGOUT (los intentos rechazados —sin motivo, a sí mismo— no dejan nada)", len(forzados) == 1, str(len(forzados)))
    f = forzados[0]
    rev("con el usuario afectado, quien lo ordenó (y en qué calidad), el motivo, la organización y la hora",
        f["usuarioGuid"] == guid and f["por"] == yo_guid and f["motivo"] == "Medida preventiva de seguridad."
        and f["actor"] == "ADMIN_ORGANIZACION" and bool(f.get("tenantGuid")) and bool(f["en"]), str(f))
    cierre_forzado = [json.loads(x) for x in registro_usuarios.read_text(encoding="utf-8").splitlines() if '"cierre_forzado"' in x]
    rev("y `cierre_forzado` guarda lo mismo en el registro de usuarios",
        len(cierre_forzado) == 1 and cierre_forzado[0]["guid"] == guid and cierre_forzado[0]["por"] == yo_guid
        and cierre_forzado[0]["motivo"] == "Medida preventiva de seguridad.", str(cierre_forzado))
    bearer_camila = {**srv(), "Authorization": f"Bearer {entrar(c, 'camila.cg@ejemplo.com', nueva_contrasena)['accessToken']}"}
    r = c.post(f"/usuarios/{yo_guid}/cerrar-sesion", json={"motivo": "Intento sin ser administradora."}, headers=bearer_camila)
    rev("un usuario sin rol de administrador no puede: 403", r.status_code == 403, r.text[:140])
    r = c.post(f"/usuarios/{guid}/cerrar-sesion", json={"motivo": "Intento de otra organización."}, headers=bearer_zs)
    rev("el administrador de OTRA organización no puede: 404 (ni se entera de que existe)", r.status_code == 404, r.text[:140])
    r = c.post(f"/usuarios/{guid}/cerrar-sesion", json={"motivo": "Intento del Super Admin."}, headers=bearer_super)
    rev("el Super Admin no usa ESTA ruta (opera sobre 'mi organización' y él no tiene una): 403 sin_organizacion; la suya "
        "cuelga de /organizaciones y se prueba en verificar_organizaciones.py (6c)",
        r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_organizacion", r.text[:140])
    forzados = [x for x in ruta_auditoria.read_text(encoding="utf-8").splitlines() if '"FORCED_LOGOUT"' in x]
    rev("ninguno de los tres intentos rechazados dejó evento", len(forzados) == 1, str(len(forzados)))

    titulo("8c · Al administrador al que se le fuerza el cierre, su JWT ya no le sirve para administrar (HU05 CA-4)")
    # El escenario que la revisión de HU05 midió como el más grave: con el JWT de un
    # administrador cerrado a la fuerza se podía crear un usuario, ascenderlo a
    # administrador y que tuviera una sesión de 7 días que sobrevivía al cierre.
    r = c.post(f"/usuarios/{guid}", json={"nombre": "Camila", "apellidos": "CG", "rol": "ADMIN"}, headers=bearer_sm)
    rev("(montaje) el administrador asciende a Camila a ADMIN, para que pueda forzarle el cierre a él",
        r.status_code == 200, r.text[:140])
    camila_admin = entrar(c, "camila.cg@ejemplo.com", nueva_contrasena)
    bearer_camila_admin = {**srv(), "Authorization": f"Bearer {camila_admin['accessToken']}"}
    rev("(antes) el JWT del administrador original funciona", c.get("/usuarios/", headers=bearer_sm).status_code == 200)
    r = c.post(f"/usuarios/{yo_guid}/cerrar-sesion", json={"motivo": "Cuenta comprometida."}, headers=bearer_camila_admin)
    rev("Camila le fuerza el cierre al administrador original: 200", r.status_code == 200 and r.json()["cerradas"] >= 1, r.text[:140])
    # Con el JWT que el administrador original TODAVÍA TIENE en la mano (no ha vencido):
    r = c.get("/usuarios/", headers=bearer_sm)
    rev("listar usuarios: 401 sesion_cerrada", r.status_code == 401 and r.json()["detail"]["codigo"] == "sesion_cerrada", r.text[:140])
    r = c.post("/usuarios/", json={"nombre": "Puerta", "apellidos": "Trasera", "email": "puerta.trasera@ejemplo.com", "rol": "ANALISTA"}, headers=bearer_sm)
    rev("crear un usuario: 401 (antes de HU05 pasaba)", r.status_code == 401, r.text[:140])
    r = c.post(f"/usuarios/{guid}", json={"nombre": "Camila", "apellidos": "CG", "rol": "SUPERVISOR"}, headers=bearer_sm)
    rev("cambiarle el rol a alguien: 401", r.status_code == 401, r.text[:140])
    rev("y no se coló ningún usuario a escondidas", usuarios.para_login("puerta.trasera@ejemplo.com") is None)
    # No lo desactivaron: puede volver a entrar y administrar.
    de_nuevo = entrar(c, "admin.sm@ejemplo.com", orgs["Seguros Monterrey"]["contrasenaTemporal"])
    rev("pero puede volver a entrar con su contraseña, y con la sesión nueva sí administra",
        c.get("/usuarios/", headers={**srv(), "Authorization": f"Bearer {de_nuevo['accessToken']}"}).status_code == 200)

finally:
    c.close()
    shutil.rmtree(RAIZ, ignore_errors=True)

print("\n" + "=" * 72)
print(f"{fallos} FALLARON" if fallos else "todas las comprobaciones pasaron")
raise SystemExit(1 if fallos else 0)
