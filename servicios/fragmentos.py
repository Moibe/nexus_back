"""Partir el texto de un documento en fragmentos, y encontrar en un fragmento las palabras de una consulta (épica 14, HU-094 y HU-097).

Lógica pura: no toca la base ni a Google, así se prueba sola (`verificar_fragmentos.py`).

## Los fragmentos

- **De texto**: el texto de cada página, en trozos de `tamano` tokens que se enciman `solape` tokens con el anterior (512 y 64 por omisión,
  lo que pide HU-094). Un trozo NUNCA cruza de página: la página que se enseña es exacta y abrir el documento cae donde está el pasaje.
- **De campos**: uno por página con los datos extraídos vigentes, uno por renglón («Nombre: ANA LÓPEZ RUIZ»), encabezado por el tipo
  documental. Así se encuentra un documento por lo que se extrajo de él, y una corrección a mano cambia este fragmento (y solo este).

Los tokens se ESTIMAN (una palabra cuenta un token por cada 4 caracteres, mínimo uno): no hace falta pedirle a Google que los cuente para
partir, y la estimación queda cerca de lo que cuenta su tokenizador en español.

## El resaltado

`resaltar` devuelve los tramos `[inicio, fin)` del fragmento donde aparecen las palabras de la consulta, sin distinguir mayúsculas ni acentos;
una palabra larga casa también con sus variantes («vencida» con «vencidas», «vencimiento» no). `pasaje` recorta el fragmento a la ventana
con más coincidencias, para enseñarlo sin el fragmento entero.
"""

from __future__ import annotations

import math
import re
import unicodedata
from typing import Any

TAMANO = 512
SOLAPE = 64

_PIEZA = re.compile(r"\S+\s*")
_PALABRA = re.compile(r"\w+", re.UNICODE)

#: Palabras que no se resaltan aunque estén en la consulta: no dicen nada del pasaje.
_VACIAS = frozenset(
    "a al ante bajo con contra de del desde donde durante el en entre es esa ese eso esta este esto hacia hasta la las le les lo los mas mi "
    "mis muy no o os para pero por que quien se sin sobre su sus te tu tus un una uno unos unas y ya cual cuales como cuando".split()
)


def estimar_tokens(texto: str) -> int:
    """Tokens aproximados: cada palabra cuenta uno por cada 4 caracteres (mínimo uno)."""
    return sum(max(1, math.ceil(len(p) / 4)) for p in texto.split())


def _tokens_de(pieza: str) -> int:
    palabra = pieza.strip()
    return max(1, math.ceil(len(palabra) / 4)) if palabra else 0


def partir(texto: str, tamano: int = TAMANO, solape: int = SOLAPE) -> list[str]:
    """Un texto en trozos de hasta `tamano` tokens; cada trozo empieza `solape` tokens antes de donde terminó el anterior.

    Una sola palabra más larga que `tamano` queda sola en su trozo (no se corta a la mitad)."""
    if tamano < 1:
        raise ValueError("El tamaño del fragmento debe ser positivo.")
    solape = max(0, min(solape, tamano - 1))
    piezas = _PIEZA.findall(texto.strip())
    if not piezas:
        return []
    costos = [_tokens_de(p) for p in piezas]
    trozos: list[str] = []
    inicio = 0
    while inicio < len(piezas):
        fin, suma = inicio, 0
        while fin < len(piezas) and (suma + costos[fin] <= tamano or fin == inicio):
            suma += costos[fin]
            fin += 1
        trozos.append("".join(piezas[inicio:fin]).strip())
        if fin >= len(piezas):
            break
        # El siguiente empieza tantas piezas atrás como quepan en `solape`, pero siempre avanza al menos una.
        atras, encimado = fin, 0
        while atras - 1 > inicio and encimado + costos[atras - 1] <= solape:
            atras -= 1
            encimado += costos[atras]
        inicio = atras if atras > inicio else fin
    return [t for t in trozos if t]


def fragmentos_de_texto(lineas_por_pagina: dict[int, list[str]], tamano: int = TAMANO, solape: int = SOLAPE) -> list[dict[str, Any]]:
    """Los fragmentos de texto de un documento: `{pagina, texto, tokens}` en orden de página, nunca cruzando de una a otra."""
    salida: list[dict[str, Any]] = []
    for pagina in sorted(lineas_por_pagina):
        texto = "\n".join(l.strip() for l in lineas_por_pagina[pagina] if l and l.strip())
        for trozo in partir(texto, tamano, solape):
            salida.append({"pagina": pagina, "texto": trozo, "tokens": estimar_tokens(trozo)})
    return salida


def _etiqueta_de_campo(ruta: str, nombres: dict[str, str]) -> str:
    """«Socios 2 › Nombre» para `socios[2].nombre`, con el nombre que se le dio en el tipo documental cuando se conoce."""
    partes = ruta.split(".")
    salida = []
    camino = ""
    for parte in partes:
        m = re.fullmatch(r"(.+?)\[(\d+)\]", parte)
        base, fila = (m.group(1), m.group(2)) if m else (parte, None)
        camino = f"{camino}.{base}" if camino else base
        nombre = nombres.get(camino) or base.replace("_", " ").strip().capitalize()
        salida.append(f"{nombre} {fila}" if fila else nombre)
    return " › ".join(salida)


