"""Verificación de `servicios/bloques.py`: partir un documento largo en bloques de páginas y unir lo que sale de cada uno.
Sin red y sin Document AI: son funciones puras.

Correr con:  python verificar_bloques.py
Mismo espíritu que los otros verificar_*.py: casos concretos, salida legible, exit code 1 si algo no cuadra.

Lo que cuida (paso 3 de las actas constitutivas, 2026-10-16): que un acta de 20 páginas, que Google rechaza entera, se pueda
procesar por pedazos SIN que el resultado se note: sin filas repetidas por el solape, sin perder lo que el corte parte, con
las páginas del documento entero y no las del bloque.
"""

import itertools
import math

from servicios.bloques import (
    Parcial,
    combinar_datos,
    planear_bloques,
    remapear_bloques,
    unir_capas_ocr,
)

fallas = 0


def ok(texto: str) -> None:
    print(f"  OK  {texto}")


def mal(texto: str) -> None:
    global fallas
    fallas += 1
    print(f"  NO  {texto}")


def caso(titulo: str) -> None:
    print(f"\n{titulo}")


def verificar(condicion: bool, bien: str, mensaje_mal: str) -> None:
    ok(bien) if condicion else mal(mensaje_mal)


def campo(valor, pagina, x=0.1, y=0.1, conf=90.0, bloque=0) -> dict:
    """Un campo hoja con la forma que empaqueta `ia._campo`."""
    return {
        "value_raw": valor,
        "value_normalized": valor,
        "confianza": conf,
        "confianza_cruda": None if conf is None else conf / 100,
        "metodo_confianza": "extractor_confidence",
        "page_number": pagina,
        "bloque_indice": bloque,
        "posicion": {"x": x, "y": y, "ancho": 0.3, "alto": 0.02},
    }


def valores(lista) -> list:
    return [c["value_normalized"] for c in lista]


# ── 1. planear_bloques ───────────────────────────────────────────────────────
caso("[1] planear_bloques: los ejemplos de verdad")
EJEMPLOS = [
    ((1, 15, 1), [(1, 1)]),
    ((15, 15, 1), [(1, 15)]),
    ((16, 15, 1), [(1, 9), (9, 16)]),
    ((20, 15, 1), [(1, 11), (11, 20)]),
    ((29, 15, 1), [(1, 15), (15, 29)]),
    ((30, 15, 1), [(1, 11), (11, 21), (21, 30)]),
    ((20, 15, 0), [(1, 10), (11, 20)]),
    ((21, 15, 0), [(1, 11), (12, 21)]),
    ((10, 5, 1), [(1, 4), (4, 7), (7, 10)]),
]
for (total, tope, solape), esperado in EJEMPLOS:
    real = planear_bloques(total, tope, solape)
    verificar(real == esperado, f"{total} págs, tope {tope}, solape {solape} → {real}", f"{total}/{tope}/{solape}: salió {real}, esperaba {esperado}")

caso("[2] planear_bloques: propiedades, para TODAS las combinaciones de 1-150 páginas, tope 1-15 y solape 0-4")
malos: list[str] = []
for total, tope, solape in itertools.product(range(1, 151), range(1, 16), range(0, 5)):
    bloques = planear_bloques(total, tope, solape)
    efectivo = max(0, min(solape, tope - 1))
    tamanos = [h - d + 1 for d, h in bloques]
    if bloques[0][0] != 1 or bloques[-1][1] != total:
        malos.append(f"{total}/{tope}/{solape}: no cubre de 1 a {total}: {bloques}")
    if any(d > h for d, h in bloques) or max(tamanos) > tope:
        malos.append(f"{total}/{tope}/{solape}: un bloque vacío o más grande que el tope: {bloques}")
    if total > tope:
        for (_, h1), (d2, _) in zip(bloques, bloques[1:]):
            if d2 != h1 - efectivo + 1:
                malos.append(f"{total}/{tope}/{solape}: el solape no es {efectivo}: {bloques}")
                break
        minimo = math.ceil((total - efectivo) / (tope - efectivo))
        if len(bloques) != minimo:
            malos.append(f"{total}/{tope}/{solape}: {len(bloques)} bloques y con {minimo} alcanzaba")
        if tamanos[0] - tamanos[-1] > len(bloques) - 1 or any(t != tamanos[0] for t in tamanos[:-1]):
            malos.append(f"{total}/{tope}/{solape}: tamaños desparejos {tamanos}")
    elif bloques != [(1, total)]:
        malos.append(f"{total}/{tope}/{solape}: cabía y se partió: {bloques}")
