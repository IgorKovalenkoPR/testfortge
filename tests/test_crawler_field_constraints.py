"""The crawler must keep the form-control constraints, not just names.

Why this matters: the deterministic test-case generator decides which
cases are *justified* from these attributes. A required-field negative is
only honest when the markup says ``required``; a boundary case needs a
real ``maxlength`` / ``min`` / ``max`` to sit on; a drop-down step can
only name a choice if the option values survived.

The parser previously kept ``name`` / ``type`` / ``placeholder`` and threw
the rest away, so every consumer had to guess — and guessing is how a
suite fills up with cases that fail for the wrong reason. The house style
calls that out directly (``qa_knowledge/style/house_style.yaml`` →
``anti_patterns`` → "Inventing UI that the artifacts do not evidence").
"""
from __future__ import annotations

import pytest

from engine.site_crawler import _PageParser


ORDER_FORM = """
<html><head><title>Order</title></head><body>
<h1>Customer order</h1>
<form method="post" action="/post">
  <label for="custname">Customer name</label>
  <input type="text" id="custname" name="custname" required maxlength="60">
  <label for="custtel">Telephone</label>
  <input type="tel" id="custtel" name="custtel" pattern="[0-9]{9}">
  <input type="email" name="custemail" placeholder="you@example.com" required>
  <label>Pizza size
    <select name="size">
      <option value="small">Small</option>
      <option value="medium">Medium</option>
      <option value="large">Large</option>
    </select>
  </label>
  <input type="number" name="qty" min="1" max="10" required>
  <textarea name="comments" maxlength="500"></textarea>
  <button>Submit order</button>
</form></body></html>
"""


@pytest.fixture(scope="module")
def fields() -> dict:
    p = _PageParser()
    p.feed(ORDER_FORM)
    assert p.forms, "no form parsed"
    return {f["name"]: f for f in p.forms[0]["fields"]}


class TestRequiredFlag:
    def test_required_is_captured(self, fields):
        assert fields["custname"]["required"] is True
        assert fields["custemail"]["required"] is True
        assert fields["qty"]["required"] is True

    def test_optional_fields_are_not_marked_required(self, fields):
        # The generator must not emit a required-field negative for these.
        assert fields["custtel"]["required"] is False
        assert fields["comments"]["required"] is False

    def test_aria_required_counts_too(self):
        p = _PageParser()
        p.feed('<form><input name="x" aria-required="true"></form>')
        assert p.forms[0]["fields"][0]["required"] is True


class TestBoundaryConstraints:
    def test_maxlength(self, fields):
        assert fields["custname"]["maxlength"] == "60"
        assert fields["comments"]["maxlength"] == "500"

    def test_numeric_range(self, fields):
        assert fields["qty"]["min"] == "1"
        assert fields["qty"]["max"] == "10"

    def test_pattern(self, fields):
        assert fields["custtel"]["pattern"] == "[0-9]{9}"

    def test_absent_constraints_are_omitted_not_faked(self, fields):
        # Absence must be distinguishable from a value, so the generator
        # can skip the boundary case instead of inventing a limit.
        assert "maxlength" not in fields["qty"]
        assert "min" not in fields["custname"]


class TestSelectOptions:
    def test_option_labels_are_captured_in_order(self, fields):
        assert fields["size"]["options"] == ["Small", "Medium", "Large"]

    def test_value_is_used_when_the_option_has_no_text(self):
        p = _PageParser()
        p.feed('<form><select name="s"><option value="only"></option>'
               "</select></form>")
        assert p.forms[0]["fields"][0]["options"] == ["only"]

    def test_option_list_is_capped(self):
        opts = "".join(f"<option>o{i}</option>" for i in range(40))
        p = _PageParser()
        p.feed(f'<form><select name="s">{opts}</select></form>')
        assert len(p.forms[0]["fields"][0]["options"]) <= 12


class TestLabels:
    def test_label_for_id_is_matched(self, fields):
        assert fields["custname"]["label"] == "Customer name"
        assert fields["custtel"]["label"] == "Telephone"

    def test_label_before_its_control_is_still_matched(self):
        # for -> id is resolved once the whole form has been seen, so a
        # label that precedes its input still lands.
        p = _PageParser()
        p.feed('<form><label for="late">Late label</label>'
               '<input id="late" name="late"></form>')
        assert p.forms[0]["fields"][0]["label"] == "Late label"

    def test_wrapping_label_does_not_swallow_option_text(self, fields):
        """<label>Size <select><option>Small</option></select></label>

        Regression: the label used to come out as
        "Pizza size Small Medium Large" because text kept accumulating
        past the nested control.
        """
        assert fields["size"]["label"] == "Pizza size"

    def test_unlabelled_field_has_no_label_key(self, fields):
        assert not fields["custemail"].get("label")


class TestBackwardCompatibility:
    def test_existing_keys_are_unchanged(self, fields):
        f = fields["custemail"]
        assert f["name"] == "custemail"
        assert f["type"] == "email"
        assert f["placeholder"] == "you@example.com"

    def test_submit_text_and_heading_still_populate(self):
        p = _PageParser()
        p.feed(ORDER_FORM)
        form = p.forms[0]
        assert form["submit_text"] == "Submit order"
        assert form["heading"] == "Customer order"
        assert form["method"] == "POST"
