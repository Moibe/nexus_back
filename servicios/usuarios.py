"""Usuarios, credenciales, intentos de login y sesiones — registro PROVISIONAL.

Lo definitivo son las tablas y los SPs pedidos al DBA en **NEX-319**
(`docs/solicitudes-dba.md`, sección 6). Mientras llegan, todo vive en un
registro de solo agregar en el NAS, `usuarios.jsonl` (ver `servicios/registro.py`),
como las llaves y los webhooks. El día que existan los SPs, cambia ESTE módulo y
nada más: cada función de aquí es el espejo de un SP, con el mismo contrato.

    bootstrap_admin            ↔ security.uspBootstrapPlatformAdmin
    crear_usuario              ↔ (HU02/HU06: alta de usuario dentro de un tenant)
    actualizar_perfil          ↔ (HU12: sus propios datos y los de recuperación)
    agregar_membresia          ↔ (membresía con rol: security.tenantMemberships)
    para_login                 ↔ security.uspGetUserForLogin
    registrar_resultado_login  ↔ security.uspRecordLoginResult
    fijar_contrasena           ↔ security.uspSetPassword
    crear_sesion               ↔ security.uspCreateSession
    sesion_por_hash            ↔ security.uspGetSession
    revocar_sesiones           ↔ security.uspRevokeSessions

## Qué se guarda, y qué NUNCA

El hash argon2id de la contraseña (autocontenido: algoritmo, sal y parámetros
van dentro del string), nunca la contraseña. El SHA-256 del refresh token,
nunca el token. Los JWT de acceso no se guardan: se verifican por firma.

## La política de intentos vive aquí (como vivirá en el SP)

5 intentos fallidos seguidos bloquean la cuenta 15 minutos; el bloqueo se
levanta solo. Un acierto, o cambiar la contraseña, pone el contador en cero. Un
intento hecho DURANTE el bloqueo se anota (bitácora) pero no cuenta: si contara,
cada intento alargaría el castigo y nunca se saldría.

## El estado no se guarda, se calcula

Igual que en las llaves: el registro trae hechos (`creado`, `contrasena`,
`intento`, `sesion`, `sesion_revocada`, `estado`) y el índice los repasa en
orden. `fallidos` y `bloqueadoHasta` salen de repasar los intentos.
"""

import logging
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import config
from servicios import auditoria, registro, roles

logger = logging.getLogger(__name__)

ARCHIVO = "usuarios.jsonl"

MOTIVOS_CONTRASENA = {"FIRST_LOGIN", "RESET", "CHANGE"}
# Con estos motivos, cambiar la contraseña cierra todas las sesiones: la
# contraseña anterior era temporal o estaba comprometida.
MOTIVOS_QUE_CIERRAN_SESIONES = {"FIRST_LOGIN", "RESET"}

_RE_CORREO = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class YaExiste(Exception):
    """El correo ya está registrado. (Varios administradores de plataforma sí se
    permiten; lo que no se repite es el correo.)"""


class NoEncontrado(Exception):
    pass


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _fecha(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso)
    except ValueError:
        return None


def correo_valido(correo: str) -> bool:
    return bool(_RE_CORREO.match(correo)) and len(correo) <= 254


def _normalizar_correo(correo: str) -> str:
    return correo.strip().lower()


