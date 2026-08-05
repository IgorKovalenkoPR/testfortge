"""The inline-edit component — E4.2.

Two things are checked here, and a third is checked in a browser rather
than pretended at.

**The static guarantees.** The app sends a strict CSP with a per-request
nonce and no ``'unsafe-inline'`` for scripts, so an ``onclick=`` attribute
would not run — silently, which is the worst way for an editor to be
broken. These tests fail the build if one appears, and if the component
stops being loadable as an external file.

**The markup contract.** The data attributes are an agreement between
``templates/_inline_edit.html`` and ``static/js/inline-edit.js``, and
between both and the field names ``engine.editable`` declares. A mismatch
in any direction produces a field that quietly is not editable, so the
three are asserted against each other rather than trusted to stay in step.

Behaviour under a real click is verified in the browser during development
— see the E4.2 commit message. What can be pinned in CI is pinned here.
"""

import pathlib
import re

import pytest

from engine import editable

JS = pathlib.Path("static/js/inline-edit.js")
MACROS = pathlib.Path("templates/_inline_edit.html")
CSS = pathlib.Path("static/css/style.css")


@pytest.fixture(scope="module")
def js() -> str:
    return JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def macros() -> str:
    return MACROS.read_text(encoding="utf-8")


# ── The files exist and are wired the CSP-safe way ────────────────

class TestAssets:
    def test_the_component_is_an_external_file(self):
        assert JS.is_file()
        assert JS.stat().st_size > 1000

    def test_the_macros_exist(self):
        assert MACROS.is_file()

    def test_the_styles_are_in_the_stylesheet(self):
        text = CSS.read_text(encoding="utf-8")
        # Not in a <style> block in a template: the CSP grants
        # style-src 'unsafe-inline' today, but a component whose styles
        # live in one template cannot be reused by the other three.
        for selector in (".ie ", ".ie-control", ".ie-message", ".ie-live"):
            assert selector in text, selector

    def test_the_script_tag_carries_a_nonce(self, macros):
        # Passed in rather than read from the context: a macro imported
        # without ``with context`` cannot see context variables, which is
        # how the first version of this file rendered nothing at all.
        assert 'nonce="{{ nonce }}"' in macros
        assert "editable_assets(nonce" in macros

    def test_the_script_is_only_loaded_when_editing_is_on(self, macros):
        # A component that loads but has nothing to bind is harmless; one
        # that loads on every page of an instance with editing off is
        # bytes nobody asked for.
        assert "if editors_enabled" in macros


class TestNoInlineHandlers:
    """An inline handler would be blocked by the CSP without a word."""

    @pytest.mark.parametrize("attribute", [
        "onclick", "onchange", "onblur", "onkeydown", "onfocus", "oninput",
        "onsubmit",
    ])
    def test_no_inline_handler_in_the_macros(self, macros, attribute):
        assert attribute not in macros.lower()

    def test_the_component_sets_no_handler_attributes(self, js):
        # setAttribute('onclick', …) is the same hole with more steps.
        assert not re.search(r"setAttribute\(\s*['\"]on\w+", js)

    def test_the_component_uses_delegated_listeners(self, js):
        # Delegation is also what makes a row rendered later editable
        # without re-binding.
        assert "document.addEventListener('click'" in js
        assert "document.addEventListener('keydown'" in js

    def test_no_eval_or_new_function(self, js):
        for forbidden in ("eval(", "new Function("):
            assert forbidden not in js, forbidden


class TestNoHtmlInjection:
    def test_values_are_written_as_text_not_html(self, js):
        """A test case's summary is user-supplied and goes back on screen.

        ``innerHTML`` with it would make every field a stored-XSS sink;
        ``textContent`` cannot be.
        """
        assert "innerHTML" not in js
        assert "insertAdjacentHTML" not in js
        assert "textContent" in js

    def test_messages_from_the_server_are_written_as_text(self, js):
        # The 409 body contains a server-authored message, but the same
        # code path shows validation messages that quote nothing — keeping
        # it textContent means no path has to be audited.
        assert re.search(r"note\.textContent\s*=", js)


