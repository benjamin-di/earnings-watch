from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import httpx
import pytest

from signin_window.oidc import BrowserAuth, OIDCConfig

from earnings_watch.composition import open_service
from earnings_watch.connections import create_app
from earnings_watch.engine.coverage import confirmation_deadline_days
from earnings_watch.model.events import Event, EventStatus, EventType
from earnings_watch.model.sources import FetchStrategy, PeriodScheme, Source
from earnings_watch.store.database import Catalogue, PersonalStore


def _src(
    key: str,
    ticker: str,
    *,
    exchange: str | None = None,
    sec_cik: int | None = None,
    hkex_stock_id: str | None = None,
    isin: str | None = None,
    calendar_url: str | None = None,
) -> Source:
    return Source(
        key=key,
        issuer=f"{ticker} Holdings",
        ticker=ticker,
        name=f"{ticker} Holdings",
        exchange=exchange,
        sec_cik=sec_cik,
        hkex_stock_id=hkex_stock_id,
        isin=isin,
        calendar_url=calendar_url or f"https://ir.{key}.example.com/events",
        fetch=FetchStrategy.WEBSEARCH,
        period_scheme=PeriodScheme.CALENDAR_Q,
        fy_end_month=12,
    )


def _ev(
    key: str,
    ticker: str,
    *,
    period: str,
    ev_date: date,
    status: EventStatus,
    first_seen: date | None = None,
    evidence: str = "Official IR release",
) -> Event:
    return Event(
        id=f"earnings:{key}:{period}",
        issuer_key=key,
        ticker=ticker,
        type=EventType.EARNINGS,
        period=period,
        date=ev_date,
        status=status,
        source_url=f"https://ir.{key}.example.com/events",
        evidence=evidence,
        first_seen=first_seen or ev_date,
        last_verified=ev_date,
    )


def test_confirmation_deadline_learns_from_observed_leads_and_falls_back_to_market_priors():
    today = date(2026, 11, 28)

    # US issuer with 8-day historical lead (e.g. announces 7-9 days prior) -> deadline 5d, typical 8d.
    us_src = _src("fastus", "FUS", exchange="XNAS", sec_cik=123456)
    us_events = [
        _ev(
            "fastus",
            "FUS",
            period="2026Q1",
            ev_date=date(2026, 3, 10),
            status=EventStatus.PAST,
            first_seen=date(2026, 3, 3),  # 7d
        ),
        _ev(
            "fastus",
            "FUS",
            period="2026Q2",
            ev_date=date(2026, 6, 10),
            status=EventStatus.PAST,
            first_seen=date(2026, 6, 1),  # 9d
        ),
        _ev(
            "fastus",
            "FUS",
            period="2026Q3",
            ev_date=date(2026, 9, 10),
            status=EventStatus.PAST,
            first_seen=date(2026, 9, 2),  # 8d
        ),
    ]
    deadline, typical = confirmation_deadline_days(us_src, us_events)
    assert deadline == 5
    assert typical == 8

    # EU annual-calendar issuer (120d observed lead) -> clamped at 30d deadline, typical 120d.
    eu_src = _src("eubank", "EUBK", exchange="XETR")
    eu_events = [
        _ev(
            "eubank",
            "EUBK",
            period="2026Q3",
            ev_date=today - timedelta(days=20),
            status=EventStatus.PAST,
            first_seen=today - timedelta(days=140),
        )
    ]
    eu_deadline, eu_typical = confirmation_deadline_days(eu_src, eu_events)
    assert eu_deadline == 30
    assert eu_typical == 120

    # Zero-history market priors: US=7d, HKEX=9d, other=14d.
    assert confirmation_deadline_days(_src("newus", "NUS", exchange="XNAS"), []) == (7, None)
    assert confirmation_deadline_days(_src("newhk", "09999", exchange="XHKG"), []) == (9, None)
    assert confirmation_deadline_days(_src("neweu", "NEU", exchange="XPAR"), []) == (14, None)


