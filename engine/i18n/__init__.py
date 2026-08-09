"""
TestFortge — Internationalization (i18n).

EN/UA language support. This package preserves the original flat API:
    from engine.i18n import get_lang, TRANSLATIONS
so no callers need to change.

Per-language dictionaries live in sibling modules (``en.py``, ``ua.py``)
so each language can be edited independently without merge conflicts
on a 2000-line monolith.
"""
from .en import TRANSLATIONS as _EN
from .plural import pick as pick_plural
from .ua import TRANSLATIONS as _UA

TRANSLATIONS: dict = {
    "en": _EN,
    "ua": _UA,
}


def plural(lang_code: str, count: int, key: str) -> str:
    """The form of *key* that agrees with *count* in *lang_code*.

    Templates reach this through the ``plural`` global (see ``app.py``),
    which supplies the active language::

        {{ n }} {{ plural(n, 'om_member_word') }}

    A key with no plural forms declared returns its single form, and an
    unknown key returns an empty string rather than raising: a missing
    translation must not take the page down with it.
    """
    table = get_lang(lang_code)
    raw = table.get(key) or TRANSLATIONS["en"].get(key) or ""
    return pick_plural(lang_code, count, raw) if raw else ""


def get_lang(lang_code: str = "en") -> dict:
    """Return the translation dictionary for ``lang_code``.

    Falls back to English if the requested language is unknown.
    """
    return TRANSLATIONS.get(lang_code, TRANSLATIONS["en"])


__all__ = ["TRANSLATIONS", "get_lang", "plural"]
