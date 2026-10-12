"""Read-mostly release smoke for a running TestForTge instance.

    BASE_URL=https://testforge.qarea.org LOADTEST_PASSWORD=... \
        python -m tools.loadtest.smoke_live [--json out.json]

Seventeen checks; the only writes are three sign-ins and the Ask calls. Exactly
**one** Ask reaches the model (the second identical one must come from the
cache, the ungrounded one is refused before the model). Prints a PASS/FAIL
table and writes JSON; exit code 1 when any check FAILs.

Accounts (defaults are the seed's; override with env):
``SMOKE_ADMIN_EMAIL`` (``lt-admin@…``, admin of LOADTEST),
``SMOKE_USER_EMAIL`` (``lt-user-01@…``, plain user of LOADTEST),
``SMOKE_B_EMAIL`` (``lt-b-user@…``, LOADTEST-B) and one password,
``SMOKE_PASSWORD`` or ``LOADTEST_PASSWORD``. ``SMOKE_ASK=0`` skips the real
Ask (and its cached repeat).

Statuses: PASS, FAIL, WARN (works, but a finding to report — e.g.
``pgvector: false``), SKIP (not applicable, e.g. TLS checks over http://).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.loadtest import _common as C  # noqa: E402

CSRF_META = re.compile(r'<meta name="csrf-token" content="([^"]+)"')
CSRF_INPUT = re.compile(r'name="csrf_token" value="([^"]+)"')
TYPEAHEAD = {"en": "password", "ua": "пароля", "ru": "оплата"}
TYPEAHEAD_LIMIT_MS = 500
ASK_Q = "Which test cases cover password reset?"
UNGROUNDED_Q = "qwzx plorf vexmune trandisk"

RESULTS: list[dict] = []


def record(n: int, name: str, status: str, detail: str = "", **data) -> None:
    RESULTS.append({"n": n, "check": name, "status": status,
                    "detail": detail, **({"data": data} if data else {})})


class Client:
    def __init__(self, base: str):
        self.base = base.rstrip("/")
        self.s = requests.Session()
        self.s.headers["User-Agent"] = "testforge-release-smoke/1"
        self.csrf = ""
        self.login_response = None

    def get(self, path, **kw):
        kw.setdefault("timeout", 30)
        return self.s.get(self.base + path, **kw)

    def post(self, path, **kw):
        kw.setdefault("timeout", 60)
        return self.s.post(self.base + path, **kw)

    def login(self, email: str, password: str) -> tuple[bool, str]:
        r = self.get("/auth/login")
        m = CSRF_INPUT.search(r.text)
        if not m:
            return False, f"no csrf_token on /auth/login ({r.status_code})"
        r = self.post("/auth/login", allow_redirects=False,
                      data={"email": email, "password": password,
                            "csrf_token": m.group(1), "next": "/"})
        self.login_response = r
        loc = r.headers.get("Location", "")
        if r.status_code not in (302, 303) or "/auth/login" in loc:
            return False, f"login answered {r.status_code} → {loc or '-'}"
        page = self.get("/")
        m = CSRF_META.search(page.text)
        self.csrf = m.group(1) if m else ""
        return True, "ok"

    def json_headers(self):
        return {"X-CSRFToken": self.csrf, "Accept": "application/json"}

    def ask(self, q: str) -> dict:
        t0 = time.perf_counter()
        r = self.post("/api/ask", json={"q": q}, stream=True,
                      headers={**self.json_headers(),
                               "Accept": "application/x-ndjson"})
        out = {"status": r.status_code, "events": [], "first_text_ms": None}
        if r.status_code != 200:
            out["body"] = r.text[:300]
            return out
        for line in r.iter_lines(decode_unicode=True):
            if not line:
                continue
            ev = json.loads(line)
            if ev.get("type") == "text" and out["first_text_ms"] is None:
                out["first_text_ms"] = round((time.perf_counter() - t0) * 1000)
            out["events"].append(ev)
        out["total_ms"] = round((time.perf_counter() - t0) * 1000)
        return out


def _ev(ask: dict, typ: str) -> list[dict]:
    return [e for e in ask.get("events", []) if e.get("type") == typ]


def find_project_id(c: Client, name: str) -> str:
    r = c.get("/test-cases")
    m = re.search(r'<option value="([0-9a-f]{32})"[^>]*>\s*' + re.escape(name)
                  + r"\s*(?:<|\(|\n)", r.text)
    return m.group(1) if m else ""


def run(base: str, password: str, *, do_ask: bool) -> None:
    https = urlparse(base).scheme == "https"
    admin_email = os.environ.get("SMOKE_ADMIN_EMAIL") or C.admin_email()
    user_email = os.environ.get("SMOKE_USER_EMAIL") or C.user_email(1)
    b_email = os.environ.get("SMOKE_B_EMAIL") or C.b_user_email()

    anon = Client(base)
    # 1. /healthz
    try:
        r = anon.get("/healthz")
        body = r.json()
        mem = {k: body[k] for k in body if "rss" in k or "mem" in k}
        ok = r.status_code == 200 and "uptime_seconds" in body
        record(1, "/healthz liveness", "PASS" if ok else "FAIL",
               f"{r.status_code}, uptime {body.get('uptime_seconds')}s; "
               + (f"memory {mem}" if mem else
                  "no memory/RSS field (finding: RSS stop condition blind)"),
               body=body)
    except Exception as exc:   # noqa: BLE001
        record(1, "/healthz liveness", "FAIL", repr(exc))
    # 2. /readyz search block
    try:
        r = anon.get("/readyz")
        body = r.json()
        srch = body.get("search") or {}
        db_ok = (body.get("checks") or {}).get("database_reachable")
        if r.status_code != 200 or not db_ok or not srch:
            st = "FAIL"
        elif not srch.get("pgvector"):
            st = "WARN"
        else:
            st = "PASS"
        record(2, "/readyz db + search block", st,
               f"{r.status_code}, db={db_ok}, fts={srch.get('fts')}, "
               f"pgvector={srch.get('pgvector')}, provider={srch.get('provider')}"
               + ("  (finding: pgvector false — vector search runs in "
                  "Python; admin must CREATE EXTENSION vector)"
                  if srch and not srch.get("pgvector") else ""),
               body=body)
    except Exception as exc:   # noqa: BLE001
        record(2, "/readyz db + search block", "FAIL", repr(exc))
    # 3. anonymous / → /auth/login
    r = anon.get("/", allow_redirects=False)
    loc = r.headers.get("Location", "")
    record(3, "anonymous / redirects to sign-in",
           "PASS" if r.status_code in (302, 303) and "/auth/login" in loc
           else "FAIL", f"{r.status_code} → {loc or '-'}")

    # 4. login (admin) + /auth/me
    admin = Client(base)
    ok, why = admin.login(admin_email, password)
    me = admin.get("/auth/me").json() if ok else {}
    record(4, "admin sign-in + /auth/me",
           "PASS" if ok and me.get("authenticated") and me.get("org_active")
           and me.get("role") == "admin" else "FAIL",
           f"{why}; auth_active={me.get('auth_active')} "
           f"org_active={me.get('org_active')} role={me.get('role')}")
    if not ok:
        return
    # 5. Secure cookie + HSTS
    set_cookie = admin.login_response.headers.get("Set-Cookie", "")
    home = admin.get("/")
    hsts = home.headers.get("Strict-Transport-Security", "")
    if not https:
        record(5, "Secure cookie + HSTS", "SKIP",
               f"http:// target — cookie Secure="
               f"{'secure' in set_cookie.lower()}, HSTS={'yes' if hsts else 'no'}")
    else:
        sec = "secure" in set_cookie.lower() and "httponly" in set_cookie.lower()
        record(5, "Secure cookie + HSTS", "PASS" if sec and hsts else "FAIL",
               f"cookie Secure+HttpOnly={sec}; HSTS={hsts or 'missing'}")
    # 6. CSP
    csp = home.headers.get("Content-Security-Policy", "")
    record(6, "CSP + framing headers",
           "PASS" if "default-src" in csp and "frame-ancestors 'none'" in csp
           and home.headers.get("X-Frame-Options") else "FAIL",
           (csp[:90] + "…") if csp else "missing")
    # 7. dashboard renders
    record(7, "dashboard renders (signed in)",
           "PASS" if home.status_code == 200 and admin.csrf
           and "/auth/login" not in home.url else "FAIL",
           f"{home.status_code}, {len(home.content)} bytes, csrf meta="
           f"{bool(admin.csrf)}")

    # 8. typeahead EN/UA/RU
    parts, worst, hits_ok = [], 0.0, True
    for lang, q in TYPEAHEAD.items():
        t0 = time.perf_counter()
        r = admin.get("/api/search", params={"q": q, "quick": "1"},
                      headers=admin.json_headers())
        ms = (time.perf_counter() - t0) * 1000
        worst = max(worst, ms)
        n = len((r.json() or {}).get("results") or []) \
            if r.status_code == 200 else -1
        hits_ok &= n > 0
        parts.append(f"{lang}:{q}={n} hits/{ms:.0f}ms")
    record(8, f"typeahead EN/UA/RU < {TYPEAHEAD_LIMIT_MS} ms with hits",
           "PASS" if hits_ok and worst < TYPEAHEAD_LIMIT_MS else "FAIL",
           "; ".join(parts))
    # 9. full search + explanation
    r = admin.get("/api/search", params={"q": "password reset email"},
                  headers=admin.json_headers())
    body = r.json() if r.status_code == 200 else {}
    res = body.get("results") or []
    record(9, "full search + explanation",
           "PASS" if res and "explain" in body and "why" in res[0] else "FAIL",
           f"{r.status_code}, {len(res)} results, mode={body.get('mode')}, "
           f"explain={'explain' in body}, took {body.get('took_ms')} ms")
    # 10. typo correction
    r = admin.get("/api/search", params={"q": "pasword reset"},
                  headers=admin.json_headers())
    q = (r.json() or {}).get("query") or {} if r.status_code == 200 else {}
    corr = q.get("corrections") or q.get("did_you_mean")
    record(10, "typo correction (pasword → password)",
           "PASS" if corr else "FAIL",
           f"corrections={q.get('corrections')} did_you_mean={q.get('did_you_mean')}")

    # 11. tenancy across two orgs
    big = find_project_id(admin, C.BIG_PROJECT)
    b = Client(base)
    b_ok, b_why = b.login(b_email, password)
    if not b_ok:
        record(11, "tenancy (org A vs org B)", "FAIL", f"B sign-in: {b_why}")
    else:
        def count(c, q):
            r = c.get("/api/search", params={"q": q},
                      headers=c.json_headers())
            return len((r.json() or {}).get("results") or []) \
                if r.status_code == 200 else -1
        a_sees_b = count(admin, C.TENANT_TOKEN_B)
        b_sees_a = count(b, C.TENANT_TOKEN_A)
        b_sees_b = count(b, C.TENANT_TOKEN_B)
        a_sees_a = count(admin, C.TENANT_TOKEN_A)
        forbid = b.get("/api/search", params={"q": "login",
                                              "project_id": big},
                       headers=b.json_headers()).status_code if big else None
        ok = (a_sees_b == 0 and b_sees_a == 0 and b_sees_b > 0
              and a_sees_a > 0 and forbid == 403)
        record(11, "tenancy (org A vs org B)", "PASS" if ok else "FAIL",
               f"A→B token {a_sees_b}, B→A token {b_sees_a}, controls "
               f"A→A {a_sees_a} B→B {b_sees_b}, B on A's project → {forbid}")
    # 12. hints
    r = admin.get("/api/hints", params={"page": "test_cases"},
                  headers=admin.json_headers())
    hints = (r.json() or {}).get("hints") or [] if r.status_code == 200 else []
    record(12, "contextual hints", "PASS" if hints else "FAIL",
           f"{r.status_code}, {len(hints)} hints")

    # 13/14. one real Ask + its cached repeat
    if not do_ask:
        record(13, "Ask: one real answer (haiku)", "SKIP", "SMOKE_ASK=0")
        record(14, "Ask: identical repeat is cached", "SKIP", "SMOKE_ASK=0")
    else:
        a1 = admin.ask(ASK_Q)
        meta = (_ev(a1, "meta") or [{}])[0]
        errs = [e.get("code") for e in _ev(a1, "error")]
        texts = _ev(a1, "text")
        ok = (a1["status"] == 200 and not errs and texts
              and meta.get("model_tier") == "haiku")
        record(13, "Ask: one real answer (haiku)", "PASS" if ok else "FAIL",
               f"{a1['status']}, tier={meta.get('model_tier')} "
               f"model={meta.get('model')}, errors={errs or 'none'}, "
               f"{len(texts)} text chunks, first chunk "
               f"{a1.get('first_text_ms')} ms, cached={meta.get('cached')}")
        if ok:
            a2 = admin.ask(ASK_Q)
            m2 = (_ev(a2, "meta") or [{}])[0]
            record(14, "Ask: identical repeat is cached",
                   "PASS" if m2.get("cached") is True else "FAIL",
                   f"cached={m2.get('cached')}, {a2.get('total_ms')} ms")
        else:
            record(14, "Ask: identical repeat is cached", "SKIP",
                   "first Ask did not produce an answer")
    # 15. ungrounded
    a3 = admin.ask(UNGROUNDED_Q)
    errs = [e.get("code") for e in _ev(a3, "error")]
    m3 = (_ev(a3, "meta") or [{}])[0]
    record(15, "Ask: ungrounded question → not_grounded",
           "PASS" if "not_grounded" in errs and m3.get("grounded") is False
           else "FAIL", f"{a3['status']}, errors={errs}, "
                        f"grounded={m3.get('grounded')}")

    # 16. AI mode select (admin)
    st = admin.get("/org/settings")
    has_aim = 'id="aim-mode"' in st.text
    record(16, "AI mode select visible to admin",
           "PASS" if st.status_code == 200 and has_aim else "FAIL",
           f"{st.status_code}, aim-mode select={has_aim}")
    # 17. model policy: admin only; the sidebar switcher is per member
    user = Client(base)
    u_ok, u_why = user.login(user_email, password)
    admin_models = "/org/settings/models" in st.text
    if not u_ok:
        record(17, "model switcher: team policy admin-only", "FAIL",
               f"user sign-in: {u_why}")
    else:
        u_st = user.get("/org/settings", allow_redirects=False)
        u_home = user.get("/")
        sidebar_user = "data-model-switcher" in u_home.text
        sidebar_admin = "data-model-switcher" in home.text
        ok = admin_models and u_st.status_code in (302, 403)
        record(17, "model switcher: team policy admin-only",
               "PASS" if ok else "FAIL",
               f"admin sees models form={admin_models}; user /org/settings → "
               f"{u_st.status_code}; personal sidebar switcher: admin="
               f"{sidebar_admin}, user={sidebar_user} (members see it by "
               f"design, ADR 0003 §4.7)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m tools.loadtest.smoke_live")
    ap.add_argument("--json", metavar="PATH",
                    help="where to write the JSON (default smoke_<ts>.json)")
    args = ap.parse_args(argv)
    try:   # a Windows console cannot print every arrow and dash
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    base = (os.environ.get("BASE_URL") or "").strip()
    password = os.environ.get("SMOKE_PASSWORD") or \
        os.environ.get("LOADTEST_PASSWORD") or ""
    if not base or not password:
        print("set BASE_URL and SMOKE_PASSWORD/LOADTEST_PASSWORD", file=sys.stderr)
        return 2
    do_ask = (os.environ.get("SMOKE_ASK") or "1").strip() != "0"
    started = time.time()
    try:
        run(base, password, do_ask=do_ask)
    except Exception as exc:   # noqa: BLE001 — report what ran
        record(99, "smoke aborted", "FAIL", repr(exc))
    RESULTS.sort(key=lambda r: r["n"])
    width = max(len(r["check"]) for r in RESULTS) if RESULTS else 10
    print(f"{'#':>3}  {'check':<{width}}  status  detail")
    for r in RESULTS:
        print(f"{r['n']:>3}  {r['check']:<{width}}  {r['status']:<6}  "
              f"{r['detail']}")
    summary = {s: sum(1 for r in RESULTS if r["status"] == s)
               for s in ("PASS", "FAIL", "WARN", "SKIP")}
    print("summary: " + ", ".join(f"{k} {v}" for k, v in summary.items()))
    out = args.json or f"smoke_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"base_url": base,
                   "at": datetime.now(timezone.utc).isoformat(),
                   "took_s": round(time.time() - started, 1),
                   "summary": summary, "checks": RESULTS}, fh, indent=2,
                  ensure_ascii=False, default=str)
    print(f"json: {out}")
    return 1 if summary["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main())
