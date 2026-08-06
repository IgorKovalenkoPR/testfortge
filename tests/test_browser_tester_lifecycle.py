"""A browser that fails to start must not take the process with it.

``BrowserTestRunner.__enter__`` starts Playwright's driver and then
launches Chromium. ``__exit__`` never runs when ``__enter__`` raises, so
for as long as those were two bare statements a failed launch left the
driver started — and that is worse than a leaked subprocess. Playwright's
sync API runs its event loop *in the calling thread*, so the thread is
left inside a running loop and every later ``sync_playwright()`` anywhere
in the process raises

    It looks like you are using Playwright Sync API inside the asyncio loop.

instead of whatever the real problem was.

Two things kept it invisible:

* ``qa_persona`` calls this inside ``except Exception: pass``, so the
  launch failure is never reported — generation continues with no browser
  findings, which is the programme's signature shape: the wrong behaviour
  looks exactly like the right one;
* on any machine where Chromium *is* installed the launch succeeds and
  the leak never happens, which is every development box here.

So it surfaced in CI, where the test matrix deliberately installs no
browsers: ``tests/test_crawl_error_surfaced.py`` poisoned the process and
``tests/test_e2e_golden_paths.py`` errored fourteen files later with an
asyncio message that had nothing to do with it. On a dyno the same
sequence is an OOM-killed Chromium followed by every later browser run
failing for an unrelated-looking reason — which is exactly the failure
``OomGuard`` exists to make legible.

No browser binary needed: the failure is the subject.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed")
import playwright.sync_api as _pw_api  # noqa: E402

from engine import browser_tester as _bt  # noqa: E402


class _Boom(RuntimeError):
    """What an OOM-killed or missing Chromium looks like from here."""


@pytest.fixture
def runner():
    """A runner with the crawl already done, so only the browser half runs."""
    return _bt.BrowserTestRunner(
        "https://example.test", max_pages=1,
        site_analysis=SimpleNamespace(pages=[]))


@pytest.fixture
def launch_always_fails(monkeypatch):
    """Chromium refuses to start, whatever this machine has installed."""
    def _refuse(self, **kwargs):
        raise _Boom("Chromium would not start")

    monkeypatch.setattr(_pw_api.BrowserType, "launch", _refuse)


class TestAFailedLaunchUnwinds:
    def test_the_caller_sees_the_launch_failure_itself(self, runner,
                                                       launch_always_fails):
        """Not swallowed and not replaced — the real cause propagates.

        If the cleanup ever swallowed the original exception the caller
        would be told the cleanup's problem instead of Chromium's, and the
        operator would be back to guessing.
        """
        with pytest.raises(_Boom):
            runner.__enter__()

    def test_the_process_can_still_use_playwright_afterwards(
            self, runner, launch_always_fails):
        """The property this file exists for.

        After a failed ``__enter__`` a fresh ``sync_playwright()`` must
        still be enterable. Before the fix this raised "Sync API inside the
        asyncio loop" — in a different test, in a different file, with
        nothing pointing back here.
        """
        with pytest.raises(_Boom):
            runner.__enter__()

        with _pw_api.sync_playwright() as pw:
            assert pw is not None

    def test_the_handles_are_cleared(self, runner, launch_always_fails):
        """A stopped driver still referenced is a second ``stop()`` waiting
        to fail, and ``__exit__`` does run on the way out of a ``with``
        block whose body never started."""
        with pytest.raises(_Boom):
            runner.__enter__()

        assert runner._pw is None
        assert runner._browser is None
        runner.__exit__(None, None, None)   # must not raise


class TestExitSurvivesAHalfBrokenBrowser:
    """The other order: the browser closes badly on the way out.

    ``close()`` raising used to skip ``stop()`` entirely — the same leak
    by a different route, and the one that happens when a page crashed
    during the run rather than before it.
    """

    def test_a_close_that_raises_still_stops_the_driver(self, runner):
        stopped: list[bool] = []

        class _Browser:
            def close(self):
                raise _Boom("the browser was already gone")

        class _Driver:
            def stop(self):
                stopped.append(True)

        runner._browser = _Browser()
        runner._pw = _Driver()

        runner.__exit__(None, None, None)

        assert stopped == [True], "the driver was left running"
        assert runner._pw is None and runner._browser is None
