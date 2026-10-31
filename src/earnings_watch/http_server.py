"""HTTP authentication chooses personal state; library reads stay shared."""

from __future__ import annotations
import hashlib
import hmac
import os
import re
from urllib.parse import urlsplit
from starlette.responses import JSONResponse
from .mcp_server import create, person_context
from .settings import Settings
from .store.database import people, token_hash, LibraryReader


class AuthenticatedApp:
    def __init__(self, settings: Settings, mode: str, *, directory=None):
        self.settings, self.mode, self.directory = settings, mode, directory
        if mode not in {"standalone", "gateway"}:
            raise ValueError("invalid HTTP identity mode")
        main = create(settings).http_app(path="/mcp", stateless_http=True)
        from fastmcp import FastMCP
        from contextlib import asynccontextmanager
        from starlette.applications import Starlette
        from starlette.routing import Mount

        monitor = FastMCP("Earnings operations", mask_error_details=True)

        @monitor.tool(annotations={"readOnlyHint": True})
        def earnings_status() -> dict:
            """Shared acquisition health only; no personal interests or receipts."""
            status = LibraryReader(settings.db).status()
            compact = {key: status[key] for key in ("sources", "events", "documents", "sequence")}
            for key in ("last_run", "last_sweep"):
                row = status[key]
                compact[key] = (
                    {k: row[k] for k in ("kind", "started", "ended", "exit")} if row else None
                )
            return compact

        monitoring = monitor.http_app(path="/mcp", stateless_http=True, json_response=True)
        self.main, self.monitoring = main, monitoring

        @asynccontextmanager
        async def lifespan(app):
            async with (
                main.router.lifespan_context(main),
                monitoring.router.lifespan_context(monitoring),
            ):
                yield

        self.app = Starlette(routes=[Mount("/", app=main)], lifespan=lifespan)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        host = headers.get(b"host", b"").decode("latin-1").lower()
        allowed = self.settings.allowed_hosts
        host_only = host.rsplit(":", 1)[0] if ":" in host and not host.endswith("]") else host
        origin = headers.get(b"origin", b"").decode("latin-1")
        try:
            valid_origin = not origin or urlsplit(origin).netloc.lower() == host
        except ValueError:
            valid_origin = False
        if host_only not in allowed or not valid_origin:
            return await JSONResponse({"error": "host or origin refused"}, 403)(
                scope, receive, send
            )
        try:
            length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            length = 262145
        if length < 0 or length > 262144:
            return await JSONResponse({"error": "body too large"}, 413)(scope, receive, send)
        bearer = headers.get(b"authorization", b"").decode("latin-1")
        token = bearer[7:] if bearer.startswith("Bearer ") else ""
        path = scope.get("path", "")
        person = None
        monitor_path = False
        if self.mode == "standalone":
            match = re.fullmatch(r"/users/([a-z0-9][a-z0-9_-]{0,63})/mcp/?", path)
            if match and len(token) >= 32:
                candidate = match.group(1)
                expected = token_hash(self.settings.db, candidate)
                if expected and hmac.compare_digest(
                    expected, hashlib.sha256(token.encode()).hexdigest()
                ):
                    person = candidate
        else:
            tokens = [os.environ.get(name, "") for name in self.settings.backend_tokens_env]
            valid = len(token) >= 32 and any(
                len(t) >= 32 and hmac.compare_digest(token.encode(), t.encode()) for t in tokens
            )
            if valid and path.rstrip("/") == "/monitor/mcp":
                monitor_path = True
            candidate = headers.get(b"x-fleet-person", b"").decode("latin-1")
            if valid and self.directory is not None and path.rstrip("/") == "/mcp":
                subject = headers.get(b"x-fleet-subject", b"").decode("latin-1")
                try:
                    info = await self.directory.resolve(subject)
                    if info["admitted"] and not info["disabled"]:
                        from .store.accounts import provision

                        person = provision(self.settings.db, subject, candidate)
                except Exception:
                    return await JSONResponse({"error": "identity directory unavailable"}, 503)(
                        scope, receive, send
                    )
            elif (
                valid
                and not self.settings.directory_required
                and candidate in {p["id"] for p in people(self.settings.db)}
                and path.rstrip("/") == "/mcp"
            ):
                person = candidate
        if person is None and not monitor_path:
            return await JSONResponse({"error": "unauthorized"}, 401)(scope, receive, send)
        received = 0
        messages = []
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            received += len(message.get("body", b""))
            if received > 262144:
                return await JSONResponse({"error": "body too large"}, 413)(scope, receive, send)
            messages.append(message)
            if not message.get("more_body", False):
                break

        async def bounded_receive():
            return messages.pop(0) if messages else await receive()

        rewritten = dict(scope, path="/mcp", raw_path=b"/mcp")
        if monitor_path:
            return await self.monitoring(rewritten, bounded_receive, send)
        assert person is not None
        identity = person_context.set(person)
        try:
            await self.app(rewritten, bounded_receive, send)
        finally:
            person_context.reset(identity)
