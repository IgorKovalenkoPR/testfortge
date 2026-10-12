"""TestForTge release load test (Locust).

Run single-process (the stop conditions and the SLO report live in this
process; do not use ``--processes`` / master-worker)::

    pip install -r requirements-loadtest.txt
    LT_ACCOUNTS_CSV=accounts.csv LOADTEST_PASSWORD=... \
    LT_CONFIRM_HOST=https://testforge.qarea.org \
    locust -f tools/loadtest/locustfile.py --headless \
        --host https://testforge.qarea.org --only-summary

The built-in shape runs 1→5 users (2 min) → 10 (5 min hold) → 20 (10 min
hold) → 0 (1 min). ``LT_TIME_SCALE`` multiplies every duration (0.0556 turns
the 18-minute profile into one minute), ``LT_MAX_USERS`` caps the user
count, and ``LT_SHAPE=0`` disables the shape so ``-u/-r/-t`` apply.

Task mix (weights): browse 40, typeahead bursts 25, full search 15,
writes 8, Ask 5, TC generation 4, export/filters 3.

Environment (nothing secret is ever hard-coded):

=========================  ===============================================
``LT_ACCOUNTS_CSV``        ``email[,password][,role][,org]`` rows (the seed's
                           ``--csv`` output); rows of org ``LOADTEST-B`` are
                           skipped. Or ``LT_EMAILS`` = comma-separated list.
``LOADTEST_PASSWORD``      password for rows without one
``LT_CONFIRM_HOST``        must equal ``--host`` for any non-local host
``LT_BIG_PROJECT_ID``      optional; found by name ``LOADTEST big`` otherwise
``LT_GEN_PROJECT_ID``      optional; found by name ``LOADTEST gen scratch``
``LT_TC_COUNT``/``LT_BUG_COUNT``  ids the write task picks from (2000/300)
``ASK_BUDGET``             hard cap on /api/ask calls per run (default 60)
``LT_ASK``/``LT_GEN``/``LT_WRITES``  ``0`` disables that task
``LT_GEN_ENHANCE``         ``1`` sends enhance=1 ("Improve with AI")
``LT_REPORT_DIR``          where ``loadtest_report_<ts>.{json,md}`` go (cwd)
``LT_RSS_LIMIT_MB`` (450) ``LT_5XX_PCT`` (2) ``LT_P95_LIMIT_MS`` (5000)
``LT_P95_HOLD_S`` (120) ``LT_HEALTH_FAILS`` (3) ``LT_HEALTH_INTERVAL_S`` (10)
=========================  ===============================================
"""
from __future__ import annotations

import collections
import csv
import itertools
import json
import logging
import math
import os
import random
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import gevent
import requests
from gevent.lock import BoundedSemaphore
from locust import HttpUser, LoadTestShape, between, events, task
from locust.clients import HttpSession
from locust.stats import StatsEntry

log = logging.getLogger("loadtest")


# ── Configuration ─────────────────────────────────────────────────────

def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_on(name: str, default: bool = True) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


TIME_SCALE = max(0.001, _env_float("LT_TIME_SCALE", 1.0))
MAX_USERS = max(1, _env_int("LT_MAX_USERS", 20))
TC_COUNT = max(1, _env_int("LT_TC_COUNT", 2000))
BUG_COUNT = max(1, _env_int("LT_BUG_COUNT", 300))
ASK_BUDGET = max(0, _env_int("ASK_BUDGET", 60))
ASK_PER_USER_PER_MIN = max(1, _env_int("LT_ASK_PER_USER_PER_MIN", 4))
GEN_TIMEOUT_S = _env_float("LT_GEN_TIMEOUT_S", 180)
GEN_MAX_CONCURRENT = max(1, _env_int("LT_GEN_MAX_CONCURRENT", 2))

STOP = {
    "rss_mb": _env_float("LT_RSS_LIMIT_MB", 450),
    "err_pct": _env_float("LT_5XX_PCT", 2.0),
    "err_window_s": 60.0,
    "p95_ms": _env_float("LT_P95_LIMIT_MS", 5000),
    "p95_hold_s": _env_float("LT_P95_HOLD_S", 120),
    "health_fails": _env_int("LT_HEALTH_FAILS", 3),
    "interval_s": _env_float("LT_HEALTH_INTERVAL_S", 10),
}

