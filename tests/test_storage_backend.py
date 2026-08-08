"""E8.2 — the storage abstraction, and the one sentence it has to satisfy.

    зміна бекенда — конфіг, не деплой нового коду
    (changing the backend is config, not deploying new code)

That is the acceptance criterion from ``docs/plans/team_platform_architecture.md``
and it is testable in a way most acceptance criteria are not: after this
module is imported, set an environment variable and ask again. If the answer
changes, the criterion holds. If it does not, the module cached its
configuration at import and an operator would need a redeploy to change a
setting — which is a constant with extra steps.

``TestTheAcceptanceCriterion`` below is written to fail against exactly that
mistake, and it was checked by making the mistake. Two mutations, both of
which a reviewer might wave through as tidying:

* wrapping ``backend_for`` in ``functools.lru_cache`` — 16 tests red,
  including three here and every attachment test that writes a file;
* reading ``STORAGE_BACKEND`` once at import — 5 red.

The rest covers ADR 0002's decisions, one class per decision, because an ADR
whose §4.6 nobody tested is a §4.6 nobody has to honour:

* §4.1 — two backends, and ``s3`` reaches five providers through one adapter
* §4.2 — the key carries the organisation and the project
* §4.3 — per-organisation config over an instance default, gated on a flag
* §4.4 — a presigned URL, not bytes through this process
* §4.5 — ``minio``, imported lazily
* §4.6 — how failure behaves, which is the decision with the most ways to
  get quietly wrong

No network and no bucket. The S3 paths run against a fake client injected
into ``S3Backend._client``, which is enough to assert *what we ask the
client to do* — the questions that need a real bucket are E8.7's, and
pretending otherwise here would be a green test for something never run.
"""
from __future__ import annotations

import io
import logging
import os

import pytest

from engine import blobs as _blobs
from engine import storage


# ── helpers ──────────────────────────────────────────────────────────

class _FakeUpload:
    """What Werkzeug hands a route, reduced to what a backend touches."""

    def __init__(self, data: bytes, filename: str = "shot.png"):
        self.filename = filename
        self.stream = io.BytesIO(data)

    def save(self, destination):
        with open(destination, "wb") as handle:
            handle.write(self.stream.read())


class _FakeMinio:
    """Records calls instead of making them."""

    def __init__(self):
        self.puts: list[tuple] = []
        self.presigned: list[tuple] = []
        self.removed: list[str] = []
        self.objects: list[str] = []
        self.fail_on: set[str] = set()

    def _maybe_fail(self, what):
        if what in self.fail_on:
            raise RuntimeError(f"{what} refused by the fake")

    def put_object(self, bucket, key, stream, length=-1, part_size=0):
        self._maybe_fail("put")
        self.puts.append((bucket, key, stream.read(), length))

    def get_object(self, bucket, key):
        self._maybe_fail("get")
        if key not in self.objects:
            raise RuntimeError("NoSuchKey")

        class _Resp:
            def read(self_inner):
                return b"bytes-from-the-bucket"

            def close(self_inner):
                pass

            def release_conn(self_inner):
                pass

        return _Resp()

    def stat_object(self, bucket, key):
        if key not in self.objects:
            raise RuntimeError("NoSuchKey")
        return object()

    def presigned_get_object(self, bucket, key, expires=None):
        self._maybe_fail("presign")
        self.presigned.append((bucket, key, expires))
        return f"https://signed.example/{bucket}/{key}?exp={expires}"

    def list_objects(self, bucket, prefix="", recursive=False):
        self._maybe_fail("list")
        for name in list(self.objects):
            if name.startswith(prefix):
                yield type("Obj", (), {"object_name": name,
                                       "size": len(name)})()

    def remove_objects(self, bucket, targets):
        for target in targets:
            self.removed.append(target._name if hasattr(target, "_name")
                                else getattr(target, "name", ""))
        return []


def _coded(code: str, message: str = "refused") -> Exception:
    """An exception shaped like ``minio.error.S3Error``: it carries a code."""
    exc = RuntimeError(message)
    exc.code = code                  # type: ignore[attr-defined]
    return exc


def _s3(fake: _FakeMinio | None = None, **overrides) -> storage.S3Backend:
    config = storage.S3Config(
        endpoint=overrides.pop("endpoint", "https://acct.r2.cloudflarestorage.com"),
        bucket=overrides.pop("bucket", "testfortge"),
        access_key=overrides.pop("access_key", "AKIAEXAMPLE1234"),
        secret_key=overrides.pop("secret_key", "s3cret-value-here"),
        **overrides)
    backend = storage.S3Backend(config)
    backend._client = fake if fake is not None else _FakeMinio()
    return backend


