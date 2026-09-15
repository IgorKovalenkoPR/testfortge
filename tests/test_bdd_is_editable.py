"""The BDD view was designed, migrated, read — and had no writer.

``TestCase.gherkin`` has been a column since its migration.
``engine.gherkin.ensure_gherkin`` has always preferred it over the derived
text, and says so: *"the column holds only text an operator hand-edited…
Hand-edited text always wins."* The page rendered it in a ``<pre>`` under
the hint *"Derived from the columns above. Edit the case, not this"* — which
read as a policy and was in fact a description of an impossibility: the
field was absent from ``engine.editable``'s allowlist, so the PATCH endpoint
answered 400 for it and no route in the product accepted an edited scenario.

The second half matters as much as the first. Even with a writer, the
``.feature`` download derived every scenario from the manual columns and
never consulted the stored text, so an operator's edit would have been
visible on the page and silently absent from the file — the failure that is
worse than the missing feature, because nothing says it happened.
"""
from __future__ import annotations

import io
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import editable as _editable   # noqa: E402
from engine import gherkin as _gherkin     # noqa: E402


class _Case:
    """The shape both the exporter and ``ensure_gherkin`` read."""

    def __init__(self, **kw):
        self.id = kw.get("id", "TC_001")
        self.summary = kw.get("summary", "Verify the contact form")
        self.section = kw.get("section", "Contact")
        self.preconditions = kw.get("preconditions", "The page is open")
        self.test_steps = kw.get("test_steps", "1. Fill the fields\n"
                                               "2. Submit the form")
        self.expected_result = kw.get("expected_result", "It submits")
        self.priority = kw.get("priority", "High")
        self.category = kw.get("category", "Positive")
        self.testing_type = kw.get("testing_type", "Functional")
        self.gherkin = kw.get("gherkin", "")


HAND_WRITTEN = (
    "@TC-001 @smoke\n"
    "Scenario: the wording the tester actually signed off\n"
    "  Given I am signed in as an editor\n"
    "  When I submit the contact form\n"
    "  Then I see the confirmation panel\n"
)


class TestTheFieldIsWritable:

    def test_gherkin_is_in_the_allowlist(self):
        assert "gherkin" in _editable.editable_fields("test_case"), (
            "without this the PATCH endpoint raises FieldNotEditable and "
            "answers 400 — which is what 'there is no way to edit the "
            "generated BDD' meant")

    def test_the_format_can_be_switched_per_case(self):
        """The .feature export refuses a pack with no BDD case and tells
        the operator to "switch individual cases to BDD in the editor".
        There was no such control."""
        assert "tc_format" in _editable.editable_fields("test_case")
        assert _editable.validate(
            "test_case", {"tc_format": "gherkin"}) == {"tc_format": "gherkin"}

    def test_a_format_outside_the_two_is_refused(self):
        import pytest
        with pytest.raises(Exception):
            _editable.validate("test_case", {"tc_format": "cucumber"})


class TestWhatIsStoredIsWhatIsRendered:

    def test_the_page_shows_the_hand_written_text(self):
        assert _gherkin.ensure_gherkin(
            _Case(gherkin=HAND_WRITTEN)) == HAND_WRITTEN

    def test_clearing_it_goes_back_to_deriving(self):
        """Empty is the revert, which is why the field takes a blank."""
        derived = _gherkin.ensure_gherkin(_Case(gherkin=""))
        assert "Scenario:" in derived
        assert "signed off" not in derived

    def test_the_feature_download_ships_the_hand_written_text(self):
        feature = _gherkin.features_from_test_cases(
            [_Case(gherkin=HAND_WRITTEN)])[0]
        rendered = feature.render()
        assert "the wording the tester actually signed off" in rendered, (
            "the export derived unconditionally, so an edit shown on the "
            "page was silently replaced in the file nobody re-read")
        assert "Given I am signed in as an editor" in rendered
        assert rendered.startswith("Feature: Contact")

    def test_an_unedited_case_still_derives_in_the_download(self):
        rendered = _gherkin.features_from_test_cases([_Case()])[0].render()
        assert "Scenario: Verify the contact form" in rendered

    def test_hand_written_text_is_indented_into_the_feature(self):
        """A scenario written flush left still has to nest correctly, and
        one the operator indented must not end up double-indented."""
        for body in (HAND_WRITTEN,
                     "\n".join("    " + ln for ln in
                               HAND_WRITTEN.strip().splitlines())):
            rendered = _gherkin.features_from_test_cases(
                [_Case(gherkin=body)])[0].render()
            assert "  Scenario: the wording the tester actually signed off" \
                in rendered
            assert "      Scenario:" not in rendered

    def test_the_result_is_still_valid_gherkin(self):
        rendered = _gherkin.features_from_test_cases(
            [_Case(gherkin=HAND_WRITTEN)])[0].render()
        assert _gherkin.lint(rendered) == []


class TestTheArchiveAgreesWithThePage:
    """One call site could have been patched instead; the rule lives in
    ``scenario_from_test_case`` so every route that renders a ``.feature``
    inherits it rather than having to remember."""

    def test_the_zip_carries_the_edit(self):
        from routes.generation import _feature_archive
        blob = _feature_archive([_Case(gherkin=HAND_WRITTEN)], "Demo")
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = [n for n in zf.namelist() if n.endswith(".feature")]
            assert names
            body = zf.read(names[0]).decode("utf-8")
        assert "the wording the tester actually signed off" in body
