"""MCP contract-eval fixtures: a DB seeded from the recorded fixtures and a fixed clock."""

from __future__ import annotations

import datetime as dt
import shutil
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType

import pytest
from mcp.server.mcpserver import MCPServer

from imda.config import Settings
from imda.mcp.server import build_server
from imda.models import IST
from mcp import Client

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
# 2026-09-25 10:00 IST, before the 13:30 cutoff: the newest FBIL USD row (09-24) is fresh.
FRESH_NOW = dt.datetime(2026, 9, 25, 10, 0, tzinfo=IST)
# 2026-10-01 10:00 IST: FBIL (to 09-24) is behind the expected 09-30, so stale.
STALE_NOW = dt.datetime(2026, 10, 1, 10, 0, tzinfo=IST)


def _seed_module() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import seed_fixtures
    finally:
        sys.path.remove(str(SCRIPTS))
    return seed_fixtures


@pytest.fixture(scope="session")
def template_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    path = tmp_path_factory.mktemp("mcp-template") / "imda.sqlite3"
    _seed_module().seed(path)
    return path


@pytest.fixture
def db_path(template_db: Path, tmp_path: Path) -> Path:
    target = tmp_path / "imda.sqlite3"
    shutil.copy(template_db, target)
    return target


@pytest.fixture
def settings(db_path: Path) -> Settings:
    return Settings(db_path=db_path, _env_file=None)  # type: ignore[call-arg]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def server(settings: Settings) -> MCPServer:
    return build_server(settings, now=lambda: FRESH_NOW)


@pytest.fixture
async def client(server: MCPServer) -> AsyncIterator[Client]:
    async with Client(server) as connected:
        yield connected
