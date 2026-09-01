"""The estimation form's ceilings disagreed with the server's, both ways.

Walked /estimation and read the config inputs out of the DOM, then read the
clamps in ``routes/estimation.py::_build_input`` next to them:

    field                  form said   server allowed
    additional_platforms   max="50"    30
    minutes_per_tc         max="60"    120
    buffer_percent         max="100"   200

Two different defects wearing one shape.

Over-permissive: the browser accepts 40 platforms, the server clamps to 30
and says nothing. The estimate comes back priced for 30 and the form
redisplays 30, so the operator's number was changed without a word.

Under-permissive, and the worse half: ``EST_MAX_MINUTES_PER_TC = 120`` is a
configuration knob nobody can reach. Measured in the page —

    input.value = "90"  →  checkValidity() false
                           "Value must be less than or equal to 60."
                           form.checkValidity() false

— so the browser refuses to submit a value the server was configured to
honour. Raising the config did nothing, which is the quiet kind of broken:
the operator concludes the setting is ignored, and they are right.

The fix is not a second copy of the numbers in the template. It is that the
form's bounds *are* the server's bounds — one source, read from the config
the clamp already reads. A value the form accepts is a value the server
honours; a value the form refuses is one the server would have refused too.

This file is a gate as much as a test: the three attributes looked entirely
plausible sitting in the HTML, and nothing but reading both files together
said otherwise.
"""
from __future__ import annotations

import pathlib
import re

import pytest

TEMPLATE = pathlib.Path("templates/estimation.html")

# field name → the config key whose value the server clamps to
BOUNDED = {
    "additional_platforms": "EST_MAX_ADDITIONAL_PLATFORMS",
    "minutes_per_tc": "EST_MAX_MINUTES_PER_TC",
    "buffer_percent": "EST_MAX_BUFFER_PERCENT",
}


def _attributes(html: str, name: str) -> str:
    match = re.search(rf'<input[^>]*name="{name}"[^>]*>', html)
    assert match, f"no input named {name} on the page"
    return match.group(0)


@pytest.fixture
def page(client):
    response = client.get("/estimation")
    assert response.status_code == 200
    return response.get_data(as_text=True)


class TestTheFormOffersWhatTheServerAccepts:

    @pytest.mark.parametrize("field,key", sorted(BOUNDED.items()))
    def test_the_rendered_max_is_the_configured_one(self, page, app, field,
                                                    key):
        expected = int(app.config[key])
        rendered = _attributes(page, field)
        assert f'max="{expected}"' in rendered, rendered

    @pytest.mark.parametrize("field", sorted(BOUNDED))
    def test_the_max_is_not_empty(self, page, field):
        """A dropped context variable is loud — Jinja raises here rather
        than rendering nothing, measured. A config key holding ``None``
        is not: it renders ``max=""``, which is not a stricter ceiling
        but *no* ceiling, and the page still returns 200."""
        rendered = _attributes(page, field)
        assert 'max=""' not in rendered, rendered
        assert re.search(r'max="\d+"', rendered), rendered

    def test_a_changed_config_moves_the_form(self, client, app):
        """The point of the whole fix. Pinning the value, not the code:
        a template that hard-coded today's 30 would pass every test
        above and fail this one."""
        original = app.config["EST_MAX_ADDITIONAL_PLATFORMS"]
        try:
            app.config["EST_MAX_ADDITIONAL_PLATFORMS"] = 7
            body = client.get("/estimation").get_data(as_text=True)
            assert 'max="7"' in _attributes(body, "additional_platforms")
        finally:
            app.config["EST_MAX_ADDITIONAL_PLATFORMS"] = original

    def test_the_minima_are_still_there(self, page):
        """The control. Dropping every bound would satisfy "the form does
        not refuse what the server allows" completely."""
        assert 'min="0"' in _attributes(page, "additional_platforms")
        assert 'min="1"' in _attributes(page, "minutes_per_tc")
        assert 'min="0"' in _attributes(page, "buffer_percent")


class TestNoCeilingIsWrittenTwice:

    @pytest.mark.parametrize("field", sorted(BOUNDED))
    def test_the_template_does_not_hard_code_one(self, field):
        """The gate. Each of the three was a literal that read as
        deliberate, and the only way to see it was wrong was to open
        ``config.py`` at the same time."""
        rendered = _attributes(TEMPLATE.read_text(encoding="utf-8"), field)
        assert not re.search(r'max="\d+"', rendered), (
            f'{field} carries a literal max in the template again; render '
            f'the configured limit instead — see routes/estimation.py '
            f'_form_limits')


class TestTheServerStillClampsAnyway:
    """The form is a courtesy, not the rule — curl, a stale tab and a
    replayed request all skip it, which is why ``_build_input`` clamps."""

    def test_a_value_over_the_ceiling_is_cut(self, app):
        from routes.estimation import _clamp
        ceiling = app.config["EST_MAX_ADDITIONAL_PLATFORMS"]
        assert _clamp(999, 0, ceiling) == ceiling

    def test_a_value_the_form_now_allows_survives_the_clamp(self, app):
        """The two halves agreeing, asserted as such: the largest number
        the form will submit must come through the server unchanged."""
        from routes.estimation import _clamp
        for field, key in BOUNDED.items():
            ceiling = app.config[key]
            assert _clamp(ceiling, 0, ceiling) == ceiling, field
