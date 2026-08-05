"""The Dashboard — requirement 10 (E7).

Four things under test, in the order they matter:

* the numbers are **counted by the database** and match the shape the template
  and the trend endpoint already read (E7.1);
* they are **this project's** numbers and nobody else's (E7.7 — the isolation
  the whole programme exists for);
* the filters narrow what is counted, and the export follows the filter (E7.4,
  E7.6);
* targets colour a tile only when there is something to colour (E7.3).
"""
from __future__ import annotations

import secrets

import pytest

from engine import dashboard_config as cfg
from engine import dashboard_metrics as dm
from engine import db

#: A token per run — see the note in tests/test_public_ids.py.
_RUN = secrets.token_hex(4)


def _tc(index, *, category="Positive", priority="High", suite=""):
    return {"id": f"SC1_{index:03d}", "section": "Auth", "section_num": 1,
            "summary": f"Case {index}", "preconditions": "",
            "test_steps": "1. Open", "test_data": "",
            "expected_result": "ok", "issues": "", "comment": "",
            "user_story_id": "", "category": category, "priority": priority,
            "status": "Unchecked", "testing_type": "Functional",
            "suite": suite}


def _bug(index, *, severity="Major", status="Open"):
    return {"id": f"BUG-{index:03d}", "title": f"Bug {index}",
            "severity": severity, "priority": "High", "status": status,
            "environment": "Chrome", "preconditions": "",
            "steps_to_reproduce": "1. Open", "actual_result": "500",
            "expected_result": "opens", "comment": "", "assignee": "",
            "bug_area": "", "browser": "", "os": "", "version": ""}


@pytest.fixture(autouse=True)
def dashboard_v2(monkeypatch):
    """E7 ships behind ``DASHBOARD_V2``, like every other epic here.

    ``features`` makes it conditional on ``WORKSPACE_DB_FIRST`` — aggregating
    in SQL means the database is the source of truth, so it cannot come on
    before that does.
    """
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("DASHBOARD_V2", "1")


@pytest.fixture
def project(app, request):
    pid = db.upsert_project(name=f"E7 {request.node.name} {_RUN}"[:180])
    db.save_test_cases(pid, [
        _tc(1, category="Positive", priority="High", suite="Smoke"),
        _tc(2, category="Positive", priority="Low", suite="Smoke"),
        _tc(3, category="Negative", priority="High"),
    ])
    db.save_checklist(pid, [{
        "id": "HDR_001", "section": "Header", "item_num": "1.1", "depth": 2,
        "objective": "Logo links home", "comments": "", "user_story_id": "",
        "category": "Positive", "priority": "High", "status": "Unchecked",
        "testing_type": "Functional"}])
    db.save_bug(pid, _bug(1, severity="Major"), source="manual")
    db.save_bug(pid, _bug(2, severity="Minor", status="Closed"),
                source="manual")
    return pid


class TestTheContract:
    """The metrics dict's shape is load-bearing — its own docstring says so."""

    def test_it_returns_exactly_the_documented_keys(self, project):
        from engine.test_metrics_generator import compute_session_metrics
        assert set(dm.aggregate(project).keys()) == \
            set(compute_session_metrics().keys())

    def test_no_project_falls_back_to_the_session_aggregator(self, app):
        """The anonymous, pre-project flow: there is nothing to count."""
        from engine.test_metrics_generator import compute_session_metrics
        assert dm.aggregate("") == compute_session_metrics()

    def test_a_database_failure_returns_the_empty_shape(self, project,
                                                       monkeypatch):
        """A dashboard that raises is worse than one that says it has
        nothing."""
        monkeypatch.setattr(db, "session_scope",
                            lambda: (_ for _ in ()).throw(RuntimeError("down")))
        metrics = dm.aggregate(project)
        assert metrics["has_data"] is False
        assert metrics["tc_total"] == 0


class TestCounting:

    def test_totals_and_groupings(self, project):
        metrics = dm.aggregate(project)
        assert metrics["tc_total"] == 3
        assert metrics["tc_by_category"] == {"Positive": 2, "Negative": 1}
        assert metrics["tc_by_priority"] == {"High": 2, "Low": 1}
        assert metrics["cl_total"] == 1
        assert metrics["bug_total"] == 2
        assert metrics["bug_by_severity"] == {"Major": 1, "Minor": 1}
        assert metrics["bug_by_status"] == {"Open": 1, "Closed": 1}

    def test_has_data_reflects_reality(self, app, project):
        empty = db.upsert_project(name=f"E7 empty {_RUN}")
        assert dm.aggregate(empty)["has_data"] is False
        assert dm.aggregate(project)["has_data"] is True

    def test_an_empty_value_is_grouped_as_unspecified(self, app, request):
        """Not dropped: three cases with no category are three cases, and a
        chart that omits them adds up to less than the total."""
        pid = db.upsert_project(name=f"E7 blank {request.node.name} {_RUN}")
        db.save_test_cases(pid, [_tc(1, category=""), _tc(2, category="")])
        assert dm.aggregate(pid)["tc_by_category"] == {"Unspecified": 2}

    def test_the_pass_rate_is_zero_rather_than_an_error_with_no_runs(
            self, project):
        assert dm.aggregate(project)["exec_pass_rate"] == 0.0


