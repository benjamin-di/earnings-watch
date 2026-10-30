"""Version, flag and optional model capability checks using bounded subprocesses."""

import asyncio
import re
import shutil
from pydantic import BaseModel
from .runner import CliRunner, environment

MINIMUM = {"claude-code": (2, 1, 312), "codex": (0, 164, 0)}
REQUIRED = {
    "claude-code": ["--strict-mcp-config", "--tools", "--restricted"],
    "codex": ["--ignore-user-config", "--ignore-rules", "--strict-config", "--output-schema"],
}


async def inspect(backend, live=False):
    binary = "claude" if backend == "claude-code" else "codex"
    if backend == "none":
        return {"backend": "none", "state": "disabled"}
    if backend not in MINIMUM or not shutil.which(binary):
        return {"backend": backend, "state": "unavailable"}

    async def output(args):
        process = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=environment(backend),
        )
        try:
            value, _ = await asyncio.wait_for(process.communicate(), 10)
        except BaseException:
            process.kill()
            await process.wait()
            raise
        if len(value) > 131072:
            raise ValueError("CLI help exceeds budget")
        return value.decode(errors="replace")

    version = await output(["--version"])
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", version)
    help_text = await output(["exec", "--help"] if backend == "codex" else ["--help"])
    compatible = bool(
        match
        and tuple(map(int, match.groups())) >= MINIMUM[backend]
        and all(flag in help_text for flag in REQUIRED[backend])
    )
    result = {
        "backend": backend,
        "version": match[0] if match else "unknown",
        "state": "ready" if compatible else "incompatible",
    }
    if live and compatible:
        from .runner import AgentTask

        class Canary(BaseModel):
            answer: str

        try:
            value = await CliRunner([backend]).run(
                AgentTask(
                    system="Return only JSON matching the schema. No tools are permitted.",
                    prompt='Set answer to "canary-ok".',
                    schema=Canary,
                    timeout_s=90,
                )
            )
            result["canary"] = (
                isinstance(value.parsed, Canary) and value.parsed.answer == "canary-ok"
            )
        except (RuntimeError, TimeoutError, ValueError) as exc:
            result["canary"] = False
            result["reason"] = type(exc).__name__
    return result
