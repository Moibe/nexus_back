"""Los avisos de eventos a los webhooks de cliente: recibirlos, entregarlos,
reintentarlos y medirlos.

## De dónde sale un aviso

El pipeline corre en el NAVEGADOR, así que el servidor no se entera solo de que
un documento terminó: el front se lo dice (`POST /webhooks/eventos`, que llama
a `avisar`). Solo de documentos que llegaron por la API: son los únicos cuyo id
conoce el cliente (es el `id` que recibió al subirlo).

Como quien avisa es el front, y el front hoy se alcanza desde internet sin
iniciar sesión, NO se le cree a ciegas:

- la entrada tiene que existir en la bandeja de ESE cliente y haber salido de
  ella porque pasó al pipeline (`bandeja.entrada_en_pipeline`);
- el tipo y el motivo vienen de listas cerradas; el resto del cuerpo lo arma el
  servidor;
- por entrada se acepta UN resultado final (completado o rechazado), y repetir
  el mismo aviso no programa nada de nuevo. Un "fallido" no es final: un
  reintento futuro podría terminar bien.

Con eso, alguien que use el front para mandar avisos falsos no puede inventar
documentos ni multiplicar envíos: a lo sumo adelantar el resultado de una
entrada real. Cerrar eso del todo es cerrar el front (ver README).

## La entrega y sus reintentos

Se programa UNA entrega por cada webhook del cliente que esté validado, activo
y suscrito a ese tipo. Cada una tiene un id de mensaje fijo (`msg_...`) que viaja
igual en todos sus intentos en `webhook-id`: es lo que Standard Webhooks usa
para que el cliente no procese dos veces el mismo aviso. El cuerpo también es
el mismo en todos; la marca de tiempo y la firma son de cada intento.

Si el endpoint no responde 2xx se reintenta según `CALENDARIO_S`: al momento,
5 s, 5 min, 30 min, 2 h y 5 h (seis intentos en unas 7.6 horas; el calendario de
Standard Webhooks, recortado). Agotados, la entrega queda `agotada`. Si el
webhook se desactiva o se elimina, sus entregas pendientes se `cancelan`: nadie
pidió que le llegara algo a un endpoint pausado.

## El trabajador

Un hilo que arranca con la app (`iniciar`, desde `app.py`) y cada segundo
intenta las entregas a las que ya les tocó, de una en una. Las pendientes viven
en memoria —es un solo proceso uvicorn—; el registro es lo durable: al arrancar
se reconstruyen de ahí, así que un reinicio no pierde avisos, a lo sumo los
retrasa. Con muchos clientes y endpoints lentos, de una en una se quedaría
corto: para entonces, un pool de hilos y la tabla del DBA.

## Registro

`entregas-webhooks.jsonl` en el NAS, de solo agregar: `aviso` (lo que avisó el
front), `programada` (con el cuerpo ya armado), `intento` (cuándo, ok, código,
ms, motivo), `agotada` y `cancelada`. Las métricas salen de los `intento` de los
avisos; el historial de intentos, de todos —también de los de validar—.

## Validar la conexión, con reintentos

`validar` manda el aviso de prueba hasta `INTENTOS_VALIDACION` (5) veces, con
esperas cortas entre ellos (1, 2, 4 y 8 s: quien validó está mirando la
pantalla), el mismo `webhook-id` en todos, y se detiene en el primer 2xx. No
reintenta lo `definitivo` (dirección interna, no https): el resultado no puede
cambiar. Cada intento se anota, y es lo que muestra el historial.

Lo peor —cinco tiempos agotados— tarda ~65 s y ocupa un hilo todo ese rato.
Como el front se alcanza desde internet, se acotan: 3 validaciones a la vez en
total, y una sola por webhook. Se
lee completo en cada aviso y en cada consulta de métricas: hoy son decenas de
líneas al día; con miles, índice en memoria como el de `uso_llaves`, o la tabla.
"""

import json
import logging
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from servicios import bandeja, entrega_webhooks, registro, webhooks_cliente
from servicios.almacen import ErrorAlmacen

logger = logging.getLogger(__name__)

ARCHIVO = "entregas-webhooks.jsonl"

# Segundos antes de cada intento: el primero al momento y los demás contados
# desde el intento anterior.
CALENDARIO_S = (0, 5, 5 * 60, 30 * 60, 2 * 3600, 5 * 3600)

# Lo que el front puede avisar, y con qué motivos. `expediente.completado` no
# está: el expediente todavía no existe.
MOTIVOS = {
    "documento.completado": frozenset(),
    "documento.rechazado": frozenset({"formato_no_soportado", "documento_no_reconocido", "tipo_no_identificado"}),
    "documento.fallido": frozenset({"error_del_servicio", "tipo_sin_configurar"}),
}
FINALES = frozenset({"documento.completado", "documento.rechazado"})

