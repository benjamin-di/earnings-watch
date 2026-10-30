from __future__ import annotations
from datetime import date, datetime, timedelta, timezone
from earnings_watch.engine.reconcile import reconcile
from earnings_watch.model.events import Event, EventStatus, EventType, ResultsLink
from earnings_watch.model.sources import FetchStrategy, PeriodScheme, Source

NOW = datetime(2026, 7, 29, 0, 0, tzinfo=timezone.utc)


def _event(
    identifier: str,
    *,
    issuer_key: str = "alpha",
    event_date: date = date(2026, 10, 14),
    status: EventStatus = EventStatus.IR_CONFIRMED,
) -> Event:
    event_type, ticker, period = identifier.split(":")
    return Event(
        id=identifier,
        issuer_key=issuer_key,
        ticker=ticker,
        type=EventType(event_type),
        period=period,
        date=event_date,
        status=status,
        source_url="https://ir.example/calendar",
        evidence=f"Results on {event_date.strftime('%d %B %Y')}.",
        first_seen=date(2026, 7, 1),
        last_verified=date(2026, 7, 29),
    )


def _reconcile(existing, observed, **kwargs):
    defaults = {
        "authoritative_sources": set(),
        "active_source_keys": {"alpha"},
        "always_source_keys": set(),
        "removal_overrides": set(),
        "seed_mode": False,
        "now": NOW,
    }
    defaults.update(kwargs)
    return reconcile(existing, observed, **defaults)


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


def test_seed_adds_without_alert_diff() -> None:
    events, diff = _reconcile([], [_event("earnings:alpha:2026Q3")], seed_mode=True)
    assert len(events) == 1
    assert not diff.has_signal


def test_reconcile_retires_observed_events_older_than_retention_window() -> None:
    retired = _event("earnings:alpha:2025Q4", event_date=date(2026, 4, 29))
    boundary = _event("earnings:alpha:2026Q1", event_date=date(2026, 4, 30))
    events, diff = _reconcile([retired], [retired, boundary], seed_mode=True)
    assert [event.id for event in events] == [boundary.id]
    assert not diff.has_signal


def test_status_ladder_promotes_but_never_downgrades_ir() -> None:
    secondary = _event("earnings:alpha:2026Q3", status=EventStatus.SECONDARY)
    confirmed = _event("earnings:alpha:2026Q3", status=EventStatus.IR_CONFIRMED)
    promoted, diff = _reconcile([secondary], [confirmed])
    assert promoted[0].status is EventStatus.IR_CONFIRMED
    assert len(diff.promoted) == 1
    retained, diff = _reconcile([confirmed], [secondary])
    assert retained[0].status is EventStatus.IR_CONFIRMED
    assert not diff.has_signal


def test_fetch_failure_keeps_unknown_event() -> None:
    old = _event("earnings:alpha:2026Q3")
    events, diff = _reconcile([old], [], authoritative_sources=set())
    assert events == [old]
    assert not diff.has_signal


def test_verified_absence_removes_at_or_below_threshold() -> None:
    old = _event("earnings:alpha:2026Q3")
    events, diff = _reconcile([old], [], authoritative_sources={"alpha"})
    assert events == []
    assert [change.event.id for change in diff.removed] == [old.id]


def test_mass_removal_circuit_holds_then_later_confirmation_clears() -> None:
    existing = [_event(f"earnings:{ticker}:2026Q3") for ticker in ("AAA", "BBB", "CCC")]
    held, first_diff = _reconcile(existing, [], authoritative_sources={"alpha"})
    assert len(held) == 3
    assert len(first_diff.held) == 3
    assert not first_diff.removed
    cleared, second_diff = _reconcile(
        held, [], authoritative_sources={"alpha"}, now=NOW + timedelta(seconds=1)
    )
    assert cleared == []
    assert len(second_diff.removed) == 3


def test_always_membership_is_never_removed() -> None:
    old = _event("earnings:bgav:2026H1", issuer_key="bawag")
    events, diff = _reconcile(
        [old],
        [],
        authoritative_sources={"bawag"},
        active_source_keys={"bawag"},
        always_source_keys={"bawag"},
    )
    assert len(events) == 1
    assert len(diff.held) == 1
    assert not diff.removed
    retained, repeated_diff = _reconcile(
        events,
        [],
        authoritative_sources={"bawag"},
        active_source_keys={"bawag"},
        always_source_keys={"bawag"},
        now=NOW + timedelta(seconds=1),
    )
    assert len(retained) == 1
    assert not repeated_diff.has_signal


def merged_note(events) -> str:
    return events[0].history[-1].note


def test_date_move_does_not_inherit_stale_time() -> None:
    stored = _event("earnings:alpha:2026Q3").model_copy(
        update={"release_time": "17:00", "release_tz": "Europe/Amsterdam"}
    )
    moved = _event("earnings:alpha:2026Q3", event_date=date(2026, 10, 16))
    events, diff = _reconcile([stored], [moved])
    assert [change.event.id for change in diff.moved] == [stored.id]
    assert events[0].release_time is None
    assert [change.event.id for change in diff.retimed] == [stored.id]


def _link(url: str = "https://ir.example/q3.pdf") -> ResultsLink:
    return ResultsLink(title="Q3 2026 press release", kind="press-release", url=url)
