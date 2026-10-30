from __future__ import annotations
from datetime import date
import pytest
from earnings_watch.model.events import EventStatus, EventType, ExtractClaim
from earnings_watch.model.sources import FetchStrategy, PeriodScheme, Source
from earnings_watch.engine.verify import (
    dates_in_evidence,
    evidence_verifies,
    times_in_evidence,
    verify_claims,
)


def _source() -> Source:
    return Source(
        key="alpha",
        issuer="Alpha Components",
        ticker="ALPHA",
        name="ALPHA",
        calendar_url="https://ir.example/calendar",
        fetch=FetchStrategy.PLAIN,
        period_scheme=PeriodScheme.CALENDAR_Q,
        fy_end_month=12,
    )


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ("Results on 14 October 2026.", date(2026, 10, 14)),
        ("Results on October 14, 2026.", date(2026, 10, 14)),
        ("Results on 14.10.2026.", date(2026, 10, 14)),
        ("Results on 2026-10-14.", date(2026, 10, 14)),
        ("Results on 14/10/2026.", date(2026, 10, 14)),
        ("Results on Jul 23, 2026.", date(2026, 7, 23)),
        ("Results on 31st July 2026.", date(2026, 7, 31)),
    ],
)
def test_explicit_date_format_table(evidence: str, expected: date) -> None:
    assert expected in dates_in_evidence(evidence)


def test_yearless_day_month_verifies_against_claim_year() -> None:
    page = "Iota Group plc will release H1 2026 Results at 7am BST on Friday 31st July."
    assert evidence_verifies(date(2026, 7, 31), page, page)
    assert not evidence_verifies(date(2026, 7, 30), page, page)
    dated = "Results were released on 31 July 2025."
    assert not evidence_verifies(date(2026, 7, 31), dated, dated)
    us_dated = "Results were released on July 31, 2025."
    assert not evidence_verifies(date(2026, 7, 31), us_dated, us_dated)


def test_us_abbreviated_date_with_markdown_artifacts_verifies() -> None:
    page = "Jul 23, 2026 • 2:00 PM PDT ## [ Q2 2026 Intel Corporation Earnings Conference Call ]"
    assert evidence_verifies(date(2026, 7, 23), page, page)


def test_verify_requires_verbatim_evidence_and_derived_date() -> None:
    page = "Financial calendar\nQ3 results will be published on 14 October 2026."
    valid = ExtractClaim(
        type=EventType.EARNINGS,
        period="Q3 2026",
        date=date(2026, 10, 14),
        evidence="Q3 results will be published on 14 October 2026.",
    )
    paraphrase = valid.model_copy(update={"evidence": "Q3 results are due on 14 October 2026."})
    wrong_date = valid.model_copy(update={"date": date(2026, 10, 15)})
    result = verify_claims(
        [valid, paraphrase, wrong_date], _source(), page, today=date(2026, 7, 29)
    )
    assert len(result.events) == 1
    assert result.events[0].id == "earnings:alpha:2026Q3"
    assert result.events[0].status is EventStatus.IR_CONFIRMED
    assert len(result.rejected) == 2


def test_verified_historical_claim_is_stored_as_past() -> None:
    page = "Q2 results were published on 14 July 2026."
    claim = ExtractClaim(
        type=EventType.EARNINGS, period="Q2 2026", date=date(2026, 7, 14), evidence=page
    )
    result = verify_claims([claim], _source(), page, today=date(2026, 7, 29))
    assert result.events[0].status is EventStatus.PAST


def test_ancient_claims_are_dropped_not_rejected() -> None:
    page = "Nov/8th/18 (5 p.m.)| Press Conference on 3rd Quarter (2018) results\nQ3 results will be published on 14 October 2026."
    ancient = ExtractClaim(
        type=EventType.EARNINGS,
        period="3rd Quarter (2018)",
        date=date(2018, 11, 8),
        evidence="Nov/8th/18 (5 p.m.)| Press Conference on 3rd Quarter (2018) results",
    )
    current = ExtractClaim(
        type=EventType.EARNINGS,
        period="Q3 2026",
        date=date(2026, 10, 14),
        evidence="Q3 results will be published on 14 October 2026.",
    )
    result = verify_claims([ancient, current], _source(), page, today=date(2026, 8, 2))
    assert [event.id for event in result.events] == ["earnings:alpha:2026Q3"]
    assert not result.rejected


def test_evidence_tolerates_normalized_punctuation_but_not_invention() -> None:
    page = "Investors' Agenda\n28/10/2026 (5 p.m.)| Earnings for the first 9 months of 2026- Press Conference"
    cleaned = "28/10/2026 (5 p.m.) | Earnings for the first 9 months of 2026 - Press Conference"
    claim = ExtractClaim(
        type=EventType.EARNINGS,
        period="first 9 months of 2026",
        date=date(2026, 10, 28),
        evidence=cleaned,
    )
    result = verify_claims([claim], _source(), page, today=date(2026, 8, 2))
    assert len(result.events) == 1
    assert result.events[0].id == "earnings:alpha:2026Q3"
    assert not result.rejected
    invented = claim.model_copy(
        update={
            "date": date(2026, 10, 29),
            "evidence": "29/10/2026 (5 p.m.) | Earnings for the first 9 months of 2026 - Press Conference",
        }
    )
    reordered = claim.model_copy(update={"evidence": "Press Conference Earnings 28/10/2026"})
    result = verify_claims([invented, reordered], _source(), page, today=date(2026, 8, 2))
    assert not result.events
    assert len(result.rejected) == 2


