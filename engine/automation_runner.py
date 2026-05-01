"""
TestFortge — Automation Runner

Executes AutomationScript objects in Playwright with step-by-step screenshots.
"""
from __future__ import annotations
import os
import re
import shutil
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime

from engine.automation_qa import AutomationScript, AutomationStep
from engine.log import get_logger
from engine.test_credentials import TestCredentials

_logger = get_logger(__name__)

# Retain automation run artefacts for this many days; older runs are
# purged after each new run to keep /storage/automation_runs from growing
# unbounded (each run can be many MB of screenshots + video).
# Render free tier has 512 MB RAM and ephemeral disk that fills fast on a
# single TestForTge instance — the previous 14-day default surfaced as
# 502 Bad Gateway after a handful of runs because gunicorn workers OOM'd
# trying to evict cached assets. Default is now 1 day plus a hard cap on
# the retained run count.
AUTOMATION_RUN_RETENTION_DAYS = int(os.environ.get("AUTOMATION_RUN_RETENTION_DAYS", "1"))
AUTOMATION_RUN_MAX_KEPT = int(os.environ.get("AUTOMATION_RUN_MAX_KEPT", "5"))


def _purge_old_automation_runs(runs_root: str, max_age_days: int,
                                max_kept: int = 0) -> int:
    """Delete per-run directories under ``runs_root`` that are either
    older than ``max_age_days`` OR beyond the most-recent ``max_kept``
    by mtime. Returns total removed. Best-effort: failures are logged
    but never raised, since retention cleanup should not block new runs.

    The combined age + count cap is what keeps Render free-tier disk
    from filling: a single full run with screenshots + video can land
    at 50–200 MB, and free-tier ephemeral storage caps around 1 GB.
    """
    if not os.path.isdir(runs_root):
        return 0
    removed = 0
    try:
        entries = os.listdir(runs_root)
    except OSError as exc:
        _logger.warning("purge: cannot list %s: %s", runs_root, exc)
        return 0

    # Score each run-dir by mtime so we can apply both retention rules.
    runs = []
    for name in entries:
        # The "_live" directory holds the polling mirror for the live
        # view; never purge it as part of run-history cleanup.
        if name == "_live":
            continue
        path = os.path.join(runs_root, name)
        if not os.path.isdir(path):
            continue
        try:
            runs.append((os.path.getmtime(path), path))
        except OSError:
            continue

    # 1) Age-based purge.
    if max_age_days > 0:
        cutoff = time.time() - (max_age_days * 86400)
        keep = []
        for mtime, path in runs:
            if mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
            else:
                keep.append((mtime, path))
        runs = keep

    # 2) Count-based purge — keep only the N most-recent.
    if max_kept > 0 and len(runs) > max_kept:
        runs.sort(key=lambda t: t[0], reverse=True)  # newest first
        for _mtime, path in runs[max_kept:]:
            shutil.rmtree(path, ignore_errors=True)
            removed += 1

    if removed:
        _logger.info("purge: removed %d old automation run(s) from %s", removed, runs_root)
    return removed


def _rel_url(path: str, root: str) -> str:
    """Return a path relative to root, using forward slashes (URL-safe on Windows)."""
    return os.path.relpath(path, root).replace(os.sep, "/")


@dataclass
class StepResult:
    index: int
    action: str
    raw: str
    status: str              # passed | failed | blocked
    duration_ms: int = 0
    comment: str = ""
    screenshot_before: str = ""
    screenshot_after: str = ""
    console_errors: list[str] = field(default_factory=list)


@dataclass
class ScriptResult:
    tc_id: str
    summary: str
    status: str              # passed | failed | blocked
    duration_ms: int = 0
    video_path: str = ""
    steps: list[StepResult] = field(default_factory=list)
    comment: str = ""
    final_url: str = ""


@dataclass
class RunReport:
    run_id: str
    started_at: str
    finished_at: str = ""
    base_url: str = ""
    headless: bool = True
    total: int = 0
    passed: int = 0
    failed: int = 0
    blocked: int = 0
    duration_ms: int = 0
    scripts: list[ScriptResult] = field(default_factory=list)

    @property
    def pass_rate(self) -> float:
        return (self.passed / self.total * 100) if self.total else 0.0


# ---------- Selector resolution ----------

def _locator(page, target: str):
    """Resolve our symbolic selectors to Playwright locators."""
    if target.startswith("role="):
        m = re.match(r"role=([a-z]+)(?:\[name=/(.+?)/i\])?", target)
        if m:
            role, name = m.group(1), m.group(2)
            return page.get_by_role(role, name=re.compile(name, re.I)) if name \
                else page.get_by_role(role)
    if target.startswith("placeholder=/"):
        m = re.match(r"placeholder=/(.+)/i", target)
        if m:
            return page.get_by_placeholder(re.compile(m.group(1), re.I))
    if target.startswith("text="):
        return page.get_by_text(target[5:], exact=False)
    return page.locator(target)


# ---------- Runner ----------

