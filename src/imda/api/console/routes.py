"""``GET /console``: a small static merchant-facing page over the same REST API.

Plain HTML, CSS and ES modules served from this package. No inline script or style, so the strict
Content-Security-Policy below holds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from fastapi import APIRouter
from fastapi.responses import FileResponse

from imda.api.errors import ApiError

ROOT: Final = Path(__file__).resolve().parent
CSP: Final = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'"
)
HEADERS: Final = {"Content-Security-Policy": CSP, "Cache-Control": "no-cache"}
MEDIA_TYPES: Final = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}
ASSETS: Final = frozenset(p.name for p in ROOT.iterdir() if p.suffix in MEDIA_TYPES)

router = APIRouter(include_in_schema=False)


@router.get("/console")
def console_page() -> FileResponse:
    return FileResponse(ROOT / "index.html", media_type="text/html; charset=utf-8", headers=HEADERS)


@router.get("/console/assets/{name}")
def console_asset(name: str) -> FileResponse:
    if name not in ASSETS:
        raise ApiError(404, "NOT_FOUND", f"No console asset {name!r}")
    path = ROOT / name
    return FileResponse(path, media_type=MEDIA_TYPES[path.suffix], headers=HEADERS)
