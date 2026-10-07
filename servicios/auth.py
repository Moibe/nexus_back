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
  sin llamar aquí en cada petición.
- **Refresh token**: 32 bytes aleatorios en base64url. Se entrega una vez; lo
  que se guarda es su SHA-256 (`servicios/usuarios.crear_sesion`).
"""

import hashlib
import secrets
import string
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
    pantalla de cambio)."""
    ahora = datetime.now(timezone.utc)
    vence = ahora + timedelta(minutes=config.AUTH_ACCESO_MIN)
    claims = {
        "sub": usuario["guid"],
        "sid": sesion_guid,
        "eml": usuario["email"],
        "nom": f"{usuario['nombre']} {usuario['apellidos']}".strip(),
        "adm": bool(usuario["esAdminPlataforma"]),
        "dcc": bool(usuario["debeCambiarContrasena"]),
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
