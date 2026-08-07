"""E9.5 — the five golden paths, and two people in one team, in a browser.

Everything else in this suite talks to Flask's test client, which is a
Python object pretending to be a browser. It does not run the page's
JavaScript, it does not carry a real cookie jar, and it never fills in a
form. Six of this programme's defects were only visible on the rendered
page, so the top of the pyramid is a real Chromium walking the product
the way a tester does: sign in through the form, click the buttons, read
what comes back.

The five paths are the programme's own list — sign-up → project → test
cases → execute → bug → dashboard — written as five journeys rather than
one long one. One long journey fails at step 6 and tells you nothing
about steps 1 to 5; five tell you which step broke.

**Mode.** Authenticated, throughout, and with CSRF enforcement **on**.
That is the deployment the product ships in, and it is the only mode in
which the multi-user path means anything. It also makes this the one file
in the suite where the tokens in the templates are exercised by something
that actually reads them out of the HTML.

**Anti-flaky.** The programme's gate is ten consecutive green runs
(``docs/plans/e9_test_strategy.md`` — no quarantine, no reruns). Every
wait below is on a *condition* — a selector, a piece of text — never on a
duration and never on ``networkidle``, which resolves whenever the last
request happens to finish and is the usual source of a browser test that
passes on a fast machine. To run the gate:

    for i in $(seq 10); do pytest tests/test_e2e_golden_paths.py -q || break; done

Skipped when Playwright or its Chromium build is unavailable, as the
sibling browser file is.
"""
from __future__ import annotations

import os
import secrets
import socket
import threading
import time
from contextlib import closing

import pytest

pytest.importorskip("playwright.sync_api", reason="playwright not installed")
from playwright.sync_api import Error as PlaywrightError  # noqa: E402
from playwright.sync_api import TimeoutError as PlaywrightTimeout  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from engine import auth as _auth  # noqa: E402
from engine import db as _db  # noqa: E402

#: Long enough for a cold Jinja render on a loaded machine, short enough
#: that a hang is a failure rather than a coffee break.
WAIT = 15_000

#: Passes ``engine.auth.validate_password``; every account below uses it.
PASSWORD = "Golden-Path-Suite-2617!"


def _free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def live_server():
    """The real app on a real port, in the mode this file describes.

    The flags go into ``os.environ`` rather than through monkeypatch
    because the server runs in a thread of this same process and reads
    them per call — a per-test patch would be invisible to the request
    handling thread half the time. Restored on the way out, so the rest
    of the session sees what it saw before.
    """
    from werkzeug.serving import make_server
    from app import app

    previous = {k: os.environ.get(k) for k in ("AUTH_ENABLED", "ORG_MODE")}
    os.environ["AUTH_ENABLED"] = "1"
    os.environ["ORG_MODE"] = "1"
    prior_csrf = app.config.get("WTF_CSRF_ENABLED")
    app.config["TESTING"] = True
    # On, unlike every other fixture in the suite: a browser renders the
    # form, so it is the one client that can supply a token — and the one
    # that proves the templates still put one there.
    app.config["WTF_CSRF_ENABLED"] = True
    _db.init_db()

    port = _free_port()
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    for _ in range(40):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.1)
    else:                                   # pragma: no cover — bind failed
        server.shutdown()
        pytest.skip("the Flask test server did not start")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        app.config["WTF_CSRF_ENABLED"] = prior_csrf
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as pw:
        try:
            instance = pw.chromium.launch(headless=True)
        except PlaywrightError as exc:      # pragma: no cover — no binary
            pytest.skip(f"chromium unavailable: {exc}")
        try:
            yield instance
        finally:
            instance.close()


# ── The people ───────────────────────────────────────────────────────

