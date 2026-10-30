"""Record the public MCP contract from an isolated synthetic database."""

import asyncio
import json
import tempfile
from pathlib import Path
from fastmcp import Client
from earnings_watch.settings import Settings
from earnings_watch.store.database import initialize, add_person
from earnings_watch.mcp_server import create


async def main():
    with tempfile.TemporaryDirectory() as directory:
        settings = Settings(data_dir=Path(directory))
        initialize(settings.db)
        add_person(settings.db, "local")
        async with Client(create(settings)) as client:
            tools = await client.list_tools()
            prompts = await client.list_prompts()
            resources = await client.list_resources()
            result = {
                "tools": [t.model_dump(mode="json", exclude_none=True) for t in tools],
                "prompts": [p.model_dump(mode="json", exclude_none=True) for p in prompts],
                "resources": [r.model_dump(mode="json", exclude_none=True) for r in resources],
            }
        Path("tests/surface.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")


asyncio.run(main())
