"""TestForTge — where artefacts live, and how that becomes a config change (E8.2).

Implements [ADR 0002](../docs/plans/adr/0002-artifact-storage.md), accepted
2026-08-08. Requirement R3: a **pluggable blob store**, chosen per
organisation, with local disk as the default. Not a pluggable database —
Postgres stays Postgres, which is the owner's ruling in plan §5.1.

E8.2's acceptance criterion is one sentence and it shapes the whole module:

> changing the backend is **config, not deploying new code**.

So nothing here is decided at import. :func:`backend_for` resolves the
backend on every call, from the environment and from the organisation's own
settings, which means switching from local disk to R2 is a dashboard edit
and switching back is the same edit. A module that read its config once, at
import, would satisfy every test in this repo and fail that criterion — the
operator would have to redeploy to change a setting, and a setting nobody
can change without a deploy is a constant with extra steps.

Two backends, and why not four
------------------------------
`local` and `s3`. The S3 adapter speaks one protocol and reaches Cloudflare
R2 (the free-tier choice), AWS S3, Backblaze B2, Wasabi and MinIO — so
self-host (E8.6) needs no separate code. Azure Blob is deferred, not
rejected: it is a different API, and an adapter with no user and no
credentials to test against is a maintenance promise rather than coverage.
ADR §4.1, approved.

Which client, and why it is not boto3
-------------------------------------
Measured while implementing this, and it reversed the ADR's own proposal:
`boto3` + `botocore` is a 15.2 MB wheel, `minio` is 91 KB and brings ~1.9 MB
new to this project. Eight times smaller on the wheel and far more than that
unpacked, on a free dyno where image size costs deploy and cold-start time.
`minio` is a full S3-compatible client and does SigV4 itself, which is the
part ADR §4.5 insisted on not hand-rolling.

The dependency is imported **lazily**, inside the S3 backend, so a
deployment using local disk never pays for it and a checkout without it
still boots.

Failure is per operation, never a silent switch
-----------------------------------------------
ADR §4.6. A screenshot the runner could not upload degrades — kept locally
and reported. A file a person just chose fails loudly, because accepting it
and saying "saved" is an assumption recorded as a fact. Reads fail
explicitly. And nothing ever falls back to a *different* backend: bytes that
quietly land somewhere else are bytes the next read cannot find.
"""
from __future__ import annotations

import io
import json
import os
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass

from engine.automation_paths import STORAGE_ROOT
from engine.log import get_logger

log = get_logger(__name__)

#: Name under which an organisation's storage configuration is stored,
#: encrypted, in ``OrgSecret``.
#:
#: One encrypted blob rather than "endpoint in settings, keys in secrets":
#: the access key and its secret travel with the endpoint and bucket they
#: belong to, and a half-configured backend is a worse state than an
#: unconfigured one. Same class of data as the BYOK API key (E0.9), so it
#: takes the same path — see ``engine.llm_keys``.
ORG_SECRET_NAME = "storage_config"

#: How long a presigned GET stays valid, in seconds.
#:
#: Ten minutes. Long enough for a page of thumbnails to load on a slow
#: connection, short enough that a URL copied out of a browser's history or
#: a referrer log is dead before it is useful.
PRESIGN_TTL_SECONDS = 600

#: How many objects :func:`Backend.usage` will enumerate before it stops.
#:
#: There is no cheap "size of this prefix" in the S3 protocol — the answer is
#: a listing, and a listing over a large prefix is many round trips on a page
#: an admin opens casually. So it is capped, and the cap is **reported**
#: (:class:`Usage` carries ``truncated``) rather than silently turning a
#: partial sum into a total. A number that says "at least" is useful; a
#: number that quietly means "the first five thousand" is worse than none.
USAGE_SCAN_LIMIT = 5000

#: Last path segment before the throw-away object a connection check writes.
#:
#: The check writes under the **organisation's own prefix** on purpose: a
#: bucket policy scoped to ``org/<id>/*`` is a sensible thing for an admin to
#: write, and a check that probed the bucket root would fail against a
#: correctly-configured bucket — which is a check that reports a problem it
#: created.
CHECK_SEGMENT = "_storage-check"

