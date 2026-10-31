import sys
import httpx
import pytest
from fastmcp import Client
from earnings_watch.http_server import AuthenticatedApp
from earnings_watch.mcp_server import create
from earnings_watch.store.database import connect


async def test_mcp_tools_prompts_resource_and_readonly_annotations(seeded):
    from earnings_watch import open

    open(seeded).watch("ALPHA")
    async with Client(create(seeded)) as client:
        tools = await client.list_tools()
        assert len(tools) == 10
        assert {t.name for t in tools} >= {
            "earnings_calendar_plan",
            "earnings_calendar_ack",
            "earnings_documents",
        }
        assert all(
            t.annotations.read_only_hint
            for t in tools
            if t.name not in {"earnings_watch", "earnings_unwatch", "earnings_calendar_ack"}
        )
        result = await client.call_tool("earnings_upcoming", {"days": 7})
        assert result.data["ok"] and result.data["events"]
        assert len(await client.list_prompts()) == 3
        resource = await client.read_resource("earnings://upcoming.ics")
        assert "BEGIN:VCALENDAR" in resource[0].text
        result = await client.call_tool("earnings_upcoming", {"days": 91})
        assert result.data["error"]["code"] == "invalid_argument"


async def test_stdio_wire(seeded):
    config = {
        "mcpServers": {
            "earnings": {
                "command": sys.executable,
                "args": ["-m", "earnings_watch.cli", "serve", "--stdio"],
                "env": {"EARNINGS_WATCH_DATA_DIR": str(seeded.data_dir)},
            }
        }
    }
    async with Client(config) as client:
        result = await client.call_tool("earnings_status")
        assert result.data["events"] == 1


@pytest.mark.parametrize(
    "path,token,headers,expected",
    [
        ("/users/alice/mcp", "a" * 40, {}, 200),
        ("/users/bob/mcp", "a" * 40, {}, 401),
        ("/users/alice/mcp", "short", {}, 401),
        ("/users/unknown/mcp", "a" * 40, {}, 401),
        ("/users/alice/mcp", "", {"X-Fleet-Person": "alice"}, 401),
        ("/users/alice/mcp", "a" * 40, {"Host": "evil.example"}, 403),
        ("/users/alice/mcp", "a" * 40, {"Origin": "https://evil.example"}, 403),
    ],
)
async def test_http_auth_host_origin(seeded, path, token, headers, expected):
    app = AuthenticatedApp(seeded, "standalone")
    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        request_headers = {
            "Authorization": "Bearer " + token,
            "Accept": "application/json, text/event-stream",
            **headers,
        }
        response = await client.post(
            path,
            headers=request_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "1"},
                },
            },
        )
        assert response.status_code == expected, response.text


async def test_gateway_requires_bearer_and_known_person(seeded, monkeypatch):
    monkeypatch.setenv("TEST_BACKEND_TOKEN", "c" * 40)
    seeded.backend_tokens_env = ["TEST_BACKEND_TOKEN"]
    app = AuthenticatedApp(seeded, "gateway")
    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        assert (await client.post("/mcp", headers={"X-Fleet-Person": "alice"})).status_code == 401
        assert (
            await client.post(
                "/mcp", headers={"Authorization": "Bearer " + "c" * 40, "X-Fleet-Person": "unknown"}
            )
        ).status_code == 401


async def test_token_rotation_refuses_old_token(seeded):
    from hashlib import sha256

    with connect(seeded.db, write=True) as db:
        db.execute(
            "UPDATE people SET token_sha256=? WHERE id=?",
            (sha256(b"new" * 20).hexdigest(), "alice"),
        )
    app = AuthenticatedApp(seeded, "standalone")
    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        assert (
            await client.post("/users/alice/mcp", headers={"Authorization": "Bearer " + "a" * 40})
        ).status_code == 401
