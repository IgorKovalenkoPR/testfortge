"""TestFortge — sending email, and being honest when it cannot (E0.4).

Until now nothing in this application sent a message. Invitations worked by
flashing the link to the admin so they could forward it by hand, and
``routes/members.py`` says why in as many words: claiming "an email has been
sent" while no provider exists would be a lie, and the admin would wait for
a message that never arrives.

This module is the provider. What it is careful about is the *other* half of
that sentence — every reason a send can fail to happen is reported to the
caller as a reason, never as a silent success, so the page above it can fall
back to the link it always had.

Three ways a send does not happen, and all three are ordinary
------------------------------------------------------------
* **No provider.** ``RESEND_API_KEY`` and ``MAIL_FROM`` are unset. This is
  the state of every developer checkout and of the deployment until somebody
  fills the dashboard in, so it is a supported mode rather than an error.
* **The daily ceiling.** Resend's free tier allows 3,000 messages a month
  but only **100 a day** (``docs/plans/cost_model.md``), and a cap that is
  discovered by having mail silently dropped is worse than one the product
  knows about. :func:`send` counts today's messages and refuses past the
  limit with a reason.
* **The provider said no**, or the network did. A bad key, a domain that is
  not verified yet, a timeout.

In each case :class:`Delivery` comes back with ``sent=False`` and the caller
shows the copy-and-paste link. The cost model asks for a queue as well; a
counter and a fallback are what is built, because a queue on a dyno that
sleeps after fifteen idle minutes needs a worker that survives the nap, and
the honest version of "we will send it later" on this plan is "here is the
link, send it now".

Why a bare HTTPS POST rather than the Resend SDK
------------------------------------------------
One endpoint, one JSON body. ``requests`` is already a dependency; an SDK
would be a second pin to keep current for a call that fits in fifteen lines,
and swapping providers later means editing :func:`_post_resend` rather than
untangling a client object from four call sites.

Never raises
------------
Sending is never the reason a user's action fails. Somebody resetting a
password whose provider is down should still get a working link on screen,
and an admin inviting a colleague should still get an invitation. Every path
here returns a :class:`Delivery`.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from engine.log import get_logger

log = get_logger(__name__)

#: Resend's transactional send endpoint.
RESEND_ENDPOINT = "https://api.resend.com/emails"

#: Messages per day on the free tier (cost_model.md §"Email").
#:
#: Overridable with ``MAIL_DAILY_LIMIT`` — upward for a paid plan, downward
#: to leave headroom for something else on a shared domain.
DEFAULT_DAILY_LIMIT = 100

#: Seconds to wait on the provider. Short: this runs while somebody is
#: watching a form, and a slow send is indistinguishable from a hung page.
TIMEOUT_SECONDS = 10

#: Audit ``entity`` for a message this module sent. The daily count is read
#: back from these rows, so the audit trail is the meter as well as the
#: record — one place that already exists, is append-only, and that an
#: operator can already read.
AUDIT_ENTITY = "email"

#: Shape check only. An address has to survive a round trip through
#: somebody's inbox to be useful, so a strict regex here would be theatre;
#: what this catches is an empty field, a missing ``@``, and whitespace that
#: would let a header be smuggled into the JSON body.
_PLAUSIBLE = re.compile(r"^[^@\s,;:<>\"]+@[^@\s,;:<>\"]+\.[^@\s,;:<>\"]+$")


# ── What happened ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Delivery:
    """The outcome of one attempt to send one message.

    ``reason`` is a stable code for the caller to branch on and for the log;
    ``detail`` is free text for the log only. Neither is written for a user —
    the pages phrase their own sentences, because "provider_error" is not
    something to show somebody who is trying to reset a password.
    """

    sent: bool
    reason: str = ""
    detail: str = ""

    @property
    def needs_fallback(self) -> bool:
        """True when the caller must show the link itself.

        A property rather than ``not sent`` at four call sites, so the
        question "does the page have to offer the link" has one answer and
        cannot drift between the invite page and the reset page.
        """
        return not self.sent


SENT = Delivery(True)


# ── Configuration ────────────────────────────────────────────────────

def api_key() -> str:
    return (os.environ.get("RESEND_API_KEY") or "").strip()


def sender() -> str:
    """The ``From`` address. Must be on a domain verified with Resend."""
    return (os.environ.get("MAIL_FROM") or "").strip()


def configured() -> bool:
    """True when this instance can actually send.

    Both halves are required and the second is the one that gets forgotten:
    a key with no verified sender address produces a 403 from the provider
    on every send, which looks like a broken integration rather than a
    missing setting.
    """
    return bool(api_key() and sender())


def daily_limit() -> int:
    raw = (os.environ.get("MAIL_DAILY_LIMIT") or "").strip()
    if not raw:
        return DEFAULT_DAILY_LIMIT
    try:
        value = int(raw)
    except ValueError:
        log.warning("MAIL_DAILY_LIMIT=%r is not an integer — using %d.",
                    raw, DEFAULT_DAILY_LIMIT)
        return DEFAULT_DAILY_LIMIT
    # Zero is meaningful here, unlike the session windows: it is how an
    # operator switches sending off without removing the key.
    return max(0, value)


def sent_today() -> int:
    """Messages this instance sent in the last 24 hours.

    A rolling window rather than a calendar day, because the provider's
    limit resets on its own clock in its own timezone and a local midnight
    would let a burst straddle the boundary and exceed the real cap.
    """
    from engine import db as _db

    try:
        return _db.count_audit_since(
            entity=AUDIT_ENTITY, action="send",
            since=datetime.now(timezone.utc) - timedelta(hours=24))
    except Exception as exc:      # pragma: no cover — never block a send
        # Counting is a courtesy to the quota, not a correctness property.
        # If the count is unavailable, sending is better than not.
        log.warning("could not count today's email: %s", exc)
        return 0


def remaining_today() -> int:
    return max(0, daily_limit() - sent_today())


def state() -> dict:
    """What an operator needs to know, in one call.

    Rendered on the team settings page, so somebody who is not getting
    invitations can find out whether that is a missing key, a used-up
    quota, or something else — rather than filing it as a bug.
    """
    return {
        "configured": configured(),
        "sender": sender(),
        "daily_limit": daily_limit(),
        "sent_today": sent_today(),
        "remaining_today": remaining_today(),
    }


# ── Sending ──────────────────────────────────────────────────────────

def plausible_address(email: str) -> bool:
    return bool(_PLAUSIBLE.match((email or "").strip()))


def send(*, to: str, subject: str, text: str, kind: str,
         user_id: str | None = None, org_id: str | None = None,
         html: str | None = None) -> Delivery:
    """Send one message. Returns why not, rather than raising.

    *kind* is a short label for the audit row (``invite``, ``reset``,
    ``verify``) — what the message was for, so the trail answers "did a
    reset go out for this person" without the body being stored.

    The recorded row deliberately carries the address and the kind and
    **not** the body. A reset email contains a working credential, and an
    audit trail is read by more people than the inbox is.
    """
    to = (to or "").strip()
    if not plausible_address(to):
        return Delivery(False, "invalid_address")
    if not configured():
        return Delivery(False, "not_configured")

    limit = daily_limit()
    if limit <= 0:
        return Delivery(False, "sending_disabled")
    if sent_today() >= limit:
        log.warning("email not sent (%s): daily cap of %d reached", kind,
                    limit)
        return Delivery(False, "daily_cap")

    outcome = _post_resend(to=to, subject=subject, text=text, html=html)
    if outcome.sent:
        _record(kind=kind, to=to, user_id=user_id, org_id=org_id)
        log.info("email sent (%s) to %s", kind, _redact(to))
    else:
        log.warning("email not sent (%s) to %s: %s %s", kind, _redact(to),
                    outcome.reason, outcome.detail)
    return outcome


def _post_resend(*, to: str, subject: str, text: str,
                 html: str | None) -> Delivery:
    """The one provider-specific function in this module."""
    try:
        import requests
    except Exception as exc:      # pragma: no cover — requests is pinned
        return Delivery(False, "provider_unavailable", str(exc))

    payload = {"from": sender(), "to": [to], "subject": subject,
               "text": text}
    if html:
        payload["html"] = html
    try:
        response = requests.post(
            RESEND_ENDPOINT, json=payload,
            headers={"Authorization": f"Bearer {api_key()}",
                     "Content-Type": "application/json"},
            timeout=TIMEOUT_SECONDS)
    except Exception as exc:
        # Timeout, DNS, TLS — all the same to the caller, which is going to
        # show the link either way.
        return Delivery(False, "provider_unreachable", str(exc)[:200])

    if 200 <= response.status_code < 300:
        return SENT
    # The body can name the problem — an unverified domain, a malformed
    # address — and it is the difference between an operator fixing it in
    # two minutes and guessing. Truncated, and it never reaches a user.
    return Delivery(False, "provider_refused",
                    f"HTTP {response.status_code}: {response.text[:200]}")


def _record(*, kind: str, to: str, user_id: str | None,
            org_id: str | None) -> None:
    from engine import db as _db

    _db.append_audit(entity=AUDIT_ENTITY, action="send", user_id=user_id,
                     org_id=org_id, diff={"kind": kind, "to": to})


def _redact(email: str) -> str:
    """``a***@example.com`` — enough to correlate, not enough to harvest.

    Logs on this platform are read in a browser tab that stays open, and a
    full address list in them is a mailing list waiting to be copied.
    """
    local, _, domain = (email or "").partition("@")
    if not domain:
        return "?"
    head = local[:1] or "?"
    return f"{head}***@{domain}"


__all__ = [
    "RESEND_ENDPOINT", "DEFAULT_DAILY_LIMIT", "TIMEOUT_SECONDS",
    "AUDIT_ENTITY", "Delivery", "SENT",
    "api_key", "sender", "configured", "daily_limit", "sent_today",
    "remaining_today", "state", "plausible_address", "send",
]
