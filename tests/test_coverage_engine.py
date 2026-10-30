from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from earnings_watch.engine import coverage as coverage
from earnings_watch.model.events import Event, EventStatus, EventType, HealthEntry
from earnings_watch.model.sources import FetchStrategy, PeriodScheme, Source

"Coverage freshness: degraded means an unchecked source, not a rejection."
TODAY = date(2026, 9, 12)
NOW = datetime(2026, 9, 12, 8, tzinfo=timezone.utc)


def _source(key: str = "alpha") -> Source:
    return Source(
        key=key,
        issuer=key.upper(),
        ticker=key.upper()[:4],
        name=key.upper(),
        calendar_url="https://ir.example/calendar",
        fetch=FetchStrategy.WEBSEARCH,
        period_scheme=PeriodScheme.CALENDAR_Q,
        fy_end_month=12,
    )


def _event(key: str, on: date, type_: EventType = EventType.EARNINGS) -> Event:
    return Event(
        id=f"{type_.value}:{key.upper()[:4]}:2026Q3",
        issuer_key=key,
        ticker=key.upper()[:4],
        type=type_,
        period="2026Q3",
        date=on,
        status=EventStatus.SECONDARY,
        source_url="https://news.example/x",
        evidence="x",
        first_seen=TODAY,
        last_verified=TODAY,
    )


def _entry(
    last_ok_days_ago: int | None, state: str = "websearch-failed", failures: int = 2
) -> HealthEntry:
    return HealthEntry(
        last_state=state,
        consecutive_failures=failures,
        last_ok=None if last_ok_days_ago is None else NOW - timedelta(days=last_ok_days_ago),
    )


def test_recent_rejections_do_not_degrade_a_freshly_checked_source() -> None:
    stale = coverage.stale_sources(
        [_source()],
        [_event("alpha", TODAY + timedelta(days=32))],
        {"alpha": _entry(6)},
        now=NOW,
        today=TODAY,
    )
    assert stale == []


def test_unchecked_source_with_imminent_event_is_stale() -> None:
    stale = coverage.stale_sources(
        [_source()],
        [_event("alpha", TODAY + timedelta(days=1))],
        {"alpha": _entry(4, state="http-error")},
        now=NOW,
        today=TODAY,
    )
    assert [item.key for item in stale] == ["alpha"]
    assert stale[0].tolerance_days == 3
    assert "STALE" in stale[0].summary and "http-error" in stale[0].summary
    assert stale[0].next_event == TODAY + timedelta(days=1)


def test_same_age_is_fine_when_the_event_is_far_out() -> None:
    stale = coverage.stale_sources(
        [_source()],
        [_event("alpha", TODAY + timedelta(days=40))],
        {"alpha": _entry(4, state="http-error")},
        now=NOW,
        today=TODAY,
    )
    assert stale == []


def test_never_successful_source_degrades_on_consecutive_failures() -> None:
    src = _source("newco")
    assert (
        coverage.stale_sources([src], [], {"newco": _entry(None, failures=1)}, now=NOW, today=TODAY)
        == []
    )
    assert coverage.stale_sources([src], [], {}, now=NOW, today=TODAY) == []
    stale = coverage.stale_sources(
        [src], [], {"newco": _entry(None, failures=2)}, now=NOW, today=TODAY
    )
    assert [item.key for item in stale] == ["newco"]
    assert stale[0].last_ok is None and "NEVER VERIFIED" in stale[0].summary


def test_only_tracked_event_types_set_the_horizon() -> None:
    stale = coverage.stale_sources(
        [_source()],
        [_event("alpha", TODAY + timedelta(days=1), EventType.AGM)],
        {"alpha": _entry(12)},
        now=NOW,
        today=TODAY,
        last_results={"alpha": TODAY - timedelta(days=30)},
    )
    assert stale == []


def test_untracked_or_expected_results_set_the_horizon() -> None:
    src = _source()
    never = coverage.stale_sources([src], [], {"alpha": _entry(4)}, now=NOW, today=TODAY)
    assert [s.key for s in never] == ["alpha"] and never[0].tolerance_days == 3
    assert "no results date tracked yet" in never[0].summary
    late = coverage.stale_sources(
        [src],
        [],
        {"alpha": _entry(4)},
        now=NOW,
        today=TODAY,
        last_results={"alpha": TODAY - timedelta(days=80)},
    )
    assert [s.key for s in late] == ["alpha"] and "next results expected" in late[0].summary
    soon = coverage.horizon(src, [], TODAY, TODAY - timedelta(days=60))
    assert soon.basis == "expected" and soon.date == TODAY + timedelta(days=17)
    assert (
        coverage.stale_sources(
            [src],
            [],
            {"alpha": _entry(9)},
            now=NOW,
            today=TODAY,
            last_results={"alpha": TODAY - timedelta(days=60)},
        )
        == []
    )
    half = src.model_copy(update={"period_scheme": PeriodScheme.HALF_YEAR})
    assert coverage.horizon(half, [], TODAY, TODAY - timedelta(days=100)).date == TODAY + timedelta(
        days=68
    )
