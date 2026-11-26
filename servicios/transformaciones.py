"""La «Regla de normalización» de cada campo de un tipo documental (paso 3 del asistente), aplicada DESPUÉS de extraer (2026-11-26, Moibe: «sigamos con
que sirvan los de transformación»). Hasta hoy se guardaba en el tipo y nadie la aplicaba.

No se le puede pedir a Document AI: medido el 2026-10-30, una instrucción que pide reescribir el valor hace que el campo DESAPAREZCA de la respuesta (el
extractor devuelve texto anclado en lo impreso). Por eso se aplica aquí, al guardar la corrida: `valueRaw` conserva lo que leyó el extractor y
`valueNormalized` guarda el valor ya transformado, que es el que usan la pantalla, las ejecuciones de procesos y la búsqueda.

Regla de oro: si la regla no puede con un valor (una fecha que no se entiende, un monto que no es número), el valor se queda COMO VINO. Transformar nunca
pierde un dato.

Reglas (las llaves son las que guarda el front en `reglaTransformacion`):
  · fecha_iso       «15/03/2026», «15 de marzo de 2026», «15-MAR-2026», «2026/03/15» → «2026-03-15». Día antes que mes (como en México); año de dos
                    dígitos: hasta 30 → 20xx, si no 19xx.
  · quitar_moneda   «$ 1,234.50 MXN» → «1234.50»; también «1.234,50 €» → «1234.50» y «(1,000.00)» → «-1000.00». Si no queda un número, solo se quitan
                    los símbolos y códigos.
  · mayusculas      «Juan Pérez» → «JUAN PÉREZ». (`cambiar_caja`, la llave de antes, significa esto mismo.)
  · minusculas      «JUAN@CORREO.COM» → «juan@correo.com».
  · trim_espacios   quita espacios al principio y al final y deja uno solo donde había varios (también saltos de línea y tabuladores).
  · valor_canonico  lleva una variante a uno de los «valores de la lista» del campo: «masc», «M.» → «Masculino». Compara sin acentos, mayúsculas ni
                    puntuación; acepta el valor exacto o una abreviatura que solo empiece UNO de los valores. Sin lista, o si no hay a cuál, se queda igual.

Este módulo es PURO (no toca red ni base): se prueba con `verificar_transformaciones.py`.
"""

from __future__ import annotations

import re
import unicodedata
from datetime import date
from typing import Any

REGLAS = ("fecha_iso", "quitar_moneda", "cambiar_caja", "mayusculas", "minusculas", "trim_espacios", "valor_canonico")

_MESES = {
    "ene": 1, "enero": 1, "jan": 1, "january": 1,
    "feb": 2, "febrero": 2, "february": 2,
    "mar": 3, "marzo": 3, "march": 3,
    "abr": 4, "abril": 4, "apr": 4, "april": 4,
    "may": 5, "mayo": 5,
    "jun": 6, "junio": 6, "june": 6,
    "jul": 7, "julio": 7, "july": 7,
    "ago": 8, "agosto": 8, "aug": 8, "august": 8,
    "sep": 9, "sept": 9, "septiembre": 9, "setiembre": 9, "september": 9,
    "oct": 10, "octubre": 10, "october": 10,
    "nov": 11, "noviembre": 11, "november": 11,
    "dic": 12, "diciembre": 12, "dec": 12, "december": 12,
}


def _sin_acentos(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto) if unicodedata.category(c) != "Mn")


def _anio(texto: str) -> int:
    n = int(texto)
    if len(texto) <= 2:
        return 2000 + n if n <= 30 else 1900 + n
    return n


def _fecha(dia: int, mes: int, anio: int) -> str | None:
    try:
        return date(anio, mes, dia).isoformat()
    except ValueError:
        return None


def fecha_iso(valor: str) -> str | None:
    """La fecha en AAAA-MM-DD, o `None` si no se entiende."""
    t = _sin_acentos(valor.strip().lower())
    t = re.sub(r"[t\s]\d{1,2}:\d{2}(:\d{2})?(\.\d+)?\s*(z|[+-]\d{2}:?\d{2}|hrs?|h)?$", "", t).strip()  # una hora al final no estorba
    if m := re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t):
        return _fecha(int(m[3]), int(m[2]), int(m[1]))
    if m := re.fullmatch(r"(\d{1,2})[-/.\s](\d{1,2})[-/.\s](\d{2}|\d{4})", t):
        return _fecha(int(m[1]), int(m[2]), _anio(m[3]))
    if m := re.fullmatch(r"(\d{8})", t):  # AAAAMMDD
        return _fecha(int(t[6:8]), int(t[4:6]), int(t[0:4]))
    # «15 de marzo de 2026», «15-MAR-2026», «15 mar. 2026», «marzo 15, 2026»
    if m := re.fullmatch(r"(\d{1,2})(?:\s*de)?[\s\-/.]*([a-z]+)\.?(?:\s*de(?:l)?)?[\s\-/.,]*(\d{2}|\d{4})", t):
        mes = _MESES.get(m[2])
        return _fecha(int(m[1]), mes, _anio(m[3])) if mes else None
    if m := re.fullmatch(r"([a-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", t):
        mes = _MESES.get(m[1])
        return _fecha(int(m[2]), mes, int(m[3])) if mes else None
    return None


