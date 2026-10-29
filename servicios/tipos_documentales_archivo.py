"""Tipos documentales guardados en el ALMACÉN, en un JSONL de solo agregar. Es el almacenamiento de siempre.

Desde el 2026-10-28 ya no es el único: donde hay base (dev, staging, producción) manda `tipos_documentales_sql.py`. Este sigue
vivo para las máquinas que NO alcanzan SQL Server —la laptop de Moibe y todas las pruebas offline—, y lo elige
`servicios/tipos_documentales.py` cuando el `.env` no trae `SQLSERVER_HOST`.

## Cómo se guarda

Un solo archivo de solo-agregar (`tipos-documentales.jsonl`, ver `servicios/registro.py`), con tres clases de evento:
`guardar` (el tipo COMPLETO, con su revisión nueva), `eliminar` y `orden` (el orden de la biblioteca de una organización).
El estado vigente se arma doblando el archivo de principio a fin.

⚠️ Cada guardado escribe el tipo entero: un tipo con muchos recortes pesa decenas de KB y el archivo crece con cada edición.
Con el uso de hoy basta; la salida de verdad es la base, que ya existe.
"""

from servicios import registro
from servicios.tipos_documentales_comun import (
    Conflicto,
    NoEncontrado,
    activos,
    ahora,
    dueños_de_procesadores,
    publico,
    validar,
    validar_revision,
)

ARCHIVO = "tipos-documentales.jsonl"


def _doblar() -> tuple[dict[str, dict], dict[str, list[str]]]:
    """El estado vigente: `{id: tipo}` y `{tenant: [ids en el orden pedido]}`. Se llama con el candado puesto."""
    tipos: dict[str, dict] = {}
    ordenes: dict[str, list[str]] = {}
    for e in registro.eventos(ARCHIVO):
        op = e.get("op")
        if op == "guardar" and isinstance(e.get("id"), str) and isinstance(e.get("datos"), dict):
            previo = tipos.get(e["id"])
            tipos[e["id"]] = {
                "id": e["id"],
                "tenant": e.get("tenant", ""),
                "revision": int(e.get("revision", 1)),
                "datos": e["datos"],
                "creadoPor": previo["creadoPor"] if previo else e.get("por"),
                "creadoEn": previo["creadoEn"] if previo else e.get("en"),
                "modificadoPor": e.get("por"),
                "modificadoEn": e.get("en"),
            }
        elif op == "eliminar":
            tipos.pop(e.get("id"), None)
        elif op == "orden" and isinstance(e.get("ids"), list):
            ordenes[e.get("tenant", "")] = [i for i in e["ids"] if isinstance(i, str)]
    return tipos, ordenes


def listar(tenant: str) -> list[dict]:
    with registro.candado:
        tipos, ordenes = _doblar()
    suyos = [t for t in tipos.values() if t["tenant"] == tenant]
    pedido = {tipo_id: i for i, tipo_id in enumerate(ordenes.get(tenant, []))}
    # `sorted` es estable: los que no están en el orden pedido conservan el de creación entre sí y van al final.
    suyos.sort(key=lambda t: pedido.get(t["id"], len(pedido)))
    return [publico(t) for t in suyos]


def obtener(tenant: str, tipo_id: str) -> dict | None:
    with registro.candado:
        tipos, _ = _doblar()
    t = tipos.get(tipo_id)
    return publico(t) if t and t["tenant"] == tenant else None


def guardar(tenant: str, tipo_id: str, datos: object, revision: int, por: str) -> tuple[dict, dict | None]:
    limpio = validar(tipo_id, datos)
    validar_revision(revision)
    with registro.candado:
        tipos, _ = _doblar()
        actual = tipos.get(tipo_id)
        if revision == 0:
            if actual is not None:
                # Repetido en esta organización: se dice cuál es la vigente. En OTRA: no se nombra de quién es.
                raise Conflicto(actual["revision"] if actual["tenant"] == tenant else None)
            nueva = 1
        else:
            if actual is None or actual["tenant"] != tenant:
                raise NoEncontrado(tipo_id)
            if actual["revision"] != revision:
                raise Conflicto(actual["revision"])
            nueva = revision + 1
        registro.agregar(
            ARCHIVO,
            {"op": "guardar", "id": tipo_id, "tenant": tenant, "revision": nueva, "datos": limpio, "por": por, "en": ahora()},
        )
        tipos, _ = _doblar()
        return publico(tipos[tipo_id]), publico(actual) if actual else None


def eliminar(tenant: str, tipo_id: str, por: str) -> dict:
    with registro.candado:
        tipos, _ = _doblar()
        actual = tipos.get(tipo_id)
        if actual is None or actual["tenant"] != tenant:
            raise NoEncontrado(tipo_id)
        registro.agregar(ARCHIVO, {"op": "eliminar", "id": tipo_id, "tenant": tenant, "por": por, "en": ahora()})
        return publico(actual)


def ordenar(tenant: str, ids: list[str], por: str) -> None:
    with registro.candado:
        tipos, _ = _doblar()
        propios = [i for i in ids if isinstance(i, str) and i in tipos and tipos[i]["tenant"] == tenant]
        # Sin repetidos, conservando el orden en que llegaron.
        unicos = list(dict.fromkeys(propios))
        registro.agregar(ARCHIVO, {"op": "orden", "tenant": tenant, "ids": unicos, "por": por, "en": ahora()})


def tipos_activos(tenant: str) -> list[dict]:
    with registro.candado:
        tipos, _ = _doblar()
    return activos(list(tipos.values()), tenant)


def procesadores_con_dueno() -> dict[str, dict]:
    with registro.candado:
        tipos, _ = _doblar()
    return dueños_de_procesadores(list(tipos.values()))
