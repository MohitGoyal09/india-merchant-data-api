"""Scraped text is data. Strip what could hide or fake structure before it reaches a model."""

from __future__ import annotations

import re
import unicodedata
from typing import Any

MAX_TEXT_CHARS = 300
ELLIPSIS = "..."
# General categories dropped outright: controls, format characters (zero-width, bidi, soft
# hyphen, tag characters), private use, unassigned, and the line and paragraph separators.
_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Co", "Cn", "Zl", "Zp"})
# Characters that render as blank or join text invisibly but are not in those categories:
# braille blank, Hangul fillers, Mongolian vowel separator, combining grapheme joiner, plus
# the variation selectors (emoji VS16 and friends), which can hide data inside a glyph.
_DROPPED_CHARS = frozenset("⠀ㅤᅟᅠﾠ᠎͏")
_DROPPED_RANGES = (
    (0x180B, 0x180F),
    (0xFE00, 0xFE0F),
    (0xE0100, 0xE01EF),
)
_WHITESPACE = re.compile(r"\s+")


def _is_dropped(char: str) -> bool:
    if char in _DROPPED_CHARS:
        return True
    code = ord(char)
    if any(low <= code <= high for low, high in _DROPPED_RANGES):
        return True
    return unicodedata.category(char) in _DROPPED_CATEGORIES


def clean_text(value: str, limit: int = MAX_TEXT_CHARS) -> str:
    """NFKC-normalise, drop invisible and control characters, collapse blanks, cap the length.

    Tab, newline and the other whitespace controls become a space first, so words they
    separated stay separate; every other dropped character is removed without a trace.
    """
    kept = [
        " " if char.isspace() else char
        for char in unicodedata.normalize("NFKC", value)
        if char.isspace() or not _is_dropped(char)
    ]
    # Normalise again: removing an invisible character can leave a letter next to a combining
    # mark that now composes ("a" + ZWJ + U+0308), and cleaning must be idempotent.
    stable = unicodedata.normalize("NFKC", "".join(kept))
    collapsed = _WHITESPACE.sub(" ", stable).strip()
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - len(ELLIPSIS)] + ELLIPSIS


def clean_strings(value: Any) -> Any:
    """``value`` with ``clean_text`` applied to every string in nested dicts and lists."""
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, dict):
        return {key: clean_strings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [clean_strings(item) for item in value]
    return value
