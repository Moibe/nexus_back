"""«Generar a partir de un documento»: ¿lo que propone Google llega bien convertido a campos del paso 2, y solo para quien puede?

    .venv/Scripts/python verificar_generar_campos.py

OFFLINE: Document AI se sustituye por un Google falso. Se vigila:

- `esquema.campos_desde_esquema` (puro), con la respuesta REAL que dio `schemaVersions:generate` el 2026-10-29 para una factura de
  prueba, más los casos que esa factura no trae (listas, obligatorios, repetibles, grupos raros, nombres repetidos);
- que el camino de ida y vuelta (campos → esquema → campos) no pierda tipo, obligatoriedad, cardinalidad ni subcampos;
- `procesadores.generar_campos`: encuentra el recurso padre por nombre (paginando), lo crea UNA sola vez aunque lleguen dos
  peticiones a la vez, manda el documento y la instrucción en español con el contexto del tipo, borra la versión que deja cada
  generación (y si el borrado falla, la propuesta llega igual), y si el padre se borró a mano crea otro y reintenta una vez;
- la ruta `POST /procesadores/generar-campos`: sesión, aislamiento y rol de administración (sin llamar a Google si no), archivo
  vacío o de formato no admitido, y los errores de Google traducidos sin filtrar su texto.
"""

import asyncio
import base64
import os
import shutil
import tempfile
from pathlib import Path

RAIZ = Path(tempfile.mkdtemp(prefix="verificar-generar-campos-"))
(RAIZ / ".nexus-almacen").touch()
SERVICIO = "llave-de-servicio-solo-local"
os.environ["ALMACEN_RUTA"] = str(RAIZ)
os.environ["NEXUS_API_KEY"] = SERVICIO
os.environ["AUTH_JWT_SECRET"] = "secreto-de-prueba-de-al-menos-32-caracteres-xx"
os.environ.setdefault("SQLSERVER_HOST", "")

import httpx  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app import app  # noqa: E402
from servicios import auth, procesadores as svc, usuarios  # noqa: E402
from servicios.esquema import campos_desde_esquema, esquema_desde_campos, nombre_legible  # noqa: E402
from servicios.procesadores import DocumentAIError  # noqa: E402

fallos = 0


def rev(descripcion: str, ok: bool, extra: str = "") -> None:
    global fallos
    print(f"  {'OK   ' if ok else 'FALLA'} {descripcion}{'  -> ' + str(extra) if extra and not ok else ''}")
    if not ok:
        fallos += 1


def titulo(t: str) -> None:
    print(f"\n=== {t} ===")


def prop(name: str, value_type: str = "string", occurrence: str = "OPTIONAL_ONCE", description: str = "") -> dict:
    return {"name": name, "valueType": value_type, "occurrenceType": occurrence, "description": description}


# La respuesta REAL (recortada) de `schemaVersions:generate` para la factura de prueba del 2026-10-29: la segunda corrida, con el
# domicilio partido en calle/colonia y los conceptos como `line_item`; `direccion_proveedor` (valueType `address`) es de la primera.
FACTURA = {
    "displayName": "Factura",
    "entityTypes": [
        {
            "name": "custom_extraction_document_type",
            "baseTypes": ["document"],
            "properties": [
                prop("nombre_proveedor", description="Nombre de la empresa que emite la factura."),
                prop("rfc_proveedor", description="Registro Federal de Contribuyentes del emisor."),
                prop("direccion_proveedor", "address", description="Dirección completa del proveedor."),
                prop("colonia_proveedor", description="Colonia del domicilio del proveedor."),
                prop("fecha_emision", "datetime", description="Fecha en la que se emitió la factura."),
                prop("uso_cfdi", description="Uso que se le dará al Comprobante Fiscal Digital por Internet."),
                prop("line_item", "line_item", "OPTIONAL_MULTIPLE", "Lista de artículos facturados."),
                prop("porcentaje_iva", "number", description="Tasa o porcentaje del Impuesto al Valor Agregado."),
                prop("total", "money", description="Monto total de la factura con impuestos."),
            ],
        },
        {
            "name": "line_item",
            "baseTypes": ["object"],
            "properties": [
                prop("cantidad", "number", description="Cantidad de unidades del artículo."),
                prop("descripcion", description="Descripción del artículo."),
                prop("precio_unitario", "money", description="Precio por unidad del artículo."),
            ],
        },
    ],
}

