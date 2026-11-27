"""Fail-first regression tests for Round 1 independent review findings."""

from __future__ import annotations

import io
import json
import socket
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client
from pypdf import PdfWriter
from signin_window.oidc import BrowserAuth, OIDCConfig

from conftest import event, source
from earnings_watch import open as open_service
from earnings_watch.connections import create_app
from earnings_watch.engine.cadence import project_expected_earnings
from earnings_watch.engine.reconcile import reconcile
from earnings_watch.engine.timing import CALL_ONLY_RESULTS, call_only, kind_for
from earnings_watch.engine.verify import verify_claims
from earnings_watch.llm.research_server import create as create_research_server
from earnings_watch.llm.runner import AgentResult, AgentTask
from earnings_watch.maintenance import Maintenance
from earnings_watch.model.events import (
    DatePrecision,
    EventStatus,
    EventType,
    ExtractClaim,
    ExtractResult,
    TimingKind,
)
from earnings_watch.model.sources import FetchStrategy, Source
from earnings_watch.model.urls import apex_domain
from earnings_watch.presentation import present
from earnings_watch.providers.generic_ir import capture
from earnings_watch.providers.hkexnews import discover_hkex
from earnings_watch.providers.q4_ir import discover_q4
from earnings_watch.providers.sec_edgar import discover_sec
from earnings_watch.store.database import (
    Catalogue,
    LibraryReader,
    PersonalStore,
    add_person,
    initialize,
    sync_holdings,
)
from earnings_watch.transport import Transport


def _public_resolver(*_args, **_kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]


def _make_pdf_bytes(pages: list[str]) -> bytes:
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
# P1-1: PersonService._coverage_warnings() respects include_holdings=False
# ---------------------------------------------------------------------------


def test_p1_1_coverage_warnings_excludes_holdings_when_not_entitled(settings):
    catalogue = Catalogue(settings.db)
    catalogue.put_source(source("holdco", "HOLD"))
    catalogue.put_source(source("watchco", "WTCH"))

    person = PersonalStore(settings.db, "local")
    sync_holdings(settings.db, "local", "broker", [{"ticker": "HOLD", "exchange": "XNAS"}])
    person.watch("WTCH")

    entitled = open_service(settings, "local", include_holdings=True)
    entitled_sources = {w["source_key"] for w in entitled.status()["coverage_warnings"]}
    assert "holdco" in entitled_sources
    assert "watchco" in entitled_sources

    restricted = open_service(settings, "local", include_holdings=False)
    restricted_sources = {w["source_key"] for w in restricted.status()["coverage_warnings"]}
    assert "holdco" not in restricted_sources
    assert "watchco" in restricted_sources


# ---------------------------------------------------------------------------
# P1-2: apex_domain hardening + filtering empty strings in all consumers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "host",
    [
        "93.184.216.34",
        "2001:db8::1",
        "localhost",
        "co.uk",
        "com.sg",
        "com.tw",
        "co.za",
        "s2.q4cdn.com",
        "d1234.cloudfront.net",
        "tenant.github.io",
        "bucket.s3.amazonaws.com",
        "app.azurewebsites.net",
        "www1.hkexnews.hk",
        "data.sec.gov",
        "www.asx.com.au",
        "site.netlify.app",
        "app.vercel.app",
        "docs.pages.dev",
        "storage.googleapis.com",
        "demo.herokuapp.com",
        "app.firebaseapp.com",
        "org.sharepoint.com",
        "portal.force.com",
    ],
)
def test_p1_2_apex_domain_rejects_ips_bare_suffixes_and_shared_platforms(host: str):
    assert apex_domain(host) == ""


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("ir.acme.com.sg", "acme.com.sg"),
        ("ir.acme.com.tw", "acme.com.tw"),
        ("ir.acme.co.nz", "acme.co.nz"),
        ("ir.acme.co.za", "acme.co.za"),
        ("ir.acme.co.in", "acme.co.in"),
        ("ir.acme.co.il", "acme.co.il"),
        ("ir.acme.com.mx", "acme.com.mx"),
        ("ir.acme.com.tr", "acme.com.tr"),
        ("ir.acme.com.pl", "acme.com.pl"),
        ("ir.acme.com.es", "acme.com.es"),
    ],
)
def test_p1_2_apex_domain_handles_expanded_two_part_suffixes(host: str, expected: str):
    assert apex_domain(host) == expected


