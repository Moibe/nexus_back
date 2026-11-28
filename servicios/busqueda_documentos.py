"""Buscar en el texto de los documentos: la consulta contra los fragmentos indexados (épica 14, HU-096, HU-097 y HU-098; 0022; la decisión, en
docs/adr-indice-semantico.md).

No es un chat ni redacta nada: devuelve los FRAGMENTOS más parecidos a la consulta, cada uno con su documento, su página, su parecido (score), el
pasaje con las palabras de la consulta resaltadas, la fecha de procesamiento, el expediente si el documento pertenece a uno y los datos extraídos
de esa página.

## Cómo

1. El vector de la consulta (`RETRIEVAL_QUERY`). Se recuerda `VIGENCIA_VECTOR_S` por consulta: pedir la página siguiente, ordenar por fecha o
   cambiar un filtro no lo vuelve a pedir (HU-097 CA-6, HU-098 CA-3).
2. La matriz de la organización: todos sus vectores vigentes, leídos de `consulta.vFragmento` con el usuario de solo lectura y la organización
   fijada en la conexión (HU-095: el filtro lo pone la base). Se guarda en memoria, UNA por organización, y se vuelve a leer sola cuando cambia
   el índice (cuántos fragmentos hay y el último).
3. Similitud coseno contra todos (numpy), filtros de tipo documental y fechas, el parecido mínimo de la organización (0.65 por omisión), y un
   resultado por documento, página y clase (los fragmentos de una página se enciman: no se repite la misma página). Orden por relevancia o por
   fecha de procesamiento, todo del lado del servidor.
4. De la página pedida se leen el texto, los expedientes y los datos extraídos (también por `[consulta]`).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np

from db import consulta
from servicios import auditoria, embeddings, fragmentos, indice_semantico

logger = logging.getLogger(__name__)

MAX_CONSULTA = 500
LIMITE_POR_OMISION = 5
MAX_LIMITE = 20
#: Cuántos resultados se ordenan y paginan como mucho (los de mayor parecido).
MAX_RESULTADOS = 200
MAX_TIPOS = 50
VIGENCIA_VECTOR_S = 1800
MAX_VECTORES_RECORDADOS = 256
ORDENES = ("relevancia", "fecha")
SIN_RESULTADOS = "Sin resultados relevantes para tu consulta"


@dataclass
class _Matriz:
    huella: tuple
    fragmentos: list[int] = field(default_factory=list)
    documentos: list[str] = field(default_factory=list)
    tipos: np.ndarray = field(default_factory=lambda: np.array([], dtype=object))
    fechas: np.ndarray = field(default_factory=lambda: np.array([], dtype="datetime64[s]"))
    clases: list[str] = field(default_factory=list)
    paginas: list[int | None] = field(default_factory=list)
    vectores: np.ndarray | None = None

    @property
    def n(self) -> int:
        return len(self.fragmentos)


_matrices: dict[str, _Matriz] = {}
_candados: dict[str, threading.Lock] = {}
_candado_general = threading.Lock()
_vectores: OrderedDict[tuple[str, str], tuple[np.ndarray, float]] = OrderedDict()
_candado_vectores = threading.Lock()


def _normal(texto: str) -> str:
    return " ".join(unicodedata.normalize("NFC", texto).lower().split())


def olvidar() -> None:
    """Vacía lo que se recuerda en memoria (las pruebas)."""
    with _candado_general:
        _matrices.clear()
    with _candado_vectores:
        _vectores.clear()


# ── El vector de la consulta ────────────────────────────────────────────────────────────────────────────────────────────


async def _vector(texto: str) -> tuple[np.ndarray, bool]:
    """El vector (normalizado) de la consulta y si ya se tenía."""
    llave = (embeddings.etiqueta(), _normal(texto))
    ahora = time.monotonic()
    with _candado_vectores:
        guardado = _vectores.get(llave)
        if guardado and ahora - guardado[1] < VIGENCIA_VECTOR_S:
            _vectores.move_to_end(llave)
            return guardado[0], True
    vectores, _ = await embeddings.vectores([texto], embeddings.TAREA_CONSULTA)
    v = np.asarray(vectores[0], dtype=np.float32)
    norma = float(np.linalg.norm(v))
    v = v / norma if norma else v
    with _candado_vectores:
        _vectores[llave] = (v, ahora)
        _vectores.move_to_end(llave)
        while len(_vectores) > MAX_VECTORES_RECORDADOS:
            _vectores.popitem(last=False)
    return v, False


# ── La matriz de la organización ────────────────────────────────────────────────────────────────────────────────────────


def _candado_de(tenant: str) -> threading.Lock:
    with _candado_general:
        return _candados.setdefault(tenant, threading.Lock())


def _matriz(tenant: str) -> _Matriz:
    etiqueta = embeddings.etiqueta()
    with _candado_de(tenant):
        con = consulta.abrir(tenant)
        try:
            cur = con.cursor()
            cur.execute("SELECT COUNT_BIG(*), MAX(fragmento) FROM consulta.vFragmento WHERE modelo = ?", etiqueta)
            huella = tuple(cur.fetchone())
            guardada = _matrices.get(tenant)
            if guardada is not None and guardada.huella == huella:
                return guardada
            cur.execute(
                "SELECT fragmento, documento, tipoDocumentalId, procesadoEn, clase, pagina, vector FROM consulta.vFragmento WHERE modelo = ? ORDER BY fragmento",
                etiqueta,
            )
            filas = cur.fetchall()
        finally:
            con.close()
        m = _Matriz(huella=huella)
        if filas:
            m.fragmentos = [int(f[0]) for f in filas]
            m.documentos = [f[1] for f in filas]
            m.tipos = np.array([f[2] for f in filas], dtype=object)
            m.fechas = np.array([np.datetime64(f[3], "s") for f in filas], dtype="datetime64[s]")
            m.clases = [f[4] for f in filas]
            m.paginas = [f[5] for f in filas]
            dims = len(filas[0][6]) // 4
            crudo = np.frombuffer(b"".join(bytes(f[6]) if len(f[6]) == dims * 4 else bytes(dims * 4) for f in filas), dtype="<f4").reshape(len(filas), dims)
            normas = np.linalg.norm(crudo, axis=1, keepdims=True)
            normas[normas == 0] = 1
            m.vectores = (crudo / normas).astype(np.float32)
        _matrices[tenant] = m
        logger.info("Búsqueda en documentos: matriz de %s con %s fragmentos (huella %s).", tenant, m.n, huella)
        return m


# ── Buscar ──────────────────────────────────────────────────────────────────────────────────────────────────────────────


def _validar(texto: Any, tipos: Any, desde: Any, hasta: Any, orden: Any, inicio: Any, limite: Any) -> tuple:
    if not isinstance(texto, str) or not texto.strip():
        raise ValueError("Escribe qué quieres buscar.")
    texto = " ".join(texto.split())
    if len(texto) > MAX_CONSULTA:
        raise ValueError(f"La consulta no puede pasar de {MAX_CONSULTA} caracteres.")
    tipos = [t for t in (tipos or []) if isinstance(t, str) and t.strip()]
    if len(tipos) > MAX_TIPOS:
        raise ValueError("Demasiados tipos documentales en el filtro.")
    if desde is not None and not isinstance(desde, date):
        raise ValueError("La fecha inicial no es válida.")
    if hasta is not None and not isinstance(hasta, date):
        raise ValueError("La fecha final no es válida.")
    if desde and hasta and desde > hasta:
        raise ValueError("La fecha inicial es posterior a la final.")
    if orden not in ORDENES:
        raise ValueError("El orden debe ser relevancia o fecha.")
    if not isinstance(inicio, int) or inicio < 0:
        raise ValueError("El inicio no es válido.")
    if not isinstance(limite, int) or not 1 <= limite <= MAX_LIMITE:
        raise ValueError(f"Se pueden pedir de 1 a {MAX_LIMITE} resultados.")
    return texto, tipos


def _marcadores(n: int) -> str:
    return ", ".join("?" * n)


def _enriquecer(tenant: str, elegidos: list[dict[str, Any]], texto: str) -> list[dict[str, Any]]:
    """El texto, los expedientes y los datos de la página de cada resultado de la página pedida."""
    if not elegidos:
        return []
    ids = [e["fragmento"] for e in elegidos]
    documentos = sorted({e["documento"] for e in elegidos})
    con = consulta.abrir(tenant)
    try:
        cur = con.cursor()
        cur.execute(
            f"SELECT fragmento, archivo, tipoDocumental, procesadoEn, texto FROM consulta.vFragmento WHERE fragmento IN ({_marcadores(len(ids))})", *ids
        )
        textos = {f[0]: f for f in cur.fetchall()}
        cur.execute(
            f"SELECT documento, expediente, nombreExpediente FROM consulta.vDocumentoDeExpediente WHERE documento IN ({_marcadores(len(documentos))}) "
            "ORDER BY nombreExpediente",
            *documentos,
        )
        expedientes: dict[str, list[dict[str, str]]] = {}
        for doc, exp, nombre in cur.fetchall():
            expedientes.setdefault(doc, []).append({"id": exp, "nombre": nombre})
        cur.execute(
            f"SELECT documento, tipoDocumentalId, campo, valor, confianza, pagina FROM consulta.vDatoExtraido WHERE documento IN ({_marcadores(len(documentos))}) "
            "AND valor IS NOT NULL AND pagina IS NOT NULL ORDER BY documento, pagina, campo",
            *documentos,
        )
        datos = cur.fetchall()
    finally:
        con.close()
    nombres = _nombres_de_tipos(tenant, {d[1] for d in datos if d[1]})
    campos: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for doc, tipo, ruta, valor, confianza, pagina in datos:
        campos.setdefault((doc, pagina), []).append(
            {
                "campo": ruta,
                "nombre": fragmentos._etiqueta_de_campo(ruta, nombres.get(tipo, {})),
                "valor": valor,
                "confianza": float(confianza) if confianza is not None else None,
            }
        )
    salida = []
    for e in elegidos:
        fila = textos.get(e["fragmento"])
        if fila is None:
            continue  # se re-extrajo entre la comparación y la lectura: ya no es vigente
        _, archivo, tipo, procesado, contenido = fila
        if e["clase"] == "campos" and "\n" in contenido:
            # El encabezado («INE · datos extraídos») sirve para el vector; en pantalla repite el tipo, que ya se enseña aparte.
            contenido = contenido.split("\n", 1)[1]
        p = fragmentos.pasaje(contenido, fragmentos.resaltar(contenido, texto))
        salida.append(
            {
                "fragmento": str(e["fragmento"]),
                "documento": e["documento"],
                "archivo": archivo,
                "tipoDocumentalId": e["tipo"],
                "tipoDocumental": tipo,
                "procesadoEn": procesado.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(procesado, datetime) else None,
                "pagina": e["pagina"],
                "clase": e["clase"],
                "score": round(e["score"], 4),
                "pasaje": p["texto"],
                "resaltado": p["resaltado"],
                "pasajeAlInicio": p["alInicio"],
                "pasajeAlFinal": p["alFinal"],
                "expedientes": expedientes.get(e["documento"], []),
                "campos": campos.get((e["documento"], e["pagina"]), []) if e["pagina"] is not None else [],
            }
        )
    return salida


def _nombres_de_tipos(tenant: str, tipos: set[str]) -> dict[str, dict[str, str]]:
    """Ruta en lo extraído → nombre en pantalla, por tipo documental de la organización."""
    if not tipos:
        return {}
    from db import tablas

    lista = sorted(tipos)
    with tablas.lectura() as cur:
        cur.execute(f"SELECT publicId, dataJson FROM nexus.documentTypes WHERE tenant = ? AND publicId IN ({_marcadores(len(lista))})", tenant, *lista)
        return {publico: indice_semantico._nombres_en_pantalla(datos) for publico, datos in cur.fetchall()}


def _comparar(tenant: str, q: np.ndarray, tipos: list[str], desde: date | None, hasta: date | None, orden: str) -> tuple[list[dict[str, Any]], float]:
    minimo = float(indice_semantico.ajustes(tenant)["minimo"])
    m = _matriz(tenant)
    if m.n == 0 or m.vectores is None or m.vectores.shape[1] != q.shape[0]:
        return [], minimo
    mascara = np.ones(m.n, dtype=bool)
    if tipos:
        mascara &= np.isin(m.tipos, np.array(tipos, dtype=object))
    if desde:
        mascara &= m.fechas >= np.datetime64(desde, "s")
    if hasta:
        mascara &= m.fechas < np.datetime64(hasta + timedelta(days=1), "s")
    parecidos = m.vectores @ q
    candidatos = np.nonzero(mascara & (parecidos >= minimo))[0]
    candidatos = candidatos[np.argsort(-parecidos[candidatos], kind="stable")]
    vistos: set[tuple] = set()
    lista: list[dict[str, Any]] = []
    for i in candidatos:
        llave = (m.documentos[i], m.paginas[i], m.clases[i])
        if llave in vistos:
            continue
        vistos.add(llave)
        lista.append(
            {
                "fragmento": m.fragmentos[i], "documento": m.documentos[i], "tipo": m.tipos[i], "fecha": m.fechas[i],
                "clase": m.clases[i], "pagina": m.paginas[i], "score": float(parecidos[i]),
            }
        )
        if len(lista) >= MAX_RESULTADOS:
            break
    if orden == "fecha":
        lista.sort(key=lambda r: (r["fecha"], r["score"]), reverse=True)
    return lista, minimo


async def buscar(
    tenant: str,
    usuario: str | None,
    texto: Any,
    tipos: Any = None,
    desde: Any = None,
    hasta: Any = None,
    orden: Any = "relevancia",
    inicio: Any = 0,
    limite: Any = LIMITE_POR_OMISION,
    sesion: str | None = None,
) -> dict[str, Any]:
    """Los fragmentos más parecidos a `texto` (ver el encabezado). Levanta `ValueError` (entrada inválida), `embeddings.SinEmbeddings` (sin
    vector de la consulta) y los errores de `db.consulta` (la conexión de solo lectura)."""
    texto, tipos = _validar(texto, tipos, desde, hasta, orden, inicio, limite)
    t0 = time.monotonic()
    q, reusado = await _vector(texto)

    def sincrono() -> dict[str, Any]:
        lista, minimo = _comparar(tenant, q, tipos, desde, hasta, orden)
        resultados = _enriquecer(tenant, lista[inicio : inicio + limite], texto)
        return {"lista": lista, "minimo": minimo, "resultados": resultados}

    r = await asyncio.to_thread(sincrono)
    total = len(r["lista"])
    respuesta = {
        "resultados": r["resultados"],
        "total": total,
        "inicio": inicio,
        "limite": limite,
        "hayMas": inicio + limite < total,
        "orden": orden,
        "minimo": r["minimo"],
        "vectorReusado": reusado,
        "milisegundos": int((time.monotonic() - t0) * 1000),
        "mensaje": None if total else SIN_RESULTADOS,
    }
    try:
        auditoria.registrar(
            auditoria.SEMANTIC_SEARCH, usuario_guid=usuario, sesionGuid=sesion, tenant=tenant, consulta=texto[:300],
            filtros={"tipos": tipos, "desde": desde.isoformat() if desde else None, "hasta": hasta.isoformat() if hasta else None},
            orden=orden, inicio=inicio, total=total, milisegundos=respuesta["milisegundos"], vectorReusado=reusado,
        )
    except Exception as exc:  # noqa: BLE001 — el rastro nunca cambia la respuesta
        logger.warning("No se pudo anotar SEMANTIC_SEARCH en la bitácora de auditoría: %s", exc)
    return respuesta
