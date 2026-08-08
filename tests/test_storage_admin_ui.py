"""E8.3 — the storage panel, and the sentence it has to earn.

    невірні креденшели → зрозуміла помилка на кроці перевірки
    (wrong credentials → a comprehensible error at the verification step)

That is the whole acceptance criterion, and it is unusually specific about
*where* the error appears: at a **verification step**, which means there has
to be one. So the largest class here is ``TestTheErrorTellsYouWhatToFix`` —
it walks the five ways an S3 configuration is wrong and asserts that each
produces a different sentence naming a different next move. A panel that
answered "storage error" to all five would satisfy a looser reading of the
criterion and be useless at three in the afternoon with a deploy waiting.

Two decisions worth knowing before reading the assertions.

**The check writes.** It puts a small object, reads it back and deletes it,
rather than asking whether the bucket exists. A key with `s3:ListBucket` and
nothing else passes every existence check ever written and then fails on the
first upload — hours later, in a different request, to a different person.
The credentials that matter are the ones an upload uses.

**Saving verifies, and refuses.** `storage.backend_for` degrades to local
disk when a configuration is unusable, and on the free plan local disk is
ephemeral. Storing a broken configuration would therefore mean a green
"Saved" over settings that quietly send this team's evidence to a disk that
is wiped on the next restart.

The provider is faked. What needs a real bucket is E8.7's, and a green test
against a bucket nobody contacted would be the thing this file exists to
prevent.
"""
from __future__ import annotations

import io
import secrets

import pytest

from app import app as flask_app
from engine import db as _db
from engine import features as _features
from engine import permissions as _perm
from engine import storage


# ── harness ──────────────────────────────────────────────────────────

class _S3Error(Exception):
    """Shaped like ``minio.error.S3Error``: it carries a ``code``."""

    def __init__(self, code: str, message: str = "refused"):
        super().__init__(message)
        self.code = code


class _Provider:
    """A bucket that can be told to fail in one specific way.

    ``fail`` is ``(step, exception)``: the step is one of
    ``storage.CHECK_STEPS`` and decides *when* it fires, so a key that can
    write but not delete is expressible — which is a real bucket policy and
    the reason `check` runs all three verbs.
    """

    def __init__(self, fail: tuple[str, Exception] | None = None):
        self.fail = fail
        self.objects: dict[str, bytes] = {}

    def _maybe(self, step):
        if self.fail and self.fail[0] == step:
            raise self.fail[1]

    def put_object(self, bucket, key, stream, length=-1, part_size=0):
        self._maybe("write")
        self.objects[key] = stream.read()

    def get_object(self, bucket, key):
        self._maybe("read")
        data = self.objects[key]
        return type("Resp", (), {
            "read": lambda self_inner: data,
            "close": lambda self_inner: None,
            "release_conn": lambda self_inner: None})()

    def stat_object(self, bucket, key):
        if key not in self.objects:
            raise _S3Error("NoSuchKey")
        return object()

    def presigned_get_object(self, bucket, key, expires=None):
        return f"https://signed.example/{bucket}/{key}"

    def list_objects(self, bucket, prefix="", recursive=False):
        self._maybe("list")
        for name, body in list(self.objects.items()):
            if name.startswith(prefix):
                yield type("Obj", (), {"object_name": name,
                                       "size": len(body)})()

    def remove_objects(self, bucket, targets):
        self._maybe("delete")
        for target in targets:
            self.objects.pop(getattr(target, "_name", None)
                             or getattr(target, "name", ""), None)
        return []


GOOD = {"endpoint": "https://acct.r2.example", "bucket": "team-evidence",
        "access_key": "AKIAEXAMPLE1234", "secret_key": "s3cret-value-here",
        "secure": "1"}


@pytest.fixture
def provider(monkeypatch):
    """Every ``S3Backend`` built during this test talks to one fake."""
    boxed: list[_Provider] = [_Provider()]

    def _minio(self):
        return boxed[0]

    monkeypatch.setattr(storage.S3Backend, "_minio", _minio)

    def _fails(step: str, exc: Exception):
        boxed[0] = _Provider(fail=(step, exc))
        return boxed[0]

    boxed[0].fails = _fails          # type: ignore[attr-defined]
    return type("Handle", (), {
        "current": property(lambda _self: boxed[0]),
        "fails": staticmethod(_fails)})()


