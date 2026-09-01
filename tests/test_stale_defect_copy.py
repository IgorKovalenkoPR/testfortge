"""The bug page told operators to suspect a defect that had been fixed.

Walking /bug-reports, the "No attachments captured" banner listed three
reasons a bug has no evidence, and the second was:

    (2) the page screenshot was captured but did not reach the finding
        (known wiring gap)

That gap is closed, and the code that closed it says so in its own comment.
``engine/live_executor.py`` injects the page shot into every finding that has
none, and the comment above that fan-out names this banner as the thing it
was making misleading — "``finding['screenshot']`` stayed '' all the way into
``create_bug_from_walkthrough_finding``, which then wrote ``attachments=[]``
and made /bug-reports render the misleading … banner even on runs where
Playwright took a perfectly valid page shot."

So the usual shape of this series, inverted. Not a stated guard that was
never wired — a stated *defect* that no longer exists, still being told to
users. It sends an operator looking in the wrong place, and it buries the two
reasons that remain true.

The remaining reasons are both real and both actionable: no Base URL means
Playwright never ran, and a run that died early saved nothing.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from engine.i18n import TRANSLATIONS

KEY = "bug_no_attachments_body"


class TestTheBannerNamesOnlyLiveReasons:

    @pytest.mark.parametrize("lang", ["en", "ua"])
    def test_the_fixed_gap_is_not_offered_as_a_reason(self, lang):
        body = TRANSLATIONS[lang][KEY]
        for gone in ("wiring gap", "розрив у дротах"):
            assert gone not in body, (lang, body)

    @pytest.mark.parametrize("lang,needle", [("en", "Base URL"),
                                             ("ua", "Base URL")])
    def test_the_reasons_that_are_still_true_survive(self, lang, needle):
        """A fix that deleted the whole banner would pass the test above."""
        assert needle in TRANSLATIONS[lang][KEY]

    @pytest.mark.parametrize("lang", ["en", "ua"])
    def test_the_numbering_has_no_hole_in_it(self, lang):
        """Removing "(2)" and leaving "(3)" behind would read as a list with
        a missing item — which is how a reader tells that something was cut
        rather than that there are two reasons."""
        numbers = re.findall(r"\((\d)\)", TRANSLATIONS[lang][KEY])
        assert numbers == [str(n + 1) for n in range(len(numbers))], numbers
        assert len(numbers) == 2, numbers

    def test_the_template_fallback_says_the_same_thing(self):
        """The fallback only renders if the key goes missing, which is
        exactly when nobody is looking — and it carried the stale reason
        too. Two copies of one sentence disagreeing is how the truncated
        strings in this dictionary went unnoticed for as long as they did.
        """
        source = pathlib.Path("templates/bug_reports.html").read_text(
            encoding="utf-8")
        assert "wiring gap" not in source


class TestTheGapIsActuallyClosed:
    """The premise. Deleting the sentence would be wrong if the defect were
    still live, so this asserts the fix it describes is still in place."""

    def test_the_executor_fans_the_page_shot_into_findings(self):
        source = pathlib.Path("engine/live_executor.py").read_text(
            encoding="utf-8")
        # The fan-out itself: every finding from this page walk that has no
        # screenshot of its own inherits the page shot.
        assert 'if not f.get("screenshot"):' in source
        assert 'f["screenshot"] = shot' in source

    def test_a_finding_with_its_own_screenshot_keeps_it(self):
        """The other half of that loop, and the reason it is a condition
        rather than an assignment: a heuristic that gains per-element
        capture must not have its shot overwritten by the page's."""
        source = pathlib.Path("engine/live_executor.py").read_text(
            encoding="utf-8")
        fan_out = source.split('f["screenshot"] = shot')[0][-400:]
        assert 'if not f.get("screenshot")' in fan_out, fan_out
