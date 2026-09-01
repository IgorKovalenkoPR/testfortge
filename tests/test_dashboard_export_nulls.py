"""``/dashboard/export.csv`` answered 500 on a row the schema permits.

    TypeError: '<' not supported between instances of 'NoneType' and 'str'

Walked: open the dashboard's CSV export on a project holding a test case
whose ``category`` is NULL. The export groups the pack into buckets and
sorts them, and ``sorted`` cannot compare ``None`` with a string.

The bucket key comes from ``engine.test_metrics_generator``:

    cat = tc.get("category", "Other")

which reads like a default and is not one. ``dict.get(key, default)``
returns the default only when the key is **absent**; a key present with
``None`` comes straight through. A pack hydrated from the database carries
exactly that for a nullable column.

Ten lines below, in the same function, the bug branch already knew:

    sev = bug.get("severity", "Minor") or "Minor"

Four lines had the guard, four did not — test cases and checklist items,
category and priority. The other aggregator got it right too:
``dashboard_metrics._counts`` folds ``None`` and ``""`` into "Unspecified".
So two of the three places that build these buckets were correct and the
crash came from the third.

Both halves are fixed here: the aggregator normalises, and the export sorts
by ``str(key)`` so a future stray one is an odd row rather than a 500. An
export that cannot be produced is worse than an export with an "Other" in it.
"""
from __future__ import annotations

import csv
import io
from unittest import mock

import pytest

from engine import db as _db
from engine import test_metrics_generator as tmg


@pytest.fixture(autouse=True)
def _ready():
    _db.init_db()


class TestTheBucketsTolerateANull:
    """The aggregator, directly — the crash was one layer above it."""

    @pytest.mark.parametrize("value", [None, ""])
    def test_a_test_case_category_falls_back(self, value):
        metrics = tmg.compute_session_metrics(
            tc_data=[{"id": "TC-001", "category": value, "priority": "High"}],
            cl_data=[], bugs_data=[], test_runs=[])
        assert metrics["tc_by_category"] == {"Other": 1}

    @pytest.mark.parametrize("value", [None, ""])
    def test_a_test_case_priority_falls_back(self, value):
        metrics = tmg.compute_session_metrics(
            tc_data=[{"id": "TC-001", "category": "Positive",
                      "priority": value}],
            cl_data=[], bugs_data=[], test_runs=[])
        assert metrics["tc_by_priority"] == {"Medium": 1}

    @pytest.mark.parametrize("value", [None, ""])
    def test_a_checklist_category_falls_back(self, value):
        metrics = tmg.compute_session_metrics(
            tc_data=[], cl_data=[{"id": "CL-001", "category": value,
                                  "priority": value}],
            bugs_data=[], test_runs=[])
        assert metrics["cl_by_category"] == {"Other": 1}
        assert metrics["cl_by_priority"] == {"Medium": 1}

    def test_the_bug_branch_still_does_what_it_did(self):
        """It always had the guard. This is the control that the fix copied
        the right shape rather than inventing one."""
        metrics = tmg.compute_session_metrics(
            tc_data=[], cl_data=[],
            bugs_data=[{"id": "BUG-001", "severity": None, "priority": None,
                        "status": None}],
            test_runs=[])
        assert metrics["bug_by_severity"] == {"Minor": 1}
        assert metrics["bug_by_priority"] == {"Medium": 1}
        assert metrics["bug_by_status"] == {"Open": 1}

    def test_a_real_value_is_left_alone(self):
        """A fallback that swallowed real values would pass everything
        above."""
        metrics = tmg.compute_session_metrics(
            tc_data=[{"id": "TC-001", "category": "Negative",
                      "priority": "Low"}],
            cl_data=[], bugs_data=[], test_runs=[])
        assert metrics["tc_by_category"] == {"Negative": 1}
        assert metrics["tc_by_priority"] == {"Low": 1}

    def test_the_get_default_idiom_is_gone_from_the_bucket_keys(self):
        """Enumerated, because the four that were wrong looked exactly like
        the four that were right until you read the end of the line."""
        import pathlib
        import re

        source = pathlib.Path(
            "engine/test_metrics_generator.py").read_text(encoding="utf-8")
        offenders = []
        for number, line in enumerate(source.splitlines(), 1):
            if re.search(r'=\s*\w+\.get\("(category|priority|severity|status)"'
                         r',\s*"[^"]+"\)\s*$', line):
                offenders.append(f"{number}: {line.strip()}")
        assert not offenders, (
            "a bucket key uses `get(key, default)` without `or`; a stored "
            "NULL passes straight through it:\n" + "\n".join(offenders))


class TestTheExportSurvivesEitherWay:

    def test_a_project_with_a_null_category_exports(self, client,
                                                    make_project):
        """The walk that found it: a NULL in the column, then the export."""
        pid = make_project("Export with a null category")
        _db.save_test_cases(pid, [{"id": "TC-001", "title": "A case",
                                   "summary": "Verify a thing",
                                   "test_steps": "1. do it"}])
        with client.session_transaction() as sess:
            sess["project_id"] = pid
        response = client.get("/dashboard/export.csv")
        assert response.status_code == 200, response.status_code
        assert "text/csv" in response.headers["Content-Type"]

    def test_the_sort_cannot_be_broken_by_a_stray_none(self, client,
                                                       make_project):
        """Belt and braces, asserted as such. Even if some future producer
        hands the export a ``None`` key, it must render a row rather than
        refuse the whole file."""
        pid = make_project("Export with a hostile mapping")
        with client.session_transaction() as sess:
            sess["project_id"] = pid

        import routes.dashboard as dash

        def _metrics():
            return {"tc_by_category": {None: 1, "Positive": 2},
                    "tc_by_priority": {}, "cl_by_category": {},
                    "bug_by_severity": {}, "bug_by_status": {}}

        with mock.patch.object(dash, "_compute_dashboard_metrics", _metrics):
            response = client.get("/dashboard/export.csv")
        assert response.status_code == 200, response.status_code
        rows = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
        assert any("Positive" in row for row in rows), rows

    def test_the_csv_still_carries_its_numbers(self, client, make_project):
        """The control for both tests above: an export that returned an
        empty 200 would satisfy them."""
        pid = make_project("Export with content")
        _db.save_test_cases(pid, [{"id": "TC-001", "summary": "A case",
                                   "category": "Negative",
                                   "priority": "High"}])
        with client.session_transaction() as sess:
            sess["project_id"] = pid
        body = client.get("/dashboard/export.csv").get_data(as_text=True)
        assert "Test cases by category" in body
        assert "metric" in body