@pytest.fixture
def admin(monkeypatch):
    """An org admin with a browser, on an instance that can encrypt."""
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    monkeypatch.setenv("STORAGE_BACKEND_CONFIGURABLE", "1")
    monkeypatch.setenv("TESTFORTGE_ENCRYPTION_KEY", "e" * 48)
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.setitem(flask_app.config, "TESTING", True)
    monkeypatch.setitem(flask_app.config, "WTF_CSRF_ENABLED", False)
    _db.init_db()

    org = _db.create_organization(f"Team {secrets.token_hex(3)}")
    uid = _db.create_user(f"admin-{secrets.token_hex(5)}@x.test",
                          email_verified=True)
    _db.add_org_member(org, uid, "admin")
    client = flask_app.test_client()
    with client.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = uid
        sess[_perm.SESSION_ORG_KEY] = org
    client.org_id = org              # type: ignore[attr-defined]
    client.user_id = uid             # type: ignore[attr-defined]
    return client


def _flashes(client) -> list[tuple[str, str]]:
    """What the redirect is carrying.

    Read from the session with ``follow_redirects=False``, because the
    settings page explains storage at length and a test that read the
    rendered page could pass on that prose while the flash said the
    opposite — the mistake this suite already made once.
    """
    with client.session_transaction() as sess:
        return list(sess.pop("_flashes", []))


def _post(client, path: str, **fields):
    response = client.post(path, data=fields, follow_redirects=False)
    return response, _flashes(client)


def _said(flashes) -> str:
    return " ".join(message for _category, message in flashes)


def _storage_card(client) -> str:
    """The Storage card only, whitespace-collapsed.

    Scoped rather than searched whole-page, because the capacity panel two
    cards up legitimately renders "0 B" (an estimate of this team's share of
    the database) and a test asserting "0 B" is absent would fail on
    somebody else's correct output. Collapsed because the template wraps its
    prose and a phrase split across two source lines is one sentence to a
    reader.
    """
    body = client.get("/org/settings").get_data(as_text=True)
    start = body.find("<h2>Storage</h2>")
    assert start > 0, "the Storage card is not on the page at all"
    end = body.find("<h2>", start + 10)
    return " ".join(body[start:end if end > 0 else None].split())


# ── the acceptance criterion ─────────────────────────────────────────

