"""Ejecuciones: correr un proceso del motor de reglas sobre un EXPEDIENTE, y su historial (migración `0009`; la `0011` quitó el expediente virtual).

## Sobre qué se corre (Moibe, 2026-11-05)

Una ejecución junta UN expediente con UN proceso. Hasta el 2026-11-05 se corría sobre un EXPEDIENTE VIRTUAL (un subconjunto de los documentos de un
expediente, instancia de un tipo al que el proceso estaba ligado). Moibe quitó los expedientes virtuales («ahora podremos meter el expediente completo a
la ejecución de un proceso»): el proceso corre sobre cualquier expediente de su organización (ni el grupo del expediente lo limita) y toma del expediente
COMPLETO los documentos de los tipos documentales que dibuja en su lienzo; esos nodos son la declaración de las entradas del proceso. Ya no hay «este
proceso no corre aquí»: el motivo de rechazo `no_corre_aqui` desapareció.

## Quién hace qué (2026-11-03)

El back NO interpreta las reglas, y esto no lo cambia. Qué significa «igual» (texto sin mayúsculas, números con símbolo de moneda, fechas escritas de
tres maneras…), cómo se combinan las reglas y qué palabra sale del Resultado vive en UN solo lugar, `reglas.ts` del front, que es lo que el lienzo
ya usa para probar con valores de ejemplo. Una ejecución corre ESA misma lógica en el servidor del front (el BFF), con los datos reales; así no hay
dos implementaciones que puedan discrepar. Si algún día se necesita correr procesos desde aquí (una API, algo automático), se porta con esa suite como
oráculo. Este módulo hace las tres cosas que sí son del back:

1. `preparar`: arma, en UNA lectura, todo lo que una ejecución necesita: el proceso, los tipos documentales que usa y, por cada uno, el documento del
   expediente que lo cubre con sus valores VIGENTES (los de su última extracción, con las correcciones a mano), o la lista de duplicados.
2. `procesos_ofrecidos`: los procesos que la pantalla de «Ejecuciones» ofrece para elegir (con cuáles se pueden ejecutar de verdad).
3. `registrar`, `listar`, `opciones_de_filtro` y `obtener`: el historial. Es una FOTO y es inmutable: no hay forma de editarlo ni de borrarlo.

## Filtros del historial (Moibe, 2026-11-03: «agrega dichos filtros»)

`listar` filtra por fecha, proceso, estado y persona (y, desde antes, por expediente). Los filtros se combinan con Y; dentro de uno de lista (proceso,
persona, caso) basta con que la ejecución cumpla UNO. `desde` incluye el instante y `hasta` NO lo incluye: quien arma el rango (la pantalla) piensa en
días de SU hora local, del inicio del primer día al inicio del día siguiente al último, y así no importa a cuántas horas de UTC esté. El «estado» es el
caso con el que terminó (`si`, `no` o `sin`): las palabras del Resultado las escribe cada organización, el caso es lo que no cambia. Qué se puede elegir
lo dice `opciones_de_filtro`, y sale de lo que ESTÁ en el historial y no del catálogo vivo: un proceso borrado, o alguien que ya no está, siguen en la foto
y se pueden filtrar. El filtro por expediente virtual se quitó con los virtuales (2026-11-05).

## Las ejecuciones de antes del 2026-11-05

Las que se corrieron sobre un expediente virtual CONSERVAN su virtual (la `0011` solo dejó nulas las columnas, no reescribió nada): se leen con
`virtual: {id, nombre, tipoNombre}`. Las nuevas traen `virtual: null`. El historial es una foto, así que el virtual de una vieja se lee aunque ya no exista
(sus tablas las borra la `0012`).

## Duplicados (Moibe, 2026-11-03; se mantiene sobre el expediente entero, 2026-11-05)

Si el expediente trae DOS O MÁS documentos de un tipo que el proceso usa, no se sabe cuál es «el INE»: la regla NO corre, se avisa que hay un
duplicado y los documentos quedan marcados en el expediente (`expedientes.duplicados`). `preparar` los entrega en `duplicados` y no da valores de ese
tipo; quien ejecuta no debe seguir. Un tipo que el proceso NO usa puede estar duplicado sin que estorbe.

## Qué se exige para ejecutar

El proceso tiene que ser de la organización, estar activo y traer su Resultado con una regla conectada (`procesos.resultado_conectado`). Cada falta se
dice con su propio código (`NoSePuedeEjecutar.codigo`: `proceso_inactivo`, `sin_resultado`, `resultado_sin_regla`) para que la pantalla explique qué hacer.

## Los nombres de los campos

Lo extraído se guarda con los nombres de Google (`ano_de_registro`, `socios[2].nombre`); el lienzo conoce los de la biblioteca («Año de registro»,
`socios.nombre`). `esquema.nombres_en_google` los une con la misma regla con que se armó el esquema. `preparar` entrega los valores por el nombre de la
biblioteca, que es el que usa el motor.

Solo SQL (decisión del 2026-10-28): sin base, escribir y leer levantan `SinBase`.
"""

