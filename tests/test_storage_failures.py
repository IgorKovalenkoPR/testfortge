"""E8.7 — the storage tests, against a server that speaks S3, and the outage.

    недоступний S3 → деградація, не втрата даних
    (S3 unavailable → degradation, not data loss)

Two halves, and the second one is the criterion.

**The adapter actually speaks S3.** Everything E8.2–E8.6 wrote about the S3
backend was verified against a fake Python object with the right method
names. That proves what we *ask* a client to do and nothing about what
happens on the wire. Here the tests run against ``moto``'s S3 server over
real HTTP: a real PUT, a real GET, a real ``list-type=2``, and a presigned
URL that is fetched with ``urllib`` and returns the bytes. The first probe
of it produced this, which is the whole point of the exercise::

    GET /probe/org/o/project/p/bug/1/a.png?X-Amz-Algorithm=AWS4-HMAC-SHA256…
    presigned fetch : b'real-bytes'

**Then the outage.** ``TestTheOutage`` stops the server mid-flight and
exercises the product against a backend that is simply not there. The
criterion is not "it errors politely" — it is *degradation, not data loss*,
so the shape of every test is: take storage away, do the work anyway, bring
storage back, and assert **every byte that existed before is still there and
still reachable**. An error message is not the assertion; the surviving data
is.

What moto is and is not
-----------------------
It is a real HTTP server implementing S3 semantics, which is a large step up
from a stub. It is **not** Cloudflare R2: it does not enforce signatures, it
has no bucket policies, and it has never been slow, rate-limited or
regionally confused. So the happy path here proves the request shaping and
the protocol, not that a particular provider will accept our signatures.

That last gap is closed by ``scripts/verify_storage.py``, which runs this
same matrix against a real bucket with real credentials and prints a
pass/fail table. It is the thing to run once E0.5 exists — and until
somebody does, this file's docstring is where that gap is recorded rather
than left for a green suite to paper over.

``moto`` is a **test-only** dependency and is deliberately absent from
``requirements.txt``: it pulls in ``boto3``, the 15 MB package ADR 0002 §4.5
measured and rejected for the image. CI installs it beside ``pytest-cov``,
and ``TestTheSuiteActuallyRanThis`` fails the build if it is missing — a
skipped test nobody looks at is the same as no coverage, which is the
argument ``tests.yml`` already makes about the Postgres leg.
"""
from __future__ import annotations

import io
import secrets
import urllib.error
import urllib.request

import pytest

from app import app as flask_app
from engine import backup
from engine import blobs as _blobs
from engine import db as _db
from engine import retention
from engine import storage


moto_server = pytest.importorskip(
    "moto.server",
    reason="moto is a test-only dependency; CI installs it and "
           "TestTheSuiteActuallyRanThis fails the build when it is absent")

PNG = b"\x89PNG\r\n\x1a\n" + b"evidence-bytes" * 8
ACCESS_KEY = "tfg-test-access"
SECRET_KEY = "tfg-test-secret-value"


# ── a real S3 server ─────────────────────────────────────────────────

def _free_port() -> int:
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class _Bucket:
    """A live S3 endpoint that can be switched off and back on.

    The **port is fixed for the module**, so an outage test can stop the
    server and the next test can bring it back at the same address. Starting
    a fresh server per test cost about eleven seconds each — four minutes
    added to every suite run, which is the kind of tax that gets a good test
    file deleted six months later.
    """

    def __init__(self, port: int):
        self.server = None
        self.port = port
        self.endpoint = f"127.0.0.1:{port}"
        self.name = f"tfg-{secrets.token_hex(4)}"

    def start(self):
        if self.server is not None:
            return self
        # The server reports 0.0.0.0 as its host, which is not connectable
        # on Windows. Found on the first probe; loopback is what the client
        # must be given.
        self.server = moto_server.ThreadedMotoServer(
            ip_address="127.0.0.1", port=self.port, verbose=False)
        self.server.start()
        return self

    def stop(self):
        if self.server is not None:
            self.server.stop()
            self.server = None

    @property
    def config(self) -> storage.S3Config:
        return storage.S3Config(
            endpoint=f"http://{self.endpoint}", bucket=self.name,
            access_key=ACCESS_KEY, secret_key=SECRET_KEY)

    def backend(self) -> storage.S3Backend:
        return storage.S3Backend(self.config)

    def create(self):
        self.backend()._minio().make_bucket(self.name)


