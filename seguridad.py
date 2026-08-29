"""Llave compartida entre el front y esta API.

Contexto: el server de CSI expone el puerto 8083 a toda la intranet, y no hay
nginx delante. Hasta ahora cualquiera que conociera la IP podía pegarle a
`/ia/ine` — y cada llamada a Document AI cuesta dinero. Con esto, solo quien
traiga la llave puede usar los endpoints de negocio.

El modelo es deliberadamente simple: UNA llave compartida en el header
`X-API-Key`, la misma que el front (su capa server, nunca el navegador) manda
en cada reenvío. No es autenticación de usuarios — es autenticación de
servicio a servicio, que es lo que este PoC necesita hoy: que el front sea el
único cliente de la API. Cuando haya usuarios de verdad, esto se complementa
con sesiones en el front; no se sustituye.

Qué se protege y qué no:
- `/ia/*` y `/documentos/*`: protegidos. Cuestan dinero o tocan la base.
- `/health` y `/health/db`: abiertos. Son diagnósticos, el deploy y pm2 los
  usan sin credenciales, y no revelan más que el estado de la infraestructura.
"""

import secrets

from fastapi import HTTPException, Request, status

from config import NEXUS_API_KEY


async def exigir_llave(request: Request) -> None:
    """Dependencia de FastAPI: corta la petición si no trae la llave correcta.

    Sin llave configurada, la API FALLA CERRADA (503) en vez de quedar abierta:
    un deploy al que se le olvidó la variable debe romperse de forma visible y
    explicarse solo, no convertirse en un endpoint público por accidente. El
    mensaje dice exactamente qué falta y dónde.
    """
    if not NEXUS_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "La API no tiene NEXUS_API_KEY configurada. Agrégala al .env "
                "de nexus_back (la misma que usa el front) y reinicia: "
                "pm2 restart nexus-back-api --update-env"
            ),
        )

    recibida = request.headers.get("x-api-key", "")
    # compare_digest y no `==`: compara en tiempo constante, así el tiempo de
    # respuesta no filtra cuántos caracteres del intento iban bien.
    if not secrets.compare_digest(recibida, NEXUS_API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Falta la llave de API o no es la correcta (header X-API-Key).",
        )
