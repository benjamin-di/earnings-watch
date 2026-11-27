from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import html2text

from earnings_watch.model.urls import public_url
from earnings_watch.providers.pdf import extract_pdf_text
from earnings_watch.transport import Transport
from .base import Capture

_MAX_TEXT_CHARS = 150000
_MAX_ICS_LINKS = 5
_MAX_JSONLD_DEPTH = 16
_MAX_JSONLD_OBJECTS = 200
_WINDOWS_TZIDS: dict[str, str] = {
    "eastern standard time": "America/New_York",
    "central standard time": "America/Chicago",
    "mountain standard time": "America/Denver",
    "pacific standard time": "America/Los_Angeles",
    "gmt standard time": "Europe/London",
    "w. europe standard time": "Europe/Berlin",
    "central european standard time": "Europe/Warsaw",
}
_JSONLD_SCRIPT_RE = re.compile(
    r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.I | re.S,
)


class Links(HTMLParser):
    def __init__(self, url: str):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.links: list[dict[str, str]] = []
        self.current: str | None = None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.current = dict(attrs).get("href")
            self.text = []

    def handle_data(self, data):
        if self.current:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.current:
            try:
                self.links.append(
                    {
                        "url": public_url(urljoin(self.url, self.current)),
                        "title": " ".join(self.text).strip()[:500],
                    }
                )
            except ValueError:
                pass
            self.current = None


def _iter_jsonld_objects(
    node: object,
    *,
    _depth: int = 0,
    _counter: list[int] | None = None,
):
    if _counter is None:
        _counter = [0]
    if _depth > _MAX_JSONLD_DEPTH or _counter[0] >= _MAX_JSONLD_OBJECTS:
        return
    if isinstance(node, dict):
        _counter[0] += 1
        yield node
        graph = node.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                if _counter[0] >= _MAX_JSONLD_OBJECTS:
                    return
                yield from _iter_jsonld_objects(item, _depth=_depth + 1, _counter=_counter)
    elif isinstance(node, list):
        for item in node:
            if _counter[0] >= _MAX_JSONLD_OBJECTS:
                return
            yield from _iter_jsonld_objects(item, _depth=_depth + 1, _counter=_counter)


def _is_jsonld_event(obj: dict) -> bool:
    kind = obj.get("@type")
    if isinstance(kind, str):
        return kind == "Event"
    if isinstance(kind, list):
        return "Event" in kind
    return False


def _extract_jsonld_events(body: str) -> list[str]:
    lines: list[str] = []
    seen: set[str] = set()
    for match in _JSONLD_SCRIPT_RE.finditer(body):
        raw = match.group(1).strip()
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, RecursionError, ValueError):
            continue
        for obj in _iter_jsonld_objects(parsed):
            if not _is_jsonld_event(obj):
                continue
            name = " ".join(str(obj.get("name") or "").split())
            start_raw = str(obj.get("startDate") or "").strip()
            if not name or not start_raw:
                continue
            date_match = re.match(
                r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}:\d{2})(?::\d{2}(?:\.\d+)?)?(.*))?$",
                start_raw,
            )
            if not date_match:
                continue
            day = date_match.group(1)
            time_part = date_match.group(2)
            tz_part = (date_match.group(3) or "").strip()
            if time_part:
                tz_label = " UTC" if tz_part.upper() == "Z" else (f" {tz_part}" if tz_part else "")
                line = f"Structured event: {name} on {day} at {time_part}{tz_label}."
            else:
                line = f"Structured event: {name} on {day}."
            if line not in seen:
                seen.add(line)
                lines.append(line)
    return lines


