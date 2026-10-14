"""Lo criptográfico del acceso: hash de contraseñas, reglas, JWT y refresh tokens.

Nada de aquí toca el registro ni la base; eso es `servicios/usuarios.py`. Lo
que sí decide este módulo:

- **Contraseñas con argon2id** (`argon2-cffi`, parámetros por default de la
  librería, que siguen la recomendación RFC 9106). El hash es un string
  autocontenido (`$argon2id$v=19$m=…$…`), así que el día que cambien los
  parámetros los hashes viejos se siguen verificando.
- **Reglas del diseño** (Sprint 1, "Configura tu nueva contraseña"): mínimo 12
  caracteres, mayúscula, minúscula, número y carácter especial. El front las
  pinta como chips; aquí se vuelven a comprobar, porque el front no es de fiar.
- **JWT de acceso HS256**, de vida corta (`AUTH_ACCESO_MIN`), firmado con
  `AUTH_JWT_SECRET`. El front (su capa server) lo verifica con el mismo secreto
  y, además, pregunta aquí (`GET /auth/sesion`) si su sesión sigue viva.
- **Refresh token**: 32 bytes aleatorios en base64url. Se entrega una vez; lo
  que se guarda es su SHA-256 (`servicios/usuarios.crear_sesion`).
"""

import hashlib
import secrets
import string
import unicodedata
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

import config

_hasher = PasswordHasher()

REGLAS = ("12 caracteres", "Mayúscula", "Minúscula", "Número", "Carácter especial")
_ESPECIALES = set(string.punctuation)


class SinConfigurar(Exception):
    """Falta `AUTH_JWT_SECRET`: no se puede emitir ni verificar nada."""


def hash_de(contrasena: str) -> str:
    return _hasher.hash(contrasena)


def verificar_contrasena(hash_guardado: str | None, contrasena: str) -> bool:
    if not hash_guardado:
        return False
    try:
        return _hasher.verify(hash_guardado, contrasena)
    except (VerifyMismatchError, InvalidHashError):
        return False


def reglas_incumplidas(contrasena: str) -> list[str]:
    """Las reglas que NO cumple, con el texto exacto de los chips del diseño."""
    faltan = []
    if len(contrasena) < 12:
        faltan.append("12 caracteres")
    if not any(c.isupper() for c in contrasena):
        faltan.append("Mayúscula")
    if not any(c.islower() for c in contrasena):
        faltan.append("Minúscula")
    if not any(c.isdigit() for c in contrasena):
        faltan.append("Número")
    if not any(c in _ESPECIALES for c in contrasena):
        faltan.append("Carácter especial")
    return faltan


# ── Contraseñas obvias (HU04 CA-3) ──────────────────────────────────────────
#
# Además de las cinco reglas de los chips, el criterio pide que la contraseña "no contenga el nombre de usuario ni
# palabras comunes de diccionario". Las cinco reglas dejan pasar "Password123!" o "Contraseña2026!": cumplen todo y
# son de las primeras que se prueban.
#
# "Palabras comunes" aquí NO es un diccionario entero (rechazaría contraseñas largas y buenas por llevar «mesa»): es una
# lista corta de las contraseñas y raíces más usadas en español y en inglés, más el nombre del producto. Se busca DENTRO de
# la contraseña, sin acentos, sin mayúsculas y sin separadores, y también con los cambios de letras por símbolos de siempre
# (`P@ssw0rd`). Una palabra de menos de 5 caracteres no está en la lista a propósito: daría falsos positivos.
PALABRAS_COMUNES = frozenset(
    {
        # las contraseñas y raíces más usadas, en español y en inglés
        "password", "passwd", "contrasena", "contrasenia", "clave", "claves", "secreto", "secret", "temporal",
        "changeme", "cambiame", "default", "welcome", "bienvenido", "bienvenida", "letmein", "iloveyou", "teamo",
        # cuentas y nombres de usuario típicos
        "admin", "administrador", "administrator", "usuario", "invitado", "guest", "login",
        # secuencias de teclado y de números
        "qwerty", "qwertyuiop", "azerty", "asdfg", "asdfgh", "zxcvb", "zxcvbn", "abcde", "abc123", "12345", "123456",
        "654321", "111111", "000000",
        # palabras que la gente usa por cariño o por costumbre
        "monkey", "dragon", "master", "shadow", "sunshine", "princess", "princesa", "superman", "batman", "football",
        "futbol", "soccer", "baseball", "michael", "jordan", "charlie", "freedom", "whatever", "starwars", "computer",
        "computadora", "internet", "summer", "winter", "verano", "invierno", "flower", "mariposa", "estrella",
        "corazon", "familia", "amigos", "amistad", "dinero", "trustno", "america", "mexico", "colombia", "argentina",
        "chivas", "pumas", "barcelona", "realmadrid",
        # el producto y la empresa
        "nexusdoc", "grupocsi",
    }
)

