"""``GET /v1/rates/mibor``: FBIL overnight MIBOR."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse

from imda.api.deps import Ctx, IsoDate, check_range
from imda.api.envelope import Used, envelope_example, success
from imda.api.serialize import mibor_view
from imda.models import Dataset, Source

router = APIRouter(prefix="/v1/rates", tags=["mibor"])

OVERNIGHT_TENORS = frozenset({"O/N", "3D"})
_EXAMPLE = [
    {
        "date": "2026-09-18",
        "tenor": "3D",
        "rate": "5.100000",
        "spans_weekend": True,
        "source": "fbil",
        "published_at": "2026-09-18T12:45:00+05:30",
    }
]


@router.get(
    "/mibor",
    summary="FBIL overnight MIBOR",
    description=(
        "On Fridays FBIL publishes tenor `3D` (Friday to Monday) instead of `O/N`. With "
        "`overnight_only=true` both are returned, each with its real `tenor` and "
        "`spans_weekend`. Rates are percent per annum."
    ),
    responses={
        200: envelope_example(
            _EXAMPLE,
            provenance=[
                {
                    "source": "fbil",
                    "dataset": "mibor_overnight",
                    "source_url": "https://www.fbil.org.in/wasdm/ovnmibor/fetchfiltered",
                    "fetched_at": "2026-10-02T05:13:18.119836+00:00",
                    "stale": False,
                }
            ],
        )
    },
)
def mibor(
    ctx: Ctx,
    from_: Annotated[IsoDate, Query(alias="from", description="First date, YYYY-MM-DD")],
    to: Annotated[IsoDate, Query(description="Last date, YYYY-MM-DD")],
    overnight_only: Annotated[bool, Query(description="Only O/N and 3D rows")] = True,
) -> JSONResponse:
    check_range(from_, to)
    rows = ctx.store.mibor(from_, to)
    if overnight_only:
        rows = [r for r in rows if r.tenor in OVERNIGHT_TENORS]
    used = [Used(Source.FBIL, Dataset.MIBOR, ctx.store.latest_mibor_date())] if rows else []
    return success(ctx, [mibor_view(r) for r in rows], used=used)
