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


_ESTRUCTURA_REAL = ia._estructura_de  # la de verdad, para el caso [17]


async def _sin_estructura(procesador_id):
    return None


# Sin esto `extraer_con_procesador` iría a Google a leer el esquema del procesador: este verificador es OFFLINE. Con `None` se
# aplana por lo que llegó, que es lo que hacían los casos de abajo antes de que existiera la estructura.
ia._estructura_de = _sin_estructura  # type: ignore[assignment]


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

# ── Lo que se REPITE: campos múltiples y grupos (2026-10-16) ─────────────────
# Respuestas de Document AI armadas a mano con la forma que se MIDIÓ contra la API real (un acta de 10 páginas): el padre de un
# grupo viene sin texto ni página, sus hijas con su tipo a secas y en orden alfabético, y las instancias en orden del documento.
def hija(tipo: str, texto: str, confianza: float = 0.9, pagina: int = 0) -> dict:
    e = entidad(tipo, texto, confianza)
    if pagina:
        e["pageAnchor"] = {"pageRefs": [{"page": pagina}]}
    return e


def fila(tipo: str, *hijas: dict) -> dict:
    return {"type": tipo, "mentionText": "", "confidence": 1.0, "properties": list(hijas)}


ESTRUCTURA = {
    "denominacion": {"obligatorio": True, "multiple": False},
    "capital": {"obligatorio": True, "multiple": False},
    "socios_nombre": {"obligatorio": False, "multiple": True},
    "fecha": {"obligatorio": False, "multiple": False},
    "domicilio": {"obligatorio": False, "multiple": False, "hijos": {"calle": {"obligatorio": True, "multiple": False}, "ciudad": {"obligatorio": False, "multiple": False}}},
    "accionistas": {
        "obligatorio": True,
        "multiple": True,
        "hijos": {
            "nombre": {"obligatorio": True, "multiple": False},
            "acciones": {"obligatorio": True, "multiple": False},
            "monto": {"obligatorio": False, "multiple": False},
        },
    },
}


def con_respuesta(entidades: list[dict], estructura=None):
    async def _p(procesador_id, contenido, mime_type, version="", opciones=None):
        return {"document": {"text": "", "pages": [], "entities": entidades}}

    async def _e(procesador_id):
        return estructura

    ia._procesar = _p  # type: ignore[assignment]
    ia._estructura_de = _e  # type: ignore[assignment]
    return asyncio.run(ia.extraer_con_procesador("proc", b"x", "application/pdf", "v1"))


caso("[9] Un campo MÚLTIPLE sale siempre como lista, en el orden del documento (antes sobrevivía solo el último)")
r = con_respuesta(
    [entidad("socios_nombre", "JUAN"), entidad("socios_nombre", "MARÍA"), entidad("socios_nombre", "CARLOS"), entidad("denominacion", "ACME"), entidad("capital", "$1")],
    ESTRUCTURA,
)
nombres = [c["value_normalized"] for c in r["socios_nombre"]] if isinstance(r.get("socios_nombre"), list) else r.get("socios_nombre")
if nombres == ["JUAN", "MARÍA", "CARLOS"]:
    ok("tres socios, tres elementos, en orden")
else:
    mal(f"quedó: {nombres}")
r = con_respuesta([entidad("socios_nombre", "JUAN"), entidad("denominacion", "ACME"), entidad("capital", "$1")], ESTRUCTURA)
if isinstance(r["socios_nombre"], list) and len(r["socios_nombre"]) == 1:
    ok("con UNA sola aparición sigue siendo lista de uno: quien lo consume no tiene que adivinar la forma")
else:
    mal(f"quedó: {r['socios_nombre']}")
if isinstance(r["denominacion"], dict) and "value_normalized" in r["denominacion"]:
    ok("un campo único sigue siendo un campo, no una lista")
else:
    mal(f"denominacion quedó: {r['denominacion']}")

