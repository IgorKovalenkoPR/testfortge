"""TestFortge — Google sign-in over OIDC (E1.4).

Authorization Code flow with PKCE, via Authlib. Not implicit: Google
retired it for web apps, and it put tokens in a URL fragment where the
browser history could keep them.

Authlib owns the parts that are easy to get subtly wrong — ``state``,
``nonce``, the PKCE verifier, the id_token signature, ``iss``/``aud``, and
expiry. What lives here is the part Authlib cannot decide: **which local
account a set of Google claims is allowed to become.** That is the
security-critical decision in an OIDC integration, and it is written as a
pure function (:func:`decide`) so it can be tested exhaustively without a
network or a browser.

The rule, and why
-----------------
Registration is invite-only (owner's decision, recorded in
``docs/plans/team_platform_architecture.md`` §5.1). Google sign-in is a
*sign-in* mechanism, not a side door around that:

1. Claims match an existing ``identity(provider='google', subject=sub)``
   → sign that user in. Keyed on ``sub``, never on email: Google
   documents ``sub`` as stable and email as changeable, so keying on
   email means a user who renames their Google account arrives as a
   stranger.
2. No identity, but a local account exists with the same address **and
   Google says that address is verified** → link and sign in.
3. No identity, no local account, but the flow carries a live invite
   → create the account, link, join.
4. Anything else → refuse. No auto-provisioning.

Step 2 is where the interesting attack lives. Without the
``email_verified`` check, anyone who can set an arbitrary unverified
address on a Google account they control can log in as the TestFortge
user who owns that address. Google will happily assert
``email: victim@customer.com, email_verified: false``. So an unverified
email is treated as no email at all.
"""
from __future__ import annotations

import os
from typing import Any, NamedTuple

from engine.log import get_logger

log = get_logger(__name__)

PROVIDER = "google"

#: Google's OIDC discovery document. Discovery rather than hardcoded
#: endpoints so signing-key rotation is handled for us.
GOOGLE_METADATA_URL = (
    "https://accounts.google.com/.well-known/openid-configuration"
)

#: What we ask for. ``openid email profile`` and nothing else — every
#: extra scope is data we would then be responsible for.
GOOGLE_SCOPE = "openid email profile"


def client_id() -> str:
    return (os.environ.get("GOOGLE_CLIENT_ID") or "").strip()


def client_secret() -> str:
    return (os.environ.get("GOOGLE_CLIENT_SECRET") or "").strip()


def is_configured() -> bool:
    """True when Google sign-in can be offered.

    Both halves required. A client id with no secret would render the
    button and fail at the callback, which is a worse experience than not
    offering it.
    """
    return bool(client_id() and client_secret())


# ── The decision ──────────────────────────────────────────────────

class Decision(NamedTuple):
    """What to do with a set of verified Google claims.

    ``action`` is one of:

    * ``sign_in``  — ``user_id`` is set; log them in.
    * ``link``     — ``user_id`` is set; create the identity row, then log in.
    * ``provision``— ``email`` is set and ``invite_token`` is live; create
                     the account, link, consume the invite.
    * ``refuse``   — ``reason`` explains it for the log; the route shows a
                     generic message.
    """
    action: str
    user_id: str | None = None
    email: str | None = None
    invite_token: str | None = None
    reason: str = ""


def decide(claims: dict[str, Any],
           invite_token: str | None = None) -> Decision:
    """Map verified Google claims onto a local account decision.

    *claims* must already have had its signature, issuer, audience,
    expiry and nonce checked — Authlib does that in ``parse_id_token``.
    This function assumes the claims are authentic and decides only what
    they entitle the holder to.
    """
    from engine import db as _db

    subject = str(claims.get("sub") or "").strip()
    if not subject:
        # Should be impossible for a validated id_token, but an empty
        # subject would match the first identity row with an empty
        # subject, so refuse rather than query.
        return Decision("refuse", reason="no_subject")

    # 1. Known identity — the ordinary returning-user path.
    existing = _db.get_user_by_identity(PROVIDER, subject)
    if existing is not None:
        if not existing.get("is_active", True):
            return Decision("refuse", reason="inactive")
        return Decision("sign_in", user_id=existing["id"])

    email = _db.normalize_email(claims.get("email"))
    email_verified = bool(claims.get("email_verified"))

    if not email:
        return Decision("refuse", reason="no_email_claim")

    if not email_verified:
        # The attack this blocks: an attacker sets victim@customer.com as
        # an unverified address on a Google account they control, and
        # without this check signs in as the victim. Treat unverified as
        # absent — there is nothing safe to do with it.
        log.warning("google sign-in refused: %s is not verified on the "
                    "Google account (sub=%s…)", email[:40], subject[:10])
        return Decision("refuse", reason="email_not_verified")

    # 2. Local account with the same verified address — link it.
    local = _db.get_user_by_email(email)
    if local is not None:
        if not local.get("is_active", True):
            return Decision("refuse", reason="inactive")
        return Decision("link", user_id=local["id"], email=email)

    # 3. No account, but the flow carries a live invite for this address.
    if invite_token:
        invite = _db.get_invite(invite_token)
        if invite is None:
            return Decision("refuse", reason="invite_not_claimable")
        if _db.normalize_email(invite.get("email")) != email:
            # The invite was issued to a different address. Honouring it
            # would let anyone with a link join as whoever they liked.
            log.warning("google sign-in refused: invite is for %s but "
                        "Google says %s",
                        str(invite.get("email"))[:40], email[:40])
            return Decision("refuse", reason="invite_email_mismatch")
        return Decision("provision", email=email, invite_token=invite_token)

    # 4. Invite-only means invite-only.
    return Decision("refuse", reason="no_account_no_invite")


def display_name_from(claims: dict[str, Any]) -> str | None:
    """A human name from the claims, if Google supplied one."""
    for key in ("name", "given_name"):
        value = (claims.get(key) or "").strip()
        if value:
            return value[:120]
    return None


#: Shown to the user for every refusal. One message, because the reasons
#: distinguish "this address has no account here" from "this address is
#: not verified on your Google account" — and the first of those is an
#: account-enumeration oracle that needs no password at all.
GENERIC_REFUSAL = (
    "We could not sign you in with that Google account. TestForTge is "
    "invite-only — ask an admin on your team for an invitation, and make "
    "sure the address on your Google account is verified."
)


def build_oauth(app) -> Any | None:
    """Register the Google client on *app*, or return ``None``.

    Returns ``None`` when the credentials are absent or Authlib is not
    installed, so the caller can hide the button rather than render one
    that 500s.
    """
    if not is_configured():
        return None
    try:
        from authlib.integrations.flask_client import OAuth
    except ImportError as exc:  # pragma: no cover — declared dependency
        log.warning("Google sign-in unavailable: authlib not importable "
                    "(%s)", exc)
        return None
    oauth = OAuth(app)
    oauth.register(
        name=PROVIDER,
        client_id=client_id(),
        client_secret=client_secret(),
        server_metadata_url=GOOGLE_METADATA_URL,
        client_kwargs={
            "scope": GOOGLE_SCOPE,
            # PKCE. Authlib generates and stores the verifier; without
            # this it would run a plain code exchange.
            "code_challenge_method": "S256",
        },
    )
    log.info("Google sign-in is configured (client_id=…%s).",
             client_id()[-6:] if len(client_id()) > 6 else "")
    return oauth


__all__ = [
    "PROVIDER", "GOOGLE_METADATA_URL", "GOOGLE_SCOPE", "GENERIC_REFUSAL",
    "Decision", "decide", "display_name_from",
    "client_id", "client_secret", "is_configured", "build_oauth",
]
