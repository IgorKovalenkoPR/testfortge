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
      * ANTHROPIC_API_KEY is missing or empty.
      * The ``anthropic`` SDK cannot be imported.
      * All 3 retry attempts exhausted on transient failures
        (APIConnectionError / APITimeoutError / RateLimitError /
        InternalServerError).
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


def _build_client() -> Any:
    """Construct an ``anthropic.Anthropic`` client.

    Raises :class:`LLMUnavailable` when the SDK is unavailable or the
    ANTHROPIC_API_KEY env var is missing/empty.
    """
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        raise LLMUnavailable("ANTHROPIC_API_KEY is not set or empty")
    try:
        from anthropic import Anthropic
    except Exception as exc:  # pragma: no cover — defensive
        raise LLMUnavailable(f"anthropic SDK not importable: {exc}") from exc
    return Anthropic(api_key=api_key)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def call_messages(*, timeout: int = DEFAULT_TIMEOUT, **kwargs: Any) -> Any:
    """Call ``Anthropic().messages.create(**kwargs)`` with retry + timeout.

    Parameters
    ----------
    timeout:
        Per-attempt timeout in seconds. Defaults to 60 s.
    **kwargs:
        Forwarded verbatim to ``messages.create`` (``model``, ``max_tokens``,
        ``system``, ``messages``, etc).

    Returns
    -------
    Any
        The raw response object returned by the Anthropic SDK.

    Raises
    ------
    LLMUnavailable
        If the API key is missing, the SDK cannot be imported, or all
        retry attempts have been exhausted on transient errors.
    """
    client = _build_client()
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
        return _do_call()
    try:
        return _do_call()
    except retryable as exc:
        _logger.warning(
            "LLM call failed after %d attempts: %s: %s",
            _MAX_ATTEMPTS, type(exc).__name__, exc,
        )
        raise LLMUnavailable(
            f"Anthropic API unavailable after {_MAX_ATTEMPTS} attempts: {exc}"
        ) from exc


__all__ = ["LLMUnavailable", "call_messages", "DEFAULT_TIMEOUT"]
