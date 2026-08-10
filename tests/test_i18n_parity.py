"""M-2 — the Ukrainian half, checked by derivation rather than by list.

The task said "every EN key has a UA key". Measured first, and it was
already true: 593 keys each way, nothing missing. The gap was somewhere
else, and worse:

* **32 keys existed only in Ukrainian.** They render through
  ``t.get('key', 'English fallback')``, so English looked right while the
  English text lived in the markup rather than in the dictionary — invisible
  to any parity check that compares the two files;
* **whole pages never asked the dictionary anything.** Sign-in, the
  invitation page, 403, the team page and the settings page were written
  after the i18n pass and went straight to English. So a Ukrainian user met
  English at the two moments there is no way around — signing in, and being
  refused — and on the page where a team's storage and spend are configured.

* **477 keys were referenced by a template and existed in neither file** —
  3 224 words of English rendering in both languages. Both dictionaries
  agreed about them perfectly, because both were equally missing them.
  That last one is the biggest, and it is the one rule 3 below exists for.

Hence five rules, each derived from the code rather than from a list
somebody has to remember to extend:

1. the key sets are equal **in both directions**;
2. a Ukrainian value that is byte-identical to the English one is either a
   term this product deliberately keeps in English (allowlisted here, with
   the reason) or an untranslated leftover;
3. every key a template asks for exists in both dictionaries — the rule
   that catches ``t.get('nav_runs', 'Runs')``, which no comparison of the
   two files ever could;
4. ``%s``/``%(name)s`` placeholders match per plural form — a mismatch is
   not a typo, it is a ``TypeError`` on a rendered page;
5. no template renders visible English that never passed through ``t``.
   This began as a ratchet — a per-file count that could fall and not
   rise — and became a rule once every user-facing page reached zero
   (§15). The two escape hatches are named and each carries its reason:
   ``NEUTRAL_IN_MARKUP`` for strings identical in every language (a suite
   tag the database stores, a shell command, another product's button),
   and ``DEV_ONLY_TEMPLATES`` for a page no user can reach.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from engine.i18n import TRANSLATIONS
from engine.i18n import plural as _plural
from engine.i18n.plural import forms as _forms


EN = TRANSLATIONS["en"]
UA = TRANSLATIONS["ua"]

#: Values that are the same in both languages on purpose.
#:
#: Two groups, and the distinction is the point. Proper nouns and
#: abbreviations have no Ukrainian form at all. The QA vocabulary —
#: statuses and severities — is the one this team writes in English in its
#: own test plans, so translating it here would make the product disagree
#: with the documents it produces. Anything not named here and identical is
#: a leftover.
DELIBERATELY_ENGLISH = {
    # Proper nouns, abbreviations, and words spelled the same way
    "chat_title": "Tedgie is a name",
    "us_id": "ID",
    "est_min": "MIN", "est_max": "MAX",
    "login_email": "Email is used as-is in Ukrainian",
    "reset_email_label": "Email",
    "om_col_email": "Email",
    "om_invite_email_placeholder": "an example address, not prose",
    "os_storage_endpoint": "the S3 field is called Endpoint everywhere",
    "domain_ecommerce": "E-Commerce",
    "domain_edtech": "EdTech / E-Learning",
    "brand_aria": "the brand's own tagline",
    "automation_base_url": "BASE_URL is the variable's name",
    "te_base_url": "the field is called Base URL in both",
    "automation_broken": "Broken is an Allure result state, like Failed",
    "review_session_url": "URL",
    "te_status_auto": "Auto, the value of the status field",
    "te_mode_walkthrough": "QA walkthrough — the mode's name in this product",
    "chat_aria": "the assistant's own name and tagline",
    "dm_unit_tc": "TC", "dm_unit_cl": "CL",
    "te_col_id": "ID",
    "te_env_web": "Web is the platform's name",
    "te_formats": "a list of file extensions",
    "bug_status_open": "bug statuses stay English, like the severities",
    # The QA vocabulary this team writes in English
    "nav_user_stories": "User Story is used untranslated in QA here",
    "dash_user_stories": "as above",
    "us_stories_label": "as above",
    "user_story": "as above",
    "edge_case": "Edge Case",
    # (Five entries left this list in §14 along with the dead keys they
    #  described — te_passed, te_failed, te_blocked, est_persona_label and
    #  lang_en were translated and rendered nowhere.)
    "passed": "test statuses stay English, as in the team's test plans",
    "failed": "as above", "passed_but": "as above", "blocked": "as above",
    "tm_pass": "as above", "tm_fail": "as above", "tm_pass_but": "as above",
    "tm_bugs_critical": "severity names stay English",
    "tm_bugs_major": "as above", "tm_bugs_minor": "as above",
    "tm_bugs_low": "as above",
}

#: Text in markup that is identical in every language, with the reason.
#: The counterpart of DELIBERATELY_ENGLISH for strings that are not
#: dictionary values at all: product vocabulary the database stores, other
#: software's own labels, and the brand split across two tags.
NEUTRAL_IN_MARKUP = {
    # Suite tags. Stored in the database and matched by name — a
    # translated tag would be a different tag.
    "Smoke": "a suite tag, stored as this string",
    "Regression": "as above",
    "E2E": "as above",
    # Platform names.
    "iOS": "Apple's name for it",
    "Android": "Google's name for it",
    # Playwright codegen's own toolbar buttons: the guide tells a tester to
    # click them, so the guide must call them what the browser calls them.
    "Assert visible": "the label on codegen's own button",
    "Assert text": "as above",
    "Assert URL": "as above",
    # The brand wordmark is split across three spans so both "Testfort"
    # and "Forge" can be read out of it.
    "TestFor": "half of the split wordmark",
    "ge": "the other half",
    # Shell commands and endpoints outside <code> in the guide's how-to.
    "npm ci    npm test": "a command",
    "npm run upload": "a command",
    "POST /automation/allure-results": "an endpoint",
    # Package names in the diagnostics banner, and the formula caption.
    "pdf2image:": "a package name",
    "poppler:": "a package name",
    "figma.com/settings": "a URL a reader types",
    "= buffer   total_tc   minutes_per_tc / 60 + 1": "a formula",
    ".r2.cloudflarestorage.com\"": "half an example endpoint",
    "sk-ant-…": "the shape of an API key",
    "RECORDER_ENABLED=1 python -m tools.tfg_record --project   --tc   --url  START_URL":
        "a command line",
}

PLACEHOLDER = re.compile(r"%(?:\((\w+)\))?[-#0 +]*\d*(?:\.\d+)?[sdfr]")


class TestTheDictionariesAgree:

    def test_every_key_exists_in_both_languages(self):
        only_en = sorted(set(EN) - set(UA))
        only_ua = sorted(set(UA) - set(EN))
        assert not only_en and not only_ua, (
            f"the dictionaries have drifted apart.\n"
            f"  only in English: {only_en}\n"
            f"  only in Ukrainian: {only_ua}\n"
            f"A key missing from English still renders, because templates "
            f"pass a fallback — which is how 32 of these went unnoticed.")

    def test_no_ukrainian_value_is_the_english_one_by_accident(self):
        leftovers = sorted(k for k in EN
                           if EN[k] == UA[k] and k not in DELIBERATELY_ENGLISH)
        assert not leftovers, (
            f"these Ukrainian values are byte-identical to the English: "
            f"{leftovers}. Translate them, or add them to "
            f"DELIBERATELY_ENGLISH with the reason — a term kept in English "
            f"on purpose and one nobody got to look the same from here.")

    def test_the_allowlist_has_no_stale_entries(self):
        """The opposite failure: an allowlist that outlives its reason.

        Once a key is translated, its entry here is a claim about the
        product that is no longer true, and the next person reads it as
        permission.
        """
        stale = sorted(k for k in DELIBERATELY_ENGLISH
                       if k not in EN or EN[k] != UA[k])
        assert not stale, (
            f"{stale} are allowlisted as deliberately English but are not "
            f"identical any more (or no longer exist). Remove the entries.")


class TestOneVoice:
    """The product addresses the reader one way, and it is «ви».

    Measured rather than chosen (§13): 48 formal forms against one informal
    in the dictionary as it stood. §15 found the exception — Tedgie's own
    greeting said «Запитуй», the one string a first-time visitor is
    guaranteed to read — plus a hint that said «додай». Both are «ви» now,
    and this keeps them there.

    The greeting also listed the modules by their English names while the
    sidebar showed Ukrainian ones, which is the same defect one layer up: a
    reader told to ask about "Test Cases" will not find that item in the
    menu.
    """

    #: Second-person-singular imperatives and possessives. Word-bounded, so
    #: «перевірка» and «додайте» do not match.
    INFORMAL = re.compile(
        r"\b(запитуй|введи|натисни|обери|перевір|спробуй|додай|відкрий|"
        r"дивись|скористайся|твій|твоя|твої|твоє|тебе|тобі)\b",
        re.IGNORECASE)

    def test_no_ukrainian_string_uses_the_informal_you(self):
        offenders = sorted(
            key for key, value in UA.items()
            if isinstance(value, str) and self.INFORMAL.search(value))
        assert not offenders, (
            f"{offenders} address the reader as «ти» while the rest of the "
            f"product uses «ви». One product, one voice.")

    def test_the_pattern_would_catch_something(self):
        assert self.INFORMAL.search("Запитуй про модулі")
        assert not self.INFORMAL.search("Перевірка вимог і додайте файл")


class TestEveryKeyATemplateAsksForExists:
    """The hole the other three rules could not see, and it was the big one.

    ``{{ t.get('nav_runs', 'Runs') }}`` referenced a key that was in
    **neither** dictionary. Both files agreed perfectly — they were equally
    missing it — so the parity rule above passed, the scanner below saw an
    expression rather than text, and the sidebar said "Runs" in Ukrainian.

    Measured when this test was first written: **477 keys**, 3 224 words of
    English, rendering in both languages. That, and not the missing-key
    count the task was written around, was M-2.

    The fallback is what makes it invisible: ``t.get(key, 'English')`` is
    the right defensive shape for a page and the wrong one for a
    translation process, because nothing ever fails.
    """

    GET = re.compile(r"\bt\.get\(\s*['\"]([a-z0-9_]+)['\"]")
    ATTR = re.compile(r"\bt\.([a-z][a-z0-9_]*)\b")

    #: ``t`` is also the conventional name for a loop variable in
    #: JavaScript, so ``t.style`` and ``t.src`` inside a <script> are not
    #: translations at all. Script and style bodies are stripped first —
    #: without it this test demanded dictionary entries for "src" and
    #: "dataset".
    @classmethod
    def _referenced(cls) -> dict[str, str]:
        keys: dict[str, str] = {}
        for path in sorted(TEMPLATES.rglob("*.html")):
            src = path.read_text(encoding="utf-8", errors="replace")
            src = _SCRIPT.sub(" ", _STYLE.sub(" ", src))
            for key in cls.GET.findall(src):
                keys.setdefault(key, path.name)
            for key in cls.ATTR.findall(src):
                if key != "get":
                    keys.setdefault(key, path.name)
        # A key the template composes (`'dash_period_' ~ period`) is only
        # ever seen as its prefix. The full names are in the dictionaries,
        # listed one per possible value, so they stay checkable — but the
        # prefix itself is not a key and must not be demanded as one.
        return {k: v for k, v in keys.items() if not k.endswith("_")}

    def test_the_scanner_finds_the_references(self):
        assert len(self._referenced()) > 400, (
            "the reference scan found almost nothing; the pattern has "
            "stopped matching how templates read translations")

    def test_no_template_asks_for_a_key_that_does_not_exist(self):
        missing = sorted((key, where) for key, where in
                         self._referenced().items()
                         if key not in EN or key not in UA)
        assert not missing, (
            f"these keys are asked for by a template and exist in neither "
            f"dictionary, so their English fallback renders in every "
            f"language: {missing}")


class TestNoKeyOutlivesItsUser:
    """The other direction: a dictionary entry nothing renders.

    §14 measured 126 of them — 53 ``guide_*`` keys describing a product
    with a Test Plan module that Stage 2 removed, and 73 more from earlier
    rewrites of the automation, estimation and execution pages. Every one
    was translated into Ukrainian, so the dictionary looked more complete
    than the product was, and "is this page translated?" could not be
    answered from the files.

    Two kinds of key are legitimately invisible here and are named rather
    than guessed at: those a template composes at render time. Everything
    else must have a literal user.
    """

    #: Prefixes assembled in a template (`'dash_period_' ~ period`), whose
    #: full names therefore appear nowhere in the source.
    COMPOSED = ("dash_period_", "guide_card_")

    #: Where a key may be used from. Deliberately wider than the two
    #: template dirs: flash messages live in ``routes/``, and the recorder
    #: extension has its own strings.
    SOURCE_DIRS = ("templates", "routes", "engine", "static", "tools",
                   "extension", "scripts")
    SUFFIXES = {".py", ".html", ".js", ".ts", ".json"}

    @classmethod
    def _source(cls) -> str:
        root = pathlib.Path(__file__).resolve().parent.parent
        chunks = [(root / "app.py").read_text(encoding="utf-8",
                                              errors="replace")]
        for name in cls.SOURCE_DIRS:
            for path in (root / name).rglob("*"):
                if (path.is_file() and path.suffix in cls.SUFFIXES
                        and "__pycache__" not in str(path)
                        and "i18n" not in str(path)):
                    chunks.append(path.read_text(encoding="utf-8",
                                                 errors="replace"))
        return "\n".join(chunks)

    def test_every_key_has_a_user(self):
        source = self._source()
        quoted = set(re.findall(r"""['"]([a-z0-9_]{1,60})['"]""", source))
        attrs = set(re.findall(r"\bt\.([a-z][a-z0-9_]*)", source))
        orphans = sorted(k for k in EN
                         if not k.startswith(self.COMPOSED)
                         and k not in quoted and k not in attrs)
        assert not orphans, (
            f"{len(orphans)} keys are translated and rendered nowhere: "
            f"{orphans}. Delete them, or — if a template composes the name "
            f"— add the prefix to COMPOSED with the reason.")

    def test_the_scan_reads_the_source(self):
        """A green run because the scan found no files would pass this
        file's main rule vacuously."""
        assert len(self._source()) > 500_000

    def test_the_composed_prefixes_are_really_composed(self):
        """An entry here is permission to skip the rule, so it has to be
        earned: the prefix must appear in a template next to a `~`."""
        root = pathlib.Path(__file__).resolve().parent.parent / "templates"
        markup = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                           for p in root.rglob("*.html"))
        for prefix in self.COMPOSED:
            assert re.search(re.escape(prefix) + r"['\"]\s*~", markup), (
                f"{prefix!r} is listed as composed and no template "
                f"concatenates it — the exemption is stale")