def test_adaptive_unconfirmed_soon_and_two_tier_coverage_status(settings):
    catalogue = Catalogue(settings.db)
    service = open_service(settings)
    now = datetime.now(timezone.utc)
    today = now.date()

    # 1. US issuer with 8d lead: expected in 11d -> NO warning; inline typical_lead_days == 8.
    us_src = _src("shortlead", "SHRT", exchange="XNAS", sec_cik=100001)
    catalogue.put_source(us_src)
    catalogue.commit_issuer(
        "shortlead",
        [
            _ev(
                "shortlead",
                "SHRT",
                period="2026Q3",
                ev_date=today - timedelta(days=80),
                status=EventStatus.PAST,
                first_seen=today - timedelta(days=88),
            ),
            _ev(
                "shortlead",
                "SHRT",
                period="2026Q4",
                ev_date=today + timedelta(days=11),
                status=EventStatus.EXPECTED,
                evidence="Estimated from issuer historical reporting cadence (unconfirmed)",
            ),
        ],
        run_id="seed-shrt",
    )
    catalogue.set_state(
        "fetch_state", "shortlead", {"last_ok": now.isoformat(), "consecutive_failures": 0}
    )
    service.watch("SHRT", "XNAS")

    # 2. Same 8d-lead profile, but expected in 4d -> OVERDUE, MUST emit unconfirmed_soon.
    overdue_src = _src("overdueus", "OVRD", exchange="XNAS", sec_cik=100002)
    catalogue.put_source(overdue_src)
    catalogue.commit_issuer(
        "overdueus",
        [
            _ev(
                "overdueus",
                "OVRD",
                period="2026Q3",
                ev_date=today - timedelta(days=87),
                status=EventStatus.PAST,
                first_seen=today - timedelta(days=95),
            ),
            _ev(
                "overdueus",
                "OVRD",
                period="2026Q4",
                ev_date=today + timedelta(days=4),
                status=EventStatus.EXPECTED,
                evidence="Estimated from issuer historical reporting cadence (unconfirmed)",
            ),
        ],
        run_id="seed-ovrd",
    )
    catalogue.set_state(
        "fetch_state", "overdueus", {"last_ok": now.isoformat(), "consecutive_failures": 0}
    )
    service.watch("OVRD", "XNAS")

    # 3. EU annual-calendar issuer (120d lead): expected in 20d -> MUST emit unconfirmed_soon.
    eu_src = _src("eucal", "EUC", exchange="XETR")
    catalogue.put_source(eu_src)
    catalogue.commit_issuer(
        "eucal",
        [
            _ev(
                "eucal",
                "EUC",
                period="2026Q3",
                ev_date=today - timedelta(days=70),
                status=EventStatus.PAST,
                first_seen=today - timedelta(days=190),
            ),
            _ev(
                "eucal",
                "EUC",
                period="2026Q4",
                ev_date=today + timedelta(days=20),
                status=EventStatus.EXPECTED,
                evidence="Estimated from issuer historical reporting cadence (unconfirmed)",
            ),
        ],
        run_id="seed-euc",
    )
    catalogue.set_state(
        "fetch_state", "eucal", {"last_ok": now.isoformat(), "consecutive_failures": 0}
    )
    service.watch("EUC", "XETR")

    # 4. HKEX issuer with 0 history: expected in 8d -> MUST emit unconfirmed_soon (9d prior).
    hk_src = _src("hkissuer", "08888", exchange="XHKG")
    catalogue.put_source(hk_src)
    catalogue.commit_issuer(
        "hkissuer",
        [
            _ev(
                "hkissuer",
                "08888",
                period="2026H2",
                ev_date=today + timedelta(days=8),
                status=EventStatus.EXPECTED,
                evidence="Estimated from issuer historical reporting cadence (unconfirmed)",
            )
        ],
        run_id="seed-hk",
    )
    catalogue.set_state(
        "fetch_state", "hkissuer", {"last_ok": now.isoformat(), "consecutive_failures": 0}
    )
    service.watch("08888", "XHKG")

    # 5. US issuer with 0 history, expected in 10d, degraded (consecutive_failures=1) -> emits warning (14d cutoff).
    deg_src = _src("degradedus", "DEGD", exchange="XNAS", sec_cik=100003)
    catalogue.put_source(deg_src)
    catalogue.commit_issuer(
        "degradedus",
        [
            _ev(
                "degradedus",
                "DEGD",
                period="2026Q4",
                ev_date=today + timedelta(days=10),
                status=EventStatus.EXPECTED,
                evidence="Estimated from issuer historical reporting cadence (unconfirmed)",
            )
        ],
        run_id="seed-degd",
    )
    catalogue.set_state(
        "fetch_state",
        "degradedus",
        {"last_ok": now.isoformat(), "consecutive_failures": 1, "error": "RuntimeError"},
    )
    service.watch("DEGD", "XNAS")

    # 6. Healthy newly-listed IPO (9d ago, 0 events) -> coverage_info (newly_listed), NOT coverage_warnings.
    ipo_src = _src("newipo", "NIPO", exchange="XNAS", sec_cik=200001)
    catalogue.put_source(ipo_src)
    catalogue.set_state(
        "fetch_state",
        "newipo",
        {
            "last_ok": now.isoformat(),
            "consecutive_failures": 0,
            "ipo_date": (today - timedelta(days=9)).isoformat(),
        },
    )
    service.watch("NIPO", "XNAS")

    # 7. Unhealthy 0-event issuer (never verified) -> MUST still emit no_events warning.
    broken_src = _src("brokenempty", "BRKE", exchange="XNAS")
    catalogue.put_source(broken_src)
    service.watch("BRKE", "XNAS")

    status = service.status()
    warn_keys = {(w["source_key"], w["kind"]) for w in status["coverage_warnings"]}
    info_keys = {(i["source_key"], i["kind"]) for i in status["coverage_info"]}

    assert ("shortlead", "unconfirmed_soon") not in warn_keys
    assert ("overdueus", "unconfirmed_soon") in warn_keys
    assert ("eucal", "unconfirmed_soon") in warn_keys
    assert ("hkissuer", "unconfirmed_soon") in warn_keys
    assert ("degradedus", "unconfirmed_soon") in warn_keys
    assert ("newipo", "no_events") not in warn_keys
    assert ("newipo", "newly_listed") in info_keys
    assert ("brokenempty", "no_events") in warn_keys

    # Upcoming payload carries typical_lead_days for shortlead.
    upcoming = service.upcoming(days=30, tickers=["SHRT"])
    assert len(upcoming["events"]) == 1
    assert upcoming["events"][0]["typical_lead_days"] == 8


