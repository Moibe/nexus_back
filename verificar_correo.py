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
