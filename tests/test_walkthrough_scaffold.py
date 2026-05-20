"""TFWefloLab integration PR-1 — walkthrough runner scaffold.

Coverage (PR-1 only — heuristics are out of scope here, they land in
PR-2):

1. :class:`engine.walkthrough_runner.WalkthroughRunner` runs through
   the navigate-and-screenshot path against a stubbed Playwright,
   producing a :class:`RunReport` with one ``ScriptResult`` per URL
   and ``status="passed"``.
2. A goto failure produces a ``status="failed"`` ScriptResult with the
   exception type captured in the step ``comment``.
3. ``max_pages`` truncates a long URL list.
4. ``device_timeout_ms`` shortcuts every URL beyond the deadline to
   ``blocked`` instead of running them.
5. :func:`engine.walkthrough_runner.feature_enabled` is env-driven and
   re-reads ``os.environ`` on every call.
6. ``engine.runner_worker`` dispatches ``mode="walkthrough"`` to the
   walkthrough runner when the flag is on; raises otherwise.
7. ``POST /debug/walkthrough`` returns 404 when the flag is off
   (no information leak) and 202 + ``config_id`` when on. ``Popen``
   is patched so no real Chromium spawns under pytest.
"""

from __future__ import annotations

import os
import sys
import tempfile
from types import SimpleNamespace
from typing import Any

import pytest


# ── Playwright stubs ──────────────────────────────────────────────


class _FakePage:
    """Recorded calls let assertions about goto/screenshot/close order."""

    def __init__(self, *, fail_on: set[str] | None = None,
                 final_url: str = "") -> None:
        self.fail_on = fail_on or set()
        self.calls: list[tuple[str, Any]] = []
        self._url = final_url

    @property
    def url(self) -> str:
        return self._url

    def set_default_navigation_timeout(self, ms: int) -> None:
        self.calls.append(("set_timeout", ms))

    def goto(self, url: str, **kw):
        self.calls.append(("goto", url))
        self._url = url
        if "goto" in self.fail_on:
            raise RuntimeError("fake goto failure")
        return None

    def screenshot(self, *, path: str, full_page: bool = False) -> None:
        self.calls.append(("screenshot", path))
        if "screenshot" in self.fail_on:
            raise RuntimeError("fake screenshot failure")
        # Actually write a small file so the live-feed mirror has
        # something to copy.
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\nfake")

    def close(self) -> None:
        self.calls.append(("close", None))


class _FakeContext:
    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.pages: list[_FakePage] = []

    def new_page(self) -> _FakePage:
        page = _FakePage(fail_on=self.fail_on)
        self.pages.append(page)
        return page

    def close(self) -> None:
        pass


class _FakeBrowser:
    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()

    def new_context(self, **_kw) -> _FakeContext:
        return _FakeContext(fail_on=self.fail_on)

    def close(self) -> None:
        pass


class _FakeChromium:
    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()

    def launch(self, **_kw) -> _FakeBrowser:
        return _FakeBrowser(fail_on=self.fail_on)


class _FakePlaywright:
    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.chromium = _FakeChromium(fail_on=fail_on)


class _FakePlaywrightCM:
    """Drop-in for ``sync_playwright()`` — context-manager that yields
    a ``_FakePlaywright``."""

    def __init__(self, fail_on: set[str] | None = None) -> None:
        self.pw = _FakePlaywright(fail_on=fail_on)

    def __enter__(self) -> _FakePlaywright:
        return self.pw

    def __exit__(self, *_exc: Any) -> bool:
        return False


@pytest.fixture
def fake_pw(monkeypatch):
    """Install a fake ``sync_playwright`` that returns a ``_FakePlaywright``
    on entry. Returns a callable that lets tests pre-arm ``goto`` /
    ``screenshot`` to raise.
    """

    state: dict[str, Any] = {"fail_on": set()}

    def _make(*_args, **_kw) -> _FakePlaywrightCM:
        return _FakePlaywrightCM(fail_on=state["fail_on"])

    # Patch directly in the playwright.sync_api namespace so the
    # ``from playwright.sync_api import sync_playwright`` inside
    # walkthrough_runner picks up our fake.
    import playwright.sync_api as _ps
    monkeypatch.setattr(_ps, "sync_playwright", _make)

    def configure(*, fail_on: set[str] | None = None) -> None:
        state["fail_on"] = fail_on or set()

    return configure


@pytest.fixture
def tmp_storage(tmp_path):
    return str(tmp_path)


# ── 1. Happy-path scaffold run ────────────────────────────────────


