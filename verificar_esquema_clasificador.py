"""Verificación del mapeo tipos activos -> DocumentSchema del Classifier.
Sin red, sin credenciales.

Correr con:  python verificar_esquema_clasificador.py
Mismo espíritu que verificar_esquema.py: casos concretos, salida legible,
exit code 1 si algo no cuadra.
"""

from servicios.esquema import (
    CATEGORIA_OTRO,
    esquema_clasificador_desde_tipos,
    normalizar_nombre,
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


# ── Forma general ────────────────────────────────────────────────────────────
caso("[1] metadata.documentSplitter va en false -- es clasificación, no partición")
e = esquema_clasificador_desde_tipos(
    [{"id": "tipo-abc", "nombre": "INE"}, {"id": "tipo-xyz", "nombre": "Pasaporte"}]
)
if e["metadata"] == {"documentSplitter": False}:
    ok("metadata correcta")
else:
    mal(f"metadata inesperada: {e['metadata']}")

caso("[2] Un EntityType por tipo MÁS la categoría de escape, con baseTypes ['document'] y SIN properties")
if len(e["entityTypes"]) == 3:
    ok("2 tipos -> 2 EntityTypes + 'otro'")
else:
    mal(f"se esperaban 3 EntityTypes (2 tipos + otro), salieron {len(e['entityTypes'])}")
for et in e["entityTypes"]:
    if et["baseTypes"] == ["document"] and "properties" not in et:
        ok(f"{et['name']}: baseTypes ['document'], sin properties")
    else:
        mal(f"{et['name']}: {et}")

# ── La categoría de escape ───────────────────────────────────────────────────
caso("[2b] SIEMPRE existe la categoría 'otro' -- sin ella el modelo fuerza todo a un tipo real")
nombres = [et["name"] for et in e["entityTypes"]]
if nombres[-1] == CATEGORIA_OTRO:
    ok(f"'{CATEGORIA_OTRO}' presente, y va al final de la lista")
else:
    mal(f"no se encontró '{CATEGORIA_OTRO}' al final: {nombres}")
otro = e["entityTypes"][-1]
if otro.get("description"):
    ok("'otro' lleva descripción (es prompt real: le da permiso de no elegir ninguna)")
else:
    mal("'otro' salió sin descripción")

caso("[2c] Un tipo cuyo id normalizado fuera 'otro' NO pisa la categoría de escape")
e_choque = esquema_clasificador_desde_tipos([{"id": "otro", "nombre": "Otro tipo del usuario"}])
nombres_choque = [et["name"] for et in e_choque["entityTypes"]]
if len(set(nombres_choque)) == len(nombres_choque) and CATEGORIA_OTRO in nombres_choque:
    ok(f"sin nombres repetidos y con la de escape intacta: {nombres_choque}")
else:
    mal(f"colisión de nombres: {nombres_choque}")

# ── name vs displayName ──────────────────────────────────────────────────────
caso("[3] `name` es el ID estable normalizado; `displayName` es el nombre visible")
et0, et1 = e["entityTypes"][0], e["entityTypes"][1]
if et0["name"] == normalizar_nombre("tipo-abc") and et0["displayName"] == "INE":
    ok(f"tipo-abc -> name={et0['name']!r}, displayName={et0['displayName']!r}")
else:
    mal(f"tipo-abc quedó mal: {et0}")
if et1["name"] == normalizar_nombre("tipo-xyz") and et1["displayName"] == "Pasaporte":
    ok(f"tipo-xyz -> name={et1['name']!r}, displayName={et1['displayName']!r}")
else:
    mal(f"tipo-xyz quedó mal: {et1}")

caso("[4] Si el tipo no trae nombre (caso raro), displayName cae al id -- nunca vacío")
e_sin_nombre = esquema_clasificador_desde_tipos([{"id": "tipo-solo-id", "nombre": ""}])
if e_sin_nombre["entityTypes"][0]["displayName"] == "tipo-solo-id":
    ok("displayName cae al id cuando el nombre viene vacío")
else:
    mal(f"quedó: {e_sin_nombre['entityTypes'][0]['displayName']!r}")

# ── Estabilidad del name ante un renombre ────────────────────────────────────
caso("[5] Renombrar el tipo NO cambia el `name` del EntityType (solo el displayName)")
antes = esquema_clasificador_desde_tipos([{"id": "tipo-abc", "nombre": "INE"}])
despues = esquema_clasificador_desde_tipos([{"id": "tipo-abc", "nombre": "INE (renombrado)"}])
if antes["entityTypes"][0]["name"] == despues["entityTypes"][0]["name"]:
    ok("el `name` sobrevive el renombre -- el pipeline puede seguir mapeando por id")
else:
    mal("el `name` cambió al renombrar, rompería el mapeo id -> categoría")
if despues["entityTypes"][0]["displayName"] == "INE (renombrado)":
    ok("el `displayName` SÍ refleja el nombre nuevo")
else:
    mal(f"displayName no se actualizó: {despues['entityTypes'][0]['displayName']!r}")

# ── Descripción opcional ──────────────────────────────────────────────────────
caso("[6] La descripción del tipo viaja como `description` del EntityType")
e_con_desc = esquema_clasificador_desde_tipos(
    [{"id": "tipo-abc", "nombre": "INE", "descripcion": "Credencial de elector con fotografía."}]
)
if e_con_desc["entityTypes"][0].get("description") == "Credencial de elector con fotografía.":
    ok("description presente y correcta")
else:
    mal(f"quedó: {e_con_desc['entityTypes'][0]!r}")

caso("[7] Sin descripción (o vacía), el EntityType NO lleva la llave `description`")
e_sin_desc = esquema_clasificador_desde_tipos([{"id": "tipo-abc", "nombre": "INE"}])
if "description" not in e_sin_desc["entityTypes"][0]:
    ok("sin `description` cuando no se manda -- no se manda un string vacío sin sentido")
else:
    mal(f"quedó: {e_sin_desc['entityTypes'][0]!r}")

# ── Lista vacía ───────────────────────────────────────────────────────────────
caso("[8] Sin tipos activos, queda SOLO la categoría de escape (no truena, no sale vacío)")
e_vacio = esquema_clasificador_desde_tipos([])
if [et["name"] for et in e_vacio["entityTypes"]] == [CATEGORIA_OTRO]:
    ok("solo 'otro', sin excepción")
else:
    mal(f"quedó: {e_vacio['entityTypes']}")
# Ojo: el endpoint igual rechaza la lista vacía con 422 antes de llegar aquí
# (un clasificador que solo sabe decir "otro" no distingue nada), así que esto
# es la forma de la función, no un estado que el sistema use.

print()
print("=== FALLÓ ===" if fallas else "=== ESQUEMA DEL CLASIFICADOR VERIFICADO ===")
raise SystemExit(1 if fallas else 0)
