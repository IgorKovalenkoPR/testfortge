"""TestFortge — which Claude model handles which kind of work (E0.8, ADR 0003 §4.7).

Before this module every call site resolved its own model out of the
environment, and seven of them had drifted onto three different pinned
defaults. Two consequences: nobody could answer "what model is production
using" without grepping, and the cheap work paid the expensive model's
price.

The model is chosen from the *kind of work*, not from the call site.

Defaults — the owner's decision (ADR 0003, 2026-10-12)
------------------------------------------------------
**Every kind runs on Haiku.** Sonnet and Opus are reachable only through
the model switcher (``MODEL_SWITCHER``, :mod:`engine.llm_prefs`) or an
operator override below. Prices, per million input / output tokens, as
published on 2026-10-06 (see :mod:`engine.llm_cost` for the table the
meter actually uses):

* Haiku 5.5  (``claude-haiku-5-5``)  — $0.10 / $0.50 (prompts ≤100K tokens)
* Sonnet 5.5 (``claude-sonnet-5-5``) — $2 / $10
* Opus 5.5   (``claude-opus-5-5``)   — $4 / $20

Precedence, most specific first
-------------------------------
1. ``LLM_MODEL_FOR_<KIND>`` — a **hard pin by the operator**. Beats
   everything, including the legacy pin below and every switcher setting;
   the switcher shows the kind as "pinned by operator".
2. ``ANTHROPIC_MODEL`` (legacy global pin) — while it is set the switcher
   is off entirely (no org or user setting applies), and the env layers
   below resolve exactly as they did before ADR 0003. :func:`routing_warnings`
   says so at boot.
3. The user's preference, when ``MODEL_SWITCHER`` is on, the kind is in
   :data:`SWITCHABLE_KINDS`, the org lets users choose and the tier is in
   the org's ``allowed_tiers``.
4. The org's ``per_kind`` tier, then its ``default_tier``.
5. ``LLM_<KIND>_TIER``.
6. :data:`KIND_TIER` (Haiku).

Steps 3–4 also pass the money rule in :func:`engine.llm_prefs.effective_tier`:
Sonnet and Opus on the *platform* key need
``LLM_ALLOW_PREMIUM_ON_PLATFORM_KEY``; a BYOK org may use any tier its admin
allows.

The tier is then mapped to a model through ``ANTHROPIC_MODEL_<TIER>``,
then the legacy ``ANTHROPIC_MODEL``, then :data:`TIER_DEFAULTS`.

Why the legacy pin sits where it does. ADR 0003 §4.7 lists it as
"overrides everything, as today", but today's code let
``LLM_MODEL_FOR_<KIND>`` and ``ANTHROPIC_MODEL_<TIER>`` win over it. This
module keeps today's env ordering exactly and adds the one thing the ADR
cares about: an org or user choice never beats it.
"""
from __future__ import annotations

import copy
import os

from engine.log import get_logger

log = get_logger(__name__)

#: Tier → default model id. Current Claude 5.5 family ids, checked against
#: Anthropic's model list on 2026-10-12. No date suffixes: these are the
#: complete ids.
TIER_DEFAULTS: dict[str, str] = {
    "haiku": "claude-haiku-5-5",
    "sonnet": "claude-sonnet-5-5",
    "opus": "claude-opus-5-5",
}

#: Tiers in ascending order of cost. Used by the UI to list them.
TIERS: tuple[str, ...] = ("haiku", "sonnet", "opus")

#: Tiers that cost the operator real money on the platform key. Gated by
#: ``LLM_ALLOW_PREMIUM_ON_PLATFORM_KEY`` — see :mod:`engine.llm_prefs`.
PREMIUM_TIERS: frozenset[str] = frozenset({"sonnet", "opus"})

#: Kind of work → tier. **Every kind is Haiku** (owner's decision,
#: 2026-10-12). The kinds stay separate because they are still the
#: dimension the usage report groups by and the switcher switches on:
#:
#: * ``authoring`` — test cases and checklist items in the house style;
#: * ``analysis`` — reading a site or a spec and reasoning about coverage;
#: * ``vision`` — reading mockup images;
#: * ``segmentation`` — short, schema-validated JSON from a recorded
#:   session, with a deterministic fallback;
#: * ``consult`` — Tedgie answering QA questions;
#: * ``search_answer`` — the short grounded answer above search results
#:   (ADR 0003 §4.6);
#: * ``query_rewrite`` — reserved, not called by anything yet.
KIND_TIER: dict[str, str] = {
    "authoring": "haiku",
    "analysis": "haiku",
    "vision": "haiku",
    "segmentation": "haiku",
    "consult": "haiku",
    "search_answer": "haiku",
    "query_rewrite": "haiku",
}

