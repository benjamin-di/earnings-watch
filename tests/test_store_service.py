from datetime import date, timedelta

import pytest

from earnings_watch import open
from earnings_watch.errors import EarningsError
from earnings_watch.store.database import (
    Catalogue,
    LibraryReader,
    backup,
    connect,
    integrity,
    restore,
)
from conftest import event, source


def test_people_share_facts_but_not_follows_or_calendar(seeded):
    alice, bob = open(seeded, "alice"), open(seeded, "bob")
    assert alice.watch("ALPHA")["state"] == "registered"
    assert bob.watchlist() == {"follows": []}
    assert alice.upcoming()["events"] == bob.upcoming()["events"]
    plan = alice.calendar_plan()
    result = [
        {"key": op["key"], "status": "applied", "provider_id": "remote"}
        for op in plan["operations"]
    ]
    assert alice.calendar_ack(plan["handle"], result)["applied"]
    assert not alice.calendar_plan()["operations"]
    assert not bob.calendar_plan()["operations"]
    with pytest.raises(EarningsError, match="invalid or expired"):
        bob.calendar_ack(plan["handle"], result)
    # Replaying an acknowledged plan is idempotent.
    assert alice.calendar_ack(plan["handle"], result)["applied"]


def test_stale_tampered_and_unknown_calendar_operations(seeded):
    service = open(seeded, "alice")
    plan = service.calendar_plan()
    with pytest.raises(EarningsError):
        service.calendar_ack(plan["handle"] + "x", [])
    with pytest.raises(EarningsError, match="unknown or duplicate"):
        service.calendar_ack(plan["handle"], [{"key": "foreign", "status": "applied"}])
    Catalogue(seeded.db).commit_issuer(
        "alpha", [event(date=date.today() + timedelta(days=2))], run_id="change"
    )
    with pytest.raises(EarningsError, match="changed"):
        service.calendar_ack(plan["handle"], [])


def test_rejected_calendar_write_is_not_recorded(seeded):
    service = open(seeded)
    service.watch("ALPHA")
    plan = service.calendar_plan()
    key = plan["operations"][0]["key"]
    assert service.calendar_ack(plan["handle"], [{"key": key, "status": "rejected"}])[
        "rejected"
    ] == [key]
    assert service.calendar_plan()["operations"]


def test_backup_restore_and_wal_read_during_write(seeded, tmp_path):
    path = tmp_path / "backup.db"
    backup(seeded.db, path)
    with connect(seeded.db, write=True) as db:
        db.execute("UPDATE people SET display_name=? WHERE id=?", ("Changed", "alice"))
        assert LibraryReader(seeded.db).events()
    restore(seeded.db, path)
    assert integrity(seeded.db) == "ok"
    with connect(seeded.db) as db:
        assert (
            db.execute('SELECT display_name FROM people WHERE id="alice"').fetchone()[0] == "alice"
        )
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    with pytest.raises(EarningsError):
        backup(seeded.db, path)


def test_unknown_person_is_refused(seeded):
    with pytest.raises(EarningsError, match="unknown person"):
        open(seeded, "unknown")


def test_colliding_ticker_requires_exchange_and_keeps_distinct_event_ids(settings):
    catalogue = Catalogue(settings.db)
    catalogue.put_source(source("first", "SAME", "XNAS"))
    catalogue.put_source(source("second", "SAME", "XLON"))
    catalogue.commit_issuer("first", [event("first", "SAME")], run_id="first")
    catalogue.commit_issuer("second", [event("second", "SAME")], run_id="second")
    service = open(settings)
    assert service.watch("SAME")["state"] == "ambiguous"
    assert service.watch("SAME", "XLON")["source_key"] == "second"
    assert len(service.upcoming()["events"]) == 1
    assert len(service.upcoming(scope="all")["events"]) == 2


