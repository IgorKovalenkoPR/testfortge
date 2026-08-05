"""The Estimation editor — requirement 4 (E4.6).

The first editable artefact that is not a row of columns, which is why E4.1
left it out of ``engine.editable``. The design follows from one fact:
``qa_estimator.compute_estimation`` takes every coefficient as a parameter, so
**the estimate is a pure function of its inputs** — editing it means changing
an input and calling that function again.

The acceptance criterion is "the totals in the UI equal the totals in the
database". That holds by construction here: the client sends drivers only, the
server computes, and there is exactly one implementation of every formula. So
most of these tests are about the two ways that could stop being true — a
derived value sneaking in through the API, and a second implementation
appearing in the browser.
"""
from __future__ import annotations

import secrets

import pytest

from engine import db
from engine import estimation_edit as ee

#: A token per run, not just per test.
#:
#: ``conftest`` cannot always delete the scratch database on Windows — the file
#: may still be held open — so a stable project name silently reuses the
#: previous run's rows. E4.1 preserves ``row_version`` across a pack save, so a
#: version assertion then depends on how many times the suite has been run.
#: Measured: three of these tests failed on the second invocation and passed on
#: the first.
_RUN = secrets.token_hex(4)



def _inputs(**overrides):
    base = {
        "features": [
            {"name": "Login", "test_cases": 40, "comment": "core",
             "is_section": False},
            {"name": "Checkout", "test_cases": 60, "comment": "",
             "is_section": False},
        ],
        "rate_usd": 35, "minutes_per_tc": 5, "team_size": 1,
    }
    base.update(overrides)
    return base


@pytest.fixture
def project(app, request):
    """A project with one generated estimation of its own."""
    pid = db.upsert_project(name=f"E4.6 {request.node.name} {_RUN}"[:180])
    payload = _inputs()
    result = ee.recompute(payload)
    db.save_estimation(pid, payload, result,
                       result["one_plat_total_expected"])
    return pid


