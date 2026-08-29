"""Verificación del mapeo wizard -> DocumentSchema. Sin red, sin credenciales.

Correr con:  python verificar_esquema.py
Mismo espíritu que verificar_normalizacion.py: casos concretos, salida legible,
exit code 1 si algo no cuadra.
"""

from servicios.esquema import esquema_desde_campos, normalizar_nombre

fallas = 0


def ok(texto: str) -> None:
    print(f"  OK  {texto}")


def mal(texto: str) -> None:
    global fallas
    fallas += 1
    print(f"  NO  {texto}")


def caso(titulo: str) -> None:
    print(f"\n{titulo}")


# ── Normalización de nombres ─────────────────────────────────────────────────
caso("[1] Nombres: lo que teclea un usuario -> lo que Google acepta")
ESPERADOS = {
    "Fecha de Nacimiento": "fecha_de_nacimiento",
    "número de póliza": "numero_de_poliza",
    "Año": "ano",
    "CURP": "curp",
    "  espacios   dobles  ": "espacios_dobles",
    "123 empieza con número": "campo_123_empieza_con_numero",
    "sólo-guiones-ok": "solo-guiones-ok",
    "señal/ruido (dB)": "senalruido_db",
    "": "campo",
    "ñoño": "nono",
}
for crudo, esperado in ESPERADOS.items():
    real = normalizar_nombre(crudo)
    if real == esperado:
        ok(f"{crudo!r} -> {real!r}")
    else:
        mal(f"{crudo!r} -> {real!r}, se esperaba {esperado!r}")

caso("[2] El nombre nunca excede 64 ni queda vacío ni empieza con no-letra")
for crudo in ["x" * 200, "____", "9", "-", "á" * 80]:
    real = normalizar_nombre(crudo)
    if len(real) <= 64 and real and real[0].isalpha():
        ok(f"{crudo[:12]!r}... -> {real[:20]!r} (largo {len(real)})")
    else:
        mal(f"{crudo[:12]!r}... -> {real!r} viola las reglas")

# ── occurrenceType ────────────────────────────────────────────────────────────
caso("[3] obligatorio x cardinalidad -> occurrenceType")
CASOS_OCC = [
    (True, "unico", "REQUIRED_ONCE"),
    (True, "multiple", "REQUIRED_MULTIPLE"),
    (False, "unico", "OPTIONAL_ONCE"),
    (False, "multiple", "OPTIONAL_MULTIPLE"),
]
for oblig, card, esperado in CASOS_OCC:
    e = esquema_desde_campos(
        "T", "", [{"nombre": "c", "tipoDato": "texto", "obligatorio": oblig, "cardinalidad": card}]
    )
    real = e["entityTypes"][0]["properties"][0]["occurrenceType"]
    if real == esperado:
        ok(f"({oblig}, {card}) -> {real}")
    else:
        mal(f"({oblig}, {card}) -> {real}, se esperaba {esperado}")

# ── valueType ────────────────────────────────────────────────────────────────
caso("[4] tipoDato -> valueType")
for tipo, esperado in [
    ("texto", "string"),
    ("numero", "number"),
    ("fecha", "datetime"),
    ("moneda", "money"),
    ("booleano", "checkbox"),
    ("desconocido_futuro", "string"),
]:
    e = esquema_desde_campos("T", "", [{"nombre": "c", "tipoDato": tipo}])
    real = e["entityTypes"][0]["properties"][0]["valueType"]
    if real == esperado:
        ok(f"{tipo} -> {real}")
    else:
        mal(f"{tipo} -> {real}, se esperaba {esperado}")

# ── Listas ───────────────────────────────────────────────────────────────────
caso("[5] Un campo lista genera su EntityType auxiliar con enumValues")
e = esquema_desde_campos(
    "T",
    "",
    [
        {
            "nombre": "Tipo de Sangre",
            "tipoDato": "lista",
            "valoresLista": ["A+", "  O-  ", "", "AB+"],
        }
    ],
)
prop = e["entityTypes"][0]["properties"][0]
aux = [et for et in e["entityTypes"] if et["name"] == prop["valueType"]]
if prop["valueType"] == "tipo_de_sangre_valores":
    ok("la Property apunta al tipo auxiliar por nombre")
else:
    mal(f"valueType = {prop['valueType']}")
if len(aux) == 1 and aux[0]["enumValues"]["values"] == ["A+", "O-", "AB+"]:
    ok("el auxiliar existe y sus valores van limpios (sin vacíos, sin espacios)")
else:
    mal(f"auxiliar mal formado: {aux}")

# ── Descripción y ejemplo ────────────────────────────────────────────────────
caso("[6] La descripción viaja, y el ejemplo se le anexa")
e = esquema_desde_campos(
    "T",
    "",
    [
        {"nombre": "poliza", "descripcion": "Número de póliza.", "valorEstructura": "POL-2026-00045871"},
        {"nombre": "sin_desc", "valorEstructura": "X-1"},
        {"nombre": "sin_nada"},
    ],
)
d0, d1, d2 = (p["description"] for p in e["entityTypes"][0]["properties"])
if d0 == "Número de póliza. Ejemplo: POL-2026-00045871":
    ok("descripción + ejemplo")
else:
    mal(f"quedó {d0!r}")
if d1 == "Ejemplo: X-1":
    ok("solo ejemplo cuando no hay descripción")
else:
    mal(f"quedó {d1!r}")
if d2 == "":
    ok("vacío cuando no hay nada — no se inventa texto")
else:
    mal(f"quedó {d2!r}")

# ── Colisiones ───────────────────────────────────────────────────────────────
caso("[7] Dos nombres que normalizan igual no tumban el esquema")
e = esquema_desde_campos(
    "T", "", [{"nombre": "Año"}, {"nombre": "ano"}, {"nombre": "AÑO"}]
)
nombres = [p["name"] for p in e["entityTypes"][0]["properties"]]
if len(set(nombres)) == 3:
    ok(f"quedaron únicos: {nombres}")
else:
    mal(f"colisionaron: {nombres}")

# ── Forma general ────────────────────────────────────────────────────────────
caso("[8] La raíz es la que Document AI espera")
raiz = esquema_desde_campos("Póliza de seguro", "Póliza vehicular.", [{"nombre": "x"}])
et = raiz["entityTypes"][0]
if et["name"] == "custom_extraction_document_type" and et["baseTypes"] == ["document"]:
    ok("entityType raíz con baseTypes ['document'] — igual que el esquema real del procesador de INE")
else:
    mal(f"raíz inesperada: {et['name']} {et['baseTypes']}")
if raiz["displayName"] == "Póliza de seguro" and raiz["description"] == "Póliza vehicular.":
    ok("displayName y description del tipo van al esquema")
else:
    mal("se perdió el nombre o la descripción del tipo")

print()
print("=== FALLÓ ===" if fallas else "=== MAPEO VERIFICADO ===")
raise SystemExit(1 if fallas else 0)
