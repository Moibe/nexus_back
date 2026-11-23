"""Ejemplos etiquetados de un tipo documental y la MEDICIÓN de su precisión (2026-11-23, migración `0016`).

Moibe: «el propósito sí es que nos sirva de entrenamiento» → «arranca con el punto 1, la medición». Antes la pantalla «Ejemplo documental»
guardaba un recorte por campo (sin el valor) que nadie leía. Ahora:

1. **Leer** (`leer`): un documento de ejemplo del tipo se extrae con la definición ACTUAL del tipo y se devuelve, campo por campo, lo que se
   leyó con su caja. Lo que devolvió Google se guarda en el almacén (`base`): con eso se podrá importar a un dataset para entrenar.
2. **Etiquetar** (`guardar_etiquetas`): la persona confirma, corrige o marca como ausente cada campo. Eso es la «respuesta buena» del
   documento.
3. **Medir** (`medir`): se vuelven a extraer TODOS los documentos etiquetados con la definición actual y se compara campo por campo contra la
   etiqueta. Queda en el historial (`extractionMeasurements`): así se ve si una descripción nueva mejoró o empeoró.

## Con qué se extrae

Siempre con la definición ACTUAL del tipo (`esquema.esquema_desde_campos` de sus campos guardados), que viaja como `schemaOverride`. Un tipo
ACTIVO se lee con SU procesador y su versión fijada (lo mismo que en producción); uno en borrador, que no tiene procesador, con el extractor
compartido (`ubicador_docai`), que es el mismo modelo. Así se pueden afinar las descripciones ANTES de activar.

## Cómo se compara

Cada etiqueta es `{ruta, valor}`; `valor` null = «no aparece en el documento». La ruta es la de los datos extraídos (`curp`,
`domicilio.calle`, `socios[2].nombre`, `telefonos[1]`). Por cada ruta etiquetada que sigue siendo un campo del tipo:

- se esperaba un valor y se leyó el mismo → `acierto` (sin fijarse en mayúsculas ni en espacios de más: una O por un cero SÍ es error);
- se esperaba un valor y se leyó otro → `error`; no se leyó nada → `falto`;
- se esperaba que no estuviera y se leyó algo → `sobro`; no estaba y no se leyó → `acierto`.

En lo que se repite (filas de un grupo, un campo múltiple), una fila leída que la persona no tenía (ni la marcó) también es `sobro`. Un campo
que no tiene etiqueta en un documento (se agregó al tipo después) no cuenta: «sin etiqueta». Las rutas de un campo que ya no existe en el tipo
se ignoran.

Los documentos de ejemplo NO se mandan a guardar a Google: eso es del paso de entrenar, que se decide aparte.
"""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from db import tablas
from errores import ErrorDocumentAI
from servicios import almacen, ia, ubicador_docai
from servicios import tipos_documentales as registro_tipos
from servicios.almacen import ErrorAlmacen
from servicios.bloques import es_campo
from servicios.datos_extraidos import _NO_SON_CAMPOS, aplanar
from servicios.esquema import TIPO_GRUPO, esquema_desde_campos, nombres_en_google
from servicios.tipos_documentales_comun import momento

logger = logging.getLogger(__name__)

#: Cuántas etiquetas puede traer un documento (un acta con muchas filas), lo más largo de un valor y cuántos documentos entran en UNA medición
#: (los más recientes): cada uno es una extracción que se cobra y la medición se espera en la pantalla.
MAX_CAMPOS = 500
MAX_VALOR = 2000
MAX_EJEMPLOS_POR_MEDICION = 25
EN_PARALELO = 4
ORIGENES = ("confirmado", "corregido", "ausente", "agregado")
MIMES = ("application/pdf", "image/jpeg", "image/png", "image/tiff")
_MIME_POR_TIPO = {"PDF": "application/pdf", "JPEG": "image/jpeg", "JPG": "image/jpeg", "PNG": "image/png", "TIFF": "image/tiff", "TIF": "image/tiff"}
#: El texto con que se cuenta cada resultado en el resumen por campo.
_CONTADOR = {"acierto": "aciertos", "error": "errores", "falto": "faltaron", "sobro": "sobraron"}

_RE_EJEMPLO = re.compile(r"^[A-Za-z0-9_-]{1,100}$")
_NOMBRE = r"[a-z][a-z0-9_-]{0,63}"
# `curp`, `telefonos[1]`, `domicilio.calle`, `socios[2].nombre` (y un subcampo que se repite: `socios[2].telefono[1]`).
_RE_RUTA = re.compile(rf"^({_NOMBRE})(?:\[(\d{{1,4}})\])?(?:\.({_NOMBRE})(?:\[(\d{{1,4}})\])?)?$")
_RE_INDICES = re.compile(r"\[\d+\]")


