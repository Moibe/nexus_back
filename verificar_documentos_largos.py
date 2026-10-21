"""Verificación de la extracción de documentos de MÁS DE 15 PÁGINAS (`ia.extraer_con_procesador` por bloques).
Sin red: `ia._procesar` es un Document AI de mentiras que se porta como el real MEDIDO el 2026-10-16:

  - el documento entero de más de 15 páginas se rechaza con 400 `PAGE_LIMIT_EXCEEDED` y `metadata {"page_limit": "15", "pages": "20"}`;
  - con `processOptions.individualPageSelector` solo procesa las páginas pedidas, aunque el archivo tenga más de 15;
  - `document.pages[].pageNumber` viene ABSOLUTO (11…20) pero las entidades apuntan por ÍNDICE dentro de esa lista (0…9, y
    Google omite `page` cuando es 0).

Correr con:  python verificar_documentos_largos.py
Mismo espíritu que los otros verificar_*.py: casos concretos, salida legible, exit code 1 si algo no cuadra.

Lo que cuida (paso 3 de las actas constitutivas): que un acta de 20 páginas se extraiga igual que una de 10, con las páginas
del documento entero, sin filas repetidas ni partidas por el corte, sin gastar de más y sin dejar trabajo suelto cuando algo falla.
"""

import asyncio
import time

import httpx

from errores import ErrorDocumentAI
from servicios import ia

fallas = 0


def ok(texto: str) -> None:
    print(f"  OK  {texto}")


def mal(texto: str) -> None:
    global fallas
    fallas += 1
    print(f"  NO  {texto}")


def caso(titulo: str) -> None:
    print(f"\n{titulo}")


def verificar(condicion: bool, bien: str, mensaje_mal: str) -> None:
    ok(bien) if condicion else mal(mensaje_mal)


# ── Un Document AI de mentiras con un «documento virtual» de N páginas ───────
def hoja(tipo, partes, conf):
    """Un dato que vive en una o más páginas: `partes` = [(página absoluta, texto, y)]."""
    return {"clase": "hoja", "tipo": tipo, "partes": partes, "conf": conf}


def grupo(tipo, filas):
    return {"clase": "grupo", "tipo": tipo, "filas": filas}


CLAUSULA_1 = "FABRICACION, COMPRA Y VENTA DE AUTOPARTES; ALQUILER DE MAQUINARIA"
CLAUSULA_2 = "MANTENIMIENTO Y REPARACION DE VEHICULOS"

VIRTUAL = [
    hoja("denominacion_social", [(1, "AUTOPARTES PREMIUM", 0.10)], 0.95),
    hoja("socios", [(3, "ANA", 0.20)], 0.90),
    # La cláusula cruza el corte (11 | 12): el bloque 1 solo ve su primera mitad —y con MÁS confianza—, el bloque 2 la ve entera.
    hoja("objeto_social", [(11, CLAUSULA_1, 0.30), (12, CLAUSULA_2, 0.05)], 0.90),
    hoja("socios", [(11, "BETO", 0.60)], 0.90),  # en una página de solape: los dos bloques la ven
    grupo(
        "accionistas",
        [
            [hoja("nombre", [(8, "JUAN PEREZ", 0.20)], 0.97), hoja("acciones", [(8, "35", 0.22)], 0.96), hoja("monto", [(8, "466,666.67", 0.24)], 0.95)],
            [hoja("nombre", [(8, "MARIA GONZALEZ", 0.40)], 0.97), hoja("acciones", [(8, "15", 0.42)], 0.96), hoja("monto", [(8, "200,000", 0.44)], 0.95)],
            # Una fila que cruza el corte: nombre y acciones al pie de la pág 11, el monto arriba de la 12.
            [hoja("nombre", [(11, "CARLOS RAMIREZ", 0.90)], 0.97), hoja("acciones", [(11, "25", 0.92)], 0.96), hoja("monto", [(12, "333,333.33", 0.05)], 0.95)],
        ],
    ),
    hoja("socios", [(15, "CARLA", 0.20)], 0.90),
    hoja("fecha_firma", [(19, "19 de septiembre de 2024", 0.50)], 0.88),
]


def _ref(paginas, pagina, y, jitter):
    indice = paginas.index(pagina)
    ref = {"boundingPoly": {"normalizedVertices": [{"x": 0.1 + jitter, "y": y + jitter}, {"x": 0.5 + jitter, "y": y + jitter}, {"x": 0.5 + jitter, "y": y + 0.02 + jitter}, {"x": 0.1 + jitter, "y": y + 0.02 + jitter}]}}
    if indice:  # Google omite `page` en la primera página del documento
        ref["page"] = indice
    return ref


