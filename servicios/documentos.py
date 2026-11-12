"""Los DOCUMENTOS de cada organización (`nexus.documents`, migración `0003`).

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

En la tabla `nexus.documents` (migración `0003`, ver `servicios/expedientes_sql.py`). Hasta el 2026-11-11, donde no había base, iba a un
JSONL de solo agregar por organización; ese camino ya no existe. Un documento no se edita ni se borra todavía: cuando haga falta (estado,
descarte) serán columnas nuevas.
"""

import re

from servicios import almacen, expedientes_sql

#: Por dónde puede entrar un documento. Es el mismo conjunto que `bandeja.CANALES`.
CANALES = frozenset({"API", "MANUAL"})

_RE_ID = re.compile(r"^documento-[0-9a-f]{32}$")


def nacer(tenant: str, guardado: dict, mime: str, nombre_original: str, canal: str, llave_id: str | None = None) -> dict:
    """Anota que un archivo YA GUARDADO en el almacén entró al sistema, y devuelve el documento.

    Va después de `almacen.guardar`, nunca antes: si esto falla queda un objeto sin registro, que es barato; al revés quedaría un
    documento apuntando a un archivo que no existe. Levanta `ErrorAlmacen` si no pudo escribirse."""
    if canal not in CANALES:
        raise ValueError(f"Canal de ingesta inválido: {canal!r}")
    almacen._validar_tenant(tenant)
    return expedientes_sql.documentos_nacer(tenant, guardado, mime, nombre_original, canal, llave_id)


def listar(tenant: str) -> list[dict]:
    """Los documentos de UNA organización, del más viejo al más nuevo."""
    return expedientes_sql.documentos_listar(almacen._validar_tenant(tenant))


def obtener(tenant: str, documento_id: str) -> dict | None:
    """El documento de esa organización, o `None` si no existe (o es de otra: no se distingue)."""
    if not isinstance(documento_id, str) or not _RE_ID.match(documento_id):
        return None
    return expedientes_sql.documentos_obtener(almacen._validar_tenant(tenant), documento_id)
