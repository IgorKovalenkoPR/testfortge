"""TestFortge — per-organisation Anthropic keys, encrypted at rest (E0.9).

BYOK — bring your own key — is what makes a $0 platform budget honest
rather than aspirational. Hosting can genuinely be free
(``docs/plans/cost_model.md``); the Anthropic API cannot. If each team
supplies its own key, the platform owner pays nothing for LLM work, the
team sees its own spend in its own Anthropic billing, and the team's
prompts go to the team's own account — which is a shorter answer to the
data-processing question than any DPA.

Resolution order for a call
---------------------------
1. the calling organisation's own key, if it has one;
2. the platform key (``ANTHROPIC_API_KEY``), if the operator set one;
3. nothing — the caller raises ``LLMUnavailable`` and falls through to the
   deterministic engine.

Step 3 matters more than it looks. A large part of TestFortge works with
no API calls at all: the rule engines (``tc_rules``, ``checklist_rules``),
the YAML knowledge packs, and a 2 826-chunk ISTQB corpus. "No key" is a
degraded platform, not a broken one, and every existing caller already
catches ``LLMUnavailable`` for exactly this reason.

Encryption
----------
Fernet (AES-128-CBC + HMAC-SHA256) with a key from
``TESTFORTGE_ENCRYPTION_KEY``.

Deliberately a *separate* env var rather than something derived from
``SECRET_KEY``. Deriving would be less configuration and a worse
operation: ``SECRET_KEY`` is the cookie-signing key, and the day someone
rotates it to invalidate sessions — a normal, correct thing to do — every
stored API key would silently become undecryptable. Two secrets with two
lifecycles.

Without ``TESTFORTGE_ENCRYPTION_KEY`` set, BYOK is off: storing a key is
refused rather than stored in the clear, and the platform key is used.
"""
from __future__ import annotations

import os

from engine.log import get_logger

log = get_logger(__name__)

#: The env var holding the Fernet key. Generate one with::
#:
#:     python -c "from cryptography.fernet import Fernet; \
#:                print(Fernet.generate_key().decode())"
ENCRYPTION_KEY_ENV = "TESTFORTGE_ENCRYPTION_KEY"

#: Anthropic keys start with this. Checked so a user who pastes the wrong
#: string finds out when they save it, not on their next generation run.
_KEY_PREFIX = "sk-ant-"

#: Shortest plausible key. Not a validation of correctness — only the API
#: can say that — just a guard against saving an obvious accident.
_KEY_MIN_LEN = 40


class BYOKUnavailable(RuntimeError):
    """Raised when a key cannot be stored because encryption is not
    configured. Never raised on the *read* path — a read failure degrades
    to the platform key instead of breaking the request."""


def is_configured() -> bool:
    """True when the platform can encrypt, i.e. BYOK is available."""
    return bool((os.environ.get(ENCRYPTION_KEY_ENV) or "").strip())


def _derive_fernet_key(secret: str) -> bytes:
    """Turn an arbitrary high-entropy secret into a valid Fernet key.

    Fernet demands exactly 32 url-safe-base64 bytes. Render's
    ``generateValue: true`` — the mechanism that keeps this secret out of
    the repository, the same way ``SECRET_KEY`` is handled — produces a
    random string in no particular format. Requiring the operator to run
    ``Fernet.generate_key()`` by hand and paste the result would mean the
    secret exists in a clipboard and a terminal history, which is a worse
    trade than deriving one.

    Plain SHA-256 rather than a password KDF on purpose: the input is a
    machine-generated random secret, not a human-chosen password, so
    there is no low-entropy guess space for iteration count to defend.
    Stretching it would cost startup time and buy nothing.
    """
    import base64
    import hashlib
    return base64.urlsafe_b64encode(
        hashlib.sha256(secret.encode("utf-8")).digest())


def _fernet():
    """Build a Fernet from the environment, or raise BYOKUnavailable.

    Accepts either a real Fernet key or any other secret, which is
    derived into one — see :func:`_derive_fernet_key`.
    """
    raw = (os.environ.get(ENCRYPTION_KEY_ENV) or "").strip()
    if not raw:
        raise BYOKUnavailable(
            f"{ENCRYPTION_KEY_ENV} is not set, so a customer API key "
            f"cannot be encrypted. Refusing to store it in plaintext."
        )
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover — hard dependency
        raise BYOKUnavailable(
            f"the cryptography package is not importable: {exc}"
        ) from exc
    try:
        return Fernet(raw.encode("utf-8"))
    except Exception:
        # Not a Fernet key — derive one. Deterministic, so keys stored
        # under a derived envelope stay readable across restarts.
        pass
    if len(raw) < 32:
        # A short secret would produce a valid-looking key with far less
        # entropy than it appears to have. Say so instead of encrypting
        # customer credentials under something guessable.
        raise BYOKUnavailable(
            f"{ENCRYPTION_KEY_ENV} is too short ({len(raw)} chars). Use at "
            f"least 32 characters of random data, or a key from "
            f"cryptography.fernet.Fernet.generate_key()."
        )
    try:
        return Fernet(_derive_fernet_key(raw))
    except Exception as exc:  # pragma: no cover — defensive
        raise BYOKUnavailable(
            f"{ENCRYPTION_KEY_ENV} could not be turned into an encryption "
            f"key: {exc}"
        ) from exc


