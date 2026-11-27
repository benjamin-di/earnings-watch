from __future__ import annotations

import html
import json
import re
from datetime import date, timedelta
from html.parser import HTMLParser

from earnings_watch.model.sources import Source
from earnings_watch.transport import Transport

_DEF14A_LOOKBACK_DAYS = 90
_HISTORY_LOOKBACK_DAYS = 548  # ~18 months
_MAX_PROXY_CHARS = 30000
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_SAFE_PRIMARY_DOC_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ROUTINE_6K_RE = re.compile(
    r"shareholding|own shares|voting rights|batch|block listing|director|pdmr|"
    r"holding\(s\) in company|notice of|agm|egm",
    re.I,
)


def _is_safe_primary_doc(name: str) -> bool:
    return bool(
        name
        and _SAFE_PRIMARY_DOC_RE.fullmatch(name)
        and ".." not in name
        and not name.startswith(".")
    )


class _EdgarTextExtractor(HTMLParser):
    """Strip HTML and iXBRL tags/headers into compact readable text."""

    _SKIP_TAGS = {"script", "style", "ix:header", "ix:hidden"}
    _BLOCK_TAGS = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "table"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lower = tag.lower()
        if lower in self._SKIP_TAGS:
            self._skip_depth += 1
        elif self._skip_depth == 0 and lower in self._BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self._parts.append(data)

    def text(self) -> str:
        raw = html.unescape("".join(self._parts))
        lines = [" ".join(line.split()) for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _has_item_202(items: object) -> bool:
    if isinstance(items, list):
        return any(str(item).strip() == "2.02" for item in items)
    if isinstance(items, str):
        return "2.02" in {part.strip() for part in items.split(",")}
    return False


def _strip_edgar_html(raw: bytes, *, max_chars: int = _MAX_PROXY_CHARS) -> str:
    parser = _EdgarTextExtractor()
    parser.feed(raw.decode("utf-8", errors="replace"))
    return parser.text()[:max_chars].strip()


async def signals(transport: Transport, source: Source, contact: str) -> list[dict]:
    if not contact or "@" not in contact:
        raise ValueError("SEC provider requires a configured contact user agent")
    if not source.sec_cik:
        return []
    page = await transport.get(
        f"https://data.sec.gov/submissions/CIK{source.sec_cik:010d}.json",
        user_agent="earnings-watch " + contact,
    )
    recent = json.loads(page.body).get("filings", {}).get("recent", {})
    return [
        {"id": accession, "date": day, "form": form}
        for accession, day, form, items in zip(
            recent.get("accessionNumber", []),
            recent.get("filingDate", []),
            recent.get("form", []),
            recent.get("items", []),
        )
        if form == "6-K" or (form == "8-K" and _has_item_202(items))
    ]


async def discover_sec(
    transport: Transport, source: Source, contact: str, *, today: date
) -> tuple[dict[str, str], list[date]]:
    """Discover recent DEF 14A proxy text and historical 8-K/6-K earnings dates."""
    if not contact or "@" not in contact:
        raise ValueError("SEC provider requires a configured contact user agent")
    if not source.sec_cik:
        return {}, []
    ua = "earnings-watch " + contact
    page = await transport.get(
        f"https://data.sec.gov/submissions/CIK{source.sec_cik:010d}.json",
        user_agent=ua,
    )
    recent = json.loads(page.body).get("filings", {}).get("recent", {})
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    forms = recent.get("form", [])
    items_list = recent.get("items", [])
    primary_docs = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])

    def14a_cutoff = today - timedelta(days=_DEF14A_LOOKBACK_DAYS)
    history_cutoff = today - timedelta(days=_HISTORY_LOOKBACK_DAYS)
    documents: dict[str, str] = {}
    historical: set[date] = set()
    def14a_candidate: tuple[date, str, str] | None = None

    for idx, form in enumerate(forms):
        if idx >= len(filing_dates) or idx >= len(accessions):
            break
        try:
            filed = date.fromisoformat(filing_dates[idx])
        except ValueError:
            continue
        accession = str(accessions[idx])
        items = items_list[idx] if idx < len(items_list) else ""
        primary_doc = str(primary_docs[idx]) if idx < len(primary_docs) else ""
        description = str(descriptions[idx]) if idx < len(descriptions) else ""

        if (
            form == "DEF 14A"
            and def14a_cutoff <= filed <= today
            and _ACCESSION_RE.fullmatch(accession)
            and _is_safe_primary_doc(primary_doc)
            and (def14a_candidate is None or filed > def14a_candidate[0])
        ):
            def14a_candidate = (filed, accession, primary_doc)

        if history_cutoff <= filed <= today:
            if form == "8-K" and _has_item_202(items):
                historical.add(filed)
            elif form == "6-K" and not _ROUTINE_6K_RE.search(description):
                historical.add(filed)

    if def14a_candidate is not None:
        _, accession, primary_doc = def14a_candidate
        doc_url = (
            f"https://www.sec.gov/Archives/edgar/data/"
            f"{source.sec_cik}/{accession.replace('-', '')}/{primary_doc}"
        )
        try:
            doc_page = await transport.get(doc_url, user_agent=ua)
            text = _strip_edgar_html(doc_page.body, max_chars=_MAX_PROXY_CHARS)
            if text:
                documents[doc_url] = text
        except Exception:
            pass

    return documents, sorted(historical)