#: SLO per category: (name prefix, p95 limit ms).
SLOS = [
    ("pages", "[page]", 800),
    ("typeahead", "[typeahead]", 400),
    ("search", "[search]", 1200),
    ("writes", "[write]", 1000),
    ("ask_first_chunk", "[ask] first chunk", 2500),
]
SLO_5XX_PCT = 0.5

# Query pools. Words come from the seed (tools/loadtest/seed_loadtest.py).
TYPEAHEAD_WORDS = ["login", "password", "payment", "checkout", "registration",
                   "пароль", "оплата", "кошик", "корзина", "сповіщення"]
SEARCH_QUERIES = {
    "en": ["login fails", "password reset email", "payment by credit card",
           "export report csv", "session timeout"],
    "ua": ["вхід у систему", "скидання пароля", "оплата карткою",
           "оформлення замовлення"],
    "ru": ["вход в систему", "сброс пароля", "оплата картой",
           "добавить товар в корзину"],
    "typo": ["pasword reset", "paymnet card", "chekout address", "registraton",
             "notifcation"],
}
ASK_QUESTIONS = [
    "Which test cases cover password reset?",
    "What bugs are open for payment?",
    "Summarise the checkout test coverage",
    "Which login tests failed?",
    "What is tested for session timeout?",
    "Які тести покривають оплату карткою?",
    "Какие баги открыты по корзине?",
    "Which bugs mention error 502?",
    "What does the registration checklist check?",
    "List the export to CSV test cases",
]


# ── Accounts ──────────────────────────────────────────────────────────

def load_accounts() -> list[tuple[str, str]]:
    default_pw = os.environ.get("LOADTEST_PASSWORD") or ""
    rows: list[tuple[str, str]] = []
    path = os.environ.get("LT_ACCOUNTS_CSV")
    if path:
        with open(path, newline="", encoding="utf-8") as fh:
            for rec in csv.DictReader(fh):
                email = (rec.get("email") or "").strip()
                if not email or (rec.get("org") or "").strip() == "LOADTEST-B":
                    continue
                rows.append((email, (rec.get("password") or "").strip()
                             or default_pw))
    for email in (os.environ.get("LT_EMAILS") or "").split(","):
        if email.strip():
            rows.append((email.strip(), default_pw))
    rows = [(e, p) for e, p in rows if p]
    return rows


_ACCOUNTS: list[tuple[str, str]] = []
_ACCOUNT_CYCLE = None


def next_account() -> tuple[str, str]:
    global _ACCOUNTS, _ACCOUNT_CYCLE
    if _ACCOUNT_CYCLE is None:
        _ACCOUNTS = load_accounts()
        if not _ACCOUNTS:
            raise RuntimeError(
                "no test accounts: set LT_ACCOUNTS_CSV (or LT_EMAILS) and "
                "LOADTEST_PASSWORD")
        _ACCOUNT_CYCLE = itertools.cycle(_ACCOUNTS)
    return next(_ACCOUNT_CYCLE)


# ── Shared run state ──────────────────────────────────────────────────

STATE = {
    "stop_reason": None,
    "ask_used": 0,
    "ask_disabled": None if _env_on("LT_ASK") else "LT_ASK=0",
    "gen_disabled": None if _env_on("LT_GEN") else "LT_GEN=0",
    "writes_disabled": None if _env_on("LT_WRITES") else "LT_WRITES=0",
    "counters": collections.Counter(),
    "health": [],          # samples from /healthz
    "rss_exposed": None,   # learned from the first /healthz body
    "requests_total": 0,
    "requests_5xx": 0,
    "started_at": None,
    "project_ids": {},
}
_WINDOW: collections.deque = collections.deque()   # (t, ms, is5xx, name)
_GEN_SLOTS = BoundedSemaphore(GEN_MAX_CONCURRENT)

