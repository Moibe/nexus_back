"""¿Funciona el acceso (HU01, HU03, HU04) de punta a punta?

    .venv/Scripts/python verificar_auth.py

OFFLINE y sin tocar nada tuyo, como `verificar_webhooks.py`: la app contra un
`ALMACEN_RUTA` temporal, con llave de servicio y secreto JWT inventados.

Lo que importa: que el bootstrap pueda correrse varias veces con correos
distintos, que rechace el correo repetido y que la contraseña temporal no
quede escrita en claro; que el login distinga correo inexistente, contraseña
mala (con la cuenta de intentos), cuenta bloqueada (5 intentos → 15 min, y que
los intentos durante el bloqueo no lo alarguen) y cuenta desactivada; que en el
primer acceso entre pero con `debeCambiarContrasena`, que el cambio exija las
reglas y cierre todas las sesiones; que el refresh rote y el viejo muera; y que
el logout revoque.
"""

import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-auth-"))
(RAIZ / ".nexus-almacen").touch()  # el centinela que el almacén exige para escribir
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
from app import app  # noqa: E402
from servicios import auth, usuarios  # noqa: E402

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


def login(c: TestClient, email: str, password: str, origen: str | None = None):
    cuerpo = {"email": email, "password": password, "ip": "127.0.0.1", "userAgent": "verificar"}
    if origen:
        cuerpo["origen"] = origen
    return c.post("/auth/login", json=cuerpo, headers=srv())


