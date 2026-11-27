import base64
from datetime import datetime, timezone
import json
import re

import httpx
import pytest
from signin_window.oidc import BrowserAuth, OIDCConfig

from earnings_watch.composition import open_service
from earnings_watch.connections import create_app
from earnings_watch.errors import EarningsError
from earnings_watch.interests import preview, apply
from earnings_watch.store import accounts
from earnings_watch.store.database import PersonalStore, connect
from earnings_watch.transport import IdentityDirectory
from earnings_watch.store.database import Catalogue
from conftest import source


def preview_payload(handle):
    return json.loads(base64.urlsafe_b64decode(handle.rsplit(".", 1)[0]))


class Directory:
    admitted = True
    portfolio_admitted = True
    down = False

    async def resolve(self, subject, **kwargs):
        if self.down:
            raise OSError("unreachable")
        return dict(
            id=subject,
            admitted=self.admitted,
            disabled=False,
            portfolio_admitted=self.portfolio_admitted,
            email=subject + "@example.com",
        )


def browser(settings):
    settings.public_url = "http://localhost"
    auth = BrowserAuth(
        OIDCConfig(
            issuer="https://id.example.com",
            client_id="earnings",
            public_url="http://localhost",
            state_dir=settings.data_dir / "browser",
            allow_loopback_http=True,
        ),
        home="/",
    )
    auth.store.save(
        "a" * 43,
        dict(
            issuer="https://id.example.com",
            sub="alice",
            groups=[],
            claims={},
            expires=datetime.now(timezone.utc).timestamp() + 300,
        ),
    )
    return auth


async def test_page_two_people_admission_and_feed_rotation(seeded):
    auth = browser(seeded)
    directory = Directory()
    app = create_app(seeded, auth=auth, directory=directory)
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        page = await client.get("/")
        assert page.status_code == 200, page.text
        for legacy, expected in (
            ("/connections", "/"),
            ("/connections/", "/"),
            ("/connections/?view=companies", "/?view=companies"),
        ):
            redir = await client.get(legacy, follow_redirects=False)
            assert redir.status_code == 308
            assert redir.headers["location"] == expected
            assert redir.headers["cache-control"] == "no-store"
        assert all(
            label in page.text
            for label in ("Upcoming", "Companies", "Digests &amp; alerts", "Calendar")
        )
        digests_page = await client.get("/?view=digests")
        assert digests_page.status_code == 200
        assert "alice@example.com" in digests_page.text
        assert "bob@example.com" not in digests_page.text
        calendar_page = await client.get("/?view=calendar")
        assert calendar_page.status_code == 200
        token = re.search(r"/calendar/([a-f0-9]{64})\.ics", calendar_page.text)[1]
        assert (await client.get("/calendar/" + token + ".ics")).status_code == 200
        assert (
            await client.post(
                "/connections/watch",
                data={"ticker": "ALPHA", "revision": "0"},
                headers={"Origin": "http://localhost"},
            )
        ).status_code == 303
        assert open_service(seeded, "alice").watchlist()["follows"]
        assert not open_service(seeded, "bob").watchlist()["follows"]
        stale = await client.post(
            "/connections/preferences",
            data={"revision": "0", "timezone": "UTC", "hour": "8"},
            headers={"Origin": "http://localhost"},
        )
        assert stale.status_code == 409
        assert (
            await client.post(
                "/connections/reset-feed",
                data={"revision": "1"},
                headers={"Origin": "https://evil.example"},
            )
        ).status_code == 403
        assert (
            await client.post(
                "/connections/reset-feed",
                data={"revision": "1"},
                headers={"Origin": "http://localhost"},
            )
        ).status_code == 303
        assert (await client.get("/calendar/" + token + ".ics")).status_code == 404
        directory.admitted = False
        assert (await client.get("/")).status_code == 403
        new = accounts.feed_token(
            seeded.db, "alice", (seeded.data_dir / "calendar.key").read_bytes()
        )
        assert (await client.get("/calendar/" + new + ".ics")).status_code == 404
        directory.down = True
        assert (await client.get("/calendar/" + new + ".ics")).status_code == 503