#: Seconds a single connect or read may take during "Test connection".
#:
#: Bounded because a person is watching. A wrong port against a firewalled
#: host answers nothing at all, and the client's default retry schedule turns
#: that into a request that can outlive the dyno's own timeout — at which
#: point the admin gets a blank page instead of the diagnosis this feature
#: exists to give. Not applied to uploads, where retries are what a flaky
#: network needs.
CHECK_TIMEOUT_SECONDS = 5.0

#: The ``org`` segment for a caller who is not in an organisation.
#:
#: Lives here rather than in ``engine.blobs`` because both modules build the
#: same prefix — ``blobs`` for real keys, this one for the check probe — and
#: two spellings of ``_none`` would be two answers to one question. ``blobs``
#: re-exports it; :func:`org_prefix` is the only place either builds it.
ORG_NONE = "_none"


class StorageError(RuntimeError):
    """A storage operation failed. The message is safe to log, not to show."""


class StorageUnavailable(StorageError):
    """The backend is configured but cannot be reached or built."""


@dataclass(frozen=True)
class Usage:
    """How much of a prefix is in use.

    ``truncated`` is not a detail. See :data:`USAGE_SCAN_LIMIT`.
    """

    objects: int = 0
    bytes: int = 0
    truncated: bool = False

    @property
    def human(self) -> str:
        size = float(self.bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                shown = f"{size:.0f} {unit}" if unit == "B" \
                    else f"{size:.1f} {unit}"
                break
            size /= 1024
        return f"at least {shown}" if self.truncated else shown


#: The steps a connection check goes through, in order.
#:
#: Named because "it failed" is not an answer an admin can act on, and the
#: five failures below want five different next moves: fix the address, fix
#: the key, fix the bucket name, widen the policy, widen it further.
CHECK_STEPS = ("reach", "authenticate", "bucket", "write", "read", "delete")


@dataclass(frozen=True)
class CheckResult:
    """The outcome of "test this connection", written for a person.

    E8.3's acceptance criterion is one sentence — *wrong credentials produce
    a comprehensible error at the verification step* — so ``message`` is the
    product here, not a side effect. ``detail`` carries the underlying
    exception for the log and is never rendered.
    """

    ok: bool
    step: str = "reach"
    message: str = ""
    detail: str = ""

    @property
    def failed_at(self) -> str:
        return "" if self.ok else self.step


@dataclass(frozen=True)
class Location:
    """Where a caller should send a reader for one key.

    Exactly one of the two is set. The route decides what to do with it —
    this module does not build Flask responses, because it is also called
    from the runner and from the MCP service, neither of which has a request.
    """

    path: str | None = None
    url: str | None = None

    @property
    def is_local(self) -> bool:
        return self.path is not None


# ── The interface ────────────────────────────────────────────────────

class Backend(ABC):
    """What every backend must do. Six verbs, and each has a caller.

    Five abstract, one concrete. ``usage`` arrived with E8.3, which needed
    "how much is this team using" on the settings page; :meth:`check` is
    concrete because the round trip it performs is the same on every backend
    and only the *diagnosis* of a failure differs — which is
    :meth:`_diagnose`'s job.

    Still deliberately small. ``copy`` and ``move`` are absent because
    nothing needs them, and an interface with methods no caller uses is the
    shape this programme has met five times.
    """

    name: str = "?"

    @abstractmethod
    def put(self, key: str, source) -> None:
        """Store *source* (a file-like object) at *key*. Raises on failure."""

    @abstractmethod
    def get_bytes(self, key: str) -> bytes:
        """Read *key*. Raises :class:`StorageError` if it is not there."""

    @abstractmethod
    def exists(self, key: str) -> bool:
        ...

    @abstractmethod
    def locate(self, key: str) -> Location:
        """How a reader should fetch *key*."""

    @abstractmethod
    def delete_prefix(self, prefix: str) -> int:
        """Remove everything under *prefix*. Returns how many objects went.

        A prefix and not a list of keys, because that is the operation
        E8.5 ("delete this project's data") needs and the reason the key
        scheme starts with the organisation and the project — ADR §4.2.
        """

    @abstractmethod
    def usage(self, prefix: str) -> Usage:
        """How many objects live under *prefix*, and how many bytes.

        Capped at :data:`USAGE_SCAN_LIMIT`, and the cap is reported.
        """

    def describe(self) -> dict:
        """What an operator needs to see. Never includes a secret."""
        return {"backend": self.name}

    # ── "test this connection" ───────────────────────────────────────

    def check(self, org_id: str | None = None) -> CheckResult:
        """Write a small object, read it back, delete it. Report what broke.

        **A round trip and not a HEAD on the bucket**, which is the whole
        value of the method. A key with `s3:ListBucket` and nothing else
        passes any check built out of "can I see the bucket", and then every
        upload fails hours later, in a different request, to a different
        person. The credentials that matter are the ones an upload uses, so
        the check uses them.

        Runs in the order of :data:`CHECK_STEPS` and stops at the first
        failure, so ``step`` says which capability is missing rather than
        that "something went wrong".
        """
        import secrets as _secrets
        key = "/".join((org_prefix(org_id), CHECK_SEGMENT,
                        f"{_secrets.token_hex(8)}.txt"))
        payload = b"testfortge storage check"

        try:
            self.put(key, io.BytesIO(payload))
        except Exception as exc:
            return self._diagnose(exc, "write")
        try:
            if self.get_bytes(key) != payload:
                return CheckResult(
                    False, "read",
                    "The file was written but came back different. That "
                    "usually means the bucket name is shared with something "
                    "else writing to the same keys.")
        except Exception as exc:
            return self._diagnose(exc, "read")
        try:
            self.delete_prefix(key)
        except Exception as exc:
            # Reported rather than swallowed: without delete, E8.5 ("remove
            # this project's data") cannot do what it promises, and finding
            # that out during a deletion request is too late.
            return self._diagnose(exc, "delete")

        return CheckResult(True, "delete",
                           "Wrote a test file, read it back and removed it.")

    def _diagnose(self, exc: Exception, step: str) -> CheckResult:
        """Turn an exception into something an admin can act on."""
        return CheckResult(False, step,
                           f"The storage could not {step} a test file.",
                           detail=f"{type(exc).__name__}: {exc}")


# ── Local disk ───────────────────────────────────────────────────────

class LocalBackend(Backend):
    """Files under ``STORAGE_ROOT``. The default, and the whole product on
    a developer's checkout.

    On the free Render plan this disk is **ephemeral** — a restart takes it.
    That is not hidden: ``engine/capacity.py`` reports the restart count and
    ``tests/test_bug_attachments.py::TestTheLocalDiskLimitation`` states the
    consequence as a test.
    """

    name = "local"

    def __init__(self, root: str | None = None):
        self.root = os.path.realpath(root or STORAGE_ROOT)

    def _absolute(self, key: str) -> str:
        target = os.path.realpath(os.path.join(self.root, key))
        if not (target == self.root or target.startswith(self.root + os.sep)):
            # Checked here as well as where keys are minted: this method is
            # also reached with a key that came out of the database, and a
            # row is not a trusted source just because we wrote it.
            raise StorageError("that key escapes the storage root")
        return target

    def put(self, key: str, source) -> None:
        destination = self._absolute(key)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        if hasattr(source, "save"):          # a Werkzeug upload
            source.save(destination)
            return
        with open(destination, "wb") as handle:
            shutil.copyfileobj(source, handle)

    def get_bytes(self, key: str) -> bytes:
        try:
            with open(self._absolute(key), "rb") as handle:
                return handle.read()
        except OSError as exc:
            raise StorageError(f"cannot read {key}") from exc

    def exists(self, key: str) -> bool:
        try:
            return os.path.isfile(self._absolute(key))
        except StorageError:
            return False

    def locate(self, key: str) -> Location:
        return Location(path=self._absolute(key))

    def delete_prefix(self, prefix: str) -> int:
        try:
            root = self._absolute(prefix)
        except StorageError:
            return 0
        if os.path.isfile(root):
            # A prefix that names one object exactly. On S3 that deletes
            # that object, because a key is a prefix of itself; here it used
            # to match nothing, since a file is not a directory.
            #
            # Not a theoretical divergence: `Backend.check` deletes its
            # probe by passing the probe's own key, so every "Test
            # connection" on the default backend left a file behind and
            # reported success. Caught by asserting the disk was clean
            # afterwards, not by reading either method.
            try:
                os.remove(root)
                return 1
            except OSError as exc:      # pragma: no cover — best effort
                log.warning("could not delete %s: %s", prefix, exc)
                return 0
        if not os.path.isdir(root):
            return 0
        removed = 0
        for current, _dirs, files in os.walk(root, topdown=False):
            for name in files:
                try:
                    os.remove(os.path.join(current, name))
                    removed += 1
                except OSError as exc:      # pragma: no cover — best effort
                    log.warning("could not delete %s: %s", name, exc)
            try:
                os.rmdir(current)
            except OSError:
                pass
        return removed

    def usage(self, prefix: str) -> Usage:
        try:
            root = self._absolute(prefix)
        except StorageError:
            return Usage()
        if not os.path.isdir(root):
            return Usage()
        objects = total = 0
        for current, _dirs, files in os.walk(root):
            for name in files:
                if objects >= USAGE_SCAN_LIMIT:
                    return Usage(objects, total, truncated=True)
                try:
                    total += os.path.getsize(os.path.join(current, name))
                except OSError:          # pragma: no cover — vanished mid-walk
                    continue
                objects += 1
        return Usage(objects, total)

    def _diagnose(self, exc: Exception, step: str) -> CheckResult:
        """The default backend's failures are a disk's failures."""
        detail = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, StorageError) and exc.__cause__ is not None:
            exc = exc.__cause__
        errno = getattr(exc, "errno", None)
        import errno as _errno
        if errno == _errno.EACCES:
            return CheckResult(
                False, step,
                f"The server cannot write to {self.root} — a permissions "
                f"problem on the machine, not a configuration one.", detail)
        if errno == _errno.ENOSPC:
            return CheckResult(
                False, step,
                "The server's disk is full. Uploads will fail until space "
                "is freed, which on this plan usually means a restart.",
                detail)
        return CheckResult(
            False, step,
            f"The server could not {step} a test file under {self.root}.",
            detail)

    def describe(self) -> dict:
        return {"backend": self.name, "root": self.root, "durable": False}


