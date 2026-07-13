"""i18n regression guards.

Two things this locks down after the 2026-07-13 UA polish pass:

* The shared project-picker keys (shown on EVERY page) now have real
  Ukrainian strings, not English fall-throughs.
* No Latin ``i`` (U+0069) sits next to a Cyrillic letter in any UA
  value — that mojibake produced "Чек-лiст" (Latin i) instead of the
  Cyrillic "Чек-ліст".
"""

import pytest

from engine.i18n import get_lang


PICKER_KEYS = [
    "pp_active_project", "pp_none", "pp_switch",
    "pp_new_placeholder", "pp_create", "pp_hint",
]


def _is_cyrillic(ch: str) -> bool:
    return "Ѐ" <= ch <= "ӿ"


class TestUkrainianPickerKeys:
    def test_picker_keys_present_and_cyrillic(self):
        ua = get_lang("ua")
        for key in PICKER_KEYS:
            assert key in ua, f"UA translation missing key: {key}"
            val = ua[key]
            assert any(_is_cyrillic(c) for c in val), \
                f"UA value for {key} has no Cyrillic — likely English fallthrough: {val!r}"


class TestNoLatinIInCyrillic:
    def test_no_latin_i_adjacent_to_cyrillic(self):
        ua = get_lang("ua")
        offenders = []
        for key, val in ua.items():
            if not isinstance(val, str):
                continue
            for i, ch in enumerate(val):
                if ch == "i":  # Latin small i, U+0069
                    prev = val[i - 1] if i > 0 else ""
                    nxt = val[i + 1] if i + 1 < len(val) else ""
                    if _is_cyrillic(prev) or _is_cyrillic(nxt):
                        offenders.append((key, val))
                        break
        assert not offenders, (
            "Latin 'i' next to Cyrillic (use Cyrillic 'і' U+0456): "
            + ", ".join(f"{k}={v!r}" for k, v in offenders)
        )
