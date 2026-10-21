"""Documentos largos: partir en bloques de páginas y unir lo que sale de cada uno.

Document AI rechaza en línea un documento de más de 15 páginas («Document pages exceed the limit: 15 got 20»), y un acta
constitutiva ronda las 20. Se procesa por BLOQUES: el mismo archivo, pidiéndole a Google solo un rango de páginas con
`processOptions.individualPageSelector`, que SÍ esquiva ese tope (medido el 2026-10-16 con un PDF de 20 páginas: pidiendo
las páginas 11 a 20 contestó bien y cobró solo esas diez). Este módulo hace las dos partes que no necesitan red:

  1. `planear_bloques`: en qué rangos partir. Parejos (no «15 + lo que sobre») para que ningún bloque sea mucho más lento que
     otro, y con un SOLAPE de una página: una cláusula o una fila de tabla que cruza el corte sale completa en alguno de los
     dos bloques, que es la única forma de no perderla (Google ve cada bloque como un documento aparte y corta lo que se
     queda en el borde).
  2. `combinar_datos` y `unir_capas_ocr`: unir lo que devolvió cada bloque en UNA extracción, con la forma que habría tenido si
     el documento hubiera pasado entero.

Lo que NO hace: llamar a Google (eso es de `servicios.ia`), ni saber de INE ni de ningún tipo documental. Trabaja sobre los
campos ya empaquetados por `ia._campo` (`value_raw`, `value_normalized`, `confianza`, `page_number`, `posicion`…).

CÓMO SE UNE (las reglas, para que nadie las redescubra):

  - Un campo ÚNICO que apareció en varios bloques: gana la mayor confianza (empate: el bloque más temprano). Excepción: un valor
    LARGO que está en una página de solape y que otro bloque trae completo (su texto lo contiene) se descarta, porque es el
    pedazo que el borde del bloque le cortó a la cláusula.
  - Un campo MÚLTIPLE: se concatenan las listas en el orden del documento, sin repetir lo que se vio dos veces por el solape.
  - Un GRUPO múltiple: se concatenan las filas, y una fila que dos bloques vieron (la del solape) se une en una sola con las
    columnas de las dos: lo que un bloque dejó fuera por el borde lo trae el otro.
  - «La misma entidad vista desde dos bloques» es: misma página, mismo texto y casi la misma caja (la caja no sale idéntica al
    píxel entre dos llamadas, de ahí la tolerancia).
"""

import math
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# Cuánto puede moverse la caja de una entidad entre dos llamadas y seguir siendo «la misma» (fracción del ancho/alto de la página).
TOLERANCIA_POSICION = 0.01

# Un valor más corto que esto no se descarta por estar contenido en otro: lo que el borde de un bloque corta es un texto largo
# (una cláusula), no un nombre ni una fecha.
LARGO_MINIMO_DE_PEDAZO = 30


def es_campo(valor: Any) -> bool:
    """¿Es un campo hoja ya empaquetado por `ia._campo`? (y no un grupo, que es un diccionario de campos)."""
    return isinstance(valor, dict) and "value_normalized" in valor and "metodo_confianza" in valor


# ── 1. En qué rangos partir ──────────────────────────────────────────────────