c = TestClient(app)
try:
    titulo("1 · Bootstrap")
    temporal = auth.contrasena_temporal()
    rev("la contraseña temporal cumple las reglas", auth.reglas_incumplidas(temporal) == [])
    u = usuarios.bootstrap_admin("Admin@Ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal))
    rev("crea al admin con el correo en minúsculas y debeCambiar", u["email"] == "admin@ejemplo.com" and u["esAdminPlataforma"] and u["debeCambiarContrasena"])
    otro = usuarios.bootstrap_admin("otro@ejemplo.com", "B", "C", auth.hash_de(temporal))
    rev("un segundo bootstrap con otro correo crea otro super admin", otro["esAdminPlataforma"] and otro["guid"] != u["guid"])
    try:
        usuarios.bootstrap_admin("ADMIN@ejemplo.com", "B", "C", auth.hash_de(temporal))
        rev("con un correo ya registrado se rechaza", False)
    except usuarios.YaExiste:
        rev("con un correo ya registrado se rechaza", True)
    registro = (RAIZ / ".registro" / "usuarios.jsonl").read_text(encoding="utf-8")
    rev("la contraseña temporal NO está en el registro; el hash es argon2id", temporal not in registro and "$argon2id$" in registro)

    titulo("2 · Login: correo, contraseña, intentos y bloqueo")
    rev("sin llave de servicio: 401", c.post("/auth/login", json={"email": "a@b.co", "password": "x"}).status_code == 401)
    r = login(c, "no-es-correo", "x")
    rev("correo mal formado: 400 correo_invalido", r.status_code == 400 and r.json()["detail"]["codigo"] == "correo_invalido", r.text)
    r = login(c, "nadie@ejemplo.com", "x")
    rev("correo inexistente: 401 correo_no_existe", r.status_code == 401 and r.json()["detail"]["codigo"] == "correo_no_existe", r.text)
    restantes = []
    for _ in range(4):
        r = login(c, "admin@ejemplo.com", "mala")
        restantes.append(r.json()["detail"].get("intentosRestantes"))
    rev("4 contraseñas malas: 401 credenciales con 4,3,2,1 intentos restantes", restantes == [4, 3, 2, 1], str(restantes))
    r = login(c, "admin@ejemplo.com", "mala")
    rev("la quinta bloquea: 423 bloqueada con hastaEn ~15 min", r.status_code == 423 and 14 * 60 < r.json()["detail"]["segundosRestantes"] <= 15 * 60, r.text)
    hasta = r.json()["detail"]["hastaEn"]
    r = login(c, "admin@ejemplo.com", temporal)
    rev("bloqueada, incluso con la contraseña correcta", r.status_code == 423)
    rev("...y el bloqueo NO se alarga por intentar", r.json()["detail"]["hastaEn"] == hasta)
    # Se adelanta el reloj del bloqueo: se reescribe el evento que bloqueó, como
    # si hubiera pasado hace 16 minutos.
    ruta = RAIZ / ".registro" / "usuarios.jsonl"
    lineas = ruta.read_text(encoding="utf-8").splitlines()
    hace = (datetime.now(timezone.utc) - timedelta(minutes=16)).isoformat(timespec="seconds")
    import json as _json
    nuevas = []
    for l in lineas:
        e = _json.loads(l)
        if e.get("evento") == "intento" and e.get("motivo") == "BAD_PASSWORD":
            e["en"] = hace
        nuevas.append(_json.dumps(e, ensure_ascii=False))
    ruta.write_text("\n".join(nuevas) + "\n", encoding="utf-8")
    r = login(c, "admin@ejemplo.com", "mala")
    rev("pasados los 15 min se puede intentar de nuevo, y el contador arrancó en cero", r.status_code == 401 and r.json()["detail"]["intentosRestantes"] == 4, r.text)

    titulo("2b · Los cinco intentos son de correo O de contraseña, por navegador")
    NAV_A, NAV_B = "navegador-prueba-a", "navegador-prueba-b"
    restantes = []
    for i in range(4):
        r = login(c, f"nadie{i}@ejemplo.com", "x", NAV_A)
        restantes.append(r.json()["detail"].get("intentosRestantes"))
    rev("4 correos inexistentes ya gastan intentos: 4,3,2,1", restantes == [4, 3, 2, 1], str(restantes))
    r = login(c, "admin@ejemplo.com", "mala", NAV_A)
    rev("la quinta, ya con contraseña mala, bloquea ese navegador 15 min",
        r.status_code == 423 and 14 * 60 < r.json()["detail"]["segundosRestantes"] <= 15 * 60, r.text)
    r = login(c, "admin@ejemplo.com", temporal, NAV_A)
    rev("el navegador bloqueado no entra ni con la contraseña correcta", r.status_code == 423, r.text[:160])
    r = login(c, "otro-que-no-existe@ejemplo.com", "x", NAV_B)
    rev("otro navegador conserva sus propios 5 intentos",
        r.status_code == 401 and r.json()["detail"]["intentosRestantes"] == 4, r.text)
    rev("y la cuenta sigue sin bloquearse: el contador del navegador es aparte",
        usuarios.para_login("admin@ejemplo.com")["bloqueadoHasta"] is None)

    titulo("3 · Primer acceso: entra, pero debe cambiar la contraseña")
    r = login(c, "admin@ejemplo.com", temporal)
    rev("login correcto: 200 con tokens y usuario", r.status_code == 200 and {"accessToken", "refreshToken", "usuario"} <= set(r.json()), r.text[:200])
    sesion1 = r.json()
    rev("el usuario viene con debeCambiarContrasena", sesion1["usuario"]["debeCambiarContrasena"] is True)
    claims = auth.verificar_acceso(sesion1["accessToken"])
    rev("el JWT trae sub, sid, dcc=true y adm=true", bool(claims) and claims["sub"] == u["guid"] and claims["dcc"] is True and claims["adm"] is True)
    rev("no-store en la respuesta", r.headers.get("cache-control") == "no-store")
    bearer = {**srv(), "Authorization": f"Bearer {sesion1['accessToken']}"}
    r = c.get("/auth/yo", headers=bearer)
    rev("/auth/yo con el bearer: 200 con el usuario", r.status_code == 200 and r.json()["usuario"]["email"] == "admin@ejemplo.com", r.text[:200])
    rev("/auth/yo sin bearer: 401", c.get("/auth/yo", headers=srv()).status_code == 401)
    r = c.post("/auth/contrasena", json={"nueva": "corta", "confirmacion": "corta"}, headers=bearer)
    rev("contraseña que no cumple: 400 reglas con lo que falta", r.status_code == 400 and r.json()["detail"]["codigo"] == "reglas" and "12 caracteres" in r.json()["detail"]["faltan"], r.text)
    r = c.post("/auth/contrasena", json={"nueva": "Nueva-Segura-123!", "confirmacion": "Otra-Segura-123!"}, headers=bearer)
    rev("no coincide: 400 no_coincide", r.status_code == 400 and r.json()["detail"]["codigo"] == "no_coincide")
    r = c.post("/auth/contrasena", json={"nueva": temporal, "confirmacion": temporal}, headers=bearer)
    rev("igual a la temporal: 400 misma", r.status_code == 400 and r.json()["detail"]["codigo"] == "misma")
    r = c.post("/auth/contrasena", json={"nueva": "Nueva-Segura-123!", "confirmacion": "Nueva-Segura-123!"}, headers=bearer)
    rev("cambio correcto: motivo FIRST_LOGIN y cierra sesiones", r.status_code == 200 and r.json()["motivo"] == "FIRST_LOGIN" and r.json()["sesionesCerradas"] is True, r.text[:200])
    r = c.post("/auth/refresh", json={"refreshToken": sesion1["refreshToken"]}, headers=srv())
    rev("el refresh de la sesión anterior ya no sirve", r.status_code == 401 and r.json()["detail"]["codigo"] == "sesion_invalida")
    rev("la temporal ya no entra", login(c, "admin@ejemplo.com", temporal).status_code == 401)

    titulo("4 · Sesión normal: refresh rota, logout revoca, cambio con actual")
    r = login(c, "admin@ejemplo.com", "Nueva-Segura-123!")
    s2 = r.json()
    rev("entra con la nueva, ya sin debeCambiar", r.status_code == 200 and s2["usuario"]["debeCambiarContrasena"] is False)
    r = c.post("/auth/refresh", json={"refreshToken": s2["refreshToken"]}, headers=srv())
    s3 = r.json()
    rev("refresh: 200 con tokens nuevos", r.status_code == 200 and s3["refreshToken"] != s2["refreshToken"] and s3["accessToken"] != s2["accessToken"], r.text[:200])
    rev("el refresh viejo quedó revocado", c.post("/auth/refresh", json={"refreshToken": s2["refreshToken"]}, headers=srv()).status_code == 401)
    bearer3 = {**srv(), "Authorization": f"Bearer {s3['accessToken']}"}
    r = c.post("/auth/contrasena", json={"nueva": "Otra-Segura-456!", "confirmacion": "Otra-Segura-456!"}, headers=bearer3)
    rev("cambio sin la actual: 401 actual_incorrecta", r.status_code == 401 and r.json()["detail"]["codigo"] == "actual_incorrecta")
    r = c.post("/auth/contrasena", json={"actual": "Nueva-Segura-123!", "nueva": "Otra-Segura-456!", "confirmacion": "Otra-Segura-456!"}, headers=bearer3)
    rev("cambio con la actual: CHANGE y NO cierra sesiones", r.status_code == 200 and r.json()["motivo"] == "CHANGE" and r.json()["sesionesCerradas"] is False, r.text[:200])
    r = c.post("/auth/refresh", json={"refreshToken": s3["refreshToken"]}, headers=srv())
    rev("la sesión sigue viva tras CHANGE", r.status_code == 200)
    s4 = r.json()
    rev("logout: 204", c.post("/auth/logout", json={"refreshToken": s4["refreshToken"]}, headers=srv()).status_code == 204)
    rev("tras logout el refresh no sirve", c.post("/auth/refresh", json={"refreshToken": s4["refreshToken"]}, headers=srv()).status_code == 401)
    rev("logout sin token: 204 igual", c.post("/auth/logout", json={}, headers=srv()).status_code == 204)

    titulo("5 · Cuenta desactivada")
    r = login(c, "admin@ejemplo.com", "Otra-Segura-456!")
    s5 = r.json()
    usuarios.cambiar_estado(u["guid"], False)
    r = login(c, "admin@ejemplo.com", "Otra-Segura-456!")
    rev("login: 403 desactivada", r.status_code == 403 and r.json()["detail"]["codigo"] == "desactivada", r.text)
    rev("su refresh quedó revocado", c.post("/auth/refresh", json={"refreshToken": s5["refreshToken"]}, headers=srv()).status_code == 401)
    rev("y su bearer da 403", c.get("/auth/yo", headers={**srv(), "Authorization": f"Bearer {s5['accessToken']}"}).status_code == 403)
    usuarios.cambiar_estado(u["guid"], True)
    rev("reactivada: vuelve a entrar", login(c, "admin@ejemplo.com", "Otra-Segura-456!").status_code == 200)

    titulo("6 · Mi perfil (HU12)")
    r = login(c, "admin@ejemplo.com", "Otra-Segura-456!")
    mio = {**srv(), "Authorization": f"Bearer {r.json()['accessToken']}"}
    rev("sin sesión: 401", c.post("/auth/perfil", json={"nombre": "X"}, headers=srv()).status_code == 401)
    r = c.post("/auth/perfil", json={"nombre": "   "}, headers=mio)
    rev("sin nombre: 400", r.status_code == 400 and r.json()["detail"]["codigo"] == "nombre_requerido", r.text[:160])
    r = c.post("/auth/perfil", json={"nombre": "Ada", "recuperacion": {"email": "no-es-correo"}}, headers=mio)
    rev("correo de recuperación inválido: 400",
        r.status_code == 400 and r.json()["detail"]["codigo"] == "correo_recuperacion_invalido", r.text[:160])
    r = c.post(
        "/auth/perfil",
        json={"nombre": "Ada", "apellidoPaterno": "Lovelace", "apellidoMaterno": "Byron",
              "telefono": "+52 55 1020 3040",
              "recuperacion": {"email": "respaldo@ejemplo.com", "telefono": "+52 55 1478 8523"}},
        headers=mio,
    )
    u = r.json().get("usuario", {})
    rev("200 con los apellidos separados", r.status_code == 200 and u.get("apellidoPaterno") == "Lovelace" and u.get("apellidoMaterno") == "Byron", r.text[:200])
    rev("y `apellidos` compuesto, para quien ya lo usaba", u.get("apellidos") == "Lovelace Byron")
    rev("con su teléfono y su recuperación",
        u.get("telefono") == "+52 55 1020 3040" and u.get("recuperacion", {}).get("email") == "respaldo@ejemplo.com")
    r = c.get("/auth/yo", headers=mio)
    rev("se lee de vuelta en /auth/yo", r.json()["usuario"]["apellidoPaterno"] == "Lovelace", r.text[:200])
    rev("el correo con el que entra NO cambia", r.json()["usuario"]["email"] == "admin@ejemplo.com")
    rev("la recuperación se puede vaciar",
        c.post("/auth/perfil", json={"nombre": "Ada", "apellidoPaterno": "Lovelace"}, headers=mio).json()["usuario"]["recuperacion"] == {})

    titulo("7 · Sin AUTH_JWT_SECRET")
    guardado = config.AUTH_JWT_SECRET
    config.AUTH_JWT_SECRET = ""
    rev("login: 503 sin_configurar", login(c, "admin@ejemplo.com", "Otra-Segura-456!").status_code == 503)
    config.AUTH_JWT_SECRET = guardado
finally:
    c.close()
    shutil.rmtree(RAIZ, ignore_errors=True)

print("\n" + "=" * 72)
print(f"{fallos} FALLARON" if fallos else "todas las comprobaciones pasaron")
raise SystemExit(1 if fallos else 0)