caso("[10] Un grupo repetible sale como lista de FILAS, con las columnas en el orden del esquema")
r = con_respuesta(
    [
        fila("accionistas", hija("acciones", "35"), hija("monto", "$466,666.7"), hija("nombre", "JUAN")),
        fila("accionistas", hija("acciones", "15"), hija("monto", "$200,000.00"), hija("nombre", "MARÍA")),
        entidad("denominacion", "ACME"),
        entidad("capital", "$1"),
    ],
    ESTRUCTURA,
)
filas = r.get("accionistas")
if isinstance(filas, list) and len(filas) == 2:
    ok("dos instancias, dos filas")
else:
    mal(f"quedó: {filas}")
if filas and list(filas[0]) == ["nombre", "acciones", "monto"]:
    ok("columnas en el orden del esquema (nombre, acciones, monto), no en el alfabético de Google")
else:
    mal(f"columnas: {list(filas[0]) if filas else None}")
if filas and [f["nombre"]["value_normalized"] for f in filas] == ["JUAN", "MARÍA"] and filas[1]["acciones"]["value_normalized"] == "15":
    ok("cada fila conserva SUS datos juntos (María con sus 15, no con las de otro)")
else:
    mal(f"filas: {filas}")

caso("[11] Un grupo ÚNICO (domicilio) sigue siendo un diccionario — INE no cambia")
r = con_respuesta(
    [fila("domicilio", hija("ciudad", "Monterrey"), hija("calle", "Reforma 1")), entidad("denominacion", "ACME"), entidad("capital", "$1")],
    ESTRUCTURA,
)
dom = r.get("domicilio")
if isinstance(dom, dict) and set(dom) == {"calle", "ciudad"}:
    ok("diccionario de subcampos, no lista")
else:
    mal(f"quedó: {dom}")
if isinstance(dom, dict) and list(dom) == ["ciudad", "calle"]:
    ok("y sus subcampos quedan en el orden en que llegaron (solo las filas de un grupo repetible se reordenan)")
else:
    mal(f"orden: {list(dom) if isinstance(dom, dict) else dom}")

caso("[12] Un campo ÚNICO que llegó dos veces se queda con la de MAYOR confianza (antes, la última)")
r = con_respuesta([entidad("fecha", "18 sep", 0.97), entidad("fecha", "19 sep", 0.55), entidad("denominacion", "A"), entidad("capital", "$1")], ESTRUCTURA)
if r["fecha"]["value_raw"] == "18 sep":
    ok("gana la de 0.97 aunque no sea la última")
else:
    mal(f"quedó: {r['fecha']}")

caso("[13] Los campos OBLIGATORIOS que faltaron se listan en `_metadata.campos_ausentes`")
r = con_respuesta([entidad("denominacion", "ACME"), fila("accionistas", hija("nombre", "JUAN"), hija("acciones", "35"))], ESTRUCTURA)
ausentes = r["_metadata"].get("campos_ausentes")
if ausentes == ["capital"]:
    ok("falta `capital` (obligatorio); `fecha` y `monto` son opcionales y no cuentan")
else:
    mal(f"campos_ausentes: {ausentes}")
r = con_respuesta([entidad("denominacion", "ACME"), entidad("capital", "$1")], ESTRUCTURA)
if r["_metadata"]["campos_ausentes"] == ["accionistas"]:
    ok("un GRUPO obligatorio sin ninguna fila se señala por su nombre")
else:
    mal(f"campos_ausentes: {r['_metadata']['campos_ausentes']}")
r = con_respuesta(
    [
        entidad("denominacion", "ACME"),
        entidad("capital", "$1"),
        fila("accionistas", hija("acciones", "75")),
        fila("accionistas", hija("nombre", "JUAN"), hija("acciones", "35")),
        fila("accionistas", hija("nombre", "MARÍA")),
    ],
    ESTRUCTURA,
)
if r["_metadata"]["campos_ausentes"] == ["accionistas[1].nombre", "accionistas[3].acciones"]:
    ok("una FILA sin un subcampo obligatorio se señala con su posición (la espuria que el modelo saca de otra parte del documento)")
else:
    mal(f"campos_ausentes: {r['_metadata']['campos_ausentes']}")
