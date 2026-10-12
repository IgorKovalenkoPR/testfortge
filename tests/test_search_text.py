"""engine.search.text — normalisation, language, stems, translation, typos.

Pure unit tests: no database, no Flask app (ADR 0003 §7.1).
"""
from __future__ import annotations

import json
import re

import pytest

from engine import istqb_rag
from engine.search import text as T


# ── The tokeniser moved out of istqb_rag without changing ─────────────

#: The definitions istqb_rag had before the move, copied verbatim. The
#: retriever's thresholds were tuned on exactly this behaviour, so the move
#: has to be a move and nothing else.
_ORIGINAL_TOKEN = re.compile(r"[A-Za-zЀ-ӿ][A-Za-z0-9Ѐ-ӿ\-]{1,}", re.UNICODE)
_ORIGINAL_STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "of", "to", "and", "or", "in",
    "for", "on", "at", "with", "by", "as", "be", "it", "from", "this",
    "that", "these", "those", "what", "which", "how",
    "є", "та", "і", "у", "в", "що", "як", "це", "до", "на", "по",
})


def _original_tokenize(text: str) -> list[str]:
    return [t.lower() for t in _ORIGINAL_TOKEN.findall(text or "")
            if t.lower() not in _ORIGINAL_STOPWORDS]


class TestIstqbTokeniserMove:
    SAMPLES = [
        "What is the difference between a test case and a test condition?",
        "Що таке еквівалентне розбиття та аналіз граничних значень?",
        "Boundary-value analysis (BVA) — 2-value vs 3-value, ISTQB CTFL 4.0",
        "", "a", "Тестування  мобільних\nзастосунків: iOS/Android",
        "ёжик Ёлка ЪЫЭ ґанок Їжак",
    ]

    def test_istqb_rag_uses_the_shared_definitions(self):
        assert istqb_rag._tokenize is T.tokenize
        assert istqb_rag._TOKEN is T.TOKEN_RE
        assert istqb_rag._STOPWORDS is T.ISTQB_STOPWORDS

    def test_pattern_and_stopwords_are_unchanged(self):
        assert T.TOKEN_RE.pattern == _ORIGINAL_TOKEN.pattern
        assert T.TOKEN_RE.flags == _ORIGINAL_TOKEN.flags
        assert T.ISTQB_STOPWORDS == _ORIGINAL_STOPWORDS

    @pytest.mark.parametrize("sample", SAMPLES)
    def test_output_is_identical(self, sample):
        assert T.tokenize(sample) == _original_tokenize(sample)

    def test_istqb_search_still_ranks(self):
        hits = istqb_rag.search("equivalence partitioning", k=3)
        assert hits, "the ISTQB retriever stopped finding anything"


# ── Normalisation ─────────────────────────────────────────────────────

class TestNormalise:
    def test_case_whitespace_and_nfkc(self):
        assert T.normalise("  ＬＯＧＩＮ \t Fails\n") == "login fails"

    def test_yo_folds_to_ye(self):
        assert T.normalise("Ещё всё") == "еще все"

    @pytest.mark.parametrize("apostrophe", ["'", "ʼ", "’", "‘", "`"])
    def test_apostrophes_fold(self, apostrophe):
        assert T.normalise(f"об{apostrophe}єкт") == "об'єкт"

    def test_empty(self):
        assert T.normalise("") == ""
        assert T.normalise(None) == ""


# ── Language detection ────────────────────────────────────────────────

class TestDetectLang:
    @pytest.mark.parametrize("text, lang", [
        ("Login fails with valid credentials", "en"),
        ("", "en"),
        ("не можу увійти", "ua"),
        ("Не працює оплата карткою", "ua"),
        ("ґанок", "ua"),
        ("не могу войти в систему", "ru"),
        ("съешь этих булок", "ru"),
        ("ещё", "ru"),
        # Cyrillic with no marker letter: Russian (Ukrainian prose has і).
        ("вход", "ru"),
        ("Login fails — не працює вхід", "mixed"),
        ("і ы", "mixed"),
        # One Cyrillic word in an English sentence is still English.
        ("The Save button on the profile page does nothing (кнопка)", "en"),
    ])
    def test_detect(self, text, lang):
        assert T.detect_lang(text) == lang


# ── Tokens and stems ──────────────────────────────────────────────────

class TestSearchTokens:
    def test_stopwords_and_negation_dropped(self):
        assert T.search_tokens("The user can not log in") == ["user", "login"]
        assert T.search_tokens("не працює вхід") == ["працює", "вхід"]

    @pytest.mark.parametrize("raw, token", [
        ("log in", "login"), ("log-in", "login"), ("Log On", "login"),
        ("log out", "logout"), ("sign up", "signup"), ("sign in", "signin"),
        ("E-mail", "email"), ("drop-down", "dropdown"), ("pop up", "popup"),
    ])
    def test_compounds_join(self, raw, token):
        assert T.search_tokens(raw) == [token]

    def test_apostrophe_word_is_one_token(self):
        assert T.search_tokens("обов’язкове поле") == ["обов'язкове", "поле"]

    def test_digits_kept(self):
        assert T.search_tokens("step 3 returns 500") == ["step", "3",
                                                          "returns", "500"]


