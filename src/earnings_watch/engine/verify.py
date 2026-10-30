from __future__ import annotations
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from earnings_watch.model.limits import PAST_RETIRE_DAYS
from collections.abc import Iterable
from earnings_watch.model.events import (
    Event,
    EventStatus,
    EventType,
    ExtractClaim,
    RejectedClaim,
    TimingEvidence,
    TimingCandidate,
    TimingKind,
)
from earnings_watch.model.sources import Source, canonical_period, event_id
from .timing import (
    board_only,
    clock_key,
    evidence_clock_key,
    is_press_conference,
    kind_for,
    merge_timing_disputes,
    pre_earnings_call,
    timing_withdrawn,
    with_legacy_evidence,
)

logger = logging.getLogger("earnings_watch.verify")
_DATE_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile("\\b(20\\d{2}-\\d{2}-\\d{2})\\b"), "%Y-%m-%d"),
    (re.compile("\\b(\\d{1,2}\\.\\d{1,2}\\.20\\d{2})\\b"), "%d.%m.%Y"),
    (re.compile("\\b(\\d{1,2}/\\d{1,2}/20\\d{2})\\b"), "%d/%m/%Y"),
    (
        re.compile(
            "\\b(\\d{1,2}\\s+(?:January|February|March|April|May|June|July|August|September|October|November|December)\\s+20\\d{2})\\b",
            re.IGNORECASE,
        ),
        "%d %B %Y",
    ),
    (
        re.compile(
            "\\b((?:January|February|March|April|May|June|July|August|September|October|November|December)\\s+\\d{1,2},\\s+20\\d{2})\\b",
            re.IGNORECASE,
        ),
        "%B %d, %Y",
    ),
    (
        re.compile(
            "\\b(\\d{1,2}\\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\\s+20\\d{2})\\b",
            re.IGNORECASE,
        ),
        "%d %b %Y",
    ),
    (
        re.compile(
            "\\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\\s+\\d{1,2},\\s+20\\d{2})\\b",
            re.IGNORECASE,
        ),
        "%b %d, %Y",
    ),
)
_MONTH_NAME = "(?:January|February|March|April|May|June|July|August|September|October|November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
_ORDINAL_DAY_RE = re.compile("\\b(\\d{1,2})(?:st|nd|rd|th)\\b", re.IGNORECASE)
_YEARLESS_DATE_RES: tuple[re.Pattern[str], ...] = (
    re.compile(
        f"\\b(?P<day>\\d{{1,2}})\\s+(?P<month>{_MONTH_NAME})\\b(?!\\s*,?\\s*20\\d{{2}})",
        re.IGNORECASE,
    ),
    re.compile(
        f"\\b(?P<month>{_MONTH_NAME})\\s+(?P<day>\\d{{1,2}})\\b(?!\\s*,?\\s*20\\d{{2}})",
        re.IGNORECASE,
    ),
)
_EVIDENCE_TIME_RE = re.compile(
    "\\b(?:(\\d{1,2})(?::|\\.|(?-i:h))(\\d{2})(?:\\s*(a\\.?\\s?m\\.?|p\\.?\\s?m\\.?))?|(\\d{1,2})\\s*(a\\.?\\s?m\\.?|p\\.?\\s?m\\.?))\\b",
    re.IGNORECASE,
)
CALL_WORD_RE = re.compile(
    "\\b(?:conference\\s+call|call|webcast|(?:management|analysts?|investors?|results)\\s+presentation|(?:results|earnings)\\s+(?:event|briefing)|briefing|Q\\s?&\\s?A)\\b",
    re.IGNORECASE,
)
_PUBLICATION_WORD_RE = re.compile(
    "\\b(?:publish(?:ed|es|ing)?|publication|release[ds]?|report(?:s|ed|ing)?|announc(?:e|ed|es|ement)|distributed|posted|available)\\b",
    re.IGNORECASE,
)
_NON_PUBLICATION_WORD_RE = re.compile(
    "\\b(?:presentation|press\\s+conference|board(?:\\s+of\\s+directors)?|approval|approve[ds]?)\\b",
    re.IGNORECASE,
)
_SUBJECT_WORD_RE = re.compile("\\b(?:results?|earnings|figures)\\b", re.IGNORECASE)
_UNIT_BOUNDARY_RE = re.compile("\\n+|;|(?<=[.!?])(?<![ap]\\.m\\.)\\s+(?=[A-Z#\\[])")