import json
import unicodedata
import uuid
from datetime import datetime, timezone
from typing import Any

from config import SQLSERVER_HOST
from servicios import datos_extraidos, esquema, expedientes, procesos, tipos_documentales
from servicios.tipos_documentales_comun import momento

CASOS = ("si", "no", "sin")
FORMAS_FINAL = ("valor", "lista", "veredicto")
FAMILIAS = ("texto", "numero", "fecha", "booleano")
ESTADOS_FINAL = ("listo", "sin_valores", "sin_conexion")

MAX_FINALES = 300
MAX_ELEMENTOS = 1000
MAX_VALOR = 8000
MAX_NOMBRE = 200
MAX_ORIGEN = 400
MAX_TEXTO_RESULTADO = 60
#: Lo que cabe en la foto de cómo salió cada regla (`detalle`): una corrida normal es de unos cuantos KB.
MAX_DETALLE_BYTES = 400_000
#: Cuántas ejecuciones entrega una lectura del historial, y el tope de lo que se puede pedir.
LIMITE_POR_OMISION = 50
LIMITE_MAXIMO = 200
#: Cuántos valores acepta cada filtro de lista (proceso, persona, caso): un tope contra peticiones absurdas (SQL Server admite 2100 parámetros por consulta).
MAX_FILTROS = 50


class SinBase(RuntimeError):
    """Las ejecuciones solo viven en SQL Server, y esta instancia no tiene base."""


class NoEncontrado(LookupError):
    """No hay tal expediente, proceso o ejecución EN ESA organización. `que` dice cuál (`expediente`, `proceso`, `ejecucion`)."""

    def __init__(self, que: str):
        super().__init__(que)
        self.que = que


class NoSePuedeEjecutar(ValueError):
    """El proceso no se puede correr. `codigo` dice por qué y el texto se le puede mostrar a la persona."""

    def __init__(self, codigo: str, mensaje: str):
        super().__init__(mensaje)
        self.codigo = codigo


class EjecucionInvalida(ValueError):
    """Lo que llegó para guardar no es una ejecución guardable."""


class Conflicto(Exception):
    """El proceso cambió mientras se ejecutaba: lo que se corrió ya no es lo que hay. `revision_actual` es la vigente."""

    def __init__(self, revision_actual: int):
        super().__init__("conflicto de revisión")
        self.revision_actual = revision_actual


def _tablas():
    if not SQLSERVER_HOST:
        raise SinBase("Las ejecuciones necesitan la base de datos, y esta instancia no tiene.")
    from db import tablas

    return tablas


# ── Preparar ─────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _nodos(datos: dict, tipo: str) -> list[dict]:
    return [n for n in datos.get("nodos") or [] if isinstance(n, dict) and n.get("type") == tipo]