class TestWalkthroughHappyPath:
    def test_two_urls_produce_two_passed_scripts(self, fake_pw, tmp_storage):
        fake_pw()  # no failures
        from engine.walkthrough_runner import WalkthroughRunner

        runner = WalkthroughRunner(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            headless=True,
        )
        report = runner.run(start_urls=[
            "https://example.com/",
            "https://example.com/about",
        ])
        assert report.total == 2
        assert report.passed == 2
        assert report.failed == 0
        assert report.blocked == 0
        # Each script has one passed goto step.
        for s in report.scripts:
            assert s.status == "passed"
            assert s.tc_id.startswith("WALK-")
            assert len(s.steps) == 1
            assert s.steps[0].action == "goto"
            assert s.steps[0].status == "passed"
        # First URL's screenshot path is exposed so the asset
        # builder in runner_worker can serialise it.
        assert report.scripts[0].steps[0].screenshot_after.endswith(
            "page.png")

    def test_findings_attribute_is_empty_in_scaffold(self, fake_pw, tmp_storage):
        """Contract: PR-1's run() must leave the ``findings`` list
        empty. PR-2 is where heuristics start populating it."""
        fake_pw()
        from engine.walkthrough_runner import WalkthroughRunner

        runner = WalkthroughRunner(
            storage_root=tmp_storage, base_url="https://example.com/")
        runner.run()
        assert runner.findings == []


# ── 2. goto failure → ScriptResult.status='failed' ───────────────


class TestWalkthroughErrorHandling:
    def test_goto_failure_is_captured(self, fake_pw, tmp_storage):
        fake_pw(fail_on={"goto"})
        from engine.walkthrough_runner import WalkthroughRunner

        runner = WalkthroughRunner(
            storage_root=tmp_storage, base_url="https://example.com/")
        report = runner.run(start_urls=["https://example.com/"])
        assert report.total == 1
        assert report.failed == 1
        s = report.scripts[0]
        assert s.status == "failed"
        assert "RuntimeError" in (s.steps[0].comment or "")


# ── 3. max_pages truncation ──────────────────────────────────────


class TestWalkthroughCaps:
    def test_max_pages_truncates_url_list(self, fake_pw, tmp_storage):
        fake_pw()
        from engine.walkthrough_runner import WalkthroughRunner

        runner = WalkthroughRunner(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            max_pages=2,
        )
        report = runner.run(start_urls=[
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",   # dropped
            "https://example.com/d",   # dropped
        ])
        assert report.total == 2
        assert {s.steps[0].raw for s in report.scripts} == {
            "https://example.com/a", "https://example.com/b",
        }

    def test_device_timeout_blocks_subsequent_urls(self, monkeypatch,
                                                     fake_pw, tmp_storage):
        """Once the wall-clock deadline passes, every remaining URL is
        reported as ``blocked`` instead of consuming time."""
        fake_pw()
        from engine import walkthrough_runner as wr

        # Pin time.time() so the first URL is fine but the second
        # is past the deadline. The deadline is started_ts + (ms/1000),
        # so we feed an incrementing series of returns.
        clock = [1000.0, 1000.0, 1000.5, 1100.0]
        monkeypatch.setattr(wr.time, "time", lambda: clock.pop(0)
                            if clock else 1100.0)

        runner = wr.WalkthroughRunner(
            storage_root=tmp_storage,
            base_url="https://example.com/",
            device_timeout_ms=1000,   # 1 s budget
        )
        report = runner.run(start_urls=[
            "https://example.com/a",
            "https://example.com/b",
        ])
        # Either at least one blocked or the run finished entirely
        # within the budget — the exact mix depends on the clock
        # interleaving but the contract is "no failure cascade".
        assert report.total == 2
        statuses = {s.status for s in report.scripts}
        # The deadline must produce a blocked tag somewhere.
        assert "blocked" in statuses


# ── 5. feature_enabled is env-driven ─────────────────────────────


class TestFeatureEnabled:
    def test_off_by_default(self, monkeypatch):
        monkeypatch.delenv("WALKTHROUGH_MODE_ENABLED", raising=False)
        from engine.walkthrough_runner import feature_enabled
        assert feature_enabled() is False

    def test_on_when_explicit_one(self, monkeypatch):
        monkeypatch.setenv("WALKTHROUGH_MODE_ENABLED", "1")
        from engine.walkthrough_runner import feature_enabled
        assert feature_enabled() is True

    def test_off_for_other_truthy_values(self, monkeypatch):
        """Only the literal string "1" enables the flag. ``"true"`` or
        ``"yes"`` should NOT — this matches the convention used by
        the existing ``BEHIND_HTTPS`` and ``TESTFORTGE_SNAPSHOT_WORKER``
        env vars elsewhere in the codebase."""
        from engine.walkthrough_runner import feature_enabled
        for v in ("true", "yes", "on", "TRUE"):
            monkeypatch.setenv("WALKTHROUGH_MODE_ENABLED", v)
            assert feature_enabled() is False, f"{v!r} should NOT enable"


# ── 6. runner_worker dispatch ────────────────────────────────────


