"""Tests for Slice 2: preferences, standalone email briefs, /internal/targets, and 4-tab SSR UI."""

from __future__ import annotations

import base64
from datetime import date, datetime, timedelta, timezone
import hashlib
import os
import re

import httpx
import pytest
from signin_window.oidc import BrowserAuth, OIDCConfig

from conftest import event, source
from earnings_watch import open as open_person
from earnings_watch.cli import execute, parser
from earnings_watch.connections import create_app
from earnings_watch.errors import EarningsError
from earnings_watch.notify import SmtpNotifier, notices, prepare, read_private_secret_file
from earnings_watch.outbox import deliver
from earnings_watch.presentation import format_utc_slot
from earnings_watch.render import render_email
from earnings_watch.settings import load
from earnings_watch.store import accounts
from earnings_watch.store.database import Catalogue, PersonalStore, connect, sync_holdings
from earnings_watch.transport import IdentityDirectory


class _Directory:
    def __init__(self, users: dict[str, dict] | None = None, *, down: bool = False):
        self.users = users or {}
        self.down = down

    async def resolve(self, subject: str, **kwargs):
        if self.down:
            raise OSError("directory unreachable")
        default = {
            "id": subject,
            "admitted": True,
            "disabled": False,
            "email": f"{subject}@example.com",
            "portfolio_admitted": False,
        }
        return default | self.users.get(subject, {})


def _browser(settings, subject: str = "alice") -> BrowserAuth:
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
            sub=subject,
            groups=[],
            claims={},
            expires=datetime.now(timezone.utc).timestamp() + 300,
        ),
    )
    return auth


def test_settings_toml_and_env_for_integrations_and_targets(tmp_path, monkeypatch):
    cfg = tmp_path / "earnings-watch.toml"
    cfg.write_text(
        "[serve]\n"
        'targets_tokens_env = ["TARGETS_TOKEN_A"]\n'
        "[integrations.news_digest]\n"
        "enabled = true\n"
    )
    s = load(cfg)
    assert s.targets_tokens_env == ["TARGETS_TOKEN_A"]
    assert s.integrations.news_digest.enabled is True

    default_s = load(tmp_path / "missing.toml")
    assert default_s.integrations.news_digest.enabled is False
    assert default_s.targets_tokens_env == []

    monkeypatch.setenv("EARNINGS_WATCH_TARGETS_TOKENS_ENV", "TOK1,TOK2")
    monkeypatch.setenv("EARNINGS_WATCH_INTEGRATIONS_NEWS_DIGEST_ENABLED", "true")
    env_s = load(tmp_path / "missing.toml")
    assert env_s.targets_tokens_env == ["TOK1", "TOK2"]
    assert env_s.integrations.news_digest.enabled is True


def test_preferences_zero_migration_validation_and_news_mute(seeded):
    with connect(seeded.db) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3

    accounts.provision(seeded.db, "alice")
    prof = accounts.profile(seeded.db, "alice")
    assert prof["settings"] == {
        "results": True,
        "brief": False,
        "weekly_brief": False,
        "urgent": False,
        "hour": 8,
        "news_digest": False,
        "news_cadence": "twice_daily",
        "news_weekdays_only": False,
        "news_pause_until": "",
        "news_scope": "both",
        "news_mutes": [],
    }

    updated = accounts.preferences(
        seeded.db,
        "alice",
        prof["revision"],
        prof["settings"]
        | {
            "weekly_brief": True,
            "news_digest": True,
            "news_cadence": "slot_a",
            "news_weekdays_only": True,
            "news_pause_until": "2026-12-15",
            "news_scope": "holdings",
            "news_mutes": ["ALPHA:XNAS"],
        },
        "Asia/Hong_Kong",
    )
    assert updated["settings"]["weekly_brief"] is True
    assert updated["settings"]["news_cadence"] == "slot_a"
    assert updated["settings"]["news_pause_until"] == "2026-12-15"
    assert updated["settings"]["news_mutes"] == ["ALPHA:XNAS"]

    for bad_patch in [
        {"news_cadence": "hourly"},
        {"news_scope": "everything"},
        {"news_pause_until": "2026-02-30"},
        {"news_pause_until": "tomorrow"},
        {"news_mutes": ["NO_COLON"]},
        {"news_mutes": ["ALPHA:BADEXCHANGE"]},
        {"news_mutes": [f"T{i}:XNAS" for i in range(201)]},
        {"weekly_brief": "yes"},
    ]:
        with pytest.raises(EarningsError):
            accounts.preferences(
                seeded.db,
                "alice",
                updated["revision"],
                updated["settings"] | bad_patch,
                "UTC",
            )

    after_mute = accounts.news_mute(
        seeded.db, "alice", "BETA", "XNAS", True, revision=updated["revision"]
    )
    assert after_mute["settings"]["news_mutes"] == ["ALPHA:XNAS", "BETA:XNAS"]
    with pytest.raises(EarningsError, match="changed"):
        accounts.news_mute(seeded.db, "alice", "BETA", "XNAS", False, revision=updated["revision"])
    after_unmute = accounts.news_mute(
        seeded.db, "alice", "BETA", "XNAS", False, revision=after_mute["revision"]
    )
    assert after_unmute["settings"]["news_mutes"] == ["ALPHA:XNAS"]


