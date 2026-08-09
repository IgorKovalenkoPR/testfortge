#!/usr/bin/env python3
"""Verify a real bucket, with real credentials (E8.7).

    python scripts/verify_storage.py

Reads the same ``STORAGE_S3_*`` variables the application does and runs the
operations the product actually performs, in order, against **your** bucket.
Prints a pass/fail table and exits non-zero on the first thing that is not
true.

Why this exists
---------------
The suite tests the S3 adapter against ``moto``, which is a real HTTP server
implementing S3 semantics — a large step up from a stub, and still not the
provider you are about to use. moto does not enforce signatures, has no
bucket policies, does not implement one provider's quirks over another's, and
has never been slow or rate-limited. So the suite proves the request shaping
and the protocol; it cannot prove that a particular provider accepts our
signatures or that your key has the permissions an upload needs.

This script closes that. It is the difference between "the code is tested"
and "your deployment works", and those are different sentences.

What it does, and undoes
------------------------
Everything it writes lives under ``_verify/<random>/`` and is deleted at the
end — including on failure. It never touches an existing key. The last check
is the delete itself, because a key that can write and not delete passes
every check people usually run and then makes E8.5 a promise you cannot
keep.

Run it after creating the bucket, and again after changing a bucket policy.
"""
from __future__ import annotations

import os
import secrets
import sys
import urllib.error
import urllib.request

# Import from the repository root regardless of where this is invoked from.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import storage  # noqa: E402


PAYLOAD = b"testfortge storage verification " + secrets.token_hex(8).encode()
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"


def _supports_colour() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _say(ok: bool, label: str, detail: str = "") -> None:
    mark, colour = ("PASS", GREEN) if ok else ("FAIL", RED)
    if not _supports_colour():
        colour = RESET = ""          # noqa: F841 — local shadow on purpose
        print(f"[{mark}] {label}" + (f"  — {detail}" if detail else ""))
        return
    print(f"{colour}[{mark}]{RESET} {label}"
          + (f"{DIM}  — {detail}{RESET}" if detail else ""))


def _why(backend, exc: Exception, step: str) -> str:
    """The sentence the Settings page would show for this same failure.

    Measured 2026-08-09 against a live S3 server: every failing step in this
    script printed the provider's own string — ``S3 operation failed; code:
    NoSuchBucket, message: The specified bucket does not exist`` — while the
    runbook promised *"there is no bucket called '<name>'"*. The diagnosis
    existed the whole time; only the Settings panel called it, and the first
    step here returns before the panel's own check is ever reached.

    That is backwards. The person running this script is the person who has
    no other way to tell a mistyped secret from a bucket policy missing
    ``s3:PutObject``, and those have different fixes. So the same method the
    panel uses is called here — the same one, not a copy, so the script and
    the page cannot come to disagree about one bucket.
    """
    try:
        return backend._diagnose(exc, step).message or str(exc)[:200]
    except Exception:                    # pragma: no cover — never mask the
        return str(exc)[:200]            # original failure with a new one


def main() -> int:
    config = storage.instance_config()
    if not config.complete:
        print("STORAGE_S3_ENDPOINT, STORAGE_S3_BUCKET, STORAGE_S3_ACCESS_KEY "
              "and STORAGE_S3_SECRET_KEY must all be set.\n")
        print("Load them from your deployment's environment, or export them "
              "in this shell, then run this again.")
        return 2

    print(f"\nVerifying {config.url or config.endpoint} "
          f"bucket '{config.bucket}'\n")
    backend = storage.S3Backend(config)
    key = f"_verify/{secrets.token_hex(8)}/probe.bin"
    failures = 0
    wrote = False

    try:
        # 1. Write. The permission most bucket policies forget.
        try:
            import io
            backend.put(key, io.BytesIO(PAYLOAD))
            wrote = True
            _say(True, "write an object", "s3:PutObject")
        except Exception as exc:
            _say(False, "write an object", _why(backend, exc, "write"))
            return 1

        # 2. Read it back, byte for byte. "It returned 200" is not the same
        #    claim as "it returned what we stored".
        try:
            got = backend.get_bytes(key)
            ok = got == PAYLOAD
            _say(ok, "read it back unchanged",
                 "s3:GetObject" if ok else "the bytes came back different")
            failures += 0 if ok else 1
        except Exception as exc:
            _say(False, "read it back unchanged", _why(backend, exc, "read"))
            failures += 1

        # 3. Stat.
        try:
            _say(backend.exists(key), "stat the object", "s3:GetObject/Head")
        except Exception as exc:
            _say(False, "stat the object", _why(backend, exc, "read"))
            failures += 1

        # 4. Presigned GET, fetched over the network. This is the one that
        #    matters most for the hosted deployment: ADR 0002 §4.4 serves
        #    artefacts straight from the bucket because egress is free *while
        #    the bytes do not pass through the application* — and that is
        #    only true if this works.
        try:
            location = backend.locate(key)
            with urllib.request.urlopen(location.url, timeout=30) as fetched:
                body = fetched.read()
            ok = body == PAYLOAD
            _say(ok, "fetch a presigned URL",
                 "the browser can reach the bytes directly" if ok
                 else "the URL returned something else")
            failures += 0 if ok else 1
        except urllib.error.HTTPError as exc:
            _say(False, "fetch a presigned URL",
                 f"HTTP {exc.code} — the signature was rejected, or the "
                 f"bucket blocks direct reads")
            failures += 1
        except Exception as exc:
            _say(False, "fetch a presigned URL", _why(backend, exc, "read"))
            failures += 1

        # 5. List by prefix — what usage and deletion both depend on.
        try:
            used = backend.usage(key.rsplit("/", 1)[0] + "/")
            ok = used.objects == 1 and used.bytes == len(PAYLOAD)
            _say(ok, "list a prefix",
                 "s3:ListBucket" if ok else f"saw {used.objects} object(s)")
            failures += 0 if ok else 1
        except Exception as exc:
            _say(False, "list a prefix", _why(backend, exc, "read"))
            failures += 1

        # 6. The application's own check, so this script and the Settings
        #    page cannot disagree about the same bucket.
        try:
            result = backend.check("verify")
            _say(result.ok, "the in-app connection check agrees",
                 result.message[:160])
            failures += 0 if result.ok else 1
        except Exception as exc:
            _say(False, "the in-app connection check agrees", str(exc)[:160])
            failures += 1

    finally:
        # 7. Delete, and report it as a check rather than as cleanup: a key
        #    that can write but not delete passes everything above and makes
        #    "delete this project's data" (E8.5) a promise you cannot keep.
        if wrote:
            try:
                removed = backend.delete_prefix(key)
                gone = removed >= 1 and not backend.exists(key)
                _say(gone, "delete what it wrote",
                     "s3:DeleteObject" if gone
                     else "the object is still in your bucket — remove "
                          f"'{key}' by hand")
                failures += 0 if gone else 1
            except Exception as exc:
                _say(False, "delete what it wrote", _why(backend, exc,
                                                         "delete"))
                print(f"\n  Left behind: {key}")
                failures += 1

    print()
    if failures:
        print(f"{failures} check(s) failed. The deployment is not ready to "
              f"use this bucket.")
        return 1
    print("All checks passed. This bucket is usable for STORAGE_BACKEND=s3.")
    print("Nothing was left behind.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