verificar(not malos, "cubre todo, nunca excede el tope, el solape es exacto, usa el mínimo de bloques y los parte parejos", f"{len(malos)} combinaciones mal; la primera: {malos[:1]}")

caso("[3] planear_bloques: entradas imposibles")
for args in ((0, 15, 1), (-3, 15, 1), (20, 0, 1)):
    try:
        planear_bloques(*args)
        mal(f"{args} no levantó error")
    except ValueError:
        ok(f"{args} → ValueError")

# ── 4. Campos únicos ─────────────────────────────────────────────────────────
caso("[4] un campo único que está en un solo bloque pasa tal cual")
parciales = [Parcial(1, 11, {"denominacion": campo("AUTOPARTES", 1)}), Parcial(11, 20, {"notario": campo("CHRISTIAN", 19)})]
unido = combinar_datos(parciales)
verificar(set(unido) == {"denominacion", "notario"} and unido["notario"]["page_number"] == 19, "los dos campos están y conservan su página", f"quedó: {list(unido)}")

caso("[5] un campo único en varios bloques: gana la mayor confianza; a igualdad, el bloque más temprano")
unido = combinar_datos([Parcial(1, 11, {"fecha": campo("2024-09-18", 2, conf=80)}), Parcial(11, 20, {"fecha": campo("2024-09-19", 19, conf=95)})])
verificar(unido["fecha"]["value_normalized"] == "2024-09-19", "la de 95 le gana a la de 80 aunque sea del bloque tardío", f"quedó {unido['fecha']['value_normalized']}")
unido = combinar_datos([Parcial(1, 11, {"fecha": campo("2024-09-18", 2, conf=90)}), Parcial(11, 20, {"fecha": campo("2024-09-19", 19, conf=90)})])
verificar(unido["fecha"]["value_normalized"] == "2024-09-18", "con la misma confianza gana el bloque más temprano", f"quedó {unido['fecha']['value_normalized']}")
unido = combinar_datos([Parcial(1, 11, {"fecha": campo("A", 2, conf=None)}), Parcial(11, 20, {"fecha": campo("B", 19, conf=10)})])
verificar(unido["fecha"]["value_normalized"] == "B", "una confianza que Google no mandó (None) pierde contra cualquiera que sí", f"quedó {unido['fecha']['value_normalized']}")

caso("[6] un valor LARGO cortado por el borde del bloque pierde contra el que lo trae completo")
COMPLETO = "FABRICACION, COMPRA Y VENTA DE AUTOPARTES; ALQUILER DE MAQUINARIA; MANTENIMIENTO Y REPARACION DE VEHICULOS"
CORTADO = "FABRICACION, COMPRA Y VENTA DE AUTOPARTES; ALQUILER DE MAQUINARIA"
unido = combinar_datos([Parcial(1, 10, {"objeto": campo(CORTADO, 10, conf=99)}), Parcial(10, 19, {"objeto": campo(COMPLETO, 10, conf=70)})])
verificar(unido["objeto"]["value_normalized"] == COMPLETO, "gana el completo aunque tenga menos confianza (el otro es el pedazo que cortó el borde)", f"quedó {unido['objeto']['value_normalized'][:50]!r}")
unido = combinar_datos([Parcial(1, 10, {"objeto": campo(COMPLETO, 10, conf=70)}), Parcial(10, 19, {"objeto": campo(CORTADO.split(";")[-1].strip() + " " + "X" * 12, 10, conf=99)})])
verificar(unido["objeto"]["value_normalized"].startswith("ALQUILER"), "si NO es un pedazo del otro, decide la confianza", f"quedó {unido['objeto']['value_normalized'][:50]!r}")
unido = combinar_datos([Parcial(1, 10, {"objeto": campo(CORTADO, 4, conf=99)}), Parcial(10, 19, {"objeto": campo(COMPLETO, 12, conf=70)})])
verificar(unido["objeto"]["value_normalized"] == CORTADO, "fuera de una página de solape no se aplica la regla: decide la confianza", f"quedó {unido['objeto']['value_normalized'][:50]!r}")
unido = combinar_datos([Parcial(1, 10, {"nombre": campo("JUAN PEREZ", 10, conf=99)}), Parcial(10, 19, {"nombre": campo("JUAN PEREZ MARTINEZ", 10, conf=70)})])
verificar(unido["nombre"]["value_normalized"] == "JUAN PEREZ", "un valor corto no se descarta por estar contenido en otro (nombres y fechas no se cortan así)", f"quedó {unido['nombre']['value_normalized']!r}")

