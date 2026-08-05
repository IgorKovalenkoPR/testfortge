"""Bug status transitions and who may make them — E4.5.

Unlike the checklist numbering (measured from the team's own reference sheet),
nothing in this repository said who may close a bug. So these tests pin a
*chosen* default, and the module keeps it in one table so the operator can
change it in one edit. What they mostly guard is that the workflow stays
permissive enough not to be worked around.
"""
from __future__ import annotations

import pytest

from engine import bug_workflow as wf
from engine.bug_report import BUG_STATUSES

ADMIN = lambda role: True            # noqa: E731 — a role oracle, not a lambda
USER = lambda role: role == "user"   # noqa: E731


class TestTheTable:

    def test_every_status_has_a_rule(self):
        """A status with no entry would silently offer the whole vocabulary."""
        assert set(wf.TRANSITIONS) == set(BUG_STATUSES)

    def test_every_destination_is_a_real_status(self):
        for source, targets in wf.TRANSITIONS.items():
            for target in targets:
                assert target in BUG_STATUSES, f"{source} → {target}"

    def test_no_status_lists_itself(self):
        """Staying put is not a transition; ``check`` short-circuits it."""
        for source, targets in wf.TRANSITIONS.items():
            assert source not in targets

    def test_only_closed_needs_a_role(self):
        assert wf.ROLE_FOR_STATUS == {"Closed": "admin"}
        assert wf.role_required("Resolved") == "user"
        assert wf.role_required("Closed") == "admin"


class TestAllowedFrom:

    def test_the_current_status_is_always_offered(self):
        """A form that re-posts the value it rendered must not fail."""
        for status in BUG_STATUSES:
            assert status in wf.allowed_from(status)

    def test_a_closed_bug_offers_only_reopened(self):
        assert set(wf.allowed_from("Closed")) == {"Closed", "Reopened"}

    def test_an_unrecognised_stored_value_offers_everything(self):
        """An odd value must not trap the bug — somebody has to be able to
        correct it."""
        assert set(wf.allowed_from("Triaged?")) == set(BUG_STATUSES)

    def test_an_empty_value_is_treated_as_open(self):
        assert set(wf.allowed_from("")) == set(wf.allowed_from("Open"))


class TestCheck:

    def test_the_ordinary_path_is_allowed_for_any_member(self):
        for current, target in (("Open", "In Progress"),
                                ("In Progress", "Resolved"),
                                ("Resolved", "Reopened"),
                                ("Reopened", "In Progress")):
            wf.check(current, target, has_role=USER)

    def test_closing_straight_from_open_is_allowed(self):
        """A bug filed by mistake or rejected as "won't fix" is closed without
        being worked on. A workflow that refuses that is one people work
        around."""
        wf.check("Open", "Closed", has_role=ADMIN)

    def test_closing_needs_an_admin(self):
        with pytest.raises(wf.TransitionRefused) as exc:
            wf.check("Resolved", "Closed", has_role=USER)
        assert exc.value.reason == "needs_role"
        assert "Resolved" in str(exc.value), "the message says what to do"

    def test_a_closed_bug_cannot_jump_to_in_progress(self):
        """Going straight there would leave the history saying it was never
        disputed."""
        with pytest.raises(wf.TransitionRefused) as exc:
            wf.check("Closed", "In Progress", has_role=ADMIN)
        assert exc.value.reason == "not_allowed_from"
        assert "Reopened" in str(exc.value), "the message names the way out"

    def test_reopening_a_closed_bug_needs_no_special_role(self):
        wf.check("Closed", "Reopened", has_role=USER)

    def test_setting_the_same_status_is_never_refused(self):
        for status in BUG_STATUSES:
            wf.check(status, status, has_role=USER)

    def test_an_invented_status_is_refused_by_name(self):
        with pytest.raises(wf.TransitionRefused) as exc:
            wf.check("Open", "Nearly done", has_role=ADMIN)
        assert exc.value.reason == "not_a_status"
        assert "Nearly done" in str(exc.value)

    def test_the_message_lists_the_real_options(self):
        with pytest.raises(wf.TransitionRefused) as exc:
            wf.check("Open", "Reopened", has_role=ADMIN)
        for option in wf.TRANSITIONS["Open"]:
            assert option in str(exc.value)

    @pytest.mark.parametrize("current,target", [
        ("In Progress", "Reopened"),
        ("Open", "Reopened"),
        ("Closed", "Resolved"),
    ])
    def test_the_message_needs_no_article(self, current, target):
        """Read in the browser: the first version produced "A in progress
        bug cannot go straight to reopened", and "A open bug" for the other
        half of the statuses. The message is read by the person whose change
        was just refused, so it names both states instead."""
        with pytest.raises(wf.TransitionRefused) as exc:
            wf.check(current, target, has_role=ADMIN)
        message = str(exc.value)
        assert f"{current} → {target}" in message
        for wrong in ("A in progress", "A open", "A resolved", "a in progress"):
            assert wrong not in message

    def test_an_admin_may_still_not_invent_a_transition(self):
        """The role gate and the transition gate are separate questions."""
        with pytest.raises(wf.TransitionRefused):
            wf.check("Closed", "Resolved", has_role=ADMIN)
