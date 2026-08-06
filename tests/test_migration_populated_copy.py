"""E9.3 — the upgrade a deployment actually performs: on a database with data in it.

``tests/test_schema_migration.py`` covers the clean half of E9.3's
acceptance criterion — build the schema, drop a column, put it back. This
file covers the other half, "a populated production copy", and the
difference is not decoration. Three of the programme's migrations only do
anything *because* rows are already there:

* ``_ensure_editable_columns`` back-fills ``row_version`` and
  ``ai_generated`` for every existing artefact, and the values it chooses
  are a claim about that data ("nothing has been edited yet");
* ``_renumber_duplicate_public_ids`` exists solely for rows written before
  ``save_*`` enforced uniqueness, and on an empty table it is a no-op;
* ``_ensure_public_id_unique_indexes`` **cannot be created** over colliding
  rows, so on a fresh database it always succeeds and on a real copy it is
  the one that can fail.

So the shape here is: build a database, fill it through the product's own
repository functions, take the schema back to what it looked like before
this programme, boot the app again, and ask what a person would ask — is
my work still there, and can I still read it?

Both engines, for the reason stated in the sibling file: production is
Postgres, the ALTERs are hand-written SQL, and a migration verified only
on SQLite is verified on the wrong engine. The Postgres leg runs when
``TFG_TEST_POSTGRES_URL`` points at a throwaway database, which CI
supplies from a service container.
"""
from __future__ import annotations

import os
import sqlite3

import pytest
from sqlalchemy import inspect, text

from engine import db as _db


#: Point at a THROWAWAY PostgreSQL database. Everything below drops
#: columns and tables, so the URL must never name anything that matters.
POSTGRES_URL = os.environ.get("TFG_TEST_POSTGRES_URL", "").strip()

#: SQLite gained ``DROP COLUMN`` in 3.35; without it the schema cannot be
#: taken backwards and there is no upgrade to simulate.
_CAN_DROP_COLUMN = sqlite3.sqlite_version_info >= (3, 35)


def _params() -> list:
    return [
        pytest.param("sqlite", id="sqlite"),
        pytest.param(
            "postgres", id="postgres",
            marks=pytest.mark.skipif(
                not POSTGRES_URL,
                reason="set TFG_TEST_POSTGRES_URL to a throwaway database")),
    ]


# ── What this programme added on top of a pre-programme database ─────
#
# (table, column, the value an existing row must read back as). ``None``
# means the column is nullable and back-fills to NULL, which is its own
# assertion: ``edited_by`` must NOT be invented for a row nobody edited.

PROGRAMME_COLUMNS: tuple[tuple[str, str, object], ...] = (
    # E2.1 — tenancy
    ("project", "org_id", None),
    # E3.5 — per-pack optimistic concurrency
    ("project", "tc_version", 0),
    ("project", "cl_version", 0),
    # E7.3 — per-project KPI targets
    ("project", "settings", {}),
    # E3.4 — where a case result came from
    ("execution_case_result", "source", ""),
    # E4.1 / E4.6 — the editing metadata, on all four editable entities
    ("test_case", "row_version", 1),
    ("test_case", "ai_generated", True),
    ("test_case", "edited_by", None),
    ("test_case", "edited_at", None),
    ("checklist_item", "row_version", 1),
    ("checklist_item", "ai_generated", True),
    ("checklist_item", "edited_by", None),
    ("checklist_item", "edited_at", None),
    ("bug_report", "row_version", 1),
    ("bug_report", "ai_generated", True),
    ("bug_report", "edited_by", None),
    ("bug_report", "edited_at", None),
    ("estimation", "row_version", 1),
    ("estimation", "ai_generated", True),
    ("estimation", "original_payload", None),
)

#: E1/E2 arrived as whole tables, which ``create_all`` adds without any
#: hand-written SQL. Listed anyway: the assertion worth making is that
#: adding them to a populated database leaves the artefacts alone, and
#: that the app can then create an account and adopt the existing work.
PROGRAMME_TABLES: tuple[str, ...] = (
    "app_user", "identity", "organization", "org_member", "invite",
    "org_secret", "llm_usage", "audit_log", "user_setting",
    "server_session",
)

PROGRAMME_INDEXES: tuple[str, ...] = (
    "ux_test_case_project_external_id",
    "ux_checklist_item_project_external_id",
)