# ── The markup contract ───────────────────────────────────────────

class TestMarkupContract:
    ATTRIBUTES = ("data-ie-entity", "data-ie-id", "data-ie-field",
                  "data-ie-version", "data-ie-kind", "data-ie-original")

    @pytest.mark.parametrize("attribute", ATTRIBUTES)
    def test_both_halves_agree_on_every_attribute(self, js, macros,
                                                  attribute):
        assert attribute in macros, f"{attribute} missing from the macro"
        assert attribute in js, f"{attribute} missing from the component"

    def test_the_component_patches_the_endpoint_e41_exposes(self, js):
        assert "/api/edit/" in js
        assert "'PATCH'" in js

    def test_it_sends_the_csrf_token(self, js):
        # Flask-WTF protects every non-GET method, PATCH included, and a
        # missing token is a 400 that looks like a validation failure.
        assert "X-CSRFToken" in js
        assert 'meta[name="csrf-token"]' in js

    def test_it_sends_the_row_version(self, js):
        # Without it the server cannot refuse a stale write, and E4.1's
        # concurrency guard is decorative.
        assert "row_version" in js

    def test_it_sends_only_the_changed_field(self, js):
        # PATCH semantics, and the reason a colleague editing another field
        # of the same row is not clobbered.
        assert "body.changes[field] = value" in js


class TestVersionIsPerRow:
    def test_a_save_updates_every_field_of_the_row(self, js):
        """``row_version`` belongs to the row, not the field.

        Editing two fields of one test case would otherwise make the second
        edit look like a conflict.
        """
        assert "siblingFields" in js
        assert "setRowVersion" in js
        assert re.search(r'data-ie-entity="\$\{entity\}"'
                         r'\]\[data-ie-id="\$\{id\}"', js)


class TestConflictHandling:
    def test_a_conflict_keeps_the_editor_open(self, js):
        """Somebody who has just written three sentences of test steps must
        be able to copy them before reloading."""
        assert "resp.status === 409" in js
        # The close() that would discard the text is not on this path: the
        # 409 branch focuses the control again instead.
        conflict = js.split("resp.status === 409")[1].split("return;")[0]
        assert "close(host" not in conflict
        assert "control.focus()" in conflict

    def test_a_network_failure_also_keeps_the_text(self, js):
        catch_block = js.split("} catch (err) {")[1].split("return;")[0]
        assert "close(host" not in catch_block
        assert "still here" in catch_block

    def test_a_conflict_offers_a_reload(self, js):
        assert "location.reload()" in js


# ── Accessibility ─────────────────────────────────────────────────

