"""Traducción del wizard de NexusDoc al `DocumentSchema` de Document AI.

Este módulo es PURO a propósito: no toca red ni disco, solo transforma datos.
Así el mapeo —que es donde viven las decisiones delicadas— se puede verificar
con `verificar_esquema.py` sin credenciales y sin gastar una llamada.

Contexto que importa: el Custom Extractor moderno es un modelo fundacional
(Gemini) que extrae en zero-shot leyendo NADA MÁS el esquema. La documentación
dice literal que "Generative AI models use the field name and description to
form the underlying prompt" — o sea que el nombre y la descripción que el
usuario teclea en el paso 2 del wizard SON la instrucción de extracción, no
metadatos decorativos. Por eso la descripción viaja completa, y el "valor de
estructura" (el ejemplo) se le anexa: es exactamente el tipo de pista que un
prompt aprovecha.
"""

import re
import unicodedata

# Wizard -> valueType de Document AI. Los nombres del lado derecho no son
# inventados: son los que devuelve getDatasetSchema del procesador de INE ya
# existente ("string", "number", "datetime") más los documentados para moneda
# y casilla. `lista` NO está aquí porque no es un valueType: se modela como un
# EntityType propio con `enumValues` (ver `esquema_desde_campos`).
_VALUE_TYPES = {
    "texto": "string",
    "numero": "number",
    "fecha": "datetime",
    "moneda": "money",
    "booleano": "checkbox",
}

# (obligatorio, cardinalidad) -> OccurrenceType. El producto cartesiano cubre
# los cuatro valores del enum de Google, y cuenta INSTANCIAS del dato en el
# documento, no menciones repetidas del mismo valor.
_OCCURRENCE = {
    (True, "unico"): "REQUIRED_ONCE",
    (True, "multiple"): "REQUIRED_MULTIPLE",
    (False, "unico"): "OPTIONAL_ONCE",
    (False, "multiple"): "OPTIONAL_MULTIPLE",
}


def normalizar_nombre(nombre: str) -> str:
    """Convierte lo que el usuario tecleó en un nombre que Google acepta.

    Las reglas documentadas para el nombre de un EntityType: snake_case, hasta
    64 caracteres, empezar con letra, solo [a-z0-9_-]. Para Property la doc no
    las declara explícitamente, pero el esquema real del procesador de INE las
    cumple todas, así que se aplican parejo: un nombre que las cumple no puede
    estar mal.

    "Fecha de Nacimiento" -> "fecha_de_nacimiento"; "Año" -> "ano".
    """
    # Acentos fuera: NFD separa la letra del diacrítico y se tira el diacrítico.
    plano = unicodedata.normalize("NFD", nombre)
    plano = "".join(c for c in plano if unicodedata.category(c) != "Mn")
    # La ñ no es diacrítico y sobrevive el NFD: se traduce a mano.
    plano = plano.replace("ñ", "n").replace("Ñ", "n")
    plano = plano.lower().strip()
    plano = re.sub(r"[\s]+", "_", plano)
    plano = re.sub(r"[^a-z0-9_-]", "", plano)
    plano = re.sub(r"_+", "_", plano).strip("_-")
    if not plano or not plano[0].isalpha():
        plano = f"campo_{plano}" if plano else "campo"
    return plano[:64]


def _descripcion_de(campo: dict) -> str:
    """La descripción que verá el modelo: la del usuario más el ejemplo.

    El "valor de estructura" del paso 2 es un ejemplo de la forma esperada
    (`POL-2026-00045871`). No hay campo de "ejemplo" en el esquema de Google,
    pero anexarlo a la descripción lo mete al prompt, que es donde sirve.
    """
    desc = (campo.get("descripcion") or "").strip()
    ejemplo = (campo.get("valorEstructura") or "").strip()
    if ejemplo:
        sufijo = f"Ejemplo: {ejemplo}"
        desc = f"{desc} {sufijo}" if desc else sufijo
    return desc


