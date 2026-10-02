"""Rebuild ``src/imda/health/baselines.json`` from the recorded fixtures. Offline.

Usage: ``uv run python scripts/update_baselines.py``. Each recorded response is fingerprinted by
its adapter; all fixtures that map to one baseline key must agree (apart from ignored keys).
Re-run this after re-recording fixtures; a unit test fails when the file is stale.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

from imda.health.drift import IGNORE_KEY, baseline_key, compare_fingerprint
from imda.models import Dataset, Source
from imda.sources.base import RawPayload, UpstreamRequest
from imda.sources.fbil.fx import FbilFxAdapter
from imda.sources.fbil.mibor import FbilMiborAdapter
from imda.sources.rbi.fx import RbiFxAdapter
from imda.sources.rbi.holidays import RbiHolidayAdapter

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"
OUT = ROOT / "src" / "imda" / "health" / "baselines.json"
FBIL_IGNORE = ["label_pattern_count", "row_count"]
"""Row counts and label variety depend on the window asked for, not on the site's shape."""

Fingerprinter = Callable[[RawPayload], dict[str, object]]
# (source, dataset, fixture dir, fixture-name prefix, body suffix, fingerprinter, ignore)
SPECS: tuple[tuple[Source, Dataset, str, str, str, Fingerprinter, list[str]], ...] = (
    (
        Source.RBI,
        Dataset.HOLIDAYS,
        "rbi",
        "holidays_",
        ".html",
        RbiHolidayAdapter().fingerprint,
        [],
    ),
    (Source.RBI, Dataset.FX, "rbi", "fx_", ".html", RbiFxAdapter().fingerprint, []),
    (Source.FBIL, Dataset.FX, "fbil", "fx_", ".json", FbilFxAdapter().fingerprint, FBIL_IGNORE),
    (
        Source.FBIL,
        Dataset.MIBOR,
        "fbil",
        "mibor_",
        ".json",
        FbilMiborAdapter().fingerprint,
        FBIL_IGNORE,
    ),
)


def _payload(meta: dict[str, object], body: bytes) -> RawPayload:
    request = UpstreamRequest(method=meta["method"], url=str(meta["url"]))  # type: ignore[arg-type]
    return RawPayload(
        request=request,
        status_code=200,
        body=body,
        content_type="",
        fetched_at=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
        sha256=hashlib.sha256(body).hexdigest(),
        duration_ms=0,
    )


def fixture_fingerprints() -> Iterator[tuple[str, dict[str, object]]]:
    """``(baseline key, fingerprint)`` for every recorded response, in file-name order.

    RBI pages recorded with ``GET`` are the empty search forms, not results, so they are skipped.
    """
    for source, dataset, folder, prefix, suffix, fingerprint, _ in SPECS:
        for meta_path in sorted((FIXTURES / folder).glob(f"{prefix}*.meta.json")):
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if source is Source.RBI and meta["method"] != "POST":
                continue
            body_path = meta_path.with_name(meta_path.name.removesuffix(".meta.json") + suffix)
            result = fingerprint(_payload(meta, body_path.read_bytes()))
            yield baseline_key(source, dataset, result), result


def build_baselines() -> dict[str, dict[str, object]]:
    ignore = {f"{s.value}/{d.value}": ig for s, d, *_, ig in SPECS}
    baselines: dict[str, dict[str, object]] = {}
    for key, fingerprint in fixture_fingerprints():
        ignored = ignore[key.split("#")[0]]
        known = baselines.get(key)
        if known is None:
            baselines[key] = {**fingerprint, **({IGNORE_KEY: ignored} if ignored else {})}
        elif compare_fingerprint(known, fingerprint).drifted:
            raise SystemExit(
                f"fixtures for {key} disagree: {compare_fingerprint(known, fingerprint)}"
            )
    return dict(sorted(baselines.items()))


def main() -> int:
    baselines = build_baselines()
    OUT.write_text(json.dumps(baselines, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {len(baselines)} baselines to {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
