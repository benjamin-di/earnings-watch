from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo
from earnings_watch.model.limits import MAX_AUTO_REMOVALS, PAST_RETIRE_DAYS
from earnings_watch.model.events import (
    Event,
    EventStatus,
    EventType,
    HistoryEntry,
    TimingResolution,
    TimingSelection,
)
from earnings_watch.model.sources import event_id
from .timing import (
    CALL_ONLY_RESULTS,
    kind_for,
    merge_observation,
    timing_candidate_fingerprint,
    usable_clock,
)

_STATUS_RANK = {
    EventStatus.EXPECTED: 0,
    EventStatus.SECONDARY: 1,
    EventStatus.IR_CONFIRMED: 2,
    EventStatus.PAST: -1,
}


@dataclass(frozen=True)
class Change:
    event: Event
    old_date: date | None = None
    old_status: EventStatus | None = None
    reason: str = ""


@dataclass
class EventDiff:
    added: list[Change] = field(default_factory=list)
    moved: list[Change] = field(default_factory=list)
    promoted: list[Change] = field(default_factory=list)
    retimed: list[Change] = field(default_factory=list)
    removed: list[Change] = field(default_factory=list)
    held: list[Change] = field(default_factory=list)

    @property
    def has_signal(self) -> bool:
        return any((self.added, self.moved, self.promoted, self.retimed, self.removed, self.held))


def timing_desc(event: Event) -> str:
    if event.timing_issue and (not event.timing_resolution):
        return "unresolved (all-day; timing review required)"
    if event.release_time and event.release_tz:
        return f"{event.release_time} {event.release_tz}"
    if event.time_hint:
        return event.time_hint.value
    return "unspecified"


def _clock(event: Event) -> str | None:
    if not usable_clock(event):
        return None
    local = datetime.fromisoformat(f"{event.date}T{event.release_time}").replace(
        tzinfo=ZoneInfo(event.release_tz or "UTC")
    )
    return f"{event.release_time} {local.tzname()}"


def retime_reason(old: Event, new: Event) -> str:
    was, now = (_clock(old), _clock(new))
    if new.timing_issue and (not new.timing_resolution):
        return "Sources now disagree on the time; review required" + (
            f" (was {was})" if was else ""
        )
    if now and was:
        return f"Time changed (was {was})"
    if now:
        return "Time confirmed (previously not announced)"
    if was:
        return f"Exact time withdrawn by the source (was {was})"
    if new.time_hint != old.time_hint:
        return (
            f"Release window now {new.time_hint.value}"
            if new.time_hint
            else "Release window no longer stated"
        )
    return "Timing basis changed"


def resolve_timing_selection(
    event: Event,
    selection: TimingSelection | None,
    checks: dict[str, bool],
    *,
    previous: Event | None = None,
) -> Event:
    prior = previous.timing_resolution if previous else None
    inherited = bool(
        prior
        and event.timing_issue
        and ((event.release_time, event.release_tz) == (prior.release_time, prior.release_tz))
    )
    if event.timing_resolution or inherited:
        event = event.model_copy(
            update={"release_time": None, "release_tz": None, "timing_resolution": None}
        )
    if not event.timing_issue:
        return event
    candidates = {
        timing_candidate_fingerprint(event.date, candidate): candidate
        for candidate in event.timing_disputes
        if candidate.release_time and candidate.release_tz
    }
    if selection is None:
        return event
    chosen = candidates.get(selection.select_candidate)
    check = checks.get(selection.select_candidate)
    already_reviewed = bool(
        prior
        and prior.candidate_fingerprint == selection.select_candidate
        and previous is not None
        and (previous.date == event.date)
    )
    if chosen is None or check is False or (check is None and (not already_reviewed)):
        return event.model_copy(
            update={
                "release_time": None,
                "release_tz": None,
                "timing_issue": "Reviewed timing selection needs current verified evidence",
            }
        )
    assert chosen.release_time and chosen.release_tz
    resolution = TimingResolution(
        kind=chosen.kind,
        release_time=chosen.release_time,
        release_tz=chosen.release_tz,
        source_url=chosen.source_url,
        evidence=chosen.evidence,
        candidate_fingerprint=selection.select_candidate,
        reviewed_on=selection.reviewed_on,
        reason=selection.reason,
    )
    return event.model_copy(
        update={
            "release_time": chosen.release_time,
            "release_tz": chosen.release_tz,
            "timing": chosen,
            "timing_resolution": resolution,
        }
    )


