"""TestFortge — checklist order and numbering (E4.4).

Requirement 6 asks for a checklist whose items can be edited, reordered and
grouped into sections. Two facts about the stored shape make that more than a
field edit:

* **Order is implicit.** ``load_checklist`` returns rows ``ORDER BY id`` —
  insertion order. There is no sort column, so "move this item up" cannot be
  expressed as an update to one row.
* **``item_num`` describes a position** — the ``1.1`` / ``2.7.1`` the
  reference sheet prints — but it is not the position itself.

The rule this module obeys, and where it comes from
--------------------------------------------------
``engine/qa_knowledge/style/checklist_style.yaml`` — measured from the team's
own reference checklist — says:

    Numbers are stable identifiers. Inserting a check appends it at the end of
    its parent rather than renumbering the siblings.

and lists "renumbering siblings to insert a check" as an anti-pattern,
because *the numbers are cited in bug reports and status updates*. The
database model repeats it: numbers "are persisted rather than recomputed".

So the first version of this module was wrong: it renumbered the pack after
every structural change. What it does instead:

===================  =========================================================
Insert an item       appended to its section with the next free number.
                     Siblings keep theirs.
Delete an item       its number is simply vacated. A gap is the honest
                     result — closing it would move numbers somebody cited.
Move an item         renumbers **that one section**, because the user asked
                     for exactly that: numbers describe position, and after a
                     deliberate reorder "1.3" sitting above "1.2" is a sheet
                     contradicting itself.
Rename a section     nothing renumbers. A rename onto an existing section's
                     name is refused rather than merged — merging changes
                     section indices, which would restate every number in
                     every later section.
Move to a section    appended there with the next free number; the old number
                     is vacated.
===================  =========================================================

Section indices are read from the numbers already on the items rather than
recomputed from position. That is what keeps them stable when a section
empties out: recomputing would shift every later section's prefix.

Why a pack rewrite for a move
-----------------------------
Only a move needs one, and it needs one because order *is* row order: writing
the pack back in the new sequence is what makes ``ORDER BY id`` agree with it.
Adding a ``sort_order`` column is the more principled answer to "order should
be data", but it means a migration plus every writer populating it and every
reader using it, while exports and the manual runner rely on ``ORDER BY id``
today. ``save_checklist`` already preserves each row's provenance across the
delete-and-reinsert and already bumps the pack version, so a concurrent write
is refused rather than merged. A checklist is tens to low hundreds of rows and
a move is a deliberate, occasional act; if reordering ever becomes hot,
``sort_order`` is the follow-up.
"""
from __future__ import annotations

import re

from engine.log import get_logger

log = get_logger(__name__)

MAX_SECTION_NAME = 200

