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

import json
import os
import random
import shutil
import tempfile
import threading
import time
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


def nota(texto: str) -> None:
    """Una observación que NO cuenta como falla: describe lo que hoy pasa en algo
    que está pendiente de decisión. Cuando se resuelva, la nota cambia sola de
    redacción y es la señal para convertirla en una comprobación."""
    print(f"  NOTA  {texto}")


def eventos_de_auditoria(accion: str) -> list[dict]:
    ruta = RAIZ / ".registro" / "auditoria.jsonl"
    if not ruta.exists():
        return []
    lineas = [json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip()]
    return [e for e in lineas if e.get("accion") == accion]


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
    # Una reescritura EN SITIO del mismo tamaño solo se nota por la fecha de modificación (el registro
    # es de solo agregar y esto lo contradice a propósito); si cae en el mismo tick de esa fecha la firma
    # no cambia, así que se le avisa a la caché para que la prueba no dependa del reloj del sistema.
    usuarios._cache_indice = None
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

    titulo("4b · HU05: cerrar sesión en un dispositivo NO cierra las otras, y deja su evento")
    d1 = login(c, "admin@ejemplo.com", "Otra-Segura-456!").json()
    d2 = login(c, "admin@ejemplo.com", "Otra-Segura-456!").json()
    d3 = login(c, "admin@ejemplo.com", "Otra-Segura-456!").json()
    rev("tres dispositivos con sesión, cada uno con su refresh", len({d1["refreshToken"], d2["refreshToken"], d3["refreshToken"]}) == 3)
    # La sección 4 ya cerró una sesión y dejó su evento: se cuenta a partir de aquí.
    antes = len(eventos_de_auditoria("LOGOUT"))
    rev("cerrar el primero: 204",
        c.post("/auth/logout", json={"refreshToken": d1["refreshToken"], "ip": "10.1.1.1", "userAgent": "navegador-1"}, headers=srv()).status_code == 204)
    rev("el refresh del primero ya no sirve", c.post("/auth/refresh", json={"refreshToken": d1["refreshToken"]}, headers=srv()).status_code == 401)
    r2 = c.post("/auth/refresh", json={"refreshToken": d2["refreshToken"]}, headers=srv())
    r3 = c.post("/auth/refresh", json={"refreshToken": d3["refreshToken"]}, headers=srv())
    rev("CA-3: los otros dos SIGUEN renovando", r2.status_code == 200 and r3.status_code == 200, f"{r2.status_code} {r3.status_code}")
    sid_cerrada = auth.verificar_acceso(d1["accessToken"])["sid"]
    cierres = [e for e in eventos_de_auditoria("LOGOUT") if e["sesionGuid"] == sid_cerrada]
    rev("CA-1: quedó UN evento LOGOUT de esa sesión en la bitácora de auditoría",
        len(cierres) == 1 and len(eventos_de_auditoria("LOGOUT")) == antes + 1, str(len(cierres)))
    rev("con la persona, la sesión que se cerró, la IP, el navegador y la hora",
        cierres[0]["usuarioGuid"] == u["guid"] and cierres[0]["ip"] == "10.1.1.1"
        and cierres[0]["userAgent"] == "navegador-1" and bool(cierres[0]["en"]), str(cierres[0]))
    rev("cerrar la MISMA sesión otra vez: 204 y NO duplica el evento",
        c.post("/auth/logout", json={"refreshToken": d1["refreshToken"]}, headers=srv()).status_code == 204
        and len(eventos_de_auditoria("LOGOUT")) == antes + 1)
    rev("un token que no existe: 204 y sin evento",
        c.post("/auth/logout", json={"refreshToken": "inventado-" + "x" * 40}, headers=srv()).status_code == 204
        and len(eventos_de_auditoria("LOGOUT")) == antes + 1)
    rev("un cliente que no manda IP ni navegador cierra igual, y el evento lo dice",
        # Con el refresh VIGENTE del tercero: el que traía de origen ya se rotó arriba al renovar.
        c.post("/auth/logout", json={"refreshToken": r3.json()["refreshToken"]}, headers=srv()).status_code == 204
        and len(eventos_de_auditoria("LOGOUT")) == antes + 2 and eventos_de_auditoria("LOGOUT")[-1]["ip"] is None)

    titulo("4c · HU05 CA-2: el JWT de una sesión cerrada se RECHAZA, no solo su refresh")
    def con(token: str) -> dict:
        return {**srv(), "Authorization": f"Bearer {token}"}

    r = c.get("/auth/yo", headers=con(d1["accessToken"]))
    rev("el JWT de la sesión cerrada: 401 sesion_cerrada (todavía no vence, pero su sesión sí murió)",
        r.status_code == 401 and r.json()["detail"]["codigo"] == "sesion_cerrada", r.text[:160])
    r = c.post("/auth/perfil", json={"nombre": "Intruso"}, headers=con(d1["accessToken"]))
    rev("y con él ya no se puede ESCRIBIR (antes un POST /auth/perfil pasaba)", r.status_code == 401, r.text[:160])
    rev("CA-3: el JWT del otro dispositivo, ya renovado, SIGUE funcionando",
        c.get("/auth/yo", headers=con(r2.json()["accessToken"])).status_code == 200)
    # Regla elegida: una sesión rotada también está muerta. Al renovar se emite una
    # sesión nueva con su propio JWT, y el front solo renueva cuando el anterior ya
    # venció. Si algún día el front renovara por anticipado, esta comprobación
    # fallaría y obligaría a decidirlo a propósito en vez de romper peticiones a medias.
    rev("el JWT ANTERIOR a la renovación de un dispositivo (su sesión quedó ROTATED) ya no vale",
        c.get("/auth/yo", headers=con(d2["accessToken"])).status_code == 401)

    titulo("4d · Un JWT con la firma buena pero una sesión que no es suya o no existe, se rechaza")
    inventada, _ = auth.emitir_acceso(usuarios.por_guid(u["guid"]), "sesion-que-no-existe")
    r = c.get("/auth/yo", headers=con(inventada))
    rev("sesión que no existe en el registro: 401 sesion_cerrada (ante la duda, se rechaza)",
        r.status_code == 401 and r.json()["detail"]["codigo"] == "sesion_cerrada", r.text[:160])
    otra = usuarios.bootstrap_admin("otra@ejemplo.com", "Otra", "Persona", auth.hash_de("Cualquiera-Larga-123!"))
    usuarios.fijar_contrasena(otra["guid"], auth.hash_de("Otra-Contrasena-123!"), "FIRST_LOGIN")
    sesion_de_otra = login(c, "otra@ejemplo.com", "Otra-Contrasena-123!").json()
    sid_de_otra = auth.verificar_acceso(sesion_de_otra["accessToken"])["sid"]
    prestada, _ = auth.emitir_acceso(usuarios.por_guid(u["guid"]), sid_de_otra)
    rev("el sid de la sesión de OTRA persona, aunque esté viva: 401",
        c.get("/auth/yo", headers=con(prestada)).status_code == 401)
    rev("y la sesión de esa otra persona sigue funcionando (no se tocó)",
        c.get("/auth/yo", headers=con(sesion_de_otra["accessToken"])).status_code == 200)
    nota(
        "2ª etapa (2026-10-13): las rutas que decide SOLO el front (bandeja, llaves, webhooks, archivos, procesadores) "
        "las guarda hooks.server.ts, que ahora le pregunta a GET /auth/sesion (4e) una vez cada pocos segundos por sesión. Eso se mide en "
        "el navegador: probar-sesion-cerrada-front.mjs."
    )

    titulo("4e · GET /auth/sesion: ¿sigue viva? (HU05, 2ª etapa; lo consulta el guard del front cada pocos segundos por sesión)")

    def estado(token: str | None):
        """(HTTP, estado) si contestó, o (HTTP, codigo del error) si no pudo."""
        cab = con(token) if token else srv()
        r = c.get("/auth/sesion", headers=cab)
        return r.status_code, (r.json().get("estado") if r.status_code == 200 else r.json()["detail"]["codigo"])

    vivo = login(c, "admin@ejemplo.com", "Otra-Segura-456!").json()
    rev("una sesión abierta: 200 viva", estado(vivo["accessToken"]) == (200, "viva"), str(estado(vivo["accessToken"])))
    rev("la respuesta no se guarda en ningún caché (no-store)",
        c.get("/auth/sesion", headers=con(vivo["accessToken"])).headers.get("cache-control") == "no-store")
    c.post("/auth/logout", json={"refreshToken": vivo["refreshToken"]}, headers=srv())
    rev("tras cerrar la sesión (logout): 200 cerrada", estado(vivo["accessToken"]) == (200, "cerrada"))
    rev("el JWT de la sesión que se cerró en 4b: cerrada", estado(d1["accessToken"]) == (200, "cerrada"))
    rev("el JWT anterior a una renovación (su sesión quedó ROTATED): cerrada", estado(d2["accessToken"]) == (200, "cerrada"))
    rev("el JWT del dispositivo que SÍ renovó: viva", estado(r2.json()["accessToken"]) == (200, "viva"))
    forzada = login(c, "admin@ejemplo.com", "Otra-Segura-456!").json()
    otro_disp = login(c, "admin@ejemplo.com", "Otra-Segura-456!").json()
    usuarios.cerrar_sesiones(u["guid"], "prueba de 4e")
    rev("cerrada a la fuerza (cierre forzado): cerrada, en TODOS sus dispositivos",
        estado(forzada["accessToken"]) == (200, "cerrada") and estado(otro_disp["accessToken"]) == (200, "cerrada"))
    rev("sesión que no existe en el registro (JWT inventado con la firma buena): cerrada, ante la duda se rechaza",
        estado(inventada) == (200, "cerrada"))
    rev("el sid de la sesión de OTRA persona: cerrada", estado(prestada) == (200, "cerrada"))
    rev("y la sesión de esa otra persona, que sigue abierta: viva", estado(sesion_de_otra["accessToken"]) == (200, "viva"))
    cuarta = usuarios.bootstrap_admin("cuarta@ejemplo.com", "Cuarta", "Persona", auth.hash_de("Cualquiera-Larga-123!"))
    usuarios.fijar_contrasena(cuarta["guid"], auth.hash_de("Cuarta-Contrasena-123!"), "FIRST_LOGIN")
    de_cuarta = login(c, "cuarta@ejemplo.com", "Cuarta-Contrasena-123!").json()
    rev("(antes) la de la cuarta persona está viva", estado(de_cuarta["accessToken"]) == (200, "viva"))
    usuarios.cambiar_estado(cuarta["guid"], False)
    rev("desactivada: 200 desactivada (se mira antes que la sesión: quien está desactivado no debe ver 'cerrada')",
        estado(de_cuarta["accessToken"]) == (200, "desactivada"))
    usuarios.cambiar_estado(cuarta["guid"], True)
    rev("reactivada: su sesión de antes sigue cerrada (reactivar no la resucita)",
        estado(de_cuarta["accessToken"]) == (200, "cerrada"))
    fantasma, _ = auth.emitir_acceso({**usuarios.por_guid(u["guid"]), "guid": "persona-que-no-existe"}, "sesion-x")
    rev("una persona que ya no existe: 200 inexistente", estado(fantasma) == (200, "inexistente"))
    rev("sin Bearer: 401 sin_sesion (no se pudo contestar; no quiere decir que la sesión murió)", estado(None) == (401, "sin_sesion"))
    rev("un Bearer que no es un JWT: 401 sesion_invalida", estado("basura") == (401, "sesion_invalida"))
    rev("sin llave de servicio: 401", c.get("/auth/sesion", headers={"Authorization": f"Bearer {de_cuarta['accessToken']}"}).status_code == 401)
    # Un montaje del almacén caído NO es una sesión muerta: se contesta 503 ("no se pudo contestar"), no un 500 anónimo.
    centinela = RAIZ / ".nexus-almacen"
    centinela.unlink()
    try:
        r = c.get("/auth/sesion", headers=con(de_cuarta["accessToken"]))
        rev("con el almacén sin montar: 503, no un 500 anónimo", r.status_code == 503, f"{r.status_code} {r.text[:80]}")
    finally:
        centinela.touch()
    rev("al volver el almacén, contesta de nuevo", estado(de_cuarta["accessToken"])[0] == 200)

    titulo("4f · El índice de usuarios se guarda mientras el registro no cambie (la consulta de sesión corre cada pocos segundos por cada sesión abierta)")
    contadas = {"n": 0}
    leer_original = usuarios.registro.eventos

    def leer_contando(nombre):
        contadas["n"] += 1
        return leer_original(nombre)

    usuarios.registro.eventos = leer_contando
    try:
        s4f = login(c, "admin@ejemplo.com", "Otra-Segura-456!").json()
        claims_4f = auth.verificar_acceso(s4f["accessToken"])
        usuarios.por_guid_y_sesion(claims_4f["sub"], claims_4f["sid"])  # deja la caché al día tras el login
        contadas["n"] = 0
        for _ in range(50):
            usuarios.por_guid_y_sesion(claims_4f["sub"], claims_4f["sid"])
        rev("50 consultas sin que nadie escriba: el registro NO se vuelve a leer", contadas["n"] == 0, str(contadas["n"]))
        for _ in range(5):
            c.get("/auth/sesion", headers=con(s4f["accessToken"]))
        rev("ni por el endpoint (5 consultas más)", contadas["n"] == 0, str(contadas["n"]))
        usuarios.crear_sesion(u["guid"], "hash-4f", datetime.now(timezone.utc) + timedelta(days=1), None, None)
        contadas["n"] = 0
        usuarios.por_guid(u["guid"])
        rev("tras una escritura, la siguiente consulta lee UNA vez", contadas["n"] == 1, str(contadas["n"]))
        usuarios.por_guid(u["guid"])
        rev("y la que sigue ya no", contadas["n"] == 1, str(contadas["n"]))
        # Un cambio hecho por FUERA de este proceso (un script, otro proceso) también debe verse.
        ruta_usuarios = RAIZ / ".registro" / "usuarios.jsonl"
        with open(ruta_usuarios, "a", encoding="utf-8") as f:
            f.write(json.dumps({"evento": "estado", "guid": u["guid"], "activo": False, "motivo": "otro proceso", "en": "2026-10-12T00:00:00+00:00"}) + chr(10))
        rev("un cambio hecho por FUERA del proceso se ve, sin reiniciar nada", usuarios.por_guid(u["guid"])["activo"] is False)
        with open(ruta_usuarios, "a", encoding="utf-8") as f:
            f.write(json.dumps({"evento": "estado", "guid": u["guid"], "activo": True, "motivo": "otro proceso", "en": "2026-10-12T00:00:01+00:00"}) + chr(10))
        rev("y el siguiente también", usuarios.por_guid(u["guid"])["activo"] is True)
    finally:
        usuarios.registro.eventos = leer_original

    titulo("4g · El repaso del registro es LINEAL y sigue las mismas reglas que el cuadrático de antes (prueba diferencial)")

    def repaso_viejo(eventos: list[dict]) -> dict:
        """Las sesiones tal como las calculaba `_indice()` hasta el 2026-10-12: cada revocación recorría TODAS."""
        personas: set[str] = set()
        sesiones_: dict[str, dict] = {}
        for e in eventos:
            tipo, guid = e.get("evento"), e.get("guid")
            if tipo == "creado" and isinstance(guid, str):
                personas.add(guid)
                continue
            existe = isinstance(guid, str) and guid in personas
            if tipo == "sesion" and existe and isinstance(e.get("sesionGuid"), str):
                sesiones_.setdefault(e["sesionGuid"], {"guid": guid, "revocadaEn": None, "revocadaPor": None})
            elif tipo == "sesion_revocada":
                objetivo = e.get("sesionGuid")
                for sid_, s in sesiones_.items():
                    if s["revocadaEn"]:
                        continue
                    if (objetivo and sid_ == objetivo) or (not objetivo and s["guid"] == guid):
                        s["revocadaEn"], s["revocadaPor"] = e.get("en"), e.get("motivo")
        return {k: (v["guid"], v["revocadaEn"], v["revocadaPor"]) for k, v in sesiones_.items()}

    def repaso_nuevo(eventos: list[dict]) -> dict:
        _, _, ses = usuarios._construir_indice(eventos)
        return {k: (v["guid"], v["revocadaEn"], v["revocadaPor"]) for k, v in ses.items()}

    azar = random.Random(20261012)
    personas_g = ["p0", "p1", "p2", "p3", "fantasma"]  # "fantasma" nunca se crea
    distintos = 0
    for ronda in range(300):
        eventos_g: list[dict] = []
        vistas: list[str] = []
        for i in range(azar.randint(5, 80)):
            en = f"2026-10-12T{i // 60:02d}:{i % 60:02d}:00+00:00"
            persona = azar.choice(personas_g)
            clase = azar.choice(["creado", "sesion", "sesion", "sesion", "revoca_una", "revoca_una", "revoca_todas",
                                 "sesion_repetida", "revoca_inventada", "revoca_sin_guid", "revoca_rara", "sesion_guid_raro"])
            if clase == "creado" and persona != "fantasma":
                eventos_g.append({"evento": "creado", "guid": persona, "email": f"{persona}@x.com", "nombre": persona, "hash": "h", "en": en})
            elif clase == "sesion":
                sid_g = f"s{ronda}-{i}"
                vistas.append(sid_g)
                eventos_g.append({"evento": "sesion", "sesionGuid": sid_g, "guid": persona, "refreshHash": "r", "en": en})
            elif clase == "sesion_repetida" and vistas:
                eventos_g.append({"evento": "sesion", "sesionGuid": azar.choice(vistas), "guid": persona, "refreshHash": "r2", "en": en})
            elif clase == "revoca_una" and vistas:
                eventos_g.append({"evento": "sesion_revocada", "sesionGuid": azar.choice(vistas), "guid": persona, "motivo": "UNA", "en": en})
            elif clase == "revoca_todas":
                eventos_g.append({"evento": "sesion_revocada", "sesionGuid": None, "guid": persona, "motivo": "TODAS", "en": en})
            elif clase == "revoca_inventada":
                eventos_g.append({"evento": "sesion_revocada", "sesionGuid": "no-existe", "guid": persona, "motivo": "NADA", "en": en})
            elif clase == "revoca_sin_guid":
                eventos_g.append({"evento": "sesion_revocada", "sesionGuid": None, "motivo": "SIN-GUID", "en": en})
            elif clase == "revoca_rara":
                # La app solo escribe texto o None, pero una línea editada a mano puede traer cualquier cosa
                # (una lista no es hashable): el repaso no debe tronar, y debe coincidir con el de antes.
                eventos_g.append({"evento": "sesion_revocada", "sesionGuid": azar.choice([["x"], {"a": 1}, 7, True, 1.5, ""]),
                                  "guid": persona, "motivo": "RARA", "en": en})
            elif clase == "sesion_guid_raro":
                eventos_g.append({"evento": "sesion", "sesionGuid": f"r{ronda}-{i}", "guid": azar.choice([None, 7, ["p0"]]),
                                  "refreshHash": "r", "en": en})
        if repaso_viejo(eventos_g) != repaso_nuevo(eventos_g):
            distintos += 1
    rev("300 registros al azar (creaciones, sesiones repetidas, revocaciones de una, de todas, de la nada): "
        "el repaso nuevo da EXACTAMENTE lo mismo que el viejo", distintos == 0, f"{distintos} distintos")

    def sesiones_grandes(n: int) -> list[dict]:
        ev: list[dict] = [{"evento": "creado", "guid": f"g{i}", "email": f"g{i}@x.com", "nombre": "g", "hash": "h", "en": "2026-10-12T00:00:00+00:00"} for i in range(50)]
        anterior: dict[str, str] = {}
        for i in range(n):  # cada sesión "renueva" a la anterior de su persona (ROTATED)
            g = f"g{i % 50}"
            ev.append({"evento": "sesion", "sesionGuid": f"big-{i}", "guid": g, "refreshHash": "r", "en": "2026-10-12T00:00:00+00:00"})
            if g in anterior:
                ev.append({"evento": "sesion_revocada", "sesionGuid": anterior[g], "guid": g, "motivo": "ROTATED", "en": "2026-10-12T00:00:00+00:00"})
            anterior[g] = f"big-{i}"
        return ev

    def tarda(ev: list[dict]) -> float:
        mejor = float("inf")
        for _ in range(3):  # el mejor de tres: lo que cuenta es el costo, no un tropiezo del sistema
            t0 = time.perf_counter()
            usuarios._construir_indice(ev)
            mejor = min(mejor, time.perf_counter() - t0)
        return mejor

    t_n, t_4n = tarda(sesiones_grandes(10_000)), tarda(sesiones_grandes(40_000))
    # Cuatro veces más sesiones: lineal tarda ~4x; el cuadrático de antes, ~16x. El umbral (8x) cae en
    # medio, así el ruido de la máquina no lo mueve. Y no se usa un tiempo absoluto: en una máquina
    # rápida el algoritmo viejo también lo pasaría.
    rev(f"el repaso es LINEAL: cuatro veces más sesiones tarda ~cuatro veces más ({t_4n / t_n:.1f}x), no dieciséis", t_4n / t_n < 8,
        f"{t_n * 1000:.0f} ms -> {t_4n * 1000:.0f} ms")
    rev(f"y 80 mil eventos se repasan en menos de 1 s ({t_4n * 1000:.0f} ms)", t_4n < 1.0)

    titulo("4h · El canje del refresh es ATÓMICO (un refresh es de un solo uso) y el login no abre sesión con la credencial vieja")

    def vivas_de(guid: str) -> list[dict]:
        _, _, ses = usuarios._indice()
        return [s for s in ses.values() if s["guid"] == guid and not s["revocadaEn"]]

    # (a) Ocho hilos canjean a la vez el MISMO refresh, varias rondas: antes salían varias sesiones.
    rondas_mal = 0
    for _ in range(25):
        usuarios.cerrar_sesiones(u["guid"], "limpiar 4h")
        _, hash0, vence0 = auth.nuevo_refresh()
        usuarios.crear_sesion(u["guid"], hash0, vence0, None, None)
        resultados: list[str] = []
        barrera = threading.Barrier(8)

        def canjear():
            _, nuevo_hash, vence = auth.nuevo_refresh()
            barrera.wait()
            try:
                usuarios.rotar_sesion(hash0, nuevo_hash, vence, None, None)
                resultados.append("ok")
            except usuarios.SesionNoCanjeable:
                resultados.append("no")

        hilos = [threading.Thread(target=canjear) for _ in range(8)]
        [h.start() for h in hilos]
        [h.join() for h in hilos]
        if resultados.count("ok") != 1 or len(vivas_de(u["guid"])) != 1:
            rondas_mal += 1
    rev("ocho hilos con el mismo refresh, 25 rondas: lo canjea UNO solo y queda UNA sesión viva", rondas_mal == 0, f"{rondas_mal} rondas mal")

    # (b) Un cierre forzado contra un refresh que rota en ciclo: al terminar el cierre no sobrevive nadie.
    sucias = 0
    for ronda in range(30):
        usuarios.cerrar_sesiones(u["guid"], "limpiar 4h")
        _, h_actual, v_actual = auth.nuevo_refresh()
        usuarios.crear_sesion(u["guid"], h_actual, v_actual, None, None)
        estado_rot = {"hash": h_actual}
        parar_rotacion = threading.Event()

        def rotar_en_ciclo():
            while not parar_rotacion.is_set():
                _, nuevo_hash, vence = auth.nuevo_refresh()
                try:
                    usuarios.rotar_sesion(estado_rot["hash"], nuevo_hash, vence, None, None)
                except usuarios.SesionNoCanjeable:
                    return
                estado_rot["hash"] = nuevo_hash

        hilo = threading.Thread(target=rotar_en_ciclo, daemon=True)
        hilo.start()
        time.sleep((ronda % 5) / 1000)
        usuarios.cerrar_sesiones(u["guid"], "cierre forzado de la prueba 4h")
        hilo.join(timeout=3)
        sigue_rotando = hilo.is_alive()
        parar_rotacion.set()  # si el cierre no lo paró, se para aquí: la ronda cuenta como sucia y la prueba termina
        hilo.join(timeout=3)
        if sigue_rotando or vivas_de(u["guid"]):
            sucias += 1
    rev("30 cierres forzados contra un refresh que rota sin parar: terminado el cierre NO queda ninguna sesión viva", sucias == 0, f"{sucias} de 30")

    # (c) La contraseña se verifica fuera del candado: si cambia antes de abrir la sesión, la sesión no nace.
    quinta = usuarios.bootstrap_admin("quinta@ejemplo.com", "Quinta", "Persona", auth.hash_de("Cualquiera-Larga-123!"))
    usuarios.fijar_contrasena(quinta["guid"], auth.hash_de("Quinta-Contrasena-123!"), "FIRST_LOGIN")
    hash_verificado = usuarios.para_login("quinta@ejemplo.com")["hash"]
    _, h5, v5 = auth.nuevo_refresh()
    usuarios.crear_sesion(quinta["guid"], h5, v5, None, None, hash_verificado=hash_verificado)
    rev("con la contraseña vigente y la cuenta activa, la sesión nace", len(vivas_de(quinta["guid"])) == 1)
    usuarios.fijar_contrasena(quinta["guid"], auth.hash_de("Quinta-Otra-Contrasena-123!"), "RESET")
    _, h6, v6 = auth.nuevo_refresh()
    try:
        usuarios.crear_sesion(quinta["guid"], h6, v6, None, None, hash_verificado=hash_verificado)
        rev("tras un RESET, un login que verificó la contraseña de antes NO abre sesión", False)
    except usuarios.SesionNoCanjeable as e:
        rev("tras un RESET, un login que verificó la contraseña de antes NO abre sesión", e.motivo == "invalida" and not vivas_de(quinta["guid"]))
    hash_nuevo = usuarios.para_login("quinta@ejemplo.com")["hash"]
    usuarios.cambiar_estado(quinta["guid"], False)
    try:
        usuarios.crear_sesion(quinta["guid"], h6, v6, None, None, hash_verificado=hash_nuevo)
        rev("si la cuenta se desactiva mientras se verifica la contraseña, tampoco", False)
    except usuarios.SesionNoCanjeable as e:
        rev("si la cuenta se desactiva mientras se verifica la contraseña, tampoco", e.motivo == "desactivada" and not vivas_de(quinta["guid"]))

    # (d) Un refresh vencido o desconocido no se canjea, con su motivo.
    usuarios.cambiar_estado(quinta["guid"], True)
    _, h7, _v = auth.nuevo_refresh()
    usuarios.crear_sesion(quinta["guid"], h7, datetime.now(timezone.utc) - timedelta(minutes=1), None, None)
    try:
        usuarios.rotar_sesion(h7, "x" * 64, datetime.now(timezone.utc) + timedelta(days=1), None, None)
        rev("un refresh vencido no se canjea (vencida)", False)
    except usuarios.SesionNoCanjeable as e:
        rev("un refresh vencido no se canjea (vencida)", e.motivo == "vencida")
    try:
        usuarios.rotar_sesion("no-existe" + "0" * 55, "x" * 64, datetime.now(timezone.utc) + timedelta(days=1), None, None)
        rev("uno que nunca existió tampoco (invalida)", False)
    except usuarios.SesionNoCanjeable as e:
        rev("uno que nunca existió tampoco (invalida)", e.motivo == "invalida")

    titulo("4i · El cableado POR HTTP: login, refresh y cambio de contraseña ante carreras (4h solo probaba el servicio)")
    from servicios.almacen import ErrorAlmacen

    def usuario_4i(correo: str, contrasena: str) -> dict:
        nuevo = usuarios.bootstrap_admin(correo, "Prueba", "Cuatro-i", auth.hash_de("Temporal-Larga-123!"))
        usuarios.fijar_contrasena(nuevo["guid"], auth.hash_de(contrasena), "FIRST_LOGIN")
        return nuevo

    def ahora_iso() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def con_efecto_tras_verificar(efecto):
        """Envuelve `auth.verificar_contrasena`: la primera vez que ACIERTA corre `efecto` (alguien más
        toca la cuenta justo ahora, con la petición a media camino). Devuelve el original para restaurarlo."""
        original = auth.verificar_contrasena
        corrido = {"n": 0}

        def envuelta(hash_guardado, password):
            ok = original(hash_guardado, password)
            if ok and not corrido["n"]:
                corrido["n"] = 1
                efecto()
            return ok

        auth.verificar_contrasena = envuelta
        return original

    # (a) Un login con un RESTABLECIMIENTO a media petición: no abre sesión, y no queda como acierto.
    ana4 = usuario_4i("cuatro-i-ana@ejemplo.com", "Cuatro-I-Vieja-123!")
    original_verificar = con_efecto_tras_verificar(
        lambda: usuarios.fijar_contrasena(ana4["guid"], auth.hash_de("Cuatro-I-Nueva-456!"), "RESET")
    )
    try:
        r = login(c, "cuatro-i-ana@ejemplo.com", "Cuatro-I-Vieja-123!")
    finally:
        auth.verificar_contrasena = original_verificar
    detalle = r.json().get("detail", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
    rev("login con un RESET a media petición: 401 credenciales CON los intentos que quedan",
        r.status_code == 401 and detalle.get("codigo") == "credenciales" and detalle.get("intentosRestantes") == config.AUTH_MAX_INTENTOS - 1, r.text[:200])
    rev("...y NO queda ninguna sesión viva", not vivas_de(ana4["guid"]))
    reg4 = usuarios.para_login("cuatro-i-ana@ejemplo.com")
    rev("...ni se anotó como acierto: sin último ingreso y con UN intento fallido", reg4["ultimoLoginEn"] is None and reg4["fallidos"] == 1, f"{reg4['ultimoLoginEn']} {reg4['fallidos']}")
    rev("la dueña entra con la contraseña del restablecimiento", login(c, "cuatro-i-ana@ejemplo.com", "Cuatro-I-Nueva-456!").status_code == 200)

    # (b) Lo mismo con la cuenta DESACTIVADA a media petición.
    beto4 = usuario_4i("cuatro-i-beto@ejemplo.com", "Cuatro-I-Beto-123!")
    original_verificar = con_efecto_tras_verificar(lambda: usuarios.cambiar_estado(beto4["guid"], False, "prueba 4i"))
    try:
        r = login(c, "cuatro-i-beto@ejemplo.com", "Cuatro-I-Beto-123!")
    finally:
        auth.verificar_contrasena = original_verificar
    detalle = r.json().get("detail", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
    rev("login con la cuenta desactivada a media petición: 403 desactivada", r.status_code == 403 and detalle.get("codigo") == "desactivada", r.text[:200])
    rev("...sin sesión viva", not vivas_de(beto4["guid"]))
    intentos_beto = [e for e in usuarios.registro.eventos(usuarios.ARCHIVO) if e.get("evento") == "intento" and e.get("guid") == beto4["guid"]]
    rev("...y el intento quedó como INACTIVE, no como acierto", bool(intentos_beto) and intentos_beto[-1]["exitoso"] is False and intentos_beto[-1]["motivo"] == "INACTIVE", str(intentos_beto[-1:]))

    # (c) POST /auth/refresh: ocho peticiones a la vez con el MISMO refresh (el cableado del router, no solo `rotar_sesion`).
    cleo4 = usuario_4i("cuatro-i-cleo@ejemplo.com", "Cuatro-I-Cleo-123!")
    rondas_mal = 0
    for _ in range(10):
        usuarios.cerrar_sesiones(cleo4["guid"], "limpiar 4i")
        refresh0, hash0, vence0 = auth.nuevo_refresh()
        usuarios.crear_sesion(cleo4["guid"], hash0, vence0, None, None)
        codigos: list[int] = []
        barrera = threading.Barrier(8)

        def pedir_refresh(refresh_token=refresh0, codigos=codigos, barrera=barrera):
            cliente = TestClient(app)
            barrera.wait()
            codigos.append(cliente.post("/auth/refresh", json={"refreshToken": refresh_token}, headers=srv()).status_code)

        hilos = [threading.Thread(target=pedir_refresh) for _ in range(8)]
        [h.start() for h in hilos]
        [h.join() for h in hilos]
        if codigos.count(200) != 1 or codigos.count(401) != 7 or len(vivas_de(cleo4["guid"])) != 1:
            rondas_mal += 1
    rev("POST /auth/refresh, ocho peticiones a la vez con el mismo refresh, 10 rondas: UNA da 200, siete 401 y queda UNA sesión viva",
        rondas_mal == 0, f"{rondas_mal} rondas mal")

    # (d) Cambiar la contraseña con un RESTABLECIMIENTO a media petición: la sesión ya cerrada no pisa la contraseña del dueño.
    dora4 = usuario_4i("cuatro-i-dora@ejemplo.com", "Cuatro-I-Dora-123!")
    bearer_dora = {**srv(), "Authorization": f"Bearer {login(c, 'cuatro-i-dora@ejemplo.com', 'Cuatro-I-Dora-123!').json()['accessToken']}"}
    cambio = {"actual": "Cuatro-I-Dora-123!", "nueva": "Cuatro-I-Ladron-456!", "confirmacion": "Cuatro-I-Ladron-456!"}
    original_verificar = con_efecto_tras_verificar(
        lambda: usuarios.fijar_contrasena(dora4["guid"], auth.hash_de("Cuatro-I-Duena-789!"), "RESET")
    )
    try:
        r = c.post("/auth/contrasena", json=cambio, headers=bearer_dora)
    finally:
        auth.verificar_contrasena = original_verificar
    detalle = r.json().get("detail", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
    rev("cambiar la contraseña con un RESET a media petición: 401 sesion_cerrada", r.status_code == 401 and detalle.get("codigo") == "sesion_cerrada", r.text[:200])
    rev("la contraseña del restablecimiento sigue vigente: la dueña entra", login(c, "cuatro-i-dora@ejemplo.com", "Cuatro-I-Duena-789!").status_code == 200)
    rev("y la de quien tenía la sesión robada NO sirve", login(c, "cuatro-i-dora@ejemplo.com", "Cuatro-I-Ladron-456!").status_code == 401)

    # (e) Y si la cambió OTRO dispositivo mientras tanto, desde HU13 su cambio CIERRA esta sesión (CA-4): la petición que
    #     iba a medias encuentra su sesión cerrada, y la contraseña que puso el otro sigue vigente.
    emi4 = usuario_4i("cuatro-i-emi@ejemplo.com", "Cuatro-I-Emi-1234!")
    sesion_emi = login(c, "cuatro-i-emi@ejemplo.com", "Cuatro-I-Emi-1234!").json()
    bearer_emi = {**srv(), "Authorization": f"Bearer {sesion_emi['accessToken']}"}
    sesion_movil = login(c, "cuatro-i-emi@ejemplo.com", "Cuatro-I-Emi-1234!").json()
    cambio = {"actual": "Cuatro-I-Emi-1234!", "nueva": "Cuatro-I-Emi-Otra-567!", "confirmacion": "Cuatro-I-Emi-Otra-567!"}
    original_verificar = con_efecto_tras_verificar(
        lambda: usuarios.fijar_contrasena(
            emi4["guid"], auth.hash_de("Cuatro-I-Emi-Movil-890!"), "CHANGE", sesion_guid=auth.verificar_acceso(sesion_movil["accessToken"])["sid"]
        )
    )
    try:
        r = c.post("/auth/contrasena", json=cambio, headers=bearer_emi)
    finally:
        auth.verificar_contrasena = original_verificar
    detalle = r.json().get("detail", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
    rev("cambiarla mientras otro dispositivo ya la cambió (cerró esta sesión): 401 sesion_cerrada", r.status_code == 401 and detalle.get("codigo") == "sesion_cerrada", r.text[:200])
    rev("la que puso el otro dispositivo sigue vigente", login(c, "cuatro-i-emi@ejemplo.com", "Cuatro-I-Emi-Movil-890!").status_code == 200)

    # (e2) Dos envíos a la vez DESDE LA MISMA sesión (doble clic): el segundo ya verificó una "actual" que dejó de serlo,
    #      pero su sesión sigue viva (el primero la conserva): 401 actual_incorrecta, y gana la contraseña del primero.
    sesion_doble = login(c, "cuatro-i-emi@ejemplo.com", "Cuatro-I-Emi-Movil-890!").json()
    bearer_doble = {**srv(), "Authorization": f"Bearer {sesion_doble['accessToken']}"}
    original_verificar = con_efecto_tras_verificar(
        lambda: usuarios.fijar_contrasena(
            emi4["guid"], auth.hash_de("Cuatro-I-Emi-Primera-111!"), "CHANGE", sesion_guid=auth.verificar_acceso(sesion_doble["accessToken"])["sid"]
        )
    )
    try:
        r = c.post("/auth/contrasena", json={"actual": "Cuatro-I-Emi-Movil-890!", "nueva": "Cuatro-I-Emi-Segunda-222!", "confirmacion": "Cuatro-I-Emi-Segunda-222!"}, headers=bearer_doble)
    finally:
        auth.verificar_contrasena = original_verificar
    detalle = r.json().get("detail", {}) if r.headers.get("content-type", "").startswith("application/json") else {}
    rev("dos cambios a la vez desde la misma sesión: el segundo da 401 actual_incorrecta", r.status_code == 401 and detalle.get("codigo") == "actual_incorrecta", r.text[:200])
    rev("y la del primero es la vigente", login(c, "cuatro-i-emi@ejemplo.com", "Cuatro-I-Emi-Primera-111!").status_code == 200
        and login(c, "cuatro-i-emi@ejemplo.com", "Cuatro-I-Emi-Segunda-222!").status_code == 401)
    usuarios.fijar_contrasena(emi4["guid"], auth.hash_de("Cuatro-I-Emi-Movil-890!"), "FIRST_LOGIN")
    rev("y el cambio normal, sin carreras, sigue funcionando (200 CHANGE)",
        c.post("/auth/contrasena", json={"actual": "Cuatro-I-Emi-Movil-890!", "nueva": "Cuatro-I-Emi-Final-321!", "confirmacion": "Cuatro-I-Emi-Final-321!"},
               headers={**srv(), "Authorization": f"Bearer {login(c, 'cuatro-i-emi@ejemplo.com', 'Cuatro-I-Emi-Movil-890!').json()['accessToken']}"}).json().get("motivo") == "CHANGE")

    # (f) `fijar_contrasena` revalida bajo el candado (la prueba directa de lo que (d) y (e) ejercitan por HTTP).
    fede4 = usuario_4i("cuatro-i-fede@ejemplo.com", "Cuatro-I-Fede-123!")
    _, h_f, v_f = auth.nuevo_refresh()
    sid_f = usuarios.crear_sesion(fede4["guid"], h_f, v_f, None, None)
    hash_vigente = usuarios.para_login("cuatro-i-fede@ejemplo.com")["hash"]
    usuarios.fijar_contrasena(fede4["guid"], auth.hash_de("Cuatro-I-Fede-Nueva-1!"), "CHANGE", sesion_guid=sid_f, hash_verificado=hash_vigente)
    rev("con la sesión viva y la contraseña vigente, el cambio se hace", usuarios.para_login("cuatro-i-fede@ejemplo.com")["hash"] != hash_vigente)

    def intento_de_cambio(**kwargs) -> str:
        """Lo que pasa al cambiar con esas condiciones: el motivo del rechazo, o 'ok'. Y si se rechazó, la contraseña NO cambió."""
        antes_hash = usuarios.para_login("cuatro-i-fede@ejemplo.com")["hash"]
        try:
            usuarios.fijar_contrasena(fede4["guid"], auth.hash_de("Cuatro-I-Fede-Otra-9!"), "CHANGE", **kwargs)
            return "ok"
        except usuarios.SesionNoCanjeable as e:
            return e.motivo if usuarios.para_login("cuatro-i-fede@ejemplo.com")["hash"] == antes_hash else "ESCRIBIÓ A PESAR DE RECHAZAR"

    rev("con una contraseña verificada que ya no es la vigente: motivo contrasena, y no escribe nada",
        intento_de_cambio(sesion_guid=sid_f, hash_verificado=hash_vigente) == "contrasena")
    hash_ahora = usuarios.para_login("cuatro-i-fede@ejemplo.com")["hash"]
    rev("con una sesión que no existe: motivo cerrada", intento_de_cambio(sesion_guid="no-existe", hash_verificado=hash_ahora) == "cerrada")
    rev("con la sesión de OTRA persona: motivo cerrada", intento_de_cambio(sesion_guid=vivas_de(ana4["guid"])[0]["sesionGuid"], hash_verificado=hash_ahora) == "cerrada")
    usuarios.revocar_sesiones(fede4["guid"], sid_f, "LOGOUT")
    rev("con la sesión ya revocada: motivo cerrada, y no escribe nada", intento_de_cambio(sesion_guid=sid_f, hash_verificado=hash_ahora) == "cerrada")
    rev("sin pedir revalidación (la recuperación por enlace), el cambio sigue siendo incondicional", intento_de_cambio() == "ok")

    # (g) Lo que 4h no miraba del canje.
    gaby4 = usuario_4i("cuatro-i-gaby@ejemplo.com", "Cuatro-I-Gaby-123!")
    _, h_g, v_g = auth.nuevo_refresh()
    sid_g = usuarios.crear_sesion(gaby4["guid"], h_g, v_g, None, None)
    refresh_g2, h_g2, v_g2 = auth.nuevo_refresh()
    sid_g2, _ = usuarios.rotar_sesion(h_g, h_g2, v_g2, None, None)
    _, _, ses_g = usuarios._indice()
    rev("rotar deja la sesión vieja revocada por ROTATED y la nueva viva", ses_g[sid_g]["revocadaPor"] == "ROTATED" and not ses_g[sid_g2]["revocadaEn"], str(ses_g[sid_g]["revocadaPor"]))
    usuarios.registro.agregar(usuarios.ARCHIVO, {"evento": "estado", "guid": gaby4["guid"], "activo": False, "motivo": "a mano (4i)", "en": ahora_iso()})
    r = c.post("/auth/refresh", json={"refreshToken": refresh_g2}, headers=srv())
    rev("renovar una sesión VIVA de una cuenta desactivada (sin revocarla): 401 sesion_invalida", r.status_code == 401 and r.json()["detail"]["codigo"] == "sesion_invalida", r.text[:160])
    usuarios.registro.agregar(usuarios.ARCHIVO, {"evento": "estado", "guid": gaby4["guid"], "activo": True, "motivo": "a mano (4i)", "en": ahora_iso()})
    refresh_v, h_v, _ = auth.nuevo_refresh()
    usuarios.crear_sesion(gaby4["guid"], h_v, datetime.now(timezone.utc) - timedelta(minutes=1), None, None)
    r = c.post("/auth/refresh", json={"refreshToken": refresh_v}, headers=srv())
    rev("renovar un refresh vencido: 401 sesion_vencida", r.status_code == 401 and r.json()["detail"]["codigo"] == "sesion_vencida", r.text[:160])

    # (h) El canje son DOS escrituras (revocar la vieja, crear la nueva): si falla alguna, nunca quedan dos sesiones.
    hugo4 = usuario_4i("cuatro-i-hugo@ejemplo.com", "Cuatro-I-Hugo-123!")
    agregar_original = usuarios.registro.agregar
    for cual, vivas_esperadas, que in ((1, 1, "la primera: no cambió nada, la sesión sigue viva"), (2, 0, "la segunda: queda CERRADA (debe volver a entrar), nunca con dos")):
        usuarios.cerrar_sesiones(hugo4["guid"], "limpiar 4i")
        refresh_h, h_h, v_h = auth.nuevo_refresh()
        usuarios.crear_sesion(hugo4["guid"], h_h, v_h, None, None)
        cuenta = {"n": 0}

        def agregar_que_falla(nombre, evento, cuenta=cuenta, cual=cual):
            cuenta["n"] += 1
            if cuenta["n"] == cual:
                raise ErrorAlmacen("disco lleno (prueba 4i)")
            return agregar_original(nombre, evento)

        usuarios.registro.agregar = agregar_que_falla
        try:
            r = c.post("/auth/refresh", json={"refreshToken": refresh_h}, headers=srv())
        finally:
            usuarios.registro.agregar = agregar_original
        rev(f"si falla la escritura {cual} del canje: 503, y {que}", r.status_code == 503 and len(vivas_de(hugo4["guid"])) == vivas_esperadas, f"{r.status_code} vivas={len(vivas_de(hugo4['guid']))}")

    # (i) y (j) El candado, de forma DETERMINISTA: un competidor que llega JUSTO antes de escribir debe esperar a que termine
    # quien ya está dentro. (Con hilos al azar la carrera solo se ve a veces; aquí se fuerza el entrelazado.)
    def con_competidor_antes_de_escribir(cuando, competidor):
        """Parchea `registro.agregar`: antes de escribir el primer evento que cumpla `cuando`, lanza `competidor` en otro hilo y le da
        0.5 s. Con el candado, el competidor se queda esperando y termina DESPUÉS de esa escritura; sin él, termina antes."""
        original = usuarios.registro.agregar
        hecho = {"hilo": None}

        def agregar(nombre, evento):
            if hecho["hilo"] is None and cuando(evento):
                hecho["hilo"] = threading.Thread(target=competidor)
                hecho["hilo"].start()
                hecho["hilo"].join(timeout=0.5)
            return original(nombre, evento)

        usuarios.registro.agregar = agregar
        return original, hecho

    nora4 = usuario_4i("cuatro-i-nora@ejemplo.com", "Cuatro-I-Nora-123!")
    hash_nora = usuarios.para_login("cuatro-i-nora@ejemplo.com")["hash"]
    original_agregar, hecho = con_competidor_antes_de_escribir(
        lambda e: e.get("evento") == "sesion" and e.get("guid") == nora4["guid"],
        lambda: usuarios.fijar_contrasena(nora4["guid"], auth.hash_de("Cuatro-I-Nora-Nueva-9!"), "RESET"),
    )
    try:
        _, h_n, v_n = auth.nuevo_refresh()
        usuarios.crear_sesion(nora4["guid"], h_n, v_n, None, None, hash_verificado=hash_nora)
    finally:
        usuarios.registro.agregar = original_agregar
    if hecho["hilo"]:
        hecho["hilo"].join(timeout=10)
    rev("un RESET que llega justo antes de escribir la sesión espera su turno y la revoca después: no sobrevive ninguna", not vivas_de(nora4["guid"]), str(len(vivas_de(nora4["guid"]))))

    omar4 = usuario_4i("cuatro-i-omar@ejemplo.com", "Cuatro-I-Omar-123!")
    _, h_o, v_o = auth.nuevo_refresh()
    sid_o = usuarios.crear_sesion(omar4["guid"], h_o, v_o, None, None)
    hash_omar = usuarios.para_login("cuatro-i-omar@ejemplo.com")["hash"]
    hash_reset_omar = auth.hash_de("Cuatro-I-Omar-Reset-9!")
    original_agregar, hecho = con_competidor_antes_de_escribir(
        lambda e: e.get("evento") == "contrasena" and e.get("motivo") == "CHANGE" and e.get("guid") == omar4["guid"],
        lambda: usuarios.fijar_contrasena(omar4["guid"], hash_reset_omar, "RESET"),
    )
    try:
        usuarios.fijar_contrasena(omar4["guid"], auth.hash_de("Cuatro-I-Omar-Ladron-9!"), "CHANGE", sesion_guid=sid_o, hash_verificado=hash_omar)
    finally:
        usuarios.registro.agregar = original_agregar
    if hecho["hilo"]:
        hecho["hilo"].join(timeout=10)
    rev("un RESET que llega justo antes de escribir el CAMBIO espera su turno: la contraseña final es la del restablecimiento",
        usuarios.para_login("cuatro-i-omar@ejemplo.com")["hash"] == hash_reset_omar)

    titulo("4j · Lo que el README promete del índice: el aviso de las reconstrucciones lentas y lo que se devuelve no se comparte")
    import logging as _logging

    class Recogedor(_logging.Handler):
        def __init__(self) -> None:
            super().__init__(_logging.WARNING)
            self.mensajes: list[str] = []

        def emit(self, registro_log) -> None:
            self.mensajes.append(registro_log.getMessage())

    recogedor = Recogedor()
    usuarios.logger.addHandler(recogedor)
    umbral_original = usuarios.UMBRAL_RECONSTRUCCION_S
    try:
        usuarios.UMBRAL_RECONSTRUCCION_S = 3600.0
        usuarios._cache_indice = None
        usuarios._indice()
        rev("una reconstrucción dentro del umbral NO avisa", not recogedor.mensajes, str(recogedor.mensajes))
        usuarios.UMBRAL_RECONSTRUCCION_S = 0.0
        usuarios._cache_indice = None
        usuarios._indice()
        rev("una que pasa del umbral avisa en el log (el registro provisional ya es grande)",
            any("Reconstruir el índice de usuarios" in m for m in recogedor.mensajes), str(recogedor.mensajes))
    finally:
        usuarios.UMBRAL_RECONSTRUCCION_S = umbral_original
        usuarios.logger.removeHandler(recogedor)

    luna4 = usuario_4i("cuatro-i-luna@ejemplo.com", "Cuatro-I-Luna-123!")
    usuarios.agregar_membresia(luna4["guid"], "tenant-4i", "ADMIN")
    usuarios.actualizar_perfil(luna4["guid"], "Luna", "Prueba", "Cuatro", None, {"email": "luna.recupera@ejemplo.com"})
    copia = usuarios.por_guid(luna4["guid"])
    copia["membresias"].append({"tenantGuid": "intruso", "rol": "ADMIN"})
    copia["membresias"][0]["rol"] = "VIEWER"
    copia["recuperacion"]["email"] = "intruso@ejemplo.com"
    limpia = usuarios.por_guid(luna4["guid"])
    rev("lo que devuelve por_guid se puede modificar sin tocar el índice (membresías y recuperación son copias)",
        limpia["membresias"] == [{"tenantGuid": "tenant-4i", "rol": "ADMIN"}] and limpia["recuperacion"] == {"email": "luna.recupera@ejemplo.com"},
        f"{limpia['membresias']} {limpia['recuperacion']}")

    titulo("4k · HU13 CA-4: cambiar la contraseña desde una sesión cierra las de los OTROS dispositivos, conserva la actual y deja PASSWORD_CHANGED")
    hugo = usuario_4i("cuatro-k-hugo@ejemplo.com", "Cuatro-K-Hugo-123!")
    sesiones_k = [login(c, "cuatro-k-hugo@ejemplo.com", "Cuatro-K-Hugo-123!").json() for _ in range(3)]
    actual_k, otra1_k, otra2_k = sesiones_k
    bearer_k = {**srv(), "Authorization": f"Bearer {actual_k['accessToken']}"}
    sid_actual_k = auth.verificar_acceso(actual_k["accessToken"])["sid"]
    cambios_antes = len(eventos_de_auditoria("PASSWORD_CHANGED"))
    NUEVA_K = "Cuatro-K-Hugo-Nueva-456!"

    def estado_de(sesion: dict) -> str:
        return c.get("/auth/sesion", headers={**srv(), "Authorization": f"Bearer {sesion['accessToken']}"}).json()["estado"]

    rev("antes del cambio, las tres sesiones viven", [estado_de(x) for x in sesiones_k] == ["viva"] * 3)
    r = c.post("/auth/contrasena", json={"actual": "Cuatro-K-Hugo-123!", "nueva": NUEVA_K, "confirmacion": NUEVA_K}, headers=bearer_k)
    rev("el cambio: 200 CHANGE, dice que cerró 2 OTRAS sesiones y que la de este dispositivo sigue",
        r.status_code == 200 and r.json()["motivo"] == "CHANGE" and r.json()["otrasSesionesCerradas"] == 2 and r.json()["sesionesCerradas"] is False, r.text[:200])
    rev("la sesión de ESTE dispositivo sigue viva y renueva", estado_de(actual_k) == "viva"
        and c.post("/auth/refresh", json={"refreshToken": actual_k["refreshToken"]}, headers=srv()).status_code == 200)
    rev("las de los OTROS dos dispositivos murieron: su JWT dice cerrada", estado_de(otra1_k) == "cerrada" and estado_de(otra2_k) == "cerrada")
    rev("y no pueden renovar: su refresh da 401",
        c.post("/auth/refresh", json={"refreshToken": otra1_k["refreshToken"]}, headers=srv()).status_code == 401
        and c.post("/auth/refresh", json={"refreshToken": otra2_k["refreshToken"]}, headers=srv()).status_code == 401)
    rev("la contraseña vieja ya no entra y la nueva sí",
        login(c, "cuatro-k-hugo@ejemplo.com", "Cuatro-K-Hugo-123!").status_code == 401 and login(c, "cuatro-k-hugo@ejemplo.com", NUEVA_K).status_code == 200)
    cambiados = eventos_de_auditoria("PASSWORD_CHANGED")
    rev("quedó UN evento PASSWORD_CHANGED, con la persona, la sesión desde la que se cambió, cuántas cerró y la hora",
        len(cambiados) == cambios_antes + 1 and cambiados[-1]["usuarioGuid"] == hugo["guid"] and cambiados[-1]["sesionGuid"] == sid_actual_k
        and cambiados[-1]["otrasSesionesCerradas"] == 2 and bool(cambiados[-1]["en"]), str(cambiados[-1:]))
    texto_auditoria = (RAIZ / ".registro" / "auditoria.jsonl").read_text(encoding="utf-8")
    rev("el evento NO lleva ninguna contraseña ni hash", NUEVA_K not in texto_auditoria and "Cuatro-K-Hugo-123!" not in texto_auditoria and "$argon2id$" not in texto_auditoria)

    # Lo que NO debe cerrar nada ni dejar evento: un cambio rechazado.
    def vivas_k() -> int:
        return len(vivas_de(hugo["guid"]))

    sesion_k = login(c, "cuatro-k-hugo@ejemplo.com", NUEVA_K).json()
    login(c, "cuatro-k-hugo@ejemplo.com", NUEVA_K)
    bearer_k2 = {**srv(), "Authorization": f"Bearer {sesion_k['accessToken']}"}
    vivas_antes, eventos_antes = vivas_k(), len(eventos_de_auditoria("PASSWORD_CHANGED"))
    rechazos = {
        "actual incorrecta (401)": ({"actual": "No-Es-La-Actual-9!", "nueva": "Cuatro-K-Otra-789!", "confirmacion": "Cuatro-K-Otra-789!"}, 401),
        "nueva igual a la actual (400)": ({"actual": NUEVA_K, "nueva": NUEVA_K, "confirmacion": NUEVA_K}, 400),
        "confirmación distinta (400)": ({"actual": NUEVA_K, "nueva": "Cuatro-K-Otra-789!", "confirmacion": "Cuatro-K-Distinta-1!"}, 400),
        "no cumple la política (400)": ({"actual": NUEVA_K, "nueva": "corta", "confirmacion": "corta"}, 400),
    }
    for nombre, (cuerpo, esperado) in rechazos.items():
        r = c.post("/auth/contrasena", json=cuerpo, headers=bearer_k2)
        rev(f"un cambio rechazado por {nombre}: no cierra ninguna sesión ni deja evento",
            r.status_code == esperado and vivas_k() == vivas_antes and len(eventos_de_auditoria("PASSWORD_CHANGED")) == eventos_antes, f"{r.status_code} {r.text[:120]}")

    # Con UNA sola sesión (la de este dispositivo): no hay otras que cerrar, y el cambio también se registra.
    for x in vivas_de(hugo["guid"]):
        if x["sesionGuid"] != auth.verificar_acceso(sesion_k["accessToken"])["sid"]:
            usuarios.revocar_sesiones(hugo["guid"], x["sesionGuid"], "PRUEBA")
    r = c.post("/auth/contrasena", json={"actual": NUEVA_K, "nueva": "Cuatro-K-Hugo-Otra-789!", "confirmacion": "Cuatro-K-Hugo-Otra-789!"}, headers=bearer_k2)
    rev("con una sola sesión: 200, otrasSesionesCerradas 0, y la sesión sigue viva",
        r.status_code == 200 and r.json()["otrasSesionesCerradas"] == 0 and estado_de(sesion_k) == "viva", r.text[:160])
    rev("y el cambio también se registra (con 0 sesiones cerradas)",
        len(eventos_de_auditoria("PASSWORD_CHANGED")) == eventos_antes + 1 and eventos_de_auditoria("PASSWORD_CHANGED")[-1]["otrasSesionesCerradas"] == 0)

    # Si el almacén falla a la mitad, no puede quedar la contraseña nueva con las sesiones viejas abiertas: las otras
    # sesiones se cierran ANTES de escribir la contraseña.
    from servicios.almacen import ErrorAlmacen as _ErrorAlmacen
    otra_d = login(c, "cuatro-k-hugo@ejemplo.com", "Cuatro-K-Hugo-Otra-789!").json()
    bearer_d = {**srv(), "Authorization": f"Bearer {otra_d['accessToken']}"}
    sid_d = auth.verificar_acceso(otra_d["accessToken"])["sid"]
    otras_d = [x["sesionGuid"] for x in vivas_de(hugo["guid"]) if x["sesionGuid"] != sid_d]
    hash_d_antes = usuarios.para_login("cuatro-k-hugo@ejemplo.com")["hash"]
    agregar_original = usuarios.registro.agregar

    def agregar_que_falla_con_la_contrasena(archivo, evento):
        if evento.get("evento") == "contrasena":
            raise _ErrorAlmacen("el NAS no contesta")
        return agregar_original(archivo, evento)

    usuarios.registro.agregar = agregar_que_falla_con_la_contrasena
    try:
        r = c.post("/auth/contrasena", json={"actual": "Cuatro-K-Hugo-Otra-789!", "nueva": "Cuatro-K-Hugo-Falla-321!", "confirmacion": "Cuatro-K-Hugo-Falla-321!"}, headers=bearer_d)
    finally:
        usuarios.registro.agregar = agregar_original
    rev("si el almacén falla al escribir la contraseña: 503, la contraseña NO cambió y las otras sesiones YA se cerraron",
        r.status_code == 503 and usuarios.para_login("cuatro-k-hugo@ejemplo.com")["hash"] == hash_d_antes
        and otras_d and all(x not in [v["sesionGuid"] for v in vivas_de(hugo["guid"])] for x in otras_d) and estado_de(otra_d) == "viva", f"{r.status_code} {r.text[:120]}")
    rev("y no quedó evento PASSWORD_CHANGED de un cambio que no se hizo", len(eventos_de_auditoria("PASSWORD_CHANGED")) == eventos_antes + 1)

    # El primer acceso NO deja PASSWORD_CHANGED (es otra historia, HU04): el evento es del cambio desde una sesión.
    primera = usuarios.bootstrap_admin("cuatro-k-primera@ejemplo.com", "Prueba", "Primera", auth.hash_de("Temporal-Larga-123!"))
    sesion_primera = login(c, "cuatro-k-primera@ejemplo.com", "Temporal-Larga-123!").json()
    cambios_k = len(eventos_de_auditoria("PASSWORD_CHANGED"))
    r = c.post("/auth/contrasena", json={"nueva": "Cuatro-K-Primera-456!", "confirmacion": "Cuatro-K-Primera-456!"},
               headers={**srv(), "Authorization": f"Bearer {sesion_primera['accessToken']}"})
    rev("el primer acceso: 200 FIRST_LOGIN, cierra todas (la suya incluida) y NO deja PASSWORD_CHANGED",
        r.status_code == 200 and r.json()["motivo"] == "FIRST_LOGIN" and r.json()["sesionesCerradas"] is True and r.json()["otrasSesionesCerradas"] == 0
        and len(eventos_de_auditoria("PASSWORD_CHANGED")) == cambios_k and estado_de(sesion_primera) == "cerrada", r.text[:200])

    # El servicio: CHANGE sin sesión desde la que se cambia no tiene nada que conservar, y los otros motivos cierran TODO.
    _, h_a, v_a = auth.nuevo_refresh()
    _, h_b, v_b = auth.nuevo_refresh()
    usuarios.crear_sesion(hugo["guid"], h_a, v_a, None, None)
    usuarios.crear_sesion(hugo["guid"], h_b, v_b, None, None)
    viva_antes_de_service = vivas_k()
    _, cerradas_sin_sesion = usuarios.fijar_contrasena_y_cerrar(hugo["guid"], auth.hash_de("Cuatro-K-Hugo-Final-1!"), "CHANGE")
    rev("el servicio: un CHANGE sin sesión desde la que se cambia cierra todas (no hay una que conservar)",
        cerradas_sin_sesion == viva_antes_de_service and vivas_k() == 0, f"{cerradas_sin_sesion} / {viva_antes_de_service}")
    usuarios.crear_sesion(hugo["guid"], h_a + "x", v_a, None, None)
    _, cerradas_reset = usuarios.fijar_contrasena_y_cerrar(hugo["guid"], auth.hash_de("Cuatro-K-Hugo-Final-2!"), "RESET")
    rev("y RESET y FIRST_LOGIN siguen cerrando TODAS, la actual incluida", cerradas_reset == 1 and vivas_k() == 0)

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
    r = c.post("/auth/perfil", json={"nombre": "Ada", "apellidoPaterno": "Lovelace",
                                     "recuperacion": {"email": "pepito", "telefono": None}}, headers=mio)
    rev("un correo de recuperación sin forma de correo: 400", r.status_code == 400 and r.json()["detail"]["codigo"] == "correo_recuperacion_invalido", r.text[:200])
    r = c.post("/auth/perfil", json={"nombre": "Ada", "apellidoPaterno": "Lovelace",
                                     "recuperacion": {"email": "  Respaldo@Ejemplo.COM  ", "telefono": None}}, headers=mio)
    rev("y uno con espacios y mayúsculas se guarda normalizado",
        r.json()["usuario"]["recuperacion"]["email"] == "respaldo@ejemplo.com", r.text[:200])
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
