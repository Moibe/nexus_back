"""Verifica la normalización de valores del extractor. Correr a mano:

    venv/bin/python verificar_normalizacion.py

No usa pytest a propósito: el proyecto no tiene framework de pruebas todavía y
esto no justifica meter una dependencia. Es un script que se corre y dice sí o
no.

Qué protege y por qué importa: `_valor_normalizado()` decide entre el valor
normalizado de Google y el `mentionText` crudo con UNA condición de tres líneas
(`if anio and mes and dia`). De esa condición dependen dos garantías que, si se
rompen, fallan EN SILENCIO — sin excepción, sin log, sin alerta. Solo se
descubrirían auditando datos ya guardados.

El esquema del procesador declara varios campos con tipos que invitan justo a
esos errores; ver docs/esquema-procesador-ine.md.
"""

import sys

from servicios.ia import _descomponer_anio_registro, _valor_normalizado

# (descripción, entidad tal como la manda Document AI, valor esperado)
CASOS = [
    (
        "clave del padrón con ceros a la izquierda (localidad)",
        # El esquema declara `localidad` como Número. Si algún día se confía en
        # la normalización numérica de Google, '0181' se vuelve 181 y se corrompe
        # una clave del padrón electoral sin que nada avise.
        {"mentionText": "0181", "normalizedValue": {"integerValue": 181}},
        "0181",
    ),
    (
        "municipio con cero a la izquierda",
        {"mentionText": "064", "normalizedValue": {"integerValue": 64}},
        "064",
    ),
    (
        "código postal que empieza con cero (caso CDMX)",
        {"mentionText": "01000", "normalizedValue": {"integerValue": 1000}},
        "01000",
    ),
    (
        "año suelto declarado como Fecha y hora (emision)",
        # `emision` y `vigencia` son SOLO el año en la credencial. Convertirlos a
        # fecha completa inventaría un mes y un día que no están impresos.
        {"mentionText": "2015", "normalizedValue": {"dateValue": {"year": 2015}}},
        "2015",
    ),
    (
        "vigencia como año suelto",
        {"mentionText": "2025", "normalizedValue": {"dateValue": {"year": 2025}}},
        "2025",
    ),
    (
        "fecha con año y mes pero sin día",
        {
            "mentionText": "05/2015",
            "normalizedValue": {"dateValue": {"year": 2015, "month": 5}},
        },
        "05/2015",
    ),
    (
        "fecha COMPLETA sí se pasa a ISO (fecha_nacimiento)",
        {
            "mentionText": "08/05/1997",
            "normalizedValue": {"dateValue": {"year": 1997, "month": 5, "day": 8}},
        },
        "1997-05-08",
    ),
    (
        "fecha completa con mes y día de un dígito se rellena a 2",
        {
            "mentionText": "1/2/2020",
            "normalizedValue": {"dateValue": {"year": 2020, "month": 2, "day": 1}},
        },
        "2020-02-01",
    ),
    (
        "valor con basura pegada que el esquema declara Número (fecha_registro)",
        {"mentionText": "2015 00", "normalizedValue": {"integerValue": 2015}},
        "2015 00",
    ),
    (
        "texto normal sin normalización",
        {"mentionText": "AV TECNOLOGICO 32"},
        "AV TECNOLOGICO 32",
    ),
    (
        "entidad sin mentionText",
        {"normalizedValue": {"text": "algo"}},
        None,
    ),
]


# "AÑO DE REGISTRO" trae dos datos pegados. Los separadores raros salieron de
# medir 44 INEs reales; los casos límite de abajo son los que romperían el
# parseo si alguien "simplifica" la expresión regular.
CASOS_ANIO_REGISTRO = [
    ("separador normal", "2024 00", "2024", "00"),
    ("credencial vieja con reposiciones", "1991 03", "1991", "03"),
    ("OCR metió un punto en vez del espacio", "2018.01", "2018", "01"),
    ("OCR metió coma y espacio", "2010, 01", "2010", "01"),
    ("OCR se comió el separador", "201102", "2011", "02"),
    ("espacios de sobra alrededor", "  2005 01  ", "2005", "01"),
]

# Estos NO deben partirse: dejar el crudo es mejor que inventar una partición.
CASOS_NO_PARTIR = [
    ("solo el año, sin contador", "2024"),
    ("texto que no es el patrón", "AÑO DE REGISTRO"),
    ("año de 2 dígitos", "24 00"),
    ("tres grupos", "2024 00 07"),
]


def probar_anio_registro() -> int:
    fallos = 0
    print()
    print("--- descomposición de AÑO DE REGISTRO ---")
    for desc, crudo, anio, emision in CASOS_ANIO_REGISTRO:
        campo = {"value_raw": crudo, "value_normalized": crudo}
        r = _descomponer_anio_registro({"fecha_registro": campo})["fecha_registro"]
        bien = (
            r.get("value_normalized") == anio
            and r.get("numero_emision") == emision
            and r.get("value_raw") == crudo  # el crudo NUNCA se toca
        )
        if not bien:
            fallos += 1
            print(f"  FALLA {desc}: {crudo!r} -> {r}")
        else:
            print(f"  OK  {desc}: {crudo!r} -> año={anio} emision={emision}")

    for desc, crudo in CASOS_NO_PARTIR:
        campo = {"value_raw": crudo, "value_normalized": crudo}
        r = _descomponer_anio_registro({"fecha_registro": campo})["fecha_registro"]
        bien = "numero_emision" not in r and r["value_normalized"] == crudo
        if not bien:
            fallos += 1
            print(f"  FALLA {desc}: {crudo!r} NO debía partirse -> {r}")
        else:
            print(f"  OK  {desc}: {crudo!r} se deja intacto")
    return fallos


def main() -> int:
    fallos = 0
    print("--- normalización de valores ---")
    for descripcion, entidad, esperado in CASOS:
        obtenido = _valor_normalizado(entidad)
        bien = obtenido == esperado
        if not bien:
            fallos += 1
        print(
            f"  {'OK ' if bien else 'FALLA'} {descripcion}\n"
            f"        esperado={esperado!r} obtenido={obtenido!r}"
            if not bien
            else f"  OK  {descripcion}  -> {obtenido!r}"
        )

    fallos += probar_anio_registro()

    print()
    if fallos:
        print(f"{fallos} casos FALLARON")
        return 1
    print(f"los {len(CASOS) + len(CASOS_ANIO_REGISTRO) + len(CASOS_NO_PARTIR)} casos pasaron")
    return 0


if __name__ == "__main__":
    sys.exit(main())