#: Excluded from the stop-condition latency guard: setup, the streamed Ask
#: (an LLM call), and the generation job's end-to-end time.
_GUARD_EXCLUDE = ("[setup]", "[ask]", "[gen] completion")

_CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
_CSRF_INPUT = re.compile(r'name="csrf_token" value="([^"]+)"')


def _is_local(host: str) -> bool:
    h = (urlparse(host).hostname or "").lower()
    return h in ("localhost", "127.0.0.1", "::1") or h.endswith(".localhost")


# ── Users ─────────────────────────────────────────────────────────────

class _Session:
    """Sign-in, CSRF and project selection on one HttpSession."""

    def __init__(self, client: HttpSession):
        self.client = client
        self.csrf = ""

    def login(self, email: str, password: str) -> bool:
        r = self.client.get("/auth/login", name="[setup] login form")
        m = _CSRF_INPUT.search(r.text or "")
        if not m:
            STATE["counters"]["login_no_csrf"] += 1
            return False
        with self.client.post("/auth/login", name="[setup] login",
                              data={"email": email, "password": password,
                                    "csrf_token": m.group(1), "next": "/"},
                              allow_redirects=False,
                              catch_response=True) as resp:
            loc = resp.headers.get("Location", "")
            if resp.status_code in (302, 303) and "/auth/login" not in loc:
                resp.success()
            else:
                resp.failure(f"login refused ({resp.status_code})")
                STATE["counters"]["login_failed"] += 1
                return False
        return self.refresh_csrf()

    def refresh_csrf(self, path: str = "/") -> bool:
        r = self.client.get(path, name="[setup] csrf")
        m = _CSRF_META.search(r.text or "")
        self.csrf = m.group(1) if m else ""
        return bool(self.csrf)

    def find_project(self, name: str, env: str) -> str:
        pid = os.environ.get(env) or STATE["project_ids"].get(name)
        if pid:
            return pid
        r = self.client.get("/test-cases", name="[setup] project lookup")
        m = re.search(r'<option value="([0-9a-f]{32})"[^>]*>\s*'
                      + re.escape(name) + r"\s*(?:<|\(|\n)", r.text or "")
        if m:
            STATE["project_ids"][name] = m.group(1)
            return m.group(1)
        return ""

    def select(self, pid: str) -> bool:
        with self.client.post(f"/projects/db/select/{pid}",
                              name="[setup] select project",
                              data={"csrf_token": self.csrf, "next": "/"},
                              allow_redirects=False,
                              catch_response=True) as resp:
            if resp.status_code in (302, 303):
                resp.success()
                return True
            resp.failure(f"select project {resp.status_code}")
            return False

    def headers(self) -> dict:
        return {"X-CSRFToken": self.csrf, "Accept": "application/json"}


