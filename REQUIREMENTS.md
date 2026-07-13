# TestFortge — Product Requirements & Limits

> **Scope.** TestFortge — це Flask-базована платформа для QA-команд, що
> автоматизує повний цикл: від парсингу вимог → генерації тест-кейсів і
> чек-лістів → виконання (мануального або через Playwright) →
> репортингу багів → оцінки QA-зусиль. Працює локально або у контейнері.

**Статус:** v1.1 (201 тест, 100% green; unit+integration+functional+E2E).
**Ліцензія:** внутрішня / не визначена.
**Stack:** Python 3.11+ / Flask 3.1 / Flask-Session / Flask-WTF / Playwright 1.49 / Anthropic SDK 0.97 (опційно) / gunicorn.

---

## 1. Функціональні блоки (що робить)

### 1.1. Dashboard
**`GET /`** — головна сторінка зі зведеною інформацією:
- Список збережених проєктів (snapshots у `STORAGE_FOLDER/`).
- Метрики: кількість тест-кейсів/чек-лістів, розподіл за категоріями / пріоритетами.
- Статистика виконання (pass / fail / blocked).
- Розподіл багів за severity та статусом.
- Перелік environments з попередніх запусків.

### 1.2. Парсинг вимог → User Stories
**`GET / POST /test-cases`**, **`GET / POST /checklist`** — приймає вхідні дані (файли / текст / URL) і автоматично:
1. Парсить вимоги (див. §2 для форматів).
2. Розбиває на атомарні User Stories.
3. Призначає пріоритет + категорію.
4. Генерує traceability matrix (story ↔ test case).

### 1.3. Генерація тест-артефактів
| Артефакт | Формат виводу | Маршрут |
|---|---|---|
| Тест-кейси | Markdown, HTML, CSV, XLSX | `/export/<fmt>` (fmt ∈ md / html / csv-testcases / xlsx-testcases) |
| Чек-лісти | Markdown, HTML, CSV, XLSX | `/export/<fmt>` (csv-checklist / xlsx-checklist) |
| Tracебасильність | HTML | вбудовано у `/test-cases` |

Кожен тест-кейс має: ID, summary (нормалізоване «verify that…»), steps, expected result, priority, category. Ревьюється QA Team Lead-модулем, що фіксить voice/duplicate-id/page-number artifacts.

### 1.4. Execution (мануальне виконання)
**`GET / POST /test-execution`**:
- Вибір environment (OS / browser / device) + credentials.
- Пошаговий проход тест-кейсів + чек-лістів.
- Фіксація pass / fail / blocked з коментарями.
- Генерація одноразового тестового акаунта — **`POST /test-execution/generate-account`**.

### 1.5. Bug Reports
**`POST /create-bug-report`** — створення баг-звіту з: severity (critical/high/medium/low), priority, status, steps-to-reproduce, очікуваним/фактичним результатом, attachments.

**`GET /bug-reports`** — перегляд списку з фільтрами.
**`GET /export-bug-reports`** — експорт усієї бази в Markdown.

### 1.6. Automation (Playwright)
| Маршрут | Синхронно / Async | Призначення |
|---|---|---|
| `GET /automation` | — | Форма налаштувань + звіт останнього запуску |
| `POST /automation/run` | sync | Fallback без JS — блокуючий виклик |
| `POST /automation/run-async` | **async** | Submit у JobQueue, повертає `job_id` |
| `GET /automation/status/<job_id>` | — | Polling статусу + фінального звіту |
| `POST /automation/generate-account` | — | Ephemeral тестовий акаунт |
| `GET /automation/asset/<path>` | — | Віддача відео/скрінів з traversal-guard |

Автоматично генеруються Playwright-скрипти з тест-кейсів (через `engine/automation_qa.py → scripts_from_session`), запускаються у headless chromium, створюють відео + скрін-артефакти у `STORAGE_FOLDER/automation_runs/<run_id>/`.

### 1.7. QA Estimation
**`GET /estimation`** + **`POST /estimation/run`** (sync) + **`POST /estimation/run-async`** (async, тільки URL).

Оцінює годинник QA-роботи на базі трьох джерел (у пріоритеті): **URL → файл → текст**. Кроки:
1. Crawl / parse → feature extraction.
2. Breakdown: minutes-per-TC × features × platforms.
3. Coefficients: compatibility rate, bug report rate, PM overhead, max stretch.
4. Вивід: one-platform total / full-compat total / cost (USD).

**`GET /estimation/export`** — XLSX workbook з повним breakdown.

