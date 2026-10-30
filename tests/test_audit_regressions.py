"""Lifecycle, upgrade and interrupted-request regressions from the candidate audit."""

import asyncio
import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from conftest import event, source
from earnings_watch import open
from earnings_watch.documents import download, verify_link
from earnings_watch.errors import EarningsError
from earnings_watch.http_server import AuthenticatedApp
from earnings_watch.maintenance import Maintenance
from earnings_watch.mcp_server import person_context
from earnings_watch.notify import prepare
from earnings_watch.polling import MAX_POLL_ATTEMPTS, due, poll
from earnings_watch.model.events import ResultsLink
from earnings_watch.release import Signal
from earnings_watch.store.database import Catalogue, connect, initialize
from earnings_watch.transport import Response


def test_poll_budget_covers_release_day_and_the_full_monitoring_window():
    start = datetime(2026, 11, 1, tzinfo=timezone.utc)
    release_day = start.date() + timedelta(days=1)
    milestone = event(date=release_day)
    state = {"event_date": str(release_day)}
    polled_days = set()
    for tick in range(10 * 144):
        now = start + timedelta(minutes=10 * tick)
        if due(milestone, state, now):
            state.update(attempts=state.get("attempts", 0) + 1, last_attempt=now.isoformat())
            polled_days.add(now.date())
    assert polled_days == {start.date() + timedelta(days=i) for i in range(9)}
    assert state["attempts"] <= MAX_POLL_ATTEMPTS
    assert due(
        milestone.model_copy(update={"date": start.date() + timedelta(days=11)}),
        state,
        start + timedelta(days=10),
    )


def document():
    return verify_link(
        source(),
        event().model_dump(mode="json"),
        {
            "url": "https://issuer.example/results",
            "title": "Q4 2026 earnings results",
            "role": "release",
            "evidence": "Q4 2026 earnings results",
        },
        "Q4 2026 earnings results",
    )


async def test_official_document_survives_a_later_ir_page_failure(seeded, monkeypatch):
    catalogue = Catalogue(seeded.db)
    src = source().model_copy(
        update={"sec_cik": 123, "results_url": "https://issuer.example/results"}
    )
    catalogue.put_source(src)
    link = ResultsLink(
        url="https://issuer.example/release.pdf",
        title="Q4 2026 earnings results",
        evidence="Q4 2026 earnings results",
        evidence_url="https://issuer.example/results",
        kind="press-release",
    )
    provider = AsyncMock(
        return_value=(Signal(event().id, "official release", link=link), ["filing"])
    )
    monkeypatch.setattr("earnings_watch.release._sec", provider)
    transport = AsyncMock()
    transport.get.side_effect = ConnectionError("IR page unavailable")
    with pytest.raises(ConnectionError):
        await poll(catalogue, src, transport)
    assert catalogue.documents()[0]["url"] == link.url
    assert catalogue.state("release_watch", event().id)["seen"] == ["filing"]


def test_reintroduced_events_keep_increasing_calendar_sequences(seeded):
    catalogue = Catalogue(seeded.db)
    open(seeded).watch("ALPHA")
    for index in range(2):
        catalogue.commit_issuer("alpha", [], run_id=f"remove-{index}", authoritative=True)
        catalogue.commit_issuer("alpha", [event()], run_id=f"return-{index}")
    assert [row["rev"] for row in catalogue.changes(0, 100)] == [1, 2, 3, 4, 5]
    assert catalogue.events()[0]["revision"] == 5
    assert "SEQUENCE:5" in open(seeded).calendar_ics()


def test_removal_respects_history_after_an_older_candidate_reset_its_counter(seeded):
    with connect(seeded.db, write=True) as db:
        db.execute("UPDATE event_revisions SET rev=5")
    catalogue = Catalogue(seeded.db)
    catalogue.commit_issuer("alpha", [], run_id="retire", authoritative=True)
    assert catalogue.changes(0, 100)[-1]["rev"] == 6


def test_event_removal_preserves_personal_download_receipts_and_files(seeded, tmp_path):
    catalogue = Catalogue(seeded.db)
    doc = document()
    catalogue.put_document(doc)
    path = tmp_path / "user.pdf"
    path.write_bytes(b"downloaded content")
    catalogue.record_download("alice", doc, path, hashlib.sha256(path.read_bytes()).hexdigest())
    catalogue.commit_issuer("alpha", [], run_id="retire", authoritative=True)
    assert catalogue.events() == [] and catalogue.documents() == []
    assert path.read_bytes() == b"downloaded content"
    with connect(seeded.db) as db:
        row = db.execute("SELECT person,document_id,path FROM document_downloads").fetchone()
        assert tuple(row) == ("alice", doc["id"], str(path))
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    with connect(seeded.db, write=True) as db:
        db.execute("DELETE FROM people WHERE id='alice'")
        assert db.execute("SELECT count(*) FROM document_downloads").fetchone()[0] == 0


