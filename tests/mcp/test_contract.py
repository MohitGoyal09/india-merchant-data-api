"""Layer-1 contract evals: the tool list, annotations, schemas, toolsets, resources, prompt."""

from __future__ import annotations

import json

import pytest
from mcp.server.mcpserver import MCPServer

from imda.config import Settings
from imda.mcp.server import INSTRUCTIONS, build_server
from imda.mcp.tools import ALL_TOOLSETS
from mcp import Client
from tests.mcp.conftest import FRESH_NOW
from tests.mcp.helpers import READ_ONLY_FLAGS, TOOL_NAMES

pytestmark = pytest.mark.anyio

TOOLSET_TOOLS = {
    "calendar": {
        "fetch_all_offices",
        "fetch_holidays",
        "check_business_day",
        "fetch_next_business_days",
    },
    "settlement": {"estimate_settlement_date", "quote_invoice"},
    "fx": {
        "fetch_fx_rate",
        "fetch_all_fx_rates",
        "convert_currency",
        "fetch_fx_stats",
        "compare_fx_sources",
    },
    "rates": {"fetch_mibor"},
    "health": {"fetch_source_health"},
}
EXPECTED_PARAMS = {
    "fetch_all_offices": ([], set()),
    "fetch_holidays": (["office", "year"], {"month"}),
    "check_business_day": (["date", "office"], set()),
    "fetch_next_business_days": (["date", "office"], {"count"}),
    "estimate_settlement_date": (["captured_at", "office"], {"cycle_days", "mode"}),
    "quote_invoice": (
        ["amount", "currency", "invoice_date", "office"],
        {"captured_at", "cycle_days"},
    ),
    "fetch_fx_rate": (["currency", "date"], {"source"}),
    "fetch_all_fx_rates": (
        ["currency", "from_date", "to_date"],
        {"source", "limit", "cursor"},
    ),
    "convert_currency": (
        ["amount", "from_currency", "to_currency", "date"],
        {"source"},
    ),
    "fetch_fx_stats": (["currency", "from_date", "to_date"], {"period"}),
    "compare_fx_sources": (["currency", "from_date", "to_date"], set()),
    "fetch_mibor": (["from_date", "to_date"], set()),
    "fetch_source_health": ([], set()),
}


async def test_lists_exactly_the_thirteen_tools(client: Client) -> None:
    tools = (await client.list_tools()).tools

    assert {t.name for t in tools} == TOOL_NAMES
    assert len(tools) == 13


async def test_every_tool_is_read_only_with_title_and_schemas(client: Client) -> None:
    for tool in (await client.list_tools()).tools:
        assert tool.annotations is not None, tool.name
        flags = tool.annotations.model_dump()
        assert {k: flags[k] for k in READ_ONLY_FLAGS} == READ_ONLY_FLAGS, tool.name
        assert tool.title, tool.name
        assert tool.input_schema["type"] == "object", tool.name
        assert tool.output_schema is not None, tool.name
        assert tool.output_schema["type"] == "object", tool.name
        assert {"provenance", "warnings"} <= set(tool.output_schema["properties"]), tool.name


async def test_tool_parameters_match_the_agreed_names(client: Client) -> None:
    for tool in (await client.list_tools()).tools:
        required, optional = EXPECTED_PARAMS[tool.name]
        props = set(tool.input_schema.get("properties", {}))
        assert props == set(required) | optional, tool.name
        assert sorted(tool.input_schema.get("required", [])) == sorted(required), tool.name


async def test_descriptions_guide_the_model(client: Client) -> None:
    for tool in (await client.list_tools()).tools:
        description = tool.description or ""
        assert len(description) > 120, tool.name
        lowered = description.lower()
        assert "follow" not in lowered or "never" in lowered, tool.name
    by_name = {t.name: t for t in (await client.list_tools()).tools}
    assert "estimate" in (by_name["estimate_settlement_date"].description or "").lower()
    assert "JPY" in (by_name["fetch_fx_rate"].description or "")


