"""TestFortge — "Attribute to WBS": put an existing project into its WBS.

Projects generated before the WBS existed (engine/wbs.py) have their items
in "General" and "Unfiled" — 752 and 65 of 817 cases on the dev QArea
project. Regenerating would fix the filing but rewrite the wording; this
pass changes only where each item is filed and the prefix that says so.

1. The WBS: the one stored with the project's site profile, or one built
   from the project's entry page (a single fetch, not a crawl).
2. Each item's node, from what the stored row still says about where it
   came from: the page in its first navigation step or preconditions
   (``Go to https://… -> …``), its ``url_pattern``, its section.
3. Items that evidence nothing (they would land in Cross-cutting >
   General) go to the model in batches, which may only answer with node
   ids from the list; anything else keeps the rule answer.
4. ``preview`` reports counts per node and touches nothing; ``apply``
   writes. Rows a person edited (``ai_generated`` False) are left alone
   unless the caller includes them.
"""
from __future__ import annotations

import json
import re
from typing import Any

from engine import folder_prefix as _fpx
from engine import wbs as _wbs
from engine.log import get_logger

log = get_logger(__name__)

_KIND = "analysis"
_PROMPT_VERSION = "wbs_attribution/1"
_BATCH = 50
_MAX_LLM_ITEMS = 400
_URL_RE = re.compile(r"https?://[^\s\"'<>,;)]+", re.I)
_FIELDS = {"test_case": "summary", "checklist_item": "objective"}


# ── the WBS for a stored project ─────────────────────────────────────

def project_wbs(project_id: str) -> tuple[_wbs.Wbs | None, str]:
    """``(wbs, source)`` — ``stored``, ``entry_page`` or ``none``."""
    from engine import db as _db
    profiles = _db.list_site_profiles(project_id, limit=10)
    for row in profiles:
        stored = _wbs.Wbs.from_dict((row.get("profile") or {}).get("wbs"))
        if stored is not None:
            return stored, "stored"
    url = ""
    if profiles:
        url = profiles[0].get("url") or ""
    if not url:
        url = (_db.get_project(project_id) or {}).get("base_url") or ""
    if not url:
        return None, "none"
    page = _fetch_entry_page(url)
    if page is None:
        return None, "none"
    return _wbs.build_wbs([page]), "entry_page"


def _fetch_entry_page(url: str) -> dict | None:
    from dataclasses import asdict
    from urllib.parse import urlparse
    from engine import site_crawler as _sc
    try:
        html, error = _sc._fetch_page(url)
        if error or not html:
            log.info("wbs attribution: entry page %s not fetched: %s",
                     url, error)
            return None
        return asdict(_sc._parse_page(html, url, urlparse(url).netloc))
    except Exception as exc:  # pragma: no cover — network
        log.warning("wbs attribution: entry page fetch failed: %s", exc)
        return None


# ── hints from a stored row ──────────────────────────────────────────

def stored_hint(row: dict) -> dict:
    """What a stored row still says about the page it was written for.

    A URL in the steps or preconditions is weaker than what the generator
    recorded at creation (``page_url``): legacy site-wide checks open with
    "Site is reachable at https://…" too. So it is passed as ``text_url``,
    which :func:`engine.wbs.attribute` weighs after the section.
    """
    for field_name in ("test_steps", "preconditions"):
        m = _URL_RE.search(str(row.get(field_name) or ""))
        if m:
            return {"text_url": m.group(0).rstrip(".")}
    return {}


def _rule_nodes(wbs, rows: list[dict], kind: str) -> list[str]:
    field_name = _FIELDS[kind]
    out = []
    for row in rows:
        item = dict(row)
        item[field_name] = _fpx.strip_prefix(item.get(field_name) or "")
        item["wbs_hint"] = stored_hint(row)
        out.append(_wbs.attribute(wbs, item, field_name))
    return out


# ── the model, for what the rules cannot place ───────────────────────

def _llm_available() -> bool:
    try:
        from engine import ai_mode
        if ai_mode.effective_mode() == ai_mode.OFF:
            return False
        from engine.llm_keys import platform_key
        return bool(platform_key())
    except Exception:  # pragma: no cover — defensive
        return False


def llm_nodes(wbs, items: list[tuple[str, str]], *,
              force: bool = False) -> dict[str, str]:
    """``{item id: node id}`` from the model, ids validated against ``wbs``.

    ``items`` is ``[(id, text)]``. Never raises; returns what it could.
    """
    if not items or not (force or _llm_available()):
        return {}
    from engine.llm_cache import cached_call
    from engine.llm_client import call_messages
    lines = wbs.prompt_lines(limit=200)
    out: dict[str, str] = {}
    for start in range(0, min(len(items), _MAX_LLM_ITEMS), _BATCH):
        batch = items[start:start + _BATCH]
        prompt = (
            "Assign each test item to the node of this product's work "
            "breakdown structure it belongs to: the page or module it "
            "exercises. Answer with ONE JSON object {\"<item id>\": "
            "\"<node id>\"}. Use only ids from the list. Use a "
            "Cross-cutting id only for a check that applies to the whole "
            "site.\n\nNodes:\n" + "\n".join(lines) + "\n\nItems:\n"
            + "\n".join(f"{item_id}: {text[:240]}" for item_id, text in batch))
        try:
            resp = cached_call(
                _KIND, prompt_version=_PROMPT_VERSION, call=call_messages,
                accept=lambda text: _parse(text) is not None,
                max_tokens=2000,
                messages=[{"role": "user", "content": prompt}])
            text = "\n".join(getattr(b, "text", "") or ""
                             for b in getattr(resp, "content", []) or [])
        except Exception as exc:
            log.warning("wbs attribution: model unavailable: %s", exc)
            break
        parsed = _parse(text) or {}
        known = {item_id for item_id, _ in batch}
        for item_id, node in parsed.items():
            if item_id in known and isinstance(node, str) and node in wbs.nodes:
                out[item_id] = node
    return out