# ── 7. Campos múltiples ──────────────────────────────────────────────────────
caso("[7] un campo múltiple: se concatenan las listas en el orden del documento")
est_multiple = {"socios": {"obligatorio": False, "multiple": True}}
unido = combinar_datos(
    [Parcial(1, 11, {"socios": [campo("ANA", 3, y=0.2), campo("BETO", 5, y=0.3)]}), Parcial(11, 20, {"socios": [campo("CARLA", 15, y=0.4)]})],
    est_multiple,
)
verificar(valores(unido["socios"]) == ["ANA", "BETO", "CARLA"], "ANA, BETO (bloque 1) y CARLA (bloque 2), en ese orden", f"quedó {valores(unido['socios'])}")

caso("[8] lo que el solape mostró dos veces no se repite (la caja no sale idéntica entre dos llamadas)")
unido = combinar_datos(
    [Parcial(1, 11, {"socios": [campo("ANA", 11, x=0.100, y=0.200)]}), Parcial(11, 20, {"socios": [campo("ANA", 11, x=0.104, y=0.197), campo("BETO", 12)]})],
    est_multiple,
)
verificar(valores(unido["socios"]) == ["ANA", "BETO"], "ANA aparece una vez; BETO se agrega", f"quedó {valores(unido['socios'])}")

caso("[9] dos datos iguales en OTRO lugar SÍ son dos datos (otra página, o la misma página lejos)")
unido = combinar_datos(
    [Parcial(1, 11, {"socios": [campo("ANA", 3, y=0.2)]}), Parcial(11, 20, {"socios": [campo("ANA", 15, y=0.2), campo("ANA", 15, y=0.8)]})],
    est_multiple,
)
verificar(valores(unido["socios"]) == ["ANA", "ANA", "ANA"], "ANA en la pág 3, y dos veces en la pág 15 (una arriba, otra abajo): tres", f"quedó {valores(unido['socios'])}")
unido = combinar_datos(
    [Parcial(1, 11, {"socios": [campo("ANA", 11, y=0.2)]}), Parcial(11, 20, {"socios": [campo("ANA", 11, y=0.8)]})],
    est_multiple,
)
verificar(valores(unido["socios"]) == ["ANA", "ANA"], "ANA arriba y ANA abajo en la misma página, vistas por bloques distintos: son dos (no la misma entidad)", f"quedó {valores(unido['socios'])}")

caso("[9c] en el MISMO lugar pero con texto distinto tampoco es la misma entidad: se conservan las dos lecturas (no se pierde nada)")
unido = combinar_datos(
    [Parcial(1, 11, {"socios": [campo("ANA MARIA", 11, y=0.2)]}), Parcial(11, 20, {"socios": [campo("ANA MARIA LOPEZ", 11, y=0.2)]})],
    est_multiple,
)
verificar(valores(unido["socios"]) == ["ANA MARIA", "ANA MARIA LOPEZ"], "las dos lecturas quedan", f"quedó {valores(unido['socios'])}")

caso("[9b] dos valores idénticos del MISMO bloque son dos datos (solo se compara contra bloques anteriores)")
unido = combinar_datos([Parcial(1, 11, {"socios": [campo("ANA", 3, y=0.2), campo("ANA", 3, y=0.2)]})], est_multiple)
verificar(valores(unido["socios"]) == ["ANA", "ANA"], "siguen siendo dos", f"quedó {valores(unido['socios'])}")

