"""Fail-first tests for Slice 2 official discovery adapters and PDF/ICS/JSON-LD capture."""

from __future__ import annotations

import io
import json
import socket
from datetime import date

import httpx
import pytest
from pypdf import PdfWriter

from earnings_watch.engine.verify import verify_claims
from earnings_watch.model.events import EventType, ExtractClaim
from earnings_watch.model.sources import Source
from earnings_watch.providers.generic_ir import capture
from earnings_watch.providers.hkexnews import discover_hkex
from earnings_watch.providers.pdf import extract_pdf_text
from earnings_watch.providers.q4_ir import discover_q4, is_q4_source
from earnings_watch.providers.sec_edgar import discover_sec, signals
from earnings_watch.transport import Transport


def _public_resolver(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def _make_pdf_bytes(pages: list[str]) -> bytes:
    """Create a minimal valid multi-page PDF containing extractable ASCII text."""
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("latin-1")
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

        font = DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
        font_ref = writer._add_object(font)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})}
        )
        content = DecodedStreamObject()
        content.set_data(stream)
        page[NameObject("/Contents")] = writer._add_object(content)
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()


def _source(
    *,
    key: str = "acme",
    ticker: str = "ACME",
    exchange: str | None = "XNAS",
    calendar_url: str = "https://investors.acme.example/events/default.aspx",
    sec_cik: int | None = None,
    hkex_stock_id: str | None = None,
) -> Source:
    return Source(
        key=key,
        issuer="Acme Industrial Holdings",
        name="Acme Industrial Holdings",
        ticker=ticker,
        exchange=exchange,
        calendar_url=calendar_url,
        fetch="plain",
        period_scheme="calendar-q",
        fy_end_month=12,
        sec_cik=sec_cik,
        hkex_stock_id=hkex_stock_id,
    )


# ---------------------------------------------------------------------------
# 1. providers/pdf.py
# ---------------------------------------------------------------------------


def test_extract_pdf_text_normalizes_and_bounds():
    pdf_bytes = _make_pdf_bytes(
        [
            "Board   Meeting   on   2026-08-20   for   Interim   Results",
            "Page 2 extra details",
            "Page 3 should be omitted when max_pages=2",
        ]
    )
    text = extract_pdf_text(pdf_bytes, max_pages=2, max_chars=200)
    assert "Board Meeting on 2026-08-20 for Interim Results" in text
    assert "Page 2 extra details" in text
    assert "Page 3" not in text

    clipped = extract_pdf_text(pdf_bytes, max_pages=3, max_chars=25)
    assert len(clipped) <= 25


def test_extract_pdf_text_fails_closed_on_invalid_or_empty_pdf():
    with pytest.raises(ValueError):
        extract_pdf_text(b"not a pdf at all")
    with pytest.raises(ValueError):
        extract_pdf_text(b"")
    blank = PdfWriter()
    blank.add_blank_page(width=612, height=792)
    buf = io.BytesIO()
    blank.write(buf)
    with pytest.raises(ValueError):
        extract_pdf_text(buf.getvalue())


# ---------------------------------------------------------------------------
# 2. providers/q4_ir.py
# ---------------------------------------------------------------------------


def test_is_q4_source_detects_url_and_page_text():
    q4_src = _source(calendar_url="https://investors.acme.example/events/default.aspx")
    plain_src = _source(calendar_url="https://investors.acme.example/calendar")
    assert is_q4_source(q4_src)
    assert not is_q4_source(plain_src)
    assert is_q4_source(plain_src, "Footer: Powered By Q4 Inc. v5")


