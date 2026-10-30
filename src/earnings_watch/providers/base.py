"""Provider interfaces. All outbound calls use Transport."""

from dataclasses import dataclass
from typing import Protocol

from earnings_watch.model.sources import Source


@dataclass(frozen=True)
class Capture:
    url: str
    text: str
    links: tuple[dict[str, str], ...] = ()


class DocumentSource(Protocol):
    async def documents(self, source: Source, period: str) -> list[dict]: ...


class BrowserReader(Protocol):
    async def read(self, url: str) -> Capture: ...