class NoEncontrado(LookupError):
    """El tipo o el documento de ejemplo no existe en esa organización. `str(exc)` se puede mostrar."""


class Invalido(ValueError):
    """Lo que se pidió no se puede hacer así. `str(exc)` se puede mostrar."""


class SinLectura(Exception):
    """Document AI no pudo leer el documento. `str(exc)` se puede mostrar (nunca el detalle de Google: va al log)."""


# ── La definición del tipo ──────────────────────────────────────────────────────────────────────────────────────────────


def definicion(datos: dict) -> tuple[dict, dict[str, dict]]:
    """`(esquema, campos)` de la definición ACTUAL de un tipo. `campos` va por nombre de Google, en el orden del tipo:
    `{google: {etiqueta, tipoDato, grupo, multiple, hijos: {google: {etiqueta, tipoDato}}}}`. Los nombres de Google salen de
    `nombres_en_google`, la misma regla con que se arma el esquema, así que coinciden con las rutas de lo que se extrae."""
    campos = [c for c in (datos.get("campos") or []) if isinstance(c, dict) and isinstance(c.get("nombre"), str) and c["nombre"].strip()]
    esquema = esquema_desde_campos(str(datos.get("nombre") or ""), str(datos.get("descripcion") or ""), campos)
    nombres = nombres_en_google(campos)
    info: dict[str, dict] = {}
    for c in campos:
        par = nombres.get(c.get("nombre", ""))
        if not par:
            continue
        google = par[1]
        grupo = c.get("tipoDato") == TIPO_GRUPO
        hijos: dict[str, dict] = {}
        if grupo:
            for s in c.get("subcampos") or []:
                if not isinstance(s, dict):
                    continue
                hijo = nombres.get(f"{c.get('nombre', '')}.{s.get('nombre', '')}")
                if hijo:
                    hijos[hijo[1]] = {"etiqueta": str(s.get("nombre") or "").strip(), "tipoDato": s.get("tipoDato") or "texto"}
        info[google] = {
            "etiqueta": c["nombre"].strip(),
            "tipoDato": c.get("tipoDato") or "texto",
            "grupo": grupo,
            "multiple": c.get("cardinalidad") == "multiple",
            "hijos": hijos,
        }
    return esquema, info


def _partes(ruta: str) -> tuple[str, int | None, str | None, int | None] | None:
    m = _RE_RUTA.match(ruta or "")
    if not m:
        return None
    top, i, hijo, j = m.groups()
    return top, int(i) if i else None, hijo, int(j) if j else None


def campo_de(ruta: str) -> str:
    """La ruta sin índices: el CAMPO que se mide (`socios[2].nombre` → `socios.nombre`)."""
    return _RE_INDICES.sub("", ruta)


def etiqueta_de(ruta: str, info: dict[str, dict]) -> str:
    """Cómo se le dice a la persona: «CURP», «Socios 2 · Nombre», «Teléfonos 1»."""
    p = _partes(ruta)
    if not p:
        return ruta
    top, i, hijo, j = p
    d = info.get(top)
    texto = d["etiqueta"] if d else top
    if i is not None:
        texto += f" {i}"
    if hijo:
        h = (d or {}).get("hijos", {}).get(hijo)
        texto += f" · {h['etiqueta'] if h else hijo}"
        if j is not None:
            texto += f" {j}"
    return texto


def _es_marcador(ruta: str, info: dict[str, dict]) -> bool:
    """La ruta pelona de algo que se repite o de un grupo («socios»): la etiqueta «no trae ninguna fila»."""
    d = info.get(ruta)
    return bool(d) and (d["grupo"] or d["multiple"])


def vigente(ruta: str, info: dict[str, dict]) -> bool:
    """¿La ruta es de un campo que el tipo TIENE hoy, con la forma que tiene hoy?"""
    p = _partes(ruta)
    if not p:
        return False
    top, i, hijo, _ = p
    d = info.get(top)
    if not d:
        return False
    if ruta == top:
        return True  # un campo suelto, o el marcador de algo que se repite
    if d["grupo"]:
        return bool(hijo) and hijo in d["hijos"] and (i is not None) == d["multiple"]
    return hijo is None and i is not None and d["multiple"]


# ── Lo que devolvió Document AI ─────────────────────────────────────────────────────────────────────────────────────────