class TestAccessibility:
    def test_fields_are_keyboard_reachable(self, js):
        assert "setAttribute('tabindex', '0')" in js

    def test_enter_and_space_open_the_editor(self, js):
        assert "event.key !== 'Enter' && event.key !== ' '" in js

    def test_escape_cancels(self, js):
        assert "event.key === 'Escape'" in js

    def test_a_textarea_keeps_enter_for_newlines(self, js):
        """Test steps are multi-line, so Enter belongs to the text and
        Ctrl/Cmd+Enter is the save."""
        assert "event.ctrlKey || event.metaKey" in js
        assert "isTextarea" in js

    def test_focus_returns_to_the_field_after_closing(self, js):
        close_body = js.split("function close(host, text) {")[1].split("\n}")[0]
        assert "host.focus()" in close_body

    def test_there_is_a_polite_live_region(self, js):
        assert "aria-live" in js and "'polite'" in js
        assert "role', 'status'" in js.replace('"', "'")

    def test_the_live_region_is_hidden_without_display_none(self):
        """A ``display: none`` live region is not announced at all."""
        css = CSS.read_text(encoding="utf-8")
        block = css.split(".ie-live {")[1].split("}")[0]
        assert "display: none" not in block
        assert "clip:" in block or "clip-path" in block

    def test_a_busy_field_says_so(self, js):
        assert "aria-busy" in js

    def test_a_validation_failure_is_announced_and_marked(self, js):
        assert "aria-invalid" in js
        assert "'alert'" in js

    def test_the_control_is_labelled(self, js):
        assert "'aria-label', 'Edit ' + fieldLabel(host)" in js

    def test_a_closed_field_is_named_by_its_value_not_only_its_field(self, js):
        """The value leads the accessible name.

        aria-label replaces the element's text for assistive tech. The
        first version named every field 'Edit <field>. Press Enter to
        edit.', so reading the harness page's accessibility tree gave six
        interchangeable buttons and no way to hear which test case a row
        was, or what the cell said. The value comes first now.
        """
        assert "function accessibleName(host, value)" in js
        # The value, then the field, then the instruction — in that order.
        assert "return spoken + '. ' + label + '. Press Enter to edit.'" in js
        # And the old value-less form is gone.
        assert "'Edit ' + fieldLabel(host) + '. Press Enter to edit.'" not in js

    def test_an_empty_field_says_so(self, js):
        """'Empty priority' beats a button whose name is the em dash."""
        assert "'Empty ' + label" in js

    def test_a_long_value_is_clipped_in_the_name(self, js):
        """A name is for identifying the row, not for reading the field."""
        assert "NAME_CLIP" in js
        assert "spoken.slice(0, NAME_CLIP)" in js

    def test_the_name_is_refreshed_when_the_value_changes(self, js):
        """A stale name is worse than none: it describes the old text.

        ``close()`` runs on save, on Escape and on blur, and by then
        data-ie-original already holds the value being shown — so the
        refresh belongs there rather than in each caller.
        """
        assert "function refreshName(host)" in js
        close_body = js.split("function close(host, text) {", 1)[1].split(
            "\n    }", 1)[0]
        assert "refreshName(host)" in close_body

    def test_escape_does_not_save_through_the_blur_handler(self, js):
        """Escape discards. It used to save.

        ``close()`` removes the focused control, and removing a focused
        element fires blur — whose handler saves. So Escape put the
        abandoned text on the server and announced "saved". Verified in the
        browser: the steps cell came back holding the discarded line.

        The fix is a mark set *before* the removal, because that blur is
        synchronous in some browsers.
        """
        close_body = js.split("function close(host, text) {", 1)[1].split(
            "\n    }", 1)[0]
        assert "control.dataset.ieClosing = '1'" in close_body
        # Order matters: marked, then removed.
        assert (close_body.index("ieClosing")
                < close_body.index("control.remove()"))

        blur = js.split("control.addEventListener('blur'", 1)[1].split(
            "});", 1)[0]
        assert "if (control.dataset.ieClosing) return;" in blur
        # And the guard is the first thing the handler does, so nothing can
        # slip in front of it.
        assert blur.index("ieClosing") < blur.index("control.disabled")

    def test_a_finished_save_cannot_be_repeated_by_the_closing_blur(self, js):
        """The same mark covers the success path.

        A successful save calls close(), which fires the same blur. It
        happened not to re-save only because the control was still
        disabled from the request — an accident, not a guarantee.
        """
        assert "control.dataset.ieClosing" in js
        ok_path = js.split("if (resp.ok) {", 1)[1].split("return;", 1)[0]
        assert "close(host, saved)" in ok_path

    def test_a_blur_after_a_refused_save_does_not_retry(self, js):
        """Clicking away from an error is not a second attempt.

        Every failure branch re-enables the control so the user can fix
        their text, which silently voided the blur handler's ``disabled``
        guard — the one whose comment claims to stop the Reload button from
        re-submitting. Measured in the browser: blurring a conflicted field
        sent a second PATCH.

        Harmless while the conflict stands. Not harmless if the other
        change was undone in between: then the retry succeeds and writes
        something the user never confirmed.
        """
        for branch in ("if (resp.status === 409) {", ):
            assert branch in js
        # Set once for the whole shared tail of the failure paths, and on
        # the network-error path which returns before reaching it.
        assert js.count("control.dataset.ieFailed = '1'") == 2

        blur = js.split("control.addEventListener('blur'", 1)[1].split(
            "});", 1)[0]
        assert "if (control.dataset.ieFailed) return;" in blur

    def test_typing_after_a_refusal_clears_the_no_retry_mark(self, js):
        """Otherwise the field would never save again without a reload."""
        assert "control.addEventListener('input'" in js
        listener = js.split("control.addEventListener('input'", 1)[1].split(
            "});", 1)[0]
        assert "delete control.dataset.ieFailed" in listener

    def test_opening_a_textarea_puts_the_caret_at_the_end(self, js):
        """Not select-all: that turns one keystroke into "delete all steps".

        Single-line fields keep select-all — a summary is usually replaced,
        and a list of steps is usually appended to.
        """
        open_body = js.split("function open(host) {", 1)[1].split(
            "\n    }", 1)[0]
        assert "control.setSelectionRange(value.length, value.length)" \
            in open_body
        assert "TEXTAREA" in open_body
        # The old unconditional form is gone.
        assert "if (control.select) control.select();" not in js

    def test_a_readonly_field_is_not_named_as_a_button(self, js):
        """It is not clickable, so 'Press Enter to edit' would be a lie."""
        refresh = js.split("function refreshName(host) {", 1)[1].split(
            "\n    }", 1)[0]
        assert "data-ie-readonly" in refresh

    def test_a_readonly_field_occupies_no_more_room_than_plain_text(self):
        """With editing off, the page must render exactly as it did before.

        ``.ie`` is inline-block, which drags in a full line box: measured in
        the browser, a read-only field was 22px tall where the bare text it
        replaced was 18px. Six fields per test-case card, so every card grew
        — on a page nobody can even edit.
        """
        css = CSS.read_text(encoding="utf-8")
        block = css.split(".ie[data-ie-readonly] {", 1)[1].split("}", 1)[0]
        assert "display: inline;" in block
        for reset in ("border-bottom: 0", "margin: 0", "padding: 0"):
            assert reset in block, reset

    def test_reduced_motion_is_respected(self):
        css = CSS.read_text(encoding="utf-8")
        assert "prefers-reduced-motion" in css

    def test_focus_is_visible(self):
        css = CSS.read_text(encoding="utf-8")
        assert ".ie:focus-visible" in css


