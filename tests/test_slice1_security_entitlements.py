"""Regression tests for Slice 1 security, entitlement, and hygiene fixes."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import threading
import time

import httpx
import pytest

from earnings_watch import calendar, outbox
from earnings_watch.composition import open_service
from earnings_watch.connections import create_app
from earnings_watch.errors import EarningsError
from earnings_watch.http_server import AuthenticatedApp
from earnings_watch.mcp_server import portfolio_admitted_context
from earnings_watch.settings import load as load_settings
from earnings_watch.store import accounts
from earnings_watch.store.database import (
    Catalogue,
    PersonalStore,
    connect,
    encode,
    sync_holdings,
)
from conftest import event, source


class _Directory:
    def __init__(self, entries: dict[str, dict], *, error: Exception | None = None):
        self.entries = entries
        self.error = error

    async def resolve(self, subject: str) -> dict:
        if self.error is not None:
            raise self.error
        return self.entries.get(
            subject,
            {"id": subject, "admitted": False, "disabled": False, "portfolio_admitted": False},
        )


class _BrowserAuth:
    def __init__(self, who: dict | None):
        self._who = who

    def identity(self, request):
        return self._who

    def origin_ok(self, request):
        return True

    def routes(self):
        return []

    def middleware(self, app):
        return app


class _Recorder:
    def __init__(self):
        self.calls: list[tuple[str, str, str, dict]] = []

    async def send(self, person: str, key: str, kind: str, payload: dict) -> str:
        self.calls.append((person, key, kind, payload))
        return f"msg-{len(self.calls)}"


def _seed_holding_and_watchlist(settings):
    cat = Catalogue(settings.db)
    cat.put_source(source(key="beta", ticker="BETA", exchange="XNAS"))
    today = datetime.now(timezone.utc).date()
    ev_alpha = event("alpha", "ALPHA", date=today + timedelta(days=2))
    ev_beta = event("beta", "BETA", date=today + timedelta(days=3))
    cat.commit_issuer("alpha", [ev_alpha], run_id="seed-a")
    cat.commit_issuer("beta", [ev_beta], run_id="seed-b")
    cat.put_document(
        {
            "id": "doc-alpha-rel",
            "event_id": ev_alpha.id,
            "role": "release",
            "url": "https://issuer.example/alpha-release.pdf",
            "title": "Alpha Q4 Release",
            "verified": True,
            "first_seen": "2026-11-26T00:00:00+00:00",
        }
    )
    cat.put_document(
        {
            "id": "doc-beta-stmt",
            "event_id": ev_beta.id,
            "role": "statements",
            "url": "https://issuer.example/beta-statements.pdf",
            "title": "Beta Q4 Statements",
            "verified": True,
            "first_seen": "2026-11-26T00:00:00+00:00",
        }
    )
    sync_holdings(settings.db, "alice", "portfolio", [{"ticker": "ALPHA", "exchange": "XNAS"}])
    PersonalStore(settings.db, "alice").watch("BETA", "XNAS")
    return ev_alpha, ev_beta


# ---------------------------------------------------------------------------
# 1. portfolio_admitted enforcement on /mcp & outbox._eligible()
# ---------------------------------------------------------------------------


async def _mcp_rpc(client: httpx.AsyncClient, method: str, params: dict | None = None) -> dict:
    resp = await client.post(
        "/mcp",
        headers={"Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
    )
    assert resp.status_code == 200, resp.text
    content_type = resp.headers.get("content-type", "")
    if "application/json" in content_type:
        return resp.json()["result"]
    for line in resp.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])["result"]
    raise AssertionError(f"Unexpected MCP response: {resp.text!r}")


async def test_mcp_gateway_enforces_portfolio_admitted_across_tools_and_ics(seeded, monkeypatch):
    ev_alpha, ev_beta = _seed_holding_and_watchlist(seeded)
    token = "g" * 40
    monkeypatch.setenv("TEST_GW_TOKEN", token)
    seeded.backend_tokens_env = ["TEST_GW_TOKEN"]

    directory = _Directory(
        {
            "alice": {
                "id": "alice",
                "admitted": True,
                "disabled": False,
                "portfolio_admitted": False,
            }
        }
    )
    app = AuthenticatedApp(seeded, mode="gateway", directory=directory)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Fleet-Subject": "alice",
        "X-Fleet-Person": "alice",
    }

    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers=headers,
        ) as client,
    ):
        wl_res = await _mcp_rpc(
            client, "tools/call", {"name": "earnings_watchlist", "arguments": {}}
        )
        wl = json.loads(wl_res["content"][0]["text"])
        assert [f["ticker"] for f in wl["follows"]] == ["BETA"]

        up_res = await _mcp_rpc(
            client, "tools/call", {"name": "earnings_upcoming", "arguments": {}}
        )
        up = json.loads(up_res["content"][0]["text"])
        assert [e["ticker"] for e in up["events"]] == ["BETA"]

        docs_res = await _mcp_rpc(
            client, "tools/call", {"name": "earnings_documents", "arguments": {}}
        )
        docs = json.loads(docs_res["content"][0]["text"])
        assert [d["id"] for d in docs["documents"]] == ["doc-beta-stmt"]

        ch_res = await _mcp_rpc(client, "tools/call", {"name": "earnings_changes", "arguments": {}})
        ch = json.loads(ch_res["content"][0]["text"])
        assert all(c["doc"]["issuer_key"] == "beta" for c in ch["changes"])
        assert ch["changes"]

        plan_res = await _mcp_rpc(
            client,
            "tools/call",
            {
                "name": "earnings_calendar_plan",
                "arguments": {"event_ids": [ev_alpha.id, ev_beta.id]},
            },
        )
        plan = json.loads(plan_res["content"][0]["text"])
        assert [op["key"] for op in plan["operations"]] == [f"earnings-watch:{ev_beta.id}"]

        ics_res = await _mcp_rpc(client, "resources/read", {"uri": "earnings://upcoming.ics"})
        ics_text = ics_res["contents"][0]["text"]
        assert "BETA" in ics_text
        assert "ALPHA" not in ics_text

    # ContextVar must be cleanly reset to its default after requests.
    assert portfolio_admitted_context.get() is True


async def test_mcp_gateway_allows_holdings_when_portfolio_admitted_true(seeded, monkeypatch):
    _seed_holding_and_watchlist(seeded)
    token = "h" * 40
    monkeypatch.setenv("TEST_GW_TOKEN", token)
    seeded.backend_tokens_env = ["TEST_GW_TOKEN"]

    directory = _Directory(
        {
            "alice": {
                "id": "alice",
                "admitted": True,
                "disabled": False,
                "portfolio_admitted": True,
            }
        }
    )
    app = AuthenticatedApp(seeded, mode="gateway", directory=directory)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Fleet-Subject": "alice",
        "X-Fleet-Person": "alice",
    }

    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            headers=headers,
        ) as client,
    ):
        wl_res = await _mcp_rpc(
            client, "tools/call", {"name": "earnings_watchlist", "arguments": {}}
        )
        wl = json.loads(wl_res["content"][0]["text"])
        assert sorted(f["ticker"] for f in wl["follows"]) == ["ALPHA", "BETA"]


async def test_outbox_deliver_respects_include_holdings_flag(seeded):
    ev_alpha, ev_beta = _seed_holding_and_watchlist(seeded)
    prof = accounts.profile(seeded.db, "alice")
    accounts.preferences(
        seeded.db,
        "alice",
        prof["revision"],
        prof["settings"] | {"results": True, "brief": True},
        "UTC",
    )

    # Holding-only results notification must be suppressed when include_holdings=False.
    outbox.enqueue(
        seeded.db,
        "alice",
        "results:alpha",
        "results",
        {"event": {"id": ev_alpha.id, "issuer_key": "alpha", "ticker": "ALPHA"}},
    )
    # Mixed brief must strip holding-only events/documents and keep watchlist items.
    outbox.enqueue(
        seeded.db,
        "alice",
        "brief:2026-11-27",
        "brief",
        {
            "sequence": 1,
            "cutoff": "2026-11-27T00:00:00+00:00",
            "events": [
                {"id": ev_alpha.id, "issuer_key": "alpha", "ticker": "ALPHA"},
                {"id": ev_beta.id, "issuer_key": "beta", "ticker": "BETA"},
            ],
            "changes": [],
            "documents": [
                {"id": "doc-alpha-rel", "event_id": ev_alpha.id},
                {"id": "doc-beta-stmt", "event_id": ev_beta.id},
            ],
        },
    )

    sender = _Recorder()
    res = await outbox.deliver(seeded.db, "alice", sender, include_holdings=False)
    assert res["sent"] == ["brief:2026-11-27"]
    assert len(sender.calls) == 1
    sent_payload = sender.calls[0][3]
    assert [e["issuer_key"] for e in sent_payload["events"]] == ["beta"]
    assert [d["id"] for d in sent_payload["documents"]] == ["doc-beta-stmt"]

    with connect(seeded.db) as db:
        row = db.execute(
            "SELECT state FROM deliveries WHERE person='alice' AND key='results:alpha'"
        ).fetchone()
        assert row["state"] == "suppressed"


# ---------------------------------------------------------------------------
# 2. Standalone portfolio_admitted consistency in /internal/targets & /internal/library-targets
# ---------------------------------------------------------------------------


async def test_standalone_internal_targets_and_library_targets_infer_portfolio_admitted(
    seeded, monkeypatch
):
    _seed_holding_and_watchlist(seeded)
    # Bob has only a watchlist follow, no holdings.
    PersonalStore(seeded.db, "bob").watch("BETA", "XNAS")

    t_tok = "t" * 40
    l_tok = "l" * 40
    monkeypatch.setenv("TARGETS_TOK", t_tok)
    monkeypatch.setenv("LIB_TARGETS_TOK", l_tok)
    seeded.targets_tokens_env = ["TARGETS_TOK"]
    seeded.library_targets_tokens_env = ["LIB_TARGETS_TOK"]

    app = create_app(seeded, mode="standalone", directory=None)
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        resp = await client.get("/internal/targets", headers={"Authorization": f"Bearer {t_tok}"})
        assert resp.status_code == 200
        by_person = {p["person"]: p for p in resp.json()["people"]}
        assert by_person["alice"]["portfolio_admitted"] is True
        assert {c["ticker"] for c in by_person["alice"]["companies"]} == {"ALPHA", "BETA"}
        assert by_person["bob"]["portfolio_admitted"] is False
        assert {c["ticker"] for c in by_person["bob"]["companies"]} == {"BETA"}

        lib_resp = await client.get(
            "/internal/library-targets", headers={"Authorization": f"Bearer {l_tok}"}
        )
        assert lib_resp.status_code == 200
        scopes = {s["person"]: s["source_keys"] for s in lib_resp.json()["scopes"]}
        assert scopes["alice"] == ["alpha", "beta"]
        assert scopes["bob"] == ["beta"]


# ---------------------------------------------------------------------------
# 3. Stored XSS fix in Connections UI
# ---------------------------------------------------------------------------


async def test_connections_ui_omits_unsafe_event_and_document_links(seeded):
    cat = Catalogue(seeded.db)
    today = datetime.now(timezone.utc).date()
    ev = event("alpha", "ALPHA", date=today + timedelta(days=2))
    cat.commit_issuer("alpha", [ev], run_id="xss-seed")
    # Inject an unsafe javascript: source_url directly into the stored event doc.
    with connect(seeded.db, write=True) as db:
        raw = json.loads(db.execute("SELECT doc FROM events WHERE id=?", (ev.id,)).fetchone()[0])
        raw["source_url"] = "javascript:alert(1)"
        db.execute("UPDATE events SET doc=? WHERE id=?", (encode(raw), ev.id))
    cat.put_document(
        {
            "id": "doc-xss",
            "event_id": ev.id,
            "role": "release",
            "url": "javascript:alert(2)",
            "title": "Evil Doc",
            "verified": True,
            "first_seen": "2026-11-26T00:00:00+00:00",
        }
    )
    cat.put_document(
        {
            "id": "doc-http",
            "event_id": ev.id,
            "role": "presentation",
            "url": "http://insecure.example/deck.pdf",
            "title": "Insecure Deck",
            "verified": True,
            "first_seen": "2026-11-26T00:00:00+00:00",
        }
    )
    cat.put_document(
        {
            "id": "doc-safe",
            "event_id": ev.id,
            "role": "statements",
            "url": "https://issuer.example/safe.pdf",
            "title": "Safe PDF",
            "verified": True,
            "first_seen": "2026-11-26T00:00:00+00:00",
        }
    )
    PersonalStore(seeded.db, "alice").watch("ALPHA", "XNAS")

    auth = _BrowserAuth({"sub": "alice", "name": "Alice", "claims": {"person_id": "alice"}})
    app = create_app(seeded, mode="standalone", auth=auth, directory=None)
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        resp = await client.get("/connections/?view=upcoming")
        assert resp.status_code == 200
        html_body = resp.text
        assert "javascript:" not in html_body
        assert "http://insecure.example/deck.pdf" not in html_body
        assert 'href="https://issuer.example/safe.pdf"' in html_body


# ---------------------------------------------------------------------------
# 4. HTTP status codes on auth failures
# ---------------------------------------------------------------------------


async def test_http_gateway_returns_401_on_provision_auth_error_and_503_on_transport_error(
    seeded, monkeypatch
):
    token = "k" * 40
    monkeypatch.setenv("TEST_GW_TOKEN", token)
    seeded.backend_tokens_env = ["TEST_GW_TOKEN"]

    # Case A: invalid subject causes accounts.provision() to raise EarningsError("unauthorized") -> 401
    directory_bad_sub = _Directory(
        {
            "bad subject!": {
                "id": "bad subject!",
                "admitted": True,
                "disabled": False,
                "portfolio_admitted": False,
            }
        }
    )
    app_bad = AuthenticatedApp(seeded, mode="gateway", directory=directory_bad_sub)
    async with (
        app_bad.app.router.lifespan_context(app_bad.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_bad), base_url="http://localhost"
        ) as client,
    ):
        r = await client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Fleet-Subject": "bad subject!",
                "X-Fleet-Person": "alice",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert r.status_code == 401
        assert r.json() == {"error": "unauthorized"}

    # Case B: directory transport failure -> 503
    directory_down = _Directory({}, error=RuntimeError("connection refused"))
    app_down = AuthenticatedApp(seeded, mode="gateway", directory=directory_down)
    async with (
        app_down.app.router.lifespan_context(app_down.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_down), base_url="http://localhost"
        ) as client,
    ):
        r = await client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Fleet-Subject": "alice",
                "X-Fleet-Person": "alice",
            },
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        )
        assert r.status_code == 503
        assert r.json() == {"error": "identity directory unavailable"}


async def test_connections_change_returns_403_when_identity_raises_unauthorized(seeded):
    auth = _BrowserAuth({"sub": "alice", "name": "Alice", "claims": {"person_id": "alice"}})
    directory = _Directory(
        {"alice": {"id": "alice", "admitted": False, "disabled": True, "portfolio_admitted": False}}
    )
    app = create_app(seeded, mode="gateway", auth=auth, directory=directory)
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        resp = await client.post(
            "/connections/watch",
            data={"revision": "0", "ticker": "ALPHA", "exchange": "XNAS"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# 5. HMAC domain separation in calendar.py
# ---------------------------------------------------------------------------


def test_calendar_plan_and_ack_enforce_purpose_domain_separation(seeded):
    PersonalStore(seeded.db, "alice").watch("ALPHA", "XNAS")
    svc = open_service(seeded, "alice")
    planned = svc.calendar_plan(event_ids=[event().id])
    data_b64, _sig = planned["handle"].rsplit(".", 1)
    decoded = json.loads(base64.urlsafe_b64decode(data_b64))
    assert decoded["purpose"] == "calendar_plan"

    # Forging a validly-signed handle with a different/missing purpose must fail with plan_stale.
    key = (seeded.data_dir / "calendar.key").read_bytes()
    for bad_payload in (
        {k: v for k, v in decoded.items() if k != "purpose"},
        decoded | {"purpose": "holdings_import", "exp": int(time.time()) + 600},
    ):
        bad_data = base64.urlsafe_b64encode(encode(bad_payload).encode()).decode()
        bad_sig = hmac.new(key, bad_data.encode(), hashlib.sha256).hexdigest()
        with pytest.raises(EarningsError) as exc_info:
            calendar.ack(svc.person, f"{bad_data}.{bad_sig}", [], key=key)
        assert exc_info.value.code == "plan_stale"


# ---------------------------------------------------------------------------
# 6. accounts.mute() validation & cap
# ---------------------------------------------------------------------------


def test_accounts_mute_validates_symbols_and_enforces_cap(seeded):
    with pytest.raises(EarningsError) as exc_info:
        accounts.mute(seeded.db, "alice", "bad ticker!", "XNAS", True)
    assert exc_info.value.code == "invalid_argument"

    with pytest.raises(EarningsError) as exc_info:
        accounts.mute(seeded.db, "alice", "ALPHA", "INVALID_EXCH", True)
    assert exc_info.value.code == "invalid_argument"

    # Exchange alias normalizes to MIC.
    accounts.mute(seeded.db, "alice", " alpha ", "NASDAQ", True)
    assert ("ALPHA", "XNAS") in accounts.mutes(seeded.db, "alice")

    # Fill up to MAX_MUTES = 200.
    for idx in range(1, accounts.MAX_MUTES):
        accounts.mute(seeded.db, "alice", f"T{idx:04d}", "XNAS", True)
    assert len(accounts.mutes(seeded.db, "alice")) == accounts.MAX_MUTES

    # Re-muting an existing entry at the cap is idempotent.
    accounts.mute(seeded.db, "alice", "ALPHA", "XNAS", True)

    # Adding a 201st distinct mute raises over_cap.
    with pytest.raises(EarningsError) as exc_info:
        accounts.mute(seeded.db, "alice", "OVER1", "XNAS", True)
    assert exc_info.value.code == "over_cap"


# ---------------------------------------------------------------------------
# 7. MCP earnings_documents roles filter aliases
# ---------------------------------------------------------------------------


def test_documents_roles_filter_maps_mcp_aliases_and_accepts_canonical(seeded):
    _seed_holding_and_watchlist(seeded)
    svc = open_service(seeded, "alice", include_holdings=True)

    by_alias_rel = svc.documents(roles=["press-release"])["documents"]
    assert [d["id"] for d in by_alias_rel] == ["doc-alpha-rel"]

    by_canon_rel = svc.documents(roles=["release"])["documents"]
    assert [d["id"] for d in by_canon_rel] == ["doc-alpha-rel"]

    by_alias_stmt = svc.documents(roles=["financial-statements"])["documents"]
    assert [d["id"] for d in by_alias_stmt] == ["doc-beta-stmt"]

    by_canon_stmt = svc.documents(roles=["statements"])["documents"]
    assert [d["id"] for d in by_canon_stmt] == ["doc-beta-stmt"]


# ---------------------------------------------------------------------------
# 8. Async event-loop offload & /health hygiene
# ---------------------------------------------------------------------------


async def test_health_offloads_integrity_check_off_event_loop(seeded, monkeypatch):
    loop_thread = threading.get_ident()
    observed_threads: list[int] = []

    def fake_integrity(_path):
        observed_threads.append(threading.get_ident())
        return "ok"

    monkeypatch.setattr("earnings_watch.connections.integrity", fake_integrity)
    app = create_app(seeded, mode="standalone")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://localhost"
    ) as client:
        resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"integrity": "ok"}
    assert observed_threads and observed_threads[0] != loop_thread


# ---------------------------------------------------------------------------
# 9. settings.load() whitespace stripping
# ---------------------------------------------------------------------------


def test_settings_load_strips_whitespace_and_empty_entries_in_list_envs(tmp_path, monkeypatch):
    monkeypatch.setenv("EARNINGS_WATCH_ALLOWED_HOSTS", " localhost , 127.0.0.1 , , [::1] ")
    monkeypatch.setenv("EARNINGS_WATCH_BACKEND_TOKENS_ENV", "  , TOK_A , TOK_B , ")
    loaded = load_settings(tmp_path / "nonexistent.toml")
    assert loaded.allowed_hosts == ["localhost", "127.0.0.1", "[::1]"]
    assert loaded.backend_tokens_env == ["TOK_A", "TOK_B"]