def planear_bloques(total_paginas: int, tope: int, solape: int = 1) -> list[tuple[int, int]]:
    """Rangos `(desde, hasta)` —páginas de 1 en adelante, ambas incluidas— que cubren el documento.

    `tope` es lo más que cabe en un bloque. Si el documento cabe, es un solo bloque. Si no, se reparte en el MENOR número de
    bloques posible y se emparejan sus tamaños (20 páginas con tope 15 son 11 + 10, no 15 + 6: el bloque de 15 tardaría casi lo
    mismo que los dos juntos). Cada bloque repite las últimas `solape` páginas del anterior.

        planear_bloques(20, 15, 1) → [(1, 11), (11, 20)]
        planear_bloques(30, 15, 1) → [(1, 11), (11, 21), (21, 30)]
    """
    if total_paginas < 1:
        raise ValueError(f"total_paginas tiene que ser al menos 1 (llegó {total_paginas}).")
    if tope < 1:
        raise ValueError(f"tope tiene que ser al menos 1 (llegó {tope}).")
    if total_paginas <= tope:
        return [(1, total_paginas)]

    # Un solape de `tope` o más haría que el bloque siguiente no avanzara nunca.
    solape = max(0, min(solape, tope - 1))
    avance = tope - solape  # páginas NUEVAS que como máximo aporta cada bloque
    cuantos = math.ceil((total_paginas - solape) / avance)
    tamano = math.ceil((total_paginas + (cuantos - 1) * solape) / cuantos)

    bloques: list[tuple[int, int]] = []
    desde = 1
    for _ in range(cuantos):
        hasta = min(desde + tamano - 1, total_paginas)
        bloques.append((desde, hasta))
        desde = hasta - solape + 1
    return bloques


# ── 2. Unir lo que devolvió cada bloque ──────────────────────────────────────


@dataclass
class Parcial:
    """Lo que salió de UN bloque, ya con páginas absolutas (no relativas al bloque) y con `bloque_indice` apuntando a la capa de
    OCR unida. `desde`/`hasta` son las páginas que ese bloque pidió."""

    desde: int
    hasta: int
    datos: dict[str, Any]