def respuesta_del_bloque(paginas: list[int]) -> dict:
    """Lo que devolvería Google al procesar solo `paginas` del documento virtual."""
    visibles = set(paginas)
    texto = ""
    offsets: dict[int, tuple[int, int]] = {}
    paginas_raw = []
    for p in paginas:
        lineas = []
        for k in (0, 1):
            contenido = f"renglon {k} de la pagina {p}\n"
            inicio, fin = len(texto), len(texto) + len(contenido) - 1
            texto += contenido
            if k == 0:
                offsets[p] = (inicio, fin)
            lineas.append(
                {
                    "layout": {
                        "textAnchor": {"textSegments": [{"startIndex": inicio, "endIndex": fin}]},
                        "boundingPoly": {"normalizedVertices": [{"x": 0.1, "y": 0.1 + 0.1 * k}, {"x": 0.6, "y": 0.1 + 0.1 * k}, {"x": 0.6, "y": 0.15 + 0.1 * k}, {"x": 0.1, "y": 0.15 + 0.1 * k}]},
                        "confidence": 0.99,
                        "orientation": "PAGE_UP",
                    }
                }
            )
        paginas_raw.append(
            {
                "pageNumber": p,
                "dimension": {"width": 612, "height": 792, "unit": "point"},
                "detectedLanguages": [{"languageCode": "es", "confidence": 0.99}],
                "lines": lineas,
            }
        )

    jitter = 0.001 * (paginas[0] % 5)  # la misma entidad vista desde dos bloques no cae en el mismo punto exacto

    def hoja_raw(e):
        vistas = [(p, t, y) for p, t, y in e["partes"] if p in visibles]
        if not vistas:
            return None
        p0 = vistas[0][0]
        return {
            "type": e["tipo"],
            "mentionText": " ".join(t for _, t, _ in vistas),
            "confidence": e["conf"],
            "pageAnchor": {"pageRefs": [_ref(paginas, p, y, jitter) for p, _, y in vistas]},
            "textAnchor": {"textSegments": [{"startIndex": offsets[p0][0], "endIndex": offsets[p0][1]}]},
        }

    entidades = []
    for e in VIRTUAL:
        if e["clase"] == "hoja":
            raw = hoja_raw(e)
            if raw:
                entidades.append(raw)
        else:
            for fila in e["filas"]:
                hijos = [h for h in (hoja_raw(c) for c in fila) if h]
                if hijos:
                    entidades.append({"type": e["tipo"], "mentionText": "", "confidence": 1.0, "properties": hijos})
    return {"document": {"text": texto, "pages": paginas_raw, "entities": entidades}}


ESTRUCTURA = {
    "denominacion_social": {"obligatorio": True, "multiple": False},
    "objeto_social": {"obligatorio": True, "multiple": False},
    "fecha_firma": {"obligatorio": True, "multiple": False},
    "capital_social": {"obligatorio": True, "multiple": False},  # el documento virtual NO lo trae
    "socios": {"obligatorio": False, "multiple": True},
    "accionistas": {
        "obligatorio": True,
        "multiple": True,
        "hijos": {"nombre": {"obligatorio": True, "multiple": False}, "acciones": {"obligatorio": True, "multiple": False}, "monto": {"obligatorio": True, "multiple": False}},
    },
}


class Google:
    """El `_procesar` de mentiras. `llamadas` guarda las `opciones` de cada llamada (None = el documento entero)."""

    def __init__(self, total=20, limite=15, con_paginas=True):
        self.total, self.limite, self.con_paginas = total, limite, con_paginas
        self.llamadas: list = []
        self.fallos: dict[tuple[int, int], list] = {}  # (desde, hasta) → errores que se levantan, en orden
        self.respuesta_vacia = False
        self.demora = 0.0
        self.demoras: dict[tuple[int, int], float] = {}  # demora propia de un bloque
        self.activas = 0
        self.max_activas = 0

    async def __call__(self, procesador_id, contenido, mime_type, version="", opciones=None):
        self.llamadas.append(opciones)
        if opciones is None:
            raise ErrorDocumentAI(
                f"Document AI respondió 400: Document pages exceed the limit: {self.limite} got {self.total}",
                status_code=400,
                motivo="limite_paginas",
                paginas=self.total if self.con_paginas else None,
                limite=self.limite if self.con_paginas else None,
            )
        paginas = opciones["processOptions"]["individualPageSelector"]["pages"]
        clave = (paginas[0], paginas[-1])
        self.activas += 1
        self.max_activas = max(self.max_activas, self.activas)
        try:
            espera = self.demoras.get(clave, self.demora)
            if espera:
                await asyncio.sleep(espera)
            pendientes = self.fallos.get(clave)
            if pendientes:
                raise pendientes.pop(0)
            if self.respuesta_vacia:
                return {"document": {"text": "", "pages": []}}
            return respuesta_del_bloque(paginas)
        finally:
            self.activas -= 1


