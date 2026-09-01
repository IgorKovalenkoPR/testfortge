"""Thin retry-wrapped wrapper around ``anthropic.Anthropic().messages.create``.

Centralises the LLM call so every caller in the codebase gets the same
timeout (60 s per attempt) and retry policy (3 attempts, exponential
backoff 1 s → 4 s) on transient failures.

On terminal failure (network/transient errors after 3 attempts, missing
API key, or SDK import problems) we raise :class:`LLMUnavailable`.
Callers decide whether to fall through to a deterministic fallback or
surface the error to the user.

Implements Sprint 1 Task 6 of TestForTge — see
``docs/plans/sprint_1_security.md``.
"""
from __future__ import annotations

import os
from typing import Any

from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from engine.log import get_logger

_logger = get_logger(__name__)

# Per-attempt timeout (seconds). The Anthropic SDK accepts ``timeout``
# kwarg on ``messages.create`` and converts to httpx timeouts.
DEFAULT_TIMEOUT = 60

# Three attempts total (initial + 2 retries) with exponential backoff
# 1 s → 2 s → capped at 4 s, matching the plan in section "Task 6".
_MAX_ATTEMPTS = 3


class LLMUnavailable(RuntimeError):
    """Raised when the Anthropic LLM cannot be reached.

    Conditions:
      * No API key — neither the organisation's own (BYOK) nor the
        platform's ``ANTHROPIC_API_KEY``.
      * The ``anthropic`` SDK cannot be imported.
      * All 3 retry attempts exhausted on transient failures
        (APIConnectionError / APITimeoutError / RateLimitError /
        InternalServerError).
      * The organisation is over its monthly budget — see
        :class:`LLMBudgetExceeded`.
    """


class LLMBudgetExceeded(LLMUnavailable):
    """The organisation has spent its monthly allowance on the platform key.

    Subclasses :class:`LLMUnavailable` deliberately. Every caller in the
    codebase already catches that and falls through to its deterministic
    path — rule engines, YAML knowledge packs, the ISTQB corpus — so a
    team that runs out of budget gets a working, if less clever, platform
    instead of an error page. A caller that wants to say something
    specific about the budget can still catch this subclass first.

    Does not apply to a BYOK organisation: capping what someone spends on
    their own key would be a strange thing for a platform to do.
    """


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _retryable_exception_types() -> tuple[type[BaseException], ...]:
    """Lazily resolve the anthropic transient-error exception classes.

    Done at call time (not import time) so that:
      * The module remains importable in environments without the SDK.
      * Unit tests can swap a fake anthropic module via monkeypatch.
    """
    try:
        from anthropic import (
            APIConnectionError,
            APITimeoutError,
            InternalServerError,
            RateLimitError,
        )
    except Exception:  # pragma: no cover — exercised in absence of SDK
        return ()
    return (
        APIConnectionError,
        APITimeoutError,
        RateLimitError,
        InternalServerError,
    )


