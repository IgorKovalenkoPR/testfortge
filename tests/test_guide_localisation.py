"""§14 — the guide reads in both languages, and stays that way.

The guide is 4 800 words of documentation, so it is **not** in the
dictionary: `templates/guide.html` is a shell (grid, modal, JavaScript,
every inline style) and the panels live in
`templates/guide/_sections_<lang>.html`, one file per language. Splitting
prose into ~900 dictionary keys would have made every wording fix a
two-file edit with an invented key name in between, and the file's own
comment says why it is plain HTML: so a technical writer can edit it
without touching Python.

That structure has exactly one failure mode, and it is the reason this
file exists: **a panel added to one language and not the other**. Nothing
would break — the reader in the other language simply would not have that
card's content, and a card whose panel is missing opens an empty modal.
So the two files are held to the same set of sections, by derivation
rather than by anyone remembering.

The 53 stale keys, for the record
---------------------------------
Before this change the dictionaries carried 53 translated ``guide_*``
keys that no template rendered — a page that had been rewritten and left
its own translation behind. Measured against the current page: **1 of 54
matched**, and that one was the word "Guide". They described a product
with a Test Plan module (removed in Stage 2) storing projects in a
``/storage`` folder. They are gone; a dictionary that is 15% fiction
lies about coverage, and the next person to read it cannot tell which
part is real.
"""
from __future__ import annotations

import html
import pathlib
import re

import pytest

from engine.i18n import TRANSLATIONS


GUIDE = pathlib.Path(__file__).resolve().parent.parent / "templates" / "guide"
SHELL = GUIDE.parent / "guide.html"
LANGS = ("en", "ua")

SECTION = re.compile(r'<template data-content="([a-z\-]+)"')
CARD = re.compile(r"\('([a-z\-]+)',\s*'([^']+)'\)")
TAGS = re.compile(r"<[^>]+>")
CYRILLIC = re.compile(r"[Ѐ-ӿ]")


def _content(lang: str) -> str:
    return (GUIDE / f"_sections_{lang}.html").read_text(encoding="utf-8")


def _sections(lang: str) -> list[str]:
    return SECTION.findall(_content(lang))


class TestBothLanguagesCarryTheSameGuide:

    def test_the_content_files_exist(self):
        for lang in LANGS:
            path = GUIDE / f"_sections_{lang}.html"
            assert path.is_file(), f"{path} is missing — /guide?lang={lang} " \
                                   f"would render a page of empty modals"

    def test_the_sections_match_exactly(self):
        """Same ids, same order — the modal is keyed by id, and the card
        order is the reading order."""
        assert _sections("en") == _sections("ua")

    def test_every_card_has_a_panel_in_every_language(self):
        """The shell's card list is the source of truth: a card with no
        panel opens an empty modal, and the JS returns silently."""
        shell = SHELL.read_text(encoding="utf-8")
        block = shell[shell.index("guide_cards = ["):shell.index("] %}")]
        cards = [section for section, _emoji in CARD.findall(block)]
        assert cards, "the card list is not where this test thinks it is"
        for lang in LANGS:
            missing = [c for c in cards if c not in _sections(lang)]
            assert not missing, (
                f"{lang}: cards {missing} have no <template data-content>, "
                f"so clicking them opens an empty modal")

    def test_every_card_has_a_label_in_every_language(self):
        shell = SHELL.read_text(encoding="utf-8")
        block = shell[shell.index("guide_cards = ["):shell.index("] %}")]
        for section, _emoji in CARD.findall(block):
            base = "guide_card_" + section.replace("-", "_")
            for lang in LANGS:
                for suffix in ("_title", "_sub"):
                    key = base + suffix
                    assert key in TRANSLATIONS[lang], (
                        f"{key} missing from {lang} — the card would render "
                        f"blank")

    @pytest.mark.parametrize("section", _sections("en"))
    def test_the_ukrainian_panel_is_actually_ukrainian(self, section):
        """Copying the English file to `_sections_ua.html` would satisfy
        every structural check above. This is the one that would not:
        each panel has to contain Cyrillic, and enough of it that a
        code sample or a product name cannot carry the panel."""
        body = re.search(
            r'<template data-content="' + section + r'">(.*?)</template>',
            _content("ua"), re.S)
        assert body, f"{section} is missing from the Ukrainian file"
        text = TAGS.sub(" ", body.group(1))
        cyrillic = len(CYRILLIC.findall(text))
        assert cyrillic > 80, (
            f"the '{section}' panel has {cyrillic} Cyrillic characters — "
            f"it looks like the English text, not a translation")

    def test_the_shell_carries_no_prose(self):
        """The point of the split. If prose creeps back into guide.html it
        is English-only again, and the ratchet in test_i18n_parity would
        be the only thing to notice."""
        shell = SHELL.read_text(encoding="utf-8")
        shell = re.sub(r"\{#.*?#\}", " ", shell, flags=re.S)
        shell = re.sub(r"(?s)<script.*?</script>", " ", shell)
        # Everything left between tags should be markup and expressions.
        text = " ".join(
            chunk.strip() for chunk in
            TAGS.sub("\n", re.sub(r"\{\{.*?\}\}|\{%.*?%\}", " ", shell,
                                  flags=re.S)).split("\n")
            if chunk.strip())
        words = [w for w in re.findall(r"[A-Za-z]{3,}", text)
                 if w not in ("TestForTge",)]
        assert not words, (
            f"guide.html has prose again: {words[:12]}. It belongs in "
            f"templates/guide/_sections_<lang>.html, in both languages.")