class TestIsolation:
    """What the whole programme is for: one project's numbers are its own."""

    def test_another_projects_rows_are_not_counted(self, project, app):
        other = db.upsert_project(name=f"E7 neighbour {_RUN}")
        db.save_test_cases(other, [_tc(9) for _ in range(1)])
        db.save_bug(other, _bug(9), source="manual")
        assert dm.aggregate(project)["tc_total"] == 3
        assert dm.aggregate(project)["bug_total"] == 2
        assert dm.aggregate(other)["tc_total"] == 1
        assert dm.aggregate(other)["bug_total"] == 1

    def test_an_unknown_project_counts_nothing(self, app):
        metrics = dm.aggregate("does-not-exist")
        assert metrics["tc_total"] == 0 and metrics["bug_total"] == 0

    def test_targets_are_per_project(self, project, app):
        other = db.upsert_project(name=f"E7 targets neighbour {_RUN}")
        cfg.set_targets(project, {"exec_pass_rate": "95"})
        assert cfg.targets(project)["exec_pass_rate"] == 95.0
        assert cfg.targets(other)["exec_pass_rate"] == 90.0, "the default"

    def test_layout_is_per_person(self, app):
        cfg.set_widgets(f"alice-{_RUN}", ["metrics"])
        assert cfg.widgets(f"alice-{_RUN}") == ["metrics"]
        assert cfg.widgets(f"bob-{_RUN}") == list(cfg.DEFAULT_WIDGETS)


class TestFilters:

    def test_a_suite_filter_narrows_the_test_cases(self, project):
        filtered = dm.aggregate(project, dm.Filters(suite="Smoke"))
        assert filtered["tc_total"] == 2

    def test_a_suite_filter_scopes_the_checklist_to_zero(self, project):
        """A suite is a property of a test case. Reporting the unfiltered
        checklist beside filtered test cases would be a wrong comparison
        presented as a right one."""
        assert dm.aggregate(project, dm.Filters(suite="Smoke"))["cl_total"] == 0

    def test_an_unmatched_run_filter_counts_nothing_executed(self, project):
        """A filter nothing matches means zero, not everything — the
        distinction ``_matching_run_ids`` returns None for."""
        filtered = dm.aggregate(project, dm.Filters(run_id=99999))
        assert filtered["exec_total"] == 0
        assert filtered["runs_count"] == 0
        assert filtered["bug_total"] == 0, \
            "bugs are scoped to the runs in view too"

    def test_no_filter_counts_everything(self, project):
        assert dm.aggregate(project, dm.Filters())["bug_total"] == 2

    @pytest.mark.parametrize("query,expected", [
        ({"period": "7d"}, "7d"),
        ({"period": "nonsense"}, dm.DEFAULT_PERIOD),
        ({}, dm.DEFAULT_PERIOD),
    ])
    def test_a_bad_period_falls_back_rather_than_failing(self, query,
                                                        expected):
        assert dm.Filters.from_request(query).period == expected

    def test_a_non_numeric_run_is_ignored(self):
        assert dm.Filters.from_request({"run": "latest"}).run_id is None

    def test_active_is_false_for_a_fresh_visit(self):
        assert dm.Filters.from_request({}).active is False
        assert dm.Filters.from_request({"suite": "Smoke"}).active is True

    def test_as_query_keeps_only_what_was_set(self):
        filters = dm.Filters(period="7d", suite="Smoke")
        assert filters.as_query() == {"period": "7d", "suite": "Smoke"}

    def test_the_options_come_from_this_projects_own_data(self, project):
        """Offering a suite nothing is tagged with produces a filter that
        returns nothing and looks broken."""
        assert dm.options(project).suites == ["Smoke"]

    def test_options_for_no_project_are_empty(self, app):
        assert dm.options("").suites == []


