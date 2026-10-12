"""How much of automation may use the LLM — ADR 0003 §4.9.

``AUTOMATION_AI_MODE`` is ``off``, ``assist`` or ``full``:

* ``off`` — rule engines only (``tc_rules``, ``checklist_rules``, the
  rule-based recon/strategy, the deterministic segmenter). No automation
  code calls the model, whatever key is configured.
* ``assist`` (**default**) — rules first. The model is called only when the
  rules leave a gap (coverage below ``AI_ASSIST_MIN_COVERAGE``, or a rule
  result the caller marks as low confidence) or when the user explicitly
  asked for it ("Improve with AI").
* ``full`` — the behaviour before this module: the model whenever a key is
  configured, rules as the fallback.

It is not a :mod:`engine.features` flag because ``FLAGS`` are on/off only.

An organisation may **lower** the mode in
``Organization.settings["automation_ai_mode"]`` but never raise it above the
environment's value: the env value is what the operator agreed to pay for.

Chat (Tedgie) and search answers are not automation and are not governed
here. Test execution never calls the model at all.

The explicit request travels in a context variable rather than through
every signature between the route and the six callers: the route binds it
around the job it submits (:func:`bind_enhance`), and each caller asks
:func:`llm_allowed`.
"""
from __future__ import annotations

import contextlib
import contextvars
import functools
import os
from typing import Any, Callable, Iterator

from engine.log import get_logger

_logger = get_logger(__name__)

OFF, ASSIST, FULL = "off", "assist", "full"
MODES: tuple[str, ...] = (OFF, ASSIST, FULL)
_RANK = {m: i for i, m in enumerate(MODES)}
DEFAULT_MODE = ASSIST

#: Key in ``Organization.settings`` an admin uses to lower the mode.
ORG_SETTING = "automation_ai_mode"

#: Coverage below which ``assist`` lets the model fill the gap.
DEFAULT_MIN_COVERAGE = 0.6

_ENHANCE: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "tfg_ai_enhance", default=False)


def _normalise(value: Any) -> str | None:
    s = str(value or "").strip().lower()
    return s if s in _RANK else None


def env_mode() -> str:
    """The operator's ceiling. An unknown value reads as the default."""
    raw = os.environ.get("AUTOMATION_AI_MODE", "")
    mode = _normalise(raw)
    if mode is None:
        if raw.strip():
            _logger.warning("AUTOMATION_AI_MODE=%r is not one of %s; using %s",
                            raw, "/".join(MODES), DEFAULT_MODE)
        return DEFAULT_MODE
    return mode


def org_mode(org_id: str | None) -> str | None:
    """The mode an org's admin chose, or ``None`` when it chose nothing."""
    if not org_id:
        return None
    try:
        from engine import db as _db
        org = _db.get_organization(org_id) or {}
    except Exception as exc:  # pragma: no cover — never block on settings
        _logger.debug("ai_mode: org lookup failed: %s", exc)
        return None
    return _normalise((org.get("settings") or {}).get(ORG_SETTING))


def clamp(requested: str | None, ceiling: str) -> str:
    """*requested* lowered to at most *ceiling*; *ceiling* when unset."""
    req = _normalise(requested)
    if req is None:
        return ceiling
    return req if _RANK[req] <= _RANK[ceiling] else ceiling


def _org_from_context(org_id: str | None) -> str | None:
    if org_id:
        return org_id
    try:
        from engine import llm_context
        ctx = llm_context.current()
        return ctx.org_id if ctx else None
    except Exception:  # pragma: no cover — defensive
        return None


def effective_mode(org_id: str | None = None) -> str:
    """The mode in force for *org_id* (default: the current request/job's).

    ``min(env, org)`` — an org can only ever lower it.
    """
    ceiling = env_mode()
    org_id = _org_from_context(org_id)
    return clamp(org_mode(org_id), ceiling)


def min_coverage() -> float:
    try:
        v = float(os.environ.get("AI_ASSIST_MIN_COVERAGE",
                                 DEFAULT_MIN_COVERAGE))
    except (TypeError, ValueError):
        return DEFAULT_MIN_COVERAGE
    return min(1.0, max(0.0, v))


# ── The explicit request ("Improve with AI") ─────────────────────────

def enhance_requested() -> bool:
    return bool(_ENHANCE.get())


@contextlib.contextmanager
def enhanced(on: bool = True) -> Iterator[None]:
    token = _ENHANCE.set(bool(on))
    try:
        yield
    finally:
        _ENHANCE.reset(token)


def bind_enhance(func: Callable[..., Any], on: bool) -> Callable[..., Any]:
    """Wrap *func* so it runs with the explicit request set to *on*.

    For work submitted to the JobQueue: the worker thread does not inherit
    the request's context variables, so the flag is carried in the closure.
    """
    @functools.wraps(func)
    def _run(*args: Any, **kwargs: Any) -> Any:
        with enhanced(on):
            return func(*args, **kwargs)
    return _run


# ── The decision ─────────────────────────────────────────────────────

def llm_allowed(*, coverage: float | None = None,
                low_confidence: bool = False,
                explicit: bool = False,
                org_id: str | None = None) -> bool:
    """May an automation caller call the model now?

    ``coverage`` is what the rules achieved on their own (0..1),
    ``low_confidence`` is the caller's own judgement that the rule result
    is generic, and ``explicit`` is a caller-level request for the model
    (the existing ``force_llm`` arguments). The "Improve with AI" request
    bound by the route counts as explicit too.

    ``off`` refuses everything, explicit requests included — it is the
    switch that makes "no automation LLM spend" a statement rather than a
    hope.
    """
    mode = effective_mode(org_id)
    if mode == OFF:
        return False
    if mode == FULL:
        return True
    if explicit or enhance_requested() or low_confidence:
        return True
    return coverage is not None and coverage < min_coverage()


def rules_first(org_id: str | None = None) -> bool:
    """True when the caller should run its rule engine before the model."""
    return effective_mode(org_id) != FULL


__all__ = [
    "OFF", "ASSIST", "FULL", "MODES", "DEFAULT_MODE", "ORG_SETTING",
    "env_mode", "org_mode", "clamp", "effective_mode", "min_coverage",
    "enhance_requested", "enhanced", "bind_enhance",
    "llm_allowed", "rules_first",
]
