"""TestFortge — which Claude model handles which kind of work (E0.8).

Before this module every call site resolved its own model out of the
environment, and seven of them had drifted onto three different pinned
defaults (``claude-sonnet-4-5``, ``claude-sonnet-4-6``, and whatever
``ANTHROPIC_MODEL`` happened to hold). Two consequences: nobody could
answer "what model is production using" without grepping, and the cheap
work paid the expensive model's price.

The model is chosen from the *kind of work*, not from the call site.

Cost, for the routing to be judged against — the platform runs on a zero
budget (``docs/plans/cost_model.md``), so this is the lever that matters
most after prompt caching:

* Sonnet 5   — $2 / $10 per million input / output tokens
* Haiku 4.5  — $1 / $5

Prices as of 2026-08; verify at anthropic.com/pricing before quoting them
to anyone.

Environment overrides, most specific first
------------------------------------------
1. ``LLM_MODEL_FOR_<KIND>``  — pin one kind, e.g. ``LLM_MODEL_FOR_CONSULT``
2. ``LLM_<KIND>_TIER``       — move one kind to another tier, e.g.
   ``LLM_CONSULT_TIER=haiku``
3. ``ANTHROPIC_MODEL_SONNET`` / ``ANTHROPIC_MODEL_HAIKU`` — repoint a tier
4. ``ANTHROPIC_MODEL``       — legacy global pin. Honoured, because
   deployments already set it, but it **disables routing entirely**;
   :func:`routing_warnings` says so at boot rather than leaving someone
   wondering why ``LLM_CONSULT_TIER`` did nothing.
"""
from __future__ import annotations

import os

from engine.log import get_logger

log = get_logger(__name__)

#: Tier → default model id.
TIER_DEFAULTS: dict[str, str] = {
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
}

#: Kind of work → tier.
#:
#: The split is by *what the answer has to be*, not by how important the
#: feature feels:
#:
#: * ``authoring`` writes prose a QA engineer signs their name to — test
#:   cases and checklist items in a measured house style. Sonnet.
#: * ``analysis`` reads a site or a spec and reasons about coverage.
#:   Sonnet.
#: * ``vision`` reads mockup images. Sonnet.
#: * ``segmentation`` extracts a short, schema-validated JSON shape from a
#:   recorded session. Haiku: the output is checked against a schema and
#:   there is a deterministic fallback if it fails, so a cheaper model is
#:   *verifiably* good enough rather than hopefully good enough.
#: * ``consult`` is Tedgie answering QA questions. Deliberately still
#:   Sonnet — see the note below.
KIND_TIER: dict[str, str] = {
    "authoring": "sonnet",
    "analysis": "sonnet",
    "vision": "sonnet",
    "segmentation": "haiku",
    "consult": "sonnet",
}

# Why ``consult`` is not on Haiku yet, even though it is the highest-volume
# kind and moving it would cut the LLM bill by roughly 45%:
#
# There is no way to tell today whether the answers got worse. Tedgie's
# mentoring answers are free prose, graded by whether a QA engineer trusts
# them, and the eval harness that would measure that is E6.7. Flipping the
# model first and building the measurement second is how a quality
# regression ships unnoticed and gets attributed to a prompt change three
# sprints later.
#
# So the routing is here, the lever is documented (LLM_CONSULT_TIER=haiku),
# and the default moves once E6.7 can show the golden-set score holding.

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


def model_for(kind: str) -> str:
    """Return the model id that should handle *kind*.

    Raises :class:`UnknownKind` for an undeclared kind.
    """
    if kind not in KIND_TIER:
        raise UnknownKind(
            f"{kind!r} is not a declared work kind. Declared: "
            f"{', '.join(KINDS)}. Add it to engine/llm_models.py."
        )

    per_kind = _env(f"LLM_MODEL_FOR_{kind.upper()}")
    if per_kind:
        return per_kind

    tier = _env(f"LLM_{kind.upper()}_TIER") or KIND_TIER[kind]
    if tier not in TIER_DEFAULTS:
        log.warning(
            "LLM_%s_TIER=%r is not a known tier (%s) — using %r.",
            kind.upper(), tier, ", ".join(sorted(TIER_DEFAULTS)),
            KIND_TIER[kind])
        tier = KIND_TIER[kind]

    per_tier = _env(f"ANTHROPIC_MODEL_{tier.upper()}")
    if per_tier:
        return per_tier

    legacy = legacy_global_pin()
    if legacy:
        return legacy

    return TIER_DEFAULTS[tier]


def tier_of(model: str) -> str | None:
    """Reverse lookup: which tier a model id belongs to, if any.

    Used by the usage meter to price a call. Matches on prefix so a
    dated id (``claude-haiku-4-5-20251001``) resolves the same as its
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


def routing_warnings() -> list[str]:
    """Boot-time warnings about configuration that does not do what it
    looks like it does."""
    out: list[str] = []
    legacy = legacy_global_pin()
    if legacy:
        out.append(
            f"ANTHROPIC_MODEL={legacy!r} pins every call to one model and "
            f"disables per-kind routing. Unset it and use "
            f"ANTHROPIC_MODEL_SONNET / ANTHROPIC_MODEL_HAIKU instead."
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
    """Kind → resolved model, for ops surfaces and the Guide."""
    return {kind: model_for(kind) for kind in sorted(KINDS)}


__all__ = [
    "TIER_DEFAULTS", "KIND_TIER", "KINDS", "UnknownKind",
    "model_for", "tier_of", "routing_warnings", "snapshot",
    "legacy_global_pin",
]
