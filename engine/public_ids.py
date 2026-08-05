"""TestFortge — public item ids that are actually unique (E4.4a).

``TC-004``, ``CNT_001``, ``HDR_012``: the ids a person sees, cites in a bug
and clicks in an export. Everything in :mod:`engine.editable` addresses a row
by one, so two rows sharing an id makes "edit CNT_001" undefined — the
substrate refuses it rather than picking one (``AmbiguousEntity``).

They were not unique. Measured on ``POST /checklist`` for
https://example.com: an 82-item pack containing ``CNT_001`` twice, once from
the site-aware builder's "Page Content" section and once from a rule-driven
"Content & Layout" section. Each builder counts from 1 over its own output
and the route concatenates the lists, so any prefix they happen to share
collides. Nothing was wrong inside either builder.

Which is why the fix is not in the builders
-------------------------------------------
There are eight write paths into a checklist pack (five ``_store_checklist``
call sites, two direct ``db.save_checklist`` calls, one in ``routes/projects``)
and more builders than that. Assigning ids per builder means every new
builder has to remember, and a shared prefix between two of them is a bug
nobody sees until an editor 409s.

So uniqueness is enforced once, at the last moment before the pack is
stored — :func:`ensure_unique`, called from ``db.save_test_cases`` and
``db.save_checklist``. Both are wipe-and-replace, so uniqueness within the
list being written is uniqueness in the table.

Stability is the constraint that shapes the rest
------------------------------------------------
These ids leave the system: exports, bug reports that cite "failed at
CNT_014", execution results, a client's review comments. So an id that is
already unique is **never** touched, and a collision renumbers the *later*
occurrence — the first row keeps the id somebody may already have written
down.
"""
from __future__ import annotations

import re

from engine.log import get_logger

log = get_logger(__name__)

#: ``CNT_001`` / ``TC-004`` / ``SC1_012`` → ("CNT_", 1) / ("TC-", 4) / …
#: The separator stays in the prefix so a renumbered id looks like its
#: neighbours rather than switching from ``_`` to ``-``.
_NUMBERED = re.compile(r"^(?P<prefix>.*?)(?P<number>\d+)$")


def split_id(value: str) -> tuple[str, int | None]:
    """``"CNT_007"`` → ``("CNT_", 7)``; ``"header"`` → ``("header", None)``."""
    text = (value or "").strip()
    if not text:
        return "", None
    match = _NUMBERED.match(text)
    if not match:
        return text, None
    return match.group("prefix"), int(match.group("number"))


def format_id(prefix: str, number: int, width: int = 3) -> str:
    return f"{prefix}{number:0{width}d}"


def ensure_unique(items, *, fallback_prefix: str = "ITEM_",
                  id_key: str = "id", taken=None) -> list[tuple[str, str]]:
    """Give every item a unique public id, **in place**. Returns the renames.

    Mutating the caller's dicts is deliberate and is the reason this works
    from inside ``save_*``: the same list is mirrored into the Flask session
    by ``routes/generation``, and a database that renumbered an id while the
    session kept the old one would show the user an id no editor could find.
    One list, one set of ids.

    ``taken`` seeds the used set — for an append that is being written
    alongside rows this pack does not contain.

    Order matters and is defined: a first pass reserves every id that is
    already unique, so a later duplicate cannot steal a number an untouched
    item is using.
    """
    if not items:
        return []

    def read(item):
        if isinstance(item, dict):
            return item.get(id_key) or ""
        return getattr(item, id_key, "") or ""

    def write(item, value):
        if isinstance(item, dict):
            item[id_key] = value
        else:
            setattr(item, id_key, value)

    used: set[str] = set(taken or ())
    #: prefix → highest number seen, so minting is O(1) per collision rather
    #: than a scan of ``used`` per attempt.
    highest: dict[str, int] = {}

    # Pass 1: reserve what is already unique and unclaimed.
    keep: list[bool] = []
    for item in items:
        value = str(read(item)).strip()
        if value and value not in used:
            used.add(value)
            prefix, number = split_id(value)
            if number is not None:
                highest[prefix] = max(highest.get(prefix, 0), number)
            keep.append(True)
        else:
            keep.append(False)

    # Pass 2: mint for the rest.
    renames: list[tuple[str, str]] = []
    for item, unchanged in zip(items, keep):
        if unchanged:
            continue
        old = str(read(item)).strip()
        prefix, number = split_id(old)
        if not prefix:
            prefix = fallback_prefix
        elif number is None:
            # An id with no numeric tail that is nevertheless duplicated:
            # "Header" twice. Keep the text and start numbering it.
            prefix = f"{prefix}_"
        width = 3
        if number is not None and len(old) - len(prefix) > 3:
            # Preserve a wider numbering scheme (``CNT_0001``) rather than
            # silently narrowing it.
            width = len(old) - len(prefix)
        candidate_number = max(highest.get(prefix, 0), 0) + 1
        new = format_id(prefix, candidate_number, width)
        while new in used:
            candidate_number += 1
            new = format_id(prefix, candidate_number, width)
        highest[prefix] = candidate_number
        used.add(new)
        write(item, new)
        renames.append((old, new))

    if renames:
        # Logged, not silent: a renamed id is a visible change to something a
        # person may have cited, and the duplicate that caused it is a bug in
        # whichever builder produced it.
        sample = ", ".join(f"{old or '(blank)'}→{new}" for old, new in
                           renames[:5])
        log.info("public ids: renumbered %d duplicate/blank id(s): %s%s",
                 len(renames), sample, " …" if len(renames) > 5 else "")
    return renames


__all__ = ["ensure_unique", "format_id", "split_id"]
