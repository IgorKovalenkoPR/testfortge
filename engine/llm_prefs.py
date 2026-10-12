"""TestFortge — the model switcher's policy and preferences (ADR 0003 §4.7).

Two things are stored, both as **tiers**, never as raw model ids — a user
must not be able to type an arbitrary model string into a request that
the operator pays for:

* the org policy, ``Organization.settings["llm_models"]``::

    {"default_tier": "haiku", "per_kind": {"authoring": "sonnet"},
     "allowed_tiers": ["haiku", "sonnet"], "users_may_choose": true}

* the user's preference, ``UserSetting(owner=user_id, key="llm_model_pref")``::

    {"*": "sonnet", "consult": "opus"}

The switcher is live only when ``MODEL_SWITCHER`` is effective (which
requires ``ORG_MODE``). Off, :func:`effective_tier` answers ``None`` without
touching the database and routing is env + ``KIND_TIER`` as before.

Money
-----
Sonnet and Opus on the **platform** key are refused unless
``LLM_ALLOW_PREMIUM_ON_PLATFORM_KEY`` is on (default off): the operator
pays for those. A **BYOK** org may use any tier its admin allows. A
refused tier is skipped, not substituted: the next candidate in the
precedence chain answers instead.

A policy nobody has saved allows Haiku only. Conservative on purpose — an
org's own key is still the org's money, and an admin should open Sonnet
and Opus deliberately rather than find users already on them.

Caching
-------
Org policies and user preferences are cached for :data:`CACHE_SECONDS`
per process so routing does not add queries to every LLM call. Saving
either through this module invalidates its entry at once; with one
gunicorn worker (``WEB_CONCURRENCY=1``) that is exact, with more it is
stale for at most the TTL.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

from engine.log import get_logger

log = get_logger(__name__)

POLICY_KEY = "llm_models"
USER_PREF_KEY = "llm_model_pref"
PREMIUM_ENV = "LLM_ALLOW_PREMIUM_ON_PLATFORM_KEY"
CACHE_SECONDS = 30.0

#: The policy an org has before its admin saves one.
DEFAULT_POLICY: dict[str, Any] = {
    "default_tier": None,
    "per_kind": {},
    "allowed_tiers": ["haiku"],
    "users_may_choose": True,
}

#: Why a tier cannot be chosen. Keys into the i18n dictionaries
#: (``ms_reason_<code>``) so the UI and the API name the same reasons.
REASON_ORG = "org"                # not in the org's allowed_tiers / users may not choose
REASON_PINNED = "pinned"          # LLM_MODEL_FOR_<KIND> or ANTHROPIC_MODEL
REASON_PREMIUM = "premium_key"    # Sonnet/Opus on the platform key
REASON_OFF = "off"                # MODEL_SWITCHER is off


# ── Flags and env ─────────────────────────────────────────────────

def switcher_on() -> bool:
    from engine import features
    return features.effective("MODEL_SWITCHER")


def premium_on_platform_allowed() -> bool:
    """``LLM_ALLOW_PREMIUM_ON_PLATFORM_KEY``, parsed the way flags are."""
    raw = (os.environ.get(PREMIUM_ENV) or "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


# ── Cache ─────────────────────────────────────────────────────────

_cache: dict[tuple[str, str], tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cached(kind: str, key: str, loader) -> Any:
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get((kind, key))
        if hit is not None and now - hit[0] < CACHE_SECONDS:
            return hit[1]
    value = loader()
    with _cache_lock:
        _cache[(kind, key)] = (now, value)
    return value


def invalidate(*, org_id: str | None = None, user_id: str | None = None) -> None:
    with _cache_lock:
        if org_id:
            _cache.pop(("org", org_id), None)
        if user_id:
            _cache.pop(("user", user_id), None)


def clear_cache() -> None:
    """For tests."""
    with _cache_lock:
        _cache.clear()


# ── Validation ────────────────────────────────────────────────────

def _tiers():
    from engine import llm_models
    return llm_models.TIERS


def _switchable():
    from engine import llm_models
    return llm_models.SWITCHABLE_KINDS


def normalise_policy(raw: Any) -> dict[str, Any]:
    """A stored policy with every field present and every value valid.

    Unknown tiers and kinds are dropped rather than raising: a policy
    written by an older build must not take routing down.
    """
    tiers = _tiers()
    out = {"default_tier": None, "per_kind": {},
           "allowed_tiers": list(DEFAULT_POLICY["allowed_tiers"]),
           "users_may_choose": True}
    if not isinstance(raw, dict):
        return out
    allowed = raw.get("allowed_tiers")
    if isinstance(allowed, (list, tuple)):
        out["allowed_tiers"] = [t for t in tiers if t in allowed]
    if not out["allowed_tiers"]:
        # An empty allow-list would make every org setting unreachable;
        # Haiku is the floor every kind falls back to anyway.
        out["allowed_tiers"] = ["haiku"]
    dt = raw.get("default_tier")
    if dt in tiers:
        out["default_tier"] = dt
    per_kind = raw.get("per_kind")
    if isinstance(per_kind, dict):
        out["per_kind"] = {k: v for k, v in per_kind.items()
                           if k in _switchable() and v in tiers}
    out["users_may_choose"] = bool(raw.get("users_may_choose", True))
    return out


def validate_policy(default_tier: str | None, per_kind: dict[str, str],
                    allowed_tiers: list[str],
                    users_may_choose: bool) -> dict[str, Any]:
    """Build a policy from an admin's form, raising ``ValueError`` with a
    message key (``ms_err_*``) for what is wrong."""
    tiers = _tiers()
    allowed = [t for t in tiers if t in (allowed_tiers or [])]
    if any(t not in tiers for t in (allowed_tiers or [])):
        raise ValueError("ms_err_unknown_tier")
    if not allowed:
        raise ValueError("ms_err_no_tiers")
    if default_tier and default_tier not in allowed:
        raise ValueError("ms_err_default_not_allowed")
    clean_kind: dict[str, str] = {}
    for kind, tier in (per_kind or {}).items():
        if not tier:
            continue
        if kind not in _switchable():
            raise ValueError("ms_err_unknown_kind")
        if tier not in allowed:
            raise ValueError("ms_err_kind_not_allowed")
        clean_kind[kind] = tier
    return {"default_tier": default_tier or None, "per_kind": clean_kind,
            "allowed_tiers": allowed,
            "users_may_choose": bool(users_may_choose)}


def validate_user_pref(raw: Any) -> dict[str, str]:
    """``{"*" | kind: tier}``, refusing anything else with ``ValueError``.

    An empty value clears that entry. Whether the org *permits* the tier
    is not checked here — the policy can change after the preference is
    saved, so it is checked at routing time, where it has to be anyway.
    """
    if not isinstance(raw, dict):
        raise ValueError("ms_err_bad_pref")
    tiers = _tiers()
    out: dict[str, str] = {}
    for key, tier in raw.items():
        if key != "*" and key not in _switchable():
            raise ValueError("ms_err_unknown_kind")
        if tier in (None, ""):
            continue
        if tier not in tiers:
            raise ValueError("ms_err_unknown_tier")
        out[key] = tier
    return out


# ── Reads and writes ──────────────────────────────────────────────

def org_policy(org_id: str | None) -> dict[str, Any]:
    if not org_id:
        return normalise_policy(None)

    def _load():
        try:
            from engine import db as _db
            org = _db.get_organization(org_id) or {}
            return normalise_policy((org.get("settings") or {}).get(POLICY_KEY))
        except Exception as exc:  # pragma: no cover — routing never fails
            log.debug("org policy unavailable for %s: %s", org_id[:8], exc)
            return normalise_policy(None)
    return _cached("org", org_id, _load)


def save_org_policy(org_id: str, policy: dict[str, Any]) -> bool:
    from engine import db as _db
    ok = _db.update_org_settings(org_id, {POLICY_KEY: policy})
    invalidate(org_id=org_id)
    return ok


def user_pref(user_id: str | None) -> dict[str, str]:
    if not user_id:
        return {}

    def _load():
        try:
            from engine import db as _db
            raw = _db.get_user_setting(user_id, USER_PREF_KEY)
            return validate_user_pref(raw) if raw else {}
        except ValueError:
            return {}
        except Exception as exc:  # pragma: no cover — routing never fails
            log.debug("user pref unavailable for %s: %s", user_id[:8], exc)
            return {}
    return _cached("user", user_id, _load)


def save_user_pref(user_id: str, pref: dict[str, str]) -> None:
    from engine import db as _db
    _db.set_user_setting(user_id, USER_PREF_KEY, pref)
    invalidate(user_id=user_id)


# ── The decision ──────────────────────────────────────────────────

def _key_source(org_id: str | None, key_source: str | None) -> str:
    if key_source:
        return key_source
    try:
        from engine import llm_keys
        return llm_keys.resolve_key(org_id)[1]
    except Exception:  # pragma: no cover — assume the operator pays
        return "platform"


def premium_permitted(org_id: str | None, key_source: str | None = None) -> bool:
    """Sonnet/Opus are affordable for this org: its own key, or the
    operator opted in to paying for them."""
    return (_key_source(org_id, key_source) == "org"
            or premium_on_platform_allowed())


def _tier_permitted(tier: str, policy: dict, org_id: str | None,
                    key_source: str | None) -> bool:
    from engine import llm_models
    if tier not in policy["allowed_tiers"]:
        return False
    if tier in llm_models.PREMIUM_TIERS:
        return premium_permitted(org_id, key_source)
    return True


def effective_tier(kind: str, *, org_id: str | None = None,
                   user_id: str | None = None,
                   key_source: str | None = None) -> str | None:
    """The tier the switcher picks for *kind*, or ``None`` to defer to the
    environment (steps 3–4 of :mod:`engine.llm_models`' precedence).

    Candidates in order: the user's per-kind choice, the user's ``*``
    choice (both only when the org lets users choose), the org's
    ``per_kind``, the org's ``default_tier``. The first one the org allows
    *and* the key can pay for wins.
    """
    if kind not in _switchable() or not org_id or not switcher_on():
        return None
    policy = org_policy(org_id)
    candidates: list[str] = []
    if user_id and policy["users_may_choose"]:
        pref = user_pref(user_id)
        candidates += [t for t in (pref.get(kind), pref.get("*")) if t]
    candidates += [t for t in (policy["per_kind"].get(kind),
                               policy["default_tier"]) if t]
    for tier in candidates:
        if _tier_permitted(tier, policy, org_id, key_source):
            return tier
    return None


def switcher_state(*, org_id: str | None, user_id: str | None,
                   kind: str = "*") -> dict[str, Any]:
    """What the UI control needs: whether it is usable, the current
    choice, and each tier with the reason it cannot be chosen, if any.

    ``kind="*"`` describes the all-kinds control; the reported current
    tier is what ``consult`` (Tedgie, the highest-volume switchable kind)
    resolves to.
    """
    from engine import llm_models
    probe = "consult" if kind == "*" else kind
    pinned = bool(llm_models.legacy_global_pin()
                  or llm_models.operator_pin(probe))
    pinned_kinds = sorted(k for k in llm_models.SWITCHABLE_KINDS
                          if llm_models.operator_pin(k))
    on = switcher_on() and bool(org_id) and bool(user_id)
    policy = org_policy(org_id) if on else normalise_policy(None)
    pref = user_pref(user_id) if on else {}
    key_source = _key_source(org_id, None) if on else "platform"
    options = []
    for tier in llm_models.TIERS:
        reason = None
        if not on:
            reason = REASON_OFF
        elif pinned:
            reason = REASON_PINNED
        elif not policy["users_may_choose"] or tier not in policy["allowed_tiers"]:
            reason = REASON_ORG
        elif (tier in llm_models.PREMIUM_TIERS
              and not premium_permitted(org_id, key_source)):
            reason = REASON_PREMIUM
        options.append({"tier": tier, "disabled": reason is not None,
                        "reason": reason})
    effective = llm_models.tier_for(probe, org_id=org_id, user_id=user_id,
                                    key_source=key_source) if on else \
        llm_models.env_tier(probe)
    return {
        "enabled": on and not pinned,
        "on": on,
        "pinned": pinned,
        "pinned_kinds": pinned_kinds,
        "kind": kind,
        "selected": pref.get(kind) or "",
        "effective_tier": effective,
        "effective_model": (llm_models.operator_pin(probe)
                            or llm_models.model_for_tier(effective)),
        "users_may_choose": policy["users_may_choose"],
        "allowed_tiers": list(policy["allowed_tiers"]),
        "key_source": key_source,
        "options": options,
    }


__all__ = [
    "POLICY_KEY", "USER_PREF_KEY", "PREMIUM_ENV", "CACHE_SECONDS",
    "DEFAULT_POLICY", "REASON_ORG", "REASON_PINNED", "REASON_PREMIUM",
    "REASON_OFF",
    "switcher_on", "premium_on_platform_allowed", "invalidate", "clear_cache",
    "normalise_policy", "validate_policy", "validate_user_pref",
    "org_policy", "save_org_policy", "user_pref", "save_user_pref",
    "premium_permitted", "effective_tier", "switcher_state",
]
