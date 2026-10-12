"""Result cache for automation LLM calls — ADR 0003 §4.9.

Anthropic prompt caching makes a repeated *prefix* cheaper; it still bills
the whole response every time. Automation asks the same question often —
the same crawl regenerated, the same requirements re-authored after an
unrelated edit — and an identical question deserves the identical answer
for nothing. :func:`cached_call` is the one place that decides that.

The key::

    sha256(scope | kind | prompt_version | model | house_style_hash
           | sha256(canonical json of system + messages + max_tokens))

* **scope** is the tenant: the org id, or ``u:<user id>`` on a deployment
  without organisations. Never shared; with neither, nothing is cached.
* **prompt_version** is each caller's ``_PROMPT_VERSION`` constant — bump it
  when the prompt's *meaning* changes in a way the text hash would not show
  (a parser that now expects a new field, say).
* **model** is the model the call would be routed to, so a model-switcher
  change is a miss rather than an answer from the previous model.
* **house_style_hash** (:func:`engine.glossary.house_style_hash`) so a
  glossary or wording-rule change invalidates exactly what it could change.

A hit never reaches ``call_messages``: no tokens, no meter row, no budget
check — a hit costs nothing, so it must not count against anything. Hits are
counted in-process (:func:`stats`) and logged.

Never for chat (``consult``) or search answers (``search_answer``): the
first has its own dialogue context, the second its own answer cache keyed on
the cited documents (§4.6). Asking for either raises :class:`ValueError` —
a programming error, not a runtime condition.
"""
from __future__ import annotations

import collections
import hashlib
import json
import threading
from types import SimpleNamespace
from typing import Any, Callable

from engine.log import get_logger

_logger = get_logger(__name__)

#: Kinds that must never be served from here.
UNCACHEABLE_KINDS = frozenset({"consult", "search_answer"})

_STATS_LOCK = threading.Lock()
_HITS: collections.Counter = collections.Counter()
_MISSES: collections.Counter = collections.Counter()


def stats() -> dict[str, dict[str, int]]:
    """In-process hit/miss counters by kind (since start-up)."""
    with _STATS_LOCK:
        return {"hits": dict(_HITS), "misses": dict(_MISSES)}


def reset_stats() -> None:
    with _STATS_LOCK:
        _HITS.clear()
        _MISSES.clear()


# ── Key ──────────────────────────────────────────────────────────────

def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)


def request_hash(call_kwargs: dict) -> str:
    """sha256 over what the model is actually asked: system, messages,
    max_tokens. Timeouts, attribution and the like are not part of it."""
    payload = {
        "system": call_kwargs.get("system"),
        "messages": call_kwargs.get("messages"),
        "max_tokens": call_kwargs.get("max_tokens"),
    }
    return hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()


def cache_key(*, scope: str, kind: str, prompt_version: str, model: str,
              house_style: str, req_hash: str) -> str:
    raw = "|".join([scope, kind, prompt_version, model, house_style,
                    req_hash])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def tenant_scope(org_id: str | None, user_id: str | None) -> str | None:
    """The cache's tenant: the org, else the user, else none at all."""
    if org_id:
        return str(org_id)
    if user_id:
        return f"u:{user_id}"
    return None


def _house_style_hash() -> str:
    try:
        from engine import glossary
        return glossary.house_style_hash()
    except Exception as exc:  # pragma: no cover — defensive
        _logger.warning("llm_cache: house style hash failed: %s", exc)
        return "unknown"


def _routed_model(kind: str, org_id: str | None,
                  user_id: str | None) -> str:
    """The model ``call_messages`` would route *kind* to for this caller."""
    from engine import llm_keys, llm_models
    try:
        _key, key_source = llm_keys.resolve_key(org_id)
    except Exception:  # pragma: no cover — mirrors call_messages
        key_source = "platform"
    return llm_models.model_for(kind, org_id=org_id, user_id=user_id,
                                key_source=key_source)


# ── Response shape ───────────────────────────────────────────────────

def response_text(resp: Any) -> str:
    chunks: list[str] = []
    for block in getattr(resp, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            chunks.append(text)
    return "\n".join(chunks).strip()


def _usage_dict(resp: Any) -> dict | None:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return None
    out = {}
    for name in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                 "cache_creation_input_tokens"):
        try:
            out[name] = int(getattr(usage, name, 0) or 0)
        except (TypeError, ValueError):
            out[name] = 0
    return out


