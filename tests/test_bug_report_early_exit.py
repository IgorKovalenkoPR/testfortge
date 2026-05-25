"""Stage 4 — unit tests for :func:`engine.bug_report.create_bug_from_early_exit`.

Stage 3 (LiveExecutor) writes an ``early_exit_reason`` string into the
worker's ``result.json`` when its OomGuard or wall-clock deadline trips
mid-run. Without Stage 4 that string only surfaced as a status string in
the UI live tab; operators had no backlog entry to follow up against
(raise the memory budget, shrink the URL plan, profile a leak).

These tests pin the factory's behaviour so a future refactor of the
LiveExecutor's reason strings does not silently break bug-creation. The
factory is pure (no DB, no Flask) so we test it directly — the
end-to-end wiring through ``/test-execution/results`` lives in
``test_walkthrough_ui_wiring.py``.
"""

from __future__ import annotations

from engine.bug_report import create_bug_from_early_exit


class TestOomEarlyExit:
    """``oom_budget_exceeded`` and the ``_after_tcs`` variant LiveExecutor
    emits when the guard trips after running TCs on the current page."""

    def test_oom_basic_reason_becomes_major_bug(self):
        bug = create_bug_from_early_exit(
            "oom_budget_exceeded (412 MB > 400 MB)",
            run_id="20260525_120000_abc",
            base_url="https://example.com/",
            environment_str="Web / Chrome",
            tester_name="ci",
        )
        # Severity reflects "partial run completes" — Major / High.
        assert bug.severity == "Major"
        assert bug.priority == "High"
        # Title contains "out-of-memory" so the backlog board reader
        # can recognise the row at a glance.
        assert "out-of-memory" in bug.title.lower()
        # Labels carry the defect class + source so /bug-reports
        # filters work the same way as walkthrough findings.
        assert "defect:early_exit_oom" in bug.labels
        assert "source:live_executor" in bug.labels
        assert "area:test_run_infra" in bug.labels
        # Linked-item-type splits live_executor infra bugs from
        # ``walkthrough`` findings and TC-driven defects on the listing
        # screen.
        assert bug.linked_item_type == "live_executor"
        assert bug.linked_item_id == "20260525_120000_abc"
        # Reason string is preserved verbatim in actual_result so
        # operators can grep for the RSS / budget numbers.
        assert "412 MB > 400 MB" in bug.actual_result
        # Component name surfaces in the bug-report listing so multi-
        # project boards can filter on infra rows.
        assert bug.component == "Test Run Infrastructure"

    def test_oom_after_tcs_variant_is_recognised(self):
        """LiveExecutor writes ``oom_budget_exceeded_after_tcs`` when
        the guard trips after running TCs on the current page (the
        second OOM check inside ``run``)."""
        bug = create_bug_from_early_exit(
            "oom_budget_exceeded_after_tcs (450 MB > 400 MB)",
            base_url="https://example.com/",
        )
        # Same defect class and severity as the plain OOM variant.
        assert "defect:early_exit_oom" in bug.labels
        assert bug.severity == "Major"


class TestWallClockEarlyExit:
    """``wall_deadline_exceeded`` — softer severity than OOM because the
    deadline is operator-configured rather than a leak."""

    def test_wall_clock_reason_becomes_minor_bug(self):
        bug = create_bug_from_early_exit(
            "wall_deadline_exceeded",
            run_id="20260525_120000_xyz",
            base_url="https://example.com/",
        )
        # Wall-clock = "operator told us to stop" — Minor/Medium.
        assert bug.severity == "Minor"
        assert bug.priority == "Medium"
        assert "wall-clock" in bug.title.lower()
        assert "defect:early_exit_wall_clock" in bug.labels
        assert "source:live_executor" in bug.labels
        # Run-id is preserved as the linked item even without findings.
        assert bug.linked_item_id == "20260525_120000_xyz"


class TestUnknownEarlyExit:
    """Future-proofing: an unknown early-exit string surfaces as a Major
    bug rather than silently disappearing into the void."""

    def test_unknown_reason_still_creates_a_bug(self):
        bug = create_bug_from_early_exit(
            "engine_panic: something unexpected",
            base_url="https://example.com/",
        )
        assert bug.severity == "Major"
        assert "defect:early_exit_unknown" in bug.labels
        assert "source:live_executor" in bug.labels
        # The raw reason makes it into the actual_result so operators
        # can route the unknown signal to whoever owns LiveExecutor.
        assert "engine_panic" in bug.actual_result

    def test_empty_run_id_falls_back_to_sentinel(self):
        """Without a run id the bug must still link somewhere
        — ``LIVE-RUN`` is the documented sentinel."""
        bug = create_bug_from_early_exit(
            "oom_budget_exceeded (500 MB > 400 MB)",
        )
        assert bug.linked_item_id == "LIVE-RUN"


class TestStepsAndPreconditions:
    """The factory must always produce non-empty ISTQB-mandatory
    fields (steps, preconditions, actual, expected) — otherwise the
    bug-listing UI shows a half-empty row."""

    def test_mandatory_fields_are_never_empty(self):
        bug = create_bug_from_early_exit(
            "oom_budget_exceeded (412 MB > 400 MB)",
            base_url="https://example.com/",
            environment_str="Web / Chrome",
        )
        assert bug.steps_to_reproduce.strip()
        assert bug.preconditions.strip()
        assert bug.actual_result.strip()
        assert bug.expected_result.strip()
        # base_url surfaces in both preconditions and step 1 so the
        # row is reproducible by a human reader.
        assert "https://example.com/" in bug.preconditions
        assert "https://example.com/" in bug.steps_to_reproduce

    def test_cases_progress_lands_in_actual_when_provided(self):
        bug = create_bug_from_early_exit(
            "oom_budget_exceeded (500 MB > 400 MB)",
            cases_done=12,
            cases_total=37,
        )
        assert "12 of 37" in bug.actual_result
