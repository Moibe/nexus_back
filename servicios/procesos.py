"""Registro de los PROCESOS de cada organización (el motor de reglas) — registro PROVISIONAL.

## Qué es un proceso

Un proceso es un conjunto de reglas y corre sobre un EXPEDIENTE (un conjunto de documentos). Se arma en un lienzo: los tipos
documentales de la organización son sus entradas (nodos de tipo `tipo-documental`), las reglas son operadores (nodos de tipo
`operador`: igual, distinto, contiene, mayor, menor; cantidad, suma, alguno, todos y unicos, que trabajan con LISTAS; existe, que
pregunta si el expediente trae un documento; y, o, no, implica),
un `constante` es un valor fijo que se escribe en el lienzo (por ejemplo el «2» de «más de 2 socios») y los cables llevan un campo,
el documento entero (el punto `@documento` de un tipo, solo para `existe`), un valor fijo o el resultado de una regla a la entrada
de un operador. Para «existe», la muestra `a` es el ejemplo elegido (`si` o `no`).

Guardado, un proceso es su `nombre`, su `descripcion` y el dibujo: `nodos` y `aristas`. Es lo mismo que maneja el lienzo del
front, así que el back lo trata como una caja y NO interpreta las reglas: no sabe qué es «igual» ni qué campo es de qué tipo
(eso vive en el front, en `reglas.ts`, hasta que haya un motor que corra los procesos de verdad). Lo que SÍ comprueba es que el
dibujo esté entero: ids de nodos únicos, cables que unen nodos que existen, números donde van números, y un tope de tamaño.

Lo demás que mande el cliente dentro de `datos` (la vista del lienzo, y desde el 2026-10-29 `activo`, que la lista de procesos del front usa
para marcar un proceso activo o inactivo; ausente = activo) se conserva tal cual: el back no lo interpreta. El front DEPENDE de eso, y
`verificar_procesos.py` lo vigila.

## Lo que pone el servidor y el cliente no puede escribir

`revision`, `creadoPor`, `creadoEn`, `modificadoPor`, `modificadoEn` y `tenant`. Se guardan junto al proceso, nunca dentro de `datos`.

## Revisión (dos personas editando lo mismo)

Igual que en `servicios/tipos_documentales.py`: cada proceso lleva un número de `revision`; para guardar hay que decir sobre cuál se
editó, y si en el ínterin otra persona lo guardó es un 409 con la revisión vigente: NO se pisa nada en silencio. `0` = «es nuevo».

## Ids

Los inventa el cliente (`proceso-` + 32 hexadecimales) y el servidor exige que no se repitan en NINGUNA organización: un id que ya
existe en OTRA se contesta igual que uno repetido en la propia, sin nombrar de quién es.

## El tipo de expediente virtual sobre el que corre (2026-10-29)

Desde que el expediente es un receptáculo y los grupitos con tipo son los EXPEDIENTES VIRTUALES (`servicios/expedientes_virtuales.py`), un
proceso se LIGA a un tipo de expediente virtual: `datos.tipoExpedienteVirtualId` («Solicitud de crédito»), o `null` mientras no se elija.
Es el contrato entre el proceso y los documentos: el proceso compara datos de ciertos tipos documentales y el tipo dice qué tipos
documentales trae cada expediente virtual. El servidor exige que sea un tipo de la MISMA organización (uno ajeno se contesta igual que uno
que no existe) y, si el tipo se borra después, el proceso se queda sin liga (en SQL lo hace la base, ON DELETE SET NULL; en el almacén se
lee como `null`). Todavía no hay motor que corra el proceso sobre las instancias: los documentos aún no guardan su tipo documental ni los
datos extraídos, que es lo que las reglas comparan.

## Cómo se guarda

Desde el 2026-10-29, donde hay base, en `nexus.processes` (migración `0005`, `servicios/procesos_sql.py`), con la liga en su propia columna.
Este módulo valida igual en los dos casos y elige por `SQLSERVER_HOST` en cada llamada. Donde no hay base (la laptop, las pruebas offline),
un solo archivo de solo-agregar (`procesos.jsonl`, ver `servicios/registro.py`) con dos clases de evento: `guardar` (el proceso COMPLETO,
con su revisión nueva) y `eliminar`. El estado vigente se arma doblando el archivo de principio a fin.

⚠️ En el almacén cada guardado escribe el proceso entero y el archivo crece con cada edición; la salida de verdad es la base, que ya existe.
"""

