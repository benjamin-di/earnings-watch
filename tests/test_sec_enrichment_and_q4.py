from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from earnings_watch.composition import open_service
from earnings_watch.llm.runner import AgentResult, AgentTask
from earnings_watch.maintenance import Maintenance, _layer1_fingerprint
from earnings_watch.model.events import Event, EventStatus, EventType, ExtractResult
from earnings_watch.model.sources import FetchStrategy, PeriodScheme, Source
from earnings_watch.providers.q4_ir import discover_q4, is_q4_source
from earnings_watch.providers.sec_edgar import resolve_sec_identity
from earnings_watch.store.database import Catalogue


class _StubTransport:
    def __init__(self, responses: dict[str, tuple[int, bytes]]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    async def get(self, url: str, *, user_agent: str | None = None):
        self.requested.append(url)
        for prefix, (status, body) in self.responses.items():
            if url == prefix or url.startswith(prefix):
                if status >= 400:
                    import httpx

                    req = httpx.Request("GET", url)
                    resp = httpx.Response(status, request=req)
                    raise httpx.HTTPStatusError("error", request=req, response=resp)
                return SimpleNamespace(url=url, status=status, body=body)
        import httpx

        req = httpx.Request("GET", url)
        resp = httpx.Response(404, request=req)
        raise httpx.HTTPStatusError("not found", request=req, response=resp)


@pytest.mark.asyncio
async def test_resolve_sec_identity_populates_cik_fy_end_and_recent_ipo():
    today = date(2026, 11, 28)
    src = Source(
        key="synthipo",
        issuer="SynthAI, PBC",
        ticker="SYNT",
        name="SynthAI, PBC Class A Common Stock",
        exchange="XNAS",
        calendar_url="https://investor.synthai.example.com/events/default.aspx",
        fetch=FetchStrategy.WEBSEARCH,
        period_scheme=PeriodScheme.CALENDAR_Q,
        fy_end_month=6,  # wrong LLM guess; SEC has 1231
        sec_cik=None,
    )
    tickers_payload = json.dumps(
        {"0": {"cik_str": 2130999, "ticker": "SYNT", "title": "SynthAI, PBC"}}
    ).encode()
    sub_payload = json.dumps(
        {
            "fiscalYearEnd": "1231",
            "filings": {
                "recent": {
                    "form": ["8-K", "424B4", "EFFECT", "S-1/A"],
                    "filingDate": ["2026-11-25", "2026-11-19", "2026-11-18", "2026-11-17"],
                    "items": [["1.01", "8.01"], [], [], []],
                }
            },
        }
    ).encode()
    transport = _StubTransport(
        {
            "https://www.sec.gov/files/company_tickers.json": (200, tickers_payload),
            "https://data.sec.gov/submissions/CIK0002130999.json": (200, sub_payload),
        }
    )

    res = await resolve_sec_identity(transport, src, "ops@example.com", today=today)
    assert res.sec_cik == 2130999
    assert res.fy_end_month == 12
    assert res.ipo_date == date(2026, 11, 19)


@pytest.mark.asyncio
async def test_resolve_sec_identity_clears_ipo_when_reporting_history_exists_and_guards_collisions():
    today = date(2026, 11, 28)

    # 1. Existing 10-Q clears ipo_date.
    mature = Source(
        key="matureco",
        issuer="Mature Systems Inc",
        ticker="MTR",
        name="Mature Systems",
        exchange="XNYS",
        calendar_url="https://ir.mature.example.com/events",
        fetch=FetchStrategy.WEBSEARCH,
        period_scheme=PeriodScheme.FISCAL_Q,
        fy_end_month=5,
        sec_cik=1341999,
    )
    sub_payload = json.dumps(
        {
            "fiscalYearEnd": "0531",
            "filings": {
                "recent": {
                    "form": ["8-K", "10-Q", "424B4"],
                    "filingDate": ["2026-11-20", "2026-09-15", "2026-08-01"],
                    "items": [["2.02"], [], []],
                }
            },
        }
    ).encode()
    res = await resolve_sec_identity(
        _StubTransport({"https://data.sec.gov/submissions/CIK0001341999.json": (200, sub_payload)}),
        mature,
        "ops@example.com",
        today=today,
    )
    assert res.sec_cik == 1341999
    assert res.fy_end_month == 5
    assert res.ipo_date is None

    # 2. Non-US issuer sharing a US ticker must NOT match.
    uk_src = Source(
        key="ukbank",
        issuer="London Example Bank",
        ticker="SYNT",
        name="London Example Bank",
        exchange="XLON",
        calendar_url="https://ir.london.example.co.uk/events",
        fetch=FetchStrategy.PLAIN,
        period_scheme=PeriodScheme.HALF_YEAR,
        fy_end_month=12,
        sec_cik=None,
    )
    tickers_payload = json.dumps(
        {"0": {"cik_str": 2130999, "ticker": "SYNT", "title": "SynthAI, PBC"}}
    ).encode()
    res_uk = await resolve_sec_identity(
        _StubTransport({"https://www.sec.gov/files/company_tickers.json": (200, tickers_payload)}),
        uk_src,
        "ops@example.com",
        today=today,
    )
    assert res_uk.sec_cik is None

    # 3. US ticker with unrelated company name must NOT match.
    mismatch = Source(
        key="otherco",
        issuer="Completely Different Corp",
        ticker="SYNT",
        name="Completely Different Corp",
        exchange="XNAS",
        calendar_url="https://ir.different.example.com/events",
        fetch=FetchStrategy.WEBSEARCH,
        period_scheme=PeriodScheme.CALENDAR_Q,
        fy_end_month=12,
        sec_cik=None,
    )
    res_mis = await resolve_sec_identity(
        _StubTransport({"https://www.sec.gov/files/company_tickers.json": (200, tickers_payload)}),
        mismatch,
        "ops@example.com",
        today=today,
    )
    assert res_mis.sec_cik is None


@pytest.mark.asyncio
async def test_q4_detection_in_plain_mode_and_subpath_fallback():
    plain_q4 = Source(
        key="q4plain",
        issuer="Q4 Plain Co",
        ticker="Q4PL",
        name="Q4 Plain Co",
        exchange="XNAS",
        calendar_url="https://investors.q4plain.example.com/events-and-presentations",
        fetch=FetchStrategy.PLAIN,
        period_scheme=PeriodScheme.CALENDAR_Q,
        fy_end_month=12,
    )
    assert is_q4_source(plain_q4) is True

    subpath_q4 = Source(
        key="subq4",
        issuer="Subpath Q4 Co",
        ticker="SUBQ",
        name="Subpath Q4 Co",
        exchange="XNAS",
        calendar_url="https://corp.example.com/investor/events/default.aspx",
        fetch=FetchStrategy.WEBSEARCH,
        period_scheme=PeriodScheme.CALENDAR_Q,
        fy_end_month=12,
    )
    event_list = json.dumps(
        {
            "GetEventListResult": [
                {"Title": "Q4 2026 Earnings Call", "StartDate": "01/28/2027 16:00:00"}
            ]
        }
    ).encode()
    transport = _StubTransport(
        {
            "https://corp.example.com/investor/feed/Event.svc/GetEventList": (200, event_list),
            "https://corp.example.com/investor/feed/PressRelease.svc/GetPressReleaseList": (
                200,
                b'{"GetPressReleaseListResult": []}',
            ),
        }
    )
    docs, _ = await discover_q4(transport, subpath_q4, today=date(2026, 11, 28))
    assert any("Q4 2026 Earnings Call" in text for text in docs.values())


@pytest.mark.asyncio
async def test_layer1_hash_gating_skips_llm_when_unchanged_and_probe_triggers_on_change(settings):
    catalogue = Catalogue(settings.db)
    svc = open_service(settings)
    now = datetime.now(timezone.utc)
    today = now.date()

    src = Source(
        key="q4gated",
        issuer="Q4 Gated Corp",
        ticker="Q4GT",
        name="Q4 Gated Corp",
        exchange="XNAS",
        calendar_url="https://investor.q4gated.example.com/events-and-presentations/default.aspx",
        fetch=FetchStrategy.WEBSEARCH,
        period_scheme=PeriodScheme.CALENDAR_Q,
        fy_end_month=12,
        sec_cik=900001,
    )
    catalogue.put_source(src)
    catalogue.commit_issuer(
        "q4gated",
        [
            Event(
                id="earnings:q4gated:2026Q3",
                issuer_key="q4gated",
                ticker="Q4GT",
                type=EventType.EARNINGS,
                period="2026Q3",
                date=today - timedelta(days=80),
                status=EventStatus.PAST,
                source_url=src.calendar_url,
                evidence="Q3 Earnings",
                first_seen=today - timedelta(days=88),
                last_verified=today - timedelta(days=80),
            ),
            Event(
                id="earnings:q4gated:2026Q4",
                issuer_key="q4gated",
                ticker="Q4GT",
                type=EventType.EARNINGS,
                period="2026Q4",
                date=today + timedelta(days=11),
                status=EventStatus.EXPECTED,
                source_url=src.calendar_url,
                evidence="Estimated from issuer historical reporting cadence (unconfirmed)",
                first_seen=today,
                last_verified=today,
            ),
        ],
        run_id="seed-q4gt",
    )
    svc.watch("Q4GT", "XNAS")

    q4_body_v1 = json.dumps(
        {
            "GetEventListResult": [
                {
                    "Title": "Q3 2026 Earnings",
                    "StartDate": (today - timedelta(days=80)).strftime("%m/%d/%Y 16:00:00"),
                }
            ]
        }
    ).encode()
    transport_v1 = _StubTransport(
        {
            "https://investor.q4gated.example.com/feed/Event.svc/GetEventList": (200, q4_body_v1),
            "https://investor.q4gated.example.com/feed/PressRelease.svc/GetPressReleaseList": (
                200,
                b'{"GetPressReleaseListResult": []}',
            ),
        }
    )
    docs_v1, hist_v1 = await discover_q4(transport_v1, src, today=today)
    hash_v1 = _layer1_fingerprint(docs_v1, hist_v1)
    assert hash_v1

    catalogue.set_state(
        "fetch_state",
        "q4gated",
        {
            "last_ok": now.isoformat(),
            "last_agent_audit": now.isoformat(),
            "consecutive_failures": 0,
            "layer1_hash": hash_v1,
            "ipo_checked_at": now.isoformat(),
        },
    )

    class _CountingRunner:
        def __init__(self) -> None:
            self.calls = 0

        async def run(self, task: AgentTask, **kwargs) -> AgentResult:
            self.calls += 1
            return AgentResult(ExtractResult(events=[], complete=True, page_relevant=True), "fake")

    runner = _CountingRunner()
    maint = Maintenance(settings, runner=runner, transport=transport_v1)
    res = await maint.sweep()
    assert res["exit"] == 0
    assert runner.calls == 0  # Unchanged Layer 1 hash skipped LLM completely!

    # Probe with unchanged feed -> 0 changed, 0 LLM calls.
    probe_same = await maint.probe_near_earnings()
    assert probe_same["checked"] == 1
    assert probe_same["changed"] == []
    assert runner.calls == 0

    # Now Q4 feed updates with a new announcement -> probe detects change & triggers targeted sweep!
    q4_body_v2 = json.dumps(
        {
            "GetEventListResult": [
                {
                    "Title": "Q4 2026 Earnings Announcement",
                    "StartDate": (today + timedelta(days=11)).strftime("%m/%d/%Y 16:00:00"),
                }
            ]
        }
    ).encode()
    transport_v2 = _StubTransport(
        {
            "https://investor.q4gated.example.com/feed/Event.svc/GetEventList": (200, q4_body_v2),
            "https://investor.q4gated.example.com/feed/PressRelease.svc/GetPressReleaseList": (
                200,
                b'{"GetPressReleaseListResult": []}',
            ),
        }
    )
    maint_v2 = Maintenance(settings, runner=runner, transport=transport_v2)
    probe_changed = await maint_v2.probe_near_earnings()
    assert probe_changed["changed"] == ["q4gated"]
    assert runner.calls == 1