class Actor:
    """One person, one browser context, one cookie jar.

    A context each rather than a page each: the multi-user path is about
    two sessions existing at once, and two pages sharing a context share
    the cookie that decides who they are.
    """

    def __init__(self, browser, base_url: str, email: str):
        self.base_url = base_url
        self.email = email
        self.context = browser.new_context(
            viewport={"width": 1280, "height": 900})
        self.page = self.context.new_page()

    def close(self) -> None:
        self.context.close()

    # — navigation —

    def go(self, path: str):
        return self.page.goto(f"{self.base_url}{path}", timeout=WAIT)

    def sign_in(self) -> "Actor":
        """Through the form, the only way a browser can.

        Asserted on arrival rather than assumed: a sign-in that silently
        failed would leave every later step measuring the login page, and
        "the text was not found" is a much worse error message than this
        one.
        """
        self.go("/auth/login?lang=en")
        self.page.fill('input[name="email"]', self.email)
        self.page.fill('input[name="password"]', PASSWORD)
        self.page.click('button[type="submit"]')
        self.page.wait_for_selector("text=Sign out", timeout=WAIT)
        return self

    # — reading —

    def text(self) -> str:
        return self.page.content()

    def sees(self, needle: str, timeout: int = WAIT) -> None:
        self.page.wait_for_selector(f"text={needle}", timeout=timeout)

    def does_not_see(self, needle: str) -> None:
        assert needle not in self.page.content(), \
            f"{needle!r} was on the page and should not have been"


@pytest.fixture
def team():
    """One organisation, an admin and a tester, both able to sign in."""
    tag = secrets.token_hex(4)
    org = _db.create_organization(f"Golden {tag}")
    people = {}
    for role in ("admin", "tester"):
        email = f"{role}-{tag}@golden.test"
        uid = _db.create_user(
            email, display_name=role.title(),
            password_hash=_auth.hash_password(PASSWORD, email=email),
            email_verified=True)
        _db.add_org_member(org, uid, "admin" if role == "admin" else "user")
        people[role] = {"id": uid, "email": email}
    return {"org": org, "tag": tag, **people}


@pytest.fixture
def editors_on():
    """Inline editing, for one test, then off again.

    Through ``os.environ`` for the same reason ``live_server`` uses it: the
    server runs in a thread of this process and reads the flags per call, so
    a monkeypatch would be invisible to the request-handling thread. Scoped
    to one test rather than added to ``live_server`` because turning the
    editors on changes what every page in this file renders, and the other
    fourteen tests describe the product with them off.
    """
    previous = {k: os.environ.get(k)
                for k in ("WORKSPACE_DB_FIRST", "EDITORS_ENABLED")}
    # Both: ``features._REQUIRES`` makes EDITORS_ENABLED a no-op without the
    # DB-first workspace, and a flag that is set but neutered renders a
    # read-only page that looks like a broken editor.
    os.environ["WORKSPACE_DB_FIRST"] = "1"
    os.environ["EDITORS_ENABLED"] = "1"
    try:
        yield True
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture
def actors(browser, live_server):
    """``actors(email)`` — a browser for that address, closed afterwards."""
    made: list[Actor] = []

    def _actor(email: str) -> Actor:
        person = Actor(browser, live_server, email)
        made.append(person)
        return person

    yield _actor
    for person in made:
        person.close()


# ── Helpers the paths share ──────────────────────────────────────────

#: Five rows, three sections. More than one section because the execution
#: page groups by it, and a single-section pack cannot tell a working
#: grouping from a missing one.
PACK_CSV = (
    "ID,Section,Summary,Preconditions,Steps,Test Data,Expected\n"
    "TC-001,Checkout,Verify that a card payment is accepted,"
    "A registered account,1. Open checkout,4111111111111111,"
    "The order is confirmed\n"
    "TC-002,Checkout,Verify that an expired card is refused,,"
    "1. Pay with an expired card,,The payment is refused\n"
    "TC-003,Login,Verify that a wrong password is refused,,"
    "1. Sign in badly,,An error is shown\n"
)


def _upload_pack(actor: Actor) -> None:
    """Put a pack of test cases in through the page's own upload form."""
    actor.go("/test-cases?lang=en")
    actor.page.set_input_files('input[name="upload_file"]', files=[{
        "name": "pack.csv", "mimeType": "text/csv",
        "buffer": PACK_CSV.encode()}])
    actor.page.click('form[action*="test-cases/upload"] button[type="submit"]')
    actor.page.wait_for_load_state("domcontentloaded", timeout=WAIT)


