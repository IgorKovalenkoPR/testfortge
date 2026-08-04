"""TestFortge — Tedgie Guide-promised handlers.

The User Guide (templates/guide.html, "What Tedgie is good at"
section) advertises six concrete sample questions Tedgie should
answer. Without specific handlers the rule-based dispatcher in
``engine.chatbot`` either falls through to a generic module help
card, returns only half of a comparison, or dumps a glossary entry
without acting on the user's actual question.

This module isolates the detectors + builders for each promise so
``engine.chatbot.respond`` only needs a few extra lines to wire them
in. Keeping them here also keeps the main chatbot.py focused on
dispatch and avoids long edits to a 1200-line file.

Each builder returns a ``ChatReply`` (re-imported from chatbot).
Detectors take the *lowercased* user message and return either a
boolean, an Optional[str] domain key, or an Optional[(flow, count)]
tuple. The dispatcher in chatbot.respond runs the detectors in order
and short-circuits on the first match.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

from .chatbot import ChatReply, _module_help


# ═══════════════════════════════════════════════════════════════════
# Detectors
# ═══════════════════════════════════════════════════════════════════

def wants_ep_bva_comparison(low: str) -> bool:
    """User asks to compare equivalence partitioning vs boundary value."""
    has_ep = ("equivalence" in low or "еквівалент" in low)
    has_bva = ("boundary" in low or "bva" in low or "граничн" in low)
    cue = any(c in low for c in (
        "diff", "vs ", " vs", "compare", " and ", "between",
        "різниц", "проти", "порівн", " та ", " і ",
    ))
    return has_ep and has_bva and cue


def wants_testing_types_for_domain(low: str) -> Optional[str]:
    """Detect 'which testing types apply to a <domain>' style questions."""
    type_cue = any(c in low for c in (
        "testing types", "testing type", "test types", "test type",
        "types of testing", "kinds of testing", "which test", "what test",
        "типи тестування", "види тестування", "тип тесту",
    ))
    if not type_cue:
        return None
    if "payment" in low or "checkout" in low or "оплат" in low or "чекаут" in low:
        return "payment"
    if ("login" in low or "sign in" in low or "sign-in" in low or "auth" in low
            or "вхід" in low or "логін" in low):
        return "login"
    if "search" in low or "пошук" in low:
        return "search"
    if ("register" in low or "registration" in low or "sign up" in low
            or "sign-up" in low or "реєстрац" in low):
        return "registration"
    return None


def wants_live_view_diag(low: str) -> bool:
    """User asks about an empty / blank live view (Test Execution module)."""
    target = ("live view" in low or "live preview" in low
              or "live screen" in low or "live-перегляд" in low
              or "лайв-перегляд" in low or "лайв перегляд" in low)
    cue = any(c in low for c in (
        "empty", "blank", "nothing", "no screenshot", "no image",
        "doesn't show", "not showing", "broken",
        "порожн", "пуст", "нічого не", "не показу", "не відобр",
    ))
    return target and cue


def wants_bug_summary(low: str) -> bool:
    """User asks to summarise / group session bug reports."""
    target = any(t in low for t in (
        "bug", "defect", "issue", "баг", "дефект", "помилк",
    ))
    action = any(a in low for a in (
        "summar", "group", "grouped", "by component", "by module",
        "by severity", "by status", "breakdown", "stats", "overview of",
        "розпод", "групу", "по компонент", "по модул", "по severity",
        "статистик",
    ))
    return target and action


_NEG_FLOW_KEYWORDS: Tuple[str, ...] = (
    "login", "sign in", "sign-in", "вхід", "логін",
    "registration", "register", "sign up", "sign-up", "реєстрац",
    "checkout", "payment", "оплат", "чекаут",
    "search", "пошук",
    "password reset", "forgot password", "відновл паролю",
    "upload", "завантаж",
)


def wants_negative_cases(low: str) -> Optional[Tuple[str, int]]:
    """Detect 'suggest N negative cases for <flow>' style questions.

    Returns (flow_key, count) when matched, otherwise None.
    """
    if not any(c in low for c in (
        "negative", "missing case", "edge case", "missing test",
        "нега", "не вистача", "крайов",
    )):
        return None
    intent_cue = any(c in low for c in (
        "suggest", "give me", "list", "generate", "show", "what are",
        "i'm missing", "im missing", "need", "more cases",
        "запропон", "дай", "покаж", "перелік", "не вистача", "потрібн",
    ))
    if not intent_cue:
        return None
    flow = next((kw for kw in _NEG_FLOW_KEYWORDS if kw in low), None)
    if flow is None:
        return None
    m = re.search(r"\b(\d{1,2})\b", low)
    count = int(m.group(1)) if m else 6
    count = max(3, min(count, 12))
    if flow in ("login", "sign in", "sign-in", "вхід", "логін"):
        flow = "login"
    elif flow in ("registration", "register", "sign up", "sign-up", "реєстрац"):
        flow = "registration"
    elif flow in ("checkout", "payment", "оплат", "чекаут"):
        flow = "payment"
    elif flow in ("search", "пошук"):
        flow = "search"
    elif flow in ("password reset", "forgot password", "відновл паролю"):
        flow = "password_reset"
    elif flow in ("upload", "завантаж"):
        flow = "upload"
    return flow, count


def wants_severity_recommendation(low: str) -> bool:
    """User asks for a severity recommendation for a specific defect."""
    if "severity" not in low and "критичн" not in low:
        return False
    intent = any(c in low for c in (
        "good severity", "right severity", "what severity",
        "which severity", "should i", "recommend", "what's a good",
        "rate this", "assign severity", "set severity",
        "яку критичн", "яка критичн", "яке severity",
        "порадь", "оцін",
    ))
    return intent


# ═══════════════════════════════════════════════════════════════════
# Builders
# ═══════════════════════════════════════════════════════════════════

def ep_bva_comparison_reply(lang: str) -> ChatReply:
    """Side-by-side EP vs BVA comparison with worked example."""
    try:
        from .istqb_knowledge import TEST_TECHNIQUES
        ep = TEST_TECHNIQUES.get("equivalence partitioning", "")
        bva = TEST_TECHNIQUES.get("boundary value analysis", "")
    except Exception:
        ep = bva = ""
    if lang == "ua":
        text = (
            "**Equivalence Partitioning vs Boundary Value Analysis "
            "(ISTQB CTFL §4.2.1–§4.2.2)**\n\n"
            "**Equivalence Partitioning (EP)** — ділить вхід/вихід на класи "
            "еквівалентності. По одному значенню з кожного класу вистачає, "
            "бо очікується ідентична обробка.\n\n"
            "**Boundary Value Analysis (BVA)** — фокус на межах класів EP, "
            "де дефекти зустрічаються найчастіше. Тестують значення *на "
            "межі* і *поряд з нею*.\n\n"
            "**Зв'язок:** BVA доповнює EP, а не замінює: спершу будуються "
            "класи (EP), потім — їх межі (BVA).\n\n"
            "**Приклад — поле «Вік», валідне 18–65:**\n"
            "• EP-класи: <18 (невал.), 18–65 (вал.), >65 (невал.) → "
            "по одному значенню з кожного: 10, 30, 80.\n"
            "• BVA: 17, 18, 19, 64, 65, 66 (двозначна BVA) — або "
            "17, 18, 65, 66 (тризначна).\n"
            "• Разом: 10, 17, 18, 19, 30, 64, 65, 66, 80 — повне "
            "покриття класів і їх меж."
        )
    else:
        text = (
            "**Equivalence Partitioning vs Boundary Value Analysis "
            "(ISTQB CTFL §4.2.1–§4.2.2)**\n\n"
            "**Equivalence Partitioning (EP)** — splits input/output into "
            "partitions where every value in a partition is processed the "
            "same way. One value per partition is enough.\n\n"
            "**Boundary Value Analysis (BVA)** — focuses on the edges of "
            "those partitions, where defects cluster. Test values *on* and "
            "*next to* the boundary.\n\n"
            "**Relationship:** BVA *complements* EP, it does not replace it. "
            "Build partitions first (EP), then exercise their boundaries "
            "(BVA).\n\n"
            "**Worked example — Age field, valid 18–65:**\n"
            "• EP partitions: <18 (invalid), 18–65 (valid), >65 (invalid) → "
            "one value each: 10, 30, 80.\n"
            "• BVA: 17, 18, 19, 64, 65, 66 (two-value BVA) — or 17, 18, 65, "
            "66 (three-value).\n"
            "• Combined: 10, 17, 18, 19, 30, 64, 65, 66, 80 — covers every "
            "partition AND every boundary, with minimal redundancy."
        )
    if ep and bva:
        text += f"\n\n— *EP (verbatim):* {ep}\n— *BVA (verbatim):* {bva}"
    return ChatReply(
        text=text,
        intent="istqb:equivalence_vs_boundary",
        suggestions=["Show decision tables",
                     "Show state-transition testing",
                     "When does pairwise help?"],
    )


_TESTING_TYPES_FOR_DOMAIN = {
    "payment": {
        "en": [
            ("Functional", "every path through cart → auth → shipping → "
                           "payment → review → confirmation must work for "
                           "card, wallet, and saved methods."),
            ("Security", "PCI-DSS surface: tokenisation, no PAN in logs, "
                         "TLS, idempotency keys, replay protection, 3-D "
                         "Secure step-up."),
            ("Performance", "checkout latency directly hurts conversion — "
                            "stress the payment gateway under peak load and "
                            "measure 95th-percentile time-to-confirmation."),
            ("Compatibility", "wallet APIs (Apple Pay / Google Pay) and "
                              "redirect-based methods behave differently per "
                              "browser, OS and mobile device."),
            ("Accessibility", "WCAG 2.2 AA on payment forms — keyboard "
                              "traversal, screen-reader labels, error "
                              "messages, focus management on validation."),
            ("Reliability", "intermittent gateway failures, retries, "
                            "duplicate-charge prevention, refund and "
                            "chargeback paths."),
        ],
        "ua": [
            ("Functional", "усі гілки cart → auth → shipping → payment → "
                           "review → confirmation мають працювати для картки, "
                           "гаманця та збережених методів."),
            ("Security", "PCI-DSS: токенізація, відсутність PAN у логах, TLS, "
                         "ключі ідемпотентності, захист від повтору, 3-D Secure."),
            ("Performance", "затримка чекаута прямо б'є по конверсії — "
                            "стрес платіжного шлюзу під пік-навантаженням, "
                            "95-й перцентиль часу до підтвердження."),
            ("Compatibility", "Apple Pay / Google Pay і redirect-флоу "
                              "поводяться по-різному в різних браузерах, ОС "
                              "та мобільних пристроях."),
            ("Accessibility", "WCAG 2.2 AA на формах оплати — клавіатура, "
                              "лейбли скрін-рідера, повідомлення про помилки, "
                              "управління фокусом."),
            ("Reliability", "переривчасті збої шлюзу, ретраї, захист від "
                            "подвійного списання, флоу повернень/чарджбеків."),
        ],
    },
    "login": {
        "en": [
            ("Functional", "happy path + every credential validation rule "
                           "(empty, too long, invalid format, locked, "
                           "expired)."),
            ("Security", "brute-force lockout, password policy, OWASP A07 "
                         "(broken auth), session-fixation, secure / "
                         "HttpOnly / SameSite cookie flags."),
            ("Usability", "error wording, password reveal, remember-me, tab "
                          "order, autofill, paste support."),
            ("Compatibility", "browsers, password-manager extensions, "
                              "passkeys, mobile keyboards."),
            ("Accessibility", "WCAG 2.2 AA — labels, focus order, error "
                              "association, screen-reader announcements."),
        ],
        "ua": [
            ("Functional", "happy path + усі правила валідації."),
            ("Security", "lockout, політика паролів, OWASP A07, "
                         "session-fixation, secure/HttpOnly/SameSite."),
            ("Usability", "тексти помилок, показ паролю, remember-me, "
                          "порядок Tab, автозаповнення, вставка."),
            ("Compatibility", "браузери, менеджери паролів, passkeys, "
                              "мобільні клавіатури."),
            ("Accessibility", "WCAG 2.2 AA — лейбли, порядок фокусу, "
                              "повідомлення для скрін-рідера."),
        ],
    },
    "search": {
        "en": [
            ("Functional", "exact, partial, multi-token, no-match and filter "
                           "combinations return correct results."),
            ("Performance", "p95 query latency under load, autocomplete "
                            "debounce, pagination cost."),
            ("Usability", "spell-correction, recent searches, empty-state, "
                          "result highlighting."),
            ("Accessibility", "ARIA combobox pattern, keyboard navigation of "
                              "suggestions, screen-reader result count."),
            ("Compatibility", "browsers, mobile keyboards, IME input."),
        ],
        "ua": [
            ("Functional", "точний, частковий, багатослівний, без збігу, "
                           "фільтри повертають коректні результати."),
            ("Performance", "p95 затримка під навантаженням, debounce."),
            ("Usability", "виправлення опечаток, нещодавні пошуки, empty-state."),
            ("Accessibility", "ARIA combobox, клавіатура, озвучення."),
            ("Compatibility", "браузери, мобільні клавіатури, IME."),
        ],
    },
    "registration": {
        "en": [
            ("Functional", "every required field, password rules, email/"
                           "phone verification, duplicate-account handling."),
            ("Security", "captcha effectiveness, rate-limit, email "
                         "enumeration, password storage policy."),
            ("Usability", "field-level error wording, progress indicator, "
                          "inline validation timing, terms-of-service link."),
            ("Accessibility", "WCAG 2.2 AA — labels, errors, focus order."),
            ("Compatibility", "browsers, password managers, mobile."),
        ],
        "ua": [
            ("Functional", "усі обов'язкові поля, правила паролю, "
                           "верифікація email/телефону, дубль-акаунти."),
            ("Security", "ефективність капчі, rate-limit, перебір email."),
            ("Usability", "тексти помилок, індикатор прогресу, інлайн-"
                          "валідація, посилання на умови."),
            ("Accessibility", "WCAG 2.2 AA — лейбли, помилки, фокус."),
            ("Compatibility", "браузери, менеджери паролів, мобільні."),
        ],
    },
}


def testing_types_for_domain_reply(domain: str, lang: str) -> ChatReply:
    rows = _TESTING_TYPES_FOR_DOMAIN.get(domain, {}).get(lang)
    if not rows and lang != "en":
        rows = _TESTING_TYPES_FOR_DOMAIN.get(domain, {}).get("en", [])
    if not rows:
        return _module_help("test_cases", lang)
    bullets = "\n".join(f"• **{n}** — {r}" for n, r in rows)
    titles_en = {
        "payment": "Testing types that apply to a payment / checkout flow",
        "login": "Testing types that apply to a login flow",
        "search": "Testing types that apply to a search flow",
        "registration": "Testing types that apply to a registration flow",
    }
    titles_ua = {
        "payment": "Типи тестування для платіжного флоу",
        "login": "Типи тестування для логін-флоу",
        "search": "Типи тестування для пошуку",
        "registration": "Типи тестування для реєстрації",
    }
    title = (titles_ua if lang == "ua" else titles_en).get(domain, "Testing types")
    return ChatReply(
        text=f"**{title}**\n\n{bullets}",
        intent=f"istqb:types_for_{domain}",
        suggestions=["Show ISTQB test types overview",
                     "Show test design techniques",
                     "Functional vs non-functional"],
    )


def live_view_diag_reply(lang: str) -> ChatReply:
    if lang == "ua":
        text = (
            "**Лайв-перегляд порожній — діагностичний чек-ліст**\n\n"
            "1. **Base URL заданий?** На картці Test Execution має бути "
            "повний URL з протоколом (https://…). Без нього раннер не знає, "
            "що відкривати.\n"
            "2. **Playwright імпортується?** У логах має бути `playwright "
            "import OK`. Якщо ні — `pip install playwright && playwright "
            "install` у середовищі Flask.\n"
            "3. **Скріншоти пишуться?** Перевірте `outputs/screenshots/` і "
            "права запису. Headless без потрібного `--shm-size` у Docker "
            "дає чорні png.\n"
            "4. **Картка Test Account заповнена?** Якщо флоу за логіном, без "
            "неї перший крок впаде на 401, і скріншот не зробиться.\n"
            "5. **Browser context живий?** На /healthz має бути "
            "`browser_pool: ok`. Якщо ні — перезапустіть worker.\n\n"
            "Якщо всі п'ять зелені — пришліть фрагмент логу runner за час "
            "прогону, я допоможу його розібрати."
        )
    else:
        text = (
            "**Live view is empty — diag checklist**\n\n"
            "1. **Base URL set?** The Test Execution card needs a full URL "
            "including the protocol (https://…). Without it the runner has "
            "no target to open.\n"
            "2. **Playwright importable?** Logs should show `playwright "
            "import OK`. If not, run `pip install playwright && playwright "
            "install` in the Flask environment.\n"
            "3. **Screenshots writing?** Check `outputs/screenshots/` exists "
            "and is writable. Headless Docker without enough `--shm-size` "
            "produces black PNGs.\n"
            "4. **Test Account card filled?** If the flow is auth-gated, an "
            "empty card means the first navigation 401s and no screenshot "
            "is taken.\n"
            "5. **Browser context alive?** /healthz should report "
            "`browser_pool: ok`. If not, restart the runner worker.\n\n"
            "If all five are green, paste the runner log slice for the "
            "failing run and I'll help diagnose."
        )
    return ChatReply(
        text=text,
        intent="diag_live_view_empty",
        suggestions=["Show Test Execution help",
                     "How do I add a test account?",
                     "Show Automation QA help"],
    )


def summarise_bugs_by_component_reply(lang: str) -> ChatReply:
    """Read session bugs and group them by component (and severity)."""
    bugs = []
    try:
        from flask import session, has_request_context
        if has_request_context():
            # The project's bugs, not this browser's. Reading
            # ``bug_reports_data`` meant Tedgie summarised whatever the
            # asker's session happened to hold — nothing at all after a
            # restart, and a different answer for each teammate looking at
            # the same project.
            #
            # Only the project *pointer* comes from the session here.
            # Reading a pointer is a different thing from reading the data,
            # and engine.permissions already does the same.
            from engine import workspace as _workspace
            bugs = _workspace.bugs(session.get("project_id") or "") or []
    except Exception:
        bugs = []
    if not bugs:
        if lang == "ua":
            text = ("Я не бачу збережених багів у вашій сесії. Створіть "
                    "репорт через Bug Reports або запустіть Test Execution "
                    "— тоді я зможу зробити зведення по компонентах.")
        else:
            text = ("I don't see any saved bugs in your session yet. Create "
                    "one via Bug Reports or run Test Execution — after that "
                    "I can group the latest 10 by component.")
        return ChatReply(text=text, intent="bug_summary_empty")
    last = sorted(bugs, key=lambda b: b.get("created_at") or "")[-10:]
    by_comp: dict = {}
    for b in last:
        comp = (b.get("component") or "").strip() or "(unspecified)"
        by_comp.setdefault(comp, []).append(b)
    header = (f"**Зведення останніх {len(last)} багів за компонентом**"
              if lang == "ua"
              else f"**Summary of the last {len(last)} bug(s) by component**")
    lines = [header]
    for comp in sorted(by_comp):
        items = by_comp[comp]
        sev_counts: dict = {}
        for b in items:
            s = (b.get("severity") or "").strip() or "Unknown"
            sev_counts[s] = sev_counts.get(s, 0) + 1
        sev_part = ", ".join(f"{k}: {v}" for k, v in sorted(sev_counts.items()))
        lines.append(f"\n**{comp}** ({len(items)}; {sev_part})")
        for b in items[:5]:
            bid = b.get("id") or "?"
            title = (b.get("title") or "").strip() or "(no title)"
            sev = (b.get("severity") or "").strip() or "Unknown"
            lines.append(f"• `{bid}` [{sev}] {title}")
        if len(items) > 5:
            lines.append(f"…+{len(items)-5} more in this component")
    return ChatReply(
        text="\n".join(lines),
        intent="bug_summary_by_component",
        suggestions=["Show defect lifecycle",
                     "What is severity?",
                     "Open Bug Reports module"],
    )


_NEGATIVE_IDEAS = {
    "login": {
        "en": [
            "Empty username and password — both fields submitted blank.",
            "Whitespace-only credentials (e.g. \"   \" / \"   \").",
            "Wrong password 5+ times in a row — verify lockout & cooldown.",
            "SQL-injection probe in username field (e.g. ' OR 1=1 --).",
            "XSS payload in username field (e.g. <script>alert(1)</script>).",
            "Unicode / emoji in password — confirm normalisation policy.",
            "Maximum-length username (e.g. 256 chars) — truncation behaviour.",
            "Login with a deactivated / suspended account.",
            "Login with an unverified email (verification still pending).",
            "Expired session cookie — re-auth flow on protected page.",
            "Password reset link reused after a successful first reset.",
            "Login rate-limit: 100 attempts/sec from same IP — should 429.",
        ],
        "ua": [
            "Порожні логін і пароль — обидва поля без даних.",
            "Тільки пробіли у полях.",
            "Неправильний пароль 5+ разів — lockout і cooldown.",
            "SQL-injection у логіні (' OR 1=1 --).",
            "XSS-навантаження у логіні.",
            "Unicode / емодзі у паролі.",
            "Максимальна довжина логіну.",
            "Вхід заблокованого / призупиненого акаунта.",
            "Вхід з неверифікованою поштою.",
            "Просрочена session cookie.",
            "Повторне використання вже вжитого password-reset лінку.",
            "Rate-limit: 100 спроб/сек з однієї IP — має бути 429.",
        ],
    },
    "registration": {
        "en": [
            "Email without @ or with two @ symbols.",
            "Already-registered email — duplicate handling and message wording.",
            "Password that violates policy (too short, no digit, no symbol).",
            "Password equals username — should be rejected.",
            "Disposable / mailinator-style email domains — policy enforcement.",
            "Captcha skipped via direct POST — server must reject.",
            "Concurrent submissions of the same form — single account created.",
            "Long names with diacritics / non-Latin scripts.",
            "Cancelling email-verification mid-way.",
            "Tampered hidden fields (role=admin) — privilege escalation blocked.",
        ],
        "ua": [
            "Email без @ або з двома @.",
            "Вже зареєстрований email — дубль і текст помилки.",
            "Пароль, що порушує політику.",
            "Пароль = логін — має бути відхилено.",
            "Тимчасові email-домени.",
            "Прямий POST в обхід капчі.",
            "Подвійний сабміт.",
            "Довгі імена з діакритикою.",
            "Скасування верифікації email.",
            "Підміна прихованих полів (role=admin).",
        ],
    },
    "payment": {
        "en": [
            "Decline test cards from the gateway sandbox — error handling.",
            "Network drop right after authorisation, before confirmation — idempotency.",
            "Double-submit on Place Order — only one charge created.",
            "Expired card / wrong CVV / wrong ZIP — gateway error wording.",
            "Negative or zero quantity in cart — server-side recompute.",
            "Coupon stacking that goes below 0 — total clamped to ≥0.",
            "3-D Secure step-up cancelled by the user — order stays Pending.",
            "Currency mismatch (cart EUR, gateway USD) — clear failure.",
            "Refund partial then full — totals reconcile.",
            "Tampered total in client request — server uses its own total.",
        ],
        "ua": [
            "Тестові картки з декла́йном.",
            "Розрив мережі після авторизації — ідемпотентність.",
            "Подвійний сабміт Place Order.",
            "Прострочена картка / невірний CVV / ZIP.",
            "Негативна / нульова кількість у кошику.",
            "Стек купонів нижче 0 — clamp до ≥0.",
            "Скасування 3-D Secure юзером.",
            "Розбіжність валют (кошик EUR, шлюз USD).",
            "Часткове + повне повернення — баланс сходиться.",
            "Підмінений total у клієнтському запиті.",
        ],
    },
    "search": {
        "en": [
            "Empty query — should not crash; expected empty-state UI.",
            "Single-character query — debounce and minimum-length guard.",
            "Special characters / Unicode / emoji — no 500.",
            "100k-character paste — input length cap.",
            "SQL-injection / NoSQL-injection probe — no info leak.",
            "Filter combination that returns 0 results — empty-state.",
            "Pagination beyond last page — graceful handling.",
            "Concurrent identical queries — same results, no race.",
        ],
        "ua": [
            "Порожній запит — без креша.",
            "Одна літера — debounce.",
            "Спецсимволи / Unicode / емодзі.",
            "Вставка 100k символів.",
            "SQL/NoSQL-injection.",
            "Фільтр, що дає 0 результатів.",
            "Пагінація за межі.",
            "Конкурентні однакові запити.",
        ],
    },
    "password_reset": {
        "en": [
            "Reset link reused after successful first reset.",
            "Reset link expired (older than the policy window).",
            "Multiple reset requests in a row — last token wins, others invalidated.",
            "Reset for a non-existent email — neutral response (no enumeration).",
            "New password equals old — should be rejected if policy disallows.",
            "Reset link tampered (changed user id) — invalid signature.",
        ],
        "ua": [
            "Повторне використання вже вжитого reset-лінку.",
            "Прострочений reset-лінк.",
            "Кілька reset-запитів поспіль.",
            "Reset на неіснуючу пошту — нейтральна відповідь.",
            "Новий пароль = старий.",
            "Підмінений reset-лінк.",
        ],
    },
    "upload": {
        "en": [
            "File above MAX_CONTENT_LENGTH — server returns 413.",
            "Empty file (0 bytes) — graceful rejection.",
            "Mismatched MIME vs extension (.pdf with EXE bytes).",
            "Filename with traversal (../../etc/passwd) — sanitised.",
            "Unicode / emoji filename — stored and rendered correctly.",
            "Concurrent uploads of the same file — no race.",
        ],
        "ua": [
            "Файл понад MAX_CONTENT_LENGTH — 413.",
            "Порожній файл (0 байт).",
            "Невідповідний MIME/розширення.",
            "Filename з traversal.",
            "Unicode / емодзі у назві.",
            "Конкурентні завантаження одного файлу.",
        ],
    },
}


def negative_cases_reply(flow: str, count: int, lang: str) -> ChatReply:
    bank = _NEGATIVE_IDEAS.get(flow, {}).get(lang) \
        or _NEGATIVE_IDEAS.get(flow, {}).get("en", [])
    if not bank:
        return _module_help("test_cases", lang)
    picks = bank[:count]
    titles = {
        "login":          ("Negative cases — Login flow",
                           "Негативні кейси — логін"),
        "registration":   ("Negative cases — Registration flow",
                           "Негативні кейси — реєстрація"),
        "payment":        ("Negative cases — Payment / Checkout",
                           "Негативні кейси — оплата / чекаут"),
        "search":         ("Negative cases — Search",
                           "Негативні кейси — пошук"),
        "password_reset": ("Negative cases — Password reset",
                           "Негативні кейси — скидання паролю"),
        "upload":         ("Negative cases — File upload",
                           "Негативні кейси — завантаження файлу"),
    }
    title = titles.get(flow, ("Negative cases", "Негативні кейси"))[
        0 if lang != "ua" else 1
    ]
    body = "\n".join(f"{i}. {p}" for i, p in enumerate(picks, 1))
    if lang == "ua":
        coda = ("\n\nХочете повний пакет (positive + negative + edge) — "
                "відкрийте Test Cases і додайте цей флоу як user-story.")
    else:
        coda = ("\n\nWant the full set (positive + negative + edge) — "
                "open Test Cases and add this flow as a user story.")
    return ChatReply(
        text=f"**{title}**\n\n{body}{coda}",
        intent=f"negative_cases:{flow}",
        suggestions=["Open Test Cases module",
                     "Show test design techniques",
                     "Show equivalence partitioning"],
    )


def severity_recommendation_reply(message: str, lang: str) -> ChatReply:
    """Recommend a severity level for the described defect."""
    low = (message or "").lower()
    is_intermittent = any(c in low for c in (
        "intermittent", "sometimes", "rare", "flaky", "occasional",
        "переривчас", "інкол", "не завжди",
    ))
    is_blocking = any(c in low for c in (
        "blocking", "blocker", "cannot proceed", "can't proceed",
        "no workaround", "block",
    ))
    is_critical_area = any(c in low for c in (
        "checkout", "payment", "auth", "login", "data loss", "wrong total",
        "money", "charge", "оплат", "чекаут", "вхід", "втрата даних",
    ))
    is_cosmetic = any(c in low for c in (
        "cosmetic", "typo", "alignment", "padding", "color", "wording",
        "косметик", "опечат", "вирівн",
    ))
    if is_cosmetic and not is_blocking:
        rec = ("Minor", "It does not affect functionality; users can still "
                        "complete the workflow.")
    elif is_blocking and is_critical_area:
        rec = ("Critical", "It blocks a business-critical workflow with no "
                           "workaround.")
    elif is_critical_area and is_intermittent:
        rec = ("Major", "Critical-area workflow is impacted, but the issue "
                        "is reproducible only intermittently and a retry "
                        "usually works — that lowers severity from Critical "
                        "to Major.")
    elif is_critical_area:
        rec = ("Major", "A critical-area function is impaired; users may "
                        "still recover via retry or alternate path.")
    elif is_intermittent:
        rec = ("Minor", "Non-critical area and reproducible only "
                        "occasionally — a Minor severity is usually right.")
    else:
        rec = ("Major", "Functionality is impaired but the workflow is not "
                        "completely blocked.")
    if lang == "ua":
        ladder = ("**Шкала severity (ISTQB CTFL §5.5):**\n"
                  "• **Critical** — блокує core-функцію, без обхідного шляху, "
                  "втрата даних, security exposure.\n"
                  "• **Major** — суттєво псує функцію, але є обхідний шлях.\n"
                  "• **Minor** — незручність, не впливає на core-флоу.\n"
                  "• **Trivial** — косметика, опечатки, вирівнювання.")
        text = (f"**Рекомендована severity: {rec[0]}**\n\n"
                f"_Чому:_ {rec[1]}\n\n{ladder}\n\n"
                "Severity = імпакт; priority = терміновість фіксу. Для "
                "переривчастих дефектів у критичних флоу зазвичай ставлять "
                "Major (severity), High (priority) — щоб команда розслідувала "
                "першочергово.")
    else:
        ladder = ("**Severity ladder (ISTQB CTFL §5.5):**\n"
                  "• **Critical** — blocks a core workflow with no "
                  "workaround, or causes data loss / security exposure.\n"
                  "• **Major** — impairs a feature significantly, but a "
                  "workaround exists.\n"
                  "• **Minor** — annoyance; core workflows still work.\n"
                  "• **Trivial** — cosmetic (typos, alignment).")
        text = (f"**Recommended severity: {rec[0]}**\n\n"
                f"_Why:_ {rec[1]}\n\n{ladder}\n\n"
                "Severity = impact; priority = how soon to fix. For an "
                "intermittent defect in a critical area the usual call is "
                "Major (severity) + High (priority) — the team triages it "
                "first because the impact is real even if the repro rate "
                "is low.")
    return ChatReply(
        text=text,
        intent="severity_recommendation",
        suggestions=["What is priority?",
                     "Severity vs priority — examples",
                     "Show defect lifecycle"],
    )


# ═══════════════════════════════════════════════════════════════════
# Top-level dispatcher entry point
# ═══════════════════════════════════════════════════════════════════

def try_guide_handlers(raw: str, low: str, lang: str):
    """Run the six Guide-promised detectors in order.

    Returns a ChatReply if any matches, else None. ``raw`` is the
    original user message (used by the severity recommender for
    keyword analysis), ``low`` is its lowercased form (used by the
    cheap detectors), and ``lang`` is the requested reply language.
    """
    if wants_ep_bva_comparison(low):
        return ep_bva_comparison_reply(lang)
    domain = wants_testing_types_for_domain(low)
    if domain is not None:
        return testing_types_for_domain_reply(domain, lang)
    if wants_live_view_diag(low):
        return live_view_diag_reply(lang)
    if wants_bug_summary(low):
        return summarise_bugs_by_component_reply(lang)
    neg = wants_negative_cases(low)
    if neg is not None:
        flow, count = neg
        return negative_cases_reply(flow, count, lang)
    if wants_severity_recommendation(low):
        return severity_recommendation_reply(raw, lang)
    return None
