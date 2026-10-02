import datetime as dt

import pytest
from typer.testing import CliRunner

from imda import __version__
from imda.cli import app
from imda.config import Settings
from imda.models import Dataset, Source
from imda.sources.base import ParseError, RawPayload, UpstreamError, UpstreamRequest


def test_settings_read_env_prefix(monkeypatch):
    monkeypatch.setenv("IMDA_MIN_INTERVAL_SECONDS", "0.5")
    monkeypatch.setenv("IMDA_ADMIN_TOKEN", "s3cret-" + "x" * 32)
    settings = Settings(_env_file=None)
    assert settings.min_interval_seconds == 0.5
    assert settings.admin_token is not None
    assert settings.admin_token.get_secret_value() == "s3cret-" + "x" * 32
    assert "s3cret" not in repr(settings)


def test_settings_reject_short_admin_token(monkeypatch):
    monkeypatch.setenv("IMDA_ADMIN_TOKEN", "short")
    with pytest.raises(ValueError, match="at least 32"):
        Settings(_env_file=None)


def test_settings_treat_blank_admin_token_as_unset(monkeypatch):
    monkeypatch.setenv("IMDA_ADMIN_TOKEN", "   ")
    assert Settings(_env_file=None).admin_token is None


def test_settings_reject_invalid_values(monkeypatch):
    monkeypatch.setenv("IMDA_MAX_ATTEMPTS", "0")
    with pytest.raises(ValueError, match="max_attempts"):
        Settings(_env_file=None)


def test_cli_version():
    result = CliRunner().invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stdout.strip() == __version__


def test_errors_carry_context():
    err = ParseError(Source.RBI, Dataset.HOLIDAYS, "table missing")
    assert str(err) == "rbi/holidays: table missing"
    up = UpstreamError("boom", url="https://x", status_code=503)
    assert (up.url, up.status_code) == ("https://x", 503)


def test_raw_payload_text_decodes_safely():
    raw = RawPayload(
        request=UpstreamRequest(method="GET", url="https://x"),
        status_code=200,
        body=b"ok\xff",
        content_type="text/html",
        fetched_at=dt.datetime.now(dt.UTC),
        sha256="0",
        duration_ms=1,
    )
    assert raw.text().startswith("ok")