def _unescape_ics(value: str) -> str:
    return (
        value.replace("\\n", " ")
        .replace("\\N", " ")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _parse_ics_dtstart(params: str, value: str) -> tuple[datetime, bool] | None:
    raw = value.strip()
    is_date_only = bool(re.search(r"VALUE=DATE\b", params, re.I)) and not bool(
        re.search(r"VALUE=DATE-TIME\b", params, re.I)
    )
    if re.fullmatch(r"\d{8}", raw) or is_date_only:
        date_token = raw[:8]
        if not re.fullmatch(r"\d{8}", date_token):
            return None
        try:
            return datetime.strptime(date_token, "%Y%m%d").replace(tzinfo=timezone.utc), False
        except ValueError:
            return None
    if re.fullmatch(r"\d{8}T\d{6}Z", raw):
        try:
            return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc), True
        except ValueError:
            return None
    if re.fullmatch(r"\d{8}T\d{6}", raw):
        try:
            dt = datetime.strptime(raw, "%Y%m%dT%H%M%S")
        except ValueError:
            return None
        tzid_match = re.search(r"TZID=([^;:]+)", params, re.I)
        if tzid_match:
            tz_name = tzid_match.group(1).strip().strip('"')
            mapped = _WINDOWS_TZIDS.get(tz_name.lower(), tz_name)
            try:
                tz = ZoneInfo(mapped)
                return dt.replace(tzinfo=tz).astimezone(timezone.utc), True
            except (ZoneInfoNotFoundError, ValueError):
                return dt.replace(tzinfo=timezone.utc), False
        return dt.replace(tzinfo=timezone.utc), False
    return None


def _parse_ics_events(raw_bytes: bytes) -> list[str]:
    text = raw_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    unfolded = re.sub(r"\n[ \t]", "", text)
    lines: list[str] = []
    in_event = False
    summary = ""
    dtstart: tuple[datetime, bool] | None = None
    for raw_line in unfolded.splitlines():
        line = raw_line.strip()
        upper = line.upper()
        if upper == "BEGIN:VEVENT":
            in_event = True
            summary = ""
            dtstart = None
            continue
        if upper == "END:VEVENT":
            if in_event and summary and dtstart is not None:
                dt, has_utc_time = dtstart
                clean_summary = " ".join(_unescape_ics(summary).split())
                if has_utc_time:
                    lines.append(
                        f"Calendar event: {clean_summary} on {dt:%d %b %Y} at {dt:%H:%M} UTC."
                    )
                else:
                    lines.append(f"Calendar event: {clean_summary} on {dt:%d %b %Y}.")
            in_event = False
            continue
        if not in_event or ":" not in line:
            continue
        key_part, value = line.split(":", 1)
        prop_name = key_part.split(";", 1)[0].upper()
        if prop_name == "SUMMARY":
            summary = value
        elif prop_name == "DTSTART":
            dtstart = _parse_ics_dtstart(key_part, value)
    return lines


def _same_host_ics_urls(page_url: str, links: list[dict[str, str]]) -> list[str]:
    page_host = (urlsplit(page_url).hostname or "").lower()
    urls: list[str] = []
    seen: set[str] = set()
    for link in links:
        candidate = link.get("url", "")
        parsed = urlsplit(candidate)
        if (
            (parsed.hostname or "").lower() == page_host
            and parsed.path.lower().endswith(".ics")
            and candidate not in seen
        ):
            seen.add(candidate)
            urls.append(candidate)
            if len(urls) >= _MAX_ICS_LINKS:
                break
    return urls


async def capture(transport: Transport, url: str) -> Capture:
    response = await transport.get(url)
    if response.status == 304:
        return Capture(response.url, "")
    content_type = response.content_type.lower()
    if "application/pdf" in content_type:
        text = extract_pdf_text(response.body, max_pages=5, max_chars=_MAX_TEXT_CHARS)
        return Capture(response.url, text, ())
    if "html" not in content_type and not content_type.startswith("text/"):
        raise ValueError("calendar page is not text or HTML")
    body = response.body.decode("utf-8", errors="replace")
    jsonld_lines = _extract_jsonld_events(body)
    parser = Links(response.url)
    parser.feed(body)
    converter = html2text.HTML2Text()
    converter.ignore_images, converter.body_width = True, 0
    text = converter.handle(body)
    if len(text) > _MAX_TEXT_CHARS:
        raise ValueError("calendar exceeds the extraction budget")

    ics_lines: list[str] = []
    for ics_url in _same_host_ics_urls(response.url, parser.links):
        try:
            ics_resp = await transport.get(ics_url)
            if ics_resp.status != 304 and ics_resp.body:
                ics_lines.extend(_parse_ics_events(ics_resp.body))
        except Exception as exc:
            if type(exc).__module__.startswith("httpx") or isinstance(exc, ValueError):
                continue
            raise

    extras = [*jsonld_lines, *ics_lines]
    if extras:
        extra_block = "\n".join(extras)
        remaining = _MAX_TEXT_CHARS - len(text) - 2
        if remaining > 0:
            text = f"{text}\n\n{extra_block[:remaining]}"

    return Capture(response.url, text, tuple(parser.links[:1000]))
