"""Verificación del mapeo tipos activos -> DocumentSchema del Classifier.
Sin red, sin credenciales.

Correr con:  python verificar_esquema_clasificador.py
Mismo espíritu que verificar_esquema.py: casos concretos, salida legible,
exit code 1 si algo no cuadra.
"""

from servicios.esquema import esquema_clasificador_desde_tipos, normalizar_nombre

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

caso("[2] Un EntityType por tipo, con baseTypes ['document'] y SIN properties")
if len(e["entityTypes"]) == 2:
    ok("2 tipos -> 2 EntityTypes")
else:
    mal(f"se esperaban 2 EntityTypes, salieron {len(e['entityTypes'])}")
for et in e["entityTypes"]:
    if et["baseTypes"] == ["document"] and "properties" not in et:
        ok(f"{et['name']}: baseTypes ['document'], sin properties")
    else:
        mal(f"{et['name']}: {et}")

# ── name vs displayName ──────────────────────────────────────────────────────
caso("[3] `name` es el ID estable normalizado; `displayName` es el nombre visible")
et0, et1 = e["entityTypes"]
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

# ── Lista vacía ───────────────────────────────────────────────────────────────
caso("[6] Sin tipos activos, el esquema sale con entityTypes vacío (no truena)")
e_vacio = esquema_clasificador_desde_tipos([])
if e_vacio["entityTypes"] == []:
    ok("entityTypes vacío, sin excepción")
else:
    mal(f"quedó: {e_vacio['entityTypes']}")

print()
print("=== FALLÓ ===" if fallas else "=== ESQUEMA DEL CLASIFICADOR VERIFICADO ===")
raise SystemExit(1 if fallas else 0)
