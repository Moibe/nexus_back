"""Verificación de `servicios.filas_partidas`: un registro que Document AI partió en varias filas se vuelve a unir, y nada más.
Sin red ni base: filas armadas a mano y una respuesta de Document AI de mentiras para el camino completo.

Correr con:  python verificar_filas_partidas.py
Mismo espíritu que los otros verificar_*.py: casos concretos, salida legible, exit code 1 si algo no cuadra.

El caso que lo originó (2026-11-13): un acta de 10 páginas con 3 accionistas salió con 4 filas; Carlos quedó partido entre la
página 8 (acciones y aportación) y la 9 (nacimiento, estado civil, ciudad).
"""

import asyncio

from servicios import ia
from servicios.datos_extraidos import aplanar
from servicios.filas_partidas import identifica, unir_filas_partidas

fallas = 0


def ok(texto: str) -> None:
    print(f"  OK  {texto}")


def mal(texto: str) -> None:
    global fallas
    fallas += 1
    print(f"  NO  {texto}")


def caso(titulo: str) -> None:
    print(f"\n{titulo}")


def c(valor: str, confianza: float = 99.0, pagina: int = 1) -> dict:
    """Un campo como lo empaqueta `ia._campo`."""
    return {
        "value_raw": valor,
        "value_normalized": valor,
        "confianza": confianza,
        "confianza_cruda": confianza / 100,
        "metodo_confianza": "extractor_confidence",
        "page_number": pagina,
        "bloque_indice": None,
        "posicion": None,
    }


def valores(fila: dict) -> dict:
    return {k: v["value_normalized"] for k, v in fila.items()}


HIJOS_ACTA = {
    h: {"obligatorio": True, "multiple": False}
    for h in (
        "nombre_accionista",
        "acciones_suscritas",
        "valor_aportacion",
        "ciudad_nacimiento",
        "estado_nacimiento",
        "fecha_nacimiento",
        "estado_civil",
        "ocupacion",
        "calle_domicilio",
        "numero_exterior_domicilio",
        "colonia_domicilio",
        "ciudad_domicilio",
        "estado_domicilio",
    )
}
ESTRUCTURA_ACTA = {
    "denominacion_social": {"obligatorio": True, "multiple": False},
    "accionista": {"obligatorio": True, "multiple": True, "hijos": HIJOS_ACTA},
}


def acta() -> dict:
    """Las cuatro filas tal como las dio Document AI (valores y páginas del acta real, abreviada)."""
    return {
        "denominacion_social": c("AUTOPARTES PREMIUM", 99.0, 1),
        "accionista": [
            {"nombre_accionista": c("CARLOS RAMÍREZ LÓPEZ", 100.0, 8), "acciones_suscritas": c("25", 100.0, 8), "valor_aportacion": c("$333,333.33", 100.0, 8)},
            {
                "nombre_accionista": c("JUAN PÉREZ MARTÍNEZ", 99.98, 8),
                "acciones_suscritas": c("35", 99.99, 8),
                "valor_aportacion": c("$466,666.7", 99.99, 8),
                "ciudad_nacimiento": c("Monterrey", 99.95, 8),
                "fecha_nacimiento": c("18 de septiembre de 1975", 99.73, 8),
            },
            {
                "nombre_accionista": c("MARÍA GONZÁLEZ SÁNCHEZ", 99.99, 8),
                "acciones_suscritas": c("15", 100.0, 8),
                "valor_aportacion": c("$200,000.00", 100.0, 8),
                "ciudad_nacimiento": c("Monterrey", 99.99, 8),
                "estado_civil": c("casada", 99.97, 8),
            },
            {
                "nombre_accionista": c("CARLOS RAMÍREZ LÓPEZ", 99.92, 9),
                "ciudad_nacimiento": c("Monterrey", 99.87, 9),
                "estado_nacimiento": c("Nuevo León", 99.83, 9),
                "fecha_nacimiento": c("23 de diciembre de 1990", 98.51, 9),
                "estado_civil": c("casado", 86.9, 9),
                "ciudad_domicilio": c("Monterrey", 98.68, 9),
            },
        ],
    }