def _valores_de(campos: list[dict], hechos: list[dict]) -> dict[str, dict]:
    """Los valores VIGENTES de un documento, por el punto con que el motor nombra cada campo de su tipo documental.

    `campos` son los campos del tipo (como están en la biblioteca) y `hechos` los `campos` de `datos_extraidos.de_documento` (`ruta`, `registro`,
    `valor`, `confianza`, `pagina`). Cada punto trae `forma` (`valor`: uno solo; `lista`: varios, en el orden del documento; `filas`: las filas de
    un grupo que se repite, de las que solo importa cuántas son), sus `valores` como texto, la `confianza` más baja y la `pagina` de la primera.
    Un campo que el documento no trae queda con `valores` vacío: la regla que lo use dirá «sin valores»."""
    nombres = esquema.nombres_en_google(campos)
    por_ruta = {h["ruta"]: h for h in hechos}

    def resumir(forma: str, usados: list[dict], valores: list[str]) -> dict:
        confianzas = [h["confianza"] for h in usados if h.get("confianza") is not None]
        paginas = [h["pagina"] for h in usados if h.get("pagina") is not None]
        return {"forma": forma, "valores": valores, "confianza": min(confianzas) if confianzas else None, "pagina": paginas[0] if paginas else None}

    def con_valor(h: dict | None) -> bool:
        return h is not None and h.get("valor") is not None

    salida: dict[str, dict] = {}
    for campo in campos:
        punto = campo.get("nombre", "")
        if punto not in nombres:
            continue
        grupo = nombres[punto][1]
        multiple = campo.get("cardinalidad") == "multiple"
        if campo.get("tipoDato") == "grupo":
            subs = [(f"{punto}.{s.get('nombre', '')}", s) for s in campo.get("subcampos", [])]
            subs = [(clave, s) for clave, s in subs if clave in nombres]
            if multiple:
                # Una fila existe si Document AI devolvió ALGO de ella (aunque algún subcampo haya quedado ausente).
                filas = sorted({h["registro"] for h in hechos if h.get("registro") and h["ruta"].startswith(f"{grupo}[")})
                salida[punto] = resumir("filas", [], [str(i) for i in filas])
                for clave, _ in subs:
                    hijo = nombres[clave][1]
                    usados = [por_ruta[f"{grupo}[{i}].{hijo}"] for i in filas if con_valor(por_ruta.get(f"{grupo}[{i}].{hijo}"))]
                    salida[clave] = resumir("lista", usados, [h["valor"] for h in usados])
            else:
                for clave, _ in subs:
                    h = por_ruta.get(f"{grupo}.{nombres[clave][1]}")
                    salida[clave] = resumir("valor", [h] if con_valor(h) else [], [h["valor"]] if con_valor(h) else [])
        elif multiple:
            usados = sorted(
                (h for r, h in por_ruta.items() if con_valor(h) and r.startswith(f"{grupo}[") and r.endswith("]") and r[len(grupo) + 1 : -1].isdigit()),
                key=lambda h: h.get("registro") or 0,
            )
            salida[punto] = resumir("lista", usados, [h["valor"] for h in usados])
        else:
            h = por_ruta.get(grupo)
            salida[punto] = resumir("valor", [h] if con_valor(h) else [], [h["valor"]] if con_valor(h) else [])
    return salida