async def test_p1_2_transport_and_research_server_never_widen_shared_platforms_or_empty_apex(
    tmp_path,
):
    transport = Transport(
        {"s2.q4cdn.com"},
        apex_domains={"s2.q4cdn.com", "93.184.216.34", "co.uk", ""},
        resolver=_public_resolver,
        mock=httpx.MockTransport(lambda r: httpx.Response(200, text="ok")),
        pace=0,
    )
    assert transport.apex_domains == set()
    with pytest.raises(ValueError, match="allowlist"):
        await transport.get("https://other.acme.example/tenant")
    with pytest.raises(ValueError, match="allowlist"):
        await transport.get("https://sibling.example.org/page")

    server = create_research_server(
        {
            "directory": str(tmp_path / "captures"),
            "hosts": ["ir.acme.example"],
            "apex_domains": ["s2.q4cdn.com", "co.uk", ""],
        }
    )
    async with Client(server) as client:
        with pytest.raises(Exception):
            await client.call_tool(
                "record_observation",
                {"url": "https://other.acme.example/tenant", "evidence": "bad"},
            )


# ---------------------------------------------------------------------------
# P1-4: generic_ir.py ICS resilience, all-day & unknown/floating timezone
# ---------------------------------------------------------------------------


async def test_p1_4_generic_ir_ics_malformed_all_day_and_tzid_handling():
    html_page = """<!doctype html>
    <html><body>
      <h1>Financial Calendar</h1>
      <a href="/cal/bad.ics">Broken ICS</a>
      <a href="/cal/events.ics">Events ICS</a>
    </body></html>
    """
    bad_ics = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
        "SUMMARY:Corrupt Date Event\r\n"
        "DTSTART:20269999T999999Z\r\n"
        "END:VEVENT\r\nEND:VCALENDAR\r\n"
    )
    good_ics = (
        "BEGIN:VCALENDAR\r\n"
        "BEGIN:VEVENT\r\n"
        "SUMMARY:All Day AGM\r\n"
        "DTSTART;VALUE=DATE:20260515\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "SUMMARY:Floating Local Results\r\n"
        "DTSTART:20260728T080000\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "SUMMARY:Unknown TZ Event\r\n"
        "DTSTART;TZID=Mars/Olympus:20260810T090000\r\n"
        "END:VEVENT\r\n"
        "BEGIN:VEVENT\r\n"
        "SUMMARY:Windows Eastern Call\r\n"
        "DTSTART;TZID=Eastern Standard Time:20260728T083000\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/calendar"):
            return httpx.Response(200, text=html_page, headers={"content-type": "text/html"})
        if url.endswith("/cal/bad.ics"):
            return httpx.Response(200, text=bad_ics, headers={"content-type": "text/calendar"})
        if url.endswith("/cal/events.ics"):
            return httpx.Response(200, text=good_ics, headers={"content-type": "text/calendar"})
        return httpx.Response(404)

    transport = Transport(
        {"investors.acme.example"},
        resolver=_public_resolver,
        mock=httpx.MockTransport(handler),
        pace=0,
    )
    cap = await capture(transport, "https://investors.acme.example/calendar")
    assert "Calendar event: All Day AGM on 15 May 2026." in cap.text
    assert "All Day AGM on 15 May 2026 at 00:00 UTC" not in cap.text
    assert "Calendar event: Floating Local Results on 28 Jul 2026." in cap.text
    assert "Floating Local Results on 28 Jul 2026 at 08:00 UTC" not in cap.text
    assert "Calendar event: Unknown TZ Event on 10 Aug 2026." in cap.text
    assert "Unknown TZ Event on 10 Aug 2026 at 09:00 UTC" not in cap.text
    # Eastern Standard Time in July (EDT = UTC-4): 08:30 -> 12:30 UTC
    assert "Calendar event: Windows Eastern Call on 28 Jul 2026 at 12:30 UTC." in cap.text


