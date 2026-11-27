"""Tests for /internal/library-targets and Source hkex_stock_id/lei fields."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from conftest import event, source
from earnings_watch.connections import create_app
from earnings_watch.model.sources import Source
from earnings_watch.settings import load
from earnings_watch.store import accounts
from earnings_watch.store.database import Catalogue, PersonalStore, sync_holdings


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


def test_settings_toml_and_env_for_library_targets_tokens(tmp_path, monkeypatch):
    cfg = tmp_path / "earnings-watch.toml"
    cfg.write_text('[serve]\nlibrary_targets_tokens_env = ["LIB_TOKEN_A"]\n')
    s = load(cfg)
    assert s.library_targets_tokens_env == ["LIB_TOKEN_A"]

    default_s = load(tmp_path / "missing.toml")
    assert default_s.library_targets_tokens_env == []

    monkeypatch.setenv("EARNINGS_WATCH_LIBRARY_TARGETS_TOKENS_ENV", "LIB1,LIB2")
    env_s = load(tmp_path / "missing.toml")
    assert env_s.library_targets_tokens_env == ["LIB1", "LIB2"]


def test_source_hkex_stock_id_and_lei_validation():
    valid = source(key="hkco", ticker="0700", exchange="XHKG").model_copy(
        update={
            "hkex_stock_id": "12345",
            "lei": "5493001KJTIIGC8Y1R12",
            "sec_cik": 320193,
        }
    )
    reloaded = Source.model_validate_json(valid.model_dump_json())
    assert reloaded.hkex_stock_id == "12345"
    assert reloaded.lei == "5493001KJTIIGC8Y1R12"
    assert reloaded.sec_cik == 320193

    default_src = source()
    assert default_src.hkex_stock_id is None
    assert default_src.lei is None

    for bad_hkex in ("", "123456789", "ABC", "12-34"):
        with pytest.raises(ValidationError):
            Source.model_validate(default_src.model_dump() | {"hkex_stock_id": bad_hkex})

    for bad_lei in ("", "5493001KJTIIGC8Y1R1", "5493001KJTIIGC8Y1R123", "5493001kjtiigc8y1r12"):
        with pytest.raises(ValidationError):
            Source.model_validate(default_src.model_dump() | {"lei": bad_lei})


async def test_library_targets_auth_and_trusted_host(seeded, monkeypatch):
    targets_tok = "t" * 40
    lib_tok = "l" * 40
    monkeypatch.setenv("TEST_TARGETS_TOKEN", targets_tok)
    monkeypatch.setenv("TEST_LIBRARY_TARGETS_TOKEN", lib_tok)
    seeded.targets_tokens_env = ["TEST_TARGETS_TOKEN"]
    seeded.library_targets_tokens_env = ["TEST_LIBRARY_TARGETS_TOKEN"]

    app = create_app(seeded, directory=_Directory())
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        # Missing, short, wrong, and targets_tokens_env bearer tokens must all fail with 401.
        for headers in (
            {},
            {"Authorization": "Bearer short"},
            {"Authorization": "Bearer " + "x" * 40},
            {"Authorization": "Bearer " + targets_tok},
        ):
            resp = await client.get("/internal/library-targets", headers=headers)
            assert resp.status_code == 401
            assert resp.headers["cache-control"] == "no-store"

        # Reverse isolation: library token must NOT authenticate /internal/targets.
        rev = await client.get("/internal/targets", headers={"Authorization": "Bearer " + lib_tok})
        assert rev.status_code == 401

        # Disallowed Host header must be rejected by TrustedHostMiddleware.
        bad_host = await client.get(
            "/internal/library-targets",
            headers={"Authorization": "Bearer " + lib_tok, "Host": "evil.example"},
        )
        assert bad_host.status_code in {400, 403}

        # Valid bearer succeeds with no-store.
        ok = await client.get(
            "/internal/library-targets", headers={"Authorization": "Bearer " + lib_tok}
        )
        assert ok.status_code == 200
        assert ok.headers["cache-control"] == "no-store"

        # Unconfigured library_targets_tokens_env fails closed with 401.
        seeded.library_targets_tokens_env = []
        unconfigured = await client.get(
            "/internal/library-targets", headers={"Authorization": "Bearer " + lib_tok}
        )
        assert unconfigured.status_code == 401
        assert unconfigured.headers["cache-control"] == "no-store"


async def test_library_targets_zero_pii_scoping_documents_and_exchange_normalization(
    seeded, monkeypatch
):
    lib_tok = "l" * 40
    monkeypatch.setenv("TEST_LIBRARY_TARGETS_TOKEN", lib_tok)
    seeded.library_targets_tokens_env = ["TEST_LIBRARY_TARGETS_TOKEN"]

    cat = Catalogue(seeded.db)
    # Alpha: US company with sec_cik, lei, duplicate results_url == calendar_url, and announcements_url.
    alpha = source(key="alpha", ticker="ALPHA", exchange="XNAS").model_copy(
        update={
            "isin": "US0000000001",
            "sec_cik": 320193,
            "lei": "5493001KJTIIGC8Y1R12",
            "results_url": "https://issuer.example/calendar",
            "announcements_url": "https://issuer.example/announcements",
        }
    )
    cat.put_source(alpha)
    # Beta: HK company with hkex_stock_id, held only by Bob (portfolio_admitted=False) -> must NOT appear.
    beta = source(key="beta", ticker="0700", exchange="XHKG").model_copy(
        update={"hkex_stock_id": "700", "name": "Beta Holdings"}
    )
    cat.put_source(beta)
    # Gamma: watched by Bob (admitted) -> must appear with hkex_stock_id.
    gamma = source(key="gamma", ticker="0005", exchange="XHKG").model_copy(
        update={
            "hkex_stock_id": "00005",
            "name": "Gamma Group",
            "results_url": "https://issuer.example/gamma/results",
        }
    )
    cat.put_source(gamma)
    # Delta: watched ONLY by Carol (disabled/unadmitted) -> must NOT appear.
    cat.put_source(source(key="delta", ticker="DELTA", exchange="XNYS"))
    # Epsilon: in catalogue, followed by nobody -> must NOT appear.
    cat.put_source(source(key="epsilon", ticker="EPS", exchange="XNAS"))

    # Add one verified and one unverified document for Alpha's seeded event.
    ev = event(key="alpha", ticker="ALPHA")
    cat.commit_issuer("alpha", [ev], run_id="doc-seed")
    cat.put_document(
        {
            "id": "doc-verified",
            "event_id": ev.id,
            "role": "release",
            "url": "https://issuer.example/q4-release.pdf",
            "title": "Q4 2026 Results Release",
            "verified": True,
            "first_seen": "2026-11-26T00:00:00+00:00",
        }
    )
    cat.put_document(
        {
            "id": "doc-unverified",
            "event_id": ev.id,
            "role": "release",
            "url": "https://issuer.example/unverified.pdf",
            "title": "Unverified Candidate",
            "verified": False,
            "first_seen": "2026-11-26T00:00:00+00:00",
        }
    )

    # Alice (portfolio_admitted=True): holds ALPHA with empty exchange in follows (must normalize to XNAS)
    # and watches an unregistered QUEUEDCO (source_key=None -> excluded).
    sync_holdings(seeded.db, "alice", "portfolio", [{"ticker": "ALPHA", "exchange": ""}])
    PersonalStore(seeded.db, "alice").watch("QUEUEDCO", "XNAS", name="Queued Synthetic")
    accounts.mute(seeded.db, "alice", "ALPHA", "XNAS", True)
    accounts.remember_email(seeded.db, "alice", "alice@example.com")
    prof_alice = accounts.profile(seeded.db, "alice")
    accounts.preferences(
        seeded.db,
        "alice",
        prof_alice["revision"],
        prof_alice["settings"]
        | {
            "news_digest": True,
            "news_cadence": "slot_b",
            "news_mutes": ["ALPHA:XNAS"],
        },
        "Asia/Hong_Kong",
    )

    # Bob (portfolio_admitted=False): holds BETA (must be stripped!) and watches GAMMA.
    sync_holdings(seeded.db, "bob", "portfolio", [{"ticker": "0700", "exchange": "XHKG"}])
    PersonalStore(seeded.db, "bob").watch("0005", "XHKG")
    accounts.remember_email(seeded.db, "bob", "bob@example.com")

    # Carol (disabled / unadmitted): watches DELTA (must be omitted and DELTA must not leak).
    accounts.provision(seeded.db, "carol")
    PersonalStore(seeded.db, "carol").watch("DELTA", "XNYS")

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
        resp = await client.get(
            "/internal/library-targets", headers={"Authorization": "Bearer " + lib_tok}
        )
        assert resp.status_code == 200
        assert resp.headers["cache-control"] == "no-store"
        body = resp.json()

    # Zero-PII check across serialized JSON.
    raw_json = json.dumps(body)
    for forbidden in (
        "email",
        "timezone",
        "news_digest",
        "news_mutes",
        "mutes",
        "@example.com",
        "Asia/Hong_Kong",
    ):
        assert forbidden not in raw_json

    assert set(body) == {"schema", "generated_at", "companies", "scopes"}
    assert body["schema"] == 1
    assert body["generated_at"]

    # Scopes: Carol omitted, Bob's holding stripped, Alice has alpha (queued omitted).
    scopes = {s["person"]: s["source_keys"] for s in body["scopes"]}
    assert "carol" not in scopes
    assert scopes["alice"] == ["alpha"]
    assert scopes["bob"] == ["gamma"]

    # Companies: ONLY alpha and gamma; beta (non-admitted holding), delta (disabled user), epsilon (unfollowed) absent.
    companies = {c["source_key"]: c for c in body["companies"]}
    assert set(companies) == {"alpha", "gamma"}

    alpha_out = companies["alpha"]
    assert alpha_out == {
        "source_key": "alpha",
        "ticker": "ALPHA",
        "exchange": "XNAS",
        "name": "Example Components",
        "isin": "US0000000001",
        "sec_cik": 320193,
        "hkex_stock_id": None,
        "lei": "5493001KJTIIGC8Y1R12",
        "ir_urls": [
            "https://issuer.example/calendar",
            "https://issuer.example/announcements",
        ],
        "verified_documents": [
            {
                "id": "doc-verified",
                "event_id": ev.id,
                "url": "https://issuer.example/q4-release.pdf",
                "title": "Q4 2026 Results Release",
                "role": "release",
                "first_seen": "2026-11-26T00:00:00+00:00",
            }
        ],
    }

    gamma_out = companies["gamma"]
    assert gamma_out["hkex_stock_id"] == "00005"
    assert gamma_out["lei"] is None
    assert gamma_out["exchange"] == "XHKG"
    assert gamma_out["ir_urls"] == [
        "https://issuer.example/calendar",
        "https://issuer.example/gamma/results",
    ]
    assert gamma_out["verified_documents"] == []

    # Opt-in schema=2 emits typed ir_urls, fy_end_month, period_scheme, and normalized categories.
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        resp_v2 = await client.get(
            "/internal/library-targets?schema=2", headers={"Authorization": "Bearer " + lib_tok}
        )
        assert resp_v2.status_code == 200
        v2_body = resp_v2.json()
    assert v2_body["schema"] == 2
    v2_alpha = next(c for c in v2_body["companies"] if c["source_key"] == "alpha")
    assert v2_alpha["ir_urls"]["calendar"] == "https://issuer.example/calendar"
    assert v2_alpha["ir_urls"]["announcements"] == "https://issuer.example/announcements"
    assert v2_alpha["verified_documents"][0]["category"] == "results_release"


async def test_library_targets_directory_outage_fails_closed(seeded, monkeypatch):
    lib_tok = "l" * 40
    monkeypatch.setenv("TEST_LIBRARY_TARGETS_TOKEN", lib_tok)
    seeded.library_targets_tokens_env = ["TEST_LIBRARY_TARGETS_TOKEN"]

    app = create_app(seeded, directory=_Directory(down=True))
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        resp = await client.get(
            "/internal/library-targets", headers={"Authorization": "Bearer " + lib_tok}
        )
        assert resp.status_code == 503
        assert resp.headers["cache-control"] == "no-store"
        assert resp.json() == {"error": "identity directory unavailable"}
