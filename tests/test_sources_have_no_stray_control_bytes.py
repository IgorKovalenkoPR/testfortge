"""A test can pass with rubbish in its payload, and did.

Written after the third occurrence in one session. Patching a file through
a shell heredoc turns ``\\x00`` in the patch text into a **real NUL byte**
in the source and ``\\n`` into a real newline. The second breaks the parse
and is caught immediately. The first does not:

    lines, err = self._told(tmp_path, "demo.mp4",
                            b"<NUL><NUL><NUL><CAN>ftypisom" + b"<NUL>" * 2048)

parses, runs, and passes — the bytes are still bytes and the assertion is
still about the message. Nothing says the literal is no longer the one
somebody meant to write, and a reader sees blanks.

So the rule is about the *file*, not about any test's subject: no source in
this repository carries a control byte outside tab, newline and carriage
return. That is cheap to check, and it is the only kind of corruption this
session produced that a green run could hide.

Scoped to text sources — ``.py``, ``.html``, ``.yml``, ``.md``, ``.json``.
Binary assets under ``static/`` are excluded by extension rather than by
directory, so a new image format does not silently leave the scan.
"""
from __future__ import annotations

import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Directories that are not this repository's source.
SKIP_DIRS = {".git", "__pycache__", "node_modules", ".claude", "storage",
             ".pytest_cache", "venv", ".venv", "htmlcov", "dist", "build"}

SUFFIXES = {".py", ".html", ".yml", ".yaml", ".md", ".json", ".txt", ".css",
            ".js", ".cfg", ".toml", ".ini"}

#: Tab, newline and carriage return are the control bytes a text file may
#: legitimately hold. Everything else — NUL, form feed, escape, a stray
#: vertical tab — is somebody's editor or somebody's shell.
ALLOWED = {0x09, 0x0A, 0x0D}


def _sources():
    for path in ROOT.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in SUFFIXES:
            continue
        # Relative to ROOT, not the absolute path. This repository is
        # checked out inside a git worktree under ``.claude/worktrees/``,
        # so matching against every ancestor skipped the entire tree and
        # the scan found nothing — which its companion test caught.
        if any(part in SKIP_DIRS
               for part in path.relative_to(ROOT).parts):
            continue
        yield path


def test_the_scan_finds_the_repository():
    """Without this the test below passes on an empty generator — a
    renamed directory, a changed suffix set, a move."""
    found = list(_sources())
    assert len(found) > 200, f"only {len(found)} source files found"
    names = {p.name for p in found}
    for expected in ("app.py", "base.html", "tests.yml"):
        assert expected in names, expected


def test_no_source_carries_a_stray_control_byte():
    offenders = []
    for path in _sources():
        raw = path.read_bytes()
        bad = sorted({b for b in raw if b < 0x20 and b not in ALLOWED})
        if bad:
            # The line number, because "somewhere in a 5 000-line file" is
            # not actionable.
            first = next(i + 1 for i, line in enumerate(raw.splitlines())
                         if any(b < 0x20 and b not in ALLOWED for b in line))
            offenders.append(
                f"{path.relative_to(ROOT).as_posix()}:{first} "
                f"has {', '.join(hex(b) for b in bad)}")
    assert not offenders, (
        "these files carry control bytes that no editor puts there — a "
        "heredoc turning \\x00 into a real NUL is the way this happens, "
        "and the file still parses and the tests still pass:\n  "
        + "\n  ".join(offenders))


@pytest.mark.parametrize("byte,name", [(0x00, "NUL"), (0x0C, "form feed"),
                                       (0x1B, "escape")])
def test_the_check_would_catch_one(byte, name):
    """One per shape, so the predicate cannot rot into `if False`."""
    payload = b"x = 1" + bytes([byte]) + b"\n"
    bad = sorted({b for b in payload if b < 0x20 and b not in ALLOWED})
    assert bad == [byte], (name, bad)


def test_the_check_passes_ordinary_text():
    payload = b"def f():\r\n\tif True:\n\t\treturn 1\n"
    bad = sorted({b for b in payload if b < 0x20 and b not in ALLOWED})
    assert bad == [], bad
