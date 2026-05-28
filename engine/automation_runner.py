"""
TestFortge — Automation Runner

Executes AutomationScript objects in Playwright with step-by-step screenshots.
"""
from __future__ import annotations
import fnmatch
import os
import re
import shutil
import time
import traceback
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime

from engine.automation_qa import AutomationScript, AutomationStep
from engine.locator_registry import (best_alternates as _best_alternates,
                                      record_failure as _record_failure,
                                      record_success as _record_success)
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

# Module-level handle to the currently-active Playwright browser. The
# runner_worker SIGTERM/SIGINT handler grabs this so it can force-close
# Chromium before the kernel reaps the worker; without it a SIGTERM
# leaves a zombie chromium process holding 250 MB of RAM until the OS
# OOM-kills it. Set inside ``AutomationRunner.run`` immediately after
# each successful ``engine_obj.launch(...)`` (initial + rotation cycle)
# and cleared in the matching ``finally`` once the browser has been
# closed cleanly.
_CURRENT_BROWSER = None


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


def _is_mostly_blank(image_path: str, threshold: float = 0.97) -> bool:
    """Return True if >threshold fraction of sampled pixels are near-
    white (R,G,B all >245). Used to detect screenshots taken before the
    page actually painted — operator reported empty white thumbnails
    in the post-run gallery. Sampling: every 16th pixel on every 16th
    row, so this is O(width*height/256) and runs in <30 ms even on
    1280×800. Returns False on any error so we never reject a real shot.
    """
    try:
        from PIL import Image
        with Image.open(image_path) as im:
            im = im.convert("RGB")
            w, h = im.size
            if w < 64 or h < 64:
                return False
            px = im.load()
            white = 0
            sampled = 0
            for y in range(0, h, 16):
                for x in range(0, w, 16):
                    r, g, b = px[x, y]
                    sampled += 1
                    if r > 245 and g > 245 and b > 245:
                        white += 1
            if sampled == 0:
                return False
            return (white / sampled) > threshold
    except Exception:
        return False


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
    # Annotated copy of the after-shot — only populated when this step
    # failed/blocked. Used exclusively as bug-report evidence; the post-
    # run gallery still uses the clean screenshot_after so Passed cases
    # never display red error banners.
    screenshot_failure: str = ""
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


# PR-A: maximum chain length after registry promotion. Caps the wait
# budget so a label with N strategies registered can't multiply a
# missing-element wait into N × per_cand_ms of dead time. 4 = primary
# + 3 alternates, which covers every recorder-emitted shape in PR-A
# (joined chain + leaf-only + role-only + text=name).
MAX_CHAIN_TARGETS = 4


def _step_label(step: AutomationStep) -> str:
    """Human-readable step label for failure annotations + logs.

    For PR-C assertion steps we surface ``"assert visible"`` /
    ``"assert text"`` / ``"assert url"`` instead of the generic
    ``step.action`` so the annotated failure screenshot — and the
    bug-report attachment that uses it — names what actually went
    wrong. Action steps keep their existing label (``click``, ``goto``)
    so PR-A/PR-B golden outputs stay byte-identical.
    """
    if step.kind == "assertion":
        atype = (step.assertion_type or "").strip().lower() or "?"
        return f"assert {atype}"
    return step.action


def _is_locator_drift(exc: BaseException) -> bool:
    """True when an exception from ``loc.wait_for(state='visible', ...)``
    looks like locator drift (element missing / detached / never
    visible) — the chain should try the next candidate. False for hard
    failures (page crashed, websocket closed, browser disconnected) —
    those must surface immediately so the runner can mark the step as
    blocked instead of silently walking the entire chain looking for
    something that the browser can no longer answer.

    Message-based because Playwright's TimeoutError class doesn't
    inherit from Python's builtin TimeoutError, and importing the
    playwright module at module-load time breaks pure-Python unit
    tests that stub the page. Both Playwright + the test's builtin
    TimeoutError surface the words ``timeout`` / ``timed out`` /
    ``never visible`` in their messages.
    """
    msg = str(exc).lower()
    return any(needle in msg for needle in
               ("timeout", "timed out", "never visible", "not visible",
                "waiting for"))


# ---------- Selector resolution ----------