def preparar(tenant: str, expediente_id: object, proceso_id: object) -> dict:
    """Todo lo que una ejecución necesita (ver el docstring del módulo): `expediente`, `proceso` (con su dibujo completo y su revisión), `tipos` (los tipos
    documentales que usa, con sus campos), `documentos` (por cada tipo: los documentos del EXPEDIENTE que lo cubren y, si es uno solo, sus valores) y
    `duplicados` (los tipos que usa el proceso y que el expediente trae repetidos).

    Levanta `NoEncontrado` (el expediente o el proceso no son de la organización) o `NoSePuedeEjecutar` (inactivo, sin Resultado o con el Resultado sin
    regla). Con duplicados NO levanta nada: los entrega, y quien ejecuta decide no seguir (la pantalla los enseña)."""
    _tablas()
    expediente = expedientes.obtener(tenant, expediente_id)
    if expediente is None:
        raise NoEncontrado("expediente")
    proceso = procesos.obtener(tenant, proceso_id) if isinstance(proceso_id, str) else None
    if proceso is None:
        raise NoEncontrado("proceso")
    datos = proceso["datos"]
    if datos.get("activo") is False:
        raise NoSePuedeEjecutar("proceso_inactivo", "Ese proceso está desactivado: actívalo desde la lista de procesos para ejecutarlo.")
    if not _nodos(datos, "resultado-final"):
        raise NoSePuedeEjecutar("sin_resultado", "Ese proceso no tiene su nodo de Resultado: ábrelo en el motor de reglas y agrégalo para poder ejecutarlo.")
    if not procesos.resultado_conectado(datos):
        raise NoSePuedeEjecutar("resultado_sin_regla", "El Resultado de ese proceso no tiene ninguna regla conectada: ábrelo en el motor de reglas y conéctalo.")

    # Los documentos de TODO el expediente con su tipo documental vigente (el de su última extracción o, si no, el de su última clasificación).
    documentos = expediente["documentos"]
    tipos_de_doc = datos_extraidos.tipos_de(tenant, [d["id"] for d in documentos])
    con_tipo = [{**d, "tipoDocumental": tipos_de_doc.get(d["id"])} for d in documentos]

    tipos_ids: list[str] = []
    for n in _nodos(datos, "tipo-documental"):
        tipo_id = (n.get("data") or {}).get("tipoId")
        if isinstance(tipo_id, str) and tipo_id not in tipos_ids:
            tipos_ids.append(tipo_id)

    tipos: list[dict] = []
    por_tipo: list[dict] = []
    duplicados: list[dict] = []
    for tipo_id in tipos_ids:
        tipo = tipos_documentales.obtener(tenant, tipo_id)
        if tipo is None:
            continue  # un tipo que ya no existe: sus campos quedan «sin conexión» en el dibujo
        nombre = tipo["datos"].get("nombre") or tipo_id
        campos = tipo["datos"].get("campos") or []
        tipos.append({"id": tipo_id, "nombre": nombre, "campos": campos})
        encontrados = [
            {"id": d["id"], "nombre": d.get("nombreOriginal"), "origenTipo": d["tipoDocumental"].get("origen")}
            for d in con_tipo
            if (d["tipoDocumental"] or {}).get("id") == tipo_id
        ]
        valores = None
        if len(encontrados) == 1:
            valores = _valores_de(campos, datos_extraidos.de_documento(tenant, encontrados[0]["id"])["campos"])
        elif len(encontrados) >= 2:
            duplicados.append({"tipoId": tipo_id, "tipoNombre": nombre, "documentos": [{"id": e["id"], "nombre": e["nombre"]} for e in encontrados]})
        por_tipo.append({"tipoId": tipo_id, "tipoNombre": nombre, "encontrados": encontrados, "valores": valores})

    return {
        "expediente": {"id": expediente["id"], "nombre": expediente["nombre"]},
        "proceso": {"id": proceso["id"], "revision": proceso["revision"], "nombre": datos["nombre"], "datos": datos},
        "tipos": tipos,
        "documentos": por_tipo,
        "duplicados": duplicados,
    }


def procesos_ofrecidos(tenant: str) -> list[dict]:
    """Los procesos de la organización que la pantalla de «Ejecuciones» ofrece para elegir: `{id, nombre, activo, ejecutable}`, TODOS (activos e inactivos: la
    pantalla filtra `activo`), en el orden de su creación. `ejecutable` dice si el proceso se puede correr (tiene su Resultado con una regla conectada,
    `procesos.resultado_conectado`); uno que no lo es se ofrece igual y la pantalla explica por qué no se ejecuta. Antes eran «los procesos que le aplican»
    a un expediente virtual (2026-11-03); ahora un proceso corre sobre cualquier expediente (Moibe, 2026-11-05). Sin base: vacío."""
    if not SQLSERVER_HOST:
        return []
    return [
        {
            "id": p["id"],
            "nombre": p["datos"]["nombre"],
            "activo": p["datos"].get("activo") is not False,
            "ejecutable": procesos.resultado_conectado(p["datos"]),
        }
        for p in procesos.listar(tenant)
    ]


# ── Registrar ────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _texto(valor: Any, que: str, maximo: int, obligatorio: bool = True) -> str | None:
    if valor is None and not obligatorio:
        return None
    if not isinstance(valor, str) or (obligatorio and not valor.strip()) or len(valor) > maximo:
        raise EjecucionInvalida(f"{que} no es válido.")
    return valor