class TestFormattingCannotBlowUpInOneLanguage:
    """A placeholder mismatch is not cosmetic — it is a 500.

    ``{{ t.key|format(x) }}`` on a value whose translation dropped the
    ``%s`` raises ``TypeError: not all arguments converted``, and it does it
    only for the language that has the bad string. Nobody running the suite
    in English would see it.
    """

    @staticmethod
    def _slots(value: str) -> list[list[str]]:
        """Placeholders per plural form — comparing whole values would
        compare English's two forms against Ukrainian's three."""
        return [sorted(PLACEHOLDER.findall(form)) for form in _forms(value)]

    def test_each_form_takes_the_same_arguments(self):
        broken = []
        for key in EN:
            en_slots = self._slots(str(EN[key]))
            ua_slots = self._slots(str(UA[key]))
            # Ukrainian's extra plural form must take the same arguments as
            # the English form it corresponds to (the last one).
            for i, slots in enumerate(ua_slots):
                expected = en_slots[min(i, len(en_slots) - 1)]
                if slots != expected:
                    broken.append((key, expected, slots))
        assert not broken, (
            f"the two languages want different arguments: {broken}. "
            f"Rendering the second one raises TypeError, in that language "
            f"only.")

    def test_plural_keys_have_a_form_for_every_ukrainian_case(self):
        """Ukrainian needs three; two would silently render "5 учасники"."""
        wrong = []
        for key in EN:
            if "|" not in str(EN[key]) and "|" not in str(UA[key]):
                continue
            if len(_forms(str(EN[key]))) != 2:
                wrong.append((key, "en", len(_forms(str(EN[key])))))
            if len(_forms(str(UA[key]))) != 3:
                wrong.append((key, "ua", len(_forms(str(UA[key])))))
        assert not wrong, (
            f"plural keys with the wrong number of forms: {wrong}. English "
            f"takes two (one|other), Ukrainian three (one|few|many).")

    @pytest.mark.parametrize("count,expected", [
        (1, "учасник"), (2, "учасники"), (4, "учасники"), (5, "учасників"),
        # The cases that catch people out: 11–14 take the *many* form even
        # though they end in 1–4.
        (11, "учасників"), (12, "учасників"), (14, "учасників"),
        (21, "учасник"), (22, "учасники"), (25, "учасників"), (0, "учасників"),
    ])
    def test_the_ukrainian_rule_itself(self, count, expected):
        assert _plural("ua", count, "om_member_word") == expected