# ── S3-compatible ────────────────────────────────────────────────────

@dataclass(frozen=True)
class S3Config:
    endpoint: str = ""
    bucket: str = ""
    access_key: str = ""
    secret_key: str = ""
    region: str = ""
    secure: bool = True

    def __post_init__(self):
        """Split an explicit scheme off the endpoint, and let it decide TLS.

        ``minio`` wants a host, not a URL, and takes the scheme from its
        ``secure=`` argument — so ``https://host`` with ``secure`` unticked
        connects in plaintext to port 80. Nothing warns; the admin typed
        "https" and believed it.

        Found by smoke-testing the settings form, where the checkbox is
        unticked by default in the browser's model of an absent value. An
        explicit scheme now wins, and the checkbox only decides for an
        endpoint given without one — which is the self-hosted MinIO case it
        exists for.

        Normalised here rather than in the client so that what is stored,
        what is displayed and what is used are the same three things.
        """
        raw = (self.endpoint or "").strip()
        for scheme, secure in (("https://", True), ("http://", False)):
            if raw.lower().startswith(scheme):
                object.__setattr__(self, "endpoint", raw[len(scheme):])
                object.__setattr__(self, "secure", secure)
                return
        object.__setattr__(self, "endpoint", raw)

    @property
    def url(self) -> str:
        """The endpoint as a person would write it, for a message or a page."""
        if not self.endpoint:
            return ""
        return f"{'https' if self.secure else 'http'}://{self.endpoint}"

    @property
    def complete(self) -> bool:
        return bool(self.endpoint and self.bucket and self.access_key
                    and self.secret_key)

    def redacted(self) -> dict:
        """Safe to render. The secret never travels back, for the reason
        ``routes/settings.py`` gives about the BYOK key: a credential that
        can be revealed is a credential that ends up in a screenshot."""
        return {
            "endpoint": self.url,
            "bucket": self.bucket,
            "region": self.region,
            "access_key_hint": (f"…{self.access_key[-4:]}"
                                if len(self.access_key) > 4 else "set"),
        }


