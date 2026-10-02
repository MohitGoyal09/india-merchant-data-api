"""Tests for RBI offices and the holidays adapter, driven by recorded fixtures."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from imda.models import Dataset, Holiday, HolidayKind, Office, Source
from imda.sources.base import (
    HolidayQuery,
    ParseError,
    RawPayload,
    UpstreamError,
    UpstreamRequest,
)
from imda.sources.rbi.aspnet import extract_form_state
from imda.sources.rbi.holidays import HOLIDAYS_URL, RbiHolidayAdapter
from imda.sources.rbi.offices import OFFICE_STATE, parse_offices, slugify

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "rbi"
NOW = dt.datetime(2026, 10, 2, tzinfo=dt.UTC)
MUMBAI = 28
NEW_DELHI = 30


def load_html(name: str) -> str:
    return (FIXTURES / f"{name}.html").read_text(encoding="utf-8")


def load_payload(name: str, body: bytes | None = None) -> RawPayload:
    meta = json.loads((FIXTURES / f"{name}.meta.json").read_text(encoding="utf-8"))
    data = body if body is not None else (FIXTURES / f"{name}.html").read_bytes()
    request = UpstreamRequest(
        method=meta["method"],
        url=meta["url"],
        form=meta["form"] or None,
    )
    return RawPayload(
        request=request,
        status_code=200,
        body=data,
        content_type="text/html",
        fetched_at=NOW,
        sha256=hashlib.sha256(data).hexdigest(),
        duration_ms=1,
    )


def days(holidays: Sequence[Holiday], office: str, month: int) -> dict[int, HolidayKind]:
    return {
        h.date.day: h.kind for h in holidays if h.office_slug == office and h.date.month == month
    }


# ---------------------------------------------------------------- offices


def test_parse_offices_skips_select_all_and_returns_every_office() -> None:
    offices = parse_offices(load_html("holidays_page"))

    assert len(offices) == 34
    assert all(o.rbi_id >= 1 for o in offices)
    assert len({o.slug for o in offices}) == 34


def test_parse_offices_maps_known_ids_and_slugs() -> None:
    by_slug = {o.slug: o for o in parse_offices(load_html("holidays_page"))}

    assert by_slug["mumbai"] == Office(rbi_id=28, slug="mumbai", name="Mumbai", state="Maharashtra")
    assert by_slug["new-delhi"].rbi_id == 30
    assert by_slug["new-delhi"].state == "Delhi"
    assert by_slug["chennai"].rbi_id == 16
    assert by_slug["thiruvananthapuram"].state == "Kerala"


def test_every_parsed_office_has_a_state_and_the_map_has_no_stale_slugs() -> None:
    offices = parse_offices(load_html("holidays_page"))

    assert [o.slug for o in offices if o.state is None] == []
    assert set(OFFICE_STATE) == {o.slug for o in offices}


def test_parse_offices_leaves_state_none_for_an_unmapped_office() -> None:
    html = (
        '<select name="drRegionalOffice"><option value="0">Select All</option>'
        '<option value="99">Brand New</option></select>'
    )

    assert parse_offices(html) == [Office(rbi_id=99, slug="brand-new", name="Brand New")]


def test_parse_offices_raises_when_dropdown_missing() -> None:
    with pytest.raises(ParseError):
        parse_offices("<html><body>nothing here</body></html>")


def test_parse_offices_raises_on_non_numeric_value() -> None:
    html = '<select name="drRegionalOffice"><option value="abc">Mumbai</option></select>'

    with pytest.raises(ParseError, match="abc"):
        parse_offices(html)


def test_parse_offices_raises_when_no_office_options() -> None:
    html = '<select name="drRegionalOffice"><option value="0">Select All</option></select>'

    with pytest.raises(ParseError):
        parse_offices(html)


@pytest.mark.parametrize(
    ("label", "slug"),
    [
        ("New Delhi", "new-delhi"),
        ("Mumbai", "mumbai"),
        ("  A  B ", "a-b"),
        ("St. Mary's", "st-mary-s"),
    ],
)
def test_slugify_makes_kebab_case(label: str, slug: str) -> None:
    assert slugify(label) == slug


# ---------------------------------------------------------------- layout 2 (one office, all months)


def test_parse_layout2_mumbai_2026_has_22_rows_with_verified_samples() -> None:
    holidays = RbiHolidayAdapter().parse(load_payload("holidays_mumbai_2026"))

    assert len(holidays) == 22
    assert {h.office_slug for h in holidays} == {"mumbai"}
    by_date = {h.date: h for h in holidays}
    pongal = by_date[dt.date(2026, 1, 15)]
    assert pongal.name.startswith("Uttarayana Punyakala/Pongal")
    assert pongal.name.endswith("Election to Municipal Corporations in Maharashtra")
    assert pongal.kind is HolidayKind.NI_ACT
    assert by_date[dt.date(2026, 1, 26)].name == "Republic Day"
    assert by_date[dt.date(2026, 2, 19)].name == "Chhatrapati Shivaji Maharaj Jayanti"


def test_parse_layout2_classifies_banks_closing_of_accounts() -> None:
    holidays = RbiHolidayAdapter().parse(load_payload("holidays_mumbai_2026"))

    closing = [h for h in holidays if h.kind is HolidayKind.CLOSING_OF_ACCOUNTS]
    assert [(h.date, h.name) for h in closing] == [
        (dt.date(2026, 4, 1), "To enable to Banks to close their yearly accounts")
    ]


def test_parse_layout2_new_delhi_2001_uses_year_from_form() -> None:
    holidays = RbiHolidayAdapter().parse(load_payload("holidays_new_delhi_2001"))

    assert len(holidays) == 18
    assert {h.office_slug for h in holidays} == {"new-delhi"}
    assert holidays[0].date == dt.date(2001, 1, 26)
    assert holidays[0].name == "Republic Day"
    assert holidays[-1].date == dt.date(2001, 12, 25)


def test_parse_layout2_without_holidays_returns_empty_list() -> None:
    html = load_html("holidays_mumbai_2026")
    start = html.index('<table width="70%"')
    end = html.index("</table>", start) + len("</table>")
    empty = (html[:start] + html[end:]).encode()

    assert RbiHolidayAdapter().parse(load_payload("holidays_mumbai_2026", body=empty)) == []


# ---------------------------------------------------------------- layout 1 (month matrix)


def test_parse_layout1_march_2026_mumbai_days() -> None:
    holidays = RbiHolidayAdapter().parse(load_payload("holidays_all_2026_03"))

    assert set(days(holidays, "mumbai", 3)) == {3, 19, 21, 26, 31}
    assert set(days(holidays, "mumbai", 3).values()) == {HolidayKind.NI_ACT}


def test_parse_layout1_covers_all_34_offices_with_day_names() -> None:
    holidays = RbiHolidayAdapter().parse(load_payload("holidays_all_2026_03"))

    assert len({h.office_slug for h in holidays}) == 34
    names = {h.date: h.name for h in holidays if h.office_slug == "mumbai"}
    assert names[dt.date(2026, 3, 26)] == "Shree Ram Navami"
    assert names[dt.date(2026, 3, 3)].startswith("Holi (Second Day)/Dol Jatra")


def test_parse_layout1_marks_closing_of_accounts_with_square() -> None:
    holidays = RbiHolidayAdapter().parse(load_payload("holidays_all_2026_04"))

    assert days(holidays, "mumbai", 4) == {
        1: HolidayKind.CLOSING_OF_ACCOUNTS,
        3: HolidayKind.NI_ACT,
        14: HolidayKind.NI_ACT,
    }


@pytest.mark.parametrize(
    ("matrix", "month"),
    [("holidays_all_2026_03", 3), ("holidays_all_2026_04", 4), ("holidays_all_2026_10", 10)],
)
def test_both_layouts_agree_for_mumbai(matrix: str, month: int) -> None:
    adapter = RbiHolidayAdapter()
    from_matrix = days(adapter.parse(load_payload(matrix)), "mumbai", month)
    from_list = days(adapter.parse(load_payload("holidays_mumbai_2026")), "mumbai", month)

    assert from_matrix == from_list
    assert from_matrix  # guard against two empty results agreeing


def test_parse_is_a_pure_function_of_the_payload() -> None:
    adapter = RbiHolidayAdapter()
    payload = load_payload("holidays_all_2026_10")

    assert adapter.parse(payload) == adapter.parse(payload)


# ---------------------------------------------------------------- malformed input


def _replace_body(name: str, old: str, new: str) -> RawPayload:
    html = load_html(name)
    assert old in html
    return load_payload(name, body=html.replace(old, new, 1).encode())


@pytest.mark.parametrize("name", ["holidays_mumbai_2026", "holidays_all_2026_03"])
def test_parse_raises_on_truncated_html(name: str) -> None:
    html = load_html(name)
    payload = load_payload(name, body=html[: len(html) // 3].encode())

    with pytest.raises(ParseError):
        RbiHolidayAdapter().parse(payload)


def test_parse_raises_on_empty_body() -> None:
    with pytest.raises(ParseError):
        RbiHolidayAdapter().parse(load_payload("holidays_all_2026_03", body=b""))


def test_parse_raises_when_request_form_lacks_query_context() -> None:
    payload = load_payload("holidays_mumbai_2026")
    bare = RawPayload(
        request=UpstreamRequest(method="POST", url=HOLIDAYS_URL, form={}),
        status_code=200,
        body=payload.body,
        content_type=payload.content_type,
        fetched_at=NOW,
        sha256=payload.sha256,
        duration_ms=1,
    )

    with pytest.raises(ParseError, match="drYear"):
        RbiHolidayAdapter().parse(bare)


def test_parse_raises_on_unknown_marker_glyph() -> None:
    payload = _replace_body(
        "holidays_all_2026_03",
        '<span aria-hidden="true" style="font-size:20px;color:red">•',
        "<span>?",
    )

    with pytest.raises(ParseError, match="marker"):
        RbiHolidayAdapter().parse(payload)


def test_parse_raises_when_a_marked_day_has_no_description() -> None:
    payload = _replace_body(
        "holidays_all_2026_03", "<tr><td>Shree Ram Navami</td> <td>26</td></tr>", ""
    )

    with pytest.raises(ParseError, match="26"):
        RbiHolidayAdapter().parse(payload)


def test_parse_raises_on_impossible_day() -> None:
    payload = _replace_body("holidays_mumbai_2026", "<td> 26</td>", "<td> 45</td>")

    with pytest.raises(ParseError, match="45"):
        RbiHolidayAdapter().parse(payload)


def test_parse_raises_on_unknown_month_name() -> None:
    payload = _replace_body("holidays_mumbai_2026", "> January<", "> Smarch<")

    with pytest.raises(ParseError, match="Smarch"):
        RbiHolidayAdapter().parse(payload)


def test_parse_raises_when_matrix_row_width_differs_from_header() -> None:
    payload = _replace_body(
        "holidays_all_2026_03",
        '<th class="tableheader" ><span class="dop_header">31</span></th>',
        "",
    )

    with pytest.raises(ParseError, match="cells"):
        RbiHolidayAdapter().parse(payload)


def test_parse_raises_when_no_result_table_is_present() -> None:
    html = load_html("holidays_mumbai_2026")
    html = html.replace("dop_header", "other").replace("holiday list for the year", "nothing")

    with pytest.raises(ParseError, match="no holiday"):
        RbiHolidayAdapter().parse(load_payload("holidays_mumbai_2026", body=html.encode()))


# ---------------------------------------------------------------- fingerprint


def test_fingerprint_ignores_data_values_but_tracks_layout() -> None:
    adapter = RbiHolidayAdapter()
    march = adapter.fingerprint(load_payload("holidays_all_2026_03"))
    october = adapter.fingerprint(load_payload("holidays_all_2026_10"))
    mumbai = adapter.fingerprint(load_payload("holidays_mumbai_2026"))
    delhi = adapter.fingerprint(load_payload("holidays_new_delhi_2001"))

    assert march == october
    assert mumbai == delhi
    assert march["layout"] == "month_matrix"
    assert mumbai["layout"] == "office_list"
    assert march != mumbai


def test_fingerprint_records_field_names_and_dropdown_counts() -> None:
    fp = RbiHolidayAdapter().fingerprint(load_payload("holidays_all_2026_03"))

    assert "__VIEWSTATE" in fp["form_fields"]  # type: ignore[operator]
    assert "btnGo" in fp["form_fields"]  # type: ignore[operator]
    assert fp["select_option_counts"] == {"drMonth": 13, "drRegionalOffice": 35}


def test_fingerprint_changes_when_dropdown_shrinks() -> None:
    adapter = RbiHolidayAdapter()
    base = adapter.fingerprint(load_payload("holidays_all_2026_03"))
    changed = adapter.fingerprint(
        _replace_body("holidays_all_2026_03", '<option value="72">Agartala</option>', "")
    )

    assert base != changed


def test_fingerprint_raises_on_garbage() -> None:
    with pytest.raises(ParseError):
        RbiHolidayAdapter().fingerprint(load_payload("holidays_all_2026_03", body=b"<html></html>"))


# ---------------------------------------------------------------- fetch


class FakeClient:
    """Serves recorded pages and remembers every request."""

    def __init__(
        self, post_fixture: str, *, fail_posts: int = 0, post_body: bytes | None = None
    ) -> None:
        self.requests: list[UpstreamRequest] = []
        self._post_fixture = post_fixture
        self._fail_posts = fail_posts
        self._post_body = post_body

    def send(self, request: UpstreamRequest) -> RawPayload:
        self.requests.append(request)
        if request.method == "POST" and self._fail_posts > 0:
            self._fail_posts -= 1
            raise UpstreamError("HTTP 500", url=request.url, status_code=500)
        name = "holidays_page" if request.method == "GET" else self._post_fixture
        data = (FIXTURES / f"{name}.html").read_bytes()
        if request.method == "POST" and self._post_body is not None:
            data = self._post_body
        return RawPayload(
            request=request,
            status_code=200,
            body=data,
            content_type="text/html",
            fetched_at=NOW,
            sha256=hashlib.sha256(data).hexdigest(),
            duration_ms=1,
        )


def test_fetch_gets_page_then_posts_form_with_hidden_fields() -> None:
    client = FakeClient("holidays_mumbai_2026")

    payloads = RbiHolidayAdapter().fetch(client, HolidayQuery(year=2026, office_rbi_id=MUMBAI))

    assert [r.method for r in client.requests] == ["GET", "POST"]
    form = client.requests[1].form
    assert form is not None
    assert (form["drRegionalOffice"], form["drMonth"], form["drYear"], form["btnGo"]) == (
        "28",
        "0",
        "2026",
        "GO",
    )
    assert form["__VIEWSTATE"].startswith("/wEP")
    assert form["__EVENTVALIDATION"]
    assert [p.request.method for p in payloads] == ["POST"]


def test_fetch_all_offices_single_month_uses_office_zero() -> None:
    client = FakeClient("holidays_all_2026_03")

    RbiHolidayAdapter().fetch(client, HolidayQuery(year=2026, month=3))

    form = client.requests[1].form
    assert form is not None
    assert (form["drRegionalOffice"], form["drMonth"]) == ("0", "3")


def test_fetch_reuses_state_from_latest_page_without_regetting() -> None:
    client = FakeClient("holidays_all_2026_03")
    adapter = RbiHolidayAdapter()

    adapter.fetch(client, HolidayQuery(year=2026, month=3))
    adapter.fetch(client, HolidayQuery(year=2026, month=4))

    assert [r.method for r in client.requests] == ["GET", "POST", "POST"]
    first_post, second_post = client.requests[1].form, client.requests[2].form
    assert first_post is not None
    assert second_post is not None
    page_state = extract_form_state(load_html("holidays_all_2026_03"))
    assert (
        first_post["__VIEWSTATE"] == extract_form_state(load_html("holidays_page"))["__VIEWSTATE"]
    )
    assert second_post["__VIEWSTATE"] == page_state["__VIEWSTATE"]
    assert second_post["drMonth"] == "4"


def test_fetch_regets_when_cached_state_is_missing() -> None:
    client = FakeClient("holidays_all_2026_03", post_body=b"<html>error page</html>")
    adapter = RbiHolidayAdapter()

    adapter.fetch(client, HolidayQuery(year=2026, month=3))
    adapter.fetch(client, HolidayQuery(year=2026, month=4))

    assert [r.method for r in client.requests] == ["GET", "POST", "GET", "POST"]


def test_fetch_retries_once_with_fresh_state_when_cached_state_is_rejected() -> None:
    adapter = RbiHolidayAdapter()
    warm = FakeClient("holidays_all_2026_03")
    adapter.fetch(warm, HolidayQuery(year=2026, month=3))
    client = FakeClient("holidays_all_2026_03", fail_posts=1)

    adapter.fetch(client, HolidayQuery(year=2026, month=4))

    assert [r.method for r in client.requests] == ["POST", "GET", "POST"]


def test_fetch_does_not_retry_forever_on_persistent_rejection() -> None:
    client = FakeClient("holidays_all_2026_03", fail_posts=5)

    with pytest.raises(UpstreamError):
        RbiHolidayAdapter().fetch(client, HolidayQuery(year=2026, month=3))

    assert [r.method for r in client.requests] == ["GET", "POST"]


def test_fetch_then_parse_round_trip() -> None:
    client = FakeClient("holidays_mumbai_2026")
    adapter = RbiHolidayAdapter()

    holidays = [
        h
        for raw in adapter.fetch(client, HolidayQuery(year=2026, office_rbi_id=MUMBAI))
        for h in adapter.parse(raw)
    ]

    assert len(holidays) == 22


def test_adapter_identity() -> None:
    adapter = RbiHolidayAdapter()

    assert (adapter.source, adapter.dataset) == (Source.RBI, Dataset.HOLIDAYS)
