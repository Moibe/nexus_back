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

# ── Grupos de campos ─────────────────────────────────────────────────────────
ACCIONISTAS = {
    "nombre": "Accionistas",
    "tipoDato": "grupo",
    "descripcion": "Cada socio fundador con las acciones que suscribe.",
    "obligatorio": True,
    "cardinalidad": "multiple",
    "subcampos": [
        {"nombre": "Nombre", "tipoDato": "texto", "descripcion": "Nombre del socio.", "obligatorio": True},
        {"nombre": "Número de acciones", "tipoDato": "numero", "obligatorio": True},
        {"nombre": "Monto", "tipoDato": "moneda", "obligatorio": False},
        {"nombre": "Fecha de ingreso", "tipoDato": "fecha"},
    ],
}

caso("[9] Un grupo repetible: la Property apunta a un EntityType `object` con sus subcampos (el patrón de `domicilio` de INE)")
e = esquema_desde_campos("Acta", "", [{"nombre": "Denominación", "tipoDato": "texto"}, ACCIONISTAS])
raiz_props = {p["name"]: p for p in e["entityTypes"][0]["properties"]}
aux = {t["name"]: t for t in e["entityTypes"][1:]}
p = raiz_props.get("accionistas", {})
if p.get("valueType") == "accionistas" and p.get("occurrenceType") == "REQUIRED_MULTIPLE":
    ok("la Property `accionistas` apunta a su EntityType y es REQUIRED_MULTIPLE (obligatorio + múltiple)")
else:
    mal(f"Property del grupo: {p}")
if p.get("description") == "Cada socio fundador con las acciones que suscribe.":
    ok("la descripción del grupo viaja (es prompt, como la de cualquier campo)")
else:
    mal(f"descripción del grupo: {p.get('description')!r}")
t = aux.get("accionistas", {})
if t.get("baseTypes") == ["object"] and [h["name"] for h in t.get("properties", [])] == ["nombre", "numero_de_acciones", "monto", "fecha_de_ingreso"]:
    ok("el EntityType auxiliar es `object` y trae los 4 subcampos, con nombres normalizados y en orden")
else:
    mal(f"EntityType del grupo: {t}")
hijos = {h["name"]: h for h in t.get("properties", [])}
esperado = {
    "nombre": ("string", "REQUIRED_ONCE"),
    "numero_de_acciones": ("number", "REQUIRED_ONCE"),
    "monto": ("money", "OPTIONAL_ONCE"),
    "fecha_de_ingreso": ("datetime", "OPTIONAL_ONCE"),
}
if all((hijos[n]["valueType"], hijos[n]["occurrenceType"]) == v for n, v in esperado.items()):
    ok("cada subcampo con su valueType y su obligatoriedad; dentro de un grupo siempre «una vez» por instancia")
else:
    mal(f"subcampos: { {n: (h['valueType'], h['occurrenceType']) for n, h in hijos.items()} }")
if hijos["nombre"]["description"] == "Nombre del socio." and hijos["monto"]["description"] == "":
    ok("las descripciones de los subcampos también viajan (y vacío sigue siendo vacío)")
else:
    mal("descripciones de subcampos")
if len(e["entityTypes"]) == 2:
    ok("un solo EntityType auxiliar: ni de más ni de menos")
else:
    mal(f"{len(e['entityTypes']) - 1} auxiliares")

caso("[10] Un grupo que aparece UNA vez (un domicilio) y el que no es obligatorio")
e = esquema_desde_campos("T", "", [{**ACCIONISTAS, "nombre": "Domicilio", "cardinalidad": "unico", "obligatorio": False}])
p = e["entityTypes"][0]["properties"][0]
if p["occurrenceType"] == "OPTIONAL_ONCE" and p["valueType"] == "domicilio":
    ok("único + opcional -> OPTIONAL_ONCE")
else:
    mal(f"quedó {p}")
