"""NexusDoc AI — API.

Backend único del proyecto: es el dueño exclusivo de SQL Server (el front nunca
toca la base) y además orquesta las llamadas a los servicios de IA.

Arranque local:  .venv/Scripts/python.exe -m uvicorn app:app --reload --port 8083
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config import CORS_ALLOWED_ORIGINS, ENVIRONMENT, PORT

app = FastAPI(
    title="NexusDoc AI · API",
    description="Ingesta, procesamiento y configuración documental para NexusDoc AI.",
    version="0.0.1",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ALLOWED_ORIGINS,
    allow_credentials=False,  # el front llama desde su capa server, no manda cookies
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
)


@app.get(
    "/health",
    tags=["Utilidad"],
    summary="Health Check",
    description="Verifica que el servidor esté en línea. No toca la base.",
)
def health():
    return {"status": "ok", "environment": ENVIRONMENT}


@app.get(
    "/health/db",
    tags=["Utilidad"],
    summary="Health Check de SQL Server",
    description="Confirma que la API alcanza SQL Server y devuelve versión y base conectada.",
)
def health_db():
    # Import local: si el driver ODBC no está instalado, que solo truene este
    # endpoint y no el arranque completo de la app.
    from db.sqlserver import probar_conexion

    try:
        return {"status": "ok", **probar_conexion()}
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "detalle": str(exc)}


# Registrar routers
from routers.documentos import router as documentos_router  # noqa: E402

app.include_router(documentos_router, prefix="/documentos")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=PORT)
