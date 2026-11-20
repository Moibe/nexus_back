"""¿Ubicar con Document AI lee bien la respuesta del extractor? (2026-11-20, `servicios/ubicador_docai.py`)

    .venv/Scripts/python verificar_ubicador_docai.py           # sin red: la parte pura, con respuestas armadas a mano
    .venv/Scripts/python verificar_ubicador_docai.py --real    # además llama al Document AI de verdad con el pasaporte de prueba (cobra ~US$0.03)

Lo que se revisa: cada entidad vuelve al campo TAL COMO SE PIDIÓ (aunque Google lo llame `clave_pais_expedicion`), la caja pasa de 0-1 a 0-1000,
lo que se repite y los grupos se marcan como un bloque (en la página de su primera aparición), lo que viene sin polígono no se marca, y el
esquema que se manda pide todo como texto opcional sin perder los grupos.
"""

import asyncio
import sys
from pathlib import Path

from servicios import ubicador_docai as u
from servicios.esquema import esquema_desde_campos

sys.stdout.reconfigure(encoding="utf-8")
fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}" + ("" if ok else f"  → {extra}"))
    fallos += 0 if ok else 1


def ent(tipo: str, caja: tuple[float, float, float, float] | None, texto: str = "", pagina: int = 0, hijas: list | None = None) -> dict:
    """Una entidad como la devuelve Document AI: caja (ymin, xmin, ymax, xmax) de 0 a 1 en un polígono de 4 vértices."""
    e: dict = {"type": tipo, "mentionText": texto}
    if caja:
        y0, x0, y1, x1 = caja
        e["pageAnchor"] = {"pageRefs": [{"page": str(pagina) if pagina else None, "boundingPoly": {"normalizedVertices": [
            {"x": x0, "y": y0}, {"x": x1, "y": y0}, {"x": x1, "y": y1}, {"x": x0, "y": y1}]}}]}
        if not pagina:
            del e["pageAnchor"]["pageRefs"][0]["page"]
    if hijas is not None:
        e["properties"] = hijas
    return e


print("\n=== 1 · El esquema que se manda ===")
pedidos = [
    {"nombre": "Clave país expedición", "descripcion": "MEX"},
    {"nombre": "Fecha de nacimiento", "descripcion": ""},
    {"nombre": "Socios", "descripcion": "", "tipoDato": "grupo", "cardinalidad": "multiple", "subcampos": [{"nombre": "Nombre", "descripcion": ""}, {"nombre": "Acciones", "descripcion": ""}]},
    {"nombre": "Teléfonos", "descripcion": "", "cardinalidad": "multiple"},
]
esquema = esquema_desde_campos("x", "", u.campos_para_esquema(pedidos))
raiz = {p["name"]: p for p in esquema["entityTypes"][0]["properties"]}
rev("todo campo suelto va como texto (una fecha también)", raiz["fecha_de_nacimiento"]["valueType"] == "string" and raiz["clave_pais_expedicion"]["valueType"] == "string", str(raiz))
rev("y opcional: no se le exige al documento", all(p["occurrenceType"].startswith("OPTIONAL") for p in raiz.values()), str([p["occurrenceType"] for p in raiz.values()]))
rev("lo que se repite se pide repetido", raiz["telefonos"]["occurrenceType"] == "OPTIONAL_MULTIPLE" and raiz["socios"]["occurrenceType"] == "OPTIONAL_MULTIPLE")
aux = {t["name"]: t for t in esquema["entityTypes"][1:]}
rev("el grupo sigue siendo grupo, con sus subcampos", [p["name"] for p in aux.get("socios", {}).get("properties", [])] == ["nombre", "acciones"], str(aux))
rev("la descripción viaja", raiz["clave_pais_expedicion"]["description"] == "MEX")

