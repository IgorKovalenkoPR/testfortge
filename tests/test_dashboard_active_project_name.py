"""The dashboard's Active Project panel must name the active project.

Found by walking the dashboard on staging, 2026-08-30. The page said
three things about the same project at the same time:

* the picker at the top: ``Active project: Browser Extension Demo``
* the project card: an ``Active`` pill
* the Active Project panel below: ``Active: —``

The panel's guard and its value read different sources. The guard is
``{% if active_project_id %}``, which the global context processor fills
from the database and which is correct. The name came from
``session['project_setup']['project_name']`` — the pre-E3 source that
Postgres replaced. Selecting a project through the picker writes the
database and does not populate that session key, so the panel took the
"there is an active project" branch and then named it nothing.

An em dash where a project name belongs reads as "no project is active",
which is the one thing the operator most needs the dashboard to be right
about: every module refuses to save without one. The header disagreeing
with the panel is worse than either being wrong alone, because it gives
the operator no way to tell which to believe.

The name is already in the template's reach: the same context processor
supplies ``projects``, and the picker's own ``<select>`` finds the
active one with ``p.id == active_project_id`` two blocks further up.
"""
from __future__ import annotations

import re
import uuid

import pytest

from engine import db as _db


PANEL = re.compile(
    r"picker-active-badge.*?</span>", re.S)


def _seed_project(client, name: str) -> str:
    """A project created and activated the way the UI does both.

    Through the route rather than ``db.upsert_project`` directly, because
    the route is what sets ``owner_sid`` and ``org_id``. A project
    created without an owner is invisible to ``visible_projects``, so the
    picker renders no options at all and any assertion about what the
    picker says passes or fails for the wrong reason. Cost an
    investigation to learn.
    """
    client.post("/projects/db/create",
                data={"project_name": name}, follow_redirects=True)
    with client.session_transaction() as sess:
        pid = sess.get("project_id") or ""
    assert pid, "creating the project did not activate it"
    row = _db.get_project(pid)
    assert row and row.get("name") == name, (
        f"expected the new project to be active, got {row!r}")
    return pid


def _panel_text(client) -> str:
    body = client.get("/").get_data(as_text=True)
    m = PANEL.search(body)
    assert m, "the Active Project panel did not render its active badge"
    # Strip tags so the assertion reads what a person reads.
    return re.sub(r"<[^>]+>", " ", m.group(0)).strip()


class TestTheActiveProjectPanel:
    def test_it_names_the_project_the_picker_names(self, client):
        name = f"Panel probe {uuid.uuid4().hex[:8]}"
        _seed_project(client, name)
        assert name in _panel_text(client), (
            "the panel took the 'a project is active' branch and did not "
            "say which")

    def test_it_does_not_fall_back_to_an_em_dash(self, client):
        # The specific symptom, pinned on its own: an em dash in the slot
        # where a name belongs is indistinguishable from having no
        # project at all, and the operator reads it that way.
        _seed_project(client, f"Panel probe {uuid.uuid4().hex[:8]}")
        assert "—" not in _panel_text(client)

    def test_it_survives_the_restart_wipe(self, client):
        """The actual trigger, driven through the hook that causes it.

        ``app.py`` clears every key in ``GENERATED_KEYS`` on the first
        request after a restart, so a filesystem session on Render's free
        plan never shows stale data from a previous boot.
        ``project_setup`` is in that list. ``project_id`` is not.

        So a restart leaves the session holding an active project id with
        no name beside it, and staging restarts on every deploy. This is
        not an exotic path: it is what the dashboard looks like the first
        time anyone opens it after a release.
        """
        name = f"Panel probe {uuid.uuid4().hex[:8]}"
        _seed_project(client, name)
        with client.session_transaction() as sess:
            # Make this session look older than the running process,
            # which is exactly what a restart does to a session on disk.
            sess["_session_active_since"] = 0
        assert name in _panel_text(client), (
            "after a restart the panel claims a project is active and "
            "will not say which")

    def test_the_restart_wipe_really_does_drop_the_legacy_key(self, client):
        # Guards the test above from becoming vacuous. If the wipe ever
        # stops clearing `project_setup`, the test passes for a reason
        # that has nothing to do with the fix.
        _seed_project(client, f"Panel probe {uuid.uuid4().hex[:8]}")
        with client.session_transaction() as sess:
            assert sess.get("project_setup"), "nothing to wipe"
            sess["_session_active_since"] = 0
        client.get("/")
        with client.session_transaction() as sess:
            assert not sess.get("project_setup"), (
                "the restart wipe no longer clears project_setup — the "
                "test above is no longer exercising the defect")
            assert sess.get("project_id"), (
                "project_id was wiped too, which would take the panel "
                "down its 'no active project' branch instead")

    def test_the_header_and_the_panel_agree(self, client):
        # The property that actually matters. Either both name the
        # project or neither claims one is active; a page that says both
        # at once cannot be acted on.
        name = f"Panel probe {uuid.uuid4().hex[:8]}"
        pid = _seed_project(client, name)
        with client.session_transaction() as sess:
            sess["_session_active_since"] = 0
        body = client.get("/").get_data(as_text=True)
        # The picker marks the active option selected; that is the header
        # naming it, and it does not depend on where either block sits in
        # the document.
        assert re.search(
            r'value="' + pid + r'"[^>]*selected', body), (
            "the picker stopped marking the project selected — test is stale")
        assert name in _panel_text(client), (
            "the picker names the project and the panel does not")

    def test_a_selected_project_that_no_longer_exists(self, client):
        """The state the fallback exists for.

        ``active_project_id`` lives in the session; the project it names
        lives in the database. They can disagree — the project is deleted,
        or handed to another owner, and the session still points at it.
        The lookup then finds nothing, and the panel has to say something
        a person can read rather than rendering ``None`` or blowing up the
        page.
        """
        _seed_project(client, f"Panel probe {uuid.uuid4().hex[:8]}")
        with client.session_transaction() as sess:
            sess["project_id"] = uuid.uuid4().hex      # nothing owns this
        resp = client.get("/")
        assert resp.status_code == 200, "the dashboard stopped rendering"
        body = resp.get_data(as_text=True)
        m = re.search(r"picker-active-badge.*?<strong>(.*?)</strong>",
                      body, re.S)
        assert m, "the panel stopped rendering a name slot at all"
        slot = m.group(1).strip()
        # The label "Active:" sits outside the slot, so an empty slot is
        # invisible to any assertion that reads the whole badge — which
        # is how the first version of this test passed with the fallback
        # deleted.
        assert slot, "the panel rendered 'Active:' followed by nothing"
        assert "None" not in slot, (
            f"the panel rendered a Python value at the operator: {slot!r}")

    def test_no_active_project_still_says_so(self, client):
        # The other branch has to keep working: the fix must not make
        # the panel claim a project when there is none.
        with client.session_transaction() as sess:
            sess.pop("project_id", None)
            sess.pop("project_setup", None)
        body = client.get("/").get_data(as_text=True)
        assert "picker-active-badge" not in body, (
            "the panel showed its active badge with no active project")
        assert "picker-empty" in body