def _locator(page, target: str):
    """Resolve our symbolic selectors to Playwright locators.

    Two authoring paths converge here: the heuristic
    ``parse_manual_step`` (regex-name form ``role=button[name=/save/i]``,
    regex-placeholder form ``placeholder=/email/i``), and the recorder
    parser (literal-name form ``role=button[name="Save"]`` plus
    ``data-testid=`` / ``label=`` / ``placeholder=`` / ``alt=`` /
    ``title=`` literal forms). Both must decode to the right Playwright
    accessor — ``page.locator("label=Email")`` is invalid CSS, so any
    unrecognised prefix that falls through is silently broken.

    Prefix → Playwright API:
      * ``role=<r>``                  → ``get_by_role(r)``
      * ``role=<r>[name=/.../i]``     → ``get_by_role(r, name=regex)``
      * ``role=<r>[name="..."]``      → ``get_by_role(r, name=literal)``
      * ``label=<text>``              → ``get_by_label(text)``
      * ``placeholder=/.../i``        → ``get_by_placeholder(regex)``
      * ``placeholder=<text>``        → ``get_by_placeholder(text)``
      * ``text=<text>``               → ``get_by_text(text, exact=False)``
      * ``data-testid=<id>``          → ``get_by_test_id(id)``
      * ``alt=<text>``                → ``get_by_alt_text(text)``
      * ``title=<text>``              → ``get_by_title(text)``
      * anything else                 → ``page.locator(target)`` (CSS/XPath)
    """
    if target.startswith("role="):
        # Literal-name form (recorder output) — must be tried BEFORE
        # the regex-name form so a quoted name doesn't get parsed as
        # the role-only group of the regex pattern below.
        m_lit = re.match(r'role=([a-zA-Z]+)\[name="([^"]+)"\]', target)
        if m_lit:
            return page.get_by_role(m_lit.group(1), name=m_lit.group(2))
        # Regex-name form (heuristic parser output).
        m_re = re.match(r"role=([a-zA-Z]+)(?:\[name=/(.+?)/i\])?", target)
        if m_re:
            role, name = m_re.group(1), m_re.group(2)
            if name:
                return page.get_by_role(role, name=re.compile(name, re.I))
            return page.get_by_role(role)
    if target.startswith("placeholder=/"):
        m = re.match(r"placeholder=/(.+)/i", target)
        if m:
            return page.get_by_placeholder(re.compile(m.group(1), re.I))
    if target.startswith("placeholder="):
        return page.get_by_placeholder(target[len("placeholder="):])
    if target.startswith("text="):
        return page.get_by_text(target[5:], exact=False)
    if target.startswith("label="):
        return page.get_by_label(target[len("label="):])
    if target.startswith("data-testid="):
        return page.get_by_test_id(target[len("data-testid="):])
    if target.startswith("alt="):
        return page.get_by_alt_text(target[len("alt="):])
    if target.startswith("title="):
        return page.get_by_title(target[len("title="):])
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
                 screenshot_before_steps: bool = False,
                 # Feature #6 — Engine × Platform × Browser matrix.
                 # When a row from PLATFORM_BROWSER_MATRIX is supplied,
                 # the runner picks the matching Playwright engine
                 # (chromium / firefox / webkit), sets a real-world UA
                 # for the chosen OS+browser combo, and pins the
                 # viewport so screenshots/videos honour what the
                 # tester picked. All three default to None so existing
                 # call-sites that don't care about the matrix keep
                 # working unchanged.
                 engine_kind: str | None = None,
                 user_agent: str | None = None,
                 viewport_override: tuple[int, int] | None = None,
                 # PR-A multi-locator fallback. When provided, the
                 # runner consults ``engine.locator_registry`` to promote
                 # the previously-winning strategy for each labelled step
                 # and writes back success/failure counts. Empty string
                 # disables registry interaction entirely — the chain
                 # walk still tries every alternate but learns nothing
                 # across runs. Defaulting to "" keeps every existing
                 # call-site working unchanged.
                 project_id: str | None = None):
        self.storage_root = storage_root
        self.base_url = base_url
        self.headless = headless
        # When the engine matrix supplies a viewport, it normally
        # wins over the generic 1280x800 default (Win 11 desktop ->
        # 1920x1080, mobile -> 390x844, etc.). However, on Render
        # free-tier (0.1 CPU, 512 MB RAM) Chromium cannot render a
        # 1920x1080 viewport inside our 8-second page.screenshot()
        # ceiling — the operator's diag dump showed step=0 and
        # strip_frame_count=0 after a 62-case run because every
        # screenshot timed out. We cap headless viewport to 1280x800
        # so every screenshot fits within budget. The matrix UA
        # still goes out untouched, so SUT-side OS detection works.
        # Mobile-emulation viewports (width <= 480) stay as-is —
        # those are tiny and rendering is cheap.
        chosen = viewport_override or viewport
        if headless and chosen[0] > 1280 and chosen[1] > 800:
            self.viewport = (1280, 800)
            self._viewport_capped_from = chosen
        else:
            self.viewport = chosen
            self._viewport_capped_from = None
        self.record_video = record_video
        # Engine matrix — None means "use chromium with no UA override",
        # which preserves pre-#6 behaviour.
        self.engine_kind = (engine_kind or "chromium").strip().lower()
        if self.engine_kind not in ("chromium", "firefox", "webkit"):
            self.engine_kind = "chromium"
        self.user_agent = user_agent or ""
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
        # The previous 350 ms slow_mo was barely perceptible in a 25-fps
        # webm and operators reported videos that looked like static page
        # screenshots. 600 ms slow_mo + 250 ms step pause hits a sweet
        # spot: the cursor's CSS transition (350 ms) finishes inside the
        # slow_mo window so the video actually shows the pointer travel,
        # but we don't bloat per-case duration the way the old 500 ms
        # step_pause did. Without record_video, headless stays at full
        # speed (no audience to slow down for).
        # Operator-reported pain: 62-case run took 89 min on Render free
        # tier. Halving slow_mo + step_pause saves ~30% wall-clock
        # without making the .webm unwatchable — the operator watches
        # at their own pace anyway.
        if slow_mo_ms is not None:
            self.slow_mo_ms = slow_mo_ms
        elif not headless:
            self.slow_mo_ms = 400
        elif record_video:
            self.slow_mo_ms = 300
        else:
            self.slow_mo_ms = 0
        if step_pause_ms is not None:
            self.step_pause_ms = step_pause_ms
        elif not headless:
            self.step_pause_ms = 500
        elif record_video:
            self.step_pause_ms = 100
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
        # PR-A: project context for the locator registry. Empty when
        # the caller (legacy code path / unit tests) doesn't care.
        self.project_id = (project_id or "").strip()



    def _write_phase(self, phase: str, error: str = "") -> None:
        """Stamp the current pipeline phase into info.json so the
        /test-execution/diag endpoint can show the operator exactly
        where the runner stopped on prod. Best-effort; never raises."""
        live_dir = getattr(self, "_live_dir", "") or os.path.join(
            self.storage_root, "automation_runs", "_live")
        try:
            os.makedirs(live_dir, exist_ok=True)
            info_path = os.path.join(live_dir, "info.json")
            try:
                with open(info_path, "r", encoding="utf-8") as f:
                    import json as _j
                    payload = _j.load(f)
            except Exception:
                payload = {}
            payload["phase"] = phase
            if error:
                payload["phase_error"] = error[:300]
            else:
                payload.pop("phase_error", None)
            payload["ts"] = int(time.time() * 1000)
            tmp = info_path + ".tmp"
            import json as _j
            with open(tmp, "w", encoding="utf-8") as f:
                _j.dump(payload, f)
            os.replace(tmp, info_path)
        except Exception as exc:  # pragma: no cover
            _logger.debug("phase write failed: %s", exc)

    def run(self, scripts: list[AutomationScript]) -> RunReport:
        # First thing — pin our storage root so _write_phase can fall
        # back to it before _live_dir is set up.
        self._write_phase("runner-init")
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            self._write_phase("failed", f"playwright-import: {type(exc).__name__}: {exc}")
            raise
        self._write_phase("playwright-imported")

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
            # Wipe the filmstrip ring so we don't mix frames from the
            # previous run.
            strip_dir = os.path.join(self._live_dir, "strip")
            if os.path.isdir(strip_dir):
                for fn in os.listdir(strip_dir):
                    try:
                        os.remove(os.path.join(strip_dir, fn))
                    except OSError:
                        pass
            self._strip_slot = 0
            # Write a "warming up" splash so the live tab doesn't sit on a
            # 1×1 transparent placeholder (which renders as a black panel
            # against our dark card background) for the first ~10 s while
            # Playwright launches Chromium.
            self._write_warmup_frame(self._live_dir, len(scripts))
            # Seed every slot of the filmstrip with the warmup frame.
            # Operator reported strip_frame_count: 0 on /test-execution/diag
            # even with cases_done=6; the strip was empty because every
            # _live_pump page.screenshot was timing out on 0.1 CPU and
            # _screenshot didn't write to strip. Pre-seeding guarantees
            # the strip is non-empty as soon as the run starts, and each
            # subsequent _screenshot / _live_pump call rotates fresh
            # frames in.
            try:
                strip_dir = os.path.join(self._live_dir, "strip")
                os.makedirs(strip_dir, exist_ok=True)
                src_warmup = os.path.join(self._live_dir, "latest.png")
                if os.path.isfile(src_warmup):
                    for slot in range(12):
                        slot_path = os.path.join(strip_dir, f"{slot:02d}.png")
                        try:
                            shutil.copyfile(src_warmup, slot_path + ".tmp")
                            os.replace(slot_path + ".tmp", slot_path)
                        except Exception:
                            pass
            except Exception as exc:
                _logger.debug("strip seed: %s", exc)
            self._write_phase("live-setup")
        except OSError as exc:
            _logger.debug("live: cannot reset _live dir: %s", exc)
            self._live_dir = ""
            self._write_phase("failed", f"live-setup: {exc}")

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
            "automation: launching %s (headless=%s, slow_mo=%dms, "
            "step_pause=%dms, viewport=%dx%d%s, scripts=%d, base_url=%s, ua=%s)",
            self.engine_kind, self.headless, self.slow_mo_ms,
            self.step_pause_ms, self.viewport[0], self.viewport[1],
            (f" CAPPED from {self._viewport_capped_from[0]}x{self._viewport_capped_from[1]}"
             if getattr(self, "_viewport_capped_from", None) else ""),
            len(scripts), self.base_url or "<none>",
            (self.user_agent[:60] + "...") if len(self.user_agent) > 60 else (self.user_agent or "<default>"),
        )

        with sync_playwright() as p:
            # Pick the engine the matrix asked for. Firefox / WebKit
            # don't accept Chrome's --start-maximized / --disable-blink
            # flags, so we only pass them when launching chromium.
            engine_obj = getattr(p, self.engine_kind, p.chromium)
            engine_args = launch_args if self.engine_kind == "chromium" else []
            _logger.info("automation: about to launch %s (headless=%s, slow_mo=%dms)",
                         self.engine_kind, self.headless, self.slow_mo_ms)
            self._write_phase(f"engine-launch:{self.engine_kind}")
            try:
                browser = engine_obj.launch(
                    headless=self.headless,
                    slow_mo=self.slow_mo_ms,
                    args=engine_args,
                )
                # Publish the live browser handle so the worker's signal
                # handler can close Chromium on SIGTERM/SIGINT. Module
                # globals avoid the runner-worker importing private
                # attributes of this instance.
                globals()["_CURRENT_BROWSER"] = browser
                _logger.info("automation: %s launched OK", self.engine_kind)
                self._write_phase(f"engine-launched:{self.engine_kind}")
            except Exception as launch_exc:
                self._write_phase(
                    "failed",
                    f"engine-launch:{self.engine_kind}: "
                    f"{type(launch_exc).__name__}: {launch_exc}",
                )
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
                    # Headless retry stays on the chosen engine so a
                    # WebKit-only / Firefox-only bug still surfaces.
                    extra_args = (["--no-sandbox", "--disable-dev-shm-usage"]
                                  if self.engine_kind == "chromium" else [])
                    browser = engine_obj.launch(
                        headless=True,
                        slow_mo=0,
                        args=extra_args,
                    )
                    globals()["_CURRENT_BROWSER"] = browser
                else:
                    raise
            # 2026-05-04 — operator reported 502/503 around case 17 on a
            # 62-case run. Render free tier (512 MB RAM, 0.1 CPU) holds a
            # Chromium context for ~250 MB and creep grows ~5 MB/case.
            # 20-case rotation left no headroom for gunicorn + Postgres
            # client on the same dyno; cut to 8 so the context never
            # exceeds ~290 MB before recycle. Trade-off: 4× more cold-
            # starts on a 62-case run, but each cold-start is ~3 s vs.
            # the 30+ s of 502 stalls operators were seeing.
            BROWSER_RESTART_EVERY = 8
            try:
                # In headed mode give the user ~1 second to notice the
                # window appearing before we start firing test cases at it.
                if not self.headless:
                    time.sleep(1.0)
                import gc as _gc
                for _idx_script, script in enumerate(scripts, start=1):
                    _logger.info("automation: case %d/%d starting tc_id=%s",
                                 _idx_script, len(scripts), script.tc_id)
                    self._write_phase(
                        f"case-running:{_idx_script}/{len(scripts)}:{script.tc_id}")
                    self._live_current_tc = (script.tc_id or "TC")
                    self._live_step = 0
                    self._write_live_info(status="running")
                    sr = self._run_script(browser, script, run_dir)
                    report.scripts.append(sr)
                    if sr.status == "passed": report.passed += 1
                    elif sr.status == "failed": report.failed += 1
                    else: report.blocked += 1
                    self._live_done += 1
                    self._write_live_info(status="running")

                    # Browser-restart cycle to keep memory bounded on
                    # Render free-tier. Holding the same browser open
                    # for 62 cases blew past the 512 MB ceiling around
                    # case 29 and triggered a 502.
                    if (_idx_script % BROWSER_RESTART_EVERY == 0
                            and _idx_script < len(scripts)):
                        _logger.info(
                            "automation: rotating browser after %d cases "
                            "to release memory", _idx_script)
                        try:
                            browser.close()
                        except Exception as exc:
                            _logger.debug("browser.close on rotate: %s", exc)
                        _gc.collect()
                        engine_args2 = (launch_args
                                        if self.engine_kind == "chromium"
                                        else [])
                        browser = engine_obj.launch(
                            headless=self.headless,
                            slow_mo=self.slow_mo_ms,
                            args=engine_args2,
                        )
                        globals()["_CURRENT_BROWSER"] = browser
                        _logger.info("automation: %s relaunched OK",
                                     self.engine_kind)
                    else:
                        # Cheap reclaim between every case — temporaries
                        # from per-step screenshots add up otherwise.
                        _gc.collect()
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
                # Clear the module handle so a stray SIGTERM arriving
                # after the run completes doesn't try to .close() an
                # already-dead browser object.
                globals()["_CURRENT_BROWSER"] = None

        report.duration_ms = int((time.time() - t0) * 1000)
        report.finished_at = datetime.now().isoformat(timespec="seconds")
        self._write_live_info(status="done")
        self._write_phase("done")
        _logger.info(
            "automation: run finished cases=%d passed=%d failed=%d blocked=%d "
            "duration_ms=%d videos_attached=%d",
            report.total, report.passed, report.failed, report.blocked,
            report.duration_ms,
            sum(1 for sr in report.scripts if sr.video_path),
        )
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
        # Pin the matrix-supplied UA so the site under test sees the
        # browser+OS the tester actually picked. Without this, every
        # run on Render's Linux host advertised a Linux-Chrome UA even
        # if the operator selected macOS Safari.
        if self.user_agent:
            ctx_kwargs["user_agent"] = self.user_agent

        # Sprint 1 Task 3 — full context-lifetime try/finally.
        # Previously only the step loop (lines ~676-697) was wrapped, so
        # any crash during page setup, init-script injection, or
        # authenticate() leaked the BrowserContext. On 512 MB Render
        # dynos ~4 leaked contexts (each ~80 MB) tripped the OOM killer.
        # Now `context` is the very first thing created inside the try,
        # and `finally` always calls context.close(). `page` is sentinel-
        # initialised so we never NameError if new_page() itself raises.
        context = browser.new_context(**ctx_kwargs)
        page = None
        t_script = time.time()
        try:
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
            #
            # 2026-05-04 — operator complaint: "Live view shows only one
            # screenshot, no scrolls/fills/clicks". Root cause: the live mirror
            # IS the audience for headless+no-video runs too, but the gating
            # below skipped cursor/animation work for that exact case. Always
            # inject — CSS-only cursor + smooth-scroll cost is negligible on
            # 0.1 CPU compared to round-tripping through Playwright APIs.
            if True:
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
            # Resolve the video path BEFORE closing the context — Playwright
            # only flushes the .webm to disk once context.close() returns,
            # but page.video itself is available from new_page() onward.
            video = None
            if page is not None:
                try:
                    video = page.video
                except Exception:
                    video = None
            try:
                context.close()
            except Exception as exc:
                _logger.debug("context.close on cleanup: %s", exc)
            if video and self.record_video:
                try:
                    result.video_path = _rel_url(video.path(), self.storage_root)
                except Exception as exc:
                    _logger.debug("video path resolve: %s", exc)

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

            if step.kind == "assertion":
                # PR-C — Assertion Mode dispatch. Separate from action
                # steps so a failed expectation surfaces as ``failed``
                # with the exact assertion type in the comment, not a
                # mis-routed click() retry. All three variants raise
                # AssertionError on miss so the outer except handler
                # records the screenshot + comment exactly as it does
                # for action failures.
                atype = (step.assertion_type or "").strip().lower()
                if atype == "visible":
                    # ``_try_locator_chain`` already walks PR-A
                    # alternates and consults the registry — exact
                    # same semantics as the click/fill branches use
                    # for finding the element. The extra wait_for is
                    # the 5 s budget the plan specified for assertion
                    # visibility checks (vs the click's 2 s/cand cap).
                    loc = self._try_locator_chain(page, step)
                    self._scroll_and_highlight(page, loc)
                    target_bbox = self._safe_bbox(loc)
                    try:
                        loc.wait_for(state="visible", timeout=5000)
                    except Exception as exc:
                        raise AssertionError(
                            f"Assert visible failed for "
                            f"{step.target!r}: "
                            f"{str(exc).splitlines()[0][:200]}"
                        )
                elif atype == "text":
                    expected = (step.value or "").strip()
                    if not expected:
                        raise AssertionError(
                            "Assert text has empty expected value")
                    try:
                        page.get_by_text(expected, exact=False).first.wait_for(
                            state="visible", timeout=5000)
                    except Exception:
                        # Fall back to substring scan of the rendered
                        # content — covers cases where the text lives
                        # inside an attribute (placeholder, aria-label)
                        # that get_by_text doesn't pick up.
                        content = ""
                        try:
                            content = page.content() or ""
                        except Exception:
                            pass
                        if expected.lower() not in content.lower():
                            raise AssertionError(
                                f"Assert text not found: {expected!r}")
                elif atype == "url":
                    pattern = (step.target or step.value or "").strip()
                    if not pattern:
                        raise AssertionError(
                            "Assert URL has empty pattern")
                    # Brief settle window so an in-flight nav lands
                    # before we read page.url — matches expect_url's
                    # treatment of the same race.
                    page.wait_for_timeout(400)
                    actual = page.url or ""
                    if "*" in pattern or "?" in pattern or "[" in pattern:
                        if not fnmatch.fnmatchcase(actual, pattern):
                            raise AssertionError(
                                f"Assert URL pattern {pattern!r} "
                                f"did not match {actual!r}")
                    else:
                        if pattern.lower() not in actual.lower():
                            raise AssertionError(
                                f"Assert URL {pattern!r} "
                                f"not found in {actual!r}")
                else:
                    raise AssertionError(
                        f"Unknown assertion_type {step.assertion_type!r}")
            elif step.action == "goto":
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
                # If we're already sitting on the target URL (or its
                # trailing-slash variant), skip the goto round-trip —
                # this halves the budget on multi-case runs that all
                # exercise the same page. We still pump a live frame so
                # the operator sees progression.
                cur = ""
                try:
                    cur = (page.url or "").rstrip("/")
                except Exception:
                    cur = ""
                want = (target_url or "").rstrip("/")
                if cur and want and cur == want:
                    self._live_pump(page, tc_dir, idx, "nav_skip")
                else:
                    # SSRF guard — operator-controlled URL flowing into
                    # page.goto could otherwise hit 127.0.0.1 admin
                    # endpoints, RFC1918 internal services, or cloud
                    # metadata at 169.254.169.254. See engine.security.
                    from engine.security import require_safe_url, UnsafeUrlError
                    try:
                        require_safe_url(target_url)
                    except UnsafeUrlError as _ssrf_exc:
                        # Surfaces as a failed step with a clear
                        # comment so the operator knows what happened.
                        raise AssertionError(str(_ssrf_exc))
                    page.goto(target_url, wait_until="domcontentloaded")
                    # Intermediate live frame so the operator sees the
                    # page arrive on the live tab, not just after the
                    # whole step.
                    self._live_pump(page, tc_dir, idx, "nav")
                    # `domcontentloaded` returns as soon as the DOM is
                    # parsed — stylesheets, fonts and images may still
                    # be loading. The first per-step screenshot taken
                    # immediately after was producing white thumbnails
                    # in the gallery (operator complaint, May 2026).
                    # Wait for the `load` event with a short budget so
                    # the screenshot has actual content.
                    try:
                        page.wait_for_load_state("load", timeout=4000)
                    except Exception:
                        # `load` can take ages on heavy SPAs; the body-
                        # children check inside _screenshot is a second
                        # safety net.
                        pass
                # In headed mode perform a visible scroll so the user
                # sees page activity. Also drives the live filmstrip.
                self._visible_scroll(page)
            elif step.action == "click":
                loc = self._try_locator_chain(page, step)
                self._scroll_and_highlight(page, loc)
                target_bbox = self._safe_bbox(loc)
                # Pump a frame BEFORE the cursor moves so the live tab
                # actually shows progression on fast click-heavy steps
                # (operator complaint: "I only see one screenshot").
                self._live_pump(page, tc_dir, idx, "pre_click")
                self._move_cursor_to(page, target_bbox)
                loc.click()
            elif step.action == "fill":
                loc = self._try_locator_chain(page, step)
                self._scroll_and_highlight(page, loc)
                target_bbox = self._safe_bbox(loc)
                self._live_pump(page, tc_dir, idx, "pre_fill")
                self._move_cursor_to(page, target_bbox)
                # Clear then fill — Playwright's fill() is instant; use type() in
                # headed mode so the user sees keystrokes. When recording
                # video, also use type() with a small per-key delay so the
                # webm shows characters actually appearing in the field
                # instead of the value materialising in one frame.
                loc.fill("")
                if self.headless and not self.record_video:
                    loc.fill(step.value)
                else:
                    loc.type(step.value, delay=40)
            elif step.action == "select":
                loc = self._try_locator_chain(page, step)
                self._scroll_and_highlight(page, loc)
                target_bbox = self._safe_bbox(loc)
                self._live_pump(page, tc_dir, idx, "pre_select")
                self._move_cursor_to(page, target_bbox)
                loc.select_option(step.value)
            elif step.action == "check":
                loc = self._try_locator_chain(page, step)
                self._scroll_and_highlight(page, loc)
                target_bbox = self._safe_bbox(loc)
                self._live_pump(page, tc_dir, idx, "pre_check")
                self._move_cursor_to(page, target_bbox)
                loc.check()
            elif step.action == "expect_text":
                # Slow visual scan so the video shows the bot actually
                # looking for something — without this the .webm sits on
                # a static frame for the whole step. No-op when not
                # recording video / not headed.
                if (self.record_video or not self.headless) and step.value:
                    self._live_pump(page, tc_dir, idx, "scan_start")
                    self._visible_scan(page, step.value)
                else:
                    page.wait_for_timeout(150)
                if step.value:
                    content = page.content()
                    if step.value.lower() not in content.lower():
                        raise AssertionError(f"Expected text not found: {step.value!r}")
                    # Found — flash a green highlight ring on the first
                    # match so the video shows WHERE the assertion landed.
                    if self.record_video or not self.headless:
                        self._highlight_text(page, step.value)
                        self._live_pump(page, tc_dir, idx, "scan_done")
            elif step.action == "scroll":
                # Smooth-scroll so the webm shows the bot moving the
                # page — operators report "live view shows nothing
                # changing" when the only steps are expect_text-only.
                direction = (step.value or "down").lower()
                if direction == "up":
                    js = (
                        "async () => {"
                        "  const total = Math.max("
                        "    document.documentElement.scrollHeight, 1);"
                        "  const steps = 6;"
                        "  for (let i = steps; i >= 0; i--) {"
                        "    window.scrollTo({top: (total * i)/steps, behavior: \"smooth\"});"
                        "    await new Promise(r => setTimeout(r, 240));"
                        "  }"
                        "}"
                    )
                else:
                    js = (
                        "async () => {"
                        "  const total = Math.max("
                        "    document.documentElement.scrollHeight, 1);"
                        "  const steps = 6;"
                        "  for (let i = 1; i <= steps; i++) {"
                        "    window.scrollTo({top: (total * i)/steps, behavior: \"smooth\"});"
                        "    await new Promise(r => setTimeout(r, 240));"
                        "  }"
                        "}"
                    )
                try:
                    page.evaluate(js)
                    self._live_pump(page, tc_dir, idx, f"scroll_{direction}")
                except Exception as exc:
                    _logger.debug("scroll: %s", exc)
            elif step.action == "expect_url":
                # Wait briefly for any in-flight nav, then assert the
                # current URL matches what the TC expected. Substring
                # match so the TC can write "https://example.com/contact"
                # and pass even if the SUT appends a query string.
                page.wait_for_timeout(400)
                expected = (step.target or step.value or "").strip()
                actual = page.url or ""
                if expected and expected.lower() not in actual.lower():
                    raise AssertionError(
                        f"Expected URL to contain {expected!r}, "
                        f"got {actual!r}"
                    )
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
            # Always write a CLEAN per-step "after" screenshot — this is
            # what the post-run gallery shows. Operators reported seeing
            # red error banners on screenshots next to a Passed status,
            # because we used to annotate the after_path in place. Now
            # the annotated copy goes to a separate failure_path used
            # only by bug-report attachments; the gallery stays clean.
            failure_path = os.path.join(tc_dir, f"step_{idx:02d}_failure.png")
            self._screenshot(page, after_path)
            sr.screenshot_after = _rel_url(after_path, self.storage_root)
            try:
                shutil.copyfile(after_path, failure_path)
                self._annotate_failure(
                    failure_path, target_bbox, sr.comment,
                    header=f"Step {idx} ({_step_label(step)})")
                sr.screenshot_failure = _rel_url(failure_path, self.storage_root)
            except Exception as ann_exc:
                _logger.debug("annotate_failure copy: %s", ann_exc)
        except Exception as e:
            msg = str(e)
            if "Timeout" in msg or "timeout" in msg:
                sr.status = "blocked"
                sr.comment = f"Timeout/selector issue: {msg.splitlines()[0][:200]}"
            else:
                sr.status = "failed"
                sr.comment = f"{type(e).__name__}: {msg.splitlines()[0][:200]}"
            try:
                failure_path = os.path.join(tc_dir, f"step_{idx:02d}_failure.png")
                self._screenshot(page, after_path)
                sr.screenshot_after = _rel_url(after_path, self.storage_root)
                try:
                    shutil.copyfile(after_path, failure_path)
                    self._annotate_failure(
                        failure_path, target_bbox, sr.comment,
                        header=f"Step {idx} ({_step_label(step)})")
                    sr.screenshot_failure = _rel_url(failure_path, self.storage_root)
                except Exception as ann_exc:
                    _logger.debug("annotate_failure copy: %s", ann_exc)
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
        # Throttle was 400 ms; dropped to 200 ms after the operator
        # complained that fast click-only steps showed only one frame
        # in the live tab. With single-flight polling on the client
        # (1 s interval) the disk IO cost is negligible.
        if now - last < 0.2:
            return
        self._live_last_pump = now
        # 2026-05-04 follow-up: previous iteration wrote JPEG bytes to
        # files with a .png extension and served them with
        # mimetype="image/png". Modern Chrome with strict-MIME rejects
        # that combination — every filmstrip slot rendered as a
        # broken-image icon (operator-reported). Reverted to PNG so
        # bytes match extension and MIME. The per-pump CPU cost is a
        # bit higher on 0.1 CPU but the alternative is a useless live
        # view, which defeats the whole feature.
        tmp_shot = os.path.join(tc_dir, f"_live_{idx:02d}_{label}.png")
        captured = False
        try:
            # 4 s budget per pump. If we miss it, fall through to the
            # mirror-only path so the strip stays live from
            # _screenshot's per-step writes (which use a 15 s budget).
            page.screenshot(path=tmp_shot, full_page=False, timeout=4000)
            captured = True
        except Exception as exc:
            # Don't bail — we still want to refresh the live frame from
            # the most recent on-disk shot so the operator's filmstrip
            # keeps moving. _screenshot's strip writes will fill the gap.
            _logger.debug("live pump screenshot (%s) timed out: %s", label, exc)
        try:
            os.makedirs(live_dir, exist_ok=True)
            dst = os.path.join(live_dir, "latest.png")
            tmp = dst + ".tmp"
            if captured and os.path.isfile(tmp_shot) and os.path.getsize(tmp_shot) > 0:
                shutil.copyfile(tmp_shot, tmp)
                os.replace(tmp, dst)
                # Strip ring buffer — only update on a successful new
                # capture so we don't write the same frame twice in a row.
                try:
                    strip_dir = os.path.join(live_dir, "strip")
                    os.makedirs(strip_dir, exist_ok=True)
                    slot = getattr(self, "_strip_slot", 0) % 12
                    self._strip_slot = slot + 1
                    strip_path = os.path.join(strip_dir, f"{slot:02d}.png")
                    shutil.copyfile(dst, strip_path + ".tmp")
                    os.replace(strip_path + ".tmp", strip_path)
                except Exception:
                    pass
            try:
                if os.path.isfile(tmp_shot):
                    os.remove(tmp_shot)
            except OSError:
                pass
            self._write_live_info(status="running")
        except Exception as exc:
            # Surface at debug — pumps are best-effort and noisy
            # failures here would drown the log on a normal run.
            _logger.debug("live pump (%s) failed: %s", label, exc)

    def _move_cursor_to(self, page, bbox: dict | None) -> None:
        """Move the cursor toward the target so the recorded video shows
        a pointer travelling between actions. Two parallel mechanisms,
        because Playwright's headless video records ONE of them
        depending on the underlying compositor:
          1) page.mouse.move(x, y, steps=12) — the real CDP mouse, which
             Playwright's video recorder captures.
          2) window.__tfMoveCursor — the injected DOM cursor as fallback
             when CDP cursor isn't rendered.
        Active for the /test-execution/live mirror audience too — the
        injected DOM cursor renders into latest.png even on headless
        runs without video. No-op when bbox is unknown."""
        if not bbox:
            return
        try:
            cx = int(bbox["x"] + bbox.get("width", 0) / 2)
            cy = int(bbox["y"] + bbox.get("height", 0) / 2)
            # Real Playwright mouse — recorded by Playwright video.
            try:
                page.mouse.move(cx, cy, steps=12)
            except Exception as exc:
                _logger.debug("playwright mouse move: %s", exc)
            # DOM cursor fallback for live screenshots / cases where the
            # video recorder loses the CDP cursor sprite.
            try:
                page.evaluate(
                    "(args) => window.__tfMoveCursor && window.__tfMoveCursor(args.x, args.y)",
                    {"x": cx, "y": cy},
                )
            except Exception:
                pass
            # Brief pause so the video has time to capture the moved pointer.
            page.wait_for_timeout(250)
        except Exception as exc:
            _logger.debug("cursor move: %s", exc)

    def _try_locator_chain(self, page, step: AutomationStep):
        """PR-A multi-locator fallback.

        Walks ``[step.target, *step.target_alternates]`` and returns the
        first Locator that resolves to a visible element. Pre-PR-A
        steps (text-authored TCs without alternates) bypass the chain
        and hand back the legacy single-target locator, preserving
        byte-identical timing for every TC that the recorder hasn't
        touched.

        When ``self.project_id`` AND ``step.locator_label`` are both
        non-empty, the registry promotes the previously-winning strategy
        to the front of the chain and learns from this run via
        ``record_success`` / ``record_failure``. No-op otherwise — keeps
        the registry empty for noise-prone text-parse steps.
        """
        primary = (step.target or "").strip()
        if not primary:
            raise AssertionError(f"empty target for step: {step.raw or step.action}")

        targets = [primary]
        for alt in step.target_alternates or []:
            alt_s = (alt or "").strip()
            if alt_s and alt_s not in targets:
                targets.append(alt_s)

        pid = self.project_id
        label = (getattr(step, "locator_label", "") or "").strip()

        # Registry consultation — only meaningful when both ends are wired.
        # Capped at MAX_CHAIN_TARGETS so a label with five strategies
        # registered doesn't blow past the per-step budget on a missing
        # element (per_cand_ms × N).
        if pid and label:
            try:
                promoted = _best_alternates(pid, label, defaults=targets)
                if promoted:
                    targets = promoted[:MAX_CHAIN_TARGETS]
            except Exception as exc:
                _logger.debug("locator_registry best_alternates: %s", exc)

        # Single-target fast path: no alternates → return the locator
        # without an explicit wait_for so the downstream click()/fill()
        # action's auto-wait drives timing exactly as it did pre-PR-A.
        if len(targets) == 1:
            return _locator(page, targets[0]).first

        # Per-candidate visibility budget. Bumped to 2000ms per the
        # plan; the cap divided by 2 is the runner's default timeout
        # so a 4-candidate chain stays inside one default_timeout
        # window worst-case.
        per_cand_ms = max(500, min(2000, self.default_timeout_ms // 2))
        tried: list[str] = []
        for target in targets:
            try:
                loc = _locator(page, target).first
                loc.wait_for(state="visible", timeout=per_cand_ms)
            except Exception as exc:
                # Only locator-drift signals (timeout / not visible) get
                # treated as "try next candidate". Anything else — page
                # crash, websocket disconnect, browser closed — must
                # surface immediately instead of being silently turned
                # into 4 × per_cand_ms of dead time and an
                # AssertionError that hides the real root cause.
                if not _is_locator_drift(exc):
                    raise
                tried.append(target)
                continue
            if pid and label:
                try:
                    _record_success(pid, label, target)
                except Exception as exc:
                    _logger.debug("locator_registry record_success: %s", exc)
            return loc

        if pid and label:
            try:
                _record_failure(pid, label)
            except Exception as exc:
                _logger.debug("locator_registry record_failure: %s", exc)
        raise AssertionError(
            f"All locators failed for step: {step.raw or step.action}\n"
            f"Tried: {tried}"
        )

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

    def _annotate_failure(self, image_path: str, bbox: dict | None, comment: str,
                          header: str = "") -> None:
        """Mark a failure screenshot so the bug location is unmistakable.

        When bbox is known: red rectangle around the element, red arrow
        from the upper-left corner pointing at it, compact red sticker
        with the failure reason next to the arrow tail.

        When bbox is None (nav timeout, expect_text, etc): instead of a
        small corner sticker, draw a full-width red banner across the
        top of the screenshot with the failure header + reason. The old
        16×16 sticker was the root of "screenshot doesn't show where
        the bug is" — reviewers literally couldn't see it on thumbnails.

        ``header`` is an optional one-line caption like
        ``"Step 3 (expect_text)"`` shown in bold above the comment.
        Best-effort — never raises.
        """
        try:
            from PIL import Image, ImageDraw, ImageFont
        except Exception as exc:
            _logger.warning("annotate: Pillow not available: %s", exc)
            return
        if not os.path.isfile(image_path):
            _logger.warning("annotate: source image missing: %s", image_path)
            return
        try:
            with Image.open(image_path) as img:
                img = img.convert("RGBA")
                draw = ImageDraw.Draw(img)
                width_px, height_px = img.size

                # Pick the right font once for both branches.
                try:
                    font = ImageFont.truetype("DejaVuSans-Bold.ttf", 14)
                except Exception:
                    font = ImageFont.load_default()
                caption = (comment or "Bug here").splitlines()[0][:140]

                # Bigger fonts than before — operators reported the old
                # 14-px sticker was illegible on thumbnail-size galleries.
                try:
                    font = ImageFont.truetype("DejaVuSans-Bold.ttf", 18)
                except Exception:
                    font = ImageFont.load_default()
                try:
                    font_small = ImageFont.truetype("DejaVuSans-Bold.ttf", 14)
                except Exception:
                    font_small = font

                hdr = (header or "").splitlines()[0][:80] if header else ""

                if bbox:
                    # Most viewports render at scale 1, but high-DPI
                    # screens report a 2× framebuffer. Compare bbox to
                    # image dims and pick the plausible scale.
                    scale = 1.0
                    if bbox.get("x", 0) + bbox.get("width", 0) > width_px:
                        scale = width_px / max(1, bbox.get("x", 0) + bbox.get("width", 0))
                    x1 = max(0, int(bbox["x"] * scale) - 4)
                    y1 = max(0, int(bbox["y"] * scale) - 4)
                    x2 = min(width_px - 1, int((bbox["x"] + bbox["width"]) * scale) + 4)
                    y2 = min(height_px - 1, int((bbox["y"] + bbox["height"]) * scale) + 4)
                    # Heavy red border (5 px) around the element so it
                    # stays visible on thumbnail-sized previews.
                    for offset in range(5):
                        draw.rectangle(
                            (x1 - offset, y1 - offset, x2 + offset, y2 + offset),
                            outline=(220, 38, 38, 255),
                        )
                    # Red arrow from upper-left margin pointing AT the box
                    arrow_tail = (max(20, x1 - 90), max(60, y1 - 60))
                    arrow_head = (x1, y1)
                    draw.line([arrow_tail, arrow_head],
                              fill=(220, 38, 38, 255), width=5)
                    import math
                    angle = math.atan2(arrow_head[1] - arrow_tail[1],
                                       arrow_head[0] - arrow_tail[0])
                    head_len = 22
                    head_angle = math.radians(26)
                    p1 = (arrow_head[0] - head_len * math.cos(angle - head_angle),
                          arrow_head[1] - head_len * math.sin(angle - head_angle))
                    p2 = (arrow_head[0] - head_len * math.cos(angle + head_angle),
                          arrow_head[1] - head_len * math.sin(angle + head_angle))
                    draw.polygon([arrow_head, p1, p2], fill=(220, 38, 38, 255))
                    sticker_anchor = arrow_tail

                    # Sticker near the arrow tail (bbox path).
                    try:
                        bb_h = draw.textbbox((0, 0), hdr, font=font) if hdr else (0, 0, 0, 0)
                        bb_c = draw.textbbox((0, 0), caption, font=font)
                        cap_w = max(bb_h[2] - bb_h[0], bb_c[2] - bb_c[0])
                        cap_h = (bb_h[3] - bb_h[1]) + (bb_c[3] - bb_c[1]) + (4 if hdr else 0)
                    except Exception:
                        cap_w = len(caption) * 11
                        cap_h = 24 + (18 if hdr else 0)
                    pad = 10
                    sticker_w = cap_w + pad * 2
                    sticker_h = cap_h + pad * 2
                    sx = max(8, min(width_px - sticker_w - 8,
                                    sticker_anchor[0] - sticker_w // 2))
                    sy = max(8, sticker_anchor[1] - sticker_h - 6)
                    draw.rectangle((sx, sy, sx + sticker_w, sy + sticker_h),
                                   fill=(220, 38, 38, 240))
                    text_y = sy + pad
                    if hdr:
                        draw.text((sx + pad, text_y), hdr,
                                  fill=(255, 255, 255, 255), font=font)
                        try:
                            text_y += (draw.textbbox((0, 0), hdr, font=font)[3]
                                       - draw.textbbox((0, 0), hdr, font=font)[1]) + 4
                        except Exception:
                            text_y += 22
                    draw.text((sx + pad, text_y), caption,
                              fill=(255, 255, 255, 255), font=font)
                else:
                    # No element to box — paint a full-width banner across
                    # the top so the failure reason is impossible to miss
                    # even on a thumbnail. This was the original "tiny
                    # 16×16 sticker in the corner" complaint.
                    banner_h = 64 if hdr else 44
                    draw.rectangle((0, 0, width_px, banner_h),
                                   fill=(220, 38, 38, 240))
                    text_y = 8
                    if hdr:
                        draw.text((16, text_y), hdr,
                                  fill=(255, 255, 255, 255), font=font)
                        text_y += 26
                    draw.text((16, text_y), caption,
                              fill=(255, 255, 255, 255), font=font_small)

                img.convert("RGB").save(image_path, "PNG", optimize=True)
                _logger.info("annotated bug screenshot: %s (bbox=%s)",
                             image_path, bool(bbox))
        except Exception as exc:
            _logger.warning("annotate failure: %s", exc)

    def _screenshot(self, page, path: str) -> None:
        """Take a step screenshot and mirror it to <live>/latest.png.

        Order of operations matters — the previous version snapped
        DIRECTLY to the live path first, and when that single
        Playwright call timed out (WebKit / Firefox under Render's
        0.1 CPU consistently miss a 3 s budget) the live tab stayed
        on the "Warming up Chromium…" splash forever even though the
        run itself was happily progressing through cases. The fix:
        take the per-step PNG first (the gallery artefact must
        always exist), then mirror to the live path as a cheap
        file copy. Live update degrades gracefully — the operator
        sees real frames even if a single Playwright call hiccups.
        """
        # 1) Per-step screenshot — the gallery + bug-screenshot
        # pipeline both depend on this file. 8 s timeout (was 3 s)
        # so non-Chromium engines under CPU pressure on Render
        # free-tier still land a frame.
        #
        # Operator complaint (2026-05-04): "first thumbnail of some TCs
        # is empty white". Root cause: page.screenshot can fire BEFORE
        # the page has any rendered content — `wait_until=
        # "domcontentloaded"` in goto returns as soon as the DOM is
        # parsed, which is often before any stylesheets/images/JS-driven
        # content has painted. Wait for the body to actually have
        # children + non-trivial scrollHeight first; cheap and almost
        # always already true by the time we reach this point.
        try:
            page.wait_for_function(
                "() => document && document.body "
                "&& document.body.children.length > 0 "
                "&& document.body.scrollHeight > 80",
                timeout=2500,
            )
        except Exception:
            # Don't bail — some pages legitimately have a near-empty
            # body (login walls, 204s, etc.). Just take the shot.
            pass
        try:
            # 15-second timeout — Render free-tier (0.1 CPU)
            # consistently breached the previous 8-second ceiling.
            page.screenshot(path=path,
                            full_page=self.screenshot_full_page,
                            timeout=15000)
        except Exception as exc:
            # Loud — operator needs to see this in Render INFO logs
            # if every step is missing a screenshot.
            _logger.error("step screenshot failed (%s): %s",
                          type(exc).__name__, exc)
            return

        # Blank-frame guard. If the result is mostly white (>97% of
        # sampled pixels), we likely caught the page mid-paint. Wait
        # 600 ms and retake exactly once. Best-effort: PIL missing →
        # skip the check.
        try:
            from PIL import Image as _PIL  # noqa: F401
            if _is_mostly_blank(path):
                _logger.info("blank screenshot detected, retrying once: %s", path)
                page.wait_for_timeout(600)
                try:
                    page.screenshot(path=path,
                                    full_page=self.screenshot_full_page,
                                    timeout=15000)
                except Exception as retry_exc:
                    _logger.debug("blank retry failed: %s", retry_exc)
        except Exception as guard_exc:
            _logger.debug("blank guard skipped: %s", guard_exc)

        # 2) Mirror to <live>/latest.png — pure filesystem copy, no
        # Playwright API surface, so this almost never fails. The
        # write is atomic (.tmp + os.replace) so the live route never
        # serves a half-written PNG.
        live_dir = getattr(self, "_live_dir", "")
        if not live_dir:
            return
        try:
            os.makedirs(live_dir, exist_ok=True)
            live_path = os.path.join(live_dir, "latest.png")
            tmp = live_path + ".tmp"
            shutil.copyfile(path, tmp)
            os.replace(tmp, live_path)
            self._live_step += 1
            if self._live_step == 1:
                try:
                    sz = os.path.getsize(live_path)
                except OSError:
                    sz = -1
                _logger.info("live mirror: first frame written to %s (%d bytes)",
                             live_path, sz)
            # 2026-05-04 — operator reported strip_frame_count=0 after
            # 6 cases done. Root cause: _live_pump was the ONLY writer to
            # the strip ring, and on Render's 0.1 CPU pump screenshots
            # routinely time out. Make the per-step "after" shot ALSO
            # populate the strip — this is a pure filesystem copy so it
            # cannot fail for the same reason. Now the filmstrip always
            # shows the most recent N "after" frames even when pumps
            # never land.
            try:
                strip_dir = os.path.join(live_dir, "strip")
                os.makedirs(strip_dir, exist_ok=True)
                slot = getattr(self, "_strip_slot", 0) % 12
                self._strip_slot = slot + 1
                strip_path = os.path.join(strip_dir, f"{slot:02d}.png")
                shutil.copyfile(live_path, strip_path + ".tmp")
                os.replace(strip_path + ".tmp", strip_path)
            except Exception as strip_exc:
                _logger.debug("strip mirror from _screenshot: %s", strip_exc)
            self._write_live_info(status="running")
        except Exception as exc:  # pragma: no cover — IO best-effort
            _logger.error("live mirror failed (%s): %s",
                          type(exc).__name__, exc)

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
        except Exception as exc:
            _logger.error("warmup frame: PIL not available — live splash "
                          "will be the 1px placeholder (%s)", exc)
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
            try:
                _logger.info("warmup frame: wrote %s (%d bytes)",
                             dst, os.path.getsize(dst))
            except OSError:
                pass
        except Exception as exc:
            _logger.error("warmup frame failed: %s", exc)

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
        # The /test-execution/live mirror is the audience for headless
        # runs without video too — without the highlight + 180 ms pause
        # the operator's filmstrip just shows static "after" frames. The
        # CSS evaluate() call costs <5 ms; cheap enough to always run.
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
            # SSRF guard — login_url from operator config could point at
            # an internal service. Mark the auth step "blocked" rather
            # than raising so the run continues with a clear reason.
            from engine.security import require_safe_url, UnsafeUrlError
            try:
                require_safe_url(login_target)
            except UnsafeUrlError as _ssrf_exc:
                sr.status = "blocked"
                sr.comment = (
                    f"Login URL blocked by SSRF policy: {str(_ssrf_exc)[:200]}"
                )
                sr.duration_ms = int((time.time() - t0) * 1000)
                return sr
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
            # SSRF guard — best-effort, just log and bail if blocked.
            from engine.security import require_safe_url, UnsafeUrlError
            try:
                require_safe_url(cred.register_url)
            except UnsafeUrlError as _ssrf_exc:
                _logger.warning("register skipped — %s", _ssrf_exc)
                return
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
            return page.locator("input[type='password']").first
        return page.locator("input[type='text'], input[type='email']").first

    def _visible_scan(self, page, needle: str) -> None:
        """Scroll the page top-to-bottom slowly so the recorded video
        shows the bot actually scanning the document for the expected
        text. Each step is ~250 ms with a 200 px delta — slow enough to
        register on a 25 fps webm. Best-effort; never raises."""
        try:
            page.evaluate(
                "async (q) => {"
                "  const total = Math.max(document.documentElement.scrollHeight, 1);"
                "  const steps = Math.min(8, Math.max(3, Math.ceil(total / 600)));"
                "  for (let i = 1; i <= steps; i++) {"
                "    window.scrollTo({top: (total * i)/steps, behavior:'smooth'});"
                "    await new Promise(r => setTimeout(r, 280));"
                "  }"
                "  window.scrollTo({top: 0, behavior:'smooth'});"
                "  await new Promise(r => setTimeout(r, 220));"
                "}",
                needle,
            )
        except Exception as exc:
            _logger.debug("visible scan: %s", exc)

    def _highlight_text(self, page, needle: str) -> None:
        """Outline the first DOM node whose text contains the expected
        string with a green dashed ring, scroll it into view, and pause
        ~600 ms so the video clearly shows where the assertion succeeded.
        Best-effort; never raises."""
        try:
            page.evaluate(
                "(q) => {"
                "  const target = q && q.toString().toLowerCase();"
                "  if (!target) return;"
                "  const walker = document.createTreeWalker("
                "    document.body, NodeFilter.SHOW_TEXT);"
                "  let node;"
                "  while ((node = walker.nextNode())) {"
                "    if (node.nodeValue && node.nodeValue.toLowerCase().includes(target)) {"
                "      const el = node.parentElement;"
                "      if (!el) continue;"
                "      el.scrollIntoView({block:'center', behavior:'smooth'});"
                "      const prevOutline = el.style.outline;"
                "      const prevBox = el.style.boxShadow;"
                "      el.style.outline = '3px dashed #16a34a';"
                "      el.style.boxShadow = '0 0 0 6px rgba(22,163,74,0.25)';"
                "      setTimeout(() => {"
                "        el.style.outline = prevOutline;"
                "        el.style.boxShadow = prevBox;"
                "      }, 1200);"
                "      break;"
                "    }"
                "  }"
                "}",
                needle,
            )
            page.wait_for_timeout(600)
        except Exception as exc:
            _logger.debug("highlight text: %s", exc)

    def _visible_scroll(self, page) -> None:
        """After a navigation, do a short visible scroll so the live
        mirror, headed-mode audience, or .webm video all see the page
        actually scrolling. Was previously gated to non-headless or
        record_video, but the /test-execution/live mirror is itself a
        live audience even on plain headless runs — without this scroll
        the filmstrip shows three identical "page rendered" frames.
        Cost: ~1 s wall-clock, well below the per-case budget.
        """
        try:
            # Smooth-scroll down 3 ticks then back to top — visible in
            # both the recorded video and the live filmstrip because
            # each screenshot fires between ticks at 250 ms apart.
            page.evaluate(
                "async () => {"
                "  const total = Math.max("
                "    document.documentElement.scrollHeight, 1);"
                "  for (let i = 1; i <= 3; i++) {"
                "    window.scrollTo({top: (total*0.18)*i, behavior:'smooth'});"
                "    await new Promise(r => setTimeout(r, 250));"
                "  }"
                "  window.scrollTo({top: 0, behavior:'smooth'});"
                "}"
            )
            page.wait_for_timeout(300)
        except Exception as exc:
            _logger.debug("visible_scroll: %s", exc)
