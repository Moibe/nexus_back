"""Excepciones propias de la aplicación.

Vive aparte de `db/` a propósito, y esto NO es organización por gusto: este
módulo no importa `pyodbc`. `db/sqlserver.py` sí lo importa a nivel de módulo,
y la app tiene que poder arrancar sin el driver ODBC instalado (así corre hoy en
el server de CSI, con el grupo /documentos/* apagado).

Si estas clases vivieran en `db/sqlserver.py`, cualquiera que quisiera
capturarlas tendría que importarlas dentro de un `try` — y entonces el `except`
que las nombra falla con `UnboundLocalError` cuando lo que truena es el import
mismo, tapando el error real con uno inventado. Ya pasó una vez.
"""


class ConfiguracionIncompleta(RuntimeError):
    """Falta algo del `.env`; la base no llegó a fallar porque nunca se intentó.

    Su mensaje SÍ se puede mostrar al cliente: lo escribimos nosotros y solo
    nombra qué variables faltan, nunca sus valores. Los errores de `pyodbc` son
    lo contrario (traen host, driver y usuario en el texto) y por eso allá solo
    se publica el tipo y el SQLSTATE.
    """


class ErrorDocumentAI(RuntimeError):
    """Document AI rechazó una llamada concreta.

    Mismo criterio que `ConfiguracionIncompleta` arriba, aplicado al revés: el
    MENSAJE de esta excepción es el texto crudo de Google y NO se puede mostrar
    al cliente (trae rutas de recurso con el id de proyecto y de procesador).
    Va al log. Lo que sí viaja al usuario es lo que el router escribe a partir
    de `motivo`.

    `motivo` clasifica la causa en categorías por lo que el usuario PUEDE HACER
    al respecto, que es la única distinción que le sirve:

      - `limite_paginas`  el documento excede el tope del procesamiento en
                          línea (15 páginas, 30 sin imágenes) → hay que
                          recortarlo o procesarlo de otra forma
      - `cuota`           se agotó la cuota de páginas por minuto → esperar
      - `archivo_ilegible` PDF corrupto/truncado/con contraseña, o imagen que
                          no se puede decodificar → conseguir otro archivo
      - `servicio`        Google falló de su lado (5xx) → reintentar
      - `desconocido`     todo lo demás; el router pone su texto genérico

    Los textos que se buscan para clasificar están MEDIDOS contra la API real,
    no supuestos — ver `_motivo_de` en `servicios/ia.py`.
    """

    def __init__(self, mensaje: str, status_code: int, motivo: str):
        super().__init__(mensaje)
        self.status_code = status_code
        self.motivo = motivo
