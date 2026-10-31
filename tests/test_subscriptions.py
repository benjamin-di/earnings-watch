"""Subscriptions must remain private and restart-safe across ambiguous effects."""

from datetime import datetime, timedelta, timezone

import pytest

from conftest import event
from earnings_watch import open
from earnings_watch.errors import EarningsError
from earnings_watch.notify import notices, prepare
from earnings_watch.outbox import deliver
from earnings_watch.store.accounts import profile, preferences, feed_token
from earnings_watch.store.database import Catalogue, PersonalStore, backup, connect, restore


def subscribe(settings, person="alice", **options):
    open(settings, person).watch("ALPHA")
    current = profile(settings.db, person)
    preferences(settings.db, person, current["revision"], current["settings"] | options, "UTC")


def put_doc(settings, identifier, *, role="release", at=None):
    Catalogue(settings.db).put_document(
        dict(
            id=identifier,
            event_id=event().id,
            url="https://issuer.example/" + identifier + ".pdf",
            title=identifier,
            role=role,
            verified=True,
            sha256="",
            first_seen=(at or datetime.now(timezone.utc)).isoformat(),
        )
    )


async def test_results_once_late_documents_once_and_recipient_isolation(seeded):
    subscribe(seeded)
    put_doc(seeded, "first")
    assert len(notices(seeded, "alice")) == 1
    assert notices(seeded, "bob") == []
    sent = []

    class Sender:
        async def send(self, person, key, kind, payload):
            sent.append((person, key, payload))
            return "submitted-" + str(len(sent))

    prepare(seeded)
    await deliver(seeded.db, "alice", Sender())
    prepare(seeded)
    await deliver(seeded.db, "alice", Sender())
    assert len(sent) == 1
    put_doc(seeded, "late", role="statements")
    prepare(seeded)
    await deliver(seeded.db, "alice", Sender())
    assert len(sent) == 2 and sent[1][1].endswith(":followup")
    put_doc(seeded, "later", role="presentation")
    prepare(seeded)
    await deliver(seeded.db, "alice", Sender())
    assert len(sent) == 2
    assert all(row[0] == "alice" for row in sent)


async def test_unknown_submission_is_not_retried_or_followed_up(seeded):
    subscribe(seeded)
    put_doc(seeded, "first")
    calls = []

    class Timeout:
        async def send(self, *args):
            calls.append(args)
            raise TimeoutError("submission may have succeeded")

    prepare(seeded)
    assert (await deliver(seeded.db, "alice", Timeout()))["unknown"]
    put_doc(seeded, "late", role="statements")
    prepare(seeded)
    await deliver(seeded.db, "alice", Timeout())
    assert len(calls) == 1
    assert not notices(seeded, "alice")


async def test_preflight_outage_keeps_pending_and_preference_revocation_suppresses(seeded):
    subscribe(seeded)
    put_doc(seeded, "first")
    prepare(seeded)

    class Unready:
        async def ready(self, *args):
            raise OSError("directory down")

        async def send(self, *args):
            pytest.fail("must not send")

    assert (await deliver(seeded.db, "alice", Unready())) == {"sent": [], "unknown": []}
    with connect(seeded.db) as db:
        assert (
            db.execute("SELECT state FROM deliveries WHERE person='alice'").fetchone()[0]
            == "pending"
        )
    subscribe(seeded, results=False)
    await deliver(seeded.db, "alice", Unready())
    with connect(seeded.db) as db:
        assert (
            db.execute("SELECT state FROM deliveries WHERE person='alice'").fetchone()[0]
            == "suppressed"
        )


async def test_brief_advances_only_after_submission_and_cannot_hide_results(seeded):
    subscribe(seeded, brief=True, hour=0)
    put_doc(seeded, "first")
    prepare(seeded)

    class Sender:
        async def send(self, *args):
            return "submitted"

    result = await deliver(seeded.db, "alice", Sender())
    assert len(result["sent"]) == 2
    assert not notices(seeded, "alice") or all(n[1] == "brief" for n in notices(seeded, "alice"))
    with connect(seeded.db) as db:
        assert db.execute("SELECT value FROM metadata WHERE key='brief-cutoff:alice'").fetchone()
        assert not db.execute("SELECT value FROM metadata WHERE key='baseline:alice'").fetchone()


def test_quiet_days_no_reminders_or_historical_documents(seeded):
    subscribe(seeded)
    put_doc(seeded, "historic", at=datetime.now(timezone.utc) - timedelta(days=2))
    assert not notices(seeded, "alice")
    subscribe(seeded, brief=True, hour=0)
    Catalogue(seeded.db).commit_issuer(
        "alpha", [event(date=datetime.now(timezone.utc).date() + timedelta(days=20))], run_id="far"
    )
    # A meaningful change can be briefed; mark it delivered then a quiet day is empty.
    with connect(seeded.db, write=True) as db:
        db.execute(
            "UPDATE people SET notify_seq=(SELECT max(seq) FROM event_revisions) WHERE id='alice'"
        )
    assert not notices(seeded, "alice")


def test_urgent_only_material_recent_changes_and_ten_minute_coalescing(seeded):
    subscribe(seeded, urgent=True)
    catalogue = Catalogue(seeded.db)
    catalogue.commit_issuer("alpha", [event(evidence="Updated evidence only")], run_id="prose")
    future = datetime.now(timezone.utc) + timedelta(minutes=11)
    assert not [r for r in notices(seeded, "alice", future) if r[1] == "urgent"]
    catalogue.commit_issuer(
        "alpha", [event(date=datetime.now(timezone.utc).date() + timedelta(days=3))], run_id="move"
    )
    assert not [r for r in notices(seeded, "alice") if r[1] == "urgent"]
    urgent = [r for r in notices(seeded, "alice", future) if r[1] == "urgent"]
    assert len(urgent) == 1 and urgent[0][2]["previous"]["date"] != urgent[0][2]["event"]["date"]


def test_atomic_form_revision_feed_rotation_and_restore_cursors(seeded, tmp_path):
    store = PersonalStore(seeded.db, "alice")
    initial = profile(seeded.db, "alice")["revision"]
    store.watch("ALPHA", revision=initial)
    with pytest.raises(EarningsError, match="changed"):
        store.unwatch("ALPHA", revision=initial)
    assert store.follows()
    key = (seeded.data_dir / "calendar.key").read_bytes()
    rev = profile(seeded.db, "alice")["revision"]
    feed_token(seeded.db, "alice", key, rotate=True, revision=rev)
    with pytest.raises(EarningsError, match="changed"):
        feed_token(seeded.db, "alice", key, rotate=True, revision=rev)
    cursor = open(seeded, "alice").changes()["cursor"]
    saved = tmp_path / "backup.db"
    backup(seeded.db, saved)
    restore(seeded.db, saved)
    with pytest.raises(EarningsError, match="cursor"):
        open(seeded, "alice").changes(cursor)


def test_explicit_calendar_only_and_no_unfollow_deletions(seeded):
    service = open(seeded, "alice")
    service.watch("ALPHA")
    assert not service.calendar_plan()["operations"]
    planned = service.calendar_plan(event_ids=[event().id])
    assert len(planned["operations"]) == 1
    service.calendar_ack(
        planned["handle"],
        [dict(key=planned["operations"][0]["key"], status="applied", provider_id="owned")],
    )
    service.unwatch("ALPHA")
    assert not service.calendar_plan()["operations"]
    assert service.person.ledger("delegated")[1][0]["provider_id"] == "owned"