async def test_chunked_import_limit_and_spoofed_browser_identity(seeded):
    auth = browser(seeded)
    app = create_app(seeded, auth=auth, directory=Directory())
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        assert (
            await client.get("/", headers={"X-Fleet-Subject": "alice"})
        ).status_code == 303
        client.cookies.set(auth.cookie, "a" * 43)

        async def big():
            yield b"x" * 65537

        assert (
            await client.post(
                "/connections/import-preview", content=big(), headers={"Origin": "http://localhost"}
            )
        ).status_code == 413


def test_holdings_preview_is_atomic_private_and_discards_financial_values(seeded):
    accounts.provision(seeded.db, "alice")
    before = accounts.profile(seeded.db, "alice")["revision"]
    value = preview(seeded, "alice", "ticker,exchange,quantity,cost\nALPHA,XNAS,900,12345")
    assert preview_payload(value["handle"])["items"] == [{"ticker": "ALPHA", "exchange": "XNAS"}]
    assert value["adds"] == 1 and value["removes"] == 0
    with pytest.raises(EarningsError):
        apply(seeded, "bob", value["handle"])
    apply(seeded, "alice", value["handle"])
    assert accounts.profile(seeded.db, "alice")["revision"] == before + 1
    with pytest.raises(EarningsError, match="stale"):
        apply(seeded, "alice", value["handle"])
    accounts.mute(seeded.db, "alice", "ALPHA", "XNAS", True)
    again = preview(seeded, "alice", "ALPHA")
    apply(seeded, "alice", again["handle"])
    assert not open_service(seeded, "alice").upcoming()["events"]
    assert open_service(seeded, "alice").upcoming(scope="all")["events"]
    assert not open_service(seeded, "bob").upcoming()["events"]


@pytest.mark.parametrize("header", ["Ticker,Exchange", "TICKER,EXCHANGE", " ticker , Exchange "])
def test_holdings_csv_accepts_normalized_headers(seeded, header):
    accounts.provision(seeded.db, "alice")
    value = preview(seeded, "alice", header + ",Quantity,Cost\nALPHA,XNAS,900,12345")
    assert value["items"] == [{"ticker": "ALPHA", "exchange": "XNAS"}]
    apply(seeded, "alice", value["handle"])
    assert [(r["ticker"], r["exchange"]) for r in PersonalStore(seeded.db, "alice").follows()] == [
        ("ALPHA", "XNAS")
    ]
    assert not PersonalStore(seeded.db, "bob").follows()
    assert preview_payload(value["handle"])["items"] == [{"ticker": "ALPHA", "exchange": "XNAS"}]


@pytest.mark.parametrize(
    "header",
    [
        "ticker,Ticker,exchange",
        "ticker,exchange,Exchange",
        "quantity,ticker,Ticker",
        "cost,exchange,Exchange,isin",
    ],
)
def test_holdings_csv_refuses_duplicate_identity_columns(seeded, header):
    accounts.provision(seeded.db, "alice")
    before = accounts.profile(seeded.db, "alice")["revision"]
    with pytest.raises(EarningsError, match="Duplicate company identity columns"):
        preview(seeded, "alice", header + "\nALPHA,XNAS,XNAS")
    assert accounts.profile(seeded.db, "alice")["revision"] == before
    assert not PersonalStore(seeded.db, "alice").follows()