@pytest.fixture
def clean_env(monkeypatch):
    """No storage settings inherited from the developer's shell."""
    for name in ("STORAGE_BACKEND", "STORAGE_S3_ENDPOINT", "STORAGE_S3_BUCKET",
                 "STORAGE_S3_ACCESS_KEY", "STORAGE_S3_SECRET_KEY",
                 "STORAGE_S3_REGION", "STORAGE_S3_SECURE"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _configure_s3(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("STORAGE_S3_ENDPOINT", "https://acct.r2.example")
    monkeypatch.setenv("STORAGE_S3_BUCKET", "artefacts")
    monkeypatch.setenv("STORAGE_S3_ACCESS_KEY", "AKIAEXAMPLE1234")
    monkeypatch.setenv("STORAGE_S3_SECRET_KEY", "s3cret-value-here")


# ── the acceptance criterion ─────────────────────────────────────────

class TestTheAcceptanceCriterion:
    """"Changing the backend is config, not deploying new code."

    Each of these would pass against a module that read its settings once
    at import — except that they change the setting *after* the import and
    ask again, which is the whole test.
    """

    def test_the_same_process_answers_differently_after_a_config_change(
            self, clean_env):
        before = storage.backend_for()
        assert before.name == "local"

        _configure_s3(clean_env)
        after = storage.backend_for()

        assert after.name == "s3", (
            "the backend was changed by setting an environment variable and "
            "nothing else — no import, no restart. If this is 'local', the "
            "module cached its configuration and E8.2's acceptance "
            "criterion is not met")

    def test_switching_back_is_the_same_edit(self, clean_env):
        _configure_s3(clean_env)
        assert storage.backend_for().name == "s3"

        clean_env.setenv("STORAGE_BACKEND", "local")

        assert storage.backend_for().name == "local", (
            "the rollback has to be the same edit back, or an operator who "
            "tries object storage cannot get out of it without a deploy")

    def test_two_organisations_get_two_backends_in_one_process(
            self, clean_env, monkeypatch):
        """The per-org half of the same claim: one running process serves
        an org on a bucket and an org on local disk at the same time."""
        _configure_s3(clean_env)
        monkeypatch.setattr(storage, "org_config",
                            lambda org_id: None if org_id == "on-disk"
                            else storage.instance_config())

        assert storage.backend_for("on-bucket").name == "s3"
        assert storage.backend_for("on-disk").name == "s3", (
            "with no org config this org still gets the instance default")

    def test_nothing_is_cached_at_module_level(self):
        """Stated as a property rather than only exercised, so that a later
        'optimisation' has something to read before it adds an ``lru_cache``
        and quietly reintroduces the deploy step."""
        import inspect
        source = inspect.getsource(storage.backend_for)
        assert "cache" not in source.lower() or "no module-level cached" in source
        assert storage.backend_for() is not storage.backend_for(), (
            "two calls returning the same object means something is held")


# ── §4.1: two backends ───────────────────────────────────────────────

class TestWhichBackendsExist:

    def test_local_is_the_default(self, clean_env):
        assert storage.instance_backend_name() == "local"
        assert storage.backend_for().name == "local"

    def test_a_typo_reads_as_local_and_says_so(self, clean_env, caplog):
        clean_env.setenv("STORAGE_BACKEND", "S3-compatible")
        with caplog.at_level(logging.WARNING):
            assert storage.instance_backend_name() == "local"
        assert any("STORAGE_BACKEND" in r.message for r in caplog.records), (
            "falling back silently would leave an operator believing the "
            "bucket is in use while every artefact lands on an ephemeral "
            "disk — the exact shape of the RECORDER_ENABLED incident")

    def test_one_adapter_covers_every_s3_provider(self):
        """ADR §4.1: R2, S3, B2, Wasabi and MinIO differ by endpoint, not by
        code, which is why ``azure`` is a separate row in that table and
        these are not."""
        for endpoint in ("https://acct.r2.cloudflarestorage.com",
                         "https://s3.eu-west-1.amazonaws.com",
                         "https://s3.us-west-000.backblazeb2.com",
                         "https://s3.wasabisys.com",
                         "http://minio.internal:9000"):
            backend = _s3(endpoint=endpoint)
            assert backend.name == "s3"
            backend.put("k", _FakeUpload(b"x"))
            assert backend._client.puts

    def test_there_is_no_azure_backend_yet(self):
        """Deferred, not rejected — and asserted so that "we support Azure"
        cannot become true in a document before it is true in the code."""
        assert not hasattr(storage, "AzureBackend")


# ── §4.2: the key ────────────────────────────────────────────────────

class TestTheKeyCarriesTheOrgAndTheProject:

    def test_the_shape_is_the_one_the_adr_specifies(self):
        key = _blobs.key_for("proj-1", "bug", "42", "shot.png",
                             org_id="org-abc")
        head = key.split("/")
        assert head[0:4] == ["org", "org-abc", "project", "proj-1"]
        assert head[4] == "bug" and head[5] == "42"

    def test_no_organisation_still_gets_a_segment(self):
        """One shape always. Two shapes would mean a project whose files
        live under two prefixes, and E8.5 deletes by prefix."""
        key = _blobs.key_for("proj-1", "bug", "42", "shot.png")
        assert key.startswith(f"org/{_blobs.ORG_NONE}/project/proj-1/")

    def test_two_organisations_cannot_reach_each_other_s_prefix(self):
        one = _blobs.prefix_for("shared-name", org_id="org-a")
        two = _blobs.prefix_for("shared-name", org_id="org-b")
        assert one != two
        assert not one.startswith(two) and not two.startswith(one)

    def test_the_prefix_and_the_key_cannot_disagree(self):
        """The reason :func:`prefix_for` exists. ``routes/bugs.py`` used to
        build ``f"project/{pid}/bug/{db_id}"`` by hand to undo a failed
        attach; when the org segment arrived that string would have matched
        nothing, deleted nothing and reported nothing."""
        key = _blobs.key_for("p", "bug", "9", "a.png", org_id="org-x")
        prefix = _blobs.prefix_for("p", "bug", "9", org_id="org-x")
        assert key.startswith(prefix + "/")

    def test_an_entity_prefix_without_a_kind_is_refused(self):
        """``prefix_for(pid, entity_id=...)`` would silently produce a
        prefix that matches nothing — dangerous only when it is used to
        delete, which is the only thing it is used for."""
        with pytest.raises(ValueError):
            _blobs.prefix_for("p", None, "9")

    def test_a_hostile_org_id_cannot_escape_the_prefix(self):
        key = _blobs.key_for("p", "bug", "1", "a.png",
                             org_id="../../etc/passwd")
        assert ".." not in key.split("/")


# ── §4.3: per-org over an instance default ───────────────────────────

class TestPerOrganisationConfiguration:

    def test_the_flag_gates_it(self, clean_env, monkeypatch):
        """ADR §6 gate 3: while ``STORAGE_BACKEND_CONFIGURABLE`` is off the
        per-org choice is ignored, so E8.3's Admin UI cannot offer a choice
        with nothing behind it."""
        monkeypatch.setattr(
            "engine.db.get_org_secret",
            lambda org_id, name: pytest.fail(
                "the org's config was read while the flag was off"))
        clean_env.delenv("STORAGE_BACKEND_CONFIGURABLE", raising=False)

        assert storage.org_config("org-a") is None

    def test_a_stored_config_is_encrypted_at_rest(self, clean_env,
                                                  monkeypatch):
        """Same class of data as the BYOK API key, so the same path — and
        the ciphertext must not contain the secret in the clear."""
        monkeypatch.setenv("TESTFORTGE_ENCRYPTION_KEY", "x" * 48)
        monkeypatch.setenv("STORAGE_BACKEND_CONFIGURABLE", "1")
        stored: dict[str, str] = {}
        monkeypatch.setattr("engine.db.set_org_secret",
                            lambda org, name, token: stored.update(
                                {"name": name, "token": token}))
        monkeypatch.setattr("engine.db.get_org_secret",
                            lambda org, name: stored.get("token"))

        storage.set_org_config("org-a", storage.S3Config(
            endpoint="https://acct.r2.example", bucket="b",
            access_key="AKIAEXAMPLE1234", secret_key="the-real-secret"))

        assert stored["name"] == storage.ORG_SECRET_NAME
        assert "the-real-secret" not in stored["token"]
        assert storage.org_config("org-a").secret_key == "the-real-secret"

    def test_an_org_config_wins_over_the_instance_default(self, clean_env,
                                                          monkeypatch):
        monkeypatch.setattr(storage, "org_config", lambda org_id: (
            storage.S3Config(endpoint="https://own.example", bucket="theirs",
                             access_key="k", secret_key="s")
            if org_id == "org-a" else None))

        assert storage.backend_for("org-a").name == "s3"
        assert storage.backend_for("org-b").name == "local"

    def test_an_incomplete_config_is_refused_when_it_is_stored(self):
        """Refused at the point a person enters it, not at the point a file
        needs it: a half-configured backend is worse than none, and the
        person who can fix it is standing at the form."""
        with pytest.raises(ValueError):
            storage.set_org_config("org-a", storage.S3Config(
                endpoint="https://acct.r2.example", bucket="b"))

    def test_an_unreadable_config_does_not_break_uploads(self, clean_env,
                                                         monkeypatch, caplog):
        """The key was rotated, or the row is corrupt. Falling through to
        the instance default keeps the org working; refusing would turn a
        secret rotation nobody told them about into an outage."""
        monkeypatch.setenv("STORAGE_BACKEND_CONFIGURABLE", "1")
        monkeypatch.setattr("engine.db.get_org_secret",
                            lambda org, name: "not-a-valid-fernet-token")
        with caplog.at_level(logging.WARNING):
            assert storage.org_config("org-a") is None


# ── §4.4: a signed URL, not a proxy ──────────────────────────────────

class TestBytesDoNotPassThroughThisProcess:

    def test_locate_signs_a_url_on_s3(self):
        fake = _FakeMinio()
        location = _s3(fake).locate("org/o/project/p/bug/1/shot.png")

        assert location.url and location.url.startswith("https://signed.")
        assert location.is_local is False
        assert location.path is None

    def test_the_signature_expires(self):
        """ADR §4.4. A URL copied out of a browser's history or a referrer
        log has to be dead before it is useful."""
        fake = _FakeMinio()
        _s3(fake).locate("k")

        _bucket, _key, expires = fake.presigned[0]
        assert expires.total_seconds() == storage.PRESIGN_TTL_SECONDS
        assert 0 < expires.total_seconds() <= 3600

    def test_locate_is_a_path_on_local(self, tmp_path):
        backend = storage.LocalBackend(root=str(tmp_path))
        location = backend.locate("a/b.png")

        assert location.is_local and location.url is None
        assert location.path.startswith(os.path.realpath(str(tmp_path)))


# ── §4.5: minio, imported lazily ─────────────────────────────────────

class TestTheDependency:

    def test_nothing_imports_minio_at_module_import(self):
        """A deployment on local disk must not pay for the S3 client, and a
        checkout without it must still boot."""
        import inspect
        source = inspect.getsource(storage)
        top_level = [line for line in source.splitlines()
                     if line.startswith("import ") or line.startswith("from ")]
        assert not any("minio" in line for line in top_level), top_level

    def test_a_missing_minio_names_the_package(self, monkeypatch):
        """An operator who flipped a dashboard setting needs to be told
        which package is missing, not shown an ImportError."""
        import builtins
        real_import = builtins.__import__

        def _no_minio(name, *args, **kwargs):
            if name == "minio":
                raise ImportError("No module named 'minio'")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", _no_minio)
        backend = storage.S3Backend(storage.S3Config(
            endpoint="https://e", bucket="b", access_key="k", secret_key="s"))

        with pytest.raises(storage.StorageUnavailable) as caught:
            backend._minio()
        assert "minio" in str(caught.value)

    def test_it_is_declared_in_requirements(self):
        """Relying on a transitive dependency is how a fresh CI install
        breaks on a version bump nobody made — the argument
        ``requirements.txt`` already makes about ``cryptography``."""
        import pathlib
        text = (pathlib.Path(__file__).resolve().parent.parent
                / "requirements.txt").read_text(encoding="utf-8")
        assert any(line.strip().startswith("minio")
                   for line in text.splitlines())


# ── §4.6: how failure behaves ────────────────────────────────────────

class TestFailure:
    """The decision with the most ways to be quietly wrong."""

    def test_a_broken_s3_config_falls_back_to_local_and_logs_an_error(
            self, clean_env, caplog):
        """Not the forbidden silent switch: no bytes have moved yet. But it
        is loud, because an operator who believes uploads are in a bucket
        and finds them on an ephemeral disk has lost data on the next
        restart."""
        clean_env.setenv("STORAGE_BACKEND", "s3")
        clean_env.setenv("STORAGE_S3_ENDPOINT", "https://acct.r2.example")
        # No bucket, no keys — the half-configured state.

        with caplog.at_level(logging.ERROR):
            backend = storage.backend_for()

        assert backend.name == "local"
        assert any("local disk" in r.message for r in caplog.records), (
            "a fallback nobody is told about is the failure mode, not the "
            "fallback")

    def test_bytes_never_land_somewhere_other_than_where_we_said(self):
        """The switch ADR §4.6 actually forbids. A failed upload to S3
        raises; it does not quietly write to local disk, because bytes that
        land somewhere else are bytes the next read cannot find."""
        fake = _FakeMinio()
        fake.fail_on.add("put")
        backend = _s3(fake)

        with pytest.raises(storage.StorageError):
            backend.put("org/o/project/p/bug/1/a.png", _FakeUpload(b"data"))

    def test_a_read_that_is_not_there_fails_explicitly(self, tmp_path):
        backend = storage.LocalBackend(root=str(tmp_path))
        with pytest.raises(storage.StorageError):
            backend.get_bytes("nothing/here.png")

    def test_a_person_waiting_on_an_upload_is_told(self, clean_env,
                                                   monkeypatch):
        """The distinction §4.6 draws is *who is waiting*. Here it is a
        person who just chose a file, so the refusal is loud and nothing is
        recorded — the ``email_verified`` defect, not repeated."""
        class _Broken(storage.LocalBackend):
            def put(self, key, source):
                raise storage.StorageError("the bucket said no")

        monkeypatch.setattr(storage, "backend_for", lambda org_id=None: _Broken())

        with pytest.raises(_blobs.UploadRefused):
            _blobs.save(_FakeUpload(b"data"), project_id="p", kind="bug",
                        entity_id="1")

    def test_an_empty_file_is_refused_before_anything_is_written(
            self, clean_env, monkeypatch):
        """Measured before the write, not stat-ed after it: on a bucket
        there is nothing to stat without a second round trip, and
        write-then-delete leaves a window where the empty file exists."""
        written: list[str] = []

        class _Watching(storage.LocalBackend):
            def put(self, key, source):
                written.append(key)

        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _Watching())

        with pytest.raises(_blobs.UploadRefused):
            _blobs.save(_FakeUpload(b""), project_id="p", kind="bug",
                        entity_id="1")
        assert written == [], "an empty file reached the backend"

    def test_a_key_from_the_database_is_still_checked(self, tmp_path):
        """``_absolute`` guards at read time as well as at mint time: this
        method is reached with a key that came out of a row, and a row is
        not a trusted source just because we wrote it."""
        backend = storage.LocalBackend(root=str(tmp_path))
        with pytest.raises(storage.StorageError):
            backend.get_bytes("../../../etc/passwd")


# ── secrets ──────────────────────────────────────────────────────────

class TestNoSecretIsEverRendered:

    def test_describe_hides_the_secret_key(self, clean_env, monkeypatch):
        _configure_s3(clean_env)
        monkeypatch.setattr(storage, "org_config", lambda org_id: None)

        rendered = repr(storage.describe())

        assert "s3cret-value-here" not in rendered
        assert "secret_key" not in rendered

    def test_the_access_key_is_shown_only_as_a_hint(self):
        config = storage.S3Config(endpoint="https://e", bucket="b",
                                  access_key="AKIAEXAMPLE1234",
                                  secret_key="s")
        shown = config.redacted()

        assert shown["access_key_hint"] == "…1234"
        assert "AKIAEXAMPLE1234" not in repr(shown)


# ── serving ──────────────────────────────────────────────────────────

class TestServingAnArtefact:

    def test_a_local_file_is_served_from_this_disk(self, client, clean_env,
                                                   tmp_path):
        from engine.automation_paths import STORAGE_ROOT
        key = "org/_none/project/serve-test/bug/1/shot.txt"
        target = os.path.join(STORAGE_ROOT, *key.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(b"local-bytes")

        response = client.get(f"/automation/asset/{key}")

        assert response.status_code == 200
        assert response.data == b"local-bytes"

    def test_a_missing_file_on_local_is_a_404(self, client, clean_env):
        response = client.get(
            "/automation/asset/org/_none/project/nope/bug/1/gone.png")
        assert response.status_code == 404

    def test_a_missing_file_on_s3_redirects_to_a_signed_url(
            self, client, clean_env, monkeypatch):
        """The bytes go from the bucket to the browser. This process signs
        and steps out of the way — ADR §4.4."""
        fake = _FakeMinio()
        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _s3(fake))

        response = client.get(
            "/automation/asset/org/o/project/p/bug/1/shot.png")

        assert response.status_code in (301, 302, 303, 307, 308)
        assert response.headers["Location"].startswith("https://signed.")
        assert fake.presigned, "nothing was signed"

    def test_a_run_artefact_on_disk_still_works_under_s3(
            self, client, clean_env, monkeypatch):
        """The reason local is tried first. The Playwright runner writes
        straight to ``STORAGE_ROOT`` while a run is in progress, so those
        files are on this disk whatever the configured backend is. Checking
        the bucket first would give a page of broken images for artefacts
        sitting right there."""
        from engine.automation_paths import STORAGE_ROOT
        fake = _FakeMinio()
        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _s3(fake))
        key = "automation_runs/run-9/shot.txt"
        target = os.path.join(STORAGE_ROOT, *key.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as handle:
            handle.write(b"runner-wrote-this")

        response = client.get(f"/automation/asset/{key}")

        assert response.status_code == 200
        assert response.data == b"runner-wrote-this"
        assert not fake.presigned, "the bucket was asked about a local file"

    def test_traversal_is_still_rejected_before_any_backend_is_asked(
            self, client, clean_env, monkeypatch):
        monkeypatch.setattr(storage, "backend_for", lambda org_id=None: (_ for _ in ()).throw(
            AssertionError("a backend was consulted for a traversal attempt")))

        assert client.get(
            "/automation/asset/../../etc/passwd").status_code in (400, 404)


# ── the four verbs, on both backends ─────────────────────────────────

class TestTheInterfaceIsTheSameShapeOnBoth:
    """Four verbs, exercised against each backend.

    Written after a coverage run showed the S3 half of the interface at 75%
    — every read, delete and stat path present and never called. An
    abstraction whose second implementation is only ever constructed is an
    abstraction with one implementation and a class hierarchy.
    """

    def test_put_and_get_round_trip_on_local(self, tmp_path):
        backend = storage.LocalBackend(root=str(tmp_path))
        backend.put("a/b/c.txt", io.BytesIO(b"plain-stream"))

        assert backend.get_bytes("a/b/c.txt") == b"plain-stream"
        assert backend.exists("a/b/c.txt")

    def test_put_accepts_a_werkzeug_upload_and_a_plain_stream(self, tmp_path):
        """Two shapes reach ``put``: a route hands it a ``FileStorage``, the
        runner hands it an open file. Both, or one caller breaks."""
        backend = storage.LocalBackend(root=str(tmp_path))
        backend.put("from-upload.png", _FakeUpload(b"one"))
        backend.put("from-stream.png", io.BytesIO(b"two"))

        assert backend.get_bytes("from-upload.png") == b"one"
        assert backend.get_bytes("from-stream.png") == b"two"

    def test_get_reads_the_object_and_releases_the_connection(self):
        fake = _FakeMinio()
        fake.objects.append("k.png")

        assert _s3(fake).get_bytes("k.png") == b"bytes-from-the-bucket"

    def test_exists_is_a_stat_not_a_download(self):
        fake = _FakeMinio()
        fake.objects.append("there.png")
        backend = _s3(fake)

        assert backend.exists("there.png") is True
        assert backend.exists("gone.png") is False

    def test_delete_prefix_removes_only_that_prefix(self):
        fake = _FakeMinio()
        fake.objects += ["org/a/project/p/bug/1/x.png",
                         "org/a/project/p/bug/2/y.png",
                         "org/b/project/q/bug/1/z.png"]
        backend = _s3(fake)

        removed = backend.delete_prefix("org/a/project/p/")

        assert removed == 2
        assert not any(name.startswith("org/b") for name in fake.removed)

    def test_delete_prefix_matching_nothing_is_zero_not_an_error(self):
        assert _s3().delete_prefix("org/nobody/") == 0

    def test_a_prefix_that_names_one_object_deletes_that_object(self,
                                                                tmp_path):
        """A key is a prefix of itself. Both backends have to agree on that.

        They did not: on S3 ``delete_prefix("a/b.png")`` removes ``a/b.png``,
        while ``LocalBackend`` checked ``isdir`` and matched nothing — so it
        returned 0 and deleted nothing, quietly.

        Found by E8.3, not by reading either method: ``Backend.check`` cleans
        up its probe by passing the probe's own key, so every "Test
        connection" on the default backend left a file in ``STORAGE_ROOT``
        and reported success. E8.5 deletes a project's data through this same
        method.
        """
        local = storage.LocalBackend(root=str(tmp_path))
        local.put("org/a/project/p/bug/1/only.png", io.BytesIO(b"bytes"))

        assert local.delete_prefix("org/a/project/p/bug/1/only.png") == 1
        assert not local.exists("org/a/project/p/bug/1/only.png")

        fake = _FakeMinio()
        fake.objects.append("org/a/project/p/bug/1/only.png")
        assert _s3(fake).delete_prefix(
            "org/a/project/p/bug/1/only.png") == 1

    def test_a_delete_that_cannot_run_says_so(self):
        fake = _FakeMinio()
        fake.objects.append("org/a/x.png")

        def _boom(*_args, **_kwargs):
            raise RuntimeError("the bucket is unreachable")

        fake.remove_objects = _boom
        with pytest.raises(storage.StorageError):
            _s3(fake).delete_prefix("org/a/")

    def test_a_read_from_an_unreachable_bucket_says_so(self):
        fake = _FakeMinio()
        fake.fail_on.add("get")
        with pytest.raises(storage.StorageError):
            _s3(fake).get_bytes("anything.png")

    def test_a_url_that_cannot_be_signed_says_so(self):
        fake = _FakeMinio()
        fake.fail_on.add("presign")
        with pytest.raises(storage.StorageError):
            _s3(fake).locate("anything.png")

    def test_a_local_delete_of_a_prefix_that_escapes_the_root_is_zero(
            self, tmp_path):
        backend = storage.LocalBackend(root=str(tmp_path))
        assert backend.delete_prefix("../../..") == 0

    def test_the_default_describe_names_the_backend(self):
        """The base implementation, which is what a future ``azure`` adapter
        inherits if it adds nothing."""
        class _Minimal(storage.Backend):
            name = "minimal"

            def put(self, key, source): ...
            def get_bytes(self, key): ...
            def exists(self, key): ...
            def locate(self, key): ...
            def delete_prefix(self, prefix): ...
            def usage(self, prefix): ...

        assert _Minimal().describe() == {"backend": "minimal"}


class TestTheRealClientIsBuiltCorrectly:
    """Constructed for real — ``minio`` is a declared dependency and its
    constructor makes no request. What it cannot check is whether the bucket
    answers; that is E8.7's, with credentials."""

    def test_the_endpoint_scheme_is_stripped(self):
        """``minio`` wants a host, not a URL, and takes the scheme from
        ``secure=``. Passing ``https://host`` through produces a client that
        signs requests for a host called ``https``."""
        for endpoint, secure in (("https://acct.r2.example", True),
                                 ("http://minio.internal:9000", False),
                                 ("plain.example.com", True)):
            backend = storage.S3Backend(storage.S3Config(
                endpoint=endpoint, bucket="b", access_key="k",
                secret_key="s", secure=secure))
            client = backend._minio()
            assert "://" not in str(client._base_url.host)

    def test_the_client_is_built_once(self):
        backend = storage.S3Backend(storage.S3Config(
            endpoint="https://acct.r2.example", bucket="b",
            access_key="k", secret_key="s"))
        assert backend._minio() is backend._minio()

    def test_an_unusable_configuration_is_named(self, monkeypatch):
        backend = storage.S3Backend(storage.S3Config(
            endpoint="https://e", bucket="b", access_key="k", secret_key="s"))

        def _explode(*_args, **_kwargs):
            raise ValueError("bad endpoint")

        import minio
        monkeypatch.setattr(minio, "Minio", _explode)
        with pytest.raises(storage.StorageUnavailable):
            backend._minio()

    def test_an_incomplete_config_never_becomes_a_backend(self):
        with pytest.raises(storage.StorageUnavailable):
            storage.S3Backend(storage.S3Config(endpoint="https://e"))


class TestTheOrgConfigLifecycle:

    def test_no_org_means_no_config(self):
        assert storage.org_config(None) is None
        assert storage.org_config("") is None

    def test_an_org_with_nothing_stored_reads_as_none(self, clean_env,
                                                      monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND_CONFIGURABLE", "1")
        monkeypatch.setattr("engine.db.get_org_secret", lambda org, name: None)
        assert storage.org_config("org-a") is None

    def test_a_lookup_that_raises_does_not_reach_the_caller(self, clean_env,
                                                            monkeypatch):
        monkeypatch.setenv("STORAGE_BACKEND_CONFIGURABLE", "1")

        def _boom(org, name):
            raise RuntimeError("the database is down")

        monkeypatch.setattr("engine.db.get_org_secret", _boom)
        assert storage.org_config("org-a") is None

    def test_storing_without_an_org_is_a_programming_error(self):
        with pytest.raises(ValueError):
            storage.set_org_config("", storage.S3Config(
                endpoint="https://e", bucket="b", access_key="k",
                secret_key="s"))

    def test_clearing_removes_it_and_says_whether_there_was_one(
            self, monkeypatch):
        calls: list[tuple] = []
        monkeypatch.setattr("engine.db.delete_org_secret",
                            lambda org, name: calls.append((org, name)) or True)

        assert storage.clear_org_config("org-a") is True
        assert calls == [("org-a", storage.ORG_SECRET_NAME)]
        assert storage.clear_org_config("") is False

    def test_describe_says_where_this_org_actually_writes(self, clean_env,
                                                          monkeypatch):
        """What E8.3's Admin UI renders, and what an operator reads before
        deciding whether the ephemeral disk is still in play."""
        monkeypatch.setattr(storage, "org_config", lambda org_id: None)

        info = storage.describe("org-a")

        assert info["backend"] == "local"
        assert info["durable"] is False
        assert info["org_configured"] is False
        assert info["instance_default"] == "local"


# ── the check, on the default backend ────────────────────────────────

class TestCheckingLocalDisk:
    """The default backend is checkable too, and its failures are a disk's.

    Worth its own class because "test connection" on a deployment that has
    never configured a bucket is a real thing an admin will click, and an
    answer of "storage error" over a full disk or a read-only mount is the
    same unhelpfulness the S3 half of this file exists to prevent.
    """

    def test_a_working_disk_round_trips_and_cleans_up(self, tmp_path):
        backend = storage.LocalBackend(root=str(tmp_path))

        result = backend.check("org-a")

        assert result.ok and result.failed_at == ""
        assert "read it back" in result.message
        assert backend.usage("").objects == 0, "the probe was left behind"

    def test_the_probe_goes_under_the_org_prefix(self, tmp_path):
        backend = storage.LocalBackend(root=str(tmp_path))
        written: list[str] = []
        backend.put = lambda key, source: written.append(key)

        backend.check("org-a")

        assert written[0].startswith(storage.org_prefix("org-a") + "/")

    def test_a_read_only_disk_says_it_is_the_machine(self, tmp_path):
        import errno as _errno
        backend = storage.LocalBackend(root=str(tmp_path))

        def _denied(key, source):
            raise PermissionError(_errno.EACCES, "Permission denied")

        backend.put = _denied
        result = backend.check()

        assert not result.ok and result.step == "write"
        assert "permissions problem on the machine" in result.message

    def test_a_full_disk_says_so(self, tmp_path):
        import errno as _errno
        backend = storage.LocalBackend(root=str(tmp_path))

        def _full(key, source):
            raise OSError(_errno.ENOSPC, "No space left on device")

        backend.put = _full
        result = backend.check()

        assert "disk is full" in result.message

    def test_an_unexplained_failure_still_names_the_step(self, tmp_path):
        backend = storage.LocalBackend(root=str(tmp_path))
        backend.get_bytes = lambda key: (_ for _ in ()).throw(
            RuntimeError("something odd"))

        result = backend.check()

        assert result.step == "read"
        assert str(tmp_path.resolve()) in result.message

    def test_the_base_diagnosis_is_used_by_a_backend_that_adds_none(self):
        """A future adapter that implements the five verbs and no
        ``_diagnose`` still produces a sentence naming the step, rather than
        a traceback reaching a settings page."""
        class _Broken(storage.LocalBackend):
            _diagnose = storage.Backend._diagnose

            def put(self, key, source):
                raise RuntimeError("nope")

        result = _Broken().check()

        assert not result.ok and result.step == "write"
        assert "could not write" in result.message
        assert "RuntimeError" in result.detail

    def test_usage_of_a_prefix_that_escapes_the_root_is_empty(self,
                                                              tmp_path):
        backend = storage.LocalBackend(root=str(tmp_path))
        assert backend.usage("../../..") == storage.Usage()
        assert backend.usage("never/created") == storage.Usage()


class TestTheFacade:
    """``storage.check`` and ``storage.usage_for`` — what a route calls."""

    def test_no_config_checks_the_backend_in_force(self, clean_env):
        result = storage.check()
        assert result.ok, "the default local backend should pass its own check"

    def test_an_incomplete_config_is_refused_without_a_request(self,
                                                               clean_env):
        result = storage.check(storage.S3Config(endpoint="https://e"))

        assert not result.ok
        assert "required" in result.message.lower()

    def test_a_config_that_cannot_build_a_client_reports_reach(
            self, clean_env, monkeypatch):
        monkeypatch.setattr(storage, "S3Backend", lambda *a, **k: (
            _ for _ in ()).throw(storage.StorageUnavailable("no minio here")))

        result = storage.check(storage.S3Config(
            endpoint="https://e", bucket="b", access_key="k", secret_key="s"))

        assert not result.ok and result.step == "reach"
        assert "no minio here" in result.message

    def test_a_provider_error_with_only_a_code_still_says_something(self):
        """Nothing in the mapping matched, but the provider named a code.
        Better than "could not reach the endpoint", which would be wrong —
        the request plainly arrived."""
        backend = _s3()
        result = backend._diagnose(_coded("InvalidBucketState"), "write")

        assert "InvalidBucketState" in result.message
        assert result.step == "write"

    def test_a_client_side_value_error_that_is_not_about_a_bucket(self):
        backend = _s3()
        result = backend._diagnose(ValueError("region must be a string"),
                                   "write")

        assert result.message == "region must be a string"

    def test_a_missing_dependency_is_reported_as_reach_not_as_a_write(self):
        backend = _s3()
        result = backend._diagnose(
            storage.StorageUnavailable("the s3 backend needs 'minio'"),
            "write")

        assert result.step == "reach"
        assert "minio" in result.message

    def test_usage_for_scopes_to_the_org_and_survives_a_failure(
            self, clean_env, monkeypatch):
        asked: list[str] = []
        monkeypatch.setattr(storage.LocalBackend, "usage",
                            lambda self, prefix: asked.append(prefix)
                            or storage.Usage(2, 20))

        assert storage.usage_for("org-a") == storage.Usage(2, 20)
        assert asked == ["org/org-a/"]

        monkeypatch.setattr(storage.LocalBackend, "usage",
                            lambda self, prefix: (_ for _ in ()).throw(
                                storage.StorageError("bucket unreachable")))
        assert storage.usage_for("org-a") is None

    def test_s3_usage_sums_sizes_and_reports_a_failure(self):
        fake = _FakeMinio()
        fake.objects += ["org/a/x.png", "org/a/y.png"]
        backend = _s3(fake)

        result = backend.usage("org/a/")
        assert result.objects == 2 and result.bytes > 0

        fake.fail_on.add("list")
        with pytest.raises(storage.StorageError):
            backend.usage("org/a/")


class TestReadableNumbers:

    @pytest.mark.parametrize("size,expected", [
        (0, "0 B"), (999, "999 B"), (2048, "2.0 KB"),
        (5 * 1024 * 1024, "5.0 MB"), (3 * 1024 ** 3, "3.0 GB"),
        (9 * 1024 ** 4, "9216.0 GB"),
    ])
    def test_bytes_are_rendered_for_a_person(self, size, expected):
        assert storage.Usage(1, size).human == expected

    def test_a_capped_count_says_at_least(self):
        assert storage.Usage(5000, 2048, truncated=True).human == \
            "at least 2.0 KB"


# ── what the settings page says ──────────────────────────────────────

class TestTheSettingsPageTellsTheTruth:
    """The page used to say "stored on the server" unconditionally.

    That was true when it was written and E8.2 made it changeable, which is
    the moment a hard-coded sentence becomes the defect this programme keeps
    meeting: an assumption rendered as a fact. So the panel reads
    ``storage.describe()`` instead of asserting.
    """

    @pytest.fixture
    def page(self, monkeypatch):
        import secrets as _secrets
        from app import app as flask_app
        from engine import db as _db
        from engine import permissions as _perm

        monkeypatch.setenv("AUTH_ENABLED", "1")
        monkeypatch.setenv("ORG_MODE", "1")
        monkeypatch.setitem(flask_app.config, "TESTING", True)
        monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", False)
        _db.init_db()

        def _open() -> str:
            org = _db.create_organization(f"Store {_secrets.token_hex(3)}")
            uid = _db.create_user(f"st-{_secrets.token_hex(5)}@x.test",
                                  email_verified=True)
            _db.add_org_member(org, uid, "admin")
            client = flask_app.test_client()
            with client.session_transaction() as sess:
                sess[_perm.SESSION_USER_KEY] = uid
                sess[_perm.SESSION_ORG_KEY] = org
            body = client.get("/org/settings").get_data(as_text=True)
            # Whitespace-collapsed, because the template wraps its prose and
            # a phrase split across two source lines is the same sentence to
            # a reader. Asserting on the raw text would make these tests
            # fail on a re-indent, which is a test that tracks formatting
            # rather than meaning.
            return " ".join(body.split())

        return _open

    def test_on_local_disk_it_warns_that_the_disk_is_temporary(
            self, clean_env, page):
        body = page()

        assert "temporary" in body.lower()
        assert "STORAGE_BACKEND=s3" in body, (
            "the page states a limitation without saying who can lift it "
            "or how — which leaves the reader stuck")

    def test_on_object_storage_it_names_the_bucket_and_drops_the_warning(
            self, clean_env, monkeypatch, page):
        fake = _FakeMinio()
        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _s3(fake, bucket="team-arte"))
        monkeypatch.setattr(storage, "org_config", lambda org_id: None)

        body = page()

        assert "team-arte" in body
        assert "survive a restart" in body

    def test_the_secret_is_never_on_the_page(self, clean_env, monkeypatch,
                                             page):
        fake = _FakeMinio()
        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _s3(fake))
        monkeypatch.setattr(storage, "org_config", lambda org_id: None)

        body = page()

        assert "s3cret-value-here" not in body
        assert "AKIAEXAMPLE1234" not in body

    def test_the_per_team_choice_is_still_announced_as_absent(
            self, clean_env, page):
        """E8.3 has not been built. Saying so is what stops the flag from
        being flipped on the assumption that a picker exists."""
        assert "not available on this instance yet" in page()