e = esquema_desde_campos("T", "", [{**ACCIONISTAS, "obligatorio": False}])
if e["entityTypes"][0]["properties"][0]["occurrenceType"] == "OPTIONAL_MULTIPLE":
    ok("múltiple + opcional -> OPTIONAL_MULTIPLE")
else:
    mal("múltiple + opcional")

caso("[11] Un subcampo de tipo lista agrega su propio EntityType con `enumValues`, sin chocar con el de otro grupo")
cargos = {
    "nombre": "Cargos",
    "tipoDato": "grupo",
    "cardinalidad": "multiple",
    "subcampos": [
        {"nombre": "Tipo", "tipoDato": "lista", "valoresLista": ["Administrador", " Comisario ", "", "Apoderado"]},
        {"nombre": "Nombre", "tipoDato": "texto"},
    ],
}
otro = {**cargos, "nombre": "Representantes"}
e = esquema_desde_campos("T", "", [cargos, otro])
nombres_aux = [t["name"] for t in e["entityTypes"][1:]]
if len(nombres_aux) == len(set(nombres_aux)) == 4:
    ok(f"cuatro EntityTypes auxiliares, todos con nombre único: {nombres_aux}")
else:
    mal(f"auxiliares: {nombres_aux}")
enum = next((t for t in e["entityTypes"] if t["name"] == "cargos_tipo_valores"), {})
if enum.get("enumValues") == {"values": ["Administrador", "Comisario", "Apoderado"]}:
    ok("los valores de la lista salen limpios (sin espacios ni vacíos)")
else:
    mal(f"enum: {enum}")
tipo_hijo = next((h for h in next(t for t in e["entityTypes"] if t["name"] == "cargos")["properties"] if h["name"] == "tipo"), {})
if tipo_hijo.get("valueType") == "cargos_tipo_valores":
    ok("el subcampo apunta a SU enum")
else:
    mal(f"subcampo lista: {tipo_hijo}")

caso("[12] Nombres: el mismo subcampo en dos grupos no choca; dos subcampos que normalizan igual sí se desambiguan")
e = esquema_desde_campos(
    "T",
    "",
    [
        {"nombre": "Grupo A", "tipoDato": "grupo", "subcampos": [{"nombre": "Año"}, {"nombre": "ano"}, {"nombre": "Nombre"}]},
        {"nombre": "Grupo B", "tipoDato": "grupo", "subcampos": [{"nombre": "Nombre"}]},
        {"nombre": "Nombre", "tipoDato": "texto"},
    ],
)
a = next(t for t in e["entityTypes"] if t["name"] == "grupo_a")
nombres_a = [h["name"] for h in a["properties"]]
if len(set(nombres_a)) == 3:
    ok(f"subcampos únicos dentro del grupo: {nombres_a}")
else:
    mal(f"colisionaron: {nombres_a}")
if [p["name"] for p in e["entityTypes"][0]["properties"]] == ["grupo_a", "grupo_b", "nombre"]:
    ok("`nombre` puede existir en la raíz y dentro de cada grupo a la vez (las propiedades se acotan por tipo)")
else:
    mal([p["name"] for p in e["entityTypes"][0]["properties"]])

caso("[13] Lo que NO se puede declarar se omite en vez de mandarle a Google un esquema inválido")
e = esquema_desde_campos(
    "T",
    "",
    [
        {"nombre": "Vacío", "tipoDato": "grupo", "subcampos": []},
        {"nombre": "Anidado", "tipoDato": "grupo", "subcampos": [{"nombre": "Otro grupo", "tipoDato": "grupo", "subcampos": [{"nombre": "x"}]}, {"nombre": "Bueno"}]},
        {"nombre": "Suelto", "tipoDato": "texto"},
    ],
)
raiz = [p["name"] for p in e["entityTypes"][0]["properties"]]
if raiz == ["anidado", "suelto"]:
    ok("el grupo sin subcampos se omite; el resto sigue")
else:
    mal(f"raíz: {raiz}")
anidado = next(t for t in e["entityTypes"] if t["name"] == "anidado")
if [h["name"] for h in anidado["properties"]] == ["bueno"]:
    ok("un grupo dentro de un grupo se ignora (un solo nivel)")