# ── Rule 4: no page goes straight to English ─────────────────────────

TEMPLATES = pathlib.Path(__file__).resolve().parent.parent / "templates"

#: Files that still carry hardcoded English, with how much of it.
#:
#: A ratchet, not an exemption: the numbers may fall and may not rise, so
#: the remaining debt is visible in one place and a *new* page fails
#: immediately because it is not listed at all. Counts are of visible text
#: chunks as ``_visible_text`` measures them, which is a proxy — the point
#: is the direction, not the absolute figure.
#: Templates allowed to carry English, with the reason each is allowed.
#:
#: This was a ratchet — a per-file count that could fall and not rise —
#: while sixteen pages still held English. They are all at zero now, so a
#: number would only be somewhere for new debt to hide. What is left is a
#: rule: a template not named here renders no English of its own, and the
#: one that is named has to earn it.
DEV_ONLY_TEMPLATES = {
    "_ie_harness.html": "E4.2 development harness — reachable only with "
                        "EDITORS_ENABLED on and FLASK_DEBUG=1, so no user "
                        "of either language can open it",
}

#: The guide's panels are 4 800 words of documentation, held in one file
#: per language under ``templates/guide/`` (§14). They are *localised
#: content*, not untranslated markup, so counting English words in
#: ``_sections_en.html`` would be counting the English guide and finding
#: it English. Their guarantee is a different one and lives in
#: ``tests/test_guide_localisation.py``: the two files carry the same
#: sections, every card has a panel in both, and each Ukrainian panel
#: actually contains Ukrainian.
LOCALISED_CONTENT_DIR = "guide"

