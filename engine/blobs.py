"""TestFortge — where an uploaded file goes (E4.5a, then E8.2).

Two modules, one question, split along the line that actually divides them:

* :mod:`engine.storage` knows **how** to write bytes — local disk or an
  S3-compatible bucket, chosen per organisation at call time (E8.2).
* this module knows **what a key means** — which characters are allowed in
  one, which file types may be attached at all, and what a refusal says to
  the person who chose the file.

E4.5a wrote this half first and promised the other: *"When E8.2 lands,
:func:`save` grows a backend and the callers do not change."* That is what
happened — :func:`save` now hands the file to ``storage.backend_for()``,
and ``routes/bugs.py`` and ``routes/chat.py`` are untouched apart from
passing the organisation along.

Why it exists at all rather than another ``upload.save(path)``
--------------------------------------------------------------
Two defects, both measured before this was written, both caused by every
uploader inventing its own path:

**Uploads collide.** ``routes/estimation.py`` saves under
``secure_filename(filename)`` with no project scope, so ``requirements.docx``
from two projects is one file. The key here starts with the project, so that
is not a bug to avoid — it is a state that cannot be reached.

**A file could be saved and then be unservable.** ``routes/chat.py`` wrote
bug attachments to ``UPLOAD_FOLDER/chat_bug_attachments/`` while the bug page
renders them through ``automation_asset``, which serves from
``STORAGE_ROOT``. Measured: the upload succeeded, the row recorded the name,
and the page got a 404 — a broken image where the evidence should be. One
place that owns the location is what stops the two halves disagreeing.

The key
-------
``org/<org_id>/project/<project_id>/<kind>/<entity_id>/<filename>``

The ``org`` segment arrived with E8.2, as ADR 0002 §4.2 specifies. E4.5a
left it out and said why: the org prefix earns its place when several
organisations share one bucket, and the change would be free later because
the disk is ephemeral and there is nothing to migrate. Both halves of that
turned out to be true, so this is the rare prefix change with no migration
step.

It is **always** present, including when nobody is signed into an
organisation, where it reads ``org/_none``. A conditional segment would mean
one project's evidence living under two different prefixes depending on
which flags were on the day each file arrived — and "delete this project's
data" (E8.5) is a prefix deletion, so a project with two prefixes is a
project whose deletion misses half its files. One shape, always. (A real
``org_id`` is a UUID, so it cannot sanitise to ``_none``.)

Three properties the key delivers, which is why it is a key and not a
filename:

* the project scope makes collisions unreachable;
* deleting a project's evidence is deleting a prefix, which is what E8.5
  needs and what makes a blob-index table unnecessary for now;
* the isolation between organisations is visible in the key itself, not only
  in the code that builds it.

Nobody builds a prefix by hand
------------------------------
:func:`prefix_for` exists because ``routes/bugs.py`` used to write
``f"project/{pid}/bug/{db_id}"`` inline to undo a failed attach. That is two
hand-maintained answers to one question — the defect this programme has now
met five times — and it would have silently stopped matching the moment the
``org`` segment appeared: the delete would have found nothing, reported
nothing, and left the orphan it exists to remove. :func:`key_for` is
:func:`prefix_for` plus a filename, so they cannot disagree.

Failure
-------
:func:`save` **raises**. ADR 0002 §4.6 distinguishes by who is waiting: a
screenshot the runner could not upload degrades, because a run that dies on
slow storage is worse than a run missing an image — but a person who has
just chosen a file is standing there, and accepting it, storing nothing and
saying "attached" is the same defect E0.4 found in ``email_verified``: an
assumption recorded as a fact.
"""
from __future__ import annotations

import os
import re
import secrets
from datetime import datetime, timezone

from werkzeug.utils import secure_filename

from engine import storage
from engine.log import get_logger

log = get_logger(__name__)

