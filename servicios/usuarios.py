"""Usuarios, credenciales, intentos de login y sesiones.

## Dónde viven

En SQL Server (migración `0014`, 2026-11-11): `nexus.users`, `userMemberships`, `userSessions`, `loginAttempts`, `loginOrigins` y
`userAdminEvents`, importados una vez desde el viejo `usuarios.jsonl` (`servicios/importacion_json.py`). Este módulo tiene las REGLAS
—validaciones, la política de intentos, cómo se separa un nombre, por qué no se puede retirar a alguien— y `servicios/usuarios_sql.py`
guarda y lee. Hasta el 2026-11-11, donde no había base, todo vivía en ese JSONL; ese camino ya no existe (su lectura, para la
importación, está en `servicios/json_heredado.py`).

## Lo atómico

Cuatro operaciones garantizan algo que no se lograría con pasos sueltos, y las hace UNA transacción con el candado de aplicación de la
persona (ver `usuarios_sql.py`):

    rotar_sesion                    comprueba que el refresh siga vivo, lo revoca y crea la
                                    sucesora, en un paso (un refresh es de UN solo uso)
    crear_sesion(hash_verificado)   solo abre la sesión si la contraseña que se verificó
                                    sigue siendo la vigente y la cuenta sigue activa
    fijar_contrasena(sesion_guid, hash_verificado)
                                    solo cambia la contraseña si la sesión que lo pide
                                    sigue viva y la que se verificó sigue siendo la vigente
    fijar_contrasena(CHANGE, sesion_guid)
                                    cierra las sesiones de los OTROS dispositivos y conserva la
                                    de este (HU13 CA-4)

## Qué se guarda, y qué NUNCA

El hash argon2id de la contraseña (autocontenido: algoritmo, sal y parámetros
van dentro del string), nunca la contraseña. El SHA-256 del refresh token,
nunca el token. Los JWT de acceso no se guardan: se verifican por firma.

## La política de intentos vive aquí

5 intentos fallidos seguidos bloquean la cuenta 15 minutos; el bloqueo se
levanta solo. Un acierto, o cambiar la contraseña, pone el contador en cero. Un
intento hecho DURANTE el bloqueo se anota (bitácora) pero no cuenta: si contara,
cada intento alargaría el castigo y nunca se saldría (`plegar_intento_cuenta`).
"""

import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

import config
from servicios import auditoria, roles, usuarios_sql

logger = logging.getLogger(__name__)

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
    guid = str(uuid.uuid4())
    # El JSONL guarda nombre y apellidos juntos y los separa al leerlos; la base los guarda ya separados, con la misma regla.
    nombres, paterno, materno = separar_nombre(nombre, apellidos)
    if not usuarios_sql.crear(guid, email, nombres, paterno, materno, None, {}, True, hash_contrasena, _ahora()):
        raise YaExiste("Ya existe un usuario con ese correo.")
    return por_guid(guid)


def administradores_de_plataforma() -> list[dict]:
    """Los administradores de plataforma que EXISTEN, en el orden de su alta (el de menos tiempo, al final).

    Es el control que `bootstrap_admin.py` no daba: saber quiénes son, si ya hicieron su primer acceso y si siguen activos.
    Los que se retiraron (`eliminar_usuario`) ya no están: el índice los quita de todos lados, y su rastro vive en la bitácora.
    Sin el hash de la contraseña (`_publico`)."""
    return [_publico(u) for u in usuarios_sql.administradores()]


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
    guid = str(uuid.uuid4())
    if not usuarios_sql.crear(guid, email, nombres, paterno, materno, telefono, recuperacion or {}, False, hash_contrasena, _ahora(), tenant_guid or None, rol):
        raise YaExiste("El correo electrónico ingresado ya se encuentra registrado, por favor intenta nuevamente")
    return por_guid(guid)


def agregar_membresia(guid: str, tenant_guid: str, rol: str = "ADMIN") -> dict:
    if not usuarios_sql.agregar_membresia(guid, tenant_guid, rol, _ahora()):
        raise NoEncontrado(guid)
    return por_guid(guid)


