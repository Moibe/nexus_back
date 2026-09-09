"""Almacén de documentos: dónde viven los BYTES de un archivo subido.

**Nadie lo usa todavía, a propósito** (2026-09-08). Se construyó por
adelantado, a pedido explícito, para que el día que exista la tabla `file` en
SQL Server los bytes y su registro puedan nacer juntos sin tener que diseñar
esto con prisa. Mientras tanto no está cableado a ningún endpoint y el
comportamiento de Nexus es exactamente el mismo que antes: un documento sigue
viviendo solo en la memoria del navegador y se pierde al refrescar.

## Qué guarda y qué NO

Guarda bytes y devuelve una RUTA RELATIVA. Los metadatos —nombre original,
MIME, tenant, expediente, quién lo subió— son de la base, no de aquí. Este
módulo no sabe qué es un documento; sabe escribir y leer contenido sin
corromperlo.

## Direccionado por contenido, dentro de cada tenant

La ruta es `{tenant}/{aa}/{bb}/{sha256}` — sin extensión, porque el MIME vive
en la base y dos archivos con los mismos bytes no son dos cosas distintas por
llamarse `.jpg` y `.jpeg`.

  - **Por hash**: subir dos veces la misma INE ocupa un solo objeto, y el hash
    ya lo calcula el front para detectar duplicados en la Bandeja, así que no
    hay trabajo nuevo. Además el nombre es verificable: si el contenido se
    corrompe, deja de coincidir con su propio nombre.
  - **Los dos niveles `aa`/`bb`** (primeros 4 hex del hash) evitan un
    directorio con cien mil entradas. Sobre NFS/CIFS eso degrada de verdad, y
    un `ls` de diagnóstico se vuelve imposible.
  - **Dentro del tenant y no global**: la deduplicación global filtraría
    información entre clientes (el tenant B "hereda" un objeto que solo el A
    subió, y quien mire el almacén sabe que existe), y volvería imposible
    borrar o poner cuota por tenant. Se prefiere gastar disco.

## Lo que este módulo NO resuelve, y hay que saberlo antes de usarlo

`borrar` quita el objeto sin preguntar a nadie. Con deduplicación eso es
peligroso: si dos filas de `file` apuntan al mismo hash —el mismo documento
subido a dos expedientes— borrar por una de ellas deja a la otra apuntando a
un archivo que ya no está. El conteo de referencias vive en la base, así que
el borrado real tiene que decidirse allá y llamar aquí solo cuando ya no
quede ninguna referencia. Está documentado en la propia función.

## Es SÍNCRONO

Escribir en un NAS puede tardar. Estas funciones bloquean, así que el endpoint
que las use debe ser un `def` normal y no `async def` — FastAPI lo corre en su
threadpool y no congela el event loop. Es la misma convención que ya sigue
`pyodbc` en este proyecto, por el mismo motivo.
"""

import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path

from config import ALMACEN_RUTA

logger = logging.getLogger(__name__)

# Un hash SHA-256 en hexadecimal y nada más. Se valida ANTES de construir una
# ruta con él: es la única defensa contra que algo como "../../etc/passwd"
# llegue desde afuera y se convierta en una escritura fuera del almacén.
_RE_HASH = re.compile(r"^[0-9a-f]{64}$")

# El identificador de tenant que se acepta como segmento de ruta. La base usa
# un GUID; se permite algo más ancho por si cambia, pero nunca separadores ni
# puntos, por el mismo motivo que el hash.
_RE_TENANT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class ErrorAlmacen(RuntimeError):
    """Algo salió mal al escribir o leer del almacén.

    Se distingue de un error de validación (esos son `ValueError`) porque
    significan cosas distintas para quien llama: un `ValueError` es un bug o
    entrada mala y no se arregla reintentando; un `ErrorAlmacen` suele ser el
    NAS caído o sin permiso, y ahí reintentar sí tiene sentido.
    """


def esta_configurado() -> bool:
    """Si hay una ruta de almacén definida.

    Existe para que quien llame pueda degradar con elegancia en vez de
    tronar: hoy `ALMACEN_RUTA` viene vacía en todos lados, y eso es un estado
    válido, no un error.
    """
    return bool(ALMACEN_RUTA)


def _raiz() -> Path:
    if not ALMACEN_RUTA:
        raise ErrorAlmacen(
            "El almacén de documentos no está configurado: falta ALMACEN_RUTA en el .env."
        )
    return Path(ALMACEN_RUTA)


def _validar_hash(hash_hex: str) -> str:
    limpio = (hash_hex or "").strip().lower()
    if not _RE_HASH.match(limpio):
        raise ValueError(f"Hash SHA-256 inválido: {hash_hex!r}")
    return limpio


def _validar_tenant(tenant: str) -> str:
    limpio = (tenant or "").strip()
    if not _RE_TENANT.match(limpio):
        raise ValueError(f"Identificador de tenant inválido: {tenant!r}")
    return limpio


def ruta_relativa(tenant: str, hash_hex: str) -> str:
    """La ruta con la que la base debe guardar el objeto.

    RELATIVA a propósito: el día que el NAS cambie de punto de montaje —o que
    esto se mude a un bucket— la columna de la base sigue siendo válida y solo
    cambia `ALMACEN_RUTA`. Guardar la ruta absoluta obligaría a una migración
    de datos por un cambio de infraestructura.
    """
    t = _validar_tenant(tenant)
    h = _validar_hash(hash_hex)
    return f"{t}/{h[0:2]}/{h[2:4]}/{h}"