def _parse(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        value = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


# ── preview / apply ──────────────────────────────────────────────────

def plan(project_id: str, *, include_edited: bool = False,
         wbs=None) -> dict[str, Any]:
    """Work out every item's node. Touches nothing."""
    from engine import db as _db
    source = "given"
    if wbs is None:
        wbs, source = project_wbs(project_id)
    if wbs is None:
        return {"ok": False, "source": source}
    kinds: dict[str, Any] = {}
    for kind, loader, meta_kind in (
            ("test_case", _db.load_test_cases, "test_cases"),
            ("checklist_item", _db.load_checklist, "checklist")):
        rows = loader(project_id)
        meta = _db.load_edit_metadata(project_id, meta_kind)
        edited = set() if include_edited else {
            r["id"] for r in rows
            if (meta.get(r["id"]) or {}).get("ai_generated") is False}
        todo = [r for r in rows if r["id"] not in edited]
        nodes = _rule_nodes(wbs, todo, kind)
        # Compared by id: creating the node here would leave an empty
        # "Cross-cutting > General" folder behind when nothing needs it.
        general = f"{_wbs.XC}:general"
        unplaced = [(r["id"], _fpx.strip_prefix(r.get(_FIELDS[kind]) or ""))
                    for r, n in zip(todo, nodes) if n == general]
        by_model = llm_nodes(wbs, unplaced)
        nodes = [by_model.get(r["id"], n) for r, n in zip(todo, nodes)]
        kinds[kind] = {"rows": todo, "nodes": nodes,
                       "skipped_edited": len(edited),
                       "by_model": len(by_model)}
    return {"ok": True, "source": source, "wbs": wbs, "kinds": kinds}


def summarise(project_id: str, planned: dict) -> dict:
    """The JSON a preview returns: counts per top-level node and per node."""
    from engine import db as _db
    if not planned.get("ok"):
        return {"ok": False, "source": planned.get("source")}
    wbs = planned["wbs"]
    out: dict[str, Any] = {"ok": True, "source": planned["source"],
                           "nodes": len(wbs.nodes), "kinds": {}}
    for kind, data in planned["kinds"].items():
        field_name = _FIELDS[kind]
        current = _db.folder_paths(project_id, kind)
        by_path: dict[str, int] = {}
        changed = 0
        for row, node in zip(data["rows"], data["nodes"]):
            path = " > ".join(wbs.path(node))
            by_path[path] = by_path.get(path, 0) + 1
            old = _fpx.parse(row.get(field_name) or "")
            if tuple(old) != wbs.path(node) or \
                    (current.get(row["id"]) or "") != " / ".join(wbs.path(node)):
                changed += 1
        out["kinds"][kind] = {
            "total": len(data["rows"]), "changed": changed,
            "skipped_edited": data["skipped_edited"],
            "by_model": data["by_model"],
            # A list, not an object: JSON responses sort object keys, which
            # turned "largest first" into alphabetical in the browser.
            "by_node": sorted(by_path.items(), key=lambda kv: (-kv[1], kv[0])),
        }
    return out


def apply(project_id: str, planned: dict, *, actor: str | None = None) -> dict:
    """Write the planned prefixes and folders; drop stale empty folders."""
    from engine import db as _db
    if not planned.get("ok"):
        return {"ok": False}
    wbs = planned["wbs"]
    _db.ensure_wbs_folders(project_id, wbs)
    written = {}
    for kind, data in planned["kinds"].items():
        folders = _db.ensure_wbs_folders(project_id, wbs,
                                         node_ids=set(data["nodes"]))
        changes = {}
        for row, node in zip(data["rows"], data["nodes"]):
            folder_id, path = folders.get(node, (None, ()))
            if folder_id is None:
                continue
            changes[row["id"]] = (folder_id, _fpx.with_prefix(
                row.get(_FIELDS[kind]), path or wbs.path(node)))
        written[kind] = _db.write_wbs_attribution(project_id, kind, changes)
    pruned = _db.prune_stale_auto_folders(project_id)
    _db.save_project_wbs(project_id, wbs)
    _db.append_audit(entity="folder", action="attribute_wbs",
                     entity_id=None, project_id=project_id, user_id=actor,
                     diff={"written": [None, written],
                           "pruned_folders": [None, pruned]})
    return {"ok": True, "written": written, "pruned_folders": pruned}


__all__ = ["project_wbs", "stored_hint", "llm_nodes", "plan", "summarise",
           "apply"]
