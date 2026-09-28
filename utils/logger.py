"""
Logging centralizado: archivo agent.log + consola, formato estructurado.

Este logger es para operación normal (debug, info, warnings de negocio).
NO confundir con audit/audit_log.py, que es el registro inmutable de
decisiones autónomas relevantes (reparaciones, herramientas creadas,
self-modification). Ambos existen porque tienen retención y garantías
distintas: este puede rotar/truncarse, el de auditoría no.
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import stat
import sys
from pathlib import Path

from utils.config import settings  # noqa: F401  (para futura config de nivel por yaml)
from utils.correlation import get_correlation_id

LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)

# VULNERABILIDAD REAL ENCONTRADA EN AUDITORÍA EXTERNA (Likay-OS,
# 2026-09-26), A-8: logs/agent.log crecía sin límite (FileHandler
# simple, nunca rotaba) y quedaba con los permisos por defecto del
# proceso (típicamente legible por cualquier otro usuario del mismo
# sistema) — un archivo que además contiene el goal del usuario
# (redactado desde agent_core/routers/chat.py, ver security_policy.py,
# pero sigue siendo texto de conversación real). 10MB x 5 backups
# acota el crecimiento; 0600 restringe la lectura al dueño del proceso.
_MAX_LOG_BYTES = 10 * 1024 * 1024
_LOG_BACKUP_COUNT = 5


class _CorrelationFilter(logging.Filter):
    """
    Inyecta el correlation_id actual (ver utils/correlation.py) en cada
    LogRecord — "-" si ningún pedido lo seteó (arranque del proceso,
    jobs de fondo como el consolidado de memoria, etc.). Un solo filtro
    por logger nombrado alcanza para que TODOS los logger.info(...)/
    warning(...)/error(...) existentes lo muestren, sin tocar ninguno
    de esos call sites.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"
        return True


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # evita handlers duplicados si se llama varias veces

    logger.setLevel(logging.INFO)
    logger.addFilter(_CorrelationFilter())

    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(correlation_id)s | %(name)s | %(message)s"
    )

    log_path = LOG_DIR / "agent.log"
    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=_MAX_LOG_BYTES, backupCount=_LOG_BACKUP_COUNT, encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    try:
        os.chmod(log_path, stat.S_IRUSR | stat.S_IWUSR)  # 0600 — solo el dueño del proceso
    except OSError:
        pass  # best-effort (p.ej. filesystem que no soporta chmod POSIX)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(console_handler)

    return logger