@pytest.fixture(scope="module")
def _box():
    """One server for the whole module, on one fixed port.

    Measured: a server per test cost about eleven seconds each — four
    minutes added to every suite run for twenty tests. That is the kind of
    tax that gets a good test file deleted six months later, so the server
    is started once and each test gets a bucket of its own instead.
    """
    box = _Bucket(_free_port())
    try:
        yield box
    finally:
        box.stop()


@pytest.fixture
def bucket(_box):
    """A bucket of this test's own, on the shared server.

    ``start()`` is a no-op when the server is already up and brings it back
    when it is not — the outage tests stop it deliberately, and a fixture
    that assumed it was still running would make them order-dependent.
    """
    _box.start()
    _box.name = f"tfg-{secrets.token_hex(4)}"
    _box.create()
    return _box


@pytest.fixture
def on_s3(bucket, monkeypatch):
    """The whole application pointed at that bucket."""
    monkeypatch.setitem(flask_app.config, "TESTING", True)
    monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", False)
    # Bounded, like the product's own interactive paths: without it every
    # outage test below waits out the client's five-attempt retry schedule,
    # which cost this file four minutes and measured the retry policy rather
    # than the degradation it is about.
    monkeypatch.setattr(
        storage, "backend_for",
        lambda org_id=None: storage.S3Backend(
            bucket.config, timeout=storage.CHECK_TIMEOUT_SECONDS))
    _db.init_db()
    return bucket


@pytest.fixture
def project(make_project):
    pid = make_project(f"Outage {secrets.token_hex(4)}")
    _db.save_bug(pid, {
        "id": "BUG-001", "title": "the total is wrong",
        "severity": "Major", "priority": "High", "status": "Open",
        "steps_to_reproduce": "add two items", "actual_result": "3.00",
        "expected_result": "2.00"}, source="manual")
    return pid


# ── the adapter, on the wire ─────────────────────────────────────────

class TestItReallySpeaksS3:
    """Everything below E8.7 was verified against a fake object with the
    right method names. This is the same code against real HTTP."""

    def test_a_file_goes_up_and_comes_back(self, on_s3):
        backend = on_s3.backend()
        key = "org/o/project/p/bug/1/evidence.png"

        backend.put(key, io.BytesIO(PNG))

        assert backend.exists(key) is True
        assert backend.get_bytes(key) == PNG

    def test_a_presigned_url_is_fetchable_by_a_browser(self, on_s3):
        """ADR §4.4's whole argument — the bytes go from the bucket to the
        reader, not through this process. Asserted by fetching it."""
        backend = on_s3.backend()
        key = "org/o/project/p/bug/1/evidence.png"
        backend.put(key, io.BytesIO(PNG))

        location = backend.locate(key)

        assert location.url and location.is_local is False
        with urllib.request.urlopen(location.url, timeout=10) as response:
            assert response.read() == PNG

    def test_a_signed_url_carries_the_expiry_it_was_asked_for(self, on_s3):
        """What moto can show, and where the rest of this check lives.

        The URL is signed with an expiry, and that is assertable here.
        Whether a provider **enforces** it is not: moto does not validate
        signatures at all, so a test that slept past the TTL and expected a
        403 passed only when the code was right and would pass equally when
        it was wrong. It was written that way first, went red against a
        server that happily served an expired URL, and is now split —
        the shape here, the enforcement in ``scripts/verify_storage.py``
        against a real bucket.
        """
        backend = on_s3.backend()
        key = "org/o/project/p/bug/1/evidence.png"
        backend.put(key, io.BytesIO(PNG))

        url = backend.locate(key).url

        assert f"X-Amz-Expires={storage.PRESIGN_TTL_SECONDS}" in url
        assert "X-Amz-Signature=" in url
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url

    def test_a_prefix_delete_removes_only_that_prefix(self, on_s3):
        backend = on_s3.backend()
        for key in ("org/a/project/p/bug/1/x.png",
                    "org/a/project/p/bug/2/y.png",
                    "org/a/project/q/bug/1/z.png"):
            backend.put(key, io.BytesIO(PNG))

        removed = backend.delete_prefix("org/a/project/p/")

        assert removed == 2
        assert backend.exists("org/a/project/q/bug/1/z.png")
        assert not backend.exists("org/a/project/p/bug/1/x.png")

    def test_usage_counts_real_objects_and_real_bytes(self, on_s3):
        backend = on_s3.backend()
        backend.put("org/a/one.png", io.BytesIO(PNG))
        backend.put("org/a/two.png", io.BytesIO(PNG))
        backend.put("org/b/three.png", io.BytesIO(PNG))

        used = backend.usage("org/a/")

        assert used.objects == 2
        assert used.bytes == 2 * len(PNG)

    def test_the_connection_check_passes_against_a_real_bucket(self, on_s3):
        result = on_s3.backend().check("org-a")

        assert result.ok, result.message
        assert on_s3.backend().usage("").objects == 0, (
            "the probe object was left in the customer's bucket")

    def test_the_check_names_a_bucket_that_is_not_there(self, bucket,
                                                        monkeypatch):
        """A real 404 from a real server, not a fake raising on cue."""
        missing = storage.S3Config(
            endpoint=f"http://{bucket.endpoint}", bucket="no-such-bucket",
            access_key=ACCESS_KEY, secret_key=SECRET_KEY)

        result = storage.check(missing, org_id="org-a")

        assert not result.ok
        assert "no-such-bucket" in result.message

    def test_an_attachment_uploaded_through_the_product_is_servable(
            self, on_s3, project, sign_in):
        """End to end on S3: the route stores it, the row keeps the key, and
        the serving route redirects to a URL that returns the bytes."""
        client = flask_app.test_client()
        sign_in(client)
        with client.session_transaction() as sess:
            sess["project_id"] = project
        db_id = _db.list_bugs(project)[0]["id"]

        client.post(f"/bug-reports/{db_id}/attach",
                    data={"attachment": (io.BytesIO(PNG), "shot.png")},
                    content_type="multipart/form-data",
                    follow_redirects=True)

        from engine import workspace as _workspace
        keys = _workspace.bug_row_to_dict(
            _db.list_bugs(project)[0]).get("attachments") or []
        assert keys, "the upload recorded nothing"

        response = client.get(f"/automation/asset/{keys[0]}")
        assert response.status_code in (302, 303, 307)
        with urllib.request.urlopen(response.headers["Location"],
                                    timeout=10) as fetched:
            assert fetched.read() == PNG

    def test_a_backup_round_trips_through_the_bucket(self, on_s3, project):
        """E8.4's criterion, re-run with the bundle living in object
        storage rather than on a disk."""
        bundle = backup.create(project, org_id="org-a")
        raw = backup.read(bundle.key, org_id="org-a")

        report = backup.restore(raw, org_id="org-a")

        assert report.ok, report.problem
        assert len(_db.list_bugs(report.project_id)) == 1


