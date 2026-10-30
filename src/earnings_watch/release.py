from __future__ import annotations
import html
import json
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from urllib.parse import parse_qs, urljoin, urlsplit
from zoneinfo import ZoneInfo
from .model.events import Event, ResultsLink, ResultsLinkKind
from .model.sources import Source, period_consistent
from .model.urls import public_url
from .providers.generic_ir import Links
from .transport import Transport

SIGNAL_DAYS_BEFORE = 1
SIGNAL_DAYS_AFTER = 7
MAX_FILINGS_PER_POLL = 2
HK = ZoneInfo("Asia/Hong_Kong")
EDGAR_TZ = ZoneInfo("America/New_York")


async def _get(url: str, transport: Transport):
    response = await transport.get(url)
    return response.url, response.body, response.content_type


def _text(body: bytes, url: str):
    import html2text

    text = body.decode("utf-8", errors="replace")
    parser = Links(url)
    parser.feed(text)
    return html2text.html2text(text), parser.links


_ROUTINE_6K = re.compile(
    "shareholding|own shares|voting rights|batch|block listing|director|pdmr|holding\\(s\\) in company|notice of|agm|egm",
    re.I,
)
_RESULTS = re.compile("\\bresults?\\b|\\bearnings\\b|\\breports?\\b", re.I)
_HKEX_RESULTS = re.compile("\\[(?:final|interim|quarterly|annual) results\\]", re.I)
_PERIOD_PHRASE = re.compile(
    "(?:\\b(?:first|second|third|fourth|1st|2nd|3rd|4th)\\b[- ](?:fiscal[- ])?quarter|\\bq[1-4]\\b|\\b[1-4]q\\b|\\bfull[- ]year\\b|\\bhalf[- ]year\\b|\\binterim\\b|\\bsix months\\b|\\bnine months\\b|\\bthree months\\b|\\byear ended\\b|\\bannual\\b)[^.;\\n]{0,80}?(?:\\b20\\d{2}\\b|\\bfy ?\\d{2}\\b)",
    re.I,
)


@dataclass(frozen=True)
class Signal:
    event_id: str
    description: str
    published_at: datetime | None = None
    link: ResultsLink | None = None


def window(event: Event) -> tuple[datetime, datetime]:
    start = datetime.combine(event.date, datetime.min.time(), timezone.utc)
    return (
        start - timedelta(days=SIGNAL_DAYS_BEFORE),
        start + timedelta(days=SIGNAL_DAYS_AFTER + 1),
    )


def hkex_code(source: Source) -> str | None:
    return source.ticker.zfill(5) if re.fullmatch("\\d{1,5}", source.ticker) else None


def page_url(source: Source) -> str:
    return source.results_url or source.announcements_url or source.calendar_url


def adapter(source: Source) -> str:
    return "sec" if source.sec_cik else "hkex" if hkex_code(source) else "page"


def checked(source: Source) -> str:
    kind = adapter(source)
    if kind == "sec":
        return f"SEC EDGAR filings of CIK {source.sec_cik} (8-K Item 2.02 / results 6-K)"
    if kind == "hkex":
        return f"HKEXnews results announcements of stock {hkex_code(source)}"
    return f"new results links on {page_url(source)}"


def period_verdict(text: str, source: Source, period: str) -> str:
    phrases = [m.group(0) for m in _PERIOD_PHRASE.finditer(" ".join(text.split())[:6000])][:12]
    if any((period_consistent(phrase, source, period) for phrase in phrases)):
        return "match"
    return "conflict" if phrases else "unknown"