### 1.8. Chat Assistant
**`POST /chat`**, **`GET /chat/history`**, **`POST /chat/reset`** — розмова з AI-помічником (чат-модель з `engine/chatbot.py`). Доповнює робочий процес QA — пропонує тест-ідеї, пояснює підходи, генерує boilerplate.

### 1.9. Project Management (Snapshots)
**`POST /new-session`** — обнулити поточну сесію.
**`POST /save-project`** — snapshot стану сесії у `STORAGE_FOLDER/<name>_<timestamp>/`.
**`GET /load-project/<folder>`** — відновити зі snapshot.
**`POST /delete-project/<folder>`** — видалити snapshot.

### 1.10. Operations Endpoints (no-auth, no-CSRF)
| Маршрут | Призначення | Формат |
|---|---|---|
| `GET /healthz` | Liveness + write-probe до session/storage/upload dirs | JSON, 200/503 |
| `GET /metrics` | JobQueue depth, by_status, by_kind, in_flight, limits | JSON |

### 1.11. i18n
Підтримувані мови: **en** (default), **ua**. Перемикач через `?lang=en|ua` — зберігається у сесії. Некоректні коди → fallback до `en`.

### 1.12. Допоміжні сторінки (docs / static)
`/guide`, `/techniques`, `/tools`, `/requirements`, `/recommendations`, `/status_report`, `/test_metrics` — статичні/шаблонні освітні сторінки з методологіями QA.

---

## 2. Формати вхідних файлів

| Розширення | Парсер | Обмеження |
|---|---|---|
| `.txt`, `.md` | рядково (UTF-8, errors=replace) | — |
| `.docx` | python-docx 1.1.2 (параграфи + таблиці) | — |
| `.doc` | **відхиляється** (треба конвертувати у `.docx`) | — |
| `.xlsx` | openpyxl 3.1.5 (всі листи, pipe-delimited) | — |
| `.csv` | csv-модуль | — |
| `.pdf` | pypdf 5.1.0 + page-number scrubbing | — |
| `.png` / `.jpg` / `.jpeg` | Pillow (тільки метадані; для OCR — інший інструмент) | — |
| `.mp4`, `.webm`, `.avi`, `.mov`, `.mkv`, `.flv`, `.wmv`, `.gif`, `.m4v`, `.3gp`, `.ts`, `.mts`, `.vob`, `.ogv` | тільки метадані (filename + size) — як attachments | — |

**Глобальний ліміт розміру запиту:** `MAX_CONTENT_LENGTH = 64 MB` (конфігурується). При перевищенні — HTTP 413.

---

## 3. Ліміти та конфігурація

### 3.1. Розміри / Контент
| Параметр | Env | Default | Призначення |
|---|---|---|---|
| Max request body | `MAX_CONTENT_LENGTH` | 64 MB | HTTP 413 при перевищенні |
| Chat message chars | `CHAT_MESSAGE_MAX_CHARS` | 4 000 | Анти-abuse, захист сесії |
| Chat history entries | `CHAT_HISTORY_MAX_ENTRIES` | 40 | Rolling window |

### 3.2. Estimation (жорстко зашиті у config.py)
| Параметр | Default | Опис |
|---|---|---|
| `EST_MAX_ADDITIONAL_PLATFORMS` | 30 | Clamp user input |
| `EST_MAX_MINUTES_PER_TC` | 120 | Clamp хв/TC |
| `EST_MAX_BUFFER_PERCENT` | 200 | Clamp буфер розкладу |

### 3.3. Job Queue (async endpoints)
| Параметр | Env | Default | Опис |
|---|---|---|---|
| Worker pool size | `JOB_QUEUE_WORKERS` | 2 | ThreadPoolExecutor width |
| Result retention | `JOB_RETENTION_SECONDS` | 1 800 (30 хв) | Lazy-prune готових jobs |
| Concurrency per session | `MAX_CONCURRENT_JOBS_PER_SESSION` | 3 | **429 + Retry-After** при перевищенні |

**Ізоляція по kind:** `automation` і `estimation` рахуються окремо — завантаження одного виду не блокує інший.
**Fairness:** ліміт per-session, а не глобальний, тож один користувач не може захопити весь пул.

### 3.4. Секрети / Debug
| Параметр | Env | Default | Примітка |
|---|---|---|---|
| Session secret | `SECRET_KEY` | **required у prod** | Без нього app падає при старті; у debug генерується ephemeral |
| HTTPS guard | `BEHIND_HTTPS` | `0` | При `1` → Secure cookie + strict CSRF SSL |
| Debug mode | `FLASK_DEBUG` | `0` | **Ніколи не вмикати у prod** — Werkzeug debugger має RCE |

