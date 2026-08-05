"""TestFortge — which status a bug may move to, and who may move it (E4.5).

``BUG_STATUSES`` has been a flat list since the module was written: any status
could be set from any other, by anyone. That is fine while one person uses the
tool and stops being fine the moment a team does — "Closed" is a sign-off, and
a report that jumps from Open to Closed without anybody resolving anything
looks identical to one that was actually verified.

Two rules, kept separate because they answer different questions:

**Is the transition meaningful?** :data:`TRANSITIONS`. Deliberately permissive:
it blocks the moves that cannot mean anything ("Closed → In Progress" without
reopening) and allows the ones teams really use, including closing straight
from Open — a bug filed by mistake or rejected as "won't fix" is closed
without ever being worked on, and a workflow that refuses that is one people
work around.

**Is this person allowed?** :data:`ROLE_FOR_STATUS`. Only ``Closed`` is
restricted, to admins. It mirrors the gate already on bulk delete in
``routes/bugs.py``: ordinary triage is everybody's job, and the acts that end
an argument about whether something is fixed belong to whoever owns the
project.

This is a **default, not a discovered rule.** Unlike the checklist numbering
(measured from the team's reference sheet in
``qa_knowledge/style/checklist_style.yaml``), nothing in this repository says
who may close a bug. It is one table in one place so the operator can change
it in one edit.
"""
from __future__ import annotations

from engine.bug_report import BUG_STATUSES
from engine.log import get_logger

log = get_logger(__name__)

#: ``from`` → the statuses that may follow it.
#:
#: A status may always be re-set to itself; that is not a transition, and a
#: form that re-posts the current value must not fail.
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "Open": ("In Progress", "Resolved", "Closed"),
    "In Progress": ("Open", "Resolved", "Closed"),
    # Resolved means "a fix is in". Either it is verified (Closed), it is not
    # (Reopened), or somebody picks it back up (In Progress).
    "Resolved": ("Closed", "Reopened", "In Progress"),
    # A closed bug is reopened before anything else happens to it. Going
    # straight to In Progress would leave the history saying it was never
    # disputed.
    "Closed": ("Reopened",),
    "Reopened": ("In Progress", "Resolved", "Closed"),
}

#: Statuses that need more than the ``user`` role. Everything absent is open
#: to any member.
ROLE_FOR_STATUS: dict[str, str] = {
    "Closed": "admin",
}


class TransitionRefused(ValueError):
    """The status change is not allowed. Message is written for the user."""

    def __init__(self, message: str, *, reason: str):
        #: ``not_a_status`` | ``not_allowed_from`` | ``needs_role``. The route
        #: maps it to a status code; the user reads the message.
        self.reason = reason
        super().__init__(message)


def allowed_from(current: str) -> tuple[str, ...]:
    """Statuses reachable from ``current``, including ``current`` itself."""
    current = (current or "Open").strip()
    if current not in TRANSITIONS:
        # An unrecognised stored value must not trap the bug: offer the whole
        # vocabulary rather than nothing, so somebody can correct it.
        return tuple(BUG_STATUSES)
    return (current,) + TRANSITIONS[current]


def role_required(status: str) -> str:
    """The minimum role for setting ``status`` — ``"user"`` for most."""
    return ROLE_FOR_STATUS.get((status or "").strip(), "user")


def check(current: str, target: str, *, has_role) -> None:
    """Raise :class:`TransitionRefused` unless the move is allowed.

    ``has_role`` is a callable so this module stays testable and free of a
    request context — ``engine.permissions.has_role`` in the app, a lambda in
    the tests.
    """
    current = (current or "Open").strip()
    target = (target or "").strip()

    if target not in BUG_STATUSES:
        raise TransitionRefused(
            f"{target!r} is not a bug status. Use one of: "
            f"{', '.join(BUG_STATUSES)}.",
            reason="not_a_status")

    if target == current:
        return

    if target not in allowed_from(current):
        # Phrased without an article on purpose: "A in progress bug" and "A
        # open bug" are what an a/an rule produces from these names, and the
        # message is read by the person whose change was just refused.
        raise TransitionRefused(
            f"{current} → {target} is not a move this workflow allows. "
            f"From {current} you can go to: "
            f"{', '.join(TRANSITIONS.get(current, ()))}.",
            reason="not_allowed_from")

    needed = role_required(target)
    if needed != "user" and not has_role(needed):
        raise TransitionRefused(
            f"Closing a bug report is limited to {needed}s — it is the "
            f"sign-off that a fix was verified. Mark it Resolved instead, or "
            f"ask an admin to close it.",
            reason="needs_role")


__all__ = ["ROLE_FOR_STATUS", "TRANSITIONS", "TransitionRefused",
           "allowed_from", "check", "role_required"]