# ─────────────────────────────────────────────────────────────────────────────
caso("[1] El acta: Carlos, partido entre las páginas 8 y 9, vuelve a ser UNA fila")
datos, unidas = unir_filas_partidas(acta(), ESTRUCTURA_ACTA)
filas = datos["accionista"]
nombres = [f["nombre_accionista"]["value_normalized"] for f in filas]
if nombres == ["CARLOS RAMÍREZ LÓPEZ", "JUAN PÉREZ MARTÍNEZ", "MARÍA GONZÁLEZ SÁNCHEZ"]:
    ok("tres accionistas, en el orden del documento (Carlos en el lugar de su primera fila)")
else:
    mal(f"quedaron: {nombres}")
carlos = valores(filas[0])
if carlos.get("acciones_suscritas") == "25" and carlos.get("fecha_nacimiento") == "23 de diciembre de 1990" and carlos.get("ciudad_domicilio") == "Monterrey":
    ok("la fila de Carlos trae lo de las dos mitades: sus 25 acciones y su nacimiento")
else:
    mal(f"Carlos quedó: {carlos}")
if len(filas[0]) == 8:
    ok("8 datos: 3 de la página 8 + 6 de la 9, con el nombre una sola vez")
else:
    mal(f"Carlos trae {len(filas[0])} datos: {list(filas[0])}")
if list(filas[0]) == [h for h in HIJOS_ACTA if h in filas[0]]:
    ok("las columnas quedan en el orden del esquema")
else:
    mal(f"orden: {list(filas[0])}")
if filas[0]["acciones_suscritas"]["page_number"] == 8 and filas[0]["estado_civil"]["page_number"] == 9:
    ok("cada dato conserva su página (acciones en la 8, estado civil en la 9)")
else:
    mal("se perdió la página de origen de algún dato")
if filas[0]["nombre_accionista"]["confianza"] == 100.0:
    ok("del nombre, que venía en las dos, queda el de mayor confianza (100 contra 99.92)")
else:
    mal(f"nombre: {filas[0]['nombre_accionista']}")
if valores(filas[1]) == valores(acta()["accionista"][1]) and valores(filas[2]) == valores(acta()["accionista"][2]):
    ok("Juan y María, intactos (comparten «Monterrey» con Carlos, pero una ciudad no identifica)")
else:
    mal("Juan o María cambiaron")
if unidas == [{"grupo": "accionista", "fila": 1, "de": [1, 4], "por": ["nombre_accionista"]}]:
    ok("queda dicho qué se unió: la fila 1 junta las filas 1 y 4 de Document AI, por el nombre")
else:
    mal(f"filas_unidas: {unidas}")
if datos["denominacion_social"]["value_normalized"] == "AUTOPARTES PREMIUM":
    ok("lo que no es un grupo repetible no se toca")
else:
    mal("cambió un campo suelto")

# ─────────────────────────────────────────────────────────────────────────────
caso("[2] El mismo nombre escrito distinto (acentos, mayúsculas, puntuación) se reconoce")
datos, unidas = unir_filas_partidas(
    {"socios": [{"nombre": c("Carlos Ramírez López"), "acciones": c("25")}, {"nombre": c("CARLOS RAMIREZ LOPEZ."), "rfc": c("RALC901223AB1")}]}
)
if len(datos["socios"]) == 1 and set(datos["socios"][0]) == {"nombre", "acciones", "rfc"}:
    ok("«Carlos Ramírez López» y «CARLOS RAMIREZ LOPEZ.» son la misma persona")
else:
    mal(f"quedó: {[valores(f) for f in datos['socios']]}")

caso("[3] Mismo nombre, pero se contradicen: son dos personas")
datos, unidas = unir_filas_partidas(
    {"socios": [{"nombre": c("CARLOS RAMÍREZ"), "fecha_nacimiento": c("1960-03-01")}, {"nombre": c("CARLOS RAMÍREZ"), "fecha_nacimiento": c("1990-12-23")}]}
)
if len(datos["socios"]) == 2 and unidas == []:
    ok("padre e hijo con el mismo nombre y distinta fecha de nacimiento: dos filas, nada unido")
else:
    mal(f"quedó: {[valores(f) for f in datos['socios']]}")