async def test_directory_resolve_preserves_portfolio_admitted():
    payloads = {
        "alice": {
            "id": "alice",
            "email": "alice@example.com",
            "admitted": True,
            "disabled": False,
            "portfolio_admitted": True,
        },
        "bob": {
            "id": "bob",
            "email": "bob@example.com",
            "admitted": True,
            "disabled": False,
        },
        "carol": {
            "id": "carol",
            "email": "carol@example.com",
            "admitted": True,
            "disabled": False,
            "portfolio_admitted": "true",
        },
    }

    def handler(request):
        sub = request.url.path.rsplit("/", 1)[-1]
        if sub not in payloads:
            return httpx.Response(404)
        return httpx.Response(200, json=payloads[sub])

    directory = IdentityDirectory(
        "https://id.example.com/directory", "k" * 40, transport=httpx.MockTransport(handler)
    )
    assert (await directory.resolve("alice"))["portfolio_admitted"] is True
    assert (await directory.resolve("bob"))["portfolio_admitted"] is False
    assert (await directory.resolve("carol"))["portfolio_admitted"] is False
    assert (await directory.resolve("missing"))["portfolio_admitted"] is False


def test_render_email_all_kinds_and_escaping():
    ev = event().model_dump(mode="json") | {"source_url": "https://issuer.example/events"}
    prev = ev | {"date": "2026-11-01"}
    docs = [
        {
            "id": "doc1",
            "title": "Q4 Release <PDF>",
            "role": "release",
            "url": "https://issuer.example/q4.pdf",
        },
        {
            "id": "doc2",
            "title": "Bad Link",
            "role": "other",
            "url": "javascript:alert(1)",
        },
    ]

    res = render_email("results", {"event": ev, "documents": docs}, timezone_name="Asia/Hong_Kong")
    assert "ALPHA" in res.subject
    assert "https://issuer.example/q4.pdf" in res.text
    assert 'href="https://issuer.example/q4.pdf"' in res.html
    assert "javascript:alert" not in res.html
    assert "Q4 Release &lt;PDF&gt;" in res.html
    assert "prefers-color-scheme:dark" in res.html

    urg = render_email(
        "urgent", {"event": ev, "previous": prev, "change": "redated"}, timezone_name="UTC"
    )
    assert "Urgent timing change" in urg.subject
    assert "REDATED" in urg.html

    brief = render_email(
        "brief",
        {"day": "2026-11-26", "events": [ev], "changes": [], "documents": docs},
        timezone_name="UTC",
    )
    assert "Daily earnings brief" in brief.subject
    assert "https://issuer.example/events" in brief.html

    weekly = render_email(
        "weekly_brief",
        {"day": "2026-11-26", "week": "2026-W48", "events": [ev], "changes": [], "documents": docs},
        timezone_name="UTC",
    )
    assert "Weekly earnings brief" in weekly.subject
    assert "2026-W48" in weekly.subject


async def test_weekly_brief_and_smtp_notifier_and_cli_all(seeded, tmp_path):
    open_person(seeded, "alice").watch("ALPHA")
    prof = accounts.profile(seeded.db, "alice")
    accounts.preferences(
        seeded.db,
        "alice",
        prof["revision"],
        prof["settings"] | {"results": False, "weekly_brief": True, "hour": 0},
        "UTC",
    )
    accounts.remember_email(seeded.db, "alice", "alice@example.com")

    pending = notices(seeded, "alice")
    assert len(pending) == 1
    assert pending[0][1] == "weekly_brief"
    assert pending[0][0].startswith("weekly_brief:")

    # Private password file permissions & symlink checks.
    bad_pw = tmp_path / "world.pw"
    bad_pw.write_text("secret-pass")
    os.chmod(bad_pw, 0o644)
    with pytest.raises(EarningsError, match="0600"):
        read_private_secret_file(bad_pw)

    good_pw = tmp_path / "smtp.pw"
    good_pw.write_text("smtp-test-fixture\n")
    os.chmod(good_pw, 0o600)
    assert read_private_secret_file(good_pw) == "smtp-test-fixture"

    link_pw = tmp_path / "link.pw"
    link_pw.symlink_to(good_pw)
    with pytest.raises(EarningsError, match="safely"):
        read_private_secret_file(link_pw)

    sent_messages: list[tuple[str, list[str], object]] = []

    class FakeSMTP:
        def __init__(self, host, port, *, timeout=20, context=None):
            self.host, self.port = host, port
            self.actions: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            self.actions.append("ehlo")

        def starttls(self, *, context=None):
            assert context is not None
            self.actions.append("starttls")

        def login(self, user, cred):
            assert user == "mailer" and cred == good_pw.read_text().strip()
            self.actions.append("login")

        def send_message(self, msg):
            sent_messages.append((self.host, list(self.actions), msg))

    notifier = SmtpNotifier(
        seeded.db,
        host="smtp.example.com",
        port=587,
        sender="alerts@example.com",
        username="mailer",
        password_file=good_pw,
        tls="starttls",
        smtp_factory=FakeSMTP,
    )
    prepare(seeded)
    result = await deliver(seeded.db, "alice", notifier)
    assert len(result["sent"]) == 1
    assert len(sent_messages) == 1
    assert sent_messages[0][1] == ["ehlo", "starttls", "ehlo", "login"]
    msg = sent_messages[0][2]
    assert msg["To"] == "alice@example.com"
    assert "Weekly earnings brief" in msg["Subject"]

    # Second run in the same ISO week must not resend.
    prepare(seeded)
    assert (await deliver(seeded.db, "alice", notifier)) == {"sent": [], "unknown": []}
    with connect(seeded.db) as db:
        assert db.execute(
            "SELECT value FROM metadata WHERE key='weekly-brief-cutoff:alice'"
        ).fetchone()


def test_cli_notify_all_people(seeded):
    res_all = execute(parser().parse_args(["notify", "--channel", "stdout", "--all"]), seeded)
    assert "people" in res_all and "alice" in res_all["people"] and "bob" in res_all["people"]


