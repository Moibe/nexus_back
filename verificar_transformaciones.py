"""La «Regla de normalización» de cada campo (`servicios/transformaciones.py`, 2026-11-26). Pura: sin base, sin red.

    .venv/Scripts/python verificar_transformaciones.py
"""

import sys

from servicios import transformaciones as T
from servicios.datos_extraidos import aplanar

sys.stdout.reconfigure(encoding="utf-8")
fallas = 0


def rev(que: str, ok: bool, extra: object = "") -> None:
    global fallas
    fallas += not ok
    print(f"  {'OK   ' if ok else 'FALLA'} {que}{'' if ok else f'  -> {extra!r}'}")


def igual(regla: str, casos: dict[str, str | None], campo: dict | None = None) -> None:
    for entrada, esperado in casos.items():
        salida = T.aplicar(regla, entrada, campo)
        rev(f"{regla}: «{entrada}» → «{esperado}»", salida == esperado, salida)


print("fecha_iso")
igual("fecha_iso", {
    "15/03/2026": "2026-03-15",
    "5/3/2026": "2026-03-05",
    "15-03-26": "2026-03-15",
    "01.12.1985": "1985-12-01",
    "15/03/85": "1985-03-15",
    "2026-03-15": "2026-03-15",
    "2026/3/5": "2026-03-05",
    "20260315": "2026-03-15",
    "15 de marzo de 2026": "2026-03-15",
    "15 DE MARZO DEL 2026": "2026-03-15",
    "15-MAR-2026": "2026-03-15",
    "15 mar. 2026": "2026-03-15",
    "1 de septiembre de 2025": "2025-09-01",
    "1 sept 2025": "2025-09-01",
    "21 Ago 2024": "2024-08-21",
    "marzo 15, 2026": "2026-03-15",
    "2026-03-15T10:30:00Z": "2026-03-15",
    "15/03/2026 10:30 hrs": "2026-03-15",
    "31/02/2026": "31/02/2026",
    "sin fecha": "sin fecha",
    "15 de marzzo de 2026": "15 de marzzo de 2026",
    "13/2026": "13/2026",
})

print("quitar_moneda")
igual("quitar_moneda", {
    "$1,234.50": "1234.50",
    "$ 1,234.50 MXN": "1234.50",
    "MXN 25,000": "25000",
    "1.234,50 €": "1234.50",
    "US$ 99.99": "99.99",
    "$1,000,000.00 M.N.": "1000000.00",
    "(1,000.00)": "-1000.00",
    "-$500": "-500",
    "500 pesos": "500",
    "1500": "1500",
    "$0.00": "0.00",
    "Cinco mil pesos": "Cinco mil",
})

print("mayúsculas / minúsculas / trim")
igual("mayusculas", {"Juan Pérez": "JUAN PÉREZ", "ñandú": "ÑANDÚ"})
igual("cambiar_caja", {"Juan Pérez": "JUAN PÉREZ"})
igual("minusculas", {"JUAN@CORREO.COM": "juan@correo.com", "ÁNGEL": "ángel"})
igual("trim_espacios", {"  JUAN   PÉREZ \n LÓPEZ  ": "JUAN PÉREZ LÓPEZ", "uno\tdos": "uno dos", "   ": "   "})

print("valor_canonico")
lista = {"valoresLista": ["Masculino", "Femenino", "No binario"]}
igual("valor_canonico", {
    "masculino": "Masculino",
    "MASC": "Masculino",
    "M.": "Masculino",
    "f": "Femenino",
    "  femenino ": "Femenino",
    "no-binario": "No binario",
    "N": "No binario",
    "X": "X",
    "": "",
}, lista)
igual("valor_canonico", {"Masc": "Masc"}, {"valoresLista": []})
igual("valor_canonico", {"Ca": "Ca"}, {"valoresLista": ["Casado", "Católico"]})  # ambiguo: se queda
igual("valor_canonico", {"cas": "Casado"}, {"valoresLista": ["Casado", "Católico"]})

print("sin regla, regla desconocida, sin valor")
rev("sin regla: igual", T.aplicar("", "  X ") == "  X ")
rev("regla que no existe: igual", T.aplicar("inventada", "x") == "x")
rev("None se queda None", T.aplicar("fecha_iso", None) is None)