async def _estructura(procesador_id):
    return ESTRUCTURA


async def _sin_estructura(procesador_id):
    return None


ia._estructura_de = _estructura  # type: ignore[assignment]
ia.PAUSA_REINTENTO_BLOQUE_S = 0  # las pruebas no esperan

TOPE_ORIGINAL = ia.EXTRACCION_MAX_PAGINAS


async def _correr(google: Google, **kwargs):
    """Corre la extracción y mira cuántas llamadas a Google quedan ACTIVAS en cuanto termina, antes de que `asyncio.run` cierre el
    loop (que cancelaría lo que haya quedado suelto y taparía el defecto)."""
    ia._procesar = google  # type: ignore[assignment]
    try:
        resultado = await ia.extraer_con_procesador("proc-acta", b"%PDF-falso", "application/pdf", "v1", **kwargs)
        return resultado, None, google.activas
    except BaseException as exc:  # noqa: BLE001
        return None, exc, google.activas


def extraer_con_estado(google: Google, **kwargs):
    """→ (resultado o None, excepción o None, llamadas activas al terminar)."""
    return asyncio.run(_correr(google, **kwargs))


def extraer(google: Google, **kwargs) -> dict:
    resultado, exc, _ = extraer_con_estado(google, **kwargs)
    if exc is not None:
        raise exc
    return resultado


def seleccion(opciones) -> tuple[int, int] | None:
    if opciones is None:
        return None
    paginas = opciones["processOptions"]["individualPageSelector"]["pages"]
    return paginas[0], paginas[-1]


def hojas(nodo):
    """Todos los campos hoja de un resultado (sueltos, de listas y de filas), sin entrar a `ocr` ni `_metadata`."""
    if isinstance(nodo, list):
        for x in nodo:
            yield from hojas(x)
    elif isinstance(nodo, dict):
        if ia._es_campo(nodo):
            yield nodo
        else:
            for k, v in nodo.items():
                if k not in ("ocr", "_metadata"):
                    yield from hojas(v)


# ── 1. El caso de verdad: 20 páginas ─────────────────────────────────────────
caso("[1] Un acta de 20 páginas: Google la rechaza entera, se parte en 2 bloques y se piden SOLO esas páginas")
g = Google(total=20)
r = extraer(g)
selecciones = [seleccion(o) for o in g.llamadas]
verificar(selecciones[0] is None, "primero se intenta entera (sin opciones), como siempre", f"la primera llamada fue {selecciones[0]}")
verificar(sorted(s for s in selecciones if s) == [(1, 11), (11, 20)], "luego los bloques 1-11 y 11-20 (parejos, con una página de solape)", f"llamadas: {selecciones}")
verificar(len(g.llamadas) == 3, "tres llamadas en total (la rechazada no se cobra)", f"hubo {len(g.llamadas)}")
meta = r["_metadata"]
verificar(meta.get("paginas_totales") == 20 and meta.get("bloques") == [{"desde": 1, "hasta": 11}, {"desde": 11, "hasta": 20}], "_metadata dice 20 páginas y los dos bloques", f"_metadata: {meta}")
verificar(meta.get("paginas_procesadas") == 21, "paginas_procesadas = 21 (las 20 + la del solape, que Google cobra dos veces)", f"quedó {meta.get('paginas_procesadas')}")
verificar(meta.get("engine_version") == "v1" and meta.get("quality_alert") is False, "engine_version y quality_alert como en un documento entero", f"_metadata: {meta}")
for llave in ("ocr", "confianza_promedio", "confianza_minima", "_metadata"):
    verificar(llave in r, f"'{llave}' presente", f"falta '{llave}'")

