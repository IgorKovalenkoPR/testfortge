"""Storage path constants for automation/execution artifacts.

Lives in ``engine/`` so the orchestrator and post-processor can import it
without pulling in ``routes/*`` (which would create an import cycle).
"""

from __future__ import annotations

import os

STORAGE_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "storage",
)
os.makedirs(STORAGE_ROOT, exist_ok=True)


__all__ = ["STORAGE_ROOT"]
