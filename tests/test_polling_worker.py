from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
import pytest
from conftest import event, source
from earnings_watch.polling import MAX_POLL_ATTEMPTS, due, poll
from earnings_watch.store.database import Catalogue
from earnings_watch.worker import sweep_due
from earnings_watch.transport import Transport
import httpx
import socket


def test_poll_cadence_and_attempt_cap():
    now = datetime.now(timezone.utc)
    e = event(date=now.date())
    assert due(e, {}, now)
    assert not due(e, {"attempts": MAX_POLL_ATTEMPTS}, now)
    assert not due(e, {"last_attempt": now.isoformat()}, now)
    assert due(e, {"last_attempt": (now - timedelta(minutes=10)).isoformat()}, now)
    assert not due(event(date=now.date() - timedelta(days=8)), {}, now)


def test_worker_restart_and_failed_sweep_retries():
    now = datetime(2026, 10, 29, 23, 20, tzinfo=timezone.utc)
    assert sweep_due({}, now)
    status = {"last_sweep": {"started": now.isoformat(), "exit": 0}}
    assert not sweep_due(status, now)
    status["last_sweep"]["exit"] = 3
    assert sweep_due(status, now)
    assert sweep_due(status, now + timedelta(hours=1))


async def test_none_backend_still_runs_official_release_provider(seeded, monkeypatch):
    from earnings_watch import open
    from earnings_watch.maintenance import Maintenance
    from earnings_watch.release import Signal

    src = source().model_copy(update={"sec_cik": 123})
    Catalogue(seeded.db).put_source(src)
    open(seeded).watch("ALPHA")
    provider = AsyncMock(return_value=(Signal(event().id, "official release"), []))
    monkeypatch.setattr("earnings_watch.release._sec", provider)
    transport = Transport(
        {"issuer.example"},
        pace=0,
        resolver=lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
        mock=httpx.MockTransport(
            lambda r: httpx.Response(200, text="calendar", headers={"content-type": "text/html"})
        ),
    )
    result = await Maintenance(seeded, transport=transport).sweep()
    assert result["exit"] == 0
    provider.assert_awaited_once()
    await poll(Catalogue(seeded.db), src, transport)
    provider.assert_awaited_once()


async def test_cached_body_survives_transport_restart(seeded):
    requests = []

    def handler(request):
        requests.append(request)
        return (
            httpx.Response(304)
            if request.headers.get("if-none-match")
            else httpx.Response(
                200, text="calendar", headers={"etag": '"v1"', "content-type": "text/html"}
            )
        )

    def make():
        return Transport(
            {"issuer.example"},
            state=Catalogue(seeded.db),
            pace=0,
            resolver=lambda *a, **k: [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
            ],
            mock=httpx.MockTransport(handler),
        )

    assert (await make().get("https://issuer.example/calendar")).body == b"calendar"
    response = await make().get("https://issuer.example/calendar")
    assert response.body == b"calendar" and response.status == 200
    assert requests[-1].headers["if-none-match"] == '"v1"'


async def test_sec_requires_contact_before_network():
    with pytest.raises(ValueError, match="contact"):
        await Transport({"data.sec.gov"}).get("https://data.sec.gov/submissions/x")


async def test_registration_failure_cooldown_survives_restarts(settings):
    from earnings_watch import open
    from earnings_watch.registration import register_pending

    open(settings).watch("MISSING")
    runner = AsyncMock()
    runner.run.side_effect = RuntimeError("unavailable")
    assert (await register_pending(settings, Catalogue(settings.db), runner))["calls"] == 1
    assert (await register_pending(settings, Catalogue(settings.db), runner))["calls"] == 0
    runner.run.assert_awaited_once()


async def test_disabled_provider_makes_no_requests_or_poll_reservations(seeded):
    from earnings_watch import open
    from earnings_watch.maintenance import Maintenance
    from earnings_watch.store.database import connect

    open(seeded).watch("ALPHA")
    disabled = seeded.model_copy(update={"provider_allowlist": []})
    result = await Maintenance(disabled).sweep()
    assert result["exit"] == 0
    with connect(seeded.db) as db:
        assert db.execute("SELECT count(*) FROM release_watch").fetchone()[0] == 0


def test_provider_registry_and_cache_byte_budget(seeded):
    from earnings_watch.providers import definitions, NAMES
    from earnings_watch.store.database import connect
    from earnings_watch.settings import Settings

    assert definitions().keys() == NAMES
    with pytest.raises(ValueError, match="unknown provider"):
        Settings(provider_allowlist=["missing"])
    catalogue = Catalogue(seeded.db)
    catalogue.put_cache("cache:one", {"body": "x" * 100}, 150)
    catalogue.put_cache("cache:two", {"body": "y" * 100}, 150)
    assert not catalogue.state("fetch_state", "cache:one")
    assert catalogue.state("fetch_state", "cache:two")
    with connect(seeded.db) as db:
        assert (
            db.execute(
                "SELECT sum(length(CAST(doc AS BLOB))) FROM fetch_state WHERE key LIKE 'cache:%'"
            ).fetchone()[0]
            <= 150
        )