def _create_project(actor: Actor, name: str) -> str:
    actor.go("/?lang=en")
    actor.page.fill('input[name="project_name"]', name)
    actor.page.click('form[action*="projects/db/create"] button[type="submit"]')
    actor.sees("created and activated")
    return name


def _project_id(org: str, name: str) -> str:
    matches = [p["id"] for p in _db.list_projects(org_id=org)
               if p["name"] == name]
    assert matches, f"no project named {name!r} in the organisation"
    return matches[0]


# ── Path 1 — signing up ──────────────────────────────────────────────

class TestPathOneSignUp:
    """An invitation is the only way into this product.

    There is no open registration by design, so "sign-up" here means
    claiming a link — and the link is the credential, which is why the
    page is reachable without a session at all.
    """

    def test_an_invited_person_creates_an_account_and_joins(self, actors,
                                                            team):
        token = secrets.token_urlsafe(32)
        email = f"newcomer-{team['tag']}@golden.test"
        assert _db.create_invite(team["org"], email, "user", token,
                                 invited_by_user_id=team["admin"]["id"])

        newcomer = actors(email)
        newcomer.go(f"/auth/accept/{token}?lang=en")
        newcomer.page.fill('input[name="display_name"]', "The Newcomer")
        newcomer.page.fill('input[name="password"]', PASSWORD)
        newcomer.page.fill('input[name="password_confirm"]', PASSWORD)
        newcomer.page.click('button[type="submit"]')

        # The outcome a person would check: they are in, and they are in
        # *this* team.
        newcomer.sees("Sign out")
        user = _db.get_user_by_email(email)
        assert user, "the form reported success and created no account"
        assert _db.get_org_role(team["org"], user["id"]) == "user"

    def test_the_same_link_does_not_work_twice(self, actors, team):
        """The seat is one seat, checked where a person would try it."""
        token = secrets.token_urlsafe(32)
        email = f"replay-{team['tag']}@golden.test"
        _db.create_invite(team["org"], email, "user", token,
                          invited_by_user_id=team["admin"]["id"])

        first = actors(email)
        first.go(f"/auth/accept/{token}?lang=en")
        first.page.fill('input[name="display_name"]', "First")
        first.page.fill('input[name="password"]', PASSWORD)
        first.page.fill('input[name="password_confirm"]', PASSWORD)
        first.page.click('button[type="submit"]')
        first.sees("Sign out")

        second = actors(email)
        second.go(f"/auth/accept/{token}?lang=en")
        second.sees("no longer valid")

    def test_a_wrong_password_does_not_sign_anyone_in(self, actors, team):
        """So "signed in" above cannot mean "the page always says that"."""
        impostor = actors(team["tester"]["email"])
        impostor.go("/auth/login?lang=en")
        impostor.page.fill('input[name="email"]', team["tester"]["email"])
        impostor.page.fill('input[name="password"]', "not the password")
        impostor.page.click('button[type="submit"]')

        with pytest.raises(PlaywrightTimeout):
            impostor.page.wait_for_selector("text=Sign out", timeout=3_000)


# ── Path 2 — a project ───────────────────────────────────────────────

class TestPathTwoProject:
    def test_an_admin_creates_a_project_and_it_becomes_active(self, actors,
                                                              team):
        admin = actors(team["admin"]["email"]).sign_in()
        name = f"Golden Project {team['tag']}"

        _create_project(admin, name)

        # On the page and in the database, because "created and activated"
        # was on the page while the row was invisible to its own author
        # for the whole of E2.
        admin.sees(name)
        assert _project_id(team["org"], name)

    def test_a_tester_does_not_get_the_create_form(self, actors, team):
        """Offering an action the server will refuse is a worse first day
        than not offering it — and the two have to stay in step."""
        tester = actors(team["tester"]["email"]).sign_in()
        tester.go("/?lang=en")

        assert tester.page.locator('input[name="project_name"]').count() == 0


