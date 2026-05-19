# Sprint 3 — Performance & Cost

**Goal:** Reduce Tedgie chat latency, cut Anthropic input-token cost, add metrics
history trends, harden the SQLite path that the snapshot writer + detached
runner are about to make spicy.

**Estimate:** ~21 h + 5 h buffer = **~26 h** (≈ 3.5 mid-level dev days).

**Recommended order:** 3.4 → 3.1 → 3.2 → 3.3 (3.4 first because 3.3's worker
writes will deadlock unless WAL + busy_timeout is in place).

## Pre-flight observations

1. **Only two real LLM call-sites.** `engine/chatbot.py::_ai_respond` and
   `engine/mockup_vision.py::_call_claude_vision`. `testcase_generator.py`,
   `user_story_generator.py`, `qa_persona.py` are fully deterministic — no LLM.
2. **Gunicorn runs `--worker-class gthread --workers 1 --threads 4`**
   (`render.yaml:56`). gthread supports SSE long-lived responses; no switch needed.
3. **Metrics-history schema already exists.** `DashboardMetricSnapshot` at
   `engine/db.py:358`; `save_metric_snapshot` / `list_metric_snapshots` at lines
   952 / 965. Dashboard route already snapshots opportunistically (`routes/dashboard.py:120`).
4. **Tedgie system prompt is ~400 tokens** — below Anthropic's 1024-token
   cache minimum. Caching requires enlarging the prompt first.
5. **CSRF + GET-streaming is a non-issue** — Flask-WTF CSRF only protects state-changing methods.
6. **SQLite locking already aggravated** by detached worker writes + dashboard snapshot writer.

---

## Task 3.1 — SSE streaming for Tedgie chat

### Why now
- Current `POST /chat` blocks for the full Claude round-trip. ~3–6 s perceived latency.
- Streaming brings time-to-first-token to ~500 ms — 6–12× perceived improvement.
- Rule-based handlers in `chatbot_guide.try_guide_handlers` are sub-50 ms — bypass streaming entirely.

### Design

**Endpoint:** `GET /chat/stream?message=...&lang=en` → `text/event-stream`.
GET (not POST) because EventSource only does GET, CSRF irrelevant.

**Event protocol** (per line, `\n\n` terminated):
```
event: meta    data: {"intent": "ai_generic", "lang": "en"}
event: delta   data: {"text": "Sure, "}
event: delta   data: {"text": "here's how..."}
event: done    data: {"suggestions": [...], "follow_up": [...]}
```

For rule-based replies: single `event: full` payload + `event: done`. JS handles
both shapes uniformly.

### Dispatch flow

```python
# routes/chat.py
@app.route("/chat/stream", methods=["GET"])
def chat_stream_route():
    message = (request.args.get("message") or "").strip()[:max_chars]
    lang = (request.args.get("lang") or session.get("lang") or "en").lower()
    if lang not in ("en", "ua"): lang = "en"

    def generate():
        # 1. Fast-path rule handlers (greeting / guide / istqb / bug_form)
        fast = _chatbot.try_fast_path(message, lang)
        if fast is not None:
            yield _sse("full", fast.__dict__)
            yield _sse("done", {})
            _append_history(message, fast)
            return

        # 2. LLM streaming path
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            fallback = _chatbot.rule_based_fallback(message, lang)
            yield _sse("full", fallback.__dict__)
            yield _sse("done", {})
            return

        yield _sse("meta", {"intent": "ai_generic", "lang": lang})
        chunks = []
        try:
            client = Anthropic(api_key=api_key)
            with client.messages.stream(
                model=_ANTHROPIC_MODEL,
                max_tokens=_ANTHROPIC_MAX_TOKENS,
                system=_ai_system_prompt(lang),
                messages=[{"role": "user", "content": message}],
            ) as stream:
                for text in stream.text_stream:
                    chunks.append(text)
                    yield _sse("delta", {"text": text})
                final = stream.get_final_message()
                _log_usage(final.usage)
            full = "".join(chunks).strip()
            intent = "bug_form" if "<BUG_FORM/>" in full else "ai_generic"
            if intent == "bug_form":
                full = full.replace("<BUG_FORM/>", "").strip()
            _append_history(message, ChatReply(text=full, intent=intent))
            yield _sse("done", {"intent": intent})
        except GeneratorExit:
            _logger.info("SSE client disconnected mid-stream")
            raise
        except Exception as exc:
            _logger.warning("stream failed: %s", exc)
            yield _sse("error", {"message": "Stream interrupted"})

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})
```

