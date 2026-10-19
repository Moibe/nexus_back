"""¿Funciona la recuperación de contraseña (HU10) de punta a punta?

    .venv/Scripts/python verificar_recuperacion.py

OFFLINE y sin tocar nada tuyo, como `verificar_auth.py`: la app contra un
`ALMACEN_RUTA` temporal, con `CORREO_MODO=archivo` (los correos caen en disco y
se leen de ahí, que es MÁS fiable que un buzón: no depende de la red ni de que
alguien abra Outlook).

Un bloque por criterio de aceptación de NEX-241. Lo que más importa:

- CA-2: la respuesta para una cuenta que existe y para una que no es IDÉNTICA,
  byte por byte, y la que no existe no manda correo.
- CA-9: una cuenta sin primer acceso NO recibe enlace de recuperación.
- CA-5/CA-6: un typo no quema el enlace; un enlace se gasta UNA vez.
- CA-7: restablecer mata las sesiones, y también el bloqueo del navegador
  (sin eso, quien agotó sus cinco intentos y restableció seguía "bloqueado").
- Que ni el enlace ni la contraseña caigan en el log.
"""

import email
import email.policy
import json
import logging
import os
import re
import shutil
import sys
import tempfile
import traceback
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-recuperacion-"))
(RAIZ / ".nexus-almacen").touch()  # el centinela que el almacén exige para escribir
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
from routers.auth import MENSAJE_NEUTRO  # noqa: E402
from servicios import auth, tokens, usuarios  # noqa: E402

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + str(extra) if extra and not ok else ''}")
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


def srv() -> dict:
    return {"x-api-key": SERVICIO}


