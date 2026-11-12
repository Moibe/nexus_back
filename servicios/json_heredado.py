"""Lectura de los registros JSONL que el producto usó hasta el 2026-11-11, SOLO para importarlos una vez a SQL.

Hasta esa fecha, donde no había base, todo vivía en archivos de solo agregar en `{ALMACEN_RUTA}/.registro/` (un evento por línea). Las
migraciones `0006`, `0008` y `0014` llevaron esos registros a SQL Server, y desde el 2026-11-11 (Moibe: «si házlo», sobre quitar los caminos
JSONL) el producto ya no lee ni escribe esos archivos: aquí solo quedan las funciones que los LEEN, con las MISMAS reglas que tenían los
módulos que los escribían, para que `servicios/importacion_json.py` importe cada registro una vez en cada ambiente.

Hace falta mientras quede un ambiente sin importar: dev ya importó todo; staging y producción importan lo suyo la primera vez que arrancan
con este código (al promover). Cuando los tres lo hayan hecho, este módulo se puede borrar junto con los importadores.

Una línea ilegible se salta con aviso en el log; el resto se lee igual. Se lee en binario y línea por línea para que un corte a media escritura
(una «á» partida a la mitad) no impida leer el archivo entero.
"""

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config
from servicios import almacen
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

# El nombre lo arma el código, nunca el usuario, pero se valida igual: es parte de una ruta en disco.
_RE_NOMBRE = re.compile(r"^[A-Za-z0-9_-]{1,100}\.jsonl$")

TENANTS = "tenants.jsonl"
CLASIFICADORES = "clasificadores.jsonl"
AUDITORIA = "auditoria.jsonl"
TOKENS = "tokens.jsonl"
LLAVES = "llaves.jsonl"
USO_LLAVES = "uso-llaves.jsonl"
WEBHOOKS = "webhooks.jsonl"
ENTREGAS_WEBHOOKS = "entregas-webhooks.jsonl"
USUARIOS = "usuarios.jsonl"
ESTADOS_WEBHOOK = ("activo", "inactivo")


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def fecha(iso) -> datetime | None:
    """Una fecha ISO de un evento, o `None` si no se puede leer."""
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def fecha_de_uso(e: dict) -> datetime | None:
    """La hora de una línea de `uso-llaves.jsonl` (sin zona se toma por UTC)."""
    try:
        f = datetime.fromisoformat(e["en"])
        return f if f.tzinfo else f.replace(tzinfo=timezone.utc)
    except (KeyError, ValueError, TypeError):
        return None


def ruta(nombre: str) -> Path:
    if not _RE_NOMBRE.match(nombre):
        raise ValueError(f"Nombre de registro inválido: {nombre!r}")
    almacen._exigir_raiz_montada()
    return almacen._raiz() / ".registro" / nombre


