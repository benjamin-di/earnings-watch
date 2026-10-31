"""The only HTTP boundary: DNS pinning, per-hop policy, budgets and response bounds."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import ipaddress
import socket
import ssl
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import urljoin, urlsplit

import httpx

from .model.urls import public_url

MAX_BYTES = 12 * 1024 * 1024


@dataclass(frozen=True)
class Response:
    url: str
    body: bytes
    content_type: str
    status: int


class Transport:
    def __init__(
        self,
        hosts: set[str],
        *,
        state: Any = None,
        daily_budget: int = 200,
        pace: float = 1.0,
        mock: httpx.AsyncBaseTransport | None = None,
        resolver: Callable[..., Any] = socket.getaddrinfo,
        sec_contact: str = "",
        policies: dict[str, dict] | None = None,
        cache_max_bytes: int = 512 * 1024 * 1024,
    ):
        self.hosts = {h.lower() for h in hosts}
        self.state, self.budget, self.pace = state, daily_budget, pace
        self.mock, self.resolver = mock, resolver
        self.sec_contact = sec_contact
        self.policies = policies or {}
        self.cache_max_bytes = cache_max_bytes
        self.cache: dict[str, dict[str, Any]] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.last: dict[str, float] = {}
        self.memory: dict[str, dict[str, Any]] = {}
        self.parallel = asyncio.Semaphore(4)

    def _get(self, host: str) -> dict[str, Any]:
        return (
            self.state.state("fetch_state", "http:" + host)
            if self.state
            else self.memory.get(host, {})
        )

    def _put(self, host: str, doc: dict[str, Any]) -> None:
        self.memory[host] = doc
        if self.state:
            self.state.set_state("fetch_state", "http:" + host, doc)

    async def get(self, url: str, **kwargs) -> Response:
        return await self.request("GET", url, **kwargs)

    async def post(self, url: str, payload: dict, **kwargs) -> Response:
        return await self.request("POST", url, content=json.dumps(payload).encode(), **kwargs)

    async def request(
        self,
        method: str,
        url: str,
        *,
        user_agent: str = "earnings-watch/0.1",
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> Response:
        async with self.parallel:
            async with httpx.AsyncClient(
                timeout=30,
                trust_env=False,
                verify=ssl.create_default_context(),
                transport=self.mock,
            ) as client:
                for _ in range(11):
                    url = public_url(url)
                    host = urlsplit(url).hostname or ""
                    if host not in self.hosts:
                        raise ValueError("host is outside the configured issuer/provider allowlist")
                    if host in {"data.sec.gov", "www.sec.gov"}:
                        if not self.sec_contact or "@" not in self.sec_contact:
                            raise ValueError("SEC provider requires a configured contact email")
                        user_agent = "earnings-watch/0.1 " + self.sec_contact
                    # DNS is checked even in tests; tests provide a deterministic resolver.
                    records = await asyncio.to_thread(
                        self.resolver, host, 443, type=socket.SOCK_STREAM
                    )
                    addresses = {r[4][0] for r in records}
                    if not addresses or any(
                        not ipaddress.ip_address(ip).is_global for ip in addresses
                    ):
                        raise ValueError("DNS resolved to a non-public address")
                    ip = sorted(
                        addresses, key=lambda value: (ipaddress.ip_address(value).version, value)
                    )[0]
                    target = str(httpx.URL(url).copy_with(host=ip)) if self.mock is None else url
                    async with self.locks.setdefault(host, asyncio.Lock()):
                        saved = self._get(host)
                        if saved.get("breaker_until", 0) > time.time():
                            raise ValueError("host circuit breaker is open")
                        day = int(time.time() // 86400)
                        used = saved.get("used", 0) if saved.get("day") == day else 0
                        policy = self.policies.get(host, {})
                        if used >= policy.get("budget", self.budget):
                            raise ValueError("daily host request budget exhausted")
                        saved.update(day=day, used=used + 1)
                        self._put(host, saved)
                        await asyncio.sleep(
                            max(
                                0,
                                policy.get("pace_ms", self.pace * 1000) / 1000
                                - (time.monotonic() - self.last.get(host, 0)),
                            )
                        )
                        request_headers = dict(headers or {}) | {
                            "Host": host,
                            "User-Agent": user_agent,
                        }
                        cache_key = "cache:" + hashlib.sha256(url.encode()).hexdigest()
                        cached = (
                            self.state.state("fetch_state", cache_key)
                            if self.state
                            else self.cache.get(cache_key, {})
                        )
                        if content is not None:
                            request_headers["Content-Type"] = "application/json"
                        if method == "GET" and cached.get("etag"):
                            request_headers.setdefault("If-None-Match", cached["etag"])
                        if method == "GET" and cached.get("modified"):
                            request_headers.setdefault("If-Modified-Since", cached["modified"])
                        async with client.stream(
                            method,
                            target,
                            headers=request_headers,
                            content=content,
                            extensions={"sni_hostname": host},
                            follow_redirects=False,
                        ) as response:
                            self.last[host] = time.monotonic()
                            if response.status_code in {301, 302, 303, 307, 308}:
                                if method != "GET":
                                    raise ValueError("webhook redirects are refused")
                                url = urljoin(url, response.headers.get("location", ""))
                                continue
                            if response.status_code in {403, 429}:
                                delay = min(21600, max(300, saved.get("backoff", 150) * 2))
                                try:
                                    delay = max(
                                        delay,
                                        min(600, int(response.headers.get("retry-after", "0"))),
                                    )
                                except ValueError:
                                    pass
                                saved.update(backoff=delay, breaker_until=time.time() + delay)
                                self._put(host, saved)
                            if response.status_code == 304:
                                if cached.get("body"):
                                    return Response(
                                        url,
                                        base64.b64decode(cached["body"]),
                                        cached["content_type"],
                                        200,
                                    )
                                return Response(url, b"", "", 304)
                            response.raise_for_status()
                            body = bytearray()
                            async for chunk in response.aiter_bytes():
                                body.extend(chunk)
                                if len(body) > MAX_BYTES:
                                    raise ValueError("response exceeds the 12 MiB limit")
                            saved.update(breaker_until=0, backoff=0)
                            self._put(host, saved)
                            # Only small validator-bearing text pages are retained in
                            # SQLite; large documents use content-addressed downloads.
                            if (
                                method == "GET"
                                and len(body) <= 256 * 1024
                                and (
                                    response.headers.get("etag")
                                    or response.headers.get("last-modified")
                                )
                            ):
                                item = {
                                    "body": base64.b64encode(body).decode(),
                                    "content_type": response.headers.get("content-type", ""),
                                    "etag": response.headers.get("etag", ""),
                                    "modified": response.headers.get("last-modified", ""),
                                }
                                if self.state:
                                    self.state.put_cache(cache_key, item, self.cache_max_bytes)
                                else:
                                    if len(self.cache) >= 64:
                                        self.cache.pop(next(iter(self.cache)))
                                    self.cache[cache_key] = item
                                    while (
                                        self.cache
                                        and sum(len(json.dumps(v)) for v in self.cache.values())
                                        > self.cache_max_bytes
                                    ):
                                        self.cache.pop(next(iter(self.cache)))
                            return Response(
                                url,
                                bytes(body),
                                response.headers.get("content-type", ""),
                                response.status_code,
                            )
                raise ValueError("redirect limit exceeded")


class IdentityDirectory:
    """Finite identity-owner boundary; never contacts issuer pages or takes caller URLs."""

    def __init__(self, url: str, key: str, *, transport=None):
        from urllib.parse import urlsplit

        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or not parts.hostname
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ValueError("directory requires a fixed HTTPS endpoint")
        if len(key) < 32:
            raise ValueError("directory key is missing")
        self.url, self.key, self.transport = url.rstrip("/"), key, transport
        self.cache: dict[str, tuple[float, dict]] = {}

    async def resolve(self, subject: str, *, fresh: bool = False) -> dict:
        import re
        from urllib.parse import quote

        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,199}", subject):
            raise ValueError("invalid subject")
        cached = self.cache.get(subject)
        if not fresh and cached and cached[0] > time.monotonic():
            return dict(cached[1])
        async with httpx.AsyncClient(
            timeout=10, follow_redirects=False, transport=self.transport, trust_env=False
        ) as client:
            async with client.stream(
                "GET",
                self.url + "/users/" + quote(subject, safe=""),
                headers={"X-API-Key": self.key},
            ) as response:
                if response.status_code == 404:
                    return {"id": subject, "admitted": False, "disabled": True, "email": ""}
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 8192:
                        raise ValueError("directory response too large")
        import json

        body = json.loads(data)
        if (
            body.get("id") != subject
            or type(body.get("admitted")) is not bool
            or type(body.get("disabled")) is not bool
            or not isinstance(body.get("email"), str)
        ):
            raise ValueError("directory response is invalid")
        if len(self.cache) >= 1024:
            self.cache.clear()
        self.cache[subject] = (time.monotonic() + 60, dict(body))
        return body
