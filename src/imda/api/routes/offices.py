"""``GET /v1/offices``."""

from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from imda.api.deps import Ctx
from imda.api.envelope import Used, envelope_example, success
from imda.api.serialize import office_view
from imda.models import Dataset, Source

router = APIRouter(prefix="/v1", tags=["offices"])

_EXAMPLE = [{"slug": "mumbai", "name": "Mumbai", "state": "Maharashtra", "rbi_id": 28}]


@router.get(
    "/offices",
    summary="RBI regional offices",
    responses={200: envelope_example(_EXAMPLE)},
)
def list_offices(ctx: Ctx) -> JSONResponse:
    offices = ctx.store.offices()
    return success(ctx, [office_view(o) for o in offices], used=[Used(Source.RBI, Dataset.OFFICES)])
