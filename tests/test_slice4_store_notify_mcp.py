"""Slice 4 regression tests: store, notify, legacy reconciliation, and interests."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta, timezone

import pytest
import yaml

from conftest import event, source
from earnings_watch import open as open_ew
from earnings_watch.errors import EarningsError
from earnings_watch.interests import apply as apply_interests
from earnings_watch.interests import preview as preview_interests
from earnings_watch.legacy import import_snapshot, reconcile
from earnings_watch.notify import event_notices, notices, prepare
from earnings_watch.store.accounts import preferences, profile
from earnings_watch.store.database import (
    Catalogue,
    PersonalStore,
    add_person,
    connect,
    encode,
    sync_holdings,
)


def _put_doc(settings, doc_id: str, *, event_id: str | None = None, role: str = "release", at=None):
    Catalogue(settings.db).put_document(
        {
            "id": doc_id,
            "event_id": event_id or event().id,
            "url": f"https://issuer.example/{doc_id}.pdf",
            "title": doc_id,
            "role": role,
            "verified": True,
            "sha256": "",
            "first_seen": (at or datetime.now(timezone.utc)).isoformat(),
        }
    )


def test_sync_holdings_persists_canonical_exchange(seeded):
    result = sync_holdings(seeded.db, "alice", "portfolio", [{"ticker": "ALPHA"}])
    assert result == {"feed": "portfolio", "follows": 1}
    follows = PersonalStore(seeded.db, "alice").follows()
    assert len(follows) == 1
    assert follows[0]["ticker"] == "ALPHA"
    assert follows[0]["exchange"] == "XNAS"

    # Bare and exchange-qualified entries for the same source deduplicate cleanly.
    dedup = sync_holdings(
        seeded.db,
        "alice",
        "portfolio",
        [{"ticker": "ALPHA"}, {"ticker": "ALPHA", "exchange": "XNAS"}],
    )
    assert dedup == {"feed": "portfolio", "follows": 1}
    assert PersonalStore(seeded.db, "alice").follows()[0]["exchange"] == "XNAS"


def test_put_source_propagates_ticker_and_exchange_to_registered_follows(seeded):
    store = PersonalStore(seeded.db, "alice")
    store.watch("ALPHA", "XNAS")
    # Also add a pending follow matching the new ticker/exchange to ensure no UNIQUE collision.
    store.watch("ALPH2", "XNYS")

    Catalogue(seeded.db).put_source(source(key="alpha", ticker="ALPH2", exchange="XNYS"))
    follows = store.follows()
    assert len(follows) == 1
    assert follows[0]["source_key"] == "alpha"
    assert follows[0]["ticker"] == "ALPH2"
    assert follows[0]["exchange"] == "XNYS"
    assert follows[0]["state"] == "registered"


def test_unwatch_empty_exchange_does_not_wildcard_delete_qualified_follows(seeded):
    store = PersonalStore(seeded.db, "alice")
    store.watch("ALPHA", "XNAS")
    # Insert an exchange-less manual follow alongside the XNAS manual follow.
    with connect(seeded.db, write=True) as db:
        db.execute(
            "INSERT INTO follows(person,source_key,ticker,exchange,origin,state,added_at) VALUES(?,?,?,?,?,?,?)",
            (
                "alice",
                None,
                "ALPHA",
                "",
                "manual",
                "queued",
                datetime.now(timezone.utc).isoformat(),
            ),
        )

    # Explicit empty exchange deletes only exchange='' rows, preserving ALPHA (XNAS).
    assert store.unwatch("ALPHA", "") == {"state": "removed"}
    remaining = store.follows()
    assert len(remaining) == 1
    assert (remaining[0]["ticker"], remaining[0]["exchange"]) == ("ALPHA", "XNAS")

    # Calling unwatch("ALPHA", "") again when only XNAS exists must still preserve XNAS.
    store.unwatch("ALPHA", "")
    remaining = store.follows()
    assert len(remaining) == 1
    assert (remaining[0]["ticker"], remaining[0]["exchange"]) == ("ALPHA", "XNAS")

    # Wildcard unwatch when exchange is None removes ALPHA (XNAS).
    assert store.unwatch("ALPHA") == {"state": "removed"}
    assert store.follows() == []


async def test_prepare_enforces_portfolio_admitted_and_skips_disabled_users(seeded):
    add_person(seeded.db, "carol")
    add_person(seeded.db, "dave")
    cat = Catalogue(seeded.db)
    cat.put_source(source(key="beta", ticker="BETA", exchange="XNYS"))
    beta_ev = event(key="beta", ticker="BETA")
    cat.commit_issuer("beta", [beta_ev], run_id="beta")

    sync_holdings(seeded.db, "alice", "broker", [{"ticker": "ALPHA", "exchange": "XNAS"}])
    sync_holdings(seeded.db, "bob", "broker", [{"ticker": "ALPHA", "exchange": "XNAS"}])
    PersonalStore(seeded.db, "bob").watch("BETA", "XNYS")
    PersonalStore(seeded.db, "carol").watch("ALPHA", "XNAS")
    PersonalStore(seeded.db, "dave").watch("ALPHA", "XNAS")

    _put_doc(seeded, "alpha-rel", event_id=event().id, role="release")
    _put_doc(seeded, "beta-rel", event_id=beta_ev.id, role="release")

    class FakeDirectory:
        def __init__(self):
            self.records = {
                "local": {
                    "id": "local",
                    "admitted": False,
                    "disabled": True,
                    "email": "",
                    "portfolio_admitted": False,
                },
                "alice": {
                    "id": "alice",
                    "admitted": True,
                    "disabled": False,
                    "email": "a@example.com",
                    "portfolio_admitted": True,
                },
                "bob": {
                    "id": "bob",
                    "admitted": True,
                    "disabled": False,
                    "email": "b@example.com",
                    "portfolio_admitted": False,
                },
                "carol": {
                    "id": "carol",
                    "admitted": True,
                    "disabled": True,
                    "email": "c@example.com",
                    "portfolio_admitted": True,
                },
                "dave": {
                    "id": "dave",
                    "admitted": False,
                    "disabled": False,
                    "email": "d@example.com",
                    "portfolio_admitted": True,
                },
            }

        async def resolve(self, subject: str, *, fresh: bool = False) -> dict:
            return dict(self.records[subject])

    prepare(seeded, directory=FakeDirectory())

    with connect(seeded.db) as db:
        rows = {
            (r["person"], r["key"])
            for r in db.execute("SELECT person, key FROM deliveries WHERE state='pending'")
        }
    assert rows == {
        ("alice", f"results:{event().id}:initial"),
        ("bob", f"results:{beta_ev.id}:initial"),
    }


def test_legacy_reconcile_writes_baseline_key_metadata_and_uses_issuer_key(settings, tmp_path):
    directory = tmp_path / "snapshot"
    directory.mkdir()
    src = source(key="custom-issuer-key", ticker="ALPHA", exchange="XNAS")
    ev = event(key="custom-issuer-key", ticker="ALPHA")
    row = ev.model_dump(mode="json") | {
        "calendar_synced": True,
        "calendar_linked_id": "ext-1",
        "calendar_sync_fingerprint": "old-fp",
        "results_links": [
            {
                "title": "Q4 Release",
                "kind": "press-release",
                "url": "https://issuer.example/q4-release.pdf",
                "verified_at": "2026-10-01T00:00:00+00:00",
            }
        ],
    }
    (directory / "ir-sources.yaml").write_text(
        yaml.safe_dump({"sources": [src.model_dump(mode="json") | {"membership": "always"}]})
    )
    (directory / "events.yaml").write_text(yaml.safe_dump({"events": [row]}))
    import_snapshot(settings.db, directory, "local")

    # Even if the source ticker changes before reconciliation, issuer_key on the legacy row resolves it.
    Catalogue(settings.db).put_source(src.model_copy(update={"ticker": "ALPHA2"}))

    reconcile(
        settings.db,
        "local",
        {
            "evidence": "Verified externally",
            "notifications_reviewed": True,
            "calendar_absent": ["earnings-watch:" + ev.id],
        },
    )

    with connect(settings.db) as db:
        baseline_delivery = db.execute(
            "SELECT key, state, payload FROM deliveries WHERE person='local' AND key=?",
            (f"results:{ev.id}:initial",),
        ).fetchone()
        baseline_meta = db.execute(
            "SELECT value FROM metadata WHERE key='baseline:local'"
        ).fetchone()
    assert baseline_delivery is not None
    assert baseline_delivery["state"] == "sent"
    payload = json.loads(baseline_delivery["payload"])
    assert "day" in payload and "sent_at" in payload
    assert baseline_meta is not None and baseline_meta[0]

    # prepare() must not enqueue a duplicate initial results delivery after reconciliation.
    prepare(settings)
    with connect(settings.db) as db:
        pending = list(
            db.execute("SELECT key FROM deliveries WHERE person='local' AND state='pending'")
        )
    assert pending == []

    # event_notices() directly emits results:<id>:initial with day and sent_at.
    emitted = list(
        event_notices(
            [ev.model_dump(mode="json")],
            [{"id": "d1", "event_id": ev.id, "verified": True}],
            datetime(2026, 11, 27, tzinfo=timezone.utc).date(),
        )
    )
    assert len(emitted) == 1
    key, kind, ev_payload = emitted[0]
    assert key == f"results:{ev.id}:initial"
    assert kind == "results"
    assert ev_payload["day"] == "2026-11-27"
    assert ev_payload["sent_at"]


def test_results_followup_24h_utc_window_and_normalized_timestamps(seeded):
    open_ew(seeded, "alice").watch("ALPHA")
    pref = profile(seeded.db, "alice")
    preferences(
        seeded.db,
        "alice",
        pref["revision"],
        pref["settings"] | {"results": True, "brief": True, "urgent": True, "hour": 0},
        "UTC",
    )

    t0 = datetime(2026, 11, 26, 23, 30, tzinfo=timezone.utc)
    # Set baseline before t0.
    with connect(seeded.db, write=True) as db:
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES('baseline:alice', ?)",
            ((t0 - timedelta(hours=1)).isoformat(),),
        )
    _put_doc(seeded, "rel-1", role="release", at=t0)

    initial_notices = [n for n in notices(seeded, "alice", at=t0) if n[1] == "results"]
    assert len(initial_notices) == 1
    init_key, _, init_payload = initial_notices[0]
    assert init_key == f"results:{event().id}:initial"
    assert init_payload["sent_at"] == t0.isoformat()

    # Mark initial as sent.
    with connect(seeded.db, write=True) as db:
        db.execute(
            "INSERT INTO deliveries(person,key,kind,state,payload,sent_at) VALUES(?,?,?,'sent',?,?)",
            ("alice", init_key, "results", encode(init_payload), t0.isoformat()),
        )

    # Late document arrives across UTC midnight (only 1.5h later) -> followup IS allowed.
    t1 = t0 + timedelta(hours=1, minutes=30)
    _put_doc(seeded, "stmt-1", role="statements", at=t1)
    followup_within_24h = [n for n in notices(seeded, "alice", at=t1) if n[1] == "results"]
    assert [n[0] for n in followup_within_24h] == [f"results:{event().id}:followup"]

    # After 24h window -> followup is NOT emitted.
    t_expired = t0 + timedelta(hours=24, minutes=1)
    assert not [n for n in notices(seeded, "alice", at=t_expired) if n[1] == "results"]

    # Legacy payload without sent_at falls back to initial.get("day") == str(today).
    legacy_payload = {k: v for k, v in init_payload.items() if k != "sent_at"}
    with connect(seeded.db, write=True) as db:
        db.execute(
            "UPDATE deliveries SET payload=? WHERE person='alice' AND key=?",
            (encode(legacy_payload), init_key),
        )
    same_day = datetime(2026, 11, 26, 23, 55, tzinfo=timezone.utc)
    next_day = datetime(2026, 11, 27, 0, 5, tzinfo=timezone.utc)
    assert [n[0] for n in notices(seeded, "alice", at=same_day) if n[1] == "results"] == [
        f"results:{event().id}:followup"
    ]
    assert not [n for n in notices(seeded, "alice", at=next_day) if n[1] == "results"]

    # Non-UTC offset cutoff (+08:00) vs UTC (+00:00) event_revisions.at comparison.
    # 2026-11-27T10:00:00+08:00 == 2026-11-27T02:00:00+00:00.
    # A revision at 2026-11-27T02:30:00+00:00 is AFTER the cutoff even though "02:30" < "10:00" as strings.
    with connect(seeded.db, write=True) as db:
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES('baseline:alice', ?)",
            ("2026-11-27T10:00:00+08:00",),
        )
        db.execute("UPDATE people SET notify_seq=0 WHERE id='alice'")
        db.execute(
            "UPDATE event_revisions SET at='2026-11-27T02:30:00+00:00' WHERE event_id=?",
            (event().id,),
        )
    hk_at = datetime(2026, 11, 27, 11, 0, tzinfo=timezone(timedelta(hours=8)))
    brief_notices = [n for n in notices(seeded, "alice", at=hk_at) if n[1] == "brief"]
    assert len(brief_notices) == 1
    assert len(brief_notices[0][2]["changes"]) >= 1
    assert brief_notices[0][2]["cutoff"] == "2026-11-27T03:00:00+00:00"


def test_interests_hmac_domain_separation_and_normalize_exchange(seeded):
    # normalize_exchange resolves aliases like NASDAQ -> XNAS for company-name matching.
    p = preview_interests(seeded, "alice", "ticker,exchange\nExample Components,NASDAQ\n")
    assert p["items"] == [{"ticker": "ALPHA", "exchange": "XNAS"}]

    body_b64, _ = p["handle"].rsplit(".", 1)
    decoded = json.loads(base64.urlsafe_b64decode(body_b64))
    assert decoded["purpose"] == "interests_preview"

    # A handle signed with the same calendar.key but missing or wrong purpose must be rejected.
    key = (seeded.data_dir / "calendar.key").read_bytes()
    for bad_payload in (
        {k: v for k, v in decoded.items() if k != "purpose"},
        decoded | {"purpose": "calendar_plan", "exp": int(time.time()) + 900},
    ):
        raw = base64.urlsafe_b64encode(json.dumps(bad_payload, sort_keys=True).encode()).decode()
        sig = hmac.new(key, raw.encode(), hashlib.sha256).hexdigest()
        with pytest.raises(EarningsError, match="expired"):
            apply_interests(seeded, "alice", f"{raw}.{sig}")
