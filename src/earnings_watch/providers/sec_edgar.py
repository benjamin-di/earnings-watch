from __future__ import annotations

from dataclasses import dataclass
import html
import json
import re
from datetime import date, timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit

from earnings_watch.model.sources import Source
from earnings_watch.transport import Transport

_DEF14A_LOOKBACK_DAYS = 90
_HISTORY_LOOKBACK_DAYS = 548  # ~18 months
_IPO_LOOKBACK_DAYS = 180
_MAX_PROXY_CHARS = 30000
_ACCESSION_RE = re.compile(r"^\d{10}-\d{2}-\d{6}$")
_SAFE_PRIMARY_DOC_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_ROUTINE_6K_RE = re.compile(
    r"shareholding|own shares|voting rights|batch|block listing|director|pdmr|"
    r"holding\(s\) in company|notice of|agm|egm",
    re.I,
)
_US_EXCHANGES = frozenset({"XNAS", "XNYS", "ARCX", "BATS"})
_NON_US_HOST_SUFFIXES = (
    "hkexnews.hk",
    "asx.com.au",
    ".co.uk",
    ".de",
    ".fr",
    ".it",
    ".pt",
    ".gr",
    ".at",
    ".kr",
    ".jp",
    ".au",
    ".hk",
)
_NAME_STOPWORDS = frozenset(
    {
        "inc",
        "incorporated",
        "corp",
        "corporation",
        "ltd",
        "limited",
        "plc",
        "pbc",
        "co",
        "company",
        "holdings",
        "holding",
        "group",
        "class",
        "common",
        "stock",
        "ordinary",
        "shares",
        "nv",
        "ag",
        "sa",
        "se",
        "spa",
        "the",
        "and",
        "of",
    }
)
_IPO_FORMS = frozenset({"424B4", "EFFECT", "S-1", "S-1/A", "F-1", "F-1/A"})
_REPORTING_FORMS = frozenset({"10-Q", "10-K", "20-F"})


@dataclass(frozen=True)
class SecIdentityResult:
    sec_cik: int | None
    fy_end_month: int | None
    ipo_date: date | None


def _is_us_sec_eligible(source: Source) -> bool:
    if source.exchange in _US_EXCHANGES or (source.isin or "").startswith("US"):
        return True
    if source.exchange is not None or source.hkex_stock_id:
        return False
    host = (urlsplit(source.calendar_url).hostname or "").lower()
    if any(host.endswith(suffix) for suffix in _NON_US_HOST_SUFFIXES):
        return False
    return True


def _name_tokens(text: str) -> set[str]:
    raw = re.findall(r"[a-z0-9]+", text.casefold())
    return {tok for tok in raw if tok not in _NAME_STOPWORDS}


def _company_names_match(source: Source, sec_title: str) -> bool:
    source_tokens = _name_tokens(f"{source.issuer} {source.name}")
    sec_tokens = _name_tokens(sec_title)
    if not source_tokens or not sec_tokens:
        return False
    long_overlap = {t for t in (source_tokens & sec_tokens) if len(t) >= 3}
    if long_overlap:
        return True
    return source_tokens == sec_tokens


def _parse_fy_end_month(raw: object) -> int | None:
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip()
    if len(cleaned) == 4 and cleaned.isdigit():
        month = int(cleaned[:2])
        if 1 <= month <= 12:
            return month
    return None


def _detect_recent_ipo(recent: dict, *, today: date) -> date | None:
    forms = recent.get("form", [])
    filing_dates = recent.get("filingDate", [])
    items_list = recent.get("items", [])
    cutoff = today - timedelta(days=_IPO_LOOKBACK_DAYS)
    best_424b4: date | None = None
    best_effect: date | None = None
    best_reg: date | None = None

    for idx, form in enumerate(forms):
        if idx >= len(filing_dates):
            break
        items = items_list[idx] if idx < len(items_list) else ""
        if form in _REPORTING_FORMS or (form == "8-K" and _has_item_202(items)):
            return None
        if form not in _IPO_FORMS:
            continue
        try:
            filed = date.fromisoformat(str(filing_dates[idx]))
        except ValueError:
            continue
        if not (cutoff <= filed <= today):
            continue
        if form == "424B4" and (best_424b4 is None or filed > best_424b4):
            best_424b4 = filed
        elif form == "EFFECT" and (best_effect is None or filed > best_effect):
            best_effect = filed
        elif best_reg is None or filed > best_reg:
            best_reg = filed

    return best_424b4 or best_effect or best_reg


async def resolve_sec_identity(
    transport: Transport, source: Source, contact: str, *, today: date
) -> SecIdentityResult:
    """Deterministically resolve SEC CIK, fiscal year-end month, and recent IPO status."""
    if not contact or "@" not in contact:
        return SecIdentityResult(source.sec_cik, None, None)
    ua = "earnings-watch " + contact
    cik = source.sec_cik

    if cik is None:
        if not _is_us_sec_eligible(source):
            return SecIdentityResult(None, None, None)
        try:
            tickers_page = await transport.get(
                "https://www.sec.gov/files/company_tickers.json",
                user_agent=ua,
            )
            payload = json.loads(tickers_page.body)
        except Exception:
            return SecIdentityResult(None, None, None)
        if isinstance(payload, dict):
            target_ticker = source.ticker.upper()
            for entry in payload.values():
                if not isinstance(entry, dict):
                    continue
                if str(entry.get("ticker") or "").upper() != target_ticker:
                    continue
                if not _company_names_match(source, str(entry.get("title") or "")):
                    continue
                try:
                    candidate = int(entry["cik_str"])
                except (KeyError, TypeError, ValueError):
                    continue
                if 1 <= candidate <= 9_999_999_999:
                    cik = candidate
                    break

    if cik is None:
        return SecIdentityResult(None, None, None)

    try:
        sub_page = await transport.get(
            f"https://data.sec.gov/submissions/CIK{cik:010d}.json",
            user_agent=ua,
        )
        sub_data = json.loads(sub_page.body)
    except Exception:
        return SecIdentityResult(cik, None, None)

    if not isinstance(sub_data, dict):
        return SecIdentityResult(cik, None, None)

    fy_end_month = _parse_fy_end_month(sub_data.get("fiscalYearEnd"))
    recent = sub_data.get("filings", {}).get("recent", {})
    ipo_date = _detect_recent_ipo(recent, today=today) if isinstance(recent, dict) else None
    return SecIdentityResult(cik, fy_end_month, ipo_date)


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