class S3Backend(Backend):
    """Any S3-compatible bucket, through the ``minio`` client.

    One adapter, five providers — see the module docstring. The client is
    built per instance and the import is lazy, so a deployment on local disk
    never loads it.
    """

    name = "s3"

    def __init__(self, config: S3Config, *, timeout: float | None = None):
        """*timeout* bounds one interactive check (E8.3).

        Left ``None`` on the upload path, where the library's own retries
        are what a flaky network needs. Set for "Test connection", which is
        a person waiting on a page: a firewalled host answers nothing, and
        the default five retries with backoff turn "wrong port" into a
        request that outlives the dyno's own timeout. A check that hangs
        reports nothing, which is worse than a check that fails.
        """
        if not config.complete:
            raise StorageUnavailable(
                "the S3 backend needs an endpoint, a bucket and a key pair")
        self.config = config
        self.timeout = timeout
        self._client = None

    def _http_client(self):
        """A urllib3 pool that gives up quickly. ``None`` for the default."""
        if self.timeout is None:
            return None
        import urllib3
        return urllib3.PoolManager(
            timeout=urllib3.util.Timeout(connect=self.timeout,
                                         read=self.timeout),
            retries=urllib3.Retry(total=1, backoff_factor=0.2,
                                  status_forcelist=[500, 502, 503, 504]))

    def _minio(self):
        if self._client is not None:
            return self._client
        try:
            from minio import Minio
        except ImportError as exc:
            # Named rather than a bare ImportError: an operator who
            # switched the backend in a dashboard needs to be told which
            # package is missing, not shown a traceback.
            raise StorageUnavailable(
                "the s3 backend needs the 'minio' package installed"
            ) from exc
        # No scheme stripping here: S3Config.__post_init__ already did it,
        # and did it in the one place where `secure` can be corrected to
        # match. Two strippers would be two answers to one question.
        try:
            self._client = Minio(
                self.config.endpoint, access_key=self.config.access_key,
                secret_key=self.config.secret_key,
                secure=self.config.secure,
                region=self.config.region or None,
                http_client=self._http_client())
        except Exception as exc:
            raise StorageUnavailable(f"could not build an S3 client: {exc}") \
                from exc
        return self._client

    def put(self, key: str, source) -> None:
        stream = getattr(source, "stream", source)
        try:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(0)
        except Exception:                    # pragma: no cover — odd stream
            size = -1
        try:
            self._minio().put_object(self.config.bucket, key, stream,
                                     length=size,
                                     part_size=10 * 1024 * 1024)
        except Exception as exc:
            raise StorageError(f"upload of {key} failed: {exc}") from exc

    def get_bytes(self, key: str) -> bytes:
        response = None
        try:
            response = self._minio().get_object(self.config.bucket, key)
            return response.read()
        except Exception as exc:
            raise StorageError(f"cannot read {key}: {exc}") from exc
        finally:
            if response is not None:
                try:
                    response.close()
                    response.release_conn()
                except Exception:            # pragma: no cover
                    pass

    def exists(self, key: str) -> bool:
        try:
            self._minio().stat_object(self.config.bucket, key)
            return True
        except Exception:
            return False

    def locate(self, key: str) -> Location:
        """A presigned GET, not a proxy through this process.

        ADR §4.4: R2's egress is free only while the bytes do not pass
        through us, and a 512 MB dyno that also sleeps is the wrong thing to
        put in front of every thumbnail.
        """
        from datetime import timedelta
        try:
            url = self._minio().presigned_get_object(
                self.config.bucket, key,
                expires=timedelta(seconds=PRESIGN_TTL_SECONDS))
        except Exception as exc:
            raise StorageError(f"cannot sign a URL for {key}: {exc}") from exc
        return Location(url=url)

    def delete_prefix(self, prefix: str) -> int:
        client = self._minio()
        removed = 0
        try:
            from minio.deleteobjects import DeleteObject
            names = [DeleteObject(obj.object_name) for obj in
                     client.list_objects(self.config.bucket, prefix=prefix,
                                         recursive=True)]
            if not names:
                return 0
            for error in client.remove_objects(self.config.bucket, names):
                log.warning("could not delete %s: %s", error.name, error)
            removed = len(names)
        except Exception as exc:
            raise StorageError(f"cannot clear {prefix}: {exc}") from exc
        return removed

    def usage(self, prefix: str) -> Usage:
        objects = total = 0
        try:
            for obj in self._minio().list_objects(
                    self.config.bucket, prefix=prefix, recursive=True):
                if objects >= USAGE_SCAN_LIMIT:
                    return Usage(objects, total, truncated=True)
                total += int(obj.size or 0)
                objects += 1
        except Exception as exc:
            # Not an error the caller can do anything with — this is a
            # number on a settings page, not a decision. Logged, and the
            # page shows nothing rather than a zero, which would read as
            # "you are using no storage".
            log.warning("could not size %s: %s", prefix, exc)
            raise StorageError(f"cannot size {prefix}: {exc}") from exc
        return Usage(objects, total)

    def _diagnose(self, exc: Exception, step: str) -> CheckResult:
        """Map a provider error onto a sentence and a next move.

        This method **is** E8.3's acceptance criterion. "Wrong credentials
        produce a comprehensible error at the verification step" is not
        satisfied by surfacing ``S3Error: An error occurred (403)`` — an
        admin reading that cannot tell a mistyped secret from a bucket
        policy that omits ``s3:PutObject``, and those have different fixes.
        """
        detail = f"{type(exc).__name__}: {exc}"
        # The four verbs wrap everything in StorageError so callers have one
        # thing to catch. That is right for them and wrong here: the code
        # this method dispatches on lives on the provider's exception, which
        # is now the ``__cause__``. Unwrapped rather than un-wrapped at the
        # source, because a route catching a bare S3Error would be worse.
        if isinstance(exc, StorageError) and exc.__cause__ is not None:
            exc = exc.__cause__
        code = str(getattr(exc, "code", "") or "")
        endpoint = self.config.url or "the endpoint"
        bucket = self.config.bucket or "the bucket"

        if code in ("InvalidAccessKeyId", "SignatureDoesNotMatch",
                    "InvalidSecurity", "AuthorizationHeaderMalformed"):
            return CheckResult(
                False, "authenticate",
                f"{endpoint} rejected the credentials. Check the access key "
                f"and the secret — a secret is easy to truncate on copy, and "
                f"most providers show it only once.", detail)
        if code in ("NoSuchBucket", "NoSuchKey"):
            return CheckResult(
                False, "bucket",
                f"The credentials worked, but there is no bucket called "
                f"'{bucket}' at {endpoint}. Check the name and the account "
                f"the key belongs to.", detail)
        if code in ("AccessDenied", "AllAccessDisabled"):
            missing = {"write": "s3:PutObject", "read": "s3:GetObject",
                       "delete": "s3:DeleteObject"}.get(step, "access")
            return CheckResult(
                False, step,
                f"The credentials are valid and '{bucket}' exists, but this "
                f"key is not allowed to {step}. Grant it {missing} on "
                f"'{bucket}'.", detail)
        if code in ("RequestTimeTooSkewed",):
            return CheckResult(
                False, "authenticate",
                "The server's clock is too far from the storage provider's, "
                "so every signature is rejected. This is a server problem, "
                "not a credentials problem.", detail)
        if isinstance(exc, StorageUnavailable):
            return CheckResult(False, "reach", str(exc), detail)
        if code:
            return CheckResult(
                False, step,
                f"{endpoint} refused the request ({code}).", detail)
        if isinstance(exc, ValueError):
            # The client rejected the input before any request went out —
            # `minio` validates bucket names itself. Found by smoke-testing
            # this method: a bucket called "b" produced "could not reach
            # the endpoint", which sends an admin to check DNS over a typo
            # in a field three rows up.
            text = str(exc)
            if "bucket" in text.lower():
                return CheckResult(
                    False, "bucket",
                    f"'{bucket}' is not a valid bucket name ({text}). Bucket "
                    f"names are 3–63 characters, lower case, and may not "
                    f"look like an IP address.", detail)
            return CheckResult(False, step, text, detail)
        # No S3 error code at all: the request never reached a bucket. DNS,
        # a wrong port, http where https was meant, a firewall.
        return CheckResult(
            False, "reach",
            f"Could not reach {endpoint}. Check the address, the port, and "
            f"whether it should be http rather than https.", detail)

    def describe(self) -> dict:
        return {"backend": self.name, "durable": True,
                **self.config.redacted()}


