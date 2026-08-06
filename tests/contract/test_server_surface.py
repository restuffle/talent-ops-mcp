"""In-process MCP contract tests.

``Client(mcp)`` in the default ``auto`` mode drives the dispatcher directly, with no
JSON-RPC framing (docs/reference/mcp-v2-verified-api.md §10.1). These tests therefore cover
the tool contract but not serialization -- ``tests/test_mcp_smoke.py`` covers that.
"""

import json

import pytest
from mcp.client import Client

from talent_ops_mcp.server import SERVER_NAME, build_server
from talent_ops_mcp.settings import Settings


async def test_server_identity_and_instructions(settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        assert client.server_info is not None
        assert client.server_info.name == SERVER_NAME
        # MCPServer's `version` defaults to "" rather than the SDK version, so an empty
        # string here would mean we forgot to pass it.
        assert client.server_info.version

        assert client.instructions is not None
        lowered = client.instructions.lower()
        assert "must not" in lowered
        assert "rank" in lowered
        assert "unsafe_operation" in lowered


async def test_health_check_is_discoverable_with_read_only_hints(settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        listed = await client.list_tools()

        assert {t.name for t in listed.tools} == {"health_check"}
        tool = listed.tools[0]
        assert tool.description is not None and len(tool.description) > 40
        assert tool.output_schema is not None
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is True
        assert tool.annotations.destructive_hint is False
        assert tool.annotations.idempotent_hint is True
        assert tool.annotations.open_world_hint is False


async def test_health_check_reports_healthy_against_a_real_database(settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        result = await client.call_tool("health_check", {})

    assert result.is_error is False
    assert result.structured_content is not None
    assert result.structured_content["status"] == "healthy"
    assert result.structured_content["database"] == "connected"
    assert result.structured_content["environment"] == "test"
    assert result.structured_content["version"]
    assert result.structured_content["mcp_sdk_version"].startswith("2.")


async def test_health_check_creates_the_data_directory(tmp_path_factory: pytest.TempPathFactory) -> None:
    data_dir = tmp_path_factory.mktemp("nonexistent") / "does" / "not" / "exist"
    settings = Settings(env="test", data_dir=data_dir)
    assert not data_dir.exists()

    async with Client(build_server(settings)) as client:
        result = await client.call_tool("health_check", {})

    assert result.structured_content is not None
    assert result.structured_content["database"] == "connected"
    assert settings.resolved_database_path.exists()


async def test_health_check_leaks_no_paths_or_connection_strings(settings: Settings) -> None:
    async with Client(build_server(settings)) as client:
        result = await client.call_tool("health_check", {})

    blob = json.dumps(result.model_dump(mode="json"))
    assert "sqlite" not in blob.lower()
    assert str(settings.data_dir) not in blob
    assert settings.data_dir.name not in blob


@pytest.mark.parametrize("mode", ["auto", "legacy"])
async def test_both_protocol_eras_are_served(settings: Settings, mode: str) -> None:
    """One stdio server answering both the current spec and older hosts is the reason for v2."""
    async with Client(build_server(settings), mode=mode) as client:
        listed = await client.list_tools()
        result = await client.call_tool("health_check", {})

    assert {t.name for t in listed.tools} == {"health_check"}
    assert result.is_error is False


async def test_unknown_tool_is_refused(settings: Settings) -> None:
    """An unregistered name returns is_error rather than raising -- assert on the result."""
    async with Client(build_server(settings)) as client:
        result = await client.call_tool("rank_candidates", {})

    assert result.is_error is True
    assert result.structured_content is None


async def test_no_resources_or_prompts_are_advertised_yet(settings: Settings) -> None:
    """Phase 1 registers a tool only. This fails loudly if something is added silently."""
    async with Client(build_server(settings)) as client:
        resources = await client.list_resources()
        prompts = await client.list_prompts()

    assert resources.resources == []
    assert prompts.prompts == []
