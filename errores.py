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
