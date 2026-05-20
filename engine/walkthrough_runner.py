"""TestForTge — Autonomous QA walkthrough runner (TFWefloLab port, PR-1 scaffold).

This module is the **plumbing** half of the
``docs/plans/tfweflo_walkthrough_integration.md`` integration. It
mirrors :mod:`engine.automation_runner`'s public surface
(``run() -> RunReport``) so :mod:`engine.runner_worker` can dispatch
to it via a ``mode="walkthrough"`` config field without touching the
existing TC-driven code path.

PR-1 explicitly carries **no exploration heuristics yet** — it
navigates each start URL, captures one screenshot per page, and
returns a :class:`RunReport` with a ``ScriptResult`` per URL. The
broken-image / dropdown / form / CTA / axe / search heuristics land
in PR-2, and the cross-env dedup + UI in PR-3.

Why a scaffold-only first PR: the TC-driven runner is the product's
audit-trail backbone. Landing the plumbing (mode dispatch, run dirs,
serialisation, retention purge, live-frame feed) on its own — behind
a default-off feature flag — gives us a tiny, reviewable diff that
de-risks every later change. Once this is on ``main``, PR-2 adds
heuristics with no schema or dispatch churn.

Reuses dataclasses from :mod:`engine.automation_runner` so the
existing :func:`engine.runner_worker._serialise_report` writer
handles walkthrough results unchanged.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from datetime import datetime
from typing import Any

from .automation_runner import (
    AUTOMATION_RUN_MAX_KEPT,
    AUTOMATION_RUN_RETENTION_DAYS,
    RunReport,
    ScriptResult,
    StepResult,
    _purge_old_automation_runs,
)
from .log import get_logger

_logger = get_logger(__name__)


# Feature flag — controls whether ``runner_worker`` will even consider
# dispatching to a walkthrough run. Default OFF so existing deployments
# never see a behaviour change from this PR landing.
WALKTHROUGH_FLAG_ENV = "WALKTHROUGH_MODE_ENABLED"


def feature_enabled() -> bool:
    """Whether the walkthrough dispatch path is currently active.

    A simple env-var check kept in this module so the route layer can
    import a single boolean without re-reading ``os.environ`` inline.
    Always reads fresh so test fixtures can flip it via
    ``monkeypatch.setenv`` without restart.
    """
    return (os.environ.get(WALKTHROUGH_FLAG_ENV) or "").strip() == "1"


class WalkthroughRunner:
    """Drop-in replacement for :class:`AutomationRunner` when the
    config carries ``mode="walkthrough"``.

    Constructor accepts a subset of ``AutomationRunner.__init__`` args
    plus walkthrough-specific knobs (``max_pages``, ``device_timeout_ms``).
    Unknown kwargs are silently ignored so the dispatch layer can pass
    one ``runner_kwargs`` dict for both runners without per-runner
    keyword-list maintenance.
    """

    def __init__(
        self,
        storage_root: str,
        base_url: str,
        *,
        headless: bool = True,
        viewport: tuple[int, int] = (1280, 800),
        navigation_timeout_ms: int = 45000,
        device_timeout_ms: int = 480000,
        max_pages: int = 6,
        record_video: bool = False,
        **_ignored: Any,
    ):
        self.storage_root = storage_root
        self.base_url = (base_url or "").strip()
        self.headless = bool(headless)
        # Cap headless viewport identically to AutomationRunner — the
        # same Render-free-tier reasoning applies (0.1 CPU cannot
        # screenshot 1920x1080 inside our timeout budget). See the
        # comment block in automation_runner.AutomationRunner.__init__
        # for the operator-reported diag that motivated this cap.
        chosen = tuple(viewport)
        if headless and chosen[0] > 1280 and chosen[1] > 800:
            self.viewport = (1280, 800)
        else:
            self.viewport = chosen
        self.navigation_timeout_ms = int(navigation_timeout_ms)
        self.device_timeout_ms = int(device_timeout_ms)
        self.max_pages = max(1, int(max_pages))
        self.record_video = bool(record_video)
        # Findings ring — populated by PR-2 heuristics. Kept here in
        # PR-1 so the public attribute exists and tests can assert
        # against an empty list as a contract.
        self.findings: list[dict] = []

    # ── live-feed helpers ─────────────────────────────────────────

    def _reset_live_dir(self, runs_root: str) -> str:
        """Wipe the shared ``_live`` dir at run start and return its
        path. Mirrors ``AutomationRunner._reset_live_dir`` in spirit so
        the existing ``/test-execution/live`` route picks up walk-
        through frames with no template changes.
        """
        live_dir = os.path.join(runs_root, "_live")
        try:
            os.makedirs(live_dir, exist_ok=True)
            stale = os.path.join(live_dir, "latest.png")
            if os.path.exists(stale):
                os.remove(stale)
            strip_dir = os.path.join(live_dir, "strip")
            if os.path.isdir(strip_dir):
                for fn in os.listdir(strip_dir):
                    try:
                        os.remove(os.path.join(strip_dir, fn))
                    except OSError:
                        pass
        except OSError as exc:
            _logger.debug("walkthrough live reset failed: %s", exc)
            return ""
        return live_dir

    def _write_live_info(self, live_dir: str, *, status: str,
                         total: int, done: int, current_url: str = "",
                         run_id: str = "",
                         started_ts: float = 0.0) -> None:
        """Atomic write of ``_live/info.json`` so the /live endpoint
        picks up walkthrough progress with no template changes."""
        if not live_dir:
            return
        info = {
            "status": status,
            "run_id": run_id,
            "total": total,
            "done": done,
            "current_tc": current_url,  # ``current_tc`` is what the
            "current_url": current_url,  # template reads — walkthrough
                                          # uses URL where TC-driven uses TC id.
            "elapsed_ms": int(max(0.0, time.time() - started_ts) * 1000),
            "mode": "walkthrough",
        }
        path = os.path.join(live_dir, "info.json")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(info, f)
            os.replace(tmp, path)
        except OSError as exc:
            _logger.debug("walkthrough live info write failed: %s", exc)

    # ── main entry point ──────────────────────────────────────────

    def run(self, start_urls: list[str] | None = None) -> RunReport:
        """Walk a list of URLs and produce a :class:`RunReport`.

        ``start_urls`` defaults to ``[self.base_url]`` so the simplest
        invocation matches the existing single-URL Test Execution form.
        Each URL becomes one :class:`ScriptResult` whose ``tc_id`` is
        a stable ``WALK-N`` identifier — the audit trail can quote it
        the same way it quotes ``TC-001``.

        PR-1 scaffold: navigates, captures one screenshot per URL,
        no findings. PR-2 will fill ``self.findings`` and emit one
        :class:`StepResult` per detected defect class.
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # pragma: no cover — playwright is
            # required for any real run; the scaffold returns a clean
            # "blocked" report so the worker can surface the error
            # without crashing.
            _logger.error("playwright import failed: %s", exc)
            return RunReport(
                run_id="",
                started_at=datetime.now().isoformat(timespec="seconds"),
                finished_at=datetime.now().isoformat(timespec="seconds"),
                base_url=self.base_url,
                headless=self.headless,
                total=0,
                blocked=1,
                scripts=[],
            )

        urls = [u.strip() for u in (start_urls or [self.base_url]) if u and u.strip()]
        if not urls:
            return RunReport(
                run_id="",
                started_at=datetime.now().isoformat(timespec="seconds"),
                finished_at=datetime.now().isoformat(timespec="seconds"),
                base_url=self.base_url,
                headless=self.headless,
                total=0,
                scripts=[],
            )
        urls = urls[: self.max_pages]

        run_id = (datetime.now().strftime("%Y%m%d_%H%M%S_")
                  + uuid.uuid4().hex[:6])
        runs_root = os.path.join(self.storage_root, "automation_runs")
        run_dir = os.path.join(runs_root, run_id)
        os.makedirs(run_dir, exist_ok=True)

        # Retention purge — same policy as AutomationRunner so walk-
        # through runs don't fill the free-tier disk.
        try:
            _purge_old_automation_runs(
                runs_root,
                AUTOMATION_RUN_RETENTION_DAYS,
                AUTOMATION_RUN_MAX_KEPT,
            )
        except Exception as exc:
            _logger.debug("walkthrough retention purge skipped: %s", exc)

        live_dir = self._reset_live_dir(runs_root)
        started_ts = time.time()
        self._write_live_info(
            live_dir, status="starting", total=len(urls), done=0,
            run_id=run_id, started_ts=started_ts,
        )

        scripts: list[ScriptResult] = []
        wall_deadline = started_ts + (self.device_timeout_ms / 1000.0)

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=self.headless)
            context_kwargs = {"viewport": {
                "width": self.viewport[0], "height": self.viewport[1],
            }}
            if self.record_video:
                context_kwargs["record_video_dir"] = run_dir
            context = browser.new_context(**context_kwargs)
            try:
                for idx, url in enumerate(urls, start=1):
                    if time.time() >= wall_deadline:
                        # Outer wall-clock kill — every page beyond
                        # this point is reported as blocked so the
                        # operator sees the budget exhaustion.
                        scripts.append(self._blocked_script(idx, url,
                            "device_timeout exceeded before this URL"))
                        continue
                    self._write_live_info(
                        live_dir, status="running",
                        total=len(urls), done=idx - 1,
                        current_url=url, run_id=run_id,
                        started_ts=started_ts,
                    )
                    script = self._walk_one(
                        context, idx, url, run_dir, live_dir,
                    )
                    scripts.append(script)
            finally:
                try:
                    context.close()
                except Exception:
                    pass
                try:
                    browser.close()
                except Exception:
                    pass

        passed = sum(1 for s in scripts if s.status == "passed")
        failed = sum(1 for s in scripts if s.status == "failed")
        blocked = sum(1 for s in scripts if s.status == "blocked")
        duration_ms = int((time.time() - started_ts) * 1000)

        self._write_live_info(
            live_dir, status="done", total=len(urls), done=len(urls),
            run_id=run_id, started_ts=started_ts,
        )

        return RunReport(
            run_id=run_id,
            started_at=datetime.fromtimestamp(started_ts)
                .isoformat(timespec="seconds"),
            finished_at=datetime.now().isoformat(timespec="seconds"),
            base_url=self.base_url,
            headless=self.headless,
            total=len(scripts),
            passed=passed,
            failed=failed,
            blocked=blocked,
            duration_ms=duration_ms,
            scripts=scripts,
        )

    # ── per-URL walk ─────────────────────────────────────────────

    def _walk_one(self, context, idx: int, url: str,
                  run_dir: str, live_dir: str) -> ScriptResult:
        """Visit ``url`` and capture one screenshot. PR-2 will replace
        this body with the full heuristic battery (broken-image scan,
        dropdown probe, form auto-fill, CTA audit, axe scan, ...);
        for PR-1 the contract is "navigate + screenshot, no findings".
        """
        tc_id = f"WALK-{idx:03d}"
        page = context.new_page()
        page.set_default_navigation_timeout(self.navigation_timeout_ms)
        steps: list[StepResult] = []
        t0 = time.time()
        try:
            page.goto(url, wait_until="domcontentloaded",
                      timeout=self.navigation_timeout_ms)
            shot_path = self._screenshot(page, run_dir, live_dir,
                                          tc_id, "page")
            steps.append(StepResult(
                index=1,
                action="goto",
                raw=url,
                status="passed",
                duration_ms=int((time.time() - t0) * 1000),
                screenshot_after=shot_path,
            ))
            final_url = page.url or url
            status = "passed"
            comment = ""
        except Exception as exc:
            steps.append(StepResult(
                index=1,
                action="goto",
                raw=url,
                status="failed",
                duration_ms=int((time.time() - t0) * 1000),
                comment=f"{type(exc).__name__}: {exc}"[:500],
            ))
            final_url = url
            status = "failed"
            comment = f"navigation failed: {type(exc).__name__}"
        finally:
            try:
                page.close()
            except Exception:
                pass
        return ScriptResult(
            tc_id=tc_id,
            summary=f"Walkthrough: {url}",
            status=status,
            duration_ms=int((time.time() - t0) * 1000),
            steps=steps,
            comment=comment,
            final_url=final_url,
        )

    # ── helpers ──────────────────────────────────────────────────

    def _blocked_script(self, idx: int, url: str,
                        reason: str) -> ScriptResult:
        return ScriptResult(
            tc_id=f"WALK-{idx:03d}",
            summary=f"Walkthrough: {url}",
            status="blocked",
            duration_ms=0,
            steps=[StepResult(
                index=1, action="goto", raw=url, status="blocked",
                comment=reason,
            )],
            comment=reason,
        )

    def _screenshot(self, page, run_dir: str, live_dir: str,
                    tc_id: str, label: str) -> str:
        """Save a screenshot to ``<run_dir>/<tc_id>/<label>.png`` and
        mirror it to ``<live_dir>/latest.png`` so the existing
        ``/test-execution/live`` viewer picks it up.

        Returns the storage-relative path so the post-run serialiser
        and ``_build_automation_assets`` in :mod:`runner_worker`
        resolve it the same way they resolve TC-driven shots.
        """
        rel_dir = os.path.join("automation_runs",
                                os.path.basename(run_dir), tc_id)
        abs_dir = os.path.join(self.storage_root, rel_dir)
        os.makedirs(abs_dir, exist_ok=True)
        rel_path = os.path.join(rel_dir, f"{label}.png").replace(os.sep, "/")
        abs_path = os.path.join(self.storage_root,
                                rel_path.replace("/", os.sep))
        try:
            page.screenshot(path=abs_path, full_page=False)
        except Exception as exc:
            _logger.debug("walkthrough screenshot failed: %s", exc)
            return ""
        if live_dir:
            try:
                shutil.copyfile(abs_path,
                                os.path.join(live_dir, "latest.png.tmp"))
                os.replace(os.path.join(live_dir, "latest.png.tmp"),
                           os.path.join(live_dir, "latest.png"))
            except OSError:
                pass
        return rel_path


__all__ = [
    "WalkthroughRunner",
    "WALKTHROUGH_FLAG_ENV",
    "feature_enabled",
]
