"""Plural forms, because Ukrainian has three of them and English has two.

The alternative was to write around it — *"Members: 3"* instead of *"3
members"* — and that is how a translated interface ends up reading like a
form rather than a sentence. It also would have changed the English copy,
which several tests pin, to work around a language they are not written in.

The forms live in the dictionaries as one string with ``|`` between them,
so every value in ``en.py`` and ``ua.py`` stays a plain string:

    en: 'om_member_word': 'member|members'
    ua: 'om_member_word': 'учасник|учасники|учасників'

Ukrainian's rule is the standard Slavic one and the cases that catch people
out are 11–14, which take the *many* form despite ending in 1–4::

    1, 21, 31       → учасник      (one)
    2–4, 22–24      → учасники     (few)
    5–20, 25–30, 0  → учасників    (many)
    11, 12, 13, 14  → учасників    (many, not few)
"""
from __future__ import annotations


def forms(raw: str) -> list[str]:
    """Split a dictionary value into its forms."""
    return [part.strip() for part in str(raw).split("|")]


def pick(lang: str, count: int, raw: str) -> str:
    """Return the form of *raw* that agrees with *count* in *lang*.

    Unknown languages fall back to the English rule, and a value with a
    single form is returned as it is — a word that does not inflect (or a
    key that has not been given its forms yet) must not become an
    IndexError on a page.
    """
    parts = forms(raw)
    if not parts:
        return ""
    if len(parts) == 1:
        return parts[0]

    n = abs(int(count))
    if lang == "ua":
        if n % 10 == 1 and n % 100 != 11:
            index = 0
        elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
            index = 1
        else:
            index = 2
        return parts[min(index, len(parts) - 1)]

    return parts[0] if n == 1 else parts[min(1, len(parts) - 1)]


__all__ = ["pick", "forms"]
