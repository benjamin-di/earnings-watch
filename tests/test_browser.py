from types import SimpleNamespace
import pytest
from earnings_watch.browser import McpBrowserReader


class Browser:
    def __init__(self, final="https://issuer.example/calendar", fail=None):
        self.final, self.fail, self.calls = final, fail, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def call_tool(self, name, args, **kwargs):
        self.calls.append(name)
        assert name in {"start", "navigate", "extract", "end"}
        data = (
            {"sessionId": "synthetic-session"}
            if name == "start"
            else {
                "url": self.final,
                "documentText": "Synthetic financial events " * 5,
                "links": [{"url": "https://issuer.example/results", "title": "Results"}],
            }
        )
        return SimpleNamespace(is_error=name == self.fail, data=data)


async def test_browser_capture_budget_and_session_cleanup():
    browser = Browser()
    reader = McpBrowserReader(lambda: browser, {"issuer.example"}, max_reads=1)
    page = await reader.read("https://issuer.example/calendar")
    assert page.links[0]["url"] == "https://issuer.example/results"
    assert browser.calls == ["start", "navigate", "extract", "end"]
    with pytest.raises(ValueError, match="budget"):
        await reader.read("https://issuer.example/calendar")


@pytest.mark.parametrize(
    "final,fail",
    [("https://other.example/calendar", None), ("https://issuer.example/calendar", "extract")],
)
async def test_browser_refuses_foreign_page_and_cleans_up_failures(final, fail):
    browser = Browser(final, fail)
    with pytest.raises(ValueError):
        await McpBrowserReader(lambda: browser, {"issuer.example"}).read(
            "https://issuer.example/calendar"
        )
    assert browser.calls[-1] == "end"


async def test_source_repair_preserves_identity_and_has_durable_cooldown(seeded, monkeypatch):
    from unittest.mock import AsyncMock
    from conftest import source
    from earnings_watch.heal import repair, SourceUrls
    from earnings_watch.llm.runner import AgentResult
    from earnings_watch.providers.base import Capture
    from earnings_watch.store.database import Catalogue

    runner = AsyncMock()
    runner.run.return_value = AgentResult(
        SourceUrls(calendar_url="https://new.issuer.example/calendar"), "fake"
    )
    monkeypatch.setattr(
        "earnings_watch.heal.capture",
        AsyncMock(
            return_value=Capture("https://new.issuer.example/calendar", "ALPHA financial events")
        ),
    )
    catalogue = Catalogue(seeded.db)
    assert await repair(source(), catalogue, runner)
    updated = catalogue.sources()[0]
    assert updated.key == source().key and updated.exchange == source().exchange
    assert updated.calendar_url == "https://new.issuer.example/calendar"
    assert not await repair(updated, Catalogue(seeded.db), runner)
    assert runner.run.await_count == 1
    assert catalogue.events()[0]["issuer_key"] == source().key