r = con_respuesta([entidad("denominacion", "ACME"), entidad("capital", "$1"), fila("accionistas", hija("nombre", "J"), hija("acciones", "1"))], ESTRUCTURA)
if r["_metadata"]["campos_ausentes"] == []:
    ok("si no faltó nada, la lista va vacía (no ausente: significa «se revisó y no faltó nada»)")
else:
    mal(f"campos_ausentes: {r['_metadata']['campos_ausentes']}")

caso("[14] Sin poder leer el esquema, se aplana por lo que llegó y NO se inventan ausentes")
r = con_respuesta([entidad("socios_nombre", "JUAN"), entidad("socios_nombre", "MARÍA"), entidad("otro", "x")], None)
if isinstance(r["socios_nombre"], list) and len(r["socios_nombre"]) == 2 and "value_normalized" in r["otro"]:
    ok("lo que se repite es lista y lo que llegó una vez, un campo")
else:
    mal(f"quedó: {r}")
if "campos_ausentes" not in r["_metadata"]:
    ok("`campos_ausentes` no aparece: una lista vacía mentiría diciendo que no faltó nada")
else:
    mal(f"campos_ausentes: {r['_metadata']['campos_ausentes']}")

caso("[15] La confianza promedio cuenta cada campo de cada fila y cada elemento de las listas")
r = con_respuesta(
    [
        entidad("denominacion", "ACME", 1.0),
        entidad("capital", "$1", 1.0),
        entidad("socios_nombre", "A", 0.8),
        entidad("socios_nombre", "B", 0.6),
        fila("accionistas", hija("nombre", "J", 0.7), hija("acciones", "1", 0.9)),
    ],
    ESTRUCTURA,
)
esperado = round((100 + 100 + 80 + 60 + 70 + 90) / 6, 2)
if r["confianza_promedio"] == esperado and r["confianza_minima"] == 60.0:
    ok(f"promedio {esperado} sobre los 6 datos y mínimo 60.0 (el padre del grupo, con su 100, no cuenta)")
else:
    mal(f"promedio {r['confianza_promedio']}, mínimo {r['confianza_minima']}; se esperaba {esperado} y 60.0")

caso("[16] Un grupo sin ninguna hija no se vuelve un «campo» vacío")
r = con_respuesta([entidad("denominacion", "ACME"), entidad("capital", "$1"), {"type": "accionistas", "mentionText": "", "confidence": 1.0}], ESTRUCTURA)
if "accionistas" not in r:
    ok("se omite, y como el grupo es obligatorio queda como ausente")
else:
    mal(f"quedó: {r.get('accionistas')}")
if r["_metadata"]["campos_ausentes"] == ["accionistas"]:
    ok("campos_ausentes: ['accionistas']")
else:
    mal(f"campos_ausentes: {r['_metadata']['campos_ausentes']}")

caso("[17] Si NO se puede leer el esquema del procesador, la extracción sigue (solo pierde la precisión)")
from servicios import procesadores  # noqa: E402


async def _google_caido(procesador_id):
    raise RuntimeError("Document AI respondió 503 al leer el esquema")


ia._estructura_de = _ESTRUCTURA_REAL  # type: ignore[assignment]
original_lectura = procesadores.estructura_del_procesador
procesadores.estructura_del_procesador = _google_caido  # type: ignore[assignment]
try:
    async def _p(procesador_id, contenido, mime_type, version="", opciones=None):
        return {"document": {"text": "", "pages": [], "entities": [entidad("denominacion", "ACME")]}}
    ia._procesar = _p  # type: ignore[assignment]
    r = asyncio.run(ia.extraer_con_procesador("proc", b"x", "application/pdf", "v1"))
    if r["denominacion"]["value_raw"] == "ACME" and "campos_ausentes" not in r["_metadata"]:
        ok("devolvió los campos y sin `campos_ausentes`; no se convirtió en un 502 por un dato accesorio")
    else:
        mal(f"quedó: {r}")
finally:
    procesadores.estructura_del_procesador = original_lectura  # type: ignore[assignment]

print()
print("=== FALLÓ ===" if fallas else "=== EXTRACCIÓN GENÉRICA VERIFICADA ===")
raise SystemExit(1 if fallas else 0)
