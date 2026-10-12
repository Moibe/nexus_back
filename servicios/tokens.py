"""Enlaces de un solo uso: activación de cuenta y recuperación de contraseña.

Es la pieza que comparten varias historias. Cambia la duración y el texto del
correo, pero el mecanismo es el mismo:

    ACTIVACION    48 h  — HU06: el enlace que se le manda a un usuario nuevo
    RECUPERACION   1 h  — HU10: "olvidé mi contraseña"

## Por qué solo se guarda el hash

Igual que los refresh tokens (`servicios/auth.py`), el token viaja en la URL
del correo y aquí solo queda su SHA-256. Si alguien lee este registro no puede
fabricar enlaces: la mitad que sirve solo existe en el correo de la persona.
No hace falta argon2id como en las contraseñas, porque el token no lo eligió
un humano — son 32 bytes al azar, no se adivina por fuerza bruta.

## De un solo uso, de verdad

`consumir` marca el token como usado DENTRO del candado y devuelve a quién
pertenece. Dos peticiones simultáneas con el mismo enlace: una gana y la otra
recibe `YaUsado`. Sin eso, un enlace reenviado o un doble clic podrían
restablecer la contraseña dos veces.

## Emitir invalida los anteriores

Pedir un enlace nuevo apaga los pendientes del mismo tipo para esa persona
(HU06 CA-6 lo pide literal para el reenvío de invitación). Si no, el correo
viejo seguiría sirviendo y bastaría con tener acceso a un buzón antiguo.

## Registro PROVISIONAL

Vive en `tokens.jsonl` como los demás, por la misma razón: dev todavía no
tiene base. El contrato imita a lo que se le pedirá al DBA, para que el cambio
sea mecánico y de un solo módulo:

    crear      ↔ security.uspCreateAccessToken(@userGuid, @kind, @tokenHash, @expiresAt)
    consumir   ↔ security.uspConsumeAccessToken(@tokenHash)
    invalidar  ↔ security.uspInvalidateAccessTokens(@userGuid, @kind, @reason)
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import config
from servicios import registro

ARCHIVO = "tokens.jsonl"

#: Para qué sirve cada enlace. El tipo decide la duración y el correo.
ACTIVACION = "ACTIVACION"
RECUPERACION = "RECUPERACION"
TIPOS = frozenset({ACTIVACION, RECUPERACION})


class TokenInvalido(Exception):
    """No existe, o nunca existió: también cubre un token inventado."""


class YaUsado(Exception):
    """El enlace ya se usó. El diseño pide pedir uno nuevo."""


class Vencido(Exception):
    """Se pasó de su vigencia."""


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _fecha(valor) -> datetime | None:
    if not valor:
        return None
    try:
        return datetime.fromisoformat(valor)
    except (TypeError, ValueError):
        return None


def hash_de(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def horas_de(tipo: str) -> int:
    """La vigencia que el diseño le da a cada tipo, configurable por ambiente."""
    if tipo == ACTIVACION:
        return config.TOKEN_ACTIVACION_HORAS
    if tipo == RECUPERACION:
        return config.TOKEN_RECUPERACION_HORAS
    raise ValueError(f"Tipo de enlace desconocido: {tipo}")


def _indice() -> dict[str, dict]:
    """Los tokens por su hash, reconstruidos de los eventos."""
    tokens: dict[str, dict] = {}
    for e in registro.eventos(ARCHIVO):
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


def _vigente(t: dict) -> bool:
    if t["usadoEn"] or t["invalidadoEn"]:
        return False
    expira = _fecha(t["expira"])
    return bool(expira and expira > _ahora())


def crear(usuario_guid: str, tipo: str, motivo_anteriores: str = "REEMPLAZADO") -> tuple[str, datetime]:
    """Emite un enlace nuevo e invalida los pendientes del mismo tipo.

    Devuelve `(token en claro, cuándo vence)`. El token en claro **no se vuelve
    a poder leer**: solo viaja al correo de la persona."""
    if tipo not in TIPOS:
        raise ValueError(f"Tipo de enlace desconocido: {tipo}")
    token = secrets.token_urlsafe(32)
    clave = hash_de(token)
    expira = _ahora() + timedelta(hours=horas_de(tipo))
    en = _ahora().isoformat(timespec="seconds")
    with registro.candado:
        for t in _indice().values():
            if t["usuarioGuid"] == usuario_guid and t["tipo"] == tipo and _vigente(t):
                registro.agregar(
                    ARCHIVO,
                    {"evento": "invalidado", "hash": t["hash"], "motivo": motivo_anteriores, "en": en},
                )
        registro.agregar(
            ARCHIVO,
            {
                "evento": "creado",
                "guid": str(uuid.uuid4()),
                "hash": clave,
                "usuarioGuid": usuario_guid,
                "tipo": tipo,
                "expira": expira.isoformat(timespec="seconds"),
                "en": en,
            },
        )
    return token, expira


def revisar(token: str, tipo: str) -> dict:
    """Mira si el enlace sirve, SIN consumirlo. Es lo que usa la pantalla al
    abrirse, para decidir si pinta el formulario o el aviso de enlace vencido."""
    t = _indice().get(hash_de(token))
    if t is None or t["tipo"] != tipo:
        raise TokenInvalido()
    if t["usadoEn"] or t["invalidadoEn"]:
        raise YaUsado()
    expira = _fecha(t["expira"])
    if not expira or expira <= _ahora():
        raise Vencido()
    return t


def consumir(token: str, tipo: str) -> dict:
    """Gasta el enlace y devuelve a quién pertenece. Atómico: dos peticiones
    con el mismo token, una gana y la otra recibe `YaUsado`."""
    clave = hash_de(token)
    with registro.candado:
        t = _indice().get(clave)
        if t is None or t["tipo"] != tipo:
            raise TokenInvalido()
        if t["usadoEn"] or t["invalidadoEn"]:
            raise YaUsado()
        expira = _fecha(t["expira"])
        if not expira or expira <= _ahora():
            raise Vencido()
        registro.agregar(ARCHIVO, {"evento": "usado", "hash": clave, "en": _ahora().isoformat(timespec="seconds")})
        return t


def invalidar_de(usuario_guid: str, tipo: str | None = None, motivo: str = "REVOCADO") -> int:
    """Apaga los enlaces pendientes de alguien. Se usa al restablecer la
    contraseña (HU10) y al desactivar la cuenta: un enlace emitido antes no
    debería seguir sirviendo. Devuelve cuántos apagó."""
    en = _ahora().isoformat(timespec="seconds")
    apagados = 0
    with registro.candado:
        for t in _indice().values():
            if t["usuarioGuid"] != usuario_guid or not _vigente(t):
                continue
            if tipo is not None and t["tipo"] != tipo:
                continue
            registro.agregar(ARCHIVO, {"evento": "invalidado", "hash": t["hash"], "motivo": motivo, "en": en})
            apagados += 1
    return apagados


def pendientes_de(usuario_guid: str, tipo: str | None = None) -> list[dict]:
    """Los enlaces que esa persona todavía podría usar. Para diagnosticar y
    para el listado de usuarios, que marca a quien sigue sin activar."""
    return [
        t
        for t in _indice().values()
        if t["usuarioGuid"] == usuario_guid and _vigente(t) and (tipo is None or t["tipo"] == tipo)
    ]