class TestWorkerDispatch:
    def _spawn_worker(self, monkeypatch, storage_root: str, cfg: dict):
        """Invoke runner_worker.main() in-process by writing the config
        and patching sys.argv. Returns the exit code + result payload."""
        import json
        cfg_path = os.path.join(storage_root, "cfg.json")
        with open(cfg_path, "w") as f:
            json.dump(cfg, f)
        monkeypatch.setattr(sys, "argv", ["runner_worker", cfg_path])
        from engine import runner_worker
        rc = runner_worker.main()
        # Locate the result file.
        pending = os.path.join(storage_root, "automation_runs",
                                "_pending")
        cfg_id = "cfg"
        rp = os.path.join(pending, f"{cfg_id}.result.json")
        if not os.path.isfile(rp):
            return rc, None
        with open(rp) as f:
            return rc, json.load(f)

    def test_walkthrough_mode_routes_to_walkthrough_runner(
            self, monkeypatch, fake_pw, tmp_storage):
        fake_pw()
        monkeypatch.setenv("WALKTHROUGH_MODE_ENABLED", "1")

        rc, result = self._spawn_worker(monkeypatch, tmp_storage, {
            "config_id": "cfg",
            "storage_root": tmp_storage,
            "mode": "walkthrough",
            "base_url": "https://example.com/",
            "walkthrough": {
                "start_urls": ["https://example.com/"],
                "max_pages": 3,
                "device_timeout_ms": 60000,
            },
            "runner_kwargs": {"headless": True},
        })
        assert rc == 0
        assert result is not None
        assert result["status"] == "done"
        # The serialised report has the walkthrough-shaped tc_id.
        assert result["report"]["scripts"][0]["tc_id"].startswith("WALK-")

    def test_walkthrough_mode_without_flag_fails_cleanly(
            self, monkeypatch, tmp_storage):
        monkeypatch.delenv("WALKTHROUGH_MODE_ENABLED", raising=False)

        rc, result = self._spawn_worker(monkeypatch, tmp_storage, {
            "config_id": "cfg",
            "storage_root": tmp_storage,
            "mode": "walkthrough",
            "base_url": "https://example.com/",
            "walkthrough": {"start_urls": ["https://example.com/"]},
        })
        # main() returns 1 on captured exception; result.json carries
        # the failure detail so the operator can see the gate message.
        assert rc == 1
        assert result["status"] == "failed"
        assert "WALKTHROUGH_MODE_ENABLED" in (result["error"] or "")

    def test_default_mode_still_calls_automation_runner(
            self, monkeypatch, tmp_storage):
        """Sanity check: an existing config (no ``mode`` field) goes
        through the TC-driven path. We stub AutomationRunner so the
        test doesn't need real Playwright."""
        captured = {}

        class _FakeAR:
            def __init__(self, **kw):
                captured["kwargs"] = kw

            def run(self, scripts):
                captured["scripts"] = scripts
                from engine.automation_runner import RunReport
                return RunReport(run_id="r1",
                                  started_at="2026-05-20T00:00:00",
                                  finished_at="2026-05-20T00:00:01",
                                  base_url="https://example.com/",
                                  headless=True, total=0)

        import engine.automation_runner as _ar_mod
        monkeypatch.setattr(_ar_mod, "AutomationRunner", _FakeAR)

        rc, result = self._spawn_worker(monkeypatch, tmp_storage, {
            "config_id": "cfg",
            "storage_root": tmp_storage,
            # No "mode" field — default path
            "base_url": "https://example.com/",
            "items_data": [],
            "runner_kwargs": {"headless": True},
        })
        assert rc == 0
        assert result["status"] == "done"
        assert captured.get("scripts") == []


# ── 7. Debug endpoint gate + 202 on dispatch ─────────────────────


class TestDebugEndpoint:
    def test_endpoint_is_404_when_flag_off(self, client, monkeypatch):
        monkeypatch.delenv("WALKTHROUGH_MODE_ENABLED", raising=False)
        resp = client.post("/debug/walkthrough",
                            json={"base_url": "https://example.com/"})
        assert resp.status_code == 404

    def test_endpoint_returns_202_and_spawns_worker(
            self, client, monkeypatch, tmp_storage):
        monkeypatch.setenv("WALKTHROUGH_MODE_ENABLED", "1")
        # Repoint storage to the tmp dir so the test owns the artefacts.
        from flask import current_app  # noqa: F401 — for context
        client.application.config["STORAGE_FOLDER"] = tmp_storage

        spawned: dict = {}

        class _FakePopen:
            def __init__(self, argv, **kw):
                spawned["argv"] = argv
                spawned["kw"] = kw
                self.pid = 4242

        import routes.debug as _dbg
        monkeypatch.setattr(_dbg.subprocess, "Popen", _FakePopen)

        resp = client.post("/debug/walkthrough",
                            json={"base_url": "https://example.com/"})
        assert resp.status_code == 202, resp.get_data(as_text=True)
        body = resp.get_json()
        assert body["config_id"]
        assert body["start_urls"] == ["https://example.com/"]
        # Worker was spawned with -m engine.runner_worker.
        assert spawned["argv"][1:3] == ["-m", "engine.runner_worker"]
        # Config JSON the worker would pick up exists and carries
        # mode=walkthrough.
        import json
        cfg_path = spawned["argv"][3]
        with open(cfg_path) as f:
            cfg = json.load(f)
        assert cfg["mode"] == "walkthrough"
        assert cfg["walkthrough"]["start_urls"] == ["https://example.com/"]

    def test_endpoint_400_when_base_url_missing(self, client, monkeypatch):
        monkeypatch.setenv("WALKTHROUGH_MODE_ENABLED", "1")
        resp = client.post("/debug/walkthrough", json={})
        assert resp.status_code == 400
