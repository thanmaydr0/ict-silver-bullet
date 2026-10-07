"""Consistent UTC console and rotating file logging for this process."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time


class _UTCFormatter(logging.Formatter):
    converter = time.gmtime


def setup_logging(name: str) -> logging.Logger:
    """Configure an INFO logger with console and logs/<name>.log output.

    File output rotates at 5 MiB with five backups. Repeated setup calls
    reuse existing handlers, and timestamps are UTC. Return the logger.
    """
    if not name or name in {".", ".."} or "/" in name or "\\" in name:
        raise ValueError("Logger name must be a nonempty filename without path separators.")
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if getattr(logger, "_ict_logging_configured", False):
        return logger
    log_dir = Path(__file__).resolve().parents[1] / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    formatter = _UTCFormatter(
        "%(asctime)sZ | %(levelname)s | %(name)s | %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    console = logging.StreamHandler()
    file_handler = RotatingFileHandler(
        log_dir / f"{name}.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    for handler in (console, file_handler):
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    logger._ict_logging_configured = True
    return logger
