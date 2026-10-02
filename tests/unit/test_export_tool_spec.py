"""The exported MCP tool spec: it matches the live server, and ``--check`` reports drift."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent.parent / "scripts"
EXPECTED_TOOL_COUNT = 13


@pytest.fixture(scope="module")
def export_tool_spec() -> ModuleType:
    sys.path.insert(0, str(SCRIPTS))
    try:
        import export_tool_spec as module
    finally:
        sys.path.remove(str(SCRIPTS))
    return module


def test_committed_spec_is_up_to_date(export_tool_spec: ModuleType) -> None:
    assert export_tool_spec.main(["--check"]) == 0


def test_spec_lists_every_tool_sorted_with_toolset_and_read_only_flags(
    export_tool_spec: ModuleType, tmp_path: Path
) -> None:
    out = tmp_path / "spec.json"

    assert export_tool_spec.main(["--out", str(out)]) == 0

    spec = json.loads(out.read_text(encoding="utf-8"))
    names = [tool["name"] for tool in spec["tools"]]
    assert names == sorted(names)
    assert len(names) == EXPECTED_TOOL_COUNT
    assert {tool["toolset"] for tool in spec["tools"]} == {
        "calendar",
        "settlement",
        "fx",
        "rates",
        "health",
    }
    assert all(tool["annotations"]["readOnlyHint"] for tool in spec["tools"])
    assert all(tool["output_schema"] and tool["input_schema"] for tool in spec["tools"])
    assert spec["server"]["name"] == "india-merchant-data"
    assert spec["server"]["instructions"]
    assert [p["name"] for p in spec["prompts"]] == ["settlement_answer"]
    assert len(spec["resources"]) == 2


def test_output_is_deterministic(export_tool_spec: ModuleType, tmp_path: Path) -> None:
    first, second = tmp_path / "a.json", tmp_path / "b.json"

    export_tool_spec.main(["--out", str(first)])
    export_tool_spec.main(["--out", str(second)])

    assert first.read_text(encoding="utf-8") == second.read_text(encoding="utf-8")


def test_check_fails_when_file_is_stale_or_missing(
    export_tool_spec: ModuleType, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / "spec.json"
    stale.write_text("{}\n", encoding="utf-8")

    assert export_tool_spec.main(["--out", str(stale), "--check"]) == 1
    assert export_tool_spec.main(["--out", str(tmp_path / "missing.json"), "--check"]) == 1
    assert "out of date" in capsys.readouterr().err
    assert stale.read_text(encoding="utf-8") == "{}\n"