def _final(f: Any, posicion: int, ids_validos: set[str], vistos: set[str]) -> dict:
    """Un valor final de la corrida, validado y listo para su renglón."""
    if not isinstance(f, dict):
        raise EjecucionInvalida("Un valor final no es válido.")
    nodo = _texto(f.get("nodoId"), "El nodo de un valor final", 100)
    if nodo not in ids_validos or nodo in vistos:
        raise EjecucionInvalida("Un valor final no es de este proceso, o viene repetido.")
    vistos.add(nodo)
    nombre = _texto(f.get("nombre"), "El nombre de un valor final", MAX_NOMBRE)
    forma, familia, estado = f.get("forma"), f.get("familia"), f.get("estado")
    if forma not in FORMAS_FINAL or estado not in ESTADOS_FINAL or (familia is not None and familia not in FAMILIAS):
        raise EjecucionInvalida("Un valor final no es válido.")
    valor = _texto(f.get("valor"), "El valor de un valor final", MAX_VALOR, obligatorio=False)
    lista = f.get("lista")
    if lista is not None:
        if forma != "lista" or not isinstance(lista, list) or len(lista) > MAX_ELEMENTOS or any(not isinstance(e, str) or len(e) > MAX_VALOR for e in lista):
            raise EjecucionInvalida("La lista de un valor final no es válida.")
    if forma == "lista" and estado == "listo" and lista is None:
        raise EjecucionInvalida("Una lista final con valor trae sus elementos.")
    if (estado == "listo") != (valor is not None):
        raise EjecucionInvalida("Un valor final tiene valor si y solo si está listo.")
    if estado != "listo" and lista is not None:
        raise EjecucionInvalida("Un valor final sin valor no trae lista.")
    return {
        "position": posicion,
        "nodeId": nodo,
        "name": nombre.strip(),
        "kind": forma,
        "family": familia,
        "status": estado,
        "valueText": valor,
        "valueJson": json.dumps(lista, ensure_ascii=False) if lista is not None else None,
        "origin": _texto(f.get("origen"), "El origen de un valor final", MAX_ORIGEN, obligatorio=False),
    }


