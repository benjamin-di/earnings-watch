"""Optional deterministic MCP reader; callers own server authentication and DNS policy."""

from urllib.parse import urlsplit

from .model.urls import public_url
from .providers.base import Capture


class McpBrowserReader:
    """Use only start/navigate/extract/end on an explicitly configured MCP client.

    The client factory may supply bearer or private-key JWT authentication. The
    trusted browser server must enforce public-address policy on every redirect;
    local URL checks cannot pin the remote browser's network connection.
    """

    def __init__(self, client_factory, hosts: set[str], *, max_reads: int = 3):
        self.client_factory = client_factory
        self.hosts = {host.lower() for host in hosts}
        self.remaining = max_reads

    def checked_url(self, url: str) -> str:
        url = public_url(url)
        if urlsplit(url).hostname not in self.hosts:
            raise ValueError("browser URL is outside the issuer allowlist")
        return url

    async def read(self, url: str) -> Capture:
        url = self.checked_url(url)
        if self.remaining <= 0:
            raise ValueError("browser read budget exhausted")
        self.remaining -= 1
        async with self.client_factory() as client:

            async def call(name, arguments):
                result = await client.call_tool(name, arguments, timeout=30)
                if result.is_error or not isinstance(result.data, dict):
                    raise ValueError("browser did not return a deterministic capture")
                return result.data

            session = {}
            try:
                started = await call("start", {})
                session = {"sessionId": started["sessionId"]}
                await call("navigate", session | {"url": url})
                page = await call("extract", session)
            finally:
                if session:
                    try:
                        await client.call_tool("end", session, timeout=15)
                    except Exception:
                        pass
        final = self.checked_url(page["url"])
        text = page["documentText"]
        if not isinstance(text, str) or not 40 <= len(text) <= 150000:
            raise ValueError("browser text is empty or exceeds the extraction budget")
        links = []
        for link in page.get("links", [])[:1000]:
            try:
                links.append(
                    {"url": public_url(link["url"]), "title": str(link.get("title", ""))[:500]}
                )
            except (KeyError, TypeError, ValueError):
                continue
        return Capture(final, text, tuple(links))