# ── Building, and un-building, a production copy ─────────────────────

def _populate() -> dict:
    """Fill the database through the product's own writers.

    Through the repository functions rather than raw INSERTs on purpose:
    a fixture that hand-writes rows encodes today's column list, and the
    next column added to a model would leave it building rows production
    never contains. This way the fixture keeps describing real data for
    free.
    """
    alpha = _db.upsert_project("Prod Copy Alpha", base_url="https://alpha.test")
    beta = _db.upsert_project("Prod Copy Beta")

    _db.save_test_cases(alpha, [
        {"id": "SC1_001", "section": "Checkout", "section_num": 1,
         "summary": "Verify that a card payment is accepted",
         "preconditions": "A registered account",
         "test_steps": "1. Open checkout\n2. Pay by card",
         "test_data": "4111 1111 1111 1111",
         "expected_result": "The order is confirmed"},
        {"id": "SC1_002", "section": "Checkout", "section_num": 1,
         "summary": "Verify that an expired card is refused",
         "preconditions": "", "test_steps": "1. Pay with an expired card",
         "test_data": "", "expected_result": "The payment is refused"},
    ])
    _db.save_checklist(alpha, [
        {"id": "HDR_001", "section": "Header",
         "objective": "Verify that the logo links to the home page"},
        {"id": "HDR_002", "section": "Header",
         "objective": "Verify that the basket count matches the basket"},
    ])
    bug_id = _db.save_bug(alpha, {
        "id": "BUG_001", "title": "The basket count is stale after removal",
        "severity": "Major", "priority": "High", "status": "Open",
        "steps_to_reproduce": "1. Add two items\n2. Remove one",
        "actual_result": "The header still reads 2",
        "expected_result": "The header reads 1",
    })
    est_id = _db.save_estimation(
        alpha, {"pages": 12, "team_size": 3}, {"total_hours": 148.0},
        total_hours=148.0)

    run_id = _db.start_execution_run(alpha, {"browser": "chromium"},
                                     base_url="https://alpha.test")
    _db.save_case_result(run_id, case_external_id="SC1_001",
                         case_kind="test_case", status="Passed")
    _db.save_case_result(run_id, case_external_id="SC1_002",
                         case_kind="test_case", status="Failed",
                         bug_report_id=bug_id)
    _db.finish_execution_run(run_id, "completed",
                             {"total": 2, "passed": 1, "failed": 1})

    # Beta stays deliberately thin — an untouched project must survive the
    # upgrade as visibly as a busy one, and a second project is what makes
    # "the rows went to the right project" assertable at all.
    _db.save_test_cases(beta, [
        {"id": "SC1_001", "section": "Login", "section_num": 1,
         "summary": "Verify that a wrong password is refused",
         "preconditions": "", "test_steps": "1. Sign in badly",
         "test_data": "", "expected_result": "An error is shown"},
    ])

    return {"alpha": alpha, "beta": beta, "bug_id": bug_id,
            "est_id": est_id, "run_id": run_id}


def _fingerprint(ids: dict) -> dict:
    """What a person would notice if the upgrade lost it."""
    alpha, beta = ids["alpha"], ids["beta"]
    return {
        "alpha_cases": [(c["id"], c["summary"]) for c in
                        _db.load_test_cases(alpha)],
        "beta_cases": [(c["id"], c["summary"]) for c in
                       _db.load_test_cases(beta)],
        "alpha_checklist": [(c["id"], c["objective"]) for c in
                            _db.load_checklist(alpha)],
        "alpha_bugs": [(b["title"], b["severity"]) for b in
                       _db.list_bugs(alpha)],
        "run_stats": (_db.get_execution_run(ids["run_id"]) or {}).get("stats"),
        "run_results": sorted(
            (r["case_external_id"], r["status"], r["source"])
            for r in _db.list_case_results(ids["run_id"])),
    }


def _indexes_on(engine, table: str, column: str) -> list[str]:
    try:
        return [ix["name"] for ix in inspect(engine).get_indexes(table)
                if column in (ix.get("column_names") or [])]
    except Exception:      # pragma: no cover — table absent
        return []


