"""TestForTge — project bundles: making one, checking one, restoring one (E8.4).

    відновлення перевірене на реальному бандлі, не «має працювати»
    (restore verified on a real bundle, not "should work")

The criterion is about *evidence*, not about features, and it is aimed at
the thing backups are famous for: existing, being green, and not restoring.
So the shape of this module is decided by what makes that verifiable —

* a bundle is **bytes**, not a promise. It goes to the configured storage
  backend (ADR 0002), which on a self-host is the team's own MinIO and on
  the hosted deployment is R2;
* every member carries a **SHA-256** in the manifest, and :func:`verify`
  recomputes them. A restore refuses a bundle that does not match, because
  restoring from a corrupt archive is worse than not restoring;
* :func:`restore` builds a **new project** and never overwrites an existing
  one, so a restore performed in a panic cannot destroy the thing it was
  meant to save;
* ``tests/test_backup_restore.py`` creates a project, backs it up, deletes
  it, restores it, and compares the result field by field — including
  fetching a restored screenshot through the route that serves it.

Where the bundle lives, and what a backup does not protect you from
-------------------------------------------------------------------
``org/<org_id>/backup/<project_id>/<stamp>.zip`` — under the organisation,
outside the project's own prefix, because a bundle is operational data
belonging to the deployment rather than an artefact belonging to the
project. It does not count towards the project's storage usage and it is
not in the project's own export.

**A backup does not survive deleting the project.** That is a real
limitation and it is chosen, not overlooked. The alternative was worse: a
"delete this project's data" that leaves a complete, restorable copy of
everything it claimed to remove is precisely the defect E8.5 was written to
end, and it is the difference between a deletion and a gesture. So
:func:`delete_for_project` exists here, ``engine.retention`` calls it, and
the test that keeps the two modules honest asserts a deleted project leaves
no bundle behind.

What that leaves backups for: a bad import, a bulk edit that went wrong,
corruption, a restore onto a different instance. **Not** "somebody deleted
the wrong project" — for that, the answer is the Export button (E8.5), whose
zip is held by the person who downloaded it and is outside anything this
application can delete. The delete confirmation says so.

The first draft of this module claimed the opposite in this very docstring
and then deleted bundles anyway. The round-trip test found it, by failing to
read a bundle it had just created.

What a restore does not bring back
----------------------------------
Test cases, checklist items, estimation, bug reports and every file. **Not**
run history: execution runs, automation runs, locators, site profiles,
recorder drafts and dashboard snapshots are in the bundle and are not
re-inserted, because their rows reference each other by integer id and
re-pointing that graph is machinery in service of history nobody restores.

:class:`RestoreReport` **names them** rather than leaving the gap to be
discovered. A restore that quietly returns less than it was given is the
same class of defect as a deletion that quietly keeps something.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone

import sqlalchemy as sa

from engine import retention, storage
from engine.log import get_logger

log = get_logger(__name__)

#: Bumped when the archive layout changes in a way a reader must know about.
#: :func:`restore` refuses a major version it does not understand rather than
#: guessing — a restore that half-reads an unfamiliar bundle produces a
#: project that looks complete and is not.
BUNDLE_VERSION = 1

#: Where the manifest lives inside the archive.
MANIFEST_NAME = "manifest.json"

#: How many bundles to keep per project. Older ones are pruned when a new
#: one is written, so an unattended weekly job cannot fill the bucket.
DEFAULT_KEEP = 7

#: Tables restored into the new project, in order. Order matters: test cases
#: come before bug reports so a bug's ``related_case_id`` can be re-pointed
#: at the case it was actually about.
RESTORED_TABLES: tuple[tuple[str, str], ...] = (
    ("Estimation", "estimation"),
    ("TestCase", "test cases"),
    ("ChecklistItem", "checklist items"),
    ("BugReport", "bug reports"),
)

#: In the bundle, deliberately not re-inserted. See the module docstring.
NOT_RESTORED = ("execution runs", "automation runs", "locators",
                "site profiles", "recorder drafts", "dashboard snapshots",
                "Tedgie submissions", "browser sessions")


def keep_count() -> int:
    raw = (os.environ.get("BACKUP_KEEP") or "").strip()
    try:
        value = int(raw) if raw else DEFAULT_KEEP
    except ValueError:
        log.warning("BACKUP_KEEP=%r is not a number — using %d.", raw,
                    DEFAULT_KEEP)
        return DEFAULT_KEEP
    # Zero would mean "prune the bundle you just wrote", which is a
    # plausible typo for "keep everything" and does the opposite.
    return value if value >= 1 else DEFAULT_KEEP


def prefix_for(org_id: str | None, project_id: str | None = None) -> str:
    """Where an organisation's bundles live. The one place it is built."""
    base = f"{storage.org_prefix(org_id)}/backup"
    if not project_id:
        return base
    return f"{base}/{re.sub(r'[^A-Za-z0-9_-]', '-', str(project_id))[:80]}"