_COMMENT = re.compile(r"\{#.*?#\}", re.S)
#: HTML comments too. A comment is not rendered, so English inside one is
#: not English a reader meets — and one such comment in test_cases.html
#: made this scanner report two phantom findings.
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
#: ``<code>`` and ``<pre>`` hold commands, identifiers and file names.
#: Translating ``npm run upload`` would break it.
_CODE = re.compile(r"(?is)<(code|pre)\b.*?</>")
_BLOCK = re.compile(r"\{%.*?%\}", re.S)
_EXPR = re.compile(r"\{\{.*?\}\}", re.S)
_SCRIPT = re.compile(r"<script\b.*?</script>", re.S | re.I)
_STYLE = re.compile(r"<style\b.*?</style>", re.S | re.I)
_TAG = re.compile(r"<[^>]+>", re.S)
_WORDS = re.compile(r"[A-Za-z]{2,}")
_VISIBLE_ATTR = re.compile(r'(?:placeholder|title|aria-label|alt)="([^"{}]+)"')
_BRAND = re.compile(r"TestForTge|TestFortge|Tedgie", re.I)
#: Strings that are identical in every language: example URLs and
#: addresses, glob patterns, and bare identifiers with a trailing colon.
_NEUTRAL = re.compile(
    r"(?:https?://\S+|\*?/[\w*/.\-]+\*?|[\w.\-]+@[\w.\-]+|[a-z_]+_id:?)\s*")
