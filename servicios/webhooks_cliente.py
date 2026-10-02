"""Registro PROVISIONAL de los webhooks de cliente.

Lo definitivo son las tablas que se le van a pedir al DBA (el webhook de cada
endpoint de cliente, y sus entregas). Mientras tanto viven en un registro de
solo agregar en el NAS, `webhooks.jsonl` (ver `servicios/registro.py`), igual
que las API Keys: un evento `registrado` por webhook, uno `estado` cada vez que
se activa o desactiva, uno `validado` cuando su endpoint respondió al aviso de
prueba, uno `validacion_fallida` cuando no respondió en ninguno de sus intentos
(es lo que el front muestra como "Con fallos"), uno `editado` cuando se le
cambian la URL o los eventos —si cambió la URL vuelve a quedar SIN validar: el
endpoint es otro; si solo cambiaron los eventos, sigue como estaba—, y uno
`eliminado`. El día que existan las tablas cambia ESTE módulo y
nada más.

## Validado antes de usarse

Un webhook nace SIN validar: hasta que su endpoint responda 2xx a un aviso de
prueba firmado (`POST /webhooks/{id}/validar`, ver
`servicios/entrega_webhooks.py`), no recibirá avisos, y el front solo le ofrece
"Validar conexión". `validadoEn` dice cuándo se validó; `None` es pendiente.

## Todavía no se envía nada

Esto es el REGISTRO: alta, listado, activar/desactivar y baja. El envío
—dispararlo desde el pipeline, firmar, reintentar, registrar las entregas y la
guarda contra SSRF al entregar— sigue pendiente (README, sección Webhooks).

## El secret de firma: cifrado, NO hasheado

Cada webhook nace con un secret (`whsec_...`) que se muestra UNA vez. Con él el
cliente comprobará que un aviso viene de NexusDoc: el servidor firmará cada
envío con HMAC-SHA256 usando ese secret.

A diferencia de las API Keys, aquí NO alcanza con guardar el hash. Una API Key
la presenta el cliente y el servidor solo tiene que comprobarla; el secret de
un webhook lo usa el SERVIDOR para firmar, así que tiene que poder recuperarlo.
Se guarda cifrado con Fernet (AES-128-CBC con HMAC-SHA256) y una llave que vive
en el `.env` del servidor, `WEBHOOKS_CLAVE_CIFRADO`, nunca en el almacén: quien
lea el registro no puede firmar avisos falsos.

Sin esa llave el alta se rechaza (`SinCifrado`, que el router contesta con
503): un secret que no se sabe cifrar no se guarda, ni en claro ni a medias.
Y la llave NO se puede cambiar a la ligera: los secrets cifrados con la
anterior dejan de poder descifrarse, y esos webhooks habría que volver a
crearlos.

## Lo que el registro de solo agregar no puede hacer

Eliminar un webhook agrega un evento `eliminado`; la línea con su secret
cifrado se queda en el archivo, sin usarse. Con las tablas, borrar la fila lo
destruye de verdad.
"""

import base64
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

import config
from servicios import almacen, registro

ARCHIVO = "webhooks.jsonl"

# Los eventos a los que se puede suscribir un webhook, en el orden del diseño.
# Son los mismos `valor` de `EVENTOS_WEBHOOK` en el front
# (`$lib/state/webhooks.svelte.ts`); `verificar_webhooks.py` revisa que no se
# separen.
EVENTOS = ("documento.completado", "documento.fallido", "documento.rechazado", "expediente.completado")
ESTADOS = ("activo", "inactivo")

LARGO_MAXIMO_URL = 2048
PREFIJO_SECRET = "whsec_"
# 24 bytes = 192 bits, en base64: 32 caracteres después del prefijo. Base64 y no
# hexadecimal porque así lo esperan las bibliotecas de Standard Webhooks del
# lado del cliente (ver `servicios/entrega_webhooks.py`); los primeros secrets,
# en hexadecimal, también son base64 válido y siguen sirviendo.
_BYTES_SECRET = 24

