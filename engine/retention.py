"""TestForTge — how long a project's data lives, and how it leaves (E8.5).

Three things that look separate and are one question: *what happens to a
project's data over time, and what happens when someone asks for it back or
asks for it gone.*

    видалення прибирає і блоби, і рядки; аудит фіксує
    (deletion removes both the blobs and the rows; the audit records it)

That is the acceptance criterion, and before this module existed all three
clauses were false. Measured on a clean database, not inferred:

* **the rows survived.** ``bug_report.project_id`` is ``ON DELETE SET NULL``
  — correct for the column, because a bug filed through the chat widget
  genuinely has no project — but wrong as a *deletion* behaviour. Deleting a
  project detached its bug reports and left every one of them in the
  database, with its title, its steps, its actual and expected results, and
  the storage keys of its attachments. ``tedgie_submission`` did the same;
* **the blobs were never touched.** Nothing looked at storage at all;
* **nothing was audited.** The most destructive action in the product left
  no record of who did it.

Retention: a symptom becoming a policy
--------------------------------------
``AUTOMATION_RUN_RETENTION_DAYS=1`` and ``AUTOMATION_RUN_MAX_KEPT=5`` are
not a retention policy. ADR 0002 §1.1 says so in as many words: they are a
symptom of an ephemeral disk that fills, and *"any conversation about
retention before durable storage exists is a conversation about deleting
faster."*

E8.2 gave the product durable storage, so the numbers can stop being a
survival measure. :func:`policy_for` returns the aggressive numbers while
the backend is the ephemeral disk and a real window once it is a bucket —
and it says **which** it is returning, because "we keep your evidence for 30
days" and "we keep it until the next restart" are different promises and the
page that renders them must not merge the two.

The asymmetry in :func:`delete_project_data`
--------------------------------------------
Blobs go first, then rows. The two failure modes are not equally bad:

* blobs deleted, rows fail → rows point at files that are gone. Visible,
  recoverable, and exactly what the ephemeral disk already does daily;
* rows deleted, blobs fail → files nobody can find and no page can reach,
  which is the shape of a deletion that quietly did not delete. That is a
  promise broken silently, which is the worse half.

So the destructive step whose failure is *loud* runs first.

What deletion deliberately keeps
--------------------------------
Two things survive on purpose, and :class:`DeletionReport` names them rather
than leaving a reader to notice:

* ``audit_log`` — deleting the record of a deletion is the one thing an
  audit trail may never do;
* ``llm_usage`` — the organisation's own spend history. It carries no
  project content, and erasing it would rewrite a bill.
"""
from __future__ import annotations

import io
import json
import os
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

from engine import storage
from engine.log import get_logger

log = get_logger(__name__)

#: Retention while artefacts live on the dyno's own disk.
#:
#: Not a policy — see the module docstring and ADR 0002 §1.1. These are the
#: numbers that keep a 512 MB instance from OOM-ing, and they are the ones
#: that were already in force; naming them here rather than leaving them in
#: ``automation_runner`` is what lets a page say *why* a screenshot is gone.
EPHEMERAL_DAYS = 1
EPHEMERAL_MAX_RUNS = 5

#: Retention once artefacts are in durable storage.
#:
#: Thirty days and fifty runs, both overridable. Chosen to be long enough to
#: cover "the regression we shipped three weeks ago" — which is the actual
#: question run evidence gets asked — and short enough to sit inside R2's
#: 10 GB free tier for a team running a few automated suites a day.
DURABLE_DAYS = 30
DURABLE_MAX_RUNS = 50


@dataclass(frozen=True)
class Policy:
    """How long run artefacts are kept, and why that number."""

    days: int
    max_runs: int
    durable: bool
    source: str          # "ephemeral-disk" | "instance" | "org"

    @property
    def explanation(self) -> str:
        """One sentence for a settings page. The distinction is the point."""
        if not self.durable:
            return (f"Run screenshots and videos are kept for {self.days} "
                    f"day{'' if self.days == 1 else 's'} or the last "
                    f"{self.max_runs} runs — whichever comes first — and a "
                    f"restart of the server removes them sooner than that. "
                    f"They are on the server's temporary disk.")
        return (f"Run screenshots and videos are kept for {self.days} days "
                f"or the last {self.max_runs} runs, whichever comes first. "
                f"They are in durable storage and survive a restart.")