@dataclass(frozen=True)
class VerificationResult:
    events: tuple[Event, ...]
    rejected: tuple[RejectedClaim, ...]
    merged: tuple[ExtractClaim, ...] = ()


def normalize_whitespace(value: str) -> str:
    return " ".join(value.split())


def dates_in_evidence(evidence: str) -> set[date]:
    found: set[date] = set()
    normalized = _ORDINAL_DAY_RE.sub("\\1", normalize_whitespace(evidence))
    normalized = re.sub(
        "\\b(Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)\\.(?=\\s)",
        "\\1",
        normalized,
        flags=re.I,
    )
    for pattern, date_format in _DATE_PATTERNS:
        for match in pattern.finditer(normalized):
            raw = re.sub("\\bSept\\b", "Sep", match.group(1), flags=re.I)
            try:
                parsed = datetime.strptime(raw, date_format).replace(tzinfo=timezone.utc)
                found.add(parsed.date())
            except ValueError:
                continue
    return found


def _loose(value: str) -> str:
    return " ".join(re.sub("[^0-9A-Za-z]+", " ", value).split())


_MARKDOWN_LINK_RE = re.compile('\\[([^\\]\\n]*)\\]\\([^)\\s]*(?:\\s+"[^"]*")?\\)')


def _visible_text(page_text: str) -> str:
    return _MARKDOWN_LINK_RE.sub("\\1", page_text)


def evidence_verifies(event_date: date, evidence: str, page_text: str) -> bool:
    normalized_evidence = normalize_whitespace(evidence)
    if not normalized_evidence:
        return False
    if normalized_evidence not in normalize_whitespace(
        page_text
    ) and normalized_evidence not in normalize_whitespace(_visible_text(page_text)):
        loose_evidence = _loose(evidence)
        if not loose_evidence or loose_evidence not in _loose(_visible_text(page_text)):
            return False
    if event_date in dates_in_evidence(normalized_evidence):
        return True
    return _yearless_dates_match(event_date, normalized_evidence)


def _yearless_dates_match(event_date: date, evidence: str) -> bool:
    normalized = _ORDINAL_DAY_RE.sub("\\1", normalize_whitespace(evidence))
    for pattern in _YEARLESS_DATE_RES:
        for match in pattern.finditer(normalized):
            day = int(match.group("day"))
            month = datetime.strptime(match.group("month")[:3].title(), "%b").month
            if day == event_date.day and month == event_date.month:
                return True
    return False


def _time_matches(evidence: str) -> list[tuple[tuple[int, int], tuple[int, int]]]:
    found: list[tuple[tuple[int, int], tuple[int, int]]] = []
    for match in _EVIDENCE_TIME_RE.finditer(normalize_whitespace(evidence)):
        if match.group(1) is not None:
            hour = int(match.group(1))
            minute = int(match.group(2))
            raw_suffix = match.group(3)
        else:
            hour = int(match.group(4))
            minute = 0
            raw_suffix = match.group(5)
        if hour > 23 or minute > 59:
            continue
        suffix = (raw_suffix or "").lower().replace(".", "").replace(" ", "")
        if suffix == "pm" and hour < 12:
            hour += 12
        elif suffix == "am" and hour == 12:
            hour = 0
        found.append(((hour, minute), match.span()))
    return found


def times_in_evidence(evidence: str) -> set[tuple[int, int]]:
    return {value for value, _span in _time_matches(evidence)}


