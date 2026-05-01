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
AUTOMATION_RUN_RETENTION_DAYS = int(os.environ.get("AUTOMATION_RUN_RETENTION_DAYS", "14"))


def _purge_old_automation_runs(runs_root: str, max_age_days: int) -> int:
    """Delete per-run directories under ``runs_root`` older than ``max_age_days``.

    Returns the number of runs removed. Best-effort: failures are logged
    but never raised, since retention cleanup should not block new runs.
    """
    if max_age_days <= 0 or not os.path.isdir(runs_root):
        return 0
    cutoff = time.time() - (max_age_days * 86400)
    removed = 0
    try:
        entries = os.listdir(runs_root)
    except OSError as exc:
        _logger.warning("purge: cannot list %s: %s", runs_root, exc)
        return 0
    for name in entries:
        path = os.path.join(runs_root, name)
        if not os.path.isdir(path):
            continue
        try:
            if os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError as exc:
            _logger.debug("purge: skip %s: %s", path, exc)
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
                 record_video: bool = True, default_timeout_ms: int = 5000,
                 slow_mo_ms: int | None = None, step_pause_ms: int | None = None,
                 credentials: TestCredentials | None = None):
        self.storage_root = storage_root
        self.base_url = base_url
        self.headless = headless
        self.viewport = viewport
        self.record_video = record_video
        self.default_timeout_ms = default_timeout_ms
        # In headed mode we visibly slow the browser so the user can see
        # clicks / fills / navigations happen. In headless mode we don't
        # need the extra delay.
        # In headed mode we visibly slow the browser so the user can see
        # clicks / fills / navigations happen. 500ms slow_mo + 700ms step
        # pause is the smallest pair that makes activity legible without
        # making the run feel sluggish.
        self.slow_mo_ms = slow_mo_ms if slow_mo_ms is not None else (500 if not headless else 0)
        self.step_pause_ms = step_pause_ms if step_pause_ms is not None else (700 if not headless else 0)
        self.credentials = credentials
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
            _purge_old_automation_runs(runs_root, AUTOMATION_RUN_RETENTION_DAYS)
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
        except OSError as exc:
            _logger.debug("live: cannot reset _live dir: %s", exc)
            self._live_dir = ""

        # Step counter for live-info JSON. Bumped on every screenshot.
        self._live_step = 0
        self._live_run_id = run_id
        self._live_total = max(1, len(scripts))
        self._live_done = 0
        self._live_current_tc = ""
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
        page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
        page.on("pageerror", lambda exc: console_errors.append(str(exc)))

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

        try:
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
                # In headed mode perform a visible scroll so the user sees page activity
                self._visible_scroll(page)
            elif step.action == "click":
                loc = _locator(page, step.target).first
                self._scroll_and_highlight(page, loc)
                loc.click()
            elif step.action == "fill":
                loc = _locator(page, step.target).first
                self._scroll_and_highlight(page, loc)
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
                loc.select_option(step.value)
            elif step.action == "check":
                loc = _locator(page, step.target).first
                self._scroll_and_highlight(page, loc)
                loc.check()
            elif step.action == "expect_text":
                page.wait_for_timeout(150)
                if step.value:
                    content = page.content()
                    if step.value.lower() not in content.lower():
                        raise AssertionError(f"Expected text not found: {step.value!r}")
            elif step.action == "wait":
                page.wait_for_timeout(int(float(step.value or "2")) * 1000)

            self._screenshot(page, after_path)
            sr.screenshot_after = _rel_url(after_path, self.storage_root)
            # Visible pause between steps in headed mode
            if self.step_pause_ms:
                page.wait_for_timeout(self.step_pause_ms)

        except AssertionError as e:
            sr.status = "failed"
            sr.comment = str(e)
            self._screenshot(page, after_path)
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
                sr.screenshot_after = _rel_url(after_path, self.storage_root)
            except Exception:
                pass

        sr.duration_ms = int((time.time() - t0) * 1000)
        return sr

    def _screenshot(self, page, path: str) -> None:
        try:
            page.screenshot(path=path, full_page=True)
        except Exception:
            return
        # Mirror the freshly-written file as the global "latest" frame so
        # the /test-execution/live page can show real-time progress on
        # cloud deployments where the browser itself is invisible to the
        # operator. Best-effort: any IO failure here must never abort the
        # run.
        live_dir = getattr(self, "_live_dir", "")
        if not live_dir:
            return
        try:
            self._live_step += 1
            dst = os.path.join(live_dir, "latest.png")
            shutil.copyfile(path, dst)
            self._write_live_info(status="running")
        except Exception as exc:  # pragma: no cover — IO best-effort
            _logger.debug("live: cannot mirror frame: %s", exc)

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
            payload = {
                "run_id": getattr(self, "_live_run_id", ""),
                "status": status,
                "step": getattr(self, "_live_step", 0),
                "cases_done": getattr(self, "_live_done", 0),
                "cases_total": getattr(self, "_live_total", 1),
                "current_tc": getattr(self, "_live_current_tc", ""),
                "base_url": self.base_url or "",
                "headless": self.headless,
                "ts": int(time.time() * 1000),
            }
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(payload, f)
            os.replace(tmp, dst)
        except Exception as exc:  # pragma: no cover — IO best-effort
            _logger.debug("live: cannot write info.json: %s", exc)

    def _scroll_and_highlight(self, page, loc) -> None:
        """Bring the target into view and flash a red outline so the user can
        see which element the automation is about to interact with. No-op in
        headless mode (where nobody is watching)."""
        try:
            loc.scroll_into_view_if_needed(timeout=2000)
        except Exception:
            pass
        if self.headless:
            return
        # Raise the window in headed mode so the highlight is visible
        # even if the IDE / Flask console grabbed focus.
        try:
            page.bring_to_front()
        except Exception:
            pass
        try:
            # Highlight via inline style; revert shortly after.
            loc.evaluate(
                "el => { const prev = el.style.outline; "
                "el.style.outline = '3px solid #ff4d4f'; "
                "el.style.outlineOffset = '2px'; "
                "setTimeout(() => { el.style.outline = prev; }, 600); }"
            )
            page.wait_for_timeout(120)
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
