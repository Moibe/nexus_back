"""Comprueba la maquinaria del correo: el envío con costura y los enlaces de
un solo uso. No toca la red ni necesita servidor de correo.

    .venv/Scripts/python verificar_correo.py

Corre sobre un `ALMACEN_RUTA` temporal con su centinela, como los demás
verificadores, así que no ensucia el almacén de trabajo.
"""

import email
import email.policy
import os
import shutil
import sys
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="nexus-correo-"))
(RAIZ / ".nexus-almacen").write_text("", encoding="utf-8")
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["CORREO_MODO"] = "archivo"
os.environ["FRONT_URL"] = "http://localhost:3400"
os.environ.setdefault("AUTH_JWT_SECRET", "x" * 48)

import config  # noqa: E402
from servicios import correo, tokens  # noqa: E402

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    if ok:
        print(f"  OK    {descripcion}")
    else:
        fallos += 1
        print(f"  FALLA {descripcion}" + (f"  -> {extra}" if extra else ""))


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


try:
    titulo("1 · Envío en modo archivo: el correo queda en disco, sin red")
    rev("el modo por default es archivo", correo.modo() == "archivo", correo.modo())
    rev("y se considera configurado", correo.esta_configurado())
    ruta = correo.enviar(
        "ana@ejemplo.com",
        "Recupera tu acceso a NexusDoc",
        "Abre este enlace: http://localhost:3400/acceso/recuperar/ABC123",
    )
    archivo = Path(ruta)
    rev("escribe un .eml dentro del almacén", archivo.exists() and archivo.suffix == ".eml", ruta)
    rev("en la carpeta correos/", archivo.parent.name == "correos", str(archivo.parent))
    # `policy=default` o devuelve el objeto viejo, sin get_content().
    mensaje = email.message_from_bytes(archivo.read_bytes(), policy=email.policy.default)
    rev("con el destinatario correcto", mensaje["To"] == "ana@ejemplo.com", str(mensaje["To"]))
    rev("y el asunto", mensaje["Subject"] == "Recupera tu acceso a NexusDoc", str(mensaje["Subject"]))
    cuerpo = mensaje.get_content()
    rev("el enlace viaja en el cuerpo", "ABC123" in cuerpo, cuerpo[:80])
    rev("el nombre del archivo deja ver a quién fue", "ana" in archivo.name, archivo.name)

    titulo("2 · Un destinatario inválido no se manda")
    for malo in ["", "pepito", None]:
        try:
            correo.enviar(malo, "x", "y")  # type: ignore[arg-type]
            rev(f"rechaza {malo!r}", False)
        except correo.ErrorCorreo:
            rev(f"rechaza {malo!r}", True)

    titulo("3 · Modo silencio: no escribe nada")
    antes = len(list((RAIZ / "correos").glob("*.eml")))
    config.CORREO_MODO = "silencio"
    rev("devuelve 'silencio'", correo.enviar("ana@ejemplo.com", "x", "y") == "silencio")
    rev("y no dejó archivo nuevo", len(list((RAIZ / "correos").glob("*.eml"))) == antes)
    config.CORREO_MODO = "archivo"

    titulo("4 · Modo smtp sin host: se considera NO configurado y falla claro")
    config.CORREO_MODO = "smtp"
    rev("esta_configurado() dice que no", not correo.esta_configurado())
    try:
        correo.enviar("ana@ejemplo.com", "x", "y")
        rev("enviar levanta ErrorCorreo", False)
    except correo.ErrorCorreo as exc:
        rev("enviar levanta ErrorCorreo", True)
        rev("y el error NO trae el cuerpo del correo", "y" != str(exc) and "CORREO_SMTP_HOST" in str(exc), str(exc)[:90])
    config.CORREO_MODO = "archivo"

    titulo("5 · Enlaces de un solo uso: se crean con su vigencia")
    ana = "guid-ana"
    token, expira = tokens.crear(ana, tokens.RECUPERACION)
    rev("devuelve un token largo y al azar", len(token) >= 32, f"{len(token)} caracteres")
    horas = (expira - tokens._ahora()).total_seconds() / 3600
    rev("recuperación vence en ~1 hora", 0.9 < horas <= 1.0, f"{horas:.2f} h")
    t_act, expira_act = tokens.crear(ana, tokens.ACTIVACION)
    horas_act = (expira_act - tokens._ahora()).total_seconds() / 3600
    rev("activación vence en ~48 horas", 47.9 < horas_act <= 48.0, f"{horas_act:.2f} h")
    registro_crudo = (RAIZ / ".registro" / "tokens.jsonl").read_text(encoding="utf-8")
    rev("el token EN CLARO no queda en el registro: solo su hash",
        token not in registro_crudo and tokens.hash_de(token) in registro_crudo)

    titulo("6 · Revisar no lo gasta; consumir sí")
    rev("revisar lo da por bueno", tokens.revisar(token, tokens.RECUPERACION)["usuarioGuid"] == ana)
    rev("y sigue sirviendo después de revisar", tokens.revisar(token, tokens.RECUPERACION)["usuarioGuid"] == ana)
    rev("consumir devuelve de quién es", tokens.consumir(token, tokens.RECUPERACION)["usuarioGuid"] == ana)
    try:
        tokens.consumir(token, tokens.RECUPERACION)
        rev("usarlo dos veces se rechaza", False)
    except tokens.YaUsado:
        rev("usarlo dos veces se rechaza", True)
    try:
        tokens.revisar(token, tokens.RECUPERACION)
        rev("y la pantalla también lo ve gastado", False)
    except tokens.YaUsado:
        rev("y la pantalla también lo ve gastado", True)

    titulo("7 · El tipo importa: un enlace de activación no sirve para recuperar")
    try:
        tokens.consumir(t_act, tokens.RECUPERACION)
        rev("no se puede cruzar el tipo", False)
    except tokens.TokenInvalido:
        rev("no se puede cruzar el tipo", True)
    rev("pero con su propio tipo sí sirve", tokens.consumir(t_act, tokens.ACTIVACION)["usuarioGuid"] == ana)

    titulo("8 · Pedir uno nuevo invalida el anterior (HU06 CA-6)")
    viejo, _ = tokens.crear(ana, tokens.ACTIVACION)
    nuevo, _ = tokens.crear(ana, tokens.ACTIVACION)
    try:
        tokens.consumir(viejo, tokens.ACTIVACION)
        rev("el enlace viejo deja de servir", False)
    except tokens.YaUsado:
        rev("el enlace viejo deja de servir", True)
    rev("y el nuevo sí sirve", tokens.consumir(nuevo, tokens.ACTIVACION)["usuarioGuid"] == ana)

    titulo("9 · Un token vencido se rechaza")
    import json as _json
    vencido, _ = tokens.crear(ana, tokens.RECUPERACION)
    ruta_reg = RAIZ / ".registro" / "tokens.jsonl"
    lineas = []
    for linea in ruta_reg.read_text(encoding="utf-8").splitlines():
        e = _json.loads(linea)
        if e.get("evento") == "creado" and e.get("hash") == tokens.hash_de(vencido):
            e["expira"] = "2020-01-01T00:00:00+00:00"
        lineas.append(_json.dumps(e, ensure_ascii=False))
    ruta_reg.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    try:
        tokens.consumir(vencido, tokens.RECUPERACION)
        rev("el vencido se rechaza", False)
    except tokens.Vencido:
        rev("el vencido se rechaza", True)

    titulo("10 · Invalidar los pendientes de alguien")
    tokens.crear(ana, tokens.RECUPERACION)
    tokens.crear(ana, tokens.ACTIVACION)
    otro, _ = tokens.crear("guid-luis", tokens.RECUPERACION)
    rev("ana tiene dos pendientes", len(tokens.pendientes_de(ana)) == 2, str(len(tokens.pendientes_de(ana))))
    rev("invalidar los suyos apaga dos", tokens.invalidar_de(ana, motivo="CONTRASENA_CAMBIADA") == 2)
    rev("y ya no le queda ninguno", tokens.pendientes_de(ana) == [])
    rev("el de otra persona no se tocó", len(tokens.pendientes_de("guid-luis")) == 1)
    try:
        tokens.consumir("esto-me-lo-invente", tokens.RECUPERACION)
        rev("un token inventado se rechaza", False)
    except tokens.TokenInvalido:
        rev("un token inventado se rechaza", True)

    titulo("11 · Tipo desconocido")
    try:
        tokens.crear(ana, "LO_QUE_SEA")
        rev("crear con un tipo que no existe truena", False)
    except ValueError:
        rev("crear con un tipo que no existe truena", True)

    titulo("12 · Modo api: el servicio HTTP de notificaciones (contra un servidor FALSO en localhost)")
    import base64
    import json
    import logging
    import socket
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    USUARIO, CONTRASENA = "usuario-de-prueba-xyz", "contrasena-de-prueba-xyz"
    CUERPO_ENLACE = "<p>Abre http://localhost:3400/acceso/restablecer-contrasena?t=TOKEN-SECRETO-123</p>"
    estado: dict = {}

    def reiniciar(**cambios) -> None:
        estado.clear()
        estado.update(
            tokens_pedidos=0, intentos_de_envio=0, recibidos=[], vigente=None, n_token=0,
            token="ok", envio="ok", vida_token=3600, formato_token="jwt",
            siguio_la_redireccion=0, codigo_rechazo=401, demora_token=0.0,
        )
        estado.update(cambios)
        correo._token.update(valor=None, vence=0.0)

    def jwt_falso(n: int, vida: float) -> str:
        def b64(o) -> str:
            return base64.urlsafe_b64encode(json.dumps(o).encode()).decode().rstrip("=")
        return f"{b64({'alg': 'HS256', 'typ': 'JWT'})}.{b64({'exp': time.time() + vida, 'n': n})}.firma"

    class Falso(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def _responder(self, codigo: int, cuerpo: dict) -> None:
            datos = json.dumps(cuerpo).encode()
            try:
                self.send_response(codigo)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(datos)))
                self.end_headers()
                self.wfile.write(datos)
            except OSError:
                pass  # el cliente ya se fue (por ejemplo, por su timeout)

        def _responder_crudo(self, codigo: int, tipo: str, datos: bytes) -> None:
            try:
                self.send_response(codigo)
                self.send_header("Content-Type", tipo)
                self.send_header("Content-Length", str(len(datos)))
                self.end_headers()
                self.wfile.write(datos)
            except OSError:
                pass

        def do_POST(self) -> None:
            datos = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path == "/OAuth/Otro":
                estado["siguio_la_redireccion"] += 1
                return self._responder(200, {"count": 1, "results": {"accessToken": jwt_falso(99, 3600)}, "error": False})
            if self.path == "/OAuth/AuthToken":
                estado["tokens_pedidos"] += 1
                if estado["demora_token"]:
                    time.sleep(estado["demora_token"])
                if estado["token"] == "redirige":
                    self.send_response(307)
                    self.send_header("Location", "/OAuth/Otro")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if estado["token"] == "401":
                    return self._responder(401, {"error": True, "msgResponse": "no autorizado"})
                if estado["token"] == "error200":
                    # A propósito: repite lo que le mandaron, como haría un servicio descuidado.
                    return self._responder(200, {"count": 0, "results": None, "error": True,
                                                 "msgResponse": f"credenciales invalidas {datos.get('usr')} {datos.get('psd')}"})
                if datos.get("usr") != USUARIO or datos.get("psd") != CONTRASENA:
                    return self._responder(401, {"error": True, "msgResponse": "credenciales invalidas"})
                estado["n_token"] += 1
                token = jwt_falso(estado["n_token"], estado["vida_token"]) if estado["formato_token"] == "jwt" else f"opaco-{estado['n_token']}"
                estado["vigente"] = token
                return self._responder(200, {"count": 1, "results": {"accessToken": token}, "error": False, "msgResponse": "ok"})
            if self.path == "/II_1_General_Notifications/Emailsending":
                estado["intentos_de_envio"] += 1
                if estado["envio"] == "500":
                    return self._responder(500, {"msgResponse": "falla interna"})
                if estado["envio"] == "lento":
                    time.sleep(1.5)
                if estado["envio"] == "401" or self.headers.get("Authorization") != f"Bearer {estado['vigente']}":
                    return self._responder(estado["codigo_rechazo"], {"error": True, "msgResponse": "token invalido"})
                if estado["envio"] == "html200":
                    return self._responder_crudo(200, "text/html", b"<html><body>Inicia sesion</body></html>")
                if estado["envio"] == "solo_enlace":
                    enlace = "http://localhost:3400/acceso/restablecer-contrasena?t=TOKEN-SECRETO-123"
                    return self._responder(200, {"count": 0, "error": True, "msgResponse": f"fallo con {enlace} intente de nuevo"})
                if estado["envio"] == "largo":
                    return self._responder(200, {"count": 0, "error": True, "msgResponse": "x" * 5000})
                if estado["envio"] == "error200":
                    # También repite el cuerpo (que lleva el enlace de un solo uso).
                    return self._responder(200, {"count": 0, "error": True, "msgResponse": f"buzon invalido {datos['mailBody']}"})
                estado["recibidos"].append(datos)
                return self._responder(200, {"count": 1, "results": "ok", "error": False, "msgResponse": "enviado"})
            return self._responder(404, {"error": True})

    servidor = ThreadingHTTPServer(("127.0.0.1", 0), Falso)
    threading.Thread(target=servidor.serve_forever, daemon=True).start()
    puerto = servidor.server_address[1]
    URL = f"http://127.0.0.1:{puerto}"

    class Recolector(logging.Handler):
        def __init__(self) -> None:
            super().__init__(logging.INFO)
            self.lineas: list[str] = []

        def emit(self, registro_log) -> None:
            self.lineas.append(registro_log.getMessage())

    recolector = Recolector()
    raiz = logging.getLogger()
    nivel_previo = raiz.level
    raiz.addHandler(recolector)
    raiz.setLevel(logging.INFO)

    previos = (config.CORREO_MODO, config.CORREO_API_URL, config.CORREO_API_USUARIO, config.CORREO_API_CONTRASENA, config.CORREO_TIMEOUT)
    config.CORREO_MODO, config.CORREO_API_URL = "api", URL
    config.CORREO_API_USUARIO, config.CORREO_API_CONTRASENA, config.CORREO_TIMEOUT = USUARIO, CONTRASENA, 1

    try:
        titulo("12a · Configuración: sin URL o sin credenciales no se manda, y solo por https")
        rev("el modo es api y, con las tres variables, se considera configurado", correo.modo() == "api" and correo.esta_configurado())
        config.CORREO_API_URL = ""
        rev("sin URL: NO configurado", not correo.esta_configurado())
        try:
            correo.enviar("ana@ejemplo.com", "x", "y")
            rev("y enviar levanta ErrorCorreo que lo dice", False)
        except correo.ErrorCorreo as exc:
            rev("y enviar levanta ErrorCorreo que lo dice", "CORREO_API_URL" in str(exc), str(exc)[:90])
        config.CORREO_API_URL = URL
        config.CORREO_API_CONTRASENA = ""
        rev("sin contraseña: NO configurado", not correo.esta_configurado())
        reiniciar()
        try:
            correo.enviar("ana@ejemplo.com", "x", "y")
            rev("y enviar sin contraseña levanta ErrorCorreo, sin pedir token", False)
        except correo.ErrorCorreo as exc:
            rev("y enviar sin contraseña levanta ErrorCorreo, sin pedir token", "CORREO_API_CONTRASENA" in str(exc) and estado["tokens_pedidos"] == 0, str(exc)[:90])
        config.CORREO_API_CONTRASENA = CONTRASENA
        reiniciar()
        config.CORREO_API_URL = "http://servidor-interno.ejemplo"
        try:
            correo.enviar("ana@ejemplo.com", "x", "y")
            rev("una URL http que NO es localhost se rechaza (ahí viajan las credenciales)", False)
        except correo.ErrorCorreo as exc:
            rev("una URL http que NO es localhost se rechaza (ahí viajan las credenciales)", "https" in str(exc) and estado["tokens_pedidos"] == 0, str(exc)[:90])
        config.CORREO_API_URL = URL

        titulo("12b · El envío: token, y el correo con el mapeo que pide el servicio")
        reiniciar()
        donde = correo.enviar("ana@ejemplo.com", "Recupera tu acceso", "texto plano", "<p>Hola <b>Ana</b></p>")
        rev("devuelve a dónde fue (api://host:puerto)", donde == f"api://127.0.0.1:{puerto}", donde)
        rev("pidió UN token", estado["tokens_pedidos"] == 1, str(estado["tokens_pedidos"]))
        envio = estado["recibidos"][0] if estado["recibidos"] else {}
        rev("mandó el HTML como mailBody", envio.get("mailBody") == "<p>Hola <b>Ana</b></p>", str(envio.get("mailBody")))
        rev("con su destinatario y su asunto", envio.get("mailRecipients") == "ana@ejemplo.com" and envio.get("mailSubject") == "Recupera tu acceso")
        rev("y sin copias", envio.get("mailCc") == "" and envio.get("mailBcc") == "")
        correo.enviar("ana@ejemplo.com", "Otro", "texto", "<p>otro</p>")
        rev("el token se guarda: dos correos, UN token", estado["tokens_pedidos"] == 1 and len(estado["recibidos"]) == 2)
        reiniciar()
        correo.enviar("ana@ejemplo.com", "Solo texto", "linea <1>" + chr(10) + "linea 2")
        rev("sin HTML, el texto va escapado y con sus saltos", estado["recibidos"][0]["mailBody"] == "<p>linea &lt;1&gt;<br>linea 2</p>", str(estado["recibidos"]))

        titulo("12c · El token vence o se revoca: se renueva UNA vez, y no más")
        reiniciar()
        correo.enviar("ana@ejemplo.com", "Uno", "t", "<p>1</p>")
        estado["vigente"] = "revocado-por-el-servicio"
        correo.enviar("ana@ejemplo.com", "Dos", "t", "<p>2</p>")
        rev("un 401 al enviar renueva el token y reintenta: llegan los dos correos", len(estado["recibidos"]) == 2, str(len(estado["recibidos"])))
        rev("con exactamente DOS tokens pedidos", estado["tokens_pedidos"] == 2, str(estado["tokens_pedidos"]))
        reiniciar(codigo_rechazo=403)
        correo.enviar("ana@ejemplo.com", "Uno", "t", "<p>1</p>")
        estado["vigente"] = "revocado-por-el-servicio"
        correo.enviar("ana@ejemplo.com", "Dos", "t", "<p>2</p>")
        rev("un 403 también renueva el token (no sabemos cuál de los dos usa el servicio)", len(estado["recibidos"]) == 2 and estado["tokens_pedidos"] == 2, f"{len(estado['recibidos'])} {estado['tokens_pedidos']}")
        reiniciar(envio="401")
        try:
            correo.enviar("ana@ejemplo.com", "Tres", "t", "<p>3</p>")
            rev("si el 401 persiste, falla (no se queda dando vueltas)", False)
        except correo.ErrorCorreo as exc:
            rev("si el 401 persiste, falla (no se queda dando vueltas)", "401" in str(exc), str(exc)[:100])
        rev("tras exactamente DOS intentos de envío", estado["intentos_de_envio"] == 2, str(estado["intentos_de_envio"]))
        reiniciar(vida_token=30)
        correo.enviar("ana@ejemplo.com", "a", "t", "<p>a</p>")
        correo.enviar("ana@ejemplo.com", "b", "t", "<p>b</p>")
        rev("un token que vence en menos del margen no se guarda (cada correo pide uno)", estado["tokens_pedidos"] == 2, str(estado["tokens_pedidos"]))
        reiniciar(formato_token="opaco")
        correo.enviar("ana@ejemplo.com", "a", "t", "<p>a</p>")
        correo.enviar("ana@ejemplo.com", "b", "t", "<p>b</p>")
        rev("un token sin vencimiento legible se guarda un rato (dos correos, un token)", estado["tokens_pedidos"] == 1, str(estado["tokens_pedidos"]))

        titulo("12d · Fallas del servicio: ErrorCorreo, sin reintentar lo que pudo haberse enviado")
        reiniciar(envio="error200")
        try:
            correo.enviar("ana@ejemplo.com", "x", "t", CUERPO_ENLACE)
            rev("HTTP 200 con error: true es una falla", False)
        except correo.ErrorCorreo as exc:
            mensaje_error = str(exc)
            rev("HTTP 200 con error: true es una falla", "buzon invalido" in mensaje_error, mensaje_error[:100])
            rev("y su mensaje NO repite el cuerpo ni el enlace aunque el servicio los repita", "TOKEN-SECRETO-123" not in mensaje_error and "localhost:3400" not in mensaje_error, mensaje_error)
        reiniciar(envio="500")
        try:
            correo.enviar("ana@ejemplo.com", "x", "t", "<p>x</p>")
            rev("un 500 es una falla", False)
        except correo.ErrorCorreo:
            rev("un 500 es una falla", True)
        rev("y NO se reintenta (el servicio pudo haberlo aceptado: se duplicaría)", estado["intentos_de_envio"] == 1, str(estado["intentos_de_envio"]))
        reiniciar(envio="html200")
        try:
            correo.enviar("ana@ejemplo.com", "x", "t", "<p>x</p>")
            rev("un 200 que no es JSON (una página de login, un proxy) NO es éxito", False)
        except correo.ErrorCorreo as exc:
            rev("un 200 que no es JSON (una página de login, un proxy) NO es éxito", "200" in str(exc), str(exc)[:100])
        reiniciar(envio="solo_enlace")
        try:
            correo.enviar("ana@ejemplo.com", "x", "t", CUERPO_ENLACE)
            rev("el servicio repite SOLO el enlace: es una falla", False)
        except correo.ErrorCorreo as exc:
            rev("el servicio repite SOLO el enlace: es una falla", "fallo con" in str(exc), str(exc)[:100])
            rev("y el enlace (con su token de un solo uso) NO sale en el mensaje", "TOKEN-SECRETO-123" not in str(exc) and "restablecer" not in str(exc), str(exc))
        reiniciar(envio="largo")
        try:
            correo.enviar("ana@ejemplo.com", "x", "t", "<p>x</p>")
            rev("el servicio contesta un mensaje enorme: es una falla", False)
        except correo.ErrorCorreo as exc:
            rev("el servicio contesta un mensaje enorme: es una falla, y el mensaje se acorta", len(str(exc)) < 400, str(len(str(exc))))
        reiniciar(token="redirige")
        try:
            correo.enviar("ana@ejemplo.com", "x", "t", "<p>x</p>")
            rev("un servicio que redirige no se sigue (las credenciales no viajan a otro lado)", False)
        except correo.ErrorCorreo as exc:
            rev("un servicio que redirige no se sigue (las credenciales no viajan a otro lado)", "307" in str(exc) and estado["siguio_la_redireccion"] == 0, str(exc)[:100])
        reiniciar(envio="lento")
        try:
            correo.enviar("ana@ejemplo.com", "x", "t", "<p>x</p>")
            rev("un timeout es una falla", False)
        except correo.ErrorCorreo as exc:
            rev("un timeout es una falla, y el mensaje solo trae el TIPO de la excepción", "Timeout" in str(exc) and str(exc).split("): ")[-1].isidentifier(), str(exc)[:100])
        rev("y tampoco se reintenta", estado["intentos_de_envio"] == 1, str(estado["intentos_de_envio"]))
        reiniciar(token="401")
        try:
            correo.enviar("ana@ejemplo.com", "x", "t", "<p>x</p>")
            rev("si el servicio no da token, falla y lo dice", False)
        except correo.ErrorCorreo as exc:
            rev("si el servicio no da token, falla y lo dice", "token" in str(exc) and "401" in str(exc), str(exc)[:100])
        reiniciar(token="error200")
        try:
            correo.enviar("ana@ejemplo.com", "x", "t", "<p>x</p>")
            rev("lo mismo con 200 y error: true", False)
        except correo.ErrorCorreo as exc:
            rev("lo mismo con 200 y error: true", "token" in str(exc))
            rev("y las credenciales que el servicio repitió NO salen en el mensaje", USUARIO not in str(exc) and CONTRASENA not in str(exc), str(exc))
        with socket.socket() as libre:
            libre.bind(("127.0.0.1", 0))
            puerto_cerrado = libre.getsockname()[1]
        config.CORREO_API_URL = f"http://127.0.0.1:{puerto_cerrado}"
        reiniciar()
        try:
            correo.enviar("ana@ejemplo.com", "x", "t", "<p>x</p>")
            rev("con el servicio inalcanzable, falla", False)
        except correo.ErrorCorreo as exc:
            rev("con el servicio inalcanzable, falla sin soltar detalles (solo el tipo)", ("ConnectError" in str(exc) or "ConnectTimeout" in str(exc)) and str(exc).split("): ")[-1].isidentifier(), str(exc)[:100])
        config.CORREO_API_URL = URL

        titulo("12e · El destinatario es UN correo: nada que el servicio pueda tomar por varios")
        reiniciar()
        for malo in ["a@b.com;c@d.com", "a@b.com,c@d.com", "a b@c.com", "<a@b.com>", 'a"@b.com', "a@b.com" + chr(10) + "c@d.com"]:
            try:
                correo.enviar(malo, "x", "t", "<p>x</p>")
                rev(f"rechaza {malo!r}", False)
            except correo.ErrorCorreo:
                rev(f"rechaza {malo!r}", True)
        rev("y no se mandó nada (ni se pidió token)", estado["tokens_pedidos"] == 0 and estado["intentos_de_envio"] == 0)

        titulo("12f · Varios hilos con el token en frío: UN solo token")
        reiniciar(demora_token=0.3)
        errores: list[str] = []

        def mandar(i: int) -> None:
            try:
                correo.enviar("ana@ejemplo.com", f"Hilo {i}", "t", f"<p>{i}</p>")
            except Exception as exc:  # noqa: BLE001
                errores.append(repr(exc))

        hilos = [threading.Thread(target=mandar, args=(i,)) for i in range(8)]
        [h.start() for h in hilos]
        [h.join() for h in hilos]
        rev("los ocho correos llegan", len(estado["recibidos"]) == 8 and not errores, f"{len(estado['recibidos'])} {errores}")
        rev("con UN token pedido, no ocho", estado["tokens_pedidos"] == 1, str(estado["tokens_pedidos"]))

        titulo("12g · Lo que NO sale en el log: credenciales, token, cuerpo")
        reiniciar()
        correo.enviar("ana@ejemplo.com", "Con enlace", "texto", CUERPO_ENLACE)
        todo_el_log = " ".join(recolector.lineas)
        rev("el log sí dice a quién se le mandó y con qué asunto", "Correo enviado a ana@ejemplo.com" in todo_el_log)
        rev("pero no el usuario ni la contraseña de la API", USUARIO not in todo_el_log and CONTRASENA not in todo_el_log)
        rev("ni el enlace del cuerpo", "TOKEN-SECRETO-123" not in todo_el_log and "restablecer-contrasena" not in todo_el_log)
        rev("ni un token de acceso", "eyJ" not in todo_el_log)
    finally:
        raiz.removeHandler(recolector)
        raiz.setLevel(nivel_previo)
        servidor.shutdown()
        servidor.server_close()
        config.CORREO_MODO, config.CORREO_API_URL, config.CORREO_API_USUARIO, config.CORREO_API_CONTRASENA, config.CORREO_TIMEOUT = previos
        correo._token.update(valor=None, vence=0.0)

except Exception:
    # Sin esto, una excepción a media corrida salía por el `finally` con
    # `fallos == 0` y el script imprimía "todas las comprobaciones pasaron":
    # una prueba que miente es peor que una que falla.
    import traceback

    fallos += 1
    print("\n  REVENTÓ a media corrida:")
    traceback.print_exc()
finally:
    print("\n" + "=" * 72)
    shutil.rmtree(RAIZ, ignore_errors=True)
    print("todas las comprobaciones pasaron" if fallos == 0 else f"{fallos} FALLARON")
    sys.exit(1 if fallos else 0)
