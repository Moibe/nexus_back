"""¿Sale la bienvenida con los accesos temporales (HU01, HU02, HU06)?

    .venv/Scripts/python verificar_invitaciones.py

OFFLINE y sin tocar nada tuyo, como los demás `verificar_*`: el servicio de correo se SIMULA (no se llama a
nadie). Lo que importa: que el correo traiga lo que dibuja el diseño y escape lo que escribió una persona; que
la contraseña temporal viaje SOLO por correo cuando el correo salió (y se muestre en pantalla, como antes,
cuando no salió); que lo que dice el correo sirva para entrar; que el alta deje su evento de auditoría sin la
contraseña; y que el script de bootstrap también la mande.
"""

import contextlib
import io
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-invitaciones-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ["CORREO_MODO"] = "archivo"
os.environ["FRONT_URL"] = "http://localhost:3400"
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

import config  # noqa: E402
from app import app  # noqa: E402
from servicios import auth, correo, invitaciones, plantillas_correo, usuarios  # noqa: E402

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


class Recolector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lineas: list[str] = []

    def emit(self, registro_log) -> None:
        self.lineas.append(registro_log.getMessage())


# Un canal de correo SIMULADO: se queda con lo que se le manda. `falla` lo hace tronar como el servicio real.
enviados: list[dict] = []
estado_canal = {"falla": False}
original_modo, original_enviar = correo.modo, correo.enviar


def enviar_falso(destino, asunto, texto, html=None, timeout=None):
    if estado_canal["falla"]:
        raise correo.ErrorCorreo("el servicio de correo no contestó (prueba)")
    enviados.append({"destino": destino, "asunto": asunto, "texto": texto, "html": html, "timeout": timeout})
    return "api://falso"


def canal_real() -> None:
    correo.modo = lambda: "api"
    correo.enviar = enviar_falso
    enviados.clear()
    estado_canal["falla"] = False


def sin_canal() -> None:
    correo.modo, correo.enviar = original_modo, original_enviar


def contrasena_del_correo(mensaje: dict) -> str:
    m = re.search(r"Contraseña temporal: (.+)", mensaje["texto"])
    return m.group(1).strip() if m else ""


def eventos(accion: str) -> list[dict]:
    ruta = RAIZ / ".registro" / "auditoria.jsonl"
    if not ruta.exists():
        return []
    lineas = (json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip())
    return [e for e in lineas if e.get("accion") == accion]