_SIMBOLOS = re.compile(r"(?i)us\$|mx\$|\$|€|£|¥|\b(?:mxn|mxp|usd|eur|m\.?\s?n\.?|pesos?|d[oó]lares?|euros?)(?=\W|$)\.?")


def quitar_moneda(valor: str) -> str:
    """El monto sin símbolo ni código de moneda; si queda un número, como número plano con punto decimal."""
    t = _SIMBOLOS.sub("", valor).strip()
    negativo = t.startswith("(") and t.endswith(")")
    t = t.strip("()").strip()
    if t.startswith("-"):
        negativo, t = True, t[1:].strip()
    t = t.replace(" ", "")
    if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?|\d+(\.\d+)?", t):
        numero = t.replace(",", "")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?|\d+,\d+", t):
        numero = t.replace(".", "").replace(",", ".")
    else:
        limpio = re.sub(r"\s+", " ", _SIMBOLOS.sub("", valor)).strip()
        return limpio or valor
    return f"-{numero}" if negativo and float(numero) != 0 else numero


def trim_espacios(valor: str) -> str:
    return re.sub(r"\s+", " ", valor).strip()


def _clave(texto: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", _sin_acentos(texto).upper())


def valor_canonico(valor: str, lista: list[str]) -> str | None:
    """Uno de los valores de `lista` al que `valor` se refiere, o `None` si no hay uno solo."""
    opciones = [v for v in lista if isinstance(v, str) and v.strip()]
    clave = _clave(valor)
    if not clave or not opciones:
        return None
    exactas = [v for v in opciones if _clave(v) == clave]
    if len(exactas) == 1:
        return exactas[0].strip()
    prefijos = [v for v in opciones if _clave(v).startswith(clave)]
    return prefijos[0].strip() if len(prefijos) == 1 else None


def aplicar(regla: str | None, valor: str | None, campo: dict[str, Any] | None = None) -> str | None:
    """`valor` con la regla aplicada; si no hay regla, no hay valor o la regla no puede con él, `valor` tal cual."""
    if not regla or not isinstance(valor, str) or not valor:
        return valor
    if regla == "fecha_iso":
        return fecha_iso(valor) or valor
    if regla == "quitar_moneda":
        return quitar_moneda(valor)
    if regla in ("mayusculas", "cambiar_caja"):
        return valor.upper()
    if regla == "minusculas":
        return valor.lower()
    if regla == "trim_espacios":
        return trim_espacios(valor) or valor
    if regla == "valor_canonico":
        return valor_canonico(valor, (campo or {}).get("valoresLista") or []) or valor
    return valor


def _reglas_por_llave(campos: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Los campos SUELTOS con regla, por el nombre con que el extractor los devuelve (`esquema.nombres_en_google`, la misma regla con que se armó su
    esquema). Los grupos y sus subcampos no tienen regla."""
    from servicios.esquema import TIPO_GRUPO, nombres_en_google

    nombres = nombres_en_google(campos)
    por_llave: dict[str, dict[str, Any]] = {}
    for campo in campos:
        regla = campo.get("reglaTransformacion")
        if campo.get("tipoDato") == TIPO_GRUPO or regla not in REGLAS:
            continue
        grupo, llave = nombres.get(campo.get("nombre", ""), (None, None))
        if llave and grupo is None:
            por_llave[llave] = campo
    return por_llave


def aplicar_a_resultado(resultado: dict[str, Any] | None, campos: list[dict[str, Any]]) -> int:
    """Aplica, EN SU LUGAR, la regla de cada campo del tipo a la respuesta del extractor (`ia.extraer_con_procesador`): el `value_normalized` de cada
    campo (o de cada valor, si el campo se repite) queda transformado y `value_raw` conserva lo que se leyó; si el extractor no mandó `value_raw`, se
    guarda ahí lo de antes, para no perderlo. Así la base (`aplanar`), la bandeja y los webhooks ven lo mismo. Devuelve cuántos valores cambiaron."""
    from servicios.bloques import es_campo

    if not isinstance(resultado, dict):
        return 0
    por_llave = _reglas_por_llave(campos)
    cambiados = 0

    def transformar(nodo: Any, campo: dict[str, Any]) -> None:
        nonlocal cambiados
        if not es_campo(nodo):
            return
        valor = nodo.get("value_normalized")
        if valor is None:
            valor = nodo.get("value_raw")
        if not isinstance(valor, str):
            return
        nuevo = aplicar(campo["reglaTransformacion"], valor, campo)
        if nuevo != valor:
            if nodo.get("value_raw") is None:
                nodo["value_raw"] = valor
            nodo["value_normalized"] = nuevo
            cambiados += 1

    for llave, campo in por_llave.items():
        nodo = resultado.get(llave)
        for elemento in nodo if isinstance(nodo, list) else [nodo]:
            transformar(elemento, campo)
    return cambiados