_HAS_LOWER = re.compile(r"[a-z]")
_ENTITY = re.compile(r"&[a-zA-Z]+;|&#\d+;")


def _visible_text(src: str) -> list[str]:
    """Text a reader sees that no expression produced.

    Comments, script and style bodies go first — the same order
    ``test_capacity`` and ``test_bug_attachments`` both had to learn, since
    slicing before stripping leaves a half-open comment behind.
    """
    stripped = _COMMENT.sub(" ", src)
    stripped = _HTML_COMMENT.sub(" ", stripped)
    stripped = _SCRIPT.sub(" ", stripped)
    stripped = _STYLE.sub(" ", stripped)
    stripped = _CODE.sub(" ", stripped)
    stripped = _BLOCK.sub(" ", stripped)
    stripped = _EXPR.sub(" ", stripped)
    attrs = _VISIBLE_ATTR.findall(stripped)
    chunks = [line.strip() for line in _TAG.sub("\n", stripped).split("\n")]
    chunks.extend(a.strip() for a in attrs)
    # The product's own name is not English, it is a name. Every page title
    # carries it, so counting it would hold every file permanently over
    # budget for a word that must never be translated.
    cleaned = []
    for chunk in chunks:
        # `&rarr;` is an arrow, not a word, and `foo="bar">` is a fragment
        # of markup the tag stripper could not close — `<account>` inside a
        # placeholder URL looks like a tag and takes the rest of the line
        # with it. Counting either as untranslated English would inflate
        # every budget below with things nobody can translate.
        if '="' in chunk:
            continue
        chunk = _ENTITY.sub(" ", _BRAND.sub("", chunk)).strip()
        if not chunk or not _WORDS.search(chunk):
            continue
        # An example URL, an example address or a glob is the same string
        # in every language. Counting them would leave permanent budget
        # entries for text nobody can translate.
        if _NEUTRAL.fullmatch(chunk):
            continue
        # No lower-case letter anywhere: an identifier or a unit — ID,
        # BLK, UTC, SP, MB, run_id. A word a reader recognises has a
        # lower-case letter in it.
        if not _HAS_LOWER.search(chunk):
            continue
        if chunk in NEUTRAL_IN_MARKUP:
            continue
        cleaned.append(chunk)
    return cleaned


