"""Registro PROVISIONAL de la bandeja de entrada: qué archivos llegaron por la
API y siguen esperando en la bandeja de preparación del front.

## Por qué existe, y por qué es provisional

La bandeja del front vivía solo en la memoria del navegador: un archivo que
un cliente sube por la API no tenía forma de aparecer ahí. Lo definitivo es
una fila en `[documents].[files]` dentro del expediente de entrada del tenant
(ver `docs/solicitudes-dba.md`, sección 4), pero eso espera los SPs del DBA.
Esto hace el mismo trabajo mientras tanto, para no depender de ese calendario.

El día que existan los SPs, lo que cambia es ESTE módulo: `registrar_entrada`,
`listar_pendientes` y `retirar` pasan a llamar a la base, y ni el router ni el
front se enteran. Los eventos ya guardados aquí sirven para el backfill: traen
exactamente lo que pide `uspCreateFile`.

## Dónde y cómo se guarda

Un archivo por tenant, `{ALMACEN_RUTA}/.registro/bandeja-{tenant}.jsonl`, de
SOLO AGREGAR: cada línea es un evento, `entrada` o `retiro`. Lo pendiente es
lo que entró y no ha salido. Se eligió así, y no un JSON que se reescribe,
porque agregar una línea nunca pone en riesgo lo que ya estaba escrito: un
corte a media escritura deja, a lo sumo, una última línea rota que se ignora.

`.registro` empieza con punto a propósito: el almacén solo acepta prefijos de
tenant `[A-Za-z0-9_-]`, así que ningún tenant puede llamarse así y pisarlo.

Vive DENTRO del almacén, así que hereda su defensa contra el montaje caído:
sin el centinela no se lee ni se escribe (ver `servicios/almacen.py`).

## Concurrencia

Un solo proceso uvicorn con threadpool: un `Lock` de módulo basta para que
dos subidas simultáneas no intercalen sus líneas. Si algún día hubiera varios
workers, esto dejaría de alcanzar — y para entonces ya debería ser la base.
"""

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from servicios import almacen
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

CANALES = {"API", "MANUAL"}
MOTIVOS_RETIRO = {"pipeline", "descartado"}

_candado = threading.Lock()


def _archivo_de(tenant: str) -> Path:
    # Valida el tenant con la MISMA regla que el almacén: es también la defensa
    # contra un `../` en el nombre del archivo de registro.
    tenant = almacen._validar_tenant(tenant)
    almacen._exigir_raiz_montada()
    return almacen._raiz() / ".registro" / f"bandeja-{tenant}.jsonl"


def _agregar(tenant: str, evento: dict) -> None:
    ruta = _archivo_de(tenant)
    linea = json.dumps(evento, ensure_ascii=False) + "\n"
    try:
        ruta.parent.mkdir(parents=True, exist_ok=True)
        # Si un corte dejó la última línea a medias (sin salto de línea), lo que
        # se agregue ahora se PEGARÍA a ella y se perdería con ella. Se cierra
        # primero la línea rota, que se ignora al leer, y se escribe limpio.
        if ruta.exists() and ruta.stat().st_size > 0:
            with open(ruta, "rb") as f:
                f.seek(-1, os.SEEK_END)
                if f.read(1) != b"\n":
                    linea = "\n" + linea
        with open(ruta, "a", encoding="utf-8") as f:
            f.write(linea)
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        logger.exception("No se pudo escribir el registro de la bandeja (%s)", ruta)
        raise ErrorAlmacen(f"No se pudo escribir el registro de la bandeja: {exc}") from exc


def _eventos(tenant: str) -> list[dict]:
    ruta = _archivo_de(tenant)
    if not ruta.exists():
        return []
    eventos = []
    try:
        with open(ruta, encoding="utf-8") as f:
            for numero, linea in enumerate(f, start=1):
                linea = linea.strip()
                if not linea:
                    continue
                try:
                    eventos.append(json.loads(linea))
                except json.JSONDecodeError:
                    # Una línea rota (un corte a media escritura) se salta: perder
                    # UN evento es mucho mejor que dejar de leer la bandeja entera.
                    logger.warning("Línea %s ilegible en %s; se ignora", numero, ruta)
    except OSError as exc:
        logger.exception("No se pudo leer el registro de la bandeja (%s)", ruta)
        raise ErrorAlmacen(f"No se pudo leer el registro de la bandeja: {exc}") from exc
    return eventos


def registrar_entrada(
    tenant: str, guardado: dict, mime: str, nombre_original: str, canal: str
) -> dict:
    """Anota que un archivo YA GUARDADO en el almacén entró a la bandeja.

    Va después de `almacen.guardar`, nunca antes — el mismo orden que tendrá
    la fila de `file`: bytes primero, registro después. Si esto falla, queda un
    objeto sin registro, que es barato; al revés quedaría una entrada apuntando
    a un archivo que no existe.
    """
    if canal not in CANALES:
        raise ValueError(f"Canal de ingesta inválido: {canal!r}")
    entrada = {
        "evento": "entrada",
        "id": uuid.uuid4().hex,
        "tenant": tenant,
        "rutaRelativa": guardado["rutaRelativa"],
        "sha256": guardado["hash"],
        "tamanoBytes": guardado["bytes"],
        "mime": mime,
        "nombreOriginal": nombre_original,
        "canal": canal,
        "recibidoEn": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    with _candado:
        _agregar(tenant, entrada)
    return _publica(entrada)


def listar_pendientes(tenant: str) -> list[dict]:
    """Lo que entró y todavía no se retira, del más viejo al más nuevo."""
    with _candado:
        eventos = _eventos(tenant)
    retirados = {e.get("id") for e in eventos if e.get("evento") == "retiro"}
    return [
        _publica(e)
        for e in eventos
        if e.get("evento") == "entrada" and e.get("id") not in retirados
    ]


def retirar(tenant: str, id_entrada: str, motivo: str) -> bool:
    """Saca una entrada de la bandeja. `False` si no existe o ya había salido.

    El archivo NO se borra del almacén: se retira de la bandeja porque pasó al
    pipeline o porque alguien lo descartó, y en los dos casos los bytes siguen
    haciendo falta (o pueden hacerla) — y con la deduplicación por contenido,
    borrar sería peligroso sin un conteo de referencias.
    """
    if motivo not in MOTIVOS_RETIRO:
        raise ValueError(f"Motivo de retiro inválido: {motivo!r}")
    with _candado:
        eventos = _eventos(tenant)
        entro = any(e.get("evento") == "entrada" and e.get("id") == id_entrada for e in eventos)
        salio = any(e.get("evento") == "retiro" and e.get("id") == id_entrada for e in eventos)
        if not entro or salio:
            return False
        _agregar(
            tenant,
            {
                "evento": "retiro",
                "id": id_entrada,
                "motivo": motivo,
                "en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
    return True


def _publica(entrada: dict) -> dict:
    """La entrada como la ve quien consulta, sin el campo interno `evento`."""
    return {k: v for k, v in entrada.items() if k != "evento"}
