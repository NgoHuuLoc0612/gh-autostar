"""Structured logging setup for gh-autostar."""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

_configured = False


def setup_logging(
    level: str = "INFO",
    log_file: Path | None = None,
    max_bytes: int = 10 * 1024 * 1024,
    backup_count: int = 5,
) -> logging.Logger:
    global _configured
    root = logging.getLogger("gh_autostar")
    if _configured:
        return root

    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    fmt = logging.Formatter(
        fmt="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler
    ch = logging.StreamHandler(sys.stderr)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    # Rotating file handler
    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=max_bytes,
            backupCount=backup_count,
            encoding="utf-8",
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    _configured = True
    return root


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"gh_autostar.{name}")
