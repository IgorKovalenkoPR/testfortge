# ADR 0003 — Platform-wide RAG: hybrid search in Postgres (FTS + pgvector), deterministic embeddings, and a short Haiku answer only on request

**Status:** Proposed, 2026-10-12. Waiting for the owner's decision.
**Epic:** E13 (proposed): Platform RAG, AI-minimal.
**Context:** [ADR 0001](0001-project-workspace-source-of-truth.md), [ADR 0002](0002-artifact-storage.md), [cost_model.md](../cost_model.md), [browser-memory.md](../../runbooks/browser-memory.md), `engine/istqb_rag.py`, `engine/llm_models.py`, `engine/llm_client.py`, `engine/features.py`.

> **Already decided by the owner, not revisited here:** Claude Haiku (`claude-haiku-5-5`) is the default for every kind of work. Sonnet and Opus are available only through a model switcher. The vector store is pgvector in the injected Postgres, and the admin runs `CREATE EXTENSION vector`. Without pgvector, and on SQLite, the platform falls back to lexical search and never crashes. Retrieval is deterministic. The LLM only writes short answers grounded in the retrieved items, with caching and token budgets.

---

## 1. Context

### 1.1. Target environment (QAREA launchpad)

| Constraint | Value | Consequence |
|---|---|---|
| Container | one, **512 MB RAM** | no in-process neural model next to Chromium (§3) |
| Process | gunicorn `WEB_CONCURRENCY=1`; `JobQueue` is a per-process singleton (`engine/job_queue.py`), held in memory only | index jobs are lost on restart, so self-healing is required (§4.4) |
| Storage | one volume `/app/data` | nothing new on disk: lexicons are in the repo, the index is in Postgres |
| DB | Postgres via `DATABASE_URL`; pgvector **may be missing** | the capability is detected at boot and the lexical fallback always works |
| Tests | SQLite locally, Postgres in CI | every path must run on SQLite |

### 1.2. What exists today (measured)

- **Retrieval:** only `engine/istqb_rag.py`, pure-Python BM25 over `engine/istqb_corpus.json` (2,826 chunks, 2,330 of them from a commercial textbook; excerpts are capped at 60 words for that reason). The tokeniser handles Latin and Cyrillic. The stop-words cover EN and UA only, with no stemming.
- **The platform's own data** (test cases, checklist items, bugs, runs, case results, estimations) cannot be searched at all.
- **LLM:** every call goes through `engine/llm_client.call_messages()`. That function picks the model (`llm_models.model_for(kind)`), resolves the key (`llm_keys.resolve_key(org_id)`), checks the budget (`check_budget`) and records cost (`_meter` → `LlmUsage`). The streaming chat in `routes/chat.py` builds its own client.
- **Measured attribution gap:** `tc_author`, `checklist_author`, `test_strategy`, `site_recon`, `session_segmenter` and `mockup_vision` do **not** pass `org_id`. As a result, the budget gate (`if not org_id … return`), BYOK and the per-org usage panel all skip them. A model switcher would skip them too.
- **No caching of LLM results.** Generation runs again whenever input changes, and also when it does not. The only caching is Anthropic's prompt caching of the chat system prompt. Test execution already uses no LLM.
- **Tenancy:** `routes/_shared.visible_projects()` and `project_access_with_meta()` decide access. With `ORG_MODE` on, org membership decides; otherwise the legacy `owner_sid` rule applies. `Project.org_id` is nullable.
- **Migrations:** `create_all` plus idempotent `_ensure_*` helpers in `engine/db.py` called from `init_db()`. `tests/test_schema_migration.py` runs on both engines. CI runs `postgres:16-alpine`, without pgvector.

### 1.3. Target UX (modelled on wad-dev.qarea.org)

- A global search field in `base.html` showing hybrid results as you type.
- An optional short "AI answer" block above the results.
- A side "Ask" panel with follow-up chips and cards for the cited items.
- A "How this was found" explanation for each result.
- Contextual hints across the platform (similar items, possible duplicates, related bugs).

## 2. Problem

1. There is no retrieval layer over the platform's own data, which is all a QA team works with.
2. Under 512 MB together with Playwright, a "proper" semantic search with a neural model does not fit in memory. A paid API adds cost and another key.
3. Queries come in Ukrainian and Russian while the data is often in English. Plain lexical search loses those matches.
4. "Minimal AI": every LLM call needs a reason, a budget, and a cache. Today there is no cache at all, and six callers spend tokens without being attributed to an org.

## 3. Options considered (embeddings)

| | (a) fastembed / ONNX in-process | (b) Voyage AI API | (c) lexical only + LLM query translation | **(d) Deterministic feature-hashing vector** |
|---|---|---|---|---|
| Model | `paraphrase-multilingual-MiniLM-L12-v2` (384d, ~220 MB in fastembed) or `multilingual-e5-small` int8 (~115 MB) | `voyage-3.5-lite`, `output_dimension=512`, multilingual | none | ~32 QA concept dimensions (EN/UA/RU keyword lists) + 256 hashed character trigrams; ~288d |
| RSS | onnxruntime ~50–70 MB + weights → **+180…330 MB** | 0 | 0 | **≈0** (lexicons ~2 MB) |
| Fits next to Chromium (393 MB tree, `MEMORY_BUDGET_MB=380`) | **No.** The OOM killer takes the worker together with the JobQueue | yes | yes | yes |
| Image | +~60 MB wheel, model download on cold start | +0 (requests) | +0 | +0 (`snowballstemmer`, ~300 KB) |
| Cost | 0 | ~$0.02 per 1M tokens (verify); 1,000 TCs × 250 tokens ≈ $0.005; an extra paid key | Haiku on every query in another language (**contradicts "minimal AI"**) | **0 tokens, $0** |
| Query latency | 20–80 ms on CPU, **seconds** for a cold load on 0.1 CPU | 100–300 ms network | +0.5–1.5 s LLM | <1 ms |
| UA/RU → EN | good (multilingual model) | very good | good, but expensive | **via a stem dictionary** UA/RU→EN; trigrams catch typos |
| Paraphrase ("can't log in" ≈ "authentication fails") | good | very good | partly | **partly**: concept dimensions + dictionary; measured on the golden set (§7) |
| SQLite fallback | needs the model again | needs the network | yes | **the same code** (pure-Python cosine over `array('f')`) |
| Determinism / reproducible tests | ±float | no (external API) | no | **full** |

