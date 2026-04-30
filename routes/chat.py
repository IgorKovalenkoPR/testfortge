"""TestFortge — Chatbot routes.

Tiny JSON API powering the floating QA assistant widget:
  * POST /chat            — answer a user message
  * GET  /chat/history    — return the per-session transcript
  * POST /chat/reset      — clear the transcript
  * POST /chat/bug-form   — submit a structured bug report from the chat
"""

from __future__ import annotations

import os
from datetime import datetime

from flask import Flask, jsonify, request, session
from werkzeug.utils import secure_filename

from engine import db as _db

from ._shared import ensure_active_project

from engine.chatbot import respond_dict as _chatbot_respond
from engine.bug_report import (
    BugReport, bug_to_dict, dict_to_bug, generate_bug_id,
)


def register(app: Flask) -> None:
    """Attach chatbot routes directly on the app (flat endpoint names)."""

    @app.route("/chat", methods=["POST"])
    def chat_route():
        """Serve JSON replies for the floating QA assistant widget.

        Request body (either JSON or form-encoded) must carry ``message``.
        Language is taken from the current session (fallback ``en``) but can
        be overridden with a ``lang`` field.
        """
        payload = request.get_json(silent=True) or request.form
        message = (payload.get("message") or "").strip()
        lang = (payload.get("lang") or session.get("lang") or "en").lower()
        if lang not in ("en", "ua"):
            lang = "en"

        if not message:
            return jsonify({
                "text": "Please type a message.",
                "intent": "empty",
                "suggestions": [],
                "follow_up": [],
            }), 400

        # Cap per-message size so the filesystem session can't balloon
        # from a stream of large messages. (MAX_CONTENT_LENGTH catches the
        # obvious monster case; this bounds the steady-state.)
        max_chars = app.config.get("CHAT_MESSAGE_MAX_CHARS", 4000)
        if len(message) > max_chars:
            return jsonify({
                "text": f"Message is too long ({len(message)} chars). "
                        f"Please keep it under {max_chars}.",
                "intent": "rejected_too_long",
                "suggestions": [],
                "follow_up": [],
            }), 413

        reply = _chatbot_respond(message, lang)

        history = session.get("chat_history", [])
        history.append({"role": "user", "text": message})
        history.append({"role": "bot", "text": reply["text"],
                        "suggestions": reply["suggestions"],
                        "follow_up": reply["follow_up"]})
        keep = app.config.get("CHAT_HISTORY_MAX_ENTRIES", 40)
        session["chat_history"] = history[-keep:]

        return jsonify(reply)

    @app.route("/chat/history", methods=["GET"])
    def chat_history_route():
        """Return the cached chat transcript for the current session."""
        return jsonify({"history": session.get("chat_history", [])})

    @app.route("/chat/reset", methods=["POST"])
    def chat_reset_route():
        session["chat_history"] = []
        return jsonify({"status": "ok"})

    @app.route("/chat/bug-form", methods=["POST"])
    def chat_bug_form_route():
        """Receive a structured bug report from the in-chat form.

        Required fields: ``summary``, ``environment``, ``steps_to_reproduce``,
        ``actual_result``, ``expected_result``.
        Optional: ``attachment`` (file), ``note``.
        """
        # Multipart form when an attachment is attached, plain form otherwise.
        f = request.form
        required = {
            "summary":             f.get("summary", "").strip(),
            "environment":         f.get("environment", "").strip(),
            "steps_to_reproduce":  f.get("steps_to_reproduce", "").strip(),
            "actual_result":       f.get("actual_result", "").strip(),
            "expected_result":     f.get("expected_result", "").strip(),
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            return jsonify({
                "ok": False,
                "error": "missing_fields",
                "missing": missing,
                "message": f"Missing required field(s): {', '.join(missing)}",
            }), 400

        # Optional attachment — store under UPLOAD_FOLDER/chat_bug_attachments/.
        attachment_name = ""
        attachment_path = ""
        upload = request.files.get("attachment")
        if upload and upload.filename:
            upload_root = app.config.get("UPLOAD_FOLDER", "./uploads")
            target_dir = os.path.join(upload_root, "chat_bug_attachments")
            os.makedirs(target_dir, exist_ok=True)
            safe_name = secure_filename(upload.filename)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            attachment_name = f"{ts}_{safe_name}"
            attachment_path = os.path.join(target_dir, attachment_name)
            try:
                upload.save(attachment_path)
            except Exception:
                # Don't fail the whole bug just because the attachment
                # didn't persist — log via the standard request flow and
                # continue with an empty attachment.
                attachment_name = ""
                attachment_path = ""

        note = f.get("note", "").strip()

        bugs = session.get("bug_reports_data", [])
        existing = [dict_to_bug(b) for b in bugs]
        new_id = generate_bug_id(existing)
        project_setup = session.get("project_setup", {}) or {}

        # Build a numbered Steps-to-Reproduce string the way the rest of
        # the framework expects.
        steps_raw = required["steps_to_reproduce"]
        if not steps_raw.lstrip().startswith("1."):
            lines = [ln.strip(" -*\u2022") for ln in steps_raw.splitlines() if ln.strip()]
            steps_raw = "\n".join(f"{i+1}. {ln}" for i, ln in enumerate(lines))

        bug = BugReport(
            id=new_id,
            title=required["summary"],
            severity="Major",
            priority="High",
            status="Open",
            environment=required["environment"],
            preconditions="",
            steps_to_reproduce=steps_raw,
            actual_result=required["actual_result"],
            expected_result=required["expected_result"],
            frequency="Always",
            affects_version=(
                project_setup.get("project_version")
                or project_setup.get("project_name")
                or "Unspecified"
            ),
            found_in_build="",
            linked_item_id="",
            linked_item_type="",
            reporter="Tedgie chat",
            assignee="",
            component="",
            labels=["chat-reported"],
            attachments=[attachment_name] if attachment_name else [],
            comment=note,
            created_at=datetime.now().isoformat(),
        )
        bug_d = bug_to_dict(bug)
        bugs.append(bug_d)
        session["bug_reports_data"] = bugs
        session.modified = True

        # Mirror to Postgres: a Tedgie-sourced BugReport row + an audit
        # entry in tedgie_submission so we can later see *what* the user
        # submitted (raw form payload) even if the bug row is mutated.
        bug_db_id = None
        try:
            pid = ensure_active_project()
            if pid:
                # Build the same shape engine.db.save_bug expects.
                db_payload = {
                    "id":                 new_id,
                    "title":              bug.title,
                    "severity":           bug.severity,
                    "priority":           bug.priority,
                    "status":             bug.status,
                    "environment":        bug.environment,
                    "steps_to_reproduce": bug.steps_to_reproduce,
                    "actual_result":      bug.actual_result,
                    "expected_result":    bug.expected_result,
                    "comment":            bug.comment,
                    "reporter":           bug.reporter,
                    "preconditions":      bug.preconditions,
                    "frequency":          bug.frequency,
                    "affects_version":    bug.affects_version,
                    "labels":             bug.labels,
                    "attachments":        bug.attachments,
                    "created_at":         bug.created_at,
                }
                bug_db_id = _db.save_bug(pid, db_payload, source="tedgie")
                _db.save_tedgie_submission(
                    project_id=pid,
                    raw_payload={
                        "summary":            required["summary"],
                        "environment":        required["environment"],
                        "steps_to_reproduce": required["steps_to_reproduce"],
                        "actual_result":      required["actual_result"],
                        "expected_result":    required["expected_result"],
                        "note":               note,
                        "attachment_name":    attachment_name,
                    },
                    reporter="Tedgie chat",
                    classified_into_bug_id=bug_db_id,
                )
        except Exception as exc:  # pragma: no cover — best-effort
            log = __import__("engine.log", fromlist=["get_logger"]).get_logger(__name__)
            log.warning("Tedgie bug persist failed: %s", exc)

        return jsonify({
            "ok": True,
            "id": new_id,
            "message": f"Bug report {new_id} created.",
        })


__all__ = ["register"]
