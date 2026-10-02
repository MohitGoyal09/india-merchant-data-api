"""Hypothesis profiles and shared fixtures for the property suite.

Select a profile with ``HYPOTHESIS_PROFILE=ci``; the default is ``dev``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from hypothesis import HealthCheck, settings

from imda.api.app import create_app
from imda.config import Settings
from tests.api.conftest import FRESH_NOW, seed

# `dev` keeps Hypothesis defaults (100 examples, 200 ms deadline) for quick local feedback.
settings.register_profile("dev")
# `ci` runs deeper and drops the per-example deadline so a loaded runner cannot flake.
settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture(scope="session")
def property_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One seeded SQLite DB shared by every API property (the API only reads from it)."""
    path = tmp_path_factory.mktemp("property") / "imda.sqlite3"
    seed(path)
    return path


@pytest.fixture(scope="session")
def api(property_db: Path) -> Iterator[TestClient]:
    """Session-scoped client: Hypothesis re-runs a test body many times per fixture."""
    config = Settings(db_path=property_db, _env_file=None)
    app = create_app(config, now=lambda: FRESH_NOW)
    with TestClient(app) as client:
        yield client
