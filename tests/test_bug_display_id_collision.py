"""Two bugs must not answer to the same id on the page.

Found by walking /bug-reports on staging, 2026-08-30. The project showed
24 bug cards whose ids ran BUG-023 down to BUG-001 — with BUG-001 twice,
on two unrelated findings:

    BUG-001  Test bug - CSV formula injection follow-up
    BUG-001  [Authentication] Login is not completed successfully ...

The product already defends this on two fronts, and neither could see
it. ``_renumber_duplicate_public_ids`` repairs colliding ids at boot and
``ux_bug_report_project_external_id`` stops new ones being written —
both keyed on the stored ``external_id``.

The collision is not in a stored id. A bug filed without one is stored
with ``external_id = NULL``, deliberately: "Empty string is not an id,
and storing it as one would put every id-less bug in a project into a
single collision the unique index cannot resolve." Then
``engine.workspace.bug_row_to_dict`` renders it as::

    row["external_id"] or f"BUG-{row['id']:03d}"

— inventing a public id from the primary key, in the same namespace as
the stored ones, downstream of every guarantee about that namespace. Row
1 with no id displays as BUG-001, and any project whose first bug really
is BUG-001 now has two.

A bug id is the most-cited string the product produces: it goes in the
XLSX export, in "reopening BUG-004", in a client's review comments.
Two findings answering to one id makes every one of those ambiguous.
"""
from __future__ import annotations

import re
import uuid

import pytest

from engine import db as _db


def _project(client) -> str:
    client.post("/projects/db/create",
                data={"project_name": f"Bug ids {uuid.uuid4().hex[:8]}"},
                follow_redirects=True)
    with client.session_transaction() as sess:
        pid = sess.get("project_id") or ""
    assert pid, "could not create and activate a project"
    return pid


def _displayed_ids(client) -> list[str]:
    body = client.get("/bug-reports").get_data(as_text=True)
    return re.findall(r'class="bug-id">([^<]+)</span>', body)


def _seed_collision(client, pid: str) -> str:
    """One bug with no id, one whose stored id is what the first will
    be *displayed* as. Returns that id.

    Built in this order on purpose: the id-less row has to exist before
    its display id can be predicted, because the display id is its
    primary key.
    """
    idless_row = _db.save_bug(pid, {
        "title": "Filed without an id",
        "severity": "Major", "priority": "High", "status": "Open",
    })
    assert idless_row, "the id-less bug was not saved"
    display_id = f"BUG-{int(idless_row):03d}"
    _db.save_bug(pid, {
        "id": display_id,
        "title": "Filed with the id the other one will be shown as",
        "severity": "Minor", "priority": "Medium", "status": "Open",
    })
    return display_id


class TestDisplayedBugIds:
    def test_an_id_less_bug_does_not_borrow_a_stored_id(self, client):
        pid = _project(client)
        clashing = _seed_collision(client, pid)
        ids = _displayed_ids(client)
        assert ids, "the page rendered no bug ids at all"
        assert ids.count(clashing) <= 1, (
            f"{clashing} is shown on {ids.count(clashing)} different bugs: "
            f"{ids}")

    def test_no_displayed_id_is_shown_twice(self, client):
        # The property, stated without reference to how it breaks.
        pid = _project(client)
        _seed_collision(client, pid)
        ids = _displayed_ids(client)
        dupes = {i for i in ids if ids.count(i) > 1}
        assert not dupes, f"ids shown more than once: {sorted(dupes)}"

    def test_the_id_less_bug_is_still_identified_somehow(self, client):
        # Not fixed by rendering nothing. The card needs a handle the
        # operator can read out loud, otherwise the fix trades an
        # ambiguous id for no id.
        pid = _project(client)
        _seed_collision(client, pid)
        ids = _displayed_ids(client)
        assert len(ids) == 2, f"expected both bugs to render: {ids}"
        assert all(i.strip() for i in ids), f"a card has a blank id: {ids}"

    def test_a_stored_id_is_never_rewritten_by_the_display(self, client):
        # Whatever the fallback does, a bug that owns an id keeps it
        # exactly — it is already in exports and citations.
        pid = _project(client)
        _db.save_bug(pid, {"id": "BUG-007", "title": "Owns its id",
                            "severity": "Major", "priority": "High",
                            "status": "Open"})
        assert "BUG-007" in _displayed_ids(client)

    def test_the_id_less_row_keeps_its_null_in_the_database(self, client):
        # The fix belongs in what the page shows, not in a read that
        # writes. `save_bug` stores NULL on purpose — the comment there
        # explains that an empty string would collapse every id-less bug
        # in a project into one collision the unique index cannot fix.
        pid = _project(client)
        row_id = _db.save_bug(pid, {"title": "Filed without an id",
                                     "severity": "Minor",
                                     "priority": "Medium", "status": "Open"})
        client.get("/bug-reports")
        rows = _db.list_bugs(pid) or []
        mine = [r for r in rows if int(r.get("id") or 0) == int(row_id)]
        assert mine, "the id-less bug vanished from the project"
        assert not (mine[0].get("external_id") or ""), (
            "rendering the page assigned a stored id — reads must not write")