async def test_internal_targets_auth_host_revocation_queued_and_holdings_stripping(
    seeded, monkeypatch
):
    token = "t" * 40
    monkeypatch.setenv("TEST_TARGETS_TOKEN", token)
    seeded.targets_tokens_env = ["TEST_TARGETS_TOKEN"]

    Catalogue(seeded.db).put_source(source(key="beta", ticker="BETA"))
    sync_holdings(seeded.db, "alice", "portfolio", [{"ticker": "ALPHA", "exchange": "XNAS"}])
    PersonalStore(seeded.db, "alice").watch("QUEUEDCO", "XNAS", name="Queued Synthetic")
    accounts.mute(seeded.db, "alice", "ALPHA", "XNAS", True)
    prof_alice = accounts.profile(seeded.db, "alice")
    accounts.preferences(
        seeded.db,
        "alice",
        prof_alice["revision"],
        prof_alice["settings"]
        | {
            "news_digest": True,
            "news_cadence": "slot_b",
            "news_scope": "both",
            "news_mutes": ["QUEUEDCO:XNAS"],
        },
        "Asia/Hong_Kong",
    )

    # Bob has holdings in SQLite, but directory says portfolio_admitted=False.
    sync_holdings(seeded.db, "bob", "portfolio", [{"ticker": "ALPHA", "exchange": "XNAS"}])
    PersonalStore(seeded.db, "bob").watch("BETA", "XNAS")
    prof_bob = accounts.profile(seeded.db, "bob")
    accounts.preferences(
        seeded.db,
        "bob",
        prof_bob["revision"],
        prof_bob["settings"] | {"news_digest": True, "news_scope": "holdings"},
        "UTC",
    )

    # Carol is disabled/unadmitted in directory and must be skipped.
    accounts.provision(seeded.db, "carol")
    PersonalStore(seeded.db, "carol").watch("BETA", "XNAS")

    directory = _Directory(
        {
            "alice": {"admitted": True, "disabled": False, "portfolio_admitted": True},
            "bob": {"admitted": True, "disabled": False, "portfolio_admitted": False},
            "carol": {"admitted": False, "disabled": True, "portfolio_admitted": True},
        }
    )
    app = create_app(seeded, directory=directory)
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        # Missing / bad bearer -> 401
        assert (await client.get("/internal/targets")).status_code == 401
        assert (
            await client.get("/internal/targets", headers={"Authorization": "Bearer short"})
        ).status_code == 401
        assert (
            await client.get("/internal/targets", headers={"Authorization": "Bearer " + "x" * 40})
        ).status_code == 401

        # Disallowed host -> refused by TrustedHostMiddleware
        bad_host = await client.get(
            "/internal/targets",
            headers={"Authorization": "Bearer " + token, "Host": "evil.example"},
        )
        assert bad_host.status_code in {400, 403}

        # Valid request
        resp = await client.get("/internal/targets", headers={"Authorization": "Bearer " + token})
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        body = resp.json()
        assert body["schema"] == 1
        assert body["generated_at"]
        by_person = {p["person"]: p for p in body["people"]}
        assert "carol" not in by_person

        alice = by_person["alice"]
        assert alice["portfolio_admitted"] is True
        assert alice["news_digest"]["scope"] == "both"
        alice_cos = {c["ticker"]: c for c in alice["companies"]}
        assert alice_cos["ALPHA"]["origin"] == "holding"
        assert alice_cos["ALPHA"]["calendar"] is False
        assert alice_cos["ALPHA"]["news"] is True
        assert alice_cos["QUEUEDCO"]["origin"] == "watchlist"
        assert alice_cos["QUEUEDCO"]["state"] == "queued"
        assert alice_cos["QUEUEDCO"]["name"] == "Queued Synthetic"
        assert "QUEUEDCO" in alice_cos["QUEUEDCO"]["queries"]
        assert alice_cos["QUEUEDCO"]["news"] is False

        bob = by_person["bob"]
        assert bob["portfolio_admitted"] is False
        assert bob["news_digest"]["scope"] == "watchlist"
        assert all(c["origin"] != "holding" for c in bob["companies"])
        assert [c["ticker"] for c in bob["companies"]] == ["BETA"]

        # Unconfigured token list -> 401
        seeded.targets_tokens_env = []
        assert (
            await client.get("/internal/targets", headers={"Authorization": "Bearer " + token})
        ).status_code == 401


