"""La bitácora de la búsqueda y los ejemplos que aprueba cada organización (NEX-609, 2026-11-23; migración 0018). Todo el SQL de `nexus.searchQuestions`
y `nexus.searchExamples` vive aquí; el camino de una pregunta (`servicios/busqueda.py`) solo llama.

## Qué se guarda de cada pregunta

Quién, de qué organización y cuándo; cómo terminó (`outcome`), la consulta que se corrió y de dónde salió (el modelo, un ejemplo aprobado o una pregunta
igual anterior), cuántos renglones dio, cuánto tardó, los tokens del modelo y lo que costó. **Los renglones de la respuesta no se guardan.** Se guarda
TODA pregunta que llega con una organización válida y texto: también las que fallan, las que se cortan por tiempo y las que frenó el tope.

## Topes

Por persona en la última hora y por organización en las últimas 24 horas (`config.BUSQUEDA_TOPE_*`), contando lo que hay en la bitácora salvo lo que ya
frenó un tope (si no, quien insiste nunca saldría). Al llegar, `tope_alcanzado` dice hasta cuándo: cuando la pregunta más vieja de la ventana salga de ella.

## La misma pregunta, la misma consulta

`reusable` busca, por la pregunta normalizada (`clave_de`): primero un ejemplo APROBADO de la organización; si no, la última pregunta de la organización
que salió bien con el MISMO catálogo (la huella de lo que el modelo supo de sus tipos documentales: si un tipo cambió, su consulta podría ya no servir) y
que nadie marcó como «no me sirvió». Lo que se reusa es la CONSULTA; los datos se leen otra vez.

## Ejemplos

Una respuesta marcada «Sí me sirvió» aparece a quien administra la organización en «Por revisar» (una por pregunta, la más reciente). Al aprobarla se
vuelve un ejemplo de ESA organización; al descartarla, sale de la lista (vuelve si alguien la marca útil otra vez). Quitar un ejemplo la descarta también.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pyodbc

import config
from db import tablas
from servicios.tipos_documentales_comun import momento

logger = logging.getLogger(__name__)

RESULTADOS = ("ok", "no_se_puede", "consulta_invalida", "tardo", "modelo", "sin_configurar", "tope", "error")
#: Lo que cuenta como que el SISTEMA falló (para Monitor). `no_se_puede` no: el modelo contestó que esos datos no responden la pregunta.
FALLIDAS = ("consulta_invalida", "tardo", "modelo", "sin_configurar", "error")
#: Cuántos ejemplos aprobados se consideran al elegir (los más recientes): con miles, leer todos sus vectores en cada pregunta pesaría.
MAX_EJEMPLOS_A_ELEGIR = 500


class NoEncontrada(LookupError):
    """No hay tal pregunta o ejemplo EN ESA organización (o no es de quien pide): no se distingue."""


class NoValorable(Exception):
    """La pregunta existe pero no salió bien: no hay respuesta que valorar ni aprobar."""


class YaEsEjemplo(Exception):
    """Esa pregunta ya es un ejemplo aprobado de la organización. `ejemplo_id` es el suyo."""

    def __init__(self, ejemplo_id: str):
        super().__init__("ya es ejemplo")
        self.ejemplo_id = ejemplo_id


@dataclass(eq=False)
class TopeAlcanzado(Exception):
    """Llegó al tope de uso: la pregunta no se hace. `reintentar_en` (UTC) es cuándo sale de la ventana la pregunta que sobra."""

    alcance: str  # 'persona' | 'organizacion'
    tope: int
    ventana: str  # 'hora' | 'dia'
    reintentar_en: datetime

    def __post_init__(self):
        super().__init__(self.mensaje)

    @property
    def mensaje(self) -> str:
        if self.alcance == "persona":
            return f"Llegaste al tope de {self.tope} preguntas por hora."
        return f"Tu organización llegó al tope de {self.tope} preguntas por día."


# ── Lo que se guarda ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def clave_de(pregunta: str) -> str:
    """La pregunta como se compara: minúsculas, sin acentos ni signos, con un espacio entre palabras. «¿Cuántas INE vencen este año?» y «cuantas ine
    vencen este ano» son la misma."""
    plano = unicodedata.normalize("NFD", str(pregunta).casefold())
    plano = "".join(c for c in plano if unicodedata.category(c) != "Mn")
    plano = " ".join(re.sub(r"[^0-9a-z]+", " ", plano).split())
    return (plano or " ".join(str(pregunta).casefold().split()))[:500]


def costo(entrada: int, cache: int, salida: int, embeddings: int) -> float:
    """Lo que costó, en dólares, con los precios de lista del `.env` (ver `config`). `cache` es la parte de `entrada` que vino de la caché de Google."""
    sin_cache = max(0, entrada - cache)
    return (
        sin_cache * config.GEMINI_PRECIO_ENTRADA_USD_MILLON
        + cache * config.GEMINI_PRECIO_ENTRADA_CACHE_USD_MILLON
        + salida * config.GEMINI_PRECIO_SALIDA_USD_MILLON
        + embeddings * config.EMBEDDINGS_PRECIO_USD_MILLON
    ) / 1_000_000


def nuevo_registro(tenant: str, usuario: str, pregunta: str, clave: str) -> dict:
    """Lo que se irá llenando mientras se contesta; `guardar` lo escribe al final, salga como salga."""
    return {
        "tenant": tenant, "usuario": usuario, "pregunta": pregunta, "clave": clave, "resultado": "error", "origen": None, "reusoDe": None,
        "consulta": None, "explicacion": None, "detalle": None, "renglones": None, "hayMas": None, "intentos": 0, "ms": 0, "msBase": None,
        "modelo": None, "entrada": 0, "cache": 0, "salida": 0, "embeddings": 0, "huella": None, "ejemplos": None,
    }


def guardar(r: dict) -> str | None:
    """Escribe el renglón de la bitácora y devuelve su `publicId`. Nunca levanta: si la base falla, se avisa en el log y la respuesta sigue (sin
    valoración posible)."""
    publico = "pregunta-" + uuid.uuid4().hex
    usados = r["entrada"] or r["salida"] or r["embeddings"]
    try:
        with tablas.transaccion() as cur:
            cur.execute(
                "INSERT INTO nexus.searchQuestions (publicId, tenant, userGuid, question, questionKey, outcome, origin, reusedFrom, sqlText, explanation, "
                "detail, resultRows, hasMore, attempts, durationMs, databaseMs, model, inputTokens, cachedTokens, outputTokens, embeddingTokens, costUsd, "
                "catalogHash, examplesJson) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                publico, r["tenant"], r["usuario"], r["pregunta"][:500], r["clave"][:500], r["resultado"], r["origen"], r["reusoDe"], r["consulta"],
                (r["explicacion"] or None) and r["explicacion"][:300], (r["detalle"] or None) and str(r["detalle"])[:500], r["renglones"],
                r["hayMas"], min(255, int(r["intentos"])), max(0, int(r["ms"])), r["msBase"], r["modelo"],
                r["entrada"] if usados else None, r["cache"] if usados else None, r["salida"] if usados else None, r["embeddings"] if usados else None,
                Decimal(str(round(costo(r["entrada"], r["cache"], r["salida"], r["embeddings"]), 6))) if usados else Decimal("0"),
                r["huella"], json.dumps(r["ejemplos"]) if r["ejemplos"] is not None else None,
            )
        return publico
    except Exception:  # noqa: BLE001 — la bitácora nunca tumba una respuesta
        logger.warning("Búsqueda: no se pudo anotar la pregunta en la bitácora (resultado %s).", r.get("resultado"), exc_info=True)
        return None


# ── Topes ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def tope_alcanzado(tenant: str, usuario: str) -> TopeAlcanzado | None:
    """El primer tope al que ya llegó (el de la persona primero), o `None`."""
    reglas = (
        ("persona", config.BUSQUEDA_TOPE_PERSONA_POR_HORA, 60, "hora", "AND userGuid = ?", (usuario,)),
        ("organizacion", config.BUSQUEDA_TOPE_ORGANIZACION_POR_DIA, 1440, "dia", "", ()),
    )
    with tablas.lectura() as cur:
        for alcance, tope, minutos, ventana, filtro, extra in reglas:
            donde = f"WHERE tenant = ? {filtro} AND outcome <> 'tope' AND createdAt > DATEADD(minute, ?, SYSUTCDATETIME())"
            cur.execute(f"SELECT COUNT(*) FROM nexus.searchQuestions {donde}", tenant, *extra, -minutos)
            cuantas = int(cur.fetchone()[0])
            if cuantas < tope:
                continue
            # La que tiene que salir de la ventana para que quepa una más: la (cuantas - tope + 1)-ésima más vieja.
            cur.execute(
                f"SELECT createdAt FROM nexus.searchQuestions {donde} ORDER BY createdAt OFFSET ? ROWS FETCH NEXT 1 ROWS ONLY",
                tenant, *extra, -minutos, cuantas - tope,
            )
            fila = cur.fetchone()
            desde = fila[0] if fila else datetime.now(timezone.utc).replace(tzinfo=None)
            return TopeAlcanzado(alcance, tope, ventana, (desde + timedelta(minutes=minutos)).replace(tzinfo=timezone.utc))
    return None


# ── Reusar ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def reusable(tenant: str, clave: str, huella: str) -> dict | None:
    """La consulta que ya existe para esta misma pregunta en la organización: `{origen, id, consulta, explicacion}` o `None`."""
    with tablas.lectura() as cur:
        cur.execute("SELECT publicId, sqlText, explanation FROM nexus.searchExamples WHERE tenant = ? AND questionKey = ?", tenant, clave)
        e = tablas.una(cur)
        if e:
            return {"origen": "ejemplo", "id": e["publicId"], "consulta": e["sqlText"], "explicacion": e["explanation"] or ""}
        cur.execute(
            "SELECT TOP (1) publicId, sqlText, explanation, useful FROM nexus.searchQuestions "
            "WHERE tenant = ? AND questionKey = ? AND outcome = 'ok' AND catalogHash = ? ORDER BY questionId DESC",
            tenant, clave, huella,
        )
        p = tablas.una(cur)
    # `useful` es bit: True, False o None. Una respuesta marcada «no me sirvió» no se repite: se le vuelve a preguntar al modelo.
    if p and p["sqlText"] and p["useful"] is not False:
        return {"origen": "anterior", "id": p["publicId"], "consulta": p["sqlText"], "explicacion": p["explanation"] or ""}
    return None


# ── Lo que ve quien pregunta ──────────────────────────────────────────────────────────────────────────────────────────────────────────

def recientes(tenant: str, usuario: str, limite: int = 8) -> list[dict]:
    """Las últimas preguntas DISTINTAS de esa persona en esa organización (la más reciente de cada una), sin las que frenó el tope."""
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT TOP (?) q.publicId, q.question, q.outcome, q.createdAt FROM nexus.searchQuestions q "
            "JOIN (SELECT MAX(questionId) AS ultima FROM nexus.searchQuestions WHERE tenant = ? AND userGuid = ? AND outcome <> 'tope' GROUP BY questionKey) u "
            "ON u.ultima = q.questionId ORDER BY q.questionId DESC",
            max(1, min(50, int(limite))), tenant, usuario,
        )
        return [{"id": f["publicId"], "pregunta": f["question"], "resultado": f["outcome"], "en": momento(f["createdAt"])} for f in tablas.filas(cur)]


def valorar(tenant: str, usuario: str, pregunta_id: str, util: bool) -> None:
    """«¿Te sirvió?». Solo quien preguntó, y solo una respuesta que salió bien; se puede cambiar (vale la última)."""
    with tablas.transaccion() as cur:
        cur.execute("SELECT outcome FROM nexus.searchQuestions WHERE publicId = ? AND tenant = ? AND userGuid = ?", pregunta_id, tenant, usuario)
        fila = cur.fetchone()
        if fila is None:
            raise NoEncontrada(pregunta_id)
        if fila[0] != "ok":
            raise NoValorable(pregunta_id)
        cur.execute(
            "UPDATE nexus.searchQuestions SET useful = ?, ratedAt = SYSUTCDATETIME() WHERE publicId = ? AND tenant = ? AND userGuid = ?",
            bool(util), pregunta_id, tenant, usuario,
        )


# ── Lo que ve quien administra ────────────────────────────────────────────────────────────────────────────────────────────────────────

def _persona(f: dict, prefijo: str = "") -> dict | None:
    guid = f.get(f"{prefijo}Guid")
    if not guid:
        return None
    nombre = " ".join(x for x in (f.get(f"{prefijo}Nombre") or "", f.get(f"{prefijo}Apellido") or "") if x).strip()
    return {"guid": guid, "nombre": nombre or None, "correo": f.get(f"{prefijo}Correo")}


def por_revisar(tenant: str, limite: int = 50) -> list[dict]:
    """Las respuestas que alguien marcó «Sí me sirvió» y quien administra todavía no aprueba ni descarta: una por pregunta (la más reciente), con
    cuántas veces se hizo esa pregunta y cuántas personas dijeron que les sirvió."""
    with tablas.lectura() as cur:
        cur.execute(
            # (SQL Server no acepta COUNT(DISTINCT …) OVER: las personas se cuentan con una subconsulta.)
            "WITH utiles AS ("
            "  SELECT q.questionId, q.publicId, q.questionKey, q.question, q.sqlText, q.explanation, q.resultRows, q.createdAt, q.userGuid,"
            "    ROW_NUMBER() OVER (PARTITION BY q.questionKey ORDER BY q.questionId DESC) AS n"
            "  FROM nexus.searchQuestions q"
            "  WHERE q.tenant = ? AND q.outcome = 'ok' AND q.useful = 1 AND q.dismissedAt IS NULL"
            "    AND NOT EXISTS (SELECT 1 FROM nexus.searchExamples e WHERE e.tenant = q.tenant AND e.questionKey = q.questionKey)"
            ") "
            "SELECT TOP (?) u.publicId, u.question, u.sqlText, u.explanation, u.resultRows, u.createdAt,"
            "  (SELECT COUNT(DISTINCT y.userGuid) FROM nexus.searchQuestions y WHERE y.tenant = ? AND y.questionKey = u.questionKey AND y.useful = 1"
            "     AND y.dismissedAt IS NULL) AS personas,"
            "  (SELECT COUNT(*) FROM nexus.searchQuestions x WHERE x.tenant = ? AND x.questionKey = u.questionKey AND x.outcome <> 'tope') AS veces,"
            "  us.guid AS autorGuid, us.firstName AS autorNombre, us.lastName AS autorApellido, us.email AS autorCorreo "
            "FROM utiles u LEFT JOIN nexus.users us ON us.guid = u.userGuid WHERE u.n = 1 ORDER BY u.questionId DESC",
            tenant, max(1, min(200, int(limite))), tenant, tenant,
        )
        return [
            {
                "id": f["publicId"], "pregunta": f["question"], "consulta": f["sqlText"], "explicacion": f["explanation"] or "",
                "renglones": f["resultRows"], "en": momento(f["createdAt"]), "veces": int(f["veces"]), "personasUtil": int(f["personas"]),
                "autor": _persona(f, "autor"),
            }
            for f in tablas.filas(cur)
        ]


def ejemplos(tenant: str) -> list[dict]:
    """Los ejemplos aprobados de la organización, el más reciente primero, con quién lo aprobó y cuántas veces se reusó su consulta tal cual."""
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT e.publicId, e.question, e.sqlText, e.explanation, e.approvedAt,"
            "  (SELECT COUNT(*) FROM nexus.searchQuestions q WHERE q.tenant = e.tenant AND q.reusedFrom = e.publicId) AS reusos,"
            "  us.guid AS aprobadorGuid, us.firstName AS aprobadorNombre, us.lastName AS aprobadorApellido, us.email AS aprobadorCorreo "
            "FROM nexus.searchExamples e LEFT JOIN nexus.users us ON us.guid = e.approvedBy WHERE e.tenant = ? ORDER BY e.exampleId DESC",
            tenant,
        )
        return [
            {
                "id": f["publicId"], "pregunta": f["question"], "consulta": f["sqlText"], "explicacion": f["explanation"] or "",
                "aprobadoEn": momento(f["approvedAt"]), "reusos": int(f["reusos"]), "aprobadoPor": _persona(f, "aprobador"),
            }
            for f in tablas.filas(cur)
        ]


def aprobar(tenant: str, actor: str, pregunta_id: str) -> dict:
    """Vuelve ejemplo de la organización una pregunta que salió bien (con su consulta y su explicación). `YaEsEjemplo` si esa pregunta ya lo es."""
    publico = "ejemplo-" + uuid.uuid4().hex
    try:
        with tablas.transaccion() as cur:
            cur.execute(
                "SELECT questionId, question, questionKey, sqlText, explanation, outcome FROM nexus.searchQuestions WHERE publicId = ? AND tenant = ?",
                pregunta_id, tenant,
            )
            q = tablas.una(cur)
            if q is None:
                raise NoEncontrada(pregunta_id)
            if q["outcome"] != "ok" or not q["sqlText"]:
                raise NoValorable(pregunta_id)
            cur.execute("SELECT publicId FROM nexus.searchExamples WHERE tenant = ? AND questionKey = ?", tenant, q["questionKey"])
            ya = cur.fetchone()
            if ya:
                raise YaEsEjemplo(ya[0])
            cur.execute(
                "INSERT INTO nexus.searchExamples (publicId, tenant, question, questionKey, sqlText, explanation, sourceQuestionId, approvedBy) "
                "OUTPUT INSERTED.approvedAt VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                publico, tenant, q["question"], q["questionKey"], q["sqlText"], q["explanation"], q["questionId"], actor,
            )
            en = cur.fetchone()[0]
    except pyodbc.IntegrityError as exc:
        # Dos aprobaciones de la misma pregunta a la vez: la segunda choca con UQ_searchExamples_question.
        with tablas.lectura() as cur:
            cur.execute("SELECT publicId FROM nexus.searchExamples WHERE tenant = ? AND questionKey = (SELECT questionKey FROM nexus.searchQuestions WHERE publicId = ?)", tenant, pregunta_id)
            ya = cur.fetchone()
        if ya:
            raise YaEsEjemplo(ya[0]) from exc
        raise
    return {"id": publico, "pregunta": q["question"], "consulta": q["sqlText"], "explicacion": q["explanation"] or "", "aprobadoEn": momento(en)}


def descartar(tenant: str, actor: str, pregunta_id: str) -> int:
    """Saca de «Por revisar» esa pregunta (todas sus veces marcadas útiles hasta esta). Devuelve cuántas se descartaron."""
    with tablas.transaccion() as cur:
        cur.execute("SELECT questionId, questionKey FROM nexus.searchQuestions WHERE publicId = ? AND tenant = ?", pregunta_id, tenant)
        q = cur.fetchone()
        if q is None:
            raise NoEncontrada(pregunta_id)
        cur.execute(
            "UPDATE nexus.searchQuestions SET dismissedAt = SYSUTCDATETIME(), dismissedBy = ? "
            "WHERE tenant = ? AND questionKey = ? AND questionId <= ? AND useful = 1 AND dismissedAt IS NULL",
            actor, tenant, q[1], q[0],
        )
        return cur.rowcount


def quitar_ejemplo(tenant: str, actor: str, ejemplo_id: str) -> dict:
    """Borra un ejemplo aprobado (deja de usarse desde la siguiente pregunta) y descarta lo marcado útil de esa pregunta, para que no vuelva solo a
    «Por revisar». La bitácora no se toca."""
    with tablas.transaccion() as cur:
        cur.execute("DELETE FROM nexus.searchExamples OUTPUT DELETED.questionKey, DELETED.question WHERE publicId = ? AND tenant = ?", ejemplo_id, tenant)
        fila = cur.fetchone()
        if fila is None:
            raise NoEncontrada(ejemplo_id)
        cur.execute(
            "UPDATE nexus.searchQuestions SET dismissedAt = SYSUTCDATETIME(), dismissedBy = ? WHERE tenant = ? AND questionKey = ? AND useful = 1 AND dismissedAt IS NULL",
            actor, tenant, fila[0],
        )
    return {"id": ejemplo_id, "pregunta": fila[1]}


# ── Para elegir ejemplos ──────────────────────────────────────────────────────────────────────────────────────────────────────────────

def ejemplos_para_elegir(tenant: str) -> list[dict]:
    """Los ejemplos aprobados de la organización con su vector (si ya lo tienen): `[{id, pregunta, consulta, vector, etiqueta}]`, los más viejos primero
    (hasta `MAX_EJEMPLOS_A_ELEGIR`, los más recientes)."""
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT * FROM (SELECT TOP (?) exampleId, publicId, question, sqlText, embeddingJson, embeddingModel FROM nexus.searchExamples "
            "WHERE tenant = ? ORDER BY exampleId DESC) x ORDER BY exampleId",
            MAX_EJEMPLOS_A_ELEGIR, tenant,
        )
        salida = []
        for f in tablas.filas(cur):
            vector = None
            if f["embeddingJson"]:
                try:
                    v = json.loads(f["embeddingJson"])
                    vector = v if isinstance(v, list) else None
                except ValueError:
                    vector = None
            salida.append({"id": f["publicId"], "pregunta": f["question"], "consulta": f["sqlText"], "vector": vector, "etiqueta": f["embeddingModel"]})
        return salida


def guardar_vector(tenant: str, ejemplo_id: str, vector: list[float], etiqueta: str) -> None:
    """Guarda el vector de un ejemplo (redondeado: con 6 decimales sobra para comparar). Nunca levanta: se puede volver a sacar."""
    try:
        with tablas.transaccion() as cur:
            cur.execute(
                "UPDATE nexus.searchExamples SET embeddingJson = ?, embeddingModel = ? WHERE publicId = ? AND tenant = ?",
                json.dumps([round(x, 6) for x in vector]), etiqueta, ejemplo_id, tenant,
            )
    except Exception:  # noqa: BLE001
        logger.warning("Búsqueda: no se pudo guardar el vector del ejemplo %s.", ejemplo_id, exc_info=True)


# ── Nexus Monitor ─────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def _numero(v):
    if v is None:
        return None
    if isinstance(v, Decimal):
        return float(v)
    return v


def panorama_monitor(dias: int = 30) -> dict:
    """Las preguntas de los últimos `dias` días por organización: cuántas, cuántas respondió, cuántas no se pudieron responder con los datos, cuántas
    fallaron, cuántas frenó el tope, cuántas reusaron una consulta, cuántas personas preguntaron, el tiempo promedio, los tokens y el costo; y los
    totales con el desglose por resultado. Solo números: nunca el texto de las preguntas (pueden traer datos de personas)."""
    dias = max(1, min(365, int(dias)))
    fallidas = ", ".join(f"'{r}'" for r in FALLIDAS)
    with tablas.lectura() as cur:
        cur.execute(
            "SELECT q.tenant, t.name AS nombre, COUNT(*) AS preguntas,"
            "  SUM(CASE WHEN q.outcome = 'ok' THEN 1 ELSE 0 END) AS respondidas,"
            "  SUM(CASE WHEN q.outcome = 'no_se_puede' THEN 1 ELSE 0 END) AS noSePuede,"
            f"  SUM(CASE WHEN q.outcome IN ({fallidas}) THEN 1 ELSE 0 END) AS fallidas,"
            "  SUM(CASE WHEN q.outcome = 'tope' THEN 1 ELSE 0 END) AS topadas,"
            "  SUM(CASE WHEN q.outcome = 'ok' AND q.origin IN ('ejemplo', 'anterior') THEN 1 ELSE 0 END) AS reusadas,"
            "  SUM(CASE WHEN q.useful = 1 THEN 1 ELSE 0 END) AS utiles, SUM(CASE WHEN q.useful = 0 THEN 1 ELSE 0 END) AS noUtiles,"
            "  COUNT(DISTINCT q.userGuid) AS personas,"
            "  AVG(CASE WHEN q.outcome <> 'tope' THEN CAST(q.durationMs AS bigint) END) AS msPromedio,"
            "  SUM(COALESCE(q.inputTokens, 0)) AS tokensEntrada, SUM(COALESCE(q.outputTokens, 0)) AS tokensSalida,"
            "  SUM(COALESCE(q.costUsd, 0)) AS costoUsd, MAX(q.createdAt) AS ultimaEn,"
            "  (SELECT COUNT(*) FROM nexus.searchExamples e WHERE e.tenant = q.tenant) AS ejemplos "
            "FROM nexus.searchQuestions q LEFT JOIN nexus.tenants t ON t.guid = q.tenant "
            "WHERE q.createdAt >= DATEADD(day, ?, SYSUTCDATETIME()) GROUP BY q.tenant, t.name ORDER BY COUNT(*) DESC, t.name",
            -dias,
        )
        organizaciones = []
        for f in tablas.filas(cur):
            organizaciones.append({
                "guid": f["tenant"], "nombre": f["nombre"], "preguntas": int(f["preguntas"]), "respondidas": int(f["respondidas"]),
                "noSePuede": int(f["noSePuede"]), "fallidas": int(f["fallidas"]), "topadas": int(f["topadas"]), "reusadas": int(f["reusadas"]),
                "utiles": int(f["utiles"]), "noUtiles": int(f["noUtiles"]), "personas": int(f["personas"]),
                "msPromedio": int(f["msPromedio"]) if f["msPromedio"] is not None else None,
                "tokensEntrada": int(f["tokensEntrada"]), "tokensSalida": int(f["tokensSalida"]),
                "costoUsd": round(float(f["costoUsd"] or 0), 6), "ultimaEn": momento(f["ultimaEn"]), "ejemplos": int(f["ejemplos"]),
            })
        cur.execute(
            "SELECT outcome, COUNT(*) AS n FROM nexus.searchQuestions WHERE createdAt >= DATEADD(day, ?, SYSUTCDATETIME()) GROUP BY outcome", -dias
        )
        por_resultado = {r: 0 for r in RESULTADOS}
        for f in tablas.filas(cur):
            por_resultado[f["outcome"]] = int(f["n"])
    suma = lambda llave: sum(o[llave] for o in organizaciones)  # noqa: E731
    respondidas_ms = [o for o in organizaciones if o["msPromedio"] is not None]
    totales = {
        "preguntas": suma("preguntas"), "respondidas": suma("respondidas"), "noSePuede": suma("noSePuede"), "fallidas": suma("fallidas"),
        "topadas": suma("topadas"), "reusadas": suma("reusadas"), "utiles": suma("utiles"), "noUtiles": suma("noUtiles"),
        "tokensEntrada": suma("tokensEntrada"), "tokensSalida": suma("tokensSalida"), "costoUsd": round(sum(o["costoUsd"] for o in organizaciones), 6),
        "msPromedio": (
            int(sum(o["msPromedio"] * (o["preguntas"] - o["topadas"]) for o in respondidas_ms) / max(1, sum(o["preguntas"] - o["topadas"] for o in respondidas_ms)))
            if respondidas_ms else None
        ),
    }
    return {
        "dias": dias,
        "organizaciones": organizaciones,
        "totales": totales,
        "porResultado": por_resultado,
        "topes": {"personaPorHora": config.BUSQUEDA_TOPE_PERSONA_POR_HORA, "organizacionPorDia": config.BUSQUEDA_TOPE_ORGANIZACION_POR_DIA},
        "precios": {
            "modelo": config.GEMINI_MODELO, "entradaUsdMillon": config.GEMINI_PRECIO_ENTRADA_USD_MILLON, "salidaUsdMillon": config.GEMINI_PRECIO_SALIDA_USD_MILLON,
            "embeddings": config.EMBEDDINGS_MODELO, "embeddingsUsdMillon": config.EMBEDDINGS_PRECIO_USD_MILLON,
        },
    }