class TestTheErrorTellsYouWhatToFix:
    """Five wrong configurations, five different next moves.

    Written as one test per cause rather than a table, so a failure names
    the cause it is about.
    """

    def test_a_rejected_key_pair_points_at_the_credentials(self, admin,
                                                           provider):
        provider.fails("write", _S3Error("SignatureDoesNotMatch"))

        _r, flashes = _post(admin, "/org/settings/storage/test", **GOOD)

        said = _said(flashes).lower()
        assert "credential" in said or "secret" in said
        assert "access key" in said
        assert "bucket" not in said, (
            "sending an admin to check the bucket name over a bad secret "
            "is the failure this criterion is about")

    def test_a_missing_bucket_points_at_the_bucket(self, admin, provider):
        provider.fails("write", _S3Error("NoSuchBucket"))

        _r, flashes = _post(admin, "/org/settings/storage/test", **GOOD)

        said = _said(flashes)
        assert "team-evidence" in said, "the name it looked for is not shown"
        assert "credentials worked" in said.lower(), (
            "an admin needs to know the key is fine so they stop "
            "re-pasting it")

    def test_a_policy_without_write_names_the_permission(self, admin,
                                                         provider):
        provider.fails("write", _S3Error("AccessDenied"))

        _r, flashes = _post(admin, "/org/settings/storage/test", **GOOD)

        said = _said(flashes)
        assert "s3:PutObject" in said, (
            "'access denied' without the missing permission leaves an admin "
            "guessing which of six actions to grant")

    def test_a_policy_that_writes_but_cannot_delete_is_still_a_failure(
            self, admin, provider):
        """Caught only because the check does all three verbs. E8.5 deletes
        a project's data, and discovering the gap during a deletion request
        is too late."""
        provider.fails("delete", _S3Error("AccessDenied"))

        _r, flashes = _post(admin, "/org/settings/storage/test", **GOOD)

        said = _said(flashes)
        assert "s3:DeleteObject" in said
        assert "delete" in said.lower()

    def test_an_unreachable_host_points_at_the_address(self, admin,
                                                       provider):
        provider.fails("write", OSError("Failed to resolve 'acct.r2.example'"))

        _r, flashes = _post(admin, "/org/settings/storage/test", **GOOD)

        said = _said(flashes).lower()
        assert "reach" in said and "port" in said
        assert "credential" not in said

    def test_an_invalid_bucket_name_is_not_reported_as_a_network_problem(
            self, admin, provider):
        """The client validates bucket names before any request goes out, so
        the exception carries no S3 code. Found by smoke-testing this panel:
        a bucket called "B" produced "could not reach the endpoint", which
        sends an admin to check DNS over a typo three fields up."""
        provider.fails("write", ValueError("invalid bucket name B"))

        _r, flashes = _post(admin, "/org/settings/storage/test", **GOOD)

        said = _said(flashes).lower()
        assert "bucket name" in said
        assert "reach" not in said

    def test_a_clock_that_is_wrong_says_it_is_the_server(self, admin,
                                                         provider):
        """Every signature fails, and none of it is the admin's fault. A
        message about credentials here would have them rotating a working
        key."""
        provider.fails("write", _S3Error("RequestTimeTooSkewed"))

        _r, flashes = _post(admin, "/org/settings/storage/test", **GOOD)

        said = _said(flashes).lower()
        assert "clock" in said
        assert "server problem" in said

    def test_a_working_bucket_says_what_it_did(self, admin, provider):
        _r, flashes = _post(admin, "/org/settings/storage/test", **GOOD)

        assert flashes and flashes[0][0] == "success"
        said = _said(flashes).lower()
        assert "wrote" in said and "read it back" in said, (
            "'connection works' over a check that only listed the bucket is "
            "the claim this check exists to make true")

    def test_the_provider_s_own_wording_stays_in_the_log(self, admin,
                                                          provider, caplog):
        """``detail`` can carry a host, a bucket, or a request id. Useful in
        a log, wrong on a screen somebody can look over."""
        import logging
        provider.fails("write", _S3Error("AccessDenied", "arn:aws:s3:::secret-bucket/*"))

        with caplog.at_level(logging.WARNING):
            _r, flashes = _post(admin, "/org/settings/storage/test", **GOOD)

        assert "arn:aws" not in _said(flashes)
        assert any("arn:aws" in record.getMessage()
                   for record in caplog.records), "the detail was dropped"


# ── the check is a round trip ────────────────────────────────────────

class TestTheCheckActuallyWrites:

    def test_it_puts_reads_and_deletes(self, admin, provider):
        _post(admin, "/org/settings/storage/test", **GOOD)

        assert provider.current.objects == {}, (
            "the probe object was left behind — a check that litters the "
            "customer's bucket is a check they will ask us to stop running")

    def test_the_probe_lands_under_this_team_s_prefix(self, admin,
                                                      monkeypatch):
        """A bucket policy scoped to ``org/<id>/*`` is a sensible thing for
        an admin to write, and a probe at the bucket root would fail against
        a correctly-configured bucket."""
        seen: list[str] = []
        provider = _Provider()
        original = provider.put_object

        def _watch(bucket, key, stream, **kwargs):
            seen.append(key)
            return original(bucket, key, stream, **kwargs)

        provider.put_object = _watch
        monkeypatch.setattr(storage.S3Backend, "_minio",
                            lambda self: provider)

        _post(admin, "/org/settings/storage/test", **GOOD)

        assert seen and seen[0].startswith(
            storage.org_prefix(admin.org_id) + "/")
        assert storage.CHECK_SEGMENT in seen[0]

    def test_a_bucket_that_returns_different_bytes_is_a_failure(
            self, admin, monkeypatch):
        """Two deployments pointed at one bucket with one key scheme. Rare,
        and silent until someone opens a bug and sees another team's
        screenshot."""
        provider = _Provider()
        provider.get_object = lambda bucket, key: type("R", (), {
            "read": lambda s: b"somebody else's bytes",
            "close": lambda s: None, "release_conn": lambda s: None})()
        monkeypatch.setattr(storage.S3Backend, "_minio",
                            lambda self: provider)

        _r, flashes = _post(admin, "/org/settings/storage/test", **GOOD)

        assert flashes[0][0] == "error"
        assert "came back different" in _said(flashes)

    def test_it_is_bounded_in_time(self):
        """A person is watching. The default retry schedule against a
        firewalled host can outlive the dyno's own request timeout, and a
        check that hangs reports nothing."""
        backend = storage.S3Backend(
            storage.S3Config(endpoint="https://e", bucket="b",
                             access_key="k", secret_key="s"),
            timeout=storage.CHECK_TIMEOUT_SECONDS)
        assert backend._http_client() is not None
        assert storage.S3Backend(
            storage.S3Config(endpoint="https://e", bucket="b",
                             access_key="k", secret_key="s")
        )._http_client() is None, "uploads keep the library's own retries"


