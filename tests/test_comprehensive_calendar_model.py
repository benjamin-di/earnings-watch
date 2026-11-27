from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import get_args

import pytest

from earnings_watch import calendar, ics
from earnings_watch.engine.timing import CALL_ONLY_RESULTS, kind_for
from earnings_watch.engine.verify import verify_claims
from earnings_watch.mcp_server import EventTypeLiteral
from earnings_watch.model.events import (
    DatePrecision,
    Event,
    EventStatus,
    EventType,
    ExtractClaim,
    TimingKind,
)
from earnings_watch.model.sources import (
    FetchStrategy,
    PeriodScheme,
    Source,
    canonical_period,
)
from earnings_watch.presentation import format_event_summary, present
from earnings_watch.store.database import (
    Catalogue,
    LibraryReader,
    PersonalStore,
    add_person,
    initialize,
)


def _source(key: str = "alpha", ticker: str = "ALPHA") -> Source:
    return Source(
        key=key,
        issuer=f"{ticker} Holdings AG",
        ticker=ticker,
        name=f"{ticker} Holdings",
        calendar_url=f"https://ir.{key}.example/calendar",
        fetch=FetchStrategy.PLAIN,
        period_scheme=PeriodScheme.CALENDAR_Q,
        fy_end_month=12,
    )


def test_event_types_and_date_precision_defaults() -> None:
    assert EventType.CONFERENCE == "conference"
    assert EventType.TRADING_UPDATE == "trading-update"
    assert set(DatePrecision) == {
        DatePrecision.EXACT,
        DatePrecision.WEEK_OF,
        DatePrecision.MONTH,
        DatePrecision.TENTATIVE,
    }
    claim = ExtractClaim(
        type=EventType.CONFERENCE,
        period="Autumn Investor Conference",
        date=date(2026, 10, 15),
        evidence="15 October 2026 | Autumn Investor Conference",
    )
    assert claim.date_precision == DatePrecision.EXACT
    event = Event(
        id="conference:alpha:2026-10-15-conference",
        issuer_key="alpha",
        ticker="ALPHA",
        type=EventType.CONFERENCE,
        period="2026-10-15-conference",
        date=date(2026, 10, 15),
        status=EventStatus.IR_CONFIRMED,
        source_url="https://ir.alpha.example/calendar",
        evidence="15 October 2026 | Autumn Investor Conference",
        first_seen=date(2026, 8, 1),
        last_verified=date(2026, 8, 1),
    )
    assert event.date_precision == DatePrecision.EXACT
    assert "conference" in get_args(EventTypeLiteral)
    assert "trading-update" in get_args(EventTypeLiteral)


@pytest.mark.parametrize(
    ("raw", "event_type", "event_date", "expected"),
    [
        (
            "Spring Financials Conference",
            EventType.CONFERENCE,
            date(2026, 3, 10),
            "2026-03-10-conference",
        ),
        (
            "Autumn European Conference",
            EventType.CONFERENCE,
            date(2026, 9, 22),
            "2026-09-22-conference",
        ),
        (
            "Q1 2026 Trading Update",
            EventType.TRADING_UPDATE,
            date(2026, 4, 21),
            "2026Q1-trading-update",
        ),
        (
            "Q3 2026 Trading Update",
            EventType.TRADING_UPDATE,
            date(2026, 10, 22),
            "2026Q3-trading-update",
        ),
        (
            "H1 2026 Trading Update",
            EventType.TRADING_UPDATE,
            date(2026, 7, 16),
            "2026H1-trading-update",
        ),
        (
            "Trading Update",
            EventType.TRADING_UPDATE,
            date(2026, 11, 5),
            "2026-11-05-trading-update",
        ),
        ("FY 2025 Dividend", EventType.DIVIDEND, date(2026, 5, 18), "2025-dividend"),
        (
            "FY2026 Interim Dividend",
            EventType.DIVIDEND,
            date(2026, 11, 23),
            "2026-interim-dividend",
        ),
        ("Special dividend", EventType.DIVIDEND, date(2026, 12, 1), "2026-special-dividend"),
        ("Q4 2026 Dividend", EventType.DIVIDEND, date(2026, 12, 15), "2026Q4-dividend"),
        ("2026 ex-dividend", EventType.DIVIDEND, date(2026, 5, 12), "2026-ex-dividend"),
        ("2026 dividend payment", EventType.DIVIDEND, date(2026, 5, 18), "2026-pay-dividend"),
        ("2026 payable dividend", EventType.DIVIDEND, date(2026, 5, 18), "2026-pay-dividend"),
        (
            "2026 record date dividend",
            EventType.DIVIDEND,
            date(2026, 5, 14),
            "2026-record-dividend",
        ),
        ("December 2026 dividend", EventType.DIVIDEND, date(2026, 12, 15), "2026-12-dividend"),
        ("Quarterly dividend", EventType.DIVIDEND, date(2026, 12, 15), "2026-12-dividend"),
    ],
)
def test_canonical_period_for_expanded_types_and_multi_milestone_dividends(
    raw: str, event_type: EventType, event_date: date, expected: str
) -> None:
    assert canonical_period(raw, _source(), event_type, event_date) == expected


