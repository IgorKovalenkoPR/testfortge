"""Import hardening — E4.8.

The behaviour this replaces: a spreadsheet whose columns are called anything
the alias lists do not know produced **0 rows**, which tells the user their
file is wrong when in fact only its vocabulary is.

The acceptance criterion is that such a file imports *through a mapping*. So
the tests care about three things: the report says which headers were found,
an explicit mapping makes the same file import, and a repeated append does not
double the pack.
"""
from __future__ import annotations

import io

import secrets

import pytest

from engine import db, import_preview
from engine import imports as _imports

#: A token per run, not just per test.
#:
#: ``conftest`` cannot always delete the scratch database on Windows — the file
#: may still be held open — so a stable project name silently reuses the
#: previous run's rows. E4.1 preserves ``row_version`` across a pack save, so a
#: version assertion then depends on how many times the suite has been run.
#: Measured: three of these tests failed on the second invocation and passed on
#: the first.
_RUN = secrets.token_hex(4)


OPAQUE = "Kolumna A,Kolumna B,Kolumna C\nZalogowanie,Otworz,Panel\n"
FAMILIAR = "ID,Summary,Steps,Expected\nTC-900,Sign in,Open,Dashboard\n"


@pytest.fixture
def project(app, request):
    return db.upsert_project(name=f"E4.8 {request.node.name} {_RUN}"[:180])


def _upload(client, project, csv, *, kind="test_cases", mode="replace",
            mapping=None):
    with client.session_transaction() as sess:
        sess["project_id"] = project
    data = {"upload_file": (io.BytesIO(csv.encode()), "pack.csv"),
            "upload_mode": mode}
    data.update(mapping or {})
    path = ("/test-cases/upload" if kind == "test_cases"
            else "/checklist/upload")
    return client.post(path, data=data, content_type="multipart/form-data",
                       follow_redirects=True)


class TestAnalyse:

    def test_familiar_headers_map_themselves(self):
        analysis = import_preview.analyse(
            "test_cases", ["ID", "Summary", "Steps", "Expected"])
        assert analysis.usable
        assert analysis.mapped.get("summary") == "Summary"

    def test_opaque_headers_are_reported_not_swallowed(self):
        analysis = import_preview.analyse(
            "test_cases", ["Kolumna A", "Kolumna B"])
        assert not analysis.usable
        assert "Kolumna A" in analysis.message()
        assert analysis.ignored == ["Kolumna A", "Kolumna B"]

    def test_the_message_says_what_is_needed(self):
        message = import_preview.analyse("test_cases", ["X"]).message()
        assert "summary" in message and "test steps" in message

    def test_a_file_with_no_header_row_says_so(self):
        assert "no header row" in import_preview.analyse(
            "test_cases", []).message()

    def test_a_checklist_needs_only_an_objective(self):
        analysis = import_preview.analyse("checklist", ["Objective"])
        assert analysis.usable
        assert analysis.missing_required == []

    def test_targets_put_the_mapped_ones_first(self):
        analysis = import_preview.analyse("test_cases", ["Summary"])
        assert analysis.targets()[0] == "summary"

    def test_an_unknown_kind_is_refused(self):
        with pytest.raises(ValueError):
            import_preview.analyse("estimations", [])


class TestResolve:

    def test_an_explicit_mapping_wins(self):
        col_map = import_preview.resolve(
            "test_cases", ["Kolumna A", "Kolumna B"],
            {"summary": "Kolumna A", "test_steps": "Kolumna B"})
        assert col_map == {"summary": 0, "test_steps": 1}

    def test_an_empty_choice_means_do_not_import_that_field(self):
        col_map = import_preview.resolve(
            "test_cases", ["Summary"], {"summary": ""},
            base={"summary": 0})
        assert "summary" not in col_map

    def test_a_stale_choice_is_ignored_rather_than_fatal(self):
        """The form offers the file's own headers, so a mismatch means a stale
        form — and refusing the whole import over one stale select would be
        worse than importing what can be read."""
        col_map = import_preview.resolve(
            "test_cases", ["Summary"], {"summary": "A column since renamed"})
        assert col_map == {}

    def test_a_field_outside_the_allowlist_is_ignored(self):
        col_map = import_preview.resolve(
            "test_cases", ["Summary"], {"project_id": "Summary"})
        assert col_map == {}


