"""
Structured logging: console + rotating file. JSON optional via env.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path


def setup_logger(
    name: str = "pm_bot",
    log_dir: str = "./logs",
    level: str = "INFO",
) -> logging.Logger:
    """Configure root logger. Idempotent — safe to call multiple times."""
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger

    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # File (rotating)
    log_path = Path(log_dir)
    log_path.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        log_path / "pm_bot.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger


def get_logger(name: str) -> logging.Logger:
    """Get a child logger. Parent must have been set up with setup_logger."""
    return logging.getLogger(f"pm_bot.{name}")
