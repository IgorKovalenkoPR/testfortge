"""The product knew why it could not read your file, and did not say.

``parse_page_input`` returns ``(raw_lines, errors, custom_prompt)``, where
``errors`` is a list of ``"<file>: <reason>"`` strings the parser produced.
Both async generation routes destructured it and never used it, and the two
live pages were handed ``errors=errors`` and rendered nothing — the only
template that ever displayed it is ``user_stories.html``, which is on the
unreachable list ("pre-E3 user-story list; no route").

Measured on the auth preview against the code as it stood, uploading a
64-byte ``requirements.doc``:

    POST /test-cases/run-async  →  400 {"message": "Please enter
                                   requirements or upload files."}
    POST /test-cases            →  no alert, no mention of .docx

``.doc`` is in ``ALLOWED_EXTENSIONS`` (though not in the picker's accept
list, so it arrives by drag-and-drop), the parser refuses it with *".doc
format is not supported directly. Please save the file as .docx"*, and the
operator was told they had uploaded nothing. Two untrue words — "enter" and
"upload" — in answer to a request that did upload, with the fixable reason
collected and thrown away.

Both paths say it now: the async routes append the parser's account to the
message the page already displays for a 400, and the two templates render
the list for the no-JavaScript form post.

One consequence, fixed here rather than noted: ``_parse_video`` returned
its metadata line with **no** message, while ``_parse_image`` next door has
always told the operator its content is not read. The accept list offers
fourteen video formats and none of them is watched — so a screen recording
bought a full generation from one line of metadata, silently. That note had
nowhere to appear until the channel above was rendered; now it does.

Not fixed, and named rather than left silent: when a file fails *and* other
input succeeds, generation proceeds and the async payload still carries no
warnings. Adding a field nothing renders would be the same defect in a new
place, so that half needs somewhere on the page to put it.
"""
from __future__ import annotations

import io
import re
import secrets

import pytest

from engine import auth as _auth
from engine import db as _db
from engine import permissions as _perm
from engine import session_timeout as _timeout
from engine.file_parser import ALLOWED_EXTENSIONS

DOC = "requirements.doc"
REASON = "save the file as .docx"


@pytest.fixture(autouse=True)
def _flags(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "1")
    monkeypatch.setenv("ORG_MODE", "1")
    _db.init_db()


@pytest.fixture
def client(app):
    org = _db.create_organization(f"Org {secrets.token_hex(3)}")
    uid = _db.create_user(
        f"u-{secrets.token_hex(4)}@example.test",
        password_hash=_auth.hash_password("a perfectly good passphrase"))
    _db.add_org_member(org, uid, "admin")
    pid = _db.upsert_project(name=f"P {secrets.token_hex(3)}", org_id=org)
    c = app.test_client()
    with c.session_transaction() as sess:
        sess[_perm.SESSION_USER_KEY] = uid
        sess[_perm.SESSION_ORG_KEY] = org
        sess["project_id"] = pid
        _timeout.stamp(sess)
    return c


def _upload(client, url, name=DOC, follow=False):
    return client.post(
        url,
        data={"input_files": (io.BytesIO(b"\x00" * 64), name)},
        content_type="multipart/form-data",
        follow_redirects=follow)


class TestThePremise:

    def test_a_doc_gets_as_far_as_the_parser(self):
        """If ``.doc`` were simply refused by ``allowed_file`` the loop
        would skip it before parsing and there would be no reason to
        report — the defect exists because the parser *does* see it and
        *does* explain itself."""
        assert "doc" in ALLOWED_EXTENSIONS

    def test_the_parser_explains_itself(self, tmp_path):
        from engine.file_parser import parse_file
        path = tmp_path / DOC
        path.write_bytes(b"\x00" * 64)
        lines, err = parse_file(str(path), DOC)
        assert lines == []
        assert REASON in (err or ""), err

    def test_the_only_template_that_rendered_errors_is_unreachable(self):
        from tests.test_every_template_is_reachable import UNREACHABLE
        assert "user_stories.html" in UNREACHABLE


