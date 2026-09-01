"""Every timestamp on screen was UTC wearing the reader's clock.

Found by walking the dashboard and reading it: "Last activity: 2026-09-01T15:39"
while the wall clock said 18:39. Every timestamp in this product is written by
``engine.db._utcnow()``, and every template displayed it with a slice —
``[:16]``, ``[:19]``, ``[:10]`` — which is exactly what removes the evidence.
On Postgres the stored string ends ``+00:00`` and the slice cuts it off; on
SQLite the offset is dropped on round-trip and was never there. Either way a
reader three hours east, which is who uses this, saw every timestamp in the
past with nothing to suggest it.

One template knew. ``test_cases.html`` wrote ``… }} UTC`` after its slice, so
the value's nature was understood by somebody; six other sites rendered the
same kind of value bare. One call site holding a guard its siblings do not is
the signpost this series keeps finding things through.

The sharpest of the six was ``org_members.html``: a pending invitation's
expiry, rendered ``[:10]`` — the date alone. An invite that lapses at 23:00
UTC is still valid past midnight for that reader, so the page named a day
earlier than the truth.

Local time would be friendlier and is a different change: the server does not
know the reader's zone, so that belongs in the browser. Marking the value is
what stops it being *wrong*.
"""
from __future__ import annotations

import pathlib
import re
from datetime import datetime, timezone

import pytest


#: Fields that carry a stored instant to a reader.
STAMP_FIELDS = ("created_at", "updated_at", "saved_at", "started_at",
                "expires_at", "finished_at", "edited_at", "captured_at")

#: A hand-sliced timestamp — the shape that dropped the offset.
SLICED = re.compile(
    r"(%s)[^\n]{0,40}\[:\d+\]" % "|".join(STAMP_FIELDS))


@pytest.fixture
def when(app):
    return app.jinja_env.filters["when"]


class TestTheFilter:

    def test_a_naive_stored_value_is_marked(self, when):
        """What SQLite hands back: no offset at all, and no way for a reader
        to tell. This is the case the dashboard was showing."""
        assert when("2026-09-01T15:39:22.772514") == "2026-09-01 15:39 UTC"

    def test_an_offset_is_not_shown_as_part_of_the_time(self, when):
        """What Postgres hands back. A length slice used to eat the ``+00:00``;
        cutting at the offset is what makes both engines render alike."""
        assert when("2026-09-01T15:39:22.772514+00:00") == \
            "2026-09-01 15:39 UTC"

    def test_a_z_suffix_is_handled_too(self, when):
        assert when("2026-09-01T15:39:22Z") == "2026-09-01 15:39 UTC"

    def test_seconds_are_available_where_they_were_shown(self, when):
        """``bug_reports.html`` used ``[:19]``. Losing that resolution would
        be a regression dressed as a fix."""
        assert when("2026-09-01T15:39:22.772514", seconds=True) == \
            "2026-09-01 15:39:22 UTC"

    def test_an_offset_never_leaks_into_the_time(self, when):
        """The case a length-based cut gets wrong, and the reason the filter
        looks for the offset instead of counting characters.

        Written because removing that search left every other test in this
        file green: with microseconds present the offset sits past character
        19 anyway, so only a value *without* them exposes the difference —
        and ``datetime.isoformat(timespec="minutes")`` produces exactly that.
        A surviving mutant is either a hole in the tests or code nobody
        needs; this one was the first.
        """
        for value in ("2026-09-01T15:39+00:00", "2026-09-01T15:39:22+00:00"):
            for seconds in (False, True):
                rendered = when(value, seconds=seconds)
                assert "+00" not in rendered, (value, seconds, rendered)
                assert rendered.endswith(" UTC")

    def test_a_datetime_works_as_well_as_a_string(self, when):
        assert when(datetime(2026, 9, 1, 15, 39, tzinfo=timezone.utc)) == \
            "2026-09-01 15:39 UTC"

    @pytest.mark.parametrize("empty", ["", None, 0])
    def test_an_empty_value_renders_empty(self, when, empty):
        """Several call sites sit inside ``{% if %}`` guards or used
        ``(x or '')``. A filter that rendered "UTC" for nothing would put a
        timezone on an absent date."""
        assert when(empty) == ""

    def test_the_marker_cannot_be_sliced_off(self, when):
        """The property that matters: the filter owns the truncation, so no
        call site can shorten the value and lose the marker with it."""
        for value in ("2026-09-01T15:39:22+00:00", "2026-09-01T15:39:22"):
            assert when(value).endswith(" UTC")


