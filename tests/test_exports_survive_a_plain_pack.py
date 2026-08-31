"""Every export has to work on the plainest pack there is.

Found by walking the exports, 2026-08-31. ``/export/html`` returns 500
for a project holding one ordinary test case:

    AttributeError: 'NoneType' object has no attribute 'replace'
      engine/exporter.py:191  a(f"... Story: {h(tc.user_story_id)} ...")

``html.escape`` calls ``.replace`` on what it is given, and
``db.load_test_cases`` returns ``user_story_id``, ``issues`` and
``comment`` as ``None`` — even where the column holds ``''``. The
exporter guards two of those three::

    a(f"<tr><th>Issues</th><td>{h(tc.issues or '—')}</td></tr>")
    a(f"<tr><th>Comment</th><td>{h(tc.comment or '—')}</td></tr>")

so only the unguarded one crashes. It needs no unusual data: a pack
generated from requirements text carries no user stories, which is the
default, and both ``/test-cases`` and ``/checklist`` render a visible
"Export HTML" button pointing straight at it.

Fixed in ``h`` rather than at the twenty call sites that use it. An
escaper that raises on an absent value is the defect; a guard repeated
twenty times is the shape where one copy is missing, and this is that
shape with one copy missing. Coercing to ``str`` at the same time closes
a second latent crash: ``h(s.story_points_hint)`` is handed a number.

The format list comes from the route, so a format added later is covered
the day it is added rather than the day someone clicks it.
"""
from __future__ import annotations

import uuid

import pytest

from engine import db as _db


#: Every ``fmt`` the export route branches on that produces a file for a
#: manual pack. ``feature`` is excluded on purpose and checked separately:
#: it refuses a manual-only pack with 409 and a sentence saying how to get
#: BDD cases, which is its contract rather than a failure.
FORMATS = ["markdown", "html", "csv-testcases", "csv-checklist",
           "xlsx-testcases", "xlsx-checklist"]

PLAIN_CASE = {
    "id": "SC1_001", "section": "Auth", "section_num": 1,
    "summary": "Verify that login succeeds with valid credentials",
    "preconditions": "", "test_steps": "1. Sign in", "test_data": "",
    "expected_result": "The user is signed in.", "category": "Positive",
    "priority": "High", "status": "Unchecked",
    # No user_story_id, no issues, no comment — exactly what
    # save_test_cases writes for a pack generated from requirements text.
}

PLAIN_ITEM = {
    "id": "CNT_001", "section": "Content",
    "objective": "Verify that the header logo links to the home page",
    "category": "Positive", "priority": "High", "status": "Unchecked",
}


@pytest.fixture()
def plain_pack(client):
    client.post("/projects/db/create",
                data={"project_name": f"Exports {uuid.uuid4().hex[:6]}"},
                follow_redirects=True)
    with client.session_transaction() as sess:
        pid = sess.get("project_id") or ""
    assert pid
    _db.save_test_cases(pid, [dict(PLAIN_CASE)])
    _db.save_checklist(pid, [dict(PLAIN_ITEM)])
    client.get("/test-cases")
    client.get("/checklist")
    return pid


def test_the_pack_really_has_no_story_id(plain_pack):
    """Guard the guard: if the read stopped returning None, every
    assertion below would pass without exercising the defect."""
    from routes._shared import reconstruct_test_cases
    tc = reconstruct_test_cases(_db.load_test_cases(plain_pack))[0]
    assert tc.user_story_id is None, (
        "the read no longer yields None — these tests no longer reproduce "
        f"what they were written for (got {tc.user_story_id!r})")


class TestEveryFormat:
    @pytest.mark.parametrize("fmt", FORMATS)
    def test_it_answers_without_an_error(self, client, plain_pack, fmt):
        resp = client.get(f"/export/{fmt}")
        assert resp.status_code == 200, (
            f"/export/{fmt} failed on a one-case pack: {resp.status_code}")
        assert resp.get_data(), f"/export/{fmt} returned an empty file"

    def test_the_feature_export_refuses_a_manual_pack_clearly(
            self, client, plain_pack):
        # Not a 200, and not a 500 either: a manual pack has nothing for
        # a runner to bind, and an empty archive would read as a failure.
        resp = client.get("/export/feature")
        assert resp.status_code == 409, resp.status_code
        body = resp.get_data(as_text=True)
        assert "BDD" in body, (
            f"the refusal does not say how to get a .feature file: {body!r}")

    def test_the_html_export_contains_the_case(self, client, plain_pack):
        # Not fixed by returning an empty page: the export exists to
        # carry the pack.
        body = client.get("/export/html").get_data(as_text=True)
        assert "SC1_001" in body
        assert "login succeeds with valid credentials" in body


class TestTheEscaper:
    def test_it_survives_an_absent_value(self):
        from engine.exporter import _escape
        assert _escape(None) == ""

    def test_it_accepts_a_number(self):
        # `h(s.story_points_hint)` hands it an int.
        from engine.exporter import _escape
        assert _escape(3) == "3"

    def test_zero_is_a_value_not_an_absence(self):
        # The docstring on `_escape` claims this and mutation testing
        # said nothing was checking it. `str(value or "")` is the tidier
        # spelling and it renders a story-point hint of 0 as blank.
        from engine.exporter import _escape
        assert _escape(0) == "0"

    def test_it_still_escapes(self):
        # A fix that stringifies must not stop escaping. This export is
        # opened in a browser.
        from engine.exporter import _escape
        out = _escape('<script>alert("x")</script>')
        assert "<script>" not in out
        assert "&lt;script&gt;" in out

    def test_a_markup_payload_in_a_case_reaches_the_file_escaped(
            self, client):
        client.post("/projects/db/create",
                    data={"project_name": f"Esc {uuid.uuid4().hex[:6]}"},
                    follow_redirects=True)
        with client.session_transaction() as sess:
            pid = sess.get("project_id") or ""
        payload = dict(PLAIN_CASE)
        payload["summary"] = '<img src=x onerror="alert(1)">'
        _db.save_test_cases(pid, [payload])
        client.get("/test-cases")
        body = client.get("/export/html").get_data(as_text=True)
        assert "<img src=x" not in body, "the export shipped live markup"
        assert "&lt;img src=x" in body
