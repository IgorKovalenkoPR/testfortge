"""TestForTge — Stage 3 Live Test Execution Agent.

The live executor is the single Playwright runner the platform uses for
``/test-execution`` after Stage 3 lands. It unifies the two pre-Stage-3
code paths:

* **TC-driven** (:class:`engine.automation_runner.AutomationRunner`) —
  good at running a fixed list of TestCases through Playwright, but
  blind to the rest of the site. Steps are executed in the order the
  generator produced them; if the operator wrote ``trigger="manual"``
  with no ``url_pattern`` the case fires once on ``base_url`` and is
  done.
* **Walkthrough** (:class:`engine.walkthrough_runner.WalkthroughRunner`)
  — runs a heuristic battery (broken images, hamburger, search probe,
  form auto-fill, CTA audit, axe-core, console errors) across a small
  set of URLs but does NOT execute project TestCases.

Live executor combines both:

1. Builds a coverage plan from ``SiteProfile.key_pages`` (Stage 2's
   recon output) **plus** Playwright-discovered links on each visited
   page. The plan covers ALL pages reachable from the seeds, capped
   at ``max_pages``.
2. On each page, runs the heuristic battery (imported from the
   refactored :mod:`engine.walkthrough_runner` module-level helpers).
3. Per page, matches the project's TestCases via ``fnmatch``-style
   ``url_pattern`` (the field added in Sprint 5) and executes the
   matching cases through the existing :class:`AutomationRunner`'s
   ``_run_script`` pipeline.
   * ``trigger="always"``  — runs on every visited URL
   * ``trigger="walkthrough_url_match"`` — runs only where pattern
     matches the URL
   * ``trigger="manual"``   — never runs from the walkthrough (only
     fires from the existing user-button surface)
4. An :class:`OomGuard` polls ``psutil.Process().memory_info().rss``
   once per TC; if the RSS crosses ``MEMORY_BUDGET_MB`` (default 400)
   the executor writes a ``status="oom_exit"`` partial result and
   shuts down the browser cleanly. The 512 MB Render free-tier
   ceiling makes this the difference between "graceful early exit"
   and "SIGKILL with a half-written result.json".

Live-feed contract
------------------
The executor writes the same ``_live/info.json`` + ``_live/latest.png``
+ ``_live/strip/<NN>.png`` ring the existing
``/test-execution/live`` UI polls — :file:`templates/test_execution_live.html`
does not change. Each TC step touches ``info.json.ts`` so the stall
detector (120 s timeout on no ``ts`` movement) keeps working.

Backward-compat
---------------
The legacy TC-driven and walkthrough dispatch paths remain in
:mod:`engine.runner_worker` behind the ``LEGACY_EXECUTOR=1`` env var.
With the flag unset (the new default), every ``mode`` value resolves
to ``"live"`` so a stray ``mode="walkthrough"`` in someone's saved
config picks up the unified executor automatically.
"""
from __future__ import annotations

import gc
import json
import os
import shutil
import time
import uuid
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

from engine.automation_qa import AutomationScript, tc_to_script
from engine.automation_runner import (
    AUTOMATION_RUN_MAX_KEPT,
    AUTOMATION_RUN_RETENTION_DAYS,
    AutomationRunner,
    RunReport,
    ScriptResult,
    StepResult,
    _purge_old_automation_runs,
)
from engine.bug_template import CLASS_SEVERITY  # noqa: F401 — referenced via walkthrough_runner helpers
from engine.log import get_logger
from engine.walkthrough_dedup import dedupe as _dedupe_findings
from engine.walkthrough_runner import (
    _SEV_CRITICAL,
    _SEV_MAJOR,
    _SEV_MINOR,
    collect_console_errors,
    install_error_listeners,
    scan_axe,
    scan_broken_images,
    scan_ctas,
    scan_footer_social,
    scan_forms,
    scan_navigation_menu,
    scan_search_field,
)
from engine.walkthrough_tc_match import match_tcs_for_url

_logger = get_logger(__name__)


# ── Defaults ───────────────────────────────────────────────────────

# Render free tier OOM-kills at ~480 MB resident (512 MB ceiling minus
# kernel + Chromium tail). 400 leaves a 80 MB cushion for the partial-
# result flush. Operator can override via the ``memory_budget_mb`` ctor
# arg OR the ``MEMORY_BUDGET_MB`` env var.
DEFAULT_MEMORY_BUDGET_MB = int(os.environ.get("MEMORY_BUDGET_MB", "400"))

# Per-page link discovery cap. Higher means more thorough coverage but
# more wall-clock; 20 matches what a human walkthrough would notice in
# a few seconds of clicking around.
DEFAULT_LINKS_PER_PAGE = 20

# Per-step heartbeat cadence. The /test-execution/live UI's stall
# detector treats info.json.ts not moving for >120 s as a hang; bumping
# every step keeps us well under that even on slow networks.
HEARTBEAT_STEP_S = 1.0

# Live-frame paint settle. ``page.goto(wait_until="domcontentloaded")``
# returns before CSS / images / fonts render, so a screenshot taken
# immediately is often blank or all-black (worst on heavy or
# dark-themed SPAs — exactly what made the live view look "broken").
# Before the first live frame we wait for the ``load`` event (capped so
# a never-loading page can't stall the walk) plus a short fixed settle.
# Both waits are best-effort: a page that never fires ``load`` still
# gets captured after LIVE_PAINT_MIN_MS.
LIVE_PAINT_LOAD_CAP_MS = int(os.environ.get("LIVE_PAINT_LOAD_CAP_MS", "8000"))
LIVE_PAINT_MIN_MS = int(os.environ.get("LIVE_PAINT_MIN_MS", "700"))