class TestTheAsyncPathSaysWhy:
    """The path the page itself takes — its JS posts to ``run-async`` and
    shows ``res.body.message`` for a 400."""

    @pytest.mark.parametrize("url", ["/test-cases/run-async",
                                     "/checklist/run-async"])
    def test_the_message_names_the_file_and_the_reason(self, client, url):
        response = _upload(client, url)
        assert response.status_code == 400, response.status_code
        message = response.get_json()["message"]
        assert DOC in message, message
        assert REASON in message, message

    @pytest.mark.parametrize("url", ["/test-cases/run-async",
                                     "/checklist/run-async"])
    def test_it_still_says_the_original_sentence(self, client, url):
        """The advice is added, not substituted: "enter requirements or
        upload files" is still what to do next."""
        message = _upload(client, url).get_json()["message"]
        assert "upload" in message.lower(), message

    @pytest.mark.parametrize("url", ["/test-cases/run-async",
                                     "/checklist/run-async"])
    def test_an_empty_form_is_unchanged(self, client, url):
        """The control. With nothing uploaded there is nothing to explain,
        and the message must not grow a stray suffix."""
        response = client.post(url, data={}, content_type="multipart/form-data")
        assert response.status_code == 400
        message = response.get_json()["message"]
        assert DOC not in message
        assert message.strip().endswith("."), message


class TestAnAttachmentSaysItIsOnlyAnAttachment:
    """The two branches that record a file without reading it. The image
    one always spoke; the video one did not, and they sit ten lines
    apart."""

    def _told(self, tmp_path, name, payload):
        from engine.file_parser import parse_file
        path = tmp_path / name
        path.write_bytes(payload)
        lines, err = parse_file(str(path), name)
        return lines, err

    def test_a_video_says_its_frames_are_not_read(self, tmp_path):
        # ``bytes(2048)`` rather than a literal: the first version of
        # this file was written through a heredoc and ended up with
        # real NUL bytes in the source.
        lines, err = self._told(tmp_path, "demo.mp4", bytes(2048))
        assert lines and "demo.mp4" in lines[0], lines
        assert err, "the video branch said nothing"
        assert "not read" in err, err
        assert "Additional Instructions" in err, err

    def test_an_image_still_says_the_same(self, tmp_path):
        """The neighbour this copies, and the control: a change that made
        the video speak by silencing the image would be no better."""
        import base64
        lines, err = self._told(tmp_path, "shot.png", base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8AAAwAB/AF/A2sAAAAASUVORK5CYII="))
        assert lines, lines
        assert err and "OCR" in err, err

    def test_a_readable_file_says_nothing(self, tmp_path):
        """A parser that returned a note for everything would turn a
        clean upload into a warning."""
        lines, err = self._told(
            tmp_path, "reqs.txt",
            "The cart must apply one discount.".encode("utf-8"))
        assert lines, lines
        assert err is None, err


class TestTheNoJavaScriptPathSaysWhy:

    @pytest.mark.parametrize("url", ["/test-cases", "/checklist"])
    def test_the_page_renders_the_warning(self, client, url):
        body = _upload(client, url, follow=True).get_data(as_text=True)
        assert "alert-warning" in body, "no warning block rendered"
        block = re.search(r'<div class="alert alert-warning">(.*?)</div>',
                          body, re.S)
        assert block, body[:400]
        assert DOC in block.group(1), block.group(1)
        assert REASON in block.group(1), block.group(1)

    @pytest.mark.parametrize("url", ["/test-cases", "/checklist"])
    def test_a_clean_page_has_no_warning_block(self, client, url):
        """A block that rendered unconditionally would be an empty yellow
        box on every visit."""
        body = client.get(url).get_data(as_text=True)
        assert '<div class="alert alert-warning">' not in body