def _regress_schema(engine) -> None:
    """Take the schema back to the shape it had before this programme.

    Indexes first: SQLite refuses to drop a column an index mentions, and
    ``project.org_id`` carries one. Dropping the index is also part of
    what is being simulated — the pre-programme database had neither.
    """
    for table, column, _expected in PROGRAMME_COLUMNS:
        for index in _indexes_on(engine, table, column):
            with engine.begin() as conn:
                conn.execute(text(f'DROP INDEX IF EXISTS "{index}"'))
    for index in PROGRAMME_INDEXES:
        with engine.begin() as conn:
            conn.execute(text(f"DROP INDEX IF EXISTS {index}"))
    with engine.begin() as conn:
        for table, column, _expected in PROGRAMME_COLUMNS:
            conn.execute(text(f'ALTER TABLE {table} DROP COLUMN "{column}"'))
    # The identity tables, dropped through the metadata so SQLAlchemy
    # sorts them by dependency — ``org_member`` points at two of the
    # others, and SQLite has foreign keys switched on.
    tables = [_db.Base.metadata.tables[name] for name in PROGRAMME_TABLES
              if name in _db.Base.metadata.tables]
    _db.Base.metadata.drop_all(engine, tables=tables, checkfirst=True)


@pytest.fixture(params=_params())
def prod_copy(request, tmp_path, monkeypatch):
    """A populated database whose schema predates this programme.

    Yields ``(engine, ids, fingerprint, backend)``. The fingerprint is
    taken **before** the schema is taken backwards, because after that the
    ORM cannot read its own rows — which is the whole failure mode.
    """
    if not _CAN_DROP_COLUMN and request.param == "sqlite":
        pytest.skip("SQLite < 3.35 has no DROP COLUMN")

    monkeypatch.setenv("FLASK_DEBUG", "1")
    if request.param == "postgres":
        url = POSTGRES_URL
        marker = url.rsplit("/", 1)[-1].split("?")[0]
    else:
        db_path = tmp_path / "prodcopy.db"
        url = f"sqlite:///{db_path}"
        marker = str(db_path)

    monkeypatch.setenv("DATABASE_URL", url)
    monkeypatch.setattr(_db, "_engine", None, raising=False)
    monkeypatch.setattr(_db, "_Session", None, raising=False)

    if request.param == "postgres":
        from sqlalchemy import create_engine
        probe = create_engine(url)
        _db.Base.metadata.drop_all(probe)
        probe.dispose()

    _db.init_db()
    engine = _db.get_engine()
    # Isolation asserted rather than assumed: the first time the sibling
    # file ran inside the full suite the swap had not taken and it dropped
    # tables out of the shared development database, surfacing six files
    # later as "no such table".
    assert marker in str(engine.url), (
        f"refusing to run destructive migration tests against {engine.url}"
        f" — the database swap did not take")

    ids = _populate()
    fingerprint = _fingerprint(ids)
    _regress_schema(engine)

    yield engine, ids, fingerprint, request.param

    if request.param == "postgres":
        _db.Base.metadata.drop_all(engine)
    engine.dispose()
    monkeypatch.setattr(_db, "_engine", None, raising=False)
    monkeypatch.setattr(_db, "_Session", None, raising=False)


def _boot(monkeypatch) -> None:
    """Do what a deploy does: start the app against the existing database."""
    monkeypatch.setattr(_db, "_engine", None, raising=False)
    monkeypatch.setattr(_db, "_Session", None, raising=False)
    _db.init_db()


def _columns(table: str) -> set[str]:
    try:
        return {c["name"] for c in inspect(_db.get_engine()).get_columns(table)}
    except Exception:
        return set()


# ── The tests ────────────────────────────────────────────────────────

class TestTheSimulationIsReal:
    """A regression harness that quietly did nothing would pass everything."""

    def test_the_schema_really_went_backwards(self, prod_copy):
        engine, _ids, _fp, _backend = prod_copy
        insp = inspect(engine)
        survivors = []
        for table, column, _expected in PROGRAMME_COLUMNS:
            names = {c["name"] for c in insp.get_columns(table)}
            if column in names:
                survivors.append(f"{table}.{column}")
        assert not survivors, (
            f"still present after the rollback, so the upgrade below is "
            f"not being exercised: {survivors}")
        present = set(insp.get_table_names())
        assert not present & set(PROGRAMME_TABLES), \
            sorted(present & set(PROGRAMME_TABLES))

    def test_the_copy_has_data_in_it(self, prod_copy):
        _engine, _ids, fingerprint, _backend = prod_copy
        assert len(fingerprint["alpha_cases"]) == 2
        assert fingerprint["alpha_bugs"], "no bug to lose"
        assert fingerprint["run_results"], "no result to lose"