_LOCALES = {"localhost", "127.0.0.1", "::1"}
_URL_INVALIDA = "No parece una URL válida. Ejemplo: https://api.empresa.com/webhooks/nexusdoc"


class Duplicado(Exception):
    """El tenant ya tiene un webhook vigente con esa URL. NO hereda de
    `ValueError` a propósito: el router lo contesta con 409, y heredando caería
    en el `except ValueError` del 400."""


class SinCifrado(RuntimeError):
    """Falta la llave para cifrar los secrets, no es válida, o no abre un
    secret que se cifró con otra."""


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _fernet() -> Fernet:
    clave = (config.WEBHOOKS_CLAVE_CIFRADO or "").strip()
    if not clave:
        raise SinCifrado("Falta WEBHOOKS_CLAVE_CIFRADO en el .env del servidor.")
    try:
        return Fernet(clave)
    except (ValueError, TypeError) as exc:
        raise SinCifrado(
            "WEBHOOKS_CLAVE_CIFRADO no es una llave válida: tienen que ser 32 bytes en base64 url-safe."
        ) from exc


def validar_url(texto: str) -> str:
    """La misma regla que el front (`validarUrlWebhook`), repetida aquí porque
    la del front se puede saltar: `https`, o `http` solo hacia localhost; sin
    usuario ni contraseña y sin fragmento.

    Devuelve la URL normalizada —esquema y host en minúsculas, sin el puerto de
    siempre, con `/` si no traía ruta—, que es contra la que se buscan
    repetidas: dos formas de escribir la misma dirección no son dos webhooks.

    Solo mira la FORMA. La defensa contra mandar avisos a direcciones internas
    de CSI (SSRF) tiene que hacerse al ENTREGAR, resolviendo el nombre, y llega
    con el envío. Levanta `ValueError` con el motivo.
    """
    limpio = (texto or "").strip()
    if not limpio:
        raise ValueError("Escribe la URL de destino.")
    if len(limpio) > LARGO_MAXIMO_URL:
        raise ValueError(f"La URL no puede pasar de {LARGO_MAXIMO_URL} caracteres.")
    try:
        partes = urlsplit(limpio)
        host = partes.hostname  # ya viene en minúsculas y sin corchetes
        puerto = partes.port  # levanta ValueError con un puerto imposible
    except ValueError as exc:
        raise ValueError(_URL_INVALIDA) from exc
    esquema = partes.scheme.lower()
    if not esquema or not host:
        raise ValueError(_URL_INVALIDA)
    if esquema != "https" and not (esquema == "http" and host in _LOCALES):
        raise ValueError("La URL tiene que empezar con https:// (http:// solo se acepta para localhost).")
    if partes.username is not None or partes.password is not None:
        raise ValueError("La URL no puede llevar usuario ni contraseña: los avisos se autentican con su firma.")
    if partes.fragment:
        raise ValueError("La URL no puede llevar fragmento (#...).")

    nombre = f"[{host}]" if ":" in host else host
    if puerto is not None and puerto != {"https": 443, "http": 80}[esquema]:
        nombre = f"{nombre}:{puerto}"
    consulta = f"?{partes.query}" if partes.query else ""
    return f"{esquema}://{nombre}{partes.path or '/'}{consulta}"


def _indice() -> tuple[dict[str, dict], dict[str, str], set[str], dict[str, str], dict[str, dict]]:
    """Los webhooks registrados por id, el estado vigente de cada uno, los
    eliminados, cuándo se validó cada uno, y su última validación fallida."""
    registrados: dict[str, dict] = {}
    estados: dict[str, str] = {}
    eliminados: set[str] = set()
    validados: dict[str, str] = {}
    fallidas: dict[str, dict] = {}
    for e in registro.eventos(ARCHIVO):
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
            estados[identificador] = e["estado"] if e.get("estado") in ESTADOS else "inactivo"
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


