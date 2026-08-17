"""Configuración de logging.

Mismo patrón que el proyecto hermano `document_ai`: INFO/DEBUG a stdout y
WARNING/ERROR/CRITICAL a stderr, para aprovechar que pm2 ya separa esos streams
en `<app>-out.log` y `<app>-error.log`.

Sin esto, bajo pm2 los `logger.info` no se emiten en absoluto y los
`logger.error/exception` salen por el handler de último recurso de Python, sin
timestamp ni nombre de módulo — o sea, imposible correlacionar nada durante un
incidente.
"""

import logging
import sys


class FiltroNivelMaximo(logging.Filter):
    """Deja pasar únicamente registros con severidad MENOR al nivel indicado."""

    def __init__(self, nivel_maximo):
        super().__init__()
        self.nivel_maximo = nivel_maximo

    def filter(self, record):
        return record.levelno < self.nivel_maximo


def configurar_logging(nivel=logging.INFO):
    """Configura el logger raíz. Se llama UNA vez, al inicio de `app.py`, antes
    de importar los routers (para que cualquier log de import ya salga con
    formato)."""
    formato = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    handler_stdout = logging.StreamHandler(sys.stdout)
    handler_stdout.setLevel(nivel)
    handler_stdout.setFormatter(formato)
    handler_stdout.addFilter(FiltroNivelMaximo(logging.WARNING))

    handler_stderr = logging.StreamHandler(sys.stderr)
    handler_stderr.setLevel(logging.WARNING)
    handler_stderr.setFormatter(formato)

    root = logging.getLogger()
    root.setLevel(nivel)
    root.handlers = [handler_stdout, handler_stderr]