class PlatformUser(HttpUser):
    wait_time = between(1, 3)

    def on_start(self):
        self.ready = False
        self.gen = None
        self.ask_times = collections.deque()
        if not _is_local(self.host or ""):
            confirm = (os.environ.get("LT_CONFIRM_HOST") or "").rstrip("/")
            if confirm != (self.host or "").rstrip("/"):
                STATE["stop_reason"] = (
                    "LT_CONFIRM_HOST does not match --host; refusing to "
                    "load a non-local host without explicit confirmation")
                log.error(STATE["stop_reason"])
                self.environment.runner.quit()
                return
        self.email, self.password = next_account()
        self.s = _Session(self.client)
        self.ready = self.s.login(self.email, self.password)
        self.big = self.s.find_project("LOADTEST big",
                                       "LT_BIG_PROJECT_ID") if self.ready else ""
        if self.big:
            self.s.select(self.big)
            self.s.refresh_csrf()
        else:
            STATE["counters"]["no_big_project"] += 1

    # ── 40%: browse ──
    @task(40)
    def browse(self):
        if not self.ready:
            return
        path, name = random.choice([
            ("/", "[page] dashboard"),
            ("/test-cases", "[page] test cases"),
            ("/checklist", "[page] checklist"),
            ("/bug-reports", "[page] bug reports"),
        ])
        with self.client.get(path, name=name, catch_response=True) as r:
            if "/auth/login" in (r.url or ""):
                r.failure("session lost (redirected to login)")
            elif r.status_code == 200:
                r.success()
        if random.random() < 0.5:
            page = {"/": "_default", "/test-cases": "test_cases",
                    "/checklist": "checklist",
                    "/bug-reports": "bug_reports"}[path]
            self.client.get(f"/api/hints?page={page}", name="[hints] /api/hints",
                            headers={"Accept": "application/json"})

    # ── 25%: typeahead burst ──
    @task(25)
    def typeahead(self):
        if not self.ready:
            return
        word = random.choice(TYPEAHEAD_WORDS)
        n = random.randint(3, 5)
        start = max(2, len(word) - n + 1)
        for k in range(start, start + n):
            q = word[:k] if k <= len(word) else word
            self.client.get("/api/search", params={"q": q, "quick": "1"},
                            name="[typeahead] /api/search?quick=1",
                            headers={"Accept": "application/json"})
            gevent.sleep(0.2)

    # ── 15%: full search ──
    @task(15)
    def search(self):
        if not self.ready:
            return
        lang = random.choice(list(SEARCH_QUERIES))
        q = random.choice(SEARCH_QUERIES[lang])
        with self.client.get("/api/search", params={"q": q},
                             name="[search] /api/search",
                             headers={"Accept": "application/json"},
                             catch_response=True) as r:
            if r.status_code != 200:
                return
            try:
                body = r.json()
            except ValueError:
                r.failure("search: not JSON")
                return
            if "results" not in body:
                r.failure("search: no results key")
            else:
                STATE["counters"][f"search_{lang}_"
                                  f"{'hit' if body['results'] else 'empty'}"] += 1
                r.success()

    # ── 8%: writes ──
    @task(8)
    def write(self):
        if not self.ready or STATE["writes_disabled"]:
            return
        if random.random() < 0.5:
            tc = f"TC-{random.randint(1, TC_COUNT):04d}"
            self._patch("test_case", tc,
                        {"comment": f"loadtest {int(time.time())}"},
                        "[write] PATCH test_case")
        else:
            bug = f"BUG-{random.randint(1, BUG_COUNT):04d}"
            r = self.client.get(f"/api/edit/bug/{bug}",
                                name="[write-read] GET bug",
                                headers=self.s.headers())
            if r.status_code != 200:
                return
            item = (r.json() or {}).get("item") or {}
            cur = item.get("status") or "Open"
            new = {"Open": "In Progress", "In Progress": "Open",
                   "Resolved": "In Progress", "Reopened": "In Progress",
                   "Closed": None}.get(cur)
            if new is None:
                return
            self._patch("bug", bug, {"status": new}, "[write] PATCH bug status",
                        version=item.get("row_version"))

    def _patch(self, entity, eid, changes, name, version=None):
        payload = {"changes": changes}
        if version is not None:
            payload["row_version"] = version
        with self.client.patch(f"/api/edit/{entity}/{eid}", json=payload,
                               name=name, headers=self.s.headers(),
                               catch_response=True) as r:
            if r.status_code == 409:
                STATE["counters"]["write_conflict_409"] += 1
                r.success()        # a concurrent edit, correctly refused
            elif r.status_code == 404 and "editors_disabled" in (r.text or ""):
                STATE["writes_disabled"] = "EDITORS_ENABLED is off"
                r.failure("editors disabled")
            elif r.status_code == 400 and "CSRF" in (r.text or ""):
                self.s.refresh_csrf()
                r.failure("csrf expired")

    # ── 5%: Ask ──
    @task(5)
    def ask(self):
        if not self.ready or STATE["ask_disabled"]:
            return
        now = time.time()
        while self.ask_times and now - self.ask_times[0] > 60:
            self.ask_times.popleft()
        if len(self.ask_times) >= ASK_PER_USER_PER_MIN:
            STATE["counters"]["ask_skipped_user_rate"] += 1
            return
        if STATE["ask_used"] >= ASK_BUDGET:
            STATE["counters"]["ask_skipped_budget"] += 1
            return
        STATE["ask_used"] += 1
        self.ask_times.append(now)
        q = random.choice(ASK_QUESTIONS)
        t0 = time.perf_counter()
        first_event = first_text = None
        codes, cached = [], None
        try:
            with self.client.post("/api/ask", json={"q": q},
                                  name="[ask] /api/ask (stream)",
                                  headers={**self.s.headers(),
                                           "Accept": "application/x-ndjson"},
                                  stream=True, catch_response=True) as r:
                if r.status_code == 404:
                    STATE["ask_disabled"] = "PLATFORM_SEARCH_AI off (404)"
                    r.success()
                    return
                if r.status_code != 200:
                    r.failure(f"ask {r.status_code}")
                    return
                for line in r.iter_lines(decode_unicode=True):
                    if not line:
                        continue
                    if first_event is None:
                        first_event = (time.perf_counter() - t0) * 1000
                    ev = json.loads(line)
                    typ = ev.get("type")
                    if typ == "meta":
                        cached = ev.get("cached")
                    elif typ == "text" and first_text is None:
                        first_text = (time.perf_counter() - t0) * 1000
                    elif typ == "error":
                        codes.append(ev.get("code"))
                r.success()
        except Exception as exc:   # noqa: BLE001 — a broken stream
            events.request.fire(request_type="ASK", name="[ask] stream error",
                                response_time=(time.perf_counter() - t0) * 1000,
                                response_length=0, exception=exc, context={},
                                response=None)
            return
        for code in codes:
            STATE["counters"][f"ask_error_{code}"] += 1
        if "no_key" in codes:
            STATE["ask_disabled"] = "no_key (no Anthropic key configured)"
            log.warning("Ask disabled for the rest of the run: no_key")
        STATE["counters"][f"ask_cached_{bool(cached)}"] += 1
        if first_text is not None:
            events.request.fire(request_type="ASK", name="[ask] first chunk",
                                response_time=first_text, response_length=0,
                                exception=None, context={}, response=None)
        elif first_event is not None:
            events.request.fire(request_type="ASK",
                                name="[ask] first event (no text)",
                                response_time=first_event, response_length=0,
                                exception=None, context={}, response=None)

    # ── 4%: TC generation (async job) ──
    @task(4)
    def generate(self):
        if not self.ready or STATE["gen_disabled"]:
            return
        if not _GEN_SLOTS.acquire(blocking=False):
            STATE["counters"]["gen_skipped_busy"] += 1
            return
        try:
            self._generate()
        finally:
            _GEN_SLOTS.release()

    def _generate(self):
        if self.gen is None:
            client = HttpSession(base_url=self.host,
                                 request_event=self.environment.events.request,
                                 user=self)
            gen = _Session(client)
            if not gen.login(self.email, self.password):
                return
            pid = gen.find_project("LOADTEST gen scratch", "LT_GEN_PROJECT_ID")
            if not pid:
                STATE["gen_disabled"] = "no 'LOADTEST gen scratch' project"
                return
            gen.select(pid)
            gen.refresh_csrf("/test-cases")
            self.gen = gen
        g = self.gen
        data = {"input_text": "As a user I can reset my password by email.",
                "csrf_token": g.csrf,
                "enhance": "1" if _env_on("LT_GEN_ENHANCE", False) else "0"}
        t0 = time.perf_counter()
        with g.client.post("/test-cases/run-async", data=data,
                           name="[gen] submit", headers=g.headers(),
                           catch_response=True) as r:
            if r.status_code == 429:
                STATE["counters"]["gen_429"] += 1
                r.success()
                return
            if r.status_code != 200:
                r.failure(f"gen submit {r.status_code}")
                return
            job_id = (r.json() or {}).get("job_id")
        if not job_id:
            return
        status, err = "pending", None
        while time.perf_counter() - t0 < GEN_TIMEOUT_S:
            gevent.sleep(2)
            p = g.client.get(f"/test-cases/status/{job_id}",
                             name="[gen] poll", headers=g.headers())
            if p.status_code != 200:
                status, err = "lost", f"poll {p.status_code}"
                break
            body = p.json() or {}
            status = body.get("status")
            if status in ("done", "failed"):
                err = body.get("error")
                break
        ms = (time.perf_counter() - t0) * 1000
        exc = None if status == "done" else RuntimeError(
            f"gen {status}: {err or 'timeout'}"[:200])
        STATE["counters"][f"gen_{status}"] += 1
        events.request.fire(request_type="GEN", name="[gen] completion",
                            response_time=ms, response_length=0,
                            exception=exc, context={}, response=None)

    # ── 3%: export / filters ──
    @task(3)
    def export(self):
        if not self.ready:
            return
        path, name = random.choice([
            ("/export-bug-reports.csv", "[export] bug reports csv"),
            ("/export/markdown", "[export] markdown pack"),
            ("/dashboard/export.csv", "[export] dashboard csv"),
            ("/bug-reports?run=latest", "[filter] bug reports run=latest"),
            ("/bug-reports?source=manual", "[filter] bug reports source"),
        ])
        self.client.get(path, name=name)