def _texto(campo: dict[str, Any]) -> str:
    """El valor de un campo sin acentos, en minúsculas y con los espacios colapsados: para decir si dos valores son el mismo."""
    valor = campo.get("value_normalized")
    if valor is None:
        valor = campo.get("value_raw")
    t = unicodedata.normalize("NFD", "" if valor is None else str(valor))
    t = "".join(c for c in t if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", t).strip().lower()


def _mismo_dato(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """¿Es la MISMA entidad vista desde dos bloques? Misma página, mismo texto y —si las dos traen posición— casi la misma caja.

    Dos valores iguales en páginas distintas (dos «Administrador» en dos cláusulas) NO son lo mismo: son dos datos."""
    if a.get("page_number") != b.get("page_number"):
        return False
    if _texto(a) != _texto(b):
        return False
    pa, pb = a.get("posicion"), b.get("posicion")
    if pa and pb:
        return abs(pa["x"] - pb["x"]) <= TOLERANCIA_POSICION and abs(pa["y"] - pb["y"]) <= TOLERANCIA_POSICION
    return True


def _confianza(campo: dict[str, Any]) -> float:
    c = campo.get("confianza")
    return float(c) if isinstance(c, (int, float)) else -1.0


def _paginas_solapadas(parciales: list[Parcial]) -> set[int]:
    """Las páginas que más de un bloque pidió."""
    vistas: set[int] = set()
    repetidas: set[int] = set()
    for p in parciales:
        for pagina in range(p.desde, p.hasta + 1):
            (repetidas if pagina in vistas else vistas).add(pagina)
    return repetidas


def _elegir_campo(candidatos: list[tuple[int, dict[str, Any]]], solapadas: set[int]) -> dict[str, Any]:
    """De las apariciones de un campo ÚNICO en distintos bloques `(indice_de_bloque, campo)`, la que se publica."""
    textos = [_texto(c) for _, c in candidatos]

    def cortado_por_el_borde(k: int) -> bool:
        i, campo = candidatos[k]
        if campo.get("page_number") not in solapadas or len(textos[k]) < LARGO_MINIMO_DE_PEDAZO:
            return False
        return any(j != i and textos[k] != t and textos[k] in t for (j, _), t in zip(candidatos, textos))

    vigentes = [c for k, c in enumerate(candidatos) if not cortado_por_el_borde(k)] or candidatos
    # Mayor confianza; a igualdad, el bloque más temprano (el orden del documento).
    return max(vigentes, key=lambda ic: (_confianza(ic[1]), -ic[0]))[1]


def _elegir_grupo(candidatos: list[tuple[int, dict[str, Any]]]) -> dict[str, Any]:
    """Lo mismo para un GRUPO único (un domicilio): el que trae más subcampos, y a igualdad el de mayor confianza media."""

    def media(grupo: dict[str, Any]) -> float:
        confianzas = [_confianza(v) for v in grupo.values() if es_campo(v) and _confianza(v) >= 0]
        return sum(confianzas) / len(confianzas) if confianzas else -1.0

    return max(candidatos, key=lambda ic: (len(ic[1]), media(ic[1]), -ic[0]))[1]


def _unir_hojas(listas: list[tuple[int, list[dict[str, Any]]]]) -> list[dict[str, Any]]:
    """Campo hoja múltiple: las listas de todos los bloques en orden, sin repetir lo que el solape mostró dos veces."""
    unidos: list[dict[str, Any]] = []
    origen: list[int] = []
    for i, lista in listas:
        for campo in lista:
            if any(o < i and _mismo_dato(campo, previo) for o, previo in zip(origen, unidos)):
                continue
            unidos.append(campo)
            origen.append(i)
    return unidos


def _misma_fila(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """¿Son la misma fila de un grupo vista desde dos bloques? Comparten al menos una columna y TODAS las que comparten son la
    misma entidad (misma página, texto y caja). Con una sola coincidencia que difiera ya son dos filas."""
    comunes = [k for k in a if k in b]
    if not comunes:
        return False
    return all(es_campo(a[k]) and es_campo(b[k]) and _mismo_dato(a[k], b[k]) for k in comunes)


def _en_orden(fila: dict[str, Any], hijos: dict[str, Any] | None) -> dict[str, Any]:
    """La fila con sus columnas en el orden del esquema (y las que el esquema no conoce, al final)."""
    if not hijos:
        return fila
    return {**{h: fila[h] for h in hijos if h in fila}, **{k: v for k, v in fila.items() if k not in hijos}}


def _unir_filas(listas: list[tuple[int, list[dict[str, Any]]]], hijos: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Grupo múltiple: las filas de todos los bloques en orden. Una fila que un bloque posterior ya había visto (la del solape) no
    se repite: se une con la anterior, que se queda con las columnas de las dos. Solo se compara contra filas de bloques
    ANTERIORES: dos filas del mismo bloque son dos filas aunque se parezcan."""
    filas: list[dict[str, Any]] = []
    origen: list[int] = []
    for i, lista in listas:
        for fila in lista:
            destino = next((k for k, previa in enumerate(filas) if origen[k] < i and _misma_fila(previa, fila)), None)
            if destino is None:
                filas.append(dict(fila))
                origen.append(i)
            else:
                for columna, valor in fila.items():
                    filas[destino].setdefault(columna, valor)
    return [_en_orden(f, hijos) for f in filas]


def combinar_datos(parciales: list[Parcial], estructura: dict[str, dict] | None = None) -> dict[str, Any]:
    """Une los campos de todos los bloques en un solo diccionario con la forma de `ia._extraer_entidades`.

    `estructura` (`esquema.estructura_desde_esquema`) dice qué se repite; sin ella se infiere por lo que llegó (una lista es un
    campo múltiple)."""
    estructura = estructura or {}
    solapadas = _paginas_solapadas(parciales)

    orden: list[str] = []
    for p in parciales:
        for nombre in p.datos:
            if nombre not in orden:
                orden.append(nombre)

    resultado: dict[str, Any] = {}
    for nombre in orden:
        apariciones = [(i, p.datos[nombre]) for i, p in enumerate(parciales) if nombre in p.datos]
        definicion = estructura.get(nombre)
        multiple = definicion["multiple"] if definicion else any(isinstance(v, list) for _, v in apariciones)
        hijos = definicion.get("hijos") if definicion else None
        if multiple:
            listas = [(i, v if isinstance(v, list) else [v]) for i, v in apariciones]
            primero = next((e for _, lista in listas for e in lista), None)
            if primero is None:
                continue
            resultado[nombre] = _unir_hojas(listas) if es_campo(primero) else _unir_filas(listas, hijos)
        else:
            unicos = [(i, v[0] if isinstance(v, list) and v else v) for i, v in apariciones]
            resultado[nombre] = (
                _elegir_campo(unicos, solapadas) if es_campo(unicos[0][1]) else _elegir_grupo(unicos)
            )
    return resultado


def remapear_bloques(datos: Any, mapa: list[int | None]) -> Any:
    """Cambia el `bloque_indice` de cada campo (que apuntaba a la capa de OCR de SU bloque) por el índice en la capa unida.
    Un índice que no esté en el mapa queda en `None`. Trabaja sobre `datos` en el lugar y lo devuelve."""
    if isinstance(datos, list):
        for elemento in datos:
            remapear_bloques(elemento, mapa)
    elif isinstance(datos, dict):
        if es_campo(datos):
            indice = datos.get("bloque_indice")
            datos["bloque_indice"] = mapa[indice] if isinstance(indice, int) and 0 <= indice < len(mapa) else None
        else:
            for valor in datos.values():
                remapear_bloques(valor, mapa)
    return datos


# ── La capa de OCR ───────────────────────────────────────────────────────────


def _bloque_equivalente(bloque: dict[str, Any], candidatos: list[dict[str, Any]]) -> int | None:
    """El bloque ya guardado que es el mismo renglón que `bloque` (misma página, mismo texto, caja casi igual), o `None`."""
    texto = re.sub(r"\s+", " ", str(bloque.get("texto", ""))).strip()
    caja = bloque.get("bbox") or {}
    for otro in candidatos:
        if re.sub(r"\s+", " ", str(otro.get("texto", ""))).strip() != texto:
            continue
        otra = otro.get("bbox") or {}
        if abs(otra.get("x", 0.0) - caja.get("x", 0.0)) <= TOLERANCIA_POSICION and abs(otra.get("y", 0.0) - caja.get("y", 0.0)) <= TOLERANCIA_POSICION:
            return otro["indice"]
    return None


def unir_capas_ocr(capas: list[dict[str, Any]], total_paginas: int) -> tuple[dict[str, Any], list[list[int | None]]]:
    """Une las capas de OCR de los bloques en UNA, con los números de página del documento entero.

    Una página que dos bloques leyeron (la del solape) se queda con los renglones del PRIMER bloque que la leyó: no se duplican
    en la capa. Devuelve la capa y, por cada bloque, un mapa `índice local → índice en la capa unida` para reapuntar los
    campos (`remapear_bloques`); un renglón de una página repetida apunta a su equivalente del bloque anterior, o a `None` si
    no se encuentra."""
    paginas_vistas: set[int] = set()
    paginas: list[dict[str, Any]] = []
    bloques: list[dict[str, Any]] = []
    mapas: list[list[int | None]] = []
    por_pagina: dict[int, list[dict[str, Any]]] = {}

    for capa in capas:
        de_esta = {p["page_number"] for p in capa.get("paginas", [])}
        nuevas = de_esta - paginas_vistas
        paginas.extend(p for p in capa.get("paginas", []) if p["page_number"] in nuevas)
        mapa: list[int | None] = []
        repetidos: list[tuple[int, dict[str, Any]]] = []
        for local, bloque in enumerate(capa.get("bloques", [])):
            if bloque["page_number"] in nuevas:
                nuevo = {**bloque, "indice": len(bloques)}
                bloques.append(nuevo)
                por_pagina.setdefault(nuevo["page_number"], []).append(nuevo)
                mapa.append(nuevo["indice"])
            else:
                mapa.append(None)
                repetidos.append((local, bloque))
        for local, bloque in repetidos:
            mapa[local] = _bloque_equivalente(bloque, por_pagina.get(bloque["page_number"], []))
        mapas.append(mapa)
        paginas_vistas |= de_esta

    primera = capas[0] if capas else {}
    unida = {
        "engine": primera.get("engine"),
        "engine_version": primera.get("engine_version"),
        "page_count": total_paginas,
        "language": next((c.get("language") for c in capas if c.get("language")), None),
        "paginas": sorted(paginas, key=lambda p: p["page_number"]),
        "bloques": bloques,
    }
    return unida, mapas