# ── Keys ──────────────────────────────────────────────────────────────

def org_prefix(org_id: str | None) -> str:
    """``org/<org_id>`` — the first two segments of every key (ADR §4.2).

    Always two segments, never zero: see ``engine.blobs``'s docstring on why
    a conditional org segment would put one project's files under two
    prefixes, and E8.5 deletes by prefix.
    """
    import re
    if not org_id:
        return f"org/{ORG_NONE}"
    cleaned = re.sub(r"[^A-Za-z0-9_\-]", "-", str(org_id).strip())[:80]
    return f"org/{cleaned or ORG_NONE}"


# ── Resolving which one to use, per call ──────────────────────────────

def instance_config() -> S3Config:
    """The instance-wide S3 settings, read from the environment each call."""
    return S3Config(
        endpoint=(os.environ.get("STORAGE_S3_ENDPOINT") or "").strip(),
        bucket=(os.environ.get("STORAGE_S3_BUCKET") or "").strip(),
        access_key=(os.environ.get("STORAGE_S3_ACCESS_KEY") or "").strip(),
        secret_key=(os.environ.get("STORAGE_S3_SECRET_KEY") or "").strip(),
        region=(os.environ.get("STORAGE_S3_REGION") or "").strip(),
        secure=(os.environ.get("STORAGE_S3_SECURE", "1").strip().lower()
                not in {"0", "false", "no", "off"}),
    )


