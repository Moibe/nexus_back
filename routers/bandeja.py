"""Dominio: bandeja — la puerta por la que un CLIENTE manda archivos a la
bandeja de preparación documental.

Subir aquí es "que esto aparezca en la bandeja de en medio del front": el
archivo se guarda en el almacén (misma validación que `POST /archivos/`, vía
`guardar_subida`) y se anota como pendiente en el registro de la bandeja. El
front consulta `GET /bandeja/` cada pocos segundos y pinta lo nuevo con origen
"API REST"; cuando lo manda al pipeline o lo descarta, avisa con `retirar`.

A diferencia de `/archivos/`, que es almacenamiento a secas (lo usan también
los ejemplos del asistente de configuración), esto es INGESTA: lo que entra
por aquí es trabajo pendiente de alguien.

## Quién puede qué

- **Subir** (`POST /`) acepta una **API Key de cliente**, y entonces el tenant
  sale de la LLAVE: el cliente no lo manda (y si lo manda distinto, es 403).
  Acepta también la llave de servicio del front, y ahí el `tenant` es
  obligatorio en el formulario — es lo que usa Swagger para probar a mano.
- **Listar y retirar** son solo del front (llave de servicio). Un cliente
  manda documentos; no administra la bandeja.

El router entero exige ALGUNA llave válida (`exigir_llave_o_cliente`, en
`app.py`), y cada ruta de administración agrega `exigir_llave` encima. Así una
ruta nueva que se agregue aquí sin pensarlo nunca queda abierta: a lo más,
queda abierta a clientes.

## Provisional

**El registro es un archivo en el NAS**, no la base (ver
`servicios/bandeja.py`). Cambia a `[documents].[files]` cuando existan los
SPs, sin tocar este router. Las llaves de cliente también viven así por ahora
(ver `servicios/llaves_cliente.py`).

Los handlers son `def`, no `async def`: el almacén y el registro son I/O
síncrono y deben ir al threadpool.
"""

import logging

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status

from routers.archivos import guardar_subida
from seguridad import Identidad, exigir_llave, exigir_llave_o_cliente
from servicios import bandeja
from servicios.almacen import ErrorAlmacen
from servicios.subidas import normalizar_tipo

logger = logging.getLogger(__name__)

router = APIRouter()

# Lo que el pipeline del front sabe procesar (su `MIME_POR_EXTENSION`), y lo
# mismo que acepta la carga manual. Más estrecho que `MIME_SOPORTADOS` a
# propósito: un GIF o un WebP entraría a la bandeja y fallaría hasta el
# pipeline con "no soportado"; mejor decírselo al cliente al subir.
MIME_BANDEJA = {"application/pdf", "image/jpeg", "image/png", "image/tiff"}


def _tenant_de(quien: Identidad, pedido: str | None) -> str:
    """De qué cliente es lo que se sube.

    Con una llave de cliente, el de la llave — y punto: si la petición dice
    otro, es un 403 y no se sube nada. Aceptarlo en silencio dejaría que un
    cliente escribiera en el prefijo de otro con solo cambiar un campo, que es
    justo lo que las llaves por cliente existen para impedir.
    """
    if quien.tipo == "cliente":
        if pedido and pedido != quien.tenant:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Esta API Key es de otro cliente. Omite `tenant`: se toma de la llave.",
            )
        return quien.tenant or ""
    if not pedido:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Falta `tenant`: con la llave de servicio hay que decir de qué cliente es.",
        )
    return pedido


def _no_disponible(exc: Exception, que: str) -> HTTPException:
    logger.exception("Falló el registro de la bandeja (%s)", que)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="La bandeja no está disponible en este momento. Intenta de nuevo en unos minutos.",
    )


@router.post(
    "/",
    status_code=status.HTTP_201_CREATED,
    tags=["Bandeja"],
    summary="Mandar un archivo a la bandeja de preparación",
    description=(
        "Guarda el archivo y lo deja pendiente en la bandeja de preparación "
        "documental, donde aparece con origen «API REST». Acepta PDF, JPEG, PNG "
        "y TIFF de hasta 20 MB. Devuelve la entrada creada; su `id` es con el "
        "que se retira."
    ),
)
def subir_a_bandeja(
    archivo: UploadFile = File(...),
    tenant: str | None = Form(
        None,
        description=(
            "Con una API Key de cliente NO hace falta: el cliente sale de la llave. "
            "Solo con la llave de servicio del front es obligatorio."
        ),
    ),
    sha256: str | None = Form(
        None, description="Hash que calculó el cliente, para verificar la transferencia"
    ),
    quien: Identidad = Depends(exigir_llave_o_cliente),
):
    tenant = _tenant_de(quien, tenant)
    if normalizar_tipo(archivo.content_type) not in MIME_BANDEJA:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Formato no admitido en la bandeja. Se aceptan PDF, JPEG, PNG y TIFF.",
        )
    guardado = guardar_subida(archivo, tenant, sha256)
    try:
        return bandeja.registrar_entrada(
            tenant,
            guardado,
            mime=normalizar_tipo(archivo.content_type),
            nombre_original=archivo.filename or "",
            canal="API",
            llave_id=quien.llave_id,
        )
    except ErrorAlmacen as exc:
        # Los bytes ya quedaron en el almacén; sin registro son un objeto suelto,
        # barato y deduplicado. Reintentar la subida lo registra sin duplicar
        # disco.
        raise _no_disponible(exc, f"registrar, tenant={tenant}") from exc


@router.get(
    "/",
    dependencies=[Depends(exigir_llave)],
    tags=["Bandeja"],
    summary="Lo que está pendiente en la bandeja",
    description="Las entradas que llegaron y todavía no pasan al pipeline ni se descartan, de la más vieja a la más nueva.",
)
def listar_bandeja(tenant: str = Query(..., description="Cliente")):
    try:
        return {"entradas": bandeja.listar_pendientes(tenant)}
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise _no_disponible(exc, f"listar, tenant={tenant}") from exc


@router.post(
    "/{id_entrada}/retirar",
    dependencies=[Depends(exigir_llave)],
    tags=["Bandeja"],
    summary="Sacar una entrada de la bandeja",
    description=(
        "La saca de lo pendiente porque pasó al pipeline o porque se descartó. "
        "El archivo NO se borra del almacén."
    ),
)
def retirar_de_bandeja(
    id_entrada: str,
    tenant: str = Form(...),
    motivo: str = Form(..., description="«pipeline» o «descartado»"),
):
    try:
        retirada = bandeja.retirar(tenant, id_entrada, motivo)
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except ErrorAlmacen as exc:
        raise _no_disponible(exc, f"retirar, tenant={tenant}") from exc
    if not retirada:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Esa entrada no está pendiente en la bandeja (no existe o ya salió).",
        )
    return {"retirada": True}
