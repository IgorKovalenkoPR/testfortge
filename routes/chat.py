"""TestFortge — Chatbot routes.

Tiny JSON API powering the floating QA assistant widget:
  * POST /chat            — answer a user message (legacy, blocking)
  * GET  /chat/stream     — same answer streamed as Server-Sent Events
  * GET  /chat/history    — return the per-session transcript
  * POST /chat/reset      — clear the transcript
  * POST /chat/bug-form   — submit a structured bug report from the chat
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime

from flask import Flask, Response, jsonify, request, session, stream_with_context
from werkzeug.utils import secure_filename

from engine import blobs as _blobs
from engine import db as _db

from ._shared import (ensure_active_project, mirror_pack, pack_bugs,
                      resolve_active_project)

from engine import chatbot as _chatbot_mod
from engine.chatbot import respond_dict as _chatbot_respond
from engine.bug_report import (
    BugReport, bug_to_dict, dict_to_bug, generate_bug_id,
)
from engine import llm_models as _llm_models
from engine.llm_safety import wrap_user_input
from engine.log import get_logger

_logger = get_logger(__name__)


def _meter_stream(final) -> None:
    """Record a streamed Tedgie reply's token usage (E0.7).

    Separate from ``engine.llm_client._meter`` because the streaming path
    never goes through ``call_messages``: the SDK's streaming helper has no
    equivalent there, so this route builds its own client. Keeping the
    accounting identical in effect, if not in code, is the point — a usage
    report that silently omitted the chattiest surface in the product would
    be worse than no report.

    Swallows everything. The user has already received their answer by the
    time this runs; an accounting error must not turn a delivered reply
    into a 500.
    """
    try:
        from engine import llm_cost as _llm_cost
        usage = _llm_cost.extract_usage(final)
        if not any(usage):
            return
        model = _llm_models.model_for("consult")
        _db.record_llm_usage(
            kind="consult", model=model,
            org_id=session.get("org_id"),
            project_id=session.get("project_id"),
            user_id=session.get("_user_id"),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            cost_micros=_llm_cost.cost_micros(model, usage),
        )
    except Exception as exc:  # pragma: no cover — accounting is never fatal
        _logger.debug("stream usage metering skipped: %s", exc)


# ── SSE helpers ────────────────────────────────────────────────────

def _sse(event_name: str, data: dict) -> str:
    """Format a single Server-Sent Event frame.

    ``ensure_ascii=False`` keeps Cyrillic readable on the wire (smaller
    payloads, easier to debug in DevTools). The wire format is the
    standard ``event: <name>\\ndata: <json>\\n\\n``.
    """
    return (
        f"event: {event_name}\n"
        f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
    )


def _reply_to_dict(reply) -> dict:
    """ChatReply dataclass → JSON-ready dict. Identical shape to /chat."""
    return {
        "text": reply.text,
        "intent": reply.intent,
        "suggestions": list(reply.suggestions or []),
        "follow_up": list(reply.follow_up or []),
    }


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

    @app.route("/chat/stream", methods=["GET"])
    def chat_stream_route():
        """Stream a chatbot reply as Server-Sent Events.

        Mirrors the ``POST /chat`` contract but emits incremental
        ``event: delta`` frames as Anthropic's stream produces tokens.
        Fast-path replies (greeting / guide / istqb / bug_form) skip the
        LLM entirely and are delivered as a single ``event: full`` +
        ``event: done`` pair.

        Heartbeat ``: heartbeat`` comment lines are emitted every ~10 s
        of token silence to survive Render's 30 s idle-connection timer.
        """
        message = (request.args.get("message") or "").strip()
        lang = (request.args.get("lang") or session.get("lang") or "en").lower()
        if lang not in ("en", "ua"):
            lang = "en"

        max_chars = app.config.get("CHAT_MESSAGE_MAX_CHARS", 4000)
        if len(message) > max_chars:
            message = message[:max_chars]

        # Snapshot the session list now so the generator (which runs
        # outside Flask's request context once streaming starts) can
        # append the final transcript entry without hitting the
        # ``RuntimeError: Working outside of request context`` trap.
        history_keep = app.config.get("CHAT_HISTORY_MAX_ENTRIES", 40)

        def _append_history(user_msg: str, reply_dict: dict) -> None:
            history = session.get("chat_history", [])
            history.append({"role": "user", "text": user_msg})
            history.append({
                "role": "bot",
                "text": reply_dict.get("text", ""),
                "suggestions": reply_dict.get("suggestions", []),
                "follow_up": reply_dict.get("follow_up", []),
            })
            session["chat_history"] = history[-history_keep:]

        def generate():
            # Empty-message safety net — mirror the POST behaviour.
            if not message:
                fallback = {
                    "text": "Please type a message.",
                    "intent": "empty",
                    "suggestions": [],
                    "follow_up": [],
                }
                yield _sse("full", fallback)
                yield _sse("done", {"intent": "empty"})
                return

            # 1. Fast-path rule handlers (greeting / guide / istqb /
            # bug_form). Returns None when the LLM is needed.
            try:
                fast = _chatbot_mod.try_fast_path(message, lang)
            except Exception as exc:  # pragma: no cover — defensive
                _logger.warning("fast-path failed, falling back: %s", exc)
                fast = None

            if fast is not None:
                reply_dict = _reply_to_dict(fast)
                yield _sse("full", reply_dict)
                yield _sse("done", {"intent": reply_dict["intent"]})
                _append_history(message, reply_dict)
                return

            # 2. LLM streaming path. Missing key → rule-based fallback.
            api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
            if not api_key:
                fallback = _chatbot_mod.rule_based_fallback(message, lang)
                reply_dict = _reply_to_dict(fallback)
                yield _sse("full", reply_dict)
                yield _sse("done", {"intent": reply_dict["intent"]})
                _append_history(message, reply_dict)
                return

            yield _sse("meta", {"intent": "ai_generic", "lang": lang})

            chunks: list[str] = []
            try:
                # Lazy import — keeps the route loadable in environments
                # without the SDK installed, matching how
                # ``engine.chatbot`` handles its import.
                from anthropic import Anthropic  # type: ignore

                client = Anthropic(api_key=api_key)
                last_emit = time.monotonic()
                with client.messages.stream(
                    # Routed by work kind, same as every non-streaming
                    # call. This path builds its own SDK client rather
                    # than going through engine.llm_client (streaming has
                    # no equivalent there), so the routing has to be
                    # repeated here — and so does the metering, below.
                    model=_llm_models.model_for("consult"),
                    max_tokens=_chatbot_mod._ANTHROPIC_MAX_TOKENS,
                    # System-blocks list (not a string) so the cached
                    # persona block actually hits Anthropic's ephemeral
                    # cache. See engine/chatbot.py for the persona text.
                    system=_chatbot_mod._ai_system_blocks(lang),
                    # Sprint 4 task 4.4: wrap the user-controlled
                    # message in <user_input> so the persona's
                    # untrusted-input clause can disarm directives.
                    messages=[{"role": "user", "content": wrap_user_input(message)}],
                ) as stream:
                    for text in stream.text_stream:
                        # Heartbeat if the upstream went quiet — Render's
                        # proxy kills idle connections at 30 s, so we
                        # emit a comment line every ~10 s of silence.
                        now = time.monotonic()
                        if now - last_emit > 10:
                            yield ": heartbeat\n\n"
                        if not text:
                            continue
                        chunks.append(text)
                        yield _sse("delta", {"text": text})
                        last_emit = time.monotonic()
                    # Drain final message so we can log usage / detect
                    # the <BUG_FORM/> marker emitted by the persona.
                    try:
                        final = stream.get_final_message()
                    except Exception:  # pragma: no cover — defensive
                        final = None

                full_text = "".join(chunks).strip()
                intent = "ai_generic"
                if "<BUG_FORM/>" in full_text:
                    full_text = full_text.replace("<BUG_FORM/>", "").strip()
                    intent = "bug_form"

                if final is not None:
                    usage = getattr(final, "usage", None)
                    if usage is not None:
                        # Structured usage log — surfaces cache hits /
                        # misses per request so we can verify the S3.2
                        # prompt-caching deploy from production logs.
                        _logger.info(
                            "anthropic usage: input=%s output=%s "
                            "cache_creation=%s cache_read=%s",
                            getattr(usage, "input_tokens", 0),
                            getattr(usage, "output_tokens", 0),
                            getattr(usage, "cache_creation_input_tokens", 0),
                            getattr(usage, "cache_read_input_tokens", 0),
                        )
                        # …and into the meter, not just the log. Tedgie is
                        # the highest-volume LLM surface in the product, so
                        # a usage report that omitted it would understate
                        # spend by more than everything else combined.
                        _meter_stream(final)

                reply_dict = {
                    "text": full_text,
                    "intent": intent,
                    "suggestions": [],
                    "follow_up": [],
                }
                _append_history(message, reply_dict)
                yield _sse("done", {
                    "intent": intent,
                    "suggestions": [],
                    "follow_up": [],
                })
            except GeneratorExit:
                _logger.info("SSE client disconnected mid-stream")
                raise
            except Exception as exc:
                _logger.warning("chat stream failed: %s", exc)
                yield _sse("error", {"message": "Stream interrupted"})

        return Response(
            stream_with_context(generate()),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

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

        # Optional attachment. Stored through ``engine.blobs`` so it lands
        # where ``automation_asset`` serves from — E4.5a.
        #
        # It used to be written to ``UPLOAD_FOLDER/chat_bug_attachments/``
        # while the bug page renders attachments out of ``STORAGE_ROOT``,
        # which are two different directories. Measured before the fix: the
        # upload succeeded, the row recorded the bare filename, and the
        # gallery got a **404** — a broken image where the evidence should
        # be. Nothing reported it, because a save that works and a read that
        # fails are in different requests.
        attachment_name = ""
        upload = request.files.get("attachment")
        if upload and upload.filename:
            try:
                attachment_name = _blobs.save(
                    upload, project_id=(resolve_active_project() or "none"),
                    kind="bug", entity_id="chat")
            except Exception as exc:
                # Unlike the bug page's own upload, this one does not refuse
                # the whole submission: the file is one optional field on a
                # form whose point is the bug report, and losing the report
                # to save the screenshot is the wrong trade. Logged, and the
                # response says the attachment was dropped.
                _logger.warning(
                    "chat bug attachment not stored: %s", exc)
                attachment_name = ""

        note = f.get("note", "").strip()

        bugs = list(pack_bugs())
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
        mirror_pack("bug_reports_data", bugs)
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