class TestTheUpgradeKeepsTheWork:
    """The question a person asks after a deploy."""

    def test_every_artefact_reads_back_unchanged(self, prod_copy, monkeypatch):
        _engine, ids, fingerprint, _backend = prod_copy
        _boot(monkeypatch)
        assert _fingerprint(ids) == fingerprint

    def test_the_orm_can_read_a_migrated_row(self, prod_copy, monkeypatch):
        """The failure this whole file exists for.

        A column the model declares and the migration forgets raises
        "no such column" on the first ORM read — after deploy, on a
        customer's data, never in CI.
        """
        _engine, ids, _fp, _backend = prod_copy
        _boot(monkeypatch)
        missing = [f"{t}.{c}" for t, c, _e in PROGRAMME_COLUMNS
                   if c not in _columns(t)]
        assert not missing, (
            f"declared by the model, never added to an existing database: "
            f"{missing}")
        # …and exercise it, because a column can exist and still be
        # unreadable when the ALTER gave it a type the model disagrees with.
        assert _db.load_test_cases(ids["alpha"])
        assert _db.list_bugs(ids["alpha"])
        assert _db.get_execution_run(ids["run_id"])

    @pytest.mark.parametrize("table,column,expected", [
        pytest.param(t, c, e, id=f"{t}.{c}") for t, c, e in PROGRAMME_COLUMNS])
    def test_existing_rows_back_fill_to_the_declared_value(
            self, prod_copy, monkeypatch, table, column, expected):
        """Each back-fill is a claim about pre-existing data.

        ``ai_generated`` true and ``row_version`` 1 say "nothing here has
        been edited", which is the honest reading — until E4 there was no
        way to edit it. ``edited_by`` NULL says the same thing from the
        other side, and is the one that would be wrong if a migration
        invented an author.
        """
        _engine, _ids, _fp, _backend = prod_copy
        _boot(monkeypatch)
        with _db.session_scope() as sess:
            value = sess.execute(
                text(f'SELECT "{column}" FROM {table} LIMIT 1')).scalar()
        if expected is None:
            assert value is None, f"{table}.{column} = {value!r}"
        elif expected is True:
            # SQLite has no boolean type; Postgres does.
            assert value in (True, 1), f"{table}.{column} = {value!r}"
        elif expected == {}:
            assert value in ({}, "{}"), f"{table}.{column} = {value!r}"
        else:
            assert value == expected, f"{table}.{column} = {value!r}"

    def test_a_json_column_added_by_alter_round_trips(self, prod_copy,
                                                      monkeypatch):
        """``project.settings`` is JSON in the model and back-filled by SQL.

        Asserted as a round trip rather than by looking at the raw column,
        because the defect this guards is a *type* mismatch and it only
        shows up on use: an ALTER declaring TEXT gives Postgres a column
        whose contents arrive as the string ``'{}'``, and
        ``get_project_setting`` — which calls ``.get`` on whatever comes
        back — raises ``AttributeError`` on the first dashboard render.
        """
        _engine, ids, _fp, _backend = prod_copy
        _boot(monkeypatch)
        assert _db.get_project_setting(ids["alpha"], "kpi_targets") is None
        _db.set_project_setting(ids["alpha"], "kpi_targets",
                                {"pass_rate": 90.0})
        assert _db.get_project_setting(
            ids["alpha"], "kpi_targets") == {"pass_rate": 90.0}

    def test_booting_twice_changes_nothing(self, prod_copy, monkeypatch):
        """Two gunicorn workers boot at once; both run the whole chain."""
        _engine, ids, fingerprint, _backend = prod_copy
        _boot(monkeypatch)
        before = {t: _columns(t) for t, _c, _e in PROGRAMME_COLUMNS}
        _boot(monkeypatch)
        _boot(monkeypatch)
        assert {t: _columns(t) for t, _c, _e in PROGRAMME_COLUMNS} == before
        assert _fingerprint(ids) == fingerprint