def instance_backend_name() -> str:
    """``local`` or ``s3``, from ``STORAGE_BACKEND``.

    An unrecognised value reads as ``local`` and warns, rather than failing
    to boot: the safe direction for a typo in a storage setting is the
    backend that always works, and silence would leave the operator
    believing S3 was in use.
    """
    raw = (os.environ.get("STORAGE_BACKEND") or "local").strip().lower()
    if raw in ("local", "s3"):
        return raw
    log.warning("STORAGE_BACKEND=%r is not 'local' or 's3' — using local.",
                raw)
    return "local"


def org_config(org_id: str | None) -> S3Config | None:
    """An organisation's own S3 settings, decrypted, or ``None``.

    Gated on ``STORAGE_BACKEND_CONFIGURABLE``. While that flag is off the
    per-organisation choice is ignored and everything uses the instance
    default — ADR §6 gate 3, so the Admin UI (E8.3) cannot offer a choice
    that has nothing behind it.
    """
    if not org_id:
        return None
    from engine import features
    if not features.is_enabled("STORAGE_BACKEND_CONFIGURABLE"):
        return None
    try:
        from engine import db as _db
        from engine import llm_keys as _keys
        token = _db.get_org_secret(org_id, ORG_SECRET_NAME)
        if not token:
            return None
        raw = _keys.decrypt_secret(token)
        if not raw:
            return None
        data = json.loads(raw)
    except Exception as exc:
        # A config we cannot read is not a config. Falling through to the
        # instance default is the safe direction — the alternative is an
        # organisation whose uploads start failing because of a key
        # rotation they were not told about.
        log.warning("org storage config unreadable for %s: %s",
                    org_id[:8], exc)
        return None
    return S3Config(
        endpoint=str(data.get("endpoint") or "").strip(),
        bucket=str(data.get("bucket") or "").strip(),
        access_key=str(data.get("access_key") or "").strip(),
        secret_key=str(data.get("secret_key") or "").strip(),
        region=str(data.get("region") or "").strip(),
        secure=bool(data.get("secure", True)),
    )


