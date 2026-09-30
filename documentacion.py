"""Las dos documentaciones de la API: la PÚBLICA, para clientes, y la INTERNA.

    /docs            → pública: solo "mandar un documento" (POST /bandeja/)
    /docs-interno    → interna: todo, como era /docs hasta el 2026-09-30

Es la práctica común en una API que tiene clientes: la documentación que ve
el cliente trae SOLO lo que él puede usar, y las rutas de administración no se
exhiben. Ocultarlas NO es la seguridad —eso lo dan las llaves: una API Key de
cliente solo abre `POST /bandeja/`, y todo lo demás le contesta 401 se vea o
no en la documentación—, es orden: el cliente ve lo suyo, y no se publica cómo
está armado el sistema por dentro.

LA PÚBLICA SE DERIVA DE LA INTERNA, no se escribe aparte: se toma el esquema
completo que genera FastAPI y se deja solo la operación de subir, con textos
pensados para quien integra. Así no puede desfasarse de lo que la API hace de
verdad: si mañana cambia un parámetro de la ruta, la pública cambia con él.
Lo que sí se reescribe a mano es lo que un cliente necesita leer distinto:
la descripción, las respuestas con ejemplo, y el campo `tenant`, que se quita
porque con su llave el cliente no lo manda (sale de la llave).

Cuando la API se publique hacia fuera, solo se expone la pública.
"""

import copy

from fastapi import FastAPI
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse

RUTA_PUBLICA = "/bandeja/"

_DESCRIPCION_PUBLICA = """
Por aquí se mandan documentos a **NexusDoc AI**. Cada documento que llega
entra a la **bandeja de preparación** y el equipo de operación lo ve en
segundos, listo para procesarse.

### Cómo autenticarse

Cada petición lleva tu **API Key** en el header `X-API-Key`:

```
X-API-Key: nxdoc_live_XXXX_sk_…
```

Aquí mismo puedes probarla: botón **Authorize**, pega tu llave, y luego
**Try it out** en la operación de abajo.

### Ejemplo

```bash
curl -X POST <servidor>/bandeja/ \\
  -H "X-API-Key: nxdoc_live_XXXX_sk_…" \\
  -F "archivo=@ine_juan_perez.pdf;type=application/pdf"
```

Tu llave vence en la fecha que se eligió al emitirla, y se puede revocar en
cualquier momento: a partir de ahí, cada petición con ella responde **401**.
"""

_OPERACION = """
Manda UN documento. Formatos: **PDF, JPEG, PNG y TIFF**, de hasta **20 MB**.

No hace falta decir de qué cliente es: sale de tu API Key.

Si mandas dos veces el mismo archivo, las dos quedan registradas y la segunda
se marca como duplicada en la bandeja.

**Opcional, para integraciones:** un campo `sha256` con el SHA-256 del archivo
(64 caracteres hexadecimales). Si lo mandas, el servidor comprueba que el
archivo llegó íntegro y, si no coincide, responde **400** sin guardarlo.
"""

_EJEMPLO_201 = {
    "id": "2ff89e2c5741435a83372bac1a202477",
    "tenant": "demo",
    "rutaRelativa": "demo/3f/a9/3fa9c1e04b…",
    "sha256": "3fa9c1e04b…",
    "tamanoBytes": 482133,
    "mime": "application/pdf",
    "nombreOriginal": "ine_juan_perez.pdf",
    "canal": "API",
    "llaveId": "a7K9",
    "recibidoEn": "2026-09-30T19:13:28+00:00",
}


def _error(descripcion: str, detalle: str) -> dict:
    return {
        "description": descripcion,
        "content": {"application/json": {"example": {"detail": detalle}}},
    }


