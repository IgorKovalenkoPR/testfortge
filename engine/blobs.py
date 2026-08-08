"""TestFortge — where an uploaded file goes (E4.5a).

**This is not the storage abstraction.** That is E8.2, and it is blocked on
[ADR 0002](../docs/plans/adr/0002-artifact-storage.md), which is Proposed
and waiting on the owner. What this module is: the narrow, local-disk half
of it, written so that E8.2 replaces a backend rather than rewriting every
caller.

Scope, stated deliberately because the prompt for E4.5a offered the choice
and this is the branch taken: **local disk only.** On the free Render plan
that disk is ephemeral — a restart takes the file with it — and
``tests/test_bug_attachments.py`` says so in a test rather than leaving it
to be discovered. When E8.2 lands, :func:`save` grows a backend and the
callers do not change.

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
``project/<project_id>/<kind>/<entity_id>/<filename>``

ADR 0002 §4.2 puts an ``org/<org_id>`` segment in front. It is left out here
on purpose: the org prefix earns its place when several organisations share
one bucket, which is E8.2's problem, and adding it now would mean writing
keys against an ADR nobody has approved. The change is free when it comes —
the disk is ephemeral, so there is nothing to migrate. That is the same
argument the ADR makes about existing files.

Two properties the key already delivers, which is why it is a key and not a
filename:

* the project scope makes collisions unreachable;
* deleting a project's evidence is deleting a prefix, which is what E8.5
  needs and what makes a blob-index table unnecessary for now.

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

from engine.automation_paths import STORAGE_ROOT
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
KINDS = frozenset({"bug", "run", "upload", "export"})

#: Mirrors ``routes._shared.SAFE_ASSET_RE``. A key this does not match
#: cannot be served by ``automation_asset``, so a key that fails it is not a
#: key at all — checked here, at the one place keys are minted, rather than
#: discovered as a 400 on the page.
_SERVABLE = re.compile(r"^[A-Za-z0-9_\-./]+$")


class UploadRefused(ValueError):
    """The file cannot be stored. The message is written for the user."""


def _extension(filename: str) -> str:
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def key_for(project_id: str, kind: str, entity_id: str,
            filename: str) -> str:
    """The storage key for one file. Raises :class:`UploadRefused`.

    Every component is sanitised rather than trusted: ``entity_id`` reaches
    this from a URL, and ``filename`` is chosen by whoever is uploading.
    """
    if kind not in KINDS:
        raise UploadRefused(f"Unknown attachment kind: {kind}.")
    if not project_id:
        raise UploadRefused("No active project.")

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

    key = "/".join((
        "project", _segment(project_id), kind, _segment(entity_id), unique))
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
    """Where *key* lives on disk. Refuses anything that escapes the root."""
    root = os.path.realpath(STORAGE_ROOT)
    target = os.path.realpath(os.path.join(root, key))
    if not (target == root or target.startswith(root + os.sep)):
        raise UploadRefused("That name cannot be stored.")
    return target


def save(upload, *, project_id: str, kind: str, entity_id: str) -> str:
    """Store *upload* and return its key. Raises on any refusal.

    The key is what callers persist and what ``automation_asset`` serves;
    nobody outside this module should hold an absolute path, because that is
    the thing that stops being true when E8.2 lands.
    """
    filename = getattr(upload, "filename", "") or ""
    if not filename:
        raise UploadRefused("No file was chosen.")

    key = key_for(project_id, kind, entity_id, filename)
    destination = absolute(key)
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    upload.save(destination)

    # Asserted rather than assumed. A zero-byte result means the browser
    # sent nothing useful, and an attachment that renders as a blank image
    # is worse than a refusal, because it looks like evidence.
    if not os.path.getsize(destination):
        try:
            os.remove(destination)
        except OSError:      # pragma: no cover — best effort
            pass
        raise UploadRefused("That file was empty.")

    log.info("stored %s attachment for %s (%d bytes)", kind, entity_id[:16],
             os.path.getsize(destination))
    return key


def exists(key: str) -> bool:
    try:
        return os.path.isfile(absolute(key))
    except UploadRefused:
        return False


def delete_prefix(prefix: str) -> int:
    """Remove every blob under *prefix*. Returns how many went.

    Here because E8.5 ("delete this project's data") is the reason the key
    is a prefix in the first place, and a scheme whose central claim is
    never exercised is a scheme nobody can trust. Not yet wired to a route —
    that is E8.5's work.
    """
    root = absolute(prefix)
    if not os.path.isdir(root):
        return 0
    removed = 0
    for current, _dirs, files in os.walk(root, topdown=False):
        for name in files:
            try:
                os.remove(os.path.join(current, name))
                removed += 1
            except OSError as exc:      # pragma: no cover — best effort
                log.warning("could not delete %s: %s", name, exc)
        try:
            os.rmdir(current)
        except OSError:
            pass
    return removed


__all__ = [
    "EVIDENCE_EXTENSIONS", "KINDS", "UploadRefused",
    "key_for", "absolute", "save", "exists", "delete_prefix",
]