def set_org_config(org_id: str, config: S3Config) -> None:
    """Store an organisation's S3 settings, encrypted. Raises on refusal."""
    if not org_id:
        raise ValueError("org_id is required")
    if not config.complete:
        raise ValueError("an endpoint, a bucket and a key pair are required")
    from engine import db as _db
    from engine import llm_keys as _keys

    payload = json.dumps({
        "endpoint": config.endpoint, "bucket": config.bucket,
        "access_key": config.access_key, "secret_key": config.secret_key,
        "region": config.region, "secure": config.secure,
    })
    _db.set_org_secret(org_id, ORG_SECRET_NAME,
                       _keys.encrypt_secret(payload))
    log.info("storage config stored for org=%s (%s)", org_id[:8],
             config.bucket)


def clear_org_config(org_id: str) -> bool:
    from engine import db as _db
    return bool(org_id) and _db.delete_org_secret(org_id, ORG_SECRET_NAME)


def backend_for(org_id: str | None = None) -> Backend:
    """The backend to use right now, for this organisation.

    Resolution order, and the whole point of the function:

    1. the organisation's own configuration, when the flag allows it;
    2. the instance default, when ``STORAGE_BACKEND=s3``;
    3. local disk.

    Read on **every call**. That is what makes E8.2's acceptance criterion
    true — "changing the backend is config, not deploying new code" — and it
    is why there is no module-level cached instance here.

    Never raises: a broken S3 configuration falls back to local disk **and
    says so in the log**. That is not the forbidden silent switch of ADR
    §4.6, which is about writing bytes somewhere other than where the caller
    was told; this is choosing a backend before any bytes move, and the
    alternative — refusing every upload because an env var is wrong — turns
    a misconfiguration into an outage.
    """
    config = org_config(org_id)
    source = "org"
    if config is None and instance_backend_name() == "s3":
        config, source = instance_config(), "instance"
    if config is None:
        return LocalBackend()
    try:
        return S3Backend(config)
    except StorageUnavailable as exc:
        log.error("%s S3 storage is configured but unusable (%s) — falling "
                  "back to local disk. Artefacts written now will not be in "
                  "the bucket.", source, exc)
        return LocalBackend()