def esquema_publico(completo: dict) -> dict:
    """Del esquema completo, solo lo que un cliente puede usar."""
    operacion = copy.deepcopy(completo["paths"][RUTA_PUBLICA]["post"])

    # El formulario, sin `tenant`: con una llave de cliente no se manda.
    contenido = operacion["requestBody"]["content"]["multipart/form-data"]
    nombre_cuerpo = contenido["schema"]["$ref"].split("/")[-1]
    cuerpo = copy.deepcopy(completo["components"]["schemas"][nombre_cuerpo])
    # Tampoco `sha256`, que es opcional: Swagger rellena con la palabra
    # "string" todo campo de texto al dar "Try it out", y el servidor —con
    # razón— rechaza ese hash con un 400 que desconcierta a quien solo quería
    # probar. Quien integre de verdad lo encuentra explicado en la descripción.
    for campo in ("tenant", "sha256"):
        cuerpo["properties"].pop(campo, None)
    if "required" in cuerpo:
        cuerpo["required"] = [c for c in cuerpo["required"] if c in cuerpo["properties"]]
    if "archivo" in cuerpo["properties"]:
        cuerpo["properties"]["archivo"]["description"] = "El documento: PDF, JPEG, PNG o TIFF, hasta 20 MB."
    nombre_publico = "Documento"
    contenido["schema"] = {"$ref": f"#/components/schemas/{nombre_publico}"}
    cuerpo["title"] = nombre_publico

    operacion.update(
        {
            "tags": ["Documentos"],
            "summary": "Mandar un documento",
            "description": _OPERACION,
            "operationId": "mandar_documento",
            "responses": {
                "201": {
                    "description": "Recibido. Ya está en la bandeja de preparación.",
                    "content": {"application/json": {"example": _EJEMPLO_201}},
                },
                "400": _error(
                    "El archivo no se acepta (formato, vacío, o el sha256 no coincide).",
                    "Formato no admitido en la bandeja. Se aceptan PDF, JPEG, PNG y TIFF.",
                ),
                "401": _error(
                    "Falta la API Key, no es válida, está revocada o ya venció.",
                    "Falta la llave de API o no es la correcta (header X-API-Key).",
                ),
                "403": _error(
                    "Se mandó `tenant` y no es el de tu llave. No lo mandes: sale de la llave.",
                    "Esta API Key es de otro cliente. Omite `tenant`: se toma de la llave.",
                ),
                "413": _error("El archivo pasa de 20 MB.", "El archivo excede el límite de 20 MB."),
                "503": _error(
                    "El servicio no está disponible en este momento. Intenta de nuevo en unos minutos.",
                    "La bandeja no está disponible en este momento. Intenta de nuevo en unos minutos.",
                ),
            },
        }
    )

    esquema_llave = copy.deepcopy(completo["components"]["securitySchemes"]["APIKeyHeader"])
    esquema_llave["description"] = "Tu API Key de NexusDoc (`nxdoc_live_…`)."

    return {
        "openapi": completo["openapi"],
        "info": {
            "title": "NexusDoc AI · API para clientes",
            "version": completo["info"].get("version", ""),
            "description": _DESCRIPCION_PUBLICA,
        },
        "tags": [{"name": "Documentos", "description": "Mandar documentos a NexusDoc AI."}],
        "paths": {RUTA_PUBLICA: {"post": operacion}},
        "components": {
            "schemas": {nombre_publico: cuerpo},
            "securitySchemes": {"APIKeyHeader": esquema_llave},
        },
    }


def instalar(app: FastAPI) -> None:
    """Registra las dos documentaciones. La app tiene que crearse con
    `docs_url=None, redoc_url=None, openapi_url=None`, para que estas rutas
    ocupen el lugar de las automáticas."""
    cache: dict[str, dict] = {}

    def publico() -> dict:
        if "publico" not in cache:
            cache["publico"] = esquema_publico(app.openapi())
        return cache["publico"]

    @app.get("/openapi.json", include_in_schema=False)
    def openapi_publico() -> JSONResponse:
        return JSONResponse(publico())

    @app.get("/docs", include_in_schema=False)
    def docs_publico() -> HTMLResponse:
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title="NexusDoc AI · API para clientes",
            # Sin la sección "Schemas" al pie: al cliente no le dice nada que
            # no esté ya en la operación.
            swagger_ui_parameters={"defaultModelsExpandDepth": -1},
        )

    @app.get("/openapi-interno.json", include_in_schema=False)
    def openapi_interno() -> JSONResponse:
        return JSONResponse(app.openapi())

    @app.get("/docs-interno", include_in_schema=False)
    def docs_interno() -> HTMLResponse:
        return get_swagger_ui_html(openapi_url="/openapi-interno.json", title="NexusDoc AI · API interna")