async def test_ui_csp_zero_app_scripts_news_hiding_and_zero_holdings_leak(seeded):
    Catalogue(seeded.db).put_source(source(key="beta", ticker="BETA"))
    today = datetime.now(timezone.utc).date()
    Catalogue(seeded.db).commit_issuer(
        "beta",
        [event("beta", "BETA", date=today + timedelta(days=5))],
        run_id="b1",
    )
    # Alice has a holding (ALPHA) and a watchlist company (BETA).
    sync_holdings(seeded.db, "alice", "portfolio", [{"ticker": "ALPHA", "exchange": "XNAS"}])
    PersonalStore(seeded.db, "alice").watch("BETA", "XNAS")

    auth = _browser(seeded, "alice")
    # Directory says portfolio_admitted=False, and news_digest integration is disabled (default).
    directory = _Directory(
        {"alice": {"admitted": True, "disabled": False, "portfolio_admitted": False}}
    )
    app = create_app(seeded, auth=auth, directory=directory)
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        for view in ("upcoming", "companies", "digests", "calendar"):
            resp = await client.get(f"/connections/?view={view}")
            assert resp.status_code == 200
            assert "max-width:1080px" in resp.text
            # Zero app-injected <script> tags: every <script> must be in signin-window's CSP hash.
            content_match = re.search(
                r'<div id="connection-content"[^>]*>(.*?)</div></main>', resp.text, re.S
            )
            assert content_match is not None
            assert "<script" not in content_match.group(1).lower()
            csp = resp.headers["content-security-policy"]
            for script in re.findall(r"<script>(.*?)</script>", resp.text, re.S):
                needed = (
                    "sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
                )
                assert needed in csp

        # Non-portfolio-admitted user must NEVER see holdings HTML on any tab.
        upcoming = (await client.get("/connections/?view=upcoming")).text
        assert "BETA" in upcoming
        assert "ALPHA" not in upcoming
        assert "filter=holdings" not in upcoming

        companies = (await client.get("/connections/?view=companies")).text
        companies_without_datalist = re.sub(
            r'<datalist id="companies">.*?</datalist>', "", companies, flags=re.S
        )
        assert "Portfolio Holdings" not in companies_without_datalist
        assert "ALPHA" not in companies_without_datalist
        assert "BETA" in companies_without_datalist
        assert 'popovertarget="import-csv-popover"' not in companies
        assert 'id="import-csv-popover"' not in companies
        assert "news-mute" not in companies

        digests = (await client.get("/connections/?view=digests")).text
        assert "Earnings Alerts" in digests
        assert "Company News Digest" not in digests

        calendar_html = (await client.get("/connections/?view=calendar")).text
        assert 'popovertarget="reset-feed-popover"' in calendar_html
        assert 'id="reset-feed-popover" popover' in calendar_html

    # Now enable integrations.news_digest and portfolio_admitted=True.
    seeded.integrations.news_digest.enabled = True
    directory.users["alice"]["portfolio_admitted"] = True
    prof = accounts.profile(seeded.db, "alice")
    accounts.preferences(
        seeded.db,
        "alice",
        prof["revision"],
        prof["settings"] | {"news_digest": True, "news_pause_until": "2099-01-15"},
        "Asia/Hong_Kong",
    )
    app2 = create_app(seeded, auth=auth, directory=directory)
    async with (
        app2.earnings_app.router.lifespan_context(app2.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app2),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        companies_on = (await client.get("/connections/?view=companies")).text
        assert "Portfolio Holdings" in companies_on
        assert "ALPHA" in companies_on
        assert 'popovertarget="import-csv-popover"' in companies_on
        assert 'id="import-csv-popover" popover' in companies_on
        assert "/connections/news-mute" in companies_on

        digests_on = (await client.get("/connections/?view=digests")).text
        assert "Company News Digest" in digests_on
        assert "Paused until 2099-01-15" in digests_on
        assert format_utc_slot(23, 30, "Asia/Hong_Kong", ref_date=date(2026, 11, 26)) in digests_on
        assert format_utc_slot(11, 30, "Asia/Hong_Kong", ref_date=date(2026, 11, 26)) in digests_on
        assert 'name="news_scope"' in digests_on

        # Toggle news mute via POST
        rev = accounts.profile(seeded.db, "alice")["revision"]
        r_mute = await client.post(
            "/connections/news-mute",
            data={"revision": str(rev), "ticker": "ALPHA", "exchange": "XNAS", "view": "companies"},
            headers={"Origin": "http://localhost"},
            follow_redirects=False,
        )
        assert r_mute.status_code == 303
        assert r_mute.headers["location"] == "/connections/?view=companies&keep_seen=1"
        assert "ALPHA:XNAS" in accounts.profile(seeded.db, "alice")["settings"]["news_mutes"]


async def test_portfolio_not_admitted_blocks_import_ics_dual_listed_and_changed_badge(seeded):
    cat = Catalogue(seeded.db)
    cat.put_source(
        source(key="alpha-xlon", ticker="ALPHA", exchange="XLON").model_copy(
            update={"name": "Alpha London"}
        )
    )
    cat.put_source(
        source(key="beta", ticker="BETA", exchange="XNAS").model_copy(update={"name": "Beta Corp"})
    )
    today = datetime.now(timezone.utc).date()
    # Holding ALPHA:XNAS has an upcoming event (2026Q4) and a recent revision;
    # Watchlist ALPHA:XLON has NO events; Watchlist BETA:XNAS has an upcoming event (2026Q1).
    cat.commit_issuer(
        "beta",
        [
            event(
                "beta",
                "BETA",
                id="earnings:beta:2026Q1",
                period="2026Q1",
                date=today + timedelta(days=5),
            )
        ],
        run_id="b1",
    )
    sync_holdings(seeded.db, "alice", "portfolio", [{"ticker": "ALPHA", "exchange": "XNAS"}])
    PersonalStore(seeded.db, "alice").watch("ALPHA", "XLON")
    PersonalStore(seeded.db, "alice").watch("BETA", "XNAS")

    # Mark everything seen BEFORE committing a new change to the hidden holding ALPHA:XNAS.
    before_seq = cat.status()["sequence"]
    accounts.mark_seen(seeded.db, "alice", before_seq)
    cat.commit_issuer(
        "alpha",
        [
            event(
                "alpha",
                "ALPHA",
                id="earnings:alpha:2026Q4",
                period="2026Q4",
                date=today + timedelta(days=7),
            )
        ],
        run_id="a-chg",
    )

    auth = _browser(seeded, "alice")
    directory = _Directory(
        {"alice": {"admitted": True, "disabled": False, "portfolio_admitted": False}}
    )
    app = create_app(seeded, auth=auth, directory=directory)
    token = accounts.feed_token(seeded.db, "alice", open_person(seeded, "alice").signing_key)
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        rev = accounts.profile(seeded.db, "alice")["revision"]
        r_prev = await client.post(
            "/connections/import-preview",
            data={"revision": str(rev), "companies": "ALPHA", "view": "companies"},
            headers={"Origin": "http://localhost"},
        )
        assert r_prev.status_code == 403
        r_apply = await client.post(
            "/connections/import-apply",
            data={"revision": str(rev), "handle": "fake", "view": "companies"},
            headers={"Origin": "http://localhost"},
        )
        assert r_apply.status_code == 403

        # ICS feed excludes holding ALPHA:XNAS, includes watchlist BETA:XNAS.
        ics_resp = await client.get(f"/calendar/{token}.ics")
        assert ics_resp.status_code == 200
        assert "2026Q1" in ics_resp.text
        assert "2026Q4" not in ics_resp.text

        # Companies tab: dual-listed ALPHA:XLON watchlist row must NOT show ALPHA:XNAS holding date.
        companies = (await client.get("/connections/?view=companies")).text
        assert "Alpha London" in companies
        assert "2026Q4" not in companies
        assert "2026Q1" in companies

        # Upcoming tab: hidden holding change must NOT trigger "Mark changes seen".
        upcoming = (await client.get("/connections/?view=upcoming&keep_seen=1")).text
        assert "Mark changes seen" not in upcoming
        assert "2026Q4" not in upcoming
        assert "BETA" in upcoming


def test_render_email_safe_url_https_only_and_subject_crlf_sanitization():
    ev = event().model_dump(mode="json") | {
        "ticker": "ALPHA\r\nBcc: evil@example.com",
        "period": "Q4\n2026",
        "source_url": "http://issuer.example/insecure",
    }
    docs = [
        {
            "id": "d-http",
            "title": "HTTP Doc",
            "role": "release",
            "url": "http://issuer.example/q4.pdf",
        },
        {
            "id": "d-crlf",
            "title": "CRLF Doc",
            "role": "release",
            "url": "https://issuer.example/q4.pdf\r\nBcc: evil@example.com",
        },
        {
            "id": "d-creds",
            "title": "Creds Doc",
            "role": "release",
            "url": "https://" + "u" + ":" + "p" + "@issuer.example/q4.pdf",
        },
        {
            "id": "d-ok",
            "title": "Good Doc",
            "role": "release",
            "url": "https://issuer.example/good.pdf",
        },
    ]

    res = render_email("results", {"event": ev, "documents": docs}, timezone_name="UTC")
    assert "\r" not in res.subject and "\n" not in res.subject
    assert "http://issuer.example" not in res.html
    assert "http://issuer.example" not in res.text
    assert "user:pass@issuer.example" not in res.html
    assert "user:pass@issuer.example" not in res.text
    assert "https://issuer.example/q4.pdf" not in res.html
    assert "https://issuer.example/q4.pdf" not in res.text
    assert "https://issuer.example/good.pdf" in res.html
    assert "https://issuer.example/good.pdf" in res.text

    urg = render_email(
        "urgent",
        {"event": ev, "previous": ev, "change": "redated"},
        timezone_name="UTC",
    )
    assert "\r" not in urg.subject and "\n" not in urg.subject

    brief = render_email(
        "brief",
        {"day": "2026-11-26\r\nInjected: header", "events": [ev], "changes": [], "documents": docs},
        timezone_name="UTC",
    )
    assert "\r" not in brief.subject and "\n" not in brief.subject
    assert "http://issuer.example" not in brief.html
    assert "http://issuer.example" not in brief.text


async def test_news_unmute_clears_all_exchange_mute_via_rendered_form(seeded):
    seeded.integrations.news_digest.enabled = True
    PersonalStore(seeded.db, "alice").watch("ALPHA", "XNAS")
    prof = accounts.profile(seeded.db, "alice")
    accounts.news_mute(seeded.db, "alice", "ALPHA", "", True, revision=prof["revision"])
    assert accounts.profile(seeded.db, "alice")["settings"]["news_mutes"] == ["ALPHA:"]

    auth = _browser(seeded, "alice")
    directory = _Directory(
        {"alice": {"admitted": True, "disabled": False, "portfolio_admitted": True}}
    )
    app = create_app(seeded, auth=auth, directory=directory)
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        html_body = (await client.get("/connections/?view=companies")).text
        form_match = re.search(
            r'<form class="hub-inline-form" method="post" action="/connections/news-unmute">(.*?)</form>',
            html_body,
            re.S,
        )
        assert form_match is not None
        form_html = form_match.group(1)
        assert "Unmute all exchanges" in form_html
        fields = dict(re.findall(r'<input type="hidden" name="([^"]+)" value="([^"]*)"', form_html))
        assert fields["ticker"] == "ALPHA"
        assert fields["exchange"] == ""

        resp = await client.post(
            "/connections/news-unmute",
            data=fields,
            headers={"Origin": "http://localhost"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert accounts.profile(seeded.db, "alice")["settings"]["news_mutes"] == []


def test_cli_notify_smtp_fail_closed_directory_and_all_to_fallback(seeded):
    seeded.smtp_host = "smtp.example.com"
    seeded.smtp_from = "alerts@example.com"
    seeded.directory_required = True
    seeded.directory_url = ""
    with pytest.raises(EarningsError, match="directory"):
        execute(parser().parse_args(["notify", "--channel", "smtp"]), seeded)

    seeded.directory_required = False
    with pytest.raises(EarningsError, match="--all"):
        execute(
            parser().parse_args(["notify", "--channel", "smtp", "--all", "--to", "a@example.com"]),
            seeded,
        )

    seeded.smtp_to = "fallback@example.com"
    with pytest.raises(EarningsError, match="--all"):
        execute(parser().parse_args(["notify", "--channel", "smtp", "--all"]), seeded)


async def test_weekly_brief_retains_changes_after_daily_brief_advances_notify_seq(seeded):
    open_person(seeded, "alice").watch("ALPHA")
    prof = accounts.profile(seeded.db, "alice")
    accounts.preferences(
        seeded.db,
        "alice",
        prof["revision"],
        prof["settings"] | {"results": False, "brief": True, "weekly_brief": True, "hour": 8},
        "UTC",
    )

    # Day 1 (Monday): establish baseline and send initial weekly_brief so weekly-brief-cutoff is Day 1.
    day1 = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)
    day2 = datetime(2026, 11, 24, 9, 0, tzinfo=timezone.utc)
    week2 = datetime(2026, 11, 30, 9, 0, tzinfo=timezone.utc)
    with connect(seeded.db, write=True) as db:
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES(?,?)",
            ("baseline:alice", (day1 - timedelta(days=1)).isoformat()),
        )
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES(?,?)",
            ("weekly-brief-cutoff:alice", day1.isoformat()),
        )
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES(?,?)",
            ("brief-cutoff:alice", day1.isoformat()),
        )

    # Day 2: commit a revision timestamped on Day 2 (event date far enough out that only changes trigger brief).
    Catalogue(seeded.db).commit_issuer(
        "alpha",
        [event("alpha", "ALPHA", date=date(2026, 12, 20))],
        run_id="day2-chg",
    )
    with connect(seeded.db, write=True) as db:
        db.execute(
            "UPDATE event_revisions SET at=? WHERE seq=(SELECT max(seq) FROM event_revisions)",
            ((day2 - timedelta(hours=1)).isoformat(),),
        )

    # Deliver daily brief on Day 2 -> advances people.notify_seq past the Day 2 revision.
    class _MemNotifier:
        async def send(self, person, key, kind, payload):
            return f"msg:{key}"

    day2_notices = [n for n in notices(seeded, "alice", at=day2) if n[1] == "brief"]
    assert len(day2_notices) == 1
    assert len(day2_notices[0][2]["changes"]) >= 1
    from earnings_watch.outbox import enqueue

    enqueue(seeded.db, "alice", *day2_notices[0])
    delivered = await deliver(seeded.db, "alice", _MemNotifier())
    assert delivered["sent"] == [day2_notices[0][0]]

    # Week 2: daily brief already consumed notify_seq, but weekly_brief must still include Day 2's change.
    week2_notices = [n for n in notices(seeded, "alice", at=week2) if n[1] == "weekly_brief"]
    assert len(week2_notices) == 1
    weekly_changes = week2_notices[0][2]["changes"]
    assert any(ch["doc"].get("date") == "2026-12-20" for ch in weekly_changes)


