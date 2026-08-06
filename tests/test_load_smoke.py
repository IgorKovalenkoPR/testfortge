"""E9.7 — ten people in one organisation, at the same moment.

A smoke, deliberately, and the strategy says so out loud: there is no
sustained-load or soak testing in this programme and on a free tier there
would be nowhere to run it. What this answers is the narrower question
the architecture actually raises — **does the product still behave when
ten colleagues are in the same organisation at once?** Everything in this
system is scoped to an organisation: the project picker, the run limit,
the bug list, the dashboard. Ten sessions writing into one scope is the
concurrency the design invites, and until now nothing had ever done it.

Three properties, in the order they matter:

1. **Nothing fails.** No 5xx, no ``database is locked``, no deadlock. On
   SQLite that is what WAL and ``busy_timeout`` are for and this is the
   only test that puts them under real pressure through HTTP.
2. **Nothing is lost.** Ten people filing one bug each leaves ten bugs.
   A lost update under concurrency is silent by construction — the
   request that lost still answered 200.
3. **It stays usable.** p95 under a stated budget.

Real HTTP against a real server, not the Flask test client: the client
runs the view in the calling thread, which is a fine way to test a view
and a useless way to test concurrency.
"""
from __future__ import annotations

import os
import secrets
import socket
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest
import requests

from engine import auth as _auth
from engine import db as _db

#: The programme's number, from the E9 task list.
USERS = 10

#: Requests each person makes: read the dashboard, read the pack, read
#: the bug list, file a bug, re-read the bug list. Modelled on what a
#: tester's minute actually looks like rather than on one endpoint
#: hammered, because a single-endpoint benchmark measures that endpoint
#: and this is meant to measure the organisation as a shared scope.
JOURNEY = ("dashboard", "test cases", "bug list", "file a bug", "bug list")

#: p95 budget for a page, in milliseconds.
#:
#: Measured rather than wished for: **185 ms** on this machine, ten
#: concurrent journeys, 2026-08-06 (p50 101 ms, mean 125 ms — the run
#: prints its own numbers, see the last test in the class). The budget is
#: set at eight times the measured p95, which is not generosity but the
#: gap between a laptop and a shared CI runner, and it still fails on the
#: regressions worth catching: an N+1 across the bug list, a per-request
#: LLM call, a lock somebody widened. It would not notice a 20% slowdown,
#: and is not meant to — that is a benchmark, and a benchmark on a free
#: runner is a flaky test with a graph.
#:
#: Override for a slower box:
#:
#:     TFG_LOAD_P95_MS=4000 pytest tests/test_load_smoke.py
BUDGET_MS = int(os.environ.get("TFG_LOAD_P95_MS", "1500"))

#: Absolute ceiling on *any* single request, sign-in included.
#:
#: Separate from the budget because signing in is deliberately expensive —
#: Argon2 is slow on purpose, and ten verifications landing together took
#: 3.0 s at p95 here. That is the password hash doing its job, not the app
#: being slow, so it gets its own line rather than being allowed to set
#: the page budget. What this catches is the other thing: a request that
#: never comes back, which is a person who has closed the tab.
HANG_MS = int(os.environ.get("TFG_LOAD_HANG_MS", "20000"))

PASSWORD = "Load-Smoke-Suite-4471!"


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """The real app, threaded, with authentication on.

    ``threaded=True`` is the point of the whole file: with a single
    worker the ten journeys would queue in the socket and every property
    below would be measured on a system that was never concurrent.
    """
    from werkzeug.serving import make_server
    from app import app

    previous = {k: os.environ.get(k) for k in ("AUTH_ENABLED", "ORG_MODE")}
    os.environ["AUTH_ENABLED"] = "1"
    os.environ["ORG_MODE"] = "1"
    prior_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["TESTING"] = True
    app.config["WTF_CSRF_ENABLED"] = True
    _db.init_db()

    port = _free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:                                   # pragma: no cover — bind failed
        server.shutdown()
        pytest.skip("the Flask test server did not start")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        app.config["WTF_CSRF_ENABLED"] = prior_csrf
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="module")
def crowd():
    """One organisation, one project, ten accounts that can sign in."""
    tag = secrets.token_hex(4)
    org = _db.create_organization(f"Load {tag}")
    project = _db.upsert_project(f"Load Project {tag}", org_id=org)
    _db.save_test_cases(project, [
        {"id": f"SC1_{n:03d}", "section": "Load", "section_num": 1,
         "summary": f"Verify that item {n} behaves",
         "preconditions": "", "test_steps": "1. Look",
         "test_data": "", "expected_result": "It behaves"}
        for n in range(1, 11)])

    people = []
    # One password hash, ten accounts: Argon2 is deliberately slow, and
    # hashing it ten times would put four seconds of setup in front of a
    # test whose subject is timing.
    shared_hash = _auth.hash_password(PASSWORD)
    for n in range(USERS):
        email = f"load-{tag}-{n}@load.test"
        uid = _db.create_user(email, display_name=f"Tester {n}",
                              password_hash=shared_hash, email_verified=True)
        _db.add_org_member(org, uid, "user")
        people.append(email)
    return {"org": org, "project": project, "tag": tag, "people": people}