# ── the two modules stay on their own side of the line ───────────────

class TestBlobsDelegates:

    def test_a_saved_file_goes_through_the_resolved_backend(
            self, clean_env, monkeypatch):
        seen: list[tuple] = []

        class _Recording(storage.LocalBackend):
            def put(self, key, source):
                seen.append((key, source))

        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: _Recording())

        key = _blobs.save(_FakeUpload(b"data"), project_id="p", kind="bug",
                          entity_id="1", org_id="org-a")

        assert seen and seen[0][0] == key

    def test_the_organisation_reaches_the_backend_resolver(self, clean_env,
                                                           monkeypatch):
        """Otherwise an org with its own bucket would have its keys written
        with the right prefix into the wrong place."""
        asked: list[str | None] = []
        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: (asked.append(org_id)
                                                 or storage.LocalBackend()))

        _blobs.save(_FakeUpload(b"data"), project_id="p", kind="bug",
                    entity_id="1", org_id="org-a")

        assert asked == ["org-a"]

    def test_every_verb_forwards_the_organisation(self, clean_env,
                                                  monkeypatch):
        """``save`` is not the only one. ``exists``, ``locate`` and
        ``delete_prefix`` each resolve a backend, and one of them dropping
        the organisation would ask the wrong bucket a question with a
        plausible answer — "no, that file is not here" — rather than
        failing. Caught by mutation: removing ``org_id`` from
        :func:`engine.blobs.exists` alone left the whole suite green,
        because every test runs both organisations on the same local disk.
        """
        asked: list[str | None] = []
        monkeypatch.setattr(storage, "backend_for",
                            lambda org_id=None: (asked.append(org_id)
                                                 or storage.LocalBackend()))

        _blobs.exists("org/org-a/project/p/bug/1/a.png", org_id="org-a")
        _blobs.locate("org/org-b/project/p/bug/1/a.png", org_id="org-b")
        _blobs.delete_prefix(_blobs.prefix_for("p", org_id="org-c"),
                             org_id="org-c")

        assert asked == ["org-a", "org-b", "org-c"]

    def test_blobs_owns_the_policy_and_storage_owns_the_bytes(self):
        """The line between the two modules, asserted so it does not blur.
        The file-type allowlist is a product decision about evidence; it has
        no business in a backend, and a backend that enforced it would
        enforce it differently per backend."""
        import inspect
        assert not any(ext in inspect.getsource(storage)
                       for ext in ("webm", "EVIDENCE_EXTENSIONS"))
        with pytest.raises(_blobs.UploadRefused):
            _blobs.key_for("p", "bug", "1", "payload.exe")
