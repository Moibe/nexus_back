"""Uso de las API Keys de cliente: cada llamada, y las métricas que salen de
ahí. Es lo que alimenta "Métricas" en el módulo API Key del front, y el tope
semanal por llave.

## Qué se registra

UNA línea por petición que trajo una llave de cliente QUE EXISTE: cuándo, qué
llave (su identificador público, nunca el secret), qué ruta, con qué código
respondió, cuánto tardó y cuántos bytes traía. Se registra desde el middleware
de `app.py`, y entran también las rechazadas —401 por llave revocada o vencida,
429—: una métrica de errores que no cuenta los rechazos no sirve para nada, y
a quien tiene una llave revocada que sigue llamando hay que podérselo mostrar.

NO se anota un identificador que NO EXISTE, y esa es la diferencia con cómo
nació esto (corregido el 2026-10-01). Antes se anotaba cualquier cosa con forma
de llave: una inundación de identificadores inventados hacía crecer sin tope el
índice en memoria y este archivo del NAS, dos recursos del servidor, sin que
hubiera jamás un cliente detrás. Mientras el puerto 8083 solo se alcanzaba
desde la intranet era una rareza; con la API publicada en internet, no.

Cada llamada es una fila de `nexus.apiKeyUsage` (migración `0008`, con llave foránea a la llave), importada una vez desde el viejo
`uso-llaves.jsonl`; desde el 2026-11-11 no hay otro almacenamiento. El índice en memoria de abajo se calienta de la tabla y es lo que contesta
el tope y las métricas.

## El tope semanal

`LIMITE_SEMANAL` solicitudes por llave por semana natural (lunes a domingo,
UTC). Al llegar, la API responde 429 a esa llave hasta el lunes. Cuentan todas
las peticiones de esa llave, aceptadas o no: si no, una llave revocada que
sigue llamando martillaría sin límite. Está implementado a pedido explícito
(2026-09-30) aunque hoy nadie se acerque: el aviso amarillo del front sale
desde `AVISO_DESDE`.
"""

import logging
import statistics
import threading
from datetime import datetime, timedelta, timezone

from servicios import integraciones_sql

logger = logging.getLogger(__name__)
LIMITE_SEMANAL = 500_000
AVISO_DESDE = 0.80


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


# ── El índice por llave, en memoria ──────────────────────────────────────────
#
# El uso se consulta en CADA petición con llave de cliente (para el tope)
# y cada vez que se abren las métricas, y releer el archivo completo escala
# mal: con 400,000 líneas eran casi 3 segundos por llamada (medido). Así que los
# eventos viven en memoria agrupados por llave: se calientan leyendo el archivo
# UNA vez, y de ahí cada `registrar` agrega el suyo. Es exacto mientras haya un
# solo proceso (que es el caso: un uvicorn sin workers, ver README). Si el
# uso no se pudo leer al calentar, se arranca vacío y se vuelve a intentar
# en la siguiente llamada — mejor subestimar el consumo que bloquear a alguien
# por un fallo de lectura.
#
# De cada evento se guardan solo su fecha (ya parseada), status, ms y bytes:
# lo que usan el tope y las métricas. Un año de uso intenso (millones de
# llamadas) cabría en unos cientos de MB; para entonces esto ya debe ser la
# tabla del DBA.
_Evento = tuple[datetime, int, int, int]  # (fecha, status, ms, bytes)
_indice: dict[str, list[_Evento]] = {}
_indice_listo = False
_indice_candado = threading.Lock()


def _calentar() -> None:
    """Carga el índice del archivo si todavía no está. Bajo `_indice_candado`."""
    global _indice_listo
    if _indice_listo:
        return
    nuevo = integraciones_sql.uso_cargar()
    _indice.clear()
    _indice.update(nuevo)
    _indice_listo = True


def calentar() -> None:
    """Carga el índice si no está. La llama `app.py` al arrancar."""
    with _indice_candado:
        _calentar()


def _eventos_de(llave_id: str) -> list[_Evento]:
    with _indice_candado:
        _calentar()
        return list(_indice.get(llave_id, ()))


def registrar(llave_id: str, ruta: str, metodo: str, status: int, ms: int, bytes_: int) -> None:
    """Anota una llamada. NUNCA levanta: registrar uso no puede tumbar la
    petición que lo generó — si la base falla, se pierde esta línea y ya."""
    ahora = _ahora()
    with _indice_candado:
        try:
            _calentar()
        except Exception:  # noqa: BLE001
            logger.exception("No se pudo calentar el índice de uso")
        _indice.setdefault(llave_id, []).append((ahora, status, ms, bytes_))
    try:
        integraciones_sql.uso_registrar(llave_id, ruta, metodo, status, ms, bytes_, ahora)
    except Exception:  # noqa: BLE001
        logger.exception("No se pudo registrar el uso de la llave %s", llave_id)


