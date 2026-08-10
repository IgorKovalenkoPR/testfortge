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