def fragmentos_de_campos(
    hechos: list[dict[str, Any]], nombres: dict[str, str], tipo: str | None, tamano: int = TAMANO, solape: int = SOLAPE
) -> list[dict[str, Any]]:
    """Los fragmentos de campos de un documento: uno por página con sus datos vigentes («Campo: valor», uno por renglón).

    `hechos`: `{ruta, valor, pagina}` (sin valor no entran). `nombres`: ruta sin filas (`socios.nombre`) → nombre en pantalla. Los datos sin
    página van en un fragmento sin página, al final. Una página con muchísimos datos se parte como el texto."""
    por_pagina: dict[int | None, list[str]] = {}
    for h in hechos:
        valor = h.get("valor")
        if valor is None or not str(valor).strip():
            continue
        pagina = h.get("pagina") if isinstance(h.get("pagina"), int) and h["pagina"] >= 1 else None
        por_pagina.setdefault(pagina, []).append(f"{_etiqueta_de_campo(str(h.get('ruta') or ''), nombres)}: {str(valor).strip()}")
    encabezado = f"{tipo} · datos extraídos" if tipo else "Datos extraídos"
    salida: list[dict[str, Any]] = []
    for pagina in sorted(por_pagina, key=lambda p: (p is None, p or 0)):
        cuerpo = "\n".join(por_pagina[pagina])
        for trozo in partir(cuerpo, max(16, tamano - estimar_tokens(encabezado)), solape):
            texto = f"{encabezado}\n{trozo}"
            salida.append({"pagina": pagina, "texto": texto, "tokens": estimar_tokens(texto)})
    return salida


# ── El resaltado ────────────────────────────────────────────────────────────────────────────────────────────────────────


def _plano(texto: str) -> str:
    """Minúsculas y sin acentos, carácter por carácter: el resultado mide lo mismo que el original, así los tramos sirven para los dos."""
    return "".join(unicodedata.normalize("NFD", ch)[0].lower()[0] for ch in texto)


def terminos(consulta: str) -> list[str]:
    """Las palabras de la consulta que vale la pena buscar en el pasaje (sin vacías, de 3 letras o más), ya en plano y sin repetir."""
    vistos: list[str] = []
    for p in _PALABRA.findall(_plano(consulta)):
        if (len(p) >= 3 or p.isdigit()) and p not in _VACIAS and p not in vistos:
            vistos.append(p)
    return vistos


def _casa(palabra: str, termino: str) -> bool:
    if palabra == termino:
        return True
    # Variantes de una palabra larga: comparten todo menos las 2 últimas letras del término (vencida ~ vencidas ~ vencido).
    if len(termino) >= 5 and len(palabra) >= 5:
        raiz = termino[: max(4, len(termino) - 2)]
        return palabra.startswith(raiz) and abs(len(palabra) - len(termino)) <= 3
    return False


def resaltar(texto: str, consulta: str) -> list[list[int]]:
    """Los tramos `[inicio, fin)` de `texto` donde aparece alguna palabra de la consulta."""
    lista = terminos(consulta)
    if not lista or not texto:
        return []
    plano = _plano(texto)
    tramos: list[list[int]] = []
    for m in _PALABRA.finditer(plano):
        if any(_casa(m.group(0), t) for t in lista):
            tramos.append([m.start(), m.end()])
    return tramos


def pasaje(texto: str, tramos: list[list[int]], largo: int = 420) -> dict[str, Any]:
    """El trozo de `texto` (de unos `largo` caracteres) con más coincidencias, cortado en espacios, y sus tramos relativos a él.

    Devuelve `{texto, resaltado, alInicio, alFinal}`: `alInicio`/`alFinal` dicen si el pasaje empieza o termina donde el fragmento (si no, la
    pantalla pone «…»)."""
    if len(texto) <= largo:
        return {"texto": texto, "resaltado": [list(t) for t in tramos], "alInicio": True, "alFinal": True}
    if tramos:
        # La ventana que empieza en cada coincidencia y abarca más coincidencias; empate: la primera.
        mejor, cuenta = tramos[0][0], 0
        for t in tramos:
            n = sum(1 for u in tramos if u[0] >= t[0] and u[1] <= t[0] + largo)
            if n > cuenta:
                mejor, cuenta = t[0], n
        inicio = max(0, mejor - largo // 5)
    else:
        inicio = 0
    fin = min(len(texto), inicio + largo)
    inicio = max(0, fin - largo)
    # Cortar en espacios para no partir palabras.
    if inicio > 0:
        espacio = texto.find(" ", inicio)
        inicio = espacio + 1 if 0 <= espacio < inicio + 40 else inicio
    if fin < len(texto):
        espacio = texto.rfind(" ", inicio, fin)
        fin = espacio if espacio > inicio + largo // 2 else fin
    dentro = [[a - inicio, b - inicio] for a, b in tramos if a >= inicio and b <= fin]
    return {"texto": texto[inicio:fin], "resaltado": dentro, "alInicio": inicio == 0, "alFinal": fin == len(texto)}