def eventos(nombre: str) -> list[dict]:
    """Todos los eventos del registro, en el orden en que se escribieron. Una
    línea ilegible se salta (con aviso en el log): perder UN evento es mucho
    mejor que dejar de leer el registro entero."""
    origen = ruta(nombre)
    leidos = []
    if True:  # solo lectura: ya nadie escribe estos archivos
        try:
            if not origen.exists():
                return []
            # En BINARIO y decodificando línea por línea, no en modo texto: un
            # corte a media escritura puede dejar un carácter de dos bytes (una
            # "á" de un nombre) a la mitad, y en modo texto eso levanta
            # UnicodeDecodeError al leer el ARCHIVO ENTERO — o sea, las llaves
            # de todos los clientes dejaban de verificarse por una sola línea.
            # Así se salta esa línea y nada más.
            with open(origen, "rb") as f:
                for numero, cruda in enumerate(f, start=1):
                    cruda = cruda.strip()
                    if not cruda:
                        continue
                    try:
                        evento = json.loads(cruda.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        logger.warning("Línea %s ilegible en %s; se ignora", numero, origen)
                        continue
                    if isinstance(evento, dict):
                        leidos.append(evento)
                    else:
                        logger.warning("Línea %s de %s no es un evento; se ignora", numero, origen)
        except OSError as exc:
            logger.exception("No se pudo leer el registro %s", origen)
            raise ErrorAlmacen(f"No se pudo leer el registro {nombre}: {exc}") from exc
    return leidos


def tenants() -> dict[str, dict]:
    """Las organizaciones vivas, por guid, en el orden en que se crearon."""
    tenants: dict[str, dict] = {}
    for e in eventos("tenants.jsonl"):
        tipo = e.get("evento")
        guid = e.get("guid")
        if not isinstance(guid, str):
            continue
        if tipo == "creada":
            tenants.setdefault(
                guid,
                {
                    "guid": guid,
                    "nombre": e.get("nombre", ""),
                    "slug": e.get("slug", ""),
                    "codigo": e.get("codigo", ""),
                    "estado": "ACTIVE",
                    "adminGuid": e.get("adminGuid"),
                    "recuperacion": e.get("recuperacion") or {},
                    "creadaEn": e.get("en"),
                },
            )
        elif tipo == "estado" and guid in tenants:
            tenants[guid]["estado"] = e.get("estado", "ACTIVE")
    return tenants


def clasificadores() -> dict[str, str]:
    """`{tenant: procesadorId}` del registro. Si una organización tiene varios eventos gana el último (se reasigna cuando el
    procesador se borró a mano y hubo que crear otro)."""
    asignados: dict[str, str] = {}
    for e in eventos("clasificadores.jsonl"):
        if e.get("evento") == "asignado" and isinstance(e.get("tenant"), str) and isinstance(e.get("procesadorId"), str):
            asignados[e["tenant"]] = e["procesadorId"]
    return asignados


def tokens() -> dict[str, dict]:
    """Los tokens por su hash, reconstruidos de los eventos."""
    tokens: dict[str, dict] = {}
    for e in eventos(TOKENS):
        tipo_evento = e.get("evento")
        clave = e.get("hash")
        if tipo_evento == "creado" and isinstance(clave, str):
            tokens[clave] = {
                "guid": e.get("guid"),
                "hash": clave,
                "usuarioGuid": e.get("usuarioGuid"),
                "tipo": e.get("tipo"),
                "expira": e.get("expira"),
                "creadoEn": e.get("en"),
                "usadoEn": None,
                "invalidadoEn": None,
                "motivo": None,
            }
            continue
        t = tokens.get(clave) if isinstance(clave, str) else None
        if t is None:
            continue
        if tipo_evento == "usado":
            t["usadoEn"] = e.get("en")
        elif tipo_evento == "invalidado":
            t["invalidadoEn"] = e.get("en")
            t["motivo"] = e.get("motivo")
    return tokens


def llaves() -> tuple[dict[str, dict], dict[str, str]]:
    """Las llaves emitidas por id, y cuándo se revocó cada una."""
    emitidas: dict[str, dict] = {}
    revocadas: dict[str, str] = {}
    for e in eventos(LLAVES):
        if e.get("evento") == "emitida" and isinstance(e.get("id"), str):
            # Si un id apareciera dos veces (no debería: se sortea contra los
            # existentes), gana la PRIMERA — nadie puede "reemitir" una llave
            # ajena escribiendo otra con el mismo identificador.
            emitidas.setdefault(e["id"], e)
        elif e.get("evento") == "revocada" and isinstance(e.get("id"), str):
            revocadas.setdefault(e["id"], e.get("en", ""))
    return emitidas, revocadas


def webhooks() -> tuple[dict[str, dict], dict[str, str], set[str], dict[str, str], dict[str, dict]]:
    """Los webhooks registrados por id, el estado vigente de cada uno, los
    eliminados, cuándo se validó cada uno, y su última validación fallida."""
    registrados: dict[str, dict] = {}
    estados: dict[str, str] = {}
    eliminados: set[str] = set()
    validados: dict[str, str] = {}
    fallidas: dict[str, dict] = {}
    for e in eventos(WEBHOOKS):
        identificador = e.get("id")
        if not isinstance(identificador, str):
            continue
        tipo = e.get("evento")
        if tipo == "registrado":
            # Si un id apareciera dos veces (no debería: se sortea contra los
            # existentes), gana el PRIMERO, como en las llaves.
            registrados.setdefault(identificador, e)
        elif tipo == "estado":
            # El último gana: es el vigente. Un estado ilegible se lee como
            # INACTIVO, nunca como activo: un webhook mal leído no debe amanecer
            # mandando avisos a un endpoint que alguien había pausado. Misma
            # regla que tenía el front cuando vivían en el navegador.
            estados[identificador] = e["estado"] if e.get("estado") in ESTADOS_WEBHOOK else "inactivo"
        elif tipo == "eliminado":
            eliminados.add(identificador)
        elif tipo == "validado" and isinstance(e.get("en"), str):
            # El último gana: es la validación vigente.
            validados[identificador] = e["en"]
        elif tipo == "validacion_fallida" and isinstance(e.get("en"), str):
            fallidas[identificador] = e
        elif tipo == "editado" and identificador in registrados:
            # Nueva URL y eventos; el secret es el mismo. Si cambió la URL, la
            # validación anterior (buena o fallida) era de otro endpoint y ya no
            # vale. Un `editado` sin la marca se lee como cambio de URL: ante la
            # duda, que se vuelva a validar.
            registrados[identificador] = {
                **registrados[identificador],
                "url": e.get("url", registrados[identificador].get("url")),
                "eventos": e.get("eventos", registrados[identificador].get("eventos")),
            }
            if e.get("urlCambio", True):
                validados.pop(identificador, None)
                fallidas.pop(identificador, None)
    return registrados, estados, eliminados, validados, fallidas


def _normalizar_correo(correo: str) -> str:
    return correo.strip().lower()


def usuarios_indice(
    eventos: list[dict], retirados: dict[str, dict] | None = None
) -> tuple[dict[str, dict], dict[str, str], dict[str, dict]]:
    """(usuarios por guid, guid por correo, sesiones por guid de sesión), repasando
    los eventos en el orden en que se escribieron. Es LINEAL en el número de
    eventos: hasta el 2026-10-12 cada revocación recorría todas las sesiones, y con
    miles de ellas el repaso tardaba segundos.

    Con `retirados` (lo usa la importación a SQL) guarda ahí, además, cada cuenta tal
    como estaba al retirarse, con la fecha en `eliminadoEn`: en la base se quedan como
    filas, porque su rastro (sesiones, intentos, enlaces) las sigue apuntando."""
    usuarios: dict[str, dict] = {}
    por_correo: dict[str, str] = {}
    sesiones: dict[str, dict] = {}
    de_usuario: dict[str, list[dict]] = {}  # las sesiones de cada persona, para "cerrar todas"
    for e in eventos:
        tipo = e.get("evento")
        guid = e.get("guid")
        if tipo == "creado" and isinstance(guid, str):
            if guid in usuarios:
                continue  # gana el primero, nadie "recrea" a otro
            # El diseño del perfil (HU12) pide los apellidos por separado, igual que `security.users` (lastName, secondLastName).
            # Los eventos nuevos ya los traen; los de antes se separan al leerlos (`separar_nombre`): unos traían `apellidos`
            # y otros —los del formulario, hasta el 2026-10-14— el nombre completo entero en `nombre` y `apellidos` vacío.
            if "apellidoPaterno" in e:
                nombres, paterno, materno = e.get("nombre", ""), e.get("apellidoPaterno") or "", e.get("apellidoMaterno") or ""
                apellidos = e.get("apellidos", "")
            else:
                nombres, paterno, materno = _separar_nombre(e.get("nombre", ""), e.get("apellidos", ""))
                apellidos = " ".join(x for x in (paterno, materno) if x)
            usuarios[guid] = {
                "guid": guid,
                "email": e.get("email", ""),
                "nombre": nombres,
                "apellidos": apellidos,
                "apellidoPaterno": paterno,
                "apellidoMaterno": materno,
                "recuperacion": e.get("recuperacion") or {},
                "recuperacionPropia": "recuperacion" in e,
                "esAdminPlataforma": bool(e.get("esAdminPlataforma")),
                "telefono": e.get("telefono"),
                "hash": e.get("hash"),
                "debeCambiar": bool(e.get("debeCambiar", True)),
                "contrasenaCambiadaEn": None,
                "fallidos": 0,
                "bloqueadoHasta": None,
                "ultimoLoginEn": None,
                "activo": True,
                "creadoEn": e.get("en"),
                # Cuándo se emitió la contraseña temporal vigente: el alta, o la última reemisión. De aquí
                # cuenta la vigencia del primer acceso (`acceso_vencido`).
                "temporalEmitidaEn": e.get("en"),
                "membresias": [],
            }
            por_correo.setdefault(_normalizar_correo(usuarios[guid]["email"]), guid)
            continue
        u = usuarios.get(guid) if isinstance(guid, str) else None
        if tipo == "contrasena" and u:
            u["hash"] = e.get("hash")
            u["debeCambiar"] = False
            u["contrasenaCambiadaEn"] = e.get("en")
            u["fallidos"] = 0
            u["bloqueadoHasta"] = None
        elif tipo == "eliminado" and u:
            # Borrado LÓGICO: la cuenta deja de existir para todo (login, listados, sesiones) y su correo queda libre
            # para darse de alta otra vez. Sus renglones se quedan en el registro, que es de solo agregar.
            correo = _normalizar_correo(u["email"])
            if por_correo.get(correo) == guid:
                del por_correo[correo]
            if retirados is not None:
                retirados[guid] = {**u, "eliminadoEn": e.get("en")}
            del usuarios[guid]
        elif tipo == "invitacion" and u:
            # Una contraseña temporal NUEVA para una cuenta que sigue en su primer acceso: la anterior deja de
            # servir y la vigencia empieza otra vez (`emitir_temporal`).
            u["hash"] = e.get("hash")
            u["debeCambiar"] = True
            u["temporalEmitidaEn"] = e.get("en")
            u["fallidos"] = 0
            u["bloqueadoHasta"] = None
        elif tipo == "intento" and u:
            if e.get("exitoso"):
                u["fallidos"] = 0
                u["bloqueadoHasta"] = None
                u["ultimoLoginEn"] = e.get("en")
            elif e.get("motivo") == "BAD_PASSWORD":
                u["fallidos"] += 1
                if u["fallidos"] >= config.AUTH_MAX_INTENTOS:
                    hasta = fecha(e.get("en")) or _ahora()
                    u["bloqueadoHasta"] = (hasta + timedelta(minutes=config.AUTH_BLOQUEO_MIN)).isoformat()
                    u["fallidos"] = 0
            # LOCKED / INACTIVE / UNKNOWN_EMAIL / EXPIRED: bitácora, no cuentan.
        elif tipo == "estado" and u:
            u["activo"] = bool(e.get("activo"))
        elif tipo == "datos" and u:
            u["telefono"] = e.get("telefono", u.get("telefono"))
            if "apellidoPaterno" in e:
                u["nombre"] = e.get("nombre", u["nombre"])
                u["apellidos"] = e.get("apellidos", u["apellidos"])
                u["apellidoPaterno"] = e.get("apellidoPaterno") or ""
                u["apellidoMaterno"] = e.get("apellidoMaterno") or ""
            else:
                # Un evento de antes del arreglo del 2026-10-14: la edición del formulario mandaba el nombre completo en `nombre`.
                nombres, paterno, materno = _separar_nombre(e.get("nombre", u["nombre"]), e.get("apellidos", u["apellidos"]))
                u["nombre"], u["apellidoPaterno"], u["apellidoMaterno"] = nombres, paterno, materno
                u["apellidos"] = " ".join(x for x in (paterno, materno) if x)
            if "recuperacion" in e:
                u["recuperacion"] = e.get("recuperacion") or {}
                u["recuperacionPropia"] = True
        elif tipo == "membresia" and u:
            tenant = e.get("tenantGuid")
            if isinstance(tenant, str):
                u["membresias"] = [m for m in u["membresias"] if m["tenantGuid"] != tenant]
                u["membresias"].append({"tenantGuid": tenant, "rol": e.get("rol", "ADMIN")})
        elif tipo == "sesion" and u and isinstance(e.get("sesionGuid"), str):
            if e["sesionGuid"] not in sesiones:  # gana la primera, como con los usuarios
                nueva = {
                    "sesionGuid": e["sesionGuid"],
                    "guid": guid,
                    "refreshHash": e.get("refreshHash"),
                    "expira": e.get("expira"),
                    "creadaEn": e.get("en"),
                    "ip": e.get("ip"),
                    "userAgent": e.get("userAgent"),
                    "revocadaEn": None,
                    "revocadaPor": None,
                }
                sesiones[e["sesionGuid"]] = nueva
                de_usuario.setdefault(guid, []).append(nueva)
        elif tipo == "sesion_revocada":
            # Una sesión concreta (logout, rotación) o, sin `sesionGuid`, TODAS las
            # que esa persona tenía hasta este punto del registro.
            objetivo = e.get("sesionGuid")
            if objetivo:
                # `isinstance`: una línea a mano con una lista o un diccionario en vez del
                # guid no es hashable y haría tronar TODO el repaso; antes simplemente no
                # coincidía con ninguna sesión.
                afectadas = [sesiones[objetivo]] if isinstance(objetivo, str) and objetivo in sesiones else []
            else:
                afectadas = de_usuario.get(guid, []) if isinstance(guid, str) else []
            for s in afectadas:
                if not s["revocadaEn"]:
                    s["revocadaEn"] = e.get("en")
                    s["revocadaPor"] = e.get("motivo")
    return usuarios, por_correo, sesiones


def _separar_nombre(nombre: str, apellidos: str = "") -> tuple[str, str, str]:
    from servicios.usuarios import separar_nombre

    return separar_nombre(nombre, apellidos)