# ── Load shape ────────────────────────────────────────────────────────

if _env_on("LT_SHAPE", True):
    class ReleaseShape(LoadTestShape):
        """1→5 (2 min) → 10 (5 min hold) → 20 (10 min hold) → 0 (1 min),
        every duration × ``LT_TIME_SCALE``, users capped at ``LT_MAX_USERS``.
        """
        stages = [(120, 1, 5), (300, 10, 10), (600, 20, 20), (60, 20, 0)]

        def tick(self):
            t = self.get_run_time() / TIME_SCALE
            if STATE["stop_reason"]:
                return None
            for dur, a, b in self.stages:
                if t < dur:
                    users = a + (b - a) * (t / dur)
                    users = math.ceil(users) if b >= a else math.floor(users)
                    return min(MAX_USERS, max(0 if b == 0 else 1,
                                              int(users))), 2
                t -= dur
            return None


# ── Stop conditions ───────────────────────────────────────────────────

@events.request.add_listener
def _on_request(request_type, name, response_time, response_length,
                response=None, exception=None, **_kw):
    status = getattr(response, "status_code", None) or 0
    is5xx = status >= 500
    STATE["requests_total"] += 1
    if is5xx:
        STATE["requests_5xx"] += 1
    _WINDOW.append((time.time(), response_time or 0.0, is5xx, name))


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    return values[min(len(values) - 1, int(math.ceil(0.95 * len(values))) - 1)]