async def test_discover_q4_events_and_press_releases():
    src = _source(calendar_url="https://investors.acme.example/events/default.aspx")
    today = date(2026, 6, 15)

    upcoming_events = {
        "GetEventListResult": [
            {
                "Title": "Q2 2026 Earnings Conference Call",
                "StartDate": "07/28/2026 08:30:00",
                "TimeZone": "ET",
                "LinkToDetailPage": "/events/event-details/q2-2026-earnings",
            },
            {
                "Title": "Annual General Meeting 2026",
                "StartDate": "09/10/2026 10:00:00",
                "TimeZone": "UTC",
                "LinkToDetailPage": "/events/event-details/agm-2026",
            },
        ]
    }
    past_events_2026 = {
        "GetEventListResult": [
            {
                "Title": "Q1 2026 Earnings Release",
                "StartDate": "04/28/2026 08:00:00",
                "TimeZone": "ET",
            },
            {
                "Title": "Sustainability Townhall",
                "StartDate": "03/15/2026 14:00:00",
                "TimeZone": "ET",
            },
        ]
    }
    past_events_2025 = {
        "GetEventListResult": [
            {
                "Title": "Q4 2025 Financial Results",
                "StartDate": "01/29/2026 08:00:00",
                "TimeZone": "ET",
            },
            {
                "Title": "Q3 2025 Earnings Call",
                "StartDate": "10/29/2025 08:00:00",
                "TimeZone": "ET",
            },
        ]
    }
    press_releases = {
        "GetPressReleaseListResult": [
            {
                "Headline": "Acme Announces Q2 2026 Earnings Release Date and Dividend",
                "PressReleaseDate": "06/01/2026 07:00:00",
                "LinkToDetailPage": "/news/news-details/2026/q2-date/default.aspx",
                "Body": "<p>Acme will report Q2 2026 results on 2026-07-28 before market open.</p>",
            },
            {
                "Headline": "Acme Launches New Product Line",
                "PressReleaseDate": "06/05/2026 09:00:00",
                "LinkToDetailPage": "/news/news-details/2026/product/default.aspx",
                "Body": "<p>Unrelated product announcement.</p>",
            },
            {
                "Headline": "Acme Q1 2026 Results Announced",
                "PressReleaseDate": "01/10/2026 09:00:00",
                "LinkToDetailPage": "/news/news-details/2026/old/default.aspx",
                "Body": "<p>Older than 60 days.</p>",
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "/feed/Event.svc/GetEventList" in url:
            if "pressReleaseDateFilter=1" in url:
                return httpx.Response(200, json=upcoming_events)
            if "year=2026" in url:
                return httpx.Response(200, json=past_events_2026)
            if "year=2025" in url:
                return httpx.Response(200, json=past_events_2025)
            return httpx.Response(200, json={"GetEventListResult": []})
        if "/feed/PressRelease.svc/GetPressReleaseList" in url:
            return httpx.Response(200, json=press_releases)
        return httpx.Response(404)

    transport = Transport(
        {"investors.acme.example"},
        resolver=_public_resolver,
        mock=httpx.MockTransport(handler),
        pace=0,
    )
    documents, historical = await discover_q4(transport, src, today=today)
    assert historical == [date(2025, 10, 29), date(2026, 1, 29), date(2026, 4, 28)]
    combined = "\n".join(documents.values())
    assert "Q2 2026 Earnings Conference Call" in combined
    assert "2026-07-28" in combined
    assert "08:30" in combined
    assert "ET" in combined
    assert "Annual General Meeting 2026" in combined
    assert "Acme will report Q2 2026 results on 2026-07-28 before market open." in combined
    assert "Unrelated product announcement" not in combined
    assert "Older than 60 days" not in combined
    assert all(url.startswith("https://investors.acme.example/") for url in documents)


# ---------------------------------------------------------------------------
# 3. providers/hkexnews.py
# ---------------------------------------------------------------------------


async def test_discover_hkex_board_meeting_pdf_and_historical_results():
    src = _source(
        key="bauhinia",
        ticker="00999",
        exchange="XHKG",
        calendar_url="https://ir.bauhinia.example/calendar",
    )
    today = date(2026, 8, 10)
    board_pdf = _make_pdf_bytes(
        [
            "Notice of Board Meeting: The Board will meet on 2026-08-25 to approve 2026 Interim Results."
        ]
    )
    agm_pdf = _make_pdf_bytes(
        ["Notice of AGM: The Annual General Meeting will be held on 2026-09-18."]
    )
    dividend_pdf = _make_pdf_bytes(["Closure of Books for Interim Dividend on 2026-09-05."])

    rows = [
        {
            "NEWS_ID": "1001",
            "DATE_TIME": "01/08/2026 17:30",
            "LONG_TEXT": "Announcements and Notices - [Date of Board Meeting]",
            "TITLE": "DATE OF BOARD MEETING",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0801/board.pdf",
        },
        {
            "NEWS_ID": "1002",
            "DATE_TIME": "02/08/2026 18:00",
            "LONG_TEXT": "Announcements and Notices - [Notice of AGM]",
            "TITLE": "NOTICE OF ANNUAL GENERAL MEETING",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0802/agm.pdf",
        },
        {
            "NEWS_ID": "1003",
            "DATE_TIME": "03/08/2026 18:10",
            "LONG_TEXT": "Announcements and Notices - [Dividend or Distribution / Closure of Books]",
            "TITLE": "CLOSURE OF REGISTER OF MEMBERS",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0803/div.pdf",
        },
        {
            "NEWS_ID": "1004",
            "DATE_TIME": "25/03/2026 16:45",
            "LONG_TEXT": "Announcements and Notices - [Final Results]",
            "TITLE": "ANNUAL RESULTS FOR THE YEAR ENDED 31 DECEMBER 2025",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0325/final.pdf",
        },
        {
            "NEWS_ID": "1005",
            "DATE_TIME": "15/05/2026 17:00",
            "LONG_TEXT": "Announcements and Notices - [Quarterly Results]",
            "TITLE": "FIRST QUARTER RESULTS 2026",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0515/q1.pdf",
        },
    ]

    pdf_fetches: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "prefix.do" in url:
            body = 'callback({"stockInfo":[{"stockId":54321,"code":"00999","name":"BAUHINIA"}]})'
            return httpx.Response(200, text=body)
        if "titleSearchServlet.do" in url:
            assert "stockId=54321" in url
            return httpx.Response(200, json={"result": json.dumps(rows)})
        if url.endswith("board.pdf"):
            pdf_fetches.append(url)
            return httpx.Response(
                200, content=board_pdf, headers={"content-type": "application/pdf"}
            )
        if url.endswith("agm.pdf"):
            pdf_fetches.append(url)
            return httpx.Response(200, content=agm_pdf, headers={"content-type": "application/pdf"})
        if url.endswith("div.pdf"):
            pdf_fetches.append(url)
            return httpx.Response(
                200, content=dividend_pdf, headers={"content-type": "application/pdf"}
            )
        return httpx.Response(404)

    transport = Transport(
        {"www1.hkexnews.hk"},
        resolver=_public_resolver,
        mock=httpx.MockTransport(handler),
        pace=0,
    )
    documents, historical = await discover_hkex(transport, src, today=today)
    assert historical == [date(2026, 3, 25), date(2026, 5, 15)]
    assert len(pdf_fetches) == 3
    board_url = "https://www1.hkexnews.hk/listedco/listconews/sehk/2026/0801/board.pdf"
    assert board_url in documents
    assert "2026-08-25" in documents[board_url]
    assert "2026-09-18" in "\n".join(documents.values())
    assert "2026-09-05" in "\n".join(documents.values())


async def test_discover_hkex_caps_pdf_downloads_at_five():
    src = _source(
        key="bauhinia",
        ticker="00999",
        exchange="XHKG",
        calendar_url="https://ir.bauhinia.example/calendar",
        hkex_stock_id="54321",
    )
    today = date(2026, 8, 10)
    sample_pdf = _make_pdf_bytes(["Board Meeting on 2026-08-25"])
    rows = [
        {
            "NEWS_ID": str(2000 + i),
            "DATE_TIME": f"0{i + 1}/08/2026 17:00",
            "LONG_TEXT": "Announcements and Notices - [Date of Board Meeting]",
            "TITLE": f"BOARD MEETING {i}",
            "FILE_LINK": f"/listedco/listconews/sehk/2026/080{i + 1}/m{i}.pdf",
        }
        for i in range(7)
    ]
    fetched = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal fetched
        url = str(request.url)
        if "titleSearchServlet.do" in url:
            return httpx.Response(200, json={"result": json.dumps(rows)})
        if url.endswith(".pdf"):
            fetched += 1
            return httpx.Response(
                200, content=sample_pdf, headers={"content-type": "application/pdf"}
            )
        return httpx.Response(404)

    transport = Transport(
        {"www1.hkexnews.hk"},
        resolver=_public_resolver,
        mock=httpx.MockTransport(handler),
        pace=0,
    )
    documents, _ = await discover_hkex(transport, src, today=today)
    assert fetched == 5
    assert len(documents) == 5


# ---------------------------------------------------------------------------
# 4. providers/sec_edgar.py
# ---------------------------------------------------------------------------


async def test_discover_sec_def14a_and_historical_8k_6k():
    src = _source(sec_cik=123456)
    today = date(2026, 6, 15)
    submissions = {
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0000123456-26-000010",
                    "0000123456-26-000009",
                    "0000123456-26-000008",
                    "0000123456-26-000007",
                    "0000123456-25-000006",
                    "0000123456-25-000005",
                    "0000123456-24-000001",
                ],
                "filingDate": [
                    "2026-05-01",
                    "2026-04-28",
                    "2026-03-10",
                    "2026-02-01",
                    "2025-10-29",
                    "2025-08-01",
                    "2024-05-01",
                ],
                "form": [
                    "DEF 14A",
                    "8-K",
                    "6-K",
                    "6-K",
                    "8-K",
                    "8-K",
                    "8-K",
                ],
                "items": [
                    [],
                    ["2.02", "9.01"],
                    "",
                    "",
                    "2.02,9.01",
                    "8.01",
                    "2.02",
                ],
                "primaryDocument": [
                    "proxy2026.htm",
                    "k8_q1.htm",
                    "k6_routine.htm",
                    "k6_results.htm",
                    "k8_q3.htm",
                    "k8_other.htm",
                    "k8_old.htm",
                ],
                "primaryDocDescription": [
                    "DEF 14A",
                    "8-K",
                    "Notification of Voting Rights and Own Shares",
                    "Q4 2025 Financial Results",
                    "8-K",
                    "8-K",
                    "8-K",
                ],
            }
        }
    }
    proxy_html = (
        "<html><head><style>.h{display:none}</style><script>var x=1;</script></head>"
        "<body><ix:header><ix:hidden>HIDDEN</ix:hidden></ix:header>"
        "<h1>Notice of 2026 Annual Meeting of Stockholders</h1>"
        "<p>The Annual Meeting will be held on June 24, 2026 at 09:00 AM EDT.</p>"
        "</body></html>"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://data.sec.gov/submissions/CIK0000123456.json":
            return httpx.Response(200, json=submissions)
        if url == "https://www.sec.gov/Archives/edgar/data/123456/000012345626000010/proxy2026.htm":
            return httpx.Response(200, text=proxy_html, headers={"content-type": "text/html"})
        return httpx.Response(404)

    transport = Transport(
        {"data.sec.gov", "www.sec.gov"},
        resolver=_public_resolver,
        mock=httpx.MockTransport(handler),
        sec_contact="ops@acme.example",
        pace=0,
    )
    # Existing signals() stays backward-compatible
    sig = await signals(transport, src, "ops@acme.example")
    assert any(item["id"] == "0000123456-26-000009" for item in sig)

    documents, historical = await discover_sec(transport, src, "ops@acme.example", today=today)
    proxy_url = "https://www.sec.gov/Archives/edgar/data/123456/000012345626000010/proxy2026.htm"
    assert proxy_url in documents
    assert "June 24, 2026" in documents[proxy_url]
    assert "var x=1" not in documents[proxy_url]
    assert "HIDDEN" not in documents[proxy_url]
    assert len(documents[proxy_url]) <= 30_000
    # 8-K with 2.02 (list and comma-string) + non-routine 6-K within 18m; excludes routine 6-K, 8-K 8.01, and >18m old
    assert historical == [date(2025, 10, 29), date(2026, 2, 1), date(2026, 4, 28)]


# ---------------------------------------------------------------------------
# 5. providers/generic_ir.py — PDF, JSON-LD, same-host .ics
# ---------------------------------------------------------------------------


async def test_generic_ir_capture_supports_pdf():
    pdf_bytes = _make_pdf_bytes(["Acme Financial Calendar: Q3 2026 Results on 2026-10-29."])

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=pdf_bytes, headers={"content-type": "application/pdf"})

    transport = Transport(
        {"investors.acme.example"},
        resolver=_public_resolver,
        mock=httpx.MockTransport(handler),
        pace=0,
    )
    cap = await capture(transport, "https://investors.acme.example/calendar.pdf")
    assert "Q3 2026 Results on 2026-10-29" in cap.text