def describe(org_id: str | None = None) -> dict:
    """What an operator needs to know about where artefacts are going."""
    backend = backend_for(org_id)
    info = backend.describe()
    info["org_configured"] = org_config(org_id) is not None
    info["instance_default"] = instance_backend_name()
    return info


def check(config: S3Config | None = None,
          org_id: str | None = None) -> CheckResult:
    """Test a configuration — a candidate one, or the one in force.

    Passing *config* is what makes the settings form's "Test connection"
    honest: it checks **what the admin just typed**, before anything is
    stored. Without it the button could only confirm that the previously
    saved settings still work, which is not the question being asked.
    """
    if config is None:
        return backend_for(org_id).check(org_id)
    if not config.complete:
        return CheckResult(
            False, "reach",
            "An endpoint, a bucket, an access key and a secret are all "
            "required before a connection can be tested.")
    try:
        backend: Backend = S3Backend(config, timeout=CHECK_TIMEOUT_SECONDS)
    except StorageUnavailable as exc:
        return CheckResult(False, "reach", str(exc), detail=str(exc))
    return backend.check(org_id)


def usage_for(org_id: str | None = None) -> Usage | None:
    """How much storage this organisation is using, or ``None``.

    ``None`` and not ``Usage()`` when the backend cannot answer: zero is a
    measurement and "we could not measure" is not, and a settings page that
    renders a failed scan as "0 B" tells an admin their evidence is gone.
    """
    try:
        return backend_for(org_id).usage(org_prefix(org_id) + "/")
    except StorageError as exc:
        log.warning("storage usage unavailable for %s: %s",
                    (org_id or "instance")[:8], exc)
        return None


__all__ = [
    "ORG_SECRET_NAME", "PRESIGN_TTL_SECONDS", "USAGE_SCAN_LIMIT",
    "CHECK_SEGMENT", "CHECK_STEPS", "CHECK_TIMEOUT_SECONDS",
    "ORG_NONE",
    "StorageError", "StorageUnavailable",
    "Location", "Usage", "CheckResult",
    "Backend", "LocalBackend", "S3Backend", "S3Config",
    "org_prefix", "instance_config", "instance_backend_name",
    "org_config", "set_org_config", "clear_org_config",
    "backend_for", "describe", "check", "usage_for",
]