async def test_news_endpoints_return_404_when_news_digest_disabled(seeded):
    seeded.integrations.news_digest.enabled = False
    PersonalStore(seeded.db, "alice").watch("ALPHA", "XNAS")
    auth = _browser(seeded, "alice")
    directory = _Directory(
        {"alice": {"admitted": True, "disabled": False, "portfolio_admitted": True}}
    )
    app = create_app(seeded, auth=auth, directory=directory)
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        rev = accounts.profile(seeded.db, "alice")["revision"]
        for path, payload in [
            (
                "/connections/news-mute",
                {"revision": str(rev), "ticker": "ALPHA", "exchange": "XNAS", "view": "companies"},
            ),
            (
                "/connections/news-unmute",
                {"revision": str(rev), "ticker": "ALPHA", "exchange": "XNAS", "view": "companies"},
            ),
            (
                "/connections/preferences",
                {"revision": str(rev), "section": "news", "news_digest": "on", "view": "digests"},
            ),
        ]:
            resp = await client.post(path, data=payload, headers={"Origin": "http://localhost"})
            assert resp.status_code == 404, (path, resp.status_code)


async def test_calendar_outlook_links_timezone_select_and_new_company_onboarding(seeded):
    seeded.integrations.news_digest.enabled = True
    accounts.provision(seeded.db, "alice")
    prof = accounts.profile(seeded.db, "alice")
    accounts.preferences(seeded.db, "alice", prof["revision"], prof["settings"], "Asia/Hong_Kong")
    auth = _browser(seeded, "alice")
    seeded.public_url = "https://earnings.example.com"
    directory = _Directory(
        {"alice": {"admitted": True, "disabled": False, "portfolio_admitted": True}}
    )
    app = create_app(seeded, auth=auth, directory=directory)
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        # 1. Calendar tab: Outlook subscribe deep link uses rru=addsubscription (not broken addcalendar).
        cal_html = (await client.get("/connections/?view=calendar")).text
        assert "/calendar/0/addcalendar" not in cal_html
        assert (
            "https://outlook.live.com/owa/?path=/calendar/action/compose&amp;rru=addsubscription&amp;url="
            in cal_html
        )
        assert "webcal://earnings.example.com/calendar/" in cal_html
        assert 'target="_blank"' in cal_html

        # 2. Digests tab: Timezone is a <select> with full IANA options (not <input list> locked to HK).
        digests_html = (await client.get("/connections/?view=digests")).text
        assert '<select id="pref-tz" name="timezone" required>' in digests_html
        assert 'list="timezones"' not in digests_html
        assert '<option value="Asia/Hong_Kong" selected>Asia/Hong_Kong</option>' in digests_html
        assert '<option value="Europe/London">Europe/London</option>' in digests_html
        assert '<option value="America/New_York">America/New_York</option>' in digests_html
        assert '<option value="Asia/Tokyo">Asia/Tokyo</option>' in digests_html

        # 3. Companies tab: supports requesting new untracked companies with exchange aliases, name, and IR URL.
        companies_html = (await client.get("/connections/?view=companies")).text
        assert 'popovertarget="request-company-popover"' in companies_html
        assert 'id="request-company-popover" popover' in companies_html
        assert 'name="ir_url"' in companies_html

        rev = accounts.profile(seeded.db, "alice")["revision"]
        r_new = await client.post(
            "/connections/watch",
            data={
                "revision": str(rev),
                "view": "companies",
                "ticker": "NASDAQ:NEWCO",
                "exchange": "",
                "name": "NewCo Synthetic",
                "ir_url": "https://ir.newco.example/events",
            },
            headers={"Origin": "http://localhost"},
            follow_redirects=False,
        )
        assert r_new.status_code == 303
        assert r_new.headers["location"] == "/connections/?view=companies&keep_seen=1"
        follows = PersonalStore(seeded.db, "alice").follows()
        queued = [f for f in follows if f["ticker"] == "NEWCO"]
        assert len(queued) == 1
        assert queued[0]["exchange"] == "XNAS"
        assert queued[0]["state"] == "queued"
        assert queued[0]["company_name"] == "NewCo Synthetic"

        companies_after = (await client.get("/connections/?view=companies")).text
        assert "NewCo Synthetic" in companies_after
        assert "Discovering..." in companies_after

        # Untracked multi-word name without ticker/ISIN gives actionable message and returns to companies view.
        rev2 = accounts.profile(seeded.db, "alice")["revision"]
        r_name = await client.post(
            "/connections/watch",
            data={
                "revision": str(rev2),
                "view": "companies",
                "ticker": "Untracked Holdings Corp",
                "exchange": "",
            },
            headers={"Origin": "http://localhost"},
        )
        assert r_name.status_code == 409
        assert "ticker" in r_name.text.lower() and "isin" in r_name.text.lower()
        assert 'href="/connections/?view=companies"' in r_name.text


