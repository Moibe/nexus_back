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
    # Una reescritura EN SITIO del mismo tamaño no cambia la firma del archivo (el registro es de
    # solo agregar y esto lo contradice a propósito): se le avisa a la caché.
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

        def rotar_en_ciclo():
            while True:
                _, nuevo_hash, vence = auth.nuevo_refresh()
                try:
                    usuarios.rotar_sesion(estado_rot["hash"], nuevo_hash, vence, None, None)
                except usuarios.SesionNoCanjeable:
                    return
                estado_rot["hash"] = nuevo_hash

        hilo = threading.Thread(target=rotar_en_ciclo)
        hilo.start()
        time.sleep((ronda % 5) / 1000)
        usuarios.cerrar_sesiones(u["guid"], "cierre forzado de la prueba 4h")
        hilo.join(timeout=10)
        if hilo.is_alive() or vivas_de(u["guid"]):
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
