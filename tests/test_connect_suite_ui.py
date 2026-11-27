"""Structure, suite-switcher, escaping, and session-compatibility tests for Connect suite UI."""

from __future__ import annotations

import json
import time

import httpx
import pytest
from signin_window.connections import SuiteDirectory
from signin_window.oidc import BrowserAuth, OIDCConfig

from earnings_watch import connections
from earnings_watch.errors import EarningsError
from earnings_watch.store import accounts
from earnings_watch.ui import hub_page, render_coverage_warnings


def _suite_json(*, endpoint_on_earnings: bool = False) -> str:
    return json.dumps(
        {
            "name": "Connect",
            "endpoint": {
                "title": "Assistant address",
                "url": "https://mcp.example.com/mcp",
                "help": "Paste this address into your assistant.",
            },
            "apps": [
                {
                    "label": "Earnings",
                    "url": "https://earnings.example.com/connections/",
                    "section": "Workspaces",
                    "description": "Dates and documents for your companies",
                    "groups": ["earnings-users"],
                    "endpoint": endpoint_on_earnings,
                },
                {
                    "label": "Filings",
                    "url": "https://filings.example.com/",
                    "section": "Workspaces",
                    "description": "Read and compare company filings",
                    "groups": ["filings-users"],
                },
                {
                    "label": "Restricted App",
                    "url": "https://restricted.example.com/",
                    "section": "Accounts",
                    "description": "Private workspace",
                    "groups": ["other-group"],
                },
            ],
        }
    )


def _auth(
    settings,
    *,
    groups: list[str] | None = None,
    suite: SuiteDirectory | None = None,
    name: str = "Alex Example",
    sid: str = "a" * 43,
) -> BrowserAuth:
    auth = BrowserAuth(
        OIDCConfig(
            issuer="https://identity.example.com",
            client_id="earnings-connections",
            public_url="https://earnings.example.com",
            state_dir=settings.data_dir / "browser",
            required_group="earnings-users",
            allow_loopback_http=True,
        ),
        suite=suite,
    )
    auth.store.save(
        sid,
        {
            "issuer": "https://identity.example.com",
            "sub": "alice",
            "name": name,
            "groups": groups if groups is not None else ["earnings-users", "filings-users"],
            "claims": {"person_id": "alice", "email": "alice@example.com"},
            "expires": time.time() + 600,
        },
    )
    return auth


@pytest.mark.asyncio
async def test_suite_switcher_present_filtered_and_endpoint_opt_in(seeded):
    seeded.public_url = "https://earnings.example.com"
    seeded.allowed_hosts = ["earnings.example.com", "localhost"]
    suite_dir = SuiteDirectory.from_json(_suite_json(endpoint_on_earnings=False))
    auth = _auth(seeded, suite=suite_dir)
    app = connections.create_app(seeded, auth=auth)

    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://earnings.example.com",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        for view in ("upcoming", "companies", "digests", "preview", "calendar"):
            resp = await client.get(f"/connections/?view={view}")
            assert resp.status_code == 200
            html_body = resp.text
            assert 'class="suite-bar"' in html_body
            assert 'class="suite-switch"' in html_body
            assert "https://filings.example.com/" in html_body
            assert "https://restricted.example.com/" not in html_body
            assert 'action="/connections/logout"' in html_body
            assert "connection-suite-endpoint" not in html_body
            assert "style=" not in html_body

        # Signed-in import preview also shows suite bar and switcher.
        rev = accounts.profile(seeded.db, "alice")["revision"]
        prev = await client.post(
            "/connections/import-preview",
            data={"revision": str(rev), "view": "companies", "companies": "ALPHA"},
            headers={"Origin": "https://earnings.example.com"},
        )
        assert prev.status_code == 200
        assert 'class="suite-switch"' in prev.text
        assert 'action="/connections/import-apply"' in prev.text

    # When directory marks Earnings with endpoint=True, the collapsed row renders.
    suite_with_ep = SuiteDirectory.from_json(_suite_json(endpoint_on_earnings=True))
    auth_ep = _auth(seeded, suite=suite_with_ep, sid="b" * 43)
    app_ep = connections.create_app(seeded, auth=auth_ep)
    async with (
        app_ep.earnings_app.router.lifespan_context(app_ep.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_ep),
            base_url="https://earnings.example.com",
            cookies={auth_ep.cookie: "b" * 43},
        ) as client,
    ):
        resp = await client.get("/connections/?view=upcoming")
        assert resp.status_code == 200
        assert 'id="connection-suite-endpoint"' in resp.text
        assert "https://mcp.example.com/mcp" in resp.text