caso("[10] sin estructura, una lista que llegó en un bloque hace al campo múltiple")
unido = combinar_datos([Parcial(1, 11, {"socios": [campo("ANA", 3)]}), Parcial(11, 20, {"socios": [campo("CARLA", 15)]})])
verificar(valores(unido["socios"]) == ["ANA", "CARLA"], "se infiere múltiple por la forma", f"quedó {unido['socios']}")

# ── 11. Grupos ───────────────────────────────────────────────────────────────
HIJOS = {"nombre": {"obligatorio": True, "multiple": False}, "acciones": {"obligatorio": True, "multiple": False}, "monto": {"obligatorio": True, "multiple": False}}
est_grupo = {"accionistas": {"obligatorio": True, "multiple": True, "hijos": HIJOS}}


def fila(pagina, y, **columnas) -> dict:
    return {k: campo(v, pagina, y=y + 0.01 * i) for i, (k, v) in enumerate(columnas.items())}


caso("[11] un grupo múltiple: las filas de todos los bloques, en orden")
unido = combinar_datos(
    [
        Parcial(1, 11, {"accionistas": [fila(8, 0.2, nombre="JUAN", acciones="35", monto="466")]}),
        Parcial(11, 20, {"accionistas": [fila(18, 0.2, nombre="MARIA", acciones="15", monto="200")]}),
    ],
    est_grupo,
)
verificar([f["nombre"]["value_normalized"] for f in unido["accionistas"]] == ["JUAN", "MARIA"], "JUAN (pág 8) y MARIA (pág 18)", f"quedó {unido['accionistas']}")

caso("[12] la fila que cruza el corte: el bloque 1 la vio PARTIDA y el 2 COMPLETA → una sola fila con las tres columnas")
parcial_1 = {"nombre": campo("CARLOS", 11, y=0.9), "acciones": campo("25", 11, y=0.92)}
completa_2 = {"nombre": campo("CARLOS", 11, x=0.103, y=0.898), "acciones": campo("25", 11, x=0.1, y=0.921), "monto": campo("333", 12, y=0.05)}
unido = combinar_datos(
    [
        Parcial(1, 11, {"accionistas": [fila(8, 0.2, nombre="JUAN", acciones="35", monto="466"), parcial_1]}),
        Parcial(11, 20, {"accionistas": [completa_2, fila(14, 0.3, nombre="MARIA", acciones="15", monto="200")]}),
    ],
    est_grupo,
)
filas = unido["accionistas"]
verificar([f["nombre"]["value_normalized"] for f in filas] == ["JUAN", "CARLOS", "MARIA"], "JUAN, CARLOS (una vez), MARIA: sin repetir a CARLOS", f"quedó {[f.get('nombre', {}).get('value_normalized') for f in filas]}")
verificar(list(filas[1]) == ["nombre", "acciones", "monto"] and filas[1]["monto"]["page_number"] == 12, "CARLOS trae nombre, acciones y el monto de la página siguiente, en el orden del esquema", f"quedó {list(filas[1])}")

caso("[12b] las columnas de una fila unida salen en el orden del ESQUEMA, aunque la unión agregue una anterior a las que ya había")
cola_1 = {"acciones": campo("25", 11, y=0.92), "monto": campo("333", 12, y=0.05)}
completa_b = {"nombre": campo("CARLOS", 11, y=0.9), "acciones": campo("25", 11, x=0.1, y=0.921), "monto": campo("333", 12, y=0.05)}
unido = combinar_datos([Parcial(1, 11, {"accionistas": [cola_1]}), Parcial(11, 20, {"accionistas": [completa_b]})], est_grupo)
verificar(len(unido["accionistas"]) == 1 and list(unido["accionistas"][0]) == ["nombre", "acciones", "monto"], "nombre, acciones, monto (el bloque 1 no traía el nombre y la unión lo agregó al final)", f"quedó {[list(f) for f in unido['accionistas']]}")

