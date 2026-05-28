"""PR-A — runner-side multi-locator chain tests.

The previous test file (``test_locator_registry.py``) covers the pure
data + DB helpers. This file pins the contract for
``AutomationRunner._try_locator_chain``: walk
``[target, *target_alternates]``, return the first Locator that
``wait_for(state='visible')`` resolves, raise ``AssertionError`` when
every candidate times out, and consult / update the registry only
when ``project_id`` AND ``locator_label`` are both set.

A fake ``page`` is enough — we only exercise ``page.locator(sel)`` plus
the locator's ``.first`` / ``.wait_for(state=..., timeout=...)``. No
Playwright runtime required.
"""
from __future__ import annotations

import pytest

from engine import db
from engine.automation_qa import AutomationStep
from engine.automation_runner import AutomationRunner
from engine.locator_registry import LocatorCandidate, register_candidates


class _FakeLocator:
    """Minimal stand-in for Playwright Locator.

    ``visible`` controls whether ``wait_for(state='visible', ...)``
    succeeds. ``.first`` returns self so the runner's existing
    ``_locator(page, target).first`` chain composes unchanged.
    """
    def __init__(self, selector: str, visible: bool):
        self.selector = selector
        self.visible = visible
        self.wait_calls = 0

    @property
    def first(self):
        return self

    def wait_for(self, state: str = "visible", timeout: int = 0):
        self.wait_calls += 1
        if not self.visible:
            raise TimeoutError(f"locator '{self.selector}' never visible "
                               f"after {timeout} ms")


class _FakePage:
    """Hands out a configured ``_FakeLocator`` per selector. Anything
    not in ``mapping`` resolves to an invisible locator so absent
    elements behave exactly like the Playwright runtime would.

    The runner's ``_locator()`` helper decodes ``role=...`` /
    ``text=...`` / ``placeholder=/.../i`` selectors via ``get_by_role``
    etc. For test ergonomics we route all of those back through
    ``self.locator()`` too — so a test that maps ``role=button``: True
    actually sees that locator resolve, no matter which decoder the
    runner picks. ``locator_calls`` keeps the chronological list of
    every selector the runner asked about, for promotion-order asserts.
    """
    def __init__(self, mapping: dict[str, bool]):
        self._mapping = dict(mapping)
        self.locator_calls: list[str] = []

    def locator(self, selector: str):
        self.locator_calls.append(selector)
        visible = self._mapping.get(selector, False)
        return _FakeLocator(selector, visible)

    def get_by_role(self, role, name=None):
        if name is not None:
            # ``re.compile("Save", re.I)`` round-trips to ``re.Pattern``;
            # extract the original source for the key lookup.
            sel = f'role={role}[name="{getattr(name, "pattern", name)}"]'
        else:
            sel = f"role={role}"
        return self.locator(sel)

    def get_by_text(self, text, exact: bool = False):
        return self.locator(f"text={text}")

    def get_by_placeholder(self, pat):
        src = getattr(pat, "pattern", pat)
        return self.locator(f"placeholder={src}")


def _make_runner(tmp_path, project_id: str = ""):
    return AutomationRunner(storage_root=str(tmp_path),
                             base_url="https://example.test",
                             headless=True, record_video=False,
                             default_timeout_ms=200,
                             project_id=project_id)


class TestChainFallback:
    def test_single_target_no_alternates_returns_locator_directly(self,
                                                                    tmp_path):
        """Backwards compatibility: a step with no ``target_alternates``
        bypasses the explicit ``wait_for`` and lets the downstream
        action drive timing. No registry interaction either."""
        page = _FakePage({".btn-primary": True})
        step = AutomationStep(action="click", target=".btn-primary",
                              raw="click .btn-primary")
        runner = _make_runner(tmp_path)
        loc = runner._try_locator_chain(page, step)
        assert isinstance(loc, _FakeLocator)
        assert loc.selector == ".btn-primary"
        # CRUCIAL: no wait_for call on the fast path.
        assert loc.wait_calls == 0

    def test_primary_visible_short_circuits(self, tmp_path):
        page = _FakePage({"data-testid=save": True, "#save-button": True})
        step = AutomationStep(
            action="click", target="data-testid=save",
            target_alternates=["#save-button", ".save"],
            raw="click data-testid=save",
        )
        runner = _make_runner(tmp_path)
        loc = runner._try_locator_chain(page, step)
        # The primary visible candidate wins → only it was inspected.
        assert loc.selector == "data-testid=save"
        assert page.locator_calls == ["data-testid=save"]

    def test_primary_missing_first_alternate_succeeds(self, tmp_path):
        page = _FakePage({
            "data-testid=save": False,    # primary fails
            "#save-button":     True,     # alternate wins
        })
        step = AutomationStep(
            action="click", target="data-testid=save",
            target_alternates=["#save-button", ".save"],
            raw="click data-testid=save",
        )
        runner = _make_runner(tmp_path)
        loc = runner._try_locator_chain(page, step)
        assert loc.selector == "#save-button"
        # Both attempted in order, no further candidates needed.
        assert page.locator_calls == ["data-testid=save", "#save-button"]

    def test_all_candidates_fail_raises_assertion(self, tmp_path):
        page = _FakePage({})  # nothing visible
        step = AutomationStep(
            action="click", target="data-testid=save",
            target_alternates=["#save-button", ".save"],
            raw="click data-testid=save",
        )
        runner = _make_runner(tmp_path)
        with pytest.raises(AssertionError) as excinfo:
            runner._try_locator_chain(page, step)
        msg = str(excinfo.value)
        # The error message must list what we tried so the operator
        # can diagnose the drift without checking the logs.
        assert "data-testid=save" in msg
        assert "#save-button" in msg
        assert ".save" in msg

    def test_empty_target_raises_assertion(self, tmp_path):
        step = AutomationStep(action="click", target="", raw="click ?")
        runner = _make_runner(tmp_path)
        with pytest.raises(AssertionError):
            runner._try_locator_chain(_FakePage({}), step)