`with client.messages.stream(...)` context manager cancels the upstream call
when generator is garbage-collected.

### Refactor in `engine/chatbot.py`

Extract `try_fast_path(message, lang) -> ChatReply | None` from `respond()` —
runs the first ~80% (everything up to `_ai_respond`). Pure refactor.

### Frontend

`static/js/app.js::sendMessage` (around line 375): feature-detect EventSource,
fall back to POST. Backward compat: POST `/chat` stays.

### Files

- `routes/chat.py` — add `chat_stream_route` (~70 lines)
- `engine/chatbot.py` — extract `try_fast_path` + stream helper (~40 lines)
- `static/js/app.js` — split sendMessage (~30 lines)
- `render.yaml` — confirm `gthread` requirement (comment only)
- `tests/test_chat_stream.py` (new) — fake Anthropic stream, assert SSE shape

### Tests
- Unit: SSE generator with fake stream yielding 3 chunks; assert byte output.
- Fast-path bypass: greeting → no Anthropic class instantiated.
- Manual: `curl -N "http://localhost:5000/chat/stream?message=..."` — first byte time.

### Risks
- **Render 30s idle-connection timeout.** Mitigation: emit `: heartbeat\n\n` every 10 s if no token.
- CSP `connect-src 'self'` already permits same-origin EventSource.

**Estimate:** 6 h. **Depends on:** nothing.

---

## Task 3.2 — Prompt caching for Anthropic

### Why now
- Anthropic ephemeral cache reduces cached input tokens to 10% cost for 5 min.
- Tedgie's 50-user × 10-msg/day cohort = 500 LLM hits/day.
- **Reality check:** today's Tedgie system prompt is ~400 tokens. Minimum cacheable = 1024.

### Plan

**Step 1 — measure prompt sizes.** Add `tools/measure_prompts.py` that calls
`client.beta.messages.count_tokens(...)` for `_ai_system_prompt`,
`istqb_persona_prompt`, `mockup_vision._SYSTEM_PROMPT`. Only restructure
prompts ≥ 1024 tokens.

**Step 2 — Tedgie: enlarge by inlining module knowledge.** Inline `_HELP_EN`
cards (~400 tokens) + ISTQB persona (~150) + 20-term glossary (~600) → clears
1024 comfortably. Side benefit: LLM stops fabricating module behaviour.

**Step 3 — restructure to system-array form with cache marker:**

```python
# engine/chatbot.py
_SYSTEM_BLOCKS_EN = [
    {
        "type": "text",
        "text": _BUILD_TEDGIE_PERSONA_EN(),  # ~1500 tokens, static
        "cache_control": {"type": "ephemeral"},
    },
]
_SYSTEM_BLOCKS_UA = [...]

def _ai_system_blocks(lang: str) -> list[dict]:
    return _SYSTEM_BLOCKS_UA if lang == "ua" else _SYSTEM_BLOCKS_EN
```

Then in stream handler:
```python
with client.messages.stream(
    model=_ANTHROPIC_MODEL,
    max_tokens=_ANTHROPIC_MAX_TOKENS,
    system=_ai_system_blocks(lang),   # list, not string
    messages=[{"role": "user", "content": message}],
) as stream:
    ...
    final = stream.get_final_message()
    _log_cache_usage(final.usage)
```

**Step 4 — same for `mockup_vision.py::_SYSTEM_PROMPT`.** Wrap in system-blocks
list with `cache_control`. Estimation re-runs on same mockup cache-hit cleanly.

**Step 5 — EN/UA separate cache entries.**

**Step 6 — observability.** Log `usage.cache_read_input_tokens` +
`usage.cache_creation_input_tokens` per request in JSON-structured logs.

### Files
- `engine/chatbot.py` (replace `_ai_system_prompt -> str` with `_ai_system_blocks -> list[dict]`; build cached persona)
- `engine/mockup_vision.py` (wrap `_SYSTEM_PROMPT`)
- `tools/measure_prompts.py` (new)
- `tests/test_chatbot_cache.py` (assert `system=` is a list, first block has `cache_control`, ≥ 1024 tokens estimated via char count ≥ 3700)

### Tests
- Cache-hit smoke: two identical requests within 5 min; assert second has `cache_read > 0`.
- Cost dashboard: 1-week window, expected cache-hit rate ≥ 70%.
- Regression: existing chatbot tests pass — same `ChatReply` shape.

