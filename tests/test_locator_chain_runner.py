"""PR-A — runner-side multi-locator chain + selector-decoder tests.

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
    def __init__(self, selector: str, visible: bool, matches: int = 1):
        self.selector = selector
        self.visible = visible
        #: How many elements the selector resolves to. One unless a test
        #: says otherwise, which keeps every case written before the
        #: ambiguity check byte-identical.
        self.matches = matches
        self.wait_calls = 0

    @property
    def first(self):
        return self

    def count(self):
        return self.matches

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
    def __init__(self, mapping: dict[str, bool],
                 matches: dict[str, int] | None = None):
        self._mapping = dict(mapping)
        #: Selectors that resolve to more than one element. Separate from
        #: ``mapping`` so no existing test has to say anything about it.
        self._matches = dict(matches or {})
        self.locator_calls: list[str] = []

    def locator(self, selector: str):
        self.locator_calls.append(selector)
        visible = self._mapping.get(selector, False)
        return _FakeLocator(selector, visible,
                            matches=self._matches.get(selector, 1))

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

    # PR-A follow-up: ``_locator()`` now routes the recorder's literal
    # forms (``data-testid=…``, ``label=…``, ``alt=…``, ``title=…``) to
    # the matching ``get_by_*`` accessor instead of falling through to
    # ``page.locator``. The fake mirrors that back into ``self.locator``
    # so test mappings can stay keyed by the canonical selector string.
    def get_by_test_id(self, tid):
        return self.locator(f"data-testid={tid}")

    def get_by_label(self, text):
        return self.locator(f"label={text}")

    def get_by_alt_text(self, text):
        return self.locator(f"alt={text}")

    def get_by_title(self, text):
        return self.locator(f"title={text}")


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

    def test_non_timeout_exception_surfaces_immediately(self, tmp_path):
        """A hard failure during wait_for (page crash / websocket
        disconnect / browser closed) must NOT be silently treated as
        locator drift — it has to surface so _run_step can mark the
        step blocked instead of walking the whole chain looking for an
        element that the browser can no longer answer about."""
        class _BoomPage:
            def __init__(self):
                self.locator_calls: list[str] = []

            def locator(self, sel):
                self.locator_calls.append(sel)
                class _Boom:
                    @property
                    def first(self): return self
                    def wait_for(self, **_kw):
                        raise RuntimeError("page crashed: websocket closed")
                return _Boom()

        page = _BoomPage()
        step = AutomationStep(
            action="click", target=".one",
            target_alternates=[".two", ".three"], raw="click",
        )
        runner = _make_runner(tmp_path)
        with pytest.raises(RuntimeError, match="websocket closed"):
            runner._try_locator_chain(page, step)
        # First candidate raised; chain MUST stop right there.
        assert page.locator_calls == [".one"]

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


class _DecoderPage:
    """Records which Playwright accessor + arguments the runner picked
    so the parametrised decoder test below can assert the routing.

    Each method captures its arguments and returns an always-visible
    fake locator — we only care about how ``_locator(page, target)``
    dispatches, not about wait_for semantics.
    """
    def __init__(self):
        self.calls: list[tuple[str, tuple, dict]] = []

    def _record(self, _method, *a, **kw):
        # ``_method`` is intentionally underscore-prefixed so it can't
        # collide with a Playwright kwarg the runner forwards (notably
        # ``get_by_role(role, name=...)``, where the literal-named
        # method ``_record("get_by_role", *a, name=...)`` would crash
        # if the first positional slot were called ``name``).
        self.calls.append((_method, a, kw))
        return _FakeLocator(_method, True)

    def locator(self, *a, **kw):            return self._record("locator", *a, **kw)
    def get_by_role(self, *a, **kw):        return self._record("get_by_role", *a, **kw)
    def get_by_label(self, *a, **kw):       return self._record("get_by_label", *a, **kw)
    def get_by_placeholder(self, *a, **kw): return self._record("get_by_placeholder", *a, **kw)
    def get_by_text(self, *a, **kw):        return self._record("get_by_text", *a, **kw)
    def get_by_test_id(self, *a, **kw):     return self._record("get_by_test_id", *a, **kw)
    def get_by_alt_text(self, *a, **kw):    return self._record("get_by_alt_text", *a, **kw)
    def get_by_title(self, *a, **kw):       return self._record("get_by_title", *a, **kw)


class TestLocatorDecoder:
    """Pin the contract that every selector format the recorder emits
    routes to the matching Playwright accessor. Before PR-A's
    follow-up fix, ``label=`` / ``placeholder=<lit>`` / ``alt=`` /
    ``title=`` / ``data-testid=`` / ``role=X[name="Y"]`` all silently
    fell through to ``page.locator(target)`` which Playwright treats
    as CSS, so every recorder-emitted alternate in those formats was
    dead at runtime."""

    @pytest.mark.parametrize("target, expected_method, expected_arg", [
        ("data-testid=submit",        "get_by_test_id",     "submit"),
        ("label=Email",               "get_by_label",       "Email"),
        ("placeholder=Search",        "get_by_placeholder", "Search"),
        ("alt=Logo",                  "get_by_alt_text",    "Logo"),
        ("title=Help",                "get_by_title",       "Help"),
        ("text=Welcome",              "get_by_text",        "Welcome"),
    ])
    def test_literal_prefix_routes_to_get_by_x(self, target, expected_method,
                                                expected_arg):
        from engine.automation_runner import _locator
        page = _DecoderPage()
        _locator(page, target)
        assert len(page.calls) == 1
        method, args, _kw = page.calls[0]
        assert method == expected_method
        assert args[0] == expected_arg

    def test_role_with_literal_name_routes_to_get_by_role(self):
        from engine.automation_runner import _locator
        page = _DecoderPage()
        _locator(page, 'role=button[name="Sign in"]')
        method, args, kwargs = page.calls[0]
        assert method == "get_by_role"
        assert args[0] == "button"
        assert kwargs.get("name") == "Sign in"

    def test_role_with_regex_name_still_works(self):
        """Heuristic-parser legacy form must not regress."""
        from engine.automation_runner import _locator
        page = _DecoderPage()
        _locator(page, "role=button[name=/save/i]")
        method, args, kwargs = page.calls[0]
        assert method == "get_by_role"
        assert args[0] == "button"
        # name was passed as compiled regex.
        import re
        assert isinstance(kwargs.get("name"), re.Pattern)
        assert kwargs["name"].pattern == "save"

    def test_role_only_routes_without_name_kwarg(self):
        from engine.automation_runner import _locator
        page = _DecoderPage()
        _locator(page, "role=link")
        method, args, kwargs = page.calls[0]
        assert method == "get_by_role"
        assert args[0] == "link"
        assert "name" not in kwargs or kwargs["name"] is None

    def test_unknown_prefix_falls_through_to_locator(self):
        """CSS / XPath / anything we don't recognise must still pass
        straight through to ``page.locator`` so existing behaviour is
        preserved for hand-written selectors."""
        from engine.automation_runner import _locator
        page = _DecoderPage()
        _locator(page, ".btn-primary")
        method, args, _kw = page.calls[0]
        assert method == "locator"
        assert args[0] == ".btn-primary"


class TestAnAmbiguousBareRoleIsNotUsed:
    """The replay half of the staging defect of 2026-08-29.

    The recorder no longer emits a bare ``role=textbox`` when several
    elements carry that role, but every pack recorded before that fix
    still does, and the chain walk is where they are cashed in.

    ``.first`` is what makes it dangerous rather than merely weak: it
    turns "this matches five elements" into "take the top one", with
    nothing in the run report to say a choice was made. On the Selenium
    practice form that would have put a fill meant for the textarea into
    the text input two fields above it — and the step would have passed,
    which is the worst available outcome.
    """

    def test_a_bare_role_matching_several_elements_is_skipped(self,
                                                               tmp_path):
        page = _FakePage({"role=textbox": True, "textarea.form-control": True},
                         matches={"role=textbox": 5})
        step = AutomationStep(action="click", target="role=textbox",
                              target_alternates=["textarea.form-control"],
                              raw="click role=textbox")
        loc = _make_runner(tmp_path)._try_locator_chain(page, step)
        assert loc.selector == "textarea.form-control", loc.selector

    def test_a_bare_role_matching_one_element_is_still_used(self, tmp_path):
        """The check is about ambiguity, not about bare roles.

        A role that identifies exactly one element is a good locator —
        it survives a renamed label. Refusing those too would pass the
        test above while quietly deleting the fallback.
        """
        page = _FakePage({"role=button": True, ".btn": True})
        step = AutomationStep(action="click", target="role=button",
                              target_alternates=[".btn"],
                              raw="click role=button")
        loc = _make_runner(tmp_path)._try_locator_chain(page, step)
        assert loc.selector == "role=button"

    def test_a_named_role_matching_several_is_left_alone(self, tmp_path):
        """Scope, pinned.

        Widening the check to every selector would change behaviour for
        every pack in the database: a CSS path that happens to match
        twice has always resolved to the first match, and quietly
        starting to skip those would break working recordings to fix a
        problem they do not have.
        """
        page = _FakePage({'role=button[name="Save"]': True, ".btn": True},
                         matches={'role=button[name="Save"]': 3})
        step = AutomationStep(action="click", target='role=button[name="Save"]',
                              target_alternates=[".btn"],
                              raw="click Save")
        loc = _make_runner(tmp_path)._try_locator_chain(page, step)
        assert loc.selector == 'role=button[name="Save"]'

    def test_a_css_path_matching_several_is_left_alone(self, tmp_path):
        page = _FakePage({"div.row > a": True, ".btn": True},
                         matches={"div.row > a": 4})
        step = AutomationStep(action="click", target="div.row > a",
                              target_alternates=[".btn"],
                              raw="click div.row > a")
        loc = _make_runner(tmp_path)._try_locator_chain(page, step)
        assert loc.selector == "div.row > a"

    def test_a_skipped_candidate_is_named_in_the_failure(self, tmp_path):
        """When nothing else resolves, the error must say what was tried.

        A step that fails with "All locators failed" and an empty list
        sends the reader looking for a missing element, when the truth is
        that a candidate was refused on purpose.
        """
        page = _FakePage({"role=textbox": True}, matches={"role=textbox": 2})
        step = AutomationStep(action="click", target="role=textbox",
                              target_alternates=["#nope"],
                              raw="click role=textbox")
        with pytest.raises(AssertionError) as excinfo:
            _make_runner(tmp_path)._try_locator_chain(page, step)
        assert "role=textbox" in str(excinfo.value)

    def test_a_locator_that_cannot_be_counted_is_not_refused(self, tmp_path):
        """Best-effort, in the safe direction.

        A page object that raises on ``count()`` must not cost the step
        its primary locator — an ambiguity check that breaks a working
        chain is worse than the ambiguity.
        """
        class _NoCount(_FakeLocator):
            def count(self):
                raise RuntimeError("counting not supported here")

        class _Page(_FakePage):
            def locator(self, selector: str):
                self.locator_calls.append(selector)
                return _NoCount(selector, self._mapping.get(selector, False))

        page = _Page({"role=textbox": True, "#fallback": True})
        step = AutomationStep(action="click", target="role=textbox",
                              target_alternates=["#fallback"],
                              raw="click role=textbox")
        loc = _make_runner(tmp_path)._try_locator_chain(page, step)
        assert loc.selector == "role=textbox"


class TestTheRunnerCanPerformWhatTheRecorderRecords:
    """Replay through ``_run_step``, in a real browser.

    The checkbox defect was not a wrong label — it was a step Playwright
    refuses to execute, so a pack containing a checkbox failed on that
    step every time. Asserting the verb the recorder writes proves half
    of it; this asserts the other half.

    **Through ``_run_step``, not by calling ``loc.check()``.** The first
    version of this class resolved the locator and invoked the method
    itself, which passes with the runner's whole dispatch deleted — it
    was a test of Playwright wearing the runner's name. Deleting the
    ``uncheck`` branch left all 31 tests green; that is what a harness
    rebuilding the chain instead of calling the entry point buys.
    """

    @staticmethod
    def _page(browser_ctx, html):
        page = browser_ctx.new_page()
        page.set_content(html)
        return page

    @staticmethod
    def _chromium(p):
        try:
            return p.chromium.launch()
        except Exception as exc:
            pytest.skip(f"chromium unavailable: {exc}")

    def test_playwright_refuses_to_fill_a_toggle(self, tmp_path):
        """The premise, taken from Playwright rather than from memory.

        If ``fill`` ever starts working on a checkbox, the recorder fix
        is no longer load-bearing and this file should say so.
        """
        pw = pytest.importorskip("playwright.sync_api")
        with pw.sync_playwright() as p:
            b = self._chromium(p)
            try:
                page = self._page(b, '<input id="c" type="checkbox">')
                with pytest.raises(Exception) as excinfo:
                    page.locator("#c").fill("on")
                assert "cannot be filled" in str(excinfo.value)
            finally:
                b.close()

    def test_a_fill_step_on_a_checkbox_is_what_it_used_to_record(self,
                                                                  tmp_path):
        """The defect itself, run through the runner.

        This is what every recorded pack containing a checkbox did until
        today: a failed step, not a wrong one.
        """
        pw = pytest.importorskip("playwright.sync_api")
        with pw.sync_playwright() as p:
            b = self._chromium(p)
            try:
                page = self._page(b, '<input id="c" type="checkbox">')
                sr = _make_runner(tmp_path)._run_step(
                    page,
                    AutomationStep(action="fill", target="#c", value="on",
                                    raw="page.locator('#c').fill('on')"),
                    1, str(tmp_path))
                assert sr.status == "failed", sr.status
                assert "cannot be filled" in sr.comment
            finally:
                b.close()

    @pytest.mark.parametrize("action,html,expected", [
        ("check", '<input id="c" type="checkbox">', True),
        ("uncheck", '<input id="c" type="checkbox" checked>', False),
    ])
    def test_a_toggle_step_replays(self, tmp_path, action, html, expected):
        pw = pytest.importorskip("playwright.sync_api")
        with pw.sync_playwright() as p:
            b = self._chromium(p)
            try:
                page = self._page(b, html)
                sr = _make_runner(tmp_path)._run_step(
                    page,
                    AutomationStep(action=action, target="#c",
                                    raw=f"page.locator('#c').{action}()"),
                    1, str(tmp_path))
                assert sr.status == "passed", sr.comment
                assert page.is_checked("#c") is expected
            finally:
                b.close()

    def test_a_press_step_replays(self, tmp_path):
        """``press`` had no branch at all.

        An unmatched action falls through the whole chain to the
        screenshot, so the step did nothing and was reported as passed —
        the Enter that never submitted the form, with a green tick.
        Codegen emits it whenever a form is submitted that way.
        """
        pw = pytest.importorskip("playwright.sync_api")
        with pw.sync_playwright() as p:
            b = self._chromium(p)
            try:
                page = self._page(b, (
                    '<input id="t"><div id="out"></div>'
                    '<script>document.getElementById("t").addEventListener('
                    '"keydown", e => { if (e.key === "Enter") '
                    'document.getElementById("out").textContent = "submitted"; '
                    '});</script>'))
                sr = _make_runner(tmp_path)._run_step(
                    page,
                    AutomationStep(action="press", target="#t", value="Enter",
                                    raw="page.locator('#t').press('Enter')"),
                    1, str(tmp_path))
                assert sr.status == "passed", sr.comment
                assert page.text_content("#out") == "submitted", (
                    "the step passed without doing anything")
            finally:
                b.close()
