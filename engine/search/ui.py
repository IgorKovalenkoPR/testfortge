"""What the search UI may show (ADR 0003 §1.3, §4.10; task T7).

One function, read by the context processor in :mod:`routes` so every
template sees the same answer:

* ``None`` while ``PLATFORM_SEARCH`` is effectively off — base.html then
  renders no search markup at all, byte for byte what it did before —
  and also with authentication on and nobody signed in;
* ``{"ai": bool}`` otherwise, where ``ai`` is ``PLATFORM_SEARCH_AI``.

``PLATFORM_SEARCH_AI`` is declared by T6 (the Ask backend), which may land
after this UI. :func:`engine.features.effective` raises ``UnknownFlag`` for
an undeclared name, so the read is defensive: an undeclared flag is off.
The AI parts of the UI therefore stay hidden until the backend that serves
them exists, whatever the environment says.
"""
from __future__ import annotations


def ai_enabled() -> bool:
    from engine import features
    try:
        return bool(features.effective("PLATFORM_SEARCH_AI"))
    except features.UnknownFlag:
        return False


def search_ui_context() -> dict | None:
    from engine import features
    if not features.effective("PLATFORM_SEARCH"):
        return None
    # Nobody signed in (the sign-in, invitation and reset pages): every
    # search endpoint would refuse them, so offer nothing.
    from engine import permissions as _perm
    if _perm.auth_active() and not _perm.current_user_id():
        return None
    return {"ai": ai_enabled()}


__all__ = ["search_ui_context", "ai_enabled"]
