"""TestFortge — the ``[Folder -> Subfolder]`` prefix that files an item.

A test-case summary or checklist objective that opens with a bracketed path
is filed into that folder (owner rule, 2026-10-20)::

    [Header] Verify that QArea logo is displayed in Header
        → folder "Header"
    [Services -> Web Development] Verify that the contact form is open …
        → folder "Web Development" inside "Services"

The prefix is the single source of truth for automatic filing: generation
writes it, import reads it, an inline edit that changes it re-files the item,
and the "Re-file from prefixes" action applies it to what is already stored.

**The prefix stays in the stored summary.** It is the human-readable
convention the owner asked generators to write, it survives an export →
edit → re-import round trip, and keeping it makes "did the prefix change?"
a comparison of the old text with the new — no extra column. Everything that
*composes* text from a summary (bug titles, the bug narrative, the
"Verify that" house-style checks, Gherkin scenario names) reads it through
:func:`strip_prefix` instead.

Pure: no database. ``engine.db.file_by_prefix`` does the filing.

Edge cases, decided once here and pinned by ``tests/test_folder_prefix.py``:

* Leading whitespace before ``[`` is allowed; the prefix must otherwise be the
  first thing in the text. A bracket later in the sentence ("Verify that the
  [Submit] button …", which the checklist rules write) is not a prefix.
* Separators: ``->``, ``→``, ``=>``, ``—>``/``–>`` and a bare ``>``. ``/`` is
  NOT a separator — page names contain it ("Terms / Privacy").
* Whitespace inside a segment collapses to one space; segments are trimmed.
* Empty segments are dropped (``[A -> -> B]`` is ``A / B``). A prefix with
  no non-empty segment (``[]``, ``[ -> ]``) is not a prefix.
* Not a prefix: a Markdown link (``[text](url)``), a task-list box (``[ ]``,
  ``[x]``), nested brackets, or an unclosed ``[``.
* Deeper than ``FOLDER_MAX_DEPTH`` (6): the extra levels are dropped, the
  same cap the folder tree enforces everywhere else.
* Matching existing folders ignores case — that is
  ``db.ensure_folder_path``'s sibling-by-name rule, so ``[header]`` lands in
  the existing "Header" rather than creating a second one.
"""
from __future__ import annotations

import re

#: Same caps as ``engine.db`` (FOLDER_MAX_DEPTH / FOLDER_NAME_MAX); repeated
#: rather than imported so this module stays free of the ORM.
MAX_DEPTH = 6
NAME_MAX = 200

SEPARATOR = " -> "

_PREFIX_RE = re.compile(r"^\s*\[(?P<inner>[^\[\]\n]*)\](?P<rest>.*)\Z",
                        re.DOTALL)
_SPLIT_RE = re.compile(r"\s*(?:[-–—=]+>|→|⟶|➔|>)\s*")
_TASK_BOX_RE = re.compile(r"^\s*[xX✓✔]?\s*$")


def _segments(inner: str) -> tuple[str, ...]:
    parts = (re.sub(r"\s+", " ", p).strip()[:NAME_MAX]
             for p in _SPLIT_RE.split(inner))
    return tuple(p for p in parts if p)[:MAX_DEPTH]


def split(text) -> tuple[tuple[str, ...], str]:
    """``(path, body)`` — the folder path and the text after the prefix.

    ``path`` is ``()`` when the text has no prefix, and then ``body`` is the
    text unchanged. The body has its leading whitespace removed.
    """
    raw = "" if text is None else str(text)
    m = _PREFIX_RE.match(raw)
    if not m:
        return (), raw
    inner, rest = m.group("inner"), m.group("rest")
    if rest.startswith("(") or _TASK_BOX_RE.match(inner):
        return (), raw
    path = _segments(inner)
    if not path:
        return (), raw
    return path, rest.lstrip()


def parse(text) -> tuple[str, ...]:
    """The folder path a leading ``[A -> B]`` names, or ``()``."""
    return split(text)[0]


def strip_prefix(text) -> str:
    """The text without its folder prefix — for anything composing prose."""
    return split(text)[1]


def path_key(path) -> tuple[str, ...]:
    """A path as folder matching sees it: case and spacing ignored."""
    return tuple(re.sub(r"\s+", " ", str(p)).strip().casefold()
                 for p in (path or ()) if str(p or "").strip())


def prefix_changed(old_text, new_text) -> bool:
    """True when ``new_text`` names a folder that ``old_text`` did not.

    The manual-move rule: an edit re-files an item only if the prefix itself
    changed. Re-typing the same prefix in another case, or removing it,
    leaves wherever a person put the item alone.
    """
    new = path_key(parse(new_text))
    return bool(new) and new != path_key(parse(old_text))


def format_prefix(path) -> str:
    """``("Services", "Web Development")`` → ``[Services -> Web Development]``."""
    names = [re.sub(r"\s+", " ", str(p)).strip() for p in (path or ())]
    # A name holding a bracket or a separator would not parse back as itself.
    names = [re.sub(r"[\[\]]", "", _SPLIT_RE.sub(" ", n)).strip()
             for n in names]
    names = [n for n in names if n][:MAX_DEPTH]
    return f"[{SEPARATOR.join(names)}]" if names else ""


def with_prefix(text, path) -> str:
    """``text`` opened with the prefix for ``path``, replacing any old one."""
    body = strip_prefix(text).strip()
    prefix = format_prefix(path)
    if not prefix:
        return body
    return f"{prefix} {body}" if body else prefix


def map_body(text, fn) -> str:
    """Apply ``fn`` to the text after the prefix and put the prefix back.

    For the house-style fixers, which rewrite a summary's opening words and
    would otherwise rewrite the prefix as if it were the first words.
    """
    path, body = split(text)
    if not path:
        return fn(text)
    return with_prefix(fn(body), path)


__all__ = ["split", "parse", "strip_prefix", "path_key", "prefix_changed",
           "format_prefix", "with_prefix", "map_body", "SEPARATOR"]