#: ``"2.7.1"`` → section 2, item 7, sub-item 1.
_NUMBER = re.compile(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?$")


class OrderError(ValueError):
    """A reorder that cannot be applied as asked."""


def _section_of(item) -> str:
    return (item.get("section") or "").strip()


def _parts(item) -> tuple[int, int, int]:
    """The item's number as integers; zeroes where it has none."""
    match = _NUMBER.match(str(item.get("item_num") or "").strip())
    if not match:
        return (0, 0, 0)
    return tuple(int(part) if part else 0 for part in match.groups())  # type: ignore[return-value]


def _depth_of(item) -> int:
    try:
        return int(item.get("depth") or 2)
    except (TypeError, ValueError):
        return 2


def sections(items) -> list[str]:
    """Section names in the order they first appear — the pack's own order."""
    out: list[str] = []
    for item in items:
        name = _section_of(item)
        if name not in out:
            out.append(name)
    return out


def section_indices(items) -> dict[str, int]:
    """``{"Header": 1, "Footer": 3}`` — read from the numbers already there.

    Taken from the items rather than recomputed from position, so a section
    that empties out does not shift every later section's prefix. A section
    whose items carry no number yet gets the next index above the highest in
    use.
    """
    out: dict[str, int] = {}
    for item in items:
        name = _section_of(item)
        index = _parts(item)[0]
        if index and name not in out:
            out[name] = index
    highest = max(out.values(), default=0)
    for name in sections(items):
        if name not in out:
            highest += 1
            out[name] = highest
    return out


def next_number(items, section: str, depth: int = 2) -> str:
    """The number an item appended to ``section`` should carry.

    The append rule from the style file: one past the highest sibling, never
    a renumbering of the siblings themselves.
    """
    section = (section or "").strip()
    index = section_indices(items).get(section)
    if index is None:
        index = max(section_indices(items).values(), default=0) + 1
    members = [item for item in items if _section_of(item) == section]
    if depth >= 3:
        # A sub-item hangs off the last level-2 item in the section.
        parents = [_parts(item)[1] for item in members if _depth_of(item) < 3]
        parent = max(parents, default=0)
        if parent:
            children = [_parts(item)[2] for item in members
                        if _depth_of(item) >= 3 and _parts(item)[1] == parent]
            return f"{index}.{parent}.{max(children, default=0) + 1}"
        # Nothing to hang off: promoted to a level-2 item, as
        # ``checklist_rules.assign_numbers`` does for a stray sub-check.
    highest = max((_parts(item)[1] for item in members), default=0)
    return f"{index}.{highest + 1}"


def renumber_section(items, section: str) -> list[tuple[str, str]]:
    """Renumber one section's items from their current order. Returns changes.

    Called after a deliberate move, and only for the section that moved —
    every other section's numbers stay exactly as cited.
    """
    section = (section or "").strip()
    index = section_indices(items).get(section)
    if index is None:
        return []
    changes: list[tuple[str, str]] = []
    level2 = 0
    level3 = 0
    for item in items:
        if _section_of(item) != section:
            continue
        if _depth_of(item) >= 3 and level2 > 0:
            level3 += 1
            number = f"{index}.{level2}.{level3}"
        else:
            level2 += 1
            level3 = 0
            item["depth"] = 2
            number = f"{index}.{level2}"
        before = str(item.get("item_num") or "")
        if before != number:
            changes.append((before, number))
            item["item_num"] = number
    return changes


def index_of(items, item_id: str) -> int:
    """Position of an item by public id. Raises if it is not in the pack."""
    for position, item in enumerate(items):
        if str(item.get("id") or "") == str(item_id):
            return position
    raise OrderError(f"{item_id!r} is not in this checklist.")


def move(items, item_id: str, delta: int) -> list[dict]:
    """Move one item within its own section. Returns the reordered pack.

    Bounded by the section rather than by the pack: dragging an item past a
    section heading would silently change which section it belongs to, and
    that is a different, explicit action. Hitting the first or last position
    is a no-op rather than an error — the control is on every row, and
    scolding somebody for pressing it is worse than doing nothing.
    """
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        raise OrderError("Move distance must be a whole number.") from None

    position = index_of(items, item_id)
    section = _section_of(items[position])
    siblings = [i for i, item in enumerate(items)
                if _section_of(item) == section]
    among = siblings.index(position)
    target = among + delta
    if target < 0 or target >= len(siblings):
        return list(items)

    out = list(items)
    moved = out.pop(position)
    remaining = [i for i, item in enumerate(out)
                 if _section_of(item) == section]
    if target >= len(remaining):
        insert_at = remaining[-1] + 1 if remaining else position
    else:
        insert_at = remaining[target]
    out.insert(insert_at, moved)
    # The deliberate reorder — and only this section.
    renumber_section(out, section)
    return out


def regroup_item(items, item_id: str) -> list[dict]:
    """Put one item where its own ``section`` value says it belongs.

    Position *and* number, for the item's current section — used after a row
    is created (``editable.create`` appends it to the end of the pack, which
    is the end of whatever section happens to be last) and after a ``section``
    change through the generic PATCH.

    Both cases produced the same visible defect: the page renders a new
    section block whenever the section name changes between adjacent rows, so
    an item sitting outside its block got a **second heading with the same
    name** at the bottom of the table. Measured in the browser after adding an
    item to "Header" while "Page footer" was the last section.

    The number is left alone when it already belongs to the section — a
    created item was numbered on the way in and renumbering it here would
    waste a number for no reason.
    """
    position = index_of(items, item_id)
    out = list(items)
    moved = out.pop(position)
    section = _section_of(moved)

    # The section's index as the *rest* of the pack defines it. Computed on
    # ``out`` rather than on ``items``: with the item still in, its own stale
    # number defines the index it is being compared against, so a move into a
    # brand-new section decided the number was already correct and kept it.
    index = section_indices(out).get(section)
    if index is None or _parts(moved)[0] != index:
        moved["item_num"] = next_number(out, section, _depth_of(moved))

    # Only move it if the section is not already one contiguous run *with* it.
    # Appending unconditionally sent an item that was merely first in its own
    # block to the end of that block — a reorder nobody asked for.
    members = [i for i, item in enumerate(items)
               if _section_of(item) == section]
    if members and members[-1] - members[0] == len(members) - 1:
        out.insert(position, moved)
        return out

    destination = [i for i, item in enumerate(out)
                   if _section_of(item) == section]
    out.insert(destination[-1] + 1 if destination else len(out), moved)
    return out


def relocate(items, item_id: str, section: str) -> list[dict]:
    """Move one item into another section, appended at its end.

    The item takes the next free number there and vacates its old one. Its
    former siblings are not renumbered — the gap is the honest result, and
    closing it would restate numbers other people have cited.
    """
    section = (section or "").strip()
    if not section:
        raise OrderError("A section needs a name.")
    if len(section) > MAX_SECTION_NAME:
        raise OrderError(
            f"A section name is limited to {MAX_SECTION_NAME} characters.")

    position = index_of(items, item_id)
    # Choosing the section an item is already in is a deliberate no-op rather
    # than a move to the end of its block: the user picked "no change", and
    # shuffling the row would be a surprise.
    if _section_of(items[position]) == section:
        return list(items)

    out = list(items)
    out[position] = dict(out[position], section=section)
    return regroup_item(out, item_id)


def rename_section(items, old_name: str, new_name: str) -> int:
    """Rename a section across every item in it. Returns rows touched.

    Nothing is renumbered: the section keeps its index, so every number in
    the checklist still means what it meant. A rename onto a name another
    section already uses is refused rather than merged — merging two sections
    changes the indices, and every number in every later section would have
    to be restated.
    """
    new_name = (new_name or "").strip()
    old_name = (old_name or "").strip()
    if not new_name:
        raise OrderError("A section needs a name.")
    if len(new_name) > MAX_SECTION_NAME:
        raise OrderError(
            f"A section name is limited to {MAX_SECTION_NAME} characters.")
    if new_name == old_name:
        return 0

    existing = sections(items)
    if old_name not in existing:
        raise OrderError(f"No section called {old_name!r} in this checklist.")
    if new_name in existing:
        raise OrderError(
            f"A section called {new_name!r} already exists. Move the items "
            f"into it instead — merging two sections would renumber every "
            f"item in the sections after them.")

    touched = 0
    for item in items:
        if _section_of(item) == old_name:
            item["section"] = new_name
            touched += 1
    return touched


__all__ = ["MAX_SECTION_NAME", "OrderError", "index_of", "move",
           "next_number", "regroup_item", "relocate", "rename_section",
           "renumber_section", "section_indices", "sections"]
