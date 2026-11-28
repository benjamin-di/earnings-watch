"""Tests for 2-phase IR asset harvesting, ir_assets persistence, and schema=2 library-targets."""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from earnings_watch.connections import _library_targets_payload
from earnings_watch.ir_harvest import (
    classify_ir_delta,
    discover_q4_ir_assets,
    extract_link_manifest,
    harvest_ir_assets,
)
from earnings_watch.llm.runner import AgentResult, AgentTask
from earnings_watch.model.events import Event, EventStatus, EventType
from earnings_watch.model.sources import FetchStrategy, PeriodScheme, Source
from earnings_watch.polling import poll
from earnings_watch.settings import Settings
from earnings_watch.store.database import Catalogue, PersonalStore, add_person, initialize
from earnings_watch.transport import Transport


def _source() -> Source:
    return Source(
        key="example-corp",
        issuer="Example Corp",
        ticker="EXM",
        name="Example Corp",
        exchange="XNYS",
        calendar_url="https://investors.example.com/calendar",
        results_url="https://investors.example.com/results",
        presentations_url="https://investors.example.com/events-and-presentations",
        transcripts_url="https://investors.example.com/transcripts",
        fetch=FetchStrategy.PLAIN,
        period_scheme=PeriodScheme.CALENDAR_Q,
        fy_end_month=12,
    )


class _StubRunner:
    def __init__(self, payload: dict[str, object] | None) -> None:
        self.payload = payload
        self.calls: list[AgentTask] = []

    async def run(self, task: AgentTask, *, research_config: Path | None = None) -> AgentResult:
        del research_config
        self.calls.append(task)
        if self.payload is None:
            return AgentResult(parsed=None, backend="stub")
        return AgentResult(parsed=task.schema.model_validate(self.payload), backend="stub")


def test_extract_link_manifest_strips_chrome_and_diffs_seen_urls() -> None:
    nav = "\n".join(f'<a href="/nav/{i}">Nav {i}</a>' for i in range(120))
    html = f"""
    <html><body>
      <header><nav>{nav}</nav></header>
      <main>
        <table>
          <tr>
            <td>Nov 5, 2026</td>
            <td>Q3 2026 Earnings Presentation</td>
            <td><a href="https://cdn.example.net/99/Q3-Deck.pdf">PDF</a></td>
            <td><a href="https://cdn.example.net/99/Q3-Audio.mp3">Listen</a></td>
          </tr>
        </table>
      </main>
      <footer>{nav}</footer>
    </body></html>
    """
    manifest = extract_link_manifest("https://investors.example.com/events-and-presentations", html)
    assert [m.url for m in manifest] == [
        "https://cdn.example.net/99/Q3-Deck.pdf",
        "https://cdn.example.net/99/Q3-Audio.mp3",
    ]
    assert "Q3 2026 Earnings Presentation" in manifest[0].context
    assert manifest[0].text == "PDF"

    delta = extract_link_manifest(
        "https://investors.example.com/events-and-presentations",
        html,
        seen_urls=[m.url for m in manifest],
    )
    assert delta == ()


