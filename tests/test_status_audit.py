"""Data and delivery regressions found in the implementation-status audit."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest

from conftest import event, source
from test_subscription_review import Recorder
from test_subscriptions import put_doc, subscribe
from earnings_watch import ics, release
from earnings_watch import open as open_service
from earnings_watch.connections import create_app
from earnings_watch.interests import apply, preview
from earnings_watch.llm.runner import AgentResult
from earnings_watch.notify import prepare
from earnings_watch.outbox import deliver
from earnings_watch.providers.base import Capture
from earnings_watch.registration import register_pending
from earnings_watch.store.database import Catalogue, PersonalStore, connect
from earnings_watch.transport import Response


@pytest.mark.parametrize(
    ("accepted", "expected"),
    [
        ("2026-11-02T13:15:30Z", "2026-11-02T13:15:30+00:00"),
        ("2026-11-02T13:15:30.123Z", "2026-11-02T13:15:30.123000+00:00"),
        ("2026-11-02T13:15:30+00:00", "2026-11-02T13:15:30+00:00"),
        ("2026-11-02T08:15:30-05:00", "2026-11-02T13:15:30+00:00"),
        # Legacy unqualified acceptance clocks use the exchange's local zone.
        ("2026-11-02T08:15:30", "2026-11-02T13:15:30+00:00"),
    ],
)
async def test_sec_acceptance_preserves_timezone_and_precision(accepted, expected):
    src = source().model_copy(update={"sec_cik": 123})
    filing = {
        "form": ["8-K"],
        "accessionNumber": ["0000000123-26-000001"],
        "filingDate": ["2026-11-02"],
        "items": ["2.02"],
        "acceptanceDateTime": [accepted],
    }
    index = '<tr><td>1</td><td>Results</td><td><a href="/Archives/results.htm">exhibit</a></td><td>EX-99.1</td></tr>'
    transport = AsyncMock()
    transport.get.side_effect = [
        Response(
            "https://data.sec.gov/submissions/CIK0000000123.json",
            json.dumps({"filings": {"recent": filing}}).encode(),
            "application/json",
            200,
        ),
        Response("https://www.sec.gov/index.htm", index.encode(), "text/html", 200),
        Response("https://www.sec.gov/Archives/results.htm", b"Q4 2026 results", "text/html", 200),
    ]
    signal, _ = await release._sec(
        src,
        event(date=datetime(2026, 11, 2).date()),
        datetime(2026, 11, 3, tzinfo=timezone.utc),
        set(),
        transport,
    )
    assert signal is not None
    assert signal.published_at == datetime.fromisoformat(expected)


@pytest.mark.parametrize("timed", [True, False])
def test_ics_end_is_strictly_after_start_when_present(timed):
    values = {"release_time": "08:00", "release_tz": "UTC"} if timed else {}
    row = event(**values).model_dump(mode="json") | {"revision": 1}
    content = ics.render([row])
    lines = content.splitlines()
    start = next(line.split(":", 1)[1] for line in lines if line.startswith("DTSTART"))
    ends = [line.split(":", 1)[1] for line in lines if line.startswith("DTEND")]
    assert all(end > start for end in ends)
    if timed:
        assert start.endswith("T080000Z")
    else:
        assert ends  # All-day entries retain their exclusive next-day end.


@pytest.mark.parametrize("ticker", ["", " ", "\t"])
def test_csv_blank_ticker_falls_back_to_isin_without_losing_holdings(seeded, ticker):
    catalogue = Catalogue(seeded.db)
    catalogue.put_source(source().model_copy(update={"isin": "US0000000001"}))
    imported = preview(seeded, "alice", "ticker,isin,exchange\n" + ticker + ",US0000000001,XNAS\n")
    assert imported["items"] == [{"ticker": "ALPHA", "exchange": "XNAS"}]
    apply(seeded, "alice", imported["handle"])
    assert PersonalStore(seeded.db, "alice").follows()[0]["ticker"] == "ALPHA"


@pytest.mark.parametrize("integrity", ["ok", "row 1 missing from index events_date"])
async def test_http_health_reports_failed_integrity_as_unavailable(seeded, monkeypatch, integrity):
    monkeypatch.setattr("earnings_watch.connections.integrity", lambda _: integrity)
    app = create_app(seeded, mode="standalone")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == (200 if integrity == "ok" else 503)


async def test_pending_brief_refreshes_before_submission(seeded):
    subscribe(seeded, results=False, brief=True, hour=0)
    prepare(seeded)

    class Unready(Recorder):
        async def ready(self, *args):
            return False

    assert not (await deliver(seeded.db, "alice", Unready()))["sent"]
    moved = event(date=event().date + timedelta(days=2))
    Catalogue(seeded.db).commit_issuer("alpha", [moved], run_id="move")
    put_doc(seeded, "new-statements", role="statements")
    prepare(seeded)
    sender = Recorder()
    assert len((await deliver(seeded.db, "alice", sender))["sent"]) == 1
    payload = sender.calls[0][3]
    assert payload["events"] == []
    assert payload["changes"][-1]["doc"]["date"] == str(moved.date)
    assert [d["id"] for d in payload["documents"]] == ["new-statements"]


async def test_uncertain_brief_remains_immutable_when_catalogue_changes(seeded):
    subscribe(seeded, results=False, brief=True, hour=0)
    prepare(seeded)

    class Uncertain(Recorder):
        async def send(self, *args):
            raise TimeoutError("Submission outcome is unknown")

    assert len((await deliver(seeded.db, "alice", Uncertain()))["unknown"]) == 1
    with connect(seeded.db) as db:
        before = tuple(
            db.execute("SELECT state,payload FROM deliveries WHERE person='alice'").fetchone()
        )
    Catalogue(seeded.db).commit_issuer("alpha", [event(evidence="New evidence")], run_id="prose")
    prepare(seeded)
    with connect(seeded.db) as db:
        after = tuple(
            db.execute("SELECT state,payload FROM deliveries WHERE person='alice'").fetchone()
        )
    assert before == after


@pytest.mark.parametrize("key", ["alpha", "beta"])
async def test_registration_cannot_reuse_another_issuers_source_key(seeded, monkeypatch, key):
    open_service(seeded, "alice").watch("BETA", exchange="XNAS")
    proposed = source(key=key, ticker="BETA").model_copy(update={"name": "Beta"})
    runner = AsyncMock()
    runner.run.return_value = AgentResult(proposed, "fake")
    monkeypatch.setattr(
        "earnings_watch.registration.capture",
        AsyncMock(return_value=Capture(proposed.calendar_url, "BETA results calendar")),
    )
    catalogue = Catalogue(seeded.db)
    before = catalogue.events()
    result = await register_pending(seeded, catalogue, runner)
    assert bool(result["errors"]) == (key == "alpha")
    assert next(s for s in catalogue.sources() if s.key == "alpha").ticker == "ALPHA"
    assert catalogue.events() == before
    follows = PersonalStore(seeded.db, "alice").follows()
    assert follows[0]["state"] == ("queued" if key == "alpha" else "registered")