def test_schema_one_upgrade_preserves_receipts_key_and_supports_retirement(seeded, tmp_path):
    path = tmp_path / "old" / "earnings.db"
    path.parent.mkdir()
    key = path.parent / "calendar.key"
    key.write_bytes(b"original-signing-key")
    catalogue = Catalogue(seeded.db)
    doc = document()
    catalogue.put_document(doc)
    # Use the original released candidate's schema, including its receipt FK.
    with sqlite3.connect(path) as old, connect(seeded.db) as current:
        old.executescript(Path(__file__).with_name("schema-v1.sql").read_text())
        for table in ("people", "sources", "events", "event_revisions", "documents"):
            rows = current.execute(f"SELECT * FROM {table}").fetchall()
            columns = ",".join("?" for _ in rows[0])
            old.executemany(f"INSERT INTO {table} VALUES({columns})", rows)
        old.execute(
            "INSERT INTO document_downloads VALUES(?,?,?,?,?)",
            ("alice", doc["id"], "user.pdf", "saved-hash", "2026-01-01"),
        )
    initialize(path)
    initialize(path)
    assert key.read_bytes() == b"original-signing-key"
    Catalogue(path).commit_issuer("alpha", [], run_id="retire", authoritative=True)
    with connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 2
        assert tuple(db.execute("SELECT * FROM document_downloads").fetchone()) == (
            "alice",
            doc["id"],
            "user.pdf",
            "saved-hash",
            "2026-01-01",
        )
        assert not db.execute("PRAGMA foreign_key_check").fetchall()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(EarningsError, match="unsupported database schema"):
        initialize(path)
    with sqlite3.connect(path) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 99


async def test_disabled_models_do_not_fetch_calendar_or_reserve_registrations(seeded):
    service = open(seeded)
    service.watch("ALPHA")
    service.watch("UNKNOWN")
    transport = AsyncMock()
    result = await Maintenance(seeded, transport=transport).sweep()
    assert result["exit"] == 0 and result["counts"]["calls"] == 0
    transport.get.assert_not_awaited()
    with connect(seeded.db) as db:
        assert not db.execute("SELECT 1 FROM heal_state").fetchall()
    assert service.watchlist()["follows"][1]["state"] == "queued"


def test_queries_and_reminders_use_utc_in_a_different_host_timezone(seeded, monkeypatch):
    zone = "Pacific/Kiritimati" if datetime.now(timezone.utc).hour >= 10 else "Etc/GMT+12"
    monkeypatch.setenv("TZ", zone)
    time.tzset()
    try:
        today = datetime.now(timezone.utc).date()
        catalogue = Catalogue(seeded.db)
        catalogue.commit_issuer("alpha", [event(date=today)], run_id="today")
        service = open(seeded)
        service.watch("ALPHA")
        assert service.upcoming()["events"][0]["date"] == str(today)
        prepare(seeded)
        with connect(seeded.db) as db:
            reminder = db.execute("SELECT payload FROM deliveries WHERE kind='reminder'").fetchone()
            assert json.loads(reminder[0])["date"] == str(today)
    finally:
        monkeypatch.undo()
        time.tzset()


@pytest.mark.parametrize("failure", [asyncio.CancelledError, ConnectionError])
async def test_interrupted_http_body_does_not_leak_person_context(seeded, failure):
    app = AuthenticatedApp(seeded, "standalone")
    original = person_context.set("bob")
    try:
        scope = {
            "type": "http",
            "path": "/users/alice/mcp",
            "headers": [(b"host", b"localhost"), (b"authorization", b"Bearer " + b"a" * 40)],
        }
        receive = AsyncMock(side_effect=failure())
        with pytest.raises(failure):
            await app(scope, receive, AsyncMock())
        assert person_context.get() == "bob"
    finally:
        person_context.reset(original)


@pytest.mark.parametrize(
    "headers,expected",
    [
        ([(b"origin", b"https://[")], 403),
        ([(b"authorization", b"Bearer " + b"\xff" * 40)], 401),
    ],
)
async def test_invalid_http_headers_are_refused(seeded, monkeypatch, headers, expected):
    monkeypatch.setenv("SYNTHETIC_BACKEND", "c" * 40)
    seeded.backend_tokens_env = ["SYNTHETIC_BACKEND"]
    scope = {"type": "http", "path": "/mcp", "headers": [(b"host", b"localhost"), *headers]}
    send = AsyncMock()
    await AuthenticatedApp(seeded, "gateway")(scope, AsyncMock(), send)
    assert send.call_args_list[0].args[0]["status"] == expected


async def test_download_uses_full_hash_and_repairs_corrupt_content(tmp_path):
    body = b"%PDF-1.4 synthetic document"
    sha = hashlib.sha256(body).hexdigest()
    transport = AsyncMock()
    transport.get.return_value = Response(
        "https://issuer.example/results", body, "application/pdf", 200
    )
    path = tmp_path / f"release-{sha}.pdf"
    path.write_bytes(b"corrupted")
    doc = document()
    assert await download(doc, transport=transport, folder=tmp_path) == path
    assert path.read_bytes() == body
    path.unlink()
    unrelated = tmp_path / "unrelated.pdf"
    unrelated.write_bytes(b"unrelated")
    path.symlink_to(unrelated)
    with pytest.raises(ValueError, match="symlink"):
        await download(doc, transport=transport, folder=tmp_path)
    assert unrelated.read_bytes() == b"unrelated"