c = TestClient(app)


def cab(bearer: str | None = None) -> dict:
    return {"x-api-key": SERVICIO, **({"Authorization": f"Bearer {bearer}"} if bearer else {})}


def entrar(correo: str, contrasena: str) -> dict:
    r = c.post("/auth/login", json={"email": correo, "password": contrasena, "ip": "127.0.0.1", "userAgent": "verificar"}, headers=cab())
    assert r.status_code == 200, r.text
    return r.json()


def con_primer_acceso(correo: str, temporal: str, definitiva: str) -> str:
    s = entrar(correo, temporal)
    r = c.post("/auth/contrasena", json={"nueva": definitiva, "confirmacion": definitiva}, headers=cab(s["accessToken"]))
    assert r.status_code == 200, r.text
    return entrar(correo, definitiva)["accessToken"]


def crear_organizacion(bearer_super: str, nombre: str, correo: str, contrasena: str) -> tuple[str, str]:
    r = c.post("/organizaciones/", json={"nombre": nombre, "adminNombre": "Admin", "adminApellidoPaterno": nombre, "adminEmail": correo}, headers=cab(bearer_super))
    assert r.status_code == 201, r.text
    return r.json()["organizacion"]["guid"], con_primer_acceso(correo, r.json()["contrasenaTemporal"], contrasena)


# ── El Google falso ─────────────────────────────────────────────────────────────────────────────────────────────────────────
PADRE = svc._PADRE
google = {
    "recursos": [],  # [{"name", "displayName"}]
    "paginas": 1,  # en cuántas páginas se reparte el listado de recursos
    "versiones": [],  # names de schemaVersion vivas
    "llamadas": [],  # (metodo, ruta, json)
    "esquema": FACTURA,  # lo que «propone» el modelo
    "falla_generar": None,  # DocumentAIError | Exception que lanza el próximo generate
    "falla_borrar": False,
    "siguiente": 0,
}


async def pedir_falso(cliente, metodo, api, ruta, *, timeout=60, **kw):
    google["llamadas"].append((metodo, ruta, kw.get("json")))
    if metodo == "GET" and ruta == f"{PADRE}/schemas":
        # Paginación: el recurso que se busca queda en la ÚLTIMA página, detrás de otros.
        pagina = int((kw.get("params") or {}).get("pageToken") or 0)
        relleno = [{"name": f"{PADRE}/schemas/ajeno-{pagina}", "displayName": f"otro-{pagina}"}]
        if pagina + 1 < google["paginas"]:
            return {"schemas": relleno, "nextPageToken": str(pagina + 1)}
        return {"schemas": relleno + google["recursos"]}
    if metodo == "POST" and ruta == f"{PADRE}/schemas":
        await asyncio.sleep(0)  # deja pasar a la otra petición: sin candado, las dos crearían
        google["siguiente"] += 1
        recurso = {"name": f"{PADRE}/schemas/gen-{google['siguiente']}", "displayName": kw["json"]["displayName"]}
        google["recursos"].append(recurso)
        return recurso
    if metodo == "POST" and ruta.endswith("/schemaVersions:generate"):
        padre = ruta.rsplit("/schemaVersions:generate", 1)[0]
        if padre not in [r["name"] for r in google["recursos"]]:
            raise DocumentAIError(f"Document AI respondió 404 en POST {ruta}: Schema not found", status_code=404)
        falla = google["falla_generar"]
        if falla is not None:
            google["falla_generar"] = None
            raise falla
        google["siguiente"] += 1
        version = f"{padre}/schemaVersions/v-{google['siguiente']}"
        google["versiones"].append(version)
        return {"schemaVersion": {"name": version, "schema": google["esquema"]}}
    if metodo == "DELETE" and "/schemaVersions/" in ruta:
        if google["falla_borrar"]:
            raise DocumentAIError(f"Document AI respondió 503 en DELETE {ruta}", status_code=503)
        google["versiones"].remove(ruta)
        return {"name": f"{PADRE}/operations/1"}
    raise AssertionError(f"Llamada inesperada a Google: {metodo} {ruta}")


svc._pedir = pedir_falso  # type: ignore[assignment]


