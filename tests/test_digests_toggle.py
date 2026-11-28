"""Tests for digests_tab=false toggle, inline timezone form, and notify pref_overrides."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import httpx
from signin_window.oidc import BrowserAuth, OIDCConfig

from conftest import event, source
from earnings_watch import notify
from earnings_watch.connections import create_app
from earnings_watch.settings import Settings
from earnings_watch.store import accounts
from earnings_watch.store.database import Catalogue, PersonalStore, initialize


class _FakeDirectory:
    async def resolve(self, subject: str, **_kwargs) -> dict:
        return {
            "id": subject,
            "admitted": True,
            "disabled": False,
            "email": f"{subject}@example.com",
            "displayName": subject.title(),
            "portfolio_admitted": True,
        }


def _make_auth(settings: Settings) -> BrowserAuth:
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


async def test_digests_tab_false_hides_nav_redirects_and_supports_inline_timezone(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path,
        public_url="http://localhost",
        allowed_hosts=["localhost"],
        digests_tab=False,
        digests_external_url="https://briefs.example.com",
    )
    initialize(settings.db)
    accounts.provision(settings.db, "alice", "alice@example.com")
    auth = _make_auth(settings)
    app = create_app(settings, auth=auth, directory=_FakeDirectory())

    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        home = await client.get("/")
        assert home.status_code == 200, home.text
        assert "Digests &amp; alerts" not in home.text
        assert 'href="/?view=digests"' not in home.text
        assert 'name="section" value="timezone"' in home.text
        assert "https://briefs.example.com" in home.text

        r_dig = await client.get("/?view=digests", follow_redirects=False)
        assert r_dig.status_code == 303
        assert r_dig.headers["location"] == "/?view=upcoming"
        r_prev = await client.get("/?view=preview", follow_redirects=False)
        assert r_prev.status_code == 303

        prof_before = accounts.profile(settings.db, "alice")
        post = await client.post(
            "/connections/preferences",
            headers={"Origin": "http://localhost", "Accept": "application/json"},
            data={
                "revision": str(prof_before["revision"]),
                "view": "upcoming",
                "section": "timezone",
                "timezone": "Asia/Hong_Kong",
            },
        )
        assert post.status_code == 200, post.text
        prof_after = accounts.profile(settings.db, "alice")
        assert prof_after["timezone"] == "Asia/Hong_Kong"
        assert prof_after["settings"] == prof_before["settings"]


async def test_digests_tab_true_default_preserves_standalone_ui(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path,
        public_url="http://localhost",
        allowed_hosts=["localhost"],
        digests_tab=True,
    )
    initialize(settings.db)
    accounts.provision(settings.db, "alice", "alice@example.com")
    auth = _make_auth(settings)
    app = create_app(settings, auth=auth, directory=_FakeDirectory())

    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        home = await client.get("/")
        assert home.status_code == 200
        assert "Digests &amp; alerts" in home.text
        assert 'href="/?view=digests"' in home.text
        dig = await client.get("/?view=digests")
        assert dig.status_code == 200
        assert "Earnings alerts" in dig.text


def test_notify_prepare_honors_pref_overrides(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path)
    initialize(settings.db)
    accounts.provision(settings.db, "alice", "alice@example.com")
    cat = Catalogue(settings.db)
    cat.put_source(source(key="alpha", ticker="ALPHA", exchange="XNAS"))
    ev = event(key="alpha", ticker="ALPHA")
    cat.commit_issuer("alpha", [ev], run_id="r1")
    PersonalStore(settings.db, "alice").watch("ALPHA", "XNAS")

    at = datetime.combine(ev.date, datetime.min.time(), tzinfo=timezone.utc).replace(hour=12)
    none_notices = notify.notices(
        settings,
        "alice",
        at=at,
        pref_override={"brief": False, "weekly_brief": False, "results": False, "urgent": False},
    )
    assert none_notices == []

    some_notices = notify.notices(
        settings,
        "alice",
        at=at,
        pref_override={"brief": True, "weekly_brief": False, "hour": 7, "timezone": "UTC"},
    )
    assert any(kind == "brief" for _k, kind, _p in some_notices)
