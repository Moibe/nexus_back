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

from servicios.ia import _valor_normalizado

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


def main() -> int:
    fallos = 0
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

    print()
    if fallos:
        print(f"{fallos} de {len(CASOS)} casos FALLARON")
        return 1
    print(f"los {len(CASOS)} casos pasaron")
    return 0


if __name__ == "__main__":
    sys.exit(main())