def _quit(environment, reason: str) -> None:
    if STATE["stop_reason"]:
        return
    STATE["stop_reason"] = reason
    log.error("STOP CONDITION: %s — stopping the run", reason)
    environment.process_exit_code = 2
    environment.runner.quit()


def _rss_mb(body: dict):
    for key in ("rss_mb", "memory_rss_mb", "rss"):
        if key in body:
            return float(body[key])
    mem = body.get("memory")
    if isinstance(mem, dict):
        for key in ("rss_mb", "rss"):
            if key in mem:
                return float(mem[key])
    return None


def _guard(environment):
    host = (environment.host or "").rstrip("/")
    fails, p95_since, last_uptime = 0, None, None
    while not STATE["stop_reason"]:
        gevent.sleep(STOP["interval_s"])
        now = time.time()
        # /healthz
        sample = {"t": round(now - (STATE["started_at"] or now), 1)}
        try:
            r = requests.get(host + "/healthz", timeout=5)
            body = r.json() if r.headers.get("content-type", "").startswith(
                "application/json") else {}
            ok = r.status_code == 200
            sample.update(status=r.status_code,
                          uptime=body.get("uptime_seconds"))
            rss = _rss_mb(body)
            if STATE["rss_exposed"] is None:
                STATE["rss_exposed"] = rss is not None
                if rss is None:
                    log.warning("/healthz exposes no RSS figure; the %d MB "
                                "memory stop condition is inactive",
                                STOP["rss_mb"])
            if rss is not None:
                sample["rss_mb"] = rss
                if rss > STOP["rss_mb"]:
                    _quit(environment, f"RSS {rss:.0f} MB > {STOP['rss_mb']:.0f}")
            up = body.get("uptime_seconds")
            if isinstance(up, (int, float)) and last_uptime is not None \
                    and up + 5 < last_uptime:
                _quit(environment, f"/healthz uptime went backwards "
                                   f"({last_uptime:.0f}s → {up:.0f}s): the "
                                   f"process restarted (OOM kill?)")
            if isinstance(up, (int, float)):
                last_uptime = up
        except Exception as exc:   # noqa: BLE001
            ok = False
            sample["error"] = type(exc).__name__
        STATE["health"].append(sample)
        fails = 0 if ok else fails + 1
        if fails >= STOP["health_fails"]:
            _quit(environment, f"{fails} consecutive /healthz failures")
        # Window statistics.
        while _WINDOW and now - _WINDOW[0][0] > STOP["err_window_s"]:
            _WINDOW.popleft()
        win = [w for w in _WINDOW if not w[3].startswith(_GUARD_EXCLUDE)]
        if len(win) >= 20:
            pct = 100.0 * sum(1 for w in win if w[2]) / len(win)
            if pct > STOP["err_pct"]:
                _quit(environment, f"5xx {pct:.1f}% over the last "
                                   f"{STOP['err_window_s']:.0f}s "
                                   f"(> {STOP['err_pct']}%)")
            p95 = _p95([w[1] for w in win])
            if p95 > STOP["p95_ms"]:
                p95_since = p95_since or now
                if now - p95_since >= STOP["p95_hold_s"]:
                    _quit(environment, f"p95 {p95:.0f} ms > "
                                       f"{STOP['p95_ms']:.0f} ms for "
                                       f"{now - p95_since:.0f}s")
            else:
                p95_since = None