#: Kinds an org policy or a user preference may move off Haiku.
#: ``segmentation`` and ``query_rewrite`` stay put: their output is checked
#: against a schema, so a more expensive model costs more with no visible
#: benefit.
SWITCHABLE_KINDS: frozenset[str] = frozenset(
    {"consult", "search_answer", "authoring", "analysis", "vision"})

# Why ``consult`` is on Haiku now, when this comment used to explain why it
# was not.
#
# The old objection was that nothing could tell whether Tedgie's answers got
# worse, so flipping the model first and measuring second was how a quality
# regression would ship unnoticed. That measurement now exists: E6.7's golden
# set (engine/tedgie_eval.py, 105 items) gates CI at 100%/100%. The owner
# decided on 2026-10-12 (ADR 0003) that Haiku is the default for every kind,
# with the merge gate being a tedgie_eval run on the golden set recorded in
# the PR.
#
# Read that gate for what it is: run_rule_layer() scores the deterministic
# chain (fast path → project facts → rule fallback), which no model change
# can move. The LLM leg of Tedgie is only measured by a live run with a key.
# Until that run is recorded, the per-org escape hatch is the switcher
# (MODEL_SWITCHER: an admin can put ``consult`` back on Sonnet for their
# team) and the operator's is LLM_CONSULT_TIER=sonnet.

#: Kinds that must exist. A typo'd kind is a KeyError, for the same reason
#: a typo'd feature flag is (see engine/features.py).
KINDS: tuple[str, ...] = tuple(KIND_TIER)


class UnknownKind(KeyError):
    """Raised for a work kind that is not declared in :data:`KIND_TIER`."""


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def legacy_global_pin() -> str:
    """The value of the legacy ``ANTHROPIC_MODEL``, or ``""``."""
    return _env("ANTHROPIC_MODEL")


def operator_pin(kind: str) -> str:
    """``LLM_MODEL_FOR_<KIND>``, or ``""``. The switcher's "pinned by
    operator" state reads this."""
    return _env(f"LLM_MODEL_FOR_{kind.upper()}")


def _check_kind(kind: str) -> None:
    if kind not in KIND_TIER:
        raise UnknownKind(
            f"{kind!r} is not a declared work kind. Declared: "
            f"{', '.join(KINDS)}. Add it to engine/llm_models.py."
        )


def env_tier(kind: str) -> str:
    """The tier the environment gives *kind*: ``LLM_<KIND>_TIER`` if it
    names a real tier, else :data:`KIND_TIER`."""
    tier = _env(f"LLM_{kind.upper()}_TIER") or KIND_TIER[kind]
    if tier not in TIER_DEFAULTS:
        log.warning(
            "LLM_%s_TIER=%r is not a known tier (%s) — using %r.",
            kind.upper(), tier, ", ".join(sorted(TIER_DEFAULTS)),
            KIND_TIER[kind])
        tier = KIND_TIER[kind]
    return tier


def model_for_tier(tier: str) -> str:
    """Tier → model id: ``ANTHROPIC_MODEL_<TIER>``, the legacy pin, then
    :data:`TIER_DEFAULTS`."""
    per_tier = _env(f"ANTHROPIC_MODEL_{tier.upper()}")
    if per_tier:
        return per_tier
    legacy = legacy_global_pin()
    if legacy:
        return legacy
    return TIER_DEFAULTS[tier]


def tier_for(kind: str, *, org_id: str | None = None,
             user_id: str | None = None,
             key_source: str | None = None) -> str:
    """The tier *kind* resolves to for this org and user (steps 2–6 of the
    module docstring). Ignores the operator pin, which names a model, not
    a tier — :func:`model_for` handles it."""
    _check_kind(kind)
    if not legacy_global_pin() and kind in SWITCHABLE_KINDS:
        from engine import llm_prefs
        chosen = llm_prefs.effective_tier(
            kind, org_id=org_id, user_id=user_id, key_source=key_source)
        if chosen:
            return chosen
    return env_tier(kind)


