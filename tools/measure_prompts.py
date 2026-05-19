"""TestForTge — Measure token counts of Anthropic system prompts.

Sprint 3 Task 3.2 introduces prompt caching, but Anthropic's ephemeral
cache only kicks in for cached blocks of >= 1024 input tokens. This
script calls ``client.beta.messages.count_tokens`` to print the exact
token count for every system prompt we cache, so we can verify each
clears the 1024-token threshold without deploying.

Usage::

    ANTHROPIC_API_KEY=sk-ant-... python tools/measure_prompts.py

Prints one ``<name>: <N> tokens`` line per prompt, plus a one-line
verdict for whether each cached block is large enough. Exit code 0
when every cacheable block is >= 1024 tokens, 1 otherwise. Fails
gracefully with a clear message if ``ANTHROPIC_API_KEY`` is not set.

This tool is intended to be run *manually* during development, not as
part of CI — it makes a real (cheap) API call to count tokens.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the repo importable when run as ``python tools/measure_prompts.py``.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# Anthropic's ephemeral cache minimum. Blocks below this threshold are
# accepted by the API but never actually cached, so we treat them as a
# soft warning here.
_CACHE_MIN_TOKENS = 1024


def _model() -> str:
    return os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-4-5")


def _count_tokens(client, system_text: str) -> int:
    """Call ``beta.messages.count_tokens`` with a dummy user turn.

    The API requires at least one ``messages`` entry; we use a
    one-character "." to keep the user-side contribution near zero so
    the printed count reflects the system prompt itself.
    """
    resp = client.beta.messages.count_tokens(
        model=_model(),
        system=system_text,
        messages=[{"role": "user", "content": "."}],
    )
    # ``input_tokens`` includes both system and the tiny user turn, so
    # the real system-only count is ~1-3 tokens lower. Good enough for
    # a 1024-threshold sanity check.
    return int(resp.input_tokens)


def _collect_prompts() -> list[tuple[str, str, bool]]:
    """Return ``(label, prompt_text, is_cached)`` triples.

    ``is_cached`` flags the prompts that S3.2 actually wraps in a
    cache_control block, so the verdict at the end can ignore prompts
    that are intentionally not cached (e.g. legacy helpers).
    """
    out: list[tuple[str, str, bool]] = []

    # engine.chatbot — Tedgie persona (EN + UA). After S3.2 enlargement
    # both should clear 1024.
    from engine import chatbot as _cb

    out.append(("chatbot EN persona", _cb._ai_system_prompt("en"), True))
    out.append(("chatbot UA persona", _cb._ai_system_prompt("ua"), True))

    # engine.istqb_knowledge — istqb_persona_prompt is inlined into the
    # main persona post-S3.2 but we still surface its size for reference.
    try:
        from engine.istqb_knowledge import istqb_persona_prompt

        out.append(("istqb persona (standalone)", istqb_persona_prompt(), False))
    except Exception:  # pragma: no cover — module is optional
        pass

    # engine.mockup_vision — image-analysis system prompt.
    try:
        from engine import mockup_vision as _mv

        if hasattr(_mv, "_SYSTEM_BLOCKS"):
            text = "".join(b.get("text", "") for b in _mv._SYSTEM_BLOCKS)
        else:
            text = getattr(_mv, "_SYSTEM_PROMPT", "")
        out.append(("mockup vision system", text, True))
    except Exception:  # pragma: no cover — module is optional
        pass

    return out


def main() -> int:
    api_key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    if not api_key:
        sys.stderr.write(
            "ANTHROPIC_API_KEY is not set. Export it (or pass it inline) "
            "before running this tool: it calls the Anthropic count_tokens "
            "endpoint which requires authentication.\n"
        )
        return 2

    try:
        from anthropic import Anthropic  # type: ignore
    except ImportError:
        sys.stderr.write(
            "The 'anthropic' package is not installed. Run `pip install "
            "anthropic` (or install the project requirements) first.\n"
        )
        return 2

    client = Anthropic(api_key=api_key)
    rows = _collect_prompts()
    all_cached_ok = True
    for label, text, is_cached in rows:
        try:
            tokens = _count_tokens(client, text)
        except Exception as exc:  # pragma: no cover — network/auth errors
            sys.stderr.write(f"count_tokens failed for {label}: {exc}\n")
            return 2
        marker = ""
        if is_cached:
            if tokens >= _CACHE_MIN_TOKENS:
                marker = " [cacheable]"
            else:
                marker = (
                    f" [NOT cacheable — needs >= {_CACHE_MIN_TOKENS}]"
                )
                all_cached_ok = False
        print(f"{label}: {tokens} tokens{marker}")

    return 0 if all_cached_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