@pytest.fixture
def editing_on(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
    monkeypatch.setenv("EDITORS_ENABLED", "1")
    return True


class TestValidation:

    def test_a_derived_value_cannot_be_set(self):
        """The whole point. A client that could post ``total_hours`` could
        make the page disagree with every formula behind it."""
        with pytest.raises(ee.EstimationEditError) as exc:
            ee.validate({"one_plat_total_expected": 1})
        assert "one_plat_total_expected" in str(exc.value)
        assert "Change what drives them" in str(exc.value)

    def test_the_whole_edit_is_refused_if_any_part_is_wrong(self):
        """A partly-applied estimation edit computes totals from a mixture of
        what was asked for and what was not."""
        with pytest.raises(ee.EstimationEditError):
            ee.validate({"minutes_per_tc": 5, "team_size": -3})

    @pytest.mark.parametrize("field,value", [
        ("minutes_per_tc", 0),          # below the floor
        ("minutes_per_tc", 10_000),     # above the ceiling
        ("minutes_per_tc", 5.5),        # not a whole number
        ("buffer", 0.5),                # below 1.0 would shrink the work
        ("team_size", 0),
        ("rate_usd", -1),
        ("max_testing_stretch", 0.9),
    ])
    def test_an_implausible_driver_is_refused_not_clamped(self, field, value):
        """Silently using a different number than the one somebody typed is
        how an estimate stops being trusted."""
        with pytest.raises(ee.EstimationEditError) as exc:
            ee.validate({field: value})
        assert exc.value.field_name == field

    @pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
    def test_nan_and_infinity_are_refused(self, value):
        """They survive ``float()`` and then poison every total they touch."""
        with pytest.raises(ee.EstimationEditError):
            ee.validate({"buffer": value})

    def test_a_non_numeric_driver_is_refused(self):
        with pytest.raises(ee.EstimationEditError):
            ee.validate({"minutes_per_tc": "soon"})

    def test_an_empty_edit_is_refused(self):
        with pytest.raises(ee.EstimationEditError):
            ee.validate({})


class TestFeatureValidation:

    def test_a_feature_needs_a_name(self):
        with pytest.raises(ee.EstimationEditError) as exc:
            ee.validate_features([{"name": "  ", "test_cases": 5}])
        assert "needs a name" in str(exc.value)

    def test_a_negative_count_is_refused(self):
        with pytest.raises(ee.EstimationEditError):
            ee.validate_features([{"name": "Login", "test_cases": -1}])

    def test_an_implausible_count_is_refused(self):
        with pytest.raises(ee.EstimationEditError) as exc:
            ee.validate_features([{"name": "Login", "test_cases": 999_999}])
        assert "implausible" in str(exc.value)

    def test_a_section_row_carries_no_cases(self):
        """A grouping header has no tests of its own; the estimator would
        otherwise count them twice."""
        out = ee.validate_features([{"name": "Auth", "test_cases": 40,
                                     "is_section": True}])
        assert out[0]["test_cases"] == 0

    def test_the_feature_cap_is_enforced(self):
        too_many = [{"name": f"F{i}", "test_cases": 1}
                    for i in range(ee.MAX_FEATURES + 1)]
        with pytest.raises(ee.EstimationEditError):
            ee.validate_features(too_many)

    def test_a_non_list_is_refused(self):
        with pytest.raises(ee.EstimationEditError):
            ee.validate_features({"name": "Login"})


class TestRecompute:

    def test_nothing_reimplements_a_formula(self):
        """The generator's own function, called again. A second
        implementation would drift, and the drift would surface as an estimate
        the XLSX export disagrees with."""
        import inspect
        source = inspect.getsource(ee.recompute)
        assert "compute_estimation" in source

    def test_more_minutes_per_case_means_more_hours(self):
        before = ee.recompute(_inputs(minutes_per_tc=5))
        after = ee.recompute(_inputs(minutes_per_tc=10))
        assert after["features_hours"] > before["features_hours"]
        assert after["one_plat_total_expected"] > \
            before["one_plat_total_expected"]

    def test_the_rate_moves_cost_and_not_hours(self):
        before = ee.recompute(_inputs(rate_usd=35))
        after = ee.recompute(_inputs(rate_usd=70))
        assert after["cost_one_expected"] > before["cost_one_expected"]
        assert after["one_plat_total_expected"] == \
            before["one_plat_total_expected"]

    def test_the_test_case_total_follows_the_features(self):
        result = ee.recompute(_inputs())
        assert result["total_tc"] == 100

    def test_a_bigger_team_adds_overhead_rather_than_dividing_the_work(self):
        """Brooks's Law is in the estimator, and this pins that the editor
        cannot be used to "reduce" an estimate by claiming more people."""
        solo = ee.recompute(_inputs(team_size=1))
        crowd = ee.recompute(_inputs(team_size=6))
        assert crowd["brooks_overhead_hours"] > 0
        assert crowd["pert_expected"] >= solo["pert_expected"]


class TestApply:

    def test_the_stored_total_equals_the_computed_one(self, project,
                                                     editing_on):
        """The acceptance criterion, stated directly."""
        state = ee.apply(project, {"minutes_per_tc": 10})
        stored = db.latest_estimation(project)
        assert stored["total_hours"] == \
            state["result"]["one_plat_total_expected"]
        assert stored["result_payload"]["features_hours"] == \
            state["result"]["features_hours"]

    def test_editing_flips_provenance_and_bumps_the_version(self, project,
                                                           editing_on):
        state = ee.apply(project, {"minutes_per_tc": 10}, actor="lead")
        assert state["ai_generated"] is False
        assert state["row_version"] == 2

    def test_the_generators_numbers_are_kept(self, project, editing_on):
        before = ee.get(project)["result"]["one_plat_total_expected"]
        state = ee.apply(project, {"minutes_per_tc": 10})
        assert state["original"]["one_plat_total_expected"] == before

    def test_a_second_edit_keeps_the_generators_original_not_the_first_edit(
            self, project, editing_on):
        """"The model said X" is always the interesting comparison."""
        first = ee.get(project)["result"]["one_plat_total_expected"]
        ee.apply(project, {"minutes_per_tc": 10})
        state = ee.apply(project, {"minutes_per_tc": 12})
        assert state["original"]["one_plat_total_expected"] == first

    def test_the_diff_names_the_headline_numbers(self, project, editing_on):
        state = ee.apply(project, {"minutes_per_tc": 10})
        labels = {row["label"] for row in state["diff"]}
        assert "One platform — expected" in labels
        assert "Cost, one platform" in labels
        for row in state["diff"]:
            assert row["delta"] is not None

    def test_an_unedited_estimation_has_no_diff(self, project, editing_on):
        state = ee.get(project)
        assert state["original"] == {}
        assert state["diff"] == []

    def test_a_no_op_is_not_a_write(self, project, editing_on):
        """It would bump the version and manufacture a conflict for a
        colleague who is mid-edit."""
        state = ee.apply(project, {"minutes_per_tc": 5})
        assert state["row_version"] == 1
        assert state["ai_generated"] is True

    def test_a_stale_version_is_a_conflict(self, project, editing_on):
        ee.apply(project, {"minutes_per_tc": 10})
        with pytest.raises(db.WriteConflict):
            ee.apply(project, {"minutes_per_tc": 12}, expected_version=1)

    def test_editing_the_feature_list_recomputes_the_total(self, project,
                                                          editing_on):
        state = ee.apply(project, {"features": [
            {"name": "Login", "test_cases": 10, "comment": "",
             "is_section": False}]})
        assert state["result"]["total_tc"] == 10

    def test_an_edit_is_audited(self, project, editing_on):
        ee.apply(project, {"minutes_per_tc": 10}, actor="lead")
        rows = [row for row in db.list_audit(project_id=project, limit=10)
                if row["entity"] == "estimation"]
        assert rows and rows[0]["action"] == "update"

    def test_editing_a_project_with_no_estimation_says_so(self, app,
                                                          editing_on):
        empty = db.upsert_project(name=f"E4.6 nothing to edit {_RUN}")
        with pytest.raises(ee.EstimationEditError) as exc:
            ee.apply(empty, {"minutes_per_tc": 10})
        assert "no estimation" in str(exc.value).lower()


class TestRevert:

    def test_it_restores_the_generators_numbers(self, project, editing_on):
        original = ee.get(project)["result"]["one_plat_total_expected"]
        ee.apply(project, {"minutes_per_tc": 10})
        state = ee.revert(project)
        assert state["result"]["one_plat_total_expected"] == original

    def test_provenance_goes_back_to_generated(self, project, editing_on):
        """After a revert the row *is* what the generator produced, and
        E4.7's merge should treat it that way."""
        ee.apply(project, {"minutes_per_tc": 10})
        state = ee.revert(project)
        assert state["ai_generated"] is True
        assert state["diff"] == []

    def test_reverting_an_unedited_estimation_says_so(self, project,
                                                     editing_on):
        with pytest.raises(ee.EstimationEditError) as exc:
            ee.revert(project)
        assert "nothing to put back" in str(exc.value)

    def test_a_stale_version_is_a_conflict(self, project, editing_on):
        ee.apply(project, {"minutes_per_tc": 10})
        with pytest.raises(db.WriteConflict):
            ee.revert(project, expected_version=1)


class TestEndpoints:

    def test_the_state_comes_back_with_its_inputs(self, client, project,
                                                  editing_on):
        headers = _prepare(client, project)
        resp = client.get("/api/edit/estimation", headers=headers)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["item"]["inputs"]["minutes_per_tc"] == 5
        assert "rate_usd" in body["inputs"]

    def test_a_patch_recomputes_and_returns_the_new_state(self, client,
                                                          project,
                                                          editing_on):
        headers = _prepare(client, project)
        resp = client.patch("/api/edit/estimation",
                            json={"changes": {"minutes_per_tc": 10},
                                  "row_version": 1}, headers=headers)
        assert resp.status_code == 200
        item = resp.get_json()["item"]
        assert item["row_version"] == 2
        assert item["diff"]

    def test_a_derived_field_is_a_400_naming_it(self, client, project,
                                                editing_on):
        headers = _prepare(client, project)
        resp = client.patch("/api/edit/estimation",
                            json={"changes": {"cost_one_expected": 1}},
                            headers=headers)
        assert resp.status_code == 400
        assert "cost_one_expected" in resp.get_json()["message"]

    def test_a_bad_driver_names_the_field_for_the_ui(self, client, project,
                                                    editing_on):
        headers = _prepare(client, project)
        resp = client.patch("/api/edit/estimation",
                            json={"changes": {"team_size": 0}},
                            headers=headers)
        assert resp.status_code == 400
        assert resp.get_json()["field"] == "team_size"

    def test_a_stale_version_is_a_409(self, client, project, editing_on):
        headers = _prepare(client, project)
        client.patch("/api/edit/estimation",
                     json={"changes": {"minutes_per_tc": 10}},
                     headers=headers)
        resp = client.patch("/api/edit/estimation",
                            json={"changes": {"minutes_per_tc": 12},
                                  "row_version": 1}, headers=headers)
        assert resp.status_code == 409

    def test_revert_works_over_http(self, client, project, editing_on):
        headers = _prepare(client, project)
        client.patch("/api/edit/estimation",
                     json={"changes": {"minutes_per_tc": 10}},
                     headers=headers)
        resp = client.post("/api/edit/estimation/revert", json={},
                           headers=headers)
        assert resp.status_code == 200
        assert resp.get_json()["item"]["ai_generated"] is True

    def test_both_endpoints_need_a_csrf_token(self, client, project,
                                             editing_on):
        with client.session_transaction() as sess:
            sess["project_id"] = project
        client.application.config["WTF_CSRF_ENABLED"] = True
        try:
            assert client.patch("/api/edit/estimation",
                                json={"changes": {"minutes_per_tc": 10}}
                                ).status_code == 400
            assert client.post("/api/edit/estimation/revert",
                               json={}).status_code == 400
        finally:
            client.application.config["WTF_CSRF_ENABLED"] = False

    @pytest.mark.parametrize("method,path", [
        ("get", "/api/edit/estimation"),
        ("patch", "/api/edit/estimation"),
        ("post", "/api/edit/estimation/revert"),
    ])
    def test_nothing_is_reachable_with_editing_off(self, client, project,
                                                    monkeypatch, method,
                                                    path):
        monkeypatch.setenv("EDITORS_ENABLED", "0")
        headers = _prepare(client, project)
        resp = getattr(client, method)(path, json={"changes": {"team_size": 2}},
                                       headers=headers)
        assert resp.status_code == 404
        assert resp.get_json()["error"] == "editors_disabled"


class TestPage:

    def test_the_drivers_are_offered(self, client, project, editing_on):
        body = _render(client, project)
        for name in ("minutes_per_tc", "rate_usd", "buffer", "team_size",
                     "pm_overhead", "max_testing_stretch"):
            assert f'data-est-input="{name}"' in body, name

    def test_the_features_become_editable(self, client, project, editing_on):
        body = _render(client, project)
        assert 'data-est-feature="0"' in body
        assert 'data-est-field="test_cases"' in body
        assert 'data-est-field="name"' in body

    def test_recalculate_carries_the_row_version(self, client, project,
                                                 editing_on):
        body = _render(client, project)
        assert 'id="est-recalculate"' in body
        assert 'data-est-version="1"' in body

    def test_revert_appears_only_once_there_is_an_original(self, client,
                                                           project,
                                                           editing_on):
        assert 'id="est-revert"' not in _render(client, project)
        ee.apply(project, {"minutes_per_tc": 10})
        assert 'id="est-revert"' in _render(client, project)

    def test_the_diff_table_appears_after_an_edit(self, client, project,
                                                 editing_on):
        ee.apply(project, {"minutes_per_tc": 10})
        body = _render(client, project)
        assert "est-diff-table" in body
        assert "One platform — expected" in body

    def test_the_editor_is_absent_with_the_flag_off(self, client, project,
                                                    monkeypatch):
        monkeypatch.setenv("EDITORS_ENABLED", "0")
        body = _render(client, project)
        for marker in ('id="est-editor"', "data-est-input", "data-est-feature",
                       "js/est-editor.js"):
            assert marker not in body, marker

    def test_the_numbers_still_render_with_the_flag_off(self, client, project,
                                                        monkeypatch):
        monkeypatch.setenv("EDITORS_ENABLED", "0")
        body = _render(client, project)
        assert "Login" in body and "Checkout" in body

    def test_a_project_with_no_estimation_renders_without_the_panel(
            self, client, app, editing_on):
        empty = db.upsert_project(name=f"E4.6 page with nothing {_RUN}")
        body = _render(client, empty)
        assert 'id="est-editor"' not in body


class TestNoArithmeticInTheBrowser:
    """The other way "UI totals == DB totals" could stop being true.

    A live preview computed in JavaScript would be a second implementation of
    the estimator, and the first time the two disagreed the page would be
    quietly wrong about a figure somebody is about to send a client.
    """

    @staticmethod
    def _code(*, strings=True):
        """The file with comments — and optionally string literals — removed.

        Both have to go before looking for arithmetic: ``/`` appears in every
        URL and in every trailing comment, so a plain substring search over the
        raw text finds "division" in ``'/api/edit/estimation'`` and in the
        sentence explaining that nothing is divided.
        """
        import pathlib
        import re
        body = pathlib.Path("static/js/est-editor.js").read_text(
            encoding="utf-8")
        body = re.sub(r"/\*.*?\*/", "", body, flags=re.DOTALL)
        body = re.sub(r"//.*$", "", body, flags=re.MULTILINE)
        if strings:
            # Double quotes first: a double-quoted string may legitimately
            # contain an apostrophe ("the model's numbers"), and stripping
            # single quotes first turns that apostrophe into the start of a
            # phantom string that swallows the rest of the file.
            body = re.sub(r'"(?:\\.|[^"\\])*"', '""', body)
            body = re.sub(r"'(?:\\.|[^'\\])*'", "''", body)
            body = re.sub(r"`(?:\\.|[^`\\])*`", "``", body)
        return body

    def test_the_editor_does_no_number_formatting_or_maths(self):
        """Not a search for ``/`` and ``*``: those appear in every URL, and
        their absence would not prove much either — arithmetic can be done
        with ``+``. What is checkable and meaningful is that no rounding,
        formatting or maths library is in here at all, because every number on
        that page arrives already computed."""
        code = self._code()
        for forbidden in ("Math.", "toFixed", "toPrecision", "parseFloat"):
            assert forbidden not in code, forbidden

    def test_it_reloads_rather_than_re_rendering_the_numbers(self):
        assert "window.location.reload()" in self._code()

    def test_it_sends_no_derived_value(self):
        code = self._code()
        for derived in ("total_hours", "one_plat", "cost_", "pert_"):
            assert derived not in code, derived

    def test_it_uses_the_shared_csrf_plumbing(self):
        code = self._code()
        assert "window.TestFortgeEditor" in code
        assert "X-CSRFToken" not in code


# ── Helpers ───────────────────────────────────────────────────────

def _prepare(client, project):
    with client.session_transaction() as sess:
        sess["project_id"] = project
    token = client.get("/api/csrf-token").get_json()["token"]
    return {"X-CSRFToken": token}


def _render(client, project):
    with client.session_transaction() as sess:
        sess["project_id"] = project
    resp = client.get("/estimation")
    assert resp.status_code == 200
    return resp.get_data(as_text=True)
