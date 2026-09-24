"""Las API keys de CLIENTE: formato y verificación.

NO confundir con `seguridad.py`, que es otra cosa: aquella es UNA llave
compartida entre el front y esta API, de servicio a servicio, que vive en el
.env y nunca cambia. Estas son muchas, las emite un usuario desde el módulo
"API Key" del front, y cada una puede revocarse o vencer por su cuenta.

ESTE ARCHIVO ES EL ESPEJO de `src/lib/apiKeys/formato.ts` en el repo del front,
que es donde vive la especificación escrita. Los dos tienen que producir el
MISMO checksum para la misma cadena: `verificar_api_keys.py` lo comprueba con
llaves generadas por el JavaScript real, no con vectores inventados aquí.

LA FORMA

    nxdoc_live_a7K9_sk_WBaGRiphmOKdoyqmeEtQOzC6EgcI0xvw
    └─┬─┘ └─┬┘ └┬─┘ └┬┘ └───────────┬────────┘└──┬───┘
   producto amb.  id  tipo      aleatorio     checksum

  · `nxdoc` — prefijo del producto, fijo. Es lo que deja que un escáner de
    secretos reconozca una llave pegada por error en un repo o un ticket.
  · `live` — el ambiente. Hoy solo se emite `live`.
  · `a7K9` — el identificador, y NO es secreto. Es la pieza que deja buscar la
    fila por índice en vez de recorrer todas las llaves comparando hashes.
  · `sk` — secret key. Deja lugar a un `pk` si algún día hay algo público.
  · 26 caracteres base62 — ~155 bits, arriba del piso de 128.
  · 6 caracteres base62 — el checksum, ver `checksum_de`.

QUÉ FALTA PARA QUE ESTO SIRVA

Nada llama a `verificar()` todavía, y es a propósito: no existe dónde guardar
las llaves. La tabla y sus SPs le tocan al DBA — está pedido en
`docs/solicitudes-dba.md`. Mientras tanto este módulo es lógica pura y
probada: no toca la base, no importa `repositorios/`, y no inventa nombres de
stored procedure (que es justo el error que ya se cometió una vez con los
placeholders de `repositorios/documentos.py`).

Y lo que hay que tener claro mientras tanto: que el front muestre "Revocada"
no impide NADA. La revocación es un hecho registrado; la defensa es esta
verificación, del lado del servidor, el día que algo empiece a aceptar llaves.
"""

import hashlib
import hmac
import re
from datetime import datetime, timezone
from typing import Any, Callable

PREFIJO = "nxdoc"
AMBIENTE = "live"
_TIPO = "sk"
_LARGO_ID = 4
_LARGO_ALEATORIO = 26
_LARGO_CHECKSUM = 6

# base62. Sin caracteres fuera de [A-Za-z0-9] para que la llave viaje en URLs,
# headers y variables de entorno sin escaparse. El ORDEN importa: es el mismo
# del front, y cambiarlo aquí rompería todos los checksums ya emitidos.
_ALFABETO = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"

_PATRON = re.compile(
    rf"^{PREFIJO}_([a-z]+)_([A-Za-z0-9]{{{_LARGO_ID}}})_{_TIPO}_"
    rf"([A-Za-z0-9]{{{_LARGO_ALEATORIO}}})([A-Za-z0-9]{{{_LARGO_CHECKSUM}}})$"
)

def _a_base62(numero: int, largo: int) -> str:
    """Entero sin signo a base62, rellenado a la izquierda hasta `largo`."""
    salida = ""
    resto = numero
    while True:
        salida = _ALFABETO[resto % 62] + salida
        resto //= 62
        if resto == 0:
            break
    return salida.rjust(largo, _ALFABETO[0])


def checksum_de(cuerpo: str) -> str:
    """Los seis caracteres finales de una llave.

    PARA QUÉ SIRVE: que una herramienta decida si una cadena con esta pinta es
    una llave nuestra SIN llamar a la API. Baja los falsos positivos de los
    escáneres de secretos y atrapa un dedazo al pegarla.

    NO ES SEGURIDAD: quien quiera fabricar llaves falsas puede calcular el
    checksum igual que nosotros. La autenticación la da el hash guardado, y
    nada más. Por eso `verificar()` no se detiene aquí.

    POR QUÉ SHA-256 Y NO CRC32, que es lo que usa GitHub: las dos sirven, pero
    CRC32 hay que escribirlo a mano en los dos lenguajes y tiene tres variantes
    que se confunden fácil (polinomio reflejado o no, valor inicial, XOR
    final). Un desacuerdo entre JavaScript y Python rechazaría llaves BUENAS en
    silencio. SHA-256 es `hashlib` de un lado y `js-sha256` del otro, sin nada
    que implementar.
    """
    # Los primeros 4 bytes del digest (8 caracteres hex) como entero de 32
    # bits. 62^6 ≈ 5.7e10 > 2^32 ≈ 4.3e9, así que seis caracteres alcanzan.
    digest = hashlib.sha256(cuerpo.encode()).hexdigest()[:8]
    return _a_base62(int(digest, 16), _LARGO_CHECKSUM)