def _cached_response(text: str, model: str) -> Any:
    """A stand-in for an SDK response, shaped like the parts callers read.

    ``usage`` is all zeros because nothing was spent; ``from_cache`` lets a
    caller (or a test) tell the two apart.
    """
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(input_tokens=0, output_tokens=0,
                              cache_read_input_tokens=0,
                              cache_creation_input_tokens=0),
        model=model,
        stop_reason="end_turn",
        from_cache=True,
    )


# ── The call ─────────────────────────────────────────────────────────

def cached_call(kind: str, *, prompt_version: str,
                org_id: str | None = None,
                bypass_cache: bool = False,
                accept: Callable[[str], bool] | None = None,
                call: Callable[..., Any] | None = None,
                **call_kwargs: Any) -> Any:
    """``call_messages(kind=kind, **call_kwargs)``, answered from the cache
    when an identical request was answered before for the same tenant.

    Parameters
    ----------
    kind:
        The work kind (:mod:`engine.llm_models`). ``consult`` and
        ``search_answer`` raise :class:`ValueError`.
    prompt_version:
        The caller's ``_PROMPT_VERSION``.
    org_id:
        Explicit tenant; otherwise taken from :mod:`engine.llm_context`.
    bypass_cache:
        Regenerate: skip the read, call the model, replace the stored row.
    accept:
        Predicate over the response text; only an accepted response is
        stored. Callers pass their parser, so a reply they would reject
        (truncated JSON, say) is not served back for thirty days.
    call:
        The function to call on a miss — the caller's own module-level
        ``call_messages``, looked up at call time, so a test that
        monkeypatches it there still intercepts. Defaults to
        :func:`engine.llm_client.call_messages`.

    Returns the SDK response on a miss and a response-shaped stand-in on a
    hit. Exceptions from the call propagate unchanged.
    """
    if kind in UNCACHEABLE_KINDS:
        raise ValueError(f"llm_cache: kind {kind!r} is never cached here")
    if call is None:
        from engine.llm_client import call_messages as call

    from engine import llm_context
    ctx_org, ctx_project, ctx_user = llm_context.fill(
        org_id, call_kwargs.get("project_id"), call_kwargs.get("user_id"))
    scope = tenant_scope(ctx_org, ctx_user)

    key = None
    model = call_kwargs.get("model") or ""
    if scope:
        try:
            model = model or _routed_model(kind, ctx_org, ctx_user)
            key = cache_key(scope=scope, kind=kind,
                            prompt_version=str(prompt_version),
                            model=model, house_style=_house_style_hash(),
                            req_hash=request_hash(call_kwargs))
        except Exception as exc:
            # An undeclared kind or a routing failure: no cache, and let the
            # call itself raise whatever it raises.
            _logger.debug("llm_cache: no key for %s: %s", kind, exc)
            key = None

    if key and not bypass_cache:
        from engine import db as _db
        row = _db.get_llm_result_cache(key, scope=scope)
        if row is not None:
            with _STATS_LOCK:
                _HITS[kind] += 1
                total = sum(_HITS.values())
            _logger.info("llm_cache hit kind=%s hits=%d (process total %d)",
                         kind, int(row.get("hits") or 0), total)
            return _cached_response(row["response_text"], row["model"])

    if org_id is not None:
        call_kwargs["org_id"] = org_id
    resp = call(kind=kind, **call_kwargs)

    if key:
        with _STATS_LOCK:
            _MISSES[kind] += 1
        text = response_text(resp)
        truncated = getattr(resp, "stop_reason", None) == "max_tokens"
        ok = bool(text) and not truncated
        if ok and accept is not None:
            try:
                ok = bool(accept(text))
            except Exception:
                ok = False
        if ok:
            from engine import db as _db
            _db.put_llm_result_cache(
                key=key, scope=scope, kind=kind,
                prompt_version=str(prompt_version),
                model=model,
                response_text=text, usage=_usage_dict(resp),
                project_id=ctx_project)
    return resp


__all__ = ["cached_call", "cache_key", "request_hash", "tenant_scope",
           "response_text", "stats", "reset_stats", "UNCACHEABLE_KINDS"]