class TestStemming:
    @pytest.mark.parametrize("word, stem", [
        ("оплатою", "оплат"), ("оплати", "оплат"), ("оплата", "оплат"),
        ("реєстрації", "реєстрац"), ("реєстрація", "реєстрац"),
        ("паролем", "парол"), ("пароль", "парол"),
        ("кошика", "кошик"), ("кошику", "кошик"),
        ("працює", "прац"), ("працювати", "прац"),
        ("помилки", "помилк"), ("помилку", "помилк"),
    ])
    def test_ua_light_stemmer(self, word, stem):
        assert T.stem_ua(word) == stem

    def test_ua_short_words_untouched(self):
        assert T.stem_ua("дата") == "дат"
        assert T.stem_ua("сайт") == "сайт"
        assert T.stem_ua("вхід") == "вхід"
        assert T.stem_ua("на") == "на"

    def test_ua_never_strips_below_three_letters(self):
        for word in ("дати", "мова", "поле", "ціна"):
            assert len(T.stem_ua(word)) >= 3, word

    def test_english_snowball(self):
        assert T.stem("payments", "en") == T.stem("payment", "en")
        assert T.stem("failing", "en") == T.stem("fails", "en")

    def test_russian_snowball(self):
        assert T.stem("входа", "ru") == T.stem("вход", "ru")
        assert T.stem("ошибки", "ru") == T.stem("ошибка", "ru")

    def test_token_script_picks_the_stemmer(self):
        # An English word inside a Ukrainian sentence is stemmed as English.
        assert T.stem("payments", "ua") == T.stem("payments", "en")
        # A word with і is Ukrainian even in a document detected as Russian.
        assert T.stem("реєстрації", "ru") == "реєстрац"


# ── Translation ───────────────────────────────────────────────────────

class TestTranslation:
    @pytest.mark.parametrize("word, english", [
        ("вхід", "login"), ("входу", "login"), ("увійти", "login"),
        ("вход", "login"), ("войти", "login"),
        ("оплатою", "payment"), ("оплаты", "payment"),
        ("кошика", "cart"), ("корзину", "cart"),
        ("пароля", "password"), ("помилки", "error"), ("ошибку", "error"),
    ])
    def test_word(self, word, english):
        assert T.translate_word(word) == english

    def test_latin_and_unknown_words_are_not_translated(self):
        assert T.translate_word("login") is None
        assert T.translate_word("абвгдежз") is None

    def test_translate_returns_the_mapping_for_the_ui(self):
        english, mapping = T.translate(T.search_tokens("не працює вхід"), "ua")
        assert mapping == {"працює": "works", "вхід": "login"}
        assert english == ["works", "login"]
        assert T.format_translation(mapping) == "працює → works, вхід → login"

    def test_multiword_target_is_split(self):
        english, mapping = T.translate(["промокод"], "ua")
        assert english == ["promo", "code"]
        assert mapping == {"промокод": "promo code"}

    def test_analyse_appends_never_replaces(self):
        a = T.analyse("не можу увійти")
        assert a.lang == "ua"
        assert T.stem_ua("увійти") in a.stems   # the original survives
        assert "login" in a.translated_stems  # the translation is added
        assert a.all_stems[:len(a.stems)] == a.stems
        assert a.mapping == {"увійти": "login"}
        assert "login" in a.lex().split()

    def test_lexicons_are_large_enough_and_well_formed(self):
        for name in ("xlate_ua.json", "xlate_ru.json"):
            raw = json.loads((T.LEXICON_DIR / name).read_text(encoding="utf-8"))
            entries = raw["entries"]
            assert len(entries) >= 300, (name, len(entries))
            for key, value in entries.items():
                assert key == key.strip() and " " not in key, (name, key)
                assert value.strip() and value.isascii(), (name, key, value)


# ── ID fast path ──────────────────────────────────────────────────────

class TestIdFastPath:
    @pytest.mark.parametrize("query, parsed", [
        ("BUG-17", ("BUG", 17)), ("bug17", ("BUG", 17)), ("tc-012", ("TC", 12)),
        ("CL_3", ("CL", 3)), ("run #42", ("RUN", 42)), (" TC 7 ", ("TC", 7)),
    ])
    def test_ids(self, query, parsed):
        assert T.parse_id_query(query) == parsed

    @pytest.mark.parametrize("query", ["bug fix", "TC", "BUG-17 login",
                                       "XY-1", "", None])
    def test_not_ids(self, query):
        assert T.parse_id_query(query) is None


# ── Typo correction ───────────────────────────────────────────────────

class TestTypos:
    VOCAB = ["login", "logout", "password", "checkout", "cart", "card",
             "payment", "registration", "search", "logo"]

    def test_levenshtein(self):
        assert T.levenshtein("kitten", "sitting") == 3
        assert T.levenshtein("", "abc") == 3
        assert T.levenshtein("same", "same") == 0
        assert T.levenshtein("kitten", "sitting", limit=1) == 2

    def test_suggestions(self):
        fixes = T.did_you_mean(["logn", "pasword", "chekout", "paymnet",
                                "cart", "zzzzzz"], self.VOCAB)
        assert fixes == {"logn": "login", "pasword": "password",
                         "chekout": "checkout", "paymnet": "payment"}

    def test_short_words_allow_one_edit_only(self):
        corrector = T.TypoCorrector(self.VOCAB)
        assert corrector.suggest("crat") is None      # 2 edits from "cart"
        assert corrector.suggest("carr") in ("cart", "card")

    def test_frequency_breaks_a_tie(self):
        corrector = T.TypoCorrector({"cart": 50, "card": 3})
        assert corrector.suggest("carx") == "cart"
        corrector = T.TypoCorrector({"cart": 3, "card": 50})
        assert corrector.suggest("carx") == "card"

    def test_known_words_digits_and_tiny_tokens_are_left_alone(self):
        assert T.did_you_mean(["login", "500", "lo"], self.VOCAB) == {}

    def test_cyrillic(self):
        assert T.did_you_mean(["пароьл"], ["пароль", "профіль"]) == {
            "пароьл": "пароль"}