def validate_key_shape(api_key: str) -> str | None:
    """Return an error message for an obviously-wrong key, else ``None``.

    Shape only. Whether the key *works* is a question for Anthropic, and
    asking them would mean an API call on a settings form.
    """
    key = (api_key or "").strip()
    if not key:
        return "The API key is empty."
    if not key.startswith(_KEY_PREFIX):
        return (f"An Anthropic API key starts with '{_KEY_PREFIX}'. Check "
                f"you pasted the key and not the key's name.")
    if len(key) < _KEY_MIN_LEN:
        return "That key looks truncated — check it copied in full."
    if any(ch.isspace() for ch in key):
        return "The key contains whitespace — it may have wrapped on copy."
    return None


def set_org_key(org_id: str, api_key: str) -> None:
    """Encrypt and store *api_key* for *org_id*.

    Raises :class:`BYOKUnavailable` when encryption is not configured, and
    ``ValueError`` when the key's shape is obviously wrong.
    """
    if not org_id:
        raise ValueError("org_id is required")
    problem = validate_key_shape(api_key)
    if problem:
        raise ValueError(problem)
    token = _fernet().encrypt(api_key.strip().encode("utf-8")).decode("ascii")
    from engine import db as _db
    _db.set_org_secret(org_id, "anthropic_api_key", token)
    log.info("BYOK key stored for org=%s", org_id[:8])


def clear_org_key(org_id: str) -> bool:
    """Forget an org's key. Falls back to the platform key afterwards."""
    if not org_id:
        return False
    from engine import db as _db
    removed = _db.delete_org_secret(org_id, "anthropic_api_key")
    if removed:
        log.info("BYOK key cleared for org=%s", org_id[:8])
    return removed


def get_org_key(org_id: str | None) -> str | None:
    """Decrypt and return the org's key, or ``None``.

    ``None`` for every failure mode — no org, no stored key, encryption
    not configured, ciphertext unreadable. Callers treat it as "this org
    has no key of its own" and fall back. A decryption failure is logged
    loudly because it usually means ``TESTFORTGE_ENCRYPTION_KEY`` was
    rotated without re-entering the keys, and the org needs telling.
    """
    if not org_id or not is_configured():
        return None
    from engine import db as _db
    try:
        token = _db.get_org_secret(org_id, "anthropic_api_key")
    except Exception as exc:
        log.warning("BYOK lookup failed for org=%s: %s", org_id[:8], exc)
        return None
    if not token:
        return None
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except Exception as exc:
        log.error(
            "BYOK key for org=%s could not be decrypted (%s). If "
            "%s was rotated, the org must re-enter its key.",
            org_id[:8], type(exc).__name__, ENCRYPTION_KEY_ENV)
        return None


def platform_key() -> str | None:
    """The operator's own key, or ``None``."""
    return (os.environ.get("ANTHROPIC_API_KEY") or "").strip() or None


def resolve_key(org_id: str | None = None) -> tuple[str | None, str]:
    """Return ``(api_key, source)`` for a call on behalf of *org_id*.

    ``source`` is ``"org"``, ``"platform"`` or ``"none"`` — carried
    through to the usage meter so a bill can be attributed to whoever
    actually paid it.
    """
    org_key = get_org_key(org_id)
    if org_key:
        return org_key, "org"
    plat = platform_key()
    if plat:
        return plat, "platform"
    return None, "none"


def redact(api_key: str | None) -> str:
    """A key rendered safe for a log line or a settings form.

    Shows the last four characters only — enough for a human to confirm
    *which* key is stored, useless to anyone who reads the log.
    """
    key = (api_key or "").strip()
    if not key:
        return "—"
    return f"…{key[-4:]}" if len(key) > 4 else "…"


__all__ = [
    "ENCRYPTION_KEY_ENV", "BYOKUnavailable",
    "is_configured", "validate_key_shape",
    "set_org_key", "clear_org_key", "get_org_key",
    "platform_key", "resolve_key", "redact",
]
