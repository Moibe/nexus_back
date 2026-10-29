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

# La categoría de escape del Classifier: "no es ninguno de los tipos
# configurados". Se agrega SIEMPRE en `esquema_clasificador_desde_tipos` (ver
# su docstring para el porqué medido) y el front la conoce por este mismo
# nombre — cambiarlo aquí sin cambiarlo allá deja lo desconocido cayendo en el
# camino de "categoría que no sé mapear" en vez del de "manda esto a revisión".
CATEGORIA_OTRO = "otro"


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


#: El tipo de dato «grupo de campos»: un campo que a su vez trae campos (accionista = nombre + acciones + monto). Con
#: cardinalidad `multiple` se repite (una persona por renglón de la tabla); con `unico`, aparece una vez (un domicilio).
TIPO_GRUPO = "grupo"


def _nombre_unico(crudo: str, vistos: set[str]) -> str:
    """El nombre normalizado, con sufijo determinista si ya hay otro igual en el MISMO nivel.

    Dos campos del wizard pueden colapsar al mismo nombre normalizado ("Año" y "ano"). Google rechazaría el esquema entero;
    mejor un sufijo determinista aquí que un 400 opaco allá."""
    nombre = normalizar_nombre(crudo)
    base, n = nombre, 2
    while nombre in vistos:
        nombre = f"{base}_{n}"[:64]
        n += 1
    vistos.add(nombre)
    return nombre