# ── The macro against the substrate ───────────────────────────────

class TestMacroRendersTheContract:
    def _render(self, app, monkeypatch, *, editing=True, **kwargs):
        """Render the macro with the real gate, not a stubbed variable.

        ``editors_enabled`` is a Jinja *global callable* — see the macro
        file for why — so a test that passed it as a bool would both miss
        the real behaviour and raise "bool is not callable".
        """
        if editing:
            monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
            monkeypatch.setenv("EDITORS_ENABLED", "1")
        else:
            monkeypatch.delenv("EDITORS_ENABLED", raising=False)
        source = (
            '{% from "_inline_edit.html" import editable %}'
            '{{ editable(entity, item_id, field, value, version=version,'
            '            kind=kind, choices=choices, readonly=readonly) }}'
        )
        defaults = {"entity": "test_case", "item_id": "TC-001",
                    "field": "summary", "value": "Sign in", "version": 3,
                    "kind": "text", "choices": None, "readonly": False}
        defaults.update(kwargs)
        with app.test_request_context("/"):
            return app.jinja_env.from_string(source).render(**defaults)

    def test_it_emits_the_attributes_the_component_reads(self, client,
                                                        monkeypatch):
        html = self._render(client.application, monkeypatch)
        for attribute in ('data-ie-entity="test_case"',
                          'data-ie-id="TC-001"',
                          'data-ie-field="summary"',
                          'data-ie-version="3"',
                          'data-ie-kind="text"',
                          'data-ie-original="Sign in"'):
            assert attribute in html, attribute

    def test_choices_are_pipe_joined(self, client, monkeypatch):
        html = self._render(client.application, monkeypatch,
                            kind="choice", choices=["Critical", "Major"])
        assert 'data-ie-choices="Critical|Major"' in html

    def test_an_empty_value_renders_a_placeholder_not_nothing(
            self, client, monkeypatch):
        # An empty cell with no affordance is a field a user cannot find.
        html = self._render(client.application, monkeypatch, value="")
        assert "—" in html
        assert 'data-ie-original=""' in html

    def test_readonly_drops_the_affordance(self, client, monkeypatch):
        html = self._render(client.application, monkeypatch,
                            readonly=True)
        assert "data-ie-readonly" in html

    def test_editing_off_renders_read_only_everywhere(self, client,
                                                      monkeypatch):
        """The gate, in the markup as well as the endpoint.

        A field that looks editable and 404s on save is worse than one that
        never offered.
        """
        html = self._render(client.application, monkeypatch,
                            editing=False)
        assert "data-ie-readonly" in html

    def test_the_component_is_not_loaded_when_editing_is_off(self, client,
                                                            monkeypatch):
        source = ('{% from "_inline_edit.html" import editable_assets %}'
                  '{{ editable_assets("n") }}')
        app = client.application
        with app.test_request_context("/"):
            monkeypatch.delenv("EDITORS_ENABLED", raising=False)
            off = app.jinja_env.from_string(source).render()
            monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
            monkeypatch.setenv("EDITORS_ENABLED", "1")
            on = app.jinja_env.from_string(source).render()
        assert "inline-edit.js" not in off
        assert "inline-edit.js" in on
        assert 'nonce="n"' in on


