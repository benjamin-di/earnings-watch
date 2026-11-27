"""Fail-first regressions for ASX discovery fallback, covered claim rejections, and notice scoping."""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path

import httpx
import pytest

from signin_window.oidc import BrowserAuth, OIDCConfig
from earnings_watch import open as open_service
from earnings_watch.connections import create_app
from earnings_watch.engine.cadence import project_expected_earnings
from earnings_watch.llm.runner import AgentResult, AgentTask
from earnings_watch.maintenance import Maintenance
from earnings_watch.model.events import Event, EventStatus, EventType, ExtractClaim, ExtractResult
from earnings_watch.model.sources import FetchStrategy, PeriodScheme, Source
from earnings_watch.providers import definitions
from earnings_watch.providers.asx import discover_asx
from earnings_watch.settings import Settings
from earnings_watch.store.database import Catalogue, PersonalStore, add_person, initialize


def _asx_source() -> Source:
    return Source(
        key="example-asx",
        issuer="Example Mining Limited",
        ticker="EXM",
        name="Example Mining",
        exchange="XASX",
        calendar_url="https://www.example.com/investors/key-dates",
        fetch=FetchStrategy.WEBSEARCH,
        period_scheme=PeriodScheme.HALF_YEAR,
        fy_end_month=6,
    )


_SAMPLE_ASX_HTML_2026 = """
<html><body><table>
<tr><th>Date</th><th>Price sens.</th><th><span>Headline</span></th></tr>
<tr>
  <td>27/11/2026<br><span class="dates-time">8:22 am</span></td>
  <td class="pricesens"></td>
  <td><a href="/asx/v2/statistics/displayAnnouncement.do?idsId=1">Update - Notification of buy-back - EXM<br><span class="page">2 pages</span></a></td>
</tr>
<tr>
  <td>19/11/2026<br><span class="dates-time">2:47 pm</span></td>
  <td class="pricesens"></td>
  <td><a href="/asx/v2/statistics/displayAnnouncement.do?idsId=2">Results of Annual General Meeting<br><span class="page">3 pages</span></a></td>
</tr>
<tr>
  <td>20/08/2026<br><span class="dates-time">7:52 am</span></td>
  <td class="pricesens"><img alt="Price sensitive"></td>
  <td><a href="/asx/v2/statistics/displayAnnouncement.do?idsId=3">EXM FY26 Appendix 4E<br><span class="page">2 pages</span></a></td>
</tr>
<tr>
  <td>19/02/2026<br><span class="dates-time">8:05 am</span></td>
  <td class="pricesens"><img alt="Price sensitive"></td>
  <td><a href="/asx/v2/statistics/displayAnnouncement.do?idsId=4">Appendix 4D Half year report 1H26<br><span class="page">2 pages</span></a></td>
</tr>
</table></body></html>
"""


@pytest.mark.asyncio
async def test_asx_falls_back_to_annual_html_when_markitdigital_capped_by_routine_filings():
    assert "www.asx.com.au" in definitions()["asx"]["hosts"]

    class FakeResp:
        def __init__(self, body: bytes, status: int = 200):
            self.status = status
            self.body = body

    requested_urls: list[str] = []

    class FakeTransport:
        async def get(self, url: str):
            requested_urls.append(url)
            if "asx.api.markitdigital.com" in url:
                payload = {
                    "data": {
                        "items": [
                            {
                                "headline": f"Update - Notification of buy-back - EXM ({i})",
                                "date": f"2026-11-{22 + i:02d}T00:00:00.000Z",
                            }
                            for i in range(5)
                        ]
                    }
                }
                return FakeResp(json.dumps(payload).encode())
            if "www.asx.com.au/asx/v2/statistics/announcements.do" in url and "year=2026" in url:
                return FakeResp(_SAMPLE_ASX_HTML_2026.encode())
            if "www.asx.com.au/asx/v2/statistics/announcements.do" in url and "year=2027" in url:
                return FakeResp(b"<html><body><table><tr><th>Date</th></tr></table></body></html>")
            raise AssertionError(f"Unexpected URL: {url}")

    src = _asx_source()
    docs, hist = await discover_asx(FakeTransport(), src, today=date(2026, 11, 28))
    assert date(2026, 2, 19) in hist
    assert date(2026, 8, 20) in hist
    assert any("EXM FY26 Appendix 4E" in text for text in docs.values())
    assert any("Results of Annual General Meeting" in text for text in docs.values())

    projected = project_expected_earnings(src, [], today=date(2026, 11, 28), historical_dates=hist)
    assert projected is not None
    assert projected.period == "2027H1"
    assert projected.date == date(2027, 2, 18)

    # Prior-year fallback when early in a new calendar year with no results filed yet
    docs_jan, hist_jan = await discover_asx(FakeTransport(), src, today=date(2027, 1, 15))
    assert date(2026, 8, 20) in hist_jan
    assert any("year=2026" in u for u in requested_urls)
    assert docs_jan