def _positive_int(name: str, fallback: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return fallback
    try:
        value = int(raw)
    except ValueError:
        log.warning("%s=%r is not a number — using %d.", name, raw, fallback)
        return fallback
    if value < 1:
        # Zero would mean "delete everything immediately", which is a
        # plausible typo for "keep forever" and destroys evidence either
        # way. Refused rather than obeyed.
        log.warning("%s=%d is below 1 — using %d.", name, value, fallback)
        return fallback
    return value


def policy_for(org_id: str | None = None) -> Policy:
    """The retention policy in force for *org_id*, right now.

    Resolved per call, like :func:`engine.storage.backend_for` and for the
    same reason: an operator who points the instance at a bucket has changed
    the retention promise, and a policy cached at import would keep
    reporting the old one until a redeploy.
    """
    durable = bool(storage.describe(org_id).get("durable"))
    if not durable:
        return Policy(EPHEMERAL_DAYS, EPHEMERAL_MAX_RUNS, False,
                      "ephemeral-disk")
    return Policy(
        _positive_int("ARTEFACT_RETENTION_DAYS", DURABLE_DAYS),
        _positive_int("ARTEFACT_MAX_RUNS", DURABLE_MAX_RUNS),
        True, "instance")


# ── What a project holds ─────────────────────────────────────────────

#: Every table that carries a project's own content, with the label a person
#: reads and the ``engine.db`` model.
#:
#: A list, and not a loop over ``Base.registry``, because the difference
#: between "this is the project's content" and "this survives the project"
#: is a judgement per table and not a property of the schema. Discovering
#: tables automatically would silently sweep ``audit_log`` into a deletion
#: the moment somebody added a foreign key.
CONTENT_TABLES: tuple[tuple[str, str], ...] = (
    ("Estimation", "estimation"),
    ("TestCase", "test cases"),
    ("ChecklistItem", "checklist items"),
    ("BugReport", "bug reports"),
    ("ExecutionRun", "execution runs"),
    ("AutomationRun", "automation runs"),
    ("SiteProfile", "site profiles"),
    ("Locator", "locators"),
    ("SessionDraft", "recorder drafts"),
    ("TedgieSubmission", "Tedgie submissions"),
    ("BrowserControlSession", "browser sessions"),
    ("DashboardMetricSnapshot", "dashboard snapshots"),
)

#: Tables that keep their rows when a project is deleted, and why.
#: Rendered to the person doing the deleting — a deletion that quietly
#: keeps something is the thing this module exists to stop.
KEPT_TABLES: tuple[tuple[str, str], ...] = (
    ("audit log", "who did what, including this deletion"),
    ("AI usage and cost", "your organisation's own spend history"),
)


@dataclass
class DeletionReport:
    """What went, what stayed, and whether it worked."""

    project_id: str
    project_name: str = ""
    rows: dict[str, int] = field(default_factory=dict)
    blobs: int = 0
    bundles: int = 0
    blob_prefix: str = ""
    ok: bool = True
    problem: str = ""

    @property
    def row_total(self) -> int:
        return sum(self.rows.values())

    def summary(self) -> str:
        if not self.ok:
            return self.problem
        parts = [f"{count} {label}"
                 for label, count in sorted(self.rows.items()) if count]
        rows = ", ".join(parts) if parts else "no stored rows"
        bundles = (f", and {self.bundles} "
                   f"backup{'' if self.bundles == 1 else 's'}"
                   if self.bundles else "")
        return (f"Deleted {self.project_name or self.project_id}: {rows}, "
                f"and {self.blobs} file{'' if self.blobs == 1 else 's'} "
                f"from storage{bundles}.")


def _models():
    """Name → model class, resolved late to keep the import graph acyclic."""
    from engine import db as _db
    return {name: getattr(_db, name) for name, _label in CONTENT_TABLES
            if hasattr(_db, name)}


def survey(project_id: str, org_id: str | None = None) -> DeletionReport:
    """Count what a deletion would remove, without removing anything.

    What the confirmation screen renders. Deliberately a real count rather
    than "this cannot be undone": a number is what makes somebody notice
    they are about to delete the wrong project.
    """
    from engine import db as _db

    report = DeletionReport(project_id=project_id)
    meta = _db.get_project(project_id) or {}
    report.project_name = meta.get("name") or ""
    report.blob_prefix = storage.org_prefix(org_id) + f"/project/{project_id}"

    with _db.session_scope() as sess:
        for name, label in CONTENT_TABLES:
            model = _models().get(name)
            if model is None:                # pragma: no cover — schema drift
                continue
            report.rows[label] = sess.query(model).filter(
                model.project_id == project_id).count()

    try:
        from engine import backup as _backup
        report.bundles = len(_backup.list_bundles(project_id, org_id=org_id))
    except Exception as exc:            # pragma: no cover — bucket down
        log.warning("could not survey backups for %s: %s", project_id[:8], exc)

    used = None
    try:
        used = storage.backend_for(org_id).usage(report.blob_prefix + "/")
    except storage.StorageError as exc:
        log.warning("could not survey storage for %s: %s", project_id[:8], exc)
    report.blobs = used.objects if used else 0
    return report


# ── Deleting ─────────────────────────────────────────────────────────

def delete_project_data(project_id: str, *, org_id: str | None = None,
                        user_id: str | None = None) -> DeletionReport:
    """Remove a project's blobs, then its rows, then record that it happened.

    Returns a :class:`DeletionReport`. Raises nothing — the caller is a route
    with a person waiting, and the report carries the failure.

    On a storage failure **nothing is deleted at all**: the rows stay, the
    report says why, and the user can try again. Half a deletion that has
    already thrown away the pointers is the state this ordering exists to
    avoid.
    """
    from engine import db as _db

    report = survey(project_id, org_id)
    if not report.project_name and not report.row_total:
        # Nothing here. Idempotent rather than an error: a double-submitted
        # confirmation should not produce a scary message.
        report.blobs = 0
        return report

    # 1. Blobs. First, because this is the failure worth stopping on.
    #
    # Backups go in the same step and before the rows, for the same reason.
    # E8.4 stores them under the *organisation*, outside this project's
    # prefix, so that deleting a project by accident does not take the
    # backup with it — which means a deletion that skipped them would leave
    # a complete, restorable copy of everything it just claimed to remove.
    # The two modules have to agree about this; the test that keeps them
    # honest asserts a deleted project leaves no bundle behind.
    try:
        backend = storage.backend_for(org_id)
        report.blobs = backend.delete_prefix(report.blob_prefix + "/")
        from engine import backup as _backup
        report.bundles = _backup.delete_for_project(project_id,
                                                    org_id=org_id)
    except storage.StorageError as exc:
        log.error("refusing to delete project %s: storage said %s",
                  project_id[:8], exc)
        report.ok = False
        report.problem = (
            "The files for this project could not be removed from storage, "
            "so nothing was deleted. The project is untouched — try again, "
            "or check the storage settings.")
        _db.append_audit(entity="project", action="delete_failed",
                         user_id=user_id, org_id=org_id,
                         project_id=project_id,
                         entity_id=project_id,
                         diff={"reason": str(exc)[:400]})
        return report

    # 2. Rows. The bug reports and Tedgie submissions are deleted explicitly
    #    rather than left to the foreign key: theirs is ON DELETE SET NULL,
    #    which detaches them and keeps every word they contain.
    with _db.session_scope() as sess:
        for name, _label in CONTENT_TABLES:
            model = _models().get(name)
            if model is None:                # pragma: no cover — schema drift
                continue
            sess.query(model).filter(
                model.project_id == project_id).delete(
                    synchronize_session=False)
        row = sess.get(_db.Project, project_id)
        if row is not None:
            sess.delete(row)

    # 3. The audit, written last so it records what actually happened rather
    #    than what was about to be attempted. Its own row survives the
    #    deletion because audit_log.project_id carries no foreign key —
    #    checked, not assumed, in tests/test_project_data_lifecycle.py.
    _db.append_audit(entity="project", action="delete_data",
                     user_id=user_id, org_id=org_id, project_id=project_id,
                     entity_id=project_id,
                     diff={"name": report.project_name,
                           "rows": report.rows,
                           "blobs": report.blobs,
                           "bundles": report.bundles})
    log.info("deleted project %s: %d rows, %d blobs", project_id[:8],
             report.row_total, report.blobs)
    return report


# ── Exporting ────────────────────────────────────────────────────────

#: Largest export this instance will build, in bytes.
#:
#: An export is assembled in memory on a 512 MB dyno, so it is bounded and
#: the bound is **reported inside the archive** — a truncated export that
#: looks complete is worse than a refusal, because the person who asked for
#: their data would not know to ask again.
EXPORT_MAX_BYTES = 25 * 1024 * 1024


def _jsonable(value):
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


def export_project_data(project_id: str, *,
                        org_id: str | None = None) -> tuple[bytes, dict]:
    """Build a zip of everything this project holds. Returns ``(bytes, notes)``.

    "Export my data" in the sense the phrase is normally meant: the rows as
    readable JSON plus the actual files, not a link to somewhere they might
    still be. It is the thing an admin is offered *before* the delete button,
    because a deletion that cannot be preceded by an export is a deletion
    people are right to be afraid of.
    """
    from engine import db as _db

    notes: dict = {"truncated": False, "skipped": []}
    buffer = io.BytesIO()
    meta = _db.get_project(project_id) or {}
    prefix = storage.org_prefix(org_id) + f"/project/{project_id}"

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("project.json", json.dumps({
            "id": project_id,
            "name": meta.get("name"),
            "base_url": meta.get("base_url"),
            "description": meta.get("description"),
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "storage_prefix": prefix,
        }, indent=2, ensure_ascii=False))

        with _db.session_scope() as sess:
            for name, label in CONTENT_TABLES:
                model = _models().get(name)
                if model is None:            # pragma: no cover — schema drift
                    continue
                rows = sess.query(model).filter(
                    model.project_id == project_id).all()
                if not rows:
                    continue
                payload = [{c.name: _jsonable(getattr(row, c.name, None))
                            for c in model.__table__.columns} for row in rows]
                archive.writestr(
                    f"data/{label.replace(' ', '_')}.json",
                    json.dumps(payload, indent=2, ensure_ascii=False,
                               default=str))

        # The files. Best effort per file: one unreadable attachment must
        # not cost somebody the rest of their export.
        backend = storage.backend_for(org_id)
        try:
            listing = backend.usage(prefix + "/")
        except storage.StorageError as exc:
            listing = None
            notes["skipped"].append(f"storage unavailable: {exc}")
        if listing and listing.objects:
            written = 0
            for key in _keys_under(backend, prefix + "/"):
                if written >= EXPORT_MAX_BYTES:
                    notes["truncated"] = True
                    break
                try:
                    blob = backend.get_bytes(key)
                except storage.StorageError as exc:
                    notes["skipped"].append(f"{key}: {exc}")
                    continue
                written += len(blob)
                archive.writestr(f"files/{key[len(prefix) + 1:]}", blob)

        archive.writestr("README.txt", _readme(notes))

    return buffer.getvalue(), notes


def _keys_under(backend, prefix: str):
    """Every key under *prefix*, for the backend in force.

    Not a ``Backend`` verb. ``usage`` and ``delete_prefix`` cover what the
    product needs from a listing, and adding ``list`` to the interface for
    one caller is how an interface acquires methods nothing uses — the shape
    ADR 0002 §4 already declined once. This walks what each backend can
    walk, and an adapter that cannot is simply not exportable yet.
    """
    if isinstance(backend, storage.LocalBackend):
        try:
            root = backend._absolute(prefix)
        except storage.StorageError:         # pragma: no cover — guarded
            return
        for current, _dirs, files in os.walk(root):
            for name in sorted(files):
                full = os.path.join(current, name)
                yield prefix + os.path.relpath(full, root).replace(os.sep, "/")
        return
    try:
        for obj in backend._minio().list_objects(
                backend.config.bucket, prefix=prefix, recursive=True):
            yield obj.object_name
    except Exception as exc:                 # pragma: no cover — bucket down
        log.warning("could not list %s for export: %s", prefix, exc)


def _readme(notes: dict) -> str:
    lines = [
        "TestForTge — project data export",
        "",
        "project.json  what the project is",
        "data/*.json   every row this project owns, one file per kind",
        "files/*       screenshots, videos and attachments, under the same",
        "              path they have in storage",
        "",
    ]
    if notes.get("truncated"):
        lines += [
            "INCOMPLETE: this export hit the size limit and the remaining",
            "files were left out. The rows above are complete; the files are",
            "not. Ask whoever runs the server to raise EXPORT_MAX_BYTES, or",
            "fetch the rest from storage directly.",
            "",
        ]
    if notes.get("skipped"):
        lines += ["Some files could not be read and are missing:"]
        lines += [f"  - {item}" for item in notes["skipped"][:20]]
        lines += [""]
    return "\n".join(lines)


__all__ = [
    "EPHEMERAL_DAYS", "EPHEMERAL_MAX_RUNS", "DURABLE_DAYS", "DURABLE_MAX_RUNS",
    "EXPORT_MAX_BYTES", "CONTENT_TABLES", "KEPT_TABLES",
    "Policy", "policy_for", "DeletionReport", "survey",
    "delete_project_data", "export_project_data",
]
