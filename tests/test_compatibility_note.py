"""The exported quote contradicted itself in one sentence.

Walked /estimation with the platform count raised above nine, exported the
xlsx and read cell C34:

    ** Compatibility testing will be performed on 20 additional
       combinations:
    - Windows 11
    - Apple MacBook Air 2025
    … nine bullets total

Twenty in the sentence, nine underneath it, and the price computed on
twenty. A client reading that has to decide which half is wrong.

The cause is a slice that cannot fail. Both callers build the list as
``_DEFAULT_COMPAT_PLATFORMS[:count]``, the reference list holds exactly nine
names, and ``list[:20]`` returns nine without complaint. The default count
is also nine, so the two agreed for every estimate anybody had looked at —
and the config allows up to thirty.

The fix keeps the count and tells the truth about the names, because the
count is what the estimate was priced on: shrinking it to fit the names on
hand would change the quote to make a sentence tidy. Nine named, eleven to
be agreed, twenty billed.
"""
from __future__ import annotations

import os
import tempfile

import openpyxl
import pytest

from engine.estimation_service import _DEFAULT_COMPAT_PLATFORMS as REFERENCE
from engine.qa_estimator import (Feature, compatibility_note,
                                 compute_estimation, export_estimation_xlsx)


def _names(n):
    return [f"Platform {i}" for i in range(n)]


def _bullets(note):
    return note.count("\n- ")


class TestTheSentenceAgreesWithItsOwnList:

    def test_a_full_list_reads_as_it_always_did(self):
        note = compatibility_note(9, _names(9))
        assert note.splitlines()[0].endswith("9 additional combinations:")
        assert _bullets(note) == 9

    def test_a_short_list_says_how_short(self):
        note = compatibility_note(20, _names(9))
        first = note.splitlines()[0]
        assert "20 additional combinations." in first
        assert "9 are proposed below" in first
        assert "remaining 11 are to be agreed" in first
        assert _bullets(note) == 9

    def test_the_count_is_never_reduced_to_fit_the_names(self):
        """The whole point. The count is what the estimate was priced on,
        and a quote that says nine while charging for twenty is a worse
        document than one that says twenty and names nine."""
        for count in (10, 20, 30):
            assert f" {count} additional" in compatibility_note(
                count, _names(9))

    def test_no_names_at_all_is_a_plain_sentence(self):
        note = compatibility_note(4, [])
        assert note.endswith("4 additional combinations.")
        assert "\n- " not in note

    def test_blank_entries_do_not_become_empty_bullets(self):
        note = compatibility_note(3, ["Windows 11", "", None])
        assert _bullets(note) == 1
        assert "- \n" not in note and not note.endswith("- ")

    @pytest.mark.parametrize("count", [0, 1, 9, 30])
    def test_the_count_always_appears_exactly_as_given(self, count):
        assert f"performed on {count} additional" in compatibility_note(
            count, _names(min(count, 9)))


class TestThroughTheRealExport:
    """The note builder could be right while the cell stayed wrong — it was
    an inline expression in ``export_estimation_xlsx`` until this fix."""

    def _c34(self, count):
        result = compute_estimation(
            features=[Feature(name="Login", test_cases=10)],
            rate_usd=50, additional_platforms=count, minutes_per_tc=5,
            buffer=1.12, project_name="Probe",
            primary_platform="Windows 10",
            platforms_list=REFERENCE[:count], source="manual", source_ref="")
        path = os.path.join(tempfile.mkdtemp(), "estimate.xlsx")
        export_estimation_xlsx(result, path)
        sheet = openpyxl.load_workbook(path).active
        return sheet["A34"].value, sheet["C34"].value

    def test_the_default_nine_is_unchanged(self):
        count, note = self._c34(9)
        assert count == 9
        assert note.splitlines()[0].endswith("9 additional combinations:")
        assert _bullets(note) == 9

    def test_twenty_no_longer_promises_twenty_names(self):
        count, note = self._c34(20)
        assert count == 20, "the priced count must survive"
        assert _bullets(note) == 9
        assert "to be agreed" in note

    def test_the_neighbouring_note_is_untouched(self):
        """C33 shares the cell block and says something different and
        still true — a fix that rewrote the wrong cell would be caught
        here rather than by a reader of the spreadsheet."""
        result = compute_estimation(
            features=[Feature(name="Login", test_cases=10)], rate_usd=50,
            additional_platforms=9, minutes_per_tc=5, buffer=1.12,
            project_name="Probe", primary_platform="Ubuntu 24.04",
            platforms_list=REFERENCE, source="manual", source_ref="")
        path = os.path.join(tempfile.mkdtemp(), "estimate.xlsx")
        export_estimation_xlsx(result, path)
        assert "Ubuntu 24.04" in openpyxl.load_workbook(
            path).active["C33"].value


class TestThePremise:

    def test_the_reference_list_is_shorter_than_the_allowed_count(self):
        """If these ever meet, the defect stops existing and this whole
        file is about nothing — which is worth failing loudly for rather
        than leaving as tests that pass vacuously."""
        from config import EST_MAX_ADDITIONAL_PLATFORMS
        assert len(REFERENCE) < EST_MAX_ADDITIONAL_PLATFORMS, (
            len(REFERENCE), EST_MAX_ADDITIONAL_PLATFORMS)

    def test_the_slice_that_caused_it_still_silently_truncates(self):
        """Not fixed, because it cannot be: Python has no other answer
        for ``list[:20]`` on nine items. That is exactly why the sentence
        had to stop assuming it got what it asked for."""
        assert len(REFERENCE[:20]) == len(REFERENCE)