# ── saving ───────────────────────────────────────────────────────────

class TestSavingVerifiesFirst:

    def test_a_configuration_that_fails_is_not_stored(self, admin, provider):
        provider.fails("write", _S3Error("SignatureDoesNotMatch"))

        _r, flashes = _post(admin, "/org/settings/storage", **GOOD)

        assert storage.org_config(admin.org_id) is None, (
            "a broken configuration was stored; backend_for would fall back "
            "to the ephemeral disk while the page said the bucket was in use")
        assert "Nothing was saved" in _said(flashes)

    def test_a_working_configuration_is_stored_and_takes_effect(
            self, admin, provider):
        _r, flashes = _post(admin, "/org/settings/storage", **GOOD)

        assert flashes[0][0] == "success"
        stored = storage.org_config(admin.org_id)
        assert stored is not None and stored.bucket == "team-evidence"
        assert storage.backend_for(admin.org_id).name == "s3", (
            "saved, but the team's uploads still go to the default backend")

    def test_the_secret_is_encrypted_and_never_comes_back(self, admin,
                                                          provider):
        _post(admin, "/org/settings/storage", **GOOD)

        token = _db.get_org_secret(admin.org_id, storage.ORG_SECRET_NAME)
        assert token and "s3cret-value-here" not in token
        body = admin.get("/org/settings").get_data(as_text=True)
        assert "s3cret-value-here" not in body

    def test_an_incomplete_form_is_refused_before_any_request(self, admin,
                                                              provider):
        _r, flashes = _post(admin, "/org/settings/storage",
                            endpoint="https://acct.r2.example",
                            bucket="team-evidence")

        assert "required" in _said(flashes).lower()
        assert storage.org_config(admin.org_id) is None

    def test_the_audit_trail_records_it_without_the_key_pair(self, admin,
                                                             provider):
        _post(admin, "/org/settings/storage", **GOOD)

        entries = [e for e in _db.list_audit(org_id=admin.org_id)
                   if e.get("action") == "set_storage"]
        assert entries, "changing where every artefact goes was not audited"
        recorded = str(entries[0].get("diff") or {})
        assert "team-evidence" in recorded
        assert "s3cret-value-here" not in recorded
        assert "AKIAEXAMPLE1234" not in recorded, (
            "an audit trail is read by more people than the form is")


class TestKeepingTheStoredSecret:

    def test_a_blank_secret_reuses_the_stored_one(self, admin, provider):
        """Changing the bucket must not require re-pasting the secret. A
        form that demanded it would train an admin to keep the secret
        somewhere they can copy it from, which is the thing this page
        exists to avoid."""
        _post(admin, "/org/settings/storage", **GOOD)

        changed = dict(GOOD, bucket="team-evidence-2", secret_key="")
        _r, flashes = _post(admin, "/org/settings/storage", **changed)

        assert flashes[0][0] == "success"
        stored = storage.org_config(admin.org_id)
        assert stored.bucket == "team-evidence-2"
        assert stored.secret_key == "s3cret-value-here"

    def test_a_blank_secret_with_nothing_stored_is_refused(self, admin,
                                                           provider):
        _r, flashes = _post(admin, "/org/settings/storage",
                            **dict(GOOD, secret_key=""))

        assert "required" in _said(flashes).lower()
        assert storage.org_config(admin.org_id) is None