def partes_de(secret: str) -> dict[str, str] | None:
    """Divide una llave en sus partes, o `None` si no tiene esta forma."""
    encontrado = _PATRON.match(secret)
    if not encontrado:
        return None
    return {
        "ambiente": encontrado.group(1),
        "id": encontrado.group(2),
        "cuerpo": secret[: -_LARGO_CHECKSUM],
        "checksum": encontrado.group(4),
    }


def validar_formato(secret: str) -> bool:
    """¿Tiene forma de llave nuestra Y su checksum cuadra?

    Es la primera puerta: descarta basura sin tocar la base. NO dice que la
    llave exista, ni que siga viva.
    """
    partes = partes_de(secret)
    if partes is None:
        return False
    return checksum_de(partes["cuerpo"]) == partes["checksum"]


def id_de(secret: str) -> str | None:
    """El identificador público de la llave, o `None` si no es una llave."""
    partes = partes_de(secret)
    return partes["id"] if partes else None


def hash_de(secret: str) -> str:
    """Lo ÚNICO que la base debe guardar del secret.

    SHA-256 pelón es lo correcto aquí y no hace falta bcrypt ni argon2: esos
    existen para defender contraseñas humanas, que tienen poca entropía y se
    atacan por diccionario. Contra 155 bits aleatorios no hay diccionario que
    sirva, y un hash lento solo costaría latencia en cada request.
    """
    return hashlib.sha256(secret.encode()).hexdigest()


def verificar(
    secret: str,
    buscar_por_id: Callable[[str], Any | None],
    ahora: datetime | None = None,
) -> bool:
    """¿Esta llave sirve AHORA MISMO?

    `buscar_por_id` recibe el identificador público y devuelve la fila de la
    llave (o `None`). Se recibe como función y no se consulta la base aquí a
    propósito: este módulo no debe saber cómo se guardan las llaves, y así se
    puede probar sin base. La fila necesita `secret_hash`, `revocada_en` y
    `expira_en`; se leen con `getattr` y con `[]` para que sirva igual un
    objeto que un dict, que es lo que devuelven los SPs vía pyodbc.

    EL ORDEN IMPORTA y es de más barato a más caro: los tres primeros pasos
    descartan basura sin tocar la base.

    Todos los caminos devuelven lo mismo — False — y quien llame debe
    responder un 401 genérico. Decir "existe pero está revocada" le confirma a
    quien anda probando llaves que acertó una; el motivo real va al log.
    """
    # 1. Forma y checksum. Sin base de datos de por medio.
    if not validar_formato(secret):
        return False

    # 2. La fila, por índice. Recorrer todas las llaves hasheando es justo lo
    #    que el identificador existe para evitar.
    identificador = id_de(secret)
    if identificador is None:
        return False
    fila = buscar_por_id(identificador)
    if fila is None:
        return False

    # 3. El hash, en tiempo CONSTANTE. Un `==` normal corta en el primer byte
    #    distinto, y esa diferencia de microsegundos es medible: deja adivinar
    #    el hash byte por byte. Mismo `compare_digest` que ya usa seguridad.py.
    guardado = _campo(fila, "secret_hash")
    if not isinstance(guardado, str) or not hmac.compare_digest(hash_de(secret), guardado):
        return False

    # 4. Revocada: un hecho registrado, no un cálculo.
    if _campo(fila, "revocada_en") is not None:
        return False

    # 5. Vencida: esto SÍ es un cálculo, y por eso se hace aquí y no se guarda
    #    como bandera — una llave marcada "activa" lo seguiría diciendo para
    #    siempre, porque nada la vuelve a tocar después de emitirla.
    expira = _campo(fila, "expira_en")
    if not isinstance(expira, datetime):
        return False
    momento = ahora or datetime.now(timezone.utc)
    # SQL Server devuelve datetimes sin zona; se asumen UTC, que es como se
    # guardan. Comparar uno naive con uno aware truena con TypeError.
    if expira.tzinfo is None:
        expira = expira.replace(tzinfo=timezone.utc)
    return expira > momento


def _campo(fila: Any, nombre: str) -> Any:
    """Lee un campo lo mismo de un dict que de un objeto."""
    if isinstance(fila, dict):
        return fila.get(nombre)
    return getattr(fila, nombre, None)