@events.test_start.add_listener
def _on_start(environment, **_kw):
    STATE["started_at"] = time.time()
    if not _is_local(environment.host or "") and \
            (os.environ.get("LT_CONFIRM_HOST") or "").rstrip("/") != \
            (environment.host or "").rstrip("/"):
        # Not even the /healthz poller may touch an unconfirmed host.
        STATE["stop_reason"] = ("LT_CONFIRM_HOST does not match --host; "
                                "refusing to load a non-local host")
        log.error(STATE["stop_reason"])
        environment.process_exit_code = 2
        gevent.spawn_later(0.1, environment.runner.quit)
        return
    gevent.spawn(_guard, environment)


# ── SLO report ────────────────────────────────────────────────────────

def _category(environment, prefix: str) -> StatsEntry:
    agg = StatsEntry(environment.stats, prefix, "", use_response_times_cache=False)
    for entry in environment.stats.entries.values():
        if entry.name.startswith(prefix):
            agg.extend(entry)
    return agg


def build_report(environment) -> dict:
    cats, verdicts = [], []
    for key, prefix, limit in SLOS:
        e = _category(environment, prefix)
        if e.num_requests == 0:
            verdict = "NO DATA"
        else:
            p95 = e.get_response_time_percentile(0.95)
            verdict = "PASS" if p95 <= limit else "FAIL"
        verdicts.append(verdict)
        cats.append({
            "category": key, "prefix": prefix, "slo_p95_ms": limit,
            "count": e.num_requests, "failures": e.num_failures,
            "p50_ms": e.get_response_time_percentile(0.5) if e.num_requests else None,
            "p95_ms": e.get_response_time_percentile(0.95) if e.num_requests else None,
            "max_ms": round(e.max_response_time, 1) if e.num_requests else None,
            "verdict": verdict,
        })
    total = max(1, STATE["requests_total"])
    pct5xx = 100.0 * STATE["requests_5xx"] / total
    v5 = "PASS" if pct5xx < SLO_5XX_PCT else "FAIL"
    verdicts.append(v5)
    overall = ("FAIL" if "FAIL" in verdicts or STATE["stop_reason"]
               else "INCOMPLETE" if "NO DATA" in verdicts else "PASS")
    per_name = sorted((
        {"name": e.name, "method": e.method, "count": e.num_requests,
         "failures": e.num_failures,
         "p50_ms": e.get_response_time_percentile(0.5),
         "p95_ms": e.get_response_time_percentile(0.95),
         "max_ms": round(e.max_response_time, 1)}
        for e in environment.stats.entries.values() if e.num_requests),
        key=lambda d: d["name"])
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "host": environment.host,
        "duration_s": round(time.time() - (STATE["started_at"] or time.time()), 1),
        "overall": overall,
        "stop_reason": STATE["stop_reason"],
        "slo": cats,
        "five_xx": {"count": STATE["requests_5xx"],
                    "total": STATE["requests_total"],
                    "pct": round(pct5xx, 3), "slo_pct": SLO_5XX_PCT,
                    "verdict": v5},
        "ask": {"used": STATE["ask_used"], "budget": ASK_BUDGET,
                "disabled": STATE["ask_disabled"]},
        "gen_disabled": STATE["gen_disabled"],
        "writes_disabled": STATE["writes_disabled"],
        "rss_exposed": STATE["rss_exposed"],
        "counters": dict(STATE["counters"]),
        "health": STATE["health"],
        "requests": per_name,
        "config": {"time_scale": TIME_SCALE, "max_users": MAX_USERS,
                   "stop": STOP},
    }