class TestGoingBackToTheDefault:

    def test_clearing_returns_the_team_to_the_instance_backend(self, admin,
                                                               provider):
        _post(admin, "/org/settings/storage", **GOOD)
        assert storage.backend_for(admin.org_id).name == "s3"

        _r, flashes = _post(admin, "/org/settings/storage/clear")

        assert storage.backend_for(admin.org_id).name == "local"
        assert flashes[0][0] == "success"

    def test_it_says_the_files_are_not_moved(self, admin, provider):
        """The surprising half. Nothing is deleted and nothing is migrated,
        so the pages that referenced those files stop finding them — and an
        admin who is not told will read the blank gallery as data loss."""
        _post(admin, "/org/settings/storage", **GOOD)

        _r, flashes = _post(admin, "/org/settings/storage/clear")

        said = _said(flashes).lower()
        assert "stay there" in said
        assert "no longer find" in said

    def test_clearing_nothing_says_so_rather_than_claiming_success(
            self, admin):
        _r, flashes = _post(admin, "/org/settings/storage/clear")
        assert flashes[0][0] == "info"


# ── the page ─────────────────────────────────────────────────────────

class TestThePage:

    def test_the_form_is_there_with_both_buttons(self, admin):
        body = " ".join(admin.get("/org/settings").get_data(
            as_text=True).split())

        assert "/org/settings/storage" in body
        assert "/org/settings/storage/test" in body
        assert "Test connection" in body

    def test_the_endpoint_field_accepts_a_scheme_less_host(self, admin):
        """``type=url`` would refuse ``minio.internal:9000`` in the browser
        — a client-side rule rejecting a value the server accepts, and the
        self-hosted case is exactly who types that."""
        body = admin.get("/org/settings").get_data(as_text=True)
        field = body[body.find('id="storage-endpoint"') - 40:]
        assert 'type="url"' not in field[:200]

    def test_an_explicit_scheme_beats_the_https_checkbox(self, admin,
                                                         provider):
        """An admin who types ``https://…`` and leaves the box unticked must
        not get plaintext on port 80.

        The browser omits an unchecked checkbox entirely, so "unticked" and
        "the field was never rendered" are the same submission — which makes
        the checkbox the *weaker* signal of the two. Found by smoke-testing
        the form, not by reading it: the request went out over HTTP while
        the field said https, and nothing anywhere said so.
        """
        _post(admin, "/org/settings/storage",
              **dict(GOOD, endpoint="https://acct.r2.example", secure=""))

        stored = storage.org_config(admin.org_id)
        assert stored.secure is True
        assert stored.url == "https://acct.r2.example"

    def test_a_scheme_less_endpoint_still_obeys_the_checkbox(self, admin,
                                                             provider):
        """Which is what the checkbox is for: a self-hosted MinIO on
        ``minio.internal:9000`` with no TLS in front of it."""
        _post(admin, "/org/settings/storage",
              **dict(GOOD, endpoint="minio.internal:9000", secure=""))

        stored = storage.org_config(admin.org_id)
        assert stored.secure is False
        assert stored.url == "http://minio.internal:9000"

    def test_the_secret_field_is_a_password_field(self, admin):
        body = admin.get("/org/settings").get_data(as_text=True)
        i = body.find('id="storage-secret-key"')
        assert i > 0 and 'type="password"' in body[i - 40:i]
        assert 'autocomplete="off"' in body[i:i + 200]

    def test_a_stored_configuration_comes_back_except_the_secret(
            self, admin, provider):
        _post(admin, "/org/settings/storage", **GOOD)

        body = admin.get("/org/settings").get_data(as_text=True)

        assert "team-evidence" in body
        assert "acct.r2.example" in body
        assert "…1234" in body, "no way to tell which key is stored"
        assert "s3cret-value-here" not in body

    def test_the_panel_is_absent_while_the_flag_is_off(self, admin,
                                                       monkeypatch):
        """ADR §6 gate 3. E8.7 has not run against a real bucket, so the
        choice is not offered yet — and a form behind a flag that nobody
        has tested is a form that produces support tickets."""
        monkeypatch.setenv("STORAGE_BACKEND_CONFIGURABLE", "0")

        body = admin.get("/org/settings").get_data(as_text=True)

        assert "Your own storage" not in body
        assert "not available on this instance yet" in " ".join(body.split())

    def test_the_routes_refuse_while_the_flag_is_off(self, admin,
                                                     monkeypatch, provider):
        """Hiding a form is not a control. The endpoint is still routable,
        and 'the button was not on the page' is the reasoning that ships
        unreachable-looking write paths."""
        monkeypatch.setenv("STORAGE_BACKEND_CONFIGURABLE", "0")

        _r, flashes = _post(admin, "/org/settings/storage", **GOOD)

        assert storage.org_config(admin.org_id) is None
        assert "not available" in _said(flashes).lower()

    def test_the_flag_is_still_off_by_default(self):
        """The gate itself, asserted where it is easy to find. E8.3 built
        what sits behind it; E8.7 is what turns it on."""
        assert _features.FLAGS["STORAGE_BACKEND_CONFIGURABLE"].default is False


