"""Filas partidas: cuando Document AI parte UN registro de un grupo que se repite en varias filas, se vuelven a unir.

Medido el 2026-11-13 con un acta constitutiva de 10 páginas: sus tres accionistas salieron como CUATRO filas. Los datos de Carlos
están en dos lugares del acta —sus acciones y su aportación en la página 8; su nacimiento, estado civil y ciudad en la 9— y el
extractor, que arma cada fila con lo que tiene cerca en la hoja, hizo una fila con cada pedazo: «¿cuántos accionistas tiene?»
decía 4. Las dos filas traían el mismo nombre y ningún dato en el que se contradijeran.

LA REGLA (para que nadie la redescubra). Dos filas del mismo grupo son el mismo registro cuando:

  1. comparten un campo QUE IDENTIFICA, con el mismo valor: uno cuyo nombre dice nombre, razón social, denominación, RFC o CURP.
     Los valores se comparan sin acentos, mayúsculas, espacios ni puntuación. Un valor cualquiera no basta: dos socios nacidos en
     Monterrey no son la misma persona. Un grupo sin campo que identifique (las partidas de una factura) no se une nunca: dos
     partidas iguales son dos partidas;
  2. no se contradicen: todo campo que traen las dos tiene el mismo valor. Con UNA diferencia ya son dos registros;
  3. no hay ambigüedad: las filas que la regla 1 y 2 conecta se unen todas juntas, y solo si ninguna pareja de ellas se contradice.
     Un pedazo («Carlos, 25 acciones») que cuadra con dos Carlos distintos (padre e hijo, con distinta fecha de nacimiento) no se le
     asigna a ninguno.

La fila unida queda en el lugar de la primera, con las columnas de todas en el orden del esquema; de un dato que venía en varias, el
de mayor confianza (a igualdad, el primero). Cada dato conserva su página y su confianza. Lo que se unió queda dicho en
`_metadata.filas_unidas` de la extracción: `{"grupo", "fila", "de", "por"}` — la fila que quedó, las filas que había entregado
Document AI (contando desde 1, en su orden) y los campos por los que se reconocieron.

Pura: no llama a Google ni a la base. `ia._cerrar_resultado` la corre sobre lo ya empaquetado por `ia._campo` (y unido por bloques,
si el documento era largo), ANTES de calcular los campos ausentes: así la fila unida no se señala como incompleta por lo que traía
la otra mitad.
"""

import re
import unicodedata
from typing import Any

from servicios.bloques import es_campo

#: Palabras de un nombre de campo que lo vuelven «el que identifica» al registro (`nombre_accionista`, `rfc_socio`, `curp`…).
_PALABRAS_QUE_IDENTIFICAN = frozenset({"nombre", "nombres", "denominacion", "rfc", "curp"})
#: Lo mismo, pero de dos palabras.
_FRASES_QUE_IDENTIFICAN = ("razon_social",)


def _sin_acentos(texto: str) -> str:
    t = unicodedata.normalize("NFD", texto)
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def identifica(campo: str) -> bool:
    """¿El nombre de este subcampo dice quién es el registro?"""
    nombre = _sin_acentos(campo).lower()
    return any(p in _PALABRAS_QUE_IDENTIFICAN for p in re.split(r"[^a-z0-9]+", nombre)) or any(
        f in nombre for f in _FRASES_QUE_IDENTIFICAN
    )


def _comparable(campo: dict[str, Any]) -> str:
    """El valor de un campo para decir si dos son el mismo: sin acentos, en minúsculas y solo letras y números
    («Carlos Ramírez López.» = «CARLOS RAMIREZ LOPEZ»; «$333,333.33» = «333333.33»)."""
    valor = campo.get("value_normalized")
    if valor is None:
        valor = campo.get("value_raw")
    return re.sub(r"[^a-z0-9]", "", _sin_acentos("" if valor is None else str(valor)).lower())


def _confianza(valor: Any) -> float:
    c = valor.get("confianza") if isinstance(valor, dict) else None
    return float(c) if isinstance(c, (int, float)) else -1.0