# ---------------------------------------------------------------------------
# P1-5: Combined results + call survives reconcile() -> merge_observation()
# ---------------------------------------------------------------------------


def test_p1_5_combined_results_and_call_survives_multi_sweep_reconcile():
    src = source("alpha", "ALPHA")
    cases = [
        (
            "Q1 2026",
            date(2026, 4, 30),
            "30 April 2026 | Earnings Report as of March 31, 2026 / Conference Call",
        ),
        (
            "Q2 2026",
            date(2026, 7, 28),
            "28 July 2026 | Interim Report as of June 30, 2026 - Conference Call",
        ),
        (
            "Q3 2026",
            date(2026, 10, 29),
            "29 October 2026 | Quarterly Results Q3 2026, Conference Call",
        ),
        ("Q4 2026", date(2027, 1, 28), "28 January 2027 | Q4 2026 Results | Webcast"),
    ]
    for _period, _dt, ev_text in cases:
        assert not call_only(ev_text), f"Expected combined evidence not to be call_only: {ev_text}"

    assert call_only("30 April 2026 | Q1 2026 Investor Call")
    assert call_only("30 April 2026 | Q1 2026 Earnings Call, Webcast")

    page = "\n".join(ev_text for _, _, ev_text in cases)
    claims = [
        ExtractClaim(type=EventType.EARNINGS, period=p, date=dt, evidence=ev_text)
        for p, dt, ev_text in cases
    ]
    day1 = date(2026, 2, 1)
    day2 = date(2026, 2, 2)
    sweep1 = verify_claims(claims, src, page, today=day1)
    assert len(sweep1.events) == 4
    merged1, _ = reconcile(
        [],
        sweep1.events,
        authoritative_sources={src.key},
        active_source_keys={src.key},
        always_source_keys=set(),
        removal_overrides=set(),
        seed_mode=False,
        now=datetime(2026, 2, 1, tzinfo=timezone.utc),
    )
    sweep2 = verify_claims(claims, src, page, today=day2)
    merged2, _ = reconcile(
        merged1,
        sweep2.events,
        authoritative_sources={src.key},
        active_source_keys={src.key},
        always_source_keys=set(),
        removal_overrides=set(),
        seed_mode=False,
        now=datetime(2026, 2, 2, tzinfo=timezone.utc),
    )
    assert len(merged2) == 4
    for ev in merged2:
        assert ev.status == EventStatus.IR_CONFIRMED
        assert kind_for(ev) == TimingKind.PUBLICATION
        assert ev.timing_issue != CALL_ONLY_RESULTS


# ---------------------------------------------------------------------------
# P1-6: Unconfirmed EXPECTED placeholders do not become PAST or pollute cadence
# ---------------------------------------------------------------------------


def test_p1_6_expected_placeholder_retires_cleanly_and_never_pollutes_cadence():
    src = source("alpha", "ALPHA")
    real_past = event(
        "alpha",
        "ALPHA",
        period="2026Q2",
        id="earnings:alpha:2026Q2",
        date=date(2026, 7, 29),
        status=EventStatus.PAST,
    )
    # Sweep 1: project Q3 expected around 2026-10-28
    day1 = date(2026, 9, 1)
    expected_q3 = project_expected_earnings(src, [real_past], today=day1)
    assert expected_q3 is not None
    assert expected_q3.status == EventStatus.EXPECTED
    assert expected_q3.date == date(2026, 10, 28)

    # Sweep 2: advance today past the unconfirmed estimated date (2026-11-10)
    day2 = date(2026, 11, 10)
    merged, _ = reconcile(
        [real_past, expected_q3],
        [],
        authoritative_sources=set(),
        active_source_keys={src.key},
        always_source_keys=set(),
        removal_overrides=set(),
        seed_mode=False,
        now=datetime(2026, 11, 10, tzinfo=timezone.utc),
    )
    assert all(e.id != expected_q3.id for e in merged)

    # Even if an old cadence-estimated event were passed into cadence directly, it must be ignored.
    stale_fake_past = expected_q3.model_copy(update={"status": EventStatus.PAST})
    assert project_expected_earnings(src, [stale_fake_past], today=day2) is None
    assert project_expected_earnings(src, [expected_q3], today=day2) is None