def actualizar_datos(guid: str, nombre: str, apellidos: str = "", telefono: str | None = None) -> dict:
    """Cambia nombre, apellidos y teléfono. El correo NO se toca: es la
    identidad con la que entra, y cambiarlo es otra historia."""
    nombre, apellidos = " ".join(nombre.split()), " ".join((apellidos or "").split())
    if not nombre:
        raise ValueError("El nombre es obligatorio.")

    def decidir(actual: dict) -> tuple[str, str, str]:
        if not apellidos and nombre == " ".join(f"{actual['nombre']} {actual['apellidos']}".split()):
            # El formulario manda de vuelta el nombre completo tal como lo mostró el listado: si no lo tocaron, no se vuelve a
            # separar, o se desharía lo que la persona corrigió en su perfil.
            return actual["nombre"], actual["apellidoPaterno"], actual["apellidoMaterno"]
        return separar_nombre(nombre, apellidos)

    if not usuarios_sql.actualizar_datos(guid, decidir, telefono):
        raise NoEncontrado(guid)
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
    if not usuarios_sql.actualizar_perfil(guid, nombre, paterno, materno, telefono, recuperacion or {}):
        raise NoEncontrado(guid)
    return por_guid(guid)


def de_tenant(tenant_guid: str) -> list[dict]:
    """Los usuarios de una organización, para su listado."""
    return [_publico(u) for u in usuarios_sql.de_tenant(tenant_guid)]


# ── uspGetUserForLogin ──────────────────────────────────────────────────────


def para_login(email: str) -> dict | None:
    """Lo que el login necesita, CON el hash (no sale de este proceso). `None`
    si el correo no existe."""
    u = usuarios_sql.por_correo(_normalizar_correo(email))
    if u is None:
        return None
    bloqueo = _bloqueo_vigente(u)
    return {
        **_publico(u),
        "hash": u["hash"],
        "fallidos": u["fallidos"],
        "bloqueadoHasta": bloqueo.isoformat() if bloqueo else None,
    }


def por_guid(guid: str) -> dict:
    u = usuarios_sql.por_guid(guid)
    if not u:
        raise NoEncontrado(guid)
    return _publico(u)