### 3.5. Логування (Wave D)
| Параметр | Env | Default | Опис |
|---|---|---|---|
| Level | `LOG_LEVEL` | `INFO` | DEBUG/INFO/WARNING/ERROR |
| Format | `LOG_FORMAT` | `text` | `json` для контейнерів / aggregators |

### 3.6. Шляхи
| Env | Default | Опис |
|---|---|---|
| `UPLOAD_FOLDER` | `./uploads/` | Тимчасове файлове сховище |
| `STORAGE_FOLDER` | `./storage/` | Snapshots проєктів + automation assets |
| `SESSION_FILE_DIR` | `./flask_session/` | Filesystem session store |

---

## 4. Security Posture

### 4.1. Session cookies
- `HttpOnly=True` — захист від XSS-крадіжки токенів.
- `SameSite=Lax` — базовий CSRF-захист.
- `Secure=True` (коли `BEHIND_HTTPS=1`) — тільки HTTPS transport.

### 4.2. CSRF
- Глобально увімкнено (`WTF_CSRF_CHECK_DEFAULT=True`).
- Token живе скільки сесія (`WTF_CSRF_TIME_LIMIT=None`).
- `WTF_CSRF_SSL_STRICT` синхронізовано з `BEHIND_HTTPS`.
- Вимикається тільки у `TESTING=True` режимі тестів.
- Вихід: `CSRFError` → `403`.

### 4.3. CSP-заголовки (з `app.py`)
```
default-src 'self';
img-src 'self' data: blob:;
script-src 'self' https://unpkg.com;
style-src 'self' 'unsafe-inline';
frame-ancestors 'none';
```
Плюс: `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
`Referrer-Policy: strict-origin-when-cross-origin`,
`Permissions-Policy` забороняє geolocation/microphone/camera.

### 4.4. Path-traversal захист
- Asset serving (`/automation/asset/<path>`): validator `SAFE_ASSET_RE` + `realpath`-перевірка, що результат всередині `STORAGE_ROOT`.
- Project folder (`/load-project/<folder>` etc.): validator `SAFE_FOLDER_RE` (`^[A-Za-z0-9_\-]{1,80}$`).

### 4.5. Cross-job isolation
Status endpoint кожного виду (`/automation/status/<id>`, `/estimation/status/<id>`) перевіряє `job.kind` матчиться з маршрутом — 404 інакше. Це запобігає leak-у результатів між категоріями.

### 4.6. Session rotation при рестарті сервера
Сесії, persistовані у flask_session/ з попереднього запуску, автоматично обнуляються при першому запиті після рестарту (через `SERVER_START_TIME` маркер).

---

## 5. Observability

### 5.1. Health
**`GET /healthz`** — **liveness** (без БД): 200 при успіху, 503 при деградації. Checks:
- `session_dir_writable`, `storage_dir_writable`, `upload_dir_writable` — write-probe (створення + видалення тестового файлу).

Навмисно **не** пінгує БД — це шлях, який опитує health-check платформи (`render.yaml` → `healthCheckPath: /healthz`). Збій БД не повинен класти весь сервіс.

**`GET /readyz`** — **readiness**: ті ж checks, що й `/healthz`, ПЛЮС `database_reachable` (`SELECT 1`). 200 лише коли все, включно з БД, здорове; інакше 503. Для зовнішніх моніторів / балансувальника з DB-aware алертами.

### 5.2. Metrics
**`GET /metrics`** — JSON:
```json
{
  "uptime_seconds": 123.4,
  "job_queue": {
    "total_tracked": N,
    "by_status": {"pending": 0, "running": 0, "done": 0, "failed": 0},
    "by_kind": {"automation": 0, "estimation": 0},
    "in_flight": 0
  },
  "limits": { ... }
}
```

### 5.3. Structured logs
`LOG_FORMAT=json` → один JSON-запис на рядок (ts, level, logger, message, exc_info) — готово для Loki / Datadog / CloudWatch.

### 5.4. Graceful shutdown
`atexit` + `SIGTERM` / `SIGINT` handlers винищують ThreadPoolExecutor з `cancel_futures=True`, щоб:
- Процес не висів на фінальних Playwright-ранах.
- Queued-but-not-started jobs не стартували після сигналу.
- In-flight jobs мали `--graceful-timeout 30s` вікно на завершення (gunicorn-сторона).

---

## 6. Внутрішня архітектура (high-level)

```
app.py                     ← Flask application factory (~140 LOC)
├── config.py              ← ENV → app.config, secrets, cookie hardening
├── routes/                ← Route modules (register-function pattern)
│   ├── _shared.py         ← helpers (session id, pagination, safe-regex)
│   ├── dashboard.py       ← /
│   ├── chat.py            ← /chat, /chat/history, /chat/reset
│   ├── generation.py      ← /test-cases, /checklist, /export/<fmt>
│   ├── execution.py       ← /test-execution, /create-bug-report, /bug-reports
│   ├── automation.py      ← /automation*, STORAGE_ROOT constant
│   ├── estimation.py      ← /estimation*
│   ├── projects.py        ← /save-project, /load-project, /delete-project
│   └── ops.py             ← /healthz, /metrics
└── engine/                ← Pure-logic modules (no Flask imports)
    ├── job_queue.py       ← ThreadPoolExecutor + lifecycle + shutdown hooks
    ├── log.py             ← logging.StreamHandler + JSONFormatter
    ├── i18n/              ← en.py, ua.py language tables
    ├── chatbot.py         ← LLM chat with tool invocation
    ├── qa_persona.py      ← role detection (admin/customer/…)
    ├── qa_team_lead.py    ← review/fix-up pass on generated artefacts
    ├── automation_qa.py   ← TC → Playwright script synthesis
    ├── automation_runner.py ← Playwright orchestration + report writer
    ├── automation_report.py ← AutomationReport + metrics
    ├── site_crawler.py    ← BeautifulSoup-based crawl for estimation
    ├── qa_estimator.py    ← feature extraction + hours calculation
    ├── file_parser.py     ← multi-format ingestion
    ├── bug_report.py      ← bug report dataclass
    ├── exporter.py        ← Markdown/HTML/CSV/XLSX exporters
    └── testcase_generator.py, user_story_generator.py, knowledge_base.py
