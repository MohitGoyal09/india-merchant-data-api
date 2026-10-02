"""Scraped text is data. Strip what could hide or fake structure before it reaches a model."""

from __future__ import annotations

import re
from typing import Any

MAX_TEXT_CHARS = 300
ELLIPSIS = "..."
# Code point ranges: C0, DEL and C1 controls (newline, tab and ESC included), zero-width and
# bidi marks, the line and paragraph separators, and the byte-order mark.
_STRIPPED_RANGES = (
    (0x00, 0x1F),
    (0x7F, 0x9F),
    (0x200B, 0x200F),
    (0x2028, 0x202E),
    (0x2060, 0x2064),
    (0x2066, 0x2069),
    (0xFEFF, 0xFEFF),
)
_CONTROLS = re.compile(
    "[" + "".join(f"{re.escape(chr(a))}-{re.escape(chr(b))}" for a, b in _STRIPPED_RANGES) + "]"
)


def clean_text(value: str, limit: int = MAX_TEXT_CHARS) -> str:
    """Replace control and invisible characters with spaces, collapse blanks, cap the length."""
    collapsed = " ".join(_CONTROLS.sub(" ", value).split())
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