class TestTheStaleGuideKeysAreGone:
    """They described a product that no longer exists — measured, not
    assumed: 1 of 54 appeared on the page, and that one was "Guide"."""

    #: The families that survive: the shell's own labels.
    LIVE_PREFIXES = ("guide_card_", "guide_page_")
    LIVE = {"guide", "guide_close", "guide_faq"}

    def test_no_orphan_guide_keys_remain(self):
        for lang in LANGS:
            orphans = sorted(
                key for key in TRANSLATIONS[lang]
                if key.startswith("guide")
                and key not in self.LIVE
                and not key.startswith(self.LIVE_PREFIXES))
            assert not orphans, (
                f"{lang} still carries guide keys nothing renders: "
                f"{orphans}. A dictionary with dead entries cannot be used "
                f"to answer 'is this translated?'")


def _cards() -> set[str]:
    shell = SHELL.read_text(encoding="utf-8")
    block = shell[shell.index("guide_cards = ["):shell.index("] %}")]
    return {section for section, _emoji in CARD.findall(block)}


class TestTheGuideCoversEveryModule:
    """The gap a person found by eye, and nothing else could.

    The sidebar had eleven entries; the guide had eight modules. **Runs,
    Team and Settings shipped undocumented** — and they are the three
    newest, which is the pattern rather than the coincidence: the guide was
    written when the product was single-user, and each capability added
    since arrived without a card. Two of the three are exactly what a QA
    team needs on day one to divide work between people.

    Nothing failed. Both languages agreed, every card had a panel, every
    panel had a label, no key was orphaned. Consistency checks cannot see a
    missing *subject* — only a comparison against the product can. So this
    derives the module list from the navigation the reader actually sees.
    """

    #: Sidebar endpoint → guide card. A module added to the sidebar and not
    #: to the guide fails here; the fix is a card, or a line in EXEMPT with
    #: a reason next to it.
    ENDPOINT_TO_CARD = {
        "index": "dashboard",
        "estimation_page": "estimation",
        "test_cases_page": "test-cases",
        "checklist_page": "checklist",
        "test_execution_page": "test-execution",
        "manual_runs_page": "runs",
        "automation_page": "automation",
        "bug_reports_page": "bug-reports",
        "org_members": "team",
        "org_settings": "settings",
    }
    #: The guide does not document itself.
    EXEMPT = {"guide_page"}

    @staticmethod
    def _sidebar_endpoints() -> list[str]:
        base = (GUIDE.parent / "base.html").read_text(encoding="utf-8")
        start = base.index('<ul class="nav-steps">')
        block = base[start:base.index("</ul>", start)]
        return re.findall(r"url_for\('([a-z_]+)'\)", block)

    def test_the_sidebar_is_where_this_test_thinks_it_is(self):
        """A structural test that silently matches nothing is worse than no
        test: it reports success for a list it never read."""
        found = self._sidebar_endpoints()
        assert len(found) >= 10, f"only found {found} in the sidebar markup"

    def test_every_module_in_the_sidebar_has_a_guide_card(self):
        cards = _cards()
        undocumented = []
        for endpoint in self._sidebar_endpoints():
            if endpoint in self.EXEMPT:
                continue
            card = self.ENDPOINT_TO_CARD.get(endpoint)
            if card is None:
                undocumented.append(f"{endpoint} (unmapped in this test)")
            elif card not in cards:
                undocumented.append(f"{endpoint} → no '{card}' card")
        assert not undocumented, (
            "the sidebar offers modules the guide does not describe: "
            f"{undocumented}. A reader who opens the guide to learn the "
            "product is told it has fewer parts than it has.")


