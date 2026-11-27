from __future__ import annotations

import asyncio
import json
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from conftest import event, source
from earnings_watch import polling, release
from earnings_watch.cli import execute, parser
from earnings_watch.heal import repair
from earnings_watch.llm.research_server import create as create_research_server
from earnings_watch.llm.runner import AgentResult, AgentTask, CliRunner
from earnings_watch.model.events import ExtractResult
from earnings_watch.providers.generic_ir import capture
from earnings_watch.registration import register_pending
from earnings_watch.store.database import Catalogue
from earnings_watch.transport import Transport
from earnings_watch.worker import sweep_due


def _public_resolver(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


@pytest.mark.parametrize("items_field", [[["2.02", "9.01"]], ["2.02, 9.01"]])
async def test_sec_edgar_items_list_and_csv(items_field):
    now = datetime(2026, 11, 27, 15, 0, tzinfo=timezone.utc)
    src = source().model_copy(update={"sec_cik": 123456})
    ev = event(date=now.date(), period="2026Q4")
    accession = "0000123456-26-000001"
    submissions = {
        "filings": {
            "recent": {
                "form": ["8-K"],
                "accessionNumber": [accession],
                "filingDate": [str(now.date())],
                "items": items_field,
                "primaryDocDescription": ["8-K"],
                "acceptanceDateTime": ["2026-11-27T14:30:00Z"],
            }
        }
    }
    index_html = (
        "<table><tr><td>1</td><td>Press Release</td>"
        '<td><a href="/Archives/edgar/data/123456/000012345626000001/ex991.htm">ex991.htm</a></td>'
        "<td>EX-99.1</td></tr></table>"
    )
    exhibit_html = "<html><body>Example Components reports Q4 2026 earnings results.</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith(".json"):
            return httpx.Response(
                200, json=submissions, headers={"content-type": "application/json"}
            )
        if url.endswith("-index.htm"):
            return httpx.Response(200, text=index_html, headers={"content-type": "text/html"})
        return httpx.Response(200, text=exhibit_html, headers={"content-type": "text/html"})

    transport = Transport(
        {"data.sec.gov", "www.sec.gov"},
        pace=0,
        sec_contact="ops@example.com",
        resolver=_public_resolver,
        mock=httpx.MockTransport(handler),
    )
    signal, seen = await release._sec(src, ev, now, set(), transport)
    assert signal is not None
    assert signal.link is not None
    assert signal.link.url.endswith("ex991.htm")
    assert accession in seen


async def test_redirect_does_not_leak_sec_contact_user_agent():
    seen_agents: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_agents.append((request.headers["host"], request.headers["user-agent"]))
        if request.headers["host"] == "www.sec.gov":
            return httpx.Response(302, headers={"location": "https://issuer.example/final"})
        return httpx.Response(200, text="ok", headers={"content-type": "text/plain"})

    transport = Transport(
        {"www.sec.gov", "issuer.example"},
        pace=0,
        sec_contact="ops@example.com",
        resolver=_public_resolver,
        mock=httpx.MockTransport(handler),
    )
    response = await transport.get("https://www.sec.gov/start")
    assert response.body == b"ok"
    assert seen_agents == [
        ("www.sec.gov", "earnings-watch/0.1 ops@example.com"),
        ("issuer.example", "earnings-watch/0.1"),
    ]


async def test_transport_disables_keepalive_pool(monkeypatch):
    captured_limits: list[httpx.Limits | None] = []
    real_async_client = httpx.AsyncClient

    def spy_client(*args, **kwargs):
        captured_limits.append(kwargs.get("limits"))
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", spy_client)
    transport = Transport(
        {"issuer.example"},
        pace=0,
        resolver=_public_resolver,
        mock=httpx.MockTransport(
            lambda _r: httpx.Response(200, text="ok", headers={"content-type": "text/plain"})
        ),
    )
    await transport.get("https://issuer.example/page")
    assert len(captured_limits) == 1
    assert captured_limits[0] is not None
    assert captured_limits[0].max_keepalive_connections == 0


def test_sec_contact_forwarded_at_all_four_transport_sites(seeded, tmp_path, monkeypatch):
    seen_contacts: list[str] = []
    real_transport = Transport

    def spy_transport(*args, **kwargs):
        seen_contacts.append(kwargs.get("sec_contact", ""))
        return real_transport(*args, **kwargs)

    # 1. cli.py documents --download
    from earnings_watch import open as open_ew

    cat = Catalogue(seeded.db)
    cat.put_document(
        {
            "id": "doc-sec-1",
            "event_id": event().id,
            "role": "release",
            "url": "https://www.sec.gov/Archives/edgar/data/1/ex991.htm",
            "title": "Q4 2026 results",
            "evidence": "Q4 2026 results",
            "evidence_url": "https://www.sec.gov/Archives/edgar/data/1/index.htm",
            "verified": True,
            "unverified_reason": None,
            "first_seen": datetime.now(timezone.utc).isoformat(),
            "sha256": "",
        }
    )
    open_ew(seeded).watch("ALPHA")
    sec_settings = seeded.model_copy(
        update={"sec_contact": "sec-ops@example.com", "downloads_root": tmp_path}
    )

    monkeypatch.setattr("earnings_watch.transport.Transport", spy_transport)
    monkeypatch.setattr(
        "earnings_watch.documents.download",
        AsyncMock(return_value=tmp_path / "release.html"),
    )
    args = parser().parse_args(["documents", "--download", str(tmp_path)])
    execute(args, sec_settings)
    assert seen_contacts[-1] == "sec-ops@example.com"

    # 2. registration.py
    from earnings_watch import registration as reg_mod

    monkeypatch.setattr(reg_mod, "Transport", spy_transport)
    monkeypatch.setattr(
        reg_mod,
        "capture",
        AsyncMock(return_value=type("Page", (), {"text": "Example Components NEWCO"})()),
    )
    open_ew(sec_settings).watch("NEWCO", "XNAS")
    proposed = source(key="newco", ticker="NEWCO", exchange="XNAS")
    runner = AsyncMock()
    runner.run.return_value = AgentResult(proposed, "codex")
    asyncio.run(register_pending(sec_settings, Catalogue(sec_settings.db), runner))
    assert seen_contacts[-1] == "sec-ops@example.com"

    # 3. heal.py
    from earnings_watch import heal as heal_mod

    monkeypatch.setattr(heal_mod, "Transport", spy_transport)
    monkeypatch.setattr(
        heal_mod,
        "capture",
        AsyncMock(return_value=type("Page", (), {"text": "Example Components ALPHA"})()),
    )
    heal_runner = AsyncMock()
    heal_runner.run.return_value = AgentResult({"calendar_url": "https://www.sec.gov/cal"}, "codex")
    asyncio.run(
        repair(source(), Catalogue(sec_settings.db), heal_runner, sec_contact="sec-ops@example.com")
    )
    assert seen_contacts[-1] == "sec-ops@example.com"

    # 4. llm/research_server.py
    create_research_server(
        {
            "directory": str(tmp_path / "captures"),
            "hosts": ["www.sec.gov"],
            "sec_contact": "sec-ops@example.com",
        }
    )
    assert seen_contacts[-1] == "sec-ops@example.com"


async def test_polling_throttles_research_and_isolates_results_url_errors(seeded, monkeypatch):
    base_now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
    current_now = [base_now]

    class FakeDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return current_now[0]

    monkeypatch.setattr(polling, "datetime", FakeDateTime)

    cat = Catalogue(seeded.db)
    src = source().model_copy(update={"results_url": "https://issuer.example/results"})
    cat.put_source(src)

    # Event is tomorrow (offset == -1): research must NOT trigger.
    ev_tomorrow = event(date=base_now.date() + timedelta(days=1))
    cat.commit_issuer("alpha", [ev_tomorrow], run_id="r1")

    transport = AsyncMock()
    transport.get.side_effect = ConnectionError("IR page down")
    research_fn = AsyncMock(return_value=[])

    # results_url failure must not raise, and offset == -1 must not invoke research
    assert await polling.poll(cat, src, transport, research=research_fn) == 0
    assert research_fn.await_count == 0

    # Move event to today (offset == 0): results_url failure falls through to research()
    ev_today = event(date=base_now.date())
    cat.commit_issuer("alpha", [ev_today], run_id="r2")

    assert await polling.poll(cat, src, transport, research=research_fn) == 0
    assert research_fn.await_count == 1
    state = cat.state("release_watch", ev_today.id)
    assert state["research_attempts"] == 1
    assert state["last_research_attempt"] == base_now.isoformat()

    # 1 hour later: poll is due, but research cooldown (< 6h) suppresses research()
    current_now[0] = base_now + timedelta(hours=1)
    assert await polling.poll(cat, src, transport, research=research_fn) == 0
    assert research_fn.await_count == 1

    # Advance 6h per step until max 6 attempts are reached; 7th attempt is suppressed
    for expected in range(2, 7):
        current_now[0] = current_now[0] + timedelta(hours=6)
        assert await polling.poll(cat, src, transport, research=research_fn) == 0
        assert research_fn.await_count == expected

    current_now[0] = current_now[0] + timedelta(hours=6)
    assert await polling.poll(cat, src, transport, research=research_fn) == 0
    assert research_fn.await_count == 6

    # Changing event_date resets research_attempts & last_research_attempt
    ev_rescheduled = event(date=current_now[0].date())
    cat.commit_issuer("alpha", [ev_rescheduled], run_id="r3")
    assert await polling.poll(cat, src, transport, research=research_fn) == 0
    assert research_fn.await_count == 7


async def test_cli_runner_null_result_raises_validation_error_not_type_error(monkeypatch):
    class SimpleModel(BaseModel):
        name: str

    class FakeProc:
        pid = 999999
        returncode = 0

        def __init__(self, stdout_bytes: bytes):
            self.stdin = _FakeStdin()
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data(stdout_bytes)
            self.stdout.feed_eof()
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_eof()

        async def wait(self):
            return 0

    class _FakeStdin:
        def write(self, _data):
            pass

        async def drain(self):
            pass

        def close(self):
            pass

    monkeypatch.setattr("shutil.which", lambda _b: "/usr/bin/claude")
    monkeypatch.setattr("earnings_watch.llm.runner._kill_group", lambda *_a: None)
    monkeypatch.setattr(
        asyncio,
        "create_subprocess_exec",
        AsyncMock(return_value=FakeProc(json.dumps({"result": None}).encode())),
    )
    runner = CliRunner(["claude-code"])
    runner.checked.add("claude-code")
    task = AgentTask(
        system="sys",
        prompt="prompt",
        schema=SimpleModel,
        allow_repair=False,
    )
    with pytest.raises(ValidationError):
        await runner._one("claude-code", task)


async def test_cli_runner_research_repair_does_not_downgrade_to_blind_json(tmp_path):
    runner = CliRunner(["codex"])
    calls: list[tuple[str, AgentTask, Path | None]] = []

    async def fake_one(backend, task, research_config=None):
        calls.append((backend, task, research_config))
        if len(calls) == 1:
            raise ValueError("schema mismatch")
        return AgentResult(None, backend)

    runner._one = fake_one  # type: ignore[method-assign]
    cfg = tmp_path / "scope.json"
    cfg.write_text("{}")
    task = AgentTask(
        system="Research",
        prompt="Find links",
        schema=ExtractResult,
        kind="research",
        allow_repair=True,
    )
    res = await runner.run(task, research_config=cfg)
    assert res.repaired is True
    assert len(calls) == 2
    assert calls[1][1].kind == "research"
    assert calls[1][2] == cfg


def test_worker_post_midnight_retry_does_not_suppress_daily_sweep():
    # Yesterday's 23:10 sweep failed and its retry succeeded at 00:15 UTC today.
    status = {
        "last_sweep": {
            "started": "2026-10-30T00:15:00+00:00",
            "ended": "2026-10-30T00:18:00+00:00",
            "exit": 0,
        }
    }
    at_window = datetime(2026, 10, 30, 23, 10, tzinfo=timezone.utc)
    assert sweep_due(status, at_window) is True

    # Once today's 23:10 sweep has run, it is no longer due.
    status_done = {
        "last_sweep": {
            "started": "2026-10-30T23:10:00+00:00",
            "ended": "2026-10-30T23:12:00+00:00",
            "exit": 0,
        }
    }
    assert sweep_due(status_done, datetime(2026, 10, 30, 23, 20, tzinfo=timezone.utc)) is False


async def test_heal_short_ticker_and_word_boundary_matching(seeded, monkeypatch):
    from earnings_watch import heal as heal_mod

    # Short ticker (<= 2 chars): "F" must NOT match a random page containing the letter/word F.
    ford = source(key="ford", ticker="F", exchange="XNYS").model_copy(
        update={"name": "Ford Motor Company", "issuer": "Ford Motor Company"}
    )
    heal_runner = AsyncMock()
    heal_runner.run.return_value = AgentResult(
        {"calendar_url": "https://issuer.example/new-cal"}, "codex"
    )
    monkeypatch.setattr(
        heal_mod,
        "capture",
        AsyncMock(return_value=type("Page", (), {"text": "Press F to pay respects"})()),
    )
    with pytest.raises(ValueError, match="does not identify"):
        await repair(ford, Catalogue(seeded.db), heal_runner, dry_run=True)

    # Short ticker accepted when source.name matches.
    monkeypatch.setattr(
        heal_mod,
        "capture",
        AsyncMock(return_value=type("Page", (), {"text": "Ford Motor Company IR Calendar"})()),
    )
    assert await repair(ford, Catalogue(seeded.db), heal_runner, dry_run=True) is True

    # 3-char ticker ("CAT") must NOT match inside "catalog".
    cat_src = source(key="cat", ticker="CAT", exchange="XNYS").model_copy(
        update={"name": "Caterpillar Inc.", "issuer": "Caterpillar Inc."}
    )
    monkeypatch.setattr(
        heal_mod,
        "capture",
        AsyncMock(return_value=type("Page", (), {"text": "Browse our product catalog"})()),
    )
    with pytest.raises(ValueError, match="does not identify"):
        await repair(cat_src, Catalogue(seeded.db), heal_runner, dry_run=True)

    # Word-boundary match for "CAT" succeeds.
    monkeypatch.setattr(
        heal_mod,
        "capture",
        AsyncMock(return_value=type("Page", (), {"text": "NYSE: CAT Investor Calendar"})()),
    )
    assert await repair(cat_src, Catalogue(seeded.db), heal_runner, dry_run=True) is True


async def test_generic_ir_capture_accepts_uppercase_content_type():
    transport = Transport(
        {"issuer.example"},
        pace=0,
        resolver=_public_resolver,
        mock=httpx.MockTransport(
            lambda _r: httpx.Response(
                200,
                text="<html><body>Earnings Calendar</body></html>",
                headers={"content-type": "TEXT/HTML; charset=UTF-8"},
            )
        ),
    )
    page = await capture(transport, "https://issuer.example/calendar")
    assert "Earnings Calendar" in page.text
