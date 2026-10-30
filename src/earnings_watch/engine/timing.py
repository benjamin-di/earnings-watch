from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from typing import Any
from earnings_watch.model.events import (
    Event,
    EventType,
    EventStatus,
    TimingEvidence,
    TimingKind,
    TimingResolution,
)

_PRESS = re.compile("\\bpress\\s+conference\\b", re.I)
_PUBLICATION = re.compile(
    "\\b(?:publish(?:ed|es|ing)?|publication|release(?:d|s)?|available)\\b", re.I
)
_BOARD = re.compile("\\b(?:board(?:\\s+of\\s+directors)?|approval|approve[ds]?)\\b", re.I)
_PRE_CALL = re.compile("\\bpre[-\\s]earnings\\b", re.I)
_WITHDRAWN = re.compile(
    "\\b(?:time|timing)\\s+(?:is\\s+|has\\s+been\\s+)?(?:TBC|TBA|to\\s+be\\s+(?:confirmed|announced)|withdrawn|not\\s+yet\\s+(?:confirmed|announced))\\b",
    re.I,
)
_CORRECTION = re.compile(
    "\\b(?:corrected\\s+(?:time|date|schedule)|(?:time|date)\\s+(?:has\\s+been\\s+|is\\s+)?(?:changed|revised)|rescheduled|supersedes|revised\\s+date|postponed|brought\\s+forward|moved\\s+the\\s+date|instead\\s+of\\s+(?:the\\s+)?(?:\\d{1,2}(?:st|nd|rd|th)?\\s+[a-z]+|[a-z]+\\s+\\d{1,2}\\b))",
    re.I,
)
CALL_ONLY_RESULTS = "Publication date unconfirmed; source gives an investor call only"
UNCONFIRMED_PUBLICATION = "Publication date unconfirmed"
DATE_DISAGREEMENT = "Source dates disagree; review before changing the calendar"


def explicit_correction(event: Event) -> bool:
    evidence = event.timing.evidence if event.timing else event.evidence
    return event.status == EventStatus.IR_CONFIRMED and bool(_CORRECTION.search(evidence))


def board_only(evidence: str) -> bool:
    return bool(_BOARD.search(evidence) and (not _PUBLICATION.search(evidence)))


def pre_earnings_call(evidence: str) -> bool:
    return bool(_PRE_CALL.search(evidence))


def call_only(evidence: str) -> bool:
    return bool(
        re.search("\\b(?:call|webcast)\\b", evidence, re.I)
        and (not _PUBLICATION.search(evidence))
        and (not re.search("\\b(?:earnings|interim|quarterly) report\\b", evidence, re.I))
    )


def timing_withdrawn(evidence: str) -> bool:
    return bool(_WITHDRAWN.search(evidence))


def usable_clock(event: Event) -> bool:
    return bool(
        event.release_time
        and event.release_tz
        and (not event.timing_issue or event.timing_resolution is not None)
        and (not (event.timing and event.timing.withdrawn))
    )


def clock_key(event: Event):
    if not event.release_time or not event.release_tz:
        return None
    return (
        datetime.fromisoformat(f"{event.date}T{event.release_time}")
        .replace(tzinfo=ZoneInfo(event.release_tz))
        .astimezone(timezone.utc)
    )


def evidence_clock_key(event_date, evidence: TimingEvidence | TimingResolution):
    if not evidence.release_time or not evidence.release_tz:
        return None
    return (
        datetime.fromisoformat(f"{event_date}T{evidence.release_time}")
        .replace(tzinfo=ZoneInfo(evidence.release_tz))
        .astimezone(timezone.utc)
    )


