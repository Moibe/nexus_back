"""Qué se acepta de una subida: formatos y tamaño.

Vive aquí y no dentro de un router porque desde el 2026-09-24 hay DOS puertas
por las que entra un archivo y tienen que exigir lo mismo:

  - `/ia/ine` y `/ia/extraer` — el archivo va derecho a Document AI.
  - `/archivos/` — el archivo se guarda en el almacén.

La lista de MIME estaba dentro de `routers/ia.py`, y el docstring de
`_leer_subida` ya advertía el riesgo de duplicar guardias: "era garantía de que
un día divergieran y un endpoint aceptara lo que el otro rechaza". Con la
segunda puerta ese día habría llegado, así que la política se muda a un solo
lugar y los dos routers la importan.

## Por qué el almacén exige la MISMA lista que Document AI

Podría aceptar cualquier cosa —guarda bytes, no los interpreta—, pero todo lo
que se sube acaba pasando por el extractor. Aceptar aquí un formato que allá se
rechaza solo mueve el fallo más adelante, cuando el archivo ya ocupa disco y el
usuario ya se fue.

## No levanta HTTPException

Levanta `SubidaInvalida`, que carga el `http_status` que le toca. Así este
módulo no depende de FastAPI y el router se limita a traducir. El status va
adentro porque es parte de la decisión —un formato no soportado es 400 y un
archivo grande es 413—, y dejárselo a cada router es pedir que un día no
coincidan.
"""

from config import MAX_SUBIDA_BYTES, MAX_SUBIDA_MB

# La lista de formatos que Document AI procesa. No es una preferencia nuestra:
# es la del proveedor, y mandar algo fuera de ella se traduce en un 400 de
# Google que llegaría disfrazado de 502.
#
# DOCX y XLSX quedan FUERA a propósito aunque la bandeja del front los admita:
# Document AI no los procesa. Se rechazan con un mensaje que lo dice, en vez de
# dejar que fallen más adentro con un error del proveedor.
MIME_SOPORTADOS = frozenset(
    {
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/gif",
        "image/bmp",
        "image/webp",
    }
)


class SubidaInvalida(ValueError):
    """Una subida que no se acepta, con el código HTTP que le corresponde."""

    def __init__(self, mensaje: str, http_status: int):
        super().__init__(mensaje)
        self.mensaje = mensaje
        self.http_status = http_status


def normalizar_tipo(content_type: str | None) -> str:
    """El `tipo/subtipo` en minúsculas, sin los parámetros del header.

    `content_type` puede traer parámetros ("image/jpeg; charset=binary"), así
    que compararlo entero contra la lista fallaría por un detalle que no
    significa nada."""
    return (content_type or "").split(";")[0].strip().lower()


def revisar_tipo(content_type: str | None) -> str:
    """Devuelve el MIME normalizado, o levanta `SubidaInvalida` (400)."""
    tipo = normalizar_tipo(content_type)
    if tipo not in MIME_SOPORTADOS:
        raise SubidaInvalida(
            f"Document AI no procesa '{tipo or 'desconocido'}'. "
            f"Formatos aceptados: {', '.join(sorted(MIME_SOPORTADOS))}.",
            400,
        )
    return tipo


def revisar_tamano(tamano: int | None, que: str = "La imagen") -> None:
    """Levanta `SubidaInvalida` (413) si excede el tope. `None` no se revisa.

    `tamano` puede venir en None cuando el `UploadFile` no lo construyó el
    parser multipart (por ejemplo, uno instanciado a mano en una prueba); ahí
    el guardia que cuenta es el que mira los bytes ya leídos."""
    if tamano is not None and tamano > MAX_SUBIDA_BYTES:
        raise SubidaInvalida(f"{que} excede el límite de {MAX_SUBIDA_MB:g} MB.", 413)
