"""Six exports built a header by interpolating the project name into it.

    f"attachment; filename=testfortge_{name}.md"

with at most a ``.replace(" ", "_")`` in front. A project name is free text
from a form. Three things followed, each measured on this suite rather than
reasoned about:

* a name containing a newline made **every** export answer 500 — Werkzeug
  refuses a header value with a newline, which is the good news: no response
  splitting. There was also no export;
* a name containing ``;`` or ``"`` added a second parameter to the header.
  ``Acme; filename=evil.sh`` produced
  ``filename=testfortge_Acme; filename=evil.sh.md``, and which of the two a
  browser saves under is the browser's business, not ours;
* a Cyrillic name — the ordinary case in this product's other language —
  produced a value that is not latin-1 encodable, which PEP 3333 requires a
  WSGI header value to be. The Flask test client hands the string back
  unencoded, so the suite saw 200 where the wire would not. Every test below
  therefore asserts the **encodability** as well as the status code; that is
  the property the test client cannot see for itself.

``routes/projects.py``'s own project export already sanitised, with a regex
and a quoted filename, and was the only one of seven that did. One call site
holding a guard the others don't is the signpost this file was found from —
the same one ``dashboard.py``'s hand-written ``if not project_id`` gave for
``list_automation_runs``.
"""
from __future__ import annotations

import re

import pytest

from engine import db as _db
from routes._shared import SERVER_START_TIME, attachment_header

#: Names a person can actually type or paste into the project-name field.
HOSTILE = {
    "newline": "Acme\r\nX-Injected: yes",
    "bare_lf": "Acme\nX-Injected: yes",
    "semicolon": "Acme; filename=evil.sh",
    "quote": 'Acme "Q" project',
    "cyrillic": "Проєкт Клієнта",
    "emoji": "Acme 🚀",
    "long": "A" * 300,
    "dots": "...",
    "empty": "",
}

#: A ``filename`` parameter whose value is interpolated. Deliberately not
#: "a line containing ``{``" — every one of these lines carries a
#: ``headers={`` dict literal, so that predicate matched the four exports
#: whose filename is a constant and is exactly right.
FILENAME_IS_BUILT = re.compile(r"filename=\{|filename=\"\{|filename=[^\"'\s]*\{")


#: Every export that names its download after the project.
NAMED_EXPORTS = (
    "/export/markdown",
    "/export/html",
    "/export-bug-reports",
    "/export-bug-reports.csv",
)


@pytest.fixture(autouse=True)
def _ready():
    _db.init_db()


def _with_name(client, name):
    with client.session_transaction() as sess:
        sess["_session_active_since"] = SERVER_START_TIME
        sess["project_setup"] = {"project_name": name}
        sess.pop("test_cases_data", None)
        sess["bug_reports_data"] = []


class TestTheHeaderHelper:
    """Unit level, because the helper is the whole fix and the routes only
    have to reach it."""

    @pytest.mark.parametrize("label", sorted(HOSTILE))
    def test_the_value_is_latin_1_encodable(self, label):
        """PEP 3333's requirement on a WSGI header value, and the one the
        Flask test client does not enforce."""
        value = attachment_header(f"testfortge_{HOSTILE[label]}", ".md")
        value.encode("latin-1")          # must not raise

    @pytest.mark.parametrize("label", sorted(HOSTILE))
    def test_no_newline_survives(self, label):
        value = attachment_header(f"testfortge_{HOSTILE[label]}", ".md")
        assert "\r" not in value and "\n" not in value

    @pytest.mark.parametrize("label", sorted(HOSTILE))
    def test_there_is_exactly_one_filename_parameter(self, label):
        """``;`` in a name used to buy a second one. Counted rather than
        pattern-matched: two ``filename=`` parameters is the defect, and a
        header that merely *contains* the name is not evidence either way."""
        value = attachment_header(f"testfortge_{HOSTILE[label]}", ".md")
        assert value.count("filename=") == 1
        assert value.count("filename*=") <= 1

    @pytest.mark.parametrize("label", sorted(HOSTILE))
    def test_the_extension_always_survives(self, label):
        """The stem can sanitise away to nothing; the suffix may not. A
        download called ``export`` with no extension is a support ticket."""
        value = attachment_header(f"testfortge_{HOSTILE[label]}", ".md")
        assert '.md"' in value

    def test_an_ordinary_name_is_left_alone(self):
        """The control. A helper that mangled every name would pass all of
        the above."""
        assert attachment_header("testfortge_Acme_Web", ".md") == \
            'attachment; filename="testfortge_Acme_Web.md"'

    def test_an_ordinary_name_gets_no_second_form(self):
        """``filename*`` is for the names that need it. Emitting it always
        would work and would also mean every header carried a percent-encoded
        duplicate of itself."""
        assert "filename*" not in attachment_header("plain_name", ".csv")

    def test_a_non_ascii_name_reaches_the_person_downloading(self):
        """The reason this is RFC 6266 and not the regex the one sanitising
        call site used: that regex turned a Ukrainian project name into a row
        of dashes, and the product ships a Ukrainian UI."""
        value = attachment_header("Проєкт", ".zip")
        assert "filename*=UTF-8''" in value
        assert "%D0%9F" in value          # П, percent-encoded
        value.encode("latin-1")

    def test_the_stem_is_capped(self):
        value = attachment_header("A" * 500, ".md")
        assert len(value) < 400, len(value)

    def test_an_empty_stem_falls_back_rather_than_naming_nothing(self):
        assert attachment_header("", ".md") == \
            'attachment; filename="export.md"'
        assert attachment_header("   ", ".md", fallback="pack") == \
            'attachment; filename="pack.md"'


