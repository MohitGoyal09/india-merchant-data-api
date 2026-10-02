"""Record small FBIL fixtures for the offline tests (6 requests at most, 2 s apart).

Usage: ``uv run python scripts/record_fbil_fixtures.py``. Fixtures are written to
``tests/fixtures/fbil/<name>.json`` next to ``<name>.meta.json`` (request, status, hash).
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from imda.config import DEFAULT_USER_AGENT, Settings
from imda.http.client import PoliteClient
from imda.sources.base import DateRangeQuery, UpstreamRequest
from imda.sources.fbil.common import build_request

OUT_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "fbil"
MIN_INTERVAL_SECONDS = 2.0
FX_PATH = "/refrates/fetchfiltered"
MIBOR_PATH = "/ovnmibor/fetchfiltered"

# name -> (endpoint path, first day, last day)
RECORDINGS: dict[str, tuple[str, dt.date, dt.date]] = {
    "fx_2026_09": (FX_PATH, dt.date(2026, 9, 1), dt.date(2026, 9, 30)),
    "fx_2021": (FX_PATH, dt.date(2021, 1, 1), dt.date(2021, 12, 31)),
    "fx_2018_07": (FX_PATH, dt.date(2018, 7, 1), dt.date(2018, 7, 31)),
    "mibor_2026_09": (MIBOR_PATH, dt.date(2026, 9, 1), dt.date(2026, 9, 30)),
}


def record(client: PoliteClient, name: str, request: UpstreamRequest) -> None:
    raw = client.send(request)
    (OUT_DIR / f"{name}.json").write_bytes(raw.body)
    meta = {
        "method": request.method,
        "url": request.url,
        "params": dict(request.params),
        "status_code": raw.status_code,
        "content_type": raw.content_type,
        "fetched_at": raw.fetched_at.isoformat(),
        "sha256": raw.sha256,
        "bytes": len(raw.body),
    }
    (OUT_DIR / f"{name}.meta.json").write_text(json.dumps(meta, indent=2) + "\n")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        _env_file=None,
        user_agent=DEFAULT_USER_AGENT,
        min_interval_seconds=MIN_INTERVAL_SECONDS,
        request_budget=len(RECORDINGS),
    )
    with PoliteClient(settings) as client:
        for name, (path, start, end) in RECORDINGS.items():
            record(client, name, build_request(path, DateRangeQuery(start, end)))


if __name__ == "__main__":
    main()