def model_for(kind: str, *, org_id: str | None = None,
              user_id: str | None = None,
              key_source: str | None = None) -> str:
    """Return the model id that should handle *kind*.

    ``org_id`` and ``user_id`` feed the model switcher; without them, or
    with ``MODEL_SWITCHER`` off, routing is env + :data:`KIND_TIER` exactly
    as before. ``key_source`` (``"org"``/``"platform"``/``"none"``) lets a
    caller that already resolved the key spare the switcher a second
    lookup.

    Raises :class:`UnknownKind` for an undeclared kind.
    """
    _check_kind(kind)
    pinned = operator_pin(kind)
    if pinned:
        return pinned
    return model_for_tier(tier_for(kind, org_id=org_id, user_id=user_id,
                                   key_source=key_source))


def tier_of(model: str) -> str | None:
    """Reverse lookup: which tier a model id belongs to, if any.

    Matches on the family name so a dated id resolves the same as its
    alias, and returns ``None`` for anything unrecognised rather than
    guessing — an unknown model priced as Haiku would understate the
    bill, which is the wrong direction to be wrong in.
    """
    if not model:
        return None
    m = model.lower()
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    if "opus" in m:
        return "opus"
    return None


#: Family → the request fields that keep a short-capped call from spending
#: its whole ``max_tokens`` on thinking. Haiku 5.5 thinks by default and
#: its thinking is billed as output but never streamed as text: with the
#: 300-token Ask cap every answer came back empty (live, 2026-10-14).
#: Matched by the longest prefix, so ``claude-opus-5-5`` is not read as
#: ``claude-opus-5``. Per family, from the API's thinking table:
#:
#: * Haiku 5.5 / Opus 5 / Sonnet 5 accept ``disabled`` (at effort ≤ high,
#:   and their default effort is ≤ high);
#: * Sonnet 5.5 rejects ``disabled``; ``between_tools`` is its off switch;
#: * Opus 5.5 and Fable cannot turn thinking off — low effort is the most
#:   that can be asked;
#: * anything older does not think unless asked, so nothing is sent.
_NO_THINKING: dict[str, dict] = {
    "claude-haiku-5-5": {"thinking": {"type": "disabled"}},
    "claude-sonnet-5-5": {"thinking": {"type": "between_tools"}},
    "claude-sonnet-5": {"thinking": {"type": "disabled"}},
    "claude-opus-5-5": {"output_config": {"effort": "low"}},
    "claude-opus-5": {"thinking": {"type": "disabled"}},
    "claude-fable-5": {"output_config": {"effort": "low"}},
    "claude-mythos-5": {"output_config": {"effort": "low"}},
}


def short_answer_params(model: str) -> dict:
    """Extra request fields for a call whose ``max_tokens`` is sized for
    the visible answer alone (Ask, the streamed Tedgie chat).

    Returns a fresh dict for ``**`` into ``messages.stream``; empty for a
    model that does not think by default or that is not recognised.
    """
    m = (model or "").lower()
    best = ""
    for prefix in _NO_THINKING:
        if m.startswith(prefix) and len(prefix) > len(best):
            best = prefix
    return copy.deepcopy(_NO_THINKING[best]) if best else {}


def routing_warnings() -> list[str]:
    """Boot-time warnings about configuration that does not do what it
    looks like it does."""
    out: list[str] = []
    legacy = legacy_global_pin()
    if legacy:
        out.append(
            f"ANTHROPIC_MODEL={legacy!r} pins every call to one model, "
            f"disables per-kind routing and the model switcher. Unset it "
            f"and use ANTHROPIC_MODEL_HAIKU / _SONNET / _OPUS instead."
        )
    for kind in KINDS:
        tier = _env(f"LLM_{kind.upper()}_TIER")
        if tier and tier not in TIER_DEFAULTS:
            out.append(
                f"LLM_{kind.upper()}_TIER={tier!r} is not a known tier; "
                f"{kind} stays on {KIND_TIER[kind]}."
            )
    return out


def snapshot() -> dict[str, str]:
    """Kind → resolved model with no org or user, for ops surfaces and the
    Guide."""
    return {kind: model_for(kind) for kind in sorted(KINDS)}


__all__ = [
    "TIER_DEFAULTS", "TIERS", "PREMIUM_TIERS", "KIND_TIER", "KINDS",
    "SWITCHABLE_KINDS", "UnknownKind",
    "model_for", "model_for_tier", "tier_for", "env_tier", "operator_pin",
    "tier_of", "short_answer_params", "routing_warnings", "snapshot",
    "legacy_global_pin",
]