@dataclass
class BundleInfo:
    key: str
    created_at: str = ""
    project_id: str = ""
    project_name: str = ""
    bytes: int = 0

    @property
    def stamp(self) -> str:
        return self.key.rsplit("/", 1)[-1].removesuffix(".zip")


@dataclass
class RestoreReport:
    ok: bool = True
    problem: str = ""
    project_id: str = ""
    project_name: str = ""
    rows: dict[str, int] = field(default_factory=dict)
    files: int = 0
    not_restored: tuple[str, ...] = NOT_RESTORED

    def summary(self) -> str:
        if not self.ok:
            return self.problem
        parts = [f"{n} {label}" for label, n in sorted(self.rows.items()) if n]
        return (f"Restored '{self.project_name}' as a new project: "
                f"{', '.join(parts) or 'no rows'}, and {self.files} "
                f"file{'' if self.files == 1 else 's'}. Run history was in "
                f"the bundle and is not restored.")


# ── Making one ───────────────────────────────────────────────────────

def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def build(project_id: str, *, org_id: str | None = None,
          now: datetime | None = None) -> tuple[bytes, dict]:
    """Return ``(archive_bytes, manifest)`` for one project.

    The body is ``engine.retention.export_project_data`` — the same archive
    a person downloads from the Export button — plus a manifest. One format
    for both on purpose: a backup nobody can open by hand is a backup nobody
    checks, and two formats would be two things to keep working.
    """
    from engine import db as _db

    raw, notes = retention.export_project_data(project_id, org_id=org_id)
    meta = _db.get_project(project_id) or {}
    stamped = (now or datetime.now(timezone.utc)).isoformat()

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        members = {name: _digest(archive.read(name))
                   for name in sorted(archive.namelist())}

    manifest = {
        "bundle_version": BUNDLE_VERSION,
        "created_at": stamped,
        "project_id": project_id,
        "project_name": meta.get("name") or "",
        "org_id": org_id or "",
        # Carried forward, not recomputed: if the export hit its size cap
        # the bundle is incomplete, and a backup that does not say so is
        # the exact failure this epic's criterion is aimed at.
        "truncated": bool(notes.get("truncated")),
        "skipped": list(notes.get("skipped") or []),
        "members": members,
        "not_restored": list(NOT_RESTORED),
    }

    buffer = io.BytesIO(raw)
    with zipfile.ZipFile(buffer, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(MANIFEST_NAME,
                         json.dumps(manifest, indent=2, ensure_ascii=False))
    return buffer.getvalue(), manifest


def create(project_id: str, *, org_id: str | None = None,
           user_id: str | None = None,
           now: datetime | None = None) -> BundleInfo:
    """Write a bundle to the configured storage. Raises on failure.

    Raises rather than returning a report: every caller here is either a
    person who pressed a button or a scheduled job, and both need "this did
    not happen" to be loud. A backup job that reports success over a failed
    write is the canonical version of this whole problem.
    """
    from engine import db as _db

    raw, manifest = build(project_id, org_id=org_id, now=now)
    stamp = manifest["created_at"].replace(":", "").replace("-", "")[:15]
    key = f"{prefix_for(org_id, project_id)}/{stamp}.zip"

    backend = storage.backend_for(org_id)
    backend.put(key, io.BytesIO(raw))

    _db.append_audit(entity="project", action="backup", user_id=user_id,
                     org_id=org_id, project_id=project_id, entity_id=key,
                     diff={"bytes": len(raw),
                           "truncated": manifest["truncated"]})
    log.info("backed up project %s to %s (%d bytes)", project_id[:8], key,
             len(raw))

    prune(project_id, org_id=org_id)
    return BundleInfo(key=key, created_at=manifest["created_at"],
                      project_id=project_id,
                      project_name=manifest["project_name"], bytes=len(raw))


def list_bundles(project_id: str, *,
                 org_id: str | None = None) -> list[BundleInfo]:
    """Newest first. Never raises — this renders a page."""
    prefix = prefix_for(org_id, project_id) + "/"
    try:
        backend = storage.backend_for(org_id)
        keys = [k for k in retention._keys_under(backend, prefix)
                if k.endswith(".zip")]
    except Exception as exc:            # pragma: no cover — bucket down
        log.warning("could not list bundles for %s: %s", project_id[:8], exc)
        return []
    return sorted((BundleInfo(key=k, project_id=project_id) for k in keys),
                  key=lambda b: b.key, reverse=True)


def prune(project_id: str, *, org_id: str | None = None) -> int:
    """Drop all but the newest :func:`keep_count` bundles. Returns how many."""
    bundles = list_bundles(project_id, org_id=org_id)
    doomed = bundles[keep_count():]
    if not doomed:
        return 0
    backend = storage.backend_for(org_id)
    removed = 0
    for bundle in doomed:
        try:
            removed += backend.delete_prefix(bundle.key)
        except storage.StorageError as exc:   # pragma: no cover — best effort
            log.warning("could not prune %s: %s", bundle.key, exc)
    log.info("pruned %d old bundle(s) for %s", removed, project_id[:8])
    return removed


def delete_for_project(project_id: str, *, org_id: str | None = None) -> int:
    """Remove every bundle of a project. Called by ``engine.retention``.

    Here rather than there because this module owns where bundles live, and
    a second module computing that path is the two-lists defect. It matters
    more than usual: a "delete this project's data" that misses the bundles
    leaves a complete copy of everything it claimed to remove.
    """
    return storage.backend_for(org_id).delete_prefix(
        prefix_for(org_id, project_id) + "/")


# ── Checking one ─────────────────────────────────────────────────────

def verify(raw: bytes) -> tuple[bool, str]:
    """``(ok, problem)`` for a bundle's bytes.

    Checks the archive opens, carries a manifest this version understands,
    and that every member's SHA-256 still matches. A truncated download and
    a half-written object both land here, and both look like a valid zip
    until something reads the bytes.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            try:
                manifest = json.loads(archive.read(MANIFEST_NAME))
            except KeyError:
                return False, ("This archive has no manifest, so it was not "
                               "produced as a backup and cannot be checked.")
            version = manifest.get("bundle_version")
            if not isinstance(version, int) or version > BUNDLE_VERSION:
                return False, (
                    f"This bundle is version {version}; this instance "
                    f"understands up to {BUNDLE_VERSION}. Upgrade before "
                    f"restoring it — reading it anyway would produce a "
                    f"project that looks complete and is not.")
            recorded = manifest.get("members") or {}
            if not recorded:
                return False, "The manifest lists no files."
            present = set(archive.namelist()) - {MANIFEST_NAME}
            missing = sorted(set(recorded) - present)
            if missing:
                return False, (f"{len(missing)} file(s) named in the manifest "
                               f"are not in the archive, starting with "
                               f"{missing[0]}.")
            for name, expected in recorded.items():
                if _digest(archive.read(name)) != expected:
                    return False, (f"'{name}' does not match its checksum — "
                                   f"the bundle is corrupt or was modified.")
    except zipfile.BadZipFile:
        return False, "That file is not a readable zip archive."
    return True, ""


def read(key: str, *, org_id: str | None = None) -> bytes:
    return storage.backend_for(org_id).get_bytes(key)


# ── Restoring one ────────────────────────────────────────────────────

def _members(archive: zipfile.ZipFile, label: str) -> list[dict]:
    name = f"data/{label.replace(' ', '_')}.json"
    try:
        return json.loads(archive.read(name))
    except KeyError:
        return []


def restore(raw: bytes, *, org_id: str | None = None,
            user_id: str | None = None,
            name_suffix: str = "restored") -> RestoreReport:
    """Rebuild a bundle into a **new** project. Never overwrites.

    A restore is performed by somebody who has just lost something, and an
    in-place restore that goes wrong in that moment destroys the only other
    copy. So this always creates a project and hands back its id; merging
    into an existing one is a decision for whoever compares them afterwards.
    """
    from engine import db as _db

    ok, problem = verify(raw)
    if not ok:
        return RestoreReport(ok=False, problem=problem)

    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        manifest = json.loads(archive.read(MANIFEST_NAME))
        project = json.loads(archive.read("project.json"))
        old_pid = str(project.get("id") or manifest.get("project_id") or "")
        base_name = (project.get("name")
                     or manifest.get("project_name") or "Restored project")
        stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
        new_name = f"{base_name} ({name_suffix} {stamp})"[:160]

        new_pid = _db.upsert_project(
            name=new_name, base_url=project.get("base_url") or None,
            description=project.get("description") or None,
            org_id=org_id or None)

        report = RestoreReport(ok=True, project_id=new_pid,
                               project_name=new_name)

        # Files first, so the key rewrite below has somewhere to point.
        old_prefix = f"{storage.org_prefix(manifest.get('org_id') or None)}" \
                     f"/project/{old_pid}"
        new_prefix = f"{storage.org_prefix(org_id)}/project/{new_pid}"
        backend = storage.backend_for(org_id)
        remapped: dict[str, str] = {}
        for name in archive.namelist():
            if not name.startswith("files/"):
                continue
            tail = name[len("files/"):]
            new_key = f"{new_prefix}/{tail}"
            backend.put(new_key, io.BytesIO(archive.read(name)))
            remapped[f"{old_prefix}/{tail}"] = new_key
            report.files += 1

        case_ids: dict[int, int] = {}
        with _db.session_scope() as sess:
            for model_name, label in RESTORED_TABLES:
                model = getattr(_db, model_name, None)
                if model is None:        # pragma: no cover — schema drift
                    continue
                columns = {c.name: c.type for c in model.__table__.columns}
                written = 0
                for row in _members(archive, label):
                    values = {k: _coerce(v, columns[k])
                              for k, v in row.items()
                              if k in columns and k != "id"}
                    values["project_id"] = new_pid
                    # Run history is not restored, so a bug pointing at a
                    # run would point at somebody else's row.
                    values.pop("run_id", None)
                    if model_name == "BugReport":
                        values["related_case_id"] = case_ids.get(
                            row.get("related_case_id"))
                        values["extra"] = _rewrite_keys(row.get("extra"),
                                                        remapped)
                    obj = model(**values)
                    sess.add(obj)
                    if model_name == "TestCase":
                        sess.flush()
                        case_ids[row.get("id")] = obj.id
                    written += 1
                report.rows[label] = written

    _db.append_audit(entity="project", action="restore", user_id=user_id,
                     org_id=org_id, project_id=new_pid, entity_id=old_pid,
                     diff={"from_project": old_pid, "rows": report.rows,
                           "files": report.files})
    log.info("restored %s into %s (%d rows, %d files)", old_pid[:8],
             new_pid[:8], sum(report.rows.values()), report.files)
    return report


def _coerce(value, column_type):
    """Turn an exported value back into something the column accepts.

    JSON has no datetime, so ``export_project_data`` writes them as ISO
    strings and they come back as strings — which SQLAlchemy refuses with
    "DateTime type only accepts Python datetime". Found by the round trip,
    not by reading the exporter: the export was valid, the restore was
    valid, and only running one into the other showed they disagreed about
    a type.
    """
    if value is None or not isinstance(value, str):
        return value
    if not isinstance(column_type, (sa.DateTime, sa.Date)):
        return value
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.date() if isinstance(column_type, sa.Date)         and not isinstance(column_type, sa.DateTime) else parsed


def _rewrite_keys(extra, remapped: dict[str, str]):
    """Re-point a bug's attachment keys at the restored project's files.

    Without this a restore looks complete and every screenshot 404s: the
    keys carry the *old* project id (ADR 0002 §4.2), the files were written
    under the new one, and nothing connects them. The kind of gap that is
    invisible in a row count and obvious the moment somebody opens the bug,
    which is why the test fetches one through the serving route rather than
    asserting the list is non-empty.
    """
    if not isinstance(extra, dict):
        return extra
    attachments = extra.get("attachments")
    if not isinstance(attachments, list):
        return extra
    out = dict(extra)
    out["attachments"] = [remapped.get(key, key) for key in attachments]
    return out


__all__ = [
    "BUNDLE_VERSION", "MANIFEST_NAME", "DEFAULT_KEEP", "NOT_RESTORED",
    "RESTORED_TABLES", "BundleInfo", "RestoreReport",
    "keep_count", "prefix_for", "build", "create", "list_bundles", "prune",
    "delete_for_project", "verify", "read", "restore",
]