def _propiedad(campo: dict, nombre: str, tipos_auxiliares: list[dict], prefijo_auxiliar: str = "") -> dict:
    """La `Property` de un campo que NO es grupo. Si es `lista`, además agrega a `tipos_auxiliares` el EntityType con sus
    `enumValues` (`prefijo_auxiliar` evita que dos subcampos de grupos distintos con el mismo nombre choquen)."""
    obligatorio = bool(campo.get("obligatorio"))
    cardinalidad = "multiple" if campo.get("cardinalidad") == "multiple" else "unico"
    occurrence = _OCCURRENCE[(obligatorio, cardinalidad)]

    tipo_dato = campo.get("tipoDato", "texto")
    if tipo_dato == "lista":
        valores = [v.strip() for v in campo.get("valoresLista", []) if v and v.strip()]
        nombre_enum = f"{prefijo_auxiliar}{nombre}_valores"[:64]
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

    return {
        "name": nombre,
        "valueType": value_type,
        "occurrenceType": occurrence,
        "description": _descripcion_de(campo),
    }


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

    Un campo `grupo` (2026-10-16) usa el MISMO patrón, tal como lo declara el
    procesador de INE para `domicilio`: la Property apunta por nombre a un
    EntityType auxiliar con `baseTypes: ["object"]` que trae sus propias
    `properties` (los subcampos). Con cardinalidad múltiple la Property es
    `*_MULTIPLE` y cada instancia —un accionista, un cargo— llega como una entidad
    con sus hijas. Un solo nivel: los subcampos son campos sueltos (texto, número,
    fecha, moneda, casilla o lista), no otro grupo. Un grupo sin subcampos no se
    puede declarar (un objeto sin propiedades no dice qué extraer) y se omite;
    quien llama —`/procesadores/activar`— ya lo rechaza antes con un 422.
    """
    tipo_raiz = "custom_extraction_document_type"
    propiedades: list[dict] = []
    tipos_auxiliares: list[dict] = []
    vistos: set[str] = set()

    for campo in campos:
        nombre = _nombre_unico(campo.get("nombre", ""), vistos)

        if campo.get("tipoDato") != TIPO_GRUPO:
            propiedades.append(_propiedad(campo, nombre, tipos_auxiliares))
            continue

        vistos_hijos: set[str] = set()
        hijos: list[dict] = []
        for sub in campo.get("subcampos", []):
            if sub.get("tipoDato") == TIPO_GRUPO:
                continue  # un solo nivel de anidación
            hijos.append(_propiedad(sub, _nombre_unico(sub.get("nombre", ""), vistos_hijos), tipos_auxiliares, f"{nombre}_"))
        if not hijos:
            continue

        tipos_auxiliares.append({"name": nombre, "displayName": nombre, "baseTypes": ["object"], "properties": hijos})
        obligatorio = bool(campo.get("obligatorio"))
        cardinalidad = "multiple" if campo.get("cardinalidad") == "multiple" else "unico"
        propiedades.append(
            {
                "name": nombre,
                "valueType": nombre,
                "occurrenceType": _OCCURRENCE[(obligatorio, cardinalidad)],
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

    `descripcion` (opcional) es la MISMA descripción que el usuario ya teclea
    al dar de alta el tipo documental (`descripcion_tipo` de
    `esquema_desde_campos`) — no una nueva captura. La consola de Google llama
    a esto justo "una descripción/prompt que ayuda al modelo a diferenciar
    etiquetas escritas de forma parecida" (verificado contra la guía de Custom
    Classifier), o sea que cumple el mismo rol aquí que en el Extractor: es
    prompt real, no metadato decorativo. Se omite del EntityType si viene
    vacía, en vez de mandar un `description: ""` sin sentido.

    **Siempre se agrega la categoría `otro`, además de los tipos recibidos**
    (2026-09-07, después de un fallo real en producción). No es decorativa ni
    opcional: un Classifier SOLO con categorías reales no tiene dónde poner un
    documento desconocido, y **fuerza** todo lo que entra a alguna de las que
    sí tiene. Está MEDIDO dos veces en este proyecto: primero con un
    clasificador de una sola etiqueta (una captura de Figma salió como INE con
    99.99% de confianza), y otra vez el 2026-09-07, cuando el front empezó a
    sincronizar solo y ESTA función reemplazó el esquema que se había armado a
    mano —que sí traía `otro`— por uno sin él: un boleto de autobús y un PNG
    corrupto salieron clasificados como un tipo documental de seguros. La guía
    de Google recomienda justo esto (una etiqueta explícita para "ninguna de
    las anteriores") y es lo que hace posible el único desenlace que el
    producto quiere para lo desconocido: mandarlo a revisión humana o darlo de
    alta como tipo nuevo, nunca extraerlo con el procesador equivocado.
    """
    entity_types = []
    for tipo in tipos:
        nombre_categoria = normalizar_nombre(tipo["id"])
        # `otro` está RESERVADO para la categoría de abajo. Con los ids que
        # genera el front (`tipo-{base36}-{n}`) esto no puede pasar, pero un id
        # que colisionara mandaría dos EntityTypes con el mismo `name` y
        # Document AI rechazaría el esquema entero — o peor, se comería la
        # categoría de escape sin avisar.
        if nombre_categoria == CATEGORIA_OTRO:
            nombre_categoria = f"{CATEGORIA_OTRO}_{normalizar_nombre(tipo['id'])}_tipo"
        entity_type = {
            "name": nombre_categoria,
            "displayName": (tipo.get("nombre") or tipo["id"]).strip() or tipo["id"],
            "baseTypes": ["document"],
        }
        descripcion = (tipo.get("descripcion") or "").strip()
        if descripcion:
            entity_type["description"] = descripcion
        entity_types.append(entity_type)

    entity_types.append(
        {
            "name": CATEGORIA_OTRO,
            "displayName": "Otro (ningún tipo documental configurado)",
            "baseTypes": ["document"],
            # La descripción es prompt real (verificado en la guía del Custom
            # Classifier: "Use the description field to enter a prompt which
            # describes the label"), así que dice explícitamente qué cae aquí:
            # es lo que le da al modelo permiso de NO elegir ninguna de las
            # otras categorías.
            #
            # OJO con calibrarla. La primera versión (2026-09-07) terminaba en
            # "...incluso si se parece un poco a alguno", y eso no es neutral:
            # es una instrucción activa de preferir el escape ante una
            # coincidencia parcial. Con las categorías reales identificadas
            # solo por un id opaco, esta categoría acababa siendo la MEJOR
            # descrita del esquema — o sea, la más fácil de elegir. Se quitó
            # esa frase el 2026-09-08. El equilibrio que se busca: que exista
            # una salida para lo desconocido (sin ella el modelo fuerza todo a
            # una categoría real, medido dos veces en este proyecto) sin que
            # sea más atractiva que las categorías de verdad.
            "description": (
                "Documento que no corresponde a ninguna de las otras "
                "categorías de este clasificador."
            ),
        }
    )

    return {
        "displayName": "Clasificador de tipos documentales NexusDoc",
        "description": "Distingue a cuál tipo documental activo pertenece un documento entrante.",
        "metadata": {"documentSplitter": False},
        "entityTypes": entity_types,
    }