def registrar(tenant: str, por: str, entrada: Any) -> dict:
    """Guarda UNA ejecución en el historial, todo o nada, y la devuelve como se ve al abrirla.

    `entrada`: `{expedienteId, procesoId, procesoRevision, resultado: {caso, texto}, valoresFinales: [...], detalle: {...}}`, lo que sacó la lógica del
    lienzo corrida con datos reales. Los NOMBRES (del expediente y del proceso) y el dibujo del proceso los toma el servidor de la base, no de la
    petición: lo que se guarda es lo que había, no lo que alguien dice que había. Una ejecución nueva NO guarda virtual: las tres columnas de virtual
    (`processRuns.virtualCase*`) quedan nulas, y una petición que todavía mande `virtualId` no lo usa.

    Se niega (400) lo que no cuadra con el proceso: una palabra del Resultado que no es la que el proceso tiene para ese caso, o un valor final que no
    es de él. Y si el proceso cambió desde que se preparó la ejecución (`procesoRevision` ya no es la vigente), `Conflicto`: lo que se corrió ya no es
    lo que hay y hay que volver a ejecutar."""
    if not isinstance(entrada, dict):
        raise EjecucionInvalida("La ejecución no es válida.")
    tablas = _tablas()
    expediente = expedientes.obtener(tenant, entrada.get("expedienteId"))
    if expediente is None:
        raise NoEncontrado("expediente")
    proceso_id = entrada.get("procesoId")
    proceso = procesos.obtener(tenant, proceso_id) if isinstance(proceso_id, str) else None
    if proceso is None:
        raise NoEncontrado("proceso")
    revision = entrada.get("procesoRevision")
    if isinstance(revision, bool) or not isinstance(revision, int):
        raise EjecucionInvalida("La revisión del proceso no es válida.")
    if revision != proceso["revision"]:
        raise Conflicto(proceso["revision"])
    datos = proceso["datos"]

    resultado = entrada.get("resultado")
    if not isinstance(resultado, dict) or resultado.get("caso") not in CASOS:
        raise EjecucionInvalida("El resultado no es válido.")
    caso = resultado["caso"]
    texto = _texto(resultado.get("texto"), "La palabra del resultado", MAX_TEXTO_RESULTADO).strip()
    nodos_resultado = _nodos(datos, "resultado-final")
    if len(nodos_resultado) != 1 or (nodos_resultado[0].get("data") or {}).get(caso) != texto:
        raise EjecucionInvalida("La palabra del resultado no es la que el proceso tiene para ese caso.")

    ids_finales = {n["id"] for n in _nodos(datos, "valor-final")}
    crudos = entrada.get("valoresFinales")
    if not isinstance(crudos, list) or len(crudos) > MAX_FINALES:
        raise EjecucionInvalida("Los valores finales no son válidos.")
    vistos: set[str] = set()
    finales = [_final(f, i, ids_finales, vistos) for i, f in enumerate(crudos, start=1)]

    detalle = entrada.get("detalle")
    if not isinstance(detalle, dict):
        raise EjecucionInvalida("El detalle de la ejecución no es válido.")
    detalle_json = json.dumps(detalle, ensure_ascii=False)
    if len(detalle_json.encode("utf-8")) > MAX_DETALLE_BYTES:
        raise EjecucionInvalida("El detalle de la ejecución es demasiado grande.")
    definicion_json = json.dumps(datos, ensure_ascii=False)

    publico = f"ejecucion-{uuid.uuid4().hex}"
    with tablas.transaccion() as cur:
        # Las columnas de virtual (`virtualCaseId`, `virtualCaseName`, `virtualCaseTypeName`) NO se nombran: quedan en NULL, que es lo que la `0011` permite
        # y lo que exige `CK_processRuns_virtual` para una corrida sin virtual.
        cur.execute(
            "INSERT INTO nexus.processRuns (publicId, tenant, caseId, caseName, processId, processName, "
            "processRevision, outcomeCase, outcomeText, status, detailJson, definitionJson, executedBy) OUTPUT INSERTED.processRunId "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            publico, tenant, expediente["id"], expediente["nombre"],
            proceso["id"], datos["nombre"], proceso["revision"], caso, texto, "incompleta" if caso == "sin" else "completa", detalle_json, definicion_json, por,
        )
        corrida = cur.fetchone()[0]
        for f in finales:
            cur.execute(
                "INSERT INTO nexus.processRunFinalValues (processRunId, position, nodeId, name, kind, family, status, valueText, valueJson, origin) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                corrida, f["position"], f["nodeId"], f["name"], f["kind"], f["family"], f["status"], f["valueText"], f["valueJson"], f["origin"],
            )
    return obtener(tenant, publico) or {}


# ── Leer ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────

_COLUMNAS = (
    "r.processRunId, r.publicId, r.caseId, r.caseName, r.virtualCaseId, r.virtualCaseName, r.virtualCaseTypeName, r.processId, r.processName, "
    "r.processRevision, r.outcomeCase, r.outcomeText, r.status, r.executedAt, r.executedBy"
)
_RESUMEN = f"SELECT {_COLUMNAS} FROM nexus.processRuns r"


def _quien(guid: str, cache: dict[str, dict]) -> dict:
    """Quién la ejecutó: su id y, si todavía se puede leer, su nombre (el historial guarda el id; los nombres cambian)."""
    if guid not in cache:
        nombre = None
        try:
            from servicios import usuarios

            u = usuarios.por_guid(guid)
            nombre = (" ".join(x for x in (u.get("nombre"), u.get("apellidos")) if x).strip()) or u.get("email") or None
        except Exception:  # noqa: BLE001 — el nombre es un adorno: sin él, la ejecución se muestra igual
            nombre = None
        cache[guid] = {"id": guid, "nombre": nombre}
    return cache[guid]


