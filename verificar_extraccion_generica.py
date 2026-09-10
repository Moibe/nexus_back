"""Verificación de `extraer_con_procesador` vs `extraer_ine`.
Sin red: se sustituye `_procesar` por una respuesta de Document AI de mentiras.

Correr con:  python verificar_extraccion_generica.py
Mismo espíritu que los otros verificar_*.py: casos concretos, salida legible,
exit code 1 si algo no cuadra.

Lo que cuida: que sacar el cuerpo genérico de `extraer_ine` (2026-09-07, para
que un tipo documental dado de alta desde el wizard se extraiga con SU PROPIO
procesador) no le haya cambiado NADA a la extracción de INE, y que el camino
genérico no arrastre las limpiezas que solo tienen sentido en una credencial.
"""

import asyncio

from servicios import ia

fallas = 0


def ok(texto: str) -> None:
    print(f"  OK  {texto}")


def mal(texto: str) -> None:
    global fallas
    fallas += 1
    print(f"  NO  {texto}")


def caso(titulo: str) -> None:
    print(f"\n{titulo}")


def entidad(tipo: str, texto: str, confianza: float = 0.9) -> dict:
    return {"type": tipo, "mentionText": texto, "confidence": confianza}


# Una respuesta con los dos casos que INE limpia: el estado con punto final y
# el `fecha_registro` compuesto ("2024 00").
RESPUESTA = {
    "document": {
        "text": "",
        "pages": [],
        "entities": [
            entidad("curp", "BEEM900101HDFXXX01"),
            entidad("fecha_registro", "2024 00"),
            {
                "type": "domicilio",
                "mentionText": "",
                "confidence": 0.9,
                "properties": [entidad("estado", "SON."), entidad("localidad", "HUATABAMPO,")],
            },
        ],
    }
}

llamadas: list[tuple] = []


async def _procesar_falso(procesador_id, contenido, mime_type, version="", opciones=None):
    llamadas.append((procesador_id, version))
    return RESPUESTA


ia._procesar = _procesar_falso  # type: ignore[assignment]


# ── INE: las limpiezas siguen aplicándose ────────────────────────────────────
caso("[1] extraer_ine sigue quitando el punto final del estado")
ine = asyncio.run(ia.extraer_ine(b"x", "image/jpeg"))
estado = ine.get("domicilio", {}).get("estado", {})
if estado.get("value_normalized") == "SON" and estado.get("value_raw") == "SON.":
    ok("value_normalized='SON' y value_raw='SON.' (el crudo conserva el punto)")
else:
    mal(f"quedó: {estado}")

caso("[2] extraer_ine sigue partiendo fecha_registro en año + numero_emision")
registro = ine.get("fecha_registro", {})
if registro.get("value_normalized") == "2024" and registro.get("numero_emision") == "00":
    ok("value_normalized='2024', numero_emision='00'")
else:
    mal(f"quedó: {registro}")

caso("[3] extraer_ine arma la respuesta completa (ocr, las dos confianzas, _metadata)")
for llave in ("ocr", "confianza_promedio", "confianza_minima", "_metadata"):
    if llave in ine:
        ok(f"'{llave}' presente")
    else:
        mal(f"falta '{llave}'")

# ── Genérico: los MISMOS campos, SIN las limpiezas de INE ────────────────────
caso("[4] extraer_con_procesador NO aplica las limpiezas de INE")
gen = asyncio.run(ia.extraer_con_procesador("proc-xyz", b"x", "image/jpeg", "v1"))
estado_gen = gen.get("domicilio", {}).get("estado", {})
registro_gen = gen.get("fecha_registro", {})
if estado_gen.get("value_normalized") == "SON.":
    ok("el estado conserva el punto: no es una credencial, no hay por qué tocarlo")
else:
    mal(f"quedó: {estado_gen}")
if "numero_emision" not in registro_gen:
    ok("fecha_registro NO se parte")
else:
    mal(f"quedó: {registro_gen}")