def _history(
    *, now: datetime, action: str, old: Event, new: Event | None = None, note: str = ""
) -> HistoryEntry:
    return HistoryEntry(
        at=now,
        action=action,
        old_date=old.date,
        new_date=new.date if new else None,
        old_status=old.status,
        new_status=new.status if new else None,
        note=note,
    )


def _previously_held(event: Event, now: datetime) -> bool:
    return any((entry.action == "removal-held" and entry.at < now for entry in event.history))


def downgrade_ir_confirmed(
    events: tuple[Event, ...],
    issuer_keys: set[str] | frozenset[str],
    *,
    today: date,
    now: datetime,
    retain_ids: set[str] | frozenset[str] = frozenset(),
) -> tuple[tuple[Event, ...], dict[str, int]]:
    counts: dict[str, int] = {}
    updated: list[Event] = []
    for event in events:
        if (
            event.issuer_key in issuer_keys
            and event.id not in retain_ids
            and (event.status is EventStatus.IR_CONFIRMED)
            and (event.date >= today)
        ):
            downgraded = event.model_copy(update={"status": EventStatus.SECONDARY})
            downgraded = downgraded.model_copy(
                update={
                    "history": [
                        *event.history,
                        _history(
                            now=now,
                            action="downgraded",
                            old=event,
                            new=downgraded,
                            note="source demoted to websearch; plain-fetch confirmation authority lost",
                        ),
                    ]
                }
            )
            counts[event.issuer_key] = counts.get(event.issuer_key, 0) + 1
            updated.append(downgraded)
        else:
            updated.append(event)
    return (tuple(updated), counts)


def results_from_calls(
    observed: tuple[Event, ...] | list[Event],
    existing: tuple[Event, ...] | list[Event],
    *,
    today: date,
) -> tuple[Event, ...]:
    results = {
        (event.issuer_key, event.period): event
        for event in (*existing, *observed)
        if event.type == EventType.EARNINGS
    }
    derived: list[Event] = []
    for call in observed:
        if (
            call.type != EventType.EARNINGS_CALL
            or call.date < today
            or call.status != EventStatus.IR_CONFIRMED
        ):
            continue
        held = results.get((call.issuer_key, call.period))
        if held is not None and (
            not (held.timing_issue == CALL_ONLY_RESULTS and held.date != call.date)
        ):
            continue
        event = Event(
            id=event_id(EventType.EARNINGS, call.issuer_key, call.period),
            issuer_key=call.issuer_key,
            ticker=call.ticker,
            type=EventType.EARNINGS,
            period=call.period,
            date=call.date,
            status=EventStatus.EXPECTED,
            source_url=call.source_url,
            evidence=call.evidence,
            first_seen=held.first_seen if held else call.first_seen,
            last_verified=call.last_verified,
            timing_issue=CALL_ONLY_RESULTS,
        )
        results[call.issuer_key, call.period] = event
        derived.append(event)
    return tuple(derived)