#: valueType de Google -> tipo de dato del wizard, para LEER un esquema que propuso Google (`campos_desde_esquema`). Es el
#: inverso de `_VALUE_TYPES` más los que Google usa y el wizard no tiene: `address` (lo propone para un domicilio entero) cae a
#: texto, igual que cualquier valueType que no se conozca. Medido el 2026-10-29 con `schemaVersions:generate` sobre una factura:
#: string, number, datetime, money, address y el nombre de un EntityType `object` (los conceptos, como grupo repetible).
_TIPO_DE_VALUE_TYPE = {
    "string": "texto",
    "number": "numero",
    "integer": "numero",
    "float": "numero",
    "datetime": "fecha",
    "date": "fecha",
    "money": "moneda",
    "checkbox": "booleano",
    "boolean": "booleano",
    "address": "texto",
}

#: Palabras que en un nombre legible van en mayúsculas («RFC del cliente», no «Rfc del cliente»).
_SIGLAS = {"rfc", "curp", "cfdi", "iva", "isr", "ieps", "nss", "ine", "imss", "sat", "cp", "clabe", "id", "uuid", "url", "iban", "swift"}


def nombre_legible(nombre: str) -> str:
    """`rfc_cliente` -> «RFC cliente»: el `name` de Google (snake_case, sin acentos) vuelto algo que una persona lee en el wizard.
    Los acentos no se pueden recuperar; el nombre queda editable en el paso 2. Al activar, `normalizar_nombre` lo devuelve a la
    forma de Google."""
    palabras = [p for p in re.split(r"[_\-\s]+", nombre or "") if p]
    if not palabras:
        return "Campo"
    legibles = [p.upper() if p.lower() in _SIGLAS else p.lower() for p in palabras]
    if legibles[0].islower():
        legibles[0] = legibles[0][:1].upper() + legibles[0][1:]
    return " ".join(legibles)