class TestTheUpgradeMakesTheProductUsable:
    """Surviving the migration is not the same as working afterwards."""

    def test_an_account_can_be_created_and_adopt_the_existing_work(
            self, prod_copy, monkeypatch):
        """E1.6's migration path, on the data it was written for.

        Every project in a real copy predates organisations and has
        ``org_id`` NULL. If adoption does not reach them the first
        administrator signs in to an empty picker and their whole history
        is invisible — which is a data-loss incident from where they sit,
        whether or not the rows are still on disk.
        """
        _engine, ids, _fp, _backend = prod_copy
        _boot(monkeypatch)

        user_id = _db.create_user("owner@prodcopy.test",
                                  display_name="The Owner",
                                  email_verified=True)
        org_id = _db.create_organization("Prod Copy Team")
        assert user_id and org_id
        assert _db.add_org_member(org_id, user_id, "admin")

        adopted = _db.adopt_orphan_projects(org_id)
        assert adopted >= 2, f"only {adopted} legacy project(s) adopted"

        visible = {p["id"] for p in _db.list_projects(org_id=org_id)}
        assert {ids["alpha"], ids["beta"]} <= visible

    def test_the_editing_metadata_works_on_a_pre_programme_row(
            self, prod_copy, monkeypatch):
        """Optimistic locking has to start somewhere.

        A row back-filled to ``row_version = 1`` must accept an edit that
        presents 1 and refuse one that presents a stale number. Without
        this, the back-fill value could be anything and every test above
        would still pass.
        """
        from engine import editable as _editable

        _engine, ids, _fp, _backend = prod_copy
        _boot(monkeypatch)

        case = _db.load_test_cases(ids["alpha"])[0]
        row = _editable.get("test_case", ids["alpha"], case["id"])
        assert row and row["row_version"] == 1
        assert row["ai_generated"] is True

        updated = _editable.patch(
            "test_case", ids["alpha"], case["id"],
            {"summary": "Verify that a card payment is accepted, edited"},
            expected_version=1, actor="u-owner")
        assert updated["row_version"] == 2
        assert updated["ai_generated"] is False, \
            "an edited row still claims to be generator output"

        with pytest.raises(_db.WriteConflict):
            _editable.patch("test_case", ids["alpha"], case["id"],
                            {"summary": "A stale write"},
                            expected_version=1, actor="u-someone-else")


class TestTheAltersAgreeWithTheModel:
    """What can be checked without a Postgres server, checked here.

    The two tests below found live defects, and both had the same shape:
    an ALTER that SQLite accepts, that Postgres rejects or silently
    reinterprets, and that no test had ever run on Postgres — because CI's
    Postgres database is created fresh every time, so ``create_all`` makes
    the column and the ALTER never fires. The populated-copy fixture above
    is what puts these on the Postgres leg; these are what fail on a
    laptop, immediately, without one.
    """

    def _declared_type(self, table: str, column: str):
        for model in _db.Base.registry.mappers:
            klass = model.class_
            if getattr(klass, "__tablename__", None) != table:
                continue
            col = klass.__table__.columns.get(column)
            if col is not None:
                return col.type
        return None

    def test_a_boolean_column_is_added_with_a_boolean_default(self):
        """``DEFAULT 1`` on a boolean is an error on Postgres.

        There is no implicit or assignment cast from integer to boolean, so
        the statement is refused outright. ``_ensure_editable_columns``
        catches and logs that, which means the column simply never appears
        and the next ORM read raises "column does not exist" — on upgraded
        Postgres instances only. Quoting the literal is what
        ``create_all`` already emits for the same column, on both engines.
        """
        import re
        offenders = []
        for table, column, statement in _db._EDITABLE_COLUMN_MIGRATIONS:
            if not isinstance(self._declared_type(table, column),
                              _db.Boolean):
                continue
            match = re.search(r"DEFAULT\s+(\S+)", statement)
            if match and match.group(1) not in ("'1'", "'0'",
                                                "true", "false"):
                offenders.append(f"{table}.{column}: {match.group(1)}")
        assert not offenders, (
            f"a boolean column given a non-boolean default; Postgres will "
            f"refuse the ALTER and the column will never be added: "
            f"{offenders}")

    def test_a_json_column_is_added_as_json(self):
        """A JSON model column added as TEXT reads back as a string.

        SQLAlchemy's ``JSON`` decodes for itself on SQLite and delegates to
        the driver on Postgres, where psycopg2 only parses columns whose
        declared type is really json. So the mismatch is invisible in
        development and turns every reader that expects a mapping into an
        ``AttributeError`` after an upgrade.
        """
        import re
        offenders = []
        for table, column, statement in _db._EDITABLE_COLUMN_MIGRATIONS:
            if not isinstance(self._declared_type(table, column), _db.JSON):
                continue
            match = re.search(rf"ADD COLUMN {column} (\w+)", statement)
            if match and match.group(1).upper() != "JSON":
                offenders.append(f"{table}.{column}: {match.group(1)}")
        assert not offenders, (
            f"declared JSON by the model, added as something else: "
            f"{offenders}")

    def test_the_matcher_would_notice(self):
        """Guards the guards.

        Both tests above pass trivially if ``_declared_type`` returns None
        for everything — which is exactly what a rename of a model
        attribute would cause, and it would look like a clean build.
        """
        assert isinstance(self._declared_type("test_case", "ai_generated"),
                          _db.Boolean)
        assert isinstance(self._declared_type("project", "settings"),
                          _db.JSON)