TIPO_VALIDACION = "webhook.validacion"
# Esperas ENTRE intentos al validar: con cuatro esperas, cinco intentos.
ESPERAS_VALIDACION_S = (1, 2, 4, 8)
INTENTOS_VALIDACION = len(ESPERAS_VALIDACION_S) + 1
MAX_VALIDACIONES_SIMULTANEAS = 3
_LARGO_TIPO_DOCUMENTAL = 100


def _ahora() -> datetime:
    return datetime.now(timezone.utc)


def _iso(momento: datetime) -> str:
    return momento.isoformat(timespec="seconds")


def _fecha(valor) -> datetime | None:
    try:
        f = datetime.fromisoformat(valor)
        return f if f.tzinfo else f.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


class ValidacionOcupada(Exception):
    """Ya hay una validación de ese webhook en curso, o demasiadas en total."""


class AvisoRechazado(Exception):
    """El aviso no se acepta. `codigo` es el HTTP con el que se contesta."""

    def __init__(self, mensaje: str, codigo: int):
        super().__init__(mensaje)
        self.codigo = codigo


@dataclass
class _Pendiente:
    id: str
    webhook: str
    tenant: str
    tipo: str
    cuerpo: bytes
    intentos: int
    proximo: datetime


_pendientes: dict[str, _Pendiente] = {}
_candado = threading.Lock()  # solo para `_pendientes`; nunca se tiene puesto al escribir el registro
_despertar = threading.Event()
_detener = threading.Event()
_hilo: threading.Thread | None = None
_cargado = False

_validando: set[str] = set()
_cupo_validacion = threading.BoundedSemaphore(MAX_VALIDACIONES_SIMULTANEAS)
_dormir = time.sleep  # las pruebas lo cambian por uno que no espera


# ── Recibir un aviso ────────────────────────────────────────────────────────


def avisar(tenant: str, tipo: str, entrada_id: str, tipo_documental: str | None, motivo: str | None) -> dict:
    """Acepta el aviso de que un documento terminó y programa una entrega por
    cada webhook que deba recibirlo. Devuelve `{"programadas": n, "duplicado": bool}`.

    Levanta `AvisoRechazado` (con su código HTTP) si no se acepta, `ValueError`
    con un tenant inválido y `ErrorAlmacen` si el registro no está."""
    if tipo == "expediente.completado":
        raise AvisoRechazado("Los expedientes todavía no existen: ese aviso no se puede emitir.", 400)
    if tipo not in MOTIVOS:
        raise AvisoRechazado(f"Tipo de aviso desconocido: {tipo!r}.", 400)
    permitidos = MOTIVOS[tipo]
    if permitidos and motivo not in permitidos:
        raise AvisoRechazado(f"Motivo inválido para {tipo}: {motivo!r}.", 400)
    if not permitidos and motivo:
        raise AvisoRechazado("Un documento completado no lleva motivo.", 400)
    tipo_documental = (tipo_documental or "").strip()[:_LARGO_TIPO_DOCUMENTAL] or None

    entrada = bandeja.entrada_en_pipeline(tenant, entrada_id)
    if entrada is None:
        raise AvisoRechazado("Esa entrada no existe, es de otro cliente o no pasó al pipeline.", 404)

    with registro.candado:
        anteriores = [
            e
            for e in registro.eventos(ARCHIVO)
            if e.get("evento") == "aviso" and e.get("tenant") == tenant and e.get("entradaId") == entrada_id
        ]
        if any(e.get("tipo") == tipo for e in anteriores):
            return {"programadas": 0, "duplicado": True}
        if any(e.get("tipo") in FINALES for e in anteriores):
            raise AvisoRechazado("Ya se avisó el resultado final de esa entrada.", 409)

        ahora = _ahora()
        cuerpo = entrega_webhooks.armar_cuerpo(
            tipo,
            {
                "entradaId": entrada_id,
                "estado": tipo.split(".", 1)[1],
                "motivo": motivo,
                "tipoDocumental": tipo_documental,
                "recibidoEn": entrada.get("recibidoEn"),
                "terminadoEn": _iso(ahora),
            },
            ahora,
        )
        destinos = webhooks_cliente.suscritos(tenant, tipo)
        registro.agregar(
            ARCHIVO,
            {"evento": "aviso", "tenant": tenant, "entradaId": entrada_id, "tipo": tipo, "en": _iso(ahora), "destinos": len(destinos)},
        )
        nuevas = []
        for webhook in destinos:
            p = _Pendiente(entrega_webhooks.nuevo_id_mensaje(), webhook, tenant, tipo, cuerpo, 0, ahora)
            registro.agregar(
                ARCHIVO,
                {"evento": "programada", "id": p.id, "webhook": webhook, "tenant": tenant, "tipo": tipo,
                 "cuerpo": cuerpo.decode(), "en": _iso(ahora)},
            )
            nuevas.append(p)
    with _candado:
        for p in nuevas:
            _pendientes[p.id] = p
    _despertar.set()
    return {"programadas": len(nuevas), "duplicado": False}