def _cajas(resultado: dict[str, Any]) -> dict[str, list[int]]:
    """La caja de cada campo leído (`[ymin, xmin, ymax, xmax]` de 0 a 1000), por la MISMA ruta que le pone `aplanar`."""
    salida: dict[str, list[int]] = {}

    def recorrer(nodo: Any, ruta: str) -> None:
        if es_campo(nodo):
            p = nodo.get("posicion")
            if isinstance(p, dict) and all(isinstance(p.get(k), (int, float)) for k in ("x", "y", "ancho", "alto")):
                y0, x0 = p["y"], p["x"]
                caja = [round(max(0.0, min(1.0, v)) * 1000) for v in (y0, x0, y0 + p["alto"], x0 + p["ancho"])]
                caja[2] = max(caja[2], min(1000, caja[0] + 1))
                caja[3] = max(caja[3], min(1000, caja[1] + 1))
                salida[ruta] = caja
        elif isinstance(nodo, list):
            for i, elemento in enumerate(nodo, start=1):
                recorrer(elemento, f"{ruta}[{i}]")
        elif isinstance(nodo, dict):
            for llave, hijo in nodo.items():
                recorrer(hijo, f"{ruta}.{llave}" if ruta else llave)

    recorrer({k: v for k, v in resultado.items() if k not in _NO_SON_CAMPOS}, "")
    return salida


def leidos(resultado: dict[str, Any]) -> list[dict[str, Any]]:
    """Lo que se leyó, un renglón por campo hoja con valor: `{ruta, valor, confianza, pagina, caja}`. El valor es el NORMALIZADO, el mismo
    que se guarda en los datos extraídos (las fechas completas, en AAAA-MM-DD)."""
    cajas = _cajas(resultado)
    salida = []
    for f in aplanar(resultado):
        if f["nullReason"] is not None or f["valueNormalized"] is None:
            continue
        salida.append(
            {"ruta": f["fieldPath"], "valor": f["valueNormalized"], "confianza": f["confidence"], "pagina": f["pageNumber"], "caja": cajas.get(f["fieldPath"])}
        )
    return salida


def _renglon(ruta: str, leido: dict | None, info: dict[str, dict]) -> dict[str, Any]:
    top, i, hijo, _ = _partes(ruta) or (ruta, None, None, None)
    d = info.get(top) or {}
    tipo_dato = (d.get("hijos", {}).get(hijo) or {}).get("tipoDato") if hijo else d.get("tipoDato")
    return {
        "ruta": ruta,
        "campo": campo_de(ruta),
        "etiqueta": etiqueta_de(ruta, info),
        "grupo": top,
        "fila": i,
        "tipoDato": tipo_dato or "texto",
        "valor": (leido or {}).get("valor"),
        "confianza": (leido or {}).get("confianza"),
        "pagina": (leido or {}).get("pagina"),
        "caja": (leido or {}).get("caja"),
    }


