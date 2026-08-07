# TestForTge — Improvement Plans

Started as five reference plans from the 2026-05-19 architectural review,
each sized as a focused mini-sprint with task list, file:line references,
code sketches, tests, risks and an estimate. The later entries are a
different kind of document and are marked as such: an ADR, and the records
of what an epic actually found once it was carried out.

| File | Sprint | Focus | Effort |
|---|---|---|---:|
| [sprint_1_security.md](sprint_1_security.md) | S1 | Critical security & robustness (SSRF, owner_sid, browser leaks, SIGTERM, concurrency cap, LLM retry, CSV injection) | ~34 h |
| [sprint_2_refactoring.md](sprint_2_refactoring.md) | S2 | Split monoliths: `routes/execution.py`, `engine/qa_persona.py`; unify estimation; extract inline JS + CSP nonce | ~42 h |
| [sprint_3_performance.md](sprint_3_performance.md) | S3 | SSE streaming, Anthropic prompt caching, metrics history, SQLite WAL + Postgres prod check | ~26 h |
| [sprint_4_polish.md](sprint_4_polish.md) | S4 | Prompt-injection guards, /healthz hardening, test plan UI, bulk bug ops, roles (deferred to S5) | ~43 h |
| [identity_model_spike.md](identity_model_spike.md) | S5 pre-spike | Email-vs-sid identifier model design; locks the open questions blocking Sprint 4.1 (Roles) | 1 day |
| [tfweflo_walkthrough_integration.md](tfweflo_walkthrough_integration.md) | TFWefloLab | Port QA walkthrough heuristics from Node into Python; URL-pattern TC binding; cross-env bug dedup | ~52 h |
| [recorder_integration.md](recorder_integration.md) | Recorder | TestLum-inspired Web Recorder via Playwright codegen CLI + multi-locator Page Object + Assertion Mode | ~70 h |
| [cost_model.md](cost_model.md) | Programme · money | **Running TestFortge on $0/month**: free-tier component map, why the DB must leave Render free, LLM cost per active QA, BYOK, and what $0 genuinely cannot buy | — |
| [adr/0001-project-workspace-source-of-truth.md](adr/0001-project-workspace-source-of-truth.md) | ADR | Postgres, not the Flask session, is the source of truth for a project's artefacts — the gate E4 and E7 wait on | — |
| [e5_execute_audit.md](e5_execute_audit.md) | E5 | Audit of the Execute module: six defects, including a run in project A rendering project B's content | — |
| [team_platform_architecture.md](team_platform_architecture.md) | **Programme** | **Single-tenant tool → multi-team platform**: auth (password + Google OIDC), org/roles, session→DB workspace refactor, editors for Estimation/TC/Checklist/Bugs, storage choice, Tedgie mentor, Dashboard v2 + full test & regression stages | ~1 100 h |
| [e9_test_strategy.md](e9_test_strategy.md) | E9.1 | Test strategy for the programme, written *after* E1–E7 rather than before: coverage targets, the risk matrix, and the two rules the defects produced | — |
| [e9_security_pass.md](e9_security_pass.md) | E9.8 | OWASP ASVS-lite on auth / RBAC / upload / storage — one High found and closed (SSRF stopped at the first hop) | — |
| [e9_delivery_record.md](e9_delivery_record.md) | E9.3–E9.7 | What the integration, functional, browser and load legs were and what they found: two Postgres-only migration defects, an invitation redeemable twice, and a failed Chromium launch that poisoned the whole process | — |
| [next_session_prompts.md](next_session_prompts.md) | **Що далі** | Ten self-contained prompts for the remaining work, each stating what already exists (with file:line), what is missing, the acceptance criterion and the measured trap. Written to be pasted into a fresh session without it having to re-derive the state | — |

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
