# E9.8 — Security pass (OWASP ASVS-lite)

Run 2026-08-05 against the code and the running app, by probing rather than
by reading a checklist. Scope as the epic sets it: **auth, RBAC, upload,
storage**, plus the outbound-fetch surface, which on this product is the
largest attack surface it has — TestForTge exists to fetch URLs somebody
typed.

Acceptance is "all High/Critical closed before E10". **One High was found
and closed.** Everything else that was probed either already held or is
recorded below with its severity.

## The finding

### H-1 — the SSRF policy stopped at the first hop · **High** · fixed

`engine/security.py` is genuinely good at what it checks. Sixteen probes,
all refused correctly:

```
http://127.0.0.1:5000/admin        http://2130706433/          (decimal loopback)
http://localhost/                  http://127.1/               (short form)
http://169.254.169.254/…           http://[0:0:0:0:0:ffff:127.0.0.1]/
http://[::1]/  http://0.0.0.0/     http://metadata.google.internal/
10/8, 172.16/12, 192.168/16        file://  gopher://
```

The problem was not the check. It was what happens after it passes:
`urllib.request.urlopen` follows up to ten redirects by default, and
nothing re-validated them. A page on an allowed host answering

```
302 Location: http://169.254.169.254/latest/meta-data/
```

had that target fetched. Every guard in the module ran, passed, and
protected nothing — **the check was on the first hop and the attack is on
the second.** Three call sites were exposed: the crawler's page fetch and
two in `site_tester`.

Fixed with `security.safe_opener()`, an opener whose redirect handler calls
`require_safe_url` on each hop, and all three call sites moved onto it. The
operator opt-out (`SSRF_ALLOWLIST_BYPASS=1`) still covers the whole chain,
because an opt-out that works for the first request and fails on the second
reads as an intermittent crawl failure rather than as policy.

`tests/test_ssrf_redirects.py` proves it with a real redirect server on
loopback, including a test that demonstrates the bare `urlopen` **would**
have followed it — that one is what stops somebody quietly reverting a call
site.

## Fixed while here, below the acceptance bar

### M-1 — the member-size cap trusted the archive's own header · Medium

`allure_ingest.parse_archive` capped members at 4 MB using
`member.file_size`, which comes from the zip header — a field the uploader
controls. A member declaring 1 KB and containing 200 MB passes the check
and then expands in memory before `zipfile` notices the mismatch, and the
expansion is the whole cost on a 512 MB dyno.

Now a bounded read of `MAX_MEMBER_BYTES + 1` makes the declared size
irrelevant. The rest of the archive limits were already right and are
worth stating because they are what made the naive zip bomb a non-event:
32 MB archive cap, 5,000-member cap, and a filename filter that ignores
everything but `*-result.json`.

### L-1 — no `Cross-Origin-Opener-Policy` · Low

Added as `same-origin-allow-popups` — not `same-origin`, because the Google
sign-in flow opens a popup and needs to talk back to it. `X-Frame-Options:
DENY` already covered the other direction.

## Probed and already holding

| Area | What was checked | Result |
|---|---|---|
| Session cookies | HttpOnly, Secure, SameSite, lifetime | HttpOnly on; `Secure` and `WTF_CSRF_SSL_STRICT` both keyed to `BEHIND_HTTPS`, which **is** `"1"` in `render.yaml`; SameSite=Lax |
| HSTS | present in production | emitted only when `BEHIND_HTTPS=1`, two years + `includeSubDomains; preload`. Correct to suppress it on plain HTTP — pinning a loopback host to HTTPS is its own outage |
| Other headers | CSP, nosniff, XFO, Referrer-Policy, Permissions-Policy | all present; CSP uses per-request nonces for scripts and has no `unsafe-inline` for them |
| CSRF | global `CSRFProtect`; exempt endpoints | exempt only for token-authenticated machine callers, each with its own check |
| RBAC | fail-closed route table | an unclassified endpoint is refused once `AUTH_ENABLED` is on, and a test fails the build for one |
| IDOR — runs | another project's manual run, by id | 404 |
| IDOR — automation runs | unknown id | 404 |
| Privilege escalation | `assignee_id` posted by a non-admin | ignored; only an admin may assign to somebody else |
| Deactivation | a deactivated user's live session | refused, and as of E9.2 they hold no role either |
| Upload — traversal | `../../../../etc/passwd.csv` as a filename | `file_parser` never writes to disk; parsing is in-memory, so there is no path to traverse |
| Upload — size | `MAX_CONTENT_LENGTH` | 64 MB, 413 above it |
| Storage — secrets | LLM API keys at rest | Fernet-encrypted, keyed by `TESTFORTGE_ENCRYPTION_KEY` |
| TLS on outbound fetches | certificate verification | `ssl.create_default_context()`; verification was disabled globally once and that is fixed |
| Open redirect | the sign-in `next` parameter | same-origin only, asserted in `test_auth_password` |

## Not covered, and why

* **Dependency vulnerabilities.** No SCA runs in CI. Worth adding
  (`pip-audit` is free and fast) and it is a different exercise from this
  one — this pass is about the code's own behaviour.
* **A real penetration test.** This is a review by the author of the code,
  which finds the classes the author can imagine. H-1 was found by reading
  what happens after a passing check; there will be classes that needs
  somebody else's eyes.
* **Rate limiting on the auth surface.** Lockout exists per account; there
  is no per-IP throttle, so account enumeration by timing and distributed
  guessing are unmeasured. Medium, and it needs a store that survives a
  free-tier restart before it can be built honestly.
* **The app's own accessibility and its logs.** Log redaction was not
  audited: HAR uploads and crawl payloads can carry tokens, and the
  recorder pack already warns about that in its own documentation.

## What an operator should verify on the live host

Two of the results above are configuration rather than code, so they hold
only if the deployment says so:

1. `BEHIND_HTTPS=1` — **verified in effect on 2026-08-06.** A live check on
   2026-07-13 had found no `Strict-Transport-Security`, which meant the
   variable was not reaching that host at that time; it is now.
   `curl -sSI https://testfortge.onrender.com/healthz` returns

       strict-transport-security: max-age=63072000; includeSubDomains; preload
       Set-Cookie: session=…; Secure; HttpOnly; Path=/; SameSite=Lax

   The header is the visible half and the cookie is the half that matters:
   the same flag gates `Secure` and `WTF_CSRF_SSL_STRICT`, so a missing
   HSTS header was never only a missing HSTS header. Both are on.

   Worth re-checking after any change to the service's environment, and
   worth allowing ~60 s for the first request — the free plan sleeps, and a
   25-second `curl` timeout reads as an outage when it is a cold start.
2. `AUTOMATION_INGEST_TOKEN` — with it unset the ingest endpoint answers
   403 and refuses everything, which is the safe default but also means CI
   result upload silently does nothing.