def _publico(webhook: dict, estado: str, validado_en: str | None, fallida_en: str | None = None) -> dict:
    """El webhook como lo ve el listado: SIN el secret ni su cifrado.
    `fallidaEn` es su última validación fallida; sin validar y con ella, el
    front lo muestra "Con fallos"."""
    return {
        "id": webhook["id"],
        "url": webhook.get("url", ""),
        "eventos": [e for e in EVENTOS if e in (webhook.get("eventos") or [])],
        "estado": estado,
        "creadoEn": webhook.get("creadoEn"),
        "validadoEn": validado_en,
        "fallidaEn": fallida_en,
    }


def _vigente(registrados: dict, eliminados: set, tenant: str, identificador: str) -> dict | None:
    webhook = registrados.get(identificador)
    if webhook is None or webhook.get("tenant") != tenant or identificador in eliminados:
        return None
    return webhook


def registrar(tenant: str, url: str, eventos: list[str]) -> tuple[str, dict]:
    """Registra un webhook ACTIVO. Devuelve `(secret, webhook)`.

    El secret se devuelve AQUÍ y nunca más: quien llama tiene que mostrarlo y
    olvidarlo. Levanta `ValueError` con datos inválidos, `Duplicado` si el
    tenant ya tiene esa URL, y `SinCifrado` si no hay con qué cifrar.
    """
    tenant = almacen._validar_tenant(tenant)
    url = validar_url(url)
    pedidos = set(eventos or [])
    desconocidos = pedidos - set(EVENTOS)
    if desconocidos:
        raise ValueError(f"Evento de suscripción desconocido: {', '.join(sorted(desconocidos))}.")
    elegidos = [e for e in EVENTOS if e in pedidos]
    if not elegidos:
        raise ValueError("Elige al menos un evento de suscripción.")
    # ANTES de generar nada: sin llave de cifrado no hay alta.
    cifrador = _fernet()

    # Buscar repetidos, sortear el id y escribir bajo el MISMO candado: si no,
    # dos altas simultáneas podrían colarse con la misma URL o el mismo id.
    with registro.candado:
        registrados, _, eliminados, _, _ = _indice()
        if any(
            w.get("tenant") == tenant and w.get("url") == url and i not in eliminados
            for i, w in registrados.items()
        ):
            raise Duplicado(
                "Ya tienes un webhook con esa URL. Si quieres otros eventos, elimínalo y vuelve a crearlo."
            )
        identificador = "wh_" + secrets.token_hex(6)
        while identificador in registrados:
            identificador = "wh_" + secrets.token_hex(6)
        secret = PREFIJO_SECRET + base64.b64encode(secrets.token_bytes(_BYTES_SECRET)).decode()
        evento = {
            "evento": "registrado",
            "id": identificador,
            "tenant": tenant,
            "url": url,
            "eventos": elegidos,
            "secret_cifrado": cifrador.encrypt(secret.encode()).decode(),
            "creadoEn": _ahora().isoformat(timespec="seconds"),
        }
        registro.agregar(ARCHIVO, evento)
    return secret, _publico(evento, "activo", None)


def listar(tenant: str) -> list[dict]:
    """Los webhooks vigentes del tenant, del más nuevo al más viejo, sin
    secrets."""
    tenant = almacen._validar_tenant(tenant)
    registrados, estados, eliminados, validados, fallidas = _indice()
    propios = [
        (posicion, w)
        for posicion, (i, w) in enumerate(registrados.items())
        if w.get("tenant") == tenant and i not in eliminados
    ]
    # Dos altas en el mismo segundo empatan en `creadoEn`: desempata el orden
    # del registro, para que el más nuevo siga saliendo primero.
    propios.sort(key=lambda par: (par[1].get("creadoEn") or "", par[0]), reverse=True)
    return [
        _publico(w, estados.get(w["id"], "activo"), validados.get(w["id"]), fallidas.get(w["id"], {}).get("en"))
        for _, w in propios
    ]


