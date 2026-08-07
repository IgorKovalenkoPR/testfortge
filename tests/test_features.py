"""Feature-flag registry — engine/features.py (E0.3).

The registry exists to make two silent failures loud: a misspelled flag
name, and a flag whose value the code does not recognise. Both previously
read as "off", indistinguishable from a correctly-disabled feature, so
these tests are the whole justification for the module.
"""

import pytest

from engine import features


class TestDeclaredFlags:
    def test_undeclared_flag_raises_rather_than_reading_as_off(self):
        # The bug this prevents: EDITOR_ENABLED (singular) silently
        # returning False forever while the operator swears the flag is
        # set in the dashboard.
        with pytest.raises(features.UnknownFlag):
            features.is_enabled("EDITOR_ENABLED")

    #: Flags that default **on**, each with the reason it has to.
    #:
    #: The rule below is about *new* behaviour: shipping a feature on by
    #: default is how something half-built reaches users. A flag that
    #: retires something already live runs the opposite risk — defaulting it
    #: off means the next deploy removes a control nobody asked to remove.
    #:
    #: Named individually so a second one cannot arrive by accident: adding
    #: to this dict is a deliberate edit with a sentence attached.
    DEFAULTS_ON = {
        "BASIC_GATE_ENABLED":
            "retires the shared HTTP Basic password, which is a live "
            "perimeter (E1.8). Defaulting it off would drop that perimeter "
            "on the next deploy of any instance that never set it.",
    }

    def test_every_flag_defaults_off_unless_it_retires_something_live(self):
        # Nothing *new* in this programme ships on by default; each epic
        # flips its own flag when its acceptance criteria are met.
        for name, flag in features.FLAGS.items():
            if name in self.DEFAULTS_ON:
                assert flag.default is True, (
                    f"{name} is listed as defaulting on but does not")
                continue
            assert flag.default is False, (
                f"{name} defaults on. If that is deliberate, add it to "
                f"DEFAULTS_ON with the reason — a feature that ships on "
                f"before its epic is finished is what this test is for.")

    def test_the_on_by_default_list_has_no_stale_entries(self):
        # An entry for a flag that no longer exists is an exemption nobody
        # can see the shape of.
        stale = sorted(set(self.DEFAULTS_ON) - set(features.FLAGS))
        assert not stale, f"DEFAULTS_ON names flags that are gone: {stale}"

    def test_every_flag_names_its_epic_and_purpose(self):
        # A flag nobody can date is a flag nobody deletes.
        for name, flag in features.FLAGS.items():
            assert flag.epic, f"{name} has no epic"
            assert len(flag.purpose) > 40, f"{name} has a token purpose"


class TestValueParsing:
    @pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " On "])
    def test_truthy_spellings_all_mean_on(self, monkeypatch, raw):
        monkeypatch.setenv("AUTH_ENABLED", raw)
        assert features.is_enabled("AUTH_ENABLED") is True

    @pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
    def test_falsy_spellings_all_mean_off(self, monkeypatch, raw):
        monkeypatch.setenv("AUTH_ENABLED", raw)
        assert features.is_enabled("AUTH_ENABLED") is False

    def test_unparseable_value_falls_back_to_the_default(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "enabled")
        assert features.is_enabled("AUTH_ENABLED") is False

    def test_env_is_read_at_call_time_not_import_time(self, monkeypatch):
        # Flipping a flag in the Render dashboard has to take effect on
        # the next request, not the next deploy.
        #
        # The starting state is asserted from a *deleted* variable rather than
        # from "nobody sets this one": this test used to rely on DASHBOARD_V2
        # being unset anywhere, and broke the moment E7 shipped and the suite
        # began running with it on. What is under test is call-time reading,
        # not which flag happens to be unfashionable.
        monkeypatch.delenv("DASHBOARD_V2", raising=False)
        assert features.is_enabled("DASHBOARD_V2") is False
        monkeypatch.setenv("DASHBOARD_V2", "1")
        assert features.is_enabled("DASHBOARD_V2") is True


class TestDependencies:
    def test_org_mode_without_auth_reads_as_off(self, monkeypatch):
        # There is nothing to attach a role to without a user, so ORG_MODE
        # alone must not let a route believe it has one.
        monkeypatch.setenv("ORG_MODE", "1")
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        assert features.is_enabled("ORG_MODE") is True      # raw env
        assert features.effective("ORG_MODE") is False      # honours deps

    def test_org_mode_with_auth_is_effective(self, monkeypatch):
        monkeypatch.setenv("ORG_MODE", "1")
        monkeypatch.setenv("AUTH_ENABLED", "1")
        assert features.effective("ORG_MODE") is True

    def test_misconfiguration_is_reported(self, monkeypatch):
        monkeypatch.setenv("ORG_MODE", "1")
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        warnings = features.misconfigurations()
        assert any("ORG_MODE" in w and "AUTH_ENABLED" in w for w in warnings)

    def test_no_misconfiguration_when_nothing_is_on(self, monkeypatch):
        for name in features.FLAGS:
            monkeypatch.delenv(name, raising=False)
        assert features.misconfigurations() == []

    def test_editors_do_nothing_without_the_db_backed_workspace(self, monkeypatch):
        # ADR 0001's gate, expressed where it cannot be forgotten: an
        # editor writing into a Flask session edits a private copy of
        # shared team data, so it would have to be written twice.
        monkeypatch.setenv("EDITORS_ENABLED", "1")
        monkeypatch.delenv("WORKSPACE_DB_FIRST", raising=False)
        assert features.effective("EDITORS_ENABLED") is False
        monkeypatch.setenv("WORKSPACE_DB_FIRST", "1")
        assert features.effective("EDITORS_ENABLED") is True

    def test_dashboard_v2_has_the_same_prerequisite(self, monkeypatch):
        # Per-project metrics computed from the caller's session are
        # per-browser metrics wearing a project's name.
        monkeypatch.setenv("DASHBOARD_V2", "1")
        monkeypatch.delenv("WORKSPACE_DB_FIRST", raising=False)
        assert features.effective("DASHBOARD_V2") is False


class TestSnapshot:
    def test_snapshot_covers_every_flag_and_is_sorted(self, monkeypatch):
        for name in features.FLAGS:
            monkeypatch.delenv(name, raising=False)
        snap = features.snapshot()
        assert set(snap) == set(features.FLAGS)
        assert list(snap) == sorted(snap)

    def test_snapshot_reports_effective_not_raw_values(self, monkeypatch):
        monkeypatch.setenv("ORG_MODE", "1")
        monkeypatch.delenv("AUTH_ENABLED", raising=False)
        # An ops surface that showed ORG_MODE: true here would be lying
        # about what the app is doing.
        assert features.snapshot()["ORG_MODE"] is False
