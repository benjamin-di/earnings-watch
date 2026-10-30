from datetime import date, datetime, timezone
import pytest
from earnings_watch.engine import reconcile as events_store
from earnings_watch.engine import verify as verify
from earnings_watch.model.events import Event, EventType, ExtractClaim
from earnings_watch.model.sources import Source
from earnings_watch.engine.timing_search import timezone_supported

TODAY = date(2026, 9, 6)
NOW = datetime(2026, 9, 6, 23, 10, tzinfo=timezone.utc)


def source(**changes):
    return Source(
        key="beta",
        issuer="BETA",
        ticker="BETA",
        name="BETA",
        calendar_url="https://ir.example/calendar",
        fetch="plain",
        period_scheme="calendar-q",
        fy_end_month=12,
    ).model_copy(update=changes)


def event(**changes):
    return Event(
        id="earnings:beta:2026Q3",
        issuer_key="beta",
        ticker="BETA",
        type="earnings",
        period="2026Q3",
        date=date(2026, 9, 8),
        status="ir-confirmed",
        source_url=source().calendar_url,
        evidence="Q3 2026 results on 8 September 2026.",
        first_seen=TODAY,
        last_verified=TODAY,
    ).model_copy(update=changes)


def claim(**changes):
    return ExtractClaim(
        type="earnings",
        period="Q3 2026",
        date=date(2026, 9, 8),
        release_time="07:00",
        release_tz="Europe/Lisbon",
        timing_kind="publication",
        evidence="Q3 2026 results will be published on 8 September 2026 at 07:00 Europe/Lisbon.",
        source_url="https://ir.example/notice",
    ).model_copy(update=changes)


def reconcile(old, incoming):
    return events_store.reconcile(
        [old],
        incoming if isinstance(incoming, list) else [incoming],
        authoritative_sources={"beta"},
        active_source_keys={"beta"},
        always_source_keys=set(),
        removal_overrides=set(),
        seed_mode=False,
        now=NOW,
    )


@pytest.mark.parametrize("text", ["8 September 2026", "8 Sept 2026", "Sep. 8, 2026"])
def test_september_evidence_is_not_mangled(text):
    assert date(2026, 9, 8) in verify.dates_in_evidence(text)


@pytest.mark.parametrize(
    "day, clock", [(date(2026, 10, 25), "01:30"), (date(2026, 3, 29), "01:30")]
)
def test_ambiguous_or_nonexistent_dst_time_is_left_unknown(day, clock):
    c = claim(
        date=day,
        release_time=clock,
        release_tz="Europe/London",
        evidence=f"Q3 2026 results will be published on {day} at {clock} Europe/London.",
    )
    assert verify.verified_release_time(c) == (None, None)


@pytest.mark.parametrize(
    "evidence",
    [
        "Q3 2026 results on 8 September 2026. Board of Directors approval at 10:00 Europe/Lisbon.",
        "Q3 2026 results will be released on 8 September 2026; press conference at 17:00 Europe/Lisbon.",
    ],
)
def test_board_or_press_conference_clock_is_not_a_publication_time(evidence):
    c = claim(release_time="10:00" if "10:00" in evidence else "17:00", evidence=evidence)
    assert verify.verified_release_time(c) == (None, None)


def test_timed_non_earnings_event_keeps_its_clock():
    c = claim(
        type=EventType.AGM,
        period="2026 AGM",
        timing_kind=None,
        evidence="Annual General Meeting on 8 September 2026 at 07:00 Europe/Lisbon.",
    )
    result = verify.verify_claims([c], source(), c.evidence, today=TODAY)
    assert not result.rejected and result.events[0].release_time == "07:00"
    assert result.events[0].timing is None


def test_call_cannot_borrow_publication_clock_from_the_same_notice():
    c = claim(
        type=EventType.EARNINGS_CALL,
        timing_kind="investor-call",
        evidence="Q3 2026 results will be published on 8 September 2026 at 07:00 Europe/Lisbon; investor call at 09:00 Europe/Lisbon.",
    )
    assert verify.verified_release_time(c) == (None, None)
    assert verify.verified_release_time(c.model_copy(update={"release_time": "09:00"})) == (
        "09:00",
        "Europe/Lisbon",
    )


def test_city_time_label_supports_its_iana_zone():
    matching = claim(
        date=date(2026, 10, 28),
        release_time="09:00",
        release_tz="Asia/Tokyo",
        evidence="October 28, 2026 9:00 a.m. (Tokyo Time)",
    )
    assert timezone_supported(matching, matching.evidence)
    mismatched = matching.model_copy(update={"evidence": "October 28, 2026 9:00 a.m. (Oslo Time)"})
    assert not timezone_supported(mismatched, mismatched.evidence)
