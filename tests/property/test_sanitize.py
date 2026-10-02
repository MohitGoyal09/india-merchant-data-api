"""Properties of the MCP text sanitiser."""

from __future__ import annotations

import unicodedata

from hypothesis import given
from hypothesis import strategies as st

from imda.mcp.sanitize import ELLIPSIS, MAX_TEXT_CHARS, clean_strings, clean_text

FORBIDDEN = frozenset({"Cc", "Cf", "Co", "Cn", "Zl", "Zp"})
# Characters the sanitiser targets, so Hypothesis reaches them far more often than by chance.
SUSPECT_CODEPOINTS = (
    0x200B, 0x200D, 0x202E, 0x2066, 0x00AD, 0xFEFF, 0x2028, 0x2029, 0x0085, 0x0000, 0x001B,
    0xE000, 0x180E, 0x034F, 0xFE0F, 0x3164, 0x2800,
)  # fmt: skip
SUSPECT_CODEPOINTS = (
    0x200B, 0x200D, 0x202E, 0x2066, 0x00AD, 0xFEFF, 0x2028, 0x2029, 0x0085, 0x0000, 0x001B,
    0xE000, 0x180E, 0x034F, 0xFE0F, 0x3164, 0x2800,
)  # fmt: skip
SUSPECTS = "".join(map(chr, SUSPECT_CODEPOINTS))
COMBINING = "\u0301\u0308\u0323\u0327\u3099"  # marks that compose with a preceding letter
texts = st.text(
    alphabet=st.one_of(
        st.characters(codec="utf-8"), st.sampled_from(SUSPECTS), st.sampled_from(COMBINING)
    ),
    max_size=600,
)
# letter, an invisible character, then a combining mark: removing the invisible one lets the
# pair compose, which a normalise-then-drop pipeline would miss.
split_marks = st.builds(
    "{}{}{}".format,
    st.sampled_from("aeiouAEIOUnc"),
    st.sampled_from(SUSPECTS),
    st.sampled_from(COMBINING),
)
limits = st.integers(min_value=len(ELLIPSIS) + 1, max_value=400)


@given(texts)
def test_output_has_no_control_format_private_unassigned_or_separator_characters(
    text: str,
) -> None:
    cleaned = clean_text(text)

    assert not {unicodedata.category(c) for c in cleaned} & FORBIDDEN


@given(texts, limits)
def test_output_never_exceeds_the_cap(text: str, limit: int) -> None:
    assert len(clean_text(text, limit)) <= limit


@given(texts)
def test_default_cap_is_the_documented_maximum(text: str) -> None:
    assert len(clean_text(text)) <= MAX_TEXT_CHARS


@given(texts, limits)
def test_cleaning_is_idempotent(text: str, limit: int) -> None:
    once = clean_text(text, limit)

    assert clean_text(once, limit) == once


@given(split_marks, texts)
def test_cleaning_is_idempotent_when_removal_exposes_a_composable_pair(
    pair: str, rest: str
) -> None:
    once = clean_text(pair + rest)

    assert clean_text(once) == once
    assert unicodedata.is_normalized("NFKC", once)


@given(texts)
def test_whitespace_is_collapsed_and_trimmed(text: str) -> None:
    cleaned = clean_text(text)

    assert cleaned == cleaned.strip()
    assert "  " not in cleaned
    assert all(not c.isspace() or c == " " for c in cleaned)


@given(st.recursive(texts, lambda inner: st.lists(inner) | st.dictionaries(st.text(), inner)))
def test_clean_strings_preserves_structure_and_cleans_every_leaf(tree: object) -> None:
    def leaves(node: object) -> list[str]:
        if isinstance(node, str):
            return [node]
        if isinstance(node, dict):
            return [leaf for value in node.values() for leaf in leaves(value)]
        if isinstance(node, list):
            return [leaf for item in node for leaf in leaves(item)]
        return []

    cleaned = clean_strings(tree)

    assert leaves(cleaned) == [clean_text(leaf) for leaf in leaves(tree)]