def timing_candidate_fingerprint(event_date, evidence: TimingEvidence | TimingResolution) -> str:
    instant = evidence_clock_key(event_date, evidence)
    if instant is None:
        raise ValueError("timing candidate has no normalized clock")
    payload = json.dumps(
        {
            "date": event_date.isoformat(),
            "kind": evidence.kind.value,
            "instant": instant.isoformat(),
            "source_url": evidence.source_url,
            "evidence": " ".join(evidence.evidence.split()),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def merge_timing_disputes(
    event_date, *groups: list[TimingEvidence] | tuple[TimingEvidence, ...]
) -> list[TimingEvidence]:
    unique: dict[tuple[object, ...], TimingEvidence] = {}
    for evidence in (item for group in groups for item in group if item):
        instant = evidence_clock_key(event_date, evidence)
        key = (
            (
                "instant",
                evidence.kind,
                instant,
                evidence.source_url,
                " ".join(evidence.evidence.split()),
            )
            if instant is not None
            else ("legacy", evidence.kind, evidence.source_url, " ".join(evidence.evidence.split()))
        )
        unique.setdefault(key, evidence)
    representatives: list[TimingEvidence] = []
    corroboration: list[TimingEvidence] = []
    seen: set[tuple[object, ...]] = set()
    for evidence in unique.values():
        group = (evidence.kind, evidence_clock_key(event_date, evidence))
        (corroboration if group in seen else representatives).append(evidence)
        seen.add(group)
    return (representatives + corroboration)[:8]


def inferred_kind(event: Event) -> TimingKind:
    if event.type == EventType.EARNINGS_CALL:
        return TimingKind.INVESTOR_CALL
    if board_only(event.evidence):
        return TimingKind.BOARD_APPROVAL
    if is_press_conference(event.evidence):
        return TimingKind.PRESS_CONFERENCE
    return TimingKind.PUBLICATION


def is_press_conference(evidence: str) -> bool:
    return bool(_PRESS.search(evidence) and (not _PUBLICATION.search(evidence)))


def timing_label(event: Event) -> str:
    return {
        TimingKind.PUBLICATION: "Release",
        TimingKind.PRESS_CONFERENCE: "Press conference",
        TimingKind.INVESTOR_CALL: "Investor call",
        TimingKind.BOARD_APPROVAL: "Board approval; publication unconfirmed",
    }[kind_for(event)]


def kind_for(event: Event) -> TimingKind:
    if event.type == EventType.EARNINGS_CALL:
        return TimingKind.INVESTOR_CALL
    if event.timing is not None:
        return event.timing.kind
    return inferred_kind(event)


def with_legacy_evidence(event: Event) -> Event:
    if event.time_hint and (not (event.timing and event.timing.window_evidence)):
        from earnings_watch.model.events import ExtractClaim
        from .verify import verified_hint

        hint, window = verified_hint(
            ExtractClaim(
                type=event.type,
                period=event.period,
                date=event.date,
                time_hint=event.time_hint,
                evidence=event.evidence,
            )
        )
        event = event.model_copy(update={"time_hint": hint})
        if window:
            basis = event.timing or TimingEvidence(
                kind=inferred_kind(event),
                source_url=event.source_url,
                evidence=event.evidence,
                verified_on=event.last_verified,
            )
            event = event.model_copy(
                update={"timing": basis.model_copy(update={"window_evidence": window})}
            )
    if (
        event.type in {EventType.EARNINGS, EventType.EARNINGS_CALL}
        and (event.release_time or inferred_kind(event) != TimingKind.PUBLICATION)
        and (event.timing is None)
    ):
        event = event.model_copy(
            update={
                "timing": TimingEvidence(
                    kind=kind_for(event),
                    source_url=event.source_url,
                    evidence=event.evidence,
                    verified_on=event.last_verified,
                )
            }
        )
    if event.type == EventType.EARNINGS and kind_for(event) == TimingKind.BOARD_APPROVAL:
        return event.model_copy(
            update={
                "status": EventStatus.EXPECTED
                if event.status != EventStatus.PAST
                else event.status,
                "release_time": None,
                "release_tz": None,
                "time_hint": None,
                "timing_issue": "Publication date unconfirmed; source gives board approval only",
            }
        )
    if event.type == EventType.EARNINGS and call_only(event.evidence) and (not event.release_time):
        return event.model_copy(
            update={
                "status": EventStatus.EXPECTED,
                "time_hint": None,
                "timing_issue": CALL_ONLY_RESULTS,
            }
        )
    return event


def merge_observation(old: Event, incoming: Event) -> Event:
    if old.timing_resolution:
        old = old.model_copy(
            update={"release_time": None, "release_tz": None, "timing_resolution": None}
        )
    if incoming.timing_resolution:
        incoming = incoming.model_copy(
            update={"release_time": None, "release_tz": None, "timing_resolution": None}
        )
    old = with_legacy_evidence(old)
    incoming = with_legacy_evidence(incoming)
    protected_date = old.status == EventStatus.IR_CONFIRMED and incoming.status not in {
        EventStatus.IR_CONFIRMED,
        EventStatus.PAST,
    }
    if incoming.date != old.date:
        if protected_date or (
            old.status == incoming.status == EventStatus.IR_CONFIRMED
            and kind_for(old) == kind_for(incoming)
            and (old.source_url != incoming.source_url)
            and (not explicit_correction(incoming))
        ):
            return old.model_copy(
                update={
                    "timing_issue": DATE_DISAGREEMENT,
                    "timing_disputes": merge_timing_disputes(
                        old.date,
                        old.timing_disputes,
                        tuple((t for t in (old.timing, incoming.timing) if t)),
                    ),
                }
            )
        return incoming
    base = (
        old
        if protected_date
        or (old.status == incoming.status and old.source_url != incoming.source_url)
        else incoming
    )
    if old.type not in {EventType.EARNINGS, EventType.EARNINGS_CALL}:
        if not base.release_time and old.release_time:
            return base.model_copy(
                update={"release_time": old.release_time, "release_tz": old.release_tz}
            )
        return base
    old_kind, new_kind = (kind_for(old), kind_for(incoming))
    updates: dict[str, Any] = {
        "timing_issue": incoming.timing_issue or old.timing_issue,
        "timing_disputes": merge_timing_disputes(
            old.date, old.timing_disputes, incoming.timing_disputes
        ),
    }
    if (
        old.timing_issue
        and old.timing_issue.startswith(UNCONFIRMED_PUBLICATION)
        and (incoming.status == EventStatus.IR_CONFIRMED)
        and (not incoming.timing_issue)
    ):
        updates.update(timing_issue=None, timing_disputes=[])
    if incoming.timing and incoming.timing.withdrawn and (old_kind == new_kind):
        if not old.timing or incoming.timing.source_url == old.timing.source_url:
            return base.model_copy(
                update={
                    "release_time": None,
                    "release_tz": None,
                    "timing": incoming.timing,
                    "time_hint": incoming.time_hint,
                    "timing_issue": incoming.timing_issue,
                    "timing_disputes": [],
                }
            )
    if incoming.release_time:
        if old.release_time and old_kind == TimingKind.PUBLICATION and (new_kind != old_kind):
            updates.update(
                release_time=old.release_time, release_tz=old.release_tz, timing=old.timing
            )
        elif (
            old.release_time
            and old_kind == new_kind
            and (clock_key(old) != clock_key(incoming))
            and old.timing is not None
            and incoming.timing is not None
            and (old.timing.source_url != incoming.timing.source_url)
            and (not explicit_correction(incoming))
        ):
            updates.update(
                release_time=old.release_time,
                release_tz=old.release_tz,
                timing=old.timing,
                timing_issue="Primary sources disagree on the time; retained the previous time for review",
                timing_disputes=merge_timing_disputes(
                    old.date,
                    old.timing_disputes,
                    tuple((t for t in (old.timing, incoming.timing) if t)),
                ),
            )
        else:
            unchanged_clock = (
                clock_key(old) == clock_key(incoming)
                and old_kind == new_kind
                and (not explicit_correction(incoming))
            )
            agreement = merge_timing_disputes(
                old.date,
                old.timing_disputes,
                incoming.timing_disputes,
                tuple((t for t in (old.timing, incoming.timing) if t)),
            )
            updates.update(
                release_time=incoming.release_time,
                release_tz=incoming.release_tz,
                timing=incoming.timing,
                timing_issue=incoming.timing_issue
                or (old.timing_issue if unchanged_clock else None),
                timing_disputes=(agreement if len(agreement) > 1 or old.timing_issue else [])
                if unchanged_clock
                else incoming.timing_disputes,
            )
    elif old.release_time:
        updates.update(release_time=old.release_time, release_tz=old.release_tz, timing=old.timing)
    else:
        updates.update(release_time=None, release_tz=None, timing=incoming.timing or old.timing)
    if (
        old.timing_issue
        and old.timing_disputes
        and (not old.release_time)
        and (not explicit_correction(incoming))
    ):
        updates.update(
            release_time=None,
            release_tz=None,
            timing=old.timing,
            timing_issue=old.timing_issue,
            timing_disputes=merge_timing_disputes(
                old.date,
                old.timing_disputes,
                incoming.timing_disputes,
                tuple((t for t in (incoming.timing,) if t)),
            ),
        )
    if (
        incoming.time_hint is None
        and (not incoming.release_time)
        and old.time_hint
        and (old_kind == new_kind)
        and old.timing
        and old.timing.window_evidence
    ):
        updates.update(time_hint=old.time_hint, timing=old.timing)
    return base.model_copy(update=updates)


def coverage(events: list[Event]) -> dict[str, int]:
    earnings = [e for e in events if e.type == EventType.EARNINGS]
    confirmed = sum((usable_clock(e) and kind_for(e) == TimingKind.PUBLICATION for e in earnings))
    review = sum((bool(e.timing_issue and e.timing_resolution is None) for e in earnings))
    return {"confirmed": confirmed, "unknown": len(earnings) - confirmed - review, "review": review}