async def test_notices_enrich_payloads_with_timezone_holdings_and_issuer_name(seeded):
    PersonalStore(seeded.db, "alice").watch("ALPHA", "XNAS")
    sync_holdings(seeded.db, "bob", "portfolio", [{"ticker": "ALPHA", "exchange": "XNAS"}])
    for person in ("alice", "bob"):
        prof = accounts.profile(seeded.db, person)
        accounts.preferences(
            seeded.db,
            person,
            prof["revision"],
            prof["settings"] | {"brief": True, "weekly_brief": True, "hour": 0},
            "Asia/Hong_Kong",
        )
    from zoneinfo import ZoneInfo

    at = datetime.now(timezone.utc) + timedelta(hours=2)
    hkt_today = at.astimezone(ZoneInfo("Asia/Hong_Kong")).date()
    Catalogue(seeded.db).commit_issuer(
        "alpha",
        [event("alpha", "ALPHA", date=hkt_today)],
        run_id="enrich-test",
    )
    alice_brief = [n for n in notices(seeded, "alice", at=at) if n[1] == "brief"][0][2]
    bob_weekly = [n for n in notices(seeded, "bob", at=at) if n[1] == "weekly_brief"][0][2]

    assert alice_brief["timezone"] == "Asia/Hong_Kong"
    assert alice_brief["has_holdings"] is False
    assert alice_brief["events"][0]["why_followed"] == ["watchlist"]
    assert alice_brief["events"][0]["issuer_name"]

    assert bob_weekly["timezone"] == "Asia/Hong_Kong"
    assert bob_weekly["has_holdings"] is True
    assert bob_weekly["events"][0]["why_followed"] == ["holding"]
    assert bob_weekly["events"][0]["issuer_name"]

    # Also verify results and urgent payload enrichment.
    ev_id = bob_weekly["events"][0]["id"]
    Catalogue(seeded.db).put_document(
        {
            "id": "doc-rel-1",
            "event_id": ev_id,
            "url": "https://alpha.example/q2-release.pdf",
            "title": "Q2 Press Release",
            "role": "release",
            "verified": True,
            "sha256": "",
            "first_seen": (at - timedelta(minutes=5)).isoformat(),
        }
    )
    results_payload = [n for n in notices(seeded, "bob", at=at) if n[1] == "results"][0][2]
    assert results_payload["timezone"] == "Asia/Hong_Kong"
    assert results_payload["has_holdings"] is True
    assert results_payload["event"]["why_followed"] == ["holding"]
    assert results_payload["event"]["issuer_name"]

    Catalogue(seeded.db).commit_issuer(
        "alpha",
        [event("alpha", "ALPHA", date=hkt_today + timedelta(days=1))],
        run_id="urgent-enrich",
    )
    with connect(seeded.db, write=True) as db:
        db.execute(
            "UPDATE event_revisions SET at=? WHERE seq=(SELECT max(seq) FROM event_revisions)",
            ((at - timedelta(minutes=20)).isoformat(),),
        )
    prof_b = accounts.profile(seeded.db, "bob")
    accounts.preferences(
        seeded.db,
        "bob",
        prof_b["revision"],
        prof_b["settings"] | {"urgent": True},
        "Asia/Hong_Kong",
    )
    urgent_payload = [n for n in notices(seeded, "bob", at=at) if n[1] == "urgent"][0][2]
    assert urgent_payload["timezone"] == "Asia/Hong_Kong"
    assert urgent_payload["has_holdings"] is True
    assert urgent_payload["event"]["why_followed"] == ["holding"]
    assert urgent_payload["event"]["issuer_name"]


