"""¿Se pueden crear, ver, regenerar y retirar administradores de plataforma desde las rutas que usa Nexus Monitor?

    .venv/Scripts/python verificar_admins_plataforma.py

OFFLINE y sin tocar nada tuyo, como los demás `verificar_*`: el almacén es una carpeta temporal y el servicio de correo se SIMULA.

Lo que se prueba:

- crear: devuelve la contraseña temporal, que sirve para entrar y obliga a cambiarla; el correo sale, no sale (sin canal), falla o se
  omite, y la contraseña se devuelve en todos los casos; un correo repetido o inválido no crea nada;
- listar: los que existen con su estado (pendiente, activo, inactivo), sin el hash, en orden de alta; los retirados ya no salen;
- regenerar la contraseña: solo a quien no ha hecho su primer acceso; la anterior deja de servir;
- retirar: pide motivo, cierra sesiones, deja libre el correo, y NO retira al último administrador de plataforma activo;
- solo se toca a administradores de PLATAFORMA: una cuenta de una organización responde como si no existiera;
- sin la llave de servicio, nada;
- crear, regenerar y retirar dejan su rastro con `actor: monitor` y jamás la contraseña.
"""

import json
import os
import shutil
import tempfile
import uuid
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-admins-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ["CORREO_MODO"] = "archivo"
os.environ["FRONT_URL"] = "http://localhost:3400"
os.environ.setdefault("SQLSERVER_HOST", "")

from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import auth, correo, usuarios  # noqa: E402

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

# Un canal de correo SIMULADO: se queda con lo que se le manda. `falla` lo hace tronar como el servicio real.
enviados: list[dict] = []
estado_canal = {"falla": False}
original_modo, original_enviar = correo.modo, correo.enviar


def enviar_falso(destino, asunto, texto, html=None, timeout=None):
    if estado_canal["falla"]:
        raise correo.ErrorCorreo("el servicio de correo no contestó (prueba)")
    enviados.append({"destino": destino, "asunto": asunto, "texto": texto})
    return "api://falso"


def canal_real() -> None:
    correo.modo = lambda: "api"
    correo.enviar = enviar_falso
    enviados.clear()
    estado_canal["falla"] = False


def sin_canal() -> None:
    correo.modo, correo.enviar = original_modo, original_enviar


def eventos(accion: str) -> list[dict]:
    ruta = RAIZ / ".registro" / "auditoria.jsonl"
    if not ruta.exists():
        return []
    return [e for e in (json.loads(x) for x in ruta.read_text(encoding="utf-8").splitlines() if x.strip()) if e.get("accion") == accion]


#: Todas las contraseñas temporales que las rutas devolvieron: al final se comprueba que ninguna quedó escrita en la bitácora.
temporales_devueltas: list[str] = []


def anotar_temporal(r):
    if r.status_code in (200, 201) and r.json().get("contrasenaTemporal"):
        temporales_devueltas.append(r.json()["contrasenaTemporal"])
    return r


def crear(email: str, nombre: str = "Ada", apellidos: str = "Lovelace", **extra):
    return anotar_temporal(c.post("/admins-plataforma/", json={"email": email, "nombre": nombre, "apellidos": apellidos, **extra}, headers=srv()))


def regenerar(guid: str):
    return anotar_temporal(c.post(f"/admins-plataforma/{guid}/reenviar-invitacion", headers=srv()))


def lista() -> list[dict]:
    return c.get("/admins-plataforma/", headers=srv()).json()["administradores"]


def correos() -> list[str]:
    return [a["email"] for a in lista()]


def entrar(email: str, contrasena: str):
    return c.post("/auth/login", json={"email": email, "password": contrasena, "ip": "127.0.0.1", "userAgent": "verificar"}, headers=srv())


def primer_acceso(email: str, temporal: str, definitiva: str) -> None:
    s = entrar(email, temporal).json()
    r = c.post("/auth/contrasena", json={"nueva": definitiva, "confirmacion": definitiva}, headers={**srv(), "Authorization": f"Bearer {s['accessToken']}"})
    assert r.status_code == 200, r.text