def test_benign_duplicate_ids_merge_without_failing_the_source() -> None:
    page = "Financial calendar\nRecord date for the 2025 dividend on 11 June 2026.\nPayment of the 2025 dividend on 15 June 2026."
    record = ExtractClaim(
        type=EventType.DIVIDEND,
        period="2025-dividend",
        date=date(2026, 6, 11),
        evidence="Record date for the 2025 dividend on 11 June 2026.",
    )
    payment = ExtractClaim(
        type=EventType.DIVIDEND,
        period="2025-dividend",
        date=date(2026, 6, 15),
        evidence="Payment of the 2025 dividend on 15 June 2026.",
    )
    result = verify_claims([record, payment], _source(), page, today=date(2026, 5, 1))
    assert len(result.events) == 1
    assert result.events[0].date == date(2026, 6, 11)
    assert not result.rejected
    assert len(result.merged) == 1


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ("Results at 17:00 CET.", (17, 0)),
        ("Results at 5:30 p.m.", (17, 30)),
        ("Results at 1:30 PM PT.", (13, 30)),
        ("Results at 07.00 in the morning.", (7, 0)),
        ("Results at 12:15 a.m.", (0, 15)),
        ("Results at 5 p.m.", (17, 0)),
        ("Results at 17h30 local time.", (17, 30)),
    ],
)
def test_time_format_table(evidence: str, expected: tuple[int, int]) -> None:
    assert expected in times_in_evidence(evidence)


def test_release_time_kept_when_evidenced_and_stripped_when_not() -> None:
    page = "Q3 results will be published on 14 October 2026 at 17:00 Europe/Amsterdam.\nQ4 results will be published on 3 February 2027."
    evidenced = ExtractClaim(
        type=EventType.EARNINGS,
        period="Q3 2026",
        date=date(2026, 10, 14),
        release_time="17:00",
        release_tz="Europe/Amsterdam",
        evidence="Q3 results will be published on 14 October 2026 at 17:00 Europe/Amsterdam.",
    )
    hallucinated = ExtractClaim(
        type=EventType.EARNINGS,
        period="Q4 2026",
        date=date(2027, 2, 3),
        release_time="09:00",
        release_tz="Europe/Amsterdam",
        evidence="Q4 results will be published on 3 February 2027.",
    )
    result = verify_claims([evidenced, hallucinated], _source(), page, today=date(2026, 7, 29))
    assert len(result.events) == 2
    assert result.events[0].release_time == "17:00"
    assert result.events[0].release_tz == "Europe/Amsterdam"
    assert result.events[1].release_time is None
    assert result.events[1].release_tz is None
    assert not result.rejected


def test_conference_call_time_is_not_accepted_as_release_time() -> None:
    page = "Q3 results will be published on 14 October 2026; conference call at 18:00."
    claim = ExtractClaim(
        type=EventType.EARNINGS,
        period="Q3 2026",
        date=date(2026, 10, 14),
        release_time="18:00",
        release_tz="Europe/Amsterdam",
        evidence=page,
    )
    result = verify_claims([claim], _source(), page, today=date(2026, 7, 29))
    assert len(result.events) == 1
    assert result.events[0].release_time is None
    assert result.events[0].release_tz is None


def test_release_time_wins_when_call_is_a_separate_later_time() -> None:
    page = "Q3 results will be released on 14 October 2026 at 07:00 Europe/Amsterdam; conference call at 09:00."
    claim = ExtractClaim(
        type=EventType.EARNINGS,
        period="Q3 2026",
        date=date(2026, 10, 14),
        release_time="07:00",
        release_tz="Europe/Amsterdam",
        evidence=page,
    )
    result = verify_claims([claim], _source(), page, today=date(2026, 7, 29))
    assert result.events[0].release_time == "07:00"


def test_evidence_matches_the_visible_text_of_markdown_links() -> None:
    page = "News release and other information will be posted to our [Quarterly Results](https://issuer0.example/financials/quarterly-results/default.aspx) page on Tuesday, October 27, 2026."
    quote = "News release and other information will be posted to our Quarterly Results page on Tuesday, October 27, 2026."
    assert evidence_verifies(date(2026, 10, 27), quote, page)
    assert not evidence_verifies(
        date(2026, 10, 27), quote.replace("Quarterly Results", "Annual Results"), page
    )


def test_rejection_is_covered_only_when_nothing_was_lost() -> None:
    from earnings_watch.engine import verify
    from earnings_watch.model.events import Event

    src = _source()
    day = date(2026, 10, 29)

    def held(type_: EventType, period: str = "2026Q3") -> Event:
        return Event(
            id=f"{type_.value}:alpha:{period}",
            issuer_key="alpha",
            ticker="ALPHA",
            type=type_,
            period=period,
            date=day,
            status=EventStatus.IR_CONFIRMED,
            source_url="https://ir.example/calendar",
            evidence="x",
            first_seen=day,
            last_verified=day,
        )

    def claim(type_: EventType, period: str = "Q3 2026") -> ExtractClaim:
        return ExtractClaim(
            type=type_,
            period=period,
            date=day,
            evidence="x",
            source_url="https://ir.example/calendar",
        )

    results = held(EventType.EARNINGS)
    assert verify.rejection_covered(claim(EventType.EARNINGS_CALL), src, [results])
    assert verify.rejection_covered(
        claim(EventType.EARNINGS_CALL, "fiscal first quarter"), src, [results]
    )
    assert verify.rejection_covered(claim(EventType.EARNINGS), src, [results])
    assert not verify.rejection_covered(claim(EventType.EARNINGS_CALL), src, [])
    assert not verify.rejection_covered(
        claim(EventType.EARNINGS), src, [held(EventType.EARNINGS, "2026Q2")]
    )
    other = results.model_copy(update={"issuer_key": "other"})
    assert not verify.rejection_covered(claim(EventType.EARNINGS_CALL), src, [other])