async def test_generic_ir_capture_extracts_jsonld_and_same_host_ics():
    html_page = """<!doctype html>
    <html>
      <head>
        <script type="application/ld+json">
        {
          "@context": "https://example.com/schema",
          "@type": "Event",
          "name": "Q2 2026 Earnings Release",
          "startDate": "2026-07-28T07:00:00Z"
        }
        </script>
      </head>
      <body>
        <h1>Financial Calendar</h1>
        <a href="/cal/q2-2026.ics">Add Q2 2026 Call to Calendar</a>
        <a href="https://thirdparty.example/other.ics">External ICS</a>
      </body>
    </html>
    """
    ics_payload = (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        "SUMMARY:Q2 2026 Earnings\r\n"
        "  Conference Call\r\n"
        "DTSTART:20260728T133000Z\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )
    fetched_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        fetched_urls.append(url)
        if url == "https://investors.acme.example/calendar":
            return httpx.Response(200, text=html_page, headers={"content-type": "text/html"})
        if url == "https://investors.acme.example/cal/q2-2026.ics":
            return httpx.Response(200, text=ics_payload, headers={"content-type": "text/calendar"})
        return httpx.Response(404)

    transport = Transport(
        {"investors.acme.example"},
        resolver=_public_resolver,
        mock=httpx.MockTransport(handler),
        pace=0,
    )
    cap = await capture(transport, "https://investors.acme.example/calendar")
    assert "https://thirdparty.example/other.ics" not in fetched_urls
    assert "Structured event: Q2 2026 Earnings Release on 2026-07-28" in cap.text
    expected_cal_line = (
        "Calendar event: Q2 2026 Earnings Conference Call on 28 Jul 2026 at 13:30 UTC."
    )
    assert expected_cal_line in cap.text

    # Verify compatibility with verify_claims() / _calendar_candidates()
    src = _source(calendar_url="https://investors.acme.example/calendar")
    claim = ExtractClaim(
        type=EventType.EARNINGS_CALL,
        period="Q2 2026",
        date=date(2026, 7, 28),
        evidence=expected_cal_line,
        source_url="https://investors.acme.example/calendar",
    )
    verified = verify_claims([claim], src, cap.text, today=date(2026, 6, 15))
    assert len(verified.events) == 1
    assert verified.events[0].release_time == "13:30"
    assert verified.events[0].release_tz == "UTC"
