"""E0.11 + E0.12 — what the free plan has left, and how often it sleeps.

Two ceilings decide whether this deployment keeps working, and before this
neither was visible from inside the product:

* **0.5 GB of database**, shared by every organisation (E0.12);
* **cold starts** — the free web service sleeps after ~15 idle minutes and
  the next visitor waits 30–60 s. Measured on production 2026-08-06:
  ``curl /readyz`` answered 200 after **45 seconds** (E0.11).

What this file is careful about
-------------------------------
**The per-org figure is an estimate and must say so.** Postgres can size a
table and cannot size one organisation's share of it, because nothing here
is partitioned by org. So the bytes are row counts times the per-row sizes
``cost_model.md`` §3 measured, and ``TestTheEstimateIsLabelled`` pins that
the page calls it an estimate. A number presented as exact, that is not, is
worse than a range.

**It must not read as "migrate".** The owner decided on 2026-08-06 that the
database stays on Render free — the deployment is a demo and a monthly
reset is cheaper than the move. A quota surface that argues for a bigger
plan would be re-opening a closed decision, so
``TestItDoesNotArgueForABiggerPlan`` checks the wording.

**Counting must not leak between organisations.** A quota that includes
somebody else's rows is worse than no quota: it tells a team to delete work
that is not the problem. ``TestOrgsAreCountedSeparately`` is that.
"""
from __future__ import annotations

import re
import secrets

import pytest

from app import app as flask_app
from engine import capacity as _capacity
from engine import db as _db
from engine import permissions as _perm


@pytest.fixture(autouse=True)
def _ready():
    _db.init_db()


def _org_with(*, cases: int = 0, checks: int = 0, bugs: int = 0) -> str:
    """An organisation owning one project with a known artefact count."""
    tag = secrets.token_hex(4)
    org = _db.create_organization(f"Cap {tag}")
    pid = _db.upsert_project(name=f"Cap {tag}", org_id=org)
    if cases:
        _db.save_test_cases(pid, [
            {"id": f"TC-{n:03d}", "section": "S", "section_num": 1,
             "summary": f"c{n}", "preconditions": "", "test_steps": "",
             "test_data": "", "expected_result": "ok", "issues": "",
             "comment": "", "user_story_id": "", "category": "Functional",
             "priority": "High", "status": "", "testing_type": "Functional"}
            for n in range(1, cases + 1)])
    if checks:
        _db.save_checklist(pid, [
            {"id": f"CL-{n:03d}", "section": "S", "section_num": 1,
             "objective": f"o{n}", "priority": "High",
             "category": "Functional", "comment": "", "expected_result": "",
             "user_story_id": "", "testing_type": "Functional"}
            for n in range(1, checks + 1)])
    for n in range(1, bugs + 1):
        _db.save_bug(pid, {"id": f"BUG-{n:03d}", "title": f"b{n}"},
                     source="manual")
    return org


# ── The database as a whole ──────────────────────────────────────────

