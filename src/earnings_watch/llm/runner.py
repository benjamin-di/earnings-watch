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


def environment(backend: str, codex_endpoint: tuple[str, str] | None = None) -> dict[str, str]:
    names = {
        "HOME",
        "PATH",
        "LANG",
        "TMPDIR",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "no_proxy",
    }
    names |= (
        {
            "CLAUDE_CONFIG_DIR",
            "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_BASE_URL",
            "ANTHROPIC_AUTH_TOKEN",
            # Bounded retries let an unavailable endpoint fail before the task timeout.
            "CLAUDE_CODE_MAX_RETRIES",
        }
        if backend == "claude-code"
        else {"CODEX_HOME", "CODEX_API_KEY"} | ({codex_endpoint[1]} if codex_endpoint else set())
    )
    return {k: v for k, v in os.environ.items() if k in names}


def codex_endpoint_overrides(codex_endpoint: tuple[str, str] | None) -> list[str]:
    """Route Codex through a configured Responses endpoint without user config."""
    if not codex_endpoint:
        return []
    base_url, key_env = codex_endpoint
    provider = (
        '{name="Earnings Watch endpoint",wire_api="responses",'
        f"base_url={json.dumps(base_url)},env_key={json.dumps(key_env)},"
        # Bounded retries let an unavailable endpoint fail before the task timeout.
        "requires_openai_auth=false,request_max_retries=2,stream_max_retries=2}"
    )
    return [
        "-c",
        'model_provider="earnings_endpoint"',
        "-c",
        "model_providers.earnings_endpoint=" + provider,
    ]