# ── Entregar ────────────────────────────────────────────────────────────────


def _cargar() -> None:
    """Reconstruye las pendientes del registro, una sola vez. Si el almacén no
    está, se deja para la siguiente vuelta: no se pierde nada, solo se espera."""
    global _cargado
    if _cargado:
        return
    try:
        eventos = registro.eventos(ARCHIVO)
    except (ErrorAlmacen, ValueError):
        logger.warning("No se pudo leer el registro de entregas de webhooks; se reintenta en la siguiente vuelta.")
        return
    programadas: dict[str, dict] = {}
    intentos: dict[str, list[dict]] = {}
    cerradas: set[str] = set()
    for e in eventos:
        i = e.get("id")
        if not isinstance(i, str):
            continue
        tipo = e.get("evento")
        if tipo == "programada":
            programadas.setdefault(i, e)
        elif tipo == "intento":
            intentos.setdefault(i, []).append(e)
        elif tipo in ("agotada", "cancelada"):
            cerradas.add(i)
    recuperadas = {}
    for i, e in programadas.items():
        hechos = intentos.get(i, [])
        if i in cerradas or any(x.get("ok") for x in hechos) or len(hechos) >= len(CALENDARIO_S):
            continue
        creada = _fecha(e.get("en")) or _ahora()
        ultimo = max((f for f in (_fecha(x.get("en")) for x in hechos) if f), default=None)
        proximo = ultimo + timedelta(seconds=CALENDARIO_S[len(hechos)]) if ultimo else creada
        recuperadas[i] = _Pendiente(
            i, str(e.get("webhook")), str(e.get("tenant")), str(e.get("tipo")), str(e.get("cuerpo", "")).encode(), len(hechos), proximo
        )
    with _candado:
        for i, p in recuperadas.items():
            _pendientes.setdefault(i, p)
    _cargado = True
    if recuperadas:
        logger.info("Entregas de webhooks pendientes recuperadas del registro: %s", len(recuperadas))


def _anotar(evento: dict) -> None:
    try:
        registro.agregar(ARCHIVO, evento)
    except ErrorAlmacen:
        # El intento SÍ ocurrió; lo que se pierde es su línea en el registro (y
        # en las métricas). No se detiene la entrega por eso.
        logger.exception("No se pudo anotar en el registro de entregas: %s", evento.get("evento"))


def _cerrar(p: _Pendiente, como: str, motivo: str) -> None:
    with _candado:
        _pendientes.pop(p.id, None)
    _anotar({"evento": como, "id": p.id, "webhook": p.webhook, "en": _iso(_ahora()), "motivo": motivo})


def _intentar(p: _Pendiente) -> None:
    try:
        destino = webhooks_cliente.para_entregar(p.webhook)
    except ErrorAlmacen:
        logger.warning("Registro de webhooks no disponible; la entrega %s espera a la siguiente vuelta.", p.id)
        return
    except webhooks_cliente.SinCifrado as exc:
        # Sin cómo firmar no se manda nada: cuenta como intento fallido y se
        # reintenta, por si la llave se corrige.
        destino, falla = None, str(exc)
    else:
        falla = None
        if destino is None:
            _cerrar(p, "cancelada", "El webhook se desactivó, se eliminó o ya no está validado.")
            return

    n = p.intentos + 1
    codigo = ms = None
    if destino is None:
        ok, motivo = False, falla
    else:
        url, secret = destino
        try:
            r = entrega_webhooks.enviar(url, secret, p.cuerpo, p.id)
            ok, codigo, ms, motivo = True, r["codigo"], r["ms"], None
        except entrega_webhooks.Rechazo as exc:
            ok, codigo, ms, motivo = False, exc.codigo, exc.ms, str(exc)
    en = _ahora()
    _anotar({"evento": "intento", "id": p.id, "webhook": p.webhook, "tenant": p.tenant, "tipo": p.tipo, "n": n,
             "en": _iso(en), "ok": ok, "codigo": codigo, "ms": ms, "motivo": motivo})
    if ok:
        with _candado:
            _pendientes.pop(p.id, None)
    elif n >= len(CALENDARIO_S):
        logger.warning("Entrega %s agotada tras %s intentos (webhook %s): %s", p.id, n, p.webhook, motivo)
        _cerrar(p, "agotada", motivo or "")
    else:
        with _candado:
            p.intentos = n
            p.proximo = en + timedelta(seconds=CALENDARIO_S[n])


