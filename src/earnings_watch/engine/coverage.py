from __future__ import annotations
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from datetime import date as Date
import statistics
from typing import Any
from earnings_watch.model.limits import (
    COVERAGE_TOLERANCE_DAYS,
    DEGRADED_CONSECUTIVE_FAILURES,
    EXPECTED_RESULTS_EARLY_DAYS,
    STALE_HEALTH_DAYS,
)
from earnings_watch.model.events import Event, EventType, HealthEntry
from earnings_watch.model.sources import PeriodScheme, Source

_TRACKED = frozenset({EventType.EARNINGS, EventType.EARNINGS_CALL})
_US_EXCHANGES = frozenset({"XNAS", "XNYS", "ARCX", "BATS"})
_MIN_CONFIRMATION_DEADLINE_DAYS = 5
_MAX_CONFIRMATION_DEADLINE_DAYS = 30
_US_DEFAULT_DEADLINE_DAYS = 7
_HKEX_DEFAULT_DEADLINE_DAYS = 9
_OTHER_DEFAULT_DEADLINE_DAYS = 14


def _parse_event_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def confirmation_deadline_days(
    source: Source, events: Sequence[Event | dict[str, Any]]
) -> tuple[int, int | None]:
    """Return (overdue_deadline_days, typical_lead_days) for an issuer's earnings confirmations.

    Learns from observed confirmed/past earnings lead times (date - first_seen) when available,
    falling back to market-regime priors (US 7d, HKEX 9d per Listing Rule 13.43, other 14d).
    """
    leads: list[int] = []
    for ev in events:
        if isinstance(ev, dict):
            ev_type = str(ev.get("type") or "")
            status = str(ev.get("status") or "")
            evidence = str(ev.get("evidence") or "")
            ev_date = _parse_event_date(ev.get("date"))
            first_seen = _parse_event_date(ev.get("first_seen"))
        else:
            ev_type = str(ev.type)
            status = str(ev.status)
            evidence = ev.evidence or ""
            ev_date = ev.date
            first_seen = ev.first_seen
        if ev_type != "earnings" or status not in {"ir-confirmed", "past"}:
            continue
        if "Estimated from issuer historical reporting cadence" in evidence:
            continue
        if ev_date is None or first_seen is None:
            continue
        lead = (ev_date - first_seen).days
        if lead > 0:
            leads.append(lead)

    if leads:
        typical = int(round(statistics.median(leads)))
        deadline = max(
            _MIN_CONFIRMATION_DEADLINE_DAYS,
            min(_MAX_CONFIRMATION_DEADLINE_DAYS, min(leads) - 2),
        )
        return deadline, typical

    if (
        source.exchange in _US_EXCHANGES
        or source.sec_cik is not None
        or (source.isin or "").startswith("US")
    ):
        return _US_DEFAULT_DEADLINE_DAYS, None
    if (
        source.exchange == "XHKG"
        or bool(source.hkex_stock_id)
        or "hkexnews.hk" in (source.calendar_url or "").lower()
    ):
        return _HKEX_DEFAULT_DEADLINE_DAYS, None
    return _OTHER_DEFAULT_DEADLINE_DAYS, None


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