#: What may be attached as evidence.
#:
#: Deliberately not ``file_parser.ALLOWED_EXTENSIONS``: that set answers
#: "what can this application read and turn into requirements", which is a
#: different question with a different risk. A ``.docx`` is a sensible thing
#: to hand an estimator and a strange thing to attach to a bug as proof, and
#: every extra type here is another thing served back to a browser.
EVIDENCE_EXTENSIONS = frozenset({
    "png", "jpg", "jpeg", "gif", "webp",
    "webm", "mp4", "mov",
    "pdf", "txt", "log",
})

#: Kinds of thing a blob can belong to. Part of the key, so it is closed
#: rather than free text — a typo would otherwise create a sibling tree that
#: nothing lists and nothing deletes.
#:
#: ADR 0002 §4.2 lists these as ``{run, upload, attachment, export}``. The
#: divergence is deliberate and it is one word: ``bug`` rather than
#: ``attachment``. The segment sits directly in front of ``<entity_id>``, and
#: ``bug/42`` says what 42 is where ``attachment/42`` does not. Recorded here
#: rather than quietly, because an ADR and its implementation disagreeing
#: without a note is how the ADR stops being read.
KINDS = frozenset({"bug", "run", "upload", "export"})

#: The org segment for a caller who is not in an organisation — ORG_MODE off,
#: or a machine caller. See the module docstring on why this is a literal
#: rather than an omitted segment.
ORG_NONE = "_none"

#: Mirrors ``routes._shared.SAFE_ASSET_RE``. A key this does not match
#: cannot be served by ``automation_asset``, so a key that fails it is not a
#: key at all — checked here, at the one place keys are minted, rather than
#: discovered as a 400 on the page.
_SERVABLE = re.compile(r"^[A-Za-z0-9_\-./]+$")


class UploadRefused(ValueError):
    """The file cannot be stored. The message is written for the user."""


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def prefix_for(project_id: str, kind: str | None = None,
               entity_id: str | None = None, *,
               org_id: str | None = None) -> str:
    """The key prefix for a project, a kind within it, or one entity.

    The single place a prefix is built, so that "where this project's files
    live" and "where one bug's files live" can never drift apart from
    :func:`key_for`. Every component is sanitised rather than trusted —
    ``entity_id`` reaches this from a URL.

    Passing *entity_id* without *kind* is a programming error, not a user
    error, and raises ``ValueError``: the result would be a prefix that
    matches nothing, which is silent when used to delete.
    """
    if not project_id:
        raise UploadRefused("No active project.")
    if entity_id is not None and kind is None:
        raise ValueError("an entity prefix needs its kind")
    if kind is not None and kind not in KINDS:
        raise UploadRefused(f"Unknown attachment kind: {kind}.")

    parts = ["org", _segment(org_id) if org_id else ORG_NONE,
             "project", _segment(project_id)]
    if kind is not None:
        parts.append(kind)
    if entity_id is not None:
        parts.append(_segment(entity_id))
    return "/".join(parts)


def key_for(project_id: str, kind: str, entity_id: str,
            filename: str, *, org_id: str | None = None) -> str:
    """The storage key for one file. Raises :class:`UploadRefused`.

    Every component is sanitised rather than trusted: ``entity_id`` reaches
    this from a URL, and ``filename`` is chosen by whoever is uploading.
    """
    prefix = prefix_for(project_id, kind, entity_id, org_id=org_id)

    safe_name = secure_filename(filename or "")
    extension = _extension(safe_name)
    if not extension or extension not in EVIDENCE_EXTENSIONS:
        raise UploadRefused(
            f"That file type cannot be attached. Allowed: "
            f"{', '.join(sorted(EVIDENCE_EXTENSIONS))}.")

    # A prefix per upload, so attaching the same screenshot twice keeps both
    # rather than the second silently replacing the first — a person who
    # attaches "before.png" and then a corrected "before.png" means two
    # pieces of evidence, not one.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    unique = f"{stamp}_{secrets.token_hex(3)}_{safe_name}"

    key = f"{prefix}/{unique}"
    if not _SERVABLE.fullmatch(key):
        # Should be unreachable — every component is sanitised above. Kept
        # because "the file saved but the page cannot show it" is exactly
        # the failure this module was written to end.
        raise UploadRefused("That name cannot be stored.")
    return key


