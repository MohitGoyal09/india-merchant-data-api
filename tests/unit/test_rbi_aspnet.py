"""Tests for the ASP.NET WebForms helpers."""

from __future__ import annotations

import pytest

from imda.models import Dataset, Source
from imda.sources.base import ParseError
from imda.sources.rbi.aspnet import extract_form_state, extract_select_options

PAGE = """
<html><body><form method="post">
<input type="hidden" name="__EVENTTARGET" id="__EVENTTARGET" value="" />
<input type="hidden" name="__VIEWSTATE" id="__VIEWSTATE" value="abc+/=" />
<input type="hidden" name="__VIEWSTATEGENERATOR" id="g" value="GEN1" />
<input type="hidden" name="__EVENTVALIDATION" id="__EVENTVALIDATION" value="val==" />
<input type="text" name="visible" value="ignored" />
<input type="hidden" id="no_name" value="x" />
<select name="drMonth" id="drMonth">
  <option selected="selected" value="0">Select</option>
  <option value="1">January</option>
  <option value="2"> February </option>
</select>
<select name="other"><option>NoValueAttr</option></select>
</form></body></html>
"""


def test_extract_form_state_returns_all_named_hidden_inputs() -> None:
    state = extract_form_state(PAGE)

    assert state == {
        "__EVENTTARGET": "",
        "__VIEWSTATE": "abc+/=",
        "__VIEWSTATEGENERATOR": "GEN1",
        "__EVENTVALIDATION": "val==",
    }


@pytest.mark.parametrize("missing", ["__VIEWSTATE", "__EVENTVALIDATION"])
def test_extract_form_state_raises_when_required_field_missing(missing: str) -> None:
    html = PAGE.replace(f'name="{missing}"', 'name="renamed"')

    with pytest.raises(ParseError) as exc:
        extract_form_state(html, source=Source.RBI, dataset=Dataset.HOLIDAYS)

    assert missing in str(exc.value)
    assert exc.value.dataset is Dataset.HOLIDAYS


def test_extract_form_state_raises_on_empty_document() -> None:
    with pytest.raises(ParseError):
        extract_form_state("")


def test_extract_select_options_returns_value_label_pairs() -> None:
    assert extract_select_options(PAGE, "drMonth") == [
        ("0", "Select"),
        ("1", "January"),
        ("2", "February"),
    ]


def test_extract_select_options_falls_back_to_label_when_value_missing() -> None:
    assert extract_select_options(PAGE, "other") == [("NoValueAttr", "NoValueAttr")]


def test_extract_select_options_raises_when_select_missing() -> None:
    with pytest.raises(ParseError, match="drYear"):
        extract_select_options(PAGE, "drYear")