class TestNoPageGoesStraightToEnglish:

    @staticmethod
    def _counts() -> dict[str, int]:
        return {path.name: len(_visible_text(
                    path.read_text(encoding="utf-8", errors="replace")))
                for path in sorted(TEMPLATES.rglob("*.html"))
                if path.parent.name != LOCALISED_CONTENT_DIR}

    def test_no_template_renders_english_of_its_own(self):
        over = {name: count for name, count in self._counts().items()
                if count and name not in DEV_ONLY_TEMPLATES}
        assert not over, (
            f"hardcoded English: {over}. Move the strings into "
            f"engine/i18n/en.py and ua.py — or, if they are the same in "
            f"every language (a suite tag, a command, another product's "
            f"button), add them to NEUTRAL_IN_MARKUP with the reason.")

    def test_the_dev_only_exemption_is_earned(self):
        """A file is exempt because a user cannot reach it, so the gate
        that keeps them out has to be visible in the file itself."""
        for name in DEV_ONLY_TEMPLATES:
            path = TEMPLATES / name
            assert path.is_file(), f"{name} is exempt and does not exist"
            text = path.read_text(encoding="utf-8")
            assert "EDITORS_ENABLED" in text and "FLASK_DEBUG" in text, (
                f"{name} is exempt as development-only and does not say so; "
                f"if a user can reach it, it needs translating like every "
                f"other page")

    def test_the_scanner_still_finds_text(self):
        """A green run because the regexes stopped matching would be the
        least useful kind of green — the failure this file exists for."""
        guide = (TEMPLATES / "guide" / "_sections_en.html").read_text(
            encoding="utf-8")
        assert len(_visible_text(guide)) > 100

    def test_the_neutral_list_has_no_stale_entries(self):
        """An exemption nothing uses is a claim about the markup that is no
        longer true."""
        markup = "\n".join(
            path.read_text(encoding="utf-8", errors="replace")
            for path in TEMPLATES.rglob("*.html"))
        stale = sorted(text for text in NEUTRAL_IN_MARKUP
                       if text.split()[0] not in markup)
        assert not stale, (
            f"{stale} are exempt as language-neutral and no longer appear "
            f"in any template")