def test_dormant_issuer_keeps_history(seeded):
    service = open(seeded)
    service.watch("ALPHA")
    assert Catalogue(seeded.db).active_keys() == {"alpha"}
    service.unwatch("ALPHA")
    assert not Catalogue(seeded.db).active_keys()
    assert service.get_events(["earnings:alpha:2026Q4"])["events"]
    service.watch("ALPHA")
    assert Catalogue(seeded.db).active_keys() == {"alpha"}


def test_unchanged_verification_does_not_create_revision_or_calendar_churn(seeded):
    catalogue = Catalogue(seeded.db)
    before = catalogue.status()["sequence"]
    catalogue.commit_issuer(
        "alpha", [event(last_verified=date.today() + timedelta(days=1))], run_id="repeat"
    )
    assert catalogue.status()["sequence"] == before


def test_incomplete_issuer_cannot_remove_and_large_removal_is_atomic(seeded):
    catalogue = Catalogue(seeded.db)
    catalogue.commit_issuer("alpha", [], run_id="incomplete", authoritative=False)
    assert catalogue.events()
    catalogue.commit_issuer(
        "alpha",
        [event(period=f"2026Q{n}", id=f"earnings:alpha:2026Q{n}") for n in range(1, 5)],
        run_id="many",
    )
    with pytest.raises(EarningsError, match="removal cap"):
        catalogue.commit_issuer("alpha", [], run_id="remove", authoritative=True)
    assert len(catalogue.events()) == 4


@pytest.mark.parametrize("days,limit", [(0, 1), (91, 1), (1, 0), (1, 101)])
def test_query_caps(seeded, days, limit):
    with pytest.raises(EarningsError):
        open(seeded).upcoming(days, limit=limit)


def test_cursor_and_ics(seeded):
    service = open(seeded)
    service.watch("ALPHA")
    first = service.changes()
    assert first["changes"] and not service.changes(first["cursor"])["changes"]
    ics = service.calendar_ics()
    assert "UID:earnings-watch:earnings:alpha:2026Q4" in ics
    assert "DTSTART;VALUE=DATE:" in ics
    assert all(len(line.encode()) <= 75 for line in ics.splitlines())


def test_registration_quota_is_personal(settings):
    alice, bob = open(settings, "alice"), open(settings, "bob")
    for ticker in ["ONE", "TWO", "THREE"]:
        assert alice.watch(ticker)["state"] == "queued"
    with pytest.raises(EarningsError, match="daily registration"):
        alice.watch("FOUR")
    assert bob.watch("FOUR")["state"] == "queued"


def test_personal_writes_do_not_modify_catalogue(seeded):
    before = LibraryReader(seeded.db).events()
    service = open(seeded)
    service.watch("ALPHA")
    service.unwatch("ALPHA")
    assert LibraryReader(seeded.db).events() == before
    with connect(seeded.db) as db:
        with pytest.raises(Exception):
            db.execute("DELETE FROM events")


def test_calendar_plan_rejects_changed_follows(seeded):
    service = open(seeded)
    plan = service.calendar_plan()
    service.watch("OTHER")
    with pytest.raises(EarningsError, match="changed"):
        service.calendar_ack(plan["handle"], [])
    assert service.calendar_plan()["operations"] == []


def test_removing_queued_follow_cannot_reset_registration_budget(settings):
    service = open(settings)
    for ticker in ["ONE", "TWO", "THREE"]:
        service.watch(ticker)
        service.unwatch(ticker)
    with pytest.raises(EarningsError, match="daily registration"):
        service.watch("FOUR")


def test_unwatching_last_issuer_plans_deletion_instead_of_following_everyone(seeded):
    service = open(seeded)
    service.watch("ALPHA")
    plan = service.calendar_plan()
    service.calendar_ack(
        plan["handle"],
        [
            {"key": op["key"], "status": "applied", "provider_id": "external"}
            for op in plan["operations"]
        ],
    )
    service.unwatch("ALPHA")
    ops = service.calendar_plan()["operations"]
    assert len(ops) == 1 and ops[0]["action"] == "delete"
    assert "DTSTART" not in service.calendar_ics()
