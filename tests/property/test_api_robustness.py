"""No query string, however odd, may cause a 5xx from the data endpoints."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from decimal import Decimal

from fastapi.testclient import TestClient
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

ALLOWED = {200, 404, 409, 422}
# The client fixture is session scoped (the seeded DB is read-only), which Hypothesis cannot
# prove; requests do real I/O, so the per-example deadline is off.
api_settings = settings(deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])

junk = st.text(max_size=40)
dates = st.dates(min_value=dt.date(1999, 1, 1), max_value=dt.date(2101, 12, 31))
date_text = st.one_of(
    dates.map(dt.date.isoformat),
    dates.map(lambda d: d.strftime("%d/%m/%Y")),
    st.dates().map(lambda d: d.isoformat() + "T00:00:00"),
    junk,
)
currency_text = st.one_of(
    st.sampled_from(["USD", "gbp", " eur ", "JPY", "AED", "IDR", "INR", "inr", "XXX", ""]), junk
)
source_text = st.one_of(st.sampled_from(["auto", "rbi", "fbil", "RBI", ""]), junk)
amount_text = st.one_of(
    st.integers(min_value=-(10**8), max_value=10**21).map(lambda cents: f"{cents / 100:.2f}"),
    st.integers(min_value=1, max_value=10**12).map(lambda n: str(Decimal(n).scaleb(-2))),
    st.integers(min_value=0, max_value=10**22).map(str),
    st.from_regex(r"[+-]?\d{0,22}(\.\d{0,22})?", fullmatch=True),
    junk,
)
office_text = st.one_of(st.sampled_from(["mumbai", "new-delhi", "Mumbai ", "pune", ""]), junk)
offsets = st.sampled_from(["Z", "+05:30", "-08:00", "+14:00", "-12:00", "+00:00", " 05:30", ""])
moment_text = st.one_of(
    st.datetimes(min_value=dt.datetime(1999, 12, 30), max_value=dt.datetime(2101, 1, 2)).map(
        lambda m: m.isoformat()
    ),
    st.builds(
        "{}{}".format,
        st.datetimes(min_value=dt.datetime(1999, 12, 30), max_value=dt.datetime(2101, 1, 2)).map(
            lambda m: m.isoformat()
        ),
        offsets,
    ),
    junk,
)
cycle_text = st.one_of(st.integers(-5, 40).map(str), junk)
mode_text = st.one_of(st.sampled_from(["working_days", "calendar_then_roll", ""]), junk)
limit_text = st.one_of(st.integers(-5, 20_000).map(str), junk)
seeded_days = st.one_of(
    st.dates(min_value=dt.date(2018, 7, 1), max_value=dt.date(2018, 8, 15)),
    st.dates(min_value=dt.date(2021, 1, 1), max_value=dt.date(2021, 12, 31)),
    st.dates(min_value=dt.date(2026, 9, 1), max_value=dt.date(2026, 10, 15)),
)
good_amounts = st.integers(min_value=1, max_value=10**15).map(
    lambda cents: str(Decimal(cents).scaleb(-2))
)
currency_pair = st.lists(
    st.sampled_from(["USD", "GBP", "EUR", "JPY", "AED", "IDR", "INR"]),
    min_size=2,
    max_size=2,
    unique=True,
)
# (office, date) pairs where the seeded DB has holiday data: Mumbai 2026, New Delhi 2001.
seeded_office_days = st.one_of(
    st.tuples(
        st.just("mumbai"), st.dates(min_value=dt.date(2026, 1, 1), max_value=dt.date(2026, 12, 31))
    ),
    st.tuples(
        st.just("new-delhi"),
        st.dates(min_value=dt.date(2001, 1, 1), max_value=dt.date(2001, 12, 31)),
    ),
)
query_keys = st.sampled_from(["currency", "from", "to", "date", "amount", "office", "extra"])


def check(api: TestClient, path: str, params: Mapping[str, str]) -> None:
    response = api.get(path, params=dict(params))
    event(f"{path} -> {response.status_code}")
    assert response.status_code in ALLOWED, (response.status_code, response.text[:300])
    assert "Traceback" not in response.text


def optional(**fields: st.SearchStrategy[str]) -> st.SearchStrategy[dict[str, str]]:
    return st.fixed_dictionaries({}, optional=fields)


@api_settings
@given(
    optional(
        currency=currency_text,
        source=source_text,
        limit=limit_text,
        cursor=junk,
        format=st.sampled_from(["csv", "json", ""]),
        **{"from": date_text, "to": date_text},
    )
)
def test_fx_rates_never_5xx_for_arbitrary_parameters(
    api: TestClient, params: dict[str, str]
) -> None:
    check(api, "/v1/fx/rates", params)


@api_settings
@given(
    st.fixed_dictionaries(
        {
            "currency": st.sampled_from(["USD", "JPY", "IDR"]),
            "from": dates.map(dt.date.isoformat),
            "to": dates.map(dt.date.isoformat),
            "source": st.sampled_from(["auto", "rbi", "fbil"]),
            "limit": st.integers(1, 1500).map(str),
        }
    )
)
def test_fx_rates_never_5xx_for_valid_looking_parameters(
    api: TestClient, params: dict[str, str]
) -> None:
    check(api, "/v1/fx/rates", params)


@api_settings
@given(
    optional(
        amount=amount_text,
        date=date_text,
        source=source_text,
        **{"from": currency_text, "to": currency_text},
    )
)
def test_fx_convert_never_5xx_for_arbitrary_parameters(
    api: TestClient, params: dict[str, str]
) -> None:
    check(api, "/v1/fx/convert", params)


@api_settings
@given(
    st.fixed_dictionaries(
        {
            "amount": amount_text,
            "from": st.sampled_from(["USD", "JPY", "IDR", "INR", "AED"]),
            "to": st.sampled_from(["USD", "JPY", "IDR", "INR", "AED"]),
            "date": dates.map(dt.date.isoformat),
            "source": st.sampled_from(["auto", "rbi", "fbil"]),
        }
    )
)
def test_fx_convert_never_5xx_for_valid_looking_parameters(
    api: TestClient, params: dict[str, str]
) -> None:
    check(api, "/v1/fx/convert", params)


@api_settings
@given(optional(date=date_text, office=office_text))
def test_business_day_never_5xx_for_arbitrary_parameters(
    api: TestClient, params: dict[str, str]
) -> None:
    check(api, "/v1/calendar/business-day", params)


@api_settings
@given(
    st.fixed_dictionaries(
        {"date": dates.map(dt.date.isoformat), "office": st.sampled_from(["mumbai", "new-delhi"])}
    )
)
def test_business_day_never_5xx_for_valid_looking_parameters(
    api: TestClient, params: dict[str, str]
) -> None:
    check(api, "/v1/calendar/business-day", params)


@api_settings
@given(optional(captured_at=moment_text, office=office_text, cycle_days=cycle_text, mode=mode_text))
def test_settlement_eta_never_5xx_for_arbitrary_parameters(
    api: TestClient, params: dict[str, str]
) -> None:
    check(api, "/v1/settlement/eta", params)


@api_settings
@given(
    st.fixed_dictionaries(
        {
            "captured_at": st.datetimes(
                min_value=dt.datetime(1999, 12, 30),
                max_value=dt.datetime(2101, 1, 2),
                timezones=st.timezones(),
            ).map(lambda m: m.isoformat()),
            "office": st.sampled_from(["mumbai", "new-delhi"]),
            "cycle_days": st.integers(-2, 35).map(str),
            "mode": st.sampled_from(["working_days", "calendar_then_roll"]),
        }
    )
)
def test_settlement_eta_never_5xx_for_valid_looking_parameters(
    api: TestClient, params: dict[str, str]
) -> None:
    check(api, "/v1/settlement/eta", params)


@api_settings
@given(
    st.sampled_from(
        ["/v1/fx/rates", "/v1/fx/convert", "/v1/calendar/business-day", "/v1/settlement/eta"]
    ),
    st.dictionaries(query_keys | junk, junk, max_size=6),
)
def test_unrelated_and_repeated_parameters_never_5xx(
    api: TestClient, path: str, params: dict[str, str]
) -> None:
    check(api, path, params)


@api_settings
@given(
    pair=currency_pair,
    amount=good_amounts,
    day=seeded_days,
    source=st.sampled_from(["auto", "rbi", "fbil"]),
)
def test_fx_convert_never_5xx_for_well_formed_seeded_requests(
    api: TestClient, pair: list[str], amount: str, day: dt.date, source: str
) -> None:
    params = {"from": pair[0], "to": pair[1], "amount": amount, "date": day.isoformat()}
    check(api, "/v1/fx/convert", {**params, "source": source})


@api_settings
@given(seeded_office_days)
def test_business_day_never_5xx_where_holiday_data_exists(
    api: TestClient, office_day: tuple[str, dt.date]
) -> None:
    office, day = office_day
    check(api, "/v1/calendar/business-day", {"office": office, "date": day.isoformat()})


@api_settings
@given(
    seeded_office_days,
    st.integers(0, 30),
    st.sampled_from(["working_days", "calendar_then_roll"]),
    st.sampled_from(["+05:30", "Z", "-08:00", "+14:00"]),
    st.times(),
)
def test_settlement_eta_never_5xx_where_holiday_data_exists(
    api: TestClient,
    office_day: tuple[str, dt.date],
    cycle: int,
    mode: str,
    offset: str,
    clock: dt.time,
) -> None:
    office, day = office_day
    captured = f"{day.isoformat()}T{clock.replace(microsecond=0).isoformat()}{offset}"
    check(
        api,
        "/v1/settlement/eta",
        {"office": office, "captured_at": captured, "cycle_days": str(cycle), "mode": mode},
    )