@pytest.mark.asyncio
async def test_classify_ir_delta_zero_calls_when_empty_and_rejects_hallucinations() -> None:
    src = _source()
    empty_runner = _StubRunner({"items": []})
    assets, calls = await classify_ir_delta(
        src, "https://investors.example.com/events-and-presentations", [], empty_runner
    )
    assert assets == []
    assert calls == 0
    assert empty_runner.calls == []

    html = """
    <main><ul>
      <li>Investor Day 2026 <a href="https://cdn.example.net/99/id-2026.pdf">Slides</a></li>
      <li>Q3 Call <a href="https://media.example.org/q3-replay.mp3">Replay</a></li>
    </ul></main>
    """
    manifest = extract_link_manifest("https://investors.example.com/events-and-presentations", html)
    runner = _StubRunner(
        {
            "items": [
                {
                    "url": "https://cdn.example.net/99/id-2026.pdf",
                    "category": "presentation",
                    "title": "Investor Day 2026",
                    "published_date": "2026-10-15",
                    "period": None,
                    "evidence": "Investor Day 2026",
                },
                {
                    "url": "https://media.example.org/q3-replay.mp3",
                    "category": "audio_replay",
                    "title": "Q3 Call Replay",
                    "published_date": "2026-11-05",
                    "period": "2026Q3",
                    "evidence": "Q3 Call",
                },
                {
                    "url": "https://cdn.example.net/99/hallucinated.pdf",
                    "category": "presentation",
                    "title": "Fake",
                    "published_date": "2026-11-05",
                    "period": None,
                    "evidence": "Investor Day 2026",
                },
            ]
        }
    )
    verified, used = await classify_ir_delta(
        src, "https://investors.example.com/events-and-presentations", manifest, runner
    )
    assert used == 1
    assert len(runner.calls) == 1
    assert runner.calls[0].kind == "json"
    assert runner.calls[0].max_turns == 1
    assert [v["category"] for v in verified] == ["presentation", "audio_replay"]


@pytest.mark.asyncio
async def test_polling_runs_ir_harvest_even_when_released_at_already_set(tmp_path: Path) -> None:
    db = tmp_path / "earnings.db"
    initialize(db)
    cat = Catalogue(db)
    src = _source()
    cat.put_source(src)
    today = datetime.now(timezone.utc).date()
    ev = Event(
        id=f"earnings:{src.key}:2026Q3",
        issuer_key=src.key,
        ticker=src.ticker,
        type=EventType.EARNINGS,
        period="2026Q3",
        date=today,
        status=EventStatus.IR_CONFIRMED,
        source_url=src.calendar_url,
        evidence="Q3 2026 earnings",
        first_seen=today,
        last_verified=today,
    )
    cat.commit_issuer(src.key, [ev], run_id="r1", authoritative=True)
    # Simulate SEC/HKEX already having found the press release earlier today.
    cat.set_state(
        "release_watch",
        ev.id,
        {
            "event_date": str(today),
            "released_at": datetime.now(timezone.utc).isoformat(),
            "attempts": 1,
        },
    )

    ir_calls: list[str] = []

    async def fake_ir_harvest(source, event_row):
        ir_calls.append(event_row["id"])
        return [{"id": "asset-1", "url": "https://cdn.example.net/99/deck.pdf"}]

    transport = Transport(
        hosts={"investors.example.com"},
        mock=httpx.MockTransport(lambda _req: httpx.Response(200, text="<html></html>")),
        resolver=lambda _h, _p, **_kw: [(2, 1, 6, "", ("93.184.216.34", 443))],
        pace=0.0,
    )
    count = await poll(
        cat,
        src,
        transport,
        ir_harvest=fake_ir_harvest,
        providers={"generic_ir"},
    )
    assert ir_calls == [ev.id]
    assert count >= 1


@pytest.mark.asyncio
async def test_ir_assets_persistence_and_library_targets_v2(tmp_path: Path) -> None:
    db = tmp_path / "earnings.db"
    initialize(db)
    cat = Catalogue(db)
    src = _source()
    cat.put_source(src)
    add_person(db, "alice")
    PersonalStore(db, "alice").watch(src.ticker, src.exchange or "", origin="manual")

    # Persist an Investor Day presentation with event_id=None
    cat.put_ir_asset(
        {
            "id": "asset-investor-day",
            "source_key": src.key,
            "event_id": None,
            "url": "https://cdn.example.net/99/investor-day-2026.pdf",
            "category": "presentation",
            "role": "presentation",
            "title": "Investor Day 2026",
            "published_date": "2026-10-15",
            "period": None,
            "evidence": "Investor Day 2026",
            "evidence_url": src.presentations_url or "",
        }
    )

    settings = Settings(data_dir=tmp_path)
    payload_v2 = await _library_targets_payload(settings, directory=None, schema_version=2)
    assert payload_v2["schema"] == 2
    comp = payload_v2["companies"][0]
    assert comp["ir_urls"]["presentations"] == src.presentations_url
    assert comp["ir_urls"]["transcripts"] == src.transcripts_url
    assert len(comp["ir_assets"]) == 1
    assert comp["ir_assets"][0]["published_date"] == "2026-10-15"
    assert comp["ir_assets"][0]["category"] == "presentation"

    # Verify schema=1 stays backward compatible
    payload_v1 = await _library_targets_payload(settings, directory=None, schema_version=1)
    assert payload_v1["schema"] == 1
    assert "ir_assets" not in payload_v1["companies"][0]