# JS used to harvest internal links on a page. We keep it tight: only
# anchor href values that resolve onto the same registrable domain,
# trimmed and dedup'd. Returns an array of absolute URLs so callers
# don't need to know the page origin.
_JS_DISCOVER_LINKS = """
() => {
    const links = new Set();
    const here = location.origin + location.pathname.split('?')[0].split('#')[0];
    document.querySelectorAll('a[href]').forEach(a => {
        try {
            const u = new URL(a.getAttribute('href'), document.baseURI);
            if (!u.protocol.startsWith('http')) return;
            if (u.origin !== location.origin) return;
            // Drop fragments and tracking-only query strings.
            u.hash = '';
            const clean = u.toString();
            if (clean === here) return;
            links.add(clean);
        } catch (e) { /* malformed href, ignore */ }
    });
    return Array.from(links).slice(0, 100);
}
"""


# ── OOM guard ──────────────────────────────────────────────────────

class OomGuard:
    """RSS-budget guard polled once per TC.

    ``psutil`` is imported lazily so live_executor can still be
    imported on environments that don't ship it (CI containers, test
    fixtures). With psutil missing the guard becomes a no-op: every
    :meth:`over_budget` call returns ``False``, every :meth:`rss_mb`
    returns ``0``, and the executor runs without an early-exit safety
    net (matches pre-Stage-3 behaviour).

    The guard reads RSS on demand — no background thread, no shared
    state. Acceptable because each call is ~50 µs on Linux / Windows
    and we only poll once per TC, not per step.
    """

    def __init__(self, budget_mb: int | None = None):
        budget = (budget_mb if budget_mb is not None
                  else DEFAULT_MEMORY_BUDGET_MB)
        self.budget_mb = max(0, int(budget))
        self.budget_bytes = self.budget_mb * 1024 * 1024
        self._psutil: Any = None
        try:
            import psutil as _ps
            self._psutil = _ps
        except ImportError:
            _logger.info("OomGuard: psutil not installed — guard is a no-op")

    @property
    def active(self) -> bool:
        return self._psutil is not None and self.budget_mb > 0

    def rss_mb(self) -> int:
        if not self._psutil:
            return 0
        try:
            return int(self._psutil.Process().memory_info().rss
                       / (1024 * 1024))
        except Exception:
            return 0

    def over_budget(self) -> bool:
        """``True`` when current RSS exceeds the configured budget."""
        if not self.active:
            return False
        try:
            rss = self._psutil.Process().memory_info().rss
            return rss > self.budget_bytes
        except Exception:
            # Permission denied on locked-down dynos / Windows AppContainers.
            # Treat as "guard inactive for this sample" rather than failing
            # the run — over-budget is the rare path, denying access is
            # benign.
            return False


# ── Helpers ────────────────────────────────────────────────────────

def _device_kind(viewport: tuple[int, int]) -> str:
    """``mobile`` / ``tablet`` / ``desktop`` derived from viewport
    width. Mirrors :prop:`WalkthroughRunner.device_kind`."""
    w = viewport[0]
    if w <= 480:
        return "mobile"
    if w <= 1024:
        return "tablet"
    return "desktop"


def discover_links_on_page(page, base_url: str, *,
                            limit: int = DEFAULT_LINKS_PER_PAGE,
                            ) -> list[str]:
    """Return up to ``limit`` internal links found on ``page``.

    Filters by the registrable domain of ``base_url`` so a stray
    redirect to a CDN subdomain doesn't bleed into the walk. Failure
    is silent — empty list rather than raise — so a Playwright stub
    that doesn't implement ``evaluate`` (unit tests) still works.
    """
    try:
        raw = page.evaluate(_JS_DISCOVER_LINKS) or []
    except Exception:
        return []
    try:
        base_host = urlparse(base_url).hostname or ""
    except Exception:
        base_host = ""
    out: list[str] = []
    for u in raw:
        try:
            host = urlparse(u).hostname or ""
        except Exception:
            continue
        if base_host and host != base_host:
            continue
        out.append(u)
        if len(out) >= limit:
            break
    return out


def _key_pages_from_profile(profile: dict | None,
                             base_url: str) -> list[str]:
    """Pull URLs from a stored ``SiteProfile.key_pages`` payload.

    The profile is stored as JSON in the DB; ``key_pages`` is a list
    of ``{url, role}`` dicts. ``base_url`` is appended so the seed set
    always includes the page the operator typed into the UI even when
    Stage 2 didn't surface it as a key page.
    """
    seed: list[str] = []
    if isinstance(profile, dict):
        for entry in (profile.get("key_pages") or []):
            if isinstance(entry, dict):
                u = (entry.get("url") or "").strip()
                if u:
                    seed.append(u)
            elif isinstance(entry, str) and entry.strip():
                seed.append(entry.strip())
    if base_url and base_url not in seed:
        seed.append(base_url)
    return seed


# ── Live executor ─────────────────────────────────────────────────