def _release_time_is_supported(claim: ExtractClaim) -> bool:
    if claim.release_time is None:
        return False
    target = tuple((int(part) for part in claim.release_time.split(":")))
    if claim.type not in {EventType.EARNINGS, EventType.EARNINGS_CALL} or (
        claim.type == EventType.EARNINGS and is_press_conference(claim.evidence)
    ):
        return target in times_in_evidence(claim.evidence)
    units = [
        unit
        for unit in (normalize_whitespace(part) for part in _UNIT_BOUNDARY_RE.split(claim.evidence))
        if unit
    ]
    is_call = claim.type == EventType.EARNINGS_CALL
    for index, unit in enumerate(units):
        for value, span in _time_matches(unit):
            if value == target and _clock_binds(units, index, span, is_call=is_call):
                return True
    return False


def _milestone_patterns(is_call: bool) -> tuple[re.Pattern[str], tuple[re.Pattern[str], ...]]:
    if is_call:
        return (CALL_WORD_RE, (_PUBLICATION_WORD_RE,))
    return (_PUBLICATION_WORD_RE, (CALL_WORD_RE, _NON_PUBLICATION_WORD_RE))


def _clock_binds(units: list[str], index: int, span: tuple[int, int], *, is_call: bool) -> bool:
    own, others = _milestone_patterns(is_call)
    unit = units[index]
    center = (span[0] + span[1]) // 2
    own_distance = min((abs(center - match.start()) for match in own.finditer(unit)), default=None)
    other_distance = min(
        (abs(center - match.start()) for pattern in others for match in pattern.finditer(unit)),
        default=None,
    )
    if own_distance is not None and other_distance is not None:
        return own_distance < other_distance
    if own_distance is not None or other_distance is not None:
        return own_distance is not None
    if _SUBJECT_WORD_RE.search(unit):
        return not is_call
    for neighbour in (index + 1, index - 1):
        if not 0 <= neighbour < len(units):
            continue
        text = units[neighbour]
        has_own = bool(own.search(text))
        has_other = any((pattern.search(text) for pattern in others))
        if has_own or has_other:
            return has_own and (not has_other)
        if _SUBJECT_WORD_RE.search(text):
            return not is_call
    return False


def timing_evidence(
    claim: ExtractClaim,
    url: str,
    today: date,
    *,
    release_time: str | None = None,
    release_tz: str | None = None,
) -> TimingEvidence | None:
    if claim.type not in {EventType.EARNINGS, EventType.EARNINGS_CALL}:
        return None
    inferred = (
        TimingKind.INVESTOR_CALL
        if claim.type == EventType.EARNINGS_CALL
        else TimingKind.BOARD_APPROVAL
        if board_only(claim.evidence)
        else TimingKind.PRESS_CONFERENCE
        if is_press_conference(claim.evidence)
        else TimingKind.PUBLICATION
    )
    if (
        claim.timing_kind is not None
        and claim.timing_kind != inferred
        and (inferred != TimingKind.BOARD_APPROVAL)
    ):
        return None
    return TimingEvidence(
        kind=inferred,
        source_url=url,
        evidence=claim.evidence,
        verified_on=today,
        release_time=release_time,
        release_tz=release_tz,
        withdrawn=timing_withdrawn(claim.evidence),
    )


def verified_hint(claim: ExtractClaim, page: str = ""):
    patterns = {
        "BMO": "\\b(?:before\\s+(?:(?:the\\s+)?market\\s+open(?:ing)?|(?:the\\s+)?opening\\s+of)|pre[-\\s]market)\\b",
        "AMC": "\\b(?:after\\s+(?:(?:the\\s+)?market\\s+clos(?:e|ing)|(?:the\\s+)?close\\s+of)|post[-\\s]market)\\b",
        "DURING": "\\b(?:during\\s+(?:market|trading)\\s+hours)\\b",
    }
    if claim.time_hint is None:
        return (None, None)
    pattern = patterns[claim.time_hint.value]
    if re.search(pattern, claim.evidence, re.I):
        return (claim.time_hint, claim.evidence)
    for sentence in re.split("(?<=[.!?])\\s+|\\n\\n", page):
        if re.search(
            "\\b(?:announcements? of financial results|financial results announcements?)\\b",
            sentence,
            re.I,
        ) and re.search(pattern, sentence, re.I):
            return (claim.time_hint, sentence.strip())
    return (None, None)


