"""
TestFortge — Central logging helper.

Provides ``get_logger(name)`` so engine modules can emit structured log
messages without each file having to reach into ``logging`` themselves.

Environment variables
---------------------
LOG_LEVEL   : Python logging level name (DEBUG / INFO / WARNING / ERROR).
              Defaults to INFO.
LOG_FORMAT  : "text" (default, human-readable) or "json" (one JSON object
              per line). JSON mode is designed for container log
              aggregators — each record carries ``ts``, ``level``,
              ``logger``, ``message`` plus any ``exc_info`` traceback.

Root configuration is applied exactly once at import time.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time


# ── One-shot root configuration ─────────────────────────────────────
_LEVEL_NAME = (os.environ.get("LOG_LEVEL") or "INFO").upper()
_LEVEL = getattr(logging, _LEVEL_NAME, logging.INFO)

_FORMAT_MODE = (os.environ.get("LOG_FORMAT") or "text").lower().strip()

_TEXT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"


class _JSONFormatter(logging.Formatter):
    """One JSON object per record — safe to pipe into aggregators.

    Fields:
      * ``ts``       ISO-8601 UTC timestamp (second precision is plenty)
      * ``level``    logging level name (INFO, WARNING, …)
      * ``logger``   name passed to ``get_logger``
      * ``message``  fully-formatted log message (`%` interpolation done)
      * ``exc_info`` traceback string when the record carries one
    """

    def format(self, record: logging.LogRecord) -> str:  # noqa: D401
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                time.gmtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        # ``default=str`` keeps us from crashing on non-JSON-serialisable
        # values that somehow wandered into a structured log call.
        return json.dumps(payload, ensure_ascii=False, default=str)


def _make_handler() -> logging.Handler:
    handler = logging.StreamHandler(stream=sys.stderr)
    if _FORMAT_MODE == "json":
        handler.setFormatter(_JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter(_TEXT_FORMAT))
    return handler


if not logging.getLogger().handlers:
    # Avoid double-configuring when Flask / Werkzeug already set up handlers.
    root = logging.getLogger()
    root.setLevel(_LEVEL)
    root.addHandler(_make_handler())


def get_logger(name: str) -> logging.Logger:
    """Return a module-scoped logger honoring the central LOG_LEVEL."""
    logger = logging.getLogger(name)
    logger.setLevel(_LEVEL)
    return logger


__all__ = ["get_logger"]