class TestTemplateContextExposesTheGate:
    def test_editors_on_is_injected(self, client, monkeypatch):
        from engine import permissions
        monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
        monkeypatch.setenv("EDITORS_ENABLED", "1")
        with client.application.test_request_context("/"):
            assert permissions.template_context()["editors_on"] is True

    def test_it_honours_the_workspace_dependency(self, client, monkeypatch):
        """ADR 0001's gate reaches the template too.

        ``effective`` rather than ``is_enabled``: editing a Flask session
        edits a private copy of shared data, so a template must not render
        an editor whose endpoint is off.
        """
        from engine import permissions
        monkeypatch.setenv("EDITORS_ENABLED", "1")
        monkeypatch.delenv("WORKSPACE_DB_FIRST", raising=False)
        with client.application.test_request_context("/"):
            assert permissions.template_context()["editors_on"] is False


class TestFieldNamesMatchTheSubstrate:
    """The macro is called with field names; the server allowlists them.

    A template asking to edit a field the substrate does not accept
    produces a 400 the user cannot act on, so the names both sides use are
    checked against each other here rather than at a customer's desk.
    """

    def test_the_entities_a_page_will_reference_are_registered(self):
        for entity in ("test_case", "checklist_item", "bug"):
            assert editable.entity(entity)

    @pytest.mark.parametrize("entity,field", [
        ("test_case", "summary"),
        ("test_case", "test_steps"),
        ("test_case", "expected_result"),
        ("checklist_item", "objective"),
        ("bug", "title"),
        ("bug", "severity"),
        ("bug", "steps_to_reproduce"),
    ])
    def test_the_fields_the_editors_will_expose_are_editable(self, entity,
                                                             field):
        assert field in editable.editable_fields(entity)

    def test_a_choice_field_can_supply_its_own_vocabulary(self):
        # So the macro's data-ie-choices comes from the same declaration the
        # server validates against, rather than a second hand-kept list.
        spec = editable.entity("bug").fields["severity"]
        assert spec.kind == "choice"
        assert spec.choices
