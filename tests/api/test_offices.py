"""``GET /v1/offices``."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.api.helpers import assert_envelope, body


def test_lists_all_34_offices_with_envelope_and_provenance(client: TestClient) -> None:
    parsed = body(client.get("/v1/offices"))

    assert_envelope(parsed)
    assert parsed["meta"]["count"] == 34
    assert parsed["meta"]["degraded"] is False
    mumbai = next(o for o in parsed["data"] if o["slug"] == "mumbai")
    assert mumbai == {"slug": "mumbai", "name": "Mumbai", "state": "Maharashtra", "rbi_id": 28}
    (prov,) = parsed["provenance"]
    assert prov["source"] == "rbi"
    assert prov["dataset"] == "offices"
    assert prov["source_url"].startswith("https://www.rbi.org.in/")
    assert prov["fetched_at"].endswith("+00:00")
    assert prov["stale"] is False


def test_empty_database_gives_an_empty_list_without_provenance(
    tmp_path,
    make_client,  # type: ignore[no-untyped-def]
) -> None:
    from imda.config import Settings

    empty = Settings(db_path=tmp_path / "empty.sqlite3", _env_file=None)
    parsed = body(make_client(custom=empty).get("/v1/offices"))

    assert parsed["data"] == []
    assert parsed["provenance"] == []
