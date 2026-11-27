"""ASX public announcements discovery adapter (MarkitDigital JSON + ASX annual HTML fallback)."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from html.parser import HTMLParser
from urllib.parse import quote
from zoneinfo import ZoneInfo

from earnings_watch.model.sources import Source
from earnings_watch.transport import Transport

_ASX_TZ = ZoneInfo("Australia/Sydney")
_ASX_TICKER_RE = re.compile(r"^[A-Z0-9]{2,6}$")
_ASX_DMY_RE = re.compile(r"\b(\d{2})/(\d{2})/(20\d{2})\b")
_ASX_RESULTS_RE = re.compile(
    r"\b(?:half[- ]?year|full[- ]?year|annual|interim|preliminary|financial)\s+results?\b"
    r"|\bappendix\s+4[de]\b",
    re.I,
)
_ASX_MILESTONE_RE = re.compile(
    r"\b(?:results?|appendix\s+4[de]|dividend|distribution|annual\s+general\s+meeting|"
    r"agm|egm|investor\s+day|trading\s+update|guidance|key\s+dates?)\b",
    re.I,
)


class _AsxHtmlTableParser(HTMLParser):
    """Extract (date, headline) pairs from ASX v2 statistics/announcements.do HTML tables."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[tuple[date, str]] = []
        self._in_tr = False
        self._td_idx = -1
        self._in_td = False
        self._in_headline_link = False
        self._stop_headline = False
        self._date_chunks: list[str] = []
        self._headline_chunks: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        t = tag.lower()
        if t == "tr":
            self._in_tr = True
            self._td_idx = -1
            self._date_chunks = []
            self._headline_chunks = []
        elif self._in_tr and t == "td":
            self._td_idx += 1
            self._in_td = True
            self._in_headline_link = False
            self._stop_headline = False
        elif self._in_td and self._td_idx == 2 and t == "a" and not self._headline_chunks:
            self._in_headline_link = True
            self._stop_headline = False
        elif self._in_headline_link and t in {"br", "span"}:
            self._stop_headline = True

    def handle_endtag(self, tag: str) -> None:
        t = tag.lower()
        if t == "a":
            self._in_headline_link = False
        elif t == "td":
            self._in_td = False
            self._in_headline_link = False
        elif t == "tr" and self._in_tr:
            self._in_tr = False
            date_text = " ".join(self._date_chunks)
            headline = " ".join("".join(self._headline_chunks).split())
            m = _ASX_DMY_RE.search(date_text)
            if m and headline:
                try:
                    parsed = date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
                except ValueError:
                    parsed = None
                if parsed is not None:
                    self.rows.append((parsed, headline))

    def handle_data(self, data: str) -> None:
        if not self._in_td:
            return
        if self._td_idx == 0:
            self._date_chunks.append(data)
        elif self._td_idx == 2 and self._in_headline_link and not self._stop_headline:
            self._headline_chunks.append(data)


def _parse_iso_date(raw: str | None) -> date | None:
    if not raw or not isinstance(raw, str):
        return None
    try:
        dt = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(_ASX_TZ).date()


def _parse_asx_html_announcements(raw_html: str) -> list[tuple[date, str]]:
    parser = _AsxHtmlTableParser()
    parser.feed(raw_html)
    parser.close()
    return parser.rows


def _collect_entries(
    entries: list[tuple[date, str]], *, today: date
) -> tuple[list[str], set[date]]:
    lines: list[str] = []
    historical: set[date] = set()
    for ann_date, headline in entries:
        if ann_date <= today and _ASX_RESULTS_RE.search(headline):
            historical.add(ann_date)
        if _ASX_MILESTONE_RE.search(headline):
            lines.append(f"{ann_date.isoformat()} — {headline}")
    return lines, historical


async def _fetch_asx_annual_html(
    transport: Transport, ticker: str, year: int, *, today: date
) -> tuple[str, list[str], set[date]]:
    url = (
        f"https://www.asx.com.au/asx/v2/statistics/announcements.do"
        f"?by=asxCode&asxCode={quote(ticker, safe='')}&timeframe=Y&year={year}"
    )
    try:
        response = await transport.get(url)
    except Exception as exc:
        if type(exc).__module__.startswith("httpx"):
            return url, [], set()
        raise
    if response.status == 304 or not response.body:
        return url, [], set()
    text = response.body.decode("utf-8", "replace")
    entries = _parse_asx_html_announcements(text)
    lines, historical = _collect_entries(entries, today=today)
    return url, lines, historical


async def discover_asx(
    transport: Transport, source: Source, *, today: date
) -> tuple[dict[str, str], list[date]]:
    """Query ASX public announcements for recent milestones and historical results dates.

    MarkitDigital's unauthenticated JSON endpoint caps responses at 5 recent items, so
    issuers with frequent routine filings (e.g. daily buy-backs) lose older results dates.
    When MarkitDigital yields no historical results, fall back to ASX's full-year HTML
    announcements table (current year, then prior year if needed).
    """
    ticker = source.ticker.strip().upper().removesuffix(".AX")
    if not _ASX_TICKER_RE.fullmatch(ticker):
        return {}, []
    url = (
        f"https://asx.api.markitdigital.com/asx-research/1.0/companies/"
        f"{quote(ticker, safe='')}/announcements?count=20&market_sensitive=false"
    )
    documents: dict[str, str] = {}
    historical: set[date] = set()
    try:
        response = await transport.get(url)
    except Exception as exc:
        if not type(exc).__module__.startswith("httpx"):
            raise
        response = None
    if response is not None and response.status != 304 and response.body:
        try:
            payload = json.loads(response.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = None
        data = payload.get("data") if isinstance(payload, dict) else None
        items = data.get("items") if isinstance(data, dict) else None
        if isinstance(items, list):
            entries: list[tuple[date, str]] = []
            for item in items:
                if not isinstance(item, dict):
                    continue
                headline = " ".join(str(item.get("headline") or "").split())
                ann_date = _parse_iso_date(item.get("date"))
                if headline and ann_date is not None:
                    entries.append((ann_date, headline))
            lines, hist = _collect_entries(entries, today=today)
            if lines:
                documents[url] = "\n".join(lines)
            historical.update(hist)

    if not historical:
        for year in (today.year, today.year - 1):
            html_url, html_lines, html_hist = await _fetch_asx_annual_html(
                transport, ticker, year, today=today
            )
            if html_lines:
                documents[html_url] = "\n".join(html_lines)
            historical.update(html_hist)
            if historical:
                break

    return documents, sorted(historical)
