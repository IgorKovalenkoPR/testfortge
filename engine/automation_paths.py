"""Storage path constants for automation/execution artifacts.

Lives in ``engine/`` so the orchestrator and post-processor can import it
without pulling in ``routes/*`` (which would create an import cycle).
"""

from __future__ import annotations

import os

#: Where run artefacts, uploads and bug attachments live on local disk.
#:
#: Overridable by ``STORAGE_ROOT`` so a deployment can put artefacts on a
#: mounted volume instead of inside the checkout — and so two test runs on
#: one machine do not write into, and sweep, the same directory (M-1). It
#: pairs with ``STORAGE_FOLDER`` in ``config.py``, which names the same
#: place for the parts of the application that read config rather than this
#: constant.
STORAGE_ROOT = os.environ.get("STORAGE_ROOT", "").strip() or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "storage",
)
os.makedirs(STORAGE_ROOT, exist_ok=True)

#: The directory ``engine/`` and ``routes/`` live in — i.e. what has to be
#: on ``sys.path`` for ``python -m engine.runner_worker`` to resolve.
#:
#: Every spawn site used to pass ``dirname(STORAGE_ROOT)`` as the worker's
#: cwd, which is the same directory only while artefacts sit inside the
#: checkout. Point ``STORAGE_ROOT`` at a mounted volume — the deployment
#: the constant above exists to support — and every dispatch died at exec
#: with ``No module named 'engine'``, while the POST still flashed
#: "✓ Playwright pass dispatched". Nothing came back and nothing said why.
#:
#: Derived from this file's own location, because that is the one thing
#: that is true wherever the application is installed.
APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


__all__ = ["STORAGE_ROOT", "APP_ROOT"]