import json
import logging
import math
import re
from datetime import datetime, timezone

from config import SQLSERVER_HOST
from servicios import registro, tipos_expediente


def _sql():
    """El módulo de SQL si el `.env` dice que hay base; `None` si no. Se importa tarde: sin base, `pyodbc` ni se carga."""
    if not SQLSERVER_HOST:
        return None
    from servicios import procesos_sql

    return procesos_sql

logger = logging.getLogger(__name__)

ARCHIVO = "procesos.jsonl"

_RE_ID = re.compile(r"^proceso-[a-z0-9-]{6,58}$")

#: Los tipos de nodo que existen en el lienzo.
TIPOS_DE_NODO = ("tipo-documental", "operador", "constante")

#: De qué clase es el valor de un nodo `constante`: las cuatro familias que el lienzo sabe comparar.
FAMILIAS = ("texto", "numero", "fecha", "booleano")

#: Topes. Un proceso normal tiene unas decenas de nodos; esto es para que un cliente descontrolado no llene el disco.
MAX_NODOS = 300
MAX_ARISTAS = 600
MAX_TEXTO = 500
#: El valor de ejemplo de una LISTA son varios renglones (uno por elemento), así que cabe mucho más que un valor suelto.
MAX_MUESTRA = 5000
TOPE_BYTES = 1_000_000

#: Lo que NO se acepta dentro de `datos`: lo pone el servidor.
_RESERVADAS = frozenset({"revision", "creadoPor", "creadoEn", "modificadoPor", "modificadoEn", "tenant", "meta"})


class ProcesoInvalido(ValueError):
    """Lo que llegó no es un proceso guardable."""


class NoEncontrado(LookupError):
    """No hay tal proceso EN ESA organización (o no existe: no se distingue)."""


class Conflicto(Exception):
    """Alguien lo guardó antes: `revision_actual` es la vigente, o `None` si el id ya es de otro."""

    def __init__(self, revision_actual: int | None):
        super().__init__("conflicto de revisión")
        self.revision_actual = revision_actual


