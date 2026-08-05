"""TestFortge — a regeneration that does not destroy manual edits (E4.7).

E4.1 gave every editable row an ``ai_generated`` flag and said this task would
be the policy that reads it. Measured behaviour before this module existed:

    save_test_cases(pack)            # generate
    editable.patch(... summary ...)   # a person rewrites one case
    save_test_cases(pack)            # click Generate again
    → the row's summary is the generated text again,
      and its metadata still says row_version=2, ai_generated=False

So the edit was gone *and* the row still claimed to be human-edited — the
worst of the two outcomes, because the "edited" pill and E4.7's own guard
would both have believed a row that no longer held the edit. (E4.1's
``_restore_edit_metadata`` preserves the flags across the wipe-and-replace; it
was never meant to preserve the content.)

The policy
----------
``merge`` (the default, and what the Generate button uses):

* an existing row whose ``ai_generated`` is False is **kept exactly as it is**
  — every field, its version, its provenance;
* an incoming row with no edited counterpart is written as generated;
* an edited row that the new pack does not mention at all is **still kept**,
  because a wipe-and-replace would otherwise delete it.

``replace``: the old behaviour, available deliberately — a user who wants the
generator's version back should be able to say so — and it reports how many
edits it is about to discard rather than doing it quietly.

Why row-level and not field-level
---------------------------------
A field-level merge ("keep the summary they rewrote, take the new expected
result") needs per-field provenance, and there is only a row-level flag.
Inventing the field-level answer from a row-level fact would mean guessing
which fields a person meant to own — and guessing wrong silently is exactly
the failure this task exists to stop. If per-field provenance ever arrives,
this is the module that changes.

Nothing here is silent
----------------------
:class:`MergeReport` exists so the route can say "12 regenerated, 3 of your
edits kept". A merge the user cannot see is as confusing as the overwrite it
replaced: they clicked Generate, the numbers did not all move, and nothing
explained why.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from engine.log import get_logger

log = get_logger(__name__)

POLICIES = ("merge", "replace")
DEFAULT_POLICY = "merge"


@dataclass
class MergeReport:
    """What a regeneration did, in terms a person can be told."""
    policy: str = DEFAULT_POLICY
    #: Incoming rows written as generated.
    generated: int = 0
    #: Edited rows kept instead of the incoming version, by public id.
    kept: list[str] = field(default_factory=list)
    #: Edited rows the new pack did not mention, kept rather than deleted.
    orphans_kept: list[str] = field(default_factory=list)
    #: Edited rows discarded — only ever non-empty under ``replace``.
    discarded: list[str] = field(default_factory=list)

    @property
    def protected(self) -> int:
        return len(self.kept) + len(self.orphans_kept)

    def message(self) -> str:
        """One sentence for a flash. Empty when there is nothing to say."""
        if self.policy == "replace":
            if not self.discarded:
                return ""
            return (f"Replaced the whole pack — {len(self.discarded)} manually "
                    f"edited item(s) were discarded: "
                    f"{_sample(self.discarded)}.")
        if not self.protected:
            return ""
        parts = [f"{self.generated} regenerated"]
        if self.kept:
            parts.append(f"{len(self.kept)} of your edits kept "
                         f"({_sample(self.kept)})")
        if self.orphans_kept:
            parts.append(f"{len(self.orphans_kept)} edited item(s) the new "
                         f"generation did not cover, kept "
                         f"({_sample(self.orphans_kept)})")
        return "; ".join(parts) + "."


def _sample(ids, limit: int = 5) -> str:
    shown = list(ids)[:limit]
    return ", ".join(shown) + (" …" if len(ids) > limit else "")


def merge(existing: list[dict], incoming: list[dict],
          metadata: dict[str, dict], *, policy: str = DEFAULT_POLICY,
          id_key: str = "id") -> tuple[list[dict], MergeReport]:
    """The pack to write, and what happened. Neither argument is mutated.

    ``existing`` and ``incoming`` are stored-shape dicts; ``metadata`` maps
    public id → the row's edit metadata (``db.load_edit_metadata``).

    Order follows the incoming pack — a regeneration is entitled to decide the
    order — with kept orphans appended, because their place in the old
    sequence no longer means anything.
    """
    if policy not in POLICIES:
        raise ValueError(
            f"{policy!r} is not a regeneration policy. Known: "
            f"{', '.join(POLICIES)}.")

    report = MergeReport(policy=policy)
    edited_ids = {
        str(row.get(id_key)) for row in existing
        if not (metadata.get(str(row.get(id_key)), {}).get("ai_generated",
                                                           True))
        and row.get(id_key)
    }

    if policy == "replace":
        report.generated = len(incoming)
        report.discarded = sorted(edited_ids)
        return list(incoming), report

    by_id = {str(row.get(id_key)): row for row in existing
             if row.get(id_key)}

    out: list[dict] = []
    seen: set[str] = set()
    for row in incoming:
        row_id = str(row.get(id_key) or "")
        if row_id and row_id in edited_ids:
            # Keep the human's row whole. Taking any field from the incoming
            # version would be the field-level guess this module refuses to
            # make.
            out.append(dict(by_id[row_id]))
            report.kept.append(row_id)
        else:
            out.append(dict(row))
            report.generated += 1
        if row_id:
            seen.add(row_id)

    for row_id in sorted(edited_ids - seen):
        out.append(dict(by_id[row_id]))
        report.orphans_kept.append(row_id)

    if report.protected:
        log.info("regeneration merge: %d generated, %d edited row(s) kept",
                 report.generated, report.protected)
    return out, report


__all__ = ["DEFAULT_POLICY", "POLICIES", "MergeReport", "merge"]