def procesar_vencidas() -> int:
    """Intenta las entregas a las que ya les tocó. Devuelve cuántas intentó.

    La llama el trabajador cada segundo; las pruebas la llaman directo, con el
    reloj (`_ahora`) que necesiten."""
    _cargar()
    ahora = _ahora()
    with _candado:
        vencidas = sorted((p for p in _pendientes.values() if p.proximo <= ahora), key=lambda p: p.proximo)
    for p in vencidas:
        _intentar(p)
    return len(vencidas)


def cancelar_de(webhook: str, motivo: str) -> int:
    """Cancela las entregas pendientes de un webhook (se desactivó o se
    eliminó). Devuelve cuántas."""
    with _candado:
        suyas = [p for p in _pendientes.values() if p.webhook == webhook]
    for p in suyas:
        _cerrar(p, "cancelada", motivo)
    return len(suyas)


def pendientes() -> int:
    with _candado:
        return len(_pendientes)


def _bucle() -> None:
    while not _detener.is_set():
        try:
            procesar_vencidas()
        except Exception:  # noqa: BLE001 — una vuelta que falla no debe matar al trabajador
            logger.exception("Falló una vuelta del trabajador de entregas de webhooks")
        _despertar.wait(timeout=1.0)
        _despertar.clear()


def iniciar() -> None:
    """Arranca el trabajador. La llama `app.py` al arrancar."""
    global _hilo
    if _hilo is not None and _hilo.is_alive():
        return
    _detener.clear()
    _hilo = threading.Thread(target=_bucle, name="entregas-webhooks", daemon=True)
    _hilo.start()


def detener() -> None:
    """Lo detiene. Una entrega a medias termina su intento (hasta el tiempo
    límite); lo que quede pendiente se recupera del registro al volver."""
    _detener.set()
    _despertar.set()
    if _hilo is not None:
        _hilo.join(timeout=15)


# ── Métricas ────────────────────────────────────────────────────────────────


def _percentil(ordenados: list[int], p: int) -> int | None:
    """Rango más cercano: el valor en la posición ⌈p/100 · n⌉. Sin interpolar,
    así siempre es una latencia que de verdad ocurrió."""
    if not ordenados:
        return None
    return ordenados[max(1, math.ceil(p / 100 * len(ordenados))) - 1]


def _resumen(intentos: list[dict]) -> dict:
    total = len(intentos)
    errores = sum(1 for e in intentos if not e.get("ok"))
    # Solo los intentos que llegaron a la red tienen ms (la guarda y un nombre
    # que no resuelve fallan sin conectar): esos no dicen nada de la latencia.
    latencias = sorted(int(e["ms"]) for e in intentos if isinstance(e.get("ms"), (int, float)))
    return {
        "solicitudes": total,
        "errores": errores,
        "tasaError": round(errores / total * 100, 1) if total else None,
        "p50Ms": _percentil(latencias, 50),
        "p90Ms": _percentil(latencias, 90),
        "p99Ms": _percentil(latencias, 99),
    }


def metricas(tenant: str, webhook: str, desde: datetime, hasta: datetime) -> dict:
    """Las entregas de un webhook en `[desde, hasta)` y en el periodo anterior
    de la misma duración. Cada INTENTO de un aviso cuenta como una solicitud:
    uno que se reintentó tres veces son tres llamadas a su endpoint. Los de
    validar NO cuentan: son pruebas, no avisos (están en el historial). El periodo, como
    en las métricas de las llaves: dos instantes con su zona."""
    if desde.tzinfo is None or hasta.tzinfo is None:
        raise ValueError("El periodo debe traer zona horaria (por ejemplo 2026-09-30T00:00:00-06:00 o ...Z).")
    if hasta <= desde:
        raise ValueError("El fin del periodo es anterior o igual al inicio.")
    try:
        ini = desde.astimezone(timezone.utc)
        fin = hasta.astimezone(timezone.utc)
        inicio_anterior = ini - (fin - ini)
    except OverflowError as exc:
        raise ValueError("El periodo queda fuera de las fechas que se pueden calcular.") from exc
    suyos = []
    for e in registro.eventos(ARCHIVO):
        if (
            e.get("evento") == "intento"
            and e.get("webhook") == webhook
            and e.get("tenant") == tenant
            and e.get("tipo") != TIPO_VALIDACION
        ):
            f = _fecha(e.get("en"))
            if f:
                suyos.append((f, e))
    return {
        "desde": desde.isoformat(),
        "hasta": hasta.isoformat(),
        "actual": _resumen([e for f, e in suyos if ini <= f < fin]),
        "anterior": _resumen([e for f, e in suyos if inicio_anterior <= f < ini]),
    }