class AutomationRunner:
    def __init__(self, storage_root: str, base_url: str,
                 headless: bool = True, viewport: tuple[int, int] = (1280, 800),
                 record_video: bool = False,
                 default_timeout_ms: int = 3000,
                 navigation_timeout_ms: int = 15000,
                 slow_mo_ms: int | None = None, step_pause_ms: int | None = None,
                 credentials: TestCredentials | None = None,
                 # Speed knobs:
                 screenshot_full_page: bool = False,
                 screenshot_before_steps: bool = False):
        self.storage_root = storage_root
        self.base_url = base_url
        self.headless = headless
        self.viewport = viewport
        self.record_video = record_video
        # Element actions (click, fill, expect_text) get the short
        # default_timeout (3 s is enough for any element that's already
        # in the DOM). Page navigations need a separate, longer budget —
        # firing domcontentloaded on a real cold-start website routinely
        # takes 5–10 s, and using 3 s here was the root cause of every
        # test case being marked failed at goto.
        self.default_timeout_ms = default_timeout_ms
        self.navigation_timeout_ms = navigation_timeout_ms
        # When recording video, run actions slowly enough that the .webm
        # actually shows what happened — clicks, fills, scrolls, redirects.
        # 350 ms slow_mo + 500 ms step pause turns a typical TC video from
        # an unwatchable blur into a legible demo, while still being faster
        # than a human tester. Without record_video, headless stays at full
        # speed (no audience to slow down for).
        if slow_mo_ms is not None:
            self.slow_mo_ms = slow_mo_ms
        elif not headless:
            self.slow_mo_ms = 500
        elif record_video:
            self.slow_mo_ms = 350
        else:
            self.slow_mo_ms = 0
        if step_pause_ms is not None:
            self.step_pause_ms = step_pause_ms
        elif not headless:
            self.step_pause_ms = 700
        elif record_video:
            self.step_pause_ms = 500
        else:
            self.step_pause_ms = 0
        self.credentials = credentials
        # Speed flags: viewport-only screenshots are 5–10× faster on long
        # pages than full_page=True (which forces a full layout pass and
        # can produce 8000-px-tall PNGs). Skipping "before" shots halves
        # IO without losing diagnostic value because the previous step's
        # "after" frame *is* the next step's "before".
        self.screenshot_full_page = screenshot_full_page
        self.screenshot_before_steps = screenshot_before_steps
        # Set to True once we've successfully authenticated inside a
        # browser context; the login sequence only needs to run once per
        # context, not once per test case.
        self._logged_in_once = False

    def run(self, scripts: list[AutomationScript]) -> RunReport:
        from playwright.sync_api import sync_playwright

        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
        runs_root = os.path.join(self.storage_root, "automation_runs")
        run_dir = os.path.join(runs_root, run_id)
        os.makedirs(run_dir, exist_ok=True)

        # Retention cleanup (fire-and-forget; never blocks the run on error).
        try:
            _purge_old_automation_runs(
                runs_root,
                AUTOMATION_RUN_RETENTION_DAYS,
                AUTOMATION_RUN_MAX_KEPT,
            )
        except Exception as exc:
            _logger.debug("retention purge skipped: %s", exc)

        # ── Live-view bookkeeping ─────────────────────────────────────
        # The /test-execution/live endpoint serves the most recent frame
        # from <storage>/automation_runs/_live/latest.png and progress
        # JSON from _live/info.json. Reset this directory at run start so
        # the operator's "Watch live" tab transitions cleanly from a
        # previous run's last frame to "running" state for this run.
        self._live_dir = os.path.join(runs_root, "_live")
        try:
            os.makedirs(self._live_dir, exist_ok=True)
            # Wipe stale frame so the live page shows a "starting" placeholder
            # until the first real screenshot lands.
            stale = os.path.join(self._live_dir, "latest.png")
            if os.path.exists(stale):
                os.remove(stale)
            # Write a "warming up" splash so the live tab doesn't sit on a
            # 1×1 transparent placeholder (which renders as a black panel
            # against our dark card background) for the first ~10 s while
            # Playwright launches Chromium.
            self._write_warmup_frame(self._live_dir, len(scripts))
        except OSError as exc:
            _logger.debug("live: cannot reset _live dir: %s", exc)
            self._live_dir = ""

        # Step counter for live-info JSON. Bumped on every screenshot.
        self._live_step = 0
        self._live_run_id = run_id
        self._live_total = max(1, len(scripts))
        self._live_done = 0
        self._live_current_tc = ""
        # Pace tracking — start time used to derive elapsed_ms,
        # avg_ms_per_case, cases_per_minute for the live page header.
        self._live_started_ts = time.time()
        self._write_live_info(status="starting")

        report = RunReport(
            run_id=run_id,
            started_at=datetime.now().isoformat(timespec="seconds"),
            base_url=self.base_url,
            headless=self.headless,
            total=len(scripts),
        )
        t0 = time.time()

        # Launch args. In headed mode we ask Chrome to start maximised so
        # the window is large and obvious; we also disable the
        # "Chrome is being controlled by automated software" infobar that
        # otherwise overlays the page (Playwright's default
        # `--enable-automation` flag is what triggers it).
        launch_args: list[str] = []
        if not self.headless:
            launch_args = [
                "--start-maximized",
                "--disable-blink-features=AutomationControlled",
            ]

        _logger.info(
            "automation: launching chromium (headless=%s, slow_mo=%dms, "
            "step_pause=%dms, scripts=%d, base_url=%s)",
            self.headless, self.slow_mo_ms, self.step_pause_ms,
            len(scripts), self.base_url or "<none>",
        )

        with sync_playwright() as p:
            # Headed mode (headless=False) requires a real display server.
            # On cloud / CI environments (Render, GitHub Actions, Docker
            # without Xvfb) there is no display, so the launch raises an
            # error like "cannot open display" or "Failed to launch".
            # We catch that specific failure and fall back to headless so
            # automation still produces screenshots / video — the only
            # thing missing is the live window on the operator's screen.
            try:
                browser = p.chromium.launch(
                    headless=self.headless,
                    slow_mo=self.slow_mo_ms,
                    args=launch_args,
                )
            except Exception as launch_exc:
                if not self.headless:
                    _logger.warning(
                        "automation: headed launch failed (%s) — no display "
                        "available on this server; retrying in headless mode.",
                        launch_exc,
                    )
                    self.headless = True
                    self.slow_mo_ms = 0
                    self.step_pause_ms = 0
                    report.headless = True
                    browser = p.chromium.launch(
                        headless=True,
                        slow_mo=0,
                        args=["--no-sandbox", "--disable-dev-shm-usage"],
                    )
                else:
                    raise
            try:
                # In headed mode give the user ~1 second to notice the
                # window appearing before we start firing test cases at it.
                if not self.headless:
                    time.sleep(1.0)
                for script in scripts:
                    self._live_current_tc = (script.tc_id or "TC")
                    # Step counter is per-test-case so the live page shows
                    # "step #3" inside the current TC, not a cumulative
                    # number across the whole run.
                    self._live_step = 0
                    self._write_live_info(status="running")
                    sr = self._run_script(browser, script, run_dir)
                    report.scripts.append(sr)
                    if sr.status == "passed": report.passed += 1
                    elif sr.status == "failed": report.failed += 1
                    else: report.blocked += 1
                    self._live_done += 1
                    self._write_live_info(status="running")
            finally:
                browser.close()

        report.duration_ms = int((time.time() - t0) * 1000)
        report.finished_at = datetime.now().isoformat(timespec="seconds")
        self._write_live_info(status="done")
        return report

    def _run_script(self, browser, script: AutomationScript, run_dir: str) -> ScriptResult:
        tc_dir = os.path.join(run_dir, script.tc_id or "TC")
        os.makedirs(tc_dir, exist_ok=True)

        result = ScriptResult(tc_id=script.tc_id, summary=script.summary, status="passed")
        console_errors: list[str] = []

        # In headed mode let the maximised window own the viewport so the
        # user sees the real browser size. In headless mode we pin a
        # 1280x800 viewport for stable screenshots/videos.
        if self.headless:
            ctx_kwargs = dict(viewport={"width": self.viewport[0], "height": self.viewport[1]})
            if self.record_video:
                ctx_kwargs["record_video_dir"] = tc_dir
                ctx_kwargs["record_video_size"] = {"width": self.viewport[0], "height": self.viewport[1]}
        else:
            ctx_kwargs = dict(no_viewport=True)
            if self.record_video:
                ctx_kwargs["record_video_dir"] = tc_dir
                ctx_kwargs["record_video_size"] = {"width": 1280, "height": 800}

        context = browser.new_context(**ctx_kwargs)
        page = context.new_page()
        # In headed mode raise the window/tab to the front so it isn't
        # hidden behind the IDE / Flask console when the run starts.
        if not self.headless:
            try:
                page.bring_to_front()
            except Exception:
                pass
        page.set_default_timeout(self.default_timeout_ms)
        page.set_default_navigation_timeout(self.navigation_timeout_ms)
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))

        # Inject a custom CSS cursor on every page so the recorded video
        # AND screenshots have a visible pointer. Headless Chromium does
        # NOT render the OS cursor in either video or screenshots, which
        # is why the user said the recordings "look like screenshots".
        # We attach to every navigation via init script so the cursor
        # survives goto / SPA route changes.
        if self.record_video or not self.headless:
            cursor_script = (
                "(() => {"
                "  if (window.__tfCursorInjected) return;"
                "  window.__tfCursorInjected = true;"
                "  const c = document.createElement('div');"
                "  c.id = '__tf_cursor';"
                "  c.style.cssText = 'position:fixed;left:0;top:0;width:28px;"
                "height:28px;pointer-events:none;z-index:2147483647;"
                "transition:transform 350ms ease;transform:translate(0,0);'"
                "+ \"background:url(\\\"data:image/svg+xml;utf8,\""
                "+ encodeURIComponent('<svg xmlns=\\\"http://www.w3.org/2000/svg\\\""
                " width=\\\"28\\\" height=\\\"28\\\" viewBox=\\\"0 0 28 28\\\">"
                "<path d=\\\"M2 2 L2 22 L7 18 L11 27 L15 25 L11 16 L19 16 Z\\\""
                " fill=\\\"%23dc2626\\\" stroke=\\\"white\\\" stroke-width=\\\"1.5\\\""
                "/></svg>') + \"\\\") no-repeat;\";"
                "  document.body && document.body.appendChild(c);"
                "  window.__tfMoveCursor = (x, y) => {"
                "    const el = document.getElementById('__tf_cursor');"
                "    if (el) el.style.transform = 'translate(' + x + 'px,' + y + 'px)';"
                "  };"
                "})();"
            )
            try:
                context.add_init_script(cursor_script)
                page.evaluate(cursor_script)
            except Exception as exc:
                _logger.debug("cursor inject: %s", exc)

        # Authenticate (and auto-register when requested) before running
        # test-case steps. Any failure here is recorded as a synthetic
        # step so the engineer sees what went wrong.
        if self.credentials and self.credentials.is_active():
            auth_step = self._authenticate(page, tc_dir)
            if auth_step is not None:
                result.steps.append(auth_step)
                if auth_step.status == "failed":
                    result.status = "blocked"
                    result.comment = f"Login failed: {auth_step.comment}"

        t_script = time.time()
        try:
            for i, step in enumerate(script.steps, start=1):
                sr = self._run_step(page, step, i, tc_dir)
                sr.console_errors = list(console_errors)
                result.steps.append(sr)
                if sr.status == "failed":
                    result.status = "failed"
                    result.comment = f"Failed at step {i}: {sr.comment}"
                    break
                if sr.status == "blocked":
                    result.status = "blocked"
                    result.comment = f"Blocked at step {i}: {sr.comment}"
                    break
            result.final_url = page.url
        finally:
            try:
                video = page.video
                context.close()
                if video and self.record_video:
                    result.video_path = _rel_url(video.path(), self.storage_root)
            except Exception:
                context.close()

        result.duration_ms = int((time.time() - t_script) * 1000)
        return result

    def _run_step(self, page, step: AutomationStep, idx: int, tc_dir: str) -> StepResult:
        sr = StepResult(index=idx, action=step.action, raw=step.raw, status="passed")
        t0 = time.time()
        before_path = os.path.join(tc_dir, f"step_{idx:02d}_before.png")
        after_path = os.path.join(tc_dir, f"step_{idx:02d}_after.png")
        # Bounding box of the target element — captured early so we can
        # annotate the after-screenshot with a red rectangle + arrow if
        # the step fails. None for steps that don't address an element
        # (goto, expect_text, wait, ...).
        target_bbox = None

        try:
            # "Before" screenshots are optional: in fast mode we skip them
            # and rely on the previous step's "after" image to provide the
            # before-state context. Halves IO per step and shaves seconds
            # off long runs.
            if self.screenshot_before_steps:
                self._screenshot(page, before_path)
                sr.screenshot_before = _rel_url(before_path, self.storage_root)

            if step.action == "goto":
                # Make sure the window is in front before each navigation so
                # the user actually sees the page load in headed mode.
                if not self.headless:
                    try: page.bring_to_front()
                    except Exception: pass
                target_url = step.target or self.base_url
                if not target_url or not target_url.strip():
                    raise AssertionError(
                        "No URL to navigate to. Set 'Base URL' on the "
                        "Automation page or include a URL in the test case."
                    )
                # Reject obviously malformed URLs early so Playwright
                # doesn't error with "navigating to <garbage>" — see
                # BUG-018 in the self-audit report.
                if not (target_url.startswith("http://")
                        or target_url.startswith("https://")):
                    raise AssertionError(
                        f"Invalid URL (must start with http:// or https://): "
                        f"{target_url!r}"
                    )
                page.goto(target_url, wait_until="domcontentloaded")
                # Intermediate live frame so the operator sees the page
                # arrive on the live tab, not just after the whole step.
                self._live_pump(page, tc_dir, idx, "nav")
                # In headed mode perform a visible scroll so the user sees page activity
                self._visible_scroll(page)
            elif step.action == "click":
                loc = _locator(page, step.target).first
                self._scroll_and_highlight(page, loc)
                target_bbox = self._safe_bbox(loc)
                self._move_cursor_to(page, target_bbox)
                loc.click()
            elif step.action == "fill":
                loc = _locator(page, step.target).first
                self._scroll_and_highlight(page, loc)
                target_bbox = self._safe_bbox(loc)
                self._move_cursor_to(page, target_bbox)
                # Clear then fill — Playwright's fill() is instant; use type() in
                # headed mode so the user sees keystrokes.
                loc.fill("")
                if self.headless:
                    loc.fill(step.value)
                else:
                    loc.type(step.value, delay=40)
            elif step.action == "select":
                loc = _locator(page, step.target).first
                self._scroll_and_highlight(page, loc)
                target_bbox = self._safe_bbox(loc)
                self._move_cursor_to(page, target_bbox)
                loc.select_option(step.value)
            elif step.action == "check":
                loc = _locator(page, step.target).first
                self._scroll_and_highlight(page, loc)
                target_bbox = self._safe_bbox(loc)
                self._move_cursor_to(page, target_bbox)
                loc.check()
            elif step.action == "expect_text":
                page.wait_for_timeout(150)
                if step.value:
                    content = page.content()
                    if step.value.lower() not in content.lower():
                        raise AssertionError(f"Expected text not found: {step.value!r}")
            elif step.action == "wait":
                # Long waits get periodic live frames so the operator
                # doesn't think the bot is frozen.
                wait_ms = int(float(step.value or "2")) * 1000
                pumped = 0
                while pumped + 1000 < wait_ms:
                    page.wait_for_timeout(1000)
                    self._live_pump(page, tc_dir, idx, f"wait_{pumped // 1000}s")
                    pumped += 1000
                if wait_ms > pumped:
                    page.wait_for_timeout(wait_ms - pumped)

            self._screenshot(page, after_path)
            sr.screenshot_after = _rel_url(after_path, self.storage_root)
            # Visible pause between steps in headed mode
            if self.step_pause_ms:
                page.wait_for_timeout(self.step_pause_ms)

        except AssertionError as e:
            sr.status = "failed"
            sr.comment = str(e)
            self._screenshot(page, after_path)
            self._annotate_failure(after_path, target_bbox, sr.comment)
            sr.screenshot_after = _rel_url(after_path, self.storage_root)
        except Exception as e:
            msg = str(e)
            if "Timeout" in msg or "timeout" in msg:
                sr.status = "blocked"
                sr.comment = f"Timeout/selector issue: {msg.splitlines()[0][:200]}"
            else:
                sr.status = "failed"
                sr.comment = f"{type(e).__name__}: {msg.splitlines()[0][:200]}"
            try:
                self._screenshot(page, after_path)
                self._annotate_failure(after_path, target_bbox, sr.comment)
                sr.screenshot_after = _rel_url(after_path, self.storage_root)
            except Exception:
                pass

        sr.duration_ms = int((time.time() - t0) * 1000)
        return sr

    def _live_pump(self, page, tc_dir: str, idx: int, label: str) -> None:
        """Cheap viewport screenshot pushed only to the live mirror so
        the /test-execution/live page updates between Playwright actions.
        Doesn't get added to the per-step gallery — each call writes to
        a single rolling temp path that's atomically renamed onto
        latest.png. Throttled to one frame per 700 ms so a fast step
        loop doesn't drown the disk.
        """
        live_dir = getattr(self, "_live_dir", "")
        if not live_dir:
            return
        now = time.time()
        last = getattr(self, "_live_last_pump", 0)
        if now - last < 0.4:
            return
        self._live_last_pump = now
        try:
            tmp_shot = os.path.join(tc_dir, f"_live_{idx:02d}_{label}.png")
            page.screenshot(path=tmp_shot, full_page=False, timeout=1500)
            os.makedirs(live_dir, exist_ok=True)
            dst = os.path.join(live_dir, "latest.png")
            tmp = dst + ".tmp"
            shutil.copyfile(tmp_shot, tmp)
            os.replace(tmp, dst)
            try:
                os.remove(tmp_shot)
            except OSError:
                pass
            self._write_live_info(status="running")
        except Exception as exc:  # pragma: no cover — best-effort
            _logger.debug("live pump: %s", exc)

    def _move_cursor_to(self, page, bbox: dict | None) -> None:
        """Animate the injected fake cursor to the centre of bbox so the
        recorded video and screenshots show a pointer travelling to the
        target. No-op when neither video recording nor headed mode are
        active (no audience). No-op when bbox is unknown."""
        if self.headless and not self.record_video:
            return
        if not bbox:
            return
        try:
            cx = int(bbox["x"] + bbox.get("width", 0) / 2)
            cy = int(bbox["y"] + bbox.get("height", 0) / 2)
            page.evaluate(
                "(args) => window.__tfMoveCursor && window.__tfMoveCursor(args.x, args.y)",
                {"x": cx, "y": cy},
            )
            # Allow CSS transition to play out so video catches the move.
            page.wait_for_timeout(380)
        except Exception as exc:
            _logger.debug("cursor move: %s", exc)

    @staticmethod
    def _safe_bbox(loc):
        """Read locator.bounding_box() defensively. Returns dict
        {x, y, width, height} in CSS pixels, or None if not measurable
        (off-screen, detached, action raised before Playwright resolved
        the selector)."""
        try:
            bbox = loc.bounding_box(timeout=500)
            if bbox and bbox.get("width", 0) > 0 and bbox.get("height", 0) > 0:
                return bbox
        except Exception:
            pass
        return None

    def _annotate_failure(self, image_path: str, bbox: dict | None, comment: str) -> None:
        """Overlay a red bounding box + arrow + caption onto a failure
        screenshot — but ONLY when we actually know which element the
        bug involves (bbox != None). Without a bbox we have nothing
        meaningful to point at, so we leave the screenshot untouched
        rather than slap a confusing full-width red banner on every
        innocent nav-timeout. Best-effort — never raises.
        """
        if not bbox:
            return  # nothing actionable to mark — leave the raw shot
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception:
            return
        try:
            with Image.open(image_path) as img:
                img = img.convert("RGBA")
                draw = ImageDraw.Draw(img)
                width_px, height_px = img.size

                # Most viewports render at scale 1, but high-DPI screens
                # report a 2× framebuffer. Compare bbox to image dims and
                # pick the plausible scale (1 or 2).
                scale = 1.0
                if bbox.get("x", 0) + bbox.get("width", 0) > width_px:
                    scale = width_px / max(1, bbox.get("x", 0) + bbox.get("width", 0))
                x1 = max(0, int(bbox["x"] * scale) - 4)
                y1 = max(0, int(bbox["y"] * scale) - 4)
                x2 = min(width_px - 1, int((bbox["x"] + bbox["width"]) * scale) + 4)
                y2 = min(height_px - 1, int((bbox["y"] + bbox["height"]) * scale) + 4)
                # Heavy red border (4 px) around the element
                for offset in range(4):
                    draw.rectangle(
                        (x1 - offset, y1 - offset, x2 + offset, y2 + offset),
                        outline=(220, 38, 38, 255),
                    )
                # Red arrow from upper-left margin pointing AT the box
                arrow_tail = (max(20, x1 - 80), max(20, y1 - 60))
                arrow_head = (x1, y1)
                draw.line([arrow_tail, arrow_head], fill=(220, 38, 38, 255), width=4)
                import math
                angle = math.atan2(arrow_head[1] - arrow_tail[1],
                                    arrow_head[0] - arrow_tail[0])
                head_len = 18
                head_angle = math.radians(26)
                p1 = (arrow_head[0] - head_len * math.cos(angle - head_angle),
                      arrow_head[1] - head_len * math.sin(angle - head_angle))
                p2 = (arrow_head[0] - head_len * math.cos(angle + head_angle),
                      arrow_head[1] - head_len * math.sin(angle + head_angle))
                draw.polygon([arrow_head, p1, p2], fill=(220, 38, 38, 255))

                # Compact caption sticker NEAR the element (not a full
                # banner across the whole page) so the screenshot stays
                # readable as a screenshot of the actual UI.
                caption = (comment or "Bug here").splitlines()[0][:140]
                try:
                    font = ImageFont.truetype("DejaVuSans-Bold.ttf", 14)
                except Exception:
                    font = ImageFont.load_default()
                # Measure caption to size the sticker
                try:
                    bb = draw.textbbox((0, 0), caption, font=font)
                    cap_w, cap_h = bb[2] - bb[0], bb[3] - bb[1]
                except Exception:
                    cap_w, cap_h = len(caption) * 8, 16
                pad = 8
                sticker_w = cap_w + pad * 2
                sticker_h = cap_h + pad * 2
                # Place sticker just above the arrow tail; clamp to image
                sx = max(8, min(width_px - sticker_w - 8, arrow_tail[0] - sticker_w // 2))
                sy = max(8, arrow_tail[1] - sticker_h - 6)
                draw.rectangle((sx, sy, sx + sticker_w, sy + sticker_h),
                               fill=(220, 38, 38, 240))
                draw.text((sx + pad, sy + pad), caption,
                          fill=(255, 255, 255, 255), font=font)

                img.convert("RGB").save(image_path, "PNG", optimize=True)
        except Exception as exc:  # pragma: no cover — annotation is best-effort
            _logger.debug("annotate failure: %s", exc)

    def _screenshot(self, page, path: str) -> None:
        try:
            page.screenshot(path=path, full_page=self.screenshot_full_page)
        except Exception:
            return
        # Mirror the freshly-written file as the global "latest" frame so
        # the /test-execution/live page can show real-time progress on
        # cloud deployments where the browser itself is invisible to the
        # operator. Atomic via tmp + os.replace so the polling reader
        # never sees a half-written file (which would surface as a broken
        # image in the browser). Best-effort: any IO failure here must
        # never abort the run.
        live_dir = getattr(self, "_live_dir", "")
        if not live_dir:
            return
        try:
            os.makedirs(live_dir, exist_ok=True)
            self._live_step += 1
            dst = os.path.join(live_dir, "latest.png")
            tmp = dst + ".tmp"
            shutil.copyfile(path, tmp)
            os.replace(tmp, dst)
            self._write_live_info(status="running")
        except Exception as exc:  # pragma: no cover — IO best-effort
            _logger.warning("live: cannot mirror frame: %s", exc)

    @staticmethod
    def _write_warmup_frame(live_dir: str, total_cases: int) -> None:
        """Drop an initial PNG into <live>/latest.png so the live page
        has something legible to show within a second of the run
        starting. Without this the operator stares at a black card for
        the 5–10 s it takes to launch Chromium and finish the first
        navigation. Best-effort: no PIL → silent no-op.
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception:
            return
        try:
            img = Image.new("RGB", (1280, 800), (15, 23, 42))
            draw = ImageDraw.Draw(img)
            try:
                font_lg = ImageFont.truetype("DejaVuSans-Bold.ttf", 32)
                font_md = ImageFont.truetype("DejaVuSans.ttf", 18)
            except Exception:
                font_lg = ImageFont.load_default()
                font_md = font_lg
            draw.text((40, 360), "Warming up Chromium…",
                      fill=(248, 250, 252), font=font_lg)
            draw.text((40, 410),
                      f"Preparing {total_cases} test case(s). The first frame "
                      f"will appear once the browser opens the first page.",
                      fill=(148, 163, 184), font=font_md)
            tmp = os.path.join(live_dir, "latest.png.tmp")
            dst = os.path.join(live_dir, "latest.png")
            img.save(tmp, "PNG", optimize=True)
            os.replace(tmp, dst)
        except Exception as exc:  # pragma: no cover — best-effort
            _logger.debug("warmup frame: %s", exc)

    def _write_live_info(self, status: str) -> None:
        """Atomically write the live status JSON consumed by the
        /test-execution/live polling endpoint. Best-effort: never raises.
        """
        live_dir = getattr(self, "_live_dir", "")
        if not live_dir:
            return
        try:
            import json
            tmp = os.path.join(live_dir, "info.json.tmp")
            dst = os.path.join(live_dir, "info.json")
            started_ts = getattr(self, "_live_started_ts", time.time())
            done = getattr(self, "_live_done", 0)
            elapsed_ms = int((time.time() - started_ts) * 1000)
            avg_ms_per_case = int(elapsed_ms / done) if done > 0 else 0
            cases_per_min = round((done / (elapsed_ms / 60000.0)), 2) if elapsed_ms > 0 and done > 0 else 0
            payload = {
                "run_id": getattr(self, "_live_run_id", ""),
                "status": status,
                "step": getattr(self, "_live_step", 0),
                "cases_done": done,
                "cases_total": getattr(self, "_live_total", 1),
                "current_tc": getattr(self, "_live_current_tc", ""),
                "base_url": self.base_url or "",
                "headless": self.headless,
                "elapsed_ms": elapsed_ms,
                "avg_ms_per_case": avg_ms_per_case,
                "cases_per_minute": cases_per_min,
                "ts": int(time.time() * 1000),
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, dst)
        except Exception as exc:  # pragma: no cover — IO best-effort
            _logger.debug("live: cannot write info.json: %s", exc)

    def _scroll_and_highlight(self, page, loc) -> None:
        """Bring the target into view and flash a red outline. Active in
        headed mode (so the operator sees what's being interacted with)
        AND in headless mode when video recording is on (so the .webm is
        legible). When neither is true, this is a quick scroll-only no-op.
        """
        try:
            loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        # Skip the highlight + delay only when there's nothing to capture
        # — pure headless without video recording. That's the case where
        # a flashing outline would just slow the run down with no benefit.
        if self.headless and not self.record_video:
            return
        if not self.headless:
            try:
                page.bring_to_front()
            except Exception:
                pass
        try:
            # Brief red outline on the target element. Stays visible long
            # enough for the camera (be it a real screen or Playwright's
            # video recorder) to catch it before reverting.
            loc.evaluate(
                "el => { const prev = el.style.outline; "
                "el.style.outline = '3px solid #ff4d4f'; "
                "el.style.outlineOffset = '2px'; "
                "setTimeout(() => { el.style.outline = prev; }, 700); }"
            )
            page.wait_for_timeout(180)
        except Exception:
            pass

    def _authenticate(self, page, tc_dir: str) -> "StepResult | None":
        """Run the pre-test login (and optionally registration) sequence.

        Returns a synthetic StepResult describing the login attempt, or
        None if no credentials are configured for this run.
        """
        cred = self.credentials
        if not cred or not cred.is_active():
            return None

        idx = 0
        sr = StepResult(index=idx, action="login",
                        raw=f"Authenticate as {cred.username}",
                        status="passed")
        t0 = time.time()
        before_path = os.path.join(tc_dir, "login_before.png")
        after_path = os.path.join(tc_dir, "login_after.png")

        try:
            if self.screenshot_before_steps:
                self._screenshot(page, before_path)
                sr.screenshot_before = _rel_url(before_path, self.storage_root)

            # Step 1 — registration (only for generated accounts with URL)
            if cred.mode == "generated" and cred.register_url:
                self._attempt_register(page, cred)

            # Step 2 — login
            login_target = cred.login_url or self.base_url
            page.goto(login_target, wait_until="domcontentloaded")
            self._visible_scroll(page)

            u_loc = self._auth_locator(page, cred.username_selector,
                                        fallbacks=("email", "username", "login"))
            p_loc = self._auth_locator(page, cred.password_selector,
                                        fallbacks=("password",))

            self._scroll_and_highlight(page, u_loc)
            if self.headless:
                u_loc.fill(cred.username)
            else:
                u_loc.fill("")
                u_loc.type(cred.username, delay=40)

            self._scroll_and_highlight(page, p_loc)
            if self.headless:
                p_loc.fill(cred.password)
            else:
                p_loc.fill("")
                p_loc.type(cred.password, delay=40)

            submit_sel = cred.submit_selector or "role=button[name=/(log.?in|sign.?in|submit|continue)/i]"
            sub_loc = _locator(page, submit_sel).first
            self._scroll_and_highlight(page, sub_loc)
            try:
                sub_loc.click()
            except Exception:
                # Fallback: press Enter in the password field
                p_loc.press("Enter")

            page.wait_for_load_state("domcontentloaded", timeout=self.default_timeout_ms)
            page.wait_for_timeout(250)

            self._screenshot(page, after_path)
            sr.screenshot_after = _rel_url(after_path, self.storage_root)
            sr.comment = f"Logged in as {cred.username} ({cred.mode})"
            self._logged_in_once = True

        except Exception as e:
            msg = str(e).splitlines()[0][:200]
            sr.status = "failed"
            sr.comment = f"{type(e).__name__}: {msg}"
            try:
                self._screenshot(page, after_path)
                sr.screenshot_after = _rel_url(after_path, self.storage_root)
            except Exception:
                pass

        sr.duration_ms = int((time.time() - t0) * 1000)
        return sr

    def _attempt_register(self, page, cred: TestCredentials) -> None:
        """Best-effort auto-registration for generated accounts.

        Fills any visible email/password/confirm-password fields on the
        registration page and clicks the primary submit button. Never
        raises — a failure here just means the engineer needs to create
        the account manually using the generated credentials.
        """
        try:
            page.goto(cred.register_url, wait_until="domcontentloaded")
            page.wait_for_timeout(150)
            # Email / username
            for sel in ("input[type='email']",
                        "input[name*='email' i]",
                        "input[name*='user' i]",
                        "input[autocomplete='username']"):
                try:
                    page.locator(sel).first.fill(cred.username, timeout=1500)
                    break
                except Exception:
                    continue
            # All password fields get the same password (signup + confirm)
            try:
                pw_fields = page.locator("input[type='password']")
                count = pw_fields.count()
                for i in range(min(count, 3)):
                    pw_fields.nth(i).fill(cred.password, timeout=1500)
            except Exception:
                pass
            # Optional display name
            if cred.display_name:
                for sel in ("input[name*='name' i]", "input[id*='name' i]"):
                    try:
                        page.locator(sel).first.fill(cred.display_name, timeout=1000)
                        break
                    except Exception:
                        continue
            # Submit
            for sel in ("role=button[name=/(sign.?up|register|create|continue)/i]",
                        "button[type='submit']"):
                try:
                    _locator(page, sel).first.click(timeout=1500)
                    break
                except Exception:
                    continue
            page.wait_for_load_state("domcontentloaded", timeout=self.default_timeout_ms)
        except Exception:
            # Swallow — registration is best-effort.
            pass

    @staticmethod
    def _auth_locator(page, selector: str, fallbacks: tuple[str, ...]):
        """Resolve a login-form field with an explicit selector or a list of
        heuristic placeholder/name fallbacks."""
        if selector:
            return _locator(page, selector).first
        # Try placeholder / name / id / type heuristics
        for f in fallbacks:
            try:
                loc = page.get_by_placeholder(re.compile(f, re.I)).first
                loc.wait_for(state="visible", timeout=500)
                return loc
            except Exception:
                pass
            for attr in ("name", "id", "autocomplete", "aria-label"):
                try:
                    loc = page.locator(f"input[{attr}*='{f}' i]").first
                    loc.wait_for(state="visible", timeout=500)
                    return loc
                except Exception:
                    continue
        # Last resort: first password-or-text input that matches type
        if "password" in fallbacks:
            return page.locator("input[type='password']").first
        return page.locator("input[type='text'], input[type='email']").first

    def _visible_scroll(self, page) -> None:
        """After a navigation, do a short visible scroll so the user sees
        the page was actually opened and rendered. No-op in headless mode."""
        if self.headless:
            return
        try:
            page.evaluate(
                "async () => {"
                "  const step = () => window.scrollBy({top: 300, behavior:'smooth'});"
                "  for (let i = 0; i < 3; i++) { step(); "
                "    await new Promise(r => setTimeout(r, 250)); }"
                "  window.scrollTo({top: 0, behavior:'smooth'});"
                "}"
            )
            page.wait_for_timeout(300)
        except Exception:
            pass