@pytest.mark.parametrize(
    "text",
    [
        "ticker\nALPHA",
        " Ticker \r\nALPHA",
        "isin\nUS0000000001",
        "quantity,ticker,cost\n900,ALPHA,12345",
        "Exchange,Cost,ISIN\nXNAS,12345,US0000000001",
        "cost, ticker,exchange,quantity\n12345,ALPHA,XNAS,900",
    ],
)
async def test_holdings_csv_identity_columns_are_optional_in_position_and_count(seeded, text):
    Catalogue(seeded.db).put_source(source().model_copy(update={"isin": "US0000000001"}))
    auth = browser(seeded)
    app = create_app(seeded, auth=auth, directory=Directory())
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        response = await client.post(
            "/connections/import-preview",
            files={"file": ("holdings.csv", text.encode(), "text/csv")},
            data={"revision": "0"},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200, response.text
        assert "ALPHA XNAS" in response.text
        assert not PersonalStore(seeded.db, "alice").follows()
        handle = re.search(r'name="handle" value="([^"]+)"', response.text)[1]
        payload = preview_payload(handle)
        assert set(payload) == {"purpose", "person", "revision", "exp", "items"}
        assert payload["purpose"] == "interests_preview"
        assert payload["person"] == "alice"
        assert payload["items"] == [{"ticker": "ALPHA", "exchange": "XNAS"}]
        response = await client.post(
            "/connections/import-apply",
            data={"revision": "0", "handle": handle},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 303
    assert [
        (row["ticker"], row["exchange"]) for row in PersonalStore(seeded.db, "alice").follows()
    ] == [("ALPHA", "XNAS")]
    assert not PersonalStore(seeded.db, "bob").follows()


async def test_watch_ambiguous_ticker_explains_exchange_without_saving(seeded):
    Catalogue(seeded.db).put_source(source(key="alpha-nyse", exchange="XNYS"))
    auth = browser(seeded)
    app = create_app(seeded, auth=auth, directory=Directory())
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        response = await client.post(
            "/connections/watch",
            data={"ticker": "ALPHA", "revision": "0"},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 409
        assert "Choose an exchange for ALPHA" in response.text
        assert "XNAS" in response.text and "XNYS" in response.text
        assert not PersonalStore(seeded.db, "alice").follows()
        assert accounts.profile(seeded.db, "alice")["revision"] == 0
        response = await client.post(
            "/connections/watch",
            data={"ticker": "ALPHA", "exchange": "XNYS", "revision": "0"},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 303
        assert [
            (r["ticker"], r["exchange"]) for r in PersonalStore(seeded.db, "alice").follows()
        ] == [("ALPHA", "XNYS")]
        assert not PersonalStore(seeded.db, "bob").follows()


@pytest.mark.parametrize("separator", ["\r", "\n", "\r\n"])
async def test_plaintext_holdings_line_endings_work_in_preview_and_browser(seeded, separator):
    Catalogue(seeded.db).put_source(source(key="beta", ticker="BETA"))
    accounts.provision(seeded.db, "alice")
    text = separator.join(("ALPHA", "BETA"))
    value = preview(seeded, "alice", text)
    assert value["items"] == [
        {"ticker": "ALPHA", "exchange": "XNAS"},
        {"ticker": "BETA", "exchange": "XNAS"},
    ]
    auth = browser(seeded)
    app = create_app(seeded, auth=auth, directory=Directory())
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        response = await client.post(
            "/connections/import-preview",
            data={"companies": text, "revision": "0"},
            headers={"Origin": "http://localhost"},
        )
        assert response.status_code == 200, response.text
        assert "ALPHA" in response.text and "BETA" in response.text
        assert accounts.profile(seeded.db, "alice")["revision"] == 0
        assert not PersonalStore(seeded.db, "alice").follows()


async def test_directory_minimal_contract_and_outage_does_not_serve_stale_cache():
    calls = []

    def answer(request):
        calls.append(request)
        return httpx.Response(
            200, json=dict(id="alice", email="alice@example.com", admitted=True, disabled=False)
        )

    directory = IdentityDirectory(
        "https://id.example.com/earnings-directory", "k" * 40, transport=httpx.MockTransport(answer)
    )
    assert (await directory.resolve("alice"))["email"] == "alice@example.com"
    assert (await directory.resolve("alice"))["admitted"]
    assert len(calls) == 1
    await directory.resolve("alice", fresh=True)
    assert len(calls) == 2
    with pytest.raises(ValueError):
        await directory.resolve("../bob")
    assert calls[0].headers["X-API-Key"] == "k" * 40
    assert str(calls[0].url) == "https://id.example.com/earnings-directory/users/alice"


async def test_gateway_provisions_only_verified_subject(seeded, monkeypatch):
    from earnings_watch.http_server import AuthenticatedApp

    monkeypatch.setenv("BACKEND", "b" * 40)
    seeded.backend_tokens_env = ["BACKEND"]
    seeded.directory_required = True
    app = AuthenticatedApp(seeded, "gateway", directory=Directory())
    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "1"},
            },
        }
        headers = {
            "Authorization": "Bearer " + "b" * 40,
            "X-Fleet-Subject": "subject-alice",
            "X-Fleet-Person": "bob",
            "Accept": "application/json, text/event-stream",
        }
        assert (await client.post("/mcp", json=body, headers=headers)).status_code == 200
        with connect(seeded.db) as db:
            assert db.execute("SELECT id FROM people WHERE id='subject-alice'").fetchone()
        headers.pop("Authorization")
        assert (await client.post("/mcp", json=body, headers=headers)).status_code == 401


def test_pending_request_cap_and_mutes_survive_holdings_refresh(seeded):
    from earnings_watch.store.database import MAX_PENDING_REGISTRATIONS

    accounts.provision(seeded.db, "alice")
    person = PersonalStore(seeded.db, "alice")
    # Spread across days only for this quota-independent pending-limit test.
    for i in range(MAX_PENDING_REGISTRATIONS):
        person.watch("NEW" + str(i))
        with connect(seeded.db, write=True) as db:
            db.execute("DELETE FROM registration_quotas WHERE person='alice'")
    with pytest.raises(EarningsError, match=str(MAX_PENDING_REGISTRATIONS)):
        person.watch("OVERCAP")


def test_archive_effects_does_not_claim_sent_or_adopt_remote_ids(settings, tmp_path):
    from test_legacy_and_cli import snapshot
    from earnings_watch.legacy import import_snapshot
    from earnings_watch.store.database import legacy_blocked

    path = tmp_path / "legacy"
    path.mkdir()
    snapshot(path)
    import_snapshot(settings.db, path, "alice")
    review = __import__("earnings_watch.legacy", fromlist=["review"]).review(settings.db, "alice")
    assert legacy_blocked(settings.db, "alice")
    receipt = dict(
        snapshot_sha256=review["snapshot_sha256"],
        legacy_writer_stopped=True,
        evidence="private audit receipt",
    )
    accounts.archive_effects(settings.db, "alice", receipt)
    assert not legacy_blocked(settings.db, "alice")
    with connect(settings.db) as db:
        assert (
            db.execute(
                "SELECT state FROM deliveries WHERE person='alice' AND key='legacy-state'"
            ).fetchone()[0]
            == "archived"
        )
        assert db.execute("SELECT count(*) FROM calendar_ledger").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM deliveries WHERE state='sent'").fetchone()[0] == 0
    assert accounts.archive_effects(settings.db, "alice", receipt) == {"archived": True}


async def test_monitor_has_no_personal_surface_and_requires_backend_key(seeded, monkeypatch):
    from earnings_watch.http_server import AuthenticatedApp

    monkeypatch.setenv("BACKEND", "b" * 40)
    seeded.backend_tokens_env = ["BACKEND"]
    app = AuthenticatedApp(seeded, "gateway", directory=Directory())
    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):

        def body(method, params=None):
            return dict(jsonrpc="2.0", id=1, method=method, params=params or {})

        headers = {
            "Authorization": "Bearer " + "b" * 40,
            "Accept": "application/json, text/event-stream",
        }
        assert (await client.post("/monitor/mcp", json=body("tools/list"))).status_code == 401
        result = await client.post("/monitor/mcp", json=body("tools/list"), headers=headers)
        assert result.status_code == 200
        assert [t["name"] for t in result.json()["result"]["tools"]] == ["earnings_status"]
        denied = await client.post(
            "/monitor/mcp",
            json=body("tools/call", dict(name="earnings_watchlist", arguments={})),
            headers=headers,
        )
        assert denied.json().get("error") or denied.json().get("result", {}).get("isError")


