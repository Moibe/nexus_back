"""Usuarios, credenciales, intentos de login y sesiones — registro PROVISIONAL.

Lo definitivo son las tablas y los SPs pedidos al DBA en **NEX-319**
(`docs/solicitudes-dba.md`, sección 6). Mientras llegan, todo vive en un
registro de solo agregar en el NAS, `usuarios.jsonl` (ver `servicios/registro.py`),
como las llaves y los webhooks. El día que existan los SPs, cambia ESTE módulo y
nada más: cada función de aquí es el espejo de un SP, con el mismo contrato (salvo
las tres que se listan debajo de esta tabla).

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

Tres cosas de aquí NO son el espejo de un SP, porque garantizan algo que un SP suelto
no da. Hoy lo dan con el candado del registro (`registro.candado`, que vale para UN solo
proceso); con la base no bastará con llamar a los SPs de arriba uno tras otro: cada una
tiene que ir en UNA transacción o ser un SP propio (`docs/solicitudes-dba.md`, 6.2):

    rotar_sesion                    comprueba que el refresh siga vivo, lo revoca y crea la
                                    sucesora, en un paso (un refresh es de UN solo uso)
    crear_sesion(hash_verificado)   solo abre la sesión si la contraseña que se verificó
                                    sigue siendo la vigente y la cuenta sigue activa
    fijar_contrasena(sesion_guid, hash_verificado)
                                    solo cambia la contraseña si la sesión que lo pide
                                    sigue viva y la que se verificó sigue siendo la vigente
    fijar_contrasena(CHANGE, sesion_guid)
                                    cierra las sesiones de los OTROS dispositivos y conserva la
                                    de este (HU13 CA-4): en el SP, un `@exceptSessionGuid`

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
# Con estos motivos, cambiar la contraseña cierra TODAS las sesiones, la actual
# incluida: la contraseña anterior era temporal o estaba comprometida.
MOTIVOS_QUE_CIERRAN_SESIONES = {"FIRST_LOGIN", "RESET"}
# Con `CHANGE` (la persona la cambia desde una sesión, HU13) se cierran las de los
# OTROS dispositivos y se conserva la de quien la cambia: lo pide el criterio CA-4, y
# es lo que corta a quien tenga una sesión robada sin sacar de su pantalla a la dueña.

# La forma de un correo (la MISMA que `RE_CORREO` de `src/lib/acceso/correo.ts` en el front: si cambia una, cambia la otra):
# parte local de caracteres normales separados por UN punto (sin punto al principio, al final ni dos seguidos, de hasta 64),
# una arroba, y un dominio de etiquetas con letras, números y guiones (que no empiezan ni terminan en guion) separadas por
# puntos, que termina en una extensión de al menos DOS caracteres y que empieza con letra. Antes era `^[^@\s]+@[^@\s]+\.[^@\s]+$`,
# que solo pedía una arroba y un punto y dejaba pasar `a@b.c`, `a@b..com` o `a@-b.com` (corregido el 2026-10-15, a pedido de
# QA). NO acepta acentos ni letras fuera del ASCII.
_RE_CORREO = re.compile(
    r"^(?=[^@]{1,64}@)[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+(?:\.[A-Za-z0-9!#$%&'*+/=?^_`{|}~-]+)*"
    r"@(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z][A-Za-z0-9-]*[A-Za-z0-9]\Z"
)


class YaExiste(Exception):
    """El correo ya está registrado. (Varios administradores de plataforma sí se
    permiten; lo que no se repite es el correo.)"""


class NoEncontrado(Exception):
    pass


class NoSePuedeEliminar(Exception):
    """El borrado lógico se negó. `motivo`: `ultimo_superadmin` (dejaría a la plataforma sin quien la administre) o
    `admin_de_organizacion` (la organización se quedaría sin administrador; se permite con `forzar=True`)."""

    def __init__(self, motivo: str, detalle: str = ""):
        super().__init__(detalle or motivo)
        self.motivo = motivo


class NoPendiente(Exception):
    """La cuenta ya hizo su primer acceso: no tiene contraseña temporal que reemitir."""


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


# Palabras que forman parte de un apellido compuesto y no son un apellido por sí solas («De la Cruz», «Del Valle», «San Martín»).
_PARTICULAS = frozenset({"de", "del", "la", "las", "los", "da", "das", "do", "dos", "di", "du", "van", "von", "san", "santa", "mc", "mac", "le"})
# «Ortega y Gasset»: la «y» (o «e») une las dos palabras de al lado en una sola unidad.
_CONECTORES = frozenset({"y", "e"})


def _unidades(palabras: list[str]) -> list[str]:
    """Agrupa palabras en unidades de nombre o de apellido: una partícula se pega a la palabra que le sigue, y «y»/«e» une la de
    antes con la de después. «De la Cruz» es UNA unidad; «María de los Ángeles», dos."""
    unidades: list[list[str]] = []
    particulas: list[str] = []
    unir = False  # la última palabra fue una «y»: la que viene se une a la unidad anterior
    for palabra in palabras:
        clave = palabra.lower()
        if clave in _CONECTORES and unidades and not particulas and not unir:
            unidades[-1].append(palabra)
            unir = True
        elif clave in _PARTICULAS:
            particulas.append(palabra)
        elif unir:
            unidades[-1].extend(particulas + [palabra])
            particulas, unir = [], False
        else:
            unidades.append(particulas + [palabra])
            particulas = []
    if particulas:
        unidades.append(particulas)
    return [" ".join(u) for u in unidades]


def separar_nombre(nombre: str, apellidos: str = "") -> tuple[str, str, str]:
    """(nombres, apellido paterno, apellido materno) a partir de lo que escribió el formulario.

    Las altas y las ediciones de usuarios y de administradores de organización traen UN campo «nombre completo» y mandan
    `apellidos` vacío (el diseño no trae campos separados), pero «Mi perfil» y `security.users` sí los separan. Con `apellidos`
    el primero es el paterno y TODO lo que sigue, el materno. Sin él se parte el nombre completo como se escribe en México: las
    dos últimas unidades son los apellidos, con sus partículas («De la Cruz»), y todo lo anterior son los nombres. Con dos
    palabras, la segunda es el paterno; con una, solo hay nombre; siempre queda al menos un nombre.

    Es una heurística y no puede adivinar «María José Pérez» (¿dos nombres y un apellido?): la persona lo corrige en su perfil, y
    una edición que manda de vuelta el mismo nombre completo no lo deshace (`actualizar_datos`)."""
    nombre = " ".join(nombre.split())
    apellidos = " ".join((apellidos or "").split())
    if apellidos:
        unidades = _unidades(apellidos.split())
        return nombre, unidades[0], " ".join(unidades[1:])
    unidades = _unidades(nombre.split())
    if len(unidades) <= 1:
        return nombre, "", ""
    if len(unidades) == 2:
        return unidades[0], unidades[1], ""
    return " ".join(unidades[:-2]), unidades[-2], unidades[-1]


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
            # El diseño del perfil (HU12) pide los apellidos por separado, igual que `security.users` (lastName, secondLastName).
            # Los eventos nuevos ya los traen; los de antes se separan al leerlos (`separar_nombre`): unos traían `apellidos`
            # y otros —los del formulario, hasta el 2026-10-14— el nombre completo entero en `nombre` y `apellidos` vacío.
            if "apellidoPaterno" in e:
                nombres, paterno, materno = e.get("nombre", ""), e.get("apellidoPaterno") or "", e.get("apellidoMaterno") or ""
                apellidos = e.get("apellidos", "")
            else:
                nombres, paterno, materno = separar_nombre(e.get("nombre", ""), e.get("apellidos", ""))
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
                    hasta = _fecha(e.get("en")) or _ahora()
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
                nombres, paterno, materno = separar_nombre(e.get("nombre", u["nombre"]), e.get("apellidos", u["apellidos"]))
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


def _heredar_recuperacion(usuarios: dict[str, dict]) -> None:
    """Un administrador de organización anterior al 2026-10-14 capturó sus datos de recuperación en el alta de la organización y
    se guardaron SOLO en la organización: su perfil los mostraba vacíos. Mientras no haya guardado los suyos (`recuperacionPropia`),
    se leen de los de su organización. Desde ese día el alta los guarda también en el usuario."""
    pendientes = [u for u in usuarios.values() if not u["recuperacion"] and not u.get("recuperacionPropia")
                  and any(m["rol"] == roles.ADMIN for m in u["membresias"])]
    if not pendientes:
        return
    from servicios import tenants_registro

    de_su_organizacion = {t["adminGuid"]: t["recuperacion"] for t in tenants_registro.listar() if t.get("adminGuid") and t.get("recuperacion")}
    for u in pendientes:
        u["recuperacion"] = dict(de_su_organizacion.get(u["guid"], {}))


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
        _heredar_recuperacion(indice[0])
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
        "temporalEmitidaEn": u.get("temporalEmitidaEn"),
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


def administradores_de_plataforma() -> list[dict]:
    """Los administradores de plataforma que EXISTEN, en el orden de su alta (el de menos tiempo, al final).

    Es el control que `bootstrap_admin.py` no daba: saber quiénes son, si ya hicieron su primer acceso y si siguen activos.
    Los que se retiraron (`eliminar_usuario`) ya no están: el índice los quita de todos lados, y su rastro vive en la bitácora.
    Sin el hash de la contraseña (`_publico`)."""
    indice, _, _ = _indice()
    admins = [_publico(u) for u in indice.values() if u["esAdminPlataforma"]]
    admins.sort(key=lambda u: u["creadoEn"] or "")
    return admins


def crear_usuario(
    email: str,
    nombre: str,
    apellidos: str,
    hash_contrasena: str,
    telefono: str | None = None,
    tenant_guid: str | None = None,
    rol: str = "ADMIN",
    *,
    apellido_paterno: str = "",
    apellido_materno: str = "",
    recuperacion: dict | None = None,
) -> dict:
    """Da de alta un usuario con contraseña temporal (`debeCambiar`) y, si se
    indica un tenant, su membresía con rol. Levanta `YaExiste` si el correo ya
    está registrado — el diseño lo dice tal cual bajo el campo.

    Los formularios piden nombre(s), apellido paterno y materno POR SEPARADO (desde el 2026-10-14) y los mandan en
    `apellido_paterno`/`apellido_materno`: se guardan tal cual. Quien llame con el nombre completo junto (`nombre`, y
    `apellidos` vacío o el par de apellidos en una cadena) sigue funcionando: se separa con `separar_nombre`."""
    email = _normalizar_correo(email)
    if not correo_valido(email):
        raise ValueError("El correo no es válido.")
    nombre, apellidos = nombre.strip(), apellidos.strip()
    if not nombre:
        raise ValueError("El nombre es obligatorio.")
    # El formulario manda el nombre completo y `apellidos` vacío: se guarda separado (ver `separar_nombre`).
    paterno_dado, materno_dado = " ".join(apellido_paterno.split()), " ".join(apellido_materno.split())
    if paterno_dado or materno_dado:
        nombres, paterno, materno = " ".join(nombre.split()), paterno_dado, materno_dado
    else:
        nombres, paterno, materno = separar_nombre(nombre, apellidos)
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
                "nombre": nombres,
                "apellidos": " ".join(x for x in (paterno, materno) if x),
                "apellidoPaterno": paterno,
                "apellidoMaterno": materno,
                "telefono": telefono,
                **({"recuperacion": recuperacion} if recuperacion is not None else {}),
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
    nombre, apellidos = " ".join(nombre.split()), " ".join((apellidos or "").split())
    if not nombre:
        raise ValueError("El nombre es obligatorio.")
    with registro.candado:
        usuarios, _, _ = _indice()
        if guid not in usuarios:
            raise NoEncontrado(guid)
        actual = usuarios[guid]
        if not apellidos and nombre == " ".join(f"{actual['nombre']} {actual['apellidos']}".split()):
            # El formulario manda de vuelta el nombre completo tal como lo mostró el listado: si no lo tocaron, no se vuelve a
            # separar, o se desharía lo que la persona corrigió en su perfil.
            nombres, paterno, materno = actual["nombre"], actual["apellidoPaterno"], actual["apellidoMaterno"]
        else:
            nombres, paterno, materno = separar_nombre(nombre, apellidos)
        registro.agregar(
            ARCHIVO,
            {
                "evento": "datos",
                "guid": guid,
                "nombre": nombres,
                "apellidos": " ".join(x for x in (paterno, materno) if x),
                "apellidoPaterno": paterno,
                "apellidoMaterno": materno,
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
    return {
        **_publico(u),
        "hash": u["hash"],
        "fallidos": u["fallidos"],
        "bloqueadoHasta": bloqueo.isoformat() if bloqueo else None,
    }


def por_guid(guid: str) -> dict:
    usuarios, _, _ = _indice()
    u = usuarios.get(guid)
    if not u:
        raise NoEncontrado(guid)
    return _publico(u)


def tiene_sesion(guid: str, ahora: datetime | None = None) -> bool:
    """¿Tiene alguna sesión sin revocar y sin vencer? Es decir, ¿hay un navegador que todavía puede renovar su acceso?
    Una sesión sin fecha de vencimiento legible se toma por vencida, igual que al canjear el refresh."""
    _, _, sesiones = _indice()
    ahora = ahora or _ahora()
    return any(
        x["guid"] == guid and not x["revocadaEn"] and (_fecha(x["expira"]) or ahora) > ahora for x in sesiones.values()
    )


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
        # Pendiente = sigue en su primer acceso; Caducada = además ya pasaron las horas de `TOKEN_ACTIVACION_HORAS`
        # (HU06 CA-5). Es lo que decide si la pantalla pinta uno u otro estatus; "Reenviar invitación" se ofrece en
        # los dos (lo decide `invitaciones.por_que_no_reenviar`).
        "accesoVencido": acceso_vencido(u),
        # Lo que el administrador necesita para saber si "Cerrar sesión" tiene algo que cerrar, y para ver que sí
        # funcionó: el estatus (Activo/Inactivo) no cambia al cerrar una sesión, así que sin esto la pantalla no
        # mostraba ninguna diferencia.
        "sesionActiva": tiene_sesion(u["guid"]),
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
    | INACTIVE | UNKNOWN_EMAIL | EXPIRED (la contraseña temporal era la correcta pero ya caducó). `origen` identifica al navegador y es lo que
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


def fijar_contrasena(
    guid: str,
    hash_contrasena: str,
    motivo: str,
    *,
    sesion_guid: str | None = None,
    hash_verificado: str | None = None,
) -> dict:
    """Lo mismo que `fijar_contrasena_y_cerrar`, sin decir cuántas sesiones cerró."""
    return fijar_contrasena_y_cerrar(guid, hash_contrasena, motivo, sesion_guid=sesion_guid, hash_verificado=hash_verificado)[0]


def fijar_contrasena_y_cerrar(
    guid: str,
    hash_contrasena: str,
    motivo: str,
    *,
    sesion_guid: str | None = None,
    hash_verificado: str | None = None,
) -> tuple[dict, int]:
    """`(usuario, cuántas sesiones se cerraron)`. Pone una contraseña nueva. Con `sesion_guid` y `hash_verificado` (lo usa el cambio
    desde una sesión, no la recuperación por enlace) comprueba, BAJO el candado, que esa
    sesión siga viva y que la contraseña contra la que se verificó siga siendo la vigente;
    si no, levanta `SesionNoCanjeable` (`cerrada` o `contrasena`) y no escribe nada.

    El cambio verifica con argon2 la contraseña actual y la nueva (decenas de ms cada una)
    FUERA del candado. Sin esta revisión, si en esa ventana el dueño restablecía la
    contraseña —lo que cierra TODAS las sesiones—, el cambio de la sesión ya cerrada la
    pisaba: quien tuviera una sesión robada recuperaba la cuenta él solo. Es la misma
    carrera que `crear_sesion(hash_verificado=...)` cierra en el login.

    Qué sesiones se cierran (HU13 CA-4): con `FIRST_LOGIN` y `RESET`, TODAS. Con `CHANGE`, las de los OTROS
    dispositivos: se conserva `sesion_guid`, la desde la que se cambió (sin ella no hay nada que conservar y se
    cierran todas). Se revocan una por una, con su propio evento, para que el índice no necesite una regla nueva."""
    if motivo not in MOTIVOS_CONTRASENA:
        raise ValueError(f"Motivo inválido: {motivo}")
    with registro.candado:
        usuarios, _, sesiones = _indice()
        if guid not in usuarios:
            raise NoEncontrado(guid)
        if sesion_guid is not None:
            s = sesiones.get(sesion_guid)
            if s is None or s["guid"] != guid or s["revocadaEn"]:
                raise SesionNoCanjeable("cerrada")
        if hash_verificado is not None and usuarios[guid]["hash"] != hash_verificado:
            raise SesionNoCanjeable("contrasena")
        en = _ahora().isoformat(timespec="seconds")
        vivas = [x["sesionGuid"] for x in sesiones.values() if x["guid"] == guid and not x["revocadaEn"]]
        evento_contrasena = {"evento": "contrasena", "guid": guid, "hash": hash_contrasena, "motivo": motivo, "en": en}
        if motivo in MOTIVOS_QUE_CIERRAN_SESIONES:
            registro.agregar(ARCHIVO, evento_contrasena)
            registro.agregar(ARCHIVO, {"evento": "sesion_revocada", "guid": guid, "sesionGuid": None, "motivo": "PASSWORD_CHANGED", "en": en})
            cerradas = len(vivas)
        else:
            # Primero las sesiones de los otros dispositivos y DESPUÉS la contraseña: si el almacén falla a la mitad,
            # que no quede la contraseña nueva con las sesiones viejas todavía abiertas.
            otras = [x for x in vivas if x != sesion_guid]
            for otra in otras:
                registro.agregar(ARCHIVO, {"evento": "sesion_revocada", "guid": guid, "sesionGuid": otra, "motivo": "PASSWORD_CHANGED", "en": en})
            registro.agregar(ARCHIVO, evento_contrasena)
            cerradas = len(otras)
    return por_guid(guid), cerradas


# ── La contraseña temporal caduca (HU06 CA-5) ──────────────────────────────


def acceso_vencido(u: dict, ahora: datetime | None = None) -> bool:
    """¿Ya caducó la contraseña temporal de esta cuenta? Recibe lo que devuelve `para_login`.

    El correo de bienvenida trae una contraseña temporal y el diseño dice que los enlaces de primer acceso
    "son válidos únicamente durante 48 horas a partir de su envío" (`TOKEN_ACTIVACION_HORAS`). Solo cuenta para
    una cuenta que SIGUE en su primer acceso; quien ya puso su contraseña no tiene nada que caducar.

    Los administradores de PLATAFORMA quedan fuera: su alta es por línea de comandos, el criterio es de HU06 (un
    administrador da de alta a alguien) y, si caducara, no habría quien les generara un acceso nuevo desde
    pantalla. `TOKEN_ACTIVACION_HORAS=0` apaga la caducidad. Sin fecha legible no se inventa un vencimiento."""
    if config.TOKEN_ACTIVACION_HORAS <= 0:
        return False
    if not u.get("debeCambiarContrasena") or u.get("esAdminPlataforma"):
        return False
    emitida = _fecha(u.get("temporalEmitidaEn"))
    if emitida is None:
        return False
    return (ahora or _ahora()) - emitida > timedelta(hours=config.TOKEN_ACTIVACION_HORAS)


def _negativa_de_eliminar(usuarios: dict, guid: str, forzar: bool) -> NoSePuedeEliminar | None:
    """Por qué NO se puede retirar a esta cuenta, o `None` si se puede."""
    u = usuarios[guid]
    if u["esAdminPlataforma"] and not any(o["esAdminPlataforma"] and o["activo"] for g, o in usuarios.items() if g != guid):
        return NoSePuedeEliminar(
            "ultimo_superadmin", "es el último administrador de plataforma activo: la plataforma se quedaría sin quien la administre"
        )
    if not forzar and any(m["rol"] == roles.ADMIN for m in u["membresias"]):
        return NoSePuedeEliminar("admin_de_organizacion", "es el administrador de una organización, que se quedaría sin administrador")
    return None


def puede_eliminar(guid: str, forzar: bool = False) -> None:
    """Levanta `NoEncontrado` o `NoSePuedeEliminar` si `eliminar_usuario` se negaría; si no, no hace nada. Es la
    simulación de `retirar_cuenta.py`."""
    usuarios, _, _ = _indice()
    if guid not in usuarios:
        raise NoEncontrado(guid)
    negativa = _negativa_de_eliminar(usuarios, guid, forzar)
    if negativa is not None:
        raise negativa


def eliminar_usuario(guid: str, motivo: str, por: str | None = None, forzar: bool = False) -> dict:
    """BORRADO LÓGICO (`retirar_cuenta.py`): la cuenta deja de existir para el sistema y su correo queda libre.

    Se cierran todas sus sesiones y después se anota el evento `eliminado`; el índice la quita de todos lados
    (login, listados, JWT: un token suyo recibe "el usuario ya no existe"). Nada se borra del registro: es de solo
    agregar, y el rastro de lo que esa cuenta hizo se queda. Devuelve cómo era (sin el hash de su contraseña).

    Se niega (`NoSePuedeEliminar`) a retirar al ÚLTIMO administrador de plataforma, y al administrador de una
    organización salvo con `forzar` (la organización queda sin administrador). `motivo` es obligatorio."""
    motivo = (motivo or "").strip()
    if not motivo:
        raise ValueError("El motivo es obligatorio.")
    with registro.candado:
        usuarios, _, _ = _indice()
        if guid not in usuarios:
            raise NoEncontrado(guid)
        negativa = _negativa_de_eliminar(usuarios, guid, forzar)
        if negativa is not None:
            raise negativa
        resumen = _publico(usuarios[guid])
        en = _ahora().isoformat(timespec="seconds")
        registro.agregar(ARCHIVO, {"evento": "sesion_revocada", "guid": guid, "sesionGuid": None, "motivo": "DELETED", "en": en})
        registro.agregar(ARCHIVO, {"evento": "eliminado", "guid": guid, "motivo": motivo, "por": por, "en": en})
    return resumen


def emitir_temporal(guid: str, hash_contrasena: str) -> dict:
    """Le pone una contraseña temporal NUEVA a una cuenta que sigue en su primer acceso (la caducada, o la que
    se perdió): la anterior deja de servir, las 48 horas empiezan otra vez, se levantan los bloqueos y se
    cierran las sesiones que se hubieran abierto con la anterior. Levanta `NoEncontrado` si la cuenta no existe
    y `NoPendiente` si ya hizo su primer acceso (para eso está "Olvidé mi contraseña")."""
    with registro.candado:
        usuarios, _, _ = _indice()
        if guid not in usuarios:
            raise NoEncontrado(guid)
        if not usuarios[guid]["debeCambiar"]:
            raise NoPendiente(guid)
        en = _ahora().isoformat(timespec="seconds")
        registro.agregar(ARCHIVO, {"evento": "invitacion", "guid": guid, "hash": hash_contrasena, "en": en})
        registro.agregar(ARCHIVO, {"evento": "sesion_revocada", "guid": guid, "sesionGuid": None, "motivo": "REINVITED", "en": en})
    return por_guid(guid)


# ── Sesiones (uspCreateSession / uspGetSession / uspRevokeSessions) ────────


class SesionNoCanjeable(Exception):
    """No se puede abrir (ni usar) la sesión que se pidió. `motivo`:

      · `invalida`    el refresh no existe, ya se canjeó o se cerró, la cuenta está
                      desactivada, o la contraseña con la que se entró dejó de ser la vigente;
      · `vencida`     el refresh venció;
      · `desactivada` solo al crear la sesión de un login: la cuenta se desactivó mientras
                      se verificaba la contraseña;
      · `cerrada`     solo al cambiar la contraseña: la sesión que lo pide se cerró mientras
                      se verificaban las contraseñas;
      · `contrasena`  solo al cambiar la contraseña: la que se verificó dejó de ser la
                      vigente (otro dispositivo la cambió)."""

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
    candado. Sin esta revisión, un restablecimiento de contraseña o una desactivación que
    ocurrieran en esa ventana dejaban pasar un login con la credencial vieja y su sesión
    nacía DESPUÉS de las revocaciones: viva y renovable por 7 días. (Un cierre forzado NO
    entra aquí: no cambia la contraseña ni desactiva la cuenta, y la persona puede volver
    a entrar —README, HU05—.)"""
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
        # Primero se cierran las sesiones y DESPUÉS se marca la cuenta como desactivada (QA, HU08: «se tiene que cerrar
        # primero la sesión y después desactivarlo»). Los dos renglones se escriben bajo el mismo candado, así que nadie
        # ve la cuenta desactivada con una sesión viva, pero el registro ya cuenta la historia en ese orden.
        if not activo:
            registro.agregar(
                ARCHIVO,
                {"evento": "sesion_revocada", "guid": guid, "sesionGuid": None, "motivo": "DEACTIVATED", "en": en},
            )
        registro.agregar(
            ARCHIVO,
            {"evento": "estado", "guid": guid, "activo": activo, "motivo": motivo, "por": por, "en": en},
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
