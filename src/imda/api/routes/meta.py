"""Service root and liveness probe. Neither touches the database."""

from __future__ import annotations

from fastapi import APIRouter, Request

from imda import __version__

router = APIRouter(tags=["meta"])

LINKS = {
    "offices": "/v1/offices",
    "holidays": "/v1/holidays?office=mumbai&year=2026",
    "business_day": "/v1/calendar/business-day?date=2026-03-28&office=mumbai",
    "next_business_days": "/v1/calendar/next-business-days?date=2026-03-27&office=mumbai&n=3",
    "calendar_ics": "/v1/calendar/mumbai.ics?year=2026",
    "settlement_eta": "/v1/settlement/eta?captured_at=2026-03-27T11:00:00%2B05:30&office=mumbai",
    "fx_rates": "/v1/fx/rates?currency=USD&from=2026-09-01&to=2026-09-30",
    "fx_as_of": "/v1/fx/rates/as-of?currency=USD&date=2026-01-26",
    "fx_convert": "/v1/fx/convert?amount=100&from=USD&to=INR&date=2026-09-30",
    "fx_stats": "/v1/fx/stats?currency=USD&from=2026-01-01&to=2026-09-30&period=month",
    "fx_compare": "/v1/fx/compare?currency=USD&from=2018-07-10&to=2018-07-24",
    "invoice_quote": "POST /v1/invoice/quote",
    "mibor": "/v1/rates/mibor?from=2026-09-01&to=2026-09-30",
}


@router.get("/", summary="Service links")
def index(request: Request) -> dict[str, object]:
    info: dict[str, object] = {"name": "India Merchant Data API", "version": __version__}
    if request.app.state.settings.enable_docs:
        info |= {"docs": "/docs", "openapi": "/openapi.json"}
    return info | {"endpoints": LINKS}


@router.get("/healthz", summary="Liveness (no database access)")
def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}