async def _sec(
    source: Source, event: Event, now: datetime, seen: set[str], transport
) -> tuple[Signal | None, list[str]]:
    if source.sec_cik is None:
        raise ValueError("SEC source requires a CIK")
    cik = source.sec_cik
    _, body, _ = await _get(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", transport)
    recent = json.loads(body)["filings"]["recent"]
    since = event.date - timedelta(days=SIGNAL_DAYS_BEFORE)
    candidates = []
    for i, form in enumerate(recent["form"]):
        accession = recent["accessionNumber"][i]
        if accession in seen or date.fromisoformat(recent["filingDate"][i]) < since:
            continue
        items = {item.strip() for item in recent.get("items", [""] * (i + 1))[i].split(",")}
        description = recent.get("primaryDocDescription", [""] * (i + 1))[i]
        if (
            form == "8-K"
            and "2.02" in items
            or (form == "6-K" and (not _ROUTINE_6K.search(description)))
        ):
            candidates.append((recent["acceptanceDateTime"][i], form, accession))
    rejected = []
    for accepted, form, accession in sorted(candidates)[:MAX_FILINGS_PER_POLL]:
        folder = f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/"
        index_url = f"{folder}{accession}-index.htm"
        _, index, _ = await _get(index_url, transport)
        rows = re.findall(
            "<tr[^>]*>(.*?)</tr>", index.decode("utf-8", errors="replace"), re.S | re.I
        )
        documents = []
        for row in rows:
            cells = [
                html.unescape(re.sub("<[^>]+>", " ", cell)).strip()
                for cell in re.findall("<td[^>]*>(.*?)</td>", row, re.S | re.I)
            ]
            href = re.search('href="(/Archives/[^"]+)"', row)
            if len(cells) >= 4 and href:
                documents.append((cells[3].upper(), urljoin(index_url, href.group(1))))
        exhibit = (
            next((url for kind, url in documents if kind == "EX-99.1"), None)
            or next((url for kind, url in documents if kind.startswith("EX-99")), None)
            or next((url for kind, url in documents if kind == form), None)
        )
        if exhibit is None:
            rejected.append(accession)
            continue
        _, document, _ = await _get(public_url(exhibit), transport)
        text, _ = _text(document, exhibit)
        verdict = period_verdict(text, source, event.period)
        results_6k = verdict == "match" and _RESULTS.search(text[:4000])
        if form == "8-K" and verdict == "conflict" or (form == "6-K" and (not results_6k)):
            rejected.append(accession)
            continue
        published = datetime.fromisoformat(accepted.rstrip("Z").split(".")[0]).replace(
            tzinfo=EDGAR_TZ
        )
        label = f"SEC {form}{(' Item 2.02' if form == '8-K' else '')} {accession}"
        link = ResultsLink(
            title=f"{source.name} {event.period} results ({form} exhibit)",
            kind=ResultsLinkKind.PRESS_RELEASE,
            url=public_url(exhibit),
            evidence_url=index_url,
            evidence=f"{label}: {text[:400]}"[:1600],
            coverage=["press-release"],
            verified_at=now,
        )
        return (
            Signal(
                event.id,
                f"{label} filed {published.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC",
                min(published.astimezone(timezone.utc), now),
                link,
            ),
            rejected + [accession],
        )
    return (None, rejected)


async def _hkex(
    source: Source, event: Event, now: datetime, seen: set[str], transport
) -> tuple[Signal | None, list[str]]:
    code = hkex_code(source)
    stock_id = next(
        (
            value[0]
            for url in (source.calendar_url, source.announcements_url or "")
            if "hkexnews.hk" in url
            for value in [parse_qs(urlsplit(url).query).get("stockId")]
            if value
        ),
        None,
    )
    stock_id = stock_id or next(
        (item.split(":", 1)[1] for item in seen if item.startswith("hkex-stock:")), None
    )
    learned = []
    if stock_id is None:
        _, body, _ = await _get(
            f"https://www1.hkexnews.hk/search/prefix.do?callback=callback&lang=EN&type=A&name={code}&market=SEHK",
            transport,
        )
        payload = body.decode("utf-8", errors="replace")
        stocks = json.loads(payload[payload.index("(") + 1 : payload.rindex(")")]).get(
            "stockInfo", []
        )
        stock_id = next((str(item["stockId"]) for item in stocks if item.get("code") == code), None)
        if stock_id is None:
            raise ValueError(f"HKEX stock code {code} not found")
        learned.append(f"hkex-stock:{stock_id}")
    today_hk = now.astimezone(HK).date()
    search = f"https://www1.hkexnews.hk/search/titleSearchServlet.do?sortDir=0&sortByOptions=DateTime&category=0&market=SEHK&stockId={stock_id}&documentType=-1&fromDate={event.date - timedelta(days=SIGNAL_DAYS_BEFORE):%Y%m%d}&toDate={today_hk:%Y%m%d}&title=&searchType=0&t1code=-2&t2Gcode=-2&t2code=-2&rowRange=100&lang=E"
    _, body, _ = await _get(search, transport)
    rows = json.loads(json.loads(body)["result"] or "[]")
    for row in reversed(rows):
        news_id = f"hkex:{row.get('NEWS_ID')}"
        if news_id in seen or not _HKEX_RESULTS.search(html.unescape(row.get("LONG_TEXT", ""))):
            continue
        title = " ".join(html.unescape(row.get("TITLE", "")).split())
        if period_verdict(title, source, event.period) == "conflict":
            learned.append(news_id)
            continue
        published = (
            datetime.strptime(row["DATE_TIME"], "%d/%m/%Y %H:%M")
            .replace(tzinfo=HK)
            .astimezone(timezone.utc)
        )
        url = public_url(urljoin("https://www1.hkexnews.hk/", row["FILE_LINK"]))
        link = ResultsLink(
            title=title or f"{source.name} results announcement",
            kind=ResultsLinkKind.EXCHANGE_ANNOUNCEMENT,
            url=url,
            evidence_url=search,
            evidence=f"HKEXnews {row.get('STOCK_CODE', code)}: {title}"[:1600],
            coverage=["press-release"],
            verified_at=now,
        )
        return (
            Signal(
                event.id,
                f"HKEX results announcement {row.get('NEWS_ID')} published {published:%Y-%m-%d %H:%M} UTC",
                min(published, now),
                link,
            ),
            learned + [news_id],
        )
    return (None, learned)