def _resumen(f: dict, cache: dict[str, dict]) -> dict:
    return {
        "id": f["publicId"],
        "ejecutadaEn": momento(f["executedAt"]),
        "ejecutadaPor": _quien(f["executedBy"], cache),
        "expediente": {"id": f["caseId"], "nombre": f["caseName"]},
        # Solo las de antes del 2026-11-05 traen virtual (la foto de aquel momento); las nuevas, `null`.
        "virtual": {"id": f["virtualCaseId"], "nombre": f["virtualCaseName"], "tipoNombre": f["virtualCaseTypeName"]} if f["virtualCaseId"] is not None else None,
        "proceso": {"id": f["processId"], "nombre": f["processName"], "revision": int(f["processRevision"])},
        "resultado": {"caso": f["outcomeCase"], "texto": f["outcomeText"]},
        "estado": f["status"],
    }


def _valores(nombre: str, valor: object, largo: int = 63) -> list[str]:
    """Un filtro de lista como lista de textos, sin repetidos ni vacíos: `None` son ninguno y un texto suelto es uno solo. Más del tope, se rechaza."""
    if valor is None:
        return []
    crudo = [valor] if isinstance(valor, str) else list(valor)  # type: ignore[call-overload]
    limpios = list(dict.fromkeys(v[:largo] for v in crudo if isinstance(v, str) and v))
    if len(limpios) > MAX_FILTROS:
        raise ValueError(f"El filtro «{nombre}» admite hasta {MAX_FILTROS} valores.")
    return limpios


def _instante(valor: datetime | None) -> datetime | None:
    """Un instante de la petición como lo guarda la base (`executedAt`): hora UTC y sin zona. Uno que llega sin zona se toma por UTC."""
    if valor is None:
        return None
    return valor.astimezone(timezone.utc).replace(tzinfo=None) if valor.tzinfo else valor


def listar(
    tenant: str,
    expediente: str | None = None,
    proceso: str | list[str] | None = None,
    limite: int = LIMITE_POR_OMISION,
    antes: str | None = None,
    *,
    persona: list[str] | None = None,
    caso: list[str] | None = None,
    desde: datetime | None = None,
    hasta: datetime | None = None,
) -> dict:
    """El historial de la organización, de la más reciente a la más antigua. Filtros opcionales, que se combinan con Y: `expediente` (un id),
    `proceso` y `persona` (uno o varios ids: basta con que sea de UNO), `caso` (`si`, `no` o `sin`, uno o varios: cómo terminó) y el rango de fechas, `desde`
    (incluido) y `hasta` (NO incluido; sin zona se toma por UTC). `limite` (1 a 200) es cuántas entrega; `antes` es el id de la última que ya se vio, para
    pedir las siguientes. Devuelve `{ejecuciones, total, hayMas}`: `total` cuenta todas las que cumplen los filtros. Sin base: vacío. Un filtro que no
    vale (un caso que no existe, más de `MAX_FILTROS` valores) es `ValueError`."""
    procesos = _valores("proceso", proceso)
    personas = _valores("persona", persona, 254)
    casos = _valores("caso", caso)
    for un_caso in casos:
        if un_caso not in CASOS:
            raise ValueError("El caso de una ejecución es «si», «no» o «sin».")
    if not SQLSERVER_HOST:
        return {"ejecuciones": [], "total": 0, "hayMas": False}
    tablas = _tablas()
    limite = max(1, min(int(limite), LIMITE_MAXIMO))
    condiciones, parametros = ["r.tenant = ?"], [tenant]
    if expediente:
        condiciones.append("r.caseId = ?")
        parametros.append(expediente[:63])
    for columna, valores in (("r.processId", procesos), ("r.executedBy", personas), ("r.outcomeCase", casos)):
        if valores:
            condiciones.append(f"{columna} IN ({', '.join('?' * len(valores))})")
            parametros.extend(valores)
    for operador, instante in ((">=", _instante(desde)), ("<", _instante(hasta))):
        if instante is not None:
            condiciones.append(f"r.executedAt {operador} ?")
            parametros.append(instante)
    donde = " AND ".join(condiciones)
    with tablas.lectura() as cur:
        cur.execute(f"SELECT count(*) FROM nexus.processRuns r WHERE {donde}", *parametros)
        total = int(cur.fetchone()[0])
        extra, extra_parametros = "", []
        if antes:
            extra, extra_parametros = " AND r.processRunId < (SELECT processRunId FROM nexus.processRuns WHERE publicId = ? AND tenant = ?)", [antes[:63], tenant]
        cur.execute(f"{_RESUMEN} WHERE {donde}{extra} ORDER BY r.processRunId DESC OFFSET 0 ROWS FETCH NEXT ? ROWS ONLY", *parametros, *extra_parametros, limite + 1)
        filas = tablas.filas(cur)
    cache: dict[str, dict] = {}
    return {"ejecuciones": [_resumen(f, cache) for f in filas[:limite]], "total": total, "hayMas": len(filas) > limite}