def _segment(value: str) -> str:
    """One path component, reduced to what the asset route will serve."""
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "-", str(value or "").strip())
    return cleaned[:80] or "unknown"


def absolute(key: str) -> str:
    """Where *key* lives **on local disk**. Refuses anything escaping the root.

    Local-only on purpose, and not part of the general path: under an S3
    backend there is no such file, and a caller holding an absolute path is
    a caller that stops working when the backend changes. It survives for
    the two jobs that are genuinely local — the traversal guard in
    ``automation_asset``, and tests that need to look at the bytes.
    """
    try:
        return storage.LocalBackend().locate(key).path or ""
    except storage.StorageError as exc:
        raise UploadRefused("That name cannot be stored.") from exc


def _size_of(upload) -> int:
    """How many bytes *upload* carries, leaving it rewound to the start.

    Measured before the write rather than stat-ed after it. Under a bucket
    there is nothing to stat without a second round trip, and "write it,
    look at it, delete it if it was empty" leaves a window where the file
    exists — on a backend where the delete can fail on its own.
    """
    stream = getattr(upload, "stream", upload)
    try:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
        return int(size)
    except Exception:            # pragma: no cover — a stream that cannot seek
        return -1


def save(upload, *, project_id: str, kind: str, entity_id: str,
         org_id: str | None = None) -> str:
    """Store *upload* and return its key. Raises on any refusal.

    The key is what callers persist and what ``automation_asset`` serves;
    nobody outside this module holds an absolute path, which is what let
    E8.2 change the backend underneath without touching a caller.
    """
    filename = getattr(upload, "filename", "") or ""
    if not filename:
        raise UploadRefused("No file was chosen.")

    # Asserted rather than assumed. A zero-byte upload means the browser
    # sent nothing useful, and an attachment that renders as a blank image
    # is worse than a refusal, because it looks like evidence.
    size = _size_of(upload)
    if size == 0:
        raise UploadRefused("That file was empty.")

    key = key_for(project_id, kind, entity_id, filename, org_id=org_id)
    backend = storage.backend_for(org_id)
    try:
        backend.put(key, upload)
    except storage.StorageError as exc:
        # Translated, because the caller for this path is a person who just
        # chose a file and the storage message is written for a log.
        log.warning("could not store %s attachment for %s: %s",
                    kind, entity_id[:16], exc)
        raise UploadRefused("That file could not be stored.") from exc

    log.info("stored %s attachment for %s (%d bytes, %s)", kind,
             entity_id[:16], size, backend.name)
    return key


def exists(key: str, *, org_id: str | None = None) -> bool:
    try:
        return storage.backend_for(org_id).exists(key)
    except storage.StorageError:      # pragma: no cover — defensive
        return False


def locate(key: str, *, org_id: str | None = None) -> storage.Location:
    """Where a reader should fetch *key* — a local path or a signed URL.

    The one function a serving route needs. It is here rather than only in
    :mod:`engine.storage` so that a route asking "where is this attachment"
    asks the module that minted the key.
    """
    return storage.backend_for(org_id).locate(key)


def delete_prefix(prefix: str, *, org_id: str | None = None) -> int:
    """Remove every blob under *prefix*. Returns how many went.

    Here because E8.5 ("delete this project's data") is the reason the key
    is a prefix in the first place, and a scheme whose central claim is
    never exercised is a scheme nobody can trust. Build *prefix* with
    :func:`prefix_for` — never by hand.
    """
    try:
        return storage.backend_for(org_id).delete_prefix(prefix)
    except storage.StorageError as exc:   # pragma: no cover — bucket down
        log.warning("could not clear %s: %s", prefix, exc)
        return 0


__all__ = [
    "EVIDENCE_EXTENSIONS", "KINDS", "ORG_NONE", "UploadRefused",
    "prefix_for", "key_for", "absolute", "save", "exists", "locate",
    "delete_prefix",
]
