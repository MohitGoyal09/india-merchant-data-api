"""CLI: backfill, refresh, status. Uses a tmp DB and a monkeypatched client factory."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from typer.testing import CliRunner

import imda.cli as cli
from imda.config import Settings
from imda.http.client import PoliteClient
from imda.ingest.common import ExchangeLog
from imda.models import Currency, Source
from imda.store.repo import Store
from tests.unit.test_ingest import FakeUpstream

runner = CliRunner()
SEPT = ["--from", "2026-09-01", "--to", "2026-09-30"]


class Env:
    def __init__(self, db: Path) -> None:
        self.db = db
        self.fake = FakeUpstream()
        self.opened = 0


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    state = Env(tmp_path / "cli.sqlite3")
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None, db_path=state.db))
    monkeypatch.setattr(cli, "_today", lambda: dt.date(2026, 10, 2))

    @contextmanager
    def fake_client(settings: Settings, log: ExchangeLog) -> Iterator[FakeUpstream]:
        state.opened += 1
        state.fake.attach(log)
        yield state.fake

    monkeypatch.setattr(cli, "_open_client", fake_client)
    return state


def test_backfill_ingests_and_exits_zero(env: Env) -> None:
    result = runner.invoke(cli.app, ["backfill", *SEPT, "--datasets", "fx,mibor"])

    assert result.exit_code == 0, result.output
    assert "ok" in result.output
    assert "fbil/mibor_overnight" in result.output
    with Store.open(env.db) as store:
        assert store.fx_rates(Currency.USD, dt.date(2026, 9, 1), dt.date(2026, 9, 30), Source.FBIL)
        assert store.runs()[0]["kind"] == "backfill"
    assert env.opened == 1


def test_backfill_defaults_to_all_datasets_and_today(env: Env) -> None:
    result = runner.invoke(cli.app, ["backfill", "--from", "2026-09-01"])
    assert result.exit_code == 0, result.output
    with Store.open(env.db) as store:
        assert len(store.offices()) == 34
        assert store.holidays("mumbai", 2026)


def test_backfill_partial_warns_but_exits_zero(env: Env) -> None:
    env.fake.down.append("fbil.org.in")
    result = runner.invoke(cli.app, ["backfill", *SEPT, "--datasets", "fx,mibor"])
    assert result.exit_code == 0
    assert "warning" in result.output
    assert "failed" in result.output


def test_backfill_all_failed_exits_one(env: Env) -> None:
    env.fake.down.append("fbil.org.in")
    result = runner.invoke(cli.app, ["backfill", *SEPT, "--datasets", "mibor"])
    assert result.exit_code == 1
    assert "every dataset failed" in result.output


FUTURE = ["--from", "2027-01-01", "--to", "2027-12-31", "--datasets", "holidays"]


def test_backfill_loads_next_year_holidays_when_rbi_offers_it(env: Env) -> None:
    env.fake.extra_years = (2027,)

    result = runner.invoke(cli.app, ["backfill", *FUTURE])

    assert result.exit_code == 0, result.output
    with Store.open(env.db) as store:
        assert store.holidays("mumbai", 2027)


def test_backfill_next_year_not_offered_exits_zero_with_a_clear_message(env: Env) -> None:
    result = runner.invoke(cli.app, ["backfill", *FUTURE])

    assert result.exit_code == 0, result.output
    assert "skipped" in result.output
    assert "2027 not offered by RBI yet" in result.output
    with Store.open(env.db) as store:
        assert store.holidays("mumbai", 2027) == []


def test_backfill_future_range_is_rejected_when_fx_is_selected(env: Env) -> None:
    result = runner.invoke(cli.app, ["backfill", "--from", "2027-01-01", "--to", "2027-12-31"])

    assert result.exit_code == 2
    assert "after end" in result.output
    assert "holidays" in result.output
    assert env.fake.requests == []


def test_backfill_help_says_which_datasets_may_go_past_today() -> None:
    result = runner.invoke(cli.app, ["backfill", "--help"])

    text = " ".join(result.output.replace("│", " ").split())
    assert "FX and MIBOR stop at today" in text
    assert "31 Dec of the newest year RBI offers" in text


def test_refresh_runs_and_reports(env: Env) -> None:
    result = runner.invoke(cli.app, ["refresh"])
    assert result.exit_code == 0, result.output
    assert "rbi/offices" in result.output
    assert "requests" in result.output
    with Store.open(env.db) as store:
        assert store.runs()[0]["kind"] == "refresh"


def test_refresh_all_down_exits_one(env: Env) -> None:
    env.fake.down.extend(["rbi.org.in", "fbil.org.in"])
    assert runner.invoke(cli.app, ["refresh"]).exit_code == 1


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--from", "yesterday"], "YYYY-MM-DD"),
        (["--from", "2026-09-01", "--to", "09/30/2026"], "YYYY-MM-DD"),
        (["--from", "2026-10-01", "--to", "2026-09-01"], "after"),
        (["--from", "2026-09-01", "--datasets", "fx,bonds"], "bonds"),
        (["--from", "2026-09-01", "--datasets", ","], "choose from"),
    ],
)
def test_backfill_rejects_bad_options(env: Env, args: list[str], message: str) -> None:
    result = runner.invoke(cli.app, ["backfill", *args])
    assert result.exit_code == 2
    assert message in result.output
    assert env.opened == 0


def test_status_on_empty_database(env: Env) -> None:
    result = runner.invoke(cli.app, ["status"])
    assert result.exit_code == 0
    assert "no source health recorded yet" in result.output


def test_status_shows_health_and_last_runs(env: Env) -> None:
    env.fake.down.append("fbil.org.in")
    runner.invoke(cli.app, ["backfill", *SEPT, "--datasets", "fx,mibor"])
    for _ in range(6):
        runner.invoke(cli.app, ["backfill", *SEPT, "--datasets", "mibor"])

    result = runner.invoke(cli.app, ["status"])

    assert result.exit_code == 0
    assert "rbi/fx_reference_rates" in result.output
    assert "ok" in result.output
    assert "fbil/mibor_overnight" in result.output
    assert "degraded" in result.output
    assert "connection refused" in result.output
    run_lines = [ln for ln in result.output.splitlines() if ln.startswith("run_")]
    assert len(run_lines) == 5


def test_version_still_works() -> None:
    assert runner.invoke(cli.app, ["version"]).exit_code == 0


def test_default_client_factory_builds_a_polite_client(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, db_path=tmp_path / "x.sqlite3")
    with (
        Store.open(settings.db_path) as store,
        cli._open_client(settings, ExchangeLog(store)) as built,
    ):
        assert isinstance(built, PoliteClient)


def test_today_is_an_ist_date() -> None:
    assert isinstance(cli._today(), dt.date)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (
            {
                "last_error": "boom",
                "last_error_at": "2026-10-02T07:00:00+00:00",
                "last_success_at": "2026-10-02T08:00:00+00:00",
            },
            "-",
        ),
        (
            {
                "last_error": "boom",
                "last_error_at": "2026-10-02T09:00:00+00:00",
                "last_success_at": "2026-10-02T08:00:00+00:00",
            },
            "boom",
        ),
        (
            {
                "last_error": "boom",
                "last_error_at": "2026-10-02T09:00:00+00:00",
                "last_success_at": None,
            },
            "boom",
        ),
        ({"last_error": None, "last_error_at": None, "last_success_at": None}, "-"),
    ],
)
def test_status_hides_errors_that_were_followed_by_a_success(row, expected):
    from imda.cli import _current_error

    assert _current_error(row) == expected


def test_canary_prints_the_new_year_and_how_to_load_it(env: Env) -> None:
    assert (
        runner.invoke(cli.app, ["backfill", "--from", "2026-01-01", "--to", "2026-12-31"]).exit_code
        == 0
    )
    env.fake.extra_years = (2027,)

    result = runner.invoke(cli.app, ["canary"])

    assert result.exit_code == 0, result.output
    assert "holidays.year_available: 2027" in result.output
    assert "imda refresh" in result.output
    assert "overall: ok" in result.output


def test_canary_prints_no_year_line_without_a_new_year(env: Env) -> None:
    result = runner.invoke(cli.app, ["canary"])

    assert result.exit_code == 0, result.output
    assert "year_available" not in result.output