caso("[2] Las páginas son las del DOCUMENTO ENTERO, no las del bloque (las entidades apuntan por índice dentro del bloque)")
verificar(r["denominacion_social"]["page_number"] == 1, "denominacion_social en la pág 1 (bloque 1)", f"quedó {r['denominacion_social']['page_number']}")
verificar(r["fecha_firma"]["page_number"] == 19, "fecha_firma en la pág 19 (bloque 2: índice 8 → pág 19; sin la traducción saldría la 9)", f"quedó {r['fecha_firma']['page_number']}")
verificar([f["nombre"]["page_number"] for f in r["accionistas"]] == [8, 8, 11], "los accionistas, en las págs 8, 8 y 11", f"quedó {[f['nombre']['page_number'] for f in r['accionistas']]}")
verificar(r["ocr"]["page_count"] == 20, "ocr.page_count = 20 (el front decide con él si muestra «Página N»)", f"quedó {r['ocr']['page_count']}")
verificar([p["page_number"] for p in r["ocr"]["paginas"]] == list(range(1, 21)), "ocr.paginas: de la 1 a la 20, cada una una vez", f"quedó {[p['page_number'] for p in r['ocr']['paginas']]}")
verificar({b["page_number"] for b in r["ocr"]["bloques"]} == set(range(1, 21)), "los renglones de OCR cubren las 20 páginas", f"cubren {sorted({b['page_number'] for b in r['ocr']['bloques']})}")
verificar(len(r["ocr"]["bloques"]) == 40, "40 renglones (2 por página): los de la página 11 no se duplican por el solape", f"hay {len(r['ocr']['bloques'])}")
verificar([b["indice"] for b in r["ocr"]["bloques"]] == list(range(40)), "los índices de los renglones son consecutivos", "índices rotos")

caso("[3] Cada campo apunta al renglón de OCR de SU página (los índices del bloque 2 se corrigieron)")
malos = []
for c in hojas(r):
    i = c["bloque_indice"]
    if i is None:
        malos.append(f"{c['value_normalized']}: sin bloque")
    elif not (0 <= i < len(r["ocr"]["bloques"])) or r["ocr"]["bloques"][i]["page_number"] != c["page_number"]:
        malos.append(f"{c['value_normalized']}: pág {c['page_number']} apunta al bloque {i}")
verificar(not malos, "los campos de las págs 1, 3, 8, 11, 12, 15 y 19 apuntan a un renglón de su propia página", "; ".join(malos))

caso("[4] Lo que cruza el corte sale UNA vez y completo")
verificar([f["nombre"]["value_normalized"] for f in r["accionistas"]] == ["JUAN PEREZ", "MARIA GONZALEZ", "CARLOS RAMIREZ"], "tres accionistas, CARLOS una sola vez (el bloque 1 lo vio partido y el 2 completo)", f"quedó {[f['nombre']['value_normalized'] for f in r['accionistas']]}")
carlos = r["accionistas"][2]
verificar(list(carlos) == ["nombre", "acciones", "monto"] and carlos["monto"]["page_number"] == 12, "CARLOS trae su monto, que está en la página siguiente al corte", f"quedó {carlos}")
verificar(r["objeto_social"]["value_normalized"] == CLAUSULA_1 + " " + CLAUSULA_2, "la cláusula del objeto sale entera aunque el pedazo cortado traiga más confianza (99 contra 90)", f"quedó {r['objeto_social']['value_normalized']!r}")
verificar([s["value_normalized"] for s in r["socios"]] == ["ANA", "BETO", "CARLA"], "los socios: ANA, BETO (en el solape, visto por los dos bloques) y CARLA, sin repetir a BETO", f"quedó {[s['value_normalized'] for s in r['socios']]}")

caso("[5] Lo obligatorio que NO salió se señala sobre el resultado ya unido")
verificar(meta.get("campos_ausentes") == ["capital_social"], "campos_ausentes = ['capital_social'] (lo demás sí salió, completo)", f"quedó {meta.get('campos_ausentes')}")

caso("[6] Las confianzas resumen se calculan sobre lo unido")
confs = [c["confianza"] for c in hojas(r)]
verificar(abs(r["confianza_promedio"] - round(sum(confs) / len(confs), 2)) < 0.011 and r["confianza_minima"] == min(confs), "promedio y mínimo salen de los campos publicados (no de los descartados)", f"promedio {r['confianza_promedio']} mínimo {r['confianza_minima']} contra {confs}")