# ---------------------------------------------------------------------------
# P1-7: hkexnews enforces www1.hkexnews.hk host and counts failed PDFs toward cap
# ---------------------------------------------------------------------------


async def test_p1_7_hkex_enforces_host_and_counts_failed_pdfs_against_cap():
    src = _source(
        key="bauhinia",
        ticker="00999",
        exchange="XHKG",
        calendar_url="https://ir.bauhinia.example/calendar",
        hkex_stock_id="54321",
    )
    today = date(2026, 8, 10)
    good_pdf = _make_pdf_bytes(["Board Meeting on 2026-08-25"])
    rows = [
        {
            "NEWS_ID": "3000",
            "DATE_TIME": "01/08/2026 17:00",
            "LONG_TEXT": "Announcements and Notices - [Date of Board Meeting]",
            "TITLE": "EVIL EXTERNAL LINK",
            "FILE_LINK": "https://evil.example/steal.pdf",
        },
        *[
            {
                "NEWS_ID": str(3001 + i),
                "DATE_TIME": f"0{i + 1}/08/2026 17:00",
                "LONG_TEXT": "Announcements and Notices - [Date of Board Meeting]",
                "TITLE": f"BAD PDF {i}",
                "FILE_LINK": f"/listedco/listconews/sehk/2026/080{i + 1}/bad{i}.pdf",
            }
            for i in range(5)
        ],
        {
            "NEWS_ID": "3010",
            "DATE_TIME": "09/08/2026 17:00",
            "LONG_TEXT": "Announcements and Notices - [Date of Board Meeting]",
            "TITLE": "SIXTH GOOD PDF",
            "FILE_LINK": "/listedco/listconews/sehk/2026/0809/good6.pdf",
        },
    ]
    fetched_urls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        fetched_urls.append(url)
        if "titleSearchServlet.do" in url:
            return httpx.Response(200, json={"result": json.dumps(rows)})
        if "bad" in url:
            return httpx.Response(
                200, content=b"not a pdf", headers={"content-type": "application/pdf"}
            )
        if "good6.pdf" in url:
            return httpx.Response(
                200, content=good_pdf, headers={"content-type": "application/pdf"}
            )
        return httpx.Response(200, content=good_pdf, headers={"content-type": "application/pdf"})

    transport = Transport(
        {"www1.hkexnews.hk", "evil.example"},
        resolver=_public_resolver,
        mock=httpx.MockTransport(handler),
        pace=0,
    )
    documents, _ = await discover_hkex(transport, src, today=today)
    assert not any("evil.example" in u for u in fetched_urls)
    assert not any("good6.pdf" in u for u in fetched_urls)
    assert documents == {}


# ---------------------------------------------------------------------------
# P2-1: repair_rejected() preserves duplicate-row disagreement & forces authoritative=False
# ---------------------------------------------------------------------------