def esquema_desde_campos(nombre_tipo: str, descripcion_tipo: str, campos: list[dict]) -> dict:
    """Arma el `DocumentSchema` (forma v1beta3) para un tipo documental.

    Forma v1beta3 y no v1 porque la Property de v1 NO tiene `description` — y
    sin descripciones el zero-shot pierde su palanca de calidad. Está
    verificado empíricamente: el mismo override que v1 rechaza por el campo
    `description`, v1beta3 lo acepta y extrae.

    Un campo `lista` se traduce en DOS piezas, siguiendo el patrón que el
    propio procesador de INE usa para `domicilio` (un valueType que nombra a
    otro EntityType del mismo esquema): la Property apunta por nombre a un
    EntityType auxiliar que carga los `enumValues`.
    """
    tipo_raiz = "custom_extraction_document_type"
    propiedades: list[dict] = []
    tipos_auxiliares: list[dict] = []
    vistos: set[str] = set()

    for campo in campos:
        nombre = normalizar_nombre(campo.get("nombre", ""))
        # Dos campos del wizard pueden colapsar al mismo nombre normalizado
        # ("Año" y "ano"). Google rechazaría el esquema entero; mejor un
        # sufijo determinista aquí que un 400 opaco allá.
        base, n = nombre, 2
        while nombre in vistos:
            nombre = f"{base}_{n}"[:64]
            n += 1
        vistos.add(nombre)

        obligatorio = bool(campo.get("obligatorio"))
        cardinalidad = "multiple" if campo.get("cardinalidad") == "multiple" else "unico"
        occurrence = _OCCURRENCE[(obligatorio, cardinalidad)]

        tipo_dato = campo.get("tipoDato", "texto")
        if tipo_dato == "lista":
            valores = [v.strip() for v in campo.get("valoresLista", []) if v and v.strip()]
            nombre_enum = f"{nombre}_valores"[:64]
            tipos_auxiliares.append(
                {
                    "name": nombre_enum,
                    "baseTypes": ["string"],
                    "enumValues": {"values": valores},
                }
            )
            value_type = nombre_enum
        else:
            value_type = _VALUE_TYPES.get(tipo_dato, "string")

        propiedades.append(
            {
                "name": nombre,
                "valueType": value_type,
                "occurrenceType": occurrence,
                "description": _descripcion_de(campo),
            }
        )

    return {
        "displayName": nombre_tipo.strip() or "Tipo documental",
        "description": (descripcion_tipo or "").strip(),
        "entityTypes": [
            {
                "name": tipo_raiz,
                "baseTypes": ["document"],
                "properties": propiedades,
            },
            *tipos_auxiliares,
        ],
    }


def esquema_clasificador_desde_tipos(tipos: list[dict]) -> dict:
    """Arma el `DocumentSchema` de un Custom Document Classifier a partir de la
    lista de tipos documentales ACTIVOS.

    A diferencia de un Extractor (UN EntityType raíz con `properties` a
    extraer, ver `esquema_desde_campos`), un Classifier tiene VARIOS EntityTypes
    hoja, uno por categoría a distinguir — cada uno con `baseTypes: ["document"]`
    (aplica al documento ENTERO, no a un campo puntual) y SIN `properties`: no
    extrae nada, solo dice a cuál categoría pertenece el documento completo.
    `metadata.documentSplitter: false` es lo que le confirma a Document AI que
    estas categorías cubren el documento entero — no son límites de partición
    de un archivo compuesto (eso sería un Custom Document Splitter, un
    procesador distinto). Verificado contra la referencia REST real de
    `DocumentSchema.metadata.documentSplitter`: "If true, a `document` entity
    type can be applied to subdocument (splitting). Otherwise, it can only be
    applied to the entire document (classification)."

    El `name` de cada EntityType es el ID ESTABLE del tipo documental, NUNCA su
    nombre visible — mismo criterio que `_prefijo_display` en
    `procesadores.py`: el nombre se puede editar cuando quiera el usuario, el id
    no, y el pipeline necesita poder mapear la categoría que devuelva Document
    AI de vuelta a un tipo documental sin que un renombre lo rompa. El nombre
    visible SÍ viaja, pero en `displayName` — para que la consola de Google (y
    cualquier humano viendo el esquema ahí) lea algo legible en vez de un id.
    """
    entity_types = [
        {
            "name": normalizar_nombre(tipo["id"]),
            "displayName": (tipo.get("nombre") or tipo["id"]).strip() or tipo["id"],
            "baseTypes": ["document"],
        }
        for tipo in tipos
    ]
    return {
        "displayName": "Clasificador de tipos documentales NexusDoc",
        "description": "Distingue a cuál tipo documental activo pertenece un documento entrante.",
        "metadata": {"documentSplitter": False},
        "entityTypes": entity_types,
    }
