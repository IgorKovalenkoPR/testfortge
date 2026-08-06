"""
The dashboard filter branches, and the eval scorer's error paths (E9.2).

Both modules were below the programme's 85% bar — `dashboard_metrics` at 78%
and `tedgie_eval` at 82% — and in both the uncovered part was the same kind
of thing: what happens when the input is *not* what the happy path assumes.
A dashboard filter arrives from a URL, so every field in it is attacker- or
typo-supplied; a golden-set item arrives from a YAML file somebody edits by
hand.

The distinction that made these worth covering rather than waving at is
recorded in `dashboard_metrics` itself: ``_matching_run_ids`` returns
``None`` for "no filter" and ``[]`` for "a filter that matched nothing", and
those two must not be confused — one means *everything*, the other means
*nothing*. A test that only ever filters on something that exists never
distinguishes them.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import pytest

from engine import dashboard_metrics as dm
from engine import db as _db
from engine import tedgie_eval as ev

_RUN = secrets.token_hex(4)


# ── Filters come from a URL, so nothing in them is trustworthy ────────

class TestFiltersFromARequest:
    def _filters(self, app, query: str):
        # from_request takes the args *mapping*, not the request. Passing
        # the request object lands in its defensive `except` and every
        # field comes back empty — which reads as "the filter did nothing"
        # rather than as a mistake in the caller.
        with app.test_request_context(f"/dashboard?{query}"):
            from flask import request
            return dm.Filters.from_request(request.args)

    def test_the_defaults_apply_to_an_empty_query(self, app):
        f = self._filters(app, "")
        assert f.period == dm.DEFAULT_PERIOD
        assert f.run_id is None
        assert not f.active

    def test_a_run_id_is_read(self, app):
        assert self._filters(app, "run=42").run_id == 42

    def test_a_non_numeric_run_id_is_ignored_rather_than_crashing(self, app):
        # It arrives from a URL somebody can edit. Refusing the whole page
        # because a query parameter is nonsense is a worse answer than
        # ignoring the parameter.
        assert self._filters(app, "run=all-of-them").run_id is None

    def test_an_unknown_period_falls_back_to_the_default(self, app):
        assert self._filters(app, "period=fortnight").period == dm.DEFAULT_PERIOD

    def test_the_text_filters_are_stripped(self, app):
        f = self._filters(app, "tester=%20alice%20&environment=%20staging%20")
        assert f.tester == "alice" and f.environment == "staging"

    def test_any_filter_makes_it_active(self, app):
        assert self._filters(app, "tester=alice").active
        assert self._filters(app, "run=1").active
        assert self._filters(app, f"period={dm.DEFAULT_PERIOD}").active is False


class TestTheFilterSurvivesInLinks:
    def test_the_default_period_is_not_repeated_in_the_query(self):
        # A link that spells out the default grows a query string for no
        # reason and makes "is anything filtered?" unanswerable by eye.
        assert dm.Filters(period=dm.DEFAULT_PERIOD).as_query() == {}

    def test_a_non_default_period_is_kept(self):
        assert dm.Filters(period="7d").as_query()["period"] == "7d"

    def test_the_run_is_kept_under_the_short_name(self):
        assert dm.Filters(run_id=7).as_query()["run"] == 7

    def test_empty_text_filters_are_dropped(self):
        # Whitespace-only is not tested here on purpose: stripping happens
        # at the boundary in from_request, so a blank-but-not-empty value
        # cannot arrive from a URL. Asserting it here would pin a
        # behaviour this method does not have and does not need.
        assert dm.Filters(tester="", suite="").as_query() == {}

    def test_text_filters_are_kept(self):
        q = dm.Filters(tester="alice", environment="staging").as_query()
        assert q == {"tester": "alice", "environment": "staging"}


class TestSince:
    def test_all_time_has_no_lower_bound(self):
        assert dm.Filters(period="all").since() is None

    def test_a_window_is_measured_backwards_from_now(self):
        since = dm.Filters(period="7d").since()
        assert since is not None
        age = datetime.now(timezone.utc) - since
        assert timedelta(days=6, hours=23) < age < timedelta(days=7, hours=1)


# ── "No filter" and "filter matched nothing" are different answers ────

class TestMatchingRunIds:
    @pytest.fixture
    def project(self, request):
        # Per test: upsert_project keys on the name, so one shared project
        # collects two more runs from every test in the class and the
        # counts drift upward as the file grows.
        pid = _db.upsert_project(f"dmf-{_RUN}-{request.node.name}")
        _db.start_execution_run(pid, {"mode": "manual", "tester_name": "alice",
                                      "environment": "staging"})
        _db.start_execution_run(pid, {"mode": "manual", "tester_name": "bob",
                                      "environment": "production"})
        return pid

    def _ids(self, project, **kw):
        with _db.session_scope() as sess:
            return dm._matching_run_ids(sess, project, dm.Filters(**kw))

    def test_no_filter_returns_none_meaning_everything(self, project):
        # None, not the full list: the caller uses it to skip the WHERE
        # clause entirely, and a list would make every query enumerate
        # every run.
        assert self._ids(project) is None

    def test_a_tester_filter_narrows(self, project):
        ids = self._ids(project, tester="alice")
        assert ids is not None and len(ids) == 1

    def test_an_environment_filter_narrows(self, project):
        ids = self._ids(project, environment="production")
        assert ids is not None and len(ids) == 1

    def test_both_together_narrow_further(self, project):
        assert self._ids(project, tester="alice",
                         environment="production") == []

    def test_a_filter_matching_nothing_returns_an_empty_list(self, project):
        """Not None. This is the distinction the module warns about.

        ``None`` means "no filter, count everything"; ``[]`` means "the
        filter matched nothing, count nothing". Collapsing them makes a
        filter for a tester who does not exist report the whole project's
        numbers as if they were hers.
        """
        assert self._ids(project, tester="nobody-at-all") == []

    def test_an_explicit_run_id_is_honoured(self, project):
        runs = _db.list_execution_runs(project)
        target = runs[0]["id"]
        assert self._ids(project, run_id=target) == [target]


# ── The golden-set scorer's error paths ──────────────────────────────

class TestRouteNormalisation:
    @pytest.mark.parametrize("intent,expected", [
        ("pack:naming", "pack:naming"),
        ("istqb:severity", "istqb:severity"),
        ("fast_path:greeting", "fast_path:greeting"),
        ("greeting", "fast_path:greeting"),
        ("gratitude", "fast_path:gratitude"),
        ("help_menu", "fast_path:default_help"),
        ("severity_recommendation", "pack:severity_priority"),
        ("istqb_glossary:failure", "istqb:glossary"),
        ("troubleshoot_upload", "fast_path:troubleshoot"),
        ("help_checkout_flow", "fast_path:module_help"),
        ("diag_live_view_empty", "fast_path:troubleshoot"),
        ("bug_summary_empty", "fast_path:troubleshoot"),
        ("something_new", "fast_path:something_new"),
    ])
    def test_each_intent_maps_to_a_route(self, intent, expected):
        assert ev.route_of(intent) == expected

    def test_no_intent_is_no_route(self):
        # Distinct from a wrong route: an unobservable route must not be
        # scored as a mismatch.
        assert ev.route_of(None) is None
        assert ev.route_of("") is None


class TestTheReportRenders:
    def _report(self):
        item = ev.Item(id="X-1", pack="process", lang="en", question="q?",
                       route="pack:process", require=(("alpha",),),
                       avoid=("bad",))
        return ev.score_all([
            (item, "alpha", "pack:process"),
            (ev.Item(id="X-2", pack="naming", lang="uk", question="q?",
                     route="pack:naming", require=(("beta",),), avoid=("bad",)),
             "beta but bad", "pack:naming"),
        ])

    def test_it_states_the_headline(self):
        text = ev.format_report(self._report())
        assert "1/2 passed" in text

    def test_it_breaks_down_by_pack(self):
        text = ev.format_report(self._report())
        assert "process" in text and "naming" in text

    def test_it_breaks_down_by_language(self):
        assert "lang uk" in ev.format_report(self._report())

    def test_wrong_advice_is_called_out_by_name(self):
        # A guard hit and a missing requirement fail differently; the
        # summary has to separate them or the harmful one gets buried.
        text = ev.format_report(self._report())
        assert "wrong advice" in text
        assert "X-2" in text

    def test_verbose_lists_the_incomplete_answers_too(self):
        item = ev.Item(id="X-3", pack="layer", lang="en", question="q?",
                       route="pack:layer", require=(("alpha",), ("beta",)))
        report = ev.score_all([(item, "alpha only", "pack:layer")])
        quiet = ev.format_report(report)
        loud = ev.format_report(report, verbose=True)
        assert "X-3" not in quiet
        assert "X-3" in loud

    def test_an_empty_report_does_not_divide_by_zero(self):
        report = ev.Report(results=[])
        assert "0/0" in ev.format_report(report)
        assert report.rate == 0.0
        assert report.route_rate == 0.0