def _construir_indice(eventos: list[dict]) -> tuple[dict[str, dict], dict[str, str], dict[str, dict]]:
    """(usuarios por guid, guid por correo, sesiones por guid de sesión), repasando
    los eventos en el orden en que se escribieron. Es LINEAL en el número de
    eventos: hasta el 2026-10-12 cada revocación recorría todas las sesiones, y con
    miles de ellas el repaso tardaba segundos."""
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
            usuarios[guid] = {
                "guid": guid,
                "email": e.get("email", ""),
                "nombre": e.get("nombre", ""),
                "apellidos": e.get("apellidos", ""),
                # El diseño del perfil (HU12) pide los apellidos por separado,
                # igual que `security.users` (lastName, secondLastName). Los
                # usuarios creados antes solo tienen `apellidos`: se parte por
                # el primer espacio, que es lo más cercano a la verdad.
                "apellidoPaterno": e.get("apellidoPaterno") or (e.get("apellidos", "").split(" ", 1) + [""])[0],
                "apellidoMaterno": e.get("apellidoMaterno") or (e.get("apellidos", "").split(" ", 1) + [""])[1],
                "recuperacion": e.get("recuperacion") or {},
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
        elif tipo == "intento" and u:
            if e.get("exitoso"):
                u["fallidos"] = 0
                u["bloqueadoHasta"] = None
                u["ultimoLoginEn"] = e.get("en")
            elif e.get("motivo") == "BAD_PASSWORD":
                u["fallidos"] += 1
                if u["fallidos"] >= config.AUTH_MAX_INTENTOS:
                    hasta = _fecha(e.get("en")) or _ahora()
                    u["bloqueadoHasta"] = (hasta + timedelta(minutes=config.AUTH_BLOQUEO_MIN)).isoformat()
                    u["fallidos"] = 0
            # LOCKED / INACTIVE / UNKNOWN_EMAIL: bitácora, no cuentan.
        elif tipo == "estado" and u:
            u["activo"] = bool(e.get("activo"))
        elif tipo == "datos" and u:
            u["nombre"] = e.get("nombre", u["nombre"])
            u["apellidos"] = e.get("apellidos", u["apellidos"])
            u["telefono"] = e.get("telefono", u.get("telefono"))
            if "apellidoPaterno" in e:
                u["apellidoPaterno"] = e.get("apellidoPaterno") or ""
                u["apellidoMaterno"] = e.get("apellidoMaterno") or ""
            elif "apellidos" in e:
                partes = (e.get("apellidos", "").split(" ", 1) + [""])
                u["apellidoPaterno"], u["apellidoMaterno"] = partes[0], partes[1]
            if "recuperacion" in e:
                u["recuperacion"] = e.get("recuperacion") or {}
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


# ── Caché del índice ────────────────────────────────────────────────────────
#
# Hasta el 2026-10-12 `_indice()` repasaba el registro ENTERO en cada llamada, y
# estaba bien: lo llamaban las acciones de una persona. Desde la 2ª etapa de HU05
# lo llama también el guard del front (`GET /auth/sesion`, una vez cada pocos
# segundos por sesión), y con un registro que solo crece eso ya no es aceptable.
# Medido el 2026-10-13: repasar 20 mil eventos con 10 mil sesiones tardaba de 1.4 a
# 2.2 s (ahora, de 12 a 60 ms), y a eso hay que sumarle leer y decodificar el archivo.
#
# El registro es de SOLO AGREGAR (`registro.agregar`; `bootstrap_admin.py` es otro
# proceso que también agrega), así que cada escritura cambia su tamaño y, mientras
# tamaño y fecha no cambien, el índice es el mismo: se guarda junto con la "firma"
# del archivo de la que salió, y se vuelve a construir solo cuando cambia. La firma
# y los eventos se toman bajo el mismo candado que las escrituras, para que nunca
# queden de instantes distintos.
#
# Lo que NO se nota: una edición EN SITIO que deje el mismo tamaño y la misma
# fecha (dos modificaciones dentro del mismo tick del reloj). Contradice el
# contrato del registro ("nunca se reescribe una línea"); solo la hace a propósito
# `verificar_auth.py`, que después pone `_cache_indice = None`.
#
# Lo que devuelve es COMPARTIDO: quien lo recibe no debe modificarlo. Ninguna
# función de este módulo lo hace (el estado cambia agregando eventos, no editando
# el índice), y `_publico` entrega copias de lo anidado.
_cache_indice: tuple[tuple, tuple] | None = None

#: Reconstruir el índice más lento que esto se avisa en el log (ver `_indice`).
UMBRAL_RECONSTRUCCION_S = 0.5


def _firma_del_registro() -> tuple | None:
    """Lo que identifica el estado del archivo: cuál es, qué tan grande y desde
    cuándo. `None` si no existe o no se puede leer (entonces no se usa la caché)."""
    ruta = registro.ruta(ARCHIVO)
    try:
        st = ruta.stat()
    except OSError:
        return None
    return str(ruta), st.st_ino, st.st_size, st.st_mtime_ns


def _indice() -> tuple[dict[str, dict], dict[str, str], dict[str, dict]]:
    """(usuarios por guid, guid por correo, sesiones por guid de sesión)."""
    global _cache_indice
    with registro.candado:
        firma = _firma_del_registro()
        guardado = _cache_indice
        if firma is not None and guardado is not None and guardado[0] == firma:
            return guardado[1]
        inicio = time.perf_counter()
        indice = _construir_indice(registro.eventos(ARCHIVO))
        duracion = time.perf_counter() - inicio
        if duracion > UMBRAL_RECONSTRUCCION_S:
            # El guard del front espera esto como mucho 2 s y, pasado ese tope, deja pasar
            # sin comprobar la sesión. Es la señal de que el registro provisional ya creció
            # más de lo que debe: hay que cambiarlo por la base.
            logger.warning(
                "Reconstruir el índice de usuarios tardó %.2f s (%s bytes): el registro provisional ya es grande",
                duracion,
                firma[2] if firma else "?",
            )
        if firma is not None:
            _cache_indice = (firma, indice)
        return indice


def _bloqueo_vigente(usuario: dict) -> datetime | None:
    hasta = _fecha(usuario.get("bloqueadoHasta"))
    if hasta and hasta > _ahora():
        return hasta
    return None


def _publico(u: dict) -> dict:
    """El usuario como viaja al front: SIN el hash."""
    return {
        "guid": u["guid"],
        "email": u["email"],
        "nombre": u["nombre"],
        "apellidos": u["apellidos"],
        "apellidoPaterno": u.get("apellidoPaterno", ""),
        "apellidoMaterno": u.get("apellidoMaterno", ""),
        "telefono": u.get("telefono"),
        "recuperacion": dict(u.get("recuperacion") or {}),
        "esAdminPlataforma": u["esAdminPlataforma"],
        "debeCambiarContrasena": u["debeCambiar"],
        "activo": u["activo"],
        "ultimoLoginEn": u["ultimoLoginEn"],
        "creadoEn": u["creadoEn"],
        "membresias": [dict(m) for m in u["membresias"]],
    }


# ── uspBootstrapPlatformAdmin ───────────────────────────────────────────────


def bootstrap_admin(email: str, nombre: str, apellidos: str, hash_contrasena: str) -> dict:
    """Crea un administrador de plataforma con contraseña temporal
    (`debeCambiar`). Puede haber varios (decisión del 2026-10-09): cada corrida
    crea otro. Solo levanta `YaExiste` si el correo ya está registrado."""
    email = _normalizar_correo(email)
    if not correo_valido(email):
        raise ValueError("El correo no es válido.")
    nombre, apellidos = nombre.strip(), apellidos.strip()
    if not nombre or not apellidos:
        raise ValueError("Nombre y apellidos son obligatorios.")
    with registro.candado:
        usuarios, por_correo, _ = _indice()
        if email in por_correo:
            raise YaExiste("Ya existe un usuario con ese correo.")
        guid = str(uuid.uuid4())
        registro.agregar(
            ARCHIVO,
            {
                "evento": "creado",
                "guid": guid,
                "email": email,
                "nombre": nombre,
                "apellidos": apellidos,
                "esAdminPlataforma": True,
                "hash": hash_contrasena,
                "debeCambiar": True,
                "en": _ahora().isoformat(timespec="seconds"),
            },
        )
    return por_guid(guid)


def crear_usuario(
    email: str,
    nombre: str,
    apellidos: str,
    hash_contrasena: str,
    telefono: str | None = None,
    tenant_guid: str | None = None,
    rol: str = "ADMIN",
) -> dict:
    """Da de alta un usuario con contraseña temporal (`debeCambiar`) y, si se
    indica un tenant, su membresía con rol. Levanta `YaExiste` si el correo ya
    está registrado — el diseño lo dice tal cual bajo el campo."""
    email = _normalizar_correo(email)
    if not correo_valido(email):
        raise ValueError("El correo no es válido.")
    nombre, apellidos = nombre.strip(), apellidos.strip()
    if not nombre:
        raise ValueError("El nombre es obligatorio.")
    with registro.candado:
        _, por_correo, _ = _indice()
        if email in por_correo:
            raise YaExiste("El correo electrónico ingresado ya se encuentra registrado, por favor intenta nuevamente")
        guid = str(uuid.uuid4())
        en = _ahora().isoformat(timespec="seconds")
        registro.agregar(
            ARCHIVO,
            {
                "evento": "creado",
                "guid": guid,
                "email": email,
                "nombre": nombre,
                "apellidos": apellidos,
                "telefono": telefono,
                "esAdminPlataforma": False,
                "hash": hash_contrasena,
                "debeCambiar": True,
                "en": en,
            },
        )
        if tenant_guid:
            registro.agregar(
                ARCHIVO,
                {"evento": "membresia", "guid": guid, "tenantGuid": tenant_guid, "rol": rol, "en": en},
            )
    return por_guid(guid)


def agregar_membresia(guid: str, tenant_guid: str, rol: str = "ADMIN") -> dict:
    with registro.candado:
        usuarios, _, _ = _indice()
        if guid not in usuarios:
            raise NoEncontrado(guid)
        registro.agregar(
            ARCHIVO,
            {
                "evento": "membresia",
                "guid": guid,
                "tenantGuid": tenant_guid,
                "rol": rol,
                "en": _ahora().isoformat(timespec="seconds"),
            },
        )
    return por_guid(guid)


def actualizar_datos(guid: str, nombre: str, apellidos: str = "", telefono: str | None = None) -> dict:
    """Cambia nombre, apellidos y teléfono. El correo NO se toca: es la
    identidad con la que entra, y cambiarlo es otra historia."""
    nombre, apellidos = nombre.strip(), (apellidos or "").strip()
    if not nombre:
        raise ValueError("El nombre es obligatorio.")
    with registro.candado:
        usuarios, _, _ = _indice()
        if guid not in usuarios:
            raise NoEncontrado(guid)
        registro.agregar(
            ARCHIVO,
            {
                "evento": "datos",
                "guid": guid,
                "nombre": nombre,
                "apellidos": apellidos,
                "telefono": telefono,
                "en": _ahora().isoformat(timespec="seconds"),
            },
        )
    return por_guid(guid)


def normalizar_recuperacion(recuperacion: dict | None) -> dict:
    """Deja los datos de recuperación como se van a guardar: el correo sin
    espacios y en minúsculas, igual que cualquier otro correo del sistema.
    Levanta `ValueError` si trae algo que no es un correo. Vacío es válido: el
    campo es opcional.

    Existe para que los dos sitios que reciben recuperación —el alta de
    organización y Mi perfil— apliquen la MISMA regla. Hasta el 2026-10-09 el
    alta de organización no miraba nada y guardaba "pepito" tal cual, y Mi
    perfil validaba sobre una copia pero guardaba el texto crudo, con sus
    espacios y mayúsculas."""
    rec = dict(recuperacion or {})
    # Sin datos, se devuelve el dict vacío tal cual: vaciar la recuperación deja
    # `{}`, no `{"email": None, "telefono": None}`, que es lo que ya esperan el
    # registro y el front.
    if not rec:
        return {}
    correo = (rec.get("email") or "").strip().lower()
    if correo and not correo_valido(correo):
        raise ValueError("El correo de recuperación no es válido.")
    if "email" in rec:
        rec["email"] = correo or None
    if "telefono" in rec:
        telefono = (rec.get("telefono") or "").strip()
        rec["telefono"] = telefono or None
    return rec


def actualizar_perfil(
    guid: str,
    nombre: str,
    apellido_paterno: str,
    apellido_materno: str,
    telefono: str | None,
    recuperacion: dict | None,
) -> dict:
    """HU12: los datos que una persona cambia de SÍ MISMA. El correo con el que
    entra y su rol no están aquí: esos los mueve quien administra."""
    nombre = nombre.strip()
    if not nombre:
        raise ValueError("El nombre es obligatorio.")
    paterno, materno = apellido_paterno.strip(), apellido_materno.strip()
    with registro.candado:
        usuarios, _, _ = _indice()
        if guid not in usuarios:
            raise NoEncontrado(guid)
        registro.agregar(
            ARCHIVO,
            {
                "evento": "datos",
                "guid": guid,
                "nombre": nombre,
                "apellidos": " ".join(x for x in (paterno, materno) if x),
                "apellidoPaterno": paterno,
                "apellidoMaterno": materno,
                "telefono": telefono,
                "recuperacion": recuperacion or {},
                "en": _ahora().isoformat(timespec="seconds"),
            },
        )
    return por_guid(guid)


def de_tenant(tenant_guid: str) -> list[dict]:
    """Los usuarios de una organización, para su listado."""
    usuarios, _, _ = _indice()
    de_la_organizacion = [
        _publico(u)
        for u in usuarios.values()
        if any(m["tenantGuid"] == tenant_guid for m in u["membresias"])
    ]
    return sorted(de_la_organizacion, key=lambda u: u["creadoEn"] or "")


# ── uspGetUserForLogin ──────────────────────────────────────────────────────


def para_login(email: str) -> dict | None:
    """Lo que el login necesita, CON el hash (no sale de este proceso). `None`
    si el correo no existe."""
    usuarios, por_correo, _ = _indice()
    guid = por_correo.get(_normalizar_correo(email))
    if not guid:
        return None
    u = usuarios[guid]
    bloqueo = _bloqueo_vigente(u)
    return {**_publico(u), "hash": u["hash"], "fallidos": u["fallidos"], "bloqueadoHasta": bloqueo.isoformat() if bloqueo else None}


def por_guid(guid: str) -> dict:
    usuarios, _, _ = _indice()
    u = usuarios.get(guid)
    if not u:
        raise NoEncontrado(guid)
    return _publico(u)


def para_listado(u: dict, tenant_guid: str, posicion: int) -> dict:
    """El usuario como lo pinta un listado de la organización `tenant_guid`: su
    rol EN ESA organización, el número de orden que el diseño muestra como ID, y
    el nombre del rol ya resuelto (el front no debería traducir códigos).

    Lo usan los dos listados —el del administrador de la organización y el del
    administrador de plataforma— para que la pantalla reciba la misma forma."""
    rol = next((m["rol"] for m in u["membresias"] if m["tenantGuid"] == tenant_guid), None)
    return {
        "guid": u["guid"],
        "numero": f"{posicion:03d}",
        "nombre": f"{u['nombre']} {u['apellidos']}".strip(),
        "email": u["email"],
        "telefono": u.get("telefono"),
        "rol": rol,
        "rolNombre": roles.nombre_de(rol) if rol else "—",
        "activo": u["activo"],
        "creadoEn": u["creadoEn"],
        "debeCambiarContrasena": u["debeCambiarContrasena"],
    }


def por_guid_y_sesion(guid: str, sesion_guid: str) -> tuple[dict, bool]:
    """`(usuario, sesion_viva)`: lo mismo que `por_guid` y, EN LA MISMA lectura
    del registro, si la sesión del JWT (su `sid`) sigue viva.

    Es lo que usan los guards que validan un Bearer. Antes solo comprobaban
    firma y vencimiento, así que un JWT ya emitido seguía valiendo hasta
    vencer (15 minutos) aunque su sesión se hubiera cerrado: voluntariamente,
    por un administrador, al restablecer la contraseña o al desactivar la
    cuenta. Revocar los refresh tokens evitaba que se RENOVARA, pero no que el
    JWT en curso hiciera cosas —incluso crear usuarios y ascenderlos a
    administrador—.

    Una sesión está viva si existe, es de ESE usuario y no está revocada, sea
    cual sea el motivo. Eso incluye `ROTATED`: al renovar se emite una sesión
    nueva con su propio JWT, y el anterior ya venció (el front solo renueva
    cuando el JWT venció). Una sesión que no existe en el registro (un JWT de un
    almacén que se vació, o inventado con la firma correcta) tampoco está viva:
    ante la duda, se rechaza.

    No levanta nada por una sesión muerta: quien llama decide el código. Sí
    levanta `NoEncontrado` si el usuario no existe."""
    usuarios, _, sesiones = _indice()
    u = usuarios.get(guid)
    if not u:
        raise NoEncontrado(guid)
    s = sesiones.get(sesion_guid)
    viva = s is not None and s["guid"] == guid and not s["revocadaEn"]
    return _publico(u), viva


# ── uspRecordLoginResult ────────────────────────────────────────────────────


def registrar_resultado_login(
    guid: str | None,
    email: str,
    exitoso: bool,
    motivo: str | None,
    ip: str | None,
    user_agent: str | None,
    origen: str | None = None,
) -> tuple[int, str | None]:
    """Anota el intento y aplica la política de la CUENTA. Devuelve
    (fallidos, bloqueadoHasta) tal como quedan. `motivo`: BAD_PASSWORD | LOCKED
    | INACTIVE | UNKNOWN_EMAIL. `origen` identifica al navegador y es lo que
    lee `estado_origen`; aquí solo se guarda."""
    with registro.candado:
        registro.agregar(
            ARCHIVO,
            {
                "evento": "intento",
                "guid": guid,
                "email": _normalizar_correo(email),
                "exitoso": exitoso,
                "motivo": None if exitoso else motivo,
                "ip": ip,
                "userAgent": (user_agent or "")[:500] or None,
                "origen": origen,
                "en": _ahora().isoformat(timespec="seconds"),
            },
        )
        if not guid:
            return 0, None
        usuarios, _, _ = _indice()
        u = usuarios.get(guid)
        if not u:
            return 0, None
        bloqueo = _bloqueo_vigente(u)
        return u["fallidos"], bloqueo.isoformat() if bloqueo else None


# ── uspGetOriginLockout ─────────────────────────────────────────────────────
#
# El contador de la cuenta solo puede crecer con la contraseña mala: un correo
# que no existe no tiene cuenta donde anotar el intento. Para que los cinco
# intentos sean "de uno o de otro" (decisión de Moibe, 2026-10-09) se lleva un
# SEGUNDO contador por navegador, con el identificador anónimo que el front
# guarda en una cookie y manda como `origen`.
#
# Por navegador y no por IP: en la oficina todos salen por la misma IP, así que
# cinco errores de cualquiera bloquearían a todos los demás. El contador de la
# cuenta se queda como está: es el que sobrevive a cambiar de navegador.

#: Lo que hace avanzar al contador del navegador. `LOCKED` no cuenta —si no, los
#: intentos durante el bloqueo lo alargarían— ni `INACTIVE`, que no es un error
#: de credenciales.
MOTIVOS_QUE_CUENTAN = {"BAD_PASSWORD", "UNKNOWN_EMAIL"}


def estado_origen(origen: str | None) -> tuple[int, str | None]:
    """(fallidos seguidos, bloqueadoHasta) de ese navegador. Sin `origen`
    —un cliente que no manda cookie— devuelve el estado vacío: el contador de
    la cuenta sigue aplicando igual."""
    if not origen:
        return 0, None
    fallidos = 0
    hasta: datetime | None = None
    for e in registro.eventos(ARCHIVO):
        if e.get("origen") != origen or e.get("evento") not in ("intento", "origen_reiniciado"):
            continue
        # Restablecer la contraseña desde este navegador levanta SU bloqueo
        # (ver `reiniciar_intentos_de_origen`).
        if e.get("evento") == "origen_reiniciado":
            fallidos, hasta = 0, None
            continue
        if e.get("exitoso"):
            fallidos, hasta = 0, None
        elif e.get("motivo") in MOTIVOS_QUE_CUENTAN:
            fallidos += 1
            if fallidos >= config.AUTH_MAX_INTENTOS:
                desde = _fecha(e.get("en")) or _ahora()
                hasta = desde + timedelta(minutes=config.AUTH_BLOQUEO_MIN)
                fallidos = 0
    if hasta and hasta <= _ahora():
        hasta = None
    return fallidos, hasta.isoformat() if hasta else None


def reiniciar_intentos_de_origen(origen: str | None) -> None:
    """Pone en cero el contador y el bloqueo de ESE navegador.

    Lo llama el restablecimiento de contraseña (HU10). Sin esto, quien agotó
    sus cinco intentos, pidió el enlace y puso su contraseña nueva se topaba
    con "cuenta bloqueada" al intentar entrar: restablecer levantaba el bloqueo
    de la CUENTA (el evento `contrasena` pone `fallidos` y `bloqueadoHasta` en
    cero) pero no el del navegador, y el del navegador se revisa primero.

    Solo levanta el del navegador que completó el restablecimiento. Quien abrió
    el enlace en otro aparato sigue con el suyo hasta que venza: es lo
    prudente, porque el enlace solo lo tiene quien controla el correo, y un
    bloqueo ajeno no debería poder borrarse desde ahí."""
    if not origen:
        return
    registro.agregar(
        ARCHIVO,
        {"evento": "origen_reiniciado", "origen": origen, "en": _ahora().isoformat(timespec="seconds")},
    )


# ── uspSetPassword ──────────────────────────────────────────────────────────


def fijar_contrasena(guid: str, hash_contrasena: str, motivo: str) -> dict:
    if motivo not in MOTIVOS_CONTRASENA:
        raise ValueError(f"Motivo inválido: {motivo}")
    with registro.candado:
        usuarios, _, _ = _indice()
        if guid not in usuarios:
            raise NoEncontrado(guid)
        en = _ahora().isoformat(timespec="seconds")
        registro.agregar(ARCHIVO, {"evento": "contrasena", "guid": guid, "hash": hash_contrasena, "motivo": motivo, "en": en})
        if motivo in MOTIVOS_QUE_CIERRAN_SESIONES:
            registro.agregar(ARCHIVO, {"evento": "sesion_revocada", "guid": guid, "sesionGuid": None, "motivo": "PASSWORD_CHANGED", "en": en})
    return por_guid(guid)


# ── Sesiones (uspCreateSession / uspGetSession / uspRevokeSessions) ────────


class SesionNoCanjeable(Exception):
    """No se puede abrir la sesión que se pidió. `motivo`: `invalida` (el refresh no
    existe, ya se canjeó o se cerró, la cuenta está desactivada, o la contraseña con la
    que se entró dejó de ser la vigente), `vencida`, o `desactivada` (solo al crear la
    sesión de un login: la cuenta se desactivó mientras se verificaba la contraseña)."""

    def __init__(self, motivo: str):
        super().__init__(motivo)
        self.motivo = motivo


def crear_sesion(
    guid: str,
    refresh_hash: str,
    expira: datetime,
    ip: str | None,
    user_agent: str | None,
    hash_verificado: str | None = None,
) -> str:
    """Abre una sesión. Con `hash_verificado` (lo usa el login) comprueba, BAJO el candado,
    que la contraseña que se verificó siga siendo la vigente y la cuenta siga activa.

    Verificar una contraseña con argon2 tarda decenas de milisegundos y se hace FUERA del
    candado. Sin esta revisión, un restablecimiento de contraseña, un cierre forzado o una
    desactivación que ocurrieran en esa ventana dejaban pasar un login con la credencial
    vieja y su sesión nacía DESPUÉS de las revocaciones: viva y renovable por 7 días."""
    sesion_guid = str(uuid.uuid4())
    with registro.candado:
        usuarios, _, _ = _indice()
        if guid not in usuarios:
            raise NoEncontrado(guid)
        if hash_verificado is not None:
            if not usuarios[guid]["activo"]:
                raise SesionNoCanjeable("desactivada")
            if usuarios[guid]["hash"] != hash_verificado:
                raise SesionNoCanjeable("invalida")
        registro.agregar(
            ARCHIVO,
            {
                "evento": "sesion",
                "sesionGuid": sesion_guid,
                "guid": guid,
                "refreshHash": refresh_hash,
                "expira": expira.isoformat(timespec="seconds"),
                "ip": ip,
                "userAgent": (user_agent or "")[:500] or None,
                "en": _ahora().isoformat(timespec="seconds"),
            },
        )
    return sesion_guid


def rotar_sesion(
    refresh_hash: str,
    nuevo_refresh_hash: str,
    expira: datetime,
    ip: str | None,
    user_agent: str | None,
) -> tuple[str, dict]:
    """Canjea un refresh token por una sesión nueva, de forma ATÓMICA: devuelve
    `(guid de la sesión nueva, usuario)`, o levanta `SesionNoCanjeable`.

    Comprobar la sesión, revocarla y crear la sucesora se hace bajo UN solo candado. Antes
    eran tres llamadas sueltas, y entre ellas cabía todo:
      · dos peticiones con el mismo refresh (un refresh es de UN solo uso) lo canjeaban las
        dos, y de un token salían varias sesiones vivas (medido: siete de ocho);
      · un cierre forzado o un restablecimiento de contraseña que llegara a la mitad
        revocaba las sesiones que existían entonces, y la sucesora nacía después, viva.
    Es lo mismo que ya hace `tokens.consumir` con los enlaces de un solo uso."""
    with registro.candado:
        usuarios, _, sesiones = _indice()
        s = next((x for x in sesiones.values() if x["refreshHash"] == refresh_hash), None)
        u = usuarios.get(s["guid"]) if s else None
        if s is None or s["revocadaEn"] or u is None or not u["activo"]:
            raise SesionNoCanjeable("invalida")
        if (_fecha(s["expira"]) or _ahora()) <= _ahora():
            raise SesionNoCanjeable("vencida")
        en = _ahora().isoformat(timespec="seconds")
        registro.agregar(
            ARCHIVO,
            {"evento": "sesion_revocada", "guid": s["guid"], "sesionGuid": s["sesionGuid"], "motivo": "ROTATED", "en": en},
        )
        sesion_guid = str(uuid.uuid4())
        registro.agregar(
            ARCHIVO,
            {
                "evento": "sesion",
                "sesionGuid": sesion_guid,
                "guid": s["guid"],
                "refreshHash": nuevo_refresh_hash,
                "expira": expira.isoformat(timespec="seconds"),
                "ip": ip,
                "userAgent": (user_agent or "")[:500] or None,
                "en": en,
            },
        )
        return sesion_guid, _publico(u)


def sesion_por_hash(refresh_hash: str) -> dict | None:
    """La sesión y su usuario (sin hash de contraseña), o `None` si no existe.
    Quien llama decide qué hacer si está revocada o vencida: se devuelven tal
    cual para que el motivo del rechazo sea el correcto."""
    usuarios, _, sesiones = _indice()
    for s in sesiones.values():
        if s["refreshHash"] == refresh_hash:
            u = usuarios.get(s["guid"])
            return {**s, "usuario": _publico(u) if u else None}
    return None


def revocar_sesiones(guid: str, sesion_guid: str | None, motivo: str) -> int:
    """Una sesión (logout) o todas las del usuario (`sesion_guid=None`).
    Devuelve cuántas quedaron revocadas con esta llamada."""
    with registro.candado:
        _, _, sesiones = _indice()
        vivas = [
            s for s in sesiones.values()
            if s["guid"] == guid and not s["revocadaEn"] and (sesion_guid is None or s["sesionGuid"] == sesion_guid)
        ]
        if not vivas:
            return 0
        registro.agregar(
            ARCHIVO,
            {"evento": "sesion_revocada", "guid": guid, "sesionGuid": sesion_guid, "motivo": motivo, "en": _ahora().isoformat(timespec="seconds")},
        )
        return len(vivas)


# ── Estado (lo usará HU08/HU09; aquí para poder probar "cuenta desactivada") ─


def cambiar_estado(guid: str, activo: bool, motivo: str | None = None, por: str | None = None) -> dict:
    """Activa o desactiva una cuenta (HU08, HU09). Al desactivar se cierran
    TODAS sus sesiones, o sea que no puede RENOVAR, y los guards del back
    rechazan su JWT al instante por dos vías: `activo` (403) y, desde HU05, la
    sesión revocada (401 `sesion_cerrada`). Lo que decide solo el front
    (bandeja, llaves, webhooks) lo rechaza el guard del front, que pregunta por
    `GET /auth/sesion` (`desactivada`): ver README, "El JWT de acceso y la
    sesión cerrada". El `motivo` es
    obligatorio en la pantalla y queda en el registro, que es lo que lo vuelve
    auditable."""
    with registro.candado:
        usuarios, _, _ = _indice()
        if guid not in usuarios:
            raise NoEncontrado(guid)
        en = _ahora().isoformat(timespec="seconds")
        registro.agregar(
            ARCHIVO,
            {"evento": "estado", "guid": guid, "activo": activo, "motivo": motivo, "por": por, "en": en},
        )
        if not activo:
            registro.agregar(
                ARCHIVO,
                {"evento": "sesion_revocada", "guid": guid, "sesionGuid": None, "motivo": "DEACTIVATED", "en": en},
            )
    return por_guid(guid)


#: Quién ordenó un cierre forzado. Va en el evento de auditoría.
ACTOR_ADMIN_ORGANIZACION = "ADMIN_ORGANIZACION"
ACTOR_ADMIN_PLATAFORMA = "ADMIN_PLATAFORMA"


def forzar_cierre(objetivo_guid: str, motivo: str, por_guid: str, tenant_guid: str | None, actor: str) -> int:
    """El cierre forzado COMPLETO (HU05 CA-4 y CA-5): deja el evento
    `FORCED_LOGOUT` en la bitácora de auditoría y cierra todas las sesiones.
    Devuelve cuántas cerró.

    Es UNA sola función para que el administrador de una organización y el de
    plataforma dejen exactamente el mismo rastro; solo cambia `actor`. El
    evento se escribe ANTES de cerrar, a propósito: si no se puede escribir
    (503), no se cierra nada. Es el modo de falla correcto para una auditoría,
    donde es peor un cierre sin saber quién lo ordenó que un aviso de más."""
    auditoria.registrar(
        auditoria.FORCED_LOGOUT,
        usuario_guid=objetivo_guid,
        por=por_guid,
        motivo=motivo,
        tenantGuid=tenant_guid,
        actor=actor,
    )
    return cerrar_sesiones(objetivo_guid, motivo, por_guid)


def cerrar_sesiones(guid: str, motivo: str, por: str | None = None) -> int:
    """Cierra todas las sesiones de un usuario SIN desactivarlo (HU05): puede
    volver a entrar con su contraseña. Devuelve cuántas se cerraron.

    Revoca las sesiones: no podrá RENOVAR, y su JWT deja de valer en los guards
    del back al instante (401 `sesion_cerrada`, ver `por_guid_y_sesion`). Lo que
    decide solo el front (bandeja, llaves, webhooks) lo rechaza su guard en unos
    segundos: pregunta por `GET /auth/sesion` (ver README).

    El rastro (`cierre_forzado`) se escribe ANTES de revocar, a propósito: si no
    se puede escribir, no se cierra nada."""
    with registro.candado:
        usuarios, _, _ = _indice()
        if guid not in usuarios:
            raise NoEncontrado(guid)
        registro.agregar(
            ARCHIVO,
            {
                "evento": "cierre_forzado",
                "guid": guid,
                "motivo": motivo,
                "por": por,
                "en": _ahora().isoformat(timespec="seconds"),
            },
        )
    return revocar_sesiones(guid, None, "FORCED_LOGOUT")