def _ahora() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _doblar() -> dict[str, dict]:
    """El estado vigente: `{id: proceso}`. Se llama con el candado puesto."""
    procesos: dict[str, dict] = {}
    for e in registro.eventos(ARCHIVO):
        op = e.get("op")
        if op == "guardar" and isinstance(e.get("id"), str) and isinstance(e.get("datos"), dict):
            previo = procesos.get(e["id"])
            procesos[e["id"]] = {
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
            procesos.pop(e.get("id"), None)
    return procesos


def _texto(valor: object, que: str, maximo: int = MAX_TEXTO, obligatorio: bool = True) -> str:
    if not isinstance(valor, str) or (obligatorio and not valor.strip()) or len(valor) > maximo:
        raise ProcesoInvalido(f"{que} no es válido.")
    return valor


def _numero(valor: object, que: str) -> float:
    if isinstance(valor, bool) or not isinstance(valor, (int, float)) or not math.isfinite(valor) or abs(valor) > 1_000_000:
        raise ProcesoInvalido(f"{que} no es válido.")
    return valor


def _nodo(n: object) -> dict:
    if not isinstance(n, dict):
        raise ProcesoInvalido("Un nodo del proceso no es válido.")
    nodo_id = _texto(n.get("id"), "El identificador de un nodo", 100)
    if n.get("type") not in TIPOS_DE_NODO:
        raise ProcesoInvalido("Un nodo del proceso es de un tipo que no existe.")
    posicion = n.get("position")
    if not isinstance(posicion, dict):
        raise ProcesoInvalido("A un nodo del proceso le falta su posición.")
    datos = n.get("data")
    if not isinstance(datos, dict):
        raise ProcesoInvalido("A un nodo del proceso le faltan sus datos.")
    limpio_datos: dict = {}
    if n["type"] == "tipo-documental":
        limpio_datos["tipoId"] = _texto(datos.get("tipoId"), "El tipo documental de un nodo", 100)
    elif n["type"] == "constante":
        if datos.get("familia") not in FAMILIAS:
            raise ProcesoInvalido("La clase de un valor fijo no es válida.")
        limpio_datos["familia"] = datos["familia"]
        limpio_datos["valor"] = _texto(datos.get("valor", ""), "Un valor fijo", obligatorio=False)
    else:
        limpio_datos["operador"] = _texto(datos.get("operador"), "El operador de un nodo", 40)
        # Los valores de ejemplo con los que se probó la regla: texto libre, pueden estar vacíos. De una lista, varios renglones.
        limpio_datos["a"] = _texto(datos.get("a", ""), "Un valor de ejemplo", MAX_MUESTRA, obligatorio=False)
        limpio_datos["b"] = _texto(datos.get("b", ""), "Un valor de ejemplo", MAX_MUESTRA, obligatorio=False)
    return {
        "id": nodo_id,
        "type": n["type"],
        "position": {"x": _numero(posicion.get("x"), "La posición de un nodo"), "y": _numero(posicion.get("y"), "La posición de un nodo")},
        "data": limpio_datos,
    }


def _arista(a: object) -> dict:
    if not isinstance(a, dict):
        raise ProcesoInvalido("Un cable del proceso no es válido.")
    asa = {}
    for lado in ("sourceHandle", "targetHandle"):
        valor = a.get(lado)
        asa[lado] = None if valor is None else _texto(valor, "El punto de un cable", 200, obligatorio=False)
    limpia = {
        "id": _texto(a.get("id"), "El identificador de un cable", 300),
        "source": _texto(a.get("source"), "El origen de un cable", 100),
        "target": _texto(a.get("target"), "El destino de un cable", 100),
        **asa,
    }
    return limpia


def _validar(proceso_id: str, datos: object) -> dict:
    if not isinstance(proceso_id, str) or not _RE_ID.match(proceso_id):
        raise ProcesoInvalido("El identificador del proceso no es válido.")
    if not isinstance(datos, dict):
        raise ProcesoInvalido("Faltan los datos del proceso.")
    nombre = _texto(datos.get("nombre"), "El nombre del proceso", 200)
    descripcion = _texto(datos.get("descripcion", ""), "La descripción del proceso", 2000, obligatorio=False)
    nodos_crudos, aristas_crudas = datos.get("nodos"), datos.get("aristas")
    if not isinstance(nodos_crudos, list) or not isinstance(aristas_crudas, list):
        raise ProcesoInvalido("El proceso necesita sus nodos y sus cables (aunque estén vacíos).")
    if len(nodos_crudos) > MAX_NODOS or len(aristas_crudas) > MAX_ARISTAS:
        raise ProcesoInvalido("El proceso es demasiado grande para guardarse.")
    nodos = [_nodo(n) for n in nodos_crudos]
    ids_nodos = [n["id"] for n in nodos]
    if len(set(ids_nodos)) != len(ids_nodos):
        raise ProcesoInvalido("Hay dos nodos con el mismo identificador.")
    aristas = [_arista(a) for a in aristas_crudas]
    ids_aristas = [a["id"] for a in aristas]
    if len(set(ids_aristas)) != len(ids_aristas):
        raise ProcesoInvalido("Hay dos cables con el mismo identificador.")
    existentes = set(ids_nodos)
    if any(a["source"] not in existentes or a["target"] not in existentes for a in aristas):
        raise ProcesoInvalido("Un cable une un nodo que no existe.")
    tipo = datos.get("tipoExpedienteVirtualId")
    if tipo is not None and (not isinstance(tipo, str) or not tipos_expediente._RE_ID.match(tipo)):
        raise ProcesoInvalido("Ese tipo de expediente virtual no existe en esta organización.")
    limpio = {k: v for k, v in datos.items() if k not in _RESERVADAS and k not in ("id", "nodos", "aristas", "nombre", "descripcion")}
    limpio.update({"nombre": nombre, "descripcion": descripcion, "nodos": nodos, "aristas": aristas, "tipoExpedienteVirtualId": tipo})
    if len(json.dumps(limpio, ensure_ascii=False).encode("utf-8")) > TOPE_BYTES:
        raise ProcesoInvalido("El proceso es demasiado grande para guardarse.")
    return limpio


def _publico(p: dict, tipos_vigentes: set[str] | None = None) -> dict:
    """El proceso como lo ve quien consulta. Con `tipos_vigentes` (los tipos de expediente virtual que existen en su organización), una liga
    a un tipo que ya no existe se lee como `null`: lo mismo que hace la base con ON DELETE SET NULL."""
    datos = {**p["datos"], "id": p["id"]}
    tipo = datos.get("tipoExpedienteVirtualId")
    datos["tipoExpedienteVirtualId"] = tipo if tipo and (tipos_vigentes is None or tipo in tipos_vigentes) else None
    return {
        "id": p["id"],
        "revision": p["revision"],
        "datos": datos,
        "creadoPor": p["creadoPor"],
        "creadoEn": p["creadoEn"],
        "modificadoPor": p["modificadoPor"],
        "modificadoEn": p["modificadoEn"],
    }


def _tipos_vigentes(tenant: str) -> set[str]:
    return {t["id"] for t in tipos_expediente.listar(tenant)}


def _sin_id(p: dict) -> dict:
    """Lo que devuelve SQL ya trae la liga resuelta; solo falta el `id` dentro de `datos`, como en el almacén."""
    return {**p, "datos": {**p["datos"], "id": p["id"]}}


def listar(tenant: str) -> list[dict]:
    """Los procesos de UNA organización, en el orden de su creación."""
    if sql := _sql():
        return [_sin_id(p) for p in sql.listar(tenant)]
    with registro.candado:
        procesos = _doblar()
    vigentes = _tipos_vigentes(tenant)
    return [_publico(p, vigentes) for p in procesos.values() if p["tenant"] == tenant]


def obtener(tenant: str, proceso_id: str) -> dict | None:
    if sql := _sql():
        p = sql.obtener(tenant, proceso_id)
        return _sin_id(p) if p else None
    with registro.candado:
        procesos = _doblar()
    p = procesos.get(proceso_id)
    return _publico(p, _tipos_vigentes(tenant)) if p and p["tenant"] == tenant else None


def de_tipo(tenant: str, tipo_id: str) -> list[dict]:
    """Los procesos ligados a ese tipo de expediente virtual: `{id, nombre, activo}`, en el orden de su creación. Lo enseña cada expediente
    virtual de ese tipo («qué procesos le aplican»)."""
    if sql := _sql():
        return sql.de_tipo(tenant, tipo_id)
    return [
        {"id": p["id"], "nombre": p["datos"]["nombre"], "activo": p["datos"].get("activo") is not False}
        for p in listar(tenant)
        if p["datos"].get("tipoExpedienteVirtualId") == tipo_id
    ]


def guardar(tenant: str, proceso_id: str, datos: object, revision: int, por: str) -> tuple[dict, dict | None]:
    """Crea (`revision == 0`) o actualiza (`revision` = la que se editó) un proceso.

    Devuelve `(el proceso como quedó, el ANTERIOR o None)`. Levanta `ProcesoInvalido`, `NoEncontrado` (actualizar uno que no es de
    esta organización) o `Conflicto`.
    """
    limpio = _validar(proceso_id, datos)
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ProcesoInvalido("La revisión no es válida.")
    if sql := _sql():
        nuevo, actual = sql.guardar(tenant, proceso_id, limpio, revision, por)
        return _sin_id(nuevo), _sin_id(actual) if actual else None
    tipo = limpio.get("tipoExpedienteVirtualId")
    if tipo is not None and tipos_expediente.obtener(tenant, tipo) is None:
        raise ProcesoInvalido("Ese tipo de expediente virtual no existe en esta organización.")
    with registro.candado:
        procesos = _doblar()
        actual = procesos.get(proceso_id)
        if revision == 0:
            if actual is not None:
                # Repetido en esta organización: se dice cuál es la vigente. En OTRA: no se nombra de quién es.
                raise Conflicto(actual["revision"] if actual["tenant"] == tenant else None)
            nueva = 1
        else:
            if actual is None or actual["tenant"] != tenant:
                raise NoEncontrado(proceso_id)
            if actual["revision"] != revision:
                raise Conflicto(actual["revision"])
            nueva = revision + 1
        registro.agregar(
            ARCHIVO,
            {"op": "guardar", "id": proceso_id, "tenant": tenant, "revision": nueva, "datos": limpio, "por": por, "en": _ahora()},
        )
        procesos = _doblar()
        return _publico(procesos[proceso_id]), _publico(actual) if actual else None


def eliminar(tenant: str, proceso_id: str, por: str) -> dict:
    """Quita un proceso de la organización. Devuelve el que se quitó. `NoEncontrado` si no es de ella."""
    if sql := _sql():
        return _sin_id(sql.eliminar(tenant, proceso_id))
    with registro.candado:
        procesos = _doblar()
        actual = procesos.get(proceso_id)
        if actual is None or actual["tenant"] != tenant:
            raise NoEncontrado(proceso_id)
        registro.agregar(ARCHIVO, {"op": "eliminar", "id": proceso_id, "tenant": tenant, "por": por, "en": _ahora()})
        return _publico(actual)