async def test_digests_delivery_history_isolation_and_brief_preview(seeded):
    PersonalStore(seeded.db, "alice").watch("ALPHA", "XNAS")
    # Add a second source held only by alice (simulating residual holdings after revocation).
    cat = Catalogue(seeded.db)
    cat.put_source(source("holdco", "HOLDCO", exchange="XNAS"))
    sync_holdings(seeded.db, "alice", "portfolio", [{"ticker": "HOLDCO", "exchange": "XNAS"}])
    sync_holdings(seeded.db, "bob", "portfolio", [{"ticker": "ALPHA", "exchange": "XNAS"}])

    prof_a = accounts.profile(seeded.db, "alice")
    accounts.preferences(
        seeded.db,
        "alice",
        prof_a["revision"],
        prof_a["settings"] | {"brief": True, "weekly_brief": True, "hour": 0},
        "UTC",
    )
    today = datetime.now(timezone.utc).date()
    cat.commit_issuer(
        "holdco",
        [event("holdco", "HOLDCO", date=today)],
        run_id="holdco-rev",
    )
    holdco_ev = [e for e in cat.events() if e["issuer_key"] == "holdco"][0]
    cat.put_document(
        {
            "id": "holdco-doc-1",
            "event_id": holdco_ev["id"],
            "url": "https://holdco.example/q4-release.pdf",
            "title": "Secret Holding Press Release",
            "role": "release",
            "verified": True,
            "sha256": "",
            "first_seen": datetime.now(timezone.utc).isoformat(),
        }
    )

    from earnings_watch.outbox import enqueue

    # Seed 20 older sent deliveries, 1 active unknown delivery, legacy/migration rows, and bob's row.
    with connect(seeded.db, write=True) as db:
        for idx in range(1, 21):
            db.execute(
                "INSERT INTO deliveries(person,key,kind,state,payload,sent_at) VALUES(?,?,?,'sent','{}',?)",
                ("alice", f"brief:2026-10-{idx:02d}", "brief", f"2026-10-{idx:02d}T08:00:00+00:00"),
            )
        db.execute(
            "INSERT INTO deliveries(person,key,kind,state,payload) VALUES('alice','legacy-state','brief','archived','{}')"
        )
        db.execute(
            "INSERT INTO deliveries(person,key,kind,state,payload) VALUES('alice','brief:migrated','brief','suppressed_by_migration','{}')"
        )
    enqueue(seeded.db, "alice", "brief:2026-11-26", "brief", {"day": "2026-11-26", "events": []})
    with connect(seeded.db, write=True) as db:
        db.execute(
            "UPDATE deliveries SET state='unknown' WHERE person='alice' AND key='brief:2026-11-26'"
        )
    enqueue(seeded.db, "bob", "brief:2026-11-25", "brief", {"day": "2026-11-25", "events": []})

    auth = _browser(seeded, "alice")
    directory = _Directory(
        {"alice": {"admitted": True, "disabled": False, "portfolio_admitted": False}}
    )
    app = create_app(seeded, auth=auth, directory=directory)
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        digests = await client.get("/connections/?view=digests")
        assert digests.status_code == 200
        assert "Preview next brief" in digests.text
        assert "Recent Earnings Deliveries" in digests.text
        # Active unknown delivery must remain visible at top even with 20 sent rows.
        assert "brief:2026-11-26" in digests.text
        assert "Waiting review" in digests.text
        # Legacy, migration-suppressed, and bob's rows must never appear.
        assert "legacy-state" not in digests.text
        assert "brief:migrated" not in digests.text
        assert "brief:2026-11-25" not in digests.text

        seen_before = accounts.profile(seeded.db, "alice")["seen_seq"]
        preview = await client.get("/connections/?view=preview")
        assert preview.status_code == 200
        assert "Back to Digests" in preview.text
        assert "hub-preview-text" in preview.text
        # Holding-only event, document, and revision must NEVER leak when portfolio_admitted=False.
        assert "HOLDCO" not in preview.text
        assert "Secret Holding Press Release" not in preview.text
        assert "holdco.example" not in preview.text
        # Preview must obey CSP (only signin-window's single hashed script).
        assert "content-security-policy" in preview.headers
        assert preview.text.count("<script") == 1
        # Preview is non-mutating.
        assert accounts.profile(seeded.db, "alice")["seen_seq"] == seen_before