```

---

## 7. Deployment

### 7.1. Локально
```bash
pip install -r requirements.txt
python -c "import secrets; print(secrets.token_urlsafe(48))"  # → SECRET_KEY
cp .env.example .env     # заповнити SECRET_KEY
python app.py            # http://127.0.0.1:5000/
```

### 7.2. Docker
```bash
cp .env.example .env     # заповнити SECRET_KEY
docker compose up -d --build
curl http://localhost:5000/healthz
```
- Base image: `mcr.microsoft.com/playwright/python:v1.49.1-jammy`.
- Процес: gunicorn 2 workers × 4 threads.
- Named volumes для `storage/`, `flask_session/`, `uploads/`.
- HEALTHCHECK прив'язано до `/healthz`.

### 7.3. CI
`.github/workflows/tests.yml` — matrix Python 3.11 / 3.12 / 3.13 на `ubuntu-latest`, повний прогін 188 тестів при push / PR.

---

## 8. Відомі обмеження / Non-goals

- **In-memory only job queue.** Jobs зникають при рестарті процесу (by design — синхронізовано з session wipe). Для багато-інстансного deployment потрібен Redis/Celery (не в поточному scope).
- **Немає автентифікації користувачів.** Припускається single-tenant / trusted-network deployment. Multi-user — окрема віха (requires users model + auth layer).
- **Без RBAC.** Всі сесії рівноправні.
- **LLM-провайдер** — залежить від конфігурації `engine/chatbot.py` (OpenAI або інший ключ). Не описано тут.
- **Email/Notifications** — немає. Статус задач polling-only.
- **Images OCR** — не реалізовано (тільки метадані). Потребує зовнішнього Tesseract.
- **Rate limit — тільки concurrency.** Нема leaky-bucket per-minute ліміту — для production-exposed deployment варто додати Flask-Limiter або nginx rate_limit.

---

## 9. Версіонування та статистика

- **Коміти у main:** 5
- **Тести:** 188 (unit + integration + functional + async + ops + rate-limit)
- **LOC (app):** `app.py` ~140, `routes/` ~9 модулів × 100–300 рядків, `engine/` ~20 модулів.
- **Шаблони:** 20 HTML-файлів.
- **Підтримувані Python-версії:** 3.11, 3.12, 3.13 (перевірено у CI).

---

*Документ згенеровано автоматично на основі аудиту кодової бази. При змінах у config.py, routes/, engine/job_queue.py або .env.example — оновлювати синхронно.*