class TestTheRoutes:
    """The routes have one job here: reach the helper. Walked rather than
    grepped, because an import is not a call."""

    @pytest.mark.parametrize("url", NAMED_EXPORTS)
    @pytest.mark.parametrize("label", sorted(HOSTILE))
    def test_the_export_answers_and_its_header_is_sendable(self, client, url,
                                                          label):
        _with_name(client, HOSTILE[label])
        response = client.get(url)
        assert response.status_code in (200, 302, 409), (
            f"{url} with a {label} project name: {response.status_code}")
        disposition = response.headers.get("Content-Disposition", "")
        disposition.encode("latin-1")    # must not raise
        assert "\n" not in disposition and "\r" not in disposition
        if disposition:
            assert disposition.count("filename=") == 1

    def test_a_newline_no_longer_kills_the_export(self, client):
        """The sharpest of the three, on its own, because it was a 500 and a
        500 is what an operator sees as "export is broken"."""
        _with_name(client, HOSTILE["newline"])
        assert client.get("/export/markdown").status_code == 200

    def test_the_project_export_still_names_the_project(self, client):
        """``export_project`` is the route that already sanitised. It moved
        onto the shared rule, so this asserts it did not lose the name in
        the process."""
        pid = _db.upsert_project(name="Acme Web")
        with client.session_transaction() as sess:
            sess["_session_active_since"] = SERVER_START_TIME
            sess["project_id"] = pid
        response = client.get(f"/projects/{pid}/export")
        if response.status_code != 200:      # admin-gated in some modes
            pytest.skip(f"export refused with {response.status_code}")
        disposition = response.headers["Content-Disposition"]
        assert "Acme_Web-export.zip" in disposition, disposition
        disposition.encode("latin-1")


class TestNoRouteBuildsItByHand:
    """The enumeration, so a seventh export cannot land with the same bug.

    Read off the source of the route modules rather than from a list of
    known offenders: a gate that knows only today's offenders is blind to
    the one added next week — the lesson the template-reachability scanner
    taught this project the hard way.
    """

    def test_no_module_interpolates_a_filename(self):
        import pathlib

        offenders = []
        # ``_shared.py`` is where the rule lives: the helper interpolates on
        # purpose, and its docstring quotes the pattern it replaced. Excluded
        # by name and with a reason rather than by a regex clever enough to
        # tell them apart — a scanner that cannot say why it skipped a file
        # is the one that declared five live templates dead.
        for path in sorted(pathlib.Path("routes").glob("*.py")):
            if path.name == "_shared.py":
                continue
            for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if FILENAME_IS_BUILT.search(line):
                        offenders.append(f"{path.name}:{number}: {line.strip()}")
        assert not offenders, (
            "a download filename is being built by interpolation; use "
            "routes._shared.attachment_header:\n" + "\n".join(offenders))

    def test_only_the_shared_helper_may_interpolate(self):
        """The exclusion above is safe only while there is exactly one place
        it excuses. Asserted so a second hand-roller cannot hide behind it."""
        import pathlib

        builders = [
            path.name for path in sorted(pathlib.Path("routes").glob("*.py"))
            if FILENAME_IS_BUILT.search(path.read_text(encoding="utf-8"))
        ]
        assert builders == ["_shared.py"], builders

    def test_the_static_filenames_are_still_allowed(self):
        """The test above must not have banned the honest case: several
        exports have a fixed name and need no helper."""
        import pathlib
        source = pathlib.Path("routes/generation.py").read_text(
            encoding="utf-8")
        assert 'filename=test_cases.csv' in source