class TestDedup:

    def test_a_repeated_id_is_skipped(self):
        kept, skipped = import_preview.dedup(
            [{"id": "TC-1"}], [{"id": "TC-1"}, {"id": "TC-2"}])
        assert [row["id"] for row in kept] == ["TC-2"]
        assert skipped == ["TC-1"]

    def test_a_repeat_inside_the_incoming_pack_is_skipped_too(self):
        kept, skipped = import_preview.dedup(
            [], [{"id": "TC-1"}, {"id": "TC-1"}])
        assert len(kept) == 1 and skipped == ["TC-1"]

    def test_rows_without_an_id_are_all_kept(self):
        """Two blank-id rows are two rows; there is nothing to compare."""
        kept, skipped = import_preview.dedup([], [{"id": ""}, {"id": ""}])
        assert len(kept) == 2 and skipped == []

    def test_content_is_not_compared(self):
        """Comparing content would silently drop a row somebody deliberately
        re-imported after editing it elsewhere."""
        kept, _ = import_preview.dedup(
            [{"id": "TC-1", "summary": "Old"}],
            [{"id": "TC-2", "summary": "Old"}])
        assert len(kept) == 1


class TestThroughTheApp:

    def test_a_familiar_file_still_imports(self, client, project):
        _upload(client, project, FAMILIAR)
        assert len(db.load_test_cases(project)) == 1

    def test_an_opaque_file_reports_instead_of_importing_nothing(
            self, client, project):
        body = _upload(client, project, OPAQUE).get_data(as_text=True)
        assert db.load_test_cases(project) == []
        assert "Kolumna A" in body, "the message names the headers found"
        assert 'name="map_summary"' in body, "and offers the mapping form"

    def test_the_form_offers_the_files_own_headers(self, client, project):
        _upload(client, project, OPAQUE)
        body = client.get("/test-cases").get_data(as_text=True)
        for header in ("Kolumna A", "Kolumna B", "Kolumna C"):
            assert f'value="{header}"' in body, header

    def test_the_same_file_imports_with_a_mapping(self, client, project):
        """The acceptance criterion, stated directly."""
        _upload(client, project, OPAQUE, mapping={
            "map_summary": "Kolumna A", "map_test_steps": "Kolumna B",
            "map_expected_result": "Kolumna C"})
        rows = db.load_test_cases(project)
        assert len(rows) == 1
        assert rows[0]["summary"] == "Zalogowanie"
        assert rows[0]["test_steps"] == "Otworz"
        assert rows[0]["expected_result"] == "Panel"

    def test_appending_the_same_file_twice_does_not_double_the_pack(
            self, client, project):
        _upload(client, project, FAMILIAR, mode="append")
        _upload(client, project, FAMILIAR, mode="append")
        ids = [row["id"] for row in db.load_test_cases(project)]
        assert ids.count("TC-900") == 1, ids

    def test_the_skipped_rows_are_reported(self, client, project):
        _upload(client, project, FAMILIAR, mode="append")
        body = _upload(client, project, FAMILIAR,
                       mode="append").get_data(as_text=True)
        assert "Skipped 1" in body

    def test_a_checklist_maps_the_same_way(self, client, project):
        opaque = "Punkt,Uwaga\nLogo prowadzi do strony glownej,tak\n"
        _upload(client, project, opaque, kind="checklist")
        assert db.load_checklist(project) == []
        _upload(client, project, opaque, kind="checklist",
                mapping={"map_objective": "Punkt", "map_comments": "Uwaga"})
        rows = db.load_checklist(project)
        assert len(rows) == 1
        assert rows[0]["objective"] == "Logo prowadzi do strony glownej"


class TestReadHeaders:

    def test_csv_headers_are_read(self, tmp_path):
        path = tmp_path / "pack.csv"
        path.write_text(FAMILIAR, encoding="utf-8")
        assert _imports.read_headers(str(path), "pack.csv")[:2] == ["ID",
                                                                    "Summary"]

    def test_a_format_without_a_header_row_returns_nothing(self, tmp_path):
        """Markdown carries its field names inside each record, so there is
        nothing to map."""
        path = tmp_path / "pack.md"
        path.write_text("# Cases\n", encoding="utf-8")
        assert _imports.read_headers(str(path), "pack.md") == []

    def test_an_unreadable_file_is_not_fatal(self, tmp_path):
        assert _imports.read_headers(str(tmp_path / "absent.csv"),
                                     "absent.csv") == []