def test_verify_claims_preserves_multi_milestone_dividends_as_distinct_events() -> None:
    page = (
        "Dividend schedule 2026\n"
        "Ex-dividend date 2026: 12 May 2026\n"
        "Record date 2026: 14 May 2026\n"
        "Dividend payment date 2026: 18 May 2026\n"
    )
    claims = [
        ExtractClaim(
            type=EventType.DIVIDEND,
            period="2026 ex-dividend",
            date=date(2026, 5, 12),
            evidence="Ex-dividend date 2026: 12 May 2026",
        ),
        ExtractClaim(
            type=EventType.DIVIDEND,
            period="2026 record date dividend",
            date=date(2026, 5, 14),
            evidence="Record date 2026: 14 May 2026",
        ),
        ExtractClaim(
            type=EventType.DIVIDEND,
            period="2026 dividend payment",
            date=date(2026, 5, 18),
            evidence="Dividend payment date 2026: 18 May 2026",
        ),
    ]
    result = verify_claims(claims, _source(), page, today=date(2026, 4, 1))
    assert not result.rejected
    assert [e.period for e in result.events] == [
        "2026-ex-dividend",
        "2026-record-dividend",
        "2026-pay-dividend",
    ]


@pytest.mark.parametrize(
    ("period", "event_date", "evidence"),
    [
        (
            "FY 2025",
            date(2026, 1, 29),
            "29 January 2026 | Preliminary results for 2025 / Annual Media Conference",
        ),
        (
            "Q1 2026",
            date(2026, 4, 30),
            "30 April 2026 | Earnings Report as of March 31, 2026 / Conference Call",
        ),
        (
            "Q2 2026",
            date(2026, 7, 28),
            "28 July 2026 | Interim Report as of June 30, 2026 - Conference Call",
        ),
        (
            "Q3 2026",
            date(2026, 10, 29),
            "29 October 2026 | Quarterly Results Q3 2026, Conference Call",
        ),
        (
            "FY 2026",
            date(2027, 2, 4),
            "4 February 2027 | Annual Results 2026 / Press Conference",
        ),
    ],
)
def test_combined_results_and_call_classified_as_publication_without_downgrade(
    period: str, event_date: date, evidence: str
) -> None:
    claim = ExtractClaim(
        type=EventType.EARNINGS,
        period=period,
        date=event_date,
        evidence=evidence,
    )
    result = verify_claims([claim], _source(), evidence, today=date(2026, 1, 1))
    assert not result.rejected
    assert len(result.events) == 1
    event = result.events[0]
    assert event.status == EventStatus.IR_CONFIRMED
    assert kind_for(event) == TimingKind.PUBLICATION
    assert event.timing_issue is None


def test_call_only_earnings_claim_still_downgraded() -> None:
    evidence = "30 April 2026 | Q1 2026 Investor Call"
    claim = ExtractClaim(
        type=EventType.EARNINGS,
        period="Q1 2026",
        date=date(2026, 4, 30),
        evidence=evidence,
    )
    result = verify_claims([claim], _source(), evidence, today=date(2026, 1, 1))
    assert len(result.events) == 1
    assert result.events[0].status == EventStatus.EXPECTED
    assert result.events[0].timing_issue == CALL_ONLY_RESULTS


def test_week_of_precision_inferred_and_explicit_precision_propagated() -> None:
    page = (
        "Financial Calendar\n"
        "Week of 19 October 2026 | Q3 2026 Results\n"
        "12 February 2027 | FY 2026 Results (tentative)\n"
    )
    claims = [
        ExtractClaim(
            type=EventType.EARNINGS,
            period="Q3 2026",
            date=date(2026, 10, 19),
            evidence="Week of 19 October 2026 | Q3 2026 Results",
        ),
        ExtractClaim(
            type=EventType.EARNINGS,
            period="FY 2026",
            date=date(2027, 2, 12),
            date_precision=DatePrecision.TENTATIVE,
            evidence="12 February 2027 | FY 2026 Results (tentative)",
        ),
    ]
    result = verify_claims(claims, _source(), page, today=date(2026, 8, 1))
    assert not result.rejected
    by_period = {e.period: e for e in result.events}
    assert by_period["2026Q3"].date_precision == DatePrecision.WEEK_OF
    assert by_period["2026Q4"].date_precision == DatePrecision.TENTATIVE