class TestHowMuchIsInUse:

    def test_it_counts_this_team_s_files_only(self, admin, monkeypatch):
        mine = storage.org_prefix(admin.org_id)
        backend = storage.LocalBackend()
        backend.put(f"{mine}/project/p/bug/1/a.png", io.BytesIO(b"x" * 100))
        backend.put(f"{mine}/project/p/bug/1/b.png", io.BytesIO(b"y" * 50))
        backend.put("org/somebody-else/project/q/bug/1/c.png",
                    io.BytesIO(b"z" * 9999))

        card = _storage_card(admin)

        assert "2 files" in card
        assert "150 B" in card

    def test_a_failed_scan_is_not_rendered_as_zero(self, admin, monkeypatch):
        """"0 B" and "we could not measure" are different facts, and telling
        an admin the first when the second is true reads as data loss."""
        monkeypatch.setattr(storage.LocalBackend, "usage",
                            lambda self, prefix: (_ for _ in ()).throw(
                                storage.StorageError("scan failed")))

        card = _storage_card(admin)

        assert "could not be measured just now" in card
        assert "0 B" not in card

    def test_it_asks_for_this_team_s_prefix_and_not_the_bucket_root(
            self, admin, monkeypatch):
        """Named as its own property because the incidental coverage was
        not enough: a mutation replacing the prefix with ``""`` was caught
        only by a byte count in another test, which reports "150 B is
        wrong" and sends a reader hunting through a page renderer.

        On a shared bucket the wrong prefix is not a wrong number — it is
        one team being shown how much another team is storing.
        """
        asked: list[str] = []
        monkeypatch.setattr(storage.LocalBackend, "usage",
                            lambda self, prefix: asked.append(prefix)
                            or storage.Usage())

        admin.get("/org/settings")

        assert asked == [storage.org_prefix(admin.org_id) + "/"]

    def test_a_capped_scan_says_the_number_is_a_floor(self, monkeypatch,
                                                      tmp_path):
        """A silent cap turns "the first five thousand" into "the total"."""
        monkeypatch.setattr(storage, "USAGE_SCAN_LIMIT", 3)
        backend = storage.LocalBackend(root=str(tmp_path))
        for n in range(6):
            backend.put(f"org/o/p/{n}.txt", io.BytesIO(b"12345"))

        result = backend.usage("org/o/")

        assert result.truncated is True
        assert result.objects == 3
        assert result.human.startswith("at least ")


# ── who may do it ────────────────────────────────────────────────────

class TestOnlyAnAdmin:

    @pytest.fixture
    def member(self, admin):
        uid = _db.create_user(f"member-{secrets.token_hex(5)}@x.test",
                              email_verified=True)
        _db.add_org_member(admin.org_id, uid, "user")
        client = flask_app.test_client()
        with client.session_transaction() as sess:
            sess[_perm.SESSION_USER_KEY] = uid
            sess[_perm.SESSION_ORG_KEY] = admin.org_id
        return client

    def test_a_plain_member_cannot_change_where_files_go(self, member,
                                                          admin, provider):
        response = member.post("/org/settings/storage", data=GOOD,
                               follow_redirects=False)

        assert response.status_code in (302, 303, 403)
        assert storage.org_config(admin.org_id) is None

    def test_a_plain_member_cannot_run_the_check(self, member, provider):
        """It writes to the bucket and it is a network call the server makes
        on request — both reasons to keep it behind the role."""
        response = member.post("/org/settings/storage/test", data=GOOD,
                               follow_redirects=False)
        assert response.status_code in (302, 303, 403)

    def test_the_routes_are_in_the_fail_closed_table(self):
        """Self-enforcing through the decorator, and listed anyway: the
        table is where somebody looks to answer "who can redirect every
        upload this team makes"."""
        from engine import route_policy
        for endpoint in ("org_settings_storage", "org_settings_storage_test",
                         "org_settings_storage_clear"):
            assert route_policy.POLICY.get(endpoint) == "admin", endpoint