### Risks
- **Prompt edits invalidate cache.** First call after deploy pays full price. Document.
- **5-min TTL.** Off-peak → ~0% hit rate. Expected.
- **Combine with 3.1 carefully:** stream wrapper reads `usage` from `stream.get_final_message()`, not partial.

**Estimate:** 4 h. **Depends on:** 3.1 (both touch `_ai_respond`; do together).

---

## Task 3.3 — Metrics history (trends)

### Why now
- Today metrics live in `session["test_runs"]` — wiped on every Render redeploy
  (`SERVER_START_TIME` invalidation in `app.py:201–205`).
- QA value of a dashboard is **change over time**: trend in pass rate, defect
  density acceleration, coverage growth.

### Schema — already exists

`engine/db.py:358` — `DashboardMetricSnapshot(id, project_id FK, captured_at indexed, metrics JSON)`.
Stores full dashboard dict (KPIs + coverage + execution + bug breakdown).

### Plan

**`engine/test_metrics_generator.py`** — add:
```python
def snapshot_metrics(project_id: str) -> int | None:
    """In-request variant — reads from session."""
    from routes.dashboard import _compute_dashboard_metrics
    metrics = _compute_dashboard_metrics()
    if not metrics.get("has_data"):
        return None
    return _db.save_metric_snapshot(project_id, metrics)

def snapshot_metrics_from_db(project_id: str) -> int | None:
    """Out-of-request variant — for detached worker."""
    tcs = _db.list_test_cases(project_id)
    bugs = _db.list_bugs(project_id)
    runs = _db.list_execution_runs(project_id)
    metrics = _aggregate_from_db_rows(tcs, bugs, runs)
    if not metrics["has_data"]:
        return None
    return _db.save_metric_snapshot(project_id, metrics)
```

### Triggers

1. **Run completion** — `engine/runner_worker.py` after `done.flag`. Subprocess, no Flask session: use `snapshot_metrics_from_db`.
2. **Dashboard load** — already throttled once-per-project-per-hour at `routes/dashboard.py:120`.
3. **Daily catch-up** — `threading.Thread(daemon=True)` started in `app.py`. Sleeps 24 h, snapshots projects with no recent snapshot. Document manual cron equivalent.

### History endpoint

```python
# routes/dashboard.py
@app.route("/metrics/history", methods=["GET"])
def metrics_history_route():
    pid = request.args.get("project_id") or session.get("project_id") or ""
    if not pid:
        return jsonify({"snapshots": []})
    days = max(1, min(int(request.args.get("days", 30)), 365))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    rows = _db.list_metric_snapshots(pid, limit=days * 4)
    out = [
        {"ts": r["captured_at"],
         "pass_rate": _kpi(r["metrics"], "exec_pass_rate"),
         "defect_density": _kpi_defect_density(r["metrics"]),
         "tc_total": r["metrics"].get("tc_total", 0),
         "bug_total": r["metrics"].get("bug_total", 0),
         "exec_total": r["metrics"].get("exec_total", 0)}
        for r in rows if r["captured_at"] >= cutoff.isoformat()
    ]
    return jsonify({"snapshots": list(reversed(out))})
```

### Chart library: **uPlot** (45 KB min+gz, MIT)
- Single `<canvas>` file, no React, no Chart.js (220 KB bloat).
- Renders 30 points in well under 1 ms.
- Vendor locally under `static/vendor/uplot.iife.min.js`.

### Template change

```html
<!-- templates/test_metrics.html -->
<section class="card">
  <h2>{{ t.tm_history_title }}</h2>
  <div id="metrics-history" style="width:100%;height:240px"></div>
  <script src="{{ url_for('static', filename='vendor/uplot.iife.min.js') }}"></script>
  <script>
    fetch('/metrics/history?project_id={{ active_project_id }}&days=30')
      .then(r => r.json()).then(({snapshots}) => {
        const xs = snapshots.map(s => Date.parse(s.ts) / 1000);
        const passRate = snapshots.map(s => s.pass_rate);
        const defects = snapshots.map(s => s.defect_density * 100);
        new uPlot({
          width: document.getElementById('metrics-history').clientWidth,
          height: 240,
          series: [{}, {label:'Pass %', stroke:'#16a34a'},
                       {label:'Defect density × 100', stroke:'#dc2626'}],
        }, [xs, passRate, defects],
        document.getElementById('metrics-history'));
      });
  </script>
</section>
```

### Backfill