@pytest.mark.asyncio
async def test_suite_switcher_absent_when_unset_or_single_app(seeded, monkeypatch):
    monkeypatch.delenv("SIGNIN_WINDOW_SUITE", raising=False)
    seeded.public_url = "https://earnings.example.com"
    seeded.allowed_hosts = ["earnings.example.com", "localhost"]
    auth = _auth(seeded, suite=None)
    app = connections.create_app(seeded, auth=auth)

    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://earnings.example.com",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        resp = await client.get("/connections/")
        assert resp.status_code == 200
        assert 'class="suite-bar"' in resp.text
        assert 'class="suite-switch"' not in resp.text
        assert "connection-suite-endpoint" not in resp.text

    # Visitor in only earnings-users sees no other apps, so no switcher.
    suite_dir = SuiteDirectory.from_json(_suite_json())
    auth_only = _auth(seeded, groups=["earnings-users"], suite=suite_dir, sid="c" * 43)
    app_only = connections.create_app(seeded, auth=auth_only)
    async with (
        app_only.earnings_app.router.lifespan_context(app_only.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_only),
            base_url="https://earnings.example.com",
            cookies={auth_only.cookie: "c" * 43},
        ) as client,
    ):
        resp = await client.get("/connections/")
        assert resp.status_code == 200
        assert 'class="suite-switch"' not in resp.text


@pytest.mark.asyncio
async def test_no_suite_switcher_on_signed_out_refused_or_error_pages(seeded, monkeypatch):
    seeded.public_url = "https://earnings.example.com"
    seeded.allowed_hosts = ["earnings.example.com", "localhost"]
    suite_dir = SuiteDirectory.from_json(_suite_json(endpoint_on_earnings=True))
    auth = _auth(seeded, suite=suite_dir)
    app = connections.create_app(seeded, auth=auth)

    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="https://earnings.example.com",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        # 409 validation conflict
        conflict = await client.post(
            "/connections/watch",
            data={"revision": "-1", "view": "companies", "ticker": "ALPHA"},
            headers={"Origin": "https://earnings.example.com"},
        )
        assert conflict.status_code == 409
        assert 'class="suite-switch"' not in conflict.text
        assert "connection-suite-endpoint" not in conflict.text

        # Signed-out page via logout
        logout = await client.post(
            "/connections/logout",
            headers={"Origin": "https://earnings.example.com"},
        )
        assert logout.status_code == 200
        assert 'class="suite-switch"' not in logout.text
        assert "connection-suite-endpoint" not in logout.text

    # 403 refused page
    auth_403 = _auth(seeded, suite=suite_dir, sid="d" * 43)

    def _deny(*args, **kwargs):
        raise EarningsError("unauthorized", "Access denied")

    monkeypatch.setattr(connections, "open_service", _deny)
    app_403 = connections.create_app(seeded, auth=auth_403)
    async with (
        app_403.earnings_app.router.lifespan_context(app_403.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app_403),
            base_url="https://earnings.example.com",
            cookies={auth_403.cookie: "d" * 43},
        ) as client,
    ):
        denied = await client.get("/connections/")
        assert denied.status_code == 403
        assert 'class="suite-switch"' not in denied.text
        assert "connection-suite-endpoint" not in denied.text


def test_escaping_and_calm_coverage_warnings():
    warnings = [
        {"ticker": f"CO{i}", "message": f"CO{i}: <script>alert({i})</script> needs check"}
        for i in range(9)
    ]
    block = render_coverage_warnings(warnings)
    assert "callout warn coverage-warnings" in block
    assert '<details class="coverage-details">' in block
    assert "Coverage notices (9)" in block
    assert "<script>" not in block
    assert "&lt;script&gt;alert(0)&lt;/script&gt;" in block

    page_html = hub_page(
        "Upcoming",
        "<p>Body</p>",
        account_label='<img src=x onerror="alert(1)">',
        coverage_warnings=warnings[:1],
    )
    assert "<img" not in page_html
    assert "&lt;img src=x onerror=&quot;alert(1)&quot;&gt;" in page_html
    assert "Coverage notice</strong>" in page_html


def test_legacy_0_4_17_session_row_survives_0_14_0_upgrade(tmp_path):
    auth = BrowserAuth(
        OIDCConfig(
            issuer="https://identity.example.com",
            client_id="earnings-connections",
            public_url="https://earnings.example.com",
            state_dir=tmp_path / "browser",
            required_group="earnings-users",
            allow_loopback_http=True,
        )
    )
    sid = "z" * 43
    # Write a legacy 0.4.17 session payload (no `label` or `_family` keys).
    legacy_payload = {
        "issuer": "https://identity.example.com",
        "sub": "alice",
        "name": "Alice Example",
        "groups": ["earnings-users"],
        "claims": {"person_id": "alice"},
        "expires": time.time() + 600,
    }
    auth.store.save(sid, legacy_payload)
    loaded = auth.store.load(sid)
    assert loaded is not None
    assert loaded["sub"] == "alice"
    assert loaded["name"] == "Alice Example"