def cambiar_estado(tenant: str, identificador: str, estado: str) -> dict | None:
    """Activa o desactiva un webhook del tenant. Devuelve cómo quedó, o `None`
    si no existe, es de otro tenant o ya se eliminó —las tres se contestan
    igual a propósito, para no confirmarle a nadie que uno ajeno existe—.

    Pedir el estado que ya tiene no es error y no escribe nada."""
    tenant = almacen._validar_tenant(tenant)
    if estado not in ESTADOS:
        raise ValueError("El estado tiene que ser activo o inactivo.")
    with registro.candado:
        registrados, estados, eliminados, validados, fallidas = _indice()
        webhook = _vigente(registrados, eliminados, tenant, identificador)
        if webhook is None:
            return None
        if estados.get(identificador, "activo") != estado:
            registro.agregar(
                ARCHIVO,
                {"evento": "estado", "id": identificador, "estado": estado, "en": _ahora().isoformat(timespec="seconds")},
            )
    return _publico(webhook, estado, validados.get(identificador), fallidas.get(identificador, {}).get("en"))


def eliminar(tenant: str, identificador: str) -> bool:
    """Elimina un webhook del tenant. `True` si era suyo, se haya eliminado
    ahora o desde antes: es IDEMPOTENTE, igual que revocar una llave, para que
    un segundo clic desde otra pestaña no vea un error de algo que ya ocurrió.
    `False` si no existe o es de otro tenant."""
    tenant = almacen._validar_tenant(tenant)
    with registro.candado:
        registrados, _, eliminados, _, _ = _indice()
        webhook = registrados.get(identificador)
        if webhook is None or webhook.get("tenant") != tenant:
            return False
        if identificador not in eliminados:
            registro.agregar(
                ARCHIVO, {"evento": "eliminado", "id": identificador, "en": _ahora().isoformat(timespec="seconds")}
            )
    return True


def secret_para_firmar(identificador: str) -> str | None:
    """El secret en claro de un webhook vigente, para firmar un envío; `None`
    si no existe o se eliminó.

    Hoy no lo llama nadie: lo usará el envío. Está desde ya para que el cifrado
    se pruebe de ida y vuelta (`verificar_webhooks.py`): un secret que se
    guarda pero no se puede recuperar no firmaría nada. Levanta `SinCifrado` si
    la llave actual no lo abre, que es lo que pasa si alguien la cambió."""
    registrados, _, eliminados, _, _ = _indice()
    webhook = registrados.get(identificador)
    if webhook is None or identificador in eliminados:
        return None
    try:
        return _fernet().decrypt(str(webhook.get("secret_cifrado", "")).encode()).decode()
    except InvalidToken as exc:
        raise SinCifrado(
            "El secret de este webhook no se puede descifrar con la llave actual: ¿cambió WEBHOOKS_CLAVE_CIFRADO?"
        ) from exc


def para_validar(tenant: str, identificador: str) -> tuple[str, str] | None:
    """`(url, secret)` de un webhook vigente del tenant, para mandarle el aviso de
    prueba; `None` si no existe, es de otro tenant o se eliminó. Levanta
    `SinCifrado` si su secret no se puede descifrar."""
    tenant = almacen._validar_tenant(tenant)
    registrados, _, eliminados, _, _ = _indice()
    if _vigente(registrados, eliminados, tenant, identificador) is None:
        return None
    return registrados[identificador]["url"], secret_para_firmar(identificador)


def marcar_validado(tenant: str, identificador: str, codigo: int, ms: int) -> dict | None:
    """Registra que el endpoint respondió al aviso de prueba. Devuelve el webhook,
    o `None` si se eliminó mientras se validaba: el aviso ya salió, pero no se
    valida algo que ya no existe."""
    tenant = almacen._validar_tenant(tenant)
    en = _ahora().isoformat(timespec="seconds")
    with registro.candado:
        registrados, estados, eliminados, _, fallidas = _indice()
        webhook = _vigente(registrados, eliminados, tenant, identificador)
        if webhook is None:
            return None
        registro.agregar(ARCHIVO, {"evento": "validado", "id": identificador, "en": en, "codigo": codigo, "ms": ms})
    return _publico(webhook, estados.get(identificador, "activo"), en, fallidas.get(identificador, {}).get("en"))