caso("[4] Un pedazo que cuadra con DOS registros distintos no se le asigna a ninguno")
filas_antes = [
    {"nombre": c("CARLOS RAMÍREZ"), "acciones": c("25")},
    {"nombre": c("CARLOS RAMÍREZ"), "fecha_nacimiento": c("1960-03-01")},
    {"nombre": c("CARLOS RAMÍREZ"), "fecha_nacimiento": c("1990-12-23")},
]
datos, unidas = unir_filas_partidas({"socios": filas_antes})
if [valores(f) for f in datos["socios"]] == [valores(f) for f in filas_antes] and unidas == []:
    ok("«Carlos, 25 acciones» podría ser del padre o del hijo: las tres filas se quedan como llegaron")
else:
    mal(f"quedó: {[valores(f) for f in datos['socios']]}")

caso("[5] Un valor que no identifica no basta, aunque coincida y no haya contradicción")
datos, unidas = unir_filas_partidas({"socios": [{"ciudad": c("Monterrey"), "acciones": c("25")}, {"ciudad": c("Monterrey"), "estado_civil": c("casado")}]})
if len(datos["socios"]) == 2 and unidas == []:
    ok("dos filas que solo comparten la ciudad siguen siendo dos")
else:
    mal(f"quedó: {[valores(f) for f in datos['socios']]}")
datos, unidas = unir_filas_partidas({"socios": [{"acciones": c("25")}, {"nombre": c("JUAN"), "acciones": c("25")}]})
if len(datos["socios"]) == 2:
    ok("una fila sin nombre no se pega a nadie, aunque sus acciones coincidan")
else:
    mal(f"quedó: {[valores(f) for f in datos['socios']]}")

caso("[6] Un grupo sin campo que identifique no se une nunca: dos partidas iguales son dos partidas")
partidas = [{"descripcion": c("Tornillo 1/4"), "cantidad": c("10"), "importe": c("$50.00")}] * 2
datos, unidas = unir_filas_partidas({"partidas": [dict(p) for p in partidas]})
if len(datos["partidas"]) == 2 and unidas == []:
    ok("dos renglones idénticos de una factura se quedan como dos")
else:
    mal(f"quedó: {datos['partidas']}")

caso("[7] Tres pedazos de la misma persona son una fila")
datos, unidas = unir_filas_partidas(
    {
        "socios": [
            {"nombre": c("ANA LÓPEZ"), "acciones": c("10")},
            {"nombre": c("JUAN PÉREZ"), "acciones": c("20")},
            {"nombre": c("ANA LOPEZ"), "fecha_nacimiento": c("1985-01-01")},
            {"nombre": c("Ana López"), "calle": c("Reforma")},
        ]
    }
)
if [valores(f).get("nombre") for f in datos["socios"]] == ["ANA LÓPEZ", "JUAN PÉREZ"] and set(datos["socios"][0]) == {"nombre", "acciones", "fecha_nacimiento", "calle"}:
    ok("Ana queda una vez, con lo de sus tres pedazos; Juan sigue en su lugar")
else:
    mal(f"quedó: {[valores(f) for f in datos['socios']]}")
if unidas == [{"grupo": "socios", "fila": 1, "de": [1, 3, 4], "por": ["nombre"]}]:
    ok("filas_unidas dice que la fila 1 junta la 1, la 3 y la 4")
else:
    mal(f"filas_unidas: {unidas}")

caso("[8] El RFC, la razón social y la CURP también identifican")
for campo in ("rfc", "razon_social", "curp_socio", "denominacion"):
    datos, unidas = unir_filas_partidas({"socios": [{campo: c("ABC123"), "acciones": c("5")}, {campo: c("abc-123"), "domicilio": c("Centro")}]})
    if len(datos["socios"]) == 1 and unidas and unidas[0]["por"] == [campo]:
        ok(f"por `{campo}`")
    else:
        mal(f"por `{campo}` quedó: {[valores(f) for f in datos['socios']]}")
if not identifica("numero_exterior_domicilio") and not identifica("nombramiento") and identifica("nombre_del_socio"):
    ok("`numero_exterior_domicilio` y `nombramiento` no identifican; `nombre_del_socio` sí")
else:
    mal("la regla de qué campo identifica se equivoca")

caso("[9] Sin nada que unir, los datos salen como entraron y no hay `filas_unidas`")
entrada = acta()
entrada["accionista"] = entrada["accionista"][:3]
datos, unidas = unir_filas_partidas(entrada, ESTRUCTURA_ACTA)
if datos == entrada and unidas == []:
    ok("idénticos")
