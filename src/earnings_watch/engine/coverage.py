from __future__ import annotations
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import date as Date
from earnings_watch.model.limits import (
    COVERAGE_TOLERANCE_DAYS,
    DEGRADED_CONSECUTIVE_FAILURES,
    EXPECTED_RESULTS_EARLY_DAYS,
    STALE_HEALTH_DAYS,
)
from earnings_watch.model.events import Event, EventType, HealthEntry
from earnings_watch.model.sources import PeriodScheme, Source

_TRACKED = frozenset({EventType.EARNINGS, EventType.EARNINGS_CALL})


@dataclass(frozen=True)
class StaleSource:
    key: str
    ticker: str
    name: str
    summary: str
    next_event: date | None
    last_ok: date | None
    tolerance_days: int


def tolerance_days(days_to_event: int | None) -> int:
    for horizon, allowed in COVERAGE_TOLERANCE_DAYS:
        if horizon is None:
            return allowed
        if days_to_event is not None and days_to_event <= horizon:
            return allowed
    return STALE_HEALTH_DAYS


@dataclass(frozen=True)
class Horizon:
    date: date
    basis: str

    def describe(self, today: Date) -> str:
        days = (self.date - today).days
        if self.basis == "tracked":
            return f"next event {self.date} in {days} day(s)"
        if self.basis == "expected":
            return f"no results date tracked; next results expected from about {self.date}"
        return "no results date tracked yet"


def horizon(
    source: Source,
    events: list[Event] | tuple[Event, ...],
    today: date,
    last_results: date | None = None,
) -> Horizon:
    own = [event for event in events if event.issuer_key == source.key]
    upcoming = [e.date for e in own if e.date >= today and e.type in _TRACKED]
    if upcoming:
        return Horizon(min(upcoming), "tracked")
    past = [e.date for e in own if e.date < today and e.type == EventType.EARNINGS]
    if last_results is not None and last_results < today:
        past.append(last_results)
    if not past:
        return Horizon(today, "untracked")
    cadence = 182 if source.period_scheme == PeriodScheme.HALF_YEAR else 91
    expected = max(past) + timedelta(days=cadence - EXPECTED_RESULTS_EARLY_DAYS)
    return Horizon(max(today, expected), "expected")


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def stale_sources(
    sources: list[Source] | tuple[Source, ...],
    events: list[Event] | tuple[Event, ...],
    health: dict[str, HealthEntry],
    *,
    now: datetime,
    today: date,
    last_results: dict[str, date] | None = None,
) -> list[StaleSource]:
    now = _aware(now)
    stale: list[StaleSource] = []
    for source in sources:
        due = horizon(source, events, today, (last_results or {}).get(source.key))
        next_event = due.date if due.basis == "tracked" else None
        tolerance = tolerance_days((due.date - today).days)
        described = due.describe(today)
        entry = health.get(source.key)
        if entry is None or entry.last_ok is None:
            failures = entry.consecutive_failures if entry else 0
            if entry is None or failures < DEGRADED_CONSECUTIVE_FAILURES:
                continue
            summary = f"NEVER VERIFIED — {entry.last_state}; {failures} consecutive failure(s); {described}"
            last_ok = None
        else:
            age = (now - _aware(entry.last_ok)).days
            if age <= tolerance:
                continue
            last_ok = _aware(entry.last_ok).date()
            summary = f"STALE — last successful check {last_ok} ({age} days ago, tolerance {tolerance}); state={entry.last_state}; {described}"
        stale.append(
            StaleSource(
                key=source.key,
                ticker=source.ticker,
                name=source.name,
                summary=summary,
                next_event=next_event,
                last_ok=last_ok,
                tolerance_days=tolerance,
            )
        )
    return stale
