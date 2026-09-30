"""Registros de SOLO AGREGAR dentro del almacén.

Los usan la bandeja de entrada (`servicios/bandeja.py`) y las llaves de
cliente (`servicios/llaves_cliente.py`). Son PROVISIONALES los dos: hacen el
trabajo de tablas que todavía no existen en SQL Server, para no depender del
calendario del DBA. El día que existan, lo que cambia son esos dos módulos;
este se queda sin usuarios.

## Cómo se guarda

Un archivo JSONL por registro, en `{ALMACEN_RUTA}/.registro/`. Cada línea es
un evento, y NUNCA se reescribe una línea ya escrita: agregar no pone en
riesgo lo que ya estaba. Un corte a media escritura deja, a lo sumo, una
última línea rota — que se ignora al leer, y que se CIERRA antes de volver a
escribir, para que el evento siguiente no quede pegado a ella y se pierda con
ella (lo encontró `verificar_bandeja.py`).

`.registro` empieza con punto a propósito: el almacén solo acepta prefijos de
tenant `[A-Za-z0-9_-]`, así que ningún tenant puede llamarse así y pisarlo.
Y vive DENTRO del almacén, así que hereda su defensa contra el montaje caído:
sin el centinela no se lee ni se escribe.

## Concurrencia

Un solo proceso uvicorn con threadpool: un candado de módulo basta para que
dos escrituras no intercalen líneas, y para que un "leer y luego agregar"
(retirar una entrada, emitir una llave con id único) sea atómico. Es `RLock`
para que quien ya lo tiene pueda llamar a `agregar` y `eventos` adentro. Con
varios workers esto dejaría de alcanzar — y para entonces ya debería ser la base.
"""

import json
import logging
import os
import re
import threading
from pathlib import Path

from servicios import almacen
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

# Tómalo con `with candado:` para leer y agregar sin que otro hilo se meta en
# medio. `agregar` y `eventos` lo toman solos cuando se llaman sueltos.
candado = threading.RLock()

# El nombre lo arma el código, nunca el usuario, pero se valida igual: es parte
# de una ruta en disco.
_RE_NOMBRE = re.compile(r"^[A-Za-z0-9_-]{1,100}\.jsonl$")


def ruta(nombre: str) -> Path:
    if not _RE_NOMBRE.match(nombre):
        raise ValueError(f"Nombre de registro inválido: {nombre!r}")
    almacen._exigir_raiz_montada()
    return almacen._raiz() / ".registro" / nombre


def agregar(nombre: str, evento: dict) -> None:
    """Agrega UN evento al final del registro. Levanta `ErrorAlmacen` si no
    pudo escribirse."""
    destino = ruta(nombre)
    linea = json.dumps(evento, ensure_ascii=False) + "\n"
    with candado:
        try:
            destino.parent.mkdir(parents=True, exist_ok=True)
            # Si un corte dejó la última línea a medias (sin salto de línea), lo
            # que se agregue ahora se PEGARÍA a ella y se perdería con ella. Se
            # cierra primero la línea rota, que se ignora al leer.
            if destino.exists() and destino.stat().st_size > 0:
                with open(destino, "rb") as f:
                    f.seek(-1, os.SEEK_END)
                    if f.read(1) != b"\n":
                        linea = "\n" + linea
            with open(destino, "a", encoding="utf-8") as f:
                f.write(linea)
                f.flush()
                os.fsync(f.fileno())
        except OSError as exc:
            logger.exception("No se pudo escribir el registro %s", destino)
            raise ErrorAlmacen(f"No se pudo escribir el registro {nombre}: {exc}") from exc


def eventos(nombre: str) -> list[dict]:
    """Todos los eventos del registro, en el orden en que se escribieron. Una
    línea ilegible se salta (con aviso en el log): perder UN evento es mucho
    mejor que dejar de leer el registro entero."""
    origen = ruta(nombre)
    leidos = []
    with candado:
        try:
            if not origen.exists():
                return []
            # En BINARIO y decodificando línea por línea, no en modo texto: un
            # corte a media escritura puede dejar un carácter de dos bytes (una
            # "á" de un nombre) a la mitad, y en modo texto eso levanta
            # UnicodeDecodeError al leer el ARCHIVO ENTERO — o sea, las llaves
            # de todos los clientes dejaban de verificarse por una sola línea.
            # Así se salta esa línea y nada más.
            with open(origen, "rb") as f:
                for numero, cruda in enumerate(f, start=1):
                    cruda = cruda.strip()
                    if not cruda:
                        continue
                    try:
                        evento = json.loads(cruda.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        logger.warning("Línea %s ilegible en %s; se ignora", numero, origen)
                        continue
                    if isinstance(evento, dict):
                        leidos.append(evento)
                    else:
                        logger.warning("Línea %s de %s no es un evento; se ignora", numero, origen)
        except OSError as exc:
            logger.exception("No se pudo leer el registro %s", origen)
            raise ErrorAlmacen(f"No se pudo leer el registro {nombre}: {exc}") from exc
    return leidos