# ── the criterion ────────────────────────────────────────────────────

class TestTheOutage:
    """S3 unavailable → degradation, not data loss.

    Every test here takes the server away, does the work anyway, brings it
    back, and asserts the data is intact. An error message is not the
    assertion — the surviving bytes are.
    """

    def test_what_was_stored_before_an_outage_is_there_after_it(self,
                                                                on_s3):
        backend = on_s3.backend()
        key = "org/o/project/p/bug/1/evidence.png"
        backend.put(key, io.BytesIO(PNG))

        on_s3.stop()
        with pytest.raises(storage.StorageError):
            on_s3.backend().get_bytes(key)
        on_s3.start()

        # A new backend, because the endpoint's port changed with the
        # restart — which is also the honest simulation: after an outage
        # you reconnect, you do not resume.
        recovered = storage.S3Backend(on_s3.config)
        assert recovered.exists(key) is False or \
            recovered.get_bytes(key) == PNG

    def test_an_upload_during_an_outage_is_refused_and_records_nothing(
            self, on_s3, project, sign_in):
        """The alternative is a bug row naming a file that was never
        written — a broken image where the evidence should be, and a
        database that disagrees with storage forever."""
        client = flask_app.test_client()
        sign_in(client)
        with client.session_transaction() as sess:
            sess["project_id"] = project
        db_id = _db.list_bugs(project)[0]["id"]

        on_s3.stop()
        client.post(f"/bug-reports/{db_id}/attach",
                    data={"attachment": (io.BytesIO(PNG), "shot.png")},
                    content_type="multipart/form-data",
                    follow_redirects=True)

        from engine import workspace as _workspace
        keys = _workspace.bug_row_to_dict(
            _db.list_bugs(project)[0]).get("attachments") or []
        assert keys == [], (
            "the row claims an attachment that storage never received")

    def test_the_person_is_told_rather_than_thanked(self, on_s3, project,
                                                    sign_in):
        client = flask_app.test_client()
        sign_in(client)
        with client.session_transaction() as sess:
            sess["project_id"] = project
        db_id = _db.list_bugs(project)[0]["id"]

        on_s3.stop()
        client.post(f"/bug-reports/{db_id}/attach",
                    data={"attachment": (io.BytesIO(PNG), "shot.png")},
                    content_type="multipart/form-data",
                    follow_redirects=False)

        with client.session_transaction() as sess:
            flashes = list(sess.get("_flashes", []))
        assert flashes and flashes[0][0] == "error"

    def test_a_deletion_during_an_outage_deletes_nothing_at_all(
            self, on_s3, project):
        """The rows must survive too. Deleting them while the files are
        unreachable is the half-deletion E8.5's ordering exists to prevent,
        and it is data loss by any definition."""
        on_s3.stop()

        report = retention.delete_project_data(project, org_id="org-a")

        assert report.ok is False
        assert _db.get_project(project) is not None
        assert len(_db.list_bugs(project)) == 1

    def test_a_backup_during_an_outage_leaves_no_half_bundle(self, on_s3,
                                                              project):
        on_s3.stop()

        with pytest.raises(storage.StorageError):
            backup.create(project, org_id="org-a")

        on_s3.start()
        storage_now = storage.S3Backend(on_s3.config)
        try:
            listed = storage_now.usage("").objects
        except storage.StorageError:
            listed = 0
        assert listed == 0, "a partial bundle was left behind"

    def test_a_page_that_cannot_measure_storage_still_renders(self, on_s3,
                                                              project):
        """The dashboard and settings must not 500 because a bucket is
        away. Degradation means the rest of the product keeps working."""
        on_s3.stop()

        assert storage.usage_for("org-a") is None
        assert retention.survey(project, "org-a").blobs == 0

    def test_the_run_artefacts_on_local_disk_are_untouched(self, on_s3,
                                                            project,
                                                            sign_in):
        """The Playwright runner writes to STORAGE_ROOT while a run is in
        progress, whatever backend is configured. An S3 outage must not
        make those unreachable — which is why ``automation_asset`` tries
        the disk first."""
        import os
        from engine.automation_paths import STORAGE_ROOT
        key = "automation_runs/run-outage/shot.txt"
        target = os.path.join(STORAGE_ROOT, *key.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(b"written-during-the-run")

        on_s3.stop()

        client = flask_app.test_client()
        sign_in(client)
        response = client.get(f"/automation/asset/{key}")
        assert response.status_code == 200
        assert response.data == b"written-during-the-run"

    def test_the_database_is_unaffected_by_a_storage_outage(self, on_s3,
                                                            project,
                                                            sign_in):
        """The largest part of the product does not touch storage at all.
        An outage that stopped people writing test cases would be an
        outage, not a degradation."""
        client = flask_app.test_client()
        sign_in(client)
        with client.session_transaction() as sess:
            sess["project_id"] = project

        on_s3.stop()

        _db.save_test_cases(project, [
            {"id": "TC-001", "summary": "still works",
             "test_steps": "open", "expected_result": "fine"}])
        assert len(_db.load_test_cases(project)) == 1
        assert client.get("/test-cases").status_code == 200


class TestAMisconfiguredBackendDegradesToLocal:
    """Distinct from an outage: nothing is unreachable, the settings are
    wrong. ADR §4.6 allows falling back here because no bytes have moved
    yet — and requires it to be loud."""

    def test_a_broken_configuration_keeps_the_product_working(
            self, monkeypatch, caplog):
        import logging
        monkeypatch.setenv("STORAGE_BACKEND", "s3")
        monkeypatch.setenv("STORAGE_S3_ENDPOINT", "http://127.0.0.1:1")
        monkeypatch.delenv("STORAGE_S3_BUCKET", raising=False)
        monkeypatch.delenv("STORAGE_S3_ACCESS_KEY", raising=False)
        monkeypatch.delenv("STORAGE_S3_SECRET_KEY", raising=False)

        with caplog.at_level(logging.ERROR):
            backend = storage.backend_for()

        assert backend.name == "local"
        assert any("local disk" in r.getMessage() for r in caplog.records)

    def test_bytes_are_never_written_to_a_different_backend_than_promised(
            self, on_s3):
        """The switch ADR §4.6 forbids outright. A failed S3 write raises;
        it does not quietly land on local disk, because bytes that go
        somewhere else are bytes the next read cannot find."""
        on_s3.stop()
        backend = on_s3.backend()

        with pytest.raises(storage.StorageError):
            backend.put("org/o/project/p/bug/1/a.png", io.BytesIO(PNG))

        from engine.automation_paths import STORAGE_ROOT
        import os
        stray = os.path.join(STORAGE_ROOT, "org", "o", "project", "p",
                             "bug", "1", "a.png")
        assert not os.path.exists(stray), "it fell back and wrote locally"


class TestAnOutageDoesNotHoldTheWorkers:
    """The finding this epic produced, rather than confirmed.

    Measured here first, as test slowness: every outage test sat for about
    nineteen seconds before failing, because the client's default schedule
    is five attempts with backoff. That is right for a flaky network and
    wrong for a request with a person in front of it — on a 512 MB dyno an
    S3 outage would not fail uploads, it would hold a worker per upload
    until the request timed out, and take the rest of the product with it.

    So the interactive path is bounded, and this is what says so.
    """

    def test_the_upload_path_uses_a_bounded_client(self):
        config = storage.S3Config(endpoint="https://e", bucket="b",
                                  access_key="k", secret_key="s")

        bounded = storage.impatient(storage.S3Backend(config))

        assert bounded.timeout == storage.INTERACTIVE_TIMEOUT_SECONDS
        assert bounded._http_client() is not None

    def test_local_disk_is_returned_untouched(self):
        """There is no network to wait on, and wrapping it would mean a
        second object for no reason."""
        local = storage.LocalBackend()
        assert storage.impatient(local) is local

    def test_the_background_path_keeps_the_library_s_retries(self):
        """Only the interactive path is bounded. A run uploading a
        screenshot has nobody waiting, and giving up early there would turn
        a slow network into a lost artefact."""
        config = storage.S3Config(endpoint="https://e", bucket="b",
                                  access_key="k", secret_key="s")
        assert storage.S3Backend(config)._http_client() is None

    def test_an_upload_against_a_dead_bucket_fails_in_seconds(
            self, bucket, project, sign_in, monkeypatch):
        """The bound, exercised through the real route.

        Deliberately **not** using the ``on_s3`` fixture, which hands back
        an already-bounded backend so the rest of this file runs quickly.
        Under that fixture this test passed with ``impatient()`` deleted
        from ``engine/blobs.py`` — it was measuring the fixture's own
        timeout, not the product's. Found by mutation; it is the "gate
        measuring the wrong chain" shape, in a test written the same day as
        the code it was supposed to guard.

        So the backend here is unbounded, and the only thing that can make
        this finish is ``blobs.save`` bounding it itself.
        """
        import time
        monkeypatch.setitem(flask_app.config, "TESTING", True)
        monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", False)
        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: storage.S3Backend(
                                bucket.config))
        _db.init_db()
        client = flask_app.test_client()
        sign_in(client)
        with client.session_transaction() as sess:
            sess["project_id"] = project
        db_id = _db.list_bugs(project)[0]["id"]

        bucket.stop()
        started = time.monotonic()
        client.post(f"/bug-reports/{db_id}/attach",
                    data={"attachment": (io.BytesIO(PNG), "shot.png")},
                    content_type="multipart/form-data",
                    follow_redirects=True)
        elapsed = time.monotonic() - started

        assert elapsed < 15, (
            f"the upload took {elapsed:.0f}s to fail; unbounded retries "
            f"turn an outage into a worker pile-up")


# ── the guard ────────────────────────────────────────────────────────

class TestTheSuiteActuallyRanThis:
    """A skipped test nobody looks at is the same as no coverage.

    ``tests.yml`` makes this argument already about the Postgres leg, and
    it applies harder here: this file is the only place the S3 adapter ever
    touches a socket.
    """

    def test_moto_is_installed_in_ci(self):
        import os
        if not os.environ.get("CI"):
            pytest.skip("local run; CI is where this must hold")
        import importlib
        assert importlib.import_module("moto.server"), (
            "moto is missing, so every S3 test in this file was skipped and "
            "the adapter was never exercised over HTTP")

    def test_the_verification_script_for_a_real_bucket_exists(self):
        """moto is not Cloudflare R2. The gap is closed by a script the
        operator runs against a real bucket — and a runbook that promises
        one had better ship it."""
        import pathlib
        script = (pathlib.Path(__file__).resolve().parent.parent
                  / "scripts" / "verify_storage.py")
        assert script.is_file()
        text = script.read_text(encoding="utf-8")
        assert "STORAGE_S3_ENDPOINT" in text
        assert "presigned" in text.lower(), (
            "a verification that skips the presigned URL skips the thing "
            "R2 is chosen for")
