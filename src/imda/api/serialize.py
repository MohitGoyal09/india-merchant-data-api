"""JSON-safe views of domain objects. Decimals are always strings, dates are ISO text."""

from __future__ import annotations

import datetime as dt
from dataclasses import fields, is_dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from pydantic import BaseModel

from imda.domain.fx_service import (
    AsOfResult,
    CompareReport,
    CompareRow,
    Conversion,
    PeriodStats,
    per_unit,
)
from imda.domain.settlement import SettlementEstimate
from imda.models import FxRate, Holiday, MiborRate, Office

SETTLEMENT_DISCLAIMER = (
    "Indicative estimate based on RBI holidays and Razorpay's published T+N rule; "
    "not Razorpay's settlement engine"
)
SPANS_WEEKEND_TENOR = "3D"
FX_COLUMNS = (
    "date",
    "currency",
    "rate",
    "unit",
    "rate_per_unit",
    "source",
    "published_at",
)


def decimal_text(value: Decimal) -> str:
    """Plain (never scientific) notation, so ``Decimal("1E-7")`` is ``"0.0000001"``."""
    return format(value, "f")


def to_jsonable(value: object) -> Any:
    """Recursively convert ``value`` into plain JSON types."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Decimal):
        return decimal_text(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if isinstance(value, BaseModel):
        return to_jsonable(value.model_dump())
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: to_jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [to_jsonable(v) for v in value]
    raise TypeError(f"cannot serialize {type(value).__name__}")


def fx_row(rate: FxRate) -> dict[str, object]:
    return {
        "date": rate.date,
        "currency": rate.currency,
        "rate": rate.rate,
        "unit": rate.unit,
        "rate_per_unit": per_unit(rate),
        "source": rate.source,
        "published_at": rate.published_at,
    }


def as_of_view(result: AsOfResult) -> dict[str, object]:
    return {
        "currency": result.currency,
        "requested_date": result.requested_date,
        "effective_date": result.effective_date,
        "lag_days": result.lag_days,
        "reason": result.reason,
        "rate": fx_row(result.rate),
    }


def conversion_view(conversion: Conversion) -> dict[str, object]:
    return {
        "amount": conversion.amount,
        "from": conversion.from_currency,
        "to": conversion.to_currency,
        "result": conversion.result,
        "exact": conversion.exact,
        "is_cross_rate": conversion.is_cross_rate,
        "rates_used": [as_of_view(r) for r in conversion.rates_used],
    }


def stats_view(stats: PeriodStats) -> dict[str, object]:
    return {f.name: getattr(stats, f.name) for f in fields(stats)}


def compare_row_view(row: CompareRow) -> dict[str, object]:
    return {f.name: getattr(row, f.name) for f in fields(row)}


def compare_view(report: CompareReport) -> dict[str, object]:
    return {
        "currency": report.currency,
        "from": report.start,
        "to": report.end,
        "rows": [compare_row_view(r) for r in report.rows],
        "summary": report.summary,
    }


def settlement_view(estimate: SettlementEstimate, office: str) -> dict[str, object]:
    return {
        "office": office,
        "captured_at": estimate.captured_at,
        "capture_date": estimate.capture_date,
        "cycle_days": estimate.cycle_days,
        "mode": estimate.mode,
        "eta_date": estimate.eta_date,
        "counted_days": estimate.counted_days,
        "skipped": [{"date": s.date, "reason": s.reason} for s in estimate.skipped],
        "disclaimer": SETTLEMENT_DISCLAIMER,
    }


def office_view(office: Office) -> dict[str, object]:
    return {
        "slug": office.slug,
        "name": office.name,
        "state": office.state,
        "rbi_id": office.rbi_id,
    }


def holiday_view(holiday: Holiday) -> dict[str, object]:
    return {
        "office": holiday.office_slug,
        "date": holiday.date,
        "weekday": holiday.date.strftime("%A"),
        "name": holiday.name,
        "kind": holiday.kind,
    }


def mibor_view(rate: MiborRate) -> dict[str, object]:
    return {
        "date": rate.date,
        "tenor": rate.tenor,
        "rate": rate.rate,
        "spans_weekend": rate.tenor == SPANS_WEEKEND_TENOR,
        "source": rate.source,
        "published_at": rate.published_at,
    }
