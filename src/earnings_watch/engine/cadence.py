"""Pure cadence-based projection for unconfirmed upcoming earnings dates."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, timedelta
from typing import Any

from earnings_watch.model.events import Event, EventStatus, EventType
from earnings_watch.model.sources import PeriodScheme, Source, event_id

_PERIOD_RE = re.compile(r"^(?P<year>20\d{2})(?P<kind>FQ|Q|H)(?P<n>[1-4])$")


def _advance_period(period: str, steps: int) -> str:
    match = _PERIOD_RE.match(period)
    if match is None:
        return period
    year = int(match.group("year"))
    kind = match.group("kind")
    n = int(match.group("n"))
    per_year = 2 if kind == "H" else 4
    total = (year * per_year + (n - 1)) + steps
    next_year, next_idx = divmod(total, per_year)
    return f"{next_year}{kind}{next_idx + 1}"


def _period_for_date(projected_date: date, source: Source) -> str:
    lag_days = 75 if source.period_scheme == PeriodScheme.HALF_YEAR else 50
    approx_end = projected_date - timedelta(days=lag_days)
    month = approx_end.month
    year = approx_end.year
    elapsed = (month - source.fy_end_month - 1) % 12 + 1
    fiscal_year = year if month <= source.fy_end_month else year + 1
    if source.period_scheme == PeriodScheme.HALF_YEAR:
        half = 1 if elapsed <= 6 else 2
        return f"{fiscal_year}H{half}"
    quarter = (elapsed - 1) // 3 + 1
    suffix = "FQ" if source.period_scheme == PeriodScheme.FISCAL_Q else "Q"
    return f"{fiscal_year}{suffix}{quarter}"


def _prior_year_anchor(past_dates: list[date], target: date, *, today: date) -> date | None:
    """Return a prior-year same-quarter/half date aligned to its ISO weekday (+364d) if one fits."""
    best: tuple[int, date] | None = None
    for prior in past_dates:
        delta = (target - prior).days
        if abs(delta - 365) <= 45:
            aligned = prior + timedelta(days=364)
            if aligned >= today:
                distance = abs(delta - 365)
                if best is None or distance < best[0]:
                    best = (distance, aligned)
    return best[1] if best is not None else None


def project_expected_earnings(
    source: Source,
    events: list[Event] | tuple[Event, ...],
    *,
    today: date,
    historical_dates: Iterable[date] = (),
) -> Event | None:
    """Project the next unconfirmed earnings event from an issuer's historical reporting cadence."""
    issuer_earnings = [
        ev for ev in events if ev.issuer_key == source.key and ev.type == EventType.EARNINGS
    ]
    if any(ev.date >= today and ev.status == EventStatus.IR_CONFIRMED for ev in issuer_earnings):
        return None

    real_past_events = [
        ev
        for ev in issuer_earnings
        if ev.date < today
        and ev.status != EventStatus.EXPECTED
        and "Estimated from issuer historical reporting cadence" not in ev.evidence
    ]
    past_set: set[date] = {ev.date for ev in real_past_events}
    past_set.update(d for d in historical_dates if d < today)
    if not past_set:
        return None
    past_dates = sorted(past_set)

    step_days = 182 if source.period_scheme == PeriodScheme.HALF_YEAR else 91
    candidate = past_dates[-1] + timedelta(days=step_days)
    while candidate < today - timedelta(days=35):
        candidate += timedelta(days=step_days)

    anchored = _prior_year_anchor(past_dates, candidate, today=today)
    if anchored is not None:
        projected_date = anchored
    else:
        while candidate < today:
            candidate += timedelta(days=step_days)
        projected_date = _prior_year_anchor(past_dates, candidate, today=today) or candidate

    past_events = sorted(
        (ev for ev in real_past_events if _PERIOD_RE.match(ev.period)),
        key=lambda ev: ev.date,
    )
    if past_events:
        latest = past_events[-1]
        steps = max(1, round((projected_date - latest.date).days / step_days))
        period = _advance_period(latest.period, steps)
    else:
        period = _period_for_date(projected_date, source)

    existing_past_periods = {ev.period for ev in real_past_events}
    while period in existing_past_periods:
        period = _advance_period(period, 1)

    extra: dict[str, Any] = {}
    if "date_precision" in Event.model_fields:
        extra["date_precision"] = "week_of"

    return Event(
        id=event_id(EventType.EARNINGS, source.key, period),
        issuer_key=source.key,
        ticker=source.ticker,
        type=EventType.EARNINGS,
        period=period,
        date=projected_date,
        status=EventStatus.EXPECTED,
        source_url=source.calendar_url,
        evidence="Estimated from issuer historical reporting cadence (unconfirmed)",
        first_seen=today,
        last_verified=today,
        **extra,
    )