class Timed:
    """One request's outcome: what it was, how long, what came back."""

    __slots__ = ("label", "ms", "status", "url")

    def __init__(self, label: str, ms: float, status: int, url: str):
        self.label, self.ms, self.status, self.url = label, ms, status, url

    def __repr__(self) -> str:                  # pragma: no cover — failures
        return f"{self.label} {self.status} in {self.ms:.0f}ms ({self.url})"


def _journey(base_url: str, email: str, project_id: str, tag: str,
             index: int, filing_gate: threading.Barrier) -> list[Timed]:
    """One person's minute, timed request by request."""
    session = requests.Session()
    timings: list[Timed] = []

    def call(label: str, method: str, path: str, **kwargs) -> Timed:
        started = time.perf_counter()
        response = session.request(method, f"{base_url}{path}",
                                   timeout=60, **kwargs)
        record = Timed(label, (time.perf_counter() - started) * 1000,
                       response.status_code, path)
        timings.append(record)
        return record

    def token() -> str:
        return session.get(f"{base_url}/api/csrf-token",
                           timeout=30).json()["token"]

    # Signing in is part of the load — ten Argon2 verifications landing at
    # once is a real spike, and it is the one the free plan feels first.
    call("sign in", "POST", "/auth/login",
         data={"email": email, "password": PASSWORD,
               "csrf_token": token()})
    call("switch project", "POST", f"/projects/db/select/{project_id}",
         data={"csrf_token": token()})

    call("dashboard", "GET", "/?lang=en")
    call("test cases", "GET", "/test-cases?lang=en")
    call("bug list", "GET", "/bug-reports?lang=en")
    # Re-synchronise before the write. Starting together is not enough:
    # by this point the ten threads have been staggered by whatever their
    # earlier requests happened to cost, and a filing that lands 40 ms
    # after the previous one has committed is not a concurrent filing.
    # The token is fetched first so the barrier is immediately followed by
    # the POST rather than by a round trip.
    filing_token = token()
    filing_gate.wait(timeout=60)
    call("file a bug", "POST", "/create-bug-report",
         data={"title": f"Concurrent finding {tag}-{index}",
               "severity": "Major", "priority": "High",
               "steps_to_reproduce": "1. Ten people at once",
               "actual_result": "measured",
               "expected_result": "measured",
               "csrf_token": filing_token})
    call("bug list again", "GET", "/bug-reports?lang=en")
    return timings


@pytest.fixture(scope="module")
def measured(live_server, crowd):
    """Ten journeys at once, run once, questioned by every test below.

    One measurement for the whole file rather than one per test: the run
    is the expensive part, and eight tests each triggering their own would
    measure eight different systems — which is also how a percentile
    assertion and a lost-write assertion end up disagreeing about what
    happened.
    """
    gate = threading.Barrier(USERS)
    filing_gate = threading.Barrier(USERS)

    def _go(index: int) -> list[Timed]:
        # Everyone starts together. Without the barrier the pool staggers
        # them by however long a thread takes to spin up, and ten people
        # arriving one after another is not the thing being tested.
        gate.wait(timeout=60)
        return _journey(live_server, crowd["people"][index],
                        crowd["project"], crowd["tag"], index, filing_gate)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=USERS) as pool:
        results = list(pool.map(_go, range(USERS)))
    return {
        "timings": [t for one in results for t in one],
        "wall_ms": (time.perf_counter() - started) * 1000,
        "crowd": crowd,
    }