@pytest.mark.asyncio
async def test_calendar_tab_omits_broken_webcal_on_windows(seeded):
    """Windows New Outlook strips webcal: to //host/... (Invalid URL); omit webcal:// on Windows."""
    auth = _browser(seeded, "alice")
    seeded.public_url = "https://earnings.example.com"
    directory = _Directory(
        {"alice": {"admitted": True, "disabled": False, "portfolio_admitted": True}}
    )
    app = create_app(seeded, auth=auth, directory=directory)
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        # 1. Windows via Chromium Client Hints: no webcal:// button; Outlook.com + copyable HTTPS link present.
        win_ch = (
            await client.get(
                "/connections/?view=calendar",
                headers={
                    "Sec-CH-UA-Platform": '"Windows"',
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0",
                },
            )
        ).text
        assert "webcal://" not in win_ch
        assert "Subscribe in calendar app" not in win_ch
        assert (
            "https://outlook.live.com/owa/?path=/calendar/action/compose&amp;rru=addsubscription&amp;url="
            in win_ch
        )
        assert '<code id="calendar-link">https://earnings.example.com/calendar/' in win_ch
        assert "Subscribe from web" in win_ch

        # 2. Windows via User-Agent only (e.g. Firefox without Sec-CH-UA-Platform).
        win_ua = (
            await client.get(
                "/connections/?view=calendar",
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:132.0) Gecko/20100101 Firefox/132.0",
                },
            )
        ).text
        assert "webcal://" not in win_ua
        assert "Subscribe in calendar app" not in win_ua

        # 3. macOS / iOS keep the standard webcal:// button for Apple Calendar.
        mac_html = (
            await client.get(
                "/connections/?view=calendar",
                headers={
                    "Sec-CH-UA-Platform": '"macOS"',
                    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15",
                },
            )
        ).text
        assert "webcal://earnings.example.com/calendar/" in mac_html
        assert "Subscribe in calendar app" in mac_html