# ── Path 3 — test cases ──────────────────────────────────────────────

class TestPathThreeTestCases:
    def test_an_uploaded_pack_is_listed_on_the_page(self, actors, team):
        admin = actors(team["admin"]["email"]).sign_in()
        name = f"Pack {team['tag']}"
        _create_project(admin, name)

        _upload_pack(admin)

        admin.sees("Verify that a card payment is accepted")
        admin.sees("Verify that a wrong password is refused")
        stored = _db.load_test_cases(_project_id(team["org"], name))
        assert len(stored) == 3, [c["id"] for c in stored]

    def test_the_pack_belongs_to_the_project_it_was_uploaded_into(
            self, actors, team):
        """The isolation rule from E5, at the top of the pyramid.

        A run in project A rendering project B's content is the defect
        this programme spent longest on, and it looked like a normal walk
        every time.
        """
        admin = actors(team["admin"]["email"]).sign_in()
        first = _create_project(admin, f"Holder {team['tag']}")
        _upload_pack(admin)
        second = _create_project(admin, f"Empty {team['tag']}")

        admin.go("/test-cases?lang=en")
        admin.does_not_see("Verify that a card payment is accepted")
        assert _db.load_test_cases(_project_id(team["org"], second)) == []
        assert len(_db.load_test_cases(_project_id(team["org"], first))) == 3


# ── Path 4 — executing ───────────────────────────────────────────────

def _start_manual_walk(actor: Actor) -> None:
    """Pick the manual mode and press Run.

    No JavaScript involved: the radio posts with the form and the route
    307s to the walk, which is deliberate — the mode has to work with
    scripting off, and it means this step tests the server rather than
    the page's event handlers.
    """
    actor.go("/test-execution?lang=en")
    # The label, not the input: the radio-card pattern hides the control
    # and paints the card, so ``check()`` waits forever on an element that
    # is never visible. Clicking what a person clicks is also the only way
    # to notice if the card ever stops being wired to its input.
    manual = 'input[name="run_mode"][value="manual"]'
    actor.page.click(f'label:has({manual})')
    assert actor.page.locator(manual).is_checked(), \
        "the mode card did not select its own radio"
    actor.page.click("#te-run-button")
    actor.page.wait_for_load_state("domcontentloaded", timeout=WAIT)


def _record(actor: Actor, verdict: str, *, file_bug: bool = False) -> None:
    if file_bug:
        actor.page.check('input[name="file_bug"]')
    actor.page.click(f'button[name="verdict"][value="{verdict}"]')
    actor.page.wait_for_load_state("domcontentloaded", timeout=WAIT)


class TestPathFourExecute:
    def test_a_manual_walk_records_verdicts_and_reports_the_result(
            self, actors, team):
        admin = actors(team["admin"]["email"]).sign_in()
        name = f"Walk {team['tag']}"
        _create_project(admin, name)
        _upload_pack(admin)

        _start_manual_walk(admin)
        _record(admin, "Passed")
        _record(admin, "Failed")
        _record(admin, "Passed")

        project_id = _project_id(team["org"], name)
        runs = _db.list_execution_runs(project_id)
        assert runs, "the walk recorded no run at all"
        results = _db.list_case_results(runs[0]["id"])
        verdicts = sorted(r["status"] for r in results)
        assert verdicts == ["Failed", "Passed", "Passed"], verdicts

    def test_one_verdict_closes_one_item(self, actors, team):
        """E5's other long-lived defect: "2 of 2, finished" after one click.

        Asserted as a count rather than as a page state, because the page
        state was exactly what looked right while it was wrong.
        """
        admin = actors(team["admin"]["email"]).sign_in()
        name = f"OneAtATime {team['tag']}"
        _create_project(admin, name)
        _upload_pack(admin)

        _start_manual_walk(admin)
        _record(admin, "Passed")

        runs = _db.list_execution_runs(_project_id(team["org"], name))
        assert len(_db.list_case_results(runs[0]["id"])) == 1