def marcar_fallida(tenant: str, identificador: str, intentos: int, codigo: int | None, motivo: str) -> dict | None:
    """Registra que su endpoint no respondió al aviso de prueba en ninguno de
    sus intentos: el webhook queda "Con fallos" hasta que se valide. Devuelve el
    webhook, o `None` si se eliminó mientras se validaba."""
    tenant = almacen._validar_tenant(tenant)
    en = _ahora().isoformat(timespec="seconds")
    with registro.candado:
        registrados, estados, eliminados, validados, _ = _indice()
        webhook = _vigente(registrados, eliminados, tenant, identificador)
        if webhook is None:
            return None
        registro.agregar(
            ARCHIVO,
            {"evento": "validacion_fallida", "id": identificador, "en": en, "intentos": intentos, "codigo": codigo, "motivo": motivo},
        )
    return _publico(webhook, estados.get(identificador, "activo"), validados.get(identificador), en)


def suscritos(tenant: str, tipo: str) -> list[str]:
    """Los webhooks del tenant que deben recibir un aviso de `tipo`: vigentes,
    VALIDADOS, activos y suscritos a ese tipo."""
    tenant = almacen._validar_tenant(tenant)
    registrados, estados, eliminados, validados, _ = _indice()
    return [
        i
        for i, w in registrados.items()
        if w.get("tenant") == tenant
        and i not in eliminados
        and i in validados
        and estados.get(i, "activo") == "activo"
        and tipo in (w.get("eventos") or [])
    ]


def para_entregar(identificador: str) -> tuple[str, str] | None:
    """`(url, secret)` si el webhook sigue en condiciones de recibir —vigente,
    validado y activo—; si no, `None`, y sus entregas pendientes se cancelan.
    Levanta `SinCifrado` si su secret no se puede descifrar."""
    registrados, estados, eliminados, validados, _ = _indice()
    webhook = registrados.get(identificador)
    if (
        webhook is None
        or identificador in eliminados
        or identificador not in validados
        or estados.get(identificador, "activo") != "activo"
    ):
        return None
    return webhook["url"], secret_para_firmar(identificador)


def editar(tenant: str, identificador: str, url: str, eventos: list[str]) -> dict | None:
    """Le cambia la URL y los eventos a un webhook del tenant, y conserva su
    secret. Si la URL cambió queda SIN validar —el endpoint es otro—; si solo
    cambiaron los eventos, sigue como estaba. Devuelve cómo quedó, o `None` si
    no existe, es de otro tenant o se eliminó. Levanta `ValueError` con datos
    inválidos y `Duplicado` si otra de sus URLs ya es esa."""
    tenant = almacen._validar_tenant(tenant)
    url = validar_url(url)
    pedidos = set(eventos or [])
    desconocidos = pedidos - set(EVENTOS)
    if desconocidos:
        raise ValueError(f"Evento de suscripción desconocido: {', '.join(sorted(desconocidos))}.")
    elegidos = [e for e in EVENTOS if e in pedidos]
    if not elegidos:
        raise ValueError("Elige al menos un evento de suscripción.")
    with registro.candado:
        registrados, estados, eliminados, validados, fallidas = _indice()
        webhook = _vigente(registrados, eliminados, tenant, identificador)
        if webhook is None:
            return None
        cambio_url = url != webhook.get("url")
        if any(
            i != identificador and w.get("tenant") == tenant and w.get("url") == url and i not in eliminados
            for i, w in registrados.items()
        ):
            raise Duplicado("Ya tienes otro webhook con esa URL.")
        registro.agregar(
            ARCHIVO,
            {"evento": "editado", "id": identificador, "url": url, "eventos": elegidos, "urlCambio": cambio_url,
             "en": _ahora().isoformat(timespec="seconds")},
        )
    if cambio_url:
        return _publico({**webhook, "url": url, "eventos": elegidos}, estados.get(identificador, "activo"), None, None)
    return _publico(
        {**webhook, "eventos": elegidos},
        estados.get(identificador, "activo"),
        validados.get(identificador),
        fallidas.get(identificador, {}).get("en"),
    )
