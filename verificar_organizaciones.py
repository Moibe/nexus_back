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

import json
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
    for malo in ["a@b.c", "a@b..com", "a@-b.com", ".a@b.com", "a@b.com."]:
        r = c.post("/organizaciones/", json={**cuerpo, "nombre": "X", "adminEmail": malo}, headers=bearer)
        rev(f"correo con arroba y punto pero mal formado ({malo!r}): 400 correo_invalido", r.status_code == 400 and r.json()["detail"]["codigo"] == "correo_invalido", r.text[:120])
    r = c.post("/organizaciones/", json={**cuerpo, "nombre": "Y", "adminEmail": "y@ejemplo.com",
                                        "recuperacion": {"email": "pepito", "telefono": None}}, headers=bearer)
    rev("correo de RECUPERACIÓN inválido: 400 y no crea nada",
        r.status_code == 400 and r.json()["detail"]["codigo"] == "correo_recuperacion_invalido", r.text[:200])
    rev("nombre vacío: 400", c.post("/organizaciones/", json={**cuerpo, "nombre": "   ", "adminEmail": "z@ejemplo.com"}, headers=bearer).status_code == 400)

    titulo("5 · Listado")
    lista = c.get("/organizaciones/", headers=bearer).json()["organizaciones"]
    rev("están las dos, de la más nueva a la más vieja", len(lista) == 2 and lista[0]["slug"] == "seguros-monterrey-1", str([t["slug"] for t in lista]))
    r = c.get(f"/organizaciones/{primera['organizacion']['guid']}/usuarios", headers=bearer)
    rev("sus usuarios: por ahora solo su administrador",
        r.status_code == 200 and [u["email"] for u in r.json()["usuarios"]] == ["benjamin.lg@ejemplo.com"], r.text[:200])
    u0 = r.json()["usuarios"][0]
    rev("con la forma del listado de su administrador (número, rol EN esa organización) y nada interno",
        set(u0) == {"guid", "numero", "nombre", "email", "telefono", "rol", "rolNombre", "activo", "creadoEn", "debeCambiarContrasena", "accesoVencido", "sesionActiva"}
        and u0["numero"] == "001" and u0["rol"] == "ADMIN" and u0["activo"] is True, str(u0))
    rev("una organización que no existe: 404", c.get("/organizaciones/no-existe/usuarios", headers=bearer).status_code == 404)
    r = c.post("/organizaciones/", json={**cuerpo, "nombre": "Z", "adminEmail": "z2@ejemplo.com",
                                        "recuperacion": {"email": "  Respaldo@Ejemplo.COM  ", "telefono": None}}, headers=bearer)
    rev("y uno con espacios y mayúsculas queda normalizado",
        r.status_code == 201 and r.json()["organizacion"]["recuperacion"]["email"] == "respaldo@ejemplo.com", r.text[:250])

    titulo("6 · El administrador nuevo entra, pero no puede crear organizaciones")
    r = c.post("/auth/login", json={"email": "benjamin.lg@ejemplo.com", "password": primera["contrasenaTemporal"]}, headers=srv())
    rev("entra con su contraseña temporal", r.status_code == 200, r.text[:200])
    rev("y le toca cambiarla", r.json()["usuario"]["debeCambiarContrasena"] is True)
    rev("su membresía dice de qué organización es",
        r.json()["usuario"]["membresias"] == [{"tenantGuid": primera["organizacion"]["guid"], "rol": "ADMIN"}], str(r.json()["usuario"]["membresias"]))
    rev("y lo que capturó como datos de recuperación está en SU perfil (no solo en la organización)",
        r.json()["usuario"]["recuperacion"] == {"telefono": "+52 55 1478 8523", "email": "respaldo@ejemplo.com"}, str(r.json()["usuario"]["recuperacion"]))
    # Un administrador creado ANTES del arreglo (2026-10-14): su recuperación vivía solo en la organización. Se lee de ahí.
    import uuid as _uuid
    from servicios import registro
    g_viejo, t_viejo = str(_uuid.uuid4()), str(_uuid.uuid4())
    registro.agregar(usuarios.ARCHIVO, {"evento": "creado", "guid": g_viejo, "email": "admin.viejo@ejemplo.com", "nombre": "Ana", "apellidos": "Vieja",
                                        "esAdminPlataforma": False, "hash": "x", "debeCambiar": True, "en": "2026-10-01T10:00:00+00:00"})
    registro.agregar(usuarios.ARCHIVO, {"evento": "membresia", "guid": g_viejo, "tenantGuid": t_viejo, "rol": "ADMIN", "en": "2026-10-01T10:00:00+00:00"})
    registro.agregar(tenants_registro.ARCHIVO, {"evento": "creada", "guid": t_viejo, "nombre": "Vieja SA", "slug": "vieja-sa", "codigo": "NEX-09999", "adminGuid": g_viejo,
                                                "recuperacion": {"telefono": "+52 55 0000 1111", "email": "vieja@ejemplo.com"}, "en": "2026-10-01T10:00:00+00:00"})
    rev("y el de un administrador anterior al arreglo se lee de los datos de su organización",
        usuarios.por_guid(g_viejo)["recuperacion"] == {"telefono": "+52 55 0000 1111", "email": "vieja@ejemplo.com"}, str(usuarios.por_guid(g_viejo)["recuperacion"]))
    usuarios.actualizar_perfil(g_viejo, "Ana", "Vieja", "", None, {})
    rev("pero si la persona los borra en su perfil, se quedan borrados (no se vuelven a heredar)", usuarios.por_guid(g_viejo)["recuperacion"] == {}, str(usuarios.por_guid(g_viejo)["recuperacion"]))
    suyo = {**srv(), "Authorization": f"Bearer {r.json()['accessToken']}"}
    r = c.get("/organizaciones/", headers=suyo)
    rev("no puede listar organizaciones: 403 sin_permiso",
        r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso", r.text[:200])
    rev("ni crearlas", c.post("/organizaciones/", json={**cuerpo, "nombre": "Nova", "adminEmail": "n@ejemplo.com"}, headers=suyo).status_code == 403)

    titulo("6b · Un super admin que cierra sesión: su JWT ya no sirve para administrar (HU05)")
    # Con una SEGUNDA sesión, para no tocar la que usa la sección 7.
    otra_sesion = c.post("/auth/login", json={"email": "super@ejemplo.com", "password": temporal_admin}, headers=srv()).json()
    bearer_segunda = {**srv(), "Authorization": f"Bearer {otra_sesion['accessToken']}"}
    rev("(antes) con la segunda sesión entra", c.get("/organizaciones/", headers=bearer_segunda).status_code == 200)
    rev("cierra esa sesión: 204",
        c.post("/auth/logout", json={"refreshToken": otra_sesion["refreshToken"]}, headers=srv()).status_code == 204)
    r = c.get("/organizaciones/", headers=bearer_segunda)
    rev("su JWT, que todavía no vence: 401 sesion_cerrada",
        r.status_code == 401 and r.json()["detail"]["codigo"] == "sesion_cerrada", r.text[:160])
    rev("y con él no puede crear organizaciones",
        c.post("/organizaciones/", json={**cuerpo, "nombre": "Colada", "adminEmail": "colada@ejemplo.com"}, headers=bearer_segunda).status_code == 401)
    rev("pero la OTRA sesión del super admin, que no se cerró, sigue funcionando",
        c.get("/organizaciones/", headers=bearer).status_code == 200)

    titulo("6c · El administrador de plataforma cierra la sesión de un usuario de una organización (HU05 CA-4)")
    org = primera["organizacion"]["guid"]
    benjamin = primera["admin"]["guid"]
    ruta = f"/organizaciones/{org}/usuarios/{benjamin}/cerrar-sesion"
    ruta_auditoria = RAIZ / ".registro" / "auditoria.jsonl"

    def forzados() -> list[dict]:
        if not ruta_auditoria.exists():
            return []
        return [json.loads(x) for x in ruta_auditoria.read_text(encoding="utf-8").splitlines() if '"FORCED_LOGOUT"' in x]

    def cerrar(ruta_: str, motivo: str = "Equipo extraviado", cabeceras: dict | None = None):
        return c.post(ruta_, json={"motivo": motivo}, headers=cabeceras or bearer)

    # Benjamin llega con tres sesiones vivas: la de la sección 6 y dos más.
    sesiones = [
        c.post("/auth/login", json={"email": "benjamin.lg@ejemplo.com", "password": primera["contrasenaTemporal"]}, headers=srv()).json()
        for _ in range(2)
    ]
    suyo_a = {**srv(), "Authorization": f"Bearer {sesiones[0]['accessToken']}"}
    # Y el administrador de la OTRA organización, para comprobar que no se entera.
    ajeno = c.post("/auth/login", json={"email": "otro@ejemplo.com", "password": segunda["contrasenaTemporal"]}, headers=srv()).json()
    bearer_ajeno = {**srv(), "Authorization": f"Bearer {ajeno['accessToken']}"}
    rev("(antes) sus sesiones funcionan: con su JWT administra su organización",
        c.get("/usuarios/", headers=suyo).status_code == 200 and c.get("/usuarios/", headers=suyo_a).status_code == 200)

    r = cerrar(ruta, motivo="   ")
    rev("sin motivo: 400 motivo_requerido",
        r.status_code == 400 and r.json()["detail"]["codigo"] == "motivo_requerido", r.text[:160])
    r = cerrar(f"/organizaciones/no-existe/usuarios/{benjamin}/cerrar-sesion")
    rev("una organización que no existe: 404", r.status_code == 404 and "organización" in r.json()["detail"]["mensaje"], r.text[:160])
    r = cerrar(f"/organizaciones/{org}/usuarios/{segunda['admin']['guid']}/cerrar-sesion")
    rev("un usuario de OTRA organización, por la ruta de ésta: 404 (la ruta nombra la organización: no se cuela uno ajeno)",
        r.status_code == 404 and "usuario" in r.json()["detail"]["mensaje"], r.text[:160])
    r = cerrar(f"/organizaciones/{org}/usuarios/no-existe/cerrar-sesion")
    rev("un usuario que no existe: 404", r.status_code == 404, r.text[:160])
    r = cerrar(ruta, cabeceras=suyo)
    rev("el administrador de la organización NO usa esta ruta: 403 sin_permiso",
        r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso", r.text[:160])
    rev("sin sesión: 401", c.post(ruta, json={"motivo": "x"}, headers=srv()).status_code == 401)
    # El super admin casi nunca es miembro de una organización; si lo fuera, cerrarse
    # a sí mismo desde la lista lo echaría de la pantalla en la que está.
    super_guid = usuarios.para_login("super@ejemplo.com")["guid"]
    usuarios.agregar_membresia(super_guid, org, "ANALISTA")
    r = cerrar(f"/organizaciones/{org}/usuarios/{super_guid}/cerrar-sesion")
    rev("a sí mismo no (si además es miembro de la organización): 409 no_a_ti_mismo",
        r.status_code == 409 and r.json()["detail"]["codigo"] == "no_a_ti_mismo", r.text[:160])
    rev("ninguno de esos intentos rechazados dejó evento ni cerró nada",
        forzados() == [] and c.get("/usuarios/", headers=suyo_a).status_code == 200, str(forzados()))

    r = cerrar(ruta, motivo="  Equipo extraviado  ")
    rev("cierra sus sesiones: 200 y cuántas eran (3)", r.status_code == 200 and r.json() == {"cerradas": 3}, r.text[:160])
    rev("ninguna de sus sesiones puede renovarse: 401",
        all(c.post("/auth/refresh", json={"refreshToken": s["refreshToken"]}, headers=srv()).status_code == 401 for s in sesiones))
    r = c.get("/usuarios/", headers=suyo_a)
    rev("su JWT, que todavía no vence, ya no administra: 401 sesion_cerrada",
        r.status_code == 401 and r.json()["detail"]["codigo"] == "sesion_cerrada", r.text[:160])
    rev("la sesión de la sección 6 tampoco", c.get("/usuarios/", headers=suyo).status_code == 401)
    rev("los usuarios de OTRA organización no se enteran: su administrador sigue con sesión",
        c.get("/usuarios/", headers=bearer_ajeno).status_code == 200)
    rev("NO lo desactivó: su contraseña sigue sirviendo",
        c.post("/auth/login", json={"email": "benjamin.lg@ejemplo.com", "password": primera["contrasenaTemporal"]}, headers=srv()).status_code == 200)
    eventos = forzados()
    rev("quedó UN evento FORCED_LOGOUT con el motivo limpio, la organización y al administrador de plataforma como actor",
        len(eventos) == 1 and eventos[0]["usuarioGuid"] == benjamin and eventos[0]["por"] == super_guid
        and eventos[0]["motivo"] == "Equipo extraviado" and eventos[0]["tenantGuid"] == org
        and eventos[0]["actor"] == "ADMIN_PLATAFORMA" and bool(eventos[0]["en"]), str(eventos))

    titulo("7 · Un super admin desactivado deja de poder")
    usuarios.cambiar_estado(usuarios.para_login("super@ejemplo.com")["guid"], False)
    rev("403 desactivada", c.get("/organizaciones/", headers=bearer).status_code == 403)
finally:
    c.close()
    shutil.rmtree(RAIZ, ignore_errors=True)

print("\n" + "=" * 72)
print(f"{fallos} FALLARON" if fallos else "todas las comprobaciones pasaron")
raise SystemExit(1 if fallos else 0)