@pytest.mark.asyncio
async def test_ui_renders_inline_lead_hint_and_newly_listed_company_badge_without_amber_noise(
    settings,
):
    catalogue = Catalogue(settings.db)
    now = datetime.now(timezone.utc)
    today = now.date()

    us_src = _src("calmus", "CALM", exchange="XNAS", sec_cik=300001)
    catalogue.put_source(us_src)
    catalogue.commit_issuer(
        "calmus",
        [
            _ev(
                "calmus",
                "CALM",
                period="2026Q3",
                ev_date=today - timedelta(days=80),
                status=EventStatus.PAST,
                first_seen=today - timedelta(days=88),
            ),
            _ev(
                "calmus",
                "CALM",
                period="2026Q4",
                ev_date=today + timedelta(days=11),
                status=EventStatus.EXPECTED,
                evidence="Estimated from issuer historical reporting cadence (unconfirmed)",
            ),
        ],
        run_id="seed-calm",
    )
    catalogue.set_state(
        "fetch_state", "calmus", {"last_ok": now.isoformat(), "consecutive_failures": 0}
    )

    ipo_src = _src("freshco", "FRSH", exchange="XNAS", sec_cik=300002)
    catalogue.put_source(ipo_src)
    catalogue.set_state(
        "fetch_state",
        "freshco",
        {
            "last_ok": now.isoformat(),
            "consecutive_failures": 0,
            "ipo_date": (today - timedelta(days=9)).isoformat(),
        },
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
        "c" * 43,
        {
            "issuer": "https://id.example.com",
            "sub": "alice",
            "groups": [],
            "claims": {},
            "expires": datetime.now(timezone.utc).timestamp() + 300,
        },
    )
    person = PersonalStore(settings.db, "alice")
    person.watch("CALM", "XNAS")
    person.watch("FRSH", "XNAS")

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
            cookies={auth.cookie: "c" * 43},
        ) as client,
    ):
        upcoming_resp = await client.get("/?view=upcoming")
        assert upcoming_resp.status_code == 200
        assert "coverage-warnings" not in upcoming_resp.text
        assert "Usually confirms ~8 days prior" in upcoming_resp.text

        companies_resp = await client.get("/?view=companies")
        assert companies_resp.status_code == 200
        assert "coverage-warnings" not in companies_resp.text
        assert "Newly listed" in companies_resp.text
        assert "awaiting first earnings" in companies_resp.text