# ── Captura del log: ni el enlace ni la contraseña deben aparecer ───────────
class _Captura(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.mensajes: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.mensajes.append(record.getMessage())


captura = _Captura()
logging.getLogger().addHandler(captura)
logging.getLogger().setLevel(logging.INFO)

c = TestClient(app)

CONTRASENA_INICIAL = "Contrasena-Inicial-1!"
CONTRASENA_NUEVA = "Segura-Nueva-2026!"


# ── Ayudas ──────────────────────────────────────────────────────────────────
def correos() -> list[Path]:
    carpeta = RAIZ / "correos"
    return sorted(carpeta.glob("*.eml")) if carpeta.exists() else []


def limpiar_correos() -> None:
    for f in correos():
        f.unlink()


def leer(path: Path):
    return email.message_from_bytes(path.read_bytes(), policy=email.policy.default)


def cuerpo_texto(path: Path) -> str:
    return leer(path).get_body(preferencelist=("plain",)).get_content()


def token_de(path: Path) -> str | None:
    m = re.search(r"\?t=([A-Za-z0-9_-]+)", cuerpo_texto(path))
    return m.group(1) if m else None


def solicitar(correo_: str, origen: str | None = None, ip: str = "10.0.0.1"):
    cuerpo = {"email": correo_, "ip": ip, "userAgent": "verificar"}
    if origen:
        cuerpo["origen"] = origen
    return c.post("/auth/recuperacion/solicitar", json=cuerpo, headers=srv())


def revisar(token: str):
    return c.post("/auth/recuperacion/revisar", json={"token": token}, headers=srv())


def confirmar(token: str, nueva: str, confirmacion: str | None = None, origen: str | None = None):
    cuerpo = {"token": token, "nueva": nueva, "confirmacion": nueva if confirmacion is None else confirmacion, "ip": "10.0.0.1"}
    if origen:
        cuerpo["origen"] = origen
    return c.post("/auth/recuperacion/confirmar", json=cuerpo, headers=srv())


def login(correo_: str, contrasena: str, origen: str | None = None):
    cuerpo = {"email": correo_, "password": contrasena, "ip": "10.0.0.1", "userAgent": "verificar"}
    if origen:
        cuerpo["origen"] = origen
    return c.post("/auth/login", json=cuerpo, headers=srv())


def vaciar_auditoria() -> None:
    ruta = RAIZ / ".registro" / "auditoria.jsonl"
    if ruta.exists():
        ruta.write_text("", encoding="utf-8")


def crear_activo(correo_: str, nombre: str) -> dict:
    """Una cuenta que YA hizo su primer acceso: la única que puede recuperar."""
    u = usuarios.bootstrap_admin(correo_, nombre, "Prueba", auth.hash_de(auth.contrasena_temporal()))
    return usuarios.fijar_contrasena(u["guid"], auth.hash_de(CONTRASENA_INICIAL), "FIRST_LOGIN")


def forzar_vencimiento(token: str) -> None:
    """Reescribe `expira` en el registro, como `verificar_correo.py`."""
    ruta = RAIZ / ".registro" / "tokens.jsonl"
    lineas = []
    for linea in ruta.read_text(encoding="utf-8").splitlines():
        e = json.loads(linea)
        if e.get("evento") == "creado" and e.get("hash") == tokens.hash_de(token):
            e["expira"] = "2020-01-01T00:00:00+00:00"
        lineas.append(json.dumps(e, ensure_ascii=False))
    ruta.write_text("\n".join(lineas) + "\n", encoding="utf-8")


try:
    # Sin esperas ni topes por ahora: cada sección cambia la config que necesite.
    config.RECUPERACION_ESPERA_SEG = 0
    config.RECUPERACION_MAX_POR_CORREO_HORA = 1000
    config.RECUPERACION_MAX_POR_ORIGEN_HORA = 1000
    config.RECUPERACION_MAX_POR_IP_HORA = 1000
    config.RECUPERACION_MAX_GLOBAL_HORA = 10000

    ana = crear_activo("ana@ejemplo.com", "Ana")
    # Pablo: dado de alta pero NUNCA ha entrado (sigue con `debeCambiar`).
    pablo = usuarios.bootstrap_admin("pablo@ejemplo.com", "Pablo", "Prueba", auth.hash_de(auth.contrasena_temporal()))
    dora = crear_activo("dora@ejemplo.com", "Dora")
    usuarios.cambiar_estado(dora["guid"], False, "prueba", None)

    titulo("1 · CA-1 y CA-2: se responde LO MISMO exista la cuenta o no")
    limpiar_correos()
    r_no = solicitar("nadie@ejemplo.com")
    rev("correo inexistente: 200", r_no.status_code == 200, r_no.text)
    rev("con el mensaje del criterio, literal", r_no.json().get("mensaje") == MENSAJE_NEUTRO, r_no.text)
    rev("y NO manda ningún correo", len(correos()) == 0, str(len(correos())))
    r_si = solicitar("ana@ejemplo.com")
    rev("cuenta real: 200", r_si.status_code == 200, r_si.text)
    rev("la respuesta es IDÉNTICA a la de la cuenta que no existe (mismo cuerpo)", r_si.json() == r_no.json(), f"{r_si.json()} vs {r_no.json()}")
    rev("e idéntico el estado y la cabecera Cache-Control",
        r_si.status_code == r_no.status_code and r_si.headers.get("cache-control") == r_no.headers.get("cache-control") == "no-store")
    rev("pero a la cuenta real SÍ le llega un correo", len(correos()) == 1, str(len(correos())))
    rev("el mensaje es el del criterio CA-2, con acentos",
        "recibirás un enlace de recuperación en los próximos minutos." in MENSAJE_NEUTRO and MENSAJE_NEUTRO.startswith("Si existe una cuenta"))

    titulo("2 · Lo que NO es un correo se rechaza, y es lo único que se distingue")
    for malo in ["pepito", "a@b", "", "no es correo@x.com", "a@b.c", "a@b..com", "a@-b.com", ".a@b.com"]:
        r = solicitar(malo)
        rev(f"{malo!r}: 400 correo_invalido", r.status_code == 400 and r.json()["detail"]["codigo"] == "correo_invalido", r.text)
    rev("sin llave de servicio: 401", c.post("/auth/recuperacion/solicitar", json={"email": "ana@ejemplo.com"}).status_code == 401)

    titulo("3 · CA-3: el enlace va al correo PRINCIPAL (CA-2 gana sobre la elección)")
    limpiar_correos()
    usuarios.actualizar_perfil(ana["guid"], "Ana", "Prueba", "", None, {"email": "respaldo@ejemplo.com", "telefono": None})
    rev("Ana tiene correo de recuperación registrado", usuarios.por_guid(ana["guid"])["recuperacion"].get("email") == "respaldo@ejemplo.com")
    solicitar("ana@ejemplo.com")
    mandados = correos()
    rev("se manda UN solo correo", len(mandados) == 1, str(len(mandados)))
    rev("al correo principal", leer(mandados[0])["To"] == "ana@ejemplo.com", str(leer(mandados[0])["To"]))
    rev("y nunca al de recuperación", all("respaldo@ejemplo.com" not in str(leer(f)["To"]) for f in mandados))
    rev("asunto del diseño", leer(mandados[0])["Subject"] == "Restablece tu contraseña", str(leer(mandados[0])["Subject"]))
    cuerpo = cuerpo_texto(mandados[0])
    rev("saluda por el nombre", "Hola, Ana." in cuerpo)
    rev("trae el enlace al front con el token", token_de(mandados[0]) is not None and "http://localhost:3400/acceso/restablecer-contrasena?t=" in cuerpo)
    rev("el enlace aparece también en la versión HTML",
        token_de(mandados[0]) in leer(mandados[0]).get_body(preferencelist=("html",)).get_content())
    rev("sin datos de contacto de ejemplo del mockup", "soporte@nexusdoc.com" not in cuerpo and "1234 5678" not in cuerpo)

    titulo("4 · CA-9: una cuenta sin primer acceso NO recibe enlace de recuperación")
    limpiar_correos()
    r = solicitar("pablo@ejemplo.com")
    rev("la respuesta es la misma de siempre", r.status_code == 200 and r.json() == r_no.json(), r.text)
    mandados = correos()
    rev("se le manda un correo, pero DISTINTO", len(mandados) == 1 and leer(mandados[0])["Subject"] == "Tu primer acceso a NexusDoc sigue pendiente",
        str([leer(f)["Subject"] for f in mandados]))
    texto = cuerpo_texto(mandados[0]) if mandados else ""
    rev("sin enlace de recuperación", "?t=" not in texto and "restablecer-contrasena" not in texto)
    rev("y no se creó ningún token a su nombre", tokens.pendientes_de(pablo["guid"]) == [])
    rev("su cuenta sigue obligada a hacer el primer acceso", usuarios.por_guid(pablo["guid"])["debeCambiarContrasena"] is True)

    titulo("5 · Una cuenta desactivada tampoco recibe nada")
    limpiar_correos()
    r = solicitar("dora@ejemplo.com")
    rev("misma respuesta", r.status_code == 200 and r.json() == r_no.json())
    rev("sin correo y sin token", len(correos()) == 0 and tokens.pendientes_de(dora["guid"]) == [])

    titulo("6 · CA-10: cada solicitud queda anotada, exista la cuenta o no")
    ruta_aud = RAIZ / ".registro" / "auditoria.jsonl"
    eventos = [json.loads(x) for x in ruta_aud.read_text(encoding="utf-8").splitlines() if x.strip()]
    pedidos = [e for e in eventos if e["accion"] == "PASSWORD_RESET_REQUESTED"]
    rev("hay un PASSWORD_RESET_REQUESTED por solicitud válida", len(pedidos) >= 5, str(len(pedidos)))
    rev("todos con fecha y hora", all(e.get("en") for e in pedidos))
    resultados = {e["resultado"] for e in pedidos}
    rev("incluidos los de cuentas inexistentes, desactivadas y pendientes",
        {"SIN_CUENTA", "ENVIADO", "PENDIENTE_PRIMER_ACCESO", "DESACTIVADA"} <= resultados, str(resultados))
    rev("las solicitudes con correo mal formado NO se anotan (no son válidas)", all(e.get("email") != "pepito" for e in pedidos))

    titulo("7 · CA-4: el enlace se revisa SIN gastarse, y no dice de quién es")
    limpiar_correos()
    solicitar("ana@ejemplo.com")
    tok = token_de(correos()[0])
    r = revisar(tok)
    rev("revisar un enlace bueno: 200", r.status_code == 200, r.text)
    rev("solo dice que sirve: ni usuario ni correo", set(r.json().keys()) == {"ok"}, r.text)
    rev("y sigue sirviendo después de revisarlo", revisar(tok).status_code == 200)
    r_basura = revisar("esto-es-basura-que-no-existe-12345")
    rev("un token inventado: 410", r_basura.status_code == 410 and r_basura.json()["detail"]["codigo"] == "enlace_invalido", r_basura.text)
    r_corto = revisar("abc")
    rev("uno con forma imposible: 410, sin tocar el registro", r_corto.status_code == 410 and r_corto.json()["detail"]["codigo"] == "enlace_invalido")
    rev("el mensaje es el del Figma, con acentos",
        r_basura.json()["detail"]["mensaje"].startswith("Por motivos de seguridad, este enlace de restablecimiento ya no se encuentra disponible."))

    titulo("8 · CA-5: la contraseña se valida ANTES de gastar el enlace")
    r = confirmar(tok, "Otra-Frase-1!", "no-es-la-misma")
    rev("no coincide: 400 no_coincide", r.status_code == 400 and r.json()["detail"]["codigo"] == "no_coincide", r.text)
    rev("con el texto del diseño", r.json()["detail"]["mensaje"] == "La contraseña no coincide; por favor intenta de nuevo.")
    for debil, falta in [("corta", "12 caracteres"), ("sinmayusculas12!!", "Mayúscula"), ("SINMINUSCULAS12!!", "Minúscula"),
                         ("SinNumeros!!!!!!", "Número"), ("SinEspeciales12345", "Carácter especial")]:
        r = confirmar(tok, debil)
        det = r.json()["detail"]
        rev(f"{debil!r}: 400 reglas, y dice qué falta ({falta})", r.status_code == 400 and det["codigo"] == "reglas" and falta in det.get("faltan", []), r.text)
    rev("el mensaje de reglas es el del Figma", det["mensaje"] == "La contraseña no cumple con el formato requerido; por favor intenta de nuevo.")
    rev("y tras tantos errores el enlace SIGUE sirviendo (un typo no lo quema)", revisar(tok).status_code == 200)

    titulo("9 · CA-6 y CA-7: se restablece, se gasta el enlace y mueren las sesiones")
    # Dos sesiones abiertas ANTES del restablecimiento.
    s1 = login("ana@ejemplo.com", CONTRASENA_INICIAL)
    s2 = login("ana@ejemplo.com", CONTRASENA_INICIAL)
    rev("Ana tiene dos sesiones abiertas", s1.status_code == 200 and s2.status_code == 200, f"{s1.status_code} {s2.status_code}")
    refresh1, refresh2 = s1.json()["refreshToken"], s2.json()["refreshToken"]
    r = confirmar(tok, CONTRASENA_NUEVA)
    rev("confirmar: 200", r.status_code == 200 and r.json().get("ok") is True, r.text)
    rev("dice que cerró las sesiones", r.json().get("sesionesCerradas") is True)
    rev("la contraseña NUEVA entra", login("ana@ejemplo.com", CONTRASENA_NUEVA).status_code == 200)
    rev("la VIEJA ya no", login("ana@ejemplo.com", CONTRASENA_INICIAL).status_code == 401)
    for i, rt in enumerate([refresh1, refresh2], start=1):
        r = c.post("/auth/refresh", json={"refreshToken": rt}, headers=srv())
        rev(f"CA-7: la sesión {i} abierta antes ya no se puede renovar", r.status_code == 401, r.text)
    rev("CA-8: el enlace usado ya no sirve para revisar", revisar(tok).status_code == 410 and revisar(tok).json()["detail"]["codigo"] == "enlace_usado")
    r = confirmar(tok, "Y-Otra-Distinta-2026!")
    rev("CA-8: ni para confirmar de nuevo", r.status_code == 410 and r.json()["detail"]["codigo"] == "enlace_usado", r.text)
    rev("la contraseña no cambió por el segundo intento", login("ana@ejemplo.com", CONTRASENA_NUEVA).status_code == 200)

    titulo("10 · CA-6: PASSWORD_RESET_COMPLETED queda anotado, sin secretos")
    eventos = [json.loads(x) for x in ruta_aud.read_text(encoding="utf-8").splitlines() if x.strip()]
    hechos = [e for e in eventos if e["accion"] == "PASSWORD_RESET_COMPLETED"]
    rev("hay un COMPLETED", len(hechos) == 1, str(len(hechos)))
    rev("con la persona y la hora", hechos[0].get("usuarioGuid") == ana["guid"] and bool(hechos[0].get("en")))
    crudo = "".join((RAIZ / ".registro" / f).read_text(encoding="utf-8") for f in sorted(os.listdir(RAIZ / ".registro")))
    rev("el token en claro NO está en ningún registro", tok not in crudo)
    rev("ni la contraseña nueva", CONTRASENA_NUEVA not in crudo)
    registros_log = "\n".join(captura.mensajes)
    rev("ni el token ni la contraseña salieron en el log", tok not in registros_log and CONTRASENA_NUEVA not in registros_log)

    titulo("11 · CA-8: un enlace vencido no sirve")
    limpiar_correos()
    solicitar("ana@ejemplo.com")
    viejo = token_de(correos()[0])
    forzar_vencimiento(viejo)
    r = revisar(viejo)
    rev("revisar: 410 enlace_vencido", r.status_code == 410 and r.json()["detail"]["codigo"] == "enlace_vencido", r.text)
    r = confirmar(viejo, "Una-Frase-Buena-1!")
    rev("confirmar: 410 y no cambia nada", r.status_code == 410 and login("ana@ejemplo.com", CONTRASENA_NUEVA).status_code == 200, r.text)

    titulo("12 · Pedir un enlace nuevo mata el anterior")
    limpiar_correos()
    solicitar("ana@ejemplo.com")
    primero = token_de(correos()[0])
    solicitar("ana@ejemplo.com")
    segundo = token_de(correos()[1])
    rev("son dos enlaces distintos", primero != segundo)
    rev("el primero ya no sirve", revisar(primero).status_code == 410)
    rev("el segundo sí", revisar(segundo).status_code == 200)

    titulo("13 · Un enlace de ACTIVACIÓN no sirve para recuperar contraseñas")
    activacion, _ = tokens.crear(ana["guid"], tokens.ACTIVACION)
    r = revisar(activacion)
    rev("410: el tipo no coincide", r.status_code == 410 and r.json()["detail"]["codigo"] == "enlace_invalido", r.text)

    titulo("14 · Un enlace de una cuenta que después se desactivó deja de servir")
    limpiar_correos()
    erika = crear_activo("erika@ejemplo.com", "Erika")
    solicitar("erika@ejemplo.com")
    t_erika = token_de(correos()[0])
    rev("mientras está activa, sirve", revisar(t_erika).status_code == 200)
    usuarios.cambiar_estado(erika["guid"], False, "prueba", None)
    r = revisar(t_erika)
    rev("desactivada: 410", r.status_code == 410, r.text)
    r = confirmar(t_erika, "Una-Frase-Buena-1!")
    rev("y confirmar tampoco la cambia", r.status_code == 410 and login("erika@ejemplo.com", CONTRASENA_INICIAL).status_code == 403, r.text)

    titulo("15 · Restablecer levanta el bloqueo del NAVEGADOR (no solo el de la cuenta)")
    limpiar_correos()
    lola = crear_activo("lola@ejemplo.com", "Lola")
    nav = "navegador-de-lola"
    for _ in range(config.AUTH_MAX_INTENTOS):
        login("lola@ejemplo.com", "mala", nav)
    r = login("lola@ejemplo.com", CONTRASENA_INICIAL, nav)
    rev("agotó los intentos: 423 bloqueada, incluso con la contraseña buena", r.status_code == 423, r.text)
    solicitar("lola@ejemplo.com", origen=nav)
    t_lola = token_de(correos()[0])
    rev("el bloqueo NO le impide pedir el enlace", t_lola is not None)
    r = confirmar(t_lola, CONTRASENA_NUEVA, origen=nav)
    rev("restablece", r.status_code == 200, r.text)
    r = login("lola@ejemplo.com", CONTRASENA_NUEVA, nav)
    rev("y con la contraseña nueva ENTRA desde ese navegador (no queda bloqueada)", r.status_code == 200, r.text)

    titulo("16 · Un fallo del servidor de correo no cambia lo que ve la persona")
    limpiar_correos()
    modo_antes = config.CORREO_MODO
    config.CORREO_MODO = "smtp"  # sin host: enviar() levanta ErrorCorreo
    r = solicitar("ana@ejemplo.com")
    rev("sigue siendo 200 con el mensaje neutro", r.status_code == 200 and r.json() == r_no.json(), r.text)
    config.CORREO_MODO = modo_antes

    titulo("17 · Los límites: frenan el abuso sin delatar quién existe")
    config.RECUPERACION_ESPERA_SEG = 60
    vaciar_auditoria()
    limpiar_correos()
    solicitar("ana@ejemplo.com")
    r = solicitar("ana@ejemplo.com")
    rev("dos solicitudes seguidas del mismo correo: la segunda 429", r.status_code == 429 and r.json()["detail"]["codigo"] == "demasiados_intentos", r.text)
    rev("la 429 no manda un correo más", len(correos()) == 1, str(len(correos())))
    r_fantasma_1 = solicitar("fantasma@ejemplo.com")
    r_fantasma_2 = solicitar("fantasma@ejemplo.com")
    rev("para un correo que NO existe el comportamiento es el mismo (200 y luego 429)",
        r_fantasma_1.status_code == 200 and r_fantasma_2.status_code == 429, f"{r_fantasma_1.status_code} {r_fantasma_2.status_code}")
    rev("y el cuerpo del 429 es idéntico exista o no la cuenta", r.json() == r_fantasma_2.json())

    config.RECUPERACION_ESPERA_SEG = 0
    vaciar_auditoria()
    config.RECUPERACION_MAX_POR_CORREO_HORA = 3
    codigos = [solicitar("tope@ejemplo.com").status_code for _ in range(5)]
    rev("por correo: tras 3 en una hora, 429", codigos == [200, 200, 200, 429, 429], str(codigos))
    config.RECUPERACION_MAX_POR_CORREO_HORA = 1000

    vaciar_auditoria()
    config.RECUPERACION_MAX_POR_ORIGEN_HORA = 2
    codigos = [solicitar(f"o{i}@ejemplo.com", origen="mismo-navegador").status_code for i in range(4)]
    rev("por navegador: tras 2, 429", codigos == [200, 200, 429, 429], str(codigos))
    config.RECUPERACION_MAX_POR_ORIGEN_HORA = 1000

    vaciar_auditoria()
    config.RECUPERACION_MAX_POR_IP_HORA = 3
    codigos = [solicitar(f"i{i}@ejemplo.com", ip="203.0.113.9").status_code for i in range(5)]
    rev("por IP, SIN cookie de navegador: tras 3, 429 (no depende de nada que el cliente controle)", codigos == [200, 200, 200, 429, 429], str(codigos))
    rev("y otra IP no se ve afectada", solicitar("otra@ejemplo.com", ip="203.0.113.77").status_code == 200)
    config.RECUPERACION_MAX_POR_IP_HORA = 1000

    vaciar_auditoria()
    config.RECUPERACION_MAX_GLOBAL_HORA = 4
    codigos = [solicitar(f"g{i}@ejemplo.com", ip=f"198.51.100.{i}").status_code for i in range(6)]
    rev("global: tras 4 en la plataforma, 429 (protege al relay)", codigos == [200, 200, 200, 200, 429, 429], str(codigos))
    config.RECUPERACION_MAX_GLOBAL_HORA = 10000

    titulo("18 · Nada de lo anterior rompió el login")
    rev("Ana entra con su contraseña vigente", login("ana@ejemplo.com", CONTRASENA_NUEVA).status_code == 200)
    rev("y un correo inexistente en el login sigue diciendo que no existe (HU03, decisión de UX)",
        login("nadie@ejemplo.com", "x").json()["detail"]["codigo"] == "correo_no_existe")

except Exception:
    # Sin esto, una excepción a media corrida saldría por el `finally` con
    # `fallos == 0` y el script diría "todas las comprobaciones pasaron": una
    # prueba que miente es peor que una que falla.
    fallos += 1
    print("\n  REVENTÓ a media corrida:")
    traceback.print_exc()
finally:
    print("\n" + "=" * 72)
    shutil.rmtree(RAIZ, ignore_errors=True)
    print("todas las comprobaciones pasaron" if fallos == 0 else f"{fallos} FALLARON")
    sys.exit(1 if fallos else 0)
