"""Convenience: a single ``get_logger(name)`` helper that respects settings.log_level."""
from __future__ import annotations

import logging

from app.config import settings


def get_logger(name: str) -> logging.Logger:
    log = logging.getLogger(name)
    log.setLevel(getattr(logging, settings.log_level.upper(), logging.INFO))
    return log
