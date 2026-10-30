"""Official protocol scenarios against disposable authenticated HTTP state.

The fixture supplies a synthetic gateway identity; auth refusal is separately
covered by the ASGI tests. The suite's kitchen-sink named tool fixtures are not
Earnings Watch capabilities and are excluded explicitly in the baseline.
"""

import asyncio
import os
import socket
import tempfile
import json
import signal
from pathlib import Path
import uvicorn
from earnings_watch.settings import Settings
from earnings_watch.store.database import initialize, add_person
from earnings_watch.http_server import AuthenticatedApp


async def main():
    baseline = json.loads(Path("tests/conformance-baseline.json").read_text())
    with tempfile.TemporaryDirectory(prefix="earnings-conformance-state-") as directory:
        settings = Settings(
            data_dir=Path(directory), backend_tokens_env=["EARNINGS_CONFORMANCE_TOKEN"]
        )
        initialize(settings.db)
        add_person(settings.db, "synthetic")
        os.environ["EARNINGS_CONFORMANCE_TOKEN"] = "synthetic-conformance-token-00000000000000"
        boundary = AuthenticatedApp(settings, "gateway")

        async def fixture(scope, receive, send):
            if scope["type"] == "http":
                scope = dict(
                    scope,
                    headers=[
                        *scope.get("headers", []),
                        (b"authorization", b"Bearer synthetic-conformance-token-00000000000000"),
                        (b"x-fleet-person", b"synthetic"),
                    ],
                )
            await boundary(scope, receive, send)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(fixture, log_level="error"))
        running = asyncio.create_task(server.serve(sockets=[sock]))
        while not server.started:
            if running.done():
                await running
            await asyncio.sleep(0.05)
        cases = baseline["scenarios"]
        failures = []
        try:
            for scenario in cases:
                p = await asyncio.create_subprocess_exec(
                    "npx",
                    "--yes",
                    "@modelcontextprotocol/conformance@" + baseline["version"],
                    "server",
                    "--url",
                    f"http://127.0.0.1:{port}/mcp",
                    "--scenario",
                    scenario,
                    "--output-dir",
                    str(Path(".artifacts/conformance") / scenario),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    start_new_session=True,
                )
                try:
                    out, _ = await asyncio.wait_for(p.communicate(), 90)
                except BaseException:
                    if p.returncode is None:
                        os.killpg(p.pid, signal.SIGKILL)
                    await p.wait()
                    raise
                print(scenario, p.returncode, flush=True)
                output = Path(".artifacts/conformance")
                output.mkdir(parents=True, exist_ok=True)
                (output / (scenario + ".log")).write_bytes(out)
                if p.returncode:
                    failures.append(scenario)
        finally:
            server.should_exit = True
            await running
        if failures:
            raise SystemExit("Conformance failures: " + ", ".join(failures))


asyncio.run(main())
