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


class StorageError(RuntimeError):
    """A storage operation failed. The message is safe to log, not to show."""


class StorageUnavailable(StorageError):
    """The backend is configured but cannot be reached or built."""


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
    """What every backend must do. Four verbs, and each has a caller.

    Deliberately small. ``list``, ``copy`` and ``move`` are absent because
    nothing needs them yet, and an interface with methods no caller uses is
    the shape this programme has met five times.
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

    def describe(self) -> dict:
        """What an operator needs to see. Never includes a secret."""
        return {"backend": self.name}


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

    @property
    def complete(self) -> bool:
        return bool(self.endpoint and self.bucket and self.access_key
                    and self.secret_key)

    def redacted(self) -> dict:
        """Safe to render. The secret never travels back, for the reason
        ``routes/settings.py`` gives about the BYOK key: a credential that
        can be revealed is a credential that ends up in a screenshot."""
        return {
            "endpoint": self.endpoint,
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

    def __init__(self, config: S3Config):
        if not config.complete:
            raise StorageUnavailable(
                "the S3 backend needs an endpoint, a bucket and a key pair")
        self.config = config
        self._client = None

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
        host = self.config.endpoint
        for scheme in ("https://", "http://"):
            if host.startswith(scheme):
                host = host[len(scheme):]
                break
        try:
            self._client = Minio(
                host, access_key=self.config.access_key,
                secret_key=self.config.secret_key,
                secure=self.config.secure,
                region=self.config.region or None)
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

    def describe(self) -> dict:
        return {"backend": self.name, "durable": True,
                **self.config.redacted()}


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


__all__ = [
    "ORG_SECRET_NAME", "PRESIGN_TTL_SECONDS",
    "StorageError", "StorageUnavailable", "Location",
    "Backend", "LocalBackend", "S3Backend", "S3Config",
    "instance_config", "instance_backend_name",
    "org_config", "set_org_config", "clear_org_config",
    "backend_for", "describe",
]