def reiniciar_google() -> None:
    google.update(recursos=[], paginas=1, versiones=[], llamadas=[], esquema=FACTURA, falla_generar=None, falla_borrar=False)
    svc._recurso_generador.clear()


def generaciones() -> list[tuple]:
    return [ll for ll in google["llamadas"] if ll[1].endswith(":generate")]


try:
    titulo("1 · Nombres legibles")
    rev("snake_case → palabras, la primera con mayúscula", nombre_legible("fecha_emision") == "Fecha emision")
    rev("las siglas van en mayúsculas", nombre_legible("rfc_cliente") == "RFC cliente" and nombre_legible("uso_cfdi") == "Uso CFDI")
    rev("una sigla al inicio también", nombre_legible("curp") == "CURP")
    rev("guiones y espacios de más no dejan huecos", nombre_legible("_total__con-letra_") == "Total con letra")
    rev("un nombre vacío no queda vacío", nombre_legible("") == "Campo")

    titulo("2 · La factura real, convertida a campos")
    campos = campos_desde_esquema(FACTURA)
    por_nombre = {c["nombre"]: c for c in campos}
    rev("un campo por propiedad, en el mismo orden", [c["nombre"] for c in campos] == [
        "Nombre proveedor", "RFC proveedor", "Direccion proveedor", "Colonia proveedor", "Fecha emision", "Uso CFDI", "Line item",
        "Porcentaje IVA", "Total"], [c["nombre"] for c in campos])
    rev("string → texto, datetime → fecha, number → número, money → moneda", [por_nombre[n]["tipoDato"] for n in (
        "Nombre proveedor", "Fecha emision", "Porcentaje IVA", "Total")] == ["texto", "fecha", "numero", "moneda"])
    rev("address (domicilio entero) → texto, porque el asistente no tiene ese tipo", por_nombre["Direccion proveedor"]["tipoDato"] == "texto")
    rev("la descripción del modelo viaja tal cual", por_nombre["Uso CFDI"]["descripcion"] == "Uso que se le dará al Comprobante Fiscal Digital por Internet.")
    rev("OPTIONAL_ONCE → opcional y único", por_nombre["Total"]["obligatorio"] is False and por_nombre["Total"]["cardinalidad"] == "unico")
    conceptos = por_nombre["Line item"]
    rev("los conceptos son un grupo repetible", conceptos["tipoDato"] == "grupo" and conceptos["cardinalidad"] == "multiple")
    rev("con sus tres campos y sus tipos", [(s["nombre"], s["tipoDato"]) for s in conceptos["subcampos"]] == [
        ("Cantidad", "numero"), ("Descripcion", "texto"), ("Precio unitario", "moneda")], conceptos["subcampos"])
    rev("cada subcampo trae su descripción", conceptos["subcampos"][0]["descripcion"] == "Cantidad de unidades del artículo.")
    rev("un campo suelto no trae subcampos ni valores de lista", por_nombre["Total"]["subcampos"] == [] and por_nombre["Total"]["valoresLista"] == [])

    titulo("3 · Lo que la factura no trae")
    raros = campos_desde_esquema({
        "entityTypes": [
            {"name": "custom_extraction_document_type", "baseTypes": ["document"], "properties": [
                prop("estado_civil", "estado_civil_valores", "REQUIRED_ONCE", "Estado civil"),
                prop("telefono", "string", "REQUIRED_MULTIPLE"),
                prop("aceptado", "checkbox"),
                prop("socio", "socio", "OPTIONAL_MULTIPLE"),
                prop("vacio", "vacio"),
                prop("rarisimo", "tipo_que_no_existe"),
                prop("rfc", "string"),
                prop("RFC", "string"),
                {"name": "", "valueType": "string"},
                {"name": "sin_ocurrencia", "valueType": "string"},
            ]},
            {"name": "estado_civil_valores", "baseTypes": ["string"], "enumValues": {"values": ["Soltero", "Casado", " "]}},
            {"name": "socio", "baseTypes": ["object"], "properties": [
                prop("nombre", "string", "REQUIRED_ONCE"),
                prop("rol", "estado_civil_valores"),
                prop("domicilio", "domicilio_socio"),
            ]},
            {"name": "domicilio_socio", "baseTypes": ["object"], "properties": [prop("calle")]},
            {"name": "vacio", "baseTypes": ["object"], "properties": []},
        ]
    })
    r = {c["nombre"]: c for c in raros}
    rev("enumValues → lista con sus valores (sin los vacíos)", r["Estado civil"]["tipoDato"] == "lista" and r["Estado civil"]["valoresLista"] == ["Soltero", "Casado"])
    rev("REQUIRED_ONCE → obligatorio", r["Estado civil"]["obligatorio"] is True and r["Estado civil"]["cardinalidad"] == "unico")
    rev("REQUIRED_MULTIPLE → obligatorio y múltiple", r["Telefono"]["obligatorio"] is True and r["Telefono"]["cardinalidad"] == "multiple")
    rev("checkbox → booleano", r["Aceptado"]["tipoDato"] == "booleano")
    rev("un grupo con una lista adentro: la lista queda como texto (el asistente no captura listas en un grupo)",
        [(s["nombre"], s["tipoDato"]) for s in r["Socio"]["subcampos"]][:2] == [("Nombre", "texto"), ("Rol", "texto")])
    rev("un objeto dentro de un grupo queda como texto (un solo nivel)", r["Socio"]["subcampos"][2] == {
        "nombre": "Domicilio", "tipoDato": "texto", "descripcion": "", "obligatorio": False})
    rev("y el subcampo obligatorio se respeta", r["Socio"]["subcampos"][0]["obligatorio"] is True)
    rev("un objeto sin propiedades no es grupo: texto", r["Vacio"]["tipoDato"] == "texto" and r["Vacio"]["subcampos"] == [])
    rev("un valueType desconocido → texto", r["Rarisimo"]["tipoDato"] == "texto")
    rev("dos nombres que serían el mismo campo: se queda el primero", [c["nombre"] for c in raros].count("RFC") == 1)
    rev("una propiedad sin nombre se ignora", all(c["nombre"] for c in raros) and len(raros) == 8, [c["nombre"] for c in raros])
    rev("sin occurrenceType: opcional y único", r["Sin ocurrencia"]["obligatorio"] is False and r["Sin ocurrencia"]["cardinalidad"] == "unico")
    rev("un esquema sin tipo raíz no da campos", campos_desde_esquema({"entityTypes": [{"name": "x", "baseTypes": ["object"]}]}) == [])
    rev("un esquema vacío tampoco", campos_desde_esquema({}) == [])
    alterno = campos_desde_esquema({"entityTypes": [{"name": "factura_raiz", "baseTypes": ["document"], "properties": [prop("folio")]}]})
    rev("si la raíz no se llama como la nuestra, se encuentra igual", [c["nombre"] for c in alterno] == ["Folio"])

    titulo("4 · Ida y vuelta: campos → esquema → campos")
    originales = [
        {"nombre": "Folio", "tipoDato": "texto", "descripcion": "El folio", "obligatorio": True, "cardinalidad": "unico", "valoresLista": [], "subcampos": []},
        {"nombre": "Fecha de pago", "tipoDato": "fecha", "descripcion": "", "obligatorio": False, "cardinalidad": "unico", "valoresLista": [], "subcampos": []},
        {"nombre": "Montos", "tipoDato": "moneda", "descripcion": "", "obligatorio": False, "cardinalidad": "multiple", "valoresLista": [], "subcampos": []},
        {"nombre": "Pagado", "tipoDato": "booleano", "descripcion": "", "obligatorio": True, "cardinalidad": "unico", "valoresLista": [], "subcampos": []},
        {"nombre": "Nivel", "tipoDato": "lista", "descripcion": "", "obligatorio": True, "cardinalidad": "unico", "valoresLista": ["Alto", "Bajo"], "subcampos": []},
        {"nombre": "Accionistas", "tipoDato": "grupo", "descripcion": "", "obligatorio": False, "cardinalidad": "multiple", "valoresLista": [], "subcampos": [
            {"nombre": "Nombre", "tipoDato": "texto", "descripcion": "", "obligatorio": True},
            {"nombre": "Acciones", "tipoDato": "numero", "descripcion": "", "obligatorio": False},
        ]},
    ]
    vuelta = campos_desde_esquema(esquema_desde_campos("Tipo", "", originales))
    clave = lambda cs: [(c["tipoDato"], c["obligatorio"], c["cardinalidad"], c["valoresLista"], [(s["tipoDato"], s["obligatorio"]) for s in c["subcampos"]]) for c in cs]  # noqa: E731
    rev("tipo, obligatorio, cardinalidad, valores y subcampos sobreviven el viaje", clave(vuelta) == clave(originales), clave(vuelta))
    rev("y los nombres regresan legibles", [c["nombre"] for c in vuelta] == ["Folio", "Fecha de pago", "Montos", "Pagado", "Nivel", "Accionistas"])

    titulo("5 · El servicio contra el Google falso")
    reiniciar_google()
    google["paginas"] = 3
    google["recursos"] = [{"name": f"{PADRE}/schemas/existente", "displayName": svc.NOMBRE_RECURSO_GENERADOR}]
    campos = asyncio.run(svc.generar_campos(b"%PDF-1.4 ejemplo", "application/pdf", "Factura de proveedor", "La que nos mandan"))
    rev("devuelve los campos de lo propuesto", [c["nombre"] for c in campos][:2] == ["Nombre proveedor", "RFC proveedor"])
    rev("encontró el recurso existente aunque estaba en la tercera página, y no creó otro",
        len(google["recursos"]) == 1 and generaciones()[0][1] == f"{PADRE}/schemas/existente/schemaVersions:generate")
    cuerpo = generaciones()[0][2]
    documento = cuerpo["rawDocuments"]["documents"][0]
    rev("manda el documento en base64 con su tipo", base64.b64decode(documento["content"]) == b"%PDF-1.4 ejemplo" and documento["mimeType"] == "application/pdf")
    instruccion = cuerpo["generateSchemaVersionParams"]["prompt"]
    rev("pide todo en español", "español" in instruccion and "snake_case" in instruccion)
    rev("y lleva el nombre y la descripción del tipo como contexto", "Factura de proveedor" in instruccion and "La que nos mandan" in instruccion)
    rev("borró la versión que dejó la generación", google["versiones"] == [])

    antes = len(google["llamadas"])
    asyncio.run(svc.generar_campos(b"x", "image/png"))
    rev("la segunda vez ya no busca el recurso: lo recuerda", [ll[0] for ll in google["llamadas"][antes:]] == ["POST", "DELETE"])
    rev("sin nombre ni descripción, la instrucción no inventa contexto", "El documento es de este tipo" not in generaciones()[-1][2]["generateSchemaVersionParams"]["prompt"])

    reiniciar_google()

    async def dos_a_la_vez():
        return await asyncio.gather(svc.generar_campos(b"a", "application/pdf"), svc.generar_campos(b"b", "application/pdf"))

    asyncio.run(dos_a_la_vez())
    rev("sin recurso, lo crea UNA sola vez aunque lleguen dos peticiones a la vez", len(google["recursos"]) == 1, google["recursos"])
    rev("con el nombre visible acordado", google["recursos"][0]["displayName"] == "nexusdoc-generador-esquemas")

    google["falla_borrar"] = True
    campos = asyncio.run(svc.generar_campos(b"a", "application/pdf"))
    rev("si no se puede borrar la versión, la propuesta llega igual", len(campos) == 9 and len(google["versiones"]) == 1)
    google["falla_borrar"] = False

    google["recursos"].clear()  # alguien lo borró a mano en la consola
    campos = asyncio.run(svc.generar_campos(b"a", "application/pdf"))
    rev("si el recurso padre desapareció, crea otro y reintenta una vez", len(campos) == 9 and len(google["recursos"]) == 1)

    google["falla_generar"] = DocumentAIError("Document AI respondió 400: Invalid document", status_code=400)
    try:
        asyncio.run(svc.generar_campos(b"a", "application/pdf"))
        rev("un 400 de Google se propaga", False)
    except DocumentAIError as exc:
        rev("un 400 de Google se propaga con su status", exc.status_code == 400)

    titulo("6 · La ruta: quién puede y qué se le dice")
    reiniciar_google()
    temporal = auth.contrasena_temporal()
    usuarios.bootstrap_admin("super@ejemplo.com", "Ada", "Lovelace", auth.hash_de(temporal))
    bearer_super = con_primer_acceso("super@ejemplo.com", temporal, "Cobre-Lento-Azul-47!")
    org_a, a_admin = crear_organizacion(bearer_super, "Alfa", "rita@ejemplo.com", "Rumbo-Sereno-Azul-47!")
    org_b, b_admin = crear_organizacion(bearer_super, "Beta", "beto@ejemplo.com", "Rumbo-Sereno-Rojo-48!")
    personas = {}
    for rol in ("SUPERVISOR", "OPERADOR", "VIEWER"):
        correo = f"{rol.lower()}@ejemplo.com"
        r = c.post("/usuarios/", json={"nombre": rol.title(), "apellidoPaterno": "Prueba", "email": correo, "rol": rol}, headers=cab(a_admin))
        assert r.status_code == 201, r.text
        personas[rol] = con_primer_acceso(correo, r.json()["contrasenaTemporal"], "Cielo-Abierto-Verde-52!")

    def generar(bearer, tenant, contenido=b"%PDF-1.4 ejemplo", tipo="application/pdf", **extra):
        return c.post(
            "/procesadores/generar-campos",
            data={"tenant": tenant, **extra},
            files={"archivo": ("ejemplo.pdf", contenido, tipo)},
            headers=cab(bearer),
        )

    r = generar(a_admin, org_a, nombre="Factura", descripcion="De proveedor")
    rev("el administrador recibe los campos propuestos", r.status_code == 200 and len(r.json()["campos"]) == 9, r.text[:200])
    rev("con la forma que entiende el paso 2", set(r.json()["campos"][0]) == {"nombre", "tipoDato", "descripcion", "obligatorio", "cardinalidad", "valoresLista", "subcampos"})
    rev("y el contexto del tipo llegó a Google", "Factura De proveedor" in generaciones()[-1][2]["generateSchemaVersionParams"]["prompt"])
    rev("el supervisor también puede (administra tipos documentales)", generar(personas["SUPERVISOR"], org_a).status_code == 200)

    antes = len(generaciones())
    r = generar(personas["OPERADOR"], org_a)
    rev("un operador recibe 403 sin_permiso", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_permiso", r.text[:200])
    rev("un viewer, también", generar(personas["VIEWER"], org_a).status_code == 403)
    r = generar(b_admin, org_a)
    rev("el administrador de otra organización recibe 403 sin_acceso", r.status_code == 403 and r.json()["detail"]["codigo"] == "sin_acceso", r.text[:200])
    rev("sin sesión, 401", generar(None, org_a).status_code == 401)
    rev("y en ninguno de esos casos se le llamó a Google", len(generaciones()) == antes)

    r = generar(a_admin, org_a, contenido=b"")
    rev("un archivo vacío se rechaza antes de ir a Google", r.status_code == 400 and len(generaciones()) == antes, r.text[:200])
    r = generar(a_admin, org_a, tipo="text/plain")
    rev("un formato no admitido también", r.status_code in (400, 415) and len(generaciones()) == antes, f"{r.status_code} {r.text[:200]}")

    google["falla_generar"] = DocumentAIError("Document AI respondió 400 en POST projects/123/...: Document pages exceed the limit", status_code=400)
    r = generar(a_admin, org_a)
    rev("si Google no puede leer el documento: 422 con un mensaje para la persona", r.status_code == 422 and "no pudo leer este documento" in r.json()["detail"], r.text[:200])
    rev("sin el texto ni las rutas de Google", "projects/" not in r.text and "exceed" not in r.text)
    google["falla_generar"] = DocumentAIError("Document AI respondió 503 en POST projects/123/...", status_code=503)
    r = generar(a_admin, org_a)
    rev("si Google falla de su lado: 502 con «intenta de nuevo»", r.status_code == 502 and "Intenta de nuevo" in r.json()["detail"] and "projects/" not in r.text, r.text[:200])
    google["falla_generar"] = httpx.ReadTimeout("tardó")
    r = generar(a_admin, org_a)
    rev("si Google no responde a tiempo: 504", r.status_code == 504 and "tardó demasiado" in r.json()["detail"], r.text[:200])
    google["esquema"] = {"entityTypes": []}
    r = generar(a_admin, org_a)
    rev("si no propone nada, la lista llega vacía (no es un error)", r.status_code == 200 and r.json()["campos"] == [])
finally:
    shutil.rmtree(RAIZ, ignore_errors=True)

print(f"\n{'TODO BIEN' if fallos == 0 else f'{fallos} FALLA(S)'}")
raise SystemExit(1 if fallos else 0)
