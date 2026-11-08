"""Registro de los DOCUMENTOS de cada organización — registro PROVISIONAL, en JSON y con forma de tabla.

## Qué es un documento

Un documento NACE cuando entra al sistema (hoy, por la bandeja: `bandeja.registrar_entrada` lo crea) y existe por sí mismo: no depende de
ningún expediente. Puede estar en ninguno (el «void»: llegó y todavía no se le asignó nada), en uno o en varios. A qué expedientes
pertenece NO se guarda aquí sino en `servicios/expedientes.py`: el documento no sabe de ellos, así que meterlo a un expediente o sacarlo
nunca lo toca.

Es UNA entidad, no dos: lleva la referencia al archivo (los bytes viven en el almacén, direccionados por su `sha256`) junto a lo que se
sabe de cómo llegó. El OCR, la clasificación y la extracción serán entidades aparte cuando les toque; todavía no existen en el servidor.

Subir dos veces el mismo archivo crea DOS documentos (son dos entradas distintas) que comparten los bytes: el almacén deduplica por
contenido, así que no se duplica disco.

## Campos

`id` (`documento-` + 32 hexadecimales, lo pone el servidor), `tenant`, `sha256`, `rutaRelativa`, `tamanoBytes`, `mime`,
`nombreOriginal`, `canal` (`API` o `MANUAL`), `llaveId` (la API Key con la que se subió, o `None`) y `recibidoEn`.

## Cómo se guarda

Desde el 2026-10-29, donde hay base (dev, staging, producción) en la tabla `nexus.documents` (migración `0003`, ver
`servicios/expedientes_sql.py`). Donde no la hay (la laptop, las pruebas offline), en el almacén: un archivo de solo agregar por
organización, `documentos-{tenant}.jsonl` (ver `servicios/registro.py`), con un solo tipo de evento, `nacer`. Se elige por
`SQLSERVER_HOST` en cada llamada, como en `servicios/tipos_documentales.py`; los dos NO se sincronizan. Un documento no se edita ni
se borra todavía: cuando haga falta (estado, descarte) serán columnas o eventos nuevos.
"""

import re
import uuid
from datetime import datetime, timezone

from config import SQLSERVER_HOST
from servicios import almacen, registro


def _sql():
    """El módulo de SQL si el `.env` dice que hay base; `None` si no. Se importa tarde: sin base, `pyodbc` ni se carga."""
    if not SQLSERVER_HOST:
        return None
    from servicios import expedientes_sql

    return expedientes_sql

#: Por dónde puede entrar un documento. Es el mismo conjunto que `bandeja.CANALES`.
CANALES = frozenset({"API", "MANUAL"})

_RE_ID = re.compile(r"^documento-[0-9a-f]{32}$")


def _nombre(tenant: str) -> str:
    # Valida el tenant con la MISMA regla que el almacén: es también la defensa contra un `../` en el nombre del archivo de registro.
    return f"documentos-{almacen._validar_tenant(tenant)}.jsonl"


def _publico(evento: dict) -> dict:
    """El documento como lo ve quien consulta, sin el campo interno `op`."""
    return {k: v for k, v in evento.items() if k != "op"}


def nacer(tenant: str, guardado: dict, mime: str, nombre_original: str, canal: str, llave_id: str | None = None) -> dict:
    """Anota que un archivo YA GUARDADO en el almacén entró al sistema, y devuelve el documento.

    Va después de `almacen.guardar`, nunca antes: si esto falla queda un objeto sin registro, que es barato; al revés quedaría un
    documento apuntando a un archivo que no existe. Levanta `ErrorAlmacen` si no pudo escribirse."""
    if canal not in CANALES:
        raise ValueError(f"Canal de ingesta inválido: {canal!r}")
    almacen._validar_tenant(tenant)
    if sql := _sql():
        return sql.documentos_nacer(tenant, guardado, mime, nombre_original, canal, llave_id)
    evento = {
        "op": "nacer",
        "id": f"documento-{uuid.uuid4().hex}",
        "tenant": tenant,
        "sha256": guardado["hash"],
        "rutaRelativa": guardado["rutaRelativa"],
        "tamanoBytes": guardado["bytes"],
        "mime": mime,
        "nombreOriginal": nombre_original,
        "canal": canal,
        "llaveId": llave_id,
        "recibidoEn": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    registro.agregar(_nombre(tenant), evento)
    return _publico(evento)


def listar(tenant: str) -> list[dict]:
    """Los documentos de UNA organización, del más viejo al más nuevo."""
    if sql := _sql():
        return sql.documentos_listar(almacen._validar_tenant(tenant))
    return [_publico(e) for e in registro.eventos(_nombre(tenant)) if e.get("op") == "nacer" and isinstance(e.get("id"), str)]


def obtener(tenant: str, documento_id: str) -> dict | None:
    """El documento de esa organización, o `None` si no existe (o es de otra: no se distingue)."""
    if not isinstance(documento_id, str) or not _RE_ID.match(documento_id):
        return None
    if sql := _sql():
        return sql.documentos_obtener(almacen._validar_tenant(tenant), documento_id)
    for d in listar(tenant):
        if d["id"] == documento_id:
            return d
    return None