class LiveExecutor:
    """Unified TC-driven + walkthrough Playwright runner (Stage 3).

    Constructor accepts the union of :class:`AutomationRunner` and
    :class:`WalkthroughRunner` knobs so the dispatch layer in
    :mod:`engine.runner_worker` can pass a single ``runner_kwargs`` dict
    without per-runner key-list maintenance. Unknown kwargs are silently
    ignored.
    """

    def __init__(
        self,
        storage_root: str,
        base_url: str,
        *,
        project_id: str = "",
        headless: bool = True,
        viewport: tuple[int, int] = (1280, 800),
        navigation_timeout_ms: int = 45000,
        device_timeout_ms: int = 480000,
        max_pages: int = 50,
        max_form_fills: int = 5,
        axe_enabled: bool = True,
        record_video: bool = False,
        test_cases: list[dict[str, Any]] | None = None,
        credentials: Any = None,
        site_profile: dict | None = None,
        memory_budget_mb: int | None = None,
        engine_kind: str = "chromium",
        user_agent: str = "",
        **_ignored: Any,
    ):
        self.storage_root = storage_root
        self.base_url = (base_url or "").strip()
        self.project_id = (project_id or "").strip()
        self.headless = bool(headless)
        # Same Render-free-tier cap as WalkthroughRunner/AutomationRunner:
        # headless above 1280x800 trips the 0.1 CPU budget per shot.
        chosen = tuple(viewport)
        if headless and chosen[0] > 1280 and chosen[1] > 800:
            self.viewport = (1280, 800)
        else:
            self.viewport = chosen
        self.navigation_timeout_ms = int(navigation_timeout_ms)
        self.device_timeout_ms = int(device_timeout_ms)
        self.max_pages = max(1, int(max_pages))
        self.max_form_fills = max(1, int(max_form_fills))
        self.axe_enabled = bool(axe_enabled)
        self.record_video = bool(record_video)
        self.engine_kind = (engine_kind or "chromium").strip().lower()
        self.user_agent = user_agent or ""
        self.test_cases: list[dict[str, Any]] = list(test_cases or [])
        self.credentials = credentials
        self.site_profile = site_profile or None
        self.oom_guard = OomGuard(memory_budget_mb)
        self.findings: list[dict[str, Any]] = []
        # Records which TCs matched on which URL. Useful for the
        # results UI to show coverage even when the TC succeeded
        # silently.
        self.tc_bindings: list[dict[str, Any]] = []
        # Internal AutomationRunner instance — created lazily inside
        # :meth:`run` once the browser is up. It owns the per-TC step
        # execution (goto/click/fill/expect_text/...) so we don't
        # duplicate that ~150-line logic here. We override its
        # ``_write_live_info`` to a no-op so it doesn't compete with
        # LiveExecutor's own live writes.
        self._inner_runner: AutomationRunner | None = None
        # Filled during run(); exposed for tests.
        self.run_id: str = ""
        self.early_exit_reason: str = ""

    @property
    def device_kind(self) -> str:
        return _device_kind(self.viewport)

    # ── findings emitter ───────────────────────────────────────────

    def _note(self, severity: str, area: str, defect_class: str,
              message: str, *, url: str, tc_id: str,
              element: str = "", screenshot: str = "",
              fix_hint: str = "", dev_detail: str = "",
              user_impact: str = "") -> None:
        self.findings.append({
            "severity":     severity,
            "area":         area,
            "defect_class": defect_class,
            "message":      message,
            "url":          url,
            "element":      element,
            "screenshot":   screenshot,
            "fix_hint":     fix_hint,
            "dev_detail":   dev_detail,
            "user_impact":  user_impact,
            "tc_id":        tc_id,
            "console_errors": [],
        })

    # ── live-feed helpers ──────────────────────────────────────────

    def _reset_live_dir(self, runs_root: str) -> str:
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
            os.makedirs(strip_dir, exist_ok=True)
        except OSError as exc:
            _logger.debug("live reset failed: %s", exc)
            return ""
        return live_dir

    def _write_live_info(self, live_dir: str, *, status: str,
                          total: int, done: int, current_url: str = "",
                          current_tc: str = "",
                          run_id: str = "",
                          started_ts: float = 0.0,
                          extra: dict | None = None) -> None:
        """Atomic write of ``_live/info.json`` so the /live endpoint
        picks up Stage-3 progress with no template changes. ``ts`` is
        always bumped so the stall detector keeps moving."""
        if not live_dir:
            return
        info = {
            "status": status,
            "run_id": run_id,
            "total": total,
            "done": done,
            "current_tc": current_tc or current_url,
            "current_url": current_url,
            "elapsed_ms": int(max(0.0, time.time() - started_ts) * 1000),
            "mode": "live",
            "ts": int(time.time() * 1000),
            "rss_mb": self.oom_guard.rss_mb(),
            "memory_budget_mb": self.oom_guard.budget_mb,
        }
        if extra:
            info.update(extra)
        path = os.path.join(live_dir, "info.json")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(info, f)
            os.replace(tmp, path)
        except OSError as exc:
            _logger.debug("live info write failed: %s", exc)

    def _push_to_strip(self, live_dir: str, src_png_abs: str,
                        slot_idx: int) -> None:
        """Rotate ``src_png_abs`` into ``_live/strip/<slot>.png``.

        Slot is ``slot_idx % 12`` so the ring buffer holds the last
        12 frames. Atomic via ``.tmp + os.replace`` so the live page
        never reads a half-written PNG.
        """
        if not live_dir or not src_png_abs or not os.path.isfile(src_png_abs):
            return
        slot = slot_idx % 12
        strip_dir = os.path.join(live_dir, "strip")
        try:
            os.makedirs(strip_dir, exist_ok=True)
            dst = os.path.join(strip_dir, f"{slot:02d}.png")
            shutil.copyfile(src_png_abs, dst + ".tmp")
            os.replace(dst + ".tmp", dst)
        except OSError as exc:
            _logger.debug("strip push failed: %s", exc)

    def _settle_for_paint(self, page) -> None:
        """Best-effort wait so the first live frame isn't a pre-paint
        blank/black shot. Waits for the ``load`` event (capped well under
        the nav timeout) then a short fixed settle. Never raises — a page
        that never reaches ``load`` (or a fake page in tests that lacks
        ``wait_for_load_state``) just falls through to the fixed settle.
        """
        try:
            cap = min(self.navigation_timeout_ms, LIVE_PAINT_LOAD_CAP_MS)
            page.wait_for_load_state("load", timeout=cap)
        except Exception:
            pass
        try:
            page.wait_for_timeout(LIVE_PAINT_MIN_MS)
        except Exception:
            pass

    def _screenshot(self, page, run_dir: str, live_dir: str,
                     tc_id: str, label: str, slot_idx: int) -> str:
        """Save a per-page screenshot, mirror to ``_live/latest.png``,
        and rotate into the filmstrip ring."""
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
            # WARNING (not DEBUG): a failed capture is the difference
            # between a working live view and a permanently black one,
            # so make it visible in logs that aren't level-filtered.
            _logger.warning("live screenshot failed (%s): %s",
                             type(exc).__name__, exc)
            return ""
        if live_dir:
            try:
                latest_tmp = os.path.join(live_dir, "latest.png.tmp")
                shutil.copyfile(abs_path, latest_tmp)
                os.replace(latest_tmp,
                           os.path.join(live_dir, "latest.png"))
                self._push_to_strip(live_dir, abs_path, slot_idx)
            except OSError:
                pass
        return rel_path

    # ── main entry point ───────────────────────────────────────────

    def run(self, start_urls: list[str] | None = None) -> RunReport:
        """Walk the resolved URL plan, run heuristics + matching TCs.

        Returns a :class:`RunReport` with one :class:`ScriptResult`
        per executed TC (zero results when no TC matched any visited
        URL — heuristic findings still surface via ``self.findings``).
        """
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            _logger.error("playwright import failed: %s", exc)
            return self._empty_report("playwright_import_failed")

        # Resolve URL plan: explicit start_urls > SiteProfile.key_pages
        # > [base_url]. Dedupe preserving order; cap at max_pages so
        # a 200-page sitemap doesn't trip the wall-clock.
        seed_urls = self._resolve_seed_urls(start_urls)
        if not seed_urls:
            return self._empty_report("no_start_urls")

        run_id = (datetime.now().strftime("%Y%m%d_%H%M%S_")
                  + uuid.uuid4().hex[:6])
        self.run_id = run_id
        runs_root = os.path.join(self.storage_root, "automation_runs")
        run_dir = os.path.join(runs_root, run_id)
        os.makedirs(run_dir, exist_ok=True)

        try:
            _purge_old_automation_runs(
                runs_root,
                AUTOMATION_RUN_RETENTION_DAYS,
                AUTOMATION_RUN_MAX_KEPT,
            )
        except Exception as exc:
            _logger.debug("retention purge skipped: %s", exc)

        live_dir = self._reset_live_dir(runs_root)
        started_ts = time.time()
        wall_deadline = started_ts + (self.device_timeout_ms / 1000.0)

        scripts: list[ScriptResult] = []
        visited: set[str] = set()
        queue: list[str] = list(seed_urls)
        slot_idx = 0
        early_exit = False
        early_reason = ""

        self._write_live_info(
            live_dir, status="starting",
            total=len(queue), done=0, run_id=run_id,
            started_ts=started_ts,
        )

        with sync_playwright() as pw:
            engine_obj = getattr(pw, self.engine_kind, pw.chromium)
            browser = engine_obj.launch(headless=self.headless)
            # Publish the live browser handle so the worker's signal
            # handler can close Chromium on SIGTERM (same contract as
            # AutomationRunner — module globals avoid private-attr
            # imports). We import the module so the assignment lands
            # on the running module dict.
            try:
                from engine import automation_runner as _ar
                _ar._CURRENT_BROWSER = browser
            except Exception:
                pass

            # Create internal AutomationRunner for TC step execution.
            # Override its live-info writes so it doesn't compete with
            # ours; otherwise the two would race-write info.json and
            # the live tab would flicker between modes.
            self._inner_runner = AutomationRunner(
                storage_root=self.storage_root,
                base_url=self.base_url,
                headless=self.headless,
                viewport=self.viewport,
                navigation_timeout_ms=self.navigation_timeout_ms,
                record_video=self.record_video,
                credentials=self.credentials,
                user_agent=self.user_agent,
                engine_kind=self.engine_kind,
                project_id=self.project_id,
            )
            self._silence_inner_runner_live(self._inner_runner)

            try:
                while queue and len(visited) < self.max_pages:
                    if time.time() >= wall_deadline:
                        early_exit = True
                        early_reason = "wall_deadline_exceeded"
                        break
                    if self.oom_guard.over_budget():
                        early_exit = True
                        early_reason = (
                            f"oom_budget_exceeded "
                            f"({self.oom_guard.rss_mb()} MB > "
                            f"{self.oom_guard.budget_mb} MB)"
                        )
                        break

                    url = queue.pop(0)
                    norm = self._normalize_url(url)
                    if not norm or norm in visited:
                        continue
                    visited.add(norm)
                    page_idx = len(visited)
                    page_tc_id = f"LIVE-PAGE-{page_idx:03d}"

                    self._write_live_info(
                        live_dir, status="running",
                        total=max(len(queue) + len(visited),
                                   len(seed_urls)),
                        done=len(visited) - 1,
                        current_url=url, current_tc=page_tc_id,
                        run_id=run_id, started_ts=started_ts,
                    )

                    # Visit, run heuristics, discover links.
                    walked = self._walk_one(
                        browser, page_idx, url, run_dir, live_dir,
                        slot_idx_base=slot_idx, queue=queue,
                        visited=visited,
                    )
                    if walked:
                        scripts.append(walked)
                        slot_idx += 1

                    # Match + execute TCs for the (final) URL we landed on.
                    landed_url = (walked.final_url
                                   if walked and walked.final_url
                                   else url)
                    tc_scripts = self._execute_matching_tcs(
                        browser, landed_url, run_dir, live_dir,
                        slot_idx_base=slot_idx,
                        started_ts=started_ts, total=len(visited),
                    )
                    scripts.extend(tc_scripts)
                    slot_idx += len(tc_scripts)

                    # GC hint between pages so leaked closures / DOM
                    # snapshots don't pile up against the OOM budget.
                    gc.collect()

                    if self.oom_guard.over_budget():
                        early_exit = True
                        early_reason = (
                            f"oom_budget_exceeded_after_tcs "
                            f"({self.oom_guard.rss_mb()} MB > "
                            f"{self.oom_guard.budget_mb} MB)"
                        )
                        break
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
                try:
                    from engine import automation_runner as _ar
                    _ar._CURRENT_BROWSER = None
                except Exception:
                    pass

        if early_exit:
            self.early_exit_reason = early_reason

        passed = sum(1 for s in scripts if s.status == "passed")
        failed = sum(1 for s in scripts if s.status == "failed")
        blocked = sum(1 for s in scripts if s.status == "blocked")
        duration_ms = int((time.time() - started_ts) * 1000)

        self._write_live_info(
            live_dir,
            status="oom_exit" if early_exit and "oom" in early_reason
                    else ("done" if not early_exit else "early_exit"),
            total=len(visited), done=len(visited),
            run_id=run_id, started_ts=started_ts,
            extra={"early_exit_reason": early_reason} if early_exit else None,
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

    # ── plan + walk ───────────────────────────────────────────────

    def _resolve_seed_urls(self, start_urls: list[str] | None) -> list[str]:
        """Deduplicate seed list: explicit > SiteProfile.key_pages > base_url."""
        out: list[str] = []
        if start_urls:
            for u in start_urls:
                if u and u.strip() and u not in out:
                    out.append(u.strip())
        # Pull from SiteProfile.key_pages if no explicit list.
        if not out and self.site_profile:
            for u in _key_pages_from_profile(self.site_profile, self.base_url):
                if u not in out:
                    out.append(u)
        # Always include base_url as a fallback.
        if self.base_url and self.base_url not in out:
            out.append(self.base_url)
        return out[: self.max_pages]

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Strip trailing slash + fragment so /foo and /foo/ aren't visited
        twice. Returns "" for malformed input."""
        if not url:
            return ""
        try:
            p = urlparse(url)
            if not p.scheme or not p.netloc:
                return ""
            path = (p.path or "/").rstrip("/")
            if not path:
                path = "/"
            q = f"?{p.query}" if p.query else ""
            return f"{p.scheme}://{p.netloc}{path}{q}"
        except Exception:
            return ""

    def _walk_one(self, browser, idx: int, url: str,
                   run_dir: str, live_dir: str,
                   *, slot_idx_base: int,
                   queue: list[str],
                   visited: set[str]) -> ScriptResult | None:
        """Visit ``url``, screenshot, run heuristics, enqueue links."""
        tc_id = f"LIVE-PAGE-{idx:03d}"
        context = browser.new_context(viewport={
            "width": self.viewport[0], "height": self.viewport[1],
        })
        try:
            page = context.new_page()
            page.set_default_navigation_timeout(self.navigation_timeout_ms)
            console_errors: list[dict[str, Any]] = []
            page_errors: list[dict[str, Any]] = []
            install_error_listeners(page, console_errors, page_errors)

            t0 = time.time()
            steps: list[StepResult] = []
            try:
                page.goto(url, wait_until="domcontentloaded",
                           timeout=self.navigation_timeout_ms)
                # Let the page actually paint before the first live frame
                # — `domcontentloaded` fires too early and yields a black
                # shot on heavy/dark SPAs.
                self._settle_for_paint(page)
                shot = self._screenshot(page, run_dir, live_dir,
                                          tc_id, "page", slot_idx_base)
                steps.append(StepResult(
                    index=1, action="goto", raw=url, status="passed",
                    duration_ms=int((time.time() - t0) * 1000),
                    screenshot_after=shot,
                ))
                final_url = (page.url if hasattr(page, "url") else "") or url
            except Exception as exc:
                steps.append(StepResult(
                    index=1, action="goto", raw=url, status="failed",
                    duration_ms=int((time.time() - t0) * 1000),
                    comment=f"{type(exc).__name__}: {exc}"[:500],
                ))
                self._note(
                    _SEV_CRITICAL, "Loading", "navigation_timeout",
                    f"Page failed to load within "
                    f"{self.navigation_timeout_ms} ms: "
                    f"{type(exc).__name__}",
                    url=url, tc_id=tc_id,
                )
                return ScriptResult(
                    tc_id=tc_id,
                    summary=f"Live walk: {url}",
                    status="failed",
                    duration_ms=int((time.time() - t0) * 1000),
                    steps=steps,
                    comment=f"navigation failed: {type(exc).__name__}",
                    final_url=url,
                )

            # Heuristic battery.
            before_count = len(self.findings)
            heuristics: list[tuple[str, Callable]] = [
                ("broken_images",   lambda: scan_broken_images(
                    page, final_url, tc_id, note=self._note)),
                ("navigation_menu", lambda: scan_navigation_menu(
                    page, final_url, tc_id, note=self._note,
                    device_kind=self.device_kind)),
                ("footer_social",   lambda: scan_footer_social(
                    page, final_url, tc_id, note=self._note)),
                ("search_field",    lambda: scan_search_field(
                    page, final_url, tc_id, note=self._note)),
                ("forms",           lambda: scan_forms(
                    page, final_url, tc_id, note=self._note,
                    max_form_fills=self.max_form_fills)),
                ("ctas",            lambda: scan_ctas(
                    page, final_url, tc_id, note=self._note)),
                ("axe",             lambda: scan_axe(
                    page, final_url, tc_id, note=self._note,
                    axe_enabled=self.axe_enabled)),
            ]
            for label, fn in heuristics:
                try:
                    fn()
                except Exception as exc:
                    _logger.debug("live heuristic %s raised: %s",
                                   label, exc)
                    self._note(
                        _SEV_MINOR, "Walkthrough", "walk_step_failed",
                        f"Heuristic {label!r} raised "
                        f"{type(exc).__name__}: {str(exc)[:160]}",
                        url=final_url, tc_id=tc_id,
                    )
            collect_console_errors(console_errors, page_errors,
                                    final_url, tc_id, note=self._note)

            # Second live frame after the heuristic battery. By now the
            # page is fully rendered and the form / CTA scans may have
            # scrolled or interacted with it, so this frame is more
            # representative — and on a single-page walk it's what keeps
            # the live view from looking frozen on one early shot. Pushed
            # to the same ring slot so the strip reflects the latest
            # state; failures are swallowed inside `_screenshot`.
            self._screenshot(page, run_dir, live_dir,
                             tc_id, "page_after", slot_idx_base)

            # PR-D′: for every finding that names a CSS selector, try
            # to capture an *annotated* screenshot — the raw page shot
            # with a red rectangle around the offending element and a
            # red arrow pointing at it. Done BEFORE the PR-B fan-out
            # below so annotation succeeds → ``finding["screenshot"]``
            # is the annotated path; annotation fails → the field stays
            # empty and PR-B's fan-out falls back to the raw page shot.
            # That two-stage degradation keeps the bug list lossless
            # while delivering the better-looking attachment on the
            # happy path. ``page`` is still alive at this point —
            # ``finally`` block at the bottom of the method closes the
            # context after we exit.
            # PR-F: hardened annotation block.
            #
            # The previous revision (PR-D′) crashed silently for every
            # axe finding because ``scan_axe`` puts a *list* of CSS
            # selectors in ``f["element"]`` (axe's ``node0.target`` is
            # the locator chain). ``str.strip()`` on a list raises and
            # the whole walk fell into the outer ``except Exception``
            # → no annotation, raw page shot via the PR-B fan-out
            # below. Three defences:
            #
            #   1. Accept ``element`` as ``list[str] | str`` — take the
            #      first non-empty string item when it's a list.
            #   2. Wrap the whole block in its own ``try`` so a single
            #      bad finding does not skip annotation for the rest
            #      of the page.
            #   3. Log every outcome at INFO so Render's stdout stream
            #      shows whether annotation fired, why it bailed, and
            #      which path got written. Without these the only way
            #      to diagnose the silent failure was to dig through
            #      bug records via the MCP server.
            # PR-H: each branch below sets ``f["annotation_status"]``
            # so the bug factory carries the diagnostic verdict into
            # ``bug.extra.annotation_status``. Render Logs strips
            # Python application logs in the current host config,
            # so writing telemetry through the bug record is the
            # only reliable way to see WHY annotation succeeded or
            # bailed for a given finding.
            if shot:
                try:
                    from engine.screenshot_annotator import (
                        annotate_screenshot, derive_annotated_path,
                    )
                except Exception as exc:
                    _logger.warning(
                        "annotate: module import failed: %s — falling back "
                        "to raw page shots via PR-B fan-out", exc,
                    )
                    annotate_screenshot = None  # type: ignore[assignment]
                    # Mark every future finding on this page so the
                    # bug record explains why annotation didn't fire.
                    for f in self.findings[before_count:]:
                        f.setdefault(
                            "annotation_status",
                            f"failed:module_import:{type(exc).__name__}",
                        )

                # PR-I: per-finding viewport screenshot.
                #
                # The PR-D/F/H approach annotated the *page-level*
                # screenshot taken at goto. That shot is viewport-
                # sized (1280×800) and captures the top of the page
                # only. The bbox we compute after running heuristics
                # is page-absolute (Playwright contract), so any
                # element below the fold (axe ``#All-vacancies`` on
                # /jobs, broken images at bottom of /careers, …) had
                # bbox.y > screenshot_height, my annotator clamped to
                # zero, and the fallback to raw page.png made the
                # attachment useless for triage.
                #
                # PR-I fixes it per-finding: scroll the element into
                # view, take a FRESH viewport screenshot (the element
                # is now visible in it), translate the page-absolute
                # bbox to viewport-relative coords by subtracting the
                # current scrollX/Y, and annotate THAT shot. Each bug
                # ends up with its own focused 1280×800 image showing
                # the defect with a red box around it — exactly what
                # the QA style guide asked for.
                if shot and annotate_screenshot is not None:
                    import os as _os_pr_i
                    for idx, f in enumerate(
                        self.findings[before_count:], start=1
                    ):
                        if f.get("screenshot"):
                            f.setdefault(
                                "annotation_status",
                                "skipped:screenshot_preset",
                            )
                            continue
                        # ── 1. Coerce element to a single string ──
                        element_raw = f.get("element") or ""
                        if isinstance(element_raw, (list, tuple)):
                            selector = next(
                                (str(s).strip() for s in element_raw
                                 if s and str(s).strip()),
                                "",
                            )
                        else:
                            selector = str(element_raw).strip()
                        if not selector:
                            f["annotation_status"] = "skipped:no_selector"
                            continue
                        # ── 2. Scroll element into view ────────────────
                        try:
                            page.locator(selector).first \
                                .scroll_into_view_if_needed(timeout=2000)
                        except Exception as exc:
                            _logger.info(
                                "annotate: scroll(%r) failed: %s — "
                                "falling back to page-level shot",
                                selector, exc,
                            )
                            f["annotation_status"] = (
                                f"failed:scroll_exc:{type(exc).__name__}"
                            )
                            continue
                        # ── 3. Read page-absolute bbox ─────────────────
                        try:
                            page_box = (
                                page.locator(selector).first
                                .bounding_box(timeout=2000)
                            )
                        except Exception as exc:
                            _logger.info(
                                "annotate: bounding_box(%r) failed: %s",
                                selector, exc,
                            )
                            f["annotation_status"] = (
                                f"failed:bbox_exc:{type(exc).__name__}"
                            )
                            continue
                        if not page_box:
                            _logger.info(
                                "annotate: bounding_box(%r) returned None",
                                selector,
                            )
                            f["annotation_status"] = "skipped:bbox_none"
                            continue
                        # ── 4. Take a fresh viewport screenshot ────────
                        # The element is now visible after the
                        # ``scroll_into_view_if_needed`` above. We
                        # save it next to the page-level shot so
                        # /automation/asset serves it from the same
                        # directory the bug attachment will reference.
                        try:
                            page_shot_dir = _os_pr_i.path.dirname(shot)
                            page_shot_stem, _ = _os_pr_i.path.splitext(
                                _os_pr_i.path.basename(shot)
                            )
                            viewport_shot = _os_pr_i.path.join(
                                page_shot_dir,
                                f"{page_shot_stem}_finding"
                                f"{idx:02d}_viewport.png",
                            )
                            page.screenshot(
                                path=viewport_shot, full_page=False,
                            )
                        except Exception as exc:
                            _logger.warning(
                                "annotate: viewport screenshot for %r "
                                "failed: %s", selector, exc,
                            )
                            f["annotation_status"] = (
                                f"failed:screenshot_exc:"
                                f"{type(exc).__name__}"
                            )
                            continue
                        # ── 5. Translate page-abs bbox → viewport-rel ──
                        try:
                            scroll_x = float(
                                page.evaluate("window.scrollX || 0")
                            )
                            scroll_y = float(
                                page.evaluate("window.scrollY || 0")
                            )
                        except Exception:
                            # Older Playwright versions / unusual
                            # contexts — assume scroll origin if
                            # the evaluate path errored. The bbox
                            # will still be roughly correct for
                            # above-fold elements.
                            scroll_x = 0.0
                            scroll_y = 0.0
                        viewport_box = {
                            "x": page_box["x"] - scroll_x,
                            "y": page_box["y"] - scroll_y,
                            "width": page_box["width"],
                            "height": page_box["height"],
                        }
                        # ── 6. Annotate the viewport shot ──────────────
                        try:
                            out_path = derive_annotated_path(shot, idx)
                            annotated = annotate_screenshot(
                                raw_path=viewport_shot,
                                bbox=viewport_box,
                                output_path=out_path,
                            )
                        except Exception as exc:
                            _logger.warning(
                                "annotate: draw failed for %r: %s",
                                selector, exc,
                            )
                            f["annotation_status"] = (
                                f"failed:draw_exc:{type(exc).__name__}"
                            )
                            continue
                        if annotated:
                            f["screenshot"] = annotated
                            f["annotation_status"] = (
                                f"annotated:{annotated}"
                            )
                            _logger.info(
                                "annotate: wrote %s for %r",
                                annotated, selector,
                            )
                        else:
                            _logger.info(
                                "annotate: annotator returned None for %r",
                                selector,
                            )
                            f["annotation_status"] = (
                                "skipped:annotator_returned_none"
                            )

            # PR-B: heuristics call ``note(...)`` without a ``screenshot=``
            # kwarg — they have no cheap way to take a per-element shot,
            # so they leave the field at its default empty string. Before
            # this fan-out, ``finding["screenshot"]`` stayed "" all the
            # way into :func:`engine.bug_report.create_bug_from_walkthrough_finding`,
            # which then wrote ``attachments=[]`` and made /bug-reports
            # render the misleading "No attachments captured / Base URL
            # missing" banner even on runs where Playwright took a
            # perfectly valid page shot at the goto step above.
            #
            # Inject ``shot`` into every finding emitted during this
            # page walk that has no screenshot of its own; heuristics
            # that gain per-element capture in the future can still
            # override the field by passing ``screenshot=...`` and we
            # preserve their value here. Side-effect: walkthrough
            # ``StepResult.screenshot_after`` slots (built right below)
            # also inherit the page shot, so the per-page gallery card
            # finally shows a thumbnail next to each defect row.
            if shot:
                for f in self.findings[before_count:]:
                    if not f.get("screenshot"):
                        f["screenshot"] = shot

            # Synthesise StepResults from new findings so the gallery
            # shows each defect as a row under this page's card.
            for offset, f in enumerate(self.findings[before_count:],
                                        start=2):
                steps.append(StepResult(
                    index=offset, action="walkthrough_check",
                    raw=f"{f['area']}: {f['message'][:120]}",
                    status="failed", duration_ms=0,
                    comment=f.get("dev_detail", "") or f.get("message", ""),
                    screenshot_after=f.get("screenshot", "") or "",
                    console_errors=list(f.get("console_errors") or []),
                ))

            # Enqueue discovered internal links.
            for new_url in discover_links_on_page(page, final_url):
                norm = self._normalize_url(new_url)
                if norm and norm not in visited and norm not in queue:
                    queue.append(norm)

            page_findings = self.findings[before_count:]
            any_critical = any(f.get("severity") == _SEV_CRITICAL
                                for f in page_findings)
            status = "failed" if any_critical else "passed"
            comment = (f"{len(page_findings)} finding(s) on this page"
                        if page_findings else "")
            return ScriptResult(
                tc_id=tc_id,
                summary=f"Live walk: {url}",
                status=status,
                duration_ms=int((time.time() - t0) * 1000),
                steps=steps,
                comment=comment,
                final_url=final_url,
            )
        finally:
            try:
                context.close()
            except Exception:
                pass

    # ── TC dispatch ───────────────────────────────────────────────

    def _execute_matching_tcs(self, browser, landed_url: str,
                                run_dir: str, live_dir: str,
                                *, slot_idx_base: int,
                                started_ts: float,
                                total: int) -> list[ScriptResult]:
        """Match TCs by ``url_pattern`` + ``trigger`` and execute each
        via the internal AutomationRunner's ``_run_script``.

        Each TC gets its own BrowserContext courtesy of AutomationRunner;
        early-cleanup between TCs is the spec's primary OOM mitigation
        and AutomationRunner already does it in its ``finally`` block.
        """
        if not self.test_cases or self._inner_runner is None:
            return []
        matched = match_tcs_for_url(self.test_cases, landed_url)
        if not matched:
            return []

        # Record bindings for the result.json audit trail. Operators
        # ask "did TC-15 actually run on /checkout?" — bindings make
        # the answer obvious.
        self.tc_bindings.append({
            "url": landed_url,
            "matches": [{
                "id":          t.get("id"),
                "external_id": t.get("external_id") or t.get("id"),
                "summary":     (t.get("summary") or "")[:120],
                "url_pattern": t.get("url_pattern", ""),
                "trigger":     t.get("trigger", ""),
            } for t in matched],
        })

        out: list[ScriptResult] = []
        for slot_offset, tc in enumerate(matched):
            # Synthesise a script: prepend a goto step pointing at the
            # landed URL so the TC starts on the right page even when
            # the parsed steps don't include an explicit navigate.
            # tc_to_script already adds a goto IF the steps mention a
            # URL; we override with the actual landed_url which is the
            # most reliable target.
            script = tc_to_script(tc, base_url=landed_url)
            # Force-prepend a goto for the landed URL when the script's
            # first step isn't already a goto to the same URL.
            from engine.automation_qa import AutomationStep as _Step
            needs_prepend = True
            if script.steps and script.steps[0].action == "goto":
                if (script.steps[0].target or "").rstrip("/") == landed_url.rstrip("/"):
                    needs_prepend = False
            if needs_prepend:
                script.steps.insert(0, _Step(
                    action="goto", target=landed_url, raw=f"goto {landed_url}",
                ))

            self._write_live_info(
                live_dir, status="running_tc",
                total=total, done=total - 1,
                current_url=landed_url,
                current_tc=script.tc_id or "TC",
                run_id=self.run_id, started_ts=started_ts,
            )

            try:
                sr = self._inner_runner._run_script(browser, script, run_dir)
            except Exception as exc:
                sr = ScriptResult(
                    tc_id=script.tc_id, summary=script.summary,
                    status="blocked",
                    duration_ms=0,
                    comment=f"runner exception: {type(exc).__name__}: {exc}"[:300],
                    final_url=landed_url,
                )
            out.append(sr)

            # Mirror the TC's last per-step screenshot into the live
            # filmstrip so the operator sees TC execution proceed.
            try:
                last_after = ""
                for st in (sr.steps or []):
                    if getattr(st, "screenshot_after", ""):
                        last_after = st.screenshot_after
                if last_after:
                    abs_p = os.path.join(
                        self.storage_root,
                        last_after.replace("/", os.sep))
                    if os.path.isfile(abs_p):
                        # Also update latest.png + ring.
                        latest_tmp = os.path.join(live_dir, "latest.png.tmp")
                        shutil.copyfile(abs_p, latest_tmp)
                        os.replace(latest_tmp,
                                   os.path.join(live_dir, "latest.png"))
                        self._push_to_strip(live_dir, abs_p,
                                             slot_idx_base + slot_offset)
            except Exception:
                pass

            # Per-TC OomGuard check + GC. AutomationRunner._run_script
            # closes its own context in finally; we GC right after to
            # reclaim the closure tree before the next TC's context
            # allocation.
            gc.collect()
            if self.oom_guard.over_budget():
                _logger.warning(
                    "LiveExecutor: OOM budget hit mid-TC-dispatch on %s "
                    "(rss=%d MB, budget=%d MB) — early exit",
                    landed_url, self.oom_guard.rss_mb(),
                    self.oom_guard.budget_mb,
                )
                # Re-raising lets the outer ``while queue`` loop in
                # ``run`` see the OOM and write the proper status.
                # We don't raise — instead we bail out of the for loop
                # so the partial TC list is preserved.
                break

        return out

    # ── helpers ───────────────────────────────────────────────────

    def _silence_inner_runner_live(self, runner: AutomationRunner) -> None:
        """Stub the inner AutomationRunner's live-feed writers so it
        doesn't race-write info.json against LiveExecutor."""
        # ``_write_live_info`` / ``_live_pump`` / ``_write_phase`` /
        # ``_write_warmup_frame`` are instance methods; replace them
        # with a noop to keep the inner runner from racing our writes.
        # We use object.__setattr__ to avoid triggering any property
        # setters AutomationRunner might gain in the future.
        def _noop(*_a, **_kw):
            return None
        for attr in ("_write_live_info", "_live_pump", "_write_phase",
                      "_write_warmup_frame"):
            try:
                object.__setattr__(runner, attr, _noop)
            except Exception:
                pass
        # Point its _live_dir at a per-run sub-directory so the
        # underlying _screenshot path lookups never collide with ours.
        runner._live_dir = ""

    def _empty_report(self, reason: str) -> RunReport:
        return RunReport(
            run_id="",
            started_at=datetime.now().isoformat(timespec="seconds"),
            finished_at=datetime.now().isoformat(timespec="seconds"),
            base_url=self.base_url,
            headless=self.headless,
            total=0,
            blocked=1,
            scripts=[ScriptResult(
                tc_id="LIVE-INIT",
                summary=f"Live executor blocked: {reason}",
                status="blocked",
                duration_ms=0,
                steps=[],
                comment=reason,
            )],
        )

    # ── post-run helpers ──────────────────────────────────────────

    def dedupe_findings(self) -> list[dict[str, Any]]:
        """Return ``self.findings`` collapsed via the existing
        :func:`engine.walkthrough_dedup.dedupe`. Useful for the
        results JSON's ``walkthrough_findings_deduped`` field."""
        return _dedupe_findings(list(self.findings))


__all__ = [
    "LiveExecutor",
    "OomGuard",
    "DEFAULT_MEMORY_BUDGET_MB",
    "discover_links_on_page",
]
