"""Qué se puede usar por el dominio público de la API.

El dominio de los clientes (`nexus-doc-api.buzzword.com.mx`) lo atiende un
reverse proxy de Soporte TI que reenvía a este servicio. Se pidió que
reenviara SOLO `/bandeja/`, `/docs` y `/openapi.json`, pero el 2026-10-01, al
estrenarlo, se comprobó que reenvía TODO: la documentación interna con su
esquema completo, y `/health/db`, que sin llave devuelve la versión exacta de
SQL Server y el nombre de la base. Las rutas administrativas sí pedían la llave
de servicio (respondían 401), pero no tenían por qué poder alcanzarse ni verse.

Esta guarda lo cierra DESDE ESTE LADO, sin depender de cómo esté armado el
proxy ni de que alguien lo vuelva a tocar: si la petición llega por un nombre
público, solo pasan las tres rutas de `RUTAS_PUBLICAS` y todo lo demás
responde 404, incluso con la llave de servicio. La llamada interna del front
(por IP o localhost) no cambia. Los nombres públicos salen de
`NEXUS_HOSTS_PUBLICOS` (ver config.py).

CÓMO SABE POR QUÉ NOMBRE LLEGÓ: mira `Host`, `X-Forwarded-Host` y el `host=` de
`Forwarded`. Un reverse proxy conserva uno u otro. Si no conservara ninguno,
esta guarda no vería nada y la restricción tendría que ponerla el proxy: por
eso al estrenar el dominio se comprueba, desde fuera, que `/docs-interno`
responde 404. `X-Forwarded-Host` puede traer varios valores separados por
coma (un cliente puede mandar el suyo y el proxy agrega el real al final), así
que se revisan TODOS: basta que uno sea público para restringir. Ningún valor
que ponga un cliente puede quitar la restricción, solo activarla.

Se compara la ruta EXACTA: `/bandeja` sin la diagonal final también responde
404 aquí (FastAPI la redirigiría, y el `Location` podría mostrar el host
interno). La documentación dice `/bandeja/`.
"""

from collections.abc import Iterable

from starlette.datastructures import Headers

from config import HOSTS_PUBLICOS

# Lo que ofrece la documentación pública, y nada más.
RUTAS_PUBLICAS = frozenset(
    {
        ("POST", "/bandeja/"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
    }
)


def _sin_puerto(valor: str) -> str:
    v = valor.strip().strip('"').lower()
    return v if v.startswith("[") else v.split(":", 1)[0]


def nombres_de(headers: Headers) -> set[str]:
    """Todos los nombres con los que la petición dice haber llegado."""
    nombres: set[str] = set()
    for v in headers.getlist("host"):
        nombres.add(_sin_puerto(v))
    for lista in headers.getlist("x-forwarded-host"):
        for v in lista.split(","):
            nombres.add(_sin_puerto(v))
    for lista in headers.getlist("forwarded"):  # RFC 7239: for=1.2.3.4;host=ejemplo.com;proto=https
        for elemento in lista.split(","):
            for par in elemento.split(";"):
                clave, _, v = par.partition("=")
                if clave.strip().lower() == "host":
                    nombres.add(_sin_puerto(v))
    nombres.discard("")
    return nombres


def por_nombre_publico(headers: Headers, publicos: Iterable[str] | None = None) -> bool:
    return not set(HOSTS_PUBLICOS if publicos is None else publicos).isdisjoint(nombres_de(headers))


def permitida(metodo: str, ruta: str) -> bool:
    return (metodo.upper(), ruta) in RUTAS_PUBLICAS