class TestTargetsAndRag:

    def test_a_kpi_without_a_target_has_no_status(self, project):
        rows = {row["key"]: row for row in
                cfg.evaluate(dm.aggregate(project), {})}
        assert rows["tc_total"]["status"] == ""

    def test_higher_is_better_and_lower_is_better_differ(self):
        rows = {row["key"]: row for row in cfg.evaluate(
            {"exec_total": 10, "exec_pass_rate": 95, "bug_total": 50},
            {"exec_pass_rate": 90, "bug_total": 10})}
        assert rows["exec_pass_rate"]["status"] == "green"
        assert rows["bug_total"]["status"] == "red", \
            "more bugs than target is not green"

    def test_a_ratio_with_no_denominator_has_no_status(self):
        """Measured: a pass rate of 0% against a 90% target came out red on a
        fresh project, which tells a team they are failing when nothing has
        been executed. A red dashboard on day one is one people ignore."""
        rows = {row["key"]: row for row in cfg.evaluate(
            {"exec_total": 0, "exec_pass_rate": 0}, {"exec_pass_rate": 90})}
        assert rows["exec_pass_rate"]["status"] == ""
        assert rows["exec_pass_rate"]["no_data"] is True

    def test_every_kpi_key_exists_in_the_metrics_dict(self):
        """A typo here is a tile that silently reports zero."""
        from engine.test_metrics_generator import compute_session_metrics
        keys = set(compute_session_metrics().keys())
        for kpi in cfg.KPIS:
            assert kpi.key in keys, kpi.key

    def test_an_empty_target_clears_it_rather_than_storing_zero(self, project):
        """A form that turns a cleared field into 0 would paint every KPI
        green."""
        cfg.set_targets(project, {"bug_total": "5"})
        cfg.set_targets(project, {"bug_total": ""})
        assert "bug_total" not in cfg.targets(project)

    def test_junk_is_refused_by_name(self, project):
        with pytest.raises(ValueError) as exc:
            cfg.set_targets(project, {"exec_pass_rate": "ninety"})
        assert "exec_pass_rate" in str(exc.value)

    def test_an_unknown_kpi_is_dropped(self, project):
        cfg.set_targets(project, {"nonsense": "1"})
        assert "nonsense" not in cfg.targets(project)


class TestLayout:

    def test_hiding_a_widget_actually_hides_it(self):
        """The first version stored only the visible list and repaired it by
        appending anything missing, so hiding was impossible — caught by its
        own smoke test."""
        owner = f"hide-{_RUN}"
        cfg.set_widgets(owner, ["metrics"])
        assert cfg.widgets(owner) == ["metrics"]
        assert "kpis" in cfg.hidden_widgets(owner)

    def test_a_widget_added_by_a_release_appears(self):
        """Somebody who saved a layout before a widget existed must still see
        it."""
        owner = f"newcomer-{_RUN}"
        db.set_user_setting(owner, "dashboard_widgets",
                            {"order": ["metrics"], "hidden": []})
        assert set(cfg.widgets(owner)) == set(cfg.DEFAULT_WIDGETS)

    def test_the_old_bare_list_shape_is_still_read(self):
        owner = f"legacy-{_RUN}"
        db.set_user_setting(owner, "dashboard_widgets", ["metrics", "kpis"])
        assert cfg.widgets(owner)[:2] == ["metrics", "kpis"]

    def test_an_unknown_widget_name_is_dropped(self):
        owner = f"junk-{_RUN}"
        db.set_user_setting(owner, "dashboard_widgets",
                            {"order": ["metrics", "nope"], "hidden": []})
        assert "nope" not in cfg.widgets(owner)

    def test_a_duplicate_is_not_rendered_twice(self):
        owner = f"dupe-{_RUN}"
        cfg.set_widgets(owner, ["metrics", "metrics"])
        assert cfg.widgets(owner).count("metrics") == 1

    def test_every_default_widget_has_a_label(self):
        """A widget with no label renders a checkbox with its internal name."""
        for name in cfg.DEFAULT_WIDGETS:
            assert name in cfg.WIDGET_LABELS