def campos_desde_esquema(esquema: dict) -> list[dict]:
    """Los CAMPOS del wizard que corresponden a un `DocumentSchema` de extractor: el camino inverso de `esquema_desde_campos`.

    Sirve para «Generar a partir de un documento» (paso 2): Google propone un esquema leyendo un documento de ejemplo y aquí se
    convierte en campos que la persona revisa y agrega. Cada campo trae `nombre` (legible), `tipoDato`, `descripcion`,
    `obligatorio`, `cardinalidad`, `valoresLista` y `subcampos`, lo mismo que captura el paso 2.

    - Una Property cuyo `valueType` nombra un EntityType `object` con `properties` es un **grupo** (los conceptos de una factura);
      sus hijas son los subcampos. Un solo nivel, como en el wizard: un objeto dentro de un grupo queda como texto.
    - Una Property cuyo `valueType` nombra un EntityType con `enumValues` es una **lista** con esos valores. Dentro de un grupo el
      wizard no captura listas, así que ahí queda como texto.
    - El `occurrenceType` da obligatorio (REQUIRED_*) y cardinalidad (*_MULTIPLE); sin él, opcional y único.
    - Dos nombres que se volverían el mismo en el wizard se quedan con el primero (Google no los repite, pero no se confía).
    """
    tipos = {t.get("name"): t for t in esquema.get("entityTypes") or []}
    raiz = tipos.get("custom_extraction_document_type") or next(
        (t for t in tipos.values() if "document" in (t.get("baseTypes") or []) and t.get("properties")), None
    )
    if raiz is None:
        return []

    def _ocurrencia(prop: dict) -> tuple[bool, str]:
        ocurrencia = str(prop.get("occurrenceType") or "OPTIONAL_ONCE")
        return ocurrencia.startswith("REQUIRED"), "multiple" if ocurrencia.endswith("MULTIPLE") else "unico"

    def _tipo_simple(prop: dict, permite_lista: bool) -> tuple[str, list[str]]:
        value_type = prop.get("valueType") or "string"
        destino = tipos.get(value_type)
        if destino is not None:
            valores = [str(v) for v in (destino.get("enumValues") or {}).get("values") or [] if str(v).strip()]
            if valores and permite_lista:
                return "lista", valores
            return "texto", []
        return _TIPO_DE_VALUE_TYPE.get(value_type, "texto"), []

    campos: list[dict] = []
    vistos: set[str] = set()
    for prop in raiz.get("properties") or []:
        if not prop.get("name"):
            continue
        nombre = nombre_legible(prop["name"])
        if normalizar_nombre(nombre) in vistos:
            continue
        vistos.add(normalizar_nombre(nombre))
        obligatorio, cardinalidad = _ocurrencia(prop)
        campo = {
            "nombre": nombre,
            "tipoDato": "texto",
            "descripcion": (prop.get("description") or "").strip(),
            "obligatorio": obligatorio,
            "cardinalidad": cardinalidad,
            "valoresLista": [],
            "subcampos": [],
        }
        destino = tipos.get(prop.get("valueType"))
        if destino and destino.get("properties") and "object" in (destino.get("baseTypes") or []):
            vistos_hijos: set[str] = set()
            for hijo in destino["properties"]:
                if not hijo.get("name"):
                    continue
                nombre_hijo = nombre_legible(hijo["name"])
                if normalizar_nombre(nombre_hijo) in vistos_hijos:
                    continue
                vistos_hijos.add(normalizar_nombre(nombre_hijo))
                tipo_hijo, _ = _tipo_simple(hijo, permite_lista=False)
                campo["subcampos"].append(
                    {
                        "nombre": nombre_hijo,
                        "tipoDato": tipo_hijo,
                        "descripcion": (hijo.get("description") or "").strip(),
                        "obligatorio": _ocurrencia(hijo)[0],
                    }
                )
            if campo["subcampos"]:
                campo["tipoDato"] = TIPO_GRUPO
        else:
            campo["tipoDato"], campo["valoresLista"] = _tipo_simple(prop, permite_lista=True)
        campos.append(campo)
    return campos


def estructura_desde_esquema(esquema: dict) -> dict[str, dict]:
    """La ESTRUCTURA que declara un `DocumentSchema` de extractor: qué es obligatorio, qué se repite y qué es un grupo.

    `{nombre: {"obligatorio": bool, "multiple": bool, "hijos": {...}}}`, en el orden del esquema; `hijos` solo existe en un
    grupo (una Property cuyo `valueType` es un EntityType `object` con sus propias `properties`, como el `domicilio` de INE o
    los grupos de `esquema_desde_campos`) y trae lo mismo, un nivel más abajo. Es PURA: la lee del esquema que ya se bajó de
    Google (`procesadores.estructura_del_procesador`), así que sirve igual para el esquema de un tipo documental del wizard y
    para el del procesador de INE, que se armó a mano en la consola.

    Existe porque la RESPUESTA de Document AI no dice si un campo se repite: un campo múltiple con una sola aparición se ve
    igual que uno único. Sin esto no se puede devolver siempre lista donde la hay, ni saber qué campo obligatorio faltó.
    """
    tipos = {t.get("name"): t for t in esquema.get("entityTypes") or []}
    raiz = tipos.get("custom_extraction_document_type") or next(
        (t for t in tipos.values() if "document" in (t.get("baseTypes") or []) and t.get("properties")), None
    )
    if raiz is None:
        return {}

    def _de(prop: dict) -> dict:
        ocurrencia = str(prop.get("occurrenceType") or "OPTIONAL_ONCE")
        return {"obligatorio": ocurrencia.startswith("REQUIRED"), "multiple": ocurrencia.endswith("MULTIPLE")}

    estructura: dict[str, dict] = {}
    for prop in raiz.get("properties") or []:
        definicion = _de(prop)
        destino = tipos.get(prop.get("valueType"))
        if destino and destino.get("properties") and "object" in (destino.get("baseTypes") or []):
            definicion["hijos"] = {hijo["name"]: _de(hijo) for hijo in destino["properties"] if hijo.get("name")}
        estructura[prop["name"]] = definicion
    return estructura