class TestDatabaseUsage:
    def test_it_reads_a_size_on_this_engine(self):
        """SQLite here, Postgres in CI and production — both have to
        answer, because a figure that only works on one of them is a
        figure the operator cannot check where it matters."""
        usage = _capacity.database_usage()
        assert usage.engine in ("sqlite", "postgresql")
        assert usage.bytes_used is None or usage.bytes_used > 0

    def test_the_limit_is_the_free_plan_ceiling(self):
        assert _capacity.FREE_DB_LIMIT_BYTES == 512 * 1024 * 1024

    def test_an_unreadable_size_is_unknown_rather_than_zero(self,
                                                            monkeypatch):
        """Zero would render as "0 B of 512 MB used" — a reassuring lie.

        The page shows "unknown" instead, which is what the operator
        actually knows.
        """
        monkeypatch.setattr(_capacity, "_sqlite_bytes", lambda url: None)
        usage = _capacity.database_usage()
        assert usage.bytes_used is None
        assert usage.readable == "unknown"
        assert usage.ratio == 0.0

    def test_a_database_error_does_not_raise(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("no database")

        monkeypatch.setattr(_db, "database_url", _boom)
        assert _capacity.database_usage() is not None

    def test_human_bytes_reads_like_a_size(self):
        assert _capacity.human_bytes(0) == "0 B"
        assert _capacity.human_bytes(512) == "512 B"
        assert _capacity.human_bytes(2048) == "2.0 KB"
        assert _capacity.human_bytes(5 * 1024 * 1024) == "5.0 MB"
        assert "GB" in _capacity.human_bytes(3 * 1024 ** 3)


# ── One organisation's share ─────────────────────────────────────────

class TestOrgUsage:
    def test_it_counts_what_the_team_actually_has(self):
        org = _org_with(cases=7, checks=3, bugs=2)
        usage = _capacity.org_usage(org)
        assert usage.counts["test_cases"] == 7
        assert usage.counts["checklist_items"] == 3
        assert usage.counts["bug_reports"] == 2
        assert usage.rows == 12
        assert usage.projects == 1

    def test_the_estimate_uses_the_measured_row_sizes(self):
        """From cost_model.md §3, not from column types — the whole reason
        an estimate is defensible at all."""
        org = _org_with(cases=10)
        usage = _capacity.org_usage(org)
        assert usage.estimated_bytes == 10 * _capacity.ROW_BYTES["test_cases"]

    def test_it_names_the_biggest_thing_by_bytes_not_by_count(self):
        """200 execution results are more rows than 100 bug reports and a
        great deal less database. Pointing at the wrong one sends somebody
        to delete the thing that was not the problem."""
        org = _org_with(checks=40, bugs=5)
        usage = _capacity.org_usage(org)
        # 40 checklist items ≈ 16 KB; 5 bugs ≈ 10 KB — checklist wins.
        assert usage.biggest == "checklist_items"
        org2 = _org_with(checks=10, bugs=5)
        # 10 × 400 = 4 KB vs 5 × 2000 = 10 KB — now bugs win, on fewer rows.
        assert _capacity.org_usage(org2).biggest == "bug_reports"

    def test_an_empty_org_is_zero_not_none(self):
        org = _db.create_organization(f"Empty {secrets.token_hex(4)}")
        usage = _capacity.org_usage(org)
        assert usage is not None
        assert usage.rows == 0 and usage.projects == 0
        assert usage.biggest == "" or usage.counts[usage.biggest] == 0

    def test_no_org_id_reads_as_nothing(self):
        assert _capacity.org_usage("") is None

    def test_a_failure_declines_rather_than_raising(self, monkeypatch):
        def _boom(*a, **k):
            raise RuntimeError("gone")

        monkeypatch.setattr(_db, "session_scope", _boom)
        assert _capacity.org_usage("some-org") is None


class TestOrgsAreCountedSeparately:
    """A quota including somebody else's rows tells a team to delete work
    that is not the problem."""

    def test_one_orgs_artefacts_do_not_appear_in_anothers_count(self):
        mine = _org_with(cases=5)
        _org_with(cases=50)          # a busy neighbour

        usage = _capacity.org_usage(mine)

        assert usage.counts["test_cases"] == 5, (
            "another organisation's test cases were counted against this "
            "one")

    def test_a_project_with_no_org_is_nobody_s_quota(self):
        """Pre-ORG_MODE projects have ``org_id = NULL``. They are real rows
        in the database — visible in the instance-wide figure — and they
        belong to no team's allowance until somebody claims them (E1.6)."""
        org = _org_with(cases=3)
        orphan = _db.upsert_project(name=f"Orphan {secrets.token_hex(4)}")
        _db.save_bug(orphan, {"id": "BUG-001", "title": "unowned"},
                     source="manual")

        assert _capacity.org_usage(org).counts["bug_reports"] == 0

    def test_audit_rows_are_scoped_to_the_org_too(self):
        org = _org_with()
        other = _org_with()
        _db.append_audit(entity="test", action="x", org_id=org)
        _db.append_audit(entity="test", action="x", org_id=other)
        _db.append_audit(entity="test", action="x", org_id=other)

        assert _capacity.org_usage(org).counts["audit_rows"] == 1
        assert _capacity.org_usage(other).counts["audit_rows"] == 2


class TestTheQuota:
    def test_the_default_is_a_row_budget(self):
        assert _capacity.org_quota_rows() == 50_000

    def test_an_operator_can_change_it(self, monkeypatch):
        monkeypatch.setenv("ORG_QUOTA_ROWS", "100")
        assert _capacity.org_quota_rows() == 100

    def test_nonsense_falls_back_rather_than_locking_the_page(self,
                                                              monkeypatch):
        for bad in ("lots", "0", "-5", ""):
            monkeypatch.setenv("ORG_QUOTA_ROWS", bad)
            assert _capacity.org_quota_rows() == 50_000, bad

    def test_warning_arrives_before_the_limit_not_at_it(self, monkeypatch):
        """A quota that only speaks up once it is breached is a quota
        nobody can act on."""
        monkeypatch.setenv("ORG_QUOTA_ROWS", "10")
        org = _org_with(cases=8)
        usage = _capacity.org_usage(org)
        assert usage.over_warn is True
        assert usage.over_quota is False

    def test_over_the_limit_is_reported_as_over(self, monkeypatch):
        monkeypatch.setenv("ORG_QUOTA_ROWS", "5")
        usage = _capacity.org_usage(_org_with(cases=6))
        assert usage.over_quota is True

    def test_nothing_is_blocked_by_being_over(self, monkeypatch):
        """Deliberate: this is a guideline surface, not an enforcement one.

        Refusing writes on a demo deployment would turn a full database
        into a broken product, and the owner accepted the free plan's
        ceiling knowingly. Saying so is the job; stopping people is not.
        """
        monkeypatch.setenv("ORG_QUOTA_ROWS", "1")
        org = _org_with(cases=5)
        pid = [p["id"] for p in _db.list_projects(org_id=org)][0]

        _db.save_bug(pid, {"id": "BUG-999", "title": "still accepted"},
                     source="manual")

        assert _capacity.org_usage(org).counts["bug_reports"] == 1


# ── Cold starts ──────────────────────────────────────────────────────

class TestColdStarts:
    def test_a_boot_is_recorded(self):
        before = _capacity.cold_starts(24) or 0
        _capacity.record_boot()
        assert (_capacity.cold_starts(24) or 0) == before + 1

    def test_recording_never_raises(self, monkeypatch):
        monkeypatch.setattr(_db, "append_audit",
                            lambda **k: (_ for _ in ()).throw(
                                RuntimeError("no db")))
        _capacity.record_boot()      # must not raise

    def test_the_count_declines_rather_than_lying(self, monkeypatch):
        monkeypatch.setattr(_db, "count_audit_since",
                            lambda **k: (_ for _ in ()).throw(
                                RuntimeError("no db")))
        assert _capacity.cold_starts() is None

    def test_availability_reports_the_measured_rate(self):
        for _ in range(6):
            _capacity.record_boot()
        state = _capacity.availability()
        assert state["cold_starts_24h"] >= 6
        assert state["noticeable"] is True

    def test_it_states_the_sleep_window_and_the_wait(self):
        """Both numbers are what a person needs to understand the blank tab
        — and the wait is the one that was measured on production, not
        assumed."""
        state = _capacity.availability()
        assert state["sleeps_after_minutes"] == 15
        assert "30" in state["cold_start_seconds"]


# ── What the page says ───────────────────────────────────────────────

class TestTheSettingsPage:
    @pytest.fixture
    def viewer(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "1")
        monkeypatch.setenv("ORG_MODE", "1")
        monkeypatch.setitem(flask_app.config, "TESTING", True)
        monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", False)

        def _open(org_id: str):
            uid = _db.create_user(f"cap-{secrets.token_hex(5)}@x.test",
                                  email_verified=True)
            _db.add_org_member(org_id, uid, "admin")
            client = flask_app.test_client()
            with client.session_transaction() as sess:
                sess[_perm.SESSION_USER_KEY] = uid
                sess[_perm.SESSION_ORG_KEY] = org_id
            return client.get("/org/settings").get_data(as_text=True)

        return _open

    def test_it_shows_the_teams_rows_and_the_server_ceiling(self, viewer):
        org = _org_with(cases=4, bugs=2)
        body = viewer(org)
        assert "Capacity" in body
        assert "512.0 MB" in body, "the free-plan ceiling is not on the page"

    def test_it_shows_how_often_the_service_restarted(self, viewer):
        _capacity.record_boot()
        body = viewer(_org_with())
        assert "Waking up" in body
        assert "15 minutes" in body and "30" in body

    def test_it_points_at_the_keepalive_workflow(self, viewer):
        """The fix for the wait is not in the product, so the page has to
        say where it is rather than leaving the reader stuck."""
        body = viewer(_org_with())
        assert "KEEPALIVE_URL" in body
        assert "keepalive.yml" in body


class TestTheEstimateIsLabelled:
    def test_the_page_calls_the_per_org_bytes_an_estimate(self, monkeypatch):
        """Postgres cannot size one org's share of a shared table. Saying
        the number is exact would be inventing precision."""
        monkeypatch.setenv("AUTH_ENABLED", "1")
        monkeypatch.setenv("ORG_MODE", "1")
        monkeypatch.setitem(flask_app.config, "TESTING", True)
        org = _org_with(cases=3)
        uid = _db.create_user(f"est-{secrets.token_hex(5)}@x.test",
                              email_verified=True)
        _db.add_org_member(org, uid, "admin")
        client = flask_app.test_client()
        with client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = uid
            sess[_perm.SESSION_ORG_KEY] = org

        body = client.get("/org/settings").get_data(as_text=True)

        assert "estimate" in body.lower()
        assert "about" in body.lower()


class TestItDoesNotArgueForABiggerPlan:
    """The owner closed the migration question on 2026-08-06: the database
    stays on Render free, and the monthly reset is an accepted risk. A
    quota surface that lobbies for a bigger plan re-opens a closed
    decision, so the wording is checked rather than trusted."""

    #: Read from the dictionaries rather than a rendered page, so the check
    #: covers every branch — including the "getting full" copy that only
    #: appears on an instance nobody wants to reproduce in a test.
    #:
    #: It used to read ``templates/org_settings.html`` and slice between
    #: two English ``<h2>`` literals. M-2 moved this card's words into
    #: ``engine/i18n``, which broke the slice — and made the check better:
    #: the rule is about **what the product says**, so it now applies to
    #: every language rather than to whichever one happened to be in the
    #: markup. A Ukrainian string offering a paid plan would have sailed
    #: past the old version.
    FORBIDDEN_EN = ("upgrade", "migrate", "paid plan", "bigger plan",
                    "move to neon", "upgrading")
    FORBIDDEN_UA = ("оновіть план", "платний план", "більший план",
                    "мігруйте", "міграція на", "перейдіть на neon")
    FORBIDDEN = FORBIDDEN_EN

    #: Every key the capacity card renders.
    PREFIXES = ("os_capacity_", "os_waking_")

    @classmethod
    def _card_strings(cls, lang: str) -> str:
        from engine.i18n import get_lang
        table = get_lang(lang)
        return " ".join(str(v) for k, v in table.items()
                        if k.startswith(cls.PREFIXES)).lower()

    @classmethod
    def _visible_card(cls) -> str:
        return cls._card_strings("en")

    def test_the_capacity_card_suggests_tidying_not_upgrading(self):
        for lang, forbidden in (("en", self.FORBIDDEN_EN),
                                ("ua", self.FORBIDDEN_UA)):
            card = self._card_strings(lang)
            for word in forbidden:
                assert word not in card, (
                    f"the {lang} capacity card says {word!r} — the plan "
                    f"decision was made on 2026-08-06 and this surface must "
                    f"not re-open it")

    def test_it_says_what_to_delete_instead(self):
        assert "delet" in self._card_strings("en"), (
            "a full-database warning with no suggested action is an alarm "
            "without an answer")
        assert "видал" in self._card_strings("ua"), (
            "the Ukrainian card warns without naming the way out")

    def test_the_stripping_leaves_the_card_itself(self):
        """Guards the opposite failure: a regex so eager that the card
        matches because nothing is left of it."""
        card = self._card_strings("en")
        assert "row counts" in card and len(card) > 500


# ── The keep-alive workflow ──────────────────────────────────────────

class TestTheKeepaliveWorkflow:
    """The schedule is the decision, so it is checked rather than assumed.

    The trap the prompt names: a keep-alive that pings around the clock
    burns ~730 of the free tier's 750 monthly instance-hours and gets the
    service suspended — the opposite of keeping it available.
    """

    @staticmethod
    def _workflow() -> str:
        import pathlib
        return pathlib.Path(
            ".github/workflows/keepalive.yml").read_text(encoding="utf-8")

    def test_it_exists_and_is_scheduled(self):
        text = self._workflow()
        assert "schedule:" in text and "cron:" in text

    def test_the_schedule_is_not_around_the_clock(self):
        """The measurable form of the prompt's warning."""
        text = self._workflow()
        crons = re.findall(r'cron:\s*"([^"]+)"', text)
        assert crons, "no cron expression found"
        for expression in crons:
            fields = expression.split()
            assert len(fields) == 5, expression
            hour, _dom, _mon, dow = fields[1], fields[2], fields[3], fields[4]
            assert hour != "*", (
                f"{expression!r} runs every hour of the day; at ~730 "
                f"instance-hours a month that exceeds the free tier's 750 "
                f"and suspends the service")
            assert dow != "*", (
                f"{expression!r} runs every day including weekends, which "
                f"is a third of the allowance spent on nobody")

    def test_the_window_fits_inside_the_free_allowance(self):
        """The arithmetic in the workflow's own comment, asserted."""
        text = self._workflow()
        expression = re.findall(r'cron:\s*"([^"]+)"', text)[0]
        hour_field = expression.split()[1]
        start, end = (int(x) for x in hour_field.split("-"))
        hours_per_day = end - start + 1
        weekdays_per_month = 22
        assert hours_per_day * weekdays_per_month < 750, (
            f"{hours_per_day} h × {weekdays_per_month} weekdays exceeds the "
            f"free tier's 750 monthly instance-hours")

    def test_it_no_ops_without_a_url_rather_than_failing(self):
        """A fork, or a checkout with no deployment, must not show a red
        workflow for a service it was never meant to ping."""
        text = self._workflow()
        assert "KEEPALIVE_URL" in text
        assert "exit 0" in text

    def test_a_bad_response_is_loud(self):
        """A keep-alive quietly pinging a 404 for a month is a keep-alive
        nobody has."""
        text = self._workflow()
        assert "::error::" in text
        assert "exit 1" in text


# ── Which database is in force ────────────────────────────────────────

class TestTheCardNamesTheDatabaseEngine:
    """`engine.capacity` has computed the engine name from the first
    version of this module and no screen has ever shown it.

    It started mattering when staging got a `DATABASE_URL` managed in the
    Render dashboard rather than linked to a database service. A
    `sync: false` key exists with **no value** until somebody pastes one,
    an empty value is falsy, and the app falls back to SQLite — quietly,
    successfully, and on a disk that a redeploy replaces. So "the
    blueprint declares DATABASE_URL" and "this instance is on Postgres"
    became two different claims, and nothing on any page could separate
    them. That is the shape of defect this repo has hit before: a
    declaration standing in for a fact.
    """

    @pytest.fixture
    def viewer(self, monkeypatch):
        monkeypatch.setenv("AUTH_ENABLED", "1")
        monkeypatch.setenv("ORG_MODE", "1")
        monkeypatch.setitem(flask_app.config, "TESTING", True)
        monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", False)

        def _open(usage: _capacity.DatabaseUsage, lang: str = "en"):
            monkeypatch.setattr(_capacity, "database_usage", lambda: usage)
            org = _org_with()
            uid = _db.create_user(f"eng-{secrets.token_hex(5)}@x.test",
                                  email_verified=True)
            _db.add_org_member(org, uid, "admin")
            client = flask_app.test_client()
            with client.session_transaction() as sess:
                sess[_perm.SESSION_USER_KEY] = uid
                sess[_perm.SESSION_ORG_KEY] = org
            return client.get(f"/org/settings?lang={lang}").get_data(as_text=True)

        return _open

    def test_it_says_postgres_when_postgres_is_in_force(self, viewer):
        body = viewer(_capacity.DatabaseUsage(bytes_used=5_000_000,
                                              engine="postgresql"))
        assert "Database engine: postgresql" in body

    def test_it_says_sqlite_when_the_url_never_arrived(self, viewer):
        body = viewer(_capacity.DatabaseUsage(bytes_used=400_000,
                                              engine="sqlite"))
        assert "Database engine: sqlite" in body

    def test_sqlite_carries_the_consequence_and_postgres_does_not(self, viewer):
        """Naming the engine is only half the answer: a reader who does not
        already know that this plan has no persistent disk learns nothing
        from the word "sqlite"."""
        sqlite = viewer(_capacity.DatabaseUsage(bytes_used=400_000,
                                                engine="sqlite"))
        assert "redeploy replaces it" in sqlite
        assert "DATABASE_URL" in sqlite

        postgres = viewer(_capacity.DatabaseUsage(bytes_used=400_000,
                                                  engine="postgresql"))
        assert "redeploy replaces it" not in postgres, (
            "the ephemeral-disk warning is about SQLite; on Postgres it is "
            "false and would send an admin looking for a problem that is "
            "not there")

    def test_it_survives_the_case_it_was_written_for(self, viewer):
        """The size is the thing most likely to be unreadable — and that is
        exactly when an admin wants to know which database they are on. The
        first draft put this line inside the `bytes_used` branch, where it
        would have been hidden in that very case."""
        body = viewer(_capacity.DatabaseUsage(bytes_used=None,
                                              engine="postgresql"))
        assert "Database engine: postgresql" in body

    def test_it_says_nothing_when_the_engine_is_unknown(self, viewer):
        """Better a missing line than "Database engine: ." """
        body = viewer(_capacity.DatabaseUsage(bytes_used=None, engine=""))
        assert "Database engine" not in body

    def test_the_ukrainian_page_says_it_too(self, viewer):
        body = viewer(_capacity.DatabaseUsage(bytes_used=400_000,
                                              engine="sqlite"), lang="ua")
        assert "Рушій бази: sqlite" in body
        assert "редеплой замінює його" in body
