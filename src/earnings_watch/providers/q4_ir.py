"""Q4 Inc investor-relations JSON feed discovery adapter."""

from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

from earnings_watch.model.sources import Source
from earnings_watch.model.urls import public_url
from earnings_watch.transport import Transport

_PR_HEADLINE_RE = re.compile(
    r"\b(?:earnings|results?|dividend|annual\s+(?:general\s+)?meeting|agm|egm|"
    r"investor\s+day|analyst\s+day|conference|capital\s+markets?\s+day|trading\s+update)\b",
    re.I,
)
_EARNINGS_EVENT_RE = re.compile(
    r"\b(?:earnings|results?|quarter|q[1-4]|[1-4]q|half[- ]?year|full[- ]?year|"
    r"annual\s+results?|interim|fiscal|financial\s+results?)\b",
    re.I,
)
_MS_DATE_RE = re.compile(r"/Date\((-?\d+)([+-]\d{4})?\)/")
_MAX_PR_CHARS = 20000
_PR_LOOKBACK_DAYS = 60


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {"script", "style"}:
            self._skip += 1
        elif tag.lower() in {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4"}:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"script", "style"} and self._skip > 0:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if self._skip == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = html.unescape("".join(self._parts))
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    parser = _TextExtractor()
    parser.feed(raw)
    return parser.text()


def is_q4_source(source: Source, page_text: str = "") -> bool:
    """Detect Q4 Inc investor relations sites from URL or rendered page text."""
    return "default.aspx" in source.calendar_url.lower() or (
        "powered by q4 inc" in page_text.lower()
    )


def _parse_q4_datetime(raw: str | None) -> tuple[date, str | None, str | None] | None:
    if not raw or not isinstance(raw, str):
        return None
    value = " ".join(raw.strip().split())
    if not value:
        return None
    ms_match = _MS_DATE_RE.search(value)
    if ms_match:
        offset_raw = ms_match.group(2)
        if offset_raw:
            sign = 1 if offset_raw[0] == "+" else -1
            hours = int(offset_raw[1:3])
            minutes = int(offset_raw[3:5])
            if hours > 23 or minutes > 59:
                return None
            tz = timezone(timedelta(minutes=sign * (hours * 60 + minutes)))
            inferred_tz: str | None = None
        else:
            tz = timezone.utc
            inferred_tz = "UTC"
        try:
            dt = datetime.fromtimestamp(int(ms_match.group(1)) / 1000, tz=tz)
        except (OverflowError, OSError, ValueError):
            return None
        time_str = dt.strftime("%H:%M") if (dt.hour or dt.minute) else None
        return dt.date(), time_str, inferred_tz
    iso_candidate = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(iso_candidate)
        has_time = "T" in value or " " in value
        time_str = dt.strftime("%H:%M") if (has_time and (dt.hour or dt.minute)) else None
        return dt.date(), time_str, None
    except ValueError:
        pass
    for fmt, has_time in (
        ("%m/%d/%Y %H:%M:%S", True),
        ("%m/%d/%Y %H:%M", True),
        ("%m/%d/%Y %I:%M:%S %p", True),
        ("%m/%d/%Y %I:%M %p", True),
        ("%m/%d/%Y", False),
        ("%B %d, %Y %I:%M %p", True),
        ("%B %d, %Y", False),
        ("%b %d, %Y", False),
    ):
        try:
            dt = datetime.strptime(value, fmt)
            time_str = dt.strftime("%H:%M") if (has_time and (dt.hour or dt.minute)) else None
            return dt.date(), time_str, None
        except ValueError:
            continue
    return None


def _format_event_line(item: dict) -> tuple[date, str] | None:
    title = " ".join(html.unescape(str(item.get("Title") or "")).split())
    parsed = _parse_q4_datetime(item.get("StartDate"))
    if not title or parsed is None:
        return None
    event_date, time_str, inferred_tz = parsed
    tz = inferred_tz or " ".join(str(item.get("TimeZone") or "").split())
    when = event_date.isoformat()
    if time_str:
        when = f"{when} {time_str}"
        if tz:
            when = f"{when} {tz}"
    elif tz:
        when = f"{when} ({tz})"
    return event_date, f"{title} — {when}"


def _same_host_url(base: str, host: str, link: str | None) -> str | None:
    if not link or not isinstance(link, str):
        return None
    try:
        resolved = public_url(urljoin(base, link.strip()))
    except ValueError:
        return None
    if (urlsplit(resolved).hostname or "").lower() != host.lower():
        return None
    return resolved


async def _fetch_json(transport: Transport, url: str) -> dict | None:
    try:
        response = await transport.get(url)
    except Exception as exc:
        if type(exc).__module__.startswith("httpx"):
            return None
        raise
    if response.status == 304 or not response.body:
        return None
    try:
        payload = json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


async def discover_q4(
    transport: Transport, source: Source, *, today: date
) -> tuple[dict[str, str], list[date]]:
    """Discover Q4 Inc upcoming/recent events and relevant press releases."""
    host = urlsplit(source.calendar_url).hostname
    if not host:
        return {}, []
    base = f"https://{host}"
    documents: dict[str, str] = {}
    historical: set[date] = set()

    upcoming_url = f"{base}/feed/Event.svc/GetEventList?" + urlencode(
        {
            "LanguageId": 1,
            "eventSelection": 1,
            "includeTags": "true",
            "year": -1,
            "pageSize": 100,
            "pageNumber": 0,
            "sortOperator": 1,
            "pressReleaseDateFilter": 1,
        }
    )
    upcoming_payload = await _fetch_json(transport, upcoming_url)
    if upcoming_payload:
        upcoming_lines: list[str] = []
        for item in upcoming_payload.get("GetEventListResult") or []:
            if not isinstance(item, dict):
                continue
            formatted = _format_event_line(item)
            if formatted is None:
                continue
            event_date, line = formatted
            upcoming_lines.append(line)
            if event_date < today and _EARNINGS_EVENT_RE.search(str(item.get("Title") or "")):
                historical.add(event_date)
        if upcoming_lines:
            documents[upcoming_url] = "\n".join(upcoming_lines)

    for year in (today.year, today.year - 1):
        past_url = f"{base}/feed/Event.svc/GetEventList?" + urlencode(
            {
                "LanguageId": 1,
                "eventSelection": 0,
                "includeTags": "true",
                "year": year,
                "pageSize": 100,
                "pageNumber": 0,
                "sortOperator": 1,
                "pressReleaseDateFilter": 3,
            }
        )
        past_payload = await _fetch_json(transport, past_url)
        if not past_payload:
            continue
        for item in past_payload.get("GetEventListResult") or []:
            if not isinstance(item, dict):
                continue
            title = " ".join(html.unescape(str(item.get("Title") or "")).split())
            parsed = _parse_q4_datetime(item.get("StartDate"))
            if parsed is None:
                continue
            event_date, _, _ = parsed
            tags = " ".join(str(t) for t in (item.get("TagsList") or []))
            if event_date < today and _EARNINGS_EVENT_RE.search(f"{title} {tags}"):
                historical.add(event_date)

    pr_cutoff = today - timedelta(days=_PR_LOOKBACK_DAYS)
    pr_years = sorted({today.year, pr_cutoff.year}, reverse=True)
    for year in pr_years:
        pr_url = f"{base}/feed/PressRelease.svc/GetPressReleaseList?" + urlencode(
            {
                "LanguageId": 1,
                "bodyType": 2,
                "pressReleaseDateFilter": 3,
                "pageSize": 50,
                "pageNumber": 0,
                "includeTags": "true",
                "year": year,
            }
        )
        pr_payload = await _fetch_json(transport, pr_url)
        if not pr_payload:
            continue
        for item in pr_payload.get("GetPressReleaseListResult") or []:
            if not isinstance(item, dict):
                continue
            headline = " ".join(html.unescape(str(item.get("Headline") or "")).split())
            if not headline or not _PR_HEADLINE_RE.search(headline):
                continue
            parsed = _parse_q4_datetime(item.get("PressReleaseDate"))
            if parsed is None:
                continue
            pr_date, time_str, _ = parsed
            if pr_date < pr_cutoff or pr_date > today + timedelta(days=1):
                continue
            body_text = _strip_html(str(item.get("Body") or ""))[:_MAX_PR_CHARS]
            header = f"{headline} ({pr_date.isoformat()}{f' {time_str}' if time_str else ''})"
            doc_text = f"{header}\n{body_text}".strip()
            detail_url = _same_host_url(base, host, item.get("LinkToDetailPage")) or pr_url
            if detail_url in documents:
                documents[detail_url] = f"{documents[detail_url]}\n\n{doc_text}"
            else:
                documents[detail_url] = doc_text

    return documents, sorted(historical)