# ── Path 5 — a bug, and the dashboard that counts it ─────────────────

class TestPathFiveBugAndDashboard:
    def test_a_failed_item_files_a_bug_the_dashboard_counts(self, actors,
                                                            team):
        admin = actors(team["admin"]["email"]).sign_in()
        name = f"Filing {team['tag']}"
        _create_project(admin, name)
        _upload_pack(admin)
        project_id = _project_id(team["org"], name)
        assert _db.list_bugs(project_id) == []

        _start_manual_walk(admin)
        _record(admin, "Failed", file_bug=True)

        bugs = _db.list_bugs(project_id)
        assert len(bugs) == 1, [b["title"] for b in bugs]

        admin.go("/bug-reports?lang=en")
        admin.sees(bugs[0]["title"])

        admin.go("/?lang=en")
        # The dashboard's own number, read off the rendered page rather
        # than recomputed — the open-runs card was absent for a whole
        # release because a NameError sat inside a best-effort `except`
        # and the page went on rendering perfectly.
        counted = admin.page.locator(
            "#metric_bug_severity .metric-big-number").inner_text()
        assert counted.strip() == "1", counted

    def test_the_bug_carries_the_item_it_came_from(self, actors, team):
        """A bug that does not name what failed is a bug nobody can act on."""
        admin = actors(team["admin"]["email"]).sign_in()
        name = f"Traced {team['tag']}"
        _create_project(admin, name)
        _upload_pack(admin)

        _start_manual_walk(admin)
        _record(admin, "Failed", file_bug=True)

        bug = _db.list_bugs(_project_id(team["org"], name))[0]
        haystack = " ".join(str(bug.get(k) or "") for k in
                            ("title", "steps_to_reproduce", "comment",
                             "related_case_id", "expected_result"))
        assert "TC-001" in haystack, bug


# ── Two people, one team ─────────────────────────────────────────────

