"""TestFortge — what a call cost, and whether the org may make another (E0.7).

Two jobs:

* price a response's token usage in micro-dollars;
* answer "is this organisation over its monthly budget".

Money is integer **micro-dollars** (1e-6 USD) throughout. Floats are fine
for one call and wrong for a month of them — summing a hundred thousand
rounded floats drifts, and this number ends up in front of whoever pays.

Prices
------
Per million tokens, as of 2026-08. Verify at anthropic.com/pricing before
quoting these to anyone; they are a moving target and this table is a
snapshot, not an authority.

Cache multipliers are Anthropic's published ones: a cache **read** costs
10% of the input rate, a cache **write** 125%. Both are counted separately
because collapsing them into "input" gets the cost wrong in both
directions at once — undercounting the writes and massively overcounting
the reads, which is exactly the case prompt caching creates most of.
"""
from __future__ import annotations

import os
from typing import Any, NamedTuple

from engine.log import get_logger
from engine.llm_models import tier_of

log = get_logger(__name__)

MICROS_PER_USD = 1_000_000


class Price(NamedTuple):
    """USD per million tokens."""
    input_per_mtok: float
    output_per_mtok: float


#: Tier → price. Keyed by tier rather than by model id so a dated model
#: (``claude-haiku-4-5-20251001``) does not need its own row, and a model
#: we have never seen still prices as its family.
PRICES: dict[str, Price] = {
    "haiku": Price(1.0, 5.0),
    "sonnet": Price(2.0, 10.0),
    "opus": Price(5.0, 25.0),
}

#: Used when a model's tier cannot be determined. Deliberately the most
#: expensive tier: an unknown model priced as Haiku would understate the
#: bill, and understating spend is the wrong direction to be wrong in.
UNKNOWN_TIER_FALLBACK = "opus"

_CACHE_READ_MULTIPLIER = 0.10
_CACHE_WRITE_MULTIPLIER = 1.25


class Usage(NamedTuple):
    """Token counts pulled off an Anthropic response."""
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


def extract_usage(response: Any) -> Usage:
    """Read token counts off an SDK response, tolerating their absence.

    Every field is optional and defaults to zero: a mocked response in a
    test, an older SDK, or a streaming wrapper may not carry all of them,
    and metering must never be the reason a generation fails.
    """
    usage = getattr(response, "usage", None)
    if usage is None:
        return Usage(0, 0, 0, 0)

    def _int(name: str) -> int:
        try:
            return max(0, int(getattr(usage, name, 0) or 0))
        except (TypeError, ValueError):
            return 0

    return Usage(
        input_tokens=_int("input_tokens"),
        output_tokens=_int("output_tokens"),
        cache_read_tokens=_int("cache_read_input_tokens"),
        cache_write_tokens=_int("cache_creation_input_tokens"),
    )


def price_for(model: str) -> Price:
    tier = tier_of(model)
    if tier is None or tier not in PRICES:
        log.debug("no price for model %r — pricing as %s",
                  model, UNKNOWN_TIER_FALLBACK)
        tier = UNKNOWN_TIER_FALLBACK
    return PRICES[tier]


def cost_micros(model: str, usage: Usage) -> int:
    """Price *usage* for *model*, in micro-dollars, rounded up.

    Rounded **up** so a long run of sub-micro-dollar calls cannot total
    zero. A thousand calls each rounding down to nothing is a meter that
    reads empty while the bill is real.
    """
    price = price_for(model)
    per_input_micro = price.input_per_mtok  # $/Mtok == micros/token
    per_output_micro = price.output_per_mtok

    total = (
        usage.input_tokens * per_input_micro
        + usage.output_tokens * per_output_micro
        + usage.cache_read_tokens * per_input_micro * _CACHE_READ_MULTIPLIER
        + usage.cache_write_tokens * per_input_micro * _CACHE_WRITE_MULTIPLIER
    )
    if total <= 0:
        return 0
    return max(1, int(total + 0.999999))


def format_usd(micros: int) -> str:
    """Micro-dollars as a human string. Cents when it is worth cents,
    four decimals when it is not — ``$0.00`` for a real cost is the kind
    of display that gets a meter mistrusted."""
    dollars = micros / MICROS_PER_USD
    if dollars >= 0.01 or dollars == 0:
        return f"${dollars:,.2f}"
    return f"${dollars:.4f}"


# ── Monthly budget ────────────────────────────────────────────────

#: Fallback monthly cap per organisation, in USD, when neither the org nor
#: the environment says otherwise.
_FALLBACK_BUDGET_USD = 5.0


def default_budget_usd() -> float:
    """The platform-wide monthly cap per org, from ``LLM_ORG_BUDGET_USD``.

    Read at call time, not at import. A cap captured at import cannot be
    changed without a redeploy and cannot be exercised by a test that sets
    the variable — the same reasoning as ``engine.features``. Set it to 0
    for no cap.

    A cap is not optional on a zero-budget platform: without one, a single
    team's runaway tab spends the operator's money with no ceiling. It does
    not apply to a BYOK org — that is their own key and their own bill.
    """
    raw = (os.environ.get("LLM_ORG_BUDGET_USD") or "").strip()
    if not raw:
        return _FALLBACK_BUDGET_USD
    try:
        return max(0.0, float(raw))
    except ValueError:
        log.warning("LLM_ORG_BUDGET_USD=%r is not a number — using $%s.",
                    raw, _FALLBACK_BUDGET_USD)
        return _FALLBACK_BUDGET_USD


def org_budget_micros(org_settings: dict | None) -> int:
    """The org's monthly cap in micros. 0 means unlimited.

    Read from ``organization.settings["llm_budget_usd"]`` so an admin can
    raise or remove it per team, falling back to the platform default.
    """
    fallback = default_budget_usd()
    raw = None
    if isinstance(org_settings, dict):
        raw = org_settings.get("llm_budget_usd")
    if raw is None:
        raw = fallback
    try:
        usd = float(raw)
    except (TypeError, ValueError):
        log.warning("llm_budget_usd=%r is not a number — using the default "
                    "of $%s.", raw, fallback)
        usd = fallback
    return max(0, int(usd * MICROS_PER_USD))


def budget_state(org_id: str | None, org_settings: dict | None = None,
                 *, key_source: str = "platform") -> dict:
    """Where an org stands against its cap.

    Returns ``{"limit_micros", "spent_micros", "over", "ratio"}``.

    A BYOK org is never over: ``key_source="org"`` short-circuits to
    unlimited, because capping somebody's spend on their own key would be
    a strange thing for a platform to do.
    """
    unlimited = {"limit_micros": 0, "spent_micros": 0,
                 "over": False, "ratio": 0.0}
    if not org_id or key_source == "org":
        return unlimited
    limit = org_budget_micros(org_settings)
    if limit <= 0:
        return unlimited
    from engine import db as _db
    spent = _db.org_spend_micros(org_id, key_source="platform")
    return {
        "limit_micros": limit,
        "spent_micros": spent,
        "over": spent >= limit,
        "ratio": (spent / limit) if limit else 0.0,
    }


__all__ = [
    "MICROS_PER_USD", "PRICES", "Price", "Usage",
    "extract_usage", "price_for", "cost_micros", "format_usd",
    "default_budget_usd", "org_budget_micros", "budget_state",
]