caso("[13] la unión funciona igual si lo completo lo vio el bloque PRIMERO y lo partido el segundo")
unido = combinar_datos(
    [
        Parcial(1, 11, {"accionistas": [{**completa_2}]}),
        Parcial(11, 20, {"accionistas": [{"nombre": campo("CARLOS", 11, y=0.9), "acciones": campo("25", 11, y=0.92)}]}),
    ],
    est_grupo,
)
verificar(len(unido["accionistas"]) == 1 and "monto" in unido["accionistas"][0], "una sola fila y conserva el monto", f"quedó {unido['accionistas']}")

caso("[14] dos filas que se parecen pero NO son la misma no se unen")
a = fila(5, 0.2, nombre="LUIS", acciones="10", monto="1")
b_mismo_nombre_otra_pagina = {"nombre": campo("LUIS", 15, y=0.2), "acciones": campo("10", 15, y=0.22)}
b_nombre_igual_acciones_distintas = {"nombre": campo("LUIS", 5, y=0.2), "acciones": campo("99", 5, y=0.5)}
b_sin_columnas_en_comun = {"monto": campo("7", 15, y=0.6)}
a_sin_monto = {"nombre": campo("LUIS", 5, y=0.2), "acciones": campo("10", 5, y=0.22)}
for etiqueta, previa, otra in (
    ("el mismo nombre en otra página (otra persona con el mismo nombre)", a, b_mismo_nombre_otra_pagina),
    ("el mismo nombre pero otras acciones (hay un desacuerdo: se conservan las dos)", a, b_nombre_igual_acciones_distintas),
    ("sin ninguna columna en común (una trae nombre y acciones; la otra, solo el monto)", a_sin_monto, b_sin_columnas_en_comun),
):
    unido = combinar_datos([Parcial(1, 11, {"accionistas": [previa]}), Parcial(11, 20, {"accionistas": [otra]})], est_grupo)
    verificar(len(unido["accionistas"]) == 2, etiqueta, f"{etiqueta}: quedaron {len(unido['accionistas'])}")

caso("[15] dos filas del MISMO bloque no se unen entre sí, aunque calcen")
igual_1 = {"nombre": campo("LUIS", 5, y=0.2)}
igual_2 = {"nombre": campo("LUIS", 5, y=0.2)}
unido = combinar_datos([Parcial(1, 11, {"accionistas": [igual_1, igual_2]})], est_grupo)
verificar(len(unido["accionistas"]) == 2, "siguen siendo dos filas (solo se une contra bloques anteriores)", f"quedaron {len(unido['accionistas'])}")

caso("[16] un grupo único (un domicilio): gana el que trae más subcampos")
est_domicilio = {"domicilio": {"obligatorio": False, "multiple": False, "hijos": {"calle": {"obligatorio": False, "multiple": False}}}}
chico = {"calle": campo("CALLE X", 2, conf=99)}
grande = {"calle": campo("CALLE X", 2, conf=60), "ciudad": campo("LEON", 2, conf=60)}
unido = combinar_datos([Parcial(1, 11, {"domicilio": chico}), Parcial(11, 20, {"domicilio": grande})], est_domicilio)
verificar(set(unido["domicilio"]) == {"calle", "ciudad"}, "el de dos subcampos le gana al de uno aunque tenga menos confianza", f"quedó {list(unido['domicilio'])}")

caso("[17] sin bloques, no hay nada que unir")
verificar(combinar_datos([]) == {}, "{}", "no devolvió {}")

# ── 18. remapear_bloques ─────────────────────────────────────────────────────
caso("[18] remapear_bloques: reapunta cada campo (sueltos, listas y filas) al índice de la capa unida")
datos = {
    "a": campo("x", 1, bloque=0),
    "lista": [campo("y", 1, bloque=1), campo("z", 1, bloque=7)],
    "grupo": [{"c1": campo("w", 1, bloque=2), "c2": campo("v", 1, bloque=None)}],
}
remapear_bloques(datos, [10, None, 30])
verificar(datos["a"]["bloque_indice"] == 10, "0 → 10", f"quedó {datos['a']['bloque_indice']}")
verificar(datos["lista"][0]["bloque_indice"] is None, "un bloque duplicado sin equivalente (mapa None) queda en None", f"quedó {datos['lista'][0]['bloque_indice']}")
verificar(datos["lista"][1]["bloque_indice"] is None, "un índice fuera del mapa queda en None", f"quedó {datos['lista'][1]['bloque_indice']}")
verificar(datos["grupo"][0]["c1"]["bloque_indice"] == 30 and datos["grupo"][0]["c2"]["bloque_indice"] is None, "dentro de las filas de un grupo también", f"quedó {datos['grupo']}")


