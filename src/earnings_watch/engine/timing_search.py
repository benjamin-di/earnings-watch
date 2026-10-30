from __future__ import annotations
from datetime import datetime
import re
from zoneinfo import ZoneInfo
from earnings_watch.model.events import ExtractClaim, EventType
from earnings_watch.model.sources import Source
from .verify import CALL_WORD_RE, evidence_verifies, normalize_whitespace, _time_matches

_ZONE_EXPRESSION = "(?:[A-Za-z_]+/[A-Za-z_/]+|(?:Eastern|Pacific|Mountain|Central|UK|London|Lisbon|Portugal|Portuguese|Hong\\s+Kong|Astana|Kazakhstan)(?:\\s+time)?|[A-Z][a-z]+(?:\\s+[A-Z][a-z]+)?\\s+[Tt]ime|[A-Z]{2,5})"


def _clock_zones(claim: ExtractClaim) -> list[str] | None:
    evidence = normalize_whitespace(claim.evidence)
    evidence = re.sub("(\\*{1,2}|__)(?=\\S)(.+?)(?<=\\S)\\1", "\\2", evidence)
    readings = _time_matches(evidence)
    target = tuple((int(part) for part in (claim.release_time or "").split(":")))
    labels: list[str] = []
    for index, (value, span) in enumerate(readings):
        if value != target:
            continue
        end = readings[index + 1][1][0] if index + 1 < len(readings) else len(evidence)
        zone = re.match("\\.?\\s*[\\[(]?\\s*(" + _ZONE_EXPRESSION + ")\\b", evidence[span[1] : end])
        if zone:
            labels.append(zone.group(1))
        if len(readings) == 1:
            leading = re.search("\\b(" + _ZONE_EXPRESSION + ")\\s*[:(]?\\s*$", evidence[: span[0]])
            if leading:
                labels.append(leading.group(1))
    return labels or None


def timezone_supported(claim: ExtractClaim, page: str) -> bool:
    if not claim.release_time or not claim.release_tz:
        return False
    labels = _clock_zones(claim)
    if labels is not None:
        return any((_zone_supported(claim, label) for label in labels))
    return any(
        (
            _zone_supported(claim, match.group(1))
            for match in re.finditer(
                "\\bAll\\s+times\\s+(?:are\\s+)?(?:in\\s+)?([^\\n.!?]+)", page, re.I
            )
        )
    )


def _seasonal_abbreviations(zone: str, year: int) -> set[str]:
    names = {datetime(year, month, 15, 12, tzinfo=ZoneInfo(zone)).tzname() for month in (1, 7)}
    return {name for name in names if name and (not name.startswith(("+", "-")))}


def seasonal_label(claim: ExtractClaim) -> str | None:
    if not claim.release_time or not claim.release_tz:
        return None
    labels = _clock_zones(claim) or []
    actual = _local_instant(claim).tzname()
    seasonal = _seasonal_abbreviations(claim.release_tz, claim.date.year)
    mismatched = [label for label in labels if label in seasonal and label != actual]
    if mismatched and (
        not any((_zone_supported(claim, label, seasonal=False) for label in labels))
    ):
        return mismatched[0]
    return None


SEASONAL_NOTE_PREFIX = "Source prints "


def seasonal_note(claim: ExtractClaim, label: str) -> str:
    place = (claim.release_tz or "").rsplit("/", 1)[-1].replace("_", " ")
    actual = _local_instant(claim).tzname()
    return f"{SEASONAL_NOTE_PREFIX}{label}; read as {place} local time ({actual} on {claim.date.day} {claim.date:%b})"


def _local_instant(claim: ExtractClaim) -> datetime:
    return datetime.fromisoformat(f"{claim.date}T{claim.release_time}").replace(
        tzinfo=ZoneInfo(claim.release_tz or "UTC")
    )


def _zone_supported(claim: ExtractClaim, page: str, *, seasonal: bool = True) -> bool:
    if not claim.release_time or not claim.release_tz:
        return False
    zone = claim.release_tz
    if zone.casefold() in page.casefold():
        return True
    aliases = {
        "America/New_York": "\\b(?:ET|Eastern\\s+time)\\b",
        "America/Los_Angeles": "\\b(?:PT|Pacific\\s+time)\\b",
        "America/Denver": "\\b(?:MT|Mountain\\s+time)\\b",
        "America/Chicago": "\\b(?:CT|Central\\s+time)\\b",
        "Europe/London": "\\b(?:UK|London)(?:\\s+time)?\\b",
        "Europe/Lisbon": "\\b(?:Lisbon|Portugal|Portuguese)\\s+time\\b",
        "Asia/Hong_Kong": "\\b(?:HKT|Hong\\s+Kong\\s+time)\\b",
        "Asia/Almaty": "\\b(?:Astana|Kazakhstan)(?:\\s+time)?\\b",
    }
    if zone in aliases and re.search(aliases[zone], page, re.I):
        return True
    city = zone.rsplit("/", 1)[-1].replace("_", " ")
    if re.search("\\b" + re.escape(city) + "\\s+time\\b", page, re.I):
        return True
    names = {_local_instant(claim).tzname()}
    if seasonal:
        names |= _seasonal_abbreviations(zone, claim.date.year)
    return any(
        (
            name
            and (not name.startswith(("+", "-")))
            and re.search("\\b" + re.escape(name) + "\\b", page)
            for name in names
        )
    )


def verify_announcement(claim: ExtractClaim, page: str, source: Source | None = None) -> None:
    if not evidence_verifies(claim.date, claim.evidence, page):
        raise ValueError("timing evidence does not match the primary announcement and date")
    if claim.type == EventType.EARNINGS_CALL and (not CALL_WORD_RE.search(claim.evidence)):
        raise ValueError("the announcement does not identify an investor call")
    convention = bool(
        source
        and source.calendar_timezone_basis
        and (claim.source_url == source.calendar_url)
        and (claim.release_tz == source.calendar_timezone)
    )
    if claim.release_time and (not timezone_supported(claim, page)) and (not convention):
        raise ValueError("timing timezone is not supported by the announcement")
