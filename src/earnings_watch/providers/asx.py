"""ASX MarkitDigital public announcements discovery adapter."""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from earnings_watch.model.sources import Source
from earnings_watch.transport import Transport

_ASX_TZ = ZoneInfo("Australia/Sydney")
_ASX_TICKER_RE = re.compile(r"^[A-Z0-9]{2,6}$")
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


async def discover_asx(
    transport: Transport, source: Source, *, today: date
) -> tuple[dict[str, str], list[date]]:
    """Query ASX public announcements JSON for recent milestones and historical results dates."""
    ticker = source.ticker.strip().upper().removesuffix(".AX")
    if not _ASX_TICKER_RE.fullmatch(ticker):
        return {}, []
    url = (
        f"https://asx.api.markitdigital.com/asx-research/1.0/companies/"
        f"{quote(ticker, safe='')}/announcements?count=20&market_sensitive=false"
    )
    try:
        response = await transport.get(url)
    except Exception as exc:
        if type(exc).__module__.startswith("httpx"):
            return {}, []
        raise
    if response.status == 304 or not response.body:
        return {}, []
    try:
        payload = json.loads(response.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}, []
    data = payload.get("data") if isinstance(payload, dict) else None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return {}, []

    lines: list[str] = []
    historical: set[date] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        headline = " ".join(str(item.get("headline") or "").split())
        ann_date = _parse_iso_date(item.get("date"))
        if not headline or ann_date is None:
            continue
        if ann_date <= today and _ASX_RESULTS_RE.search(headline):
            historical.add(ann_date)
        if _ASX_MILESTONE_RE.search(headline):
            lines.append(f"{ann_date.isoformat()} — {headline}")

    documents = {url: "\n".join(lines)} if lines else {}
    return documents, sorted(historical)