# ── 7. Otros tamaños ─────────────────────────────────────────────────────────
caso("[7] Un documento de 15 páginas o menos NO se parte: una sola llamada, como siempre")
class Corto(Google):
    async def __call__(self, procesador_id, contenido, mime_type, version="", opciones=None):
        self.llamadas.append(opciones)
        return respuesta_del_bloque(list(range(1, 11)))


g = Corto(total=10)
r_corto = extraer(g)
verificar(len(g.llamadas) == 1 and g.llamadas[0] is None, "una llamada, sin selector de páginas", f"llamadas: {g.llamadas}")
verificar("bloques" not in r_corto["_metadata"] and "paginas_totales" not in r_corto["_metadata"], "_metadata no trae nada de bloques", f"_metadata: {r_corto['_metadata']}")
verificar(r_corto["accionistas"][0]["nombre"]["page_number"] == 8, "las páginas son las de siempre (índice 7 → pág 8)", f"quedó {r_corto['accionistas'][0]['nombre']['page_number']}")

caso("[8] 16 páginas: 2 bloques de 9 y 8 (no 15 + 1)")
g = Google(total=16)
extraer(g)
verificar(sorted(s for s in map(seleccion, g.llamadas) if s) == [(1, 9), (9, 16)], "bloques 1-9 y 9-16", f"llamadas: {[seleccion(o) for o in g.llamadas]}")

caso("[9] Más páginas que el máximo (20): se rechaza SIN procesar nada y el mensaje trae los números")
g = Google(total=45)
try:
    extraer(g)
    mal("no levantó error")
except ErrorDocumentAI as exc:
    verificar(exc.motivo == "limite_paginas" and exc.paginas == 45 and exc.limite == 20 and exc.status_code == 422, "limite_paginas, 45 páginas, máximo 20, 422", f"motivo={exc.motivo} paginas={exc.paginas} limite={exc.limite}")
verificar(len(g.llamadas) == 1, "una sola llamada (la que Google rechazó, sin costo): ningún bloque se procesó", f"hubo {len(g.llamadas)} llamadas")

caso("[10] Exactamente en el máximo (20) sí se procesa; con 21 no")
g = Google(total=21)
try:
    extraer(g)
    mal("21 páginas no se rechazó")
except ErrorDocumentAI as exc:
    verificar(exc.paginas == 21 and exc.limite == 20, "21 páginas con máximo 20 → rechazo", f"paginas={exc.paginas} limite={exc.limite}")

caso("[11] INE no se parte en bloques (permitir_bloques=False): el rechazo de Google sale tal cual, con SUS números")
g = Google(total=20)
try:
    extraer(g, permitir_bloques=False)
    mal("no levantó error")
except ErrorDocumentAI as exc:
    verificar(exc.motivo == "limite_paginas" and exc.paginas == 20 and exc.limite == 15, "limite_paginas, 20 páginas, tope 15", f"motivo={exc.motivo} paginas={exc.paginas} limite={exc.limite}")
verificar(len(g.llamadas) == 1, "no se procesó ningún bloque", f"hubo {len(g.llamadas)} llamadas")

caso("[11b] `extraer_ine` (una credencial) NO parte nada: el rechazo sale tal cual y no se procesa ningún bloque")
g = Google(total=20)
ia._procesar = g  # type: ignore[assignment]
try:
    asyncio.run(ia.extraer_ine(b"%PDF-falso", "application/pdf"))
    mal("extraer_ine devolvió un resultado")
except ErrorDocumentAI as exc:
    verificar(exc.motivo == "limite_paginas" and exc.paginas == 20 and exc.limite == 15, "limite_paginas con los números de Google (20 y 15)", f"motivo={exc.motivo} paginas={exc.paginas} limite={exc.limite}")
verificar(len(g.llamadas) == 1, "ningún bloque se procesó (una credencial de 20 páginas no se paga)", f"hubo {len(g.llamadas)} llamadas")

caso("[12] Si Google no dijo cuántas páginas tiene el documento, no hay con qué partirlo: el error sale como siempre")
g = Google(total=20, con_paginas=False)
try:
    extraer(g)
    mal("no levantó error")
except ErrorDocumentAI as exc:
    verificar(exc.motivo == "limite_paginas" and exc.paginas is None, "limite_paginas sin números", f"motivo={exc.motivo} paginas={exc.paginas}")
verificar(len(g.llamadas) == 1, "una sola llamada", f"hubo {len(g.llamadas)}")

