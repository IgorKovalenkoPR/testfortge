"""
TestFortge — Central logging helper.

Provides ``get_logger(name)`` so engine modules can emit structured log
messages without each file having to reach into ``logging`` themselves.
Log level is controlled by ``LOG_LEVEL`` env var (default INFO).
Root configuration is applied exactly once at import time.
"""
from __future__ import annotations

import logging
import os
import sys

# ── One-shot root configuration ─────────────────────────────────────
_LEVEL_NAME = (os.environ.get("LOG_LEVEL") or "INFO").upper()
_LEVEL = getattr(logging, _LEVEL_NAME, logging.INFO)

_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"

if not logging.getLogger().handlers:
    # Avoid double-configuring when Flask / Werkzeug already set up handlers.
    logging.basicConfig(level=_LEVEL, format=_FORMAT, stream=sys.stderr)


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger honoring the central LOG_LEVEL."""
    logger = logging.getLogger(name)
    logger.setLevel(_LEVEL)
    return logger


__all__ = ["get_logger"]
