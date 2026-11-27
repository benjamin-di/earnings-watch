"""Behavioral tests for real-world symbol normalization, partial CSV onboarding,
fast-path registration with visible failure reasons, and recent/timeline projections.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from signin_window.oidc import BrowserAuth, OIDCConfig

from earnings_watch.composition import open_service
from earnings_watch.connections import create_app
from earnings_watch.errors import EarningsError
from earnings_watch.interests import apply, preview
from earnings_watch.llm.runner import AgentResult
from earnings_watch.maintenance import Maintenance
from earnings_watch.model.events import EventStatus
from earnings_watch.presentation import group_upcoming
from earnings_watch.store import accounts
from earnings_watch.store.database import (
    MAX_DAILY_REGISTRATIONS,
    Catalogue,
    PersonalStore,
    connect,
)
from earnings_watch.symbols import parse_symbol, validate_ir_url

from conftest import event, source


class _Directory:
    async def resolve(self, subject, **kwargs):
        return dict(id=subject, admitted=True, disabled=False, email=subject + "@example.com")


def _browser(settings):
    settings.public_url = "http://localhost"
    auth = BrowserAuth(
        OIDCConfig(
            issuer="https://id.example.com",
            client_id="earnings",
            public_url="http://localhost",
            state_dir=settings.data_dir / "browser",
            allow_loopback_http=True,
        )
    )
    auth.store.save(
        "a" * 43,
        dict(
            issuer="https://id.example.com",
            sub="alice",
            groups=[],
            claims={},
            expires=datetime.now(timezone.utc).timestamp() + 300,
        ),
    )
    return auth


def test_parse_symbol_handles_exchange_suffixes_and_share_classes():
    assert parse_symbol("7203.T") == ("7203", "XTKS")
    assert parse_symbol("sap.de") == ("SAP", "XETR")
    assert parse_symbol("AIR.PA") == ("AIR", "XPAR")
    assert parse_symbol("XTKS:7203") == ("7203", "XTKS")
    # Share-class dots/hyphens must not be treated as exchange suffixes.
    assert parse_symbol("BRK.B") == ("BRK.B", "")
    assert parse_symbol("BF-B") == ("BF-B", "")
    with pytest.raises(EarningsError, match="Conflicting exchange"):
        parse_symbol("7203.T", "XNAS")


def test_watch_accepts_suffix_class_share_and_company_name(seeded):
    accounts.provision(seeded.db, "alice")
    jp_source = source(key="jp-example", ticker="7203").model_copy(
        update={"name": "Harbor Holdings", "exchange": "XTKS"}
    )
    class_source = source(key="brk-example", ticker="BRK.B").model_copy(
        update={"name": "Berkshire Synthetic", "exchange": "XNYS"}
    )
    cat = Catalogue(seeded.db)
    cat.put_source(jp_source)
    cat.put_source(class_source)

    person = PersonalStore(seeded.db, "alice")
    assert person.watch("7203.T")["source_key"] == "jp-example"
    assert person.watch("BRK.B")["source_key"] == "brk-example"
    assert person.watch("Example Components")["source_key"] == "alpha"

    follows = {row["ticker"]: row for row in person.follows()}
    assert follows["7203"]["company_name"] == "Harbor Holdings"
    assert follows["BRK.B"]["company_name"] == "Berkshire Synthetic"
    assert follows["ALPHA"]["company_name"] == "Example Components"


def test_watch_ir_url_hint_validation_and_retry_reset(seeded):
    accounts.provision(seeded.db, "alice")
    person = PersonalStore(seeded.db, "alice")
    with pytest.raises(EarningsError, match="public https://"):
        validate_ir_url("http://ir.example.com/calendar")
    with pytest.raises(EarningsError, match="public https://"):
        validate_ir_url("https://127.0.0.1/calendar")

    res = person.watch("NEWCO", "XNAS", ir_url="https://ir.newco.example/events")
    assert res["state"] == "queued"
    cat = Catalogue(seeded.db)
    cat.set_state(
        "heal_state",
        "registration:" + json.dumps(["NEWCO", "XNAS"]),
        {"next_attempt": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
    )
    cat.record_registration_error("NEWCO", "XNAS", "Official IR page could not be verified.")
    assert person.follows()[0]["reason"] == "Official IR page could not be verified."

    # Supplying a new IR URL hint clears the error reason, charges quota, and records hint_updated_at
    # without mutating shared heal_state directly.
    updated = person.watch("NEWCO", "XNAS", ir_url="https://investors.newco.example/calendar#top")
    assert updated["updated"] is True
    row = person.follows()[0]
    assert row["reason"] == ""
    hints = json.loads(row["hints"])
    assert hints["ir_url"] == "https://investors.newco.example/calendar"
    assert hints.get("hint_updated_at")
    # Re-submitting the identical IR URL is a no-op and does not charge quota again.
    with connect(seeded.db) as db:
        used_before = db.execute(
            "SELECT used FROM registration_quotas WHERE person='alice'"
        ).fetchone()[0]
    noop = person.watch("NEWCO", "XNAS", ir_url="https://investors.newco.example/calendar")
    assert noop["updated"] is False
    with connect(seeded.db) as db:
        used_after = db.execute(
            "SELECT used FROM registration_quotas WHERE person='alice'"
        ).fetchone()[0]
    assert used_after == used_before


def test_partial_csv_import_attaches_known_and_queues_unlisted(seeded):
    accounts.provision(seeded.db, "alice")
    csv_text = (
        "ticker,exchange,quantity,cost\nALPHA,XNAS,100,5000\n7203.T,,200,8000\nBRK.B,,10,4000\n"
    )
    p = preview(seeded, "alice", csv_text)
    assert p["items"] == [{"ticker": "ALPHA", "exchange": "XNAS"}]
    assert p["unlisted"] == [
        {"ticker": "7203", "exchange": "XTKS", "isin": None},
        {"ticker": "BRK.B", "exchange": "", "isin": None},
    ]
    assert p["queued"] == 2
    assert p["adds"] == 3

    applied = apply(seeded, "alice", p["handle"])
    assert applied == {"follows": 1, "queued": 2}

    follows = {(r["ticker"], r["exchange"]): r for r in PersonalStore(seeded.db, "alice").follows()}
    assert follows[("ALPHA", "XNAS")]["state"] == "registered"
    assert follows[("7203", "XTKS")]["state"] == "queued"
    assert follows[("BRK.B", "")]["state"] == "queued"

    # Re-importing the exact same CSV must not double-charge daily registration quota.
    with connect(seeded.db) as db:
        used_before = db.execute(
            "SELECT used FROM registration_quotas WHERE person='alice'"
        ).fetchone()[0]
    assert used_before == 2
    p2 = preview(seeded, "alice", csv_text)
    apply(seeded, "alice", p2["handle"])
    with connect(seeded.db) as db:
        used_after = db.execute(
            "SELECT used FROM registration_quotas WHERE person='alice'"
        ).fetchone()[0]
    assert used_after == 2


def test_daily_registration_quota_is_100(seeded):
    accounts.provision(seeded.db, "alice")
    lines = ["ticker"] + [f"UNL{i}" for i in range(MAX_DAILY_REGISTRATIONS)]
    p = preview(seeded, "alice", "\n".join(lines))
    assert p["queued"] == MAX_DAILY_REGISTRATIONS
    res = apply(seeded, "alice", p["handle"])
    assert res["queued"] == MAX_DAILY_REGISTRATIONS

    with pytest.raises(EarningsError, match="pending|limit"):
        PersonalStore(seeded.db, "alice").watch("UNLOVER")


async def test_fast_path_register_records_and_clears_failure_reason(seeded):
    accounts.provision(seeded.db, "alice")
    person = PersonalStore(seeded.db, "alice")
    person.watch("FAILCO", "XNAS")
    cat = Catalogue(seeded.db)
    assert cat.has_pending_registrations()

    class DeferRunner:
        async def run(self, task, *, research_config=None):
            return AgentResult(parsed=None, backend="fake")

    maint = Maintenance(seeded, runner=DeferRunner())
    res = await maint.register()
    assert res["calls"] == 1
    assert res["registered"] == []
    assert "deferred" in person.follows()[0]["reason"].lower()

    cat.put_source(
        source(key="failco", ticker="FAILCO").model_copy(
            update={"name": "FailCo Synthetic", "exchange": "XNAS"}
        )
    )
    row = person.follows()[0]
    assert row["state"] == "registered"
    assert row["reason"] == ""
    assert not cat.has_pending_registrations()


async def test_fast_path_register_falls_back_to_browser_on_403(seeded, monkeypatch):
    from earnings_watch.providers.base import Capture

    accounts.provision(seeded.db, "alice")
    person = PersonalStore(seeded.db, "alice")
    person.watch("CFCO", "XNAS")

    class ProposeRunner:
        async def run(self, task, *, research_config=None):
            return AgentResult(
                parsed=source(key="cfco", ticker="CFCO").model_copy(
                    update={
                        "name": "Cloudflare Protected Corp",
                        "exchange": "XNAS",
                        "calendar_url": "https://investor.cfco.example/overview/default.aspx",
                    }
                ),
                backend="fake",
            )

    async def fail_403(transport, url):
        req = httpx.Request("GET", url)
        resp = httpx.Response(403, request=req)
        raise httpx.HTTPStatusError("403 Forbidden", request=req, response=resp)

    monkeypatch.setattr("earnings_watch.registration.capture", fail_403)

    browser_calls = []

    class FakeBrowser:
        async def read(self, url, *, allow_hosts=None):
            browser_calls.append((url, allow_hosts))
            return Capture(
                url,
                "Cloudflare Protected Corp (NASDAQ: CFCO) Investor Relations Financial Calendar",
                (),
            )

    maint = Maintenance(seeded, runner=ProposeRunner(), browser=FakeBrowser())
    res = await maint.register()
    assert res["registered"] == ["cfco"]
    assert res["errors"] == []
    assert browser_calls == [
        ("https://investor.cfco.example/overview/default.aspx", {"investor.cfco.example"})
    ]
    row = person.follows()[0]
    assert row["state"] == "registered"
    assert row["source_key"] == "cfco"
    assert row["reason"] == ""


def test_recent_results_and_timeline_grouping(seeded):
    accounts.provision(seeded.db, "alice")
    PersonalStore(seeded.db, "alice").watch("ALPHA", "XNAS")
    cat = Catalogue(seeded.db)
    today = datetime.now(timezone.utc).date()

    past_recent = event(
        id="earnings:alpha:2025Q4",
        date=today - timedelta(days=3),
        period="2025Q4",
        status=EventStatus.PAST,
    )
    past_old = event(
        id="earnings:alpha:2025Q3",
        date=today - timedelta(days=45),
        period="2025Q3",
        status=EventStatus.PAST,
    )
    upcoming_w1 = event(id="earnings:alpha:2026Q1", date=today + timedelta(days=2), period="2026Q1")
    upcoming_w2 = event(
        id="earnings:alpha:2026Q2", date=today + timedelta(days=10), period="2026Q2"
    )
    upcoming_later = event(
        id="earnings:alpha:2026Q3", date=today + timedelta(days=40), period="2026Q3"
    )
    cat.commit_issuer(
        "alpha",
        [past_recent, past_old, upcoming_w1, upcoming_w2, upcoming_later],
        run_id="r-timeline",
    )
    cat.put_document(
        {
            "id": "doc:2025Q4:release",
            "event_id": past_recent.id,
            "role": "press-release",
            "title": "Q4 2025 Earnings Release",
            "url": "https://issuer.example/q4-release.pdf",
            "verified": True,
            "first_seen": datetime.now(timezone.utc).isoformat(),
        }
    )

    svc = open_service(seeded, "alice")
    upcoming = svc.upcoming(days=90)["events"]
    assert all(e["company_name"] == "Example Components" for e in upcoming)
    grouped = group_upcoming(upcoming, today=today)
    assert [e["period"] for e in grouped["this_week"]] == ["2026Q4", "2026Q1"]
    assert [e["period"] for e in grouped["next_week"]] == ["2026Q2"]
    assert [e["period"] for e in grouped["later"]] == ["2026Q3"]

    recent = svc.recent(days=14)["events"]
    assert [e["period"] for e in recent] == ["2025Q4"]
    assert recent[0]["company_name"] == "Example Components"
    assert recent[0]["documents"][0]["title"] == "Q4 2025 Earnings Release"


async def test_changed_badge_survives_post_redirect_until_marked_seen(seeded):
    accounts.provision(seeded.db, "alice")
    PersonalStore(seeded.db, "alice").watch("ALPHA", "XNAS")
    auth = _browser(seeded)
    app = create_app(seeded, auth=auth, directory=_Directory())
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        rev = accounts.profile(seeded.db, "alice")["revision"]
        r = await client.post(
            "/connections/watch",
            data={"revision": str(rev), "ticker": "NEWCO"},
            headers={"Origin": "http://localhost"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert r.headers["location"] == "/connections/?keep_seen=1"
        page_after = await client.get(r.headers["location"])
        assert "Changed" in page_after.text
        assert accounts.profile(seeded.db, "alice")["seen_seq"] == 0

        rev2 = accounts.profile(seeded.db, "alice")["revision"]
        r2 = await client.post(
            "/connections/mark-seen",
            data={"revision": str(rev2)},
            headers={"Origin": "http://localhost"},
            follow_redirects=True,
        )
        assert r2.status_code == 200
        assert "Changed" not in r2.text
        assert accounts.profile(seeded.db, "alice")["seen_seq"] > 0


def test_short_unlisted_tickers_are_not_hijacked_by_company_names(seeded):
    accounts.provision(seeded.db, "alice")
    person = PersonalStore(seeded.db, "alice")
    # "ON", "MP", "T" are all substrings of "Example Components" (ticker ALPHA).
    for short_ticker in ("ON", "MP", "T"):
        res = person.watch(short_ticker)
        assert res["state"] == "queued"
        assert res["source_key"] is None
    p = preview(seeded, "alice", "ticker\nON\nMP\nT\n")
    assert p["items"] == []
    assert [u["ticker"] for u in p["unlisted"]] == ["MP", "ON", "T"]


def test_unwatch_normalizes_suffix_and_share_class(seeded):
    accounts.provision(seeded.db, "alice")
    cat = Catalogue(seeded.db)
    cat.put_source(
        source(key="jp-example", ticker="7203").model_copy(
            update={"name": "Harbor Holdings", "exchange": "XTKS"}
        )
    )
    cat.put_source(
        source(key="brk-example", ticker="BRK.B").model_copy(
            update={"name": "Berkshire Synthetic", "exchange": "XNYS"}
        )
    )
    person = PersonalStore(seeded.db, "alice")
    person.watch("7203.T")
    person.watch("BRK.B")
    assert person.unwatch("7203.T")["state"] == "removed"
    assert person.unwatch("BRK.B")["state"] == "removed"
    assert person.follows() == []


def test_sync_holdings_accepts_share_class_tickers(seeded):
    from earnings_watch.store.database import sync_holdings

    accounts.provision(seeded.db, "alice")
    Catalogue(seeded.db).put_source(
        source(key="brk-example", ticker="BRK.B").model_copy(
            update={"name": "Berkshire Synthetic", "exchange": "XNYS"}
        )
    )
    res = sync_holdings(seeded.db, "alice", "portfolio", [{"ticker": "BRK.B", "exchange": "XNYS"}])
    assert res["follows"] == 1


async def test_csv_queued_holding_ir_url_hint_triggers_deterministic_retry(seeded, monkeypatch):
    from earnings_watch.providers.base import Capture
    from earnings_watch.registration import register_pending

    accounts.provision(seeded.db, "alice")
    p = preview(seeded, "alice", "ticker,exchange\nHINTCO,XNAS\n")
    apply(seeded, "alice", p["handle"])

    seen_prompts: list[dict] = []

    class CaptureRunner:
        async def run(self, task, *, research_config=None):
            seen_prompts.append(json.loads(task.prompt))
            if not seen_prompts[-1]["hints"].get("ir_url"):
                return AgentResult(parsed=None, backend="fake")
            return AgentResult(
                parsed=source(key="hintco", ticker="HINTCO").model_copy(
                    update={
                        "name": "HintCo Synthetic",
                        "exchange": "XNAS",
                        "calendar_url": "https://issuer.example/hintco",
                    }
                ),
                backend="fake",
            )

    async def fake_capture(transport, url):
        return Capture(url=url, text="HINTCO official investor relations calendar")

    monkeypatch.setattr("earnings_watch.registration.capture", fake_capture)
    cat = Catalogue(seeded.db)
    first = await register_pending(seeded, cat, CaptureRunner())
    assert first["registered"] == []
    # Updating ir_url updates the existing holdings:upload row in place (no duplicate manual row).
    person = PersonalStore(seeded.db, "alice")
    upd = person.watch("HINTCO", "XNAS", ir_url="https://issuer.example/hintco")
    assert upd["updated"] is True
    assert len(person.follows()) == 1
    assert person.follows()[0]["origin"] == "holdings:upload"

    # Next register_pending retries immediately despite 24h backoff because hint_updated_at > last_attempt.
    second = await register_pending(seeded, cat, CaptureRunner())
    assert second["registered"] == ["hintco"]
    assert seen_prompts[-1]["hints"]["ir_url"] == "https://issuer.example/hintco"
    assert person.follows()[0]["state"] == "registered"


async def test_connections_escapes_registration_reason_and_paused_register(seeded):
    accounts.provision(seeded.db, "alice")
    PersonalStore(seeded.db, "alice").watch("XSSCO", "XNAS")
    Catalogue(seeded.db).record_registration_error("XSSCO", "XNAS", '<script>alert("xss")</script>')
    auth = _browser(seeded)
    app = create_app(seeded, auth=auth, directory=_Directory())
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        resp = await client.get("/connections/?view=companies")
        assert resp.status_code == 200
        assert '<script>alert("xss")</script>' not in resp.text
        assert "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt;" in resp.text

    (seeded.data_dir / ".paused").write_text("paused")
    res = await Maintenance(seeded).register()
    assert res == {"state": "paused", "exit": 3}
