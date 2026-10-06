"""Logging utilities: colored console output + rotating file logs + progress bar."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

try:
    from tqdm import tqdm
except ImportError:  # pragma: no cover
    tqdm = None


class ColorFormatter(logging.Formatter):
    COLORS = {
        logging.DEBUG: "\033[36m",
        logging.INFO: "\033[32m",
        logging.WARNING: "\033[33m",
        logging.ERROR: "\033[31m",
        logging.CRITICAL: "\033[41m",
    }
    RESET = "\033[0m"

    def __init__(self, *args, color: bool = True, **kwargs):
        super().__init__(*args, **kwargs)
        self.color = color

    def format(self, record: logging.LogRecord) -> str:
        color = self.COLORS.get(record.levelno, "")
        msg = super().format(record)
        return f"{color}{msg}{self.RESET}" if self.color else msg


def setup_logger(
    name: str = "mtselector", log_dir: str = "logs", level: int = logging.INFO
) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        logger.setLevel(level)
        return logger
    logger.setLevel(level)
    logger.propagate = False

    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

    console = logging.StreamHandler(sys.stdout)
    color = sys.stdout.isatty() and "NO_COLOR" not in os.environ and os.environ.get("TERM") != "dumb"
    console.setFormatter(ColorFormatter(fmt, datefmt="%H:%M:%S", color=color))
    logger.addHandler(console)

    try:
        Path(log_dir).mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            Path(log_dir) / f"{name}.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(logging.Formatter(fmt, datefmt="%Y-%m-%d %H:%M:%S"))
        logger.addHandler(file_handler)
    except OSError as exc:
        logger.warning("File logging unavailable (%s); continuing with console logs.", exc)

    return logger


def progress_bar(total: int, desc: str = "Working"):
    """Return a tqdm progress bar, or a no-op fallback if tqdm isn't installed."""
    if tqdm is not None:
        return tqdm(
            total=total, desc=desc, dynamic_ncols=True, disable=not sys.stdout.isatty(), file=sys.stdout
        )

    class _Noop:
        def update(self, n=1):
            pass

        def close(self):
            pass

    return _Noop()