class TestTheGuidePromisesOnlyExportsThatExist:
    """Found while writing the workflow card: the guide offered **.docx and
    Jira-XML**, in three places, in both languages. Neither has ever been
    served — ``routes/generation.py`` answers markdown, html, csv, xlsx and
    feature, bug reports answer csv and markdown, and the word "jira"
    appears in no export handler at all.

    Same defect as the 53 stale keys in this file's docstring, one layer
    up: documentation for a product that does not exist. Worse than a
    missing feature, because a tester promises the client a Jira import and
    discovers it on handover day.

    The vocabulary is derived from the routes rather than listed here, so
    the day a real .docx export lands this test starts allowing the word on
    its own.
    """

    ROUTES = GUIDE.parent.parent / "routes"

    #: Format-looking tokens a panel might use. Only sentences that talk
    #: about exporting are scanned, because .docx and .pdf are legitimate
    #: *inputs* — the estimator and both generators accept them.
    TOKEN = re.compile(
        r"\.?(docx|xlsx|csv|pdf|feature|markdown|html|json)\b"
        r"|jira[\s(-]*xml\)?", re.IGNORECASE)
    ABOUT_EXPORT = re.compile(r"export|експорт", re.IGNORECASE)

    def _served(self) -> set[str]:
        generation = (self.ROUTES / "generation.py").read_text(encoding="utf-8")
        bugs = (self.ROUTES / "bugs.py").read_text(encoding="utf-8")
        served = {fmt.split("-")[0].lower()
                  for fmt in re.findall(r'fmt == "([a-z-]+)"', generation)}
        # The bug exports are two fixed endpoints rather than one
        # parameterised route, so they are read from the filenames they
        # answer with.
        served |= {ext.lower() for ext in re.findall(
            r"filename=bug_reports_\{name\}\.([a-z]+)", bugs)}
        # The project export (Dashboard → Saved Projects → Export) is a zip;
        # what it holds is read from the names it writes into the archive.
        retention = (self.ROUTES.parent / "engine" / "retention.py").read_text(
            encoding="utf-8")
        body = retention.split("def export_project_data", 1)[1]
        body = body.split("\ndef ", 1)[0]
        served |= {ext.lower() for ext in re.findall(
            r'writestr\(\s*f?"[^"]*\.([a-z]+)"', body)}
        return served

    def test_the_project_export_vocabulary_was_read(self):
        """The zip's own contents: without them the scan would refuse the
        Guide's description of Export — or, if the regex broke, quietly
        allow nothing new while looking like it read the file."""
        assert {"json", "txt"} <= self._served()

    def test_the_route_vocabulary_was_actually_read(self):
        served = self._served()
        assert {"markdown", "html", "csv", "xlsx", "feature"} <= served, served
        assert "docx" not in served and "xml" not in served, (
            f"a docx or XML export exists now — {sorted(served)} — so the "
            f"guide may promise it again")

    @staticmethod
    def _blocks(lang: str) -> list[str]:
        """One list item or paragraph per block, tags stripped afterwards.

        Not sentences: splitting prose on "." cuts ".xlsx" and ".docx" into
        fragments that no longer contain the word "export", so the filter
        below discards them and the scan reports nothing. The first version
        of this test did exactly that and passed with the defect restored —
        caught by putting the old claim back, which is the only way that
        kind of hole shows itself.
        """
        return [TAGS.sub(" ", block)
                for block in re.split(r"</li>|</p>|</h3>", _content(lang))]

    @pytest.mark.parametrize("lang", LANGS)
    def test_no_panel_offers_an_export_the_app_cannot_produce(self, lang):
        served = self._served()
        promised: set[str] = set()
        for block in self._blocks(lang):
            if not self.ABOUT_EXPORT.search(block):
                continue
            for match in self.TOKEN.finditer(block):
                token = (match.group(1) or match.group(0)).lower()
                promised.add("xml" if "jira" in token else token)
        unserved = sorted(token for token in promised if token not in served)
        assert not unserved, (
            f"{lang}: the guide offers exports the app does not produce: "
            f"{unserved}. Served formats are {sorted(served)}.")

    def test_the_scan_would_catch_the_defect_it_was_written_for(self):
        """The claim that was live until today, verbatim."""
        old = "Use Export for .xlsx / .csv / .docx / Jira-XML."
        found = {(m.group(1) or m.group(0)).lower()
                 for m in self.TOKEN.finditer(old)}
        assert "docx" in found and any("jira" in f for f in found)