async def test_p2_1_repair_rejected_preserves_disagreement_and_forces_non_authoritative(seeded):
    catalogue = Catalogue(seeded.db)
    service = open_service(seeded)
    service.watch("ALPHA")
    # Seed an existing future event that would be deleted if authoritative=True.
    other_future = event(
        "alpha",
        "ALPHA",
        period="2027Q1",
        id="earnings:alpha:2027Q1",
        date=date.today() + timedelta(days=120),
        status=EventStatus.IR_CONFIRMED,
    )
    catalogue.commit_issuer("alpha", [other_future], run_id="seed-keep")

    d1 = (date.today() + timedelta(days=20)).isoformat()
    d2 = (date.today() + timedelta(days=25)).isoformat()
    page = (
        f"Alpha Corp Q4 2026 financial results will be published on {d1}.\n"
        f"Alternative notice: Alpha Corp Q4 2026 financial results will be published on {d2}."
    )
    transport = Transport(
        {"issuer.example"},
        resolver=_public_resolver,
        mock=httpx.MockTransport(
            lambda r: httpx.Response(200, text=page, headers={"content-type": "text/html"})
        ),
        pace=0,
    )

    async def fake_run(task: AgentTask, *, research_config: Path | None = None):
        if task.schema is ExtractResult and task.kind == "json":
            return AgentResult(
                ExtractResult(
                    page_relevant=True,
                    complete=True,
                    events=[
                        ExtractClaim(
                            type=EventType.EARNINGS,
                            period="Q4 2026",
                            date=date.fromisoformat(d1),
                            evidence=f"Alpha Corp Q4 2026 financial results will be published on {d1}.",
                        ),
                        ExtractClaim(
                            type=EventType.EARNINGS,
                            period="Q4 2026",
                            date=date.fromisoformat(d2),
                            evidence="Non-verbatim paraphrase",
                        ),
                    ],
                ),
                "fake",
            )
        if task.schema is ExtractResult and task.kind == "research":
            return AgentResult(
                ExtractResult(
                    page_relevant=True,
                    complete=True,
                    events=[
                        ExtractClaim(
                            type=EventType.EARNINGS,
                            period="Q4 2026",
                            date=date.fromisoformat(d2),
                            evidence=f"Alternative notice: Alpha Corp Q4 2026 financial results will be published on {d2}.",
                            source_url="https://issuer.example/calendar",
                        )
                    ],
                ),
                "fake",
            )
        return AgentResult(task.schema.model_validate({"documents": []}), "fake")

    runner = AsyncMock()
    runner.run.side_effect = fake_run
    res = await Maintenance(seeded, runner=runner, transport=transport).sweep(force=True)
    assert res["exit"] == 0
    events_by_id = {e["id"]: e for e in catalogue.events() if e["issuer_key"] == "alpha"}
    # Because repair ran, authoritative must be False -> existing 2027Q1 event is retained.
    assert "earnings:alpha:2027Q1" in events_by_id
    # Conflicting dates for 2026Q4 must surface as disagreement rather than silent overwrite.
    q4 = events_by_id["earnings:alpha:2026Q4"]
    assert q4["timing_issue"] == "Current source rows disagree; review required"


# ---------------------------------------------------------------------------
# P2-2: official_documents bounded (60k total / 15k per doc) and passed on researched=True
# ---------------------------------------------------------------------------


async def test_p2_2_official_documents_bounded_and_passed_on_researched_path(settings, monkeypatch):
    catalogue = Catalogue(settings.db)
    service = open_service(settings)
    src = source("alpha", "ALPHA").model_copy(update={"fetch": FetchStrategy.WEBSEARCH})
    catalogue.put_source(src)
    service.watch("ALPHA")

    big_docs = {f"https://issuer.example/doc{i}": ("X" * 25_000) for i in range(6)}

    async def fake_layer1(*_args, **_kwargs):
        return big_docs, []

    monkeypatch.setattr("earnings_watch.maintenance._discover_layer1", fake_layer1)

    prompts: list[dict] = []

    async def fake_run(task: AgentTask, *, research_config: Path | None = None):
        if task.schema is ExtractResult:
            prompts.append(json.loads(task.prompt))
            return AgentResult(ExtractResult(page_relevant=True, complete=True, events=[]), "fake")
        return AgentResult(task.schema.model_validate({"documents": []}), "fake")

    runner = AsyncMock()
    runner.run.side_effect = fake_run
    transport = Transport({"issuer.example"}, resolver=_public_resolver, pace=0)
    await Maintenance(settings, runner=runner, transport=transport).sweep(force=True)

    assert len(prompts) == 1
    injected = prompts[0].get("official_documents")
    assert isinstance(injected, dict) and injected
    assert all(len(v) <= 15_000 for v in injected.values())
    assert sum(len(v) for v in injected.values()) <= 60_000