print("a los renglones de una extracción")
resultado = {
    "fecha_de_nacimiento": {"value_raw": "15/03/1985", "value_normalized": "15/03/1985", "confianza": 95.0, "metodo_confianza": "extractor_confidence", "page_number": 1},
    "monto": {"value_raw": "$1,500.00 MXN", "value_normalized": None, "confianza": 90.0, "metodo_confianza": "extractor_confidence", "page_number": 1},
    "sexo": {"value_raw": "M", "value_normalized": "M", "confianza": 99.0, "metodo_confianza": "extractor_confidence", "page_number": 1},
    "telefonos": [
        {"value_raw": " 55 1234  5678 ", "value_normalized": " 55 1234  5678 ", "confianza": 90.0, "metodo_confianza": "extractor_confidence", "page_number": 1},
        {"value_raw": "55 8765 4321", "value_normalized": "55 8765 4321", "confianza": 90.0, "metodo_confianza": "extractor_confidence", "page_number": 2},
    ],
    "socios": [{"nombre": {"value_raw": "ana", "value_normalized": "ana", "confianza": 90.0, "metodo_confianza": "extractor_confidence", "page_number": 3}}],
    "nombre": {"value_raw": "Juan", "value_normalized": "Juan", "confianza": 99.0, "metodo_confianza": "extractor_confidence", "page_number": 1},
    "_metadata": {"campos_ausentes": ["vigencia"]},
}
campos = [
    {"nombre": "Fecha de nacimiento", "tipoDato": "fecha", "reglaTransformacion": "fecha_iso"},
    {"nombre": "Monto", "tipoDato": "moneda", "reglaTransformacion": "quitar_moneda"},
    {"nombre": "Sexo", "tipoDato": "lista", "reglaTransformacion": "valor_canonico", "valoresLista": ["Masculino", "Femenino"]},
    {"nombre": "Teléfonos", "tipoDato": "texto", "cardinalidad": "multiple", "reglaTransformacion": "trim_espacios"},
    {"nombre": "Socios", "tipoDato": "grupo", "reglaTransformacion": "mayusculas", "subcampos": [{"nombre": "Nombre", "tipoDato": "texto"}]},
    {"nombre": "Nombre", "tipoDato": "texto", "reglaTransformacion": ""},
    {"nombre": "Vigencia", "tipoDato": "fecha", "reglaTransformacion": "fecha_iso"},
]
import copy
original = copy.deepcopy(resultado)
n = T.aplicar_a_resultado(resultado, campos)
renglones = aplanar(resultado)
por = {r["fieldPath"]: r for r in renglones}
rev("cambiaron 4 valores (fecha, monto, sexo, el primer teléfono)", n == 4, n)
rev("fecha: normalizada a ISO y lo leído en valueRaw", por["fecha_de_nacimiento"]["valueNormalized"] == "1985-03-15" and por["fecha_de_nacimiento"]["valueRaw"] == "15/03/1985", por["fecha_de_nacimiento"])
rev("monto sin value_normalized: se transforma lo crudo y valueRaw lo conserva", por["monto"]["valueNormalized"] == "1500.00" and por["monto"]["valueRaw"] == "$1,500.00 MXN", por["monto"])
rev("lista: «M» → «Masculino»", por["sexo"]["valueNormalized"] == "Masculino" and por["sexo"]["valueRaw"] == "M")
rev("un campo que se repite: cada valor con su regla", por["telefonos[1]"]["valueNormalized"] == "55 1234 5678" and por["telefonos[2]"]["valueNormalized"] == "55 8765 4321")
rev("el subcampo de un grupo no tiene regla (aunque el grupo traiga una)", por["socios[1].nombre"]["valueNormalized"] == "ana")
rev("sin regla: igual", por["nombre"]["valueNormalized"] == "Juan")
rev("un ausente sigue ausente", por["vigencia"]["valueNormalized"] is None and por["vigencia"]["nullReason"] == "not_present")
rev("la respuesta (lo que ve la bandeja) queda transformada y su value_raw intacto", resultado["fecha_de_nacimiento"]["value_normalized"] == "1985-03-15" and resultado["fecha_de_nacimiento"]["value_raw"] == "15/03/1985" and resultado["monto"]["value_raw"] == "$1,500.00 MXN")
otra = copy.deepcopy(original)
rev("sin campos con regla: 0 y nada cambia", T.aplicar_a_resultado(otra, [{"nombre": "Nombre", "tipoDato": "texto"}]) == 0 and otra == original)
rev("respuesta que no es diccionario: 0", T.aplicar_a_resultado(None, campos) == 0)
dos = {"ano": {"value_raw": "a", "value_normalized": "a", "metodo_confianza": "extractor_confidence"}, "ano_2": {"value_raw": "b", "value_normalized": "b", "metodo_confianza": "extractor_confidence"}}
n2 = T.aplicar_a_resultado(dos, [{"nombre": "Año", "tipoDato": "texto", "reglaTransformacion": ""}, {"nombre": "ano", "tipoDato": "texto", "reglaTransformacion": "mayusculas"}])
rev("dos campos con el mismo nombre normalizado («Año» y «ano») van con su sufijo, como en el esquema", n2 == 1 and dos["ano"]["value_normalized"] == "a" and dos["ano_2"]["value_normalized"] == "B", dos)

print("\nTODO BIEN" if not fallas else f"\n{fallas} FALLA(S)")
sys.exit(1 if fallas else 0)