**Chosen: (d) as the default provider, with a pluggable `Embedder` interface.**
- (a) is rejected for this deployment target on memory grounds, not quality. It may come back for self-hosted setups with more RAM through the same interface.
- (b) is a documented upgrade (`EMBEDDINGS_PROVIDER=voyage`). It is switched on only if the golden set shows (d) below the threshold (§7.3), and only with a separate key that the org or operator stores encrypted alongside the Anthropic key in `OrgSecret`.
- (c) is rejected. A dictionary translates without spending tokens.

**Fallback chain at query time** (each step lower happens automatically):
1. pgvector cosine search on `search_vector`, plus Postgres full-text search.
2. Pure-Python cosine over `search_doc.vec_blob`, for SQLite or a missing extension, plus Python BM25.
3. Lexical only, when the vectors' version does not match the active one (for example mid-backfill) or the candidate set exceeds `SEARCH_PY_VECTOR_MAX_DOCS`.

The response always reports `mode: "hybrid" | "hybrid-py" | "lexical"`.

## 4. Decision

### 4.1. Data model

All tables are portable through SQLAlchemy except `search_vector` and the `tsv` column, which are Postgres-only raw DDL in `_ensure_search_schema()`.

```
search_doc                                      -- ORM, SQLite + PG; the source of truth for the index
  id               INTEGER PK
  doc_key          VARCHAR(200) UNIQUE          -- "{scope}:{project_id|-}:{kind}:{ref}:{chunk_no}"
                                                --  (portable: SQLite UNIQUE treats NULLs as distinct)
  scope            VARCHAR(10)  NOT NULL        -- 'project' | 'global'
  org_id           VARCHAR(32)  NULL  INDEX     -- denormalised from project.org_id (no FK, like Project.org_id)
  project_id       VARCHAR(32)  NULL  FK project.id ON DELETE CASCADE, INDEX
  kind             VARCHAR(20)  NOT NULL        -- test_case|checklist_item|bug|run|case_result|estimation|guide|knowledge|istqb
  ref              VARCHAR(60)  NOT NULL        -- external_id (TC-012, BUG-7) or 'run:123'
  chunk_no         INTEGER NOT NULL DEFAULT 0
  title            VARCHAR(300)
  body             TEXT                         -- clipped to 4000 characters
  lang             VARCHAR(4)                   -- en|ua|ru|mixed
  lex_title        TEXT                         -- normalised + stemmed + translated tokens
  lex_body         TEXT
  url              VARCHAR(300)                 -- deep link
  facets           JSON                         -- {status, priority, severity, section, run_id, suite}
  content_hash     CHAR(40)                     -- sha1(title|body|facets|LEXICON_VERSION)
  vec_version      VARCHAR(24) NULL             -- 'hash-v1-288' | 'voyage-3.5-lite-512'
  vec_blob         LARGEBINARY NULL             -- array('f').tobytes(); 288*4 = 1152 bytes
  source_updated_at TIMESTAMPTZ, indexed_at TIMESTAMPTZ
  INDEX (project_id, kind)

-- Postgres only, always (no extension needed):
  ALTER TABLE search_doc ADD COLUMN tsv tsvector GENERATED ALWAYS AS (
      setweight(to_tsvector('simple', coalesce(lex_title,'')), 'A') ||
      setweight(to_tsvector('simple', coalesce(lex_body,'')),  'B')) STORED;
  CREATE INDEX ix_search_doc_tsv ON search_doc USING GIN (tsv);

search_vector                                   -- Postgres + pgvector only; a copy of vec_blob
  doc_id     INTEGER PK REFERENCES search_doc(id) ON DELETE CASCADE
  project_id VARCHAR(32) NULL                    -- denormalised for the filter
  embedding  vector(288)                         -- dims = active provider; a provider change = DROP+CREATE+backfill
  CREATE INDEX ix_search_vector_hnsw ON search_vector
      USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64);
  CREATE INDEX ix_search_vector_project ON search_vector (project_id);

search_scope_state                               -- ORM
  scope_key   VARCHAR(40) PK                      -- project_id | 'global'
  version     INTEGER NOT NULL DEFAULT 0          -- +1 on every index change
  indexed_at, doc_count, provider, vec_version, last_error TEXT
  vocab_json  JSON NULL                           -- top terms for "did you mean" (≤20k)

rag_answer_cache                                 -- ORM (§4.6)
  key CHAR(64) PK, org_id INDEX, project_id INDEX, model, lang,
  answer_json JSON, created_at, hits INT, last_hit_at

llm_result_cache                                 -- ORM (§4.9, automation)
  key CHAR(64) PK, org_id INDEX, kind VARCHAR(30) INDEX, prompt_version VARCHAR(40),
  model VARCHAR(80), response_text TEXT, usage JSON, created_at, hits, last_hit_at
```

**Why `vec_blob` and `search_vector` are both stored.** `vec_blob` is portable. It is what the SQLite and no-extension fallback reads. When the admin enables the extension later, `search_vector` is filled from it **without recomputing anything**. `search_vector` is a derived index and can be dropped at any time.

**HNSW vs ivfflat: HNSW.** ivfflat needs data present before the index is built (k-means over `lists`), and its recall drops as data grows. On an empty, growing database that is the wrong profile. HNSW has no training step, updates incrementally, and has better recall for the same latency. Its costs are slower builds and more memory in `maintenance_work_mem`, both irrelevant up to around 100k rows.

**The tenancy filter and HNSW.** Most queries are scoped to one project (a few thousand rows). There, the planner picks the `project_id` btree and an exact distance sort, which is **exact** and takes under 10 ms. For org-wide scope, `SET LOCAL hnsw.ef_search = 200` is used, plus `SET LOCAL hnsw.iterative_scan = relaxed_order` when `pg_extension.extversion >= 0.8`. Otherwise the query over-fetches `k*4` and filters. Separate per-org partial indexes were rejected: they mean DDL on every new org.

**Storage size:** about 1.2 KB of vector plus ~1.5 KB of text and tsv per document. 50k documents come to roughly 135 MB together with HNSW. The size of the injected DB is unknown, so this is a risk (§5).

### 4.2. Tenancy (enforced, not advisory)