def test_calendar_desired_and_ics_include_new_types_est_prefix_and_precision() -> None:
    events = [
        {
            "id": "earnings:alpha:2026Q3",
            "issuer_key": "alpha",
            "ticker": "ALPHA",
            "type": "earnings",
            "period": "2026Q3",
            "date": "2026-10-19",
            "date_precision": "week_of",
            "status": "expected",
            "source_url": "https://ir.alpha.example/calendar",
            "evidence": "Week of 19 October 2026 | Q3 2026 Results",
            "revision": 1,
        },
        {
            "id": "dividend:alpha:2026-ex-dividend",
            "issuer_key": "alpha",
            "ticker": "ALPHA",
            "type": "dividend",
            "period": "2026-ex-dividend",
            "date": "2026-05-12",
            "date_precision": "exact",
            "status": "ir-confirmed",
            "source_url": "https://ir.alpha.example/calendar",
            "evidence": "Ex-dividend date 2026: 12 May 2026",
            "revision": 2,
        },
        {
            "id": "conference:alpha:2026-09-22-conference",
            "issuer_key": "alpha",
            "ticker": "ALPHA",
            "type": "conference",
            "period": "2026-09-22-conference",
            "date": "2026-09-22",
            "status": "ir-confirmed",
            "source_url": "https://ir.alpha.example/calendar",
            "evidence": "22 September 2026 | Autumn European Conference",
            "revision": 3,
        },
        {
            "id": "trading-update:alpha:2026Q3-trading-update",
            "issuer_key": "alpha",
            "ticker": "ALPHA",
            "type": "trading-update",
            "period": "2026Q3-trading-update",
            "date": "2026-10-22",
            "status": "ir-confirmed",
            "source_url": "https://ir.alpha.example/calendar",
            "evidence": "22 October 2026 | Q3 2026 Trading Update",
            "revision": 4,
        },
        {
            "id": "conference:alpha:2025-09-22-conference",
            "issuer_key": "alpha",
            "ticker": "ALPHA",
            "type": "conference",
            "period": "2025-09-22-conference",
            "date": "2025-09-22",
            "status": "past",
            "source_url": "https://ir.alpha.example/calendar",
            "evidence": "22 September 2025 | Past Conference",
            "revision": 5,
        },
    ]
    entries = calendar.desired(events)
    keys = [entry["key"] for entry in entries]
    assert keys == [
        "earnings-watch:earnings:alpha:2026Q3",
        "earnings-watch:dividend:alpha:2026-ex-dividend",
        "earnings-watch:conference:alpha:2026-09-22-conference",
        "earnings-watch:trading-update:alpha:2026Q3-trading-update",
    ]
    est_entry = entries[0]
    assert est_entry["title"] == "[Est.] ALPHA 2026Q3 earnings"
    assert "Date precision: week_of" in est_entry["body"]
    assert not entries[1]["title"].startswith("[Est.] ")
    assert "Date precision:" not in entries[1]["body"]

    rendered = ics.render(events)
    assert "SUMMARY:[Est.] ALPHA 2026Q3 earnings" in rendered
    assert "Date precision: week_of" in rendered
    assert "UID:earnings-watch:dividend:alpha:2026-ex-dividend" in rendered
    assert "UID:earnings-watch:conference:alpha:2026-09-22-conference" in rendered
    assert "UID:earnings-watch:trading-update:alpha:2026Q3-trading-update" in rendered
    assert "UID:earnings-watch:conference:alpha:2025-09-22-conference" not in rendered


def test_presentation_labels_est_prefix_and_date_precision(tmp_path: Path) -> None:
    assert format_event_summary("2026-09-22-conference", "conference") == "2026-09-22 Conference"
    assert (
        format_event_summary("2026Q3-trading-update", "trading-update") == "2026Q3 Trading Update"
    )

    db_path = tmp_path / "earnings.sqlite"
    initialize(db_path)
    add_person(db_path, "alice")
    cat = Catalogue(db_path)
    cat.put_source(_source())
    person = PersonalStore(db_path, "alice")
    person.watch("ALPHA")
    library = LibraryReader(db_path)

    week_of_event = {
        "id": "earnings:alpha:2026Q3",
        "issuer_key": "alpha",
        "ticker": "ALPHA",
        "type": "earnings",
        "period": "2026Q3",
        "date": "2026-10-19",
        "date_precision": "week_of",
        "status": "expected",
        "source_url": "https://ir.alpha.example/calendar",
        "evidence": "Week of 19 October 2026 | Q3 2026 Results",
    }
    presented_week = present(week_of_event, person, library)
    assert presented_week["title"] == "[Est.] ALPHA · 2026Q3 Results"
    assert presented_week["when"] == "Week of Mon 19 Oct (tentative)"

    tentative_event = {
        "id": "conference:alpha:2026-11-10-conference",
        "issuer_key": "alpha",
        "ticker": "ALPHA",
        "type": "conference",
        "period": "2026-11-10-conference",
        "date": "2026-11-10",
        "date_precision": "tentative",
        "status": "ir-confirmed",
        "source_url": "https://ir.alpha.example/calendar",
        "evidence": "10 November 2026 | Investor Conference (tentative)",
    }
    presented_tentative = present(tentative_event, person, library)
    assert presented_tentative["title"] == "ALPHA · 2026-11-10 Conference"
    assert presented_tentative["when"] == "Tue 10 Nov (tentative)"
