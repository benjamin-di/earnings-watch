"""Fail-first regression tests for v0.7.1 reliability, dedup, Q4/ASX discovery, and signal hygiene."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from earnings_watch.engine.reconcile import reconcile
from earnings_watch.engine.timing import merge_observation
from earnings_watch.engine.verify import verify_claims
from earnings_watch.llm.runner import AgentResult, AgentTask
from earnings_watch.maintenance import Maintenance
from earnings_watch.model.events import (
    Event,
    EventStatus,
    EventType,
    ExtractClaim,
    TimingCandidate,
    TimingEvidence,
    TimingKind,
)
from earnings_watch.model.sources import (
    FetchStrategy,
    PeriodScheme,
    Source,
    canonical_period,
    event_id,
)
from earnings_watch.providers.q4_ir import is_q4_source
from earnings_watch.settings import Settings
from earnings_watch.store.database import Catalogue, PersonalStore, add_person, initialize


def _source(
    key: str = "acme",
    *,
    ticker: str = "ACME",
    exchange: str | None = "XNYS",
    fetch: FetchStrategy = FetchStrategy.PLAIN,
    calendar_url: str = "https://ir.example.com/events-and-presentations",
    period_scheme: PeriodScheme = PeriodScheme.CALENDAR_Q,
    fy_end_month: int = 12,
    hkex_stock_id: str | None = None,
) -> Source:
    return Source(
        key=key,
        issuer=f"{ticker} Corp",
        ticker=ticker,
        name=f"{ticker} Corp",
        exchange=exchange,
        calendar_url=calendar_url,
        fetch=fetch,
        period_scheme=period_scheme,
        fy_end_month=fy_end_month,
        hkex_stock_id=hkex_stock_id,
    )


def _event(
    *,
    issuer_key: str = "acme",
    ticker: str = "ACME",
    event_type: EventType = EventType.EARNINGS,
    period: str = "2026Q3",
    event_date: date = date(2026, 10, 29),
    status: EventStatus = EventStatus.PAST,
    source_url: str = "https://ir.example.com/calendar",
    evidence: str = "October 29, 2026 Q3 2026 results",
    last_verified: date = date(2026, 11, 1),
    timing_issue: str | None = None,
    timing_disputes: list[TimingEvidence] | None = None,
) -> Event:
    return Event(
        id=event_id(event_type, issuer_key, period),
        issuer_key=issuer_key,
        ticker=ticker,
        type=event_type,
        period=period,
        date=event_date,
        status=status,
        source_url=source_url,
        evidence=evidence,
        first_seen=date(2026, 10, 1),
        last_verified=last_verified,
        timing_issue=timing_issue,
        timing_disputes=timing_disputes or [],
    )


def test_hkex_stock_id_accepts_ten_digit_ipo_ids():
    src = _source(
        key="hk-issuer",
        ticker="09999",
        exchange="XHKG",
        calendar_url="https://www1.hkexnews.hk/search/titlesearch.xhtml?lang=en",
        period_scheme=PeriodScheme.HALF_YEAR,
        hkex_stock_id="1234567890",
    )
    assert src.hkex_stock_id == "1234567890"


def test_agm_canonical_period_uses_meeting_date_year():
    src = _source(key="acme", ticker="ACME")
    assert canonical_period("2026", src, EventType.AGM, date(2027, 1, 14)) == "2027"
    assert (
        canonical_period("Annual General Meeting 2026", src, EventType.AGM, date(2027, 4, 21))
        == "2027"
    )
    assert canonical_period("2026 EGM", src, EventType.AGM, date(2027, 2, 10)) == "2027-egm"


def test_reconcile_and_store_dedupe_same_day_dividend_aliases(tmp_path: Path):
    now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
    old_generic = _event(
        event_type=EventType.DIVIDEND,
        period="2026-dividend",
        event_date=date(2026, 9, 21),
        status=EventStatus.PAST,
        evidence="21 September 2026 Dividend payment",
        last_verified=date(2026, 10, 1),
    )
    new_interim = _event(
        event_type=EventType.DIVIDEND,
        period="2026-interim-dividend",
        event_date=date(2026, 9, 21),
        status=EventStatus.PAST,
        evidence="21 September 2026 Interim dividend payment",
        last_verified=date(2026, 11, 27),
    )
    merged, _ = reconcile(
        [old_generic],
        [new_interim],
        authoritative_sources=set(),
        active_source_keys={"acme"},
        always_source_keys=set(),
        removal_overrides=set(),
        seed_mode=False,
        now=now,
    )
    divs = [e for e in merged if e.type == EventType.DIVIDEND and e.date == date(2026, 9, 21)]
    assert len(divs) == 1
    assert divs[0].period == "2026-interim-dividend"

    # Also verify non-authoritative commit_issuer deletes the superseded duplicate from SQLite
    settings = Settings(data_dir=tmp_path, max_calls_per_run=5)
    db_path = settings.db
    initialize(db_path)
    cat = Catalogue(db_path)
    cat.put_source(_source())
    cat.commit_issuer("acme", [old_generic], run_id="r1", authoritative=False)
    cat.commit_issuer("acme", merged, run_id="r2", authoritative=False)
    stored = [e for e in cat.events() if e["issuer_key"] == "acme" and e["type"] == "dividend"]
    assert len(stored) == 1
    assert stored[0]["period"] == "2026-interim-dividend"


def test_verify_claims_suppresses_unverified_timing_candidate_on_past_events():
    src = _source()
    page_text = "October 28, 2026 Q3 2026 Financial Results Conference Call"
    claim = ExtractClaim(
        type=EventType.EARNINGS,
        period="2026Q3",
        date=date(2026, 10, 28),
        evidence="October 28, 2026 Q3 2026 Financial Results Conference Call",
        source_url="https://ir.example.com/events-and-presentations",
        timing_candidates=[
            TimingCandidate(
                release_time="16:00",
                release_tz="America/New_York",
                source_url="https://ir.example.com/unfetched-pr",
                evidence="4:00 PM ET",
            )
        ],
    )
    res = verify_claims(
        [claim],
        src,
        page_text,
        today=date(2026, 11, 27),
        documents={"https://ir.example.com/events-and-presentations": page_text},
    )
    assert len(res.events) == 1
    assert res.rejected == ()


def test_merge_observation_clears_stale_row_disagreement_on_clean_reconfirmation():
    dispute = TimingEvidence(
        kind=TimingKind.PUBLICATION,
        release_time="07:00",
        release_tz="Europe/Vienna",
        source_url="https://ir.example.com/calendar",
        evidence="February 18 Preliminary FY 2026 results",
        verified_on=date(2026, 11, 1),
    )
    old = _event(
        event_type=EventType.EARNINGS,
        period="2026Q4",
        event_date=date(2027, 2, 18),
        status=EventStatus.IR_CONFIRMED,
        timing_issue="Current source rows disagree; review required",
        timing_disputes=[dispute],
    )
    incoming = _event(
        event_type=EventType.EARNINGS,
        period="2026Q4",
        event_date=date(2027, 2, 18),
        status=EventStatus.IR_CONFIRMED,
        last_verified=date(2026, 11, 27),
        timing_issue=None,
        timing_disputes=[],
    )
    merged = merge_observation(old, incoming)
    assert merged.timing_issue is None
    assert merged.timing_disputes == []


def test_is_q4_source_allows_websearch_issuer_ir_without_default_aspx():
    src = _source(
        fetch=FetchStrategy.WEBSEARCH,
        calendar_url="https://investors.example.com/events-and-presentations",
    )
    assert is_q4_source(src) is True


@pytest.mark.asyncio
async def test_maintenance_projects_cadence_fallback_when_sweep_raises(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, max_calls_per_run=5)
    db_path = settings.db
    initialize(db_path)
    cat = Catalogue(db_path)
    src = _source(key="acme", ticker="ACME", fetch=FetchStrategy.WEBSEARCH)
    cat.put_source(src)
    add_person(db_path, "owner")
    PersonalStore(db_path, "owner").watch("ACME", "XNYS")
    cat.commit_issuer(
        "acme",
        [
            _event(period="2026Q2", event_date=date(2026, 7, 29), status=EventStatus.PAST),
            _event(period="2026Q3", event_date=date(2026, 10, 29), status=EventStatus.PAST),
        ],
        run_id="seed",
        authoritative=False,
    )

    class FailingRunner:
        async def run(self, task: AgentTask, **kwargs) -> AgentResult:
            raise RuntimeError("model run timed out")

    class OfflineTransport:
        async def get(self, url: str, **kwargs):
            raise RuntimeError("offline")

    maint = Maintenance(settings, runner=FailingRunner(), transport=OfflineTransport())
    result = await maint.sweep(force=True)

    assert any(e["source"] == "acme" and e["reason"] == "RuntimeError" for e in result["errors"])
    future = [
        e
        for e in cat.events()
        if e["issuer_key"] == "acme" and e["date"] >= date.today().isoformat()
    ]
    assert len(future) >= 1
    assert future[0]["status"] == "expected"
    state = cat.state("fetch_state", "acme")
    assert state["error"] == "RuntimeError"
    assert "timed out" in state.get("error_detail", "")


@pytest.mark.asyncio
async def test_asx_adapter_parses_markitdigital_announcements():
    from earnings_watch.providers.asx import discover_asx

    class FakeResp:
        status = 200
        body = json.dumps(
            {
                "data": {
                    "displayName": "EXAMPLE MINING LIMITED",
                    "items": [
                        {
                            "headline": "Half Year Results and Appendix 4D",
                            "date": "2026-08-20T00:15:00.000Z",
                            "announcementType": "PERIODIC REPORTS",
                            "documentKey": "2924-03000001-6A1200001",
                        },
                        {
                            "headline": "Notice of Annual General Meeting",
                            "date": "2026-10-15T01:00:00.000Z",
                            "announcementType": "ANNUAL GENERAL MEETING",
                            "documentKey": "2924-03000002-6A1200002",
                        },
                    ],
                }
            }
        ).encode()

    class FakeTransport:
        async def get(self, url: str):
            assert "asx.api.markitdigital.com" in url
            assert "/companies/EXM/announcements" in url
            return FakeResp()

    src = _source(
        key="example-asx",
        ticker="EXM",
        exchange="XASX",
        period_scheme=PeriodScheme.HALF_YEAR,
        fy_end_month=6,
        calendar_url="https://www.example.com/investors",
    )
    docs, hist = await discover_asx(FakeTransport(), src, today=date(2026, 11, 27))
    assert date(2026, 8, 20) in hist
    assert len(docs) == 1
    text = next(iter(docs.values()))
    assert "Half Year Results and Appendix 4D" in text
    assert "Notice of Annual General Meeting" in text


def test_same_day_dedupe_preserves_distinct_subtypes_and_keeps_diff_consistent(tmp_path: Path):
    now = datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc)
    agm = _event(
        event_type=EventType.AGM,
        period="2027",
        event_date=date(2027, 5, 20),
        status=EventStatus.IR_CONFIRMED,
        evidence="20 May 2027 Annual General Meeting",
    )
    egm = _event(
        event_type=EventType.AGM,
        period="2027-egm",
        event_date=date(2027, 5, 20),
        status=EventStatus.IR_CONFIRMED,
        evidence="20 May 2027 Extraordinary General Meeting",
    )
    reg_div = _event(
        event_type=EventType.DIVIDEND,
        period="2026-interim-dividend",
        event_date=date(2026, 12, 10),
        status=EventStatus.IR_CONFIRMED,
        evidence="10 December 2026 Interim dividend",
    )
    spec_div = _event(
        event_type=EventType.DIVIDEND,
        period="2026-special-dividend",
        event_date=date(2026, 12, 10),
        status=EventStatus.IR_CONFIRMED,
        evidence="10 December 2026 Special dividend",
    )
    merged, diff = reconcile(
        [agm, reg_div],
        [egm, spec_div],
        authoritative_sources=set(),
        active_source_keys={"acme"},
        always_source_keys=set(),
        removal_overrides=set(),
        seed_mode=False,
        now=now,
    )
    ids = {e.id for e in merged}
    assert {agm.id, egm.id, reg_div.id, spec_div.id}.issubset(ids)
    assert diff.removed == []

    # Non-authoritative commit with only EGM must NOT delete stored AGM
    settings = Settings(data_dir=tmp_path)
    initialize(settings.db)
    cat = Catalogue(settings.db)
    cat.put_source(_source())
    cat.commit_issuer("acme", [agm], run_id="r1", authoritative=False)
    cat.commit_issuer("acme", [egm], run_id="r2", authoritative=False)
    stored_ids = {e["id"] for e in cat.events() if e["issuer_key"] == "acme"}
    assert {agm.id, egm.id}.issubset(stored_ids)


def test_merge_observation_keeps_multi_source_row_disagreement():
    other_dispute = TimingEvidence(
        kind=TimingKind.PUBLICATION,
        release_time="08:00",
        release_tz="Europe/Vienna",
        source_url="https://ir.example.com/press-release",
        evidence="February 18 Preliminary FY 2026 results at 08:00",
        verified_on=date(2026, 11, 1),
    )
    old = _event(
        event_type=EventType.EARNINGS,
        period="2026Q4",
        event_date=date(2027, 2, 18),
        status=EventStatus.IR_CONFIRMED,
        timing_issue="Current source rows disagree; review required",
        timing_disputes=[other_dispute],
    )
    incoming = _event(
        event_type=EventType.EARNINGS,
        period="2026Q4",
        event_date=date(2027, 2, 18),
        status=EventStatus.IR_CONFIRMED,
        last_verified=date(2026, 11, 27),
    )
    merged = merge_observation(old, incoming)
    assert merged.timing_issue == "Current source rows disagree; review required"


def test_markitdigital_is_shared_platform_and_asx_uses_sydney_date_and_rejects_bad_ticker():
    from earnings_watch.model.urls import apex_domain
    from earnings_watch.providers.asx import _parse_iso_date, discover_asx

    assert apex_domain("asx.api.markitdigital.com") == ""
    # 22:30Z on Aug 19 is 08:30 AEST on Aug 20 in Australia/Sydney
    assert _parse_iso_date("2026-08-19T22:30:00.000Z") == date(2026, 8, 20)

    import asyncio

    class NeverTransport:
        async def get(self, url: str):
            raise AssertionError("should not fetch invalid ticker")

    bad_src = _source(key="bad-asx", ticker="BAD-TICKER-LONG", exchange="XASX")
    docs, hist = asyncio.run(discover_asx(NeverTransport(), bad_src, today=date(2026, 11, 27)))
    assert docs == {} and hist == []


@pytest.mark.asyncio
async def test_q4_short_circuits_on_non_list_json_and_rejects_exchange_hosts():
    from earnings_watch.providers.q4_ir import discover_q4

    ex_src = _source(
        fetch=FetchStrategy.WEBSEARCH,
        calendar_url="https://www.sec.gov/events-and-presentations",
    )
    assert is_q4_source(ex_src) is False

    calls: list[str] = []

    class FakeResp:
        status = 200
        body = b'{"error": "not found"}'

    class CountingTransport:
        async def get(self, url: str):
            calls.append(url)
            return FakeResp()

    src = _source(
        fetch=FetchStrategy.WEBSEARCH,
        calendar_url="https://investors.example.com/events-and-presentations",
    )
    docs, hist = await discover_q4(CountingTransport(), src, today=date(2026, 11, 27))
    assert docs == {} and hist == []
    assert len(calls) == 2
    assert all("PressRelease.svc" not in u for u in calls)


@pytest.mark.asyncio
async def test_maintenance_redacts_validation_and_secret_errors_and_preserves_state(tmp_path: Path):
    from earnings_watch.model.events import ExtractResult

    settings = Settings(data_dir=tmp_path, max_calls_per_run=5)
    initialize(settings.db)
    cat = Catalogue(settings.db)
    src = _source(key="acme", ticker="ACME", fetch=FetchStrategy.WEBSEARCH)
    cat.put_source(src)
    add_person(settings.db, "owner")
    PersonalStore(settings.db, "owner").watch("ACME", "XNYS")
    cat.set_state(
        "fetch_state",
        "acme",
        {"last_ok": "2026-11-26T00:00:00+00:00", "last_agent_audit": "2026-11-26T00:00:00+00:00"},
    )

    class BadValidationRunner:
        async def run(self, task: AgentTask, **kwargs) -> AgentResult:
            ExtractResult.model_validate({"untrusted_field": "UNTRUSTED_PAYLOAD_MARKER_123"})
            return AgentResult(parsed=None)

    class OfflineTransport:
        async def get(self, url: str, **kwargs):
            raise RuntimeError("offline")

    maint = Maintenance(settings, runner=BadValidationRunner(), transport=OfflineTransport())
    result = await maint.sweep(force=True)
    err = next(e for e in result["errors"] if e["source"] == "acme")
    assert err["detail"] == "schema validation failed"
    assert "UNTRUSTED_PAYLOAD_MARKER_123" not in json.dumps(result)
    state = cat.state("fetch_state", "acme")
    assert state["error_detail"] == "schema validation failed"
    assert state["last_ok"] == "2026-11-26T00:00:00+00:00"
    assert state["last_agent_audit"] == "2026-11-26T00:00:00+00:00"