def _sin_acentos(texto: str) -> str:
    """Para ordenar nombres como se leen: sin mayúsculas ni acentos (Álvaro entre Alberto y Beatriz, no después de Zoé)."""
    return "".join(c for c in unicodedata.normalize("NFKD", texto.casefold()) if not unicodedata.combining(c))


def opciones_de_filtro(tenant: str) -> dict:
    """Lo que se puede elegir para filtrar el historial: `procesos` (id y nombre) y `personas` (id y nombre, `None` si ya no se puede leer), cada lista por
    orden de nombre. Salen de lo que ESTÁ en el historial y no del catálogo vivo (un proceso borrado, o alguien que ya no está, siguen en la foto). Cada
    proceso lleva el nombre de su ejecución más reciente, que es el que tiene ahora si se renombró. Sin base: vacío."""
    if not SQLSERVER_HOST:
        return {"procesos": [], "personas": []}
    tablas = _tablas()
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT r.processId, r.processName FROM nexus.processRuns r "
            "JOIN (SELECT processId, MAX(processRunId) AS ultima FROM nexus.processRuns WHERE tenant = ? GROUP BY processId) u ON u.ultima = r.processRunId",
            tenant,
        )
        procesos = [{"id": f["processId"], "nombre": f["processName"]} for f in tablas.filas(cur)]
        cur.execute("SELECT DISTINCT executedBy FROM nexus.processRuns WHERE tenant = ?", tenant)
        guids = [f["executedBy"] for f in tablas.filas(cur)]
    cache: dict[str, dict] = {}
    personas = [_quien(g, cache) for g in guids]
    procesos.sort(key=lambda p: (_sin_acentos(p["nombre"]), p["id"]))
    personas.sort(key=lambda p: (p["nombre"] is None, _sin_acentos(p["nombre"] or ""), p["id"]))
    return {"procesos": procesos, "personas": personas}


def obtener(tenant: str, ejecucion_id: object) -> dict | None:
    """Una ejecución completa: su resumen, la lista de valores finales (en el orden del proceso), la foto de cómo salió cada regla (`detalle`) y el
    proceso tal como estaba (`definicion`). `None` si no es de esa organización (o no existe: no se distingue)."""
    if not SQLSERVER_HOST or not isinstance(ejecucion_id, str):
        return None
    tablas = _tablas()
    with tablas.lectura() as cur:
        cur.execute(
            f"SELECT {_COLUMNAS}, r.detailJson, r.definitionJson FROM nexus.processRuns r WHERE r.tenant = ? AND r.publicId = ?", tenant, ejecucion_id[:63]
        )
        f = tablas.una(cur)
        if f is None:
            return None
        cur.execute(
            "SELECT position, nodeId, name, kind, family, status, valueText, valueJson, origin FROM nexus.processRunFinalValues "
            "WHERE processRunId = ? ORDER BY position",
            f["processRunId"],
        )
        finales = tablas.filas(cur)
    return {
        **_resumen(f, {}),
        "valoresFinales": [
            {
                "nodoId": v["nodeId"],
                "nombre": v["name"],
                "forma": v["kind"],
                "familia": v["family"],
                "estado": v["status"],
                "valor": v["valueText"],
                "lista": json.loads(v["valueJson"]) if v["valueJson"] else None,
                "origen": v["origin"],
            }
            for v in finales
        ],
        "detalle": json.loads(f["detailJson"]),
        "definicion": json.loads(f["definitionJson"]),
    }
