"""Delete the LOADTEST tenant(s) — and nothing else.

    python -m tools.loadtest.cleanup_loadtest            # dry run: counts only
    python -m tools.loadtest.cleanup_loadtest --yes      # delete
    python -m tools.loadtest.cleanup_loadtest --org LOADTEST --yes

Guards, in order:

1. ``--org`` must be **exactly** ``LOADTEST`` or ``LOADTEST-B``
   (:func:`tools.loadtest._common.check_org_name`); anything else exits 2
   before the database is opened.
2. The organisation is found by exact name; zero matches is a no-op, two or
   more is a refusal (exit 2) — the script never guesses.
3. Projects are selected by ``project.org_id`` = that organisation's id —
   never by name.
4. A user is deleted only when their email matches the seed's pattern
   (``lt-…@loadtest.testforge.invalid``) **and** they belong to no other
   organisation. Anyone else is detached from the load-test org only.

Deletion uses the product's own paths: ``retention.delete_project_data``
(blobs, every content table, search docs + scope state, AI caches) then
``db.delete_project``. Afterwards it removes the org's leftover search docs,
answer/result caches, usage and audit rows (``--keep-audit`` keeps the
audit), memberships, invites, secrets, the seeded users and the org row.

It prints the row counts of **every other organisation** (and of the
org-less/global rows) before and after, and exits 1 if any of them changed.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.loadtest import _common as C  # noqa: E402


def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="python -m tools.loadtest.cleanup_loadtest",
        description="Delete the LOADTEST tenant(s) only.")
    ap.add_argument("--org", action="append", default=[],
                    help=f"organisation to delete (exact name; one of "
                         f"{', '.join(C.ORG_NAMES)}). Repeatable. "
                         f"Default: all of them.")
    ap.add_argument("--yes", action="store_true",
                    help="actually delete (without it: dry run)")
    ap.add_argument("--keep-audit", action="store_true",
                    help="keep the org's audit_log rows")
    return ap.parse_args(argv)


# ── Snapshot of everything that is NOT ours ───────────────────────────

def other_orgs_snapshot(own_ids: set[str]) -> dict:
    """Row counts per foreign organisation, plus org-less/global rows.

    The proof that a cleanup touched only its own tenant: this must be
    identical before and after.
    """
    from sqlalchemy import func, select

    from engine import db as _db
    from engine.db import (AuditLog, BugReport, ChecklistItem, Organization,
                           OrgMember, Project, SearchDoc, TestCase, User)
    out: dict[str, dict] = {}
    with _db.session_scope() as sess:
        orgs = sess.query(Organization.id, Organization.name).all()
        for oid, name in orgs:
            if oid in own_ids:
                continue
            pids = [p for (p,) in sess.query(Project.id).filter(
                Project.org_id == oid).all()]

            def n(model, col, values):
                if not values:
                    return 0
                return int(sess.execute(select(func.count()).select_from(
                    model).where(col.in_(values))).scalar() or 0)
            out[f"{name} [{oid[:8]}]"] = {
                "projects": len(pids),
                "test_cases": n(TestCase, TestCase.project_id, pids),
                "checklist_items": n(ChecklistItem, ChecklistItem.project_id,
                                     pids),
                "bugs": n(BugReport, BugReport.project_id, pids),
                "members": n(OrgMember, OrgMember.org_id, [oid]),
                "search_docs": n(SearchDoc, SearchDoc.project_id, pids),
                "audit": n(AuditLog, AuditLog.org_id, [oid]),
            }

        def c(stmt):
            return int(sess.execute(stmt).scalar() or 0)
        out["(no org)"] = {
            "projects": c(select(func.count(Project.id)).where(
                Project.org_id.is_(None))),
            "search_docs_global": c(select(func.count(SearchDoc.id)).where(
                SearchDoc.project_id.is_(None))),
            "users_not_loadtest": sum(
                1 for (e,) in sess.query(User.email).all()
                if not C.is_loadtest_email(e)),
        }
    return out


# ── What belongs to us ────────────────────────────────────────────────

def resolve_orgs(names: list[str]) -> dict[str, str]:
    """``{name: org_id}`` for the requested load-test orgs that exist.

    Raises :class:`RefusedError` for a foreign name or an ambiguous one.
    """
    from engine import db as _db
    from engine.db import Organization
    for name in names:
        C.check_org_name(name)
    found: dict[str, str] = {}
    with _db.session_scope() as sess:
        for name in names:
            ids = [oid for (oid,) in sess.query(Organization.id).filter(
                Organization.name == name).all()]
            if len(ids) > 1:
                raise C.RefusedError(
                    f"{len(ids)} organisations are named {name!r}; refusing "
                    f"to guess. Resolve by hand.")
            if ids:
                found[name] = ids[0]
    return found


def plan(org_ids: set[str]) -> dict:
    """What a cleanup would remove — project ids, deletable users, counts."""
    from engine import db as _db
    from engine.db import OrgMember, Project, User
    with _db.session_scope() as sess:
        projects = [(pid, name) for pid, name in sess.query(
            Project.id, Project.name).filter(Project.org_id.in_(org_ids))]
        member_ids = {uid for (uid,) in sess.query(OrgMember.user_id).filter(
            OrgMember.org_id.in_(org_ids)).all()}
        deletable, detach_only = [], []
        for uid in member_ids:
            user = sess.get(User, uid)
            foreign = [o for (o,) in sess.query(OrgMember.org_id).filter(
                OrgMember.user_id == uid).all() if o not in org_ids]
            if user is not None and C.is_loadtest_email(user.email) \
                    and not foreign:
                deletable.append((uid, user.email))
            else:
                detach_only.append(uid)
    return {"projects": projects, "users": deletable,
            "detach_only": detach_only}


def delete_everything(org_ids: dict[str, str], the_plan: dict, *,
                      keep_audit: bool, out) -> None:
    from engine import db as _db
    from engine import retention
    from engine.db import (AuditLog, AuthToken, Identity, Invite, LlmResultCache,
                           LlmUsage, Organization, OrgMember, OrgSecret,
                           RagAnswerCache, SearchDoc, ServerSession, User,
                           UserSetting)

    by_id = {v: k for k, v in org_ids.items()}
    proj_org = {}
    with _db.session_scope() as sess:
        from engine.db import Project
        for pid, oid in sess.query(Project.id, Project.org_id).filter(
                Project.org_id.in_(list(by_id))):
            proj_org[pid] = oid
    for pid, name in the_plan["projects"]:
        rep = retention.delete_project_data(pid, org_id=proj_org.get(pid))
        if not rep.ok:
            raise C.RefusedError(f"project {name!r}: {rep.problem}")
        _db.delete_project(pid)        # no-op when already gone; drops docs
        print(f"  deleted project {name!r} ({pid[:8]}): "
              f"{rep.row_total} rows, {rep.blobs} blobs", file=out)

    user_ids = [uid for uid, _e in the_plan["users"]]
    scopes = list(by_id) + [f"u:{u}" for u in user_ids]
    with _db.session_scope() as sess:
        oids = list(by_id)
        n_docs = sess.query(SearchDoc).filter(SearchDoc.org_id.in_(oids)) \
            .delete(synchronize_session=False)
        n_rag = sess.query(RagAnswerCache).filter(
            RagAnswerCache.org_id.in_(scopes)).delete(synchronize_session=False)
        n_llm = sess.query(LlmResultCache).filter(
            LlmResultCache.org_id.in_(scopes)).delete(synchronize_session=False)
        n_use = sess.query(LlmUsage).filter(LlmUsage.org_id.in_(oids)) \
            .delete(synchronize_session=False)
        n_aud = 0
        if not keep_audit:
            n_aud = sess.query(AuditLog).filter(AuditLog.org_id.in_(oids)) \
                .delete(synchronize_session=False)
        sess.query(Invite).filter(Invite.org_id.in_(oids)) \
            .delete(synchronize_session=False)
        sess.query(OrgSecret).filter(OrgSecret.org_id.in_(oids)) \
            .delete(synchronize_session=False)
        sess.query(OrgMember).filter(OrgMember.org_id.in_(oids)) \
            .delete(synchronize_session=False)
        if user_ids:
            sess.query(Identity).filter(Identity.user_id.in_(user_ids)) \
                .delete(synchronize_session=False)
            sess.query(AuthToken).filter(AuthToken.user_id.in_(user_ids)) \
                .delete(synchronize_session=False)
            sess.query(UserSetting).filter(UserSetting.owner.in_(user_ids)) \
                .delete(synchronize_session=False)
            sess.query(ServerSession).filter(
                ServerSession.user_id.in_(user_ids)) \
                .delete(synchronize_session=False)
            if not keep_audit:
                n_aud += sess.query(AuditLog).filter(
                    AuditLog.user_id.in_(user_ids),
                    AuditLog.org_id.is_(None)).delete(
                        synchronize_session=False)
            sess.query(User).filter(User.id.in_(user_ids)) \
                .delete(synchronize_session=False)
        sess.query(Organization).filter(Organization.id.in_(oids)) \
            .delete(synchronize_session=False)
    print(f"  removed: {n_docs} leftover search docs, {n_rag} cached answers, "
          f"{n_llm} cached results, {n_use} usage rows, {n_aud} audit rows, "
          f"{len(user_ids)} users, {len(by_id)} orgs", file=out)


def main(argv=None, *, out=None) -> int:
    out = out or sys.stdout
    args = _parse(argv)
    names = args.org or list(C.ORG_NAMES)
    try:
        for name in names:
            C.check_org_name(name)
    except C.RefusedError as exc:
        print(f"REFUSED: {exc}", file=out)
        return 2

    from engine import db as _db
    _db.init_db()
    try:
        orgs = resolve_orgs(names)
    except C.RefusedError as exc:
        print(f"REFUSED: {exc}", file=out)
        return 2
    if not orgs:
        print(f"nothing to do: no organisation named {', '.join(names)}",
              file=out)
        return 0
    own = set(orgs.values())
    the_plan = plan(own)
    before = other_orgs_snapshot(own)

    print("load-test orgs: " + ", ".join(f"{n} [{i[:8]}]"
                                         for n, i in orgs.items()), file=out)
    print(f"  projects: {len(the_plan['projects'])} "
          f"({', '.join(n for _p, n in the_plan['projects'])})", file=out)
    print(f"  users to delete: {len(the_plan['users'])}; "
          f"members only detached: {len(the_plan['detach_only'])}", file=out)
    print("other organisations BEFORE:", file=out)
    for k, v in before.items():
        print(f"  {k}: {v}", file=out)
    if not args.yes:
        print("dry run — pass --yes to delete", file=out)
        return 0

    try:
        delete_everything(orgs, the_plan, keep_audit=args.keep_audit, out=out)
    except C.RefusedError as exc:
        print(f"STOPPED: {exc}", file=out)
        return 2
    after = other_orgs_snapshot(own)
    print("other organisations AFTER:", file=out)
    for k, v in after.items():
        print(f"  {k}: {v}", file=out)
    if after != before:
        print("MISMATCH: rows outside the load-test tenant changed", file=out)
        return 1
    print("OK: every other organisation is unchanged", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