class TestTwoPeopleInOneTeam:
    """The scenario the whole identity programme exists for.

    Everything above could be true of a single-user tool. These are the
    properties that only appear when two sessions exist at once, and they
    are the ones with no unit-test equivalent: a browser is the only
    client that has a cookie jar.
    """

    def test_each_sees_the_other_s_work_in_the_shared_project(self, actors,
                                                              team):
        admin = actors(team["admin"]["email"]).sign_in()
        name = f"Shared {team['tag']}"
        _create_project(admin, name)
        _upload_pack(admin)
        project_id = _project_id(team["org"], name)

        tester = actors(team["tester"]["email"]).sign_in()
        tester.page.click(f'form[action*="/projects/db/select/{project_id}"] '
                          f'button[type="submit"]')
        tester.page.wait_for_load_state("domcontentloaded", timeout=WAIT)
        tester.go("/test-cases?lang=en")
        tester.sees("Verify that a card payment is accepted")

        _start_manual_walk(tester)
        _record(tester, "Failed", file_bug=True)

        # The admin, in their own session, sees what the tester did.
        admin.go("/bug-reports?lang=en")
        filed = _db.list_bugs(project_id)
        assert len(filed) == 1
        admin.sees(filed[0]["title"])

    def test_a_conflict_keeps_the_losers_words_on_screen(self, actors, team,
                                                        editors_on):
        """E3.7 in a browser — the part with no server-side equivalent.

        ``tests/test_parallel_edits.py`` pins what the two requests do: the
        second is refused, the loser's text reaches no row, the provenance
        names the winner. None of that can see the three properties that
        only exist on a rendered page, and all three are deliberate choices
        in ``static/js/inline-edit.js``:

        * the editor **stays open**;
        * what the loser typed is **still in it** — somebody who has just
          written three sentences of test steps has to be able to copy them
          before reloading, and throwing them away to show a cleaner error
          would be a worse bug than the conflict;
        * a **Reload** button is offered, so "reload and make your change
          again" is something they can do rather than something they are
          told.

        Until now that behaviour was verified by hand during E4.2 — the
        component's own test file says so — and the conflict path is the
        one worth pinning, because it is the one where a mistake loses
        somebody's writing.
        """
        admin = actors(team["admin"]["email"]).sign_in()
        name = f"Contested {team['tag']}"
        _create_project(admin, name)
        _upload_pack(admin)
        project_id = _project_id(team["org"], name)

        tester = actors(team["tester"]["email"]).sign_in()
        tester.page.click(f'form[action*="/projects/db/select/{project_id}"] '
                          f'button[type="submit"]')
        tester.page.wait_for_load_state("domcontentloaded", timeout=WAIT)

        field = ('span.ie[data-ie-id="TC-001"][data-ie-field="suite"]'
                 ':not([data-ie-readonly])')
        for person in (admin, tester):
            person.go("/test-cases?lang=en")
            # Asserted rather than assumed: a field that rendered read-only
            # would make every step below time out on a selector, and "no
            # element found" is a far worse error than this sentence.
            person.page.wait_for_selector(field, timeout=WAIT)

        def _type(person, text: str):
            person.page.click(field)
            control = person.page.wait_for_selector(
                f"{field}.ie-editing input", timeout=WAIT)
            control.fill(text)
            return control

        # Both have the row open, so both hold the same row_version.
        admin_control = _type(admin, "Smoke")
        tester_control = _type(tester, "Regression")

        admin_control.press("Enter")
        admin.sees("Saved")

        tester_control.press("Enter")

        note = tester.page.wait_for_selector(
            ".ie-message.ie-message-error", timeout=WAIT)
        assert "reload" in (note.inner_text() or "").lower(), \
            note.inner_text()
        tester.page.wait_for_selector("button.ie-reload", timeout=WAIT)
        assert tester_control.input_value() == "Regression", (
            "the conflict discarded what the loser had typed, which is the "
            "one thing the component promises not to do")

        # And the winner's value is what the row actually holds.
        rows = {r["id"]: r for r in _db.load_test_cases(project_id)}
        assert rows["TC-001"]["suite"] == "Smoke"

    def test_a_member_of_another_team_cannot_reach_the_project(self, actors,
                                                               team):
        """Asserted on the page, because a 404 satisfies "does not see it".

        The negative was checked at the repository in E9.3; this is the
        other end of the same property, and the two together are what
        stops it passing for the wrong reason.
        """
        admin = actors(team["admin"]["email"]).sign_in()
        name = f"Private {team['tag']}"
        _create_project(admin, name)
        project_id = _project_id(team["org"], name)

        tag = secrets.token_hex(4)
        other_org = _db.create_organization(f"Outsiders {tag}")
        email = f"outsider-{tag}@golden.test"
        uid = _db.create_user(
            email, display_name="Outsider",
            password_hash=_auth.hash_password(PASSWORD, email=email),
            email_verified=True)
        _db.add_org_member(other_org, uid, "admin")

        outsider = actors(email).sign_in()
        outsider.go("/?lang=en")
        outsider.does_not_see(name)

        # Typed straight into the address bar, which is the whole attack:
        # not seeing a link is not the same as not being able to follow it.
        response = outsider.page.goto(
            f"{outsider.base_url}/load-project/{project_id}", timeout=WAIT)
        assert response is not None and response.status == 403, (
            f"another team's project answered "
            f"{response.status if response else 'nothing'} to a non-member")
        outsider.does_not_see(name)


# ── The harness ──────────────────────────────────────────────────────

class TestTheHarnessWouldNotice:
    def test_the_server_is_running_authenticated_with_csrf_on(self,
                                                              live_server,
                                                              actors):
        """Three conditions everything above depends on, in one place.

        Anonymous reaches the sign-in page and nothing else; a POST with no
        token is refused. If either stopped being true, most of this file
        would keep passing while checking nothing.
        """
        anonymous = actors("nobody@golden.test")
        anonymous.go("/?lang=en")
        assert "/auth/login" in anonymous.page.url, anonymous.page.url

        refused = anonymous.page.request.post(f"{live_server}/new-session")
        assert refused.status == 400, refused.status
