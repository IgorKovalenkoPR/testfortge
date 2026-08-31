"""``walkthrough_findings_stats`` read whichever path it was handed.

Its own docstring said the argument is "paths to ``automation_runs/*.result
.json`` files". Nothing enforced that, so any findings-shaped JSON the
process could open was summarised and its ``message`` strings came back in
``samples`` — measured with a file written outside the directory, not
supposed. The caller here is an agent over a network holding one
instance-wide bearer token, which is the opposite of the CLI's operator at
their own shell.

So the confinement is at the MCP boundary and **not** in
``engine.walkthrough_stats.summarise_files``: the module's stated contract
is "or whichever paths the operator passes", and an operator pointing the
CLI at a result.json in Downloads is the normal case. What the library did
gain is a size cap, which is not a scope change — ``read_text()`` had no
upper bound at all, so any path was pulled into memory in full before the
JSON parse could reject it. On a 512 MB dyno that is one argument from an
OOM, and a file that never ends is one from a hang.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from engine import walkthrough_stats
from mcp_server import server as mcp_server


SECRET = "SECRET-FROM-OUTSIDE-THE-RUNS-DIR"


def _findings_payload(message: str) -> str:
    return json.dumps({"walkthrough_findings": [{
        "defect_class": "leak", "severity": "High",
        "area": "Somewhere", "url": "https://secret.example/admin",
        "message": message,
    }]})


@pytest.fixture
def runs_dir() -> Path:
    d = (Path(mcp_server.__file__).resolve().parent.parent
         / "automation_runs")
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def inside(runs_dir):
    """A real result.json where the tool is meant to look."""
    p = runs_dir / f"zz-{os.urandom(4).hex()}.result.json"
    p.write_text(_findings_payload("A finding from a real run"),
                 encoding="utf-8")
    yield p
    try:
        p.unlink()
    except OSError:
        pass


@pytest.fixture
def outside(tmp_path):
    """A findings-shaped file somewhere the tool has no business reading."""
    p = tmp_path / "elsewhere.result.json"
    p.write_text(_findings_payload(SECRET), encoding="utf-8")
    return p


class TestTheConfinement:

    def test_a_path_outside_the_directory_is_refused(self, outside):
        out = mcp_server.walkthrough_findings_stats([str(outside)])
        assert out.get("error") == "path_not_allowed", out
        assert SECRET not in json.dumps(out)

    def test_it_is_refused_by_name_rather_than_skipped(self, inside, outside):
        """A client that believes it summarised a file the tool ignored is
        worse off than one told it cannot. So a mixed list fails whole."""
        out = mcp_server.walkthrough_findings_stats(
            [str(inside), str(outside)])
        assert out.get("error") == "path_not_allowed", out
        assert "total" not in out

    def test_a_traversal_out_of_the_directory_is_refused(self, runs_dir,
                                                        outside):
        """``..`` is settled by ``resolve()`` before the comparison, so a
        path that *starts* inside and climbs out does not pass on its
        prefix."""
        climbing = runs_dir / ".." / ".." / outside.name
        out = mcp_server.walkthrough_findings_stats([str(climbing)])
        assert out.get("error") == "path_not_allowed", out

    def test_a_file_inside_but_not_a_result_json_is_refused(self, runs_dir):
        """The suffix is half the promise. The directory holds run
        subdirectories full of screenshots and ``_pending`` configs naming
        other projects' base URLs — none of them is this tool's business."""
        p = runs_dir / f"zz-{os.urandom(4).hex()}.json"
        p.write_text(_findings_payload("not a result file"), encoding="utf-8")
        try:
            out = mcp_server.walkthrough_findings_stats([str(p)])
            assert out.get("error") == "path_not_allowed", out
        finally:
            try:
                p.unlink()
            except OSError:
                pass

    def test_too_many_paths_is_refused(self, inside):
        out = mcp_server.walkthrough_findings_stats(
            [str(inside)] * (mcp_server.MAX_STATS_FILES + 1))
        assert out.get("error") == "too_many_paths", out


class TestTheControl:
    """The tool still does its job — asserted because every test above
    would also pass if it refused everything."""

    def test_a_file_inside_the_directory_is_summarised(self, inside):
        out = mcp_server.walkthrough_findings_stats([str(inside)])
        assert out.get("total") == 1, out
        assert "leak" in out["by_class"]

    def test_a_relative_name_resolves_against_the_directory(self, inside):
        """What a client naturally sends: the file name, not an absolute
        path. It has to keep working, or the confinement reads as a
        breakage."""
        out = mcp_server.walkthrough_findings_stats([inside.name])
        assert out.get("total") == 1, out

    def test_no_paths_still_globs_the_directory(self, inside):
        out = mcp_server.walkthrough_findings_stats()
        assert out.get("total", 0) >= 1, out


class TestTheSizeCap:
    """The library's half. Applies to the CLI too, which is the point."""

    def test_an_oversized_file_is_skipped_unparsed(self, tmp_path,
                                                   monkeypatch):
        monkeypatch.setattr(walkthrough_stats, "MAX_RESULT_BYTES", 512)
        big = tmp_path / "big.result.json"
        payload = json.loads(_findings_payload("x"))
        payload["padding"] = "y" * 2048
        big.write_text(json.dumps(payload), encoding="utf-8")
        assert walkthrough_stats.summarise_files([str(big)])["total"] == 0

    def test_a_file_within_the_cap_is_read(self, tmp_path, monkeypatch):
        monkeypatch.setattr(walkthrough_stats, "MAX_RESULT_BYTES", 4096)
        small = tmp_path / "small.result.json"
        small.write_text(_findings_payload("within the cap"), encoding="utf-8")
        assert walkthrough_stats.summarise_files([str(small)])["total"] == 1

    def test_the_cap_is_measured_on_what_was_read_not_on_stat(self, tmp_path,
                                                             monkeypatch):
        """Reading one byte past the cap is what makes the check hold on a
        file whose size cannot be taken in advance. Asserted by making
        ``stat`` lie: a size-based check would let this through."""
        monkeypatch.setattr(walkthrough_stats, "MAX_RESULT_BYTES", 512)
        big = tmp_path / "liar.result.json"
        payload = json.loads(_findings_payload("x"))
        payload["padding"] = "y" * 2048
        big.write_text(json.dumps(payload), encoding="utf-8")

        real_stat = Path.stat

        def _small_lie(self, *args, **kwargs):
            got = real_stat(self, *args, **kwargs)

            class _Faked:
                st_size = 1
                def __getattr__(inner, name):
                    return getattr(got, name)

            return _Faked()

        monkeypatch.setattr(Path, "stat", _small_lie)
        assert walkthrough_stats.summarise_files([str(big)])["total"] == 0

    def test_the_library_still_accepts_any_path(self, outside):
        """Deliberately unconfined, and asserted so a later tightening has
        to argue with this test rather than break the CLI silently."""
        assert walkthrough_stats.summarise_files([str(outside)])["total"] == 1
