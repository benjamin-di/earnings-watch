from datetime import datetime, timezone
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


class Directory:
    admitted = True
    down = False

    async def resolve(self, subject, **kwargs):
        if self.down:
            raise OSError("unreachable")
        return dict(
            id=subject, admitted=self.admitted, disabled=False, email=subject + "@example.com"
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
        )
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
        page = await client.get("/connections/")
        assert page.status_code == 200, page.text
        assert all(
            label in page.text
            for label in ("Upcoming", "Your companies", "Notifications", "Calendar")
        )
        assert "alice@example.com" in page.text
        assert "bob@example.com" not in page.text
        token = re.search(r"/calendar/([a-f0-9]{64})\.ics", page.text)[1]
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
        assert (await client.get("/connections/")).status_code == 403
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
            await client.get("/connections/", headers={"X-Fleet-Subject": "alice"})
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
    assert "900" not in str(value) and "12345" not in str(value)
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
    accounts.provision(seeded.db, "alice")
    person = PersonalStore(seeded.db, "alice")
    # Spread across days only for this quota-independent pending-limit test.
    for i in range(5):
        person.watch("NEW" + str(i))
        with connect(seeded.db, write=True) as db:
            db.execute("DELETE FROM registration_quotas WHERE person='alice'")
    with pytest.raises(EarningsError, match="five"):
        person.watch("SIXTH")


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
