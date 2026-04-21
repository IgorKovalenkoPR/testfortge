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
from .ua import TRANSLATIONS as _UA

TRANSLATIONS: dict = {
    "en": _EN,
    "ua": _UA,
}


def get_lang(lang_code: str = "en") -> dict:
    """Return the translation dictionary for ``lang_code``.

    Falls back to English if the requested language is unknown.
    """
    return TRANSLATIONS.get(lang_code, TRANSLATIONS["en"])


__all__ = ["TRANSLATIONS", "get_lang"]
