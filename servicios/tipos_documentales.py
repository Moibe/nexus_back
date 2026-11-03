"""Registro de los TIPOS DOCUMENTALES de cada organización.

## Qué es un tipo

El back lo trata casi como una caja: `datos` es el objeto que el front ya manejaba (campos, pasos, historial, recortes…) y
se guarda tal cual. Solo se EXIGE y se lee lo que el back necesita para algo:

- `nombre` y `descripcion` — son las categorías del clasificador (la descripción es prompt real para el modelo);
- `instrucciones` — opcionales (hasta `MAX_INSTRUCCIONES` caracteres): viajan al extractor como su `documentPrompt`, ver `esquema.esquema_desde_campos`;
- `estado` — `borrador`, `activo` o `archivado`: solo los `activo` entran al clasificador;
- `procesadorId` — para saber qué procesadores de Google tienen dueño (`servicios/procesadores_huerfanos.py`).

Lo que el servidor pone y el cliente no puede escribir: `revision`, `creadoPor`, `creadoEn`, `modificadoPor`, `modificadoEn`
y `tenant`. Se guardan junto al tipo, nunca dentro de `datos`.

## Revisión (dos personas editando lo mismo)

Cada tipo lleva un número de `revision`. Para guardar hay que decir sobre cuál se editó: si en el ínterin otra persona lo
guardó, es un 409 con la revisión vigente, y NO se pisa nada en silencio. `0` significa «es nuevo».

## Ids

Los inventa el cliente (`tipo-` + 32 hexadecimales de un UUID) y el servidor exige que no se repitan EN NINGUNA organización:
el id nombra cada categoría del clasificador y cada procesador en Google (y el registro lo busca sin mirar el tenant), así que dos
tipos con el mismo id, de organizaciones distintas, serían el mismo. Un id que ya existe en OTRA organización se contesta igual
que uno repetido en la propia, sin nombrar de quién es.

## Dónde se guarda (2026-10-28)

Este módulo es la PUERTA; el almacenamiento son dos, y se elige según haya base o no:

- **`tipos_documentales_sql.py` — la tabla `nexus.documentTypes`** (migración `0002`). Es lo que corre en dev, staging y
  producción. Primera entidad del producto en SQL: el piloto de la decisión del 2026-10-21 («Nexus sin stored procedures»).
- **`tipos_documentales_archivo.py` — el JSONL del almacén**, lo de siempre. Es lo que corre donde NO hay base: la laptop de
  Moibe (sus `SQLSERVER_*` están vacíos a propósito) y todas las pruebas offline.

Se decide por `SQLSERVER_HOST`, el mismo interruptor con el que `app.py` publica o no el grupo `/documentos/*`, y se decide en
cada llamada, no al importar. El módulo de SQL se importa tarde a propósito: sin base, `pyodbc` ni se carga (hay máquinas sin el
runtime de ODBC, y ahí importarlo reventaría).

⚠️ Los dos almacenamientos NO se sincronizan ni se migran entre sí: son el mismo producto sobre bases distintas. Lo que se dé de
alta en la laptop no aparece en dev, y al revés. Las herramientas que reescriben registros del almacén (`renombrar_tenant.py`,
`purgar_creado_por.py`) solo ven el JSONL: donde los tipos ya viven en SQL, no los tocan.
"""

from config import SQLSERVER_HOST

# Los errores del dominio viven en el módulo común, pero se re-exportan aquí porque es a esta puerta a la que llaman los
# routers (`except registro_tipos.Conflicto`), y quien llama no tiene por qué saber dónde se guarda.
from servicios.tipos_documentales_comun import (  # noqa: F401
    ESTADOS,
    TOPE_BYTES,
    Conflicto,
    NoEncontrado,
    TipoInvalido,
)


def _almacenamiento():
    """El módulo que guarda: SQL si el `.env` dice que hay base, el almacén si no."""
    if SQLSERVER_HOST:
        from servicios import tipos_documentales_sql

        return tipos_documentales_sql
    from servicios import tipos_documentales_archivo

    return tipos_documentales_archivo


def en_sql() -> bool:
    """Si los tipos de esta instancia viven en la base. Para que un diagnóstico pueda decirlo sin adivinar."""
    return bool(SQLSERVER_HOST)


def listar(tenant: str) -> list[dict]:
    """Los tipos de UNA organización, en el orden que ella les dio (los nuevos al final, en el de su creación)."""
    return _almacenamiento().listar(tenant)


def obtener(tenant: str, tipo_id: str) -> dict | None:
    return _almacenamiento().obtener(tenant, tipo_id)


def guardar(tenant: str, tipo_id: str, datos: object, revision: int, por: str) -> tuple[dict, dict | None]:
    """Crea (`revision == 0`) o actualiza (`revision` = la que se editó) un tipo.

    Devuelve `(el tipo como quedó, el tipo ANTERIOR o None)`; el anterior es para que quien llama anote el cambio de estado.
    Levanta `TipoInvalido`, `NoEncontrado` (actualizar uno que no es de esta organización) o `Conflicto`.
    """
    return _almacenamiento().guardar(tenant, tipo_id, datos, revision, por)


def eliminar(tenant: str, tipo_id: str, por: str) -> dict:
    """Quita un tipo de la organización. Devuelve el que se quitó. `NoEncontrado` si no es de ella."""
    return _almacenamiento().eliminar(tenant, tipo_id, por)


def ordenar(tenant: str, ids: list[str], por: str) -> None:
    """Fija el orden de la biblioteca de una organización. Solo cuentan los ids que son SUYOS: el resto se ignora."""
    return _almacenamiento().ordenar(tenant, ids, por)


def tipos_activos(tenant: str) -> list[dict]:
    """Los tipos ACTIVOS de UNA organización, con lo que su clasificador necesita de cada uno. El orden es el de creación.

    Desde el 2026-10-26 cada organización tiene su clasificador (`servicios/clasificadores.py`), así que arma sus categorías solo con
    los suyos: antes eran los de todas, y un documento de una podía caer en un tipo de otra.
    """
    return _almacenamiento().tipos_activos(tenant)


def procesadores_con_dueno() -> dict[str, dict]:
    """`{procesadorId: {tenant, tipo, version}}` de cada procesador que algún tipo conoce, vigente o de su historial.

    Es lo que usa la limpieza de procesadores huérfanos: lo que NO está aquí no tiene dueño en ninguna organización.
    """
    return _almacenamiento().procesadores_con_dueno()