print("\n=== 2 · Leer la respuesta ===")
documento = {"entities": [
    ent("clave_pais_expedicion", (0.539, 0.514, 0.549, 0.564), "MEX"),
    ent("fecha_de_nacimiento", None, "30 12 1990"),  # sin polígono: no se marca
    ent("telefonos", (0.10, 0.10, 0.12, 0.30), "555 1", pagina=1),
    ent("telefonos", (0.20, 0.10, 0.22, 0.40), "555 2", pagina=1),
    ent("telefonos", (0.50, 0.10, 0.52, 0.40), "555 3", pagina=2),  # otra página: fuera del bloque
    ent("socios", None, "", hijas=[ent("nombre", (0.60, 0.10, 0.62, 0.30), "ANA", pagina=0), ent("acciones", (0.60, 0.50, 0.62, 0.60), "10")]),
    ent("socios", None, "", hijas=[ent("nombre", (0.65, 0.10, 0.67, 0.30), "LUIS"), ent("acciones", (0.65, 0.50, 0.67, 0.62), "5")]),
    ent("otra_cosa", (0.1, 0.1, 0.2, 0.2), "no se pidió"),
]}
ub = {x["campo"]: x for x in u.ubicaciones_de(documento, pedidos)}
rev("el campo vuelve con el nombre que se pidió (no el de Google)", "Clave país expedición" in ub and "clave_pais_expedicion" not in ub, str(list(ub)))
rev("la caja pasa a 0-1000", ub["Clave país expedición"]["caja"] == [539, 514, 549, 564] and ub["Clave país expedición"]["pagina"] == 1, str(ub["Clave país expedición"]))
rev("el valor es lo que leyó el extractor", ub["Clave país expedición"]["valor"] == "MEX")
rev("sin polígono no se marca", "Fecha de nacimiento" not in ub)
rev("lo que se repite es UN bloque, solo en la página de su primera aparición",
    ub["Teléfonos"]["pagina"] == 2 and ub["Teléfonos"]["caja"] == [100, 100, 220, 400] and ub["Teléfonos"]["valor"] == "555 1", str(ub["Teléfonos"]))
rev("un grupo sin caja propia se marca con la unión de sus filas y sus partes",
    ub["Socios"]["caja"] == [600, 100, 670, 620] and ub["Socios"]["pagina"] == 1, str(ub["Socios"]))
rev("…y su valor junta las partes de la primera fila", ub["Socios"]["valor"] == "ANA · 10", ub["Socios"]["valor"])
rev("lo que no se pidió no aparece", len(ub) == 3, str(list(ub)))
rev("respuesta vacía: nada", u.ubicaciones_de({}, pedidos) == [])
plano = u.ubicaciones_de({"entities": [ent("clave_pais_expedicion", (0.5, 0.5, 0.5, 0.6), "X")]}, pedidos)
rev("una caja sin alto (una línea) se marca con 1 de alto, no se tira", plano and plano[0]["caja"] == [500, 500, 501, 600], str(plano))

if "--real" in sys.argv:
    print("\n=== 3 · Document AI de verdad (pasaporte de prueba) ===")
    foto = Path("C:/Moibe/documentos_ocr/pasaporte/296940-3A-2021.11.10 21.36.36.994.jpg")
    nombres = ["Pasaporte no", "Tipo", "Clave país expedición", "Apellidos", "Nombres", "Nacionalidad", "Fecha de nacimiento", "CURP", "Sexo",
               "Lugar de nacimiento", "Fecha de expedición", "Fecha de caducidad", "Autoridad", "Mrz line 1", "Mrz line 2"]
    campos = [{"nombre": n, "descripcion": ""} for n in nombres]
    reales = asyncio.run(u.ubicar(foto.read_bytes(), "image/png", campos))
    por = {x["campo"]: x for x in reales}
    for x in reales:
        print(f"      {x['campo']:24} {x['caja']}  «{x['valor']}»")
    rev("los 15 campos con caja", len(reales) == 15, str(list(por)))
    # Dónde están de verdad (medido a mano sobre la foto, 0-1000): la página de datos es la de abajo (y > 500).
    rev("todos los de la página de datos caen en la de abajo", all(por[n]["caja"][0] > 500 for n in nombres if n in por), str({n: por[n]["caja"][0] for n in por}))
    rev("«Tipo» sobre la P y «Apellidos» sobre RAMIREZ PEREZ", "P" in por["Tipo"]["valor"] and "RAMIREZ" in por["Apellidos"]["valor"] and 520 < por["Tipo"]["caja"][0] < 560 and 560 < por["Apellidos"]["caja"][0] < 590)

print()
if fallos:
    print(f"{fallos} FALLA(S)")
    raise SystemExit(1)
print("Todo bien.")