# Letras que se escriben como símbolo: `P@ssw0rd`, `Adm1n`, `$ecreto`.
_COMO_LETRAS = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s", "!": "i"})
# Menos de esto, el nombre de usuario o de la persona no se busca en la contraseña: «ana» está dentro de «banana».
LARGO_MINIMO_NOMBRE = 4


def _sin_acentos(texto: str) -> str:
    return unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode("ascii").lower()


def _alfanumerico(texto: str) -> str:
    return "".join(c for c in _sin_acentos(texto) if c.isalnum())


def contrasena_obvia(contrasena: str, correo: str = "", nombres: tuple[str, ...] | list[str] = ()) -> str | None:
    """Por qué esta contraseña es demasiado fácil de adivinar, o `None` si no lo es (HU04 CA-3).

    - `"usuario"`: contiene el nombre de usuario (la parte del correo antes de la `@`) o el nombre o un apellido de la
      persona (`nombres`, cada palabra suelta). Se compara sin acentos, sin mayúsculas, sin separadores
      (`Juan-Perez` contiene `juan.perez`) y también con los símbolos por letras (`J0an`).
    - `"comun"`: contiene una de las `PALABRAS_COMUNES`.

    No mira las cinco reglas de los chips (`reglas_incumplidas`): se llama DESPUÉS de ellas."""
    plana = _alfanumerico(contrasena)
    como_letras = "".join(c for c in _sin_acentos(contrasena).translate(_COMO_LETRAS) if c.isalnum())
    propios = {_alfanumerico(correo.split("@", 1)[0])}
    for nombre in nombres:
        propios.update(_alfanumerico(parte) for parte in nombre.split())
    for propio in propios:
        if len(propio) >= LARGO_MINIMO_NOMBRE and (propio in plana or propio in como_letras):
            return "usuario"
    for palabra in PALABRAS_COMUNES:
        if palabra in plana or palabra in como_letras:
            return "comun"
    return None


def contrasena_temporal() -> str:
    """Una contraseña temporal que cumple las reglas, para el bootstrap. 16
    caracteres: cuatro fijos de cada clase y doce al azar del alfabeto completo,
    barajados con `secrets`."""
    alfabeto = string.ascii_letters + string.digits + "!@#$%&*+-_?"
    base = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice("!@#$%&*+-_?"),
    ] + [secrets.choice(alfabeto) for _ in range(12)]
    # Fisher-Yates con `secrets`: `random.shuffle` no es criptográfico.
    for i in range(len(base) - 1, 0, -1):
        j = secrets.randbelow(i + 1)
        base[i], base[j] = base[j], base[i]
    return "".join(base)


# ── JWT ────────────────────────────────────────────────────────────────────


def _secreto() -> str:
    if not config.AUTH_JWT_SECRET or len(config.AUTH_JWT_SECRET) < 32:
        raise SinConfigurar("AUTH_JWT_SECRET falta o es demasiado corto (mínimo 32 caracteres).")
    return config.AUTH_JWT_SECRET


def emitir_acceso(usuario: dict, sesion_guid: str) -> tuple[str, datetime]:
    """El JWT de acceso y cuándo vence. Claims cortos: `sub` (guid), `sid`
    (sesión, para poder invalidar), `eml`, `nom`, `adm` (admin de plataforma),
    `dcc` (debe cambiar contraseña: con esto el front solo deja llegar a la
    pantalla de cambio), y —si pertenece a una organización— `tnt` (su guid) y
    `rol`, que es lo que decide qué puede hacer dentro de ella (HU07)."""
    ahora = datetime.now(timezone.utc)
    vence = ahora + timedelta(minutes=config.AUTH_ACCESO_MIN)
    claims = {
        "sub": usuario["guid"],
        "sid": sesion_guid,
        "eml": usuario["email"],
        "nom": f"{usuario['nombre']} {usuario['apellidos']}".strip(),
        "adm": bool(usuario["esAdminPlataforma"]),
        "dcc": bool(usuario["debeCambiarContrasena"]),
        **(
            {"tnt": usuario["membresias"][0]["tenantGuid"], "rol": usuario["membresias"][0]["rol"]}
            if usuario.get("membresias")
            else {}
        ),
        "iat": int(ahora.timestamp()),
        "exp": int(vence.timestamp()),
        "jti": str(uuid.uuid4()),
        "iss": "nexusdoc",
    }
    return jwt.encode(claims, _secreto(), algorithm="HS256"), vence


def verificar_acceso(token: str) -> dict | None:
    """Los claims si la firma y la vigencia son válidas; `None` si no."""
    try:
        return jwt.decode(token, _secreto(), algorithms=["HS256"], issuer="nexusdoc", options={"require": ["exp", "sub", "sid"]})
    except jwt.PyJWTError:
        return None


# ── Refresh ────────────────────────────────────────────────────────────────


def nuevo_refresh() -> tuple[str, str, datetime]:
    """(token para el cliente, su SHA-256 para guardar, cuándo vence)."""
    token = secrets.token_urlsafe(32)
    return token, hash_refresh(token), datetime.now(timezone.utc) + timedelta(days=config.AUTH_REFRESH_DIAS)


def hash_refresh(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