def _p(values: list[float], pct: float) -> float:
    """The ``pct`` percentile, nearest-rank.

    Written out rather than taken from ``statistics.quantiles`` because
    that interpolates, and an interpolated p95 over 70 samples is a number
    no request actually took.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, int(round(pct / 100 * len(ordered))))
    return ordered[min(rank, len(ordered)) - 1]


class TestTenPeopleInOneOrganisation:

    def test_the_measurement_happened(self, measured):
        """A run that quietly did nothing would satisfy every assertion
        below, because every one of them is about an absence."""
        timings = measured["timings"]
        assert len(timings) == USERS * 7, len(timings)
        assert {t.label for t in timings} >= set(JOURNEY)

    def test_nothing_failed(self, measured):
        """No 5xx anywhere.

        This is where ``database is locked`` would appear on SQLite, and
        where a deadlock would appear on Postgres. Both surface as a 500
        from a view that did nothing wrong, which is why the assertion is
        on the whole run rather than on one endpoint.
        """
        broken = [t for t in measured["timings"] if t.status >= 500]
        assert not broken, broken

    def test_every_request_was_answered_by_the_thing_it_asked_for(
            self, measured):
        """A 4xx here is not "handled" — it is the load having pushed
        somebody out of their own session, or a token round trip having
        lost a race with another thread's session write."""
        refused = [t for t in measured["timings"] if t.status >= 400]
        assert not refused, refused

    def test_no_one_s_bug_was_lost(self, measured):
        """Ten filings, ten bugs.

        The property with no visible symptom: a lost write answers 200 and
        redirects to a list that looks plausible, and the person who filed
        it has no reason to check.
        """
        crowd = measured["crowd"]
        titles = {b["title"] for b in _db.list_bugs(crowd["project"])}
        expected = {f"Concurrent finding {crowd['tag']}-{i}"
                    for i in range(USERS)}
        assert expected <= titles, sorted(expected - titles)

    def test_p95_is_within_budget(self, measured):
        page_loads = [t.ms for t in measured["timings"]
                      if t.label in JOURNEY]
        p95 = _p(page_loads, 95)
        p50 = _p(page_loads, 50)
        assert p95 <= BUDGET_MS, (
            f"p95 {p95:.0f}ms over the {BUDGET_MS}ms budget "
            f"(p50 {p50:.0f}ms, worst {max(page_loads):.0f}ms, "
            f"{len(page_loads)} samples across {USERS} concurrent people)")

    def test_no_single_request_hangs(self, measured):
        """p95 hides one request in twenty, and one that never returns is
        a person who has closed the tab.

        Every request, sign-in included — this is the only assertion that
        covers the expensive one, and a password verification that stopped
        finishing would otherwise be invisible here.
        """
        worst = max(measured["timings"], key=lambda t: t.ms)
        assert worst.ms <= HANG_MS, worst

    def test_the_numbers_are_reported_even_when_green(self, measured,
                                                      capsys):
        """A gate with no reading is a gate nobody can tune.

        Printed rather than asserted: the point is that the next person to
        change the budget can see what it is being set against, in the
        run's own output, without re-deriving it.
        """
        by_label: dict[str, list[float]] = {}
        for t in measured["timings"]:
            by_label.setdefault(t.label, []).append(t.ms)
        with capsys.disabled():
            print(f"\n  E9.7 — {USERS} concurrent users, one organisation, "
                  f"{measured['wall_ms']:.0f}ms wall clock")
            for label in ("sign in", "switch project", *JOURNEY):
                samples = by_label.get(label) or []
                if not samples:
                    continue
                print(f"    {label:<16} n={len(samples):<3} "
                      f"p50={_p(samples, 50):>7.0f}ms  "
                      f"p95={_p(samples, 95):>7.0f}ms  "
                      f"max={max(samples):>7.0f}ms")
            page_loads = [t.ms for t in measured["timings"]
                          if t.label in JOURNEY]
            print(f"    {'PAGES p95':<16} {_p(page_loads, 95):.0f}ms "
                  f"(budget {BUDGET_MS}ms, "
                  f"mean {statistics.mean(page_loads):.0f}ms)")
        assert True


class TestPublicIdsUnderConcurrency:
    """Ten bugs filed at the same instant, and what they end up called.

    Worth its own class because the id is not decoration. It is what a
    person cites — "reopening BUG-004" — so two rows answering to one id
    means the citation names two findings and the reader cannot tell
    which. E4.4a made exactly that impossible for test cases and
    checklist items: a unique ``(project_id, external_id)`` index, and a
    retry in ``editable.create`` that takes the next number when the
    index refuses. **Bug reports were never brought under it.**

    ``create_bug_report`` still mints "one past the highest" by reading
    the project's bugs and then writing, with nothing between the read
    and the write. Nothing in the schema prevents the collision; what
    prevents it today is timing.

    So this is measured rather than reasoned about. Ten filings released
    from one barrier, seven runs on 2026-08-06: **no collision, ever** —
    SQLite's single writer plus per-request overhead staggers ten
    filings enough, at ten. That is a fact about this engine at this
    scale, not a property of the code, which is why the assertion stays:
    if a faster engine, a bigger team or a cheaper request path ever
    closes the gap, this is what says so instead of a client noticing
    two BUG-004s in a report.
    """

    def test_ten_simultaneous_filings_get_ten_distinct_ids(self, measured,
                                                           capsys):
        crowd = measured["crowd"]
        minted = [b["id"] for b in _db.list_bugs(crowd["project"])
                  if b.get("id")]
        duplicates = len(minted) - len(set(minted))
        with capsys.disabled():
            print(f"    {'public ids':<16} {len(minted)} filed, "
                  f"{len(set(minted))} distinct, {duplicates} collision(s)")
        assert duplicates == 0, sorted(minted)

    def test_every_filing_produced_a_row(self, measured):
        """The worse failure, separated from the naming one.

        A collision is a defect in what things are called; a missing row
        is a person's finding that no longer exists. Split so a run that
        loses one cannot be read as a run that merely duplicated an id.
        """
        crowd = measured["crowd"]
        assert len(_db.list_bugs(crowd["project"])) >= USERS