def _se_contradicen(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Algún campo que traen las dos con distinto valor. Un subgrupo anidado en las dos cuenta como diferencia: no se sabe comparar
    y, ante la duda, no se une."""
    for k in a.keys() & b.keys():
        if not (es_campo(a[k]) and es_campo(b[k])) or _comparable(a[k]) != _comparable(b[k]):
            return True
    return False


def _se_reconocen(a: dict[str, Any], b: dict[str, Any]) -> bool:
    """Comparten al menos un campo que identifica, con el mismo valor (y no vacío)."""
    return any(
        identifica(k) and es_campo(a[k]) and es_campo(b[k]) and _comparable(a[k]) and _comparable(a[k]) == _comparable(b[k])
        for k in a.keys() & b.keys()
    )


def _fundir(partes: list[dict[str, Any]], hijos: dict[str, Any] | None) -> dict[str, Any]:
    """Una fila con lo de todas las partes: de un dato que traen varias, el de mayor confianza (a igualdad, el de la primera)."""
    fila: dict[str, Any] = {}
    for parte in partes:
        for columna, valor in parte.items():
            if columna not in fila or _confianza(valor) > _confianza(fila[columna]):
                fila[columna] = valor
    if not hijos:
        return fila
    return {**{h: fila[h] for h in hijos if h in fila}, **{k: v for k, v in fila.items() if k not in hijos}}


def _unir_grupo(filas: list[dict[str, Any]], hijos: dict[str, Any] | None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Las filas de UN grupo con las partidas ya unidas, y la lista de lo que se unió (sin el nombre del grupo)."""
    n = len(filas)
    # Quién se reconoce con quién (reglas 1 y 2), agrupado en componentes.
    padre = list(range(n))

    def raiz(i: int) -> int:
        while padre[i] != i:
            padre[i] = padre[padre[i]]
            i = padre[i]
        return i

    for i in range(n):
        for j in range(i + 1, n):
            if _se_reconocen(filas[i], filas[j]) and not _se_contradicen(filas[i], filas[j]):
                ri, rj = raiz(i), raiz(j)
                if ri != rj:
                    padre[max(ri, rj)] = min(ri, rj)

    componentes: dict[int, list[int]] = {}
    for i in range(n):
        componentes.setdefault(raiz(i), []).append(i)

    # Regla 3: un componente se une solo si ninguna pareja suya se contradice.
    se_unen = {
        r: len(c) > 1 and not any(_se_contradicen(filas[a], filas[b]) for k, a in enumerate(c) for b in c[k + 1 :])
        for r, c in componentes.items()
    }

    salida: list[dict[str, Any]] = []
    unidas: list[dict[str, Any]] = []
    for i in range(n):
        r = raiz(i)
        if not se_unen[r]:
            salida.append(filas[i])
            continue
        componente = componentes[r]
        if i != componente[0]:
            continue  # ya va dentro de la fila unida, en el lugar de la primera
        partes = [filas[k] for k in componente]
        fila = _fundir(partes, hijos)
        salida.append(fila)
        unidas.append(
            {
                "fila": len(salida),
                "de": [k + 1 for k in componente],
                "por": [c for c in fila if identifica(c) and sum(c in p for p in partes) > 1],
            }
        )
    return salida, unidas


def unir_filas_partidas(
    datos: dict[str, Any], estructura: dict[str, dict] | None = None
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """`datos` (la forma de `ia._extraer_entidades`) con las filas partidas de cada grupo repetible ya unidas, y la lista de lo que
    se unió (`[]` si nada). No toca lo que no es un grupo que se repite: campos únicos, grupos únicos ni listas de campos sueltos.

    `estructura` (`esquema.estructura_desde_esquema`) solo da el orden de las columnas; sin ella se reconoce el grupo por su forma
    (una lista de filas)."""
    resultado = dict(datos)
    unidas: list[dict[str, Any]] = []
    for nombre, valor in datos.items():
        if not isinstance(valor, list) or len(valor) < 2:
            continue
        if not all(isinstance(f, dict) and not es_campo(f) for f in valor):
            continue  # un campo hoja que se repite (una lista de teléfonos) no tiene filas que unir
        hijos = ((estructura or {}).get(nombre) or {}).get("hijos")
        filas, del_grupo = _unir_grupo(valor, hijos)
        if del_grupo:
            resultado[nombre] = filas
            unidas.extend({"grupo": nombre, **u} for u in del_grupo)
    return resultado, unidas