class TestDuplicatePublicIdsInTheCopy:
    """The case a clean database cannot reach.

    ``_ensure_public_id_unique_indexes`` always succeeds on an empty
    table. On a real copy it is the migration most likely to fail, because
    the site-aware generator emitted duplicate checklist ids for months —
    an 82-item pack containing ``CNT_001`` twice was measured on
    production. So the repair has to run first and has to actually work,
    or the guard silently never goes on.
    """

    def _make_a_collision(self, engine, project_id: str) -> None:
        """A second ``HDR_001``, written the way the old generator wrote it.

        Raw SQL because there is no longer a supported way to produce this
        — ``save_checklist`` runs ``public_ids.ensure_unique`` over the
        pack. The rows exist anyway, on every instance that ran before it
        did, which is the whole point of a populated copy.
        """
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO checklist_item "
                "(project_id, external_id, section, objective, item_num, "
                " depth, created_at, updated_at) "
                "VALUES (:p, 'HDR_001', 'Header', "
                "'Verify that the duplicate is repaired', '', 2, :t, :t)"),
                {"p": project_id, "t": now})

    def test_the_index_goes_on_over_a_repaired_collision(self, prod_copy,
                                                         monkeypatch):
        engine, ids, _fp, _backend = prod_copy
        self._make_a_collision(engine, ids["alpha"])
        _boot(monkeypatch)

        names = {ix["name"] for ix in
                 inspect(_db.get_engine()).get_indexes("checklist_item")}
        assert "ux_checklist_item_project_external_id" in names, (
            "the unique index was not created, so hand-created ids are "
            "unprotected against a same-instant collision")

    def test_nothing_is_deleted_to_make_room_for_the_index(self, prod_copy,
                                                           monkeypatch):
        """Repair, not removal.

        The colliding row is somebody's checklist item. Renumbering keeps
        it; dropping it would make the index creatable and lose work, and
        both outcomes look identical from the index's point of view.
        """
        engine, ids, _fp, _backend = prod_copy
        self._make_a_collision(engine, ids["alpha"])
        _boot(monkeypatch)

        items = _db.load_checklist(ids["alpha"])
        assert len(items) == 3, [i["id"] for i in items]
        objectives = {i["objective"] for i in items}
        assert "Verify that the duplicate is repaired" in objectives
        ids_seen = [i["id"] for i in items]
        assert len(set(ids_seen)) == len(ids_seen), ids_seen

    def test_the_row_that_was_there_first_keeps_its_id(self, prod_copy,
                                                       monkeypatch):
        """Which row gets renumbered is not arbitrary.

        These ids appear in exports, in bug reports that cite "failed at
        HDR_001", and in a client's review comments. The oldest row is the
        one most likely to be the one already cited, so it keeps the id and
        the newcomer moves.
        """
        engine, ids, _fp, _backend = prod_copy
        self._make_a_collision(engine, ids["alpha"])
        _boot(monkeypatch)

        by_objective = {i["objective"]: i["id"]
                        for i in _db.load_checklist(ids["alpha"])}
        assert by_objective[
            "Verify that the logo links to the home page"] == "HDR_001"
        assert by_objective[
            "Verify that the duplicate is repaired"] != "HDR_001"