class TestTheDashboardsAdminActionsAreDocumented:
    """ADR 0003 gate 3 (2026-10-15): asked "How do I export a project?"
    and "How do I move test cases to another project?", Ask honestly said
    the materials did not cover it — the Guide never mentioned the Export
    button or the Move-artefacts card. Every gate here compared the Guide
    with itself, so an omission could not show.

    The list is derived from the product: every button or link label inside
    an ``{% if is_admin %}`` block of the Dashboard template must appear in
    the Guide in both languages, as the dictionary renders it.
    """

    DASHBOARD = GUIDE.parent / "index.html"
    BLOCK = re.compile(r"{%-?\s*(if|endif)\b([^%]*)%}")
    CONTROL = re.compile(r"<(button|a)\b[^>]*>(.*?)</\1>", re.S)
    LABEL = re.compile(r"t\.get\('(\w+)'|\bt\.(\w+)")

    #: Labels with a reason not to be named in the Guide.
    EXEMPT = {
        "save": "the inline rename form's own Save",
        "cancel": "the inline rename form's own Cancel",
    }

    def _admin_labels(self) -> list[str]:
        src = self.DASHBOARD.read_text(encoding="utf-8")
        regions, stack = [], []
        for m in self.BLOCK.finditer(src):
            if m.group(1) == "if":
                stack.append((m.group(2).strip(), m.end()))
            else:
                cond, start = stack.pop()
                if cond == "is_admin":
                    regions.append(src[start:m.start()])
        keys: list[str] = []
        for region in regions:
            for control in self.CONTROL.finditer(region):
                for a, b in self.LABEL.findall(control.group(2)):
                    if (a or b) not in keys:
                        keys.append(a or b)
        return keys

    def test_the_derivation_finds_the_actions(self):
        keys = self._admin_labels()
        assert {"export_project", "backup_project", "move_btn"} <= set(keys)

    @pytest.mark.parametrize("lang", LANGS)
    def test_each_admin_action_is_named_in_the_guide(self, lang):
        text = re.sub(r"\s+", " ", html.unescape(
            TAGS.sub(" ", _content(lang)))).lower()
        missing = []
        for key in self._admin_labels():
            if key in self.EXEMPT:
                continue
            label = TRANSLATIONS[lang].get(key)
            assert label, f"{lang}: no dictionary entry for {key}"
            if label.strip().lower() not in text:
                missing.append(f"{key} = {label!r}")
        assert not missing, (
            f"{lang}: Dashboard admin actions the Guide never names: "
            f"{missing}")

    def test_an_exemption_still_names_a_dashboard_control(self):
        keys = set(self._admin_labels())
        stale = sorted(set(self.EXEMPT) - keys)
        assert not stale, f"exemptions for controls that are gone: {stale}"