else:
    mal(f"subcampos: {[h['name'] for h in anidado['properties']]}")

caso("[14] Un esquema sin grupos sigue saliendo EXACTAMENTE igual que antes (ningún EntityType auxiliar de más)")
e = esquema_desde_campos("T", "", [{"nombre": "A", "tipoDato": "texto"}, {"nombre": "B", "tipoDato": "lista", "valoresLista": ["x"]}])
if [t["name"] for t in e["entityTypes"]] == ["custom_extraction_document_type", "b_valores"]:
    ok("solo la raíz y el enum de la lista, como antes")
else:
    mal([t["name"] for t in e["entityTypes"]])

caso("[15] La estructura que se lee de un esquema: qué es obligatorio, qué se repite y qué es grupo")
from servicios.esquema import estructura_desde_esquema  # noqa: E402

# El esquema REAL del procesador de INE (bajado de Google el 2026-10-16), recortado: un grupo único declarado a mano en la consola.
ESQUEMA_INE = {
    "entityTypes": [
        {
            "name": "custom_extraction_document_type",
            "baseTypes": ["document"],
            "properties": [
                {"name": "curp", "valueType": "string", "occurrenceType": "REQUIRED_ONCE"},
                {"name": "domicilio", "valueType": "domicilio", "occurrenceType": "REQUIRED_ONCE"},
                {"name": "folio", "valueType": "number", "occurrenceType": "OPTIONAL_ONCE"},
            ],
        },
        {
            "name": "domicilio",
            "baseTypes": ["object"],
            "displayName": "domicilio",
            "properties": [
                {"name": "calle_numero", "valueType": "string", "occurrenceType": "REQUIRED_ONCE"},
                {"name": "localidad", "valueType": "address", "occurrenceType": "OPTIONAL_ONCE"},
            ],
        },
    ]
}
est = estructura_desde_esquema(ESQUEMA_INE)
if est["curp"] == {"obligatorio": True, "multiple": False} and est["folio"] == {"obligatorio": False, "multiple": False}:
    ok("campos sueltos: obligatorio y múltiple salen del occurrenceType")
else:
    mal(f"quedó: {est}")
if est["domicilio"].get("hijos") == {"calle_numero": {"obligatorio": True, "multiple": False}, "localidad": {"obligatorio": False, "multiple": False}}:
    ok("el `domicilio` de INE (un EntityType `object`) se reconoce como grupo, con sus hijas")
else:
    mal(f"domicilio: {est['domicilio']}")
if list(est) == ["curp", "domicilio", "folio"]:
    ok("en el orden del esquema")
else:
    mal(list(est))

e = esquema_desde_campos("T", "", [{"nombre": "Nombre", "tipoDato": "texto", "obligatorio": True}, ACCIONISTAS, {"nombre": "Tipo", "tipoDato": "lista", "valoresLista": ["a"], "cardinalidad": "multiple"}])
est = estructura_desde_esquema(e)
if est["accionistas"]["multiple"] is True and est["accionistas"]["obligatorio"] is True and list(est["accionistas"]["hijos"]) == ["nombre", "numero_de_acciones", "monto", "fecha_de_ingreso"]:
    ok("lo que genera `esquema_desde_campos` se lee de vuelta: ida y vuelta sin perder nada")
else:
    mal(f"accionistas: {est['accionistas']}")
if "hijos" not in est["tipo"] and est["tipo"]["multiple"] is True:
    ok("un campo `lista` (EntityType con enumValues, sin properties) NO se confunde con un grupo")
else:
    mal(f"tipo: {est['tipo']}")
if estructura_desde_esquema({}) == {} and estructura_desde_esquema({"entityTypes": []}) == {}:
    ok("un esquema vacío da estructura vacía, sin tronar")
else:
    mal("esquema vacío")

print()
print("=== FALLÓ ===" if fallas else "=== MAPEO VERIFICADO ===")
raise SystemExit(1 if fallas else 0)