def tiene_sesion(guid: str, ahora: datetime | None = None) -> bool:
    """¿Tiene alguna sesión sin revocar y sin vencer? Es decir, ¿hay un navegador que todavía puede renovar su acceso?
    Una sesión sin fecha de vencimiento legible se toma por vencida, igual que al canjear el refresh."""
    ahora = ahora or _ahora()
    return usuarios_sql.tiene_sesion(guid, ahora)


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
    u, viva = usuarios_sql.por_guid_y_sesion(guid, sesion_guid)
    if u is None:
        raise NoEncontrado(guid)
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
    lee `estado_origen`; su contador se lleva aquí mismo (`plegar_intento_origen`)."""
    fallidos, hasta = usuarios_sql.registrar_intento(
        guid, _normalizar_correo(email), exitoso, motivo, ip, (user_agent or "")[:500] or None, origen, _ahora()
    )
    hasta = hasta.replace(tzinfo=timezone.utc) if hasta else None
    return fallidos, hasta.isoformat() if hasta and hasta > _ahora() else None


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


def plegar_intento_cuenta(fallidos: int, hasta: datetime | None, exitoso: bool, motivo: str | None, en: datetime) -> tuple[int, datetime | None]:
    """La política de la CUENTA aplicada a UN intento: `(fallidos, bloqueado hasta)` como quedan. Es la misma que aplica el repaso del JSONL
    (`json_heredado.usuarios_indice`, al importar); aquí se aplica al anotar el intento y el resultado se guarda en la cuenta. Un acierto lo pone todo en cero; la
    contraseña mala suma, y la quinta bloquea `AUTH_BLOQUEO_MIN` minutos contados desde ese intento (el contador vuelve a cero); lo demás
    (bloqueada, desactivada, caducada) es bitácora y no cuenta."""
    if exitoso:
        return 0, None
    if motivo == "BAD_PASSWORD":
        fallidos += 1
        if fallidos >= config.AUTH_MAX_INTENTOS:
            return 0, en + timedelta(minutes=config.AUTH_BLOQUEO_MIN)
    return fallidos, hasta


def plegar_intento_origen(fallidos: int, hasta: datetime | None, exitoso: bool, motivo: str | None, en: datetime) -> tuple[int, datetime | None]:
    """Lo mismo para el contador del NAVEGADOR (`estado_origen`): ahí cuentan la contraseña mala y el correo que no existe."""
    if exitoso:
        return 0, None
    if motivo in MOTIVOS_QUE_CUENTAN:
        fallidos += 1
        if fallidos >= config.AUTH_MAX_INTENTOS:
            return 0, en + timedelta(minutes=config.AUTH_BLOQUEO_MIN)
    return fallidos, hasta


def estado_origen(origen: str | None) -> tuple[int, str | None]:
    """(fallidos seguidos, bloqueadoHasta) de ese navegador. Sin `origen`
    —un cliente que no manda cookie— devuelve el estado vacío: el contador de
    la cuenta sigue aplicando igual."""
    if not origen:
        return 0, None
    fallidos, guardado = usuarios_sql.estado_origen(origen)
    vigente = guardado.replace(tzinfo=timezone.utc) if guardado else None
    return fallidos, vigente.isoformat() if vigente and vigente > _ahora() else None


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
    usuarios_sql.reiniciar_origen(origen, _ahora())


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
    cerradas = usuarios_sql.fijar_contrasena(guid, hash_contrasena, motivo in MOTIVOS_QUE_CIERRAN_SESIONES, sesion_guid, hash_verificado, _ahora())
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
    usuarios = usuarios_sql.para_retiro(guid)
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
    return _publico(usuarios_sql.eliminar(guid, motivo, por, forzar, _ahora()))


def emitir_temporal(guid: str, hash_contrasena: str) -> dict:
    """Le pone una contraseña temporal NUEVA a una cuenta que sigue en su primer acceso (la caducada, o la que
    se perdió): la anterior deja de servir, las 48 horas empiezan otra vez, se levantan los bloqueos y se
    cierran las sesiones que se hubieran abierto con la anterior. Levanta `NoEncontrado` si la cuenta no existe
    y `NoPendiente` si ya hizo su primer acceso (para eso está "Olvidé mi contraseña")."""
    usuarios_sql.emitir_temporal(guid, hash_contrasena, _ahora())
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
    usuarios_sql.crear_sesion(guid, sesion_guid, refresh_hash, expira, ip, (user_agent or "")[:500] or None, hash_verificado, _ahora())
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
    sesion_guid = str(uuid.uuid4())
    u = usuarios_sql.rotar_sesion(refresh_hash, nuevo_refresh_hash, sesion_guid, expira, ip, (user_agent or "")[:500] or None, _ahora())
    return sesion_guid, _publico(u)


def sesion_por_hash(refresh_hash: str) -> dict | None:
    """La sesión y su usuario (sin hash de contraseña), o `None` si no existe.
    Quien llama decide qué hacer si está revocada o vencida: se devuelven tal
    cual para que el motivo del rechazo sea el correcto."""
    encontrada = usuarios_sql.sesion_por_hash(refresh_hash)
    if encontrada is None:
        return None
    s, u = encontrada
    return {**s, "usuario": _publico(u) if u else None}


def revocar_sesiones(guid: str, sesion_guid: str | None, motivo: str) -> int:
    """Una sesión (logout) o todas las del usuario (`sesion_guid=None`).
    Devuelve cuántas quedaron revocadas con esta llamada."""
    return usuarios_sql.revocar_sesiones(guid, sesion_guid, motivo, _ahora())


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
    auditable (en SQL, en `nexus.userAdminEvents`)."""
    usuarios_sql.cambiar_estado(guid, activo, motivo, por, _ahora())
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
    se puede escribir, no se cierra nada. (En SQL van los dos en la misma
    transacción, en `nexus.userAdminEvents` y `userSessions`.)"""
    return usuarios_sql.cerrar_sesiones(guid, motivo, por, _ahora())