class TestThePage:

    @staticmethod
    def _open(client, project, **query):
        with client.session_transaction() as sess:
            sess["project_id"] = project
        resp = client.get("/", query_string=query)
        assert resp.status_code == 200
        return resp.get_data(as_text=True)

    def test_the_filters_and_kpis_render(self, client, project):
        body = self._open(client, project)
        assert 'name="period"' in body
        assert "dash-kpi-grid" in body
        assert "dash-customise" in body

    def test_the_suite_filter_is_offered_and_kept(self, client, project):
        body = self._open(client, project, suite="Smoke")
        assert 'value="Smoke"' in body
        assert "dash-filter-note" in body, "the page says it is filtered"

    def test_a_hidden_widget_is_not_rendered(self, client, project):
        from engine.permissions import current_user_id  # noqa: F401
        with client.session_transaction() as sess:
            sess["project_id"] = project
        # The layout form posts the visible set.
        client.post("/dashboard/layout", data={"widgets": ["kpis"]})
        body = self._open(client, project)
        assert "dash-kpi-grid" in body
        assert "Test Metrics" not in body

    def test_targets_can_be_saved_and_show_up(self, client, project):
        with client.session_transaction() as sess:
            sess["project_id"] = project
        client.post("/dashboard/targets", data={"target_bug_total": "1"})
        assert cfg.targets(project)["bug_total"] == 1.0
        assert "dash-kpi-" in self._open(client, project)

    def test_the_export_is_a_csv_of_what_is_on_screen(self, client, project):
        with client.session_transaction() as sess:
            sess["project_id"] = project
        resp = client.get("/dashboard/export.csv")
        assert resp.status_code == 200
        assert resp.mimetype == "text/csv"
        body = resp.get_data(as_text=True)
        assert "Test cases by category" in body
        assert "Negative" in body

    def test_the_export_follows_the_filter(self, client, project):
        """A download that silently ignores the filter is one somebody pastes
        into a report as if it matched."""
        with client.session_transaction() as sess:
            sess["project_id"] = project
        resp = client.get("/dashboard/export.csv",
                          query_string={"suite": "Smoke"})
        text = resp.get_data(as_text=True)
        assert "Filtered by" in text
        assert "suite=Smoke" in text

    def test_setting_targets_needs_admin(self, client, project, monkeypatch):
        """A target is a team agreement, so it is not a personal setting."""
        monkeypatch.setenv("AUTH_ENABLED", "1")
        monkeypatch.setenv("ORG_MODE", "1")
        monkeypatch.setattr("engine.permissions.has_role",
                            lambda role: role != "admin")
        with client.session_transaction() as sess:
            sess["project_id"] = project
        resp = client.post("/dashboard/targets",
                           data={"target_bug_total": "3"})
        assert resp.status_code in (302, 401, 403)
        assert "bug_total" not in cfg.targets(project)


class TestTheFlag:
    """Off, the page is what shipped before."""

    def test_the_new_blocks_are_absent_with_the_flag_off(self, client,
                                                        project, monkeypatch):
        monkeypatch.setenv("DASHBOARD_V2", "0")
        with client.session_transaction() as sess:
            sess["project_id"] = project
        body = client.get("/").get_data(as_text=True)
        for marker in ("dash-filters", "dash-kpi-grid", "dash-customise"):
            assert marker not in body, marker

    def test_the_old_metrics_panel_still_renders_with_the_flag_off(
            self, client, project, monkeypatch):
        """Hiding a widget is a v2 feature, so with the flag off every block
        shows regardless of a stored preference."""
        monkeypatch.setenv("DASHBOARD_V2", "0")
        cfg.set_widgets(f"nobody-{_RUN}", [])
        with client.session_transaction() as sess:
            sess["project_id"] = project
        assert "Test Metrics" in client.get("/").get_data(as_text=True)

    def test_it_requires_the_workspace_flag(self, monkeypatch):
        """Declared in E0.3 and read nowhere until E7 — the dependency was
        already in ``features._REQUIRES``."""
        from engine import features
        monkeypatch.setenv("DASHBOARD_V2", "1")
        monkeypatch.delenv("WORKSPACE_DB_FIRST", raising=False)
        assert features.effective("DASHBOARD_V2") is False


class TestSnapshotsAreNotWrittenByAPageView:
    """E7.5.

    Snapshots used to be written opportunistically on dashboard load, throttled
    by a module-level dict. That throttle is per *process* — gunicorn runs
    several workers, so "once per hour" was once per hour per worker, and a
    restart reset it. A project nobody opened was never sampled at all, so the
    trend series had holes exactly where a team stopped looking. app.py's daily
    worker is the one writer now.
    """

    def test_the_module_no_longer_holds_a_throttle(self):
        import routes.dashboard as dashboard
        assert not hasattr(dashboard, "_LAST_SNAPSHOT_AT")
        assert not hasattr(dashboard, "_maybe_snapshot_metrics")

    def test_opening_the_dashboard_writes_no_snapshot(self, client, project):
        before = len(db.list_metric_snapshots(project, limit=50) or [])
        with client.session_transaction() as sess:
            sess["project_id"] = project
        for _ in range(3):
            client.get("/")
        after = len(db.list_metric_snapshots(project, limit=50) or [])
        assert after == before, "rendering a page is not a write"

    def test_the_daily_worker_still_exists(self):
        """Removing the opportunistic write only makes sense because something
        else samples every project."""
        import pathlib
        body = pathlib.Path("app.py").read_text(encoding="utf-8")
        assert "TESTFORTGE_SNAPSHOT_WORKER" in body
        assert "save_metric_snapshot" in body or "snapshot_metrics_from_db" in body
