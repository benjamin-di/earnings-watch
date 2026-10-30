"""User-owned agent CLIs. No login flow, shell, inherited MCP or project tools."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import signal
import tempfile
import sys
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


@dataclass(frozen=True)
class AgentTask:
    system: str
    prompt: str
    schema: type[BaseModel]
    kind: str = "json"
    timeout_s: int = 180
    max_turns: int = 4
    model: str | None = None
    allow_repair: bool = True


@dataclass(frozen=True)
class AgentResult:
    parsed: BaseModel | None
    backend: str
    repaired: bool = False


class AgentRunner(Protocol):
    async def run(self, task: AgentTask, *, research_config: Path | None = None) -> AgentResult: ...


class NotStarted(RuntimeError):
    """Only startup/auth/capacity failures may try another configured backend."""


class BudgetRunner:
    """Reserve the possible repair before I/O; failures retain their reservation."""

    def __init__(self, runner: AgentRunner, limit: int):
        self.runner, self.limit, self.calls = runner, limit, 0

    @property
    def remaining(self) -> int:
        return self.limit - self.calls

    async def run(self, task: AgentTask, *, research_config: Path | None = None) -> AgentResult:
        if not self.remaining:
            raise NotStarted("model call budget exhausted")
        task = replace(task, allow_repair=task.allow_repair and self.remaining > 1)
        reserved = 2 if task.allow_repair else 1
        self.calls += reserved
        result = await self.runner.run(task, research_config=research_config)
        used = 0 if result.backend == "none" else 2 if result.repaired else 1
        self.calls -= reserved - used
        return result


_DISABLED = (
    "shell_tool",
    "unified_exec",
    "shell_snapshot",
    "multi_agent",
    "multi_agent_v2",
    "code_mode",
    "js_repl",
    "image_generation",
    "view_image",
    "browser_use",
    "browser_use_external",
    "computer_use",
    "apps",
    "plugins",
    "remote_plugin",
    "hooks",
    "workspace_dependencies",
    "skill_search",
    "skill_mcp_dependency_install",
    "memories",
    "goals",
    "sleep_tool",
    "tool_suggest",
)


def environment(backend: str) -> dict[str, str]:
    names = {"HOME", "PATH", "LANG", "TMPDIR"}
    names |= (
        {
            "CLAUDE_CONFIG_DIR",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
        }
        if backend == "claude-code"
        else {"CODEX_HOME", "CODEX_API_KEY"}
    )
    return {k: v for k, v in os.environ.items() if k in names}


def command(
    backend: str, task: AgentTask, schema: Path, scratch: Path, research_config: Path | None = None
) -> list[str]:
    if task.kind not in {"json", "research"}:
        raise ValueError("unknown model task kind")
    if backend == "claude-code":
        cmd = [
            "claude",
            "-p",
            "--restricted",
            "--permission-mode",
            "dontAsk",
            "--output-format",
            "json",
            "--tools",
            "WebSearch" if task.kind == "research" else "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--setting-sources",
            "",
            "--settings",
            '{"disableAllHooks":true}',
            "--disable-slash-commands",
            "--no-session-persistence",
            "--max-turns",
            str(task.max_turns),
            "--json-schema",
            schema.read_text(),
        ]
    elif backend == "codex":
        cmd = [
            "codex",
            "-a",
            "never",
            "exec",
            "--json",
            "--ephemeral",
            "--ignore-user-config",
            "--ignore-rules",
            "--skip-git-repo-check",
            "--sandbox",
            "read-only",
            "--strict-config",
            "--output-schema",
            str(schema),
            "--output-last-message",
            str(scratch / "answer.json"),
        ]
        for feature in _DISABLED:
            cmd += ["-c", f"features.{feature}=false"]
        # MCP discovery/dispatch uses this host even with code mode disabled.
        # Pure transformations keep it off; research exposes only scoped tools.
        cmd += [
            "-c",
            "features.code_mode_host=" + ("true" if task.kind == "research" else "false"),
        ]
        config = Path("/etc/codex/config.toml")
        servers = (
            tomllib.loads(config.read_text()).get("mcp_servers", {}) if config.exists() else {}
        )
        disabled = ",".join(json.dumps(name) + "={enabled=false}" for name in servers)
        cmd += [
            "-c",
            "mcp_servers={" + disabled + "}",
            "-c",
            'web_search="live"' if task.kind == "research" else 'web_search="disabled"',
            "-c",
            "notify=[]",
            "-c",
            "project_doc_max_bytes=0",
            "-c",
            "skills.include_instructions=false",
            "-c",
            "skills.bundled.enabled=false",
            "-c",
            "tools.update_plan.enabled=false",
            "-c",
            "tools.experimental_request_user_input.enabled=false",
        ]
        ancestry = ",".join(
            json.dumps(str(p)) + '={trust_level="untrusted"}' for p in (scratch, *scratch.parents)
        )
        cmd += ["-c", "projects={" + ancestry + "}"]
    else:
        raise NotStarted("unknown model backend")
    if research_config is not None:
        args = ["-m", "earnings_watch.llm.research_server", str(research_config)]
        if backend == "claude-code":
            cmd[cmd.index("--mcp-config") + 1] = json.dumps(
                {"mcpServers": {"earnings_research": {"command": sys.executable, "args": args}}}
            )
            cmd[cmd.index("--tools") + 1] = (
                "WebSearch,mcp__earnings_research__open_page,mcp__earnings_research__record_observation"
            )
            cmd += [
                "--allowedTools",
                "WebSearch,mcp__earnings_research__open_page,mcp__earnings_research__record_observation",
            ]
        else:
            cmd += [
                "-c",
                "mcp_servers.earnings_research={command="
                + json.dumps(sys.executable)
                + ",args="
                + json.dumps(args)
                + ',enabled=true,enabled_tools=["open_page","record_observation"]}',
            ]
    if task.model:
        cmd += ["--model", task.model]
    return cmd


async def bounded_read(stream: asyncio.StreamReader) -> bytes:
    output = bytearray()
    while chunk := await stream.read(65536):
        output.extend(chunk)
        if len(output) > 2 * 1024 * 1024:
            raise ValueError("model output exceeds 2 MiB")
    return bytes(output)


def structured_schema(model: type[BaseModel]) -> dict:
    """Close objects for CLI strict output; leave optional metadata maps to code."""
    schema = model.model_json_schema()

    def visit(node):
        if isinstance(node, list):
            for item in node:
                visit(item)
        elif isinstance(node, dict):
            properties = node.get("properties")
            if properties is not None:
                for name, prop in list(properties.items()):
                    if (
                        prop.get("type") == "object"
                        and "properties" not in prop
                        and prop.get("additionalProperties") is not False
                    ):
                        if name in node.get("required", []):
                            raise ValueError(
                                "model output cannot contain an unbounded metadata map"
                            )
                        del properties[name]
                node["required"] = list(properties)
            if node.get("type") == "object":
                node["additionalProperties"] = False
            node.pop("default", None)
            for value in node.values():
                visit(value)

    visit(schema)
    return schema


class CliRunner:
    def __init__(self, backends: list[str]):
        self.backends = backends
        self.checked: set[str] = set()

    async def _one(
        self, backend: str, task: AgentTask, research_config: Path | None = None
    ) -> AgentResult:
        binary = "claude" if backend == "claude-code" else "codex"
        if not shutil.which(binary):
            raise NotStarted("model CLI is missing")
        if backend not in self.checked:
            from .doctor import inspect

            if (await inspect(backend))["state"] != "ready":
                raise NotStarted("model CLI does not support the required isolation flags")
            self.checked.add(backend)
        with tempfile.TemporaryDirectory(prefix="earnings-model-") as directory:
            scratch = Path(directory)
            schema = scratch / "schema.json"
            schema.write_text(json.dumps(structured_schema(task.schema)))
            process = await asyncio.create_subprocess_exec(
                *command(backend, task, schema, scratch, research_config),
                cwd=scratch,
                env=environment(backend),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            assert process.stdin and process.stdout and process.stderr
            prompt = task.system + "\n\n" + task.prompt
            process.stdin.write(prompt.encode())
            await process.stdin.drain()
            process.stdin.close()
            try:
                stdout, stderr, _ = await asyncio.wait_for(
                    asyncio.gather(
                        bounded_read(process.stdout), bounded_read(process.stderr), process.wait()
                    ),
                    task.timeout_s,
                )
            except BaseException:
                if process.returncode is None:
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
                raise
            if process.returncode:
                # Never include CLI output: it can contain account details.
                text = (stdout + stderr).decode(errors="replace")
                if re.search(
                    r"not logged in|login required|authenticat\w*|quota|capacity|region.*(?:block|unsupported)",
                    text,
                    re.I,
                ):
                    raise NotStarted("model authentication or capacity unavailable")
                raise RuntimeError("model run failed")
            if backend == "claude-code":
                envelope = json.loads(stdout)
                value = envelope.get("structured_output") or json.loads(
                    envelope.get("result", "{}")
                )
            else:
                value = json.loads((scratch / "answer.json").read_text())
            return AgentResult(task.schema.model_validate(value), backend)

    async def run(self, task: AgentTask, *, research_config: Path | None = None) -> AgentResult:
        for backend in self.backends:
            if backend == "none":
                return AgentResult(None, "none")
            try:
                return await self._one(backend, task, research_config)
            except NotStarted:
                continue
            except (ValueError, json.JSONDecodeError):
                if not task.allow_repair:
                    raise
                repair = replace(
                    task,
                    kind="json",
                    prompt=task.prompt
                    + "\nReturn valid JSON matching the schema exactly; the previous output was invalid.",
                )
                result = await self._one(backend, repair)
                return AgentResult(result.parsed, result.backend, repaired=True)
        raise NotStarted("no configured model backend can start")