def verified_release_time(claim: ExtractClaim) -> tuple[str | None, str | None]:
    if claim.release_time is None or claim.release_tz is None:
        return (None, None)
    local = datetime.fromisoformat(f"{claim.date}T{claim.release_time}").replace(
        tzinfo=ZoneInfo(claim.release_tz)
    )
    if local.utcoffset() != local.replace(fold=1).utcoffset():
        return (None, None)
    if _release_time_is_supported(claim):
        return (claim.release_time, claim.release_tz)
    logger.warning(
        "verify.release_time_stripped",
        extra={
            "extras": {
                "type": claim.type.value,
                "period": claim.period,
                "date": claim.date.isoformat(),
                "release_time": claim.release_time,
                "evidence": claim.evidence,
            }
        },
    )
    return (None, None)


def _verified_candidate(
    parent: ExtractClaim,
    *,
    source: Source,
    today: date,
    source_url: str,
    evidence: str,
    release_time: str,
    release_tz: str,
    document_text: str,
) -> TimingEvidence | None:
    candidate = parent.model_copy(
        update={
            "release_time": release_time,
            "release_tz": release_tz,
            "evidence": evidence,
            "source_url": source_url,
            "timing_candidates": [],
            "timing_conflict": False,
        }
    )
    if not evidence_verifies(candidate.date, candidate.evidence, document_text):
        return None
    verified_time, verified_tz = verified_release_time(candidate)
    if not verified_time or not verified_tz:
        return None
    from .timing_search import seasonal_label, seasonal_note, timezone_supported

    convention = (
        source_url == source.calendar_url
        and source.calendar_timezone == verified_tz
        and source.calendar_timezone_basis
    )
    if not timezone_supported(candidate, document_text) and (not convention):
        return None
    basis = timing_evidence(
        candidate, source_url, today, release_time=verified_time, release_tz=verified_tz
    )
    if basis is None or basis.withdrawn:
        return None
    label = seasonal_label(candidate)
    if label and source.calendar_timezone == verified_tz and source.calendar_timezone_basis:
        timezone_basis = source.calendar_timezone_basis
    elif label:
        timezone_basis = seasonal_note(candidate, label)
    elif convention:
        timezone_basis = source.calendar_timezone_basis or "Primary document timezone"
    else:
        timezone_basis = "Primary document timezone"
    return basis.model_copy(update={"timezone_basis": timezone_basis})


def check_timing_candidates(
    events: list[Event] | tuple[Event, ...], source: Source, documents: dict[str, str]
) -> dict[str, bool]:
    from .timing import timing_candidate_fingerprint

    checks: dict[str, bool] = {}
    for event in events:
        for candidate in [event.timing, event.timing_resolution, *event.timing_disputes]:
            if not candidate or not candidate.release_time or (not candidate.release_tz):
                continue
            document_text = documents.get(candidate.source_url)
            if document_text is None:
                continue
            parent = ExtractClaim(
                type=event.type,
                period=event.period,
                date=event.date,
                timing_kind=candidate.kind,
                evidence=event.evidence,
                source_url=event.source_url,
            )
            verified = _verified_candidate(
                parent,
                source=source,
                today=event.last_verified,
                source_url=candidate.source_url,
                evidence=candidate.evidence,
                release_time=candidate.release_time,
                release_tz=candidate.release_tz,
                document_text=document_text,
            )
            checks[timing_candidate_fingerprint(event.date, candidate)] = verified is not None
    return checks


def _calendar_candidates(claim: ExtractClaim, source: Source, documents: dict[str, str]):
    expected = canonical_period(claim.period, source, claim.type, claim.date)
    for url, text in documents.items():
        for match in re.finditer(
            "^Calendar event: (?P<title>[^\\n]+) on \\d{2} \\w+ 20\\d{2} at (?P<time>\\d{2}:\\d{2}) UTC\\.$",
            text,
            re.M,
        ):
            title = match.group("title")
            period = re.search("\\b(?:H[12]|Q[1-4]|FY|[369]M)\\s*20\\d{2}\\b", title, re.I)
            if not period or claim.date not in dates_in_evidence(match.group()):
                continue
            try:
                actual = canonical_period(period.group(), source, claim.type, claim.date)
            except ValueError:
                continue
            is_call = bool(CALL_WORD_RE.search(title))
            if actual != expected or is_call != (claim.type == EventType.EARNINGS_CALL):
                continue
            yield TimingCandidate(
                release_time=match.group("time"),
                release_tz="UTC",
                source_url=url,
                evidence=match.group(),
            )