class TestNoTemplateSlicesAStampByHand:
    """Enumerated from the templates, not from a list of the seven that were
    wrong — a gate that knows only today's offenders is blind to the eighth.
    """

    @staticmethod
    def _offenders():
        out = []
        for path in sorted(pathlib.Path("templates").rglob("*.html")):
            text = path.read_text(encoding="utf-8")
            # Whole ``{# … #}`` blocks, not "lines starting with {#": several
            # of these templates carry a multi-line comment *describing* the
            # old ``started_at[:10]`` shape, and a per-line test flagged the
            # continuation lines. A scanner that cannot tell prose from code
            # is the one that declared five live templates dead.
            for number, line in enumerate(
                    re.sub(r"\{#.*?#\}", "", text,
                           flags=re.S).splitlines(), 1):
                if SLICED.search(line):
                    out.append(f"{path.name}:{number}: {line.strip()[:90]}")
        return out

    def test_none_are_left(self):
        offenders = self._offenders()
        assert not offenders, (
            "a stored timestamp is being sliced in a template, which drops "
            "the offset and the marker with it; use the `when` filter:\n"
            + "\n".join(offenders))

    def test_the_scanner_would_notice(self):
        """Guards the guard. The pattern above passes trivially if it matches
        nothing, which is what a renamed field would do to it."""
        assert SLICED.search("{{ p.updated_at[:16] }}")
        assert SLICED.search("<td>{{ (r.started_at or '')[:16] }}</td>")
        assert not SLICED.search("{{ p.updated_at | when }}")


class TestTheScreensThatShowThem:
    """Walked rather than diffed: a filter registered and never reached is
    the failure this file is about, one level up."""

    def test_the_dashboard_renders_nothing_raw(self, client, make_project):
        """The dashboard's three stamp cells need state these fixtures do not
        build — the recent-projects strip and the saved-projects table read
        different lists, and neither is populated by ``upsert_project``. So
        this asserts the *negative* on whatever does render, and the three
        cells are covered by the template test below. The live render was
        verified by walking it, which is where this was found.
        """
        pid = make_project("Timestamps on the dashboard")
        with client.session_transaction() as sess:
            sess["project_id"] = pid
        body = client.get("/").get_data(as_text=True)
        assert not re.search(r"20\d\d-\d\d-\d\dT\d\d:\d\d", body), \
            "a raw ISO timestamp is still being rendered"

    def test_the_dashboards_three_cells_use_the_filter(self):
        source = pathlib.Path("templates/index.html").read_text(
            encoding="utf-8")
        for expression in ("p.updated_at | when", "run.started_at | when",
                           "p.saved_at | when"):
            assert expression in source, expression

    def test_a_bug_reports_created_stamp_is_marked(self, client,
                                                  make_project):
        from engine import db as _db
        pid = make_project("Timestamps on bugs")
        with client.session_transaction() as sess:
            sess["project_id"] = pid
        _db.save_bug(pid, {"bug_id": "BUG-001", "title": "A defect",
                           "severity": "Major", "priority": "High",
                           "status": "Open"})
        body = client.get("/bug-reports").get_data(as_text=True)
        assert "UTC" in body

    def test_the_runs_table_marks_its_started_at(self, client, make_project):
        from engine import db as _db
        pid = make_project("Timestamps on runs")
        # Assigned to whoever is signed in, because with authentication on
        # this page defaults to "assigned to me" — for an admin too, who can
        # widen it but does not start there. An unassigned run renders no row
        # at all, and a test asserting on an empty table would have been
        # green for the wrong reason in one of the two flag modes.
        with client.session_transaction() as sess:
            sess["project_id"] = pid
            assignee = sess.get("_user_id") or ""
        _db.start_execution_run(pid, {"mode": "manual",
                                      "manual_queue": {"items": []},
                                      "tester": "alice",
                                      "assignee_id": assignee})
        body = client.get("/test-execution/runs").get_data(as_text=True)
        assert "UTC" in body
        assert not re.search(r"20\d\d-\d\d-\d\dT\d\d:\d\d", body)

    def test_the_invite_expiry_shows_the_whole_instant(self):
        """No route renders this with authentication off, so it is asserted on
        the template. The date alone was a day early for a reader east of
        UTC, which is the one case here where the *date* could be wrong and
        not just the hour."""
        source = pathlib.Path("templates/org_members.html").read_text(
            encoding="utf-8")
        assert "inv.expires_at | when" in source
        assert "expires_at or '')[:10]" not in source
