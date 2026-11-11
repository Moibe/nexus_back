"""Registro de los PROCESOS de cada organización (el motor de reglas) — registro PROVISIONAL.

## Qué es un proceso

Un proceso es un conjunto de reglas y corre sobre un EXPEDIENTE (un conjunto de documentos). Se arma en un lienzo: los tipos
documentales de la organización son sus entradas (nodos de tipo `tipo-documental`: lo que se dibuja ahí es la declaración de qué documentos
necesita el proceso del expediente), las reglas son operadores (nodos de tipo
`operador`: igual, distinto, contiene, parecido (con su `minimo`, en %), mayor, mayor_igual, menor, menor_igual, entre (el único con TRES entradas:
valor, desde y hasta; su tercera muestra va en `c`); antiguedad (una fecha contra hoy, con su `maximo` y su `unidad`: dias, meses o anios); cantidad, suma, alguno, todos y unicos, que trabajan con LISTAS; existe, que
pregunta si el expediente trae un documento; y, o, no, implica),
un `constante` es un valor fijo que se escribe en el lienzo (por ejemplo el «2» de «más de 2 socios») y los cables llevan un campo,
el documento entero (el punto `@documento` de un tipo, solo para `existe`), un valor fijo o el resultado de una regla a la entrada
de un operador. Para «existe», la muestra `a` es el ejemplo elegido (`si` o `no`).

Desde el 2026-11-03 el lienzo tiene dos nodos FINALES, los que dicen qué entrega el proceso al terminar (Moibe: un valor final «es aquel que ya
no se compara más y que pertenecerá a un listado de valores finales de la corrida»): `valor-final` (un dato que entra a esa lista con su
`nombre`; recibe un campo, una lista, un valor fijo, un número calculado o el pasa / no pasa de una regla) y `resultado-final` (la palabra con
la que termina el proceso: recibe una regla y guarda tres textos libres, `si`, `no` y `sin`, para cuando la regla pasa, no pasa o no se puede
evaluar por falta de datos; «Aprobado», «Certificado»…). Un proceso tiene a lo más UN `resultado-final`. Ninguno de los dos entrega nada a otro
nodo: solo reciben cable.

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

## Sobre qué expedientes corre: sobre cualquiera (Moibe, 2026-11-05)

Entre el 2026-10-29 y el 2026-11-05 un proceso se LIGABA a un tipo de expediente virtual (`datos.tipoExpedienteVirtualId`) y corría sobre los
expedientes virtuales de ese tipo. Moibe quitó los expedientes virtuales («ahora podremos meter el expediente completo a la ejecución de un
proceso»): un proceso ya no se limita a nada y corre sobre CUALQUIER expediente de su organización, tomando
del expediente completo los documentos de los tipos documentales que dibuja en su lienzo (`servicios/ejecuciones.py`).

Por eso `datos.tipoExpedienteVirtualId` ya no existe: si un cliente viejo todavía lo manda, el servidor lo IGNORA (no lo guarda ni falla) y las
lecturas jamás lo traen, ni de los procesos que lo traían guardado en el almacén. La columna `processes.virtualCaseTypePublicId` de la `0005` la
borra la migración `0012`; este módulo ya no la lee ni la escribe.

## Cómo se guarda

Desde el 2026-10-29, donde hay base, en `nexus.processes` (migración `0005`, `servicios/procesos_sql.py`).
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
from servicios import registro


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
TIPOS_DE_NODO = ("tipo-documental", "operador", "constante", "valor-final", "resultado-final")

#: De qué clase es el valor de un nodo `constante`: las cuatro familias que el lienzo sabe comparar.
FAMILIAS = ("texto", "numero", "fecha", "booleano")

#: Las tres palabras con las que puede terminar un proceso (nodo `resultado-final`): cuando la condición que le llega se cumple (`si`), no se
#: cumple (`no`) o no se puede evaluar por falta de datos (`sin`). Son texto libre de la organización («Aprobado», «Certificado»…).
CASOS_DEL_RESULTADO = ("si", "no", "sin")

#: Topes. Un proceso normal tiene unas decenas de nodos; esto es para que un cliente descontrolado no llene el disco.
MAX_NODOS = 300
#: El nombre con el que un `valor-final` entra a la lista de valores finales de una corrida, y cada una de las tres palabras del resultado.
MAX_NOMBRE_FINAL = 80
MAX_TEXTO_RESULTADO = 60
MAX_ARISTAS = 600
MAX_TEXTO = 500
#: El valor de ejemplo de una LISTA son varios renglones (uno por elemento), así que cabe mucho más que un valor suelto.
MAX_MUESTRA = 5000
TOPE_BYTES = 1_000_000
#: El parecido mínimo (en %) de «Parecido a» cuando el nodo no trae otro. El mismo valor por defecto que pone el lienzo.
MINIMO_PARECIDO = 90
#: Los operadores con una TERCERA entrada, cuya muestra va en `c` (2026-11-10: «Entre» = valor, desde y hasta). Solo ellos la guardan; los demás
#: siguen guardándose con `a` y `b`, como el lienzo (`usaTerceraEntrada` en reglas.ts del front).
OPERADORES_CON_TERCERA_ENTRADA = frozenset({"entre"})
#: «Antigüedad contra hoy» (2026-11-10): pasa si la fecha tiene como máximo `maximo` días, meses o años (`unidad`) contados hasta hoy. Sin ellos, 3
#: meses, como el lienzo (`MAXIMO_ANTIGUEDAD` y `UNIDAD_ANTIGUEDAD` en reglas.ts). La fecha de «hoy» con que se prueba en el lienzo NO se guarda: una
#: ejecución usa la del día en que corre.
MAXIMO_ANTIGUEDAD = 3
TOPE_MAXIMO_ANTIGUEDAD = 9999
UNIDAD_ANTIGUEDAD = "meses"
UNIDADES_DE_TIEMPO = ("dias", "meses", "anios")

#: Lo que NO se acepta dentro de `datos`: lo pone el servidor.
_RESERVADAS = frozenset({"revision", "creadoPor", "creadoEn", "modificadoPor", "modificadoEn", "tenant", "meta"})

#: Lo que un cliente viejo todavía puede mandar dentro de `datos` y el servidor DESCARTA sin fallar: ni se guarda ni se devuelve. Hoy, la liga con un tipo
#: de expediente virtual (Moibe, 2026-11-05: se quitaron los expedientes virtuales; un proceso ya no se limita a ningún tipo).
_DESCARTADAS = frozenset({"tipoExpedienteVirtualId"})


class ProcesoInvalido(ValueError):
    """Lo que llegó no es un proceso guardable."""


class NoEncontrado(LookupError):
    """No hay tal proceso EN ESA organización (o no existe: no se distingue)."""


class NombreRepetido(Exception):
    """Ya hay OTRO proceso con ese nombre en la organización (sin distinguir mayúsculas ni espacios de más)."""


def clave_de_nombre(nombre: str) -> str:
    """Con qué se compara un nombre para saber si choca con otro: sin espacios de más y sin distinguir mayúsculas."""
    return " ".join(str(nombre).split()).casefold()


def exigir_nombre_libre(nombre: str, otros: list[str]) -> None:
    """`NombreRepetido` si alguno de los nombres de `otros` (los demás procesos de la organización) es ése."""
    clave = clave_de_nombre(nombre)
    if any(clave_de_nombre(o) == clave for o in otros):
        raise NombreRepetido()


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
        # La versión del tipo con la que se armó (o se confirmó por última vez) el proceso. Opcional: los procesos de antes no la traen.
        version = datos.get("version")
        if version is not None:
            if isinstance(version, bool) or not isinstance(version, int) or not 0 <= version <= 100_000:
                raise ProcesoInvalido("La versión de un tipo documental no es válida.")
            limpio_datos["version"] = version
    elif n["type"] == "constante":
        if datos.get("familia") not in FAMILIAS:
            raise ProcesoInvalido("La clase de un valor fijo no es válida.")
        limpio_datos["familia"] = datos["familia"]
        limpio_datos["valor"] = _texto(datos.get("valor", ""), "Un valor fijo", obligatorio=False)
    elif n["type"] == "valor-final":
        # Lo que el proceso entrega al terminar, con el nombre que lleva en la lista de la corrida (vacío = el que el lienzo le da por su número) y,
        # como un operador, el valor de EJEMPLO con que se probó mientras el lienzo no corre sobre un expediente de verdad.
        limpio_datos["nombre"] = _texto(datos.get("nombre", ""), "El nombre de un valor final", MAX_NOMBRE_FINAL, obligatorio=False)
        limpio_datos["a"] = _texto(datos.get("a", ""), "Un valor de ejemplo", MAX_MUESTRA, obligatorio=False)
    elif n["type"] == "resultado-final":
        for caso in CASOS_DEL_RESULTADO:
            limpio_datos[caso] = _texto(datos.get(caso), "Una palabra del resultado", MAX_TEXTO_RESULTADO)
    else:
        limpio_datos["operador"] = _texto(datos.get("operador"), "El operador de un nodo", 40)
        # Los valores de ejemplo con los que se probó la regla: texto libre, pueden estar vacíos. De una lista, varios renglones.
        limpio_datos["a"] = _texto(datos.get("a", ""), "Un valor de ejemplo", MAX_MUESTRA, obligatorio=False)
        limpio_datos["b"] = _texto(datos.get("b", ""), "Un valor de ejemplo", MAX_MUESTRA, obligatorio=False)
        if limpio_datos["operador"] in OPERADORES_CON_TERCERA_ENTRADA:
            limpio_datos["c"] = _texto(datos.get("c", ""), "Un valor de ejemplo", MAX_MUESTRA, obligatorio=False)
        if limpio_datos["operador"] == "parecido":
            # «Parecido a» (2026-11-10): el porcentaje mínimo de parecido para que pase. Sin él, el de siempre.
            minimo = datos.get("minimo", MINIMO_PARECIDO)
            if isinstance(minimo, bool) or not isinstance(minimo, int) or not 1 <= minimo <= 100:
                raise ProcesoInvalido("El parecido mínimo de «Parecido a» debe ser un número entero de 1 a 100.")
            limpio_datos["minimo"] = minimo
        if limpio_datos["operador"] == "antiguedad":
            maximo = datos.get("maximo", MAXIMO_ANTIGUEDAD)
            if isinstance(maximo, bool) or not isinstance(maximo, int) or not 1 <= maximo <= TOPE_MAXIMO_ANTIGUEDAD:
                raise ProcesoInvalido(f"El máximo de «Antigüedad contra hoy» debe ser un número entero de 1 a {TOPE_MAXIMO_ANTIGUEDAD}.")
            unidad = datos.get("unidad", UNIDAD_ANTIGUEDAD)
            if unidad not in UNIDADES_DE_TIEMPO:
                raise ProcesoInvalido("La unidad de «Antigüedad contra hoy» debe ser días, meses o años.")
            limpio_datos["maximo"] = maximo
            limpio_datos["unidad"] = unidad
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
    if sum(1 for n in nodos if n["type"] == "resultado-final") > 1:
        # El resultado es UNA palabra por corrida: dos nodos no sabrían cuál decide.
        raise ProcesoInvalido("Un proceso solo puede tener un resultado.")
    aristas = [_arista(a) for a in aristas_crudas]
    ids_aristas = [a["id"] for a in aristas]
    if len(set(ids_aristas)) != len(ids_aristas):
        raise ProcesoInvalido("Hay dos cables con el mismo identificador.")
    existentes = set(ids_nodos)
    if any(a["source"] not in existentes or a["target"] not in existentes for a in aristas):
        raise ProcesoInvalido("Un cable une un nodo que no existe.")
    limpio = {k: v for k, v in datos.items() if k not in _RESERVADAS and k not in _DESCARTADAS and k not in ("id", "nodos", "aristas", "nombre", "descripcion")}
    limpio.update({"nombre": nombre, "descripcion": descripcion, "nodos": nodos, "aristas": aristas})
    if len(json.dumps(limpio, ensure_ascii=False).encode("utf-8")) > TOPE_BYTES:
        raise ProcesoInvalido("El proceso es demasiado grande para guardarse.")
    return limpio


def _publico(p: dict) -> dict:
    """El proceso como lo ve quien consulta. Sin lo que se descarta (`_DESCARTADAS`): un proceso guardado en el almacén ANTES del 2026-11-05 trae
    `tipoExpedienteVirtualId` dentro de sus datos y no se devuelve."""
    datos = {**{k: v for k, v in p["datos"].items() if k not in _DESCARTADAS}, "id": p["id"]}
    return {
        "id": p["id"],
        "revision": p["revision"],
        "datos": datos,
        "creadoPor": p["creadoPor"],
        "creadoEn": p["creadoEn"],
        "modificadoPor": p["modificadoPor"],
        "modificadoEn": p["modificadoEn"],
    }


def _sin_id(p: dict) -> dict:
    """Lo que devuelve SQL solo necesita el `id` dentro de `datos`, como en el almacén. Y sin lo que se descarta (`_DESCARTADAS`): una fila con
    `tipoExpedienteVirtualId` dentro de su JSON (solo posible por SQL directo: desde la 0005 la liga iba en su propia columna, y la 0012 la borró) no se
    devuelve, igual que `_publico` lo hace con el almacén; el contrato es que ese campo nunca sale, venga la fila de donde venga."""
    datos = {k: v for k, v in p["datos"].items() if k not in _DESCARTADAS}
    return {**p, "datos": {**datos, "id": p["id"]}}


def listar(tenant: str) -> list[dict]:
    """Los procesos de UNA organización, en el orden de su creación."""
    if sql := _sql():
        return [_sin_id(p) for p in sql.listar(tenant)]
    with registro.candado:
        procesos = _doblar()
    return [_publico(p) for p in procesos.values() if p["tenant"] == tenant]


def obtener(tenant: str, proceso_id: str) -> dict | None:
    if sql := _sql():
        p = sql.obtener(tenant, proceso_id)
        return _sin_id(p) if p else None
    with registro.candado:
        procesos = _doblar()
    p = procesos.get(proceso_id)
    return _publico(p) if p and p["tenant"] == tenant else None


def resultado_conectado(datos: dict) -> bool:
    """¿El proceso tiene su nodo de Resultado con una regla conectada? Es lo que lo hace EJECUTABLE: desde el 2026-11-03 el Resultado es obligatorio
    para guardar desde el lienzo (Moibe), pero el back no lo exige (los de antes no lo traen, y activar o desactivar guarda el proceso entero), y
    uno sin regla en su Resultado no tiene con qué contestar. Es solo mirar el dibujo: el back sigue sin interpretar las reglas."""
    nodos = datos.get("nodos") or []
    tipos = {n.get("id"): n.get("type") for n in nodos if isinstance(n, dict)}
    resultados = {i for i, t in tipos.items() if t == "resultado-final"}
    return any(
        isinstance(a, dict) and a.get("target") in resultados and a.get("targetHandle") == "a" and tipos.get(a.get("source")) == "operador"
        for a in datos.get("aristas") or []
    )


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
        # El nombre es único en la organización. Solo se revisa si es nuevo o si cambió: guardar sin tocar el nombre (activar o desactivar desde la
        # lista, por ejemplo) no puede fallar por un duplicado que ya existía antes de esta regla.
        if actual is None or clave_de_nombre(actual["datos"]["nombre"]) != clave_de_nombre(limpio["nombre"]):
            exigir_nombre_libre(
                limpio["nombre"], [p["datos"]["nombre"] for i, p in procesos.items() if p["tenant"] == tenant and i != proceso_id]
            )
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