# ---------------------------------------------------------------------------
# P2-3: generic_ir JSON-LD depth/count caps and fractional seconds in startDate
# ---------------------------------------------------------------------------


async def test_p2_3_generic_ir_jsonld_bounds_and_fractional_seconds():
    deep = {"@type": "Event", "name": "Too Deep", "startDate": "2026-08-01"}
    for _ in range(25):
        deep = {"@graph": [deep]}
    many = [{"@type": "Event", "name": f"Event {i}", "startDate": "2026-08-01"} for i in range(250)]
    html_page = (
        "<html><head>"
        f'<script type="application/ld+json">{json.dumps(deep)}</script>'
        f'<script type="application/ld+json">{json.dumps(many)}</script>'
        '<script type="application/ld+json">'
        '{"@type":"Event","name":"Q3 Fractional","startDate":"2026-10-29T07:30:00.000Z"}'
        "</script>"
        "</head><body>Calendar</body></html>"
    )
    transport = Transport(
        {"investors.acme.example"},
        resolver=_public_resolver,
        mock=httpx.MockTransport(
            lambda r: httpx.Response(200, text=html_page, headers={"content-type": "text/html"})
        ),
        pace=0,
    )
    cap = await capture(transport, "https://investors.acme.example/calendar")
    assert "Too Deep" not in cap.text
    assert cap.text.count("Structured event: Event ") <= 200
    assert "Structured event: Q3 Fractional on 2026-10-29 at 07:30 UTC." in cap.text


# ---------------------------------------------------------------------------
# P2-4: sec_edgar validates accession & primary_doc and preserves historical on DEF 14A failure
# ---------------------------------------------------------------------------