@pytest.mark.asyncio
async def test_maintenance_filters_already_covered_rejected_claims_from_warnings_and_errors(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path, max_calls_per_run=10)
    initialize(settings.db)
    cat = Catalogue(settings.db)
    src = Source(
        key="acme",
        issuer="Acme Corp",
        ticker="ACME",
        name="Acme Corp",
        exchange="XNYS",
        calendar_url="https://ir.acme.example/investors",
        fetch=FetchStrategy.WEBSEARCH,
        period_scheme=PeriodScheme.CALENDAR_Q,
        fy_end_month=12,
    )
    cat.put_source(src)
    add_person(settings.db, "owner")
    PersonalStore(settings.db, "owner").watch("ACME", "XNYS")

    # Seed an already-confirmed upcoming CMD, an already-past Q3 earnings, and an expected Q4 earnings.
    seen_at = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)
    existing = [
        Event(
            id="cmd:acme:2026",
            issuer_key="acme",
            ticker="ACME",
            type=EventType.CMD,
            period="2026",
            date=date(2026, 12, 8),
            status=EventStatus.IR_CONFIRMED,
            source_url="https://ir.acme.example/newsroom/investor-day",
            evidence="Acme announces Investor Day on December 8, 2026",
            first_seen=seen_at,
            last_verified=seen_at,
        ),
        Event(
            id="earnings:acme:2026Q3",
            issuer_key="acme",
            ticker="ACME",
            type=EventType.EARNINGS,
            period="2026Q3",
            date=date(2026, 11, 5),
            status=EventStatus.PAST,
            source_url="https://ir.acme.example/investors",
            evidence="05 November 2026 Q3 2026 Results",
            first_seen=seen_at,
            last_verified=seen_at,
        ),
        Event(
            id="earnings:acme:2026Q4",
            issuer_key="acme",
            ticker="ACME",
            type=EventType.EARNINGS,
            period="2026Q4",
            date=date(2027, 1, 28),
            status=EventStatus.EXPECTED,
            source_url="https://ir.acme.example/investors",
            evidence="Estimated from issuer historical reporting cadence",
            first_seen=seen_at,
            last_verified=seen_at,
        ),
    ]
    cat.commit_issuer("acme", existing, run_id="seed", authoritative=False)

    class OfflineTransport:
        async def get(self, url: str, **kwargs):
            raise RuntimeError("offline")

    # Case A: model emits unverified claims for already-confirmed CMD and already-past Q3.
    class CoveredRejectRunner:
        async def run(self, task: AgentTask, **kwargs) -> AgentResult:
            return AgentResult(
                ExtractResult(
                    events=[
                        ExtractClaim(
                            type=EventType.CMD,
                            period="2026",
                            date=date(2026, 12, 8),
                            evidence="not present on captured page",
                        ),
                        ExtractClaim(
                            type=EventType.EARNINGS,
                            period="2026Q3",
                            date=date(2026, 11, 6),
                            evidence="not present on captured page",
                        ),
                    ],
                    complete=True,
                    page_relevant=True,
                ),
                "fake",
            )

    maint = Maintenance(settings, runner=CoveredRejectRunner(), transport=OfflineTransport())
    res_a = await maint.sweep(force=True)
    assert res_a["exit"] == 0
    assert res_a["errors"] == []
    state_a = cat.state("fetch_state", "acme")
    assert state_a["rejected"] == []
    svc = open_service(settings, person="owner")
    assert not any(w["kind"] == "claims_rejected" for w in svc.status()["coverage_warnings"])

    # Case B: model emits an unverified claim for 2026Q4 (only covered by an EXPECTED placeholder).
    class ExpectedOnlyRejectRunner:
        async def run(self, task: AgentTask, **kwargs) -> AgentResult:
            return AgentResult(
                ExtractResult(
                    events=[
                        ExtractClaim(
                            type=EventType.EARNINGS,
                            period="2026Q4",
                            date=date(2027, 1, 29),
                            evidence="not present on captured page",
                        )
                    ],
                    complete=True,
                    page_relevant=True,
                ),
                "fake",
            )

    maint_b = Maintenance(settings, runner=ExpectedOnlyRejectRunner(), transport=OfflineTransport())
    res_b = await maint_b.sweep(force=True)
    assert any(
        e.get("source") == "acme" and e.get("reason") == "claims_rejected" for e in res_b["errors"]
    )
    state_b = cat.state("fetch_state", "acme")
    assert len(state_b["rejected"]) == 1
    assert any(
        w["source_key"] == "acme" and w["kind"] == "claims_rejected"
        for w in svc.status()["coverage_warnings"]
    )


@pytest.mark.asyncio
async def test_connections_hub_scopes_coverage_warnings_to_upcoming_and_companies_views(
    settings,
):
    catalogue = Catalogue(settings.db)
    catalogue.put_source(
        Source(
            key="emptyco",
            issuer="Empty Co",
            ticker="EMPTY",
            name="Empty Co",
            calendar_url="https://ir.empty.example/calendar",
            fetch=FetchStrategy.PLAIN,
            period_scheme=PeriodScheme.CALENDAR_Q,
            fy_end_month=12,
        )
    )
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
        "b" * 43,
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
            cookies={auth.cookie: "b" * 43},
        ) as client,
    ):
        for visible_view in ("upcoming", "companies"):
            resp = await client.get(f"/?view={visible_view}")
            assert resp.status_code == 200
            assert "coverage-warnings" in resp.text
        for hidden_view in ("digests", "calendar"):
            resp = await client.get(f"/?view={hidden_view}")
            assert resp.status_code == 200
            assert "coverage-warnings" not in resp.text