@pytest.mark.asyncio
async def test_harvest_ir_assets_persists_seen_urls_and_skips_llm_on_second_pass(
    tmp_path: Path,
) -> None:
    db = tmp_path / "earnings.db"
    initialize(db)
    cat = Catalogue(db)
    src = _source()
    cat.put_source(src)

    html = """
    <html><body><main>
      <div>Q3 2026 Slides <a href="https://cdn.example.net/99/q3-slides.pdf">PDF</a></div>
    </main></body></html>
    """
    transport = Transport(
        hosts={"investors.example.com"},
        mock=httpx.MockTransport(
            lambda _req: httpx.Response(200, text=html, headers={"content-type": "text/html"})
        ),
        resolver=lambda _h, _p, **_kw: [(2, 1, 6, "", ("93.184.216.34", 443))],
        pace=0.0,
    )
    runner = _StubRunner(
        {
            "items": [
                {
                    "url": "https://cdn.example.net/99/q3-slides.pdf",
                    "category": "presentation",
                    "title": "Q3 2026 Slides",
                    "published_date": "2026-11-05",
                    "period": "2026Q3",
                    "evidence": "Q3 2026 Slides",
                }
            ]
        }
    )
    found1, calls1 = await harvest_ir_assets(cat, src, transport, runner=runner)
    assert len(found1) == 1
    assert calls1 == 1

    # Second pass on unchanged HTML must make 0 LLM calls!
    found2, calls2 = await harvest_ir_assets(cat, src, transport, runner=runner)
    assert found2 == []
    assert calls2 == 0
    assert len(runner.calls) == 1


@pytest.mark.asyncio
async def test_q4_ir_assets_discovers_presentations_and_events() -> None:
    src = _source()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("GetPresentationList"):
            return httpx.Response(
                200,
                json={
                    "GetPresentationListResult": [
                        {
                            "Title": "Investor Day 2026",
                            "PresentationDate": "10/15/2026 09:00:00",
                            "DocumentPath": "https://cdn.example.net/99/id.pdf",
                        }
                    ]
                },
            )
        if request.url.path.endswith("GetEventList"):
            return httpx.Response(
                200,
                json={
                    "GetEventListResult": [
                        {
                            "Title": "Q3 2026 Earnings Call",
                            "StartDate": "11/05/2026 14:00:00",
                            "WebCastLink": "https://cdn.example.net/99/call.mp3",
                            "Attachments": [
                                {
                                    "Title": "Q3 Transcript",
                                    "Type": "Transcript",
                                    "Url": "https://cdn.example.net/99/q3-transcript.pdf",
                                }
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(404)

    transport = Transport(
        hosts={"investors.example.com"},
        mock=httpx.MockTransport(handler),
        resolver=lambda _h, _p, **_kw: [(2, 1, 6, "", ("93.184.216.34", 443))],
        pace=0.0,
    )
    assets = await discover_q4_ir_assets(transport, src, today=date(2026, 11, 20))
    cats = {a["category"] for a in assets}
    assert cats == {"presentation", "transcript", "audio_replay"}
