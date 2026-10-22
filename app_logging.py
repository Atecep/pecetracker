"""Persistent diagnostics for both console and windowed launchers."""
import logging
from logging.handlers import RotatingFileHandler
import threading

from app_paths import ensure_data_dir

_lock = threading.Lock()


def get_logger(name):
    return logging.getLogger(f"slt.{name}")


def configure_logging():
    with _lock:
        logger = logging.getLogger("slt")
        if logger.handlers:
            return
        path = ensure_data_dir() / "slt.log"
        handler = RotatingFileHandler(
            path, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8"
        )
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s"
        ))
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        try:
            path.chmod(0o600)
        except OSError:
            pass