caso("[13] Si Google dice un tope distinto de 15, los bloques lo respetan")
g = Google(total=20, limite=10)
extraer(g)
bloques = sorted(s for s in map(seleccion, g.llamadas) if s)
verificar(all(h - d + 1 <= 10 for d, h in bloques) and len(bloques) == 3 and bloques[0][0] == 1 and bloques[-1][1] == 20, "ningún bloque pasa de 10 páginas (con el tope de 15 habría salido uno de 11) y cubren de la 1 a la 20", f"bloques: {bloques}")

# ── 14. Fallos ───────────────────────────────────────────────────────────────
caso("[14] Un bloque que falla por algo PASAJERO se repite una vez y la extracción sale bien")
g = Google(total=20)
g.fallos[(11, 20)] = [ErrorDocumentAI("Document AI respondió 503", 503, "servicio")]
r = extraer(g)
intentos = [seleccion(o) for o in g.llamadas].count((11, 20))
verificar(intentos == 2 and r["fecha_firma"]["page_number"] == 19, "el bloque 11-20 se pidió dos veces y el resultado está completo", f"intentos={intentos}")
g = Google(total=20)
g.fallos[(1, 11)] = [ErrorDocumentAI("cuota", 429, "cuota")]
extraer(g)
verificar([seleccion(o) for o in g.llamadas].count((1, 11)) == 2, "también la cuota por minuto se repite una vez", "no se repitió")

caso("[15] Si falla DOS veces, falla todo: no se entrega el documento sin un pedazo")
g = Google(total=20)
g.fallos[(11, 20)] = [ErrorDocumentAI("503", 503, "servicio"), ErrorDocumentAI("503", 503, "servicio")]
try:
    extraer(g)
    mal("devolvió un resultado incompleto")
except ErrorDocumentAI as exc:
    verificar(exc.motivo == "servicio", "el error del bloque sale tal cual (servicio)", f"motivo={exc.motivo}")
verificar([seleccion(o) for o in g.llamadas].count((11, 20)) == 2, "se intentó dos veces y no más", f"intentos={[seleccion(o) for o in g.llamadas].count((11, 20))}")

caso("[16] Lo que es del DOCUMENTO no se repite (saldría igual)")
g = Google(total=20)
g.fallos[(11, 20)] = [ErrorDocumentAI("corrupto", 400, "archivo_ilegible")]
try:
    extraer(g)
    mal("no levantó error")
except ErrorDocumentAI as exc:
    verificar(exc.motivo == "archivo_ilegible", "archivo_ilegible sale tal cual", f"motivo={exc.motivo}")
verificar([seleccion(o) for o in g.llamadas].count((11, 20)) == 1, "se intentó una sola vez", "se repitió")

caso("[17] Cuando un bloque falla, el otro no sigue trabajando (ni cobrando) en el aire")
g = Google(total=20)
g.demoras[(11, 20)] = 5.0  # el bloque 2 tarda; el 1 falla enseguida
g.fallos[(1, 11)] = [ErrorDocumentAI("archivo", 400, "archivo_ilegible")]
inicio = time.time()
_, error, activas = extraer_con_estado(g)
verificar(isinstance(error, ErrorDocumentAI) and time.time() - inicio < 3, f"el error sale enseguida ({time.time() - inicio:.1f} s), sin esperar al otro bloque", f"error={error!r} tardó {time.time() - inicio:.1f} s")
verificar(activas == 0, "al salir el error no queda ninguna llamada activa: el bloque lento se canceló", f"quedaron {activas} activas")

caso("[18] Si los bloques no terminan a tiempo: 504 (httpx.ReadTimeout), sin dejar trabajo suelto")
g = Google(total=20)
g.demora = 5
tiempo_original = ia.IA_TIMEOUT
ia.IA_TIMEOUT = 0.2
inicio = time.time()
try:
    _, error, activas = extraer_con_estado(g)
finally:
    ia.IA_TIMEOUT = tiempo_original
verificar(isinstance(error, httpx.TimeoutException), "levanta httpx.TimeoutException (el router lo traduce a 504)", f"error={error!r}")
verificar(time.time() - inicio < 3, f"se rindió a los {time.time() - inicio:.1f} s (no esperó los 5 s del bloque)", f"tardó {time.time() - inicio:.1f} s")
verificar(activas == 0, "no queda ninguna llamada activa: los bloques se cancelaron", f"quedaron {activas}")