`tools/backfill_metric_snapshots.py` — iterates `list_projects()`, calls
`snapshot_metrics_from_db(pid)`. Documented one-shot.

### Files
- `engine/test_metrics_generator.py` — add 4 functions (~120 LOC)
- `engine/runner_worker.py` — call `snapshot_metrics_from_db(project_id)` after `done.flag` (best-effort)
- `routes/dashboard.py` — new `/metrics/history` route
- `routes/_shared.py` — `_kpi`, `_kpi_defect_density` helpers
- `templates/test_metrics.html` — history section
- `templates/index.html` — small sparkline on project card
- `static/vendor/uplot.iife.min.js` — vendored
- `app.py` — daily catch-up thread after `init_db()`
- `tools/backfill_metric_snapshots.py` — one-shot script

### Risks
- **Detached subprocess DB write.** Doubles lock window. **3.4 must ship before 3.3 in prod.**
- **Routes→engine→routes cycle:** importing `routes.dashboard._compute_dashboard_metrics` from `engine.*` creates a cycle. Extract `_compute_dashboard_metrics` to `engine/test_metrics_generator.py::compute_session_metrics()` and re-export from the route.
- **Multi-worker daily thread** runs N times. On Render `--workers 1`, fine. Multi-worker: gate by env var `TESTFORTGE_SNAPSHOT_WORKER=1` or move to external cron.

**Estimate:** 8 h. **Depends on:** 3.4 (in prod sequence).

---

## Task 3.4 — SQLite locking strategy

### Plan — do both

**A. WAL + busy_timeout** in `engine/db.py::_build_engine` connect listener (line 106):

```python
@event.listens_for(eng, "connect")
def _configure_sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA synchronous=NORMAL")
    cur.execute("PRAGMA busy_timeout=5000")
    cur.close()
```

WAL → snapshot writer + runner worker stop blocking each other on read.
`busy_timeout=5000` → rare write-write collisions wait quietly.

**B. Force Postgres in prod** — `engine/db.py::init_db` guard:

```python
def _assert_prod_safety(url: str) -> None:
    in_debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    if not in_debug and url.startswith("sqlite"):
        msg = ("TestForTge starting with SQLite in non-debug mode. "
               "SQLite OK for unit tests and local dev, NOT production: "
               "concurrent writes from gunicorn workers + detached "
               "runner_worker will deadlock under load. Set "
               "DATABASE_URL=postgresql://... or FLASK_DEBUG=1.")
        if os.environ.get("TESTFORTGE_ALLOW_SQLITE_PROD") == "1":
            log.warning(msg + " Continuing because TESTFORTGE_ALLOW_SQLITE_PROD=1.")
        else:
            raise RuntimeError(msg)
```

Refuse to boot by default; escape hatch for solo-VM self-hosters.

**C. Document in README:** Postgres required in prod; SQLite + WAL OK for dev.

### Files
- `engine/db.py` — extend connect listener + add `_assert_prod_safety`
- `README.md` — Postgres-in-prod note
- `tests/test_sqlite_pragmas.py` — open fresh engine, assert `journal_mode == 'wal'`, `busy_timeout == 5000`
- `tests/test_concurrent_writes.py` — 5 threads × 10 writes, assert no deadlock

### Risks
- **NFS/SMB incompatible with WAL.** Render uses ext4-local, fine. Document.
- **Connection-pool reuse:** `pool_pre_ping=True` already in place.
- **Operators with `FLASK_DEBUG=0` + SQLite local setups** now fail. Loud error tells them what to do.

**Estimate:** 3 h. **Depends on:** nothing. **Blocks:** 3.3 in prod.

---

## Measurable improvements

| Metric | Before | Target | How measured |
|---|---|---|---|
| Tedgie chat p50 time-to-first-token | 2.5 s | 0.5 s | Synthetic probe via `curl -N -w '%{time_starttransfer}'` |
| Tedgie chat p95 wall-clock for AI reply | 6 s | 2.5 s | Same probe |
| Anthropic input-token cost per 100 Tedgie messages | $X | $X × 0.25 | Sum `input_tokens` − 0.9 × `cache_read_input_tokens` from JSON logs |
| Dashboard load on project with 90-day history | n/a | < 250 ms | DevTools Network `/metrics/history` |
| SQLite lock errors in dev logs per week | "occasional" | 0 | grep "database is locked" in JSON logs |
| Prod boot fails fast on misconfigured SQLite | silent corruption risk | RuntimeError within 100 ms | `tests/test_db_safety.py` |