class TestRegistryInteraction:
    @pytest.fixture
    def project(self, app):
        db.init_db()
        pid = db.upsert_project(name="ChainRunnerProj",
                                  base_url="https://chain.test",
                                  owner_sid="t-chain")
        yield pid
        db.delete_project(pid)

    def test_no_label_means_no_registry_writes(self, tmp_path, project):
        """When ``step.locator_label`` is empty (every text-authored
        TC), the runner must NOT touch the registry — keeps the table
        from filling up with noise from heuristic parses."""
        page = _FakePage({"data-testid=ok": True})
        step = AutomationStep(
            action="click", target="data-testid=ok",
            target_alternates=["text=ok"],
            locator_label="",  # explicit
            raw="click",
        )
        runner = _make_runner(tmp_path, project_id=project)
        runner._try_locator_chain(page, step)
        assert db.list_locators(project) == []

    def test_no_project_id_means_no_registry_writes(self, tmp_path, project):
        """When the runner was constructed without a project_id, even
        a labelled step must not touch the registry — there's no DB
        scope for the writes to live in."""
        page = _FakePage({"data-testid=ok": True})
        step = AutomationStep(
            action="click", target="data-testid=ok",
            locator_label="testid=ok",
            raw="click",
        )
        runner = _make_runner(tmp_path, project_id="")
        runner._try_locator_chain(page, step)
        assert db.list_locators(project) == []

    def test_success_recorded_when_pid_and_label_set(self, tmp_path, project):
        # Pre-register so record_success has a row to bump.
        register_candidates(project, "testid=ok", [
            LocatorCandidate("testid", "data-testid=ok"),
            LocatorCandidate("text",   "text=ok"),
        ])
        page = _FakePage({
            "data-testid=ok": False,   # primary fails
            "text=ok":        True,    # alternate wins → strategy "text"
        })
        step = AutomationStep(
            action="click", target="data-testid=ok",
            target_alternates=["text=ok"],
            locator_label="testid=ok",
            raw="click",
        )
        runner = _make_runner(tmp_path, project_id=project)
        runner._try_locator_chain(page, step)
        row = db.get_locator(project, "testid=ok")
        assert row is not None
        assert row["success_count"] == 1
        # Strategy of the winning selector is "text".
        assert row["last_success_strategy"] == "text"

    def test_all_fail_records_failure(self, tmp_path, project):
        # Register every alternate the step carries, so best_alternates
        # returns a multi-element chain that exercises the failure-recording
        # branch — single-target chains take the fast path and never
        # touch the registry's fail counter.
        register_candidates(project, "testid=gone", [
            LocatorCandidate("testid", "data-testid=gone"),
            LocatorCandidate("text",   "text=gone"),
        ])
        step = AutomationStep(
            action="click", target="data-testid=gone",
            target_alternates=["text=gone"],
            locator_label="testid=gone",
            raw="click",
        )
        runner = _make_runner(tmp_path, project_id=project)
        with pytest.raises(AssertionError):
            runner._try_locator_chain(_FakePage({}), step)
        row = db.get_locator(project, "testid=gone")
        assert row["fail_count"] == 1
        assert row["success_count"] == 0

    def test_registry_promotes_winner_to_front(self, tmp_path, project):
        """After a prior success records ``last_success_strategy``, the
        next run's chain walk must try that strategy FIRST."""
        register_candidates(project, "testid=promote", [
            LocatorCandidate("testid", "data-testid=promote"),
            LocatorCandidate("text",   "text=Promote"),
        ])
        # Pretend last run text= was the winner.
        db.record_locator_success(project, "testid=promote", "text")
        # Make BOTH selectors visible. The runner should still try
        # text= first because the registry promoted it.
        page = _FakePage({"data-testid=promote": True,
                          "text=Promote":        True})
        step = AutomationStep(
            action="click", target="data-testid=promote",
            target_alternates=["text=Promote"],
            locator_label="testid=promote",
            raw="click",
        )
        runner = _make_runner(tmp_path, project_id=project)
        runner._try_locator_chain(page, step)
        # First call must be the promoted text= selector, not testid=.
        assert page.locator_calls[0] == "text=Promote"
