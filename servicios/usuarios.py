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

import re
import uuid
from datetime import datetime, timedelta, timezone

import config
from servicios import registro

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


def _indice() -> tuple[dict[str, dict], dict[str, str], dict[str, dict]]:
    """(usuarios por guid, guid por correo, sesiones por guid de sesión)."""
    usuarios: dict[str, dict] = {}
    por_correo: dict[str, str] = {}
    sesiones: dict[str, dict] = {}
    for e in registro.eventos(ARCHIVO):
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
            sesiones.setdefault(
                e["sesionGuid"],
                {
                    "sesionGuid": e["sesionGuid"],
                    "guid": guid,
                    "refreshHash": e.get("refreshHash"),
                    "expira": e.get("expira"),
                    "creadaEn": e.get("en"),
                    "ip": e.get("ip"),
                    "userAgent": e.get("userAgent"),
                    "revocadaEn": None,
                    "revocadaPor": None,
                },
            )
        elif tipo == "sesion_revocada":
            objetivo = e.get("sesionGuid")
            for s in sesiones.values():
                if s["revocadaEn"]:
                    continue
                if (objetivo and s["sesionGuid"] == objetivo) or (not objetivo and s["guid"] == guid):
                    s["revocadaEn"] = e.get("en")
                    s["revocadaPor"] = e.get("motivo")
    return usuarios, por_correo, sesiones


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
        "recuperacion": u.get("recuperacion") or {},
        "esAdminPlataforma": u["esAdminPlataforma"],
        "debeCambiarContrasena": u["debeCambiar"],
        "activo": u["activo"],
        "ultimoLoginEn": u["ultimoLoginEn"],
        "creadoEn": u["creadoEn"],
        "membresias": list(u["membresias"]),
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


# ── uspRecordLoginResult ────────────────────────────────────────────────────


def registrar_resultado_login(
    guid: str | None, email: str, exitoso: bool, motivo: str | None, ip: str | None, user_agent: str | None
) -> tuple[int, str | None]:
    """Anota el intento y aplica la política. Devuelve (fallidos, bloqueadoHasta)
    tal como quedan. `motivo`: BAD_PASSWORD | LOCKED | INACTIVE | UNKNOWN_EMAIL."""
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


def crear_sesion(guid: str, refresh_hash: str, expira: datetime, ip: str | None, user_agent: str | None) -> str:
    sesion_guid = str(uuid.uuid4())
    with registro.candado:
        usuarios, _, _ = _indice()
        if guid not in usuarios:
            raise NoEncontrado(guid)
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
    TODAS sus sesiones: si no, seguiría trabajando con el JWT que ya tiene
    hasta que venciera. El `motivo` es obligatorio en la pantalla y queda en el
    registro, que es lo que lo vuelve auditable."""
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


def cerrar_sesiones(guid: str, motivo: str, por: str | None = None) -> int:
    """Cierra todas las sesiones de un usuario SIN desactivarlo (HU05): puede
    volver a entrar con su contraseña. Devuelve cuántas se cerraron."""
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