def _ruta_absoluta(relativa: str) -> Path:
    destino = (_raiz() / relativa).resolve()
    raiz = _raiz().resolve()
    # Cinturón sobre los tirantes: aunque `ruta_relativa` ya valide sus partes,
    # `leer`/`borrar` reciben la ruta desde la BASE, y una fila corrupta o
    # manipulada no debe poder salir del almacén.
    if raiz != destino and raiz not in destino.parents:
        raise ValueError(f"La ruta {relativa!r} apunta fuera del almacén.")
    return destino


def guardar(tenant: str, contenido: bytes, hash_esperado: str | None = None) -> dict:
    """Guarda los bytes y devuelve dónde quedaron.

    `hash_esperado` es opcional y sirve para VERIFICAR, no para confiar: el
    hash con el que se nombra el objeto siempre se recalcula aquí sobre los
    bytes que de verdad llegaron. Si el front manda el suyo y no coinciden, el
    archivo se corrompió en el camino y es mejor saberlo ahora que descubrirlo
    meses después con un documento ilegible.

    Es idempotente: si el objeto ya existe con ese contenido, no se reescribe
    y `yaExistia` sale en True. Volver a subir el mismo documento no gasta
    disco ni arriesga un archivo a medio escribir.

    La escritura es ATÓMICA: primero a un temporal en el MISMO directorio y
    después `os.replace`, que en POSIX es atómico dentro del mismo sistema de
    archivos. Sin eso, un corte de red del NAS a media escritura dejaría un
    objeto truncado con nombre de objeto completo — es decir, basura que se ve
    válida. Con esto, o está entero o no está.
    """
    if not contenido:
        raise ValueError("No hay contenido que guardar: el archivo llegó vacío.")

    hash_real = hashlib.sha256(contenido).hexdigest()
    if hash_esperado is not None and _validar_hash(hash_esperado) != hash_real:
        raise ValueError(
            "El contenido no coincide con su hash: llegó "
            f"{_validar_hash(hash_esperado)} y los bytes dan {hash_real}."
        )

    relativa = ruta_relativa(tenant, hash_real)
    destino = _ruta_absoluta(relativa)
    temporal: Path | None = None

    if destino.exists():
        return {
            "rutaRelativa": relativa,
            "hash": hash_real,
            "bytes": len(contenido),
            "yaExistia": True,
        }

    try:
        destino.parent.mkdir(parents=True, exist_ok=True)
        # `delete=False` + `os.replace` en vez de escribir directo al destino:
        # ver el docstring. El temporal va en el MISMO directorio porque
        # `os.replace` solo es atómico dentro del mismo sistema de archivos, y
        # /tmp casi nunca lo es cuando el destino es un montaje de red.
        with tempfile.NamedTemporaryFile(
            dir=destino.parent, prefix=".tmp-", suffix=".parcial", delete=False
        ) as tmp:
            temporal = Path(tmp.name)
            tmp.write(contenido)
            tmp.flush()
            # Sin esto los bytes pueden estar solo en el caché del sistema: un
            # corte de energía justo después del replace dejaría un archivo
            # vacío con nombre bueno.
            os.fsync(tmp.fileno())
        os.replace(temporal, destino)
    except OSError as exc:
        # Limpiar el temporal si quedó a medias; si esa limpieza también falla
        # no se propaga, porque el error que importa es el de arriba.
        try:
            if temporal is not None:
                temporal.unlink(missing_ok=True)
        except OSError:
            pass
        logger.exception("No se pudo guardar el documento en %s", relativa)
        raise ErrorAlmacen(f"No se pudo guardar el documento: {exc}") from exc

    return {
        "rutaRelativa": relativa,
        "hash": hash_real,
        "bytes": len(contenido),
        "yaExistia": False,
    }


def leer(relativa: str) -> bytes:
    """Devuelve los bytes de un objeto por su ruta relativa.

    No verifica el hash al leer: hacerlo costaría recorrer el archivo entero
    en cada lectura, y el nombre del objeto ya ES su hash, así que quien
    necesite comprobar integridad puede hacerlo sin ayuda de esta función.
    """
    ruta = _ruta_absoluta(relativa)
    try:
        return ruta.read_bytes()
    except FileNotFoundError as exc:
        raise ErrorAlmacen(f"El documento {relativa} no está en el almacén.") from exc
    except OSError as exc:
        logger.exception("No se pudo leer el documento %s", relativa)
        raise ErrorAlmacen(f"No se pudo leer el documento: {exc}") from exc


def existe(relativa: str) -> bool:
    """Si el objeto está. Útil para detectar filas de la base que apuntan a un
    archivo que ya no existe, sin traerse su contenido a memoria."""
    try:
        return _ruta_absoluta(relativa).is_file()
    except OSError:
        return False


def borrar(relativa: str) -> bool:
    """Borra el objeto. Devuelve False si ya no estaba.

    **OJO con la deduplicación.** Los objetos se direccionan por contenido, así
    que dos filas de `file` —el mismo documento subido a dos expedientes—
    apuntan al MISMO archivo. Borrar aquí por una de ellas deja a la otra
    colgando. Quien llame tiene que haber comprobado en la base que no queda
    ninguna referencia; este módulo no tiene forma de saberlo.

    No borra los directorios que queden vacíos, a propósito: dos borrados
    simultáneos podrían competir por el mismo directorio padre y hacer fallar
    al otro. Un directorio vacío no molesta a nadie.
    """
    try:
        _ruta_absoluta(relativa).unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.exception("No se pudo borrar el documento %s", relativa)
        raise ErrorAlmacen(f"No se pudo borrar el documento: {exc}") from exc
