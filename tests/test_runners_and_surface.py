import json
import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from earnings_watch.llm.runner import CliRunner, AgentTask, AgentResult, NotStarted, command
from earnings_watch.model.events import ExtractResult
from earnings_watch.mcp_server import create


def task():
    return AgentTask(system="No tools", prompt="One synthetic issuer", schema=ExtractResult)


async def test_fallback_is_only_for_startup_and_invalid_output_repairs_same_backend():
    runner = CliRunner(["first", "second"])
    answer = AgentResult(None, "second")
    runner._one = AsyncMock(side_effect=[NotStarted("missing"), answer])
    assert await runner.run(task()) == answer
    runner._one = AsyncMock(side_effect=[ValueError("bad JSON"), AgentResult(None, "first")])
    assert (await runner.run(task())).repaired
    assert all(call.args[0] == "first" for call in runner._one.await_args_list)
    runner._one = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(TimeoutError):
        await runner.run(task())
    assert runner._one.await_count == 1


async def test_none_backend_never_starts_a_cli():
    runner = CliRunner(["none"])
    runner._one = AsyncMock()
    assert (await runner.run(task())).backend == "none"
    runner._one.assert_not_called()


def test_scoped_research_mcp_is_explicit(tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    config = tmp_path / "scope.json"
    config.write_text(
        json.dumps({"directory": str(tmp_path / "captures"), "hosts": ["issuer.example"]})
    )
    research = AgentTask(
        system="Research", prompt="One issuer", schema=ExtractResult, kind="research"
    )
    claude = command("claude-code", research, schema, tmp_path, config)
    assert "mcp__earnings_research__open_page" in claude[claude.index("--tools") + 1]
    assert "earnings_research" in claude[claude.index("--mcp-config") + 1]
    codex = command("codex", research, schema, tmp_path, config)
    assert any(value.startswith("mcp_servers.earnings_research=") for value in codex)
    assert 'web_search="live"' in codex
    assert "features.code_mode_host=true" in codex
    assert "features.code_mode=false" in codex
    assert "features.shell_tool=false" in codex
    assert 'enabled_tools=["open_page","record_observation"]' in " ".join(codex)
    pure = command("codex", task(), schema, tmp_path)
    assert "features.code_mode_host=false" in pure


async def test_committed_mcp_surface_matches_runtime(settings):
    async with Client(create(settings)) as client:
        value = {
            "tools": [
                t.model_dump(mode="json", exclude_none=True) for t in await client.list_tools()
            ],
            "prompts": [
                p.model_dump(mode="json", exclude_none=True) for p in await client.list_prompts()
            ],
            "resources": [
                r.model_dump(mode="json", exclude_none=True) for r in await client.list_resources()
            ],
        }
    expected = Path(__file__).with_name("surface.json").read_text()
    assert value == json.loads(expected)
    assert len(json.dumps(value, indent=2, sort_keys=True).encode()) <= 13503


async def test_real_stdio_wire_uses_the_same_bounded_service(seeded):
    transport = StdioTransport(
        command=sys.executable,
        args=["-m", "earnings_watch.cli", "serve", "--stdio"],
        env=dict(os.environ, EARNINGS_WATCH_DATA_DIR=str(seeded.data_dir)),
    )
    async with Client(transport) as client:
        assert len(await client.list_tools()) == 10
        result = await client.call_tool("earnings_upcoming", {"days": 30})
        assert result.data["ok"] and result.data["events"][0]["ticker"] == "ALPHA"


async def test_call_budget_reserves_repairs_and_counts_failed_runs():
    from dataclasses import replace
    from earnings_watch.llm.runner import BudgetRunner

    backend = CliRunner(["first"])
    backend._one = AsyncMock(side_effect=[ValueError("bad output"), AgentResult(None, "first")])
    limited = BudgetRunner(backend, 1)
    with pytest.raises(ValueError):
        await limited.run(task())
    assert limited.calls == 1
    assert backend._one.await_count == 1
    with pytest.raises(NotStarted, match="budget"):
        await limited.run(task())
    backend._one = AsyncMock(side_effect=[ValueError("bad output"), AgentResult(None, "first")])
    limited = BudgetRunner(backend, 2)
    assert (await limited.run(task())).repaired
    assert limited.calls == 2
    backend._one = AsyncMock(return_value=AgentResult(None, "first"))
    limited = BudgetRunner(backend, 2)
    await limited.run(replace(task(), allow_repair=False))
    await limited.run(task())
    assert limited.remaining == 0


def test_model_schema_closes_nested_objects_and_keeps_metadata_out():
    from earnings_watch.llm.runner import structured_schema
    from earnings_watch.model.sources import Source

    for model in (Source, ExtractResult):
        schema = structured_schema(model)

        def visit(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node["additionalProperties"] is False
                    assert node["required"] == list(node["properties"])
                for child in node.values():
                    visit(child)
            elif isinstance(node, list):
                for child in node:
                    visit(child)

        visit(schema)
    assert "extensions" not in structured_schema(Source)["properties"]
    assert "coverage" not in structured_schema(ExtractResult)["properties"]
