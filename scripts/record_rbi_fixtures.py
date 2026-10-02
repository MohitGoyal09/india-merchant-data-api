"""Record RBI HTML fixtures politely (honest User-Agent, >= 2 s between requests).

Usage: ``uv run python scripts/record_rbi_fixtures.py``

Each response is stored as ``tests/fixtures/rbi/<name>.html`` next to
``<name>.meta.json`` holding the request method, URL and the user-controlled form
fields (hidden ASP.NET state is omitted: it is large and not needed to rebuild a payload).
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from imda.config import DEFAULT_USER_AGENT
from imda.sources.rbi.aspnet import extract_form_state

HOLIDAYS_URL = "https://www.rbi.org.in/Scripts/HolidayMatrixDisplay.aspx"
FX_URL = "https://www.rbi.org.in/Scripts/ReferenceRateArchive.aspx"
FIXTURE_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "rbi"
MIN_INTERVAL_SECONDS = 2.0
TIMEOUT_SECONDS = 30.0
MUMBAI_ID = 28
NEW_DELHI_ID = 30
FX_CHECKBOXES = ("chkAll", "chkUSD", "chkGBP", "chkEURO", "chkYEN", "chkAED", "chkIDR")
BLOCK_MARKERS = ("Access Denied", "captcha", "Request Rejected", "Pardon Our Interruption")


@dataclass
class Recorder:
    client: httpx.Client
    state: dict[str, str] = field(default_factory=dict)
    state_url: str = ""
    last_request_at: float = 0.0

    def _wait(self) -> None:
        remaining = MIN_INTERVAL_SECONDS - (time.monotonic() - self.last_request_at)
        if remaining > 0:
            time.sleep(remaining)

    def _send(
        self, name: str, method: str, url: str, form: dict[str, str] | None, *, save: bool = True
    ) -> str:
        if method == "POST" and self.state_url != url:
            self._send(f"{name}_state", "GET", url, None, save=False)
        self._wait()
        if method == "GET":
            response = self.client.get(url)
        else:
            response = self.client.post(url, data={**self.state, **(form or {})})
        self.last_request_at = time.monotonic()
        response.raise_for_status()
        text = response.text
        if any(marker.lower() in text.lower() for marker in BLOCK_MARKERS):
            raise RuntimeError(f"{name}: RBI returned a block/challenge page; stopping")
        if save:
            self._save(name, {"method": method, "url": url, "form": form or {}}, text)
        self.state = extract_form_state(text)
        self.state_url = url
        return text

    @staticmethod
    def _save(name: str, meta: dict[str, object], text: str) -> None:
        FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
        (FIXTURE_DIR / f"{name}.html").write_text(text, encoding="utf-8")
        (FIXTURE_DIR / f"{name}.meta.json").write_text(
            json.dumps(meta, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )

    def get(self, name: str, url: str) -> None:
        self._send(name, "GET", url, None)

    def post_holidays(self, name: str, *, office: int, month: int, year: int) -> None:
        form = {
            "drRegionalOffice": str(office),
            "drMonth": str(month),
            "drYear": str(year),
            "btnGo": "GO",
        }
        self._send(name, "POST", HOLIDAYS_URL, form)

    def post_fx(self, name: str, *, start: str, end: str) -> None:
        form = {box: "on" for box in FX_CHECKBOXES}
        form.update({"txtFromDate": start, "txtToDate": end, "btnSubmit": " GO "})
        self._send(name, "POST", FX_URL, form)


def record_all(rec: Recorder, only: frozenset[str] = frozenset()) -> None:
    """Record every fixture, or only the named ones when ``only`` is not empty."""
    steps: dict[str, Callable[[], None]] = {
        "holidays_page": lambda: rec.get("holidays_page", HOLIDAYS_URL),
        "holidays_mumbai_2026": lambda: rec.post_holidays(
            "holidays_mumbai_2026", office=MUMBAI_ID, month=0, year=2026
        ),
        "holidays_all_2026_03": lambda: rec.post_holidays(
            "holidays_all_2026_03", office=0, month=3, year=2026
        ),
        "holidays_all_2026_04": lambda: rec.post_holidays(
            "holidays_all_2026_04", office=0, month=4, year=2026
        ),
        "holidays_all_2026_10": lambda: rec.post_holidays(
            "holidays_all_2026_10", office=0, month=10, year=2026
        ),
        "holidays_new_delhi_2001": lambda: rec.post_holidays(
            "holidays_new_delhi_2001", office=NEW_DELHI_ID, month=0, year=2001
        ),
        "fx_page": lambda: rec.get("fx_page", FX_URL),
        "fx_2026_09": lambda: rec.post_fx("fx_2026_09", start="01/09/2026", end="30/09/2026"),
        "fx_2018_07": lambda: rec.post_fx("fx_2018_07", start="01/07/2018", end="31/07/2018"),
        "fx_2019_01_gap": lambda: rec.post_fx(
            "fx_2019_01_gap", start="01/01/2019", end="31/01/2019"
        ),
    }
    unknown = only - steps.keys()
    if unknown:
        raise SystemExit(f"unknown fixture name(s): {', '.join(sorted(unknown))}")
    for name, step in steps.items():
        if not only or name in only:
            step()


def main() -> int:
    headers = {"User-Agent": DEFAULT_USER_AGENT}
    with httpx.Client(headers=headers, timeout=TIMEOUT_SECONDS, follow_redirects=True) as client:
        record_all(Recorder(client), frozenset(sys.argv[1:]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