# ── 19. Capa de OCR ──────────────────────────────────────────────────────────
def renglon(indice, pagina, texto, x=0.1, y=0.1) -> dict:
    return {"indice": indice, "page_number": pagina, "block_type": "line", "texto": texto, "bbox": {"x": x, "y": y, "ancho": 0.5, "alto": 0.02}, "confianza": 99.0, "orientacion": None, "orientacion_grados": None}


def pagina(n) -> dict:
    return {"page_number": n, "ancho": 612, "alto": 792, "unidad": "point", "idioma": "es"}


caso("[19] unir_capas_ocr: páginas del documento entero, sin duplicar la del solape, y el mapa para reapuntar los campos")
capa_1 = {"engine": "document_ai", "engine_version": "v1", "page_count": 3, "language": "es", "paginas": [pagina(1), pagina(2), pagina(3)],
          "bloques": [renglon(0, 1, "uno"), renglon(1, 2, "dos"), renglon(2, 3, "tres", y=0.1), renglon(3, 3, "tres", y=0.5)]}
capa_2 = {"engine": "document_ai", "engine_version": "v1", "page_count": 3, "language": "es", "paginas": [pagina(3), pagina(4), pagina(5)],
          "bloques": [renglon(0, 3, "tres", x=0.103, y=0.503), renglon(1, 3, "tres", x=0.1, y=0.1), renglon(2, 4, "cuatro"), renglon(3, 5, "cinco"), renglon(4, 3, "renglon que solo vio el bloque 2", y=0.9)]}
unida, mapas = unir_capas_ocr([capa_1, capa_2], 5)
verificar([p["page_number"] for p in unida["paginas"]] == [1, 2, 3, 4, 5], "páginas 1 a 5, la 3 una sola vez", f"quedó {[p['page_number'] for p in unida['paginas']]}")
verificar(unida["page_count"] == 5, "page_count es el del documento entero (5), no el de un bloque (3)", f"quedó {unida['page_count']}")
verificar([b["indice"] for b in unida["bloques"]] == list(range(len(unida["bloques"]))), "los índices de los renglones son consecutivos", f"quedó {[b['indice'] for b in unida['bloques']]}")
verificar([b["texto"] for b in unida["bloques"]] == ["uno", "dos", "tres", "tres", "cuatro", "cinco"], "los renglones de la pág 3 salen una vez (los del primer bloque)", f"quedó {[b['texto'] for b in unida['bloques']]}")
verificar(mapas[0] == [0, 1, 2, 3], "mapa del bloque 1: identidad", f"quedó {mapas[0]}")
verificar(mapas[1][:4] == [3, 2, 4, 5], "mapa del bloque 2: cada «tres» de la pág 3 apunta a SU equivalente del bloque 1 —el de abajo, aunque la caja se movió 0.003, y el de arriba—; lo demás, a los nuevos", f"quedó {mapas[1]}")
verificar(mapas[1][4] is None, "un renglón de la pág repetida que el primer bloque no leyó apunta a None", f"quedó {mapas[1][4]}")
verificar(unida["engine"] == "document_ai" and unida["engine_version"] == "v1" and unida["language"] == "es", "engine, versión e idioma se conservan", f"quedó {unida}")
unida_7, _ = unir_capas_ocr([capa_1, capa_2], 7)
verificar(unida_7["page_count"] == 7, "page_count es lo que se le dijo del documento (7) aunque los bloques solo devolvieran 5 páginas", f"quedó {unida_7['page_count']}")
vacia, mapas_vacios = unir_capas_ocr([], 0)
verificar(vacia["bloques"] == [] and mapas_vacios == [], "sin capas, una capa vacía", f"quedó {vacia}")

print()
if fallas:
    print(f"{fallas} caso(s) NO cuadran")
    raise SystemExit(1)
print("todo cuadra")