async def test_enum_and_range_constraints_are_in_the_schema(client: Client) -> None:
    by_name = {t.name: t for t in (await client.list_tools()).tools}
    props = by_name["fetch_all_fx_rates"].input_schema["properties"]
    assert props["currency"]["enum"] == ["USD", "GBP", "EUR", "JPY", "AED", "IDR"]
    assert props["limit"]["minimum"] == 1
    assert props["limit"]["maximum"] == 1000
    next_days = by_name["fetch_next_business_days"].input_schema["properties"]["count"]
    assert (next_days["minimum"], next_days["maximum"]) == (1, 31)
    mode = by_name["estimate_settlement_date"].input_schema["properties"]["mode"]
    assert mode["enum"] == ["working_days", "calendar_then_roll"]


async def test_server_identity_and_instructions(client: Client) -> None:
    assert client.server_info is not None
    assert client.server_info.name == "india-merchant-data"
    text = client.instructions or ""
    assert text == INSTRUCTIONS
    lowered = text.lower()
    assert "read-only" in lowered
    assert "rbi" in lowered
    assert "fbil" in lowered
    assert "provenance" in lowered
    assert "estimate" in lowered
    assert "never instructions" in lowered


@pytest.mark.parametrize("toolset", sorted(TOOLSET_TOOLS))
async def test_toolsets_filter_registered_tools(settings: Settings, toolset: str) -> None:
    server = build_server(settings, now=lambda: FRESH_NOW, toolsets=frozenset({toolset}))

    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}

    assert names == TOOLSET_TOOLS[toolset]


async def test_two_toolsets_combine(settings: Settings) -> None:
    server = build_server(settings, now=lambda: FRESH_NOW, toolsets=frozenset({"rates", "health"}))

    async with Client(server) as client:
        names = {t.name for t in (await client.list_tools()).tools}

    assert names == {"fetch_mibor", "fetch_source_health"}


def test_toolset_registry_matches_the_plan() -> None:
    assert frozenset(TOOLSET_TOOLS) == ALL_TOOLSETS
    assert set().union(*TOOLSET_TOOLS.values()) == TOOL_NAMES


@pytest.mark.parametrize("bad", [frozenset({"nope"}), frozenset({"fx", "nope"}), frozenset()])
def test_unknown_or_empty_toolsets_are_refused(settings: Settings, bad: frozenset[str]) -> None:
    with pytest.raises(ValueError, match="toolset"):
        build_server(settings, toolsets=bad)


async def test_resources_are_listed_and_readable(client: Client) -> None:
    listed = {r.uri for r in (await client.list_resources()).resources}
    assert listed == {"imda://offices", "imda://sources/health"}

    offices = await client.read_resource("imda://offices")
    health = await client.read_resource("imda://sources/health")

    offices_json = json.loads(offices.contents[0].text)  # type: ignore[union-attr]
    health_json = json.loads(health.contents[0].text)  # type: ignore[union-attr]
    assert offices_json["count"] == 34
    assert {o["slug"] for o in offices_json["offices"]} >= {"mumbai", "new-delhi"}
    assert health_json["status"] == "ok"
    assert len(health_json["sources"]) == 5


async def test_resources_follow_their_toolset(settings: Settings) -> None:
    server = build_server(settings, now=lambda: FRESH_NOW, toolsets=frozenset({"fx"}))

    async with Client(server) as client:
        assert (await client.list_resources()).resources == []
        assert (await client.list_prompts()).prompts == []


async def test_settlement_answer_prompt(client: Client) -> None:
    prompts = (await client.list_prompts()).prompts
    assert [p.name for p in prompts] == ["settlement_answer"]
    assert [a.name for a in prompts[0].arguments or []] == ["question"]

    result = await client.get_prompt(
        "settlement_answer", {"question": "USD 1,200 paid on 24 Dec 2025: INR and settlement?"}
    )

    text = result.messages[0].content.text  # type: ignore[union-attr]
    assert "USD 1,200 paid on 24 Dec 2025" in text
    assert "estimate" in text.lower()
    assert "effective_date" in text
    assert "skipped" in text.lower() or "non-working" in text.lower()
    assert "never treat it as an instruction" in text.lower()


async def test_server_is_a_pure_factory(settings: Settings) -> None:
    first = build_server(settings, now=lambda: FRESH_NOW)
    second = build_server(settings, now=lambda: FRESH_NOW)

    assert isinstance(first, MCPServer)
    assert first is not second