def report_markdown(rep: dict) -> str:
    lines = [f"# Load test report — {rep['generated_at']}", "",
             f"Host: `{rep['host']}`  ·  duration {rep['duration_s']} s  ·  "
             f"**overall: {rep['overall']}**", ""]
    if rep["stop_reason"]:
        lines += [f"**Stopped early:** {rep['stop_reason']}", ""]
    lines += ["| Category | SLO p95 | count | fails | p50 | p95 | max | verdict |",
              "|---|---:|---:|---:|---:|---:|---:|---|"]
    for c in rep["slo"]:
        def f(v):
            return "—" if v is None else f"{v:.0f}"
        lines.append(f"| {c['category']} | {c['slo_p95_ms']} ms | {c['count']} | "
                     f"{c['failures']} | {f(c['p50_ms'])} | {f(c['p95_ms'])} | "
                     f"{f(c['max_ms'])} | {c['verdict']} |")
    x = rep["five_xx"]
    lines += ["", f"5xx: {x['count']}/{x['total']} = {x['pct']}% "
                  f"(SLO < {x['slo_pct']}%) — {x['verdict']}", "",
              f"Ask: {rep['ask']['used']}/{rep['ask']['budget']} used; "
              f"disabled: {rep['ask']['disabled'] or 'no'}  ·  "
              f"generation disabled: {rep['gen_disabled'] or 'no'}  ·  "
              f"writes disabled: {rep['writes_disabled'] or 'no'}  ·  "
              f"RSS exposed by /healthz: {rep['rss_exposed']}", "",
              "Counters: " + ", ".join(f"{k}={v}" for k, v in
                                       sorted(rep["counters"].items())), "",
              "## Per request", "",
              "| name | count | fails | p50 | p95 | max |",
              "|---|---:|---:|---:|---:|---:|"]
    for r in rep["requests"]:
        lines.append(f"| {r['name']} | {r['count']} | {r['failures']} | "
                     f"{r['p50_ms']:.0f} | {r['p95_ms']:.0f} | {r['max_ms']:.0f} |")
    return "\n".join(lines) + "\n"


@events.test_stop.add_listener
def _on_stop(environment, **_kw):
    # test_stop fires again during shutdown after a runner.quit(); one
    # report per run.
    if STATE.get("report_written"):
        return
    STATE["report_written"] = True
    rep = build_report(environment)
    out_dir = os.environ.get("LT_REPORT_DIR") or os.getcwd()
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = os.path.join(out_dir, f"loadtest_report_{stamp}")
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump(rep, fh, indent=2, ensure_ascii=False, default=str)
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write(report_markdown(rep))
    log.warning("SLO report: %s.{json,md} — overall %s%s", base, rep["overall"],
                f" (stopped: {rep['stop_reason']})" if rep["stop_reason"] else "")
    if rep["overall"] == "FAIL" and not environment.process_exit_code:
        environment.process_exit_code = 1