def para_revisar(info: dict[str, dict], lecturas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Lo que ve la persona para etiquetar, en el orden del tipo: cada campo suelto (con lo leído, o vacío si no apareció); de un grupo
    único, cada subcampo; de lo que se repite, cada fila leída con todos sus subcampos, o un renglón «sin filas» (la ruta pelona) si no
    se leyó ninguna."""
    por_ruta = {l["ruta"]: l for l in lecturas}
    salida: list[dict[str, Any]] = []
    for top, d in info.items():
        if not d["multiple"] and not d["grupo"]:
            salida.append(_renglon(top, por_ruta.get(top), info))
        elif not d["multiple"]:
            salida.extend(_renglon(f"{top}.{h}", por_ruta.get(f"{top}.{h}"), info) for h in d["hijos"])
        else:
            filas = sorted({p[1] for l in lecturas if (p := _partes(l["ruta"])) and p[0] == top and p[1] is not None})
            if not filas:
                salida.append({**_renglon(top, None, info), "sinFilas": True})
            elif d["grupo"]:
                salida.extend(_renglon(f"{top}[{i}].{h}", por_ruta.get(f"{top}[{i}].{h}"), info) for i in filas for h in d["hijos"])
            else:
                salida.extend(_renglon(f"{top}[{i}]", por_ruta.get(f"{top}[{i}]"), info) for i in filas)
    return salida


# ── Comparar ────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _igual(a: Any, b: Any) -> bool:
    return " ".join(str(a).split()).casefold() == " ".join(str(b).split()).casefold()


def _resultado(esperado: Any, leido: Any) -> str:
    if esperado is None:
        return "acierto" if leido is None else "sobro"
    if leido is None:
        return "falto"
    return "acierto" if _igual(esperado, leido) else "error"


def comparar(etiquetas: list[dict[str, Any]], lecturas: list[dict[str, Any]], info: dict[str, dict]) -> list[dict[str, Any]]:
    """Cada ruta etiquetada (de un campo que sigue en el tipo) contra lo leído, y las filas leídas que nadie etiquetó. Ver el docstring del
    módulo. Cada comparación: `{ruta, campo, etiqueta, esperado, leido, resultado, confianza}`."""
    lei = {l["ruta"]: l for l in lecturas}
    etq = {e["ruta"]: e for e in etiquetas if isinstance(e, dict) and vigente(e.get("ruta", ""), info)}
    tops = {(_partes(r) or (r,))[0] for r in etq}
    filas_etiquetadas = {(p[0], p[1]) for r in etq if (p := _partes(r)) and p[1] is not None}

    def comp(ruta: str, esperado: Any, l: dict | None, resultado: str) -> dict[str, Any]:
        return {
            "ruta": ruta,
            "campo": campo_de(ruta),
            "etiqueta": etiqueta_de(ruta, info),
            "esperado": esperado,
            "leido": (l or {}).get("valor"),
            "resultado": resultado,
            "confianza": (l or {}).get("confianza"),
        }

    salida: list[dict[str, Any]] = []
    for ruta, e in etq.items():
        if _es_marcador(ruta, info):
            # «No trae ninguna fila»: si de verdad no se leyó ninguna, es un acierto; si se leyeron, cada una cuenta abajo como «sobró».
            if not any((_partes(r) or (r,))[0] == ruta for r in lei):
                salida.append(comp(ruta, None, None, "acierto"))
            continue
        l = lei.get(ruta)
        esperado = e.get("valor")
        salida.append(comp(ruta, esperado, l, _resultado(esperado, (l or {}).get("valor"))))
    for ruta, l in lei.items():
        if ruta in etq or not vigente(ruta, info):
            continue
        p = _partes(ruta)
        if p and p[1] is not None and p[0] in tops and (p[0], p[1]) not in filas_etiquetadas:
            salida.append(comp(ruta, None, l, "sobro"))
    return salida


def resumir(documentos: list[dict[str, Any]], info: dict[str, dict]) -> list[dict[str, Any]]:
    """Por CAMPO (la ruta sin índices), cuántos aciertos, errores, faltantes y sobrantes, en el orden del tipo."""
    orden: list[str] = []
    for top, d in info.items():
        orden.extend([f"{top}.{h}" for h in d["hijos"]] if d["grupo"] else [top])
    por_campo: dict[str, dict[str, Any]] = {}
    for doc in documentos:
        for c in doc.get("comparaciones") or []:
            r = por_campo.setdefault(
                c["campo"], {"campo": c["campo"], "etiqueta": etiqueta_de(c["campo"], info), "aciertos": 0, "errores": 0, "faltaron": 0, "sobraron": 0}
            )
            r[_CONTADOR[c["resultado"]]] += 1
    for r in por_campo.values():
        r["total"] = r["aciertos"] + r["errores"] + r["faltaron"] + r["sobraron"]
    return sorted(por_campo.values(), key=lambda r: orden.index(r["campo"]) if r["campo"] in orden else len(orden))


# ── Validar lo que manda la pantalla ────────────────────────────────────────────────────────────────────────────────────


def validar_campos(campos: object, info: dict[str, dict]) -> list[dict[str, Any]]:
    """Las etiquetas de un documento, limpias: `[{ruta, valor (null = no aparece), pagina, caja, origen}]`. `Invalido` si no sirven."""
    if not isinstance(campos, list) or not campos:
        raise Invalido("Faltan las etiquetas del documento.")
    if len(campos) > MAX_CAMPOS:
        raise Invalido(f"Son demasiadas etiquetas: a lo más {MAX_CAMPOS}.")
    limpias: list[dict[str, Any]] = []
    vistas: set[str] = set()
    for c in campos:
        if not isinstance(c, dict) or not isinstance(c.get("ruta"), str):
            raise Invalido("Cada etiqueta debe decir de qué campo es.")
        ruta = c["ruta"]
        if not vigente(ruta, info):
            raise Invalido(f"«{ruta[:80]}» no es un campo de este tipo documental.")
        if ruta in vistas:
            raise Invalido(f"El campo «{etiqueta_de(ruta, info)}» viene dos veces.")
        vistas.add(ruta)
        valor = c.get("valor")
        if valor is not None:
            if not isinstance(valor, str):
                raise Invalido(f"El valor de «{etiqueta_de(ruta, info)}» debe ser texto.")
            valor = valor.strip() or None
            if valor is not None and len(valor) > MAX_VALOR:
                raise Invalido(f"El valor de «{etiqueta_de(ruta, info)}» es demasiado largo: a lo más {MAX_VALOR} caracteres.")
        if valor is not None and _es_marcador(ruta, info):
            raise Invalido(f"«{etiqueta_de(ruta, info)}» se etiqueta por filas, no con un solo valor.")
        pagina = c.get("pagina")
        if pagina is not None and (not isinstance(pagina, int) or isinstance(pagina, bool) or not 1 <= pagina <= 10000):
            raise Invalido("La página de una etiqueta no es válida.")
        caja = c.get("caja")
        if caja is not None:
            if (
                not isinstance(caja, list)
                or len(caja) != 4
                or not all(isinstance(n, (int, float)) and not isinstance(n, bool) and 0 <= n <= 1000 for n in caja)
                or caja[2] <= caja[0]
                or caja[3] <= caja[1]
            ):
                raise Invalido("La caja de una etiqueta no es válida.")
            caja = [round(n) for n in caja]
        origen = c.get("origen") or ("ausente" if valor is None else "confirmado")
        if origen not in ORIGENES:
            raise Invalido("El origen de una etiqueta no es válido.")
        limpias.append({"ruta": ruta, "valor": valor, "pagina": pagina, "caja": caja, "origen": origen})
    return limpias


# ── El tipo y sus documentos de ejemplo ─────────────────────────────────────────────────────────────────────────────────


def _tipo(tenant: str, tipo_id: str) -> dict:
    tipo = registro_tipos.obtener(tenant, tipo_id) if isinstance(tipo_id, str) else None
    if not tipo:
        raise NoEncontrado("Ese tipo documental no existe.")
    return tipo


def ejemplo(tenant: str, datos: dict, ejemplo_id: object) -> dict[str, str]:
    """El documento de ejemplo del tipo: `{id, ruta, mime, nombre, sha256}`. La ruta sale del `dataJson` (lo escribe el navegador), así que se
    exige que sea un objeto del almacén de ESTA organización: un tipo no puede apuntar al archivo de otra."""
    if not isinstance(ejemplo_id, str) or not _RE_EJEMPLO.match(ejemplo_id):
        raise Invalido("El documento de ejemplo no es válido.")
    for e in datos.get("documentosEjemplo") or []:
        if not isinstance(e, dict) or e.get("id") != ejemplo_id:
            continue
        ruta, mime = e.get("rutaRelativa"), e.get("mime")
        if not ruta:
            # Un PDF viejo del que solo sobrevivió la página 1 como imagen.
            ruta, mime = e.get("rutaVista"), e.get("mimeVista") or "image/png"
        if not isinstance(ruta, str) or not ruta.startswith(f"{tenant}/"):
            raise NoEncontrado("Ese documento de ejemplo no está en el almacén de la organización.")
        if mime not in MIMES:
            mime = _MIME_POR_TIPO.get(str(e.get("tipo") or "").upper().strip(), "")
        if mime not in MIMES:
            raise Invalido("Ese documento de ejemplo no tiene un formato que se pueda leer.")
        return {"id": ejemplo_id, "ruta": ruta, "mime": mime, "nombre": str(e.get("nombre") or "")[:400], "sha256": ruta.rsplit("/", 1)[-1]}
    raise NoEncontrado("Ese documento de ejemplo no existe en este tipo documental.")


def _ids_de_ejemplos(datos: dict) -> list[str]:
    return [e["id"] for e in datos.get("documentosEjemplo") or [] if isinstance(e, dict) and isinstance(e.get("id"), str)]


async def _procesador_para(datos: dict) -> tuple[str, str, str]:
    """`(procesador, versión, origen)`: el del tipo si está activo; si no, el compartido."""
    propio = str(datos.get("procesadorId") or "").strip()
    if datos.get("estado") == "activo" and propio:
        return propio, str(datos.get("procesadorVersion") or "").strip(), "tipo"
    compartido, version = await ubicador_docai.id_del_ubicador()
    return compartido, version, "compartido"


def _mensaje(exc: BaseException) -> str:
    """Lo que se le dice a la persona cuando un documento no se pudo leer (el detalle va al log)."""
    if isinstance(exc, ErrorDocumentAI):
        if exc.motivo == "limite_paginas":
            return "El documento tiene demasiadas páginas para leerlo."
        if exc.motivo in ("cuota", "servicio"):
            return "Biblioratum está muy ocupado o falló de su lado: intenta de nuevo en un momento."
        if exc.motivo == "archivo_ilegible":
            return "El archivo está dañado, protegido con contraseña o no se puede abrir."
        return "Biblioratum no pudo leer este documento."
    if isinstance(exc, ErrorAlmacen):
        return "El documento de ejemplo no se encontró en el almacén."
    return "No se pudo leer este documento."


# ── Leer un ejemplo para etiquetarlo ────────────────────────────────────────────────────────────────────────────────────


async def leer(tenant: str, tipo_id: str, ejemplo_id: str) -> dict[str, Any]:
    """Extrae UN documento de ejemplo con la definición actual del tipo y devuelve lo que se leyó, listo para revisar. Guarda en el almacén
    lo que devolvió Google (`base`): la pantalla lo regresa al guardar las etiquetas."""
    tipo = await asyncio.to_thread(_tipo, tenant, tipo_id)
    datos = tipo["datos"]
    ej = ejemplo(tenant, datos, ejemplo_id)
    esquema, info = definicion(datos)
    if not info:
        raise Invalido("Agrega primero los campos del tipo documental.")
    contenido = await asyncio.to_thread(almacen.leer, ej["ruta"])
    procesador, version, origen = await _procesador_para(datos)
    crudos: list[dict[str, Any]] = []
    try:
        resultado = await ia.extraer_con_procesador(procesador, contenido, ej["mime"], version, esquema=esquema, documentos_crudos=crudos)
    except Exception as exc:  # noqa: BLE001 — se dice qué pasó sin el detalle de Google
        logger.warning("Leer el ejemplo %s del tipo %s falló: %s", ejemplo_id, tipo_id, exc)
        raise SinLectura(_mensaje(exc)) from exc
    base = None
    if crudos:
        contenido_base = json.dumps({"procesador": procesador, "version": version or None, "documentos": crudos}, ensure_ascii=False).encode("utf-8")
        base = (await asyncio.to_thread(almacen.guardar, tenant, contenido_base))["rutaRelativa"]
    return {
        "ejemplo": ejemplo_id,
        "revision": tipo["revision"],
        "procesador": origen,
        "base": base,
        "campos": para_revisar(info, leidos(resultado)),
    }


# ── Guardar, listar y quitar etiquetas ──────────────────────────────────────────────────────────────────────────────────


def _llave(cur, tenant: str, tipo_id: str) -> int:
    cur.execute("SELECT documentTypeId FROM nexus.documentTypes WHERE publicId = ? AND tenant = ?", tipo_id, tenant)
    f = tablas.una(cur)
    if not f:
        raise NoEncontrado("Ese tipo documental no existe.")
    return int(f["documentTypeId"])


def _base(tenant: str, base: object) -> tuple[str | None, str | None, str | None]:
    """`(ruta, procesador, versión)` del documento base que devolvió `leer`, comprobado: un objeto del almacén de ESTA organización."""
    if base is None or base == "":
        return None, None, None
    if not isinstance(base, str) or not base.startswith(f"{tenant}/"):
        raise Invalido("La lectura del documento no es válida: vuelve a leerlo.")
    try:
        if not almacen.existe(base):
            raise Invalido("La lectura del documento ya no está: vuelve a leerlo.")
        guardado = json.loads(almacen.leer(base).decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise Invalido("La lectura del documento no es válida: vuelve a leerlo.") from exc
    if not isinstance(guardado, dict) or not isinstance(guardado.get("documentos"), list):
        raise Invalido("La lectura del documento no es válida: vuelve a leerlo.")
    procesador = guardado.get("procesador") if isinstance(guardado.get("procesador"), str) else None
    version = guardado.get("version") if isinstance(guardado.get("version"), str) else None
    return base, (procesador or "")[:120] or None, (version or "")[:120] or None


def _publica(f: dict, info: dict[str, dict]) -> dict[str, Any]:
    """Las etiquetas de un documento como las ve la pantalla. Cada campo trae además su rótulo, su campo (la ruta sin índices), su tipo de dato
    y si sigue siendo un campo del tipo (`vigente`); y `camposSinEtiqueta` dice qué campos del tipo de HOY no tienen etiqueta en este documento
    (se agregaron después de etiquetarlo): para etiquetarlos hay que volver a leerlo."""
    campos = []
    for c in json.loads(f["labelsJson"]).get("campos", []):
        ruta = c.get("ruta", "")
        top, _, hijo, _ = _partes(ruta) or (ruta, None, None, None)
        d = info.get(top) or {}
        tipo_dato = (d.get("hijos", {}).get(hijo) or {}).get("tipoDato") if hijo else d.get("tipoDato")
        campos.append({**c, "campo": campo_de(ruta), "etiqueta": etiqueta_de(ruta, info), "grupo": top, "tipoDato": tipo_dato or "texto",
                       "vigente": vigente(ruta, info)})
    con_etiqueta = {c["grupo"] for c in campos}
    return {
        "ejemplo": f["exampleId"],
        "revision": int(f["documentTypeRevision"]),
        "campos": campos,
        "camposSinEtiqueta": [d["etiqueta"] for top, d in info.items() if top not in con_etiqueta],
        "conBase": bool(f["baseDocumentPath"]),
        "guardadoEn": momento(f["updatedAt"] or f["createdAt"]),
        "guardadoPor": f["updatedBy"] or f["createdBy"],
    }


_COLUMNAS_ETIQUETA = "l.exampleId, l.documentTypeRevision, l.labelsJson, l.baseDocumentPath, l.createdAt, l.createdBy, l.updatedAt, l.updatedBy"


def guardar_etiquetas(tenant: str, tipo_id: str, ejemplo_id: str, campos: object, base: object, por: str) -> dict[str, Any]:
    """Guarda (o reemplaza) las etiquetas de un documento de ejemplo."""
    tipo = _tipo(tenant, tipo_id)
    datos = tipo["datos"]
    ej = ejemplo(tenant, datos, ejemplo_id)
    _, info = definicion(datos)
    limpias = validar_campos(campos, info)
    if not almacen.existe(ej["ruta"]):
        raise NoEncontrado("El documento de ejemplo no se encontró en el almacén.")
    ruta_base, procesador, version = _base(tenant, base)
    etiquetas = json.dumps({"campos": limpias}, ensure_ascii=False)
    with tablas.transaccion() as cur:
        llave = _llave(cur, tenant, tipo_id)
        cur.execute("SELECT exampleLabelId FROM nexus.exampleLabels WITH (UPDLOCK, HOLDLOCK) WHERE documentTypeId = ? AND exampleId = ?", llave, ejemplo_id)
        existente = tablas.una(cur)
        if existente:
            cur.execute(
                "UPDATE nexus.exampleLabels SET sha256 = ?, relativePath = ?, mime = ?, originalName = ?, documentTypeRevision = ?, labelsJson = ?, "
                "updatedAt = sysutcdatetime(), updatedBy = ? WHERE exampleLabelId = ?",
                ej["sha256"], ej["ruta"], ej["mime"], ej["nombre"], tipo["revision"], etiquetas, por, existente["exampleLabelId"],
            )
            if ruta_base is not None:
                # Una lectura nueva reemplaza la base; sin ella (se corrigió algo sin volver a leer) se queda la de antes.
                cur.execute(
                    "UPDATE nexus.exampleLabels SET baseDocumentPath = ?, baseProcessorId = ?, baseProcessorVersion = ? WHERE exampleLabelId = ?",
                    ruta_base, procesador, version, existente["exampleLabelId"],
                )
        else:
            cur.execute(
                "INSERT INTO nexus.exampleLabels (tenant, documentTypeId, exampleId, sha256, relativePath, mime, originalName, documentTypeRevision, "
                "labelsJson, baseDocumentPath, baseProcessorId, baseProcessorVersion, createdBy) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tenant, llave, ejemplo_id, ej["sha256"], ej["ruta"], ej["mime"], ej["nombre"], tipo["revision"], etiquetas,
                ruta_base, procesador, version, por,
            )
        cur.execute(
            f"SELECT {_COLUMNAS_ETIQUETA} FROM nexus.exampleLabels l WHERE l.documentTypeId = ? AND l.exampleId = ?", llave, ejemplo_id
        )
        return _publica(tablas.una(cur), info)


def etiquetas(tenant: str, tipo_id: str) -> list[dict[str, Any]]:
    """Las etiquetas de los documentos de ejemplo que el tipo todavía tiene (las de uno que se quitó no cuentan), en el orden en que se
    etiquetaron."""
    tipo = _tipo(tenant, tipo_id)
    presentes = set(_ids_de_ejemplos(tipo["datos"]))
    _, info = definicion(tipo["datos"])
    with tablas.lectura() as cur:
        cur.execute(
            f"SELECT {_COLUMNAS_ETIQUETA} FROM nexus.exampleLabels l JOIN nexus.documentTypes t ON t.documentTypeId = l.documentTypeId "
            "WHERE t.publicId = ? AND t.tenant = ? AND l.tenant = ? ORDER BY l.exampleLabelId",
            tipo_id, tenant, tenant,
        )
        return [_publica(f, info) for f in tablas.filas(cur) if f["exampleId"] in presentes]


def borrar_etiquetas(tenant: str, tipo_id: str, ejemplo_id: str) -> bool:
    """Quita las etiquetas de un documento de ejemplo (al quitar el documento, o para empezar de nuevo). `False` si no tenía."""
    if not isinstance(ejemplo_id, str) or not _RE_EJEMPLO.match(ejemplo_id):
        raise Invalido("El documento de ejemplo no es válido.")
    _tipo(tenant, tipo_id)
    with tablas.transaccion() as cur:
        llave = _llave(cur, tenant, tipo_id)
        cur.execute("DELETE FROM nexus.exampleLabels WHERE documentTypeId = ? AND exampleId = ?", llave, ejemplo_id)
        return cur.rowcount > 0


# ── Medir ───────────────────────────────────────────────────────────────────────────────────────────────────────────────


async def medir(tenant: str, tipo_id: str, por: str) -> dict[str, Any]:
    """Vuelve a extraer los documentos etiquetados (hasta `MAX_EJEMPLOS_POR_MEDICION`, los más recientes) con la definición actual y los compara
    contra sus etiquetas. Guarda la medición y la devuelve con su detalle."""
    tipo = await asyncio.to_thread(_tipo, tenant, tipo_id)
    datos = tipo["datos"]
    esquema, info = definicion(datos)
    if not info:
        raise Invalido("El tipo documental no tiene campos que medir.")
    guardadas = await asyncio.to_thread(etiquetas, tenant, tipo_id)
    if not guardadas:
        raise Invalido("Todavía no hay documentos etiquetados: etiqueta al menos uno para medir.")
    guardadas = guardadas[-MAX_EJEMPLOS_POR_MEDICION:]
    procesador, version, origen = await _procesador_para(datos)
    iniciada = datetime.now(timezone.utc).replace(tzinfo=None)
    semaforo = asyncio.Semaphore(EN_PARALELO)

    async def uno(etq: dict[str, Any]) -> dict[str, Any]:
        doc: dict[str, Any] = {"ejemplo": etq["ejemplo"], "nombre": "", "estado": "ok", "comparaciones": []}
        try:
            ej = ejemplo(tenant, datos, etq["ejemplo"])
            doc["nombre"] = ej["nombre"]
            async with semaforo:
                contenido = await asyncio.to_thread(almacen.leer, ej["ruta"])
                resultado = await ia.extraer_con_procesador(procesador, contenido, ej["mime"], version, esquema=esquema)
        except Exception as exc:  # noqa: BLE001 — un documento que no se pudo leer no tumba la medición
            logger.warning("Medir: el ejemplo %s del tipo %s no se pudo leer: %s", etq["ejemplo"], tipo_id, exc)
            return {**doc, "estado": "error", "mensaje": _mensaje(exc) if not isinstance(exc, (NoEncontrado, Invalido)) else str(exc)}
        doc["comparaciones"] = comparar(etq["campos"], leidos(resultado), info)
        doc["aciertos"] = sum(1 for c in doc["comparaciones"] if c["resultado"] == "acierto")
        doc["total"] = len(doc["comparaciones"])
        return doc

    documentos = list(await asyncio.gather(*(uno(e) for e in guardadas)))
    medidos = [d for d in documentos if d["estado"] == "ok"]
    estado = "completa" if len(medidos) == len(documentos) else ("fallida" if not medidos else "parcial")
    comparaciones = sum(d["total"] for d in medidos)
    aciertos = sum(d["aciertos"] for d in medidos)
    detalle = {"documentos": documentos, "campos": resumir(medidos, info)}
    public_id = f"medicion-{uuid.uuid4().hex}"

    def guardar() -> dict[str, Any]:
        with tablas.transaccion() as cur:
            llave = _llave(cur, tenant, tipo_id)
            cur.execute(
                "INSERT INTO nexus.extractionMeasurements (publicId, tenant, documentTypeId, documentTypeRevision, processorOrigin, processorId, "
                "processorVersion, status, documentsCount, documentsMeasured, comparisonsCount, correctCount, detailJson, startedAt, createdBy) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                public_id, tenant, llave, tipo["revision"], origen, procesador, version or None, estado, len(documentos), len(medidos),
                comparaciones, aciertos, json.dumps(detalle, ensure_ascii=False), iniciada, por,
            )
            cur.execute(f"SELECT {_COLUMNAS_MEDICION} FROM nexus.extractionMeasurements WHERE publicId = ?", public_id)
            return _medicion(tablas.una(cur), con_detalle=True)

    return await asyncio.to_thread(guardar)


_COLUMNAS_MEDICION = (
    "publicId, documentTypeRevision, processorOrigin, processorVersion, status, documentsCount, documentsMeasured, comparisonsCount, "
    "correctCount, detailJson, startedAt, finishedAt, createdBy"
)


def _medicion(f: dict, con_detalle: bool) -> dict[str, Any]:
    salida = {
        "id": f["publicId"],
        "revision": int(f["documentTypeRevision"]),
        "procesador": f["processorOrigin"],
        "version": f["processorVersion"],
        "estado": f["status"],
        "documentos": int(f["documentsCount"]),
        "documentosMedidos": int(f["documentsMeasured"]),
        "comparaciones": int(f["comparisonsCount"]),
        "aciertos": int(f["correctCount"]),
        "iniciadaEn": momento(f["startedAt"]),
        "terminadaEn": momento(f["finishedAt"]),
        "medidaPor": f["createdBy"],
    }
    if con_detalle:
        salida["detalle"] = json.loads(f["detailJson"])
    return salida


def mediciones(tenant: str, tipo_id: str, limite: int = 10) -> dict[str, Any]:
    """El historial de mediciones del tipo, de la más reciente a la más antigua; la más reciente trae su detalle."""
    _tipo(tenant, tipo_id)
    limite = max(1, min(int(limite), 50))
    with tablas.lectura() as cur:
        cur.execute(
            f"SELECT TOP ({limite}) {', '.join('m.' + c.strip() for c in _COLUMNAS_MEDICION.split(','))} FROM nexus.extractionMeasurements m "
            "JOIN nexus.documentTypes t ON t.documentTypeId = m.documentTypeId WHERE t.publicId = ? AND t.tenant = ? AND m.tenant = ? "
            "ORDER BY m.measurementId DESC",
            tipo_id, tenant, tenant,
        )
        filas = tablas.filas(cur)
    return {"mediciones": [_medicion(f, con_detalle=(i == 0)) for i, f in enumerate(filas)]}