try:
    titulo("1 · Sin nadie todavía")
    r = c.get("/admins-plataforma/", headers=srv())
    rev("la lista está vacía y dice cuántos son", r.status_code == 200 and r.json() == {"total": 0, "administradores": []}, r.text[:200])

    titulo("2 · Crear (sin canal de correo: la contraseña es el respaldo)")
    sin_canal()
    r = crear("ada@ejemplo.com")
    d = r.json()
    rev("crea con 201", r.status_code == 201, r.text[:200])
    rev("devuelve el administrador, su contraseña temporal y qué pasó con el correo", d["administrador"]["email"] == "ada@ejemplo.com" and len(d["contrasenaTemporal"]) >= 12 and d["correo"] == "sin_canal", str(d))
    rev("queda como administrador de plataforma, pendiente de su primer acceso", d["administrador"]["estado"] == "pendiente" and d["administrador"]["ultimoLoginEn"] is None and d["administrador"]["temporalEmitidaEn"], str(d["administrador"]))
    rev("el resumen no lleva el hash ni nada de la contraseña", "hash" not in json.dumps(d["administrador"]).lower() and d["contrasenaTemporal"] not in json.dumps(d["administrador"]))
    temporal_ada = d["contrasenaTemporal"]
    s = entrar("ada@ejemplo.com", temporal_ada)
    rev("la contraseña temporal sirve para entrar", s.status_code == 200 and s.json()["usuario"]["esAdminPlataforma"] is True, s.text[:200])
    rev("y la app obligará a cambiarla", s.json()["usuario"]["debeCambiarContrasena"] is True)
    rev("el correo se normaliza (mayúsculas y espacios)", crear("  Beto@Ejemplo.COM ").json()["administrador"]["email"] == "beto@ejemplo.com")

    titulo("3 · Crear con canal de correo")
    canal_real()
    r = crear("carla@ejemplo.com", "Carla", "Pérez Soto")
    d = r.json()
    rev("el correo sale y lo dice", r.status_code == 201 and d["correo"] == "enviado" and len(enviados) == 1 and enviados[0]["destino"] == "carla@ejemplo.com", str(d))
    rev("el correo trae la contraseña que se devolvió", d["contrasenaTemporal"] in enviados[0]["texto"])
    rev("y la contraseña se devuelve igual (es el respaldo de quien opera)", len(d["contrasenaTemporal"]) >= 12)
    rev("el nombre y los apellidos se guardan", d["administrador"]["nombre"] == "Carla" and d["administrador"]["apellidos"] == "Pérez Soto")
    estado_canal["falla"] = True
    r = crear("diego@ejemplo.com")
    rev("si el servicio de correo falla: se crea igual y dice «fallo»", r.status_code == 201 and r.json()["correo"] == "fallo" and r.json()["contrasenaTemporal"], r.text[:200])
    estado_canal["falla"] = False
    n_antes = len(enviados)
    r = crear("elena@ejemplo.com", enviarCorreo=False)
    rev("con enviarCorreo=false no se manda nada y dice «omitido»", r.status_code == 201 and r.json()["correo"] == "omitido" and len(enviados) == n_antes and r.json()["contrasenaTemporal"], r.text[:200])

    titulo("4 · Lo que no se crea")
    antes = correos()
    r = crear("ada@ejemplo.com")
    rev("un correo ya registrado: 409 y no crea nada", r.status_code == 409 and r.json()["detail"]["codigo"] == "ya_existe" and correos() == antes, r.text[:200])
    rev("un correo con otras mayúsculas también es el mismo", crear("ADA@ejemplo.com").status_code == 409)
    for correo_malo in ("sin-arroba", "a@b", "", "   "):
        r = crear(correo_malo)
        rev(f"un correo inválido ({correo_malo!r}) se rechaza", r.status_code in (400, 422) and correos() == antes, f"{r.status_code} {r.text[:120]}")
    rev("sin nombre no se crea", crear("x1@ejemplo.com", nombre="   ").status_code in (400, 422))
    rev("sin apellidos no se crea", crear("x2@ejemplo.com", apellidos="").status_code in (400, 422))
    rev("faltando campos: 422", c.post("/admins-plataforma/", json={"email": "x3@ejemplo.com"}, headers=srv()).status_code == 422)
    rev("un nombre larguísimo se rechaza", crear("x4@ejemplo.com", nombre="a" * 101).status_code == 422)
    rev("nada de lo anterior creó a nadie", correos() == antes)

    titulo("5 · Listar con estados")
    por_correo = {a["email"]: a for a in lista()}
    rev("salen los cinco, en el orden de su alta", [a["email"] for a in lista()] == ["ada@ejemplo.com", "beto@ejemplo.com", "carla@ejemplo.com", "diego@ejemplo.com", "elena@ejemplo.com"], str(correos()))
    rev("todos pendientes: ninguno ha entrado con su contraseña definitiva", all(a["estado"] == "pendiente" for a in lista()))
    primer_acceso("ada@ejemplo.com", temporal_ada, "Cobre-Lento-Azul-47!")
    entrar("ada@ejemplo.com", "Cobre-Lento-Azul-47!")  # cambiar la contraseña cierra las sesiones: se vuelve a entrar con la definitiva
    por_correo = {a["email"]: a for a in lista()}
    rev("tras su primer acceso Ada está «activa», con su último acceso y sin fecha de contraseña temporal", por_correo["ada@ejemplo.com"]["estado"] == "activo" and por_correo["ada@ejemplo.com"]["ultimoLoginEn"] and por_correo["ada@ejemplo.com"]["temporalEmitidaEn"] is None, str(por_correo["ada@ejemplo.com"]))
    rev("y tiene una sesión abierta", por_correo["ada@ejemplo.com"]["sesionActiva"] is True and por_correo["beto@ejemplo.com"]["sesionActiva"] is False)
    rev("los demás siguen pendientes", all(por_correo[e]["estado"] == "pendiente" for e in ("beto@ejemplo.com", "carla@ejemplo.com")))
    rev("el resumen no lleva el hash", "hash" not in json.dumps(lista()).lower())
    gina = crear("gina@ejemplo.com").json()["administrador"]
    usuarios.cambiar_estado(gina["guid"], False, "prueba")
    rev("una cuenta desactivada sale como «inactivo»", next(a for a in lista() if a["email"] == "gina@ejemplo.com")["estado"] == "inactivo")

    titulo("6 · Regenerar la contraseña")
    canal_real()
    guid_beto = por_correo["beto@ejemplo.com"]["guid"]
    guid_ada = por_correo["ada@ejemplo.com"]["guid"]
    vieja_beto = None
    r = regenerar(guid_beto)
    d = r.json()
    rev("a quien no ha entrado se le regenera: 200, correo enviado y contraseña nueva", r.status_code == 200 and d["correo"] == "enviado" and len(d["contrasenaTemporal"]) >= 12 and d["contrasenaTemporal"] in enviados[0]["texto"], r.text[:200])
    rev("la contraseña nueva sirve para entrar", entrar("beto@ejemplo.com", d["contrasenaTemporal"]).status_code == 200)
    r2 = regenerar(guid_beto)
    rev("y regenerar otra vez deja sin servir a la anterior", entrar("beto@ejemplo.com", d["contrasenaTemporal"]).status_code == 401 and r2.status_code == 200 and entrar("beto@ejemplo.com", r2.json()["contrasenaTemporal"]).status_code == 200)
    r = c.post(f"/admins-plataforma/{guid_ada}/reenviar-invitacion", headers=srv())
    rev("a quien YA hizo su primer acceso, no: 409 «ya_hizo_su_primer_acceso»", r.status_code == 409 and r.json()["detail"]["codigo"] == "ya_hizo_su_primer_acceso", r.text[:200])
    rev("y su contraseña definitiva sigue sirviendo", entrar("ada@ejemplo.com", "Cobre-Lento-Azul-47!").status_code == 200)
    r = c.post(f"/admins-plataforma/{uuid.uuid4()}/reenviar-invitacion", headers=srv())
    rev("regenerar a quien no existe: 404", r.status_code == 404, r.text[:200])
    sin_canal()
    r = regenerar(guid_beto)
    rev("sin canal de correo la contraseña nueva viene igual y dice «sin_canal»", r.status_code == 200 and r.json()["correo"] == "sin_canal" and r.json()["contrasenaTemporal"], r.text[:200])

    titulo("7 · Solo se toca a administradores de plataforma")
    # Una cuenta de una organización, creada por los servicios, NO es un administrador de plataforma.
    usuario_org = usuarios.crear_usuario("rita@ejemplo.com", "Rita", "Rumbo", auth.hash_de(auth.contrasena_temporal()), None, None, "ADMIN")
    guid_org = usuario_org["guid"]
    rev("la cuenta de una organización NO sale en la lista", "rita@ejemplo.com" not in correos())
    r = c.post(f"/admins-plataforma/{guid_org}/reenviar-invitacion", headers=srv())
    rev("regenerarle la contraseña por aquí: 404 (como si no existiera)", r.status_code == 404, r.text[:200])
    r = c.delete(f"/admins-plataforma/{guid_org}", params={"motivo": "prueba"}, headers=srv())
    rev("retirarla por aquí: 404", r.status_code == 404, r.text[:200])
    rev("y la cuenta sigue intacta", usuarios.para_login("rita@ejemplo.com") is not None)
    rev("las dos respuestas 404 son iguales a las de un guid que no existe", c.delete(f"/admins-plataforma/{uuid.uuid4()}", params={"motivo": "x"}, headers=srv()).json() == r.json())

    titulo("8 · Retirar")
    r = c.delete(f"/admins-plataforma/{guid_beto}", headers=srv())
    rev("sin motivo: 422", r.status_code == 422, r.text[:200])
    r = c.delete(f"/admins-plataforma/{guid_beto}", params={"motivo": "   "}, headers=srv())
    rev("con un motivo en blanco: 400", r.status_code == 400, r.text[:200])
    rev("y no se retiró", "beto@ejemplo.com" in correos())
    r = c.delete(f"/admins-plataforma/{guid_beto}", params={"motivo": "Ya no trabaja aquí"}, headers=srv())
    rev("con motivo se retira: 200", r.status_code == 200 and r.json() == {"retirado": True, "email": "beto@ejemplo.com"}, r.text[:200])
    rev("ya no sale en la lista", "beto@ejemplo.com" not in correos())
    rev("ya no puede entrar", entrar("beto@ejemplo.com", "lo-que-sea").status_code == 401)
    r = c.delete(f"/admins-plataforma/{guid_beto}", params={"motivo": "otra vez"}, headers=srv())
    rev("retirarlo otra vez: 404", r.status_code == 404, r.text[:200])
    rev("su correo queda libre para darse de alta otra vez", crear("beto@ejemplo.com").status_code == 201)

    titulo("9 · No se retira al último administrador activo")
    # Quedan Ada (activa), Beto (nuevo, pendiente), Carla, Diego y Elena. Se retiran todos menos uno ACTIVO: los pendientes también
    # cuentan como administradores activos (`activo`), así que se retira a todos menos a Ada.
    for correo_a_retirar in ("beto@ejemplo.com", "carla@ejemplo.com", "diego@ejemplo.com", "elena@ejemplo.com", "gina@ejemplo.com"):
        g = next(a["guid"] for a in lista() if a["email"] == correo_a_retirar)
        r = c.delete(f"/admins-plataforma/{g}", params={"motivo": "limpieza de la prueba"}, headers=srv())
        assert r.status_code == 200, r.text
    rev("solo queda Ada", correos() == ["ada@ejemplo.com"])
    r = c.delete(f"/admins-plataforma/{guid_ada}", params={"motivo": "intento de dejar sin administrador"}, headers=srv())
    rev("retirar al último: 409 «ultimo_superadmin»", r.status_code == 409 and r.json()["detail"]["codigo"] == "ultimo_superadmin", r.text[:200])
    rev("y sigue ahí, pudiendo entrar", correos() == ["ada@ejemplo.com"] and entrar("ada@ejemplo.com", "Cobre-Lento-Azul-47!").status_code == 200)

    titulo("10 · Sesiones y enlaces al retirar")
    nuevo = crear("fabi@ejemplo.com").json()
    primer_acceso("fabi@ejemplo.com", nuevo["contrasenaTemporal"], "Rumbo-Sereno-Azul-47!")
    s = entrar("fabi@ejemplo.com", "Rumbo-Sereno-Azul-47!").json()
    rev("Fabi entra y tiene sesión", next(a for a in lista() if a["email"] == "fabi@ejemplo.com")["sesionActiva"] is True)
    c.delete(f"/admins-plataforma/{nuevo['administrador']['guid']}", params={"motivo": "prueba de sesiones"}, headers=srv())
    rev("al retirarla, su token deja de servir («el usuario ya no existe»)", c.get("/auth/yo", headers={**srv(), "Authorization": f"Bearer {s['accessToken']}"}).status_code in (401, 403, 404))
    rev("y no sale en la lista", "fabi@ejemplo.com" not in correos())

    titulo("11 · Sin la llave de servicio, nada")
    rev("listar sin llave: 401", c.get("/admins-plataforma/").status_code == 401)
    rev("crear sin llave: 401 y no crea nada", c.post("/admins-plataforma/", json={"email": "z@ejemplo.com", "nombre": "Z", "apellidos": "Z"}).status_code == 401 and "z@ejemplo.com" not in correos())
    rev("regenerar sin llave: 401", c.post(f"/admins-plataforma/{guid_ada}/reenviar-invitacion").status_code == 401)
    rev("retirar sin llave: 401", c.delete(f"/admins-plataforma/{guid_ada}", params={"motivo": "x"}).status_code == 401)
    rev("con una llave equivocada: 401", c.get("/admins-plataforma/", headers={"x-api-key": "otra"}).status_code == 401)

    titulo("12 · Rastro")
    creados = eventos("USER_CREATED")
    rev("cada alta deja USER_CREATED con actor «monitor» y qué pasó con el correo", any(e.get("actor") == "monitor" and e.get("correo") == "enviado" for e in creados) and any(e.get("correo") == "sin_canal" for e in creados) and any(e.get("correo") == "fallo" for e in creados) and any(e.get("correo") == "omitido" for e in creados), str(creados[:2]))
    rev("el alta anota el rol y que no es de ninguna organización", all(e.get("rol") == "ADMIN_PLATAFORMA" and e.get("tenantGuid") is None for e in creados if e.get("actor") == "monitor"))
    rev("regenerar deja INVITATION_REISSUED con actor «monitor»", any(e.get("usuarioGuid") == guid_beto and e.get("actor") == "monitor" for e in eventos("INVITATION_REISSUED")))
    borrados = eventos("USER_DELETED")
    rev("retirar deja USER_DELETED con actor, motivo y correo", any(e.get("email") == "beto@ejemplo.com" and e.get("motivo") == "Ya no trabaja aquí" and e.get("actor") == "monitor" and e.get("esAdminPlataforma") is True for e in borrados), str(borrados[:1]))
    todo = (RAIZ / ".registro" / "auditoria.jsonl").read_text(encoding="utf-8")
    rev("ningún renglón de la bitácora lleva una contraseña temporal (se revisaron todas las que las rutas devolvieron)", len(temporales_devueltas) >= 8 and not any(t in todo for t in temporales_devueltas) and "contrasenaTemporal" not in todo, str(len(temporales_devueltas)))
    rev("los intentos rechazados no dejan alta ni retiro", sum(1 for e in borrados if e.get("motivo") == "intento de dejar sin administrador") == 0)

    titulo("13 · Los scripts de siempre siguen funcionando con lo creado desde aquí")
    rev("un administrador creado por aquí es el mismo que ve `bootstrap_admin` (no se puede repetir)", crear("ada@ejemplo.com").status_code == 409 and usuarios.para_login("ada@ejemplo.com")["esAdminPlataforma"] is True)
finally:
    sin_canal()
    shutil.rmtree(RAIZ, ignore_errors=True)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
