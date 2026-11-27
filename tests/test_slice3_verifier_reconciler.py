"""Regression tests for Slice 3: verifier, reconciler, timing, period, and URL hardening."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
import socket
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from conftest import event
from earnings_watch import open as open_service
from earnings_watch.engine import reconcile, timing, timing_search, verify
from earnings_watch.llm.runner import AgentResult
from earnings_watch.maintenance import Maintenance
from earnings_watch.model.events import (
    Event,
    EventStatus,
    EventType,
    ExtractClaim,
    ExtractResult,
    ResultsLink,
    ResultsLinkKind,
    TimingEvidence,
    TimingKind,
)
from earnings_watch.model.sources import (
    FetchStrategy,
    PeriodScheme,
    Source,
    canonical_period,
    period_consistent,
)
from earnings_watch.model.urls import public_url
from earnings_watch.store.database import Catalogue, connect
from earnings_watch.transport import Transport


def _transport(body: str) -> Transport:
    return Transport(
        {"issuer.example"},
        resolver=lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
        mock=httpx.MockTransport(
            lambda r: httpx.Response(200, text=body, headers={"content-type": "text/html"})
        ),
    )


TODAY = date(2026, 4, 1)
NOW = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)


def _source(
    *,
    key: str = "alpha",
    ticker: str = "ALPHA",
    exchange: str | None = "XLON",
    period_scheme: PeriodScheme = PeriodScheme.CALENDAR_Q,
    fy_end_month: int = 12,
    calendar_url: str = "https://issuer.example/calendar",
) -> Source:
    return Source(
        key=key,
        issuer="Alpha Corp",
        ticker=ticker,
        name="Alpha Corp",
        exchange=exchange,
        calendar_url=calendar_url,
        fetch=FetchStrategy.PLAIN,
        period_scheme=period_scheme,
        fy_end_month=fy_end_month,
    )


# --- 1. Anchored evidence_verifies -----------------------------------------


def test_evidence_verifies_requires_alphanumeric_boundaries() -> None:
    page = "Financial calendar: Q1 2026 results on 14 May 2026."
    assert verify.evidence_verifies(date(2026, 5, 14), "14 May 2026", page)
    assert not verify.evidence_verifies(date(2026, 5, 4), "4 May 2026", page)

    md_page = "Calendar: [Q1 results on 14 May 2026](https://issuer.example/cal)"
    assert verify.evidence_verifies(date(2026, 5, 14), "14 May 2026", md_page)
    assert not verify.evidence_verifies(date(2026, 5, 4), "4 May 2026", md_page)

    loose_page = "Results — 14/May/2026 — confirmed"
    assert not verify.evidence_verifies(date(2026, 5, 4), "4 May 2026", loose_page)

    suffix_page = "Results on May 1, 20260."
    assert not verify.evidence_verifies(date(2026, 5, 1), "May 1, 2026", suffix_page)


# --- 2. Date parsing in dates_in_evidence ----------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Results on May 14 2026.", date(2026, 5, 14)),
        ("Results on Jan 14,2026.", date(2026, 1, 14)),
        ("Results on 14 May, 2026.", date(2026, 5, 14)),
        ("Results on 14 May,2026.", date(2026, 5, 14)),
    ],
)
def test_dates_in_evidence_optional_comma_variants(text: str, expected: date) -> None:
    assert expected in verify.dates_in_evidence(text)


def test_dates_in_evidence_slash_dates_us_and_non_us() -> None:
    # Unambiguous when one side > 12 regardless of us_dates
    assert date(2026, 5, 14) in verify.dates_in_evidence("05/14/2026", us_dates=False)
    assert date(2026, 5, 14) in verify.dates_in_evidence("05/14/2026", us_dates=True)
    assert date(2026, 5, 14) in verify.dates_in_evidence("14/05/2026", us_dates=False)
    assert date(2026, 5, 14) in verify.dates_in_evidence("14/05/2026", us_dates=True)

    # Ambiguous: US uses %m/%d/%Y, non-US uses %d/%m/%Y
    assert verify.dates_in_evidence("05/04/2026", us_dates=True) == {date(2026, 5, 4)}
    assert verify.dates_in_evidence("05/04/2026", us_dates=False) == {date(2026, 4, 5)}


def test_verify_claims_infers_us_dates_from_exchange() -> None:
    page = "Q1 2026 results on 05/04/2026."
    us_claim = ExtractClaim(
        type=EventType.EARNINGS, period="Q1 2026", date=date(2026, 5, 4), evidence=page
    )
    uk_claim = ExtractClaim(
        type=EventType.EARNINGS, period="Q1 2026", date=date(2026, 4, 5), evidence=page
    )
    us_src = _source(exchange="XNAS")
    uk_src = _source(exchange="XLON")

    assert verify.verify_claims([us_claim], us_src, page, today=TODAY).events
    assert not verify.verify_claims([uk_claim], us_src, page, today=TODAY).events

    assert verify.verify_claims([uk_claim], uk_src, page, today=TODAY).events
    assert not verify.verify_claims([us_claim], uk_src, page, today=TODAY).events


# --- 3. Maintenance.sweep wires timing_candidate_checks --------------------


async def test_maintenance_sweep_wires_timing_candidate_checks(seeded) -> None:
    open_service(seeded).watch("ALPHA")
    catalogue = Catalogue(seeded.db)
    day = date.today() + timedelta(days=30)
    cal_url = "https://issuer.example/calendar"
    stale_quote = f"Q4 2026 results will be published on {day.strftime('%d %B %Y')} at 08:00 UTC."
    other_quote = f"Q4 2026 results will be published on {day.strftime('%d %B %Y')} at 09:00 UTC."

    cand_a = TimingEvidence(
        kind=TimingKind.PUBLICATION,
        release_time="08:00",
        release_tz="UTC",
        source_url=cal_url,
        evidence=stale_quote,
        verified_on=date.today(),
    )
    cand_b = TimingEvidence(
        kind=TimingKind.PUBLICATION,
        release_time="09:00",
        release_tz="UTC",
        source_url="https://issuer.example/other",
        evidence=other_quote,
        verified_on=date.today(),
    )
    fp_a = timing.timing_candidate_fingerprint(day, cand_a)

    disputed = event(
        period="2026Q4",
        id="earnings:alpha:2026Q4",
        date=day,
        source_url=cal_url,
        evidence=stale_quote,
        timing=cand_a,
        timing_issue="Conflicting source timings; review required",
        timing_disputes=[cand_a, cand_b],
    )
    catalogue.commit_issuer("alpha", [disputed], run_id="seed-disputed")
    with connect(seeded.db, write=True) as db:
        db.execute(
            "INSERT INTO overrides(event_id, kind, doc) VALUES(?, ?, ?)",
            (
                disputed.id,
                "timing_selection",
                json.dumps(
                    {
                        "select_candidate": fp_a,
                        "reviewed_on": date.today().isoformat(),
                        "reason": "Verified on calendar",
                    }
                ),
            ),
        )

    runner = AsyncMock()
    page_with_time = stale_quote
    page_without_time = f"Q4 2026 results will be published on {day.strftime('%d %B %Y')}."

    async def run_task_with_time(task, **kwargs):
        if task.schema is ExtractResult:
            return AgentResult(
                ExtractResult(
                    page_relevant=True,
                    complete=True,
                    events=[
                        ExtractClaim(
                            type=EventType.EARNINGS,
                            period="Q4 2026",
                            date=day,
                            release_time="08:00",
                            release_tz="UTC",
                            evidence=page_with_time,
                            source_url=cal_url,
                        )
                    ],
                ),
                "fake",
            )
        return AgentResult(task.schema.model_validate({"documents": []}), "fake")

    runner.run.side_effect = run_task_with_time
    maint_ok = Maintenance(seeded, runner=runner, transport=_transport(page_with_time))
    res_ok = await maint_ok.sweep(force=True)
    assert res_ok["exit"] == 0
    stored_ok = catalogue.events()[0]
    assert stored_ok["release_time"] == "08:00"
    assert stored_ok["timing_resolution"] is not None

    async def run_task_without_time(task, **kwargs):
        if task.schema is ExtractResult:
            return AgentResult(
                ExtractResult(
                    page_relevant=True,
                    complete=True,
                    events=[
                        ExtractClaim(
                            type=EventType.EARNINGS,
                            period="Q4 2026",
                            date=day,
                            evidence=page_without_time,
                            source_url=cal_url,
                        )
                    ],
                ),
                "fake",
            )
        return AgentResult(task.schema.model_validate({"documents": []}), "fake")

    runner.run.side_effect = run_task_without_time
    maint = Maintenance(seeded, runner=runner, transport=_transport(page_without_time))
    res = await maint.sweep(force=True)
    assert res["exit"] == 0
    stored = catalogue.events()[0]
    assert stored["release_time"] is None
    assert stored["timing_resolution"] is None
    assert stored["timing_issue"] == "Reviewed timing selection needs current verified evidence"


# --- 4. Circuit-breaker & results_from_calls -------------------------------


def test_mass_removal_circuit_breaker_stays_held_without_overrides() -> None:
    existing = [
        event(id=f"earnings:alpha:2026Q{n}", period=f"2026Q{n}", date=date(2026, 6, n))
        for n in (1, 2, 3)
    ]
    held1, diff1 = reconcile.reconcile(
        existing,
        [],
        authoritative_sources={"alpha"},
        active_source_keys={"alpha"},
        always_source_keys=set(),
        removal_overrides=set(),
        seed_mode=False,
        now=NOW,
    )
    assert len(held1) == 3
    assert len(diff1.held) == 3
    assert not diff1.removed

    # Second run without removal_overrides must keep all 3 held (no auto-removal)
    held2, diff2 = reconcile.reconcile(
        held1,
        [],
        authoritative_sources={"alpha"},
        active_source_keys={"alpha"},
        always_source_keys=set(),
        removal_overrides=set(),
        seed_mode=False,
        now=NOW + timedelta(hours=1),
    )
    assert len(held2) == 3
    assert not diff2.removed
    assert not diff2.held

    # Explicit removal_overrides clears them
    cleared, diff3 = reconcile.reconcile(
        held2,
        [],
        authoritative_sources={"alpha"},
        active_source_keys={"alpha"},
        always_source_keys=set(),
        removal_overrides={e.id for e in existing},
        seed_mode=False,
        now=NOW + timedelta(hours=2),
    )
    assert cleared == []
    assert len(diff3.removed) == 3


def test_results_from_calls_retains_existing_call_only_results_without_oscillation() -> None:
    call = event(
        id="earnings-call:alpha:2026Q4",
        type=EventType.EARNINGS_CALL,
        period="2026Q4",
        date=date(2026, 11, 5),
    )
    derived1 = reconcile.results_from_calls([call], [], today=TODAY)
    assert len(derived1) == 1
    assert derived1[0].timing_issue == timing.CALL_ONLY_RESULTS

    # Subsequent run with existing call-only results must still emit it so authoritative reconcile retains it
    derived2 = reconcile.results_from_calls([call], [call, derived1[0]], today=TODAY)
    assert len(derived2) == 1
    assert derived2[0].id == derived1[0].id
    assert derived2[0].timing_issue == timing.CALL_ONLY_RESULTS

    merged, diff = reconcile.reconcile(
        [call, derived1[0]],
        [call, *derived2],
        authoritative_sources={"alpha"},
        active_source_keys={"alpha"},
        always_source_keys=set(),
        removal_overrides=set(),
        seed_mode=False,
        now=NOW,
    )
    assert {e.id for e in merged} == {call.id, derived1[0].id}
    assert not diff.removed
    assert not diff.added


# --- 5. Same-run >=3-way timing_disputes preservation ----------------------


def test_verify_claims_preserves_three_way_same_run_timing_disputes_and_clears_clock() -> None:
    src = _source()
    q1 = "Q4 2026 results will be published on 5 November 2026 at 08:00 UTC."
    q2 = "Q4 2026 results will be published on 5 November 2026 at 09:00 UTC."
    q3 = "Q4 2026 results will be published on 5 November 2026 at 10:00 UTC."
    u1, u2, u3 = (
        src.calendar_url,
        "https://issuer.example/notice-2",
        "https://issuer.example/notice-3",
    )
    claims = [
        ExtractClaim(
            type=EventType.EARNINGS,
            period="Q4 2026",
            date=date(2026, 11, 5),
            release_time="08:00",
            release_tz="UTC",
            evidence=q1,
            source_url=u1,
        ),
        ExtractClaim(
            type=EventType.EARNINGS,
            period="Q4 2026",
            date=date(2026, 11, 5),
            release_time="09:00",
            release_tz="UTC",
            evidence=q2,
            source_url=u2,
        ),
        ExtractClaim(
            type=EventType.EARNINGS,
            period="Q4 2026",
            date=date(2026, 11, 5),
            release_time="10:00",
            release_tz="UTC",
            evidence=q3,
            source_url=u3,
        ),
    ]
    res = verify.verify_claims(claims, src, q1, today=TODAY, documents={u1: q1, u2: q2, u3: q3})
    assert not res.rejected
    assert len(res.events) == 1
    ev = res.events[0]
    assert ev.release_time is None
    assert ev.release_tz is None
    assert ev.timing_issue
    assert {d.release_time for d in ev.timing_disputes} == {"08:00", "09:00", "10:00"}


def test_verify_claims_two_way_clock_conflict_clears_release_time() -> None:
    src = _source()
    q1 = "Q4 2026 results will be published on 5 November 2026 at 08:00 UTC."
    q2 = "Q4 2026 results will be published on 5 November 2026 at 09:00 UTC."
    u1, u2 = src.calendar_url, "https://issuer.example/notice-2"
    claims = [
        ExtractClaim(
            type=EventType.EARNINGS,
            period="Q4 2026",
            date=date(2026, 11, 5),
            release_time="08:00",
            release_tz="UTC",
            evidence=q1,
            source_url=u1,
        ),
        ExtractClaim(
            type=EventType.EARNINGS,
            period="Q4 2026",
            date=date(2026, 11, 5),
            release_time="09:00",
            release_tz="UTC",
            evidence=q2,
            source_url=u2,
        ),
    ]
    res = verify.verify_claims(claims, src, q1, today=TODAY, documents={u1: q1, u2: q2})
    ev = res.events[0]
    assert ev.release_time is None
    assert ev.release_tz is None
    assert ev.timing_issue
    assert len(ev.timing_disputes) == 2


# --- 6. period_consistent & _AS_AT_RE --------------------------------------


def test_period_consistent_applies_half_quarter_equivalence() -> None:
    cal = _source(period_scheme=PeriodScheme.CALENDAR_Q, fy_end_month=12)
    half = _source(period_scheme=PeriodScheme.HALF_YEAR, fy_end_month=12)
    fiscal = _source(period_scheme=PeriodScheme.FISCAL_Q, fy_end_month=9)

    assert period_consistent("H1 2026", cal, "2026Q2")
    assert period_consistent("H2 2026", cal, "2026Q4")
    assert period_consistent("2026 Half-Year Report", cal, "2026Q2")
    assert period_consistent("Q2 2026", half, "2026H1")
    assert period_consistent("Q4 2026", half, "2026H2")
    assert period_consistent("H1 2026", fiscal, "2026FQ2")
    assert period_consistent("H2 2026", fiscal, "2026FQ4")
    assert not period_consistent("H1 2026", cal, "2026Q1")
    assert not period_consistent("H1 2026", cal, "2025Q2")


def test_as_at_december_fy_validates_day_and_emits_q2_for_quarterly() -> None:
    cal = _source(period_scheme=PeriodScheme.CALENDAR_Q, fy_end_month=12)
    fiscal_dec = _source(period_scheme=PeriodScheme.FISCAL_Q, fy_end_month=12)
    half_dec = _source(period_scheme=PeriodScheme.HALF_YEAR, fy_end_month=12)
    day = date(2026, 8, 20)

    assert (
        canonical_period("half-yearly report as at 30 June 2026", cal, EventType.EARNINGS, day)
        == "2026Q2"
    )
    assert (
        canonical_period(
            "half-yearly report as at 30 June 2026", fiscal_dec, EventType.EARNINGS, day
        )
        == "2026FQ2"
    )
    assert (
        canonical_period("half-yearly report as at 30 June 2026", half_dec, EventType.EARNINGS, day)
        == "2026H1"
    )
    with pytest.raises(ValueError):
        canonical_period("half-yearly report as at 15 June 2026", cal, EventType.EARNINGS, day)


# --- 7. with_legacy_evidence & timezone_supported --------------------------


def test_with_legacy_evidence_preserves_past_status_for_call_only() -> None:
    past_ev = event(
        date=date(2026, 2, 1),
        status=EventStatus.PAST,
        evidence="Q4 2025 earnings conference call on 1 February 2026",
    )
    normalized = timing.with_legacy_evidence(past_ev)
    assert normalized.status == EventStatus.PAST
    assert normalized.timing_issue == timing.CALL_ONLY_RESULTS


def test_timezone_supported_rejects_utc_and_gmt_offsets() -> None:
    utc_offset_claim = ExtractClaim(
        type=EventType.EARNINGS,
        period="Q1 2026",
        date=date(2026, 5, 14),
        release_time="08:00",
        release_tz="UTC",
        evidence="Q1 2026 results on 14 May 2026 at 08:00 UTC+8.",
    )
    assert not timing_search.timezone_supported(utc_offset_claim, utc_offset_claim.evidence)

    gmt_offset_claim = ExtractClaim(
        type=EventType.EARNINGS,
        period="Q4 2025",
        date=date(2026, 1, 15),
        release_time="08:00",
        release_tz="Europe/London",
        evidence="Q4 2025 results on 15 January 2026 at 08:00 GMT+2.",
    )
    assert not timing_search.timezone_supported(gmt_offset_claim, gmt_offset_claim.evidence)

    page_offset_claim = ExtractClaim(
        type=EventType.EARNINGS,
        period="Q1 2026",
        date=date(2026, 5, 14),
        release_time="08:00",
        release_tz="UTC",
        evidence="Q1 2026 results on 14 May 2026 at 08:00.",
    )
    assert not timing_search.timezone_supported(page_offset_claim, "All times are in UTC+8.")

    bare_utc_claim = ExtractClaim(
        type=EventType.EARNINGS,
        period="Q1 2026",
        date=date(2026, 5, 14),
        release_time="08:00",
        release_tz="UTC",
        evidence="Q1 2026 results on 14 May 2026 at 08:00 UTC.",
    )
    assert timing_search.timezone_supported(bare_utc_claim, bare_utc_claim.evidence)


# --- 8. URL validation hardening -------------------------------------------


_MULTICAST_V4 = f"https://{'.'.join(('224', '0', '0', '1'))}/calendar"


@pytest.mark.parametrize(
    "bad_url",
    [
        "https://@example.com/calendar",
        "https://:@example.com/calendar",
        _MULTICAST_V4,
        "https://[ff02::1]/calendar",
    ],
)
def test_public_url_rejects_empty_userinfo_and_multicast(bad_url: str) -> None:
    with pytest.raises(ValueError):
        public_url(bad_url)


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://issuer.example/calendar",
        "https://127.0.0.1/calendar",
        _MULTICAST_V4,
        "https://@issuer.example/calendar",
    ],
)
def test_event_source_url_and_results_link_validated_by_public_url(bad_url: str) -> None:
    with pytest.raises(ValidationError):
        Event(
            id="earnings:alpha:2026Q4",
            issuer_key="alpha",
            ticker="ALPHA",
            type=EventType.EARNINGS,
            period="2026Q4",
            date=date(2026, 11, 5),
            status=EventStatus.IR_CONFIRMED,
            source_url=bad_url,
            evidence="Q4 2026 results on 5 November 2026.",
            first_seen=TODAY,
            last_verified=TODAY,
        )
    with pytest.raises(ValidationError):
        ResultsLink(
            title="Q4 2026 Press Release",
            kind=ResultsLinkKind.PRESS_RELEASE,
            url=bad_url,
        )
