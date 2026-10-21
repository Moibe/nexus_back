"""Inventario del esquema de la base, de solo lectura.

Lo consume Nexus Monitor para su pantalla de Tablas, que hasta ahora mostraba
una foto copiada a mano de `docs/solicitudes-dba.md` y por lo tanto envejecía
sin avisar. Sirviéndolo desde aquí, las credenciales de SQL Server siguen
viviendo en UN solo lugar — este servicio — en vez de repartirse a cada app
que quiera mirar la base.

No expone datos de negocio: solo nombres de tabla y conteos de fila (ver
`repositorios/esquema.py`, que explica por qué eso no se puede torcer).
"""

import logging

from fastapi import APIRouter, HTTPException, status

from repositorios.esquema import listar_tablas

logger = logging.getLogger(__name__)

router = APIRouter()


@router.get(
    "/tablas",
    summary="Inventario de tablas de la base",
    description=(
        "Todas las tablas con su esquema y su número de filas, leídas del "
        "catálogo de SQL Server (`sys.*`). Los conteos son los del motor: "
        "exactos para un inventario, no para cuadrar cifras."
    ),
)
def tablas():
    try:
        filas = listar_tablas()
        return {"total": len(filas), "tablas": filas}
    except Exception as exc:  # noqa: BLE001
        # Mismo criterio que /health/db: el texto de una excepción de pyodbc
        # trae host, driver y usuario, así que el detalle se va al log y al
        # cliente solo el tipo de error.
        logger.exception("Falló el inventario de tablas")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"No se pudo leer el catálogo de SQL Server ({type(exc).__name__}).",
        ) from exc