caso("[19] Sin ninguna entidad en NINGÚN bloque: quality_alert, como un documento entero")
g = Google(total=20)
g.respuesta_vacia = True
r = extraer(g)
verificar(r["_metadata"]["quality_alert"] is True and r["confianza_promedio"] is None and "ocr" not in r, "quality_alert, sin confianzas y sin ocr", f"quedó {r}")
verificar(r["_metadata"].get("paginas_totales") == 20, "y _metadata dice cuántas páginas tenía", f"_metadata: {r['_metadata']}")

caso("[19b] Si solo UN bloque no trae entidades, no es quality_alert: lo del otro bloque sí se entrega")


class UnoVacio(Google):
    async def __call__(self, procesador_id, contenido, mime_type, version="", opciones=None):
        if opciones and seleccion(opciones) == (11, 20):
            self.llamadas.append(opciones)
            return {"document": {"text": "", "pages": [{"pageNumber": p, "dimension": {}} for p in range(11, 21)]}}
        return await super().__call__(procesador_id, contenido, mime_type, version, opciones)


r = extraer(UnoVacio(total=20))
verificar(r["_metadata"]["quality_alert"] is False and r["denominacion_social"]["page_number"] == 1, "sin alerta y con los campos del bloque que sí leyó", f"_metadata: {r['_metadata']}")

# ── 20. Concurrencia ─────────────────────────────────────────────────────────
caso("[20] Los bloques corren a la vez, pero no más de BLOQUES_EN_PARALELO")
ia.EXTRACCION_MAX_PAGINAS = 100
ia.EXTRACCION_PAGINAS_POR_BLOQUE = 10
g = Google(total=60)
g.demora = 0.05
extraer(g)
n_bloques = len([o for o in g.llamadas if o])
verificar(n_bloques >= 6 and g.max_activas == 3, f"{n_bloques} bloques, nunca más de 3 a la vez (máximo visto: {g.max_activas})", f"bloques={n_bloques} máximo simultáneo={g.max_activas}")
ia.BLOQUES_EN_PARALELO = 2
g = Google(total=60)
g.demora = 0.05
extraer(g)
verificar(g.max_activas == 2, "con BLOQUES_EN_PARALELO=2, nunca más de 2", f"máximo simultáneo={g.max_activas}")
ia.BLOQUES_EN_PARALELO = 3
ia.EXTRACCION_MAX_PAGINAS = TOPE_ORIGINAL
ia.EXTRACCION_PAGINAS_POR_BLOQUE = 15

# ── 21. Lo demás que hace extraer_con_procesador ─────────────────────────────
caso("[21] `transformar` se aplica al resultado ya unido")
g = Google(total=20)
r = extraer(g, transformar=lambda datos: {**datos, "marca_del_tipo": True})
verificar(r.get("marca_del_tipo") is True and "ocr" in r, "la transformación del tipo corre sobre lo unido y la capa de OCR sigue ahí", f"llaves: {list(r)[:8]}")

caso("[22] Sin estructura (no se pudo leer el esquema): se une igual, infiriendo lo múltiple, y no se inventa campos_ausentes")
ia._estructura_de = _sin_estructura  # type: ignore[assignment]
g = Google(total=20)
r = extraer(g)
ia._estructura_de = _estructura  # type: ignore[assignment]
verificar("campos_ausentes" not in r["_metadata"], "sin la clave campos_ausentes (no se sabe qué era obligatorio)", f"_metadata: {r['_metadata']}")
verificar([s["value_normalized"] for s in r["socios"]] == ["ANA", "BETO", "CARLA"] and len(r["accionistas"]) == 3, "socios y accionistas siguen siendo listas, sin repetidos", f"socios={r.get('socios')}")

caso("[23] Si Google devolviera páginas RELATIVAS al bloque, igual salen las del documento entero")
original = respuesta_del_bloque


def con_numeros_relativos(paginas):
    raw = original(paginas)
    for i, p in enumerate(raw["document"]["pages"], start=1):
        p["pageNumber"] = i  # 1…n en vez de 11…20
    return raw


respuesta_del_bloque = con_numeros_relativos  # type: ignore[assignment]
g = Google(total=20)
r = extraer(g)
respuesta_del_bloque = original  # type: ignore[assignment]
verificar(r["fecha_firma"]["page_number"] == 19 and [p["page_number"] for p in r["ocr"]["paginas"]] == list(range(1, 21)), "fecha_firma en la 19 y ocr.paginas de la 1 a la 20 (se confía en lo que se pidió)", f"fecha_firma={r['fecha_firma']['page_number']} paginas={[p['page_number'] for p in r['ocr']['paginas']]}")