def rejection_covered(claim: ExtractClaim, source: Source, held: Iterable[Event]) -> bool:
    held = [event for event in held if event.issuer_key == source.key]
    try:
        period = canonical_period(claim.period, source, claim.type, claim.date)
    except ValueError:
        period = None
    if period is not None and event_id(claim.type, source.key, period) in {e.id for e in held}:
        return True
    if claim.type != EventType.EARNINGS_CALL:
        return False
    results = [e for e in held if e.type == EventType.EARNINGS]
    if period is not None:
        return any((e.id == event_id(EventType.EARNINGS, source.key, period) for e in results))
    return any((e.date == claim.date for e in results))


def verify_claims(
    claims: list[ExtractClaim],
    source: Source,
    page_text: str,
    *,
    today: date,
    documents: dict[str, str] | None = None,
) -> VerificationResult:
    events: list[Event] = []
    rejected: list[RejectedClaim] = []
    merged: list[ExtractClaim] = []
    seen_ids: set[str] = set()
    retire_cutoff = today - timedelta(days=PAST_RETIRE_DAYS)
    ancient = sum((1 for claim in claims if claim.date < retire_cutoff))
    if ancient:
        logger.info(
            "verify.ancient_claims_skipped",
            extra={"extras": {"source": source.key, "count": ancient}},
        )
        claims = [claim for claim in claims if claim.date >= retire_cutoff]
    for claim in claims:
        if claim.type == EventType.EARNINGS_CALL and pre_earnings_call(claim.evidence):
            continue
        document_url = source.calendar_url
        document_text = page_text
        if documents:
            candidates = [
                (url, text)
                for url, text in documents.items()
                if (not claim.source_url or url == claim.source_url)
                and evidence_verifies(claim.date, claim.evidence, text)
            ]
            if candidates:
                document_url, document_text = candidates[0]
            else:
                document_text = ""
        if not evidence_verifies(claim.date, claim.evidence, document_text):
            rejected.append(
                RejectedClaim(
                    claim=claim,
                    reason="evidence is not a verbatim page substring with the claimed date",
                )
            )
            continue
        try:
            period = canonical_period(claim.period, source, claim.type, claim.date)
        except ValueError as exc:
            rejected.append(RejectedClaim(claim=claim, reason=str(exc)))
            continue
        identifier = event_id(claim.type, source.key, period)
        if identifier in seen_ids:
            if claim.type not in {EventType.EARNINGS, EventType.EARNINGS_CALL}:
                merged.append(claim)
                continue
        seen_ids.add(identifier)
        release_time, release_tz = verified_release_time(claim)
        if release_time and claim.type not in {EventType.EARNINGS, EventType.EARNINGS_CALL}:
            from .timing_search import timezone_supported

            convention = (
                document_url == source.calendar_url
                and source.calendar_timezone == release_tz
                and source.calendar_timezone_basis
            )
            if not timezone_supported(claim, document_text) and (not convention):
                release_time, release_tz = (None, None)
        basis = timing_evidence(claim, document_url, today)
        verified_candidates: list[TimingEvidence] = []
        if release_time and release_tz:
            verified = _verified_candidate(
                claim,
                source=source,
                today=today,
                source_url=document_url,
                evidence=claim.evidence,
                release_time=release_time,
                release_tz=release_tz,
                document_text=document_text,
            )
            if verified is not None:
                verified_candidates.append(verified)
            elif claim.type in {EventType.EARNINGS, EventType.EARNINGS_CALL}:
                release_time, release_tz = (None, None)
        for candidate in [
            *claim.timing_candidates,
            *_calendar_candidates(claim, source, documents or {document_url: document_text}),
        ]:
            candidate_text = (
                (documents or {}).get(candidate.source_url)
                if documents
                else document_text
                if candidate.source_url == document_url
                else None
            )
            verified = (
                _verified_candidate(
                    claim,
                    source=source,
                    today=today,
                    source_url=candidate.source_url,
                    evidence=candidate.evidence,
                    release_time=candidate.release_time,
                    release_tz=candidate.release_tz,
                    document_text=candidate_text,
                )
                if candidate_text is not None
                else None
            )
            if verified is None:
                rejected.append(
                    RejectedClaim(
                        claim=claim,
                        reason=f"timing candidate is not verified by its primary document: {candidate.source_url}",
                    )
                )
                continue
            verified_candidates.append(verified)
        verified_candidates = merge_timing_disputes(claim.date, verified_candidates)
        timing_issue = None
        instants = {evidence_clock_key(claim.date, item) for item in verified_candidates}
        if len(instants) > 1:
            release_time, release_tz = (None, None)
            timing_issue = "Conflicting source timings; review required"
        elif verified_candidates:
            selected = verified_candidates[0]
            release_time, release_tz = (selected.release_time, selected.release_tz)
            basis = selected
        elif claim.timing_conflict:
            release_time, release_tz = (None, None)
            timing_issue = "Timing conflict lacks verified candidates; review required"
            rejected.append(
                RejectedClaim(
                    claim=claim, reason="timing conflict did not include verified candidates"
                )
            )
        hint, window_evidence = verified_hint(claim, document_text)
        if basis and window_evidence:
            basis = basis.model_copy(update={"window_evidence": window_evidence})
        if (
            release_time
            and basis is None
            and (claim.type in {EventType.EARNINGS, EventType.EARNINGS_CALL})
        ):
            release_time, release_tz = (None, None)
        item = Event(
            id=identifier,
            issuer_key=source.key,
            ticker=source.ticker,
            type=claim.type,
            period=period,
            date=claim.date,
            time_hint=hint,
            release_time=release_time,
            release_tz=release_tz,
            timing=basis,
            timing_issue=timing_issue,
            timing_disputes=verified_candidates
            if timing_issue or len(verified_candidates) > 1
            else [],
            status=EventStatus.PAST if claim.date < today else EventStatus.IR_CONFIRMED,
            source_url=document_url,
            evidence=claim.evidence,
            first_seen=today,
            last_verified=today,
        )
        previous = next((e for e in events if e.id == identifier), None)
        if previous:
            if kind_for(previous) != kind_for(item):
                priority = {
                    TimingKind.PUBLICATION: 3,
                    TimingKind.PRESS_CONFERENCE: 2,
                    TimingKind.BOARD_APPROVAL: 1,
                }
                item = max((previous, item), key=lambda e: priority.get(kind_for(e), 0))
            elif previous.date != item.date or (
                clock_key(previous) and clock_key(item) and (clock_key(previous) != clock_key(item))
            ):
                item = previous.model_copy(
                    update={
                        "timing_issue": "Current source rows disagree; review required",
                        "timing_disputes": merge_timing_disputes(
                            item.date, tuple((t for t in (previous.timing, item.timing) if t))
                        ),
                    }
                )
            elif item.timing_issue or previous.timing_issue:
                item = item.model_copy(
                    update={
                        "release_time": None,
                        "release_tz": None,
                        "timing_issue": item.timing_issue or previous.timing_issue,
                        "timing_disputes": merge_timing_disputes(
                            item.date,
                            previous.timing_disputes,
                            item.timing_disputes,
                            tuple((t for t in (previous.timing, item.timing) if t)),
                        ),
                    }
                )
            elif (
                not item.release_time
                and previous.release_time
                and (not (item.timing and item.timing.withdrawn))
            ):
                item = previous
            events = [e for e in events if e.id != identifier]
        events.append(item)
    return VerificationResult(
        events=tuple((with_legacy_evidence(e) for e in events)),
        rejected=tuple(rejected),
        merged=tuple(merged),
    )


def reverify_event(event: Event, page_text: str, *, today: date) -> Event | None:
    if not evidence_verifies(event.date, event.evidence, page_text):
        return None
    return with_legacy_evidence(event.model_copy(update={"last_verified": today}))
