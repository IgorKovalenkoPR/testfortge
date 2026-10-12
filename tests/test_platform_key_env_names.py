"""The platform key is read from ``CLAUDE_KEY`` too — the QAREA launchpad
delivers ``APP_CLAUDE_KEY`` with its prefix stripped, and a module reading
``ANTHROPIC_API_KEY`` directly would report "no key" on that host."""
import pathlib
import re

from engine import llm_keys


def _clear(monkeypatch):
    for name in llm_keys.PLATFORM_KEY_ENVS:
        monkeypatch.delenv(name, raising=False)


def test_claude_key_alone_is_the_platform_key(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CLAUDE_KEY", "  sk-ant-launchpad  ")
    assert llm_keys.platform_key() == "sk-ant-launchpad"
    assert llm_keys.resolve_key(None) == ("sk-ant-launchpad", "platform")


def test_anthropic_api_key_wins_over_claude_key(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-primary")
    monkeypatch.setenv("CLAUDE_KEY", "sk-ant-launchpad")
    assert llm_keys.platform_key() == "sk-ant-primary"


def test_blank_values_are_no_key(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "   ")
    monkeypatch.setenv("CLAUDE_KEY", "")
    assert llm_keys.platform_key() is None


def test_no_module_reads_the_key_env_directly():
    """Every "is there a key?" gate must go through platform_key()."""
    root = pathlib.Path(__file__).resolve().parent.parent
    pat = re.compile(r'environ(?:\.get)?\(\s*["\']ANTHROPIC_API_KEY')
    offenders = []
    for d in ("engine", "routes"):
        for p in (root / d).rglob("*.py"):
            if p.name == "llm_keys.py":
                continue
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if (pat.search(line) and "CLAUDE_KEY" not in line
                        and not line.lstrip().startswith("#")):
                    offenders.append(f"{p.relative_to(root)}:{i}")
    assert not offenders, offenders
