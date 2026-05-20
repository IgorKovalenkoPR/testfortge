# TestForTge — Improvement Plans

Five reference plans produced from the 2026-05-19 architectural review.
Each plan is sized as a focused mini-sprint with task list, file:line
references, code sketches, tests, risks, and an estimate.

| File | Sprint | Focus | Effort |
|---|---|---|---:|
| [sprint_1_security.md](sprint_1_security.md) | S1 | Critical security & robustness (SSRF, owner_sid, browser leaks, SIGTERM, concurrency cap, LLM retry, CSV injection) | ~34 h |
| [sprint_2_refactoring.md](sprint_2_refactoring.md) | S2 | Split monoliths: `routes/execution.py`, `engine/qa_persona.py`; unify estimation; extract inline JS + CSP nonce | ~42 h |
| [sprint_3_performance.md](sprint_3_performance.md) | S3 | SSE streaming, Anthropic prompt caching, metrics history, SQLite WAL + Postgres prod check | ~26 h |
| [sprint_4_polish.md](sprint_4_polish.md) | S4 | Prompt-injection guards, /healthz hardening, test plan UI, bulk bug ops, roles (deferred to S5) | ~43 h |
| [identity_model_spike.md](identity_model_spike.md) | S5 pre-spike | Email-vs-sid identifier model design; locks the open questions blocking Sprint 4.1 (Roles) | 1 day |
| [tfweflo_walkthrough_integration.md](tfweflo_walkthrough_integration.md) | TFWefloLab | Port QA walkthrough heuristics from Node into Python; URL-pattern TC binding; cross-env bug dedup | ~52 h |

## Recommended sequencing

```
Week 1   S1 Security                  (must do first — blocks non-loopback deploy)
Week 2-3 S2 Refactor      ┐ S3.4 SQLite (parallel — unblocks S3.3)
Week 3-4 S3.1 + S3.2      ┘ TFWefloLab PR-1 (on the cleaned runner architecture)
Week 4-5 S3.3 + S4-lite     TFWefloLab PR-2 + PR-3
Spike    S5 identity design (1 day): email vs sid
Week 6   S5 Roles (if identity model agreed)
```

**Total**: ~197 h ≈ 25 dev-days ≈ 5 weeks solo, or 3 weeks with two parallel streams.

## What we deliberately do NOT do

- Replace TC-driven runner with the walkthrough — TC trace is the product's audit-trail backbone.
- Spawn Node from Python — two Playwright caches on a 512 MB dyno is a footgun.
- Multi-tenant SaaS architecture — single-tenant on-prem + BasicAuth is the chosen product shape.
- Replace SQLite by default — WAL is enough for dev; Postgres only required in prod via env-guard.

## Audit context

These plans were drafted after a five-agent code review covering: estimation, generation,
execution, bug-reports + DB + ops, and Tedgie + RAG. Findings recalibrated for the
single-tenant threat model:

- CSRF is globally enabled (`app.py:65`).
- Sessions are server-side filesystem, not cookie-based.
- `SECRET_KEY` validated in prod, ephemeral in debug.
- `MAX_CONTENT_LENGTH=64MB`.
- Optional `engine/basic_auth.py` HTTP-Basic gate for any non-loopback host.

The plans focus on what is actually present in the code, not generic best-practice lectures.