else:
    mal("cambió algo sin motivo")
datos, unidas = unir_filas_partidas({"telefonos": [c("8112345678"), c("8112345678")], "socios": [{"nombre": c("JUAN")}]})
if len(datos["telefonos"]) == 2 and unidas == []:
    ok("una lista de campos sueltos (teléfonos) no se toca, aunque se repita un valor")
else:
    mal(f"quedó: {datos}")

# ─────────────────────────────────────────────────────────────────────────────
caso("[10] El camino completo: Document AI → extracción → renglones de entityFacts")


def entidad(tipo: str, texto: str, confianza: float, pagina: int) -> dict:
    return {"type": tipo, "mentionText": texto, "confidence": confianza, "pageAnchor": {"pageRefs": [{"page": pagina - 1}]}}


def fila(tipo: str, *hijas: dict) -> dict:
    return {"type": tipo, "mentionText": "", "confidence": 1.0, "properties": list(hijas)}


ENTIDADES = [
    entidad("denominacion_social", "AUTOPARTES PREMIUM", 0.99, 1),
    fila("accionista", entidad("acciones_suscritas", "25", 1.0, 8), entidad("nombre_accionista", "CARLOS RAMÍREZ LÓPEZ", 1.0, 8)),
    fila("accionista", entidad("acciones_suscritas", "35", 0.99, 8), entidad("nombre_accionista", "JUAN PÉREZ MARTÍNEZ", 0.99, 8)),
    fila(
        "accionista",
        entidad("estado_civil", "casado", 0.87, 9),
        entidad("fecha_nacimiento", "23 de diciembre de 1990", 0.98, 9),
        entidad("nombre_accionista", "CARLOS RAMÍREZ LÓPEZ", 0.99, 9),
    ),
]


async def _procesar(procesador_id, contenido, mime_type, version="", opciones=None):
    return {"document": {"text": "", "pages": [], "entities": ENTIDADES}}


async def _estructura(procesador_id):
    return ESTRUCTURA_ACTA


ia._procesar = _procesar  # type: ignore[assignment]
ia._estructura_de = _estructura  # type: ignore[assignment]
r = asyncio.run(ia.extraer_con_procesador("proc", b"x", "application/pdf", "v1"))

if len(r["accionista"]) == 2:
    ok("la respuesta de /ia/extraer ya trae 2 accionistas, no 3")
else:
    mal(f"accionista: {[valores(f) for f in r['accionista']]}")
if r["_metadata"].get("filas_unidas") == [{"grupo": "accionista", "fila": 1, "de": [1, 3], "por": ["nombre_accionista"]}]:
    ok("y `_metadata.filas_unidas` lo dice (se guarda con la corrida, en metadataJson)")
else:
    mal(f"_metadata: {r['_metadata']}")
ausentes = r["_metadata"].get("campos_ausentes") or []
if "accionista[1].fecha_nacimiento" not in ausentes and "accionista[1].estado_civil" not in ausentes and "accionista[1].ocupacion" in ausentes:
    ok("los ausentes se calculan sobre la fila completa: a Carlos ya no le «falta» lo que traía su otra mitad")
else:
    mal(f"campos_ausentes: {ausentes}")
if not any(a.startswith("accionista[3]") for a in ausentes):
    ok("y ya no hay una fila 3 incompleta que señalar")
else:
    mal(f"campos_ausentes: {ausentes}")
renglones = aplanar(r)
fecha = next((x for x in renglones if x["fieldPath"] == "accionista[1].fecha_nacimiento"), None)
if fecha and fecha["recordIndex"] == 1 and fecha["pageNumber"] == 9 and fecha["valueNormalized"]:
    ok("en entityFacts la fecha de Carlos queda en la fila 1, con su página 9")
else:
    mal(f"renglón: {fecha}")
if not any(x["recordIndex"] == 3 for x in renglones):
    ok("ningún renglón con recordIndex 3")
else:
    mal("quedó algún renglón de una fila 3")

print()
print("=== FALLÓ ===" if fallas else "=== FILAS PARTIDAS VERIFICADAS ===")
raise SystemExit(1 if fallas else 0)