def reconcile(
    existing: tuple[Event, ...] | list[Event],
    observed: tuple[Event, ...] | list[Event],
    *,
    authoritative_sources: set[str],
    active_source_keys: set[str],
    always_source_keys: set[str],
    removal_overrides: set[str],
    seed_mode: bool,
    now: datetime,
    retain_ids: set[str] | frozenset[str] = frozenset(),
    timing_selections: dict[str, TimingSelection] | None = None,
    timing_candidate_checks: dict[str, bool] | None = None,
) -> tuple[list[Event], EventDiff]:
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    today = now.date()
    old_by_id = {event.id: event for event in existing}
    observed_by_id: dict[str, Event] = {}
    for event in observed:
        previous = observed_by_id.get(event.id)
        observed_by_id[event.id] = merge_observation(previous, event) if previous else event
    merged: dict[str, Event] = {}
    diff = EventDiff()
    for identifier, incoming in observed_by_id.items():
        if incoming.date < today - timedelta(days=PAST_RETIRE_DAYS):
            continue
        old = old_by_id.get(identifier)
        merged[identifier] = merge_observation(old, incoming) if old else incoming
    removal_candidates: list[tuple[Event, str]] = []
    for identifier, old in old_by_id.items():
        if identifier in merged:
            continue
        if old.date < today - timedelta(days=PAST_RETIRE_DAYS) and identifier not in retain_ids:
            continue
        if old.date < today:
            merged[identifier] = old.model_copy(update={"status": EventStatus.PAST})
            continue
        universe_exit = old.issuer_key not in active_source_keys
        verified_absence = old.issuer_key in authoritative_sources
        if universe_exit or verified_absence:
            reason = "issuer left live universe" if universe_exit else "verified absence"
            removal_candidates.append((old, reason))
        else:
            merged[identifier] = old
    fresh_candidates = [
        (event, reason)
        for event, reason in removal_candidates
        if event.id not in removal_overrides and (not _previously_held(event, now))
    ]
    trip_circuit = len(fresh_candidates) > MAX_AUTO_REMOVALS
    for event, reason in removal_candidates:
        protected = event.issuer_key in always_source_keys
        confirmed = event.id in removal_overrides or _previously_held(event, now)
        if protected or (trip_circuit and (not confirmed)):
            note = "membership:always" if protected else "mass-removal circuit breaker"
            history = list(event.history)
            newly_held = not any(
                (entry.action == "removal-held" and entry.note == note for entry in history)
            )
            if newly_held:
                history.append(_history(now=now, action="removal-held", old=event, note=note))
            held = event.model_copy(update={"history": history})
            merged[event.id] = held
            if newly_held:
                diff.held.append(Change(event=held, reason=f"{reason}: {note}"))
        else:
            diff.removed.append(Change(event=event, reason=reason))
    resolved: dict[str, Event] = {}
    for identifier, incoming in merged.items():
        old = old_by_id.get(identifier)
        observation = observed_by_id.get(identifier)
        historical = (
            old
            and old.date == incoming.date < today
            and (observation is None or observation.date == old.date)
            and (incoming.timing_issue == old.timing_issue)
        )
        if historical and old is not None:
            if old.timing_resolution is not None:
                incoming = incoming.model_copy(
                    update={
                        "release_time": old.release_time,
                        "release_tz": old.release_tz,
                        "timing": old.timing,
                        "timing_resolution": old.timing_resolution,
                    }
                )
        else:
            incoming = resolve_timing_selection(
                incoming,
                (timing_selections or {}).get(identifier),
                timing_candidate_checks or {},
                previous=old,
            )
        if old is None:
            resolved[identifier] = incoming
            if not seed_mode:
                diff.added.append(Change(event=incoming))
            continue
        changed_date = incoming.date != old.date
        if incoming.timing_issue and incoming.timing_issue != old.timing_issue:
            diff.held.append(Change(event=incoming, reason=incoming.timing_issue))
        updates: dict[str, Any] = {
            "first_seen": old.first_seen,
            "history": list(incoming.history or old.history),
        }
        promoted = _STATUS_RANK[incoming.status] > _STATUS_RANK[old.status]
        retimed = incoming.date >= today and (
            incoming.release_time != old.release_time
            or incoming.release_tz != old.release_tz
            or usable_clock(incoming) != usable_clock(old)
            or (
                kind_for(incoming) != kind_for(old)
                and bool(incoming.release_time or old.release_time)
            )
            or (incoming.time_hint != old.time_hint and (not incoming.release_time))
        )
        if changed_date:
            updates["history"].append(_history(now=now, action="moved", old=old, new=incoming))
        if promoted:
            updates["history"].append(_history(now=now, action="promoted", old=old, new=incoming))
        if retimed:
            timing_note = f"{timing_desc(old)} -> {timing_desc(incoming)}"
            updates["history"].append(
                _history(now=now, action="retimed", old=old, new=incoming, note=timing_note)
            )
        current = incoming.model_copy(update=updates)
        resolved[identifier] = current
        if changed_date:
            diff.moved.append(Change(event=current, old_date=old.date, old_status=old.status))
        if promoted:
            diff.promoted.append(Change(event=current, old_date=old.date, old_status=old.status))
        if retimed:
            diff.retimed.append(
                Change(
                    event=current,
                    old_date=old.date,
                    old_status=old.status,
                    reason=retime_reason(old, incoming),
                )
            )
    diff.held = [
        Change(
            event=resolved.get(change.event.id, change.event),
            old_date=change.old_date,
            old_status=change.old_status,
            reason=change.reason,
        )
        for change in diff.held
    ]
    return (sorted(resolved.values(), key=lambda item: (item.date, item.id)), diff)