caso("[5] extraer_con_procesador usa el procesador y la versión que se le pasan")
if llamadas[-1] == ("proc-xyz", "v1"):
    ok(f"llamó a {llamadas[-1]}")
else:
    mal(f"llamó a {llamadas[-1]}")

caso("[6] La FORMA de la respuesta genérica es la misma que la de INE")
for llave in ("ocr", "confianza_promedio", "confianza_minima", "_metadata"):
    if llave in gen:
        ok(f"'{llave}' presente")
    else:
        mal(f"falta '{llave}'")
if gen["_metadata"].get("engine_version") == "v1":
    ok("_metadata.engine_version refleja la versión pedida")
else:
    mal(f"engine_version quedó: {gen['_metadata'].get('engine_version')}")

# ── Documento que Document AI no reconoce ────────────────────────────────────
caso("[7] Sin entities, el genérico marca quality_alert igual que INE")


async def _sin_entities(procesador_id, contenido, mime_type, version="", opciones=None):
    return {"document": {"text": "", "pages": []}}


ia._procesar = _sin_entities  # type: ignore[assignment]
vacio = asyncio.run(ia.extraer_con_procesador("proc-xyz", b"x", "image/jpeg"))
if (
    vacio["_metadata"]["quality_alert"] is True
    and vacio["confianza_minima"] is None
    and vacio["confianza_promedio"] is None
):
    ok("quality_alert=True y las dos confianzas en None, sin excepción")
else:
    mal(f"quedó: {vacio}")

# ── El promedio, que es el número que ve el usuario ──────────────────────────
caso("[8] confianza_promedio es el PROMEDIO, y confianza_minima el mínimo")

# Se arma a mano en vez de depender del stub: así el caso dice qué números
# entran y cuál tiene que salir, y no hay que leer otro archivo para saberlo.
# Incluye un campo SIN confianza (None) y uno anidado, que son los dos casos
# que rompieron esto antes.
def _c(v):
    return {"value_raw": "x", "value_normalized": "x", "confianza": v,
            "confianza_cruda": None, "metodo_confianza": "extractor_confidence",
            "page_number": 1, "bloque_indice": None, "posicion": None}


muestra = {
    "nombre": _c(100.0),
    "apellido_paterno": _c(99.94),
    "apellido_materno": _c(78.2),
    "sin_dato": _c(None),          # Document AI omitió `confidence`: se ignora
    "domicilio": {"estado": _c(60.26)},   # anidado: SÍ cuenta
}
esperado = round((100.0 + 99.94 + 78.2 + 60.26) / 4, 2)
promedio = ia._confianza_promedio(muestra)
minimo = ia._confianza_minima(muestra)
if promedio == esperado:
    ok(f"promedio = {promedio} (los 4 con dato; el None no cuenta)")
else:
    mal(f"promedio quedó {promedio}, se esperaba {esperado}")
if minimo == 60.26:
    ok("mínimo = 60.26, el peor campo, y sale del anidado")
else:
    mal(f"mínimo quedó {minimo}")
if promedio is not None and minimo is not None and promedio > minimo:
    ok("el promedio es MAYOR que el mínimo: son números distintos de verdad")
else:
    mal("el promedio y el mínimo salieron iguales; el cambio no sirve de nada")
if ia._confianza_promedio({}) is None and ia._confianza_minima({}) is None:
    ok("sin campos, las dos devuelven None en vez de tronar")
else:
    mal("sin campos no devolvieron None")

# Redondeo a 2: tres tercios de 100 dan 33.333... y no se publican 15 decimales.
if ia._confianza_promedio({"a": _c(0.0), "b": _c(0.0), "c": _c(100.0)}) == 33.33:
    ok("redondea a 2 decimales (33.33), la misma precisión que la entrada")
else:
    mal(f"redondeo quedó: {ia._confianza_promedio({'a': _c(0.0), 'b': _c(0.0), 'c': _c(100.0)})}")

print()
print("=== FALLÓ ===" if fallas else "=== EXTRACCIÓN GENÉRICA VERIFICADA ===")
raise SystemExit(1 if fallas else 0)