- `allowed = [p["id"] for p in visible_projects()]` for org scope. For project scope, `project_access_with_meta(pid)[0] == "ok"`, otherwise **403** (the same rule as `routes.projects._require_project_owner`).
- **Every** SQL query has the form `WHERE (scope = 'global' OR project_id = ANY(:allowed))`. An empty `allowed` returns only global documents. There is no code path that omits the filter. All queries go through the single function `retrieve._scope_clause()`, and a test monkeypatches it to fail if it is called without ids.
- Both the legacy era (`ORG_MODE` off, `owner_sid`) and the org era work through `visible_projects()` with no changes.
- `rag_answer_cache` keys include the ids **and content hashes of the cited documents** (§4.6), so a cached answer cannot leak into another tenant. A tenant without access to a document never reaches the cache key that contains it.
- `retention.delete_project_data` also deletes `rag_answer_cache` and `llm_result_cache` rows for the project. `search_doc` is covered by the cascade, which the test still checks explicitly.

### 4.3. What gets indexed, and how it is chunked

| kind | Source (`engine/db.py`) | title | body | facets |
|---|---|---|---|---|
| `test_case` | `TestCase` | `external_id · summary` | section, preconditions, test_steps, test_data, expected_result, issues, comment, gherkin | status, priority, suite, category, testing_type, section |
| `checklist_item` | `ChecklistItem` | `external_id · section` | objective, comments | status, priority, depth |
| `bug` | `BugReport` | `external_id · title` | steps_to_reproduce, actual/expected, preconditions, environment, comment | severity, priority, status, bug_area, run_id |
| `run` | `ExecutionRun` + summary | `Run #id · date` | env, totals, notes | status, mode |
| `case_result` | `ExecutionCaseResult` (failed/blocked only) | `case_external_id · result` | note/error (stack traces clipped to 600 characters) | run_id, status |
| `estimation` | `Estimation` (latest only) | `Estimate · total_hours` | input summary | — |
| `guide` (global) | `engine/chatbot_guide.py`, `templates/guide/*` | topic | text | page |
| `knowledge` (global) | `engine/qa_knowledge/*.yaml`, `engine/glossary.py` | term/rule | definition | — |
| `istqb` (global) | `istqb_corpus.json`, **syllabus part only** | section | text | page |

The textbook half of the ISTQB corpus is **not** copied into the database, for the copyright reason recorded in `istqb_rag.py`. It stays in the existing BM25 with the 60-word excerpt cap, and the search adds its results as a separate "ISTQB" group through `istqb_rag.search()`.

**Chunking:** one document per entity. When the body is over 1,500 characters it is split into windows of ~800 characters with a 100-character overlap, on sentence or line boundaries, and the title is repeated in every chunk. Results are grouped by `(kind, ref)`, and the best chunk wins.

### 4.4. When indexing happens: three layers that do not trust each other

1. **The ORM hook** (`engine/search/hooks.py`): `event.listen(Session, "after_flush")` collects the `project_id` of new, dirty and deleted `TestCase`/`ChecklistItem`/`BugReport`/`ExecutionRun`/`ExecutionCaseResult`/`Estimation` objects into `session.info`. `after_commit` puts `reindex_project(pid)` on the `JobQueue`, debounced by 3 s per project. That way, deleting everything and inserting 300 rows produces one job, not 300.
2. **Lag check on read:** `/api/search` compares `search_scope_state.indexed_at` with `project.updated_at`. Most write paths bump that through `touch_project`. If the index is behind, a job is queued and the results come back with `stale: true`. This catches bulk `query.update()/delete()` statements that the ORM events do not see, and jobs lost on a restart.
3. **Content hash during reindex:** `reindex_project` reads all of the project's rows in batches of 500, builds the documents, and compares `content_hash`. Only changed documents get an UPSERT, and documents that disappeared are deleted. Embedding with (d) is pure CPU, about 0.2 ms per document, so 5k documents take about 1–2 s. `search_scope_state.version` is incremented.

Plus:
- **Backfill:** `python -m tools.search_backfill [--project ID] [--global] [--rebuild] [--to-pgvector]`. It is also triggered lazily on the first search of a project that has never been indexed.
- **Global corpora:** indexed at boot in the background, keyed by a hash of the source files plus `LEXICON_VERSION`. Unchanged sources are a no-op.
- **Indexing never calls an LLM or the network** when the provider is (d).

### 4.5. Query pipeline (`engine/search/retrieve.py`)

```
q_raw
 → normalise: NFKC, lower, whitespace, ё→е, apostrophes ʼ'’ → '
 → lang: Cyrillic? і/ї/є/ґ → ua; ы/э/ъ/ё → ru; else en
 → ID fast path: /^(TC|CL|BUG|RUN)-?\d+$/i → exact match on ref, pinned to the top, boost=∞
 → tokens → stems (snowball english/russian; in-house light UA suffix stripper ~40 rules)
 → translation: UA/RU stem → EN canonical via engine/search/lexicon/xlate_{ua,ru}.json
   (APPENDED to the originals, so Cyrillic data is still found)
 → typo: tokens with no df in the project vocabulary → trigram candidates + Levenshtein ≤2 → did_you_mean
 → lexical list (top 50):
     PG:  tsv @@ to_tsquery('simple', t1 | t2 | last:*)  ORDER BY ts_rank_cd(tsv, q, 32)
     SQLite/fallback: Python BM25 (tokeniser shared with istqb_rag, moved to engine/search/text.py)
 → vector list (top 50): embed(query) → cosine
     PG+pgvector: ORDER BY embedding <=> :q   |  else: Python cosine over vec_blob
 → RRF: score = Σ w_i / (60 + rank_i), w_lex = 1.0, w_vec = 1.0; boosts: ID ×∞, title-only
   match +0.005, open bugs +0.002; tie-break by source_updated_at
 → group by (kind, ref), top N, snippet highlighting from matched terms
```