# ── Validar la conexión ─────────────────────────────────────────────────────


def validar(tenant: str, webhook: str, url: str, secret: str) -> dict:
    """Manda el aviso de prueba hasta `INTENTOS_VALIDACION` veces y se detiene en
    el primer 2xx. Devuelve `{"ok": True, "codigo", "ms", "intentos"}` o
    `{"ok": False, "motivo", "codigo", "intentos"}` (motivo y código del último
    intento). Levanta `ValidacionOcupada` si ese webhook ya se está validando o
    hay demasiadas validaciones en curso."""
    with _candado:
        if webhook in _validando:
            raise ValidacionOcupada("Este webhook ya se está validando. Espera a que termine.")
        if not _cupo_validacion.acquire(blocking=False):
            raise ValidacionOcupada("Hay otras validaciones en curso. Intenta de nuevo en un momento.")
        _validando.add(webhook)
    try:
        cuerpo = entrega_webhooks.armar_cuerpo(
            TIPO_VALIDACION,
            {
                "webhookId": webhook,
                "mensaje": "Aviso de prueba de NexusDoc. Responde con un código 2xx para validar este webhook.",
            },
        )
        id_mensaje = entrega_webhooks.nuevo_id_mensaje()
        ultimo: entrega_webhooks.Rechazo | None = None
        n = 0
        for n in range(1, INTENTOS_VALIDACION + 1):
            try:
                r = entrega_webhooks.enviar(url, secret, cuerpo, id_mensaje)
            except entrega_webhooks.Rechazo as exc:
                ultimo = exc
                _anotar_intento(id_mensaje, webhook, tenant, TIPO_VALIDACION, n, False, exc.codigo, exc.ms, str(exc))
                if exc.definitivo or n == INTENTOS_VALIDACION:
                    break
                _dormir(ESPERAS_VALIDACION_S[n - 1])
                continue
            _anotar_intento(id_mensaje, webhook, tenant, TIPO_VALIDACION, n, True, r["codigo"], r["ms"], None)
            return {"ok": True, "codigo": r["codigo"], "ms": r["ms"], "intentos": n}
        return {"ok": False, "motivo": str(ultimo), "codigo": ultimo.codigo if ultimo else None, "intentos": n}
    finally:
        with _candado:
            _validando.discard(webhook)
        _cupo_validacion.release()


def _anotar_intento(id_mensaje, webhook, tenant, tipo, n, ok, codigo, ms, motivo) -> None:
    _anotar({"evento": "intento", "id": id_mensaje, "webhook": webhook, "tenant": tenant, "tipo": tipo, "n": n,
             "en": _iso(_ahora()), "ok": ok, "codigo": codigo, "ms": ms, "motivo": motivo})


# ── El historial de intentos ────────────────────────────────────────────────


def historial(tenant: str, webhook: str, limite: int = 50) -> list[dict]:
    """Cada vez que NexusDoc le habló al endpoint de un webhook —las
    validaciones y las entregas de avisos—, del más reciente al más viejo. De
    las entregas trae además el `entradaId` del documento que avisaban."""
    eventos = registro.eventos(ARCHIVO)
    entradas: dict[str, str | None] = {}
    for e in eventos:
        if e.get("evento") == "programada" and e.get("webhook") == webhook:
            try:
                entradas[e["id"]] = json.loads(e.get("cuerpo") or "{}").get("data", {}).get("entradaId")
            except (TypeError, ValueError, AttributeError, KeyError):
                continue
    intentos = [
        e for e in eventos if e.get("evento") == "intento" and e.get("webhook") == webhook and e.get("tenant") == tenant
    ]
    intentos.reverse()
    return [
        {
            "en": e.get("en"),
            "tipo": e.get("tipo"),
            "n": e.get("n"),
            "ok": bool(e.get("ok")),
            "codigo": e.get("codigo"),
            "ms": e.get("ms"),
            "motivo": e.get("motivo"),
            "mensajeId": e.get("id"),
            "entradaId": entradas.get(e.get("id")),
        }
        for e in intentos[:limite]
    ]