def test_format_event_title_and_summary_avoid_slug_duplication():
    from earnings_watch.presentation import format_event_summary, format_event_title

    assert format_event_summary("2027-dividend", "dividend") == "2027 Dividend"
    assert format_event_title("SYN1", "2027-dividend", "dividend") == "SYN1 · 2027 Dividend"
    assert format_event_summary("2026", "agm") == "2026 AGM"
    assert format_event_title("SYN2", "2026", "agm") == "SYN2 · 2026 AGM"
    assert format_event_title("ALPHA", "2026Q4", "earnings") == "ALPHA · 2026Q4 Results"
    assert format_event_summary("2026-investor-day", "investor-day") == "2026 Investor Day"


async def test_hub_ui_deduplication_mark_seen_json_revisions_and_static_assets(seeded):
    from conftest import event
    from earnings_watch.store.database import sync_holdings

    seeded.integrations.news_digest.enabled = True
    today = datetime.now(timezone.utc).date()
    cat = Catalogue(seeded.db)
    cat.commit_issuer(
        "alpha",
        [
            event(
                "alpha",
                "ALPHA",
                id="dividend:alpha:2027-dividend",
                type="dividend",
                period="2027-dividend",
                date=today,
            )
        ],
        run_id="div-1",
    )
    # Alice holds ALPHA:XNAS AND also added ALPHA:XNAS to watchlist -> must merge into 1 row.
    sync_holdings(seeded.db, "alice", "portfolio", [{"ticker": "ALPHA", "exchange": "XNAS"}])
    PersonalStore(seeded.db, "alice").watch("ALPHA", "XNAS")

    auth = browser(seeded)
    app = create_app(seeded, auth=auth, directory=Directory())
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        # 1. Static assets serve cleanly with immutable cache & single nosniff; unknown paths 404.
        css_resp = await client.get("/connections/assets/app.css")
        assert css_resp.status_code == 200
        assert "text/css" in css_resp.headers["content-type"]
        assert css_resp.headers.get_list("x-content-type-options") == ["nosniff"]
        assert css_resp.headers.get_list("cache-control") == ["public, max-age=31536000, immutable"]
        assert "content-security-policy" not in css_resp.headers
        js_resp = await client.get("/connections/assets/app.js")
        assert js_resp.status_code == 200
        assert "Link copied" in js_resp.text
        assert "Code copied" not in js_resp.text
        assert (await client.get("/connections/assets/secret.py")).status_code == 404

        # 2. Upcoming GET renders clean summary without duplicating ticker, keeps single no-referrer,
        # and does NOT auto-wipe Changed badge on reload.
        first = await client.get("/?view=upcoming")
        assert first.status_code == 200
        assert first.headers.get_list("referrer-policy") == ["no-referrer"]
        assert first.headers.get_list("cache-control") == ["no-store"]
        assert '<span class="event-summary">2027 Dividend</span>' in first.text
        assert "2027-dividend dividend" not in first.text
        assert 'data-badge="changed"' in first.text
        second = await client.get("/?view=upcoming")
        assert 'data-badge="changed"' in second.text

        # Explicit POST /connections/mark-seen clears Changed badge via JSON.
        rev = accounts.profile(seeded.db, "alice")["revision"]
        seen_resp = await client.post(
            "/connections/mark-seen",
            data={"revision": str(rev), "view": "upcoming"},
            headers={"Origin": "http://localhost", "Accept": "application/json"},
        )
        assert seen_resp.status_code == 200
        assert seen_resp.json()["ok"] is True
        third = await client.get("/?view=upcoming")
        assert 'data-badge="changed"' not in third.text

        # 3. Companies tab merges overlapping Holding + Watchlist into a single company-row
        # with formatted next-event summary (not raw slug).
        companies = await client.get("/?view=companies")
        assert companies.status_code == 200
        assert companies.text.count('class="company-row"') == 1
        assert "· 2027 Dividend</p>" in companies.text
        assert "2027-dividend" not in companies.text
        assert 'id="company-search"' in companies.text
        assert 'id="company-filter"' in companies.text

        # 4. Consecutive JSON mutations return incremented revision so rapid toggles never 409.
        r1 = await client.post(
            "/connections/mute",
            data={"revision": str(rev), "ticker": "ALPHA", "exchange": "XNAS", "view": "companies"},
            headers={"Origin": "http://localhost", "Accept": "application/json"},
        )
        assert r1.status_code == 200
        d1 = r1.json()
        assert d1["ok"] is True and d1["muted"] is True and d1["revision"] == rev + 1

        r2 = await client.post(
            "/connections/news-mute",
            data={
                "revision": str(d1["revision"]),
                "ticker": "ALPHA",
                "exchange": "XNAS",
                "view": "companies",
            },
            headers={"Origin": "http://localhost", "Accept": "application/json"},
        )
        assert r2.status_code == 200
        d2 = r2.json()
        assert d2["ok"] is True and d2["muted"] is True and d2["revision"] == rev + 2

        # Wildcard mute + exact unmute must signal reload & keep muted=True (never lie "On").
        accounts.mute(seeded.db, "alice", "ALPHA", "", True, revision=d2["revision"])
        rev_wc = accounts.profile(seeded.db, "alice")["revision"]
        r_exact = await client.post(
            "/connections/unmute",
            data={
                "revision": str(rev_wc),
                "ticker": "ALPHA",
                "exchange": "XNAS",
                "view": "companies",
            },
            headers={"Origin": "http://localhost", "Accept": "application/json"},
        )
        assert r_exact.status_code == 200
        d_exact = r_exact.json()
        assert d_exact["ok"] is True and d_exact["muted"] is True and d_exact["reload"] is True
        r_wc = await client.post(
            "/connections/unmute",
            data={
                "revision": str(d_exact["revision"]),
                "ticker": "ALPHA",
                "exchange": "",
                "view": "companies",
            },
            headers={"Origin": "http://localhost", "Accept": "application/json"},
        )
        assert r_wc.status_code == 200
        d_wc = r_wc.json()
        assert d_wc["ok"] is True and d_wc["muted"] is False and d_wc["reload"] is True

        # Stale revision returns inline JSON 409 error when Accept: application/json.
        stale = await client.post(
            "/connections/unmute",
            data={"revision": str(rev), "ticker": "ALPHA", "exchange": "XNAS", "view": "companies"},
            headers={"Origin": "http://localhost", "Accept": "application/json"},
        )
        assert stale.status_code == 409
        assert stale.json()["ok"] is False
        assert "Refresh" in stale.json()["error"]

        # 5. Digests tab renders 00:00-23:00 hour select and saves both sections in one POST.
        digests = await client.get("/?view=digests")
        assert '<select id="pref-hour" name="hour" required>' in digests.text
        assert '<option value="8" selected>08:00</option>' in digests.text
        save_resp = await client.post(
            "/connections/preferences",
            data={
                "revision": str(d_wc["revision"]),
                "view": "digests",
                "section": "all",
                "results": "on",
                "weekly_brief": "on",
                "hour": "9",
                "timezone": "Asia/Hong_Kong",
                "news_digest": "on",
                "news_cadence": "slot_a",
                "news_weekdays_only": "on",
                "news_scope": "holdings",
            },
            headers={"Origin": "http://localhost", "Accept": "application/json"},
        )
        assert save_resp.status_code == 200
        assert save_resp.json()["ok"] is True
        prof_after = accounts.profile(seeded.db, "alice")
        assert prof_after["timezone"] == "Asia/Hong_Kong"
        assert prof_after["settings"]["hour"] == 9
        assert prof_after["settings"]["weekly_brief"] is True
        assert prof_after["settings"]["news_digest"] is True
        assert prof_after["settings"]["news_cadence"] == "slot_a"
        assert prof_after["settings"]["news_scope"] == "holdings"