def command(
    backend: str,
    task: AgentTask,
    schema: Path,
    scratch: Path,
    research_config: Path | None = None,
    codex_endpoint: tuple[str, str] | None = None,
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
        cmd += codex_endpoint_overrides(codex_endpoint)
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


async def bounded_read(stream: asyncio.StreamReader, watch: "_CodexWatch | None" = None) -> bytes:
    output = bytearray()
    while chunk := await stream.read(65536):
        output.extend(chunk)
        if watch is not None:
            watch.feed(chunk)
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


# Only explicit sign-in or account-limit refusals mean the backend never ran;
# ordinary words in a failed run's output must not trigger a second paid run.
_UNAVAILABLE = re.compile(
    r"not logged in|login required|please run /login|invalid api key|"
    r"authentication[ _](?:failed|required|error)|\b401\b[^\n]{0,40}unauthori[sz]ed|"
    r"(?:usage|rate|quota)[ _]limit|rate_limit_error|quota exceeded|insufficient[ _]quota|"
    r"exceeded your (?:current )?quota|out of (?:credits|quota)|"
    r"overloaded_error|model is (?:currently )?(?:at capacity|overloaded)|"
    r"\b429 too many requests|credit balance is too low|oauth token (?:has )?expired|"
    r"\bregion (?:is )?(?:not available|not supported|unsupported|blocked)|"
    r"unsupported country, region",
    re.I,
)
# Messages of Codex error/turn.failed events for an unreachable or failing
# endpoint. They mean "not started" only before turn output (see _CodexWatch).
_CODEX_ENDPOINT_FAILURE = re.compile(
    r"unexpected status (?:429|5\d\d)|waiting for network|error sending request"
)


class _CodexWatch:
    """Stop a Codex run that cannot reach its endpoint before producing output.

    Codex waits indefinitely for an unreachable host; without this the task
    would hold its worker until the timeout instead of trying the next backend.
    """

    def __init__(self, process: asyncio.subprocess.Process):
        self.process, self.turn, self.started, self.unreachable = process, False, False, False
        self.pending = b""

    @property
    def not_started(self) -> bool:
        return self.unreachable and not self.started

    def feed(self, chunk: bytes) -> None:
        lines = (self.pending + chunk).split(b"\n")
        self.pending = lines.pop()
        if len(self.pending) > 65536:
            # Only model output produces lines this long once the turn runs.
            self.started = self.started or self.turn
            self.pending = b""
        for raw in lines:
            line = raw.decode(errors="replace")
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = str(event.get("type", ""))
            if kind == "turn.started":
                self.turn = True
            elif kind.startswith("item."):
                item = event.get("item")
                # Startup notices arrive as error items; only turn output counts.
                if self.turn and not (isinstance(item, dict) and item.get("type") == "error"):
                    self.started = True
            elif kind in {"error", "turn.failed"} and not self.started:
                error = event.get("error")
                message = event.get("message") or (
                    error.get("message") if isinstance(error, dict) else ""
                )
                if isinstance(message, str) and _CODEX_ENDPOINT_FAILURE.search(message):
                    self.unreachable = True
                    if "waiting for network" in message:
                        _kill_group(self.process.pid, self.process.returncode is None)


def _group_alive(pgid: int) -> bool:
    """Whether any process still belongs to the CLI's group.

    A process group ID stays reserved while it has members, so killing a
    non-empty group cannot reach an unrelated process that reused the PID.
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return False
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            stat = (entry / "stat").read_text()
        except OSError:
            continue
        fields = stat.rsplit(")", 1)[-1].split()
        if len(fields) > 2 and fields[2] == str(pgid):
            return True
    return False


def _kill_group(pid: int, leader_running: bool) -> None:
    if not leader_running and not _group_alive(pid):
        return
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


class CliRunner:
    def __init__(self, backends: list[str], codex_endpoint: tuple[str, str] | None = None):
        self.backends = backends
        self.codex_endpoint = codex_endpoint
        self.checked: set[str] = set()

    async def _one(
        self, backend: str, task: AgentTask, research_config: Path | None = None
    ) -> AgentResult:
        binary = "claude" if backend == "claude-code" else "codex"
        if not shutil.which(binary):
            raise NotStarted("model CLI is missing")
        if backend not in self.checked:
            from .doctor import inspect

            if (await inspect(backend, codex_endpoint=self.codex_endpoint))["state"] != "ready":
                raise NotStarted("model CLI does not support the required isolation flags")
            self.checked.add(backend)
        with tempfile.TemporaryDirectory(prefix="earnings-model-") as directory:
            scratch = Path(directory)
            schema = scratch / "schema.json"
            schema.write_text(json.dumps(structured_schema(task.schema)))
            process = await asyncio.create_subprocess_exec(
                *command(backend, task, schema, scratch, research_config, self.codex_endpoint),
                cwd=scratch,
                env=environment(backend, self.codex_endpoint),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            assert process.stdin and process.stdout and process.stderr
            prompt = (task.system + "\n\n" + task.prompt).encode()
            stdin = process.stdin

            async def feed() -> None:
                # Inside the timeout: a CLI that never reads must not block the tick,
                # and one that exits early (e.g. not signed in) is classified below.
                try:
                    stdin.write(prompt)
                    await stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    stdin.close()

            watch = _CodexWatch(process) if backend == "codex" else None
            try:
                _, stdout, stderr, _ = await asyncio.wait_for(
                    asyncio.gather(
                        feed(),
                        bounded_read(process.stdout, watch),
                        bounded_read(process.stderr),
                        process.wait(),
                    ),
                    task.timeout_s,
                )
            except BaseException:
                _kill_group(process.pid, process.returncode is None)
                await process.wait()
                raise
            finally:
                # Children may outlive the CLI and keep running after it exits.
                _kill_group(process.pid, process.returncode is None)
            if process.returncode:
                # Never include CLI output: it can contain account details.
                text = (stdout + stderr).decode(errors="replace")
                if _UNAVAILABLE.search(text):
                    raise NotStarted("model authentication or capacity unavailable")
                if watch is not None and watch.not_started:
                    raise NotStarted("model endpoint unavailable")
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