# ── 24. El error de límite de Google, leído de la respuesta HTTP ─────────────
caso("[24] `_procesar` saca cuántas páginas tiene el documento del error de Google (details.metadata, o el mensaje)")


class RespuestaHttp:
    def __init__(self, status_code, cuerpo):
        self.status_code, self._cuerpo = status_code, cuerpo
        self.text = str(cuerpo)

    def json(self):
        return self._cuerpo


def cliente_falso(respuesta):
    class Cliente:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            return respuesta

    return Cliente


_PROCESAR_REAL = None
# `ia._procesar` quedó sustituido más arriba: se vuelve a leer el de verdad recargando el módulo no hace falta, se reconstruye.
import importlib  # noqa: E402

_modulo_real = importlib.reload(ia)
ia_real = _modulo_real
ia_real._token = lambda: "token"  # type: ignore[assignment]
cliente_original = ia_real.httpx.AsyncClient


def probar(cuerpo_error, status=400):
    ia_real.httpx.AsyncClient = cliente_falso(RespuestaHttp(status, {"error": cuerpo_error}))  # type: ignore[assignment]
    try:
        asyncio.run(ia_real._procesar("proc", b"x", "application/pdf", "v1"))
    except ErrorDocumentAI as exc:
        return exc
    finally:
        ia_real.httpx.AsyncClient = cliente_original  # type: ignore[assignment]
    raise AssertionError("no levantó")


e = probar({"message": "Document pages exceed the limit: 15 got 20", "details": [{"reason": "PAGE_LIMIT_EXCEEDED", "metadata": {"page_limit": "15", "pages": "20"}}]})
verificar(e.motivo == "limite_paginas" and e.paginas == 20 and e.limite == 15, "del details.metadata: 20 páginas, tope 15", f"motivo={e.motivo} paginas={e.paginas} limite={e.limite}")
e = probar({"message": "Document pages exceed the limit: 15 got 28", "details": []})
verificar(e.motivo == "limite_paginas" and e.paginas == 28 and e.limite == 15, "si no hay metadata, del texto del mensaje: 28 páginas, tope 15", f"motivo={e.motivo} paginas={e.paginas} limite={e.limite}")
e = probar({"message": "Document pages exceed the limit, pero sin números", "details": [{"reason": "PAGE_LIMIT_EXCEEDED"}]})
verificar(e.motivo == "limite_paginas" and e.paginas is None, "sin números en ningún lado, paginas=None (no se inventa)", f"paginas={e.paginas}")
e = probar({"message": "Quota exceeded", "details": [{"reason": "RATE_LIMIT_EXCEEDED", "metadata": {"pages": "99", "page_limit": "1"}}]}, 429)
verificar(e.motivo == "cuota" and e.paginas is None, "un error que NO es de límite de páginas no trae páginas aunque su metadata las tenga", f"motivo={e.motivo} paginas={e.paginas}")

caso("[25] El router dice cuántas páginas tiene el documento y cuál es el máximo (y sin números, el texto general)")
from routers.ia import _fallar  # noqa: E402

h = _fallar(ErrorDocumentAI("x", 422, "limite_paginas", paginas=25, limite=20), "generico")
verificar(h.status_code == 422 and h.detail == "El documento tiene 25 páginas y el máximo que se procesa es 20.", "422 «El documento tiene 25 páginas y el máximo que se procesa es 20.»", f"{h.status_code} {h.detail!r}")
h = _fallar(ErrorDocumentAI("x", 422, "limite_paginas"), "generico")
verificar(h.status_code == 422 and "excede el máximo de páginas" in h.detail and "25" not in h.detail, "sin números, el texto general", f"{h.status_code} {h.detail!r}")
h = _fallar(ErrorDocumentAI("x", 503, "servicio", paginas=25, limite=20), "generico")
verificar(h.status_code == 502 and h.detail == "Document AI no está disponible en este momento. Intenta de nuevo en unos minutos.", "otro motivo no se toca (aunque traiga números)", f"{h.status_code} {h.detail!r}")

print()
if fallas:
    print(f"{fallas} caso(s) NO cuadran")
    raise SystemExit(1)
print("todo cuadra")
