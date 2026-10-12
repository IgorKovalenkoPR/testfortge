"""TestFortge — who an LLM call is on behalf of, without threading it through
every signature (ADR 0003 §4.7, "Plumbing").

The measured gap this closes: ``tc_author``, ``checklist_author``,
``test_strategy``, ``site_recon``, ``session_segmenter`` and
``mockup_vision`` call :func:`engine.llm_client.call_messages` without an
``org_id``. So the budget gate (``if not org_id: return``), the org's own
key (BYOK) and the per-org usage panel all skipped them — and a model
switcher keyed on the org would have skipped them too.

Rather than change six signatures and every caller of each, the request
records who it is for in a :class:`contextvars.ContextVar`, and
``call_messages`` reads it whenever the caller did not pass the values
explicitly. An explicit argument always wins.

Two places set it:

* :func:`install` registers a ``before_request`` hook that reads the
  signed-in user, the active org and the session's project — session
  reads only, no database query, because the hook runs for every request
  including static files;
* :class:`engine.job_queue.JobQueue` captures it at ``submit`` time and
  sets it inside the worker thread, because a ``ThreadPoolExecutor`` does
  not carry context variables across and most generation runs there.

The project id is attribution, not authorisation: it is whatever the
session has pinned. Access to the project was decided by the route that
started the work.
"""
from __future__ import annotations

import contextlib
import contextvars
from dataclasses import dataclass
from typing import Iterator

from engine.log import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class LlmContext:
    """Attribution for LLM calls made while this context is active."""
    org_id: str | None = None
    project_id: str | None = None
    user_id: str | None = None

    def is_empty(self) -> bool:
        return not (self.org_id or self.project_id or self.user_id)


_CTX: contextvars.ContextVar[LlmContext | None] = contextvars.ContextVar(
    "testfortge_llm_context", default=None)


def current() -> LlmContext | None:
    """The active context, or ``None``."""
    return _CTX.get()


def set_context(ctx: LlmContext | None) -> contextvars.Token:
    """Make *ctx* active. Returns the token :func:`reset` needs."""
    return _CTX.set(ctx)


def reset(token: contextvars.Token) -> None:
    """Undo a :func:`set_context`. Tolerates a token from another context
    (a teardown on a different thread), which is a no-op rather than an
    error: the worst case is a stale value overwritten by the next set."""
    try:
        _CTX.reset(token)
    except (ValueError, RuntimeError):
        _CTX.set(None)


@contextlib.contextmanager
def bound(org_id: str | None = None, project_id: str | None = None,
          user_id: str | None = None) -> Iterator[LlmContext]:
    """``with bound(org_id=…):`` — for scripts, tests and the MCP service."""
    ctx = LlmContext(org_id=org_id, project_id=project_id, user_id=user_id)
    token = set_context(ctx)
    try:
        yield ctx
    finally:
        reset(token)


def fill(org_id: str | None, project_id: str | None,
         user_id: str | None) -> tuple[str | None, str | None, str | None]:
    """Each value as given, or from the active context when not given."""
    ctx = current()
    if ctx is None:
        return org_id, project_id, user_id
    return (org_id or ctx.org_id,
            project_id or ctx.project_id,
            user_id or ctx.user_id)


def from_request() -> LlmContext:
    """Read the current request's attribution out of the session.

    Session only — ``permissions.current_user_id`` and ``current_org_id``
    both read the cookie-backed session, and the project is the session's
    pinned ``project_id``. ADR 0003 names ``resolve_active_project`` for
    this; that can query Postgres, and doing so for every request,
    including every stylesheet, is not worth it for attribution.
    """
    from engine import permissions as _perm
    project_id = None
    try:
        from flask import session
        project_id = session.get("project_id") or None
    except Exception:          # outside a request context
        project_id = None
    return LlmContext(org_id=_perm.current_org_id(),
                      project_id=project_id,
                      user_id=_perm.current_user_id())


_G_TOKEN = "_llm_context_token"


def install(app) -> None:
    """Set the context at the start of each request, clear it at the end."""
    from flask import g

    @app.before_request
    def _bind_llm_context():
        try:
            setattr(g, _G_TOKEN, set_context(from_request()))
        except Exception as exc:  # pragma: no cover — attribution only
            log.debug("llm context not bound: %s", exc)

    @app.teardown_request
    def _unbind_llm_context(_exc=None):
        token = g.pop(_G_TOKEN, None) if hasattr(g, "pop") else None
        if token is not None:
            reset(token)


__all__ = ["LlmContext", "current", "set_context", "reset", "bound", "fill",
           "from_request", "install"]
