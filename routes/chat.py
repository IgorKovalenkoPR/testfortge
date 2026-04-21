"""TestFortge — Chatbot routes.

Tiny JSON API powering the floating QA assistant widget:
  * POST /chat          — answer a user message
  * GET  /chat/history  — return the per-session transcript
  * POST /chat/reset    — clear the transcript
"""

from __future__ import annotations

from flask import Flask, jsonify, request, session

from engine.chatbot import respond_dict as _chatbot_respond


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


__all__ = ["register"]