def _build_client(api_key: str | None = None) -> Any:
    """Construct an ``anthropic.Anthropic`` client.

    Raises :class:`LLMUnavailable` when the SDK is unavailable or no key
    could be resolved. ``api_key`` is passed in by :func:`call_messages`
    after BYOK resolution; when omitted we fall back to the platform key
    so the older call shape keeps working.
    """
    key = (api_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise LLMUnavailable("no Anthropic API key (neither org nor platform)")
    try:
        from anthropic import Anthropic
    except Exception as exc:  # pragma: no cover — defensive
        raise LLMUnavailable(f"anthropic SDK not importable: {exc}") from exc
    return Anthropic(api_key=key)


def _org_settings(org_id: str | None) -> dict | None:
    if not org_id:
        return None
    try:
        from engine import db as _db
        org = _db.get_organization(org_id)
        return (org or {}).get("settings") if org else None
    except Exception as exc:  # pragma: no cover — metering is best-effort
        _logger.debug("org settings lookup failed for %s: %s",
                      (org_id or "")[:8], exc)
        return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_messages(*, timeout: int = DEFAULT_TIMEOUT,
                  kind: str | None = None,
                  org_id: str | None = None,
                  project_id: str | None = None,
                  user_id: str | None = None,
                  **kwargs: Any) -> Any:
    """Call ``Anthropic().messages.create(**kwargs)`` with retry + timeout.

    This is the single chokepoint every LLM call in the codebase goes
    through, so it is also where model routing (E0.8), the per-org key
    (E0.9) and usage accounting (E0.7) live. None of that is visible to a
    caller that does not ask for it: the original two-argument shape
    behaves exactly as before.

    Parameters
    ----------
    timeout:
        Per-attempt timeout in seconds. Defaults to 60 s.
    kind:
        Work kind from :mod:`engine.llm_models` (``authoring``,
        ``analysis``, ``consult``, ``segmentation``, ``vision``). When
        given and ``model`` is not, the model is routed from it. Also the
        dimension the usage report groups by, so passing it is what makes
        "which module is expensive" answerable.
    org_id, project_id, user_id:
        Attribution. ``org_id`` additionally selects the organisation's
        own API key when it has one, and is what the budget is checked
        against.
    **kwargs:
        Forwarded verbatim to ``messages.create`` (``model``,
        ``max_tokens``, ``system``, ``messages``, …).

    Returns
    -------
    Any
        The raw response object returned by the Anthropic SDK.

    Raises
    ------
    LLMBudgetExceeded
        The org is over its monthly allowance on the platform key.
    LLMUnavailable
        No API key, the SDK cannot be imported, or all retry attempts
        have been exhausted on transient errors.
    """
    # Route the model when the caller named a kind instead of a model.
    if not kwargs.get("model") and kind:
        from engine import llm_models
        kwargs["model"] = llm_models.model_for(kind)
    model = kwargs.get("model") or "unknown"

    # Whose key, and therefore whose money.
    try:
        from engine import llm_keys
        api_key, key_source = llm_keys.resolve_key(org_id)
    except Exception as exc:  # pragma: no cover — never block on BYOK
        _logger.warning("BYOK resolution failed, using platform key: %s", exc)
        api_key, key_source = None, "platform"

    # Budget gate, before the call rather than after it.
    check_budget(org_id, key_source=key_source)

    client = _build_client(api_key)
    retryable = _retryable_exception_types()

    @retry(
        retry=retry_if_exception_type(retryable),
        stop=stop_after_attempt(_MAX_ATTEMPTS),
        wait=wait_exponential(multiplier=1, min=1, max=4),
        reraise=True,
    )
    def _do_call() -> Any:
        return client.messages.create(timeout=timeout, **kwargs)

    if not retryable:
        # SDK unavailable — nothing sensible to do beyond surfacing it.
        response = _do_call()
        _meter(response, kind=kind, model=model, key_source=key_source,
               org_id=org_id, project_id=project_id, user_id=user_id)
        return response
    try:
        response = _do_call()
    except retryable as exc:
        _logger.warning(
            "LLM call failed after %d attempts: %s: %s",
            _MAX_ATTEMPTS, type(exc).__name__, exc,
        )
        raise LLMUnavailable(
            f"Anthropic API unavailable after {_MAX_ATTEMPTS} attempts: {exc}"
        ) from exc
    _meter(response, kind=kind, model=model, key_source=key_source,
           org_id=org_id, project_id=project_id, user_id=user_id)
    return response


def check_budget(org_id: str | None, *, key_source: str = "platform") -> None:
    """Raise :class:`LLMBudgetExceeded` when this org is over its allowance.

    The condition is "not the org's own key", not "is the platform key".
    Those differ when no key resolved at all, and gating on the narrower one
    meant an instance with no platform key skipped the check entirely — the
    guard was only armed when a key happened to be configured. Only a BYOK
    org is exempt.

    Public, and called from two places, because the streaming chat path
    cannot go through :func:`call_messages` at all: the SDK's streaming
    helper has no equivalent there, so ``routes/chat.py`` builds its own
    client — and therefore skipped this gate by construction. Two surfaces
    of one feature answered "may I spend this team's allowance?"
    differently, and the non-streaming one skipped it too, for the separate
    reason that ``engine.chatbot`` never passed an ``org_id`` at all.

    Fails **open**: a metering outage must not lock every org out of
    generation. The budget is a cost guard, not a security control.
    """
    if not org_id or key_source == "org":
        return
    try:
        from engine import llm_cost
        state = llm_cost.budget_state(org_id, _org_settings(org_id),
                                      key_source=key_source)
    except Exception as exc:  # pragma: no cover — fail open
        _logger.warning("budget check skipped: %s", exc)
        return
    if state["over"]:
        raise LLMBudgetExceeded(
            f"organisation {org_id[:8]} has used "
            f"{llm_cost.format_usd(state['spent_micros'])} of its "
            f"{llm_cost.format_usd(state['limit_micros'])} monthly "
            f"allowance; falling back to the deterministic engine"
        )


def _meter(response: Any, *, kind: str | None, model: str, key_source: str,
           org_id: str | None, project_id: str | None,
           user_id: str | None) -> None:
    """Price the response and record it. Swallows everything.

    Wrapped in a bare ``except`` on purpose, and this is the one place in
    the module where that is right: the API call has already succeeded and
    the caller is about to receive a valid answer. Letting an accounting
    error turn that into an exception would throw away work the user
    already paid for — the worst possible trade.
    """
    try:
        from engine import llm_cost
        from engine import db as _db
        usage = llm_cost.extract_usage(response)
        if not any(usage):
            # A mocked response in a test, or an SDK that reports nothing.
            # No row rather than a zero row: a meter full of zeroes reads
            # like "we made a thousand free calls".
            return
        _db.record_llm_usage(
            kind=kind or "unknown", model=model, key_source=key_source,
            org_id=org_id, project_id=project_id, user_id=user_id,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_micros=llm_cost.cost_micros(model, usage),
        )
    except Exception as exc:  # pragma: no cover — accounting is never fatal
        _logger.debug("usage metering skipped: %s", exc)


__all__ = ["LLMUnavailable", "LLMBudgetExceeded", "call_messages",
           "check_budget", "DEFAULT_TIMEOUT"]