def inicio_de_semana(momento: datetime) -> datetime:
    lunes = momento.date() - timedelta(days=momento.weekday())
    return datetime(lunes.year, lunes.month, lunes.day, tzinfo=timezone.utc)


def consumo_semanal(llave_id: str, momento: datetime | None = None) -> int:
    """Cuántas llamadas lleva la llave esta semana (lunes a domingo, UTC)."""
    momento = momento or _ahora()
    desde = inicio_de_semana(momento)
    return sum(1 for f, _, _, _ in _eventos_de(llave_id) if f >= desde)


def excede_limite(llave_id: str) -> bool:
    return consumo_semanal(llave_id) >= LIMITE_SEMANAL


def _resumen(eventos: list[_Evento]) -> dict:
    total = len(eventos)
    exitosas = sum(1 for _, st, _, _ in eventos if 200 <= st < 300)
    errores = total - exitosas
    latencias = sorted(ms for _, _, ms, _ in eventos)
    p50 = int(statistics.median(latencias)) if latencias else None
    return {
        "solicitudes": total,
        "exitosas": exitosas,
        "errores": errores,
        "porcentajeExito": round(exitosas / total * 100, 1) if total else None,
        "latenciaP50Ms": p50,
        "bytes": sum(b for _, _, _, b in eventos),
    }


def metricas(llave_id: str, desde: datetime, hasta: datetime) -> dict:
    """Las cifras del periodo [desde, hasta), comparadas con el periodo
    inmediato anterior de la misma duración — que es lo que el diseño llama
    "vs. semana anterior". Más el consumo de la semana en curso contra el
    tope, y el último uso de la llave.

    El periodo llega como DOS INSTANTES con su zona, no como fechas, y es
    semiabierto: incluye `desde` y excluye `hasta`. Es lo que evita el error de
    día: el registro guarda UTC, pero "hoy" para quien mira es el día de SU
    zona. Con fechas leídas como días UTC, todo lo que entraba después de las
    18:00 en México (que ya es mañana en UTC) quedaba fuera de "hoy". Quien
    llama manda la medianoche local de cada extremo (p. ej.
    `2026-09-30T00:00:00-06:00` a `2026-10-01T00:00:00-06:00` para el 30 de
    septiembre), y así cada extremo lleva su propio desfase, también si el
    periodo cruza un cambio de horario.

    `porDia` agrupa por día EN LA ZONA de `desde` (su desfase), que es el día
    que ve quien consulta."""
    if desde.tzinfo is None or hasta.tzinfo is None:
        raise ValueError("El periodo debe traer zona horaria (por ejemplo 2026-09-30T00:00:00-06:00 o ...Z).")
    if hasta <= desde:
        raise ValueError("El fin del periodo es anterior o igual al inicio.")
    try:
        ini = desde.astimezone(timezone.utc)
        fin = hasta.astimezone(timezone.utc)
        duracion = fin - ini
        inicio_anterior = ini - duracion
    except OverflowError as exc:
        raise ValueError("El periodo queda fuera de las fechas que se pueden calcular.") from exc
    todos = _eventos_de(llave_id)
    actual = [e for e in todos if ini <= e[0] < fin]
    anterior = [e for e in todos if inicio_anterior <= e[0] < ini]
    ahora = _ahora()
    semana = inicio_de_semana(ahora)
    consumo = sum(1 for e in todos if e[0] >= semana)
    ultimo = max((e[0] for e in todos), default=None)
    desfase = desde.utcoffset() or timedelta(0)
    por_dia: dict[str, int] = {}
    for e in actual:
        clave = (e[0] + desfase).date().isoformat()
        por_dia[clave] = por_dia.get(clave, 0) + 1
    return {
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "actual": _resumen(actual),
        "anterior": _resumen(anterior),
        "porDia": [{"dia": d, "solicitudes": n} for d, n in sorted(por_dia.items())],
        "limiteSemanal": {
            "consumo": consumo,
            "limite": LIMITE_SEMANAL,
            "fraccion": round(consumo / LIMITE_SEMANAL, 4),
            "avisoDesde": AVISO_DESDE,
            "semanaDesde": semana.date().isoformat(),
        },
        "ultimoUso": ultimo.isoformat(timespec="seconds") if ultimo else None,
    }


def ultimos_usos(llave_ids: list[str]) -> dict[str, str | None]:
    """El último uso de varias llaves de una vez, para el listado."""
    salida: dict[str, str | None] = {}
    for k in llave_ids:
        ultimo = max((e[0] for e in _eventos_de(k)), default=None)
        salida[k] = ultimo.isoformat(timespec="seconds") if ultimo else None
    return salida