async def test_p2_4_sec_edgar_validates_paths_and_preserves_history_on_def14a_failure():
    src = _source(sec_cik=123456)
    today = date(2026, 6, 15)
    submissions_bad = {
        "filings": {
            "recent": {
                "accessionNumber": ["../../evil", "0000123456-26-000010", "0000123456-26-000009"],
                "filingDate": ["2026-05-02", "2026-05-01", "2026-04-28"],
                "form": ["DEF 14A", "DEF 14A", "8-K"],
                "items": [[], [], ["2.02"]],
                "primaryDocument": ["proxy.htm", "../secret.htm", "k8.htm"],
                "primaryDocDescription": ["DEF 14A", "DEF 14A", "8-K"],
            }
        }
    }
    fetched: list[str] = []

    def handler_bad(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        fetched.append(url)
        if url.endswith("CIK0000123456.json"):
            return httpx.Response(200, json=submissions_bad)
        return httpx.Response(200, text="should not be fetched")

    transport_bad = Transport(
        {"data.sec.gov", "www.sec.gov"},
        resolver=_public_resolver,
        mock=httpx.MockTransport(handler_bad),
        sec_contact="ops@acme.example",
        pace=0,
    )
    docs, hist = await discover_sec(transport_bad, src, "ops@acme.example", today=today)
    assert docs == {}
    assert hist == [date(2026, 4, 28)]
    assert len(fetched) == 1

    # Valid DEF 14A that returns HTTP 500 must NOT discard historical 8-K dates
    submissions_fail = {
        "filings": {
            "recent": {
                "accessionNumber": ["0000123456-26-000010", "0000123456-26-000009"],
                "filingDate": ["2026-05-01", "2026-04-28"],
                "form": ["DEF 14A", "8-K"],
                "items": [[], ["2.02"]],
                "primaryDocument": ["proxy2026.htm", "k8.htm"],
                "primaryDocDescription": ["DEF 14A", "8-K"],
            }
        }
    }

    def handler_fail(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("CIK0000123456.json"):
            return httpx.Response(200, json=submissions_fail)
        return httpx.Response(500, text="upstream error")

    transport_fail = Transport(
        {"data.sec.gov", "www.sec.gov"},
        resolver=_public_resolver,
        mock=httpx.MockTransport(handler_fail),
        sec_contact="ops@acme.example",
        pace=0,
    )
    docs2, hist2 = await discover_sec(transport_fail, src, "ops@acme.example", today=today)
    assert docs2 == {}
    assert hist2 == [date(2026, 4, 28)]


# ---------------------------------------------------------------------------
# P2-5: q4_ir /Date(ms-offset)/ applies offset, emits UTC when absent, guards overflow
# ---------------------------------------------------------------------------


async def test_p2_5_q4_ir_ms_date_offset_utc_and_overflow():
    src = _source(calendar_url="https://investors.acme.example/events/default.aspx")
    today = date(2026, 6, 15)
    # 2026-07-28T12:30:00Z == 1785414600000 ms -> -0400 is 08:30 ET
    epoch_ms = int(datetime(2026, 7, 28, 12, 30, tzinfo=timezone.utc).timestamp() * 1000)
    upcoming = {
        "GetEventListResult": [
            {
                "Title": "Q2 2026 Earnings Call With Offset",
                "StartDate": f"/Date({epoch_ms}-0400)/",
                "TimeZone": "ET",
            },
            {
                "Title": "Q2 2026 Earnings Call No Offset",
                "StartDate": f"/Date({epoch_ms})/",
                "TimeZone": "ET",
            },
            {
                "Title": "Overflow Event",
                "StartDate": "/Date(99999999999999999999)/",
                "TimeZone": "UTC",
            },
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "pressReleaseDateFilter=1" in str(request.url):
            return httpx.Response(200, json=upcoming)
        return httpx.Response(200, json={"GetEventListResult": [], "GetPressReleaseListResult": []})

    transport = Transport(
        {"investors.acme.example"},
        resolver=_public_resolver,
        mock=httpx.MockTransport(handler),
        pace=0,
    )
    docs, _ = await discover_q4(transport, src, today=today)
    combined = "\n".join(docs.values())
    assert "Q2 2026 Earnings Call With Offset — 2026-07-28 08:30 ET" in combined
    assert "Q2 2026 Earnings Call No Offset — 2026-07-28 12:30 UTC" in combined
    assert "Overflow Event" not in combined


# ---------------------------------------------------------------------------
# P2-6: verify.py infers WEEK_OF for "Week of October 19, 2026"
# ---------------------------------------------------------------------------


def test_p2_6_week_of_precision_inferred_with_comma_date():
    evidence = "Week of October 19, 2026 | Q3 2026 Results"
    claim = ExtractClaim(
        type=EventType.EARNINGS,
        period="Q3 2026",
        date=date(2026, 10, 19),
        evidence=evidence,
    )
    res = verify_claims([claim], source("alpha", "ALPHA"), evidence, today=date(2026, 8, 1))
    assert not res.rejected
    assert len(res.events) == 1
    assert res.events[0].date_precision == DatePrecision.WEEK_OF


# ---------------------------------------------------------------------------
# P2-7: connections.py passes coverage_warnings to hub_page & presentation handles MONTH
# ---------------------------------------------------------------------------


async def test_p2_7_connections_hub_renders_coverage_warnings_and_month_precision(
    settings, tmp_path
):
    catalogue = Catalogue(settings.db)
    catalogue.put_source(source("emptyco", "EMPTY"))
    settings.public_url = "http://localhost"
    auth = BrowserAuth(
        OIDCConfig(
            issuer="https://id.example.com",
            client_id="earnings",
            public_url="http://localhost",
            state_dir=settings.data_dir / "browser",
            allow_loopback_http=True,
        )
    )
    auth.store.save(
        "a" * 43,
        {
            "issuer": "https://id.example.com",
            "sub": "alice",
            "groups": [],
            "claims": {},
            "expires": datetime.now(timezone.utc).timestamp() + 300,
        },
    )
    PersonalStore(settings.db, "alice").watch("EMPTY")

    class _Dir:
        async def resolve(self, subject, **kwargs):
            return {
                "id": subject,
                "admitted": True,
                "disabled": False,
                "portfolio_admitted": True,
                "email": "alice@example.com",
            }

    app = create_app(settings, auth=auth, directory=_Dir())
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "coverage-warnings" in resp.text
        assert "EMPTY: no tracked events" in resp.text

    # Presentation handles DatePrecision.MONTH
    db_path = tmp_path / "month.sqlite"
    initialize(db_path)
    add_person(db_path, "alice")
    cat = Catalogue(db_path)
    cat.put_source(source("alpha", "ALPHA"))
    person = PersonalStore(db_path, "alice")
    person.watch("ALPHA")
    month_event = {
        "id": "earnings:alpha:2027Q2",
        "issuer_key": "alpha",
        "ticker": "ALPHA",
        "type": "earnings",
        "period": "2027Q2",
        "date": "2027-07-15",
        "date_precision": "month",
        "status": "expected",
        "source_url": "https://issuer.example/calendar",
        "evidence": "July 2027 | Q2 2027 Results",
    }
    presented = present(month_event, person, LibraryReader(db_path))
    assert presented["when"] == "Jul 2027 (month estimate)"


def test_commit_issuer_non_authoritative_retires_expired_expected_only(tmp_path):
    from datetime import date
    from earnings_watch.model.events import Event, EventStatus, EventType
    from earnings_watch.model.sources import FetchStrategy, PeriodScheme, Source
    from earnings_watch.store.database import Catalogue, initialize

    db_path = tmp_path / "earnings.db"
    initialize(db_path)
    cat = Catalogue(db_path)
    src = Source(
        key="alpha",
        issuer="Alpha Corp",
        ticker="ALP",
        name="Alpha Corp",
        calendar_url="https://ir.alpha.example/calendar",
        fetch=FetchStrategy.WEBSEARCH,
        period_scheme=PeriodScheme.CALENDAR_Q,
        fy_end_month=12,
    )
    cat.put_source(src)
    expired_expected = Event(
        id="earnings:alpha:2026Q3",
        issuer_key="alpha",
        ticker="ALP",
        type=EventType.EARNINGS,
        period="2026Q3",
        date=date(2026, 10, 20),
        status=EventStatus.EXPECTED,
        source_url="https://ir.alpha.example/calendar",
        evidence="Estimated from issuer historical reporting cadence (unconfirmed)",
        first_seen=date(2026, 10, 1),
        last_verified=date(2026, 10, 1),
    )
    future_confirmed = Event(
        id="earnings:alpha:2026Q4",
        issuer_key="alpha",
        ticker="ALP",
        type=EventType.EARNINGS,
        period="2026Q4",
        date=date(2027, 1, 28),
        status=EventStatus.IR_CONFIRMED,
        source_url="https://ir.alpha.example/calendar",
        evidence="28 January 2027 Q4 2026 Results",
        first_seen=date(2026, 10, 1),
        last_verified=date(2026, 10, 1),
    )
    cat.commit_issuer(
        "alpha", [expired_expected, future_confirmed], run_id="r1", authoritative=False
    )
    # Next non-authoritative sweep omits both: expired EXPECTED retires, confirmed stays.
    cat.commit_issuer("alpha", [], run_id="r2", authoritative=False, today=date(2026, 11, 27))
    remaining_ids = {row["id"] for row in cat.events()}
    assert "earnings:alpha:2026Q3" not in remaining_ids
    assert "earnings:alpha:2026Q4" in remaining_ids
