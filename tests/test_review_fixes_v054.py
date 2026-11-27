"""Regression tests for independent review findings on v0.5.4 audit remediation."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from earnings_watch import calendar, documents, outbox, research
from earnings_watch.composition import open_service
from earnings_watch.http_server import AuthenticatedApp
from earnings_watch.llm.runner import AgentResult
from earnings_watch.model.events import Event, EventStatus, EventType
from earnings_watch.model.sources import Source
from earnings_watch.settings import Settings
from earnings_watch.store import accounts
from earnings_watch.store.database import (
    Catalogue,
    PersonalStore,
    connect,
    initialize,
    sync_holdings,
)
from earnings_watch.transport import Transport


def _make_source(key: str, ticker: str, exchange: str = "XNAS") -> Source:
    return Source(
        key=key,
        name=f"{ticker} Corp",
        issuer=f"{ticker} Corp",
        ticker=ticker,
        exchange=exchange,
        calendar_url=f"https://ir.{key}.example/calendar",
        fetch="plain",
        period_scheme="calendar-q",
        fy_end_month=12,
    )


def _make_event(source: Source, ev_id: str, d: date) -> Event:
    now = datetime(2026, 5, 1, tzinfo=timezone.utc)
    return Event(
        id=ev_id,
        issuer_key=source.key,
        ticker=source.ticker,
        type=EventType.EARNINGS,
        period="2026Q2",
        date=d,
        status=EventStatus.IR_CONFIRMED,
        source_url=source.calendar_url,
        evidence="June 15, 2026",
        first_seen=now,
        last_verified=now,
    )


def test_calendar_plan_no_holding_oracle_when_include_holdings_false(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    initialize(settings.db)
    cat = Catalogue(settings.db)
    s_held = _make_source("heldco", "HELD")
    s_pub = _make_source("pubco", "PUBC")
    cat.put_source(s_held)
    cat.put_source(s_pub)
    e_held = _make_event(s_held, "earnings:heldco:2026Q2", date(2026, 6, 15))
    e_pub = _make_event(s_pub, "earnings:pubco:2026Q2", date(2026, 6, 16))
    cat.commit_issuer(s_held.key, [e_held], run_id="r1")
    cat.commit_issuer(s_pub.key, [e_pub], run_id="r1")

    accounts.provision(settings.db, "alice", "alice")
    sync_holdings(settings.db, "alice", "broker", [{"ticker": "HELD", "exchange": "XNAS"}])

    key = b"k" * 32
    person = PersonalStore(settings.db, "alice")
    p_held = calendar.plan(person, key=key, event_ids=[e_held.id], include_holdings=False)
    p_pub = calendar.plan(person, key=key, event_ids=[e_pub.id], include_holdings=False)
    assert p_held["operations"] == []
    assert p_pub["operations"] == []


def test_unwatch_does_not_leak_or_mute_hidden_holdings(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    initialize(settings.db)
    cat = Catalogue(settings.db)
    src = _make_source("alpha", "ALPH")
    cat.put_source(src)
    accounts.provision(settings.db, "alice", "alice")
    sync_holdings(settings.db, "alice", "broker", [{"ticker": "ALPH", "exchange": "XNAS"}])

    svc = open_service(settings, "alice", include_holdings=False)
    res = svc.unwatch("ALPH", "XNAS")
    assert res == {"state": "removed"}
    assert accounts.mutes(settings.db, "alice") == set()


def test_outbox_strips_holding_metadata_on_dual_followed_when_revoked(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    initialize(settings.db)
    cat = Catalogue(settings.db)
    src = _make_source("dual", "DUAL")
    cat.put_source(src)
    ev = _make_event(src, "earnings:dual:2026Q2", date(2026, 6, 15))
    cat.commit_issuer(src.key, [ev], run_id="r1")
    accounts.provision(settings.db, "alice", "alice")
    PersonalStore(settings.db, "alice").watch("DUAL", "XNAS")
    sync_holdings(settings.db, "alice", "broker", [{"ticker": "DUAL", "exchange": "XNAS"}])

    payload = {
        "has_holdings": True,
        "event": {"id": ev.id, "issuer_key": src.key, "why_followed": ["holding", "watchlist"]},
        "documents": [],
    }
    outbox.enqueue(settings.db, "alice", "results:dual:initial", "results", payload)

    sent: list[dict[str, Any]] = []

    class CaptureNotifier:
        async def send(self, person: str, key: str, kind: str, body: dict[str, Any]) -> str:
            sent.append(body)
            return "ok"

    asyncio.run(outbox.deliver(settings.db, "alice", CaptureNotifier(), include_holdings=False))
    assert len(sent) == 1
    assert sent[0]["has_holdings"] is False
    assert sent[0]["event"]["why_followed"] == ["watchlist"]


def test_put_source_handles_preexisting_duplicate_source_key_rows(tmp_path: Path):
    settings = Settings(data_dir=tmp_path)
    initialize(settings.db)
    cat = Catalogue(settings.db)
    src = _make_source("dupco", "DUP", exchange="XNAS")
    cat.put_source(src)
    accounts.provision(settings.db, "alice", "alice")
    with connect(settings.db, write=True) as db:
        db.execute(
            "INSERT INTO follows(person,source_key,ticker,exchange,origin,state,added_at) VALUES(?,?,?,?,?,?,?)",
            ("alice", "dupco", "DUP", "", "holdings:broker", "registered", "2026-05-01T00:00:00Z"),
        )
        db.execute(
            "INSERT INTO follows(person,source_key,ticker,exchange,origin,state,added_at) VALUES(?,?,?,?,?,?,?)",
            (
                "alice",
                "dupco",
                "DUP",
                "XNAS",
                "holdings:broker",
                "registered",
                "2026-05-01T00:00:00Z",
            ),
        )
    # Must not raise sqlite3.IntegrityError on UNIQUE(person, ticker, exchange, origin)
    cat.put_source(src)
    follows = PersonalStore(settings.db, "alice").follows()
    assert len(follows) == 1
    assert follows[0]["exchange"] == "XNAS"


def test_http_server_directory_corrupt_json_returns_503_not_401(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("EW_BACKEND_TOKEN", "b" * 40)
    settings = Settings(
        data_dir=tmp_path,
        allowed_hosts=["testserver"],
        backend_tokens_env=["EW_BACKEND_TOKEN"],
        directory_url="https://dir.example",
        directory_required=True,
    )

    class BrokenDir:
        async def resolve(self, subject: str) -> dict[str, Any]:
            raise json.JSONDecodeError("bad json", "{", 1)

    initialize(settings.db)
    app = AuthenticatedApp(settings, "gateway", directory=BrokenDir())  # type: ignore[arg-type]
    client = TestClient(app)
    resp = client.post(
        "/mcp",
        headers={
            "Authorization": "Bearer " + "b" * 40,
            "X-Fleet-Subject": "valid_user_1",
            "X-Fleet-Person": "alice",
        },
        json={},
    )
    assert resp.status_code == 503


def test_transport_rejects_multicast_dns_resolution():
    import ipaddress

    multicast_ip = str(ipaddress.IPv4Address(0xE0000001))

    def multicast_resolver(host: str, port: int, **kwargs: Any):
        return [(2, 1, 6, "", (multicast_ip, port))]

    transport = Transport({"example.com"}, resolver=multicast_resolver)
    with pytest.raises(ValueError, match="non-public"):
        asyncio.run(transport.get("https://example.com/ir"))


def test_research_and_documents_write_sec_contact_into_scope_json(tmp_path: Path):
    src = _make_source("secissuer", "SECI")
    seen_scopes: list[dict[str, Any]] = []

    class FakeRunner:
        async def run(self, task: Any, *, research_config: Path | None = None) -> AgentResult:
            assert research_config is not None
            seen_scopes.append(json.loads(research_config.read_text()))
            return AgentResult(None, "none")

    asyncio.run(research.events(src, FakeRunner(), sec_contact="ops@example.com"))
    asyncio.run(documents.research(src, {"id": "e1"}, FakeRunner(), sec_contact="ops@example.com"))
    assert len(seen_scopes) == 2
    assert all(scope["sec_contact"] == "ops@example.com" for scope in seen_scopes)


def test_cli_notify_all_handles_directory_errors_and_unadmitted_users(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    from earnings_watch import cli
    from earnings_watch.transport import IdentityDirectory

    monkeypatch.setenv("EW_DIR_KEY", "k" * 40)
    settings = Settings(
        data_dir=tmp_path,
        directory_url="https://dir.example",
        directory_key_env="EW_DIR_KEY",
    )
    initialize(settings.db)
    cat = Catalogue(settings.db)
    src = _make_source("clico", "CLIC")
    cat.put_source(src)
    ev = _make_event(src, "earnings:clico:2026Q2", date(2026, 6, 15))
    cat.commit_issuer(src.key, [ev], run_id="r1")

    for pid in ("broken_user", "revoked_user", "good_user"):
        accounts.provision(settings.db, pid, pid)
        PersonalStore(settings.db, pid).watch("CLIC", "XNAS")
        outbox.enqueue(
            settings.db,
            pid,
            f"results:{ev.id}:initial",
            "results",
            {"event": {"id": ev.id, "issuer_key": src.key}, "documents": []},
        )

    async def fake_resolve(self: Any, subject: str, *, fresh: bool = False) -> dict[str, Any]:
        if subject == "broken_user":
            raise RuntimeError("directory timeout")
        if subject == "revoked_user":
            return {"admitted": False, "disabled": False, "portfolio_admitted": False}
        return {"admitted": True, "disabled": False, "portfolio_admitted": False}

    monkeypatch.setattr(IdentityDirectory, "resolve", fake_resolve)
    monkeypatch.setenv("EARNINGS_WATCH_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("EARNINGS_WATCH_DIRECTORY_URL", "https://dir.example")
    monkeypatch.setenv("EARNINGS_WATCH_DIRECTORY_KEY_ENV", "EW_DIR_KEY")
    rc = cli.main(["notify", "--all", "--channel", "stdout"])
    assert rc == 0
    raw = capsys.readouterr().out
    # First line is StdoutNotifier's delivered JSON line; remainder is the CLI summary JSON.
    delivered_line, summary_json = raw.split("\n", 1)
    assert json.loads(delivered_line)["person"] == "good_user"
    out = json.loads(summary_json)
    assert out["people"]["broken_user"] == {
        "sent": [],
        "unknown": [],
        "blocked": "directory unavailable",
    }
    assert out["people"]["revoked_user"] == {
        "sent": [],
        "unknown": [],
        "blocked": "not admitted",
    }
    assert out["people"]["good_user"]["sent"] == [f"results:{ev.id}:initial"]
    assert out["people"]["good_user"]["unknown"] == []


def test_connections_unwatch_does_not_mute_hidden_holdings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from earnings_watch.connections import create_app

    settings = Settings(data_dir=tmp_path, allowed_hosts=["testserver"])
    initialize(settings.db)
    cat = Catalogue(settings.db)
    src = _make_source("hidco", "HIDC")
    cat.put_source(src)
    accounts.provision(settings.db, "alice", "alice")
    sync_holdings(settings.db, "alice", "broker", [{"ticker": "HIDC", "exchange": "XNAS"}])
    rev = accounts.profile(settings.db, "alice")["revision"]

    class FakeAuth:
        def routes(self):
            return []

        def middleware(self, app):
            return app

        def identity(self, request):
            return {"sub": "alice", "name": "Alice", "claims": {"person_id": "alice"}}

        def origin_ok(self, request):
            return True

    class FakeDir:
        async def resolve(self, subject: str, *, fresh: bool = False) -> dict[str, Any]:
            return {"admitted": True, "disabled": False, "portfolio_admitted": False}

    app = create_app(settings, mode="gateway", auth=FakeAuth(), directory=FakeDir())  # type: ignore[arg-type]
    client = TestClient(app)
    resp = client.post(
        "/connections/unwatch",
        data={"ticker": "HIDC", "exchange": "XNAS", "revision": str(rev), "view": "companies"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert accounts.mutes(settings.db, "alice") == set()