recolector = Recolector()
raiz_log = logging.getLogger()
nivel_previo = raiz_log.level
raiz_log.addHandler(recolector)
raiz_log.setLevel(logging.INFO)
c = TestClient(app)
try:
    titulo("1 · La plantilla: lo que dibuja el diseño, y lo que escribió una persona va escapado")
    enlace = "http://localhost:3400/acceso/iniciar-sesion"
    for perfil, frase in (
        ("super_admin", "Como Super Administrador, ahora cuentas con acceso al entorno de administración principal"),
        ("admin_organizacion", "Tu cuenta como administrador de Seguros Monterrey ha sido creada correctamente"),
        ("usuario", "Tu cuenta ha sido creada exitosamente por el administrador de tu organización"),
    ):
        asunto, texto, html = plantillas_correo.bienvenida("Ana", "ana@ejemplo.com", "Ab1&xyz", enlace, perfil, "Seguros Monterrey")
        rev(f"{perfil}: asunto, presentación del diseño, accesos, recomendación y botón",
            asunto == "Bienvenido a NexusDoc" and frase in texto and "Tus accesos temporales" in texto
            and "Usuario: ana@ejemplo.com" in texto and "Contraseña temporal: Ab1&xyz" in texto
            and "Por seguridad, te recomendamos cambiar tu contraseña después de iniciar sesión por primera vez." in texto
            and "Ir a portal Administrador" in html and "Hola, Ana." in texto)
    hostil = "<img src=x onerror=alert(1)>"
    asunto, texto, html = plantillas_correo.bienvenida(hostil, "a@e.com", "P&<\"'>", enlace, "admin_organizacion", "<b>Org</b>")
    rev("el nombre, la organización y la contraseña van ESCAPADOS en el HTML (no se puede meter marcado)",
        "<img" not in html and "<b>Org" not in html and "P&amp;&lt;&quot;" in html and "&lt;img" in html)
    rev("y en el texto plano van tal cual (no hay HTML que escapar)", "Contraseña temporal: P&<\"'>" in texto)
    rev("la línea visible y el botón llevan la MISMA dirección", html.count(enlace) >= 2 and f'href="{enlace}"' in html)
    try:
        plantillas_correo.bienvenida("A", "a@e.com", "x", enlace, "inventado")
        rev("un perfil que no existe se rechaza", False)
    except ValueError:
        rev("un perfil que no existe se rechaza", True)
    config.CORREO_SOPORTE = ""
    _, t_sin, _ = plantillas_correo.bienvenida("A", "a@e.com", "x", enlace, "usuario")
    config.CORREO_SOPORTE = "soporte@ejemplo.com"
    _, t_con, _ = plantillas_correo.bienvenida("A", "a@e.com", "x", enlace, "usuario")
    config.CORREO_SOPORTE = ""
    rev("el bloque de soporte solo sale si hay CORREO_SOPORTE", "Contactar soporte" not in t_sin and "soporte@ejemplo.com" in t_con)

    titulo("2 · El servicio: ENVIADO, SIN_CANAL o FALLO, y jamás levanta ni suelta la contraseña")
    sin_canal()
    rev("en modo archivo: SIN_CANAL, y no deja ningún .eml con la contraseña",
        invitaciones.enviar_bienvenida("a@ejemplo.com", "Ana", "Secreta-123!", "usuario") == invitaciones.SIN_CANAL
        and not (RAIZ / "correos").exists())
    canal_real()
    r = invitaciones.enviar_bienvenida("b@ejemplo.com", "Beto", "Secreta-456!", "usuario")
    rev("con canal real: ENVIADO, a la dirección y con el asunto del diseño",
        r == invitaciones.ENVIADO and len(enviados) == 1 and enviados[0]["destino"] == "b@ejemplo.com"
        and enviados[0]["asunto"] == "Bienvenido a NexusDoc", str(r))
    rev("lleva la contraseña y el enlace de ESTE ambiente (FRONT_URL)",
        "Secreta-456!" in enviados[0]["texto"] and f"{config.FRONT_URL}/acceso/iniciar-sesion" in enviados[0]["texto"])
    rev("y la espera está acotada (hay una persona mirando la pantalla)", enviados[0]["timeout"] == invitaciones.ESPERA_MAX_S)
    estado_canal["falla"] = True
    rev("si el servicio falla: FALLO (no levanta)",
        invitaciones.enviar_bienvenida("c@ejemplo.com", "Cris", "Secreta-789!", "usuario") == invitaciones.FALLO)
    sin_canal()
    correo.modo = lambda: "api"  # canal real pero SIN configurar: el correo de verdad truena y se vuelve FALLO
    previos = (config.CORREO_API_URL, config.CORREO_API_USUARIO, config.CORREO_API_CONTRASENA)
    config.CORREO_API_URL = config.CORREO_API_USUARIO = config.CORREO_API_CONTRASENA = ""
    rev("canal sin configurar: FALLO",
        invitaciones.enviar_bienvenida("d@ejemplo.com", "Dani", "Secreta-000!", "usuario") == invitaciones.FALLO)
    config.CORREO_API_URL, config.CORREO_API_USUARIO, config.CORREO_API_CONTRASENA = previos
    sin_canal()
    rev("para_respuesta: salió → sin contraseña",
        invitaciones.para_respuesta(invitaciones.ENVIADO, "X") == {"correoEnviado": True, "correoMotivo": None})
    rev("para_respuesta: no salió → con contraseña y el motivo",
        invitaciones.para_respuesta(invitaciones.FALLO, "X") == {"correoEnviado": False, "correoMotivo": "fallo", "contrasenaTemporal": "X"})
    log_hasta_aqui = " ".join(recolector.lineas)
    rev("el log dice a quién y qué pasó, pero NO las contraseñas",
        "b@ejemplo.com" in log_hasta_aqui and not any(p in log_hasta_aqui for p in ("Secreta-123!", "Secreta-456!", "Secreta-789!", "Secreta-000!")))

    titulo("3 · Las altas por HTTP: la contraseña viaja SOLO por correo cuando el correo salió")
    sin_canal()
    tmp_super = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(tmp_super))
    s = c.post("/auth/login", json={"email": "super@ejemplo.com", "password": tmp_super}, headers=srv()).json()
    bearer_super = {**srv(), "Authorization": f"Bearer {s['accessToken']}"}

    # (a) Un ambiente SIN canal real (archivo): todo como antes, y la respuesta dice por qué.
    r = c.post("/organizaciones/", json={"nombre": "Sin Canal SA", "adminNombre": "Sara", "adminApellidos": "Sin", "adminEmail": "sara@ejemplo.com"}, headers=bearer_super)
    rev("sin canal real: 201 con la contraseña en pantalla y el motivo sin_canal",
        r.status_code == 201 and r.json().get("contrasenaTemporal") and r.json()["correoEnviado"] is False
        and r.json()["correoMotivo"] == "sin_canal", r.text[:200])

    # (b) Con canal real: la contraseña NO se devuelve, y el correo lleva lo que sirve para entrar.
    canal_real()
    r = c.post("/organizaciones/", json={"nombre": "Seguros & Monterrey", "adminNombre": "Beto", "adminApellidos": "Admin", "adminEmail": "beto@ejemplo.com"}, headers=bearer_super)
    cuerpo = r.json()
    rev("con canal real: 201, correoEnviado y SIN contrasenaTemporal",
        r.status_code == 201 and cuerpo["correoEnviado"] is True and "contrasenaTemporal" not in cuerpo and cuerpo["correoMotivo"] is None, r.text[:300])
    rev("el correo salió al administrador, con el texto de HU02 y el nombre de la organización",
        len(enviados) == 1 and enviados[0]["destino"] == "beto@ejemplo.com"
        and "administrador de Seguros & Monterrey" in enviados[0]["texto"], str([e["destino"] for e in enviados]))
    pwd_beto = contrasena_del_correo(enviados[0]) if enviados else ""
    r = c.post("/auth/login", json={"email": "beto@ejemplo.com", "password": pwd_beto}, headers=srv())
    rev("la contraseña del correo SIRVE para entrar, y la cuenta nace obligada a cambiarla",
        r.status_code == 200 and r.json()["usuario"]["debeCambiarContrasena"] is True, r.text[:160])
    beto = r.json()
    bearer_beto = {**srv(), "Authorization": f"Bearer {beto['accessToken']}"}
    rev("la respuesta no se guarda en cachés (no-store)", c.post("/organizaciones/", json={"nombre": "Otra", "adminNombre": "O", "adminApellidos": "O", "adminEmail": "otra@ejemplo.com"}, headers=bearer_super).headers.get("cache-control") == "no-store")
    alta = [e for e in eventos("USER_CREATED") if e.get("usuarioGuid") == cuerpo["admin"]["guid"]]
    rev("el alta dejó su evento USER_CREATED: el creado, su rol, quién lo dio de alta y qué pasó con el correo",
        len(alta) == 1 and alta[0]["rol"] == "ADMIN" and alta[0]["actor"] == s["usuario"]["guid"]
        and alta[0]["correo"] == "enviado" and alta[0]["tenantGuid"] == cuerpo["organizacion"]["guid"], str(alta))
    rev("y el evento no lleva la contraseña", pwd_beto not in (RAIZ / ".registro" / "auditoria.jsonl").read_text(encoding="utf-8"))

    # (c) El administrador de la organización da de alta a un usuario (HU06).
    cambio = c.post("/auth/contrasena", json={"nueva": "Nueva-Contrasena-123!", "confirmacion": "Nueva-Contrasena-123!"}, headers=bearer_beto)
    admin_org = c.post("/auth/login", json={"email": "beto@ejemplo.com", "password": "Nueva-Contrasena-123!"}, headers=srv()).json()
    bearer_admin = {**srv(), "Authorization": f"Bearer {admin_org['accessToken']}"}
    enviados.clear()
    r = c.post("/usuarios/", json={"nombre": hostil, "email": "camila@ejemplo.com", "rol": "ANALISTA"}, headers=bearer_admin)
    rev("alta de usuario con canal real: 201, correoEnviado y sin contraseña en la respuesta",
        r.status_code == 201 and r.json()["correoEnviado"] is True and "contrasenaTemporal" not in r.json(), r.text[:300])
    rev("el correo lleva el texto de HU06 y el nombre ESCAPADO en el HTML",
        len(enviados) == 1 and "creada exitosamente por el administrador de tu organización" in enviados[0]["texto"]
        and "<img" not in enviados[0]["html"], str(enviados[:1])[:200])
    pwd_camila = contrasena_del_correo(enviados[0]) if enviados else ""
    rev("con la contraseña del correo, la usuaria entra",
        c.post("/auth/login", json={"email": "camila@ejemplo.com", "password": pwd_camila}, headers=srv()).status_code == 200)
    alta = [e for e in eventos("USER_CREATED") if e.get("rol") == "ANALISTA"]
    rev("y su alta quedó auditada con el administrador responsable", len(alta) == 1 and alta[0]["actor"] == admin_org["usuario"]["guid"], str(alta))

    # (d) El servicio falla: se vuelve al comportamiento de antes (contraseña en pantalla) y la pantalla lo sabe.
    estado_canal["falla"] = True
    enviados.clear()
    r = c.post("/usuarios/", json={"nombre": "Dora", "email": "dora@ejemplo.com", "rol": "VIEWER"}, headers=bearer_admin)
    rev("si el correo falla: 201 igual, con la contraseña en pantalla y el motivo fallo",
        r.status_code == 201 and r.json()["correoEnviado"] is False and r.json()["correoMotivo"] == "fallo"
        and r.json().get("contrasenaTemporal"), r.text[:200])
    rev("y esa contraseña sirve para entrar",
        c.post("/auth/login", json={"email": "dora@ejemplo.com", "password": r.json().get("contrasenaTemporal", "")}, headers=srv()).status_code == 200)
    alta = [e for e in eventos("USER_CREATED") if e.get("correo") == "fallo"]
    rev("el alta quedó auditada con correo=fallo", len(alta) == 1, str(alta))
    sin_canal()
    todo_el_log = " ".join(recolector.lineas)
    rev("ninguna contraseña (la de beto, camila ni dora) llegó al log", not any(p and p in todo_el_log for p in (pwd_beto, pwd_camila, r.json().get("contrasenaTemporal"))))

    titulo("4 · El script de bootstrap (HU01): imprime SIEMPRE la contraseña y, si hay canal real, también la manda")
    import bootstrap_admin

    def correr_bootstrap(*argumentos: str) -> tuple[int, str]:
        previos_argv = sys.argv
        sys.argv = ["bootstrap_admin.py", *argumentos]
        salida = io.StringIO()
        try:
            with contextlib.redirect_stdout(salida):
                rc = bootstrap_admin.main()
        finally:
            sys.argv = previos_argv
        return rc, salida.getvalue()

    canal_real()
    rc, texto_salida = correr_bootstrap("--email", "nuevo.super@ejemplo.com", "--nombre", "Nuria", "--apellidos", "Super Admin")
    pwd_impresa = (re.search(r"Contraseña temporal: (\S+)", texto_salida) or [None, ""])[1]
    rev("con canal real: crea al super admin, imprime la contraseña y dice que mandó el correo",
        rc == 0 and pwd_impresa and "Correo de bienvenida: enviado a nuevo.super@ejemplo.com" in texto_salida, texto_salida[:300])
    rev("el correo salió a su dirección con el texto de HU01 y la MISMA contraseña que se imprimió",
        len(enviados) == 1 and enviados[0]["destino"] == "nuevo.super@ejemplo.com"
        and "Como Super Administrador" in enviados[0]["texto"] and contrasena_del_correo(enviados[0]) == pwd_impresa)
    rev("y con ella entra", c.post("/auth/login", json={"email": "nuevo.super@ejemplo.com", "password": pwd_impresa}, headers=srv()).status_code == 200)
    enviados.clear()
    rc, texto_salida = correr_bootstrap("--email", "sin.correo@ejemplo.com", "--nombre", "Saúl", "--apellidos", "Sin", "--sin-correo")
    rev("con --sin-correo: crea, imprime la contraseña y NO manda nada",
        rc == 0 and "Contraseña temporal:" in texto_salida and "omitido" in texto_salida and not enviados, texto_salida[:300])
    estado_canal["falla"] = True
    rc, texto_salida = correr_bootstrap("--email", "falla@ejemplo.com", "--nombre", "Fer", "--apellidos", "Falla")
    rev("si el correo falla: crea la cuenta, imprime la contraseña y lo dice (NO SE PUDO ENVIAR)",
        rc == 0 and "Contraseña temporal:" in texto_salida and "NO SE PUDO ENVIAR" in texto_salida, texto_salida[:300])
    estado_canal["falla"] = False
    enviados.clear()
    rc, texto_salida = correr_bootstrap("--email", "nuevo.super@ejemplo.com", "--nombre", "Otra", "--apellidos", "Vez")
    rev("con un correo ya registrado no crea nada, no imprime contraseña y no manda nada",
        rc == 2 and "Contraseña temporal:" not in texto_salida and not enviados, texto_salida[:200])
    sin_canal()
    rc, texto_salida = correr_bootstrap("--email", "archivo@ejemplo.com", "--nombre", "Ari", "--apellidos", "Archivo")
    rev("en un ambiente sin canal real: imprime la contraseña y avisa que no se mandó",
        rc == 0 and "Contraseña temporal:" in texto_salida and "NO se mandó" in texto_salida, texto_salida[:300])

    titulo("5 · La contraseña temporal caduca a las 48 horas (HU06 CA-5)")
    from datetime import datetime, timedelta, timezone

    reloj_real = usuarios._ahora

    def viajar(horas: float) -> None:
        """El reloj del registro de usuarios marca `horas` más tarde (el del JWT, el de `auth`, no se mueve)."""
        usuarios._ahora = lambda: reloj_real() + timedelta(hours=horas)

    def volver() -> None:
        usuarios._ahora = reloj_real

    def entrar(correo: str, contrasena: str):
        return c.post("/auth/login", json={"email": correo, "password": contrasena}, headers=srv())

    def sesiones_vivas(guid: str) -> int:
        _, _, ses = usuarios._indice()
        return sum(1 for x in ses.values() if x["guid"] == guid and not x["revocadaEn"])

    # La regla sola, con fechas exactas.
    ahora_fijo = datetime(2026, 10, 13, 12, 0, tzinfo=timezone.utc)
    base = {"debeCambiarContrasena": True, "esAdminPlataforma": False}

    def con(horas_atras: float, **extra) -> dict:
        return {**base, **extra, "temporalEmitidaEn": (ahora_fijo - timedelta(hours=horas_atras)).isoformat()}

    rev("a las 47 h 59 min todavía sirve", not usuarios.acceso_vencido(con(47.99), ahora_fijo))
    rev("a las 48 h 01 min ya caducó", usuarios.acceso_vencido(con(48.02), ahora_fijo))
    rev("quien ya hizo su primer acceso no tiene nada que caducar", not usuarios.acceso_vencido(con(900, debeCambiarContrasena=False), ahora_fijo))
    rev("el administrador de PLATAFORMA no caduca (no habría quien le generara otro desde pantalla)", not usuarios.acceso_vencido(con(900, esAdminPlataforma=True), ahora_fijo))
    rev("una fecha ilegible no inventa un vencimiento", not usuarios.acceso_vencido({**base, "temporalEmitidaEn": "no-es-fecha"}, ahora_fijo))
    config.TOKEN_ACTIVACION_HORAS = 0
    rev("TOKEN_ACTIVACION_HORAS=0 apaga la caducidad", not usuarios.acceso_vencido(con(900), ahora_fijo))
    config.TOKEN_ACTIVACION_HORAS = 0.5
    rev("y la vigencia puede ser fraccionaria (media hora): 29 min sirve, 31 min no",
        not usuarios.acceso_vencido(con(29 / 60), ahora_fijo) and usuarios.acceso_vencido(con(31 / 60), ahora_fijo))
    config.TOKEN_ACTIVACION_HORAS = 48

    # Por HTTP, con un usuario que el administrador de la organización acaba de dar de alta.
    sin_canal()
    r = c.post("/usuarios/", json={"nombre": "Gael", "email": "gael@ejemplo.com", "rol": "VIEWER"}, headers=bearer_admin)
    pwd_gael = r.json()["contrasenaTemporal"]
    guid_gael = r.json()["usuario"]["guid"]
    viajar(47)
    r = entrar("gael@ejemplo.com", pwd_gael)
    rev("a las 47 horas entra (y se le obliga a cambiarla), sin que la respuesta suelte la fecha de emisión",
        r.status_code == 200 and r.json()["usuario"]["debeCambiarContrasena"] is True and "temporalEmitidaEn" not in r.json()["usuario"], r.text[:160])
    token_gael = {**srv(), "Authorization": f"Bearer {r.json()['accessToken']}"}
    viajar(49)
    r = entrar("gael@ejemplo.com", pwd_gael)
    detalle = r.json().get("detail", {})
    rev("a las 49 horas, con la contraseña CORRECTA: 403 activacion_vencida con el texto del diseño",
        r.status_code == 403 and detalle.get("codigo") == "activacion_vencida" and "48 horas" in detalle.get("mensaje", ""), r.text[:200])
    r = entrar("gael@ejemplo.com", "Una-Contrasena-Equivocada-1!")
    rev("con una contraseña EQUIVOCADA sigue siendo 401 credenciales (no se le cuenta a quien no la tiene)",
        r.status_code == 401 and r.json()["detail"]["codigo"] == "credenciales", r.text[:160])
    fallidos_antes = usuarios.para_login("gael@ejemplo.com")["fallidos"]
    codigos = [entrar("gael@ejemplo.com", pwd_gael).status_code for _ in range(6)]
    rev("seis intentos más con la correcta: siempre 403, nunca 423 (caducar no cuenta como intento fallido)", codigos == [403] * 6, str(codigos))
    rev("y la cuenta no acumuló intentos fallidos", usuarios.para_login("gael@ejemplo.com")["fallidos"] == fallidos_antes)
    ultimo = [e for e in usuarios.registro.eventos(usuarios.ARCHIVO) if e.get("evento") == "intento" and e.get("guid") == guid_gael][-1]
    rev("el intento quedó en la bitácora como EXPIRED", ultimo["exitoso"] is False and ultimo["motivo"] == "EXPIRED", str(ultimo))
    r = c.post("/auth/contrasena", json={"nueva": "Nueva-Contrasena-456!", "confirmacion": "Nueva-Contrasena-456!"}, headers=token_gael)
    rev("quien entró a tiempo pero NO terminó tampoco puede completar el primer acceso después: 403",
        r.status_code == 403 and r.json()["detail"]["codigo"] == "activacion_vencida", r.text[:160])
    rev("y sus sesiones quedaron cerradas", sesiones_vivas(guid_gael) == 0)
    volver()
    # Los demás perfiles.
    sin_canal()
    r = c.post("/organizaciones/", json={"nombre": "Caduca SA", "adminNombre": "Cata", "adminApellidos": "Caduca", "adminEmail": "cata@ejemplo.com"}, headers=bearer_super)
    pwd_cata = r.json()["contrasenaTemporal"]
    viajar(49)
    rev("el administrador de una ORGANIZACIÓN (HU02) también caduca",
        entrar("cata@ejemplo.com", pwd_cata).status_code == 403)
    viajar(400)
    rev("el superadmin creado por línea de comandos NO caduca, ni a las 400 horas",
        entrar("nuevo.super@ejemplo.com", pwd_impresa).status_code == 200)
    volver()
    r = c.post("/usuarios/", json={"nombre": "Hana", "email": "hana@ejemplo.com", "rol": "VIEWER"}, headers=bearer_admin)
    pwd_hana = r.json()["contrasenaTemporal"]
    h = entrar("hana@ejemplo.com", pwd_hana).json()
    c.post("/auth/contrasena", json={"nueva": "Hana-Definitiva-123!", "confirmacion": "Hana-Definitiva-123!"}, headers={**srv(), "Authorization": f"Bearer {h['accessToken']}"})
    viajar(400)
    rev("quien YA hizo su primer acceso entra normal a las 400 horas", entrar("hana@ejemplo.com", "Hana-Definitiva-123!").status_code == 200)
    volver()
    config.TOKEN_ACTIVACION_HORAS = 0.5
    r = c.post("/usuarios/", json={"nombre": "Ivo", "email": "ivo@ejemplo.com", "rol": "VIEWER"}, headers=bearer_admin)
    pwd_ivo = r.json()["contrasenaTemporal"]
    viajar(0.4)
    ok_antes = entrar("ivo@ejemplo.com", pwd_ivo).status_code
    viajar(0.6)
    rev("con TOKEN_ACTIVACION_HORAS=0.5: a los 24 min entra y a los 36 min ya no (así se prueba sin esperar dos días)",
        ok_antes == 200 and entrar("ivo@ejemplo.com", pwd_ivo).status_code == 403)
    volver()
    config.TOKEN_ACTIVACION_HORAS = 48

    titulo("6 · Reemitir la contraseña a quien no entró a tiempo (reenviar_invitacion.py)")
    # Gael lleva 49 horas sin poder entrar: se le genera una contraseña NUEVA.
    canal_real()
    viajar(49)
    resultado, nueva_pwd = invitaciones.reenviar(guid_gael, actor_guid=s["usuario"]["guid"])
    rev("reenviar: el correo salió, con la contraseña NUEVA y el texto de HU06",
        resultado == invitaciones.ENVIADO and len(enviados) == 1 and enviados[0]["destino"] == "gael@ejemplo.com"
        and contrasena_del_correo(enviados[0]) == nueva_pwd and nueva_pwd != pwd_gael
        and "creada exitosamente por el administrador de tu organización" in enviados[0]["texto"])
    rev("la contraseña ANTERIOR ya no sirve", entrar("gael@ejemplo.com", pwd_gael).status_code == 401)
    r = entrar("gael@ejemplo.com", nueva_pwd)
    rev("la nueva sirve, y las horas empezaron otra vez (a las 49 h del reloj ya no está caducada)",
        r.status_code == 200 and r.json()["usuario"]["debeCambiarContrasena"] is True, r.text[:160])
    viajar(49 + 47)
    rev("a las 47 horas de la reemisión todavía sirve", entrar("gael@ejemplo.com", nueva_pwd).status_code == 200)
    viajar(49 + 49)
    rev("y a las 49 horas de la reemisión vuelve a caducar", entrar("gael@ejemplo.com", nueva_pwd).status_code == 403)
    alta = [e for e in eventos("INVITATION_REISSUED") if e.get("usuarioGuid") == guid_gael]
    rev("quedó el evento INVITATION_REISSUED con quién la pidió y qué pasó con el correo, sin la contraseña",
        len(alta) == 1 and alta[0]["actor"] == s["usuario"]["guid"] and alta[0]["correo"] == "enviado"
        and nueva_pwd not in (RAIZ / ".registro" / "auditoria.jsonl").read_text(encoding="utf-8"), str(alta))
    volver()

    # Las sesiones abiertas con la contraseña anterior mueren.
    sin_canal()
    r = c.post("/usuarios/", json={"nombre": "Jana", "email": "jana@ejemplo.com", "rol": "VIEWER"}, headers=bearer_admin)
    guid_jana, pwd_jana = r.json()["usuario"]["guid"], r.json()["contrasenaTemporal"]
    entrar("jana@ejemplo.com", pwd_jana)
    rev("Jana tiene una sesión abierta con su contraseña temporal", sesiones_vivas(guid_jana) == 1)
    invitaciones.reenviar(guid_jana, actor_guid=None)
    rev("al reemitirle la contraseña esa sesión se cierra", sesiones_vivas(guid_jana) == 0)
    rev("y se levantan los bloqueos: no queda bloqueada ni con intentos", usuarios.para_login("jana@ejemplo.com")["bloqueadoHasta"] is None)

    # A quién NO se le puede reemitir.
    try:
        invitaciones.reenviar(cuerpo["admin"]["guid"], actor_guid=None)  # beto, que ya puso su contraseña
        rev("a quien ya hizo su primer acceso NO se le reemite (NoPendiente)", False)
    except usuarios.NoPendiente:
        rev("a quien ya hizo su primer acceso NO se le reemite (NoPendiente)", True)
    try:
        invitaciones.reenviar("no-existe", actor_guid=None)
        rev("a una cuenta que no existe, tampoco (NoEncontrado)", False)
    except usuarios.NoEncontrado:
        rev("a una cuenta que no existe, tampoco (NoEncontrado)", True)

    # El script.
    import reenviar_invitacion

    def correr_reenvio(*argumentos: str) -> tuple[int, str, str]:
        previos_argv = sys.argv
        sys.argv = ["reenviar_invitacion.py", *argumentos]
        salida, errores = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(salida), contextlib.redirect_stderr(errores):
                rc = reenviar_invitacion.main()
        finally:
            sys.argv = previos_argv
        return rc, salida.getvalue(), errores.getvalue()

    canal_real()
    viajar(60)
    rc, out, err = correr_reenvio("--email", "cata@ejemplo.com")
    pwd_script = (re.search(r"Contraseña temporal: (\S+)", out) or [None, ""])[1]
    rev("el script: imprime la contraseña nueva y dice que mandó el correo (al administrador de la organización)",
        rc == 0 and pwd_script and "enviado a cata@ejemplo.com" in out and len(enviados) == 1
        and "administrador de Caduca SA" in enviados[0]["texto"] and contrasena_del_correo(enviados[0]) == pwd_script, out[:300])
    rev("y con ella la administradora vuelve a poder entrar", entrar("cata@ejemplo.com", pwd_script).status_code == 200)
    rc, out, err = correr_reenvio("--email", "hana@ejemplo.com")
    rev("el script se niega con quien ya hizo su primer acceso, sin imprimir contraseña",
        rc == 2 and "ya hizo su primer acceso" in err and "Contraseña temporal" not in out)
    rc, out, err = correr_reenvio("--email", "nadie@ejemplo.com")
    rev("y con un correo que no existe", rc == 2 and "No existe" in err)
    volver()
    sin_canal()
    todo_el_log = " ".join(recolector.lineas)
    rev("ninguna contraseña nueva llegó al log", not any(p and p in todo_el_log for p in (nueva_pwd, pwd_script, pwd_gael, pwd_cata)))

finally:
    sin_canal()
    raiz_log.removeHandler(recolector)
    raiz_log.setLevel(nivel_previo)
    c.close()
    shutil.rmtree(RAIZ, ignore_errors=True)

print("\n" + "=" * 72)
print(f"{fallos} FALLARON" if fallos else "todas las comprobaciones pasaron")
raise SystemExit(1 if fallos else 0)
