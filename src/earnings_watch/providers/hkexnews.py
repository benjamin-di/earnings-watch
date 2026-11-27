"""HKEXnews deterministic discovery adapter for board meetings, AGMs, dividends, and results."""

from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timedelta
from urllib.parse import parse_qs, urljoin, urlsplit

from earnings_watch.model.sources import Source
from earnings_watch.model.urls import public_url
from earnings_watch.providers.pdf import extract_pdf_text
from earnings_watch.transport import Transport

_HKEX_LOOKBACK_DAYS = 180
_MAX_PDFS_PER_SWEEP = 5
_HKEX_RESULTS_RE = re.compile(r"\[(?:final|interim|quarterly|annual)\s+results\]", re.I)
_HKEX_FORWARD_RE = re.compile(
    r"\[(?:date\s+of\s+board\s+meeting|"
    r"notice\s+of\s+(?:agm|egm|annual\s+general\s+meeting|extraordinary\s+general\s+meeting)|"
    r"[^\]]*(?:dividend|closure\s+of\s+books)[^\]]*)\]",
    re.I,
)


def _hkex_code(source: Source) -> str | None:
    if source.exchange not in {None, "XHKG"}:
        return None
    return source.ticker.zfill(5) if re.fullmatch(r"\d{1,5}", source.ticker) else None


def _parse_hkex_date(raw: str) -> date | None:
    value = raw.strip()
    for fmt in ("%d/%m/%Y %H:%M", "%d/%m/%Y"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


async def _resolve_stock_id(transport: Transport, source: Source) -> str | None:
    if source.hkex_stock_id:
        return source.hkex_stock_id
    for url in (source.calendar_url, source.announcements_url or ""):
        if "hkexnews.hk" in url:
            values = parse_qs(urlsplit(url).query).get("stockId")
            if values and values[0].isdigit():
                return values[0]
    code = _hkex_code(source)
    if code is None:
        return None
    resp = await transport.get(
        f"https://www1.hkexnews.hk/search/prefix.do?callback=callback&lang=EN&type=A&name={code}&market=SEHK"
    )
    payload = resp.body.decode("utf-8", errors="replace")
    start = payload.find("(")
    end = payload.rfind(")")
    if start < 0 or end <= start:
        return None
    try:
        stocks = json.loads(payload[start + 1 : end]).get("stockInfo", [])
    except json.JSONDecodeError:
        return None
    return next((str(item["stockId"]) for item in stocks if item.get("code") == code), None)


async def discover_hkex(
    transport: Transport, source: Source, *, today: date
) -> tuple[dict[str, str], list[date]]:
    """Query HKEXnews for forward milestone PDFs and historical results dates."""
    stock_id = await _resolve_stock_id(transport, source)
    if not stock_id:
        return {}, []
    from_date = today - timedelta(days=_HKEX_LOOKBACK_DAYS)
    search_url = (
        "https://www1.hkexnews.hk/search/titleSearchServlet.do?"
        f"sortDir=0&sortByOptions=DateTime&category=0&market=SEHK&stockId={stock_id}"
        f"&documentType=-1&fromDate={from_date:%Y%m%d}&toDate={today:%Y%m%d}"
        "&title=&searchType=0&t1code=-2&t2Gcode=-2&t2code=-2&rowRange=100&lang=E"
    )
    response = await transport.get(search_url)
    rows = json.loads(json.loads(response.body).get("result") or "[]")

    documents: dict[str, str] = {}
    historical: set[date] = set()
    pdf_count = 0

    for row in rows:
        if not isinstance(row, dict):
            continue
        long_text = html.unescape(str(row.get("LONG_TEXT") or ""))
        title = " ".join(html.unescape(str(row.get("TITLE") or "")).split())
        row_date = _parse_hkex_date(str(row.get("DATE_TIME") or ""))

        if row_date is not None and row_date <= today and _HKEX_RESULTS_RE.search(long_text):
            historical.add(row_date)

        if pdf_count >= _MAX_PDFS_PER_SWEEP:
            continue
        if not _HKEX_FORWARD_RE.search(long_text):
            continue
        file_link = str(row.get("FILE_LINK") or "").strip()
        if not file_link:
            continue
        try:
            pdf_url = public_url(urljoin("https://www1.hkexnews.hk/", file_link))
        except ValueError:
            continue
        if (urlsplit(pdf_url).hostname or "").lower() != "www1.hkexnews.hk":
            continue
        if pdf_url in documents:
            continue
        pdf_count += 1
        try:
            pdf_response = await transport.get(pdf_url)
            extracted = extract_pdf_text(pdf_response.body, max_pages=5, max_chars=150000)
        except Exception as exc:
            if type(exc).__module__.startswith("httpx") or isinstance(exc, ValueError):
                continue
            raise
        date_prefix = f" ({row_date.isoformat()})" if row_date else ""
        header = f"HKEXnews {title}{date_prefix}".strip()
        documents[pdf_url] = f"{header}\n{extracted}"

    return documents, sorted(historical)
