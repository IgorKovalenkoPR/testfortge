"""Seed the LOADTEST tenant for the release load test.

Run inside the app container (it uses the app's own DB layer, so it writes
wherever ``DATABASE_URL`` points)::

    LOADTEST_PASSWORD='<12+ chars>' python -m tools.loadtest.seed_loadtest \
        --users 20 --tcs 2000 --checklist 1000 --bugs 300 \
        --csv /app/data/loadtest_accounts.csv

What it creates (idempotent — a re-run converges, it never duplicates):

* organisation ``LOADTEST`` with ``lt-admin@…`` (admin) and
  ``lt-user-01..NN@…`` (user);
* organisation ``LOADTEST-B`` with ``lt-b-user@…`` (admin of B) — only for
  the smoke test's tenancy check;
* project ``LOADTEST big`` — N test cases, checklist items and bugs, text
  in English, Ukrainian and Russian so search has something in each
  language, plus the tenant token ``zephyrquartz``;
* project ``LOADTEST gen scratch`` — empty; TC generation writes here, since
  a generated pack *replaces* the project's test cases;
* project ``LOADTEST-B tenancy`` in org B — a handful of rows carrying the
  token ``obsidianlark``.

Every write goes through ``engine.db`` (``create_user``,
``create_organization``, ``add_org_member``, ``upsert_project``,
``save_test_cases``, ``save_checklist``, ``save_bug``) — the same functions
the product uses. The password comes from ``LOADTEST_PASSWORD``; it is
hashed with the product's own policy and never printed. ``--csv`` writes the
account emails and roles (no passwords) for the load test and the smoke.

It does **not** build the search index; run
``python -m tools.search_backfill --all`` (and ``--global``) afterwards.

Exit codes: 0 ok, 2 refused (a guard fired — nothing was written past it).
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.loadtest import _common as C  # noqa: E402

# ── Content ───────────────────────────────────────────────────────────
# (section, English, Ukrainian, Russian). The search query pools in the
# locustfile and the smoke are drawn from these words.
TOPICS: list[tuple[str, str, str, str]] = [
    ("Login", "Login with valid credentials",
     "Вхід у систему з дійсними даними", "Вход в систему с верными данными"),
    ("Password", "Password reset by email link",
     "Скидання пароля за посиланням", "Сброс пароля по ссылке"),
    ("Payment", "Payment by credit card",
     "Оплата банківською карткою", "Оплата банковской картой"),
    ("Checkout", "Checkout with saved address",
     "Оформлення замовлення зі збереженою адресою",
     "Оформление заказа с сохраненным адресом"),
    ("Cart", "Add product to the shopping cart",
     "Додати товар до кошика", "Добавить товар в корзину"),
    ("Search", "Search results show matching products",
     "Пошук показує відповідні товари", "Поиск показывает подходящие товары"),
    ("Profile", "Update profile photo",
     "Оновлення фото профілю", "Обновление фото профиля"),
    ("Upload", "Upload a PDF attachment",
     "Завантаження PDF вкладення", "Загрузка PDF вложения"),
    ("Export", "Export report to CSV",
     "Експорт звіту в CSV", "Экспорт отчета в CSV"),
    ("Registration", "Registration with a new email",
     "Реєстрація з новою поштою", "Регистрация с новой почтой"),
    ("Notification", "Email notification after order",
     "Сповіщення після замовлення", "Уведомление после заказа"),
    ("Session", "Session timeout signs the user out",
     "Тайм-аут сесії виходить із системи", "Тайм-аут сессии выходит из системы"),
]
PRIORITIES = ["High", "Medium", "Low"]
TC_STATUSES = ["Passed", "Failed", "Unchecked", "Blocked"]
BUG_SEVERITIES = ["Critical", "Major", "Minor", "Trivial"]
BUG_PRIORITIES = ["Highest", "High", "Medium", "Low", "Lowest"]


def _phrase(i: int) -> tuple[str, str]:
    section, en, ua, ru = TOPICS[(i // 3) % len(TOPICS)]
    return section, (en, ua, ru)[i % 3]


def make_test_cases(n: int, token: str = C.TENANT_TOKEN_A) -> list[dict]:
    """*n* cases; every 250th (and the first) carries the tenant *token*."""
    out = []
    for i in range(n):
        section, text = _phrase(i)
        marker = f" {token}" if i % 250 == 0 else ""
        out.append({
            "id": f"TC-{i + 1:04d}",
            "summary": f"{text} — variant {i + 1}{marker}",
            "section": section,
            "preconditions": f"1. User account exists\n2. Data set {i % 7}",
            "test_steps": (f"1. Open the {section.lower()} page\n"
                           f"2. {text}\n3. Observe the result"),
            "test_data": "",
            "expected_result": f"1. {text}: the result is correct",
            "priority": PRIORITIES[i % 3],
            "category": "Positive" if i % 4 else "Negative",
            "status": TC_STATUSES[i % 4],
            "testing_type": "Functional",
        })
    return out


def make_checklist(n: int) -> list[dict]:
    out = []
    for i in range(n):
        section, text = _phrase(i + 1)
        out.append({
            "id": f"CL-{i + 1:04d}",
            "section": section,
            "objective": f"Check: {text} ({i + 1})",
            "priority": PRIORITIES[i % 3],
            "category": "Positive",
            "status": "Unchecked",
            "testing_type": "Functional",
            "item_num": f"{(i % 50) + 1}",
        })
    return out


def make_bugs(n: int) -> list[dict]:
    out = []
    for i in range(n):
        section, text = _phrase(i + 2)
        out.append({
            "id": f"BUG-{i + 1:04d}",
            "title": f"{section}: {text} fails with error {500 + i % 4}",
            "severity": BUG_SEVERITIES[i % 4],
            "priority": BUG_PRIORITIES[i % 5],
            "status": "Open",
            "environment": "staging",
            "steps_to_reproduce": f"1. {text}\n2. Submit",
            "actual_result": f"Error {500 + i % 4} is shown",
            "expected_result": f"{text} succeeds",
        })
    return out


# ── DB helpers ────────────────────────────────────────────────────────

def find_org_ids(name: str) -> list[str]:
    """Ids of organisations whose name is *exactly* ``name``."""
    from engine import db as _db
    from engine.db import Organization
    with _db.session_scope() as sess:
        return [oid for (oid,) in sess.query(Organization.id).filter(
            Organization.name == name).all()]


def ensure_org(name: str) -> str:
    from engine import db as _db
    C.check_org_name(name)
    ids = find_org_ids(name)
    if len(ids) > 1:
        raise C.RefusedError(
            f"{len(ids)} organisations are named {name!r}; refusing to guess "
            f"which one is the load-test tenant. Resolve by hand.")
    return ids[0] if ids else _db.create_organization(name)


def _foreign_memberships(user_id: str, own_org_ids: set[str]) -> list[str]:
    from engine import db as _db
    from engine.db import OrgMember
    with _db.session_scope() as sess:
        return [oid for (oid,) in sess.query(OrgMember.org_id).filter(
            OrgMember.user_id == user_id).all() if oid not in own_org_ids]


def ensure_user(email: str, password: str, *, display: str,
                own_org_ids: set[str]) -> str:
    from engine import auth as _auth
    from engine import db as _db
    if not C.is_loadtest_email(email):
        raise C.RefusedError(f"{email!r} is not a load-test address")
    pw_hash = _auth.hash_password(password, email=email)
    existing = _db.get_user_by_email(email)
    if existing:
        foreign = _foreign_memberships(existing["id"], own_org_ids)
        if foreign:
            raise C.RefusedError(
                f"{email} already belongs to organisation(s) outside the "
                f"load test ({len(foreign)}); refusing to touch it")
        _db.set_password_hash(existing["id"], pw_hash)
        _db.clear_login_failures(existing["id"])
        return existing["id"]
    uid = _db.create_user(email, display_name=display,
                          password_hash=pw_hash, email_verified=True)
    if not uid:
        raise C.RefusedError(f"could not create {email}")
    return uid


def ensure_project(name: str, org_id: str) -> str:
    from engine import db as _db
    pid = _db.upsert_project(name, org_id=org_id,
                             description="Load-test data. Safe to delete "
                                         "with tools.loadtest.cleanup_loadtest")
    meta = _db.get_project(pid) or {}
    if meta.get("org_id") != org_id:
        raise C.RefusedError(
            f"project {name!r} already exists in another organisation; "
            f"refusing to write into it")
    return pid


def _counts(pid: str) -> dict:
    from sqlalchemy import func, select

    from engine import db as _db
    from engine.db import BugReport, ChecklistItem, TestCase
    with _db.session_scope() as sess:
        def n(model):
            return int(sess.execute(select(func.count(model.id)).where(
                model.project_id == pid)).scalar() or 0)
        return {"test_cases": n(TestCase), "checklist_items": n(ChecklistItem),
                "bugs": n(BugReport)}


def fill_project(pid: str, *, tcs: int, checklist: int, bugs: int,
                 rewrite: bool, token: str = C.TENANT_TOKEN_A,
                 extra_marker: str | None = None) -> dict:
    from engine import db as _db
    have = _counts(pid)
    if rewrite or have["test_cases"] != tcs:
        _db.save_test_cases(pid, make_test_cases(tcs, token))
    if rewrite or have["checklist_items"] != checklist:
        _db.save_checklist(pid, make_checklist(checklist))
    if rewrite or have["bugs"] != bugs:
        _db.delete_bugs_for_project(pid)
        for bug in make_bugs(bugs):
            if extra_marker:
                bug["title"] += f" {extra_marker}"
            _db.save_bug(pid, bug)
    return _counts(pid)


# ── Main ──────────────────────────────────────────────────────────────

def _parse(argv):
    ap = argparse.ArgumentParser(prog="python -m tools.loadtest.seed_loadtest",
                                 description=__doc__.splitlines()[0])
    ap.add_argument("--users", type=int, default=20,
                    help="plain user accounts in LOADTEST (default 20)")
    ap.add_argument("--tcs", type=int, default=2000)
    ap.add_argument("--checklist", type=int, default=1000)
    ap.add_argument("--bugs", type=int, default=300)
    ap.add_argument("--rewrite", action="store_true",
                    help="rewrite the packs even when the counts match")
    ap.add_argument("--csv", metavar="PATH",
                    help="write email,role,org of every account (no password)")
    args = ap.parse_args(argv)
    if not 1 <= args.users <= 200:
        ap.error("--users must be 1..200")
    for name in ("tcs", "checklist", "bugs"):
        if not 0 <= getattr(args, name) <= 20000:
            ap.error(f"--{name} must be 0..20000")
    return args


def main(argv=None, *, out=None) -> int:
    out = out or sys.stdout
    args = _parse(argv)
    try:
        password = C.password_from_env()
        from engine import auth as _auth
        _auth.validate_password(password)
    except Exception as exc:   # RefusedError or PasswordPolicyError
        print(f"REFUSED: {exc}", file=out)
        return 2

    from engine import db as _db
    _db.init_db()
    try:
        org_a = ensure_org(C.ORG_NAME)
        org_b = ensure_org(C.ORG_NAME_B)
        own = {org_a, org_b}
        accounts = []
        admin = ensure_user(C.admin_email(), password, display="LT Admin",
                            own_org_ids=own)
        _db.add_org_member(org_a, admin, "admin")
        accounts.append((C.admin_email(), "admin", C.ORG_NAME))
        for i in range(1, args.users + 1):
            email = C.user_email(i)
            uid = ensure_user(email, password, display=f"LT User {i:02d}",
                              own_org_ids=own)
            _db.add_org_member(org_a, uid, "user")
            accounts.append((email, "user", C.ORG_NAME))
        b_user = ensure_user(C.b_user_email(), password, display="LT B User",
                             own_org_ids=own)
        _db.add_org_member(org_b, b_user, "admin")
        accounts.append((C.b_user_email(), "admin", C.ORG_NAME_B))

        big = ensure_project(C.BIG_PROJECT, org_a)
        gen = ensure_project(C.GEN_PROJECT, org_a)
        b_proj = ensure_project(C.B_PROJECT, org_b)
        big_counts = fill_project(big, tcs=args.tcs, checklist=args.checklist,
                                  bugs=args.bugs, rewrite=args.rewrite)
        b_counts = fill_project(b_proj, tcs=5, checklist=3, bugs=2,
                                rewrite=args.rewrite,
                                token=C.TENANT_TOKEN_B,
                                extra_marker=C.TENANT_TOKEN_B)
    except C.RefusedError as exc:
        print(f"REFUSED: {exc}", file=out)
        return 2

    if args.csv:
        with open(args.csv, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["email", "role", "org"])
            w.writerows(accounts)
    print(f"org {C.ORG_NAME}: {org_a}  users: {args.users + 1}", file=out)
    print(f"org {C.ORG_NAME_B}: {org_b}  users: 1", file=out)
    print(f"project {C.BIG_PROJECT}: {big} {big_counts}", file=out)
    print(f"project {C.GEN_PROJECT}: {gen} {_counts(gen)}", file=out)
    print(f"project {C.B_PROJECT}: {b_proj} {b_counts}", file=out)
    if args.csv:
        print(f"accounts csv: {args.csv} ({len(accounts)} rows, no passwords)",
              file=out)
    print("next: python -m tools.search_backfill --global --all", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