**Why RRF rather than `0.45*BM25 + 0.55*cosine` (the reference site's formula).** `ts_rank_cd` on Postgres and Python BM25 on SQLite have different score scales. A linear blend would rank differently on each backend and need normalisation tuning. RRF uses only ranks, so it behaves the same on both. The weights stay configurable (`SEARCH_W_LEX`, `SEARCH_W_VEC`) for the golden-set experiment.

**How the hash embedder works (`engine/search/embed.py`, `HashEmbedder`):**
- **Concept block (~32 dimensions):** each concept, such as login/auth, payment/checkout, cart, search, form-validation, navigation, performance, security, a11y, l10n, upload, notification/email, API, responsive/mobile, crash/error, CRUD, roles/permissions, session, layout, browser-compat, date-time, pagination, filter-sort, registration, password, profile, settings, export/report, negative, smoke/regression, has a list of EN/UA/RU stems. A dimension's value is `log1p(hits)`.
- **Trigram block (256 dimensions):** trigrams of `#token#` from the **translated EN stream plus the original**, using signed hashing: bucket = `blake2b(tri, 2 bytes) % 256`, sign = one bit.
- **Never use Python's `hash()`.** It is salted per process, so the same text would get different vectors after a restart.
- Each block is L2-normalised separately and weighted 0.5/0.5, then the whole vector is L2-normalised.
- `vec_version = f"hash-v{LEXICON_VERSION}-{dims}"`. Changing the lexicon means a backfill, which costs seconds of CPU and no tokens.

**Explanation for each result** (no LLM; returned when `explain=1` and always in the Ask panel):
`{lex_rank, vec_rank, rrf, matched_terms:["login","вхід→login"], concepts:["auth","validation"], typo:{"logn":"login"}, boosts:["id_exact"], mode}`.

### 4.6. Where the LLM is used, and where it is not

**Not used:** indexing, embeddings, query translation, typo correction, ranking, typeahead, explanations, contextual hints, and the facet-based chips (for example "Only open bugs" or "Show linked tests for TC-012").

**Used in exactly one place: `POST /api/ask`**, and only when:
- the user clicked "Ask" in the panel, or
- the query looks like a question (ends with `?`, starts with how/what/why/як/що/чому/как/что/почему, or has ≥4 words), the results have already been shown, and the AI block is requested lazily, debounced by 600 ms after typing stops. It never fires on each keystroke.

**Grounding gate:** if the top-1 RRF score is below `RAG_MIN_GROUNDING`, there are fewer than 2 results, or the results together match directly (own word or translation — a typo correction does not count) less than `RAG_MIN_COVERAGE` (default 0.5) of the question's words — unless one result is an exact id hit — **no LLM call is made**. (Coverage clause added 2026-10-13 after a nonsense question passed live on one word plus a typo guess.) The response is a deterministic "Not enough related items found" plus the chips.

**Prompt (kind `search_answer` → Haiku):**
- system: a static instruction of about 300 tokens. It asks for 2–3 sentences in the user's language, using only facts from the items, citing `[[TC-012]]`, with a last line of `SUGGESTIONS: a | b | c`.
- user: the question plus the top 8 items, each `[[ref]] title — body[:400]`.
- Limits: `max_tokens=300`; input ≤ 3,000 tokens, estimated as characters ÷ 3.2 for Cyrillic. Items are dropped from the end to fit.
- Up to 2 previous turns of follow-up (questions only, with their answers compressed to ≤200 characters).

**After the answer:**
- Citations are validated, and any `[[ref]]` not among the retrieved items is removed.
- The `SUGGESTIONS:` line is parsed into chips, merged with the deterministic chips, and deduplicated (≤4).

**Answer cache (`rag_answer_cache`):** `key = sha256(PROMPT_VERSION | model | lang | normalised_query | sorted[(doc_id, content_hash) of the 8 items] | follow-up history hash)`.
- Tying the key to the evidence rather than to the whole corpus version means an unrelated edit does not invalidate the cache, while a change to any cited item does.
- TTL 14 days (`RAG_ANSWER_CACHE_DAYS`). It is purged by the existing retention sweeper.
- A cache hit costs 0 tokens and still replays as a stream.

**Budgets:**
1. The existing monthly budget gate in `call_messages` (`LLMBudgetExceeded` → the deterministic "AI answer unavailable" message; search still works).
2. `RAG_ASK_DAILY_CAP_PER_ORG` (default 300), counted as `LlmUsage` rows with `kind='search_answer'` today.
3. Per user: 6/min and 60/day, using an in-memory token bucket. That is correct with a single worker; with more workers it becomes approximate, which is stated in the code.

**Cost estimate:** ~2,500 input + 200 output tokens per uncached answer. At $1/$5 per 1M tokens (verify Haiku 5.5 pricing in `llm_cost.PRICES`) that is about $0.0035. 1,000 asks a month comes to about $3.5, or about $1–2 with a 50% cache hit rate.

### 4.7. Model switcher

**Defaults (owner's decision):**
- `TIER_DEFAULTS = {"haiku": "claude-haiku-5-5", "sonnet": "claude-sonnet-5", "opus": "claude-opus-5-5"}`. Check the Sonnet id against Anthropic's current model list.
- `KIND_TIER`: **every** kind → `haiku`. New kinds: `search_answer` and `query_rewrite` (reserved, not used).
- Moving `consult` from Sonnet to Haiku conflicts with the comment in `llm_models.py` about E6.7. The gate is a run of `engine/tedgie_eval.py` on the golden set before the merge, with the result recorded in the PR.

**What is stored (tiers, never raw model ids, so users cannot type arbitrary model strings):**
- Org policy: `Organization.settings["llm_models"] = {"default_tier": "haiku", "per_kind": {"authoring": "sonnet"}, "allowed_tiers": ["haiku", "sonnet"], "users_may_choose": true}`.
- User preference: `UserSetting(owner=user_id, key="llm_model_pref") = {"*": "sonnet", "consult": "opus"}`.

**Precedence in `llm_models.model_for(kind, *, org_id=None, user_id=None)`, most specific first:**
0. `ANTHROPIC_MODEL` (legacy global pin). It overrides everything, as today, with a warning at boot.
1. `LLM_MODEL_FOR_<KIND>`: a **hard pin by the operator**. The switcher shows "pinned by operator" and is disabled for that kind.
2. The user's preference, if the org allows it (`users_may_choose`) and the tier is in `allowed_tiers`.
3. The org's `per_kind` setting, then the org's `default_tier`.
4. `LLM_<KIND>_TIER`.
5. `KIND_TIER[kind]` (haiku).

The tier is then mapped to a model through `ANTHROPIC_MODEL_<TIER>`, falling back to `TIER_DEFAULTS`.

**Which kinds can be switched:** `SWITCHABLE_KINDS = {"consult", "search_answer", "authoring", "analysis", "vision"}`. `segmentation` and `query_rewrite` stay on Haiku: their output is checked against a schema, so a more expensive model would cost more without a visible benefit.

**Permissions and money:**
- The org admin sets the policy (`/org/settings/models`). A user chooses within it.
- **Platform key:** Sonnet and Opus are allowed only when `LLM_ALLOW_PREMIUM_ON_PLATFORM_KEY` is on (default off), because the operator pays.
- **BYOK org:** any tier the admin allows.
- The check happens in `llm_prefs.effective_tier()` using `llm_keys.resolve_key(org_id)[1] == "org"`.

**Plumbing:**
- A new `engine/llm_context.py` holds a `contextvar` with org, project and user, set in `before_request` (from `permissions.current_org_id/current_user_id` and `resolve_active_project`) and carried into JobQueue jobs through `job.meta`.
- `call_messages` reads it when `org_id` and the others are not passed explicitly. That closes the attribution gap for all six callers without changing their signatures.
- The streaming chat path (`routes/chat.py` lines 68 and 338) switches to `model_for("consult", org_id=…, user_id=…)`.
- Org settings are cached for 30 s, so routing does not add DB queries to every call.

### 4.8. Endpoints

All of them are registered in the `routes/search.py` module (the register-function pattern) and classified in `engine/route_policy.POLICY`.

| Method / path | Policy | Purpose |
|---|---|---|
| `GET /api/search` | user | hybrid search; no LLM |
| `POST /api/ask` | user (CSRF) | AI answer, NDJSON stream |
| `POST /api/ask/feedback` | user | 👍/👎 on a cached answer (`hits`/`helpful` counter) |
| `GET /api/hints` | user | contextual hints; no LLM |
| `GET /api/search/status` | admin | index state |
| `POST /api/search/reindex` | admin | reindex job |
| `GET /api/me/model-preference`, `PUT …` | user | user's model choice |
| `GET /org/settings/models` (inside `org_settings`), `POST /org/settings/models` | admin | org policy (form, flash, audit `append_audit`) |

`GET /api/search?q=&project_id=&scope=project|org&kinds=test_case,bug&limit=20&quick=0&explain=0`
```json
{"query":{"raw":"не працює вхід","normalised":"не працює вхід","lang":"ua",
          "translated":{"вхід":"login"},"did_you_mean":null},
 "mode":"hybrid","stale":false,"took_ms":18,"corpus_version":42,
 "results":[{"doc_id":913,"kind":"bug","ref":"BUG-17","title":"Login fails with valid creds",
   "snippet":"…<mark>login</mark> button returns 500…","url":"/bug-reports?focus=BUG-17",
   "project":{"id":"…","name":"Shop"},"facets":{"status":"open","severity":"major"},
   "score":0.0321,
   "why":{"lex_rank":1,"vec_rank":2,"matched_terms":["вхід→login"],"concepts":["auth"],
          "typo":{},"boosts":[]}}],
 "groups":{"istqb":[{"source":"syllabus","page":31,"excerpt":"…"}]},
 "chips":[{"label":"Only open bugs","q":"вхід","kinds":["bug"],"facets":{"status":"open"}}]}
```
`quick=1` (typeahead) returns ID and title prefix matches only (lexical, ≤8, <30 ms) and never queues reindexing.

`POST /api/ask` with body `{"q":"…","project_id":"…","scope":"project","lang":"ua","pinned":["TC-12"],"history":[{"q":"…","a":"…"}]}` returns `application/x-ndjson`:
```
{"type":"meta","cached":false,"model":"claude-haiku-5-5","grounded":true,"items":[{"n":1,"ref":"BUG-17","kind":"bug","title":"…","url":"…"}]}
{"type":"delta","text":"Вхід падає з 500 через …"}
{"type":"delta","text":" [[BUG-17]] …"}
{"type":"citations","refs":["BUG-17","TC-12"]}
{"type":"suggestions","items":["Які тести покривають логін?","Показати відкриті баги логіну"]}
{"type":"done","cache_key":"…","usage":{"in":2140,"out":180}}
```
The errors `{"type":"error","code":"budget|cap|ungrounded|llm_unavailable","message":"…"}` all have deterministic text and still include the cards.

`GET /api/hints?project_id=&context=test_case:TC-012|page:bug_reports` returns `{"hints":[{"type":"similar","ref":"TC-031","title":"…","similarity":0.91,"label":"Possible duplicate"},{"type":"related_bug","ref":"BUG-17",…},{"type":"guide","topic":"…","url":"…"}]}`. These are vector neighbours of the context document, at zero tokens.

`GET /api/search/status` returns `{"fts":true,"pgvector":true,"pgvector_version":"0.8.0","provider":"hash","vec_version":"hash-v1-288","docs":5321,"vectors":5321,"pending_projects":0,"last_indexed_at":"…","stale_projects":[]}`.

`POST /api/search/reindex` takes `{"project_id":null|"…","rebuild":false}` and returns `{"job_id":"…"}`, which is polled through the existing job status pattern.

### 4.9. Automation: `llm_result_cache` and `AUTOMATION_AI_MODE`

- **`engine/llm_cache.py`:** `cached_call(kind, *, prompt_version, cache_parts, org_id, **call_kwargs)`.
  - `key = sha256(org_id | kind | prompt_version | model | house_style_hash | sha256(json(system, messages)))`.
  - `house_style_hash` is a hash of `glossary.glossary_text()` plus `wording_rules_text()` plus `tc_house_style` rules, so a style change invalidates exactly the dependent results.
  - The scope is per org, never shared between orgs.
  - TTL 30 days (`LLM_RESULT_CACHE_DAYS`), capped at 2,000 rows per org (LRU by `last_hit_at`), purged in `engine/retention.py`.
  - A cache hit does not go through `call_messages`, so it is neither metered nor counted against the budget. A cheap counter in the log records the hits.
- **Callers:** `tc_author`, `checklist_author`, `test_strategy`, `site_recon`, `session_segmenter` and `mockup_vision` each get a `_PROMPT_VERSION` constant and go through `cached_call`. Chat is **not** cached here; it has its own dialogue context.
- **`AUTOMATION_AI_MODE=off|assist|full`** (`engine/ai_mode.py`). It is not a `Flag`, because `features.FLAGS` is on/off only. An org can lower the mode in `Organization.settings["automation_ai_mode"]` but cannot raise it above the env value.
  - `off`: rule engines only (`tc_rules`, `checklist_rules`, the YAML packs). No LLM call in automation.
  - `assist` (**default, rules first**): rules run first, and the LLM is called only when the rules leave gaps (coverage below `AI_ASSIST_MIN_COVERAGE`) or the user clicks "Improve with AI".
  - `full`: today's behaviour.
  - Test execution needs no change; it does not use the LLM.

### 4.10. Feature flags (`engine/features.py`, mirrored in `render.yaml`)

| Flag | Default | Requires | What it turns on |
|---|---|---|---|
| `PLATFORM_SEARCH` | off | `WORKSPACE_DB_FIRST` (the index reads Postgres, not the session) | indexing hooks, `/api/search`, `/api/hints`, the search field, hints |
| `PLATFORM_SEARCH_AI` | off | `PLATFORM_SEARCH` | `/api/ask`, the AI block, the Ask panel |
| `MODEL_SWITCHER` | off | `ORG_MODE` | org policy and user preferences. With it off, routing is env + `KIND_TIER` exactly as today, with the Haiku defaults |

Plain settings (not flags): `EMBEDDINGS_PROVIDER=hash|voyage`, `SEARCH_W_LEX`, `SEARCH_W_VEC`, `SEARCH_PY_VECTOR_MAX_DOCS=20000`, `RAG_MIN_GROUNDING`, `RAG_MIN_COVERAGE`, `RAG_ASK_DAILY_CAP_PER_ORG`, `RAG_ANSWER_CACHE_DAYS`, `LLM_RESULT_CACHE_DAYS`, `LLM_ALLOW_PREMIUM_ON_PLATFORM_KEY`, `AUTOMATION_AI_MODE`.

With a flag off, the routes return 404 (a hidden feature, not a 403). The hooks are still registered but do nothing, so turning a flag on does not need a restart, as in `features.py`. `features.snapshot()` and `/readyz` show the flags. `/readyz` also gets a `search: {fts, pgvector, provider}` block.

### 4.11. Memory budget (512 MB container)

| Component | Steady state | Peak |
|---|---|---|
| Lexicons (concepts + xlate UA/RU, ~10k entries) + snowballstemmer | ~3 MB | — |
| Query vector LRU (2,000 × ~1.5 KB) | ~3 MB | — |
| "Did you mean" vocabulary (≤4 projects × ≤20k terms) | ~8 MB | — |
| Reindex job (batches of 500 rows, streamed) | — | ~10 MB |
| Python cosine fallback (≤20k × 1,152 B in `array('f')`) | — | ~25 MB transient |
| **Total** | **≤15 MB** | **≤50 MB** |
| For comparison: (a) fastembed | +180…330 MB | — |

No numpy and no onnxruntime. A test checks that `import engine.search` does not pull in `numpy` or `onnxruntime` (`sys.modules`), and that `tracemalloc` peak during a 2k-document reindex stays under 40 MB.

### 4.12. Migrations

Following the repo's own pattern, with no Alembic:
- New ORM models (`SearchDoc`, `SearchScopeState`, `RagAnswerCache`, `LlmResultCache`) are created by `create_all`.
- `_ensure_search_schema(engine)` runs in `init_db()` after `_ensure_public_id_unique_indexes`. It is idempotent and wrapped in try/except with a warning, as the other `_ensure_*` helpers are.
  - **PG:** add `tsv` + GIN if missing. Try `CREATE EXTENSION IF NOT EXISTS vector` inside a savepoint (`begin_nested`); if it fails (no privileges), log at info level and continue.
  - Read `SELECT extversion FROM pg_extension WHERE extname = 'vector'`. If present, `CREATE TABLE IF NOT EXISTS search_vector …` plus the HNSW index, created with plain `CREATE INDEX IF NOT EXISTS` (`CONCURRENTLY` is not possible inside a transaction, and the table is new).
  - Publish capabilities into `engine.search.caps = {"fts": bool, "pgvector": bool, "pgvector_version": str|None}`.
  - **SQLite:** no-op beyond `create_all`, with `caps = {"fts": False, "pgvector": False}`.
- If the extension appears later, a **re-check** happens in `/api/search/status` and in the backfill tool (`--to-pgvector`). It fills `search_vector` from `vec_blob` without recomputing anything.
- Changing the provider or dimensions means: `DROP TABLE search_vector`, then `CREATE` with the new `vector(N)`, then backfill. Search runs in `lexical` mode in the meantime.

## 5. Consequences

### Positive
- A search across all of a team's data at zero tokens and ≈0 MB, giving the same results on SQLite and Postgres.
- The LLM is used in exactly one place, with a grounding gate, a cache keyed by the evidence, and three levels of budget.
- The six previously unattributed LLM callers now respect the budget, BYOK and the usage panel. That is a fix in its own right.
- `llm_result_cache` plus `AUTOMATION_AI_MODE=assist` cut the main source of spend (generation) without losing the option of `full`.

### Negative / costs
- The hash embedder is weaker than a neural model on paraphrases. This is mitigated by the concept dimensions and the dictionary, measured by the golden set, and has a documented path to Voyage.
- The lexicons (concepts, UA/RU→EN) need maintenance. That is a task for a QA analyst, not a developer.
- `search_doc` duplicates text: roughly 1.5–3 KB per entity in Postgres.

### Risks
| Risk | Mitigation |
|---|---|
| The admin has not run `CREATE EXTENSION vector` | `mode=hybrid-py`/`lexical`, a warning at boot, `status.pgvector=false`; tests cover the no-extension path |
| Bulk UPDATE/DELETE bypasses ORM events, so the index goes stale | lag check on `project.updated_at` + content-hash reindex + `stale:true` in the response |
| JobQueue loses jobs on restart / OOM | the same lag check on the first search, a global reindex at boot |
| Cross-tenant leak through search or cache | one `_scope_clause`, cache keys over cited `doc_id`s, a tenancy test in both eras |
| Size of the injected Postgres is unknown | `status.docs` + byte estimate, case results limited to failed/blocked, `body` ≤4,000 characters |
| Haiku for `consult` lowers Tedgie's quality | golden-set gate through `tedgie_eval` before the merge; the switcher gives an override per org |
| Haiku 5.5 / Sonnet / Opus model ids and prices | verify before merging (`llm_cost.PRICES`, `TIER_DEFAULTS`) |
| Python `hash()` salt makes vectors non-deterministic | only `blake2b`; a test compares vectors across two subprocesses |
| An NDJSON stream behind a proxy gets buffered | the `X-Accel-Buffering: no` header, as SSE already does; tests for `Content-Type` |
| The launchpad Dockerfile (5000 vs 8080, volumes) | out of scope for this ADR, a separate task |

## 6. Execution gates

1. Before **task 1** is merged: `tedgie_eval` golden set on Haiku ≥ the current baseline, minus an agreed tolerance.
2. Before `PLATFORM_SEARCH` is turned on in prod: the search golden set (`tests/fixtures/search_golden.json`, ≥40 queries, a third each in EN/UA/RU) reaches **recall@5 ≥ 0.80** in `hybrid` mode and ≥ 0.65 in `lexical` mode on SQLite and PG. If `hybrid` < 0.80, run the Voyage experiment (§3) **before** turning the flag on.
3. Before `PLATFORM_SEARCH_AI` is turned on: 20 manual answers checked for groundedness (no claims without `[[ref]]`), p95 time to first chunk < 2.5 s, cache hit rate measured over a week.
4. The tenancy test is green in both eras (`ORG_MODE` on/off) on both engines.

## 7. Test strategy

1. **Unit tests, pure, no DB** (`tests/test_search_text.py`, `tests/test_search_embed.py`):
   - normalisation, language detection, UA stemmer, translation dictionary;
   - the ID fast path;
   - a deterministic vector (snapshot, and a comparison across two subprocesses);
   - L2 norm = 1;
   - "вхід"/"вход"/"login" have cosine > 0.5;
   - RRF on synthetic lists.
2. **Schema** (`tests/test_schema_migration.py`, extended, both engines):
   - `_ensure_search_schema` is idempotent;
   - on a PG database **without** the extension, `caps.pgvector=False` and no exception;
   - **with** the extension, `search_vector` and HNSW exist.
   - CI: the service image becomes `pgvector/pgvector:pg16`, and the job creates two databases, `tfg_migration_test` (with `CREATE EXTENSION vector`) and `tfg_novector_test` (without). The extension is per database.
3. **Indexer** (`tests/test_search_indexer.py`, `tests/test_search_hooks.py`):
   - `save_test_cases` → one job (debounce);
   - unchanged content → 0 UPSERTs;
   - an item deleted → its document deleted;
   - `bulk_update_bugs` → caught by the lag check;
   - `delete_project` → cascade;
   - a global corpus is not re-indexed when unchanged.
4. **API + tenancy** (`tests/test_search_api.py`, `tests/test_search_tenancy.py`):
   - JSON shapes;
   - 404 with the flag off;
   - a project from another org gives 403 on project scope and is missing from org scope;
   - in the legacy era a foreign `owner_sid` is not visible;
   - every new endpoint is classified (the existing `TestEveryRouteIsClassified` catches omissions automatically).
5. **Ask** (`tests/test_search_ask.py`), with a fake SDK as in `test_llm_client.py`:
   - the grounding gate makes no call;
   - cache hit = 0 calls, and the same NDJSON;
   - changing the `content_hash` of a cited document is a miss;
   - foreign `[[ref]]` citations are removed;
   - `LLMBudgetExceeded` → `{"type":"error","code":"budget"}`;
   - the daily cap;
   - an `LlmUsage` row with `kind='search_answer'` and the `org_id`.
6. **Golden set** (`tests/test_search_golden.py`): fixtures of ~150 documents plus ≥40 queries, asserting recall@5 thresholds rather than exact order, since PG and SQLite rank slightly differently by design.
7. **Memory** (`tests/test_search_memory.py`): no numpy/onnxruntime imported, `tracemalloc` peak < 40 MB on 2k documents.
8. **UI:**
   - `test_every_template_is_reachable`, `test_embedded_js_syntax` and `test_csp_headers` cover the new assets;
   - `test_i18n_parity` covers the new strings;
   - one Playwright e2e in `test_e2e_golden_paths.py`: type, see results, open the Ask panel, see cards.
9. **Model switcher** (`tests/test_model_switcher.py`):
   - the precedence matrix (env pin > user > org > env tier > default);
   - premium tiers forbidden on the platform key without `LLM_ALLOW_PREMIUM…`;
   - BYOK allows them;
   - a non-switchable kind ignores preferences;
   - the attribution contextvar reaches `_meter` from a JobQueue job.
10. **Result cache** (`tests/test_llm_result_cache.py`):
    - a key change on `prompt_version` or `house_style`;
    - isolation between orgs;
    - TTL purge;
    - `AUTOMATION_AI_MODE=off` → 0 calls in `tc_author` and the others, while the rules still return a result.

## 8. Rollout

1. Merge with all flags off. The tables and `tsv` are created, and the indexing hooks do nothing.
2. Staging: `PLATFORM_SEARCH=1`, run `tools.search_backfill --global` and then per project, check gate 2.
3. Prod: `PLATFORM_SEARCH=1`. A week of observation (`/api/search/status`, `took_ms` in the logs).
4. `PLATFORM_SEARCH_AI=1` for a single pilot org. Without per-org flags this is done through the org's `settings["search_ai"]=true` as an extra condition; the flag is the global switch.
5. `MODEL_SWITCHER=1` after `ORG_MODE` is on in prod.
6. `AUTOMATION_AI_MODE=assist` is the default from task 2 onwards. `full` is available by env for anyone who needs today's behaviour.

## 9. What this ADR does **not** decide

- The Voyage/fastembed experiment and its key: only the interface and the threshold are fixed here.
- Licensing of the textbook half of the ISTQB corpus: unchanged; it stays in the existing BM25.
- Porting the Dockerfile to the launchpad (port 8080, `/app/data`, 1 worker).
- Search over files and attachments (PDF/DOCX uploads, screenshots).
- Per-org feature flags as a mechanism.

---


---

## Work breakdown (ordered; each task can be merged on its own and is sized for one developer agent)

**T1. Haiku by default, model switcher, LLM attribution.** Depends on nothing.
- **Files:**
  - `engine/llm_models.py`: TIER_DEFAULTS + opus, all KIND_TIER → haiku, new kinds, `SWITCHABLE_KINDS`, `model_for(kind, *, org_id, user_id)`.
  - `engine/llm_prefs.py` (new): org policy/user pref resolver, 30 s cache, premium-on-platform rule.
  - `engine/llm_context.py` (new): contextvar.
  - `engine/llm_client.py`: fill org/project/user from context, pass them to `model_for`.
  - `engine/llm_cost.py`: Haiku 5.5 price, verify.
  - `engine/job_queue.py`: carry context into the worker thread.
  - `app.py`: `before_request` sets the context.
  - `routes/chat.py` (lines 68 and 338): `model_for` with org/user.
  - `routes/settings.py`: `POST /org/settings/models`, `models` in context.
  - New routes `GET/PUT /api/me/model-preference`.
  - `templates/org_settings.html`.
  - `engine/route_policy.py`.
  - `engine/features.py`: `MODEL_SWITCHER` + `_REQUIRES`.
  - `render.yaml`.
  - `engine/i18n/*`.
  - Tests: `tests/test_llm_routing_and_cost.py` (update), `tests/test_model_switcher.py`, `tests/test_llm_attribution_context.py`.
- **Gate:** the `tedgie_eval` run is recorded in the PR.

**T2. `llm_result_cache` and `AUTOMATION_AI_MODE`.** After T1 (shares `llm_client`).
- **Files:**
  - `engine/db.py` (`LlmResultCache` + purge helper).
  - `engine/llm_cache.py` (new).
  - `engine/ai_mode.py` (new).
  - `engine/tc_author.py`, `engine/checklist_author.py`, `engine/test_strategy.py`, `engine/site_recon.py`, `engine/session_segmenter.py`, `engine/mockup_vision.py`: `_PROMPT_VERSION`, `cached_call`, mode gating with a rules-first fallback.
  - `engine/glossary.py` / `engine/tc_house_style.py`: `house_style_hash()`.
  - `engine/retention.py` (purge + delete on project removal).
  - `routes/settings.py` + `templates/org_settings.html` (org lowering of the mode).
  - `render.yaml`.
  - Tests: `tests/test_llm_result_cache.py`, `tests/test_ai_mode.py`.

**T3. Search foundation: schema, capabilities, text, hash embedder.** Independent of T1 and T2.
- **Files:**
  - `engine/search/__init__.py` (`caps`).
  - `engine/search/text.py`: normalise, lang detect, tokeniser moved from `istqb_rag`, snowball + UA light stemmer, translation.
  - `engine/search/lexicon/concepts.json`, `xlate_ua.json`, `xlate_ru.json`.
  - `engine/search/embed.py`: `Embedder` protocol, `HashEmbedder`, `get_embedder()`.
  - `engine/istqb_rag.py`: import the tokeniser from `text.py`, without changing behaviour.
  - `engine/db.py`: `SearchDoc`, `SearchScopeState`, `_ensure_search_schema` in `init_db`.
  - `requirements.txt`: `snowballstemmer`.
  - `.github/workflows/tests.yml`: `pgvector/pgvector:pg16`, a second database without the extension.
  - Tests: `tests/test_search_text.py`, `tests/test_search_embed.py`, `tests/test_schema_migration.py` (extended), `tests/test_search_memory.py`.

**T4. Indexer, hooks, backfill.** After T3.
- **Files:**
  - `engine/search/indexer.py`: per-kind document builders, chunking, content hash, upsert/delete, `search_vector` mirror, global corpora.
  - `engine/search/hooks.py`: `after_flush`/`after_commit`, debounce through `job_queue`.
  - `app.py`: install hooks, background global indexing at boot.
  - `tools/search_backfill.py` (new).
  - `engine/retention.py`: scope state on project delete.
  - `engine/features.py`: `PLATFORM_SEARCH` + `_REQUIRES` on `WORKSPACE_DB_FIRST`.
  - `render.yaml`.
  - Tests: `tests/test_search_indexer.py`, `tests/test_search_hooks.py`.

**T5. Retrieval and `/api/search`, `/api/hints`, `/api/search/status`, `/api/search/reindex`.** After T4.
- **Files:**
  - `engine/search/retrieve.py`: `_scope_clause`, PG FTS / Python BM25, pgvector / Python cosine, RRF, ID fast path, did-you-mean, explanation, deterministic chips, istqb group.
  - `engine/search/hints.py`.
  - `routes/search.py` (new), `routes/__init__.py`.
  - `engine/route_policy.py`.
  - `routes/ops.py`: `search` block in `/readyz`.
  - Tests: `tests/fixtures/search_golden.json`, `tests/test_search_api.py`, `tests/test_search_tenancy.py`, `tests/test_search_golden.py`.

**T6. Ask: `/api/ask` NDJSON, grounding gate, cache, budgets.** After T5 and T1.
- **Files:**
  - `engine/search/answer.py`: prompt, `PROMPT_VERSION`, token packing, citation validation, `SUGGESTIONS:` parsing, caps, rate limiting.
  - `engine/db.py`: `RagAnswerCache` + purge.
  - `engine/retention.py`.
  - `routes/search.py`: `/api/ask`, `/api/ask/feedback`.
  - `engine/features.py`: `PLATFORM_SEARCH_AI`.
  - `engine/route_policy.py`.
  - `render.yaml`.
  - Tests: `tests/test_search_ask.py`.

**T7. UI: global search, AI block, Ask panel, explanation, contextual hints.** After T5. The AI parts are hidden behind `PLATFORM_SEARCH_AI` and can merge before T6.
- **Files:**
  - `templates/base.html` (field + panel mount).
  - `templates/_search_panel.html` (new).
  - `static/js/search.js` (new): typeahead with `quick=1`, 150 ms debounce, NDJSON reader, chips, explanation popover.
  - `static/css/search.css` (or extend the existing css).
  - Hint mounts in `templates/test_cases.html`, `templates/bug_reports.html`, `templates/checklist.html`.
  - `engine/i18n/*` (en/ua; RU queries without an RU UI).
  - Tests: `tests/test_search_ui.py`, a scenario in `tests/test_e2e_golden_paths.py`.

**T8 (optional, last). Tedgie and MCP on the shared retrieval.**
- **Files:**
  - `engine/chatbot.py`: project-grounded answers through `retrieve` before the `istqb_rag` fallback, behind `PLATFORM_SEARCH`.
  - `mcp_server/` (`search` tool).
  - `engine/project_facts.py` (link to search cards).
  - Tests: `tests/test_tedgie_eval.py` (golden set unchanged), `tests/test_mcp_server_tools.py`.

### Critical files for implementation
- F:\ClaudeProjects\TestForTge\.claude\worktrees\testfortge-rag-ai-platform-629f1b\engine\db.py (models, `init_db`, `_ensure_*` migration pattern)
- F:\ClaudeProjects\TestForTge\.claude\worktrees\testfortge-rag-ai-platform-629f1b\engine\llm_client.py and engine\llm_models.py (routing, attribution, budget)
- F:\ClaudeProjects\TestForTge\.claude\worktrees\testfortge-rag-ai-platform-629f1b\routes\_shared.py (`visible_projects`, `project_access_with_meta`, the tenancy source)
- F:\ClaudeProjects\TestForTge\.claude\worktrees\testfortge-rag-ai-platform-629f1b\engine\features.py and engine\route_policy.py (flags, endpoint classification)
