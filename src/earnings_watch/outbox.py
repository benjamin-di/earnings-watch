"""Durable personal delivery state; uncertain effects are never automatically replayed."""

from typing import Any, Protocol
from contextlib import contextmanager
import fcntl
import hashlib
import os
from .store.database import connect, encode, now, legacy_blocked

KINDS = frozenset(
    {
        "change",
        "reminder",
        "documents",
        "release_missing",
        "degraded",
        "digest",
        "calendar_attention",
        "results",
        "brief",
        "urgent",
    }
)


class Notifier(Protocol):
    async def send(self, person: str, key: str, kind: str, payload: dict[str, Any]) -> str: ...


def enqueue(path, person: str, key: str, kind: str, payload: dict[str, Any]) -> None:
    if legacy_blocked(path, person):
        return
    if kind not in KINDS:
        raise ValueError("unknown notification kind")
    with connect(path, write=True) as db:
        db.execute(
            "INSERT OR IGNORE INTO deliveries(person,key,kind,state,payload) VALUES(?,?,?,?,?)",
            (person, key, kind, "pending", encode(payload)),
        )
        if kind in {"results", "urgent"}:
            db.execute(
                "UPDATE deliveries SET payload=? WHERE person=? AND key=? AND state='pending'",
                (encode(payload), person, key),
            )


@contextmanager
def delivery_lock(path, person):
    name = "delivery-" + hashlib.sha256(person.encode()).hexdigest() + ".lock"
    fd = os.open(path.parent / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


async def deliver(path, person: str, notifier: Notifier) -> dict:
    try:
        with delivery_lock(path, person):
            return await _deliver(path, person, notifier)
    except BlockingIOError:
        return {"sent": [], "unknown": [], "blocked": "delivery already running"}


async def _deliver(path, person: str, notifier: Notifier) -> dict:
    import json

    with connect(path) as db:
        pending = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM deliveries WHERE person=? AND state='pending' ORDER BY key",
                (person,),
            )
        ]
    if legacy_blocked(path, person):
        return {"sent": [], "unknown": [], "blocked": "legacy reconciliation required"}
    sent, unknown = [], []
    for row in pending:
        with connect(path, write=True) as db:
            current = db.execute(
                "SELECT * FROM deliveries WHERE person=? AND key=? AND state='pending'",
                (person, row["key"]),
            ).fetchone()
            if current is None:
                continue
            row = dict(current)
            if not _eligible(db, person, row):
                continue
        ready = getattr(notifier, "ready", None)
        if ready is not None:
            try:
                if not await ready(person, row["key"], row["kind"], json.loads(row["payload"])):
                    continue
            except Exception:
                continue  # Identity/provider readiness failed before any account effect.
        with connect(path, write=True) as db:
            current = db.execute(
                "SELECT * FROM deliveries WHERE person=? AND key=? AND state='pending'",
                (person, row["key"]),
            ).fetchone()
            if current is None:
                continue
            row = dict(current)
            if not _eligible(db, person, row):
                continue
            claimed = db.execute(
                "UPDATE deliveries SET state='unknown',attempts=attempts+1 WHERE person=? AND key=? AND state='pending'",
                (person, row["key"]),
            )
            if not claimed.rowcount:
                continue
        try:
            message_id = await notifier.send(
                person, row["key"], row["kind"], json.loads(row["payload"])
            )
        except Exception:
            unknown.append(row["key"])
            continue
        with connect(path, write=True) as db:
            db.execute(
                "UPDATE deliveries SET state='sent',message_id=?,sent_at=? WHERE person=? AND key=?",
                (message_id, now(), person, row["key"]),
            )
            if row["kind"] == "brief":
                body = json.loads(row["payload"])
                db.execute(
                    "UPDATE people SET notify_seq=max(notify_seq,?) WHERE id=?",
                    (body["sequence"], person),
                )
                db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES(?,?)",
                    ("brief-cutoff:" + person, body["cutoff"]),
                )
        sent.append(row["key"])
    return {"sent": sent, "unknown": unknown}


def reconcile(path, person: str, key: str, receipt: dict) -> dict:
    from .errors import EarningsError

    try:
        with delivery_lock(path, person):
            return _reconcile(path, person, key, receipt)
    except BlockingIOError:
        raise EarningsError(
            "conflict", "delivery is still running; stop and verify it first"
        ) from None


def _reconcile(path, person: str, key: str, receipt: dict) -> dict:
    """Operator proof of a submission or proven absence; never infer absence from timeout."""
    import json
    from .errors import EarningsError
    from .store.database import digest

    if (
        not isinstance(receipt, dict)
        or not receipt.get("evidence")
        or receipt.get("outcome") not in {"submitted", "not_submitted"}
    ):
        raise EarningsError("invalid_argument", "submission evidence and outcome are required")
    with connect(path, write=True) as db:
        row = db.execute(
            "SELECT state,payload,attempts FROM deliveries WHERE person=? AND key=?", (person, key)
        ).fetchone()
        if (
            row is None
            or row[0] != "unknown"
            or receipt.get("payload_sha256") != digest(json.loads(row[1]))
            or receipt.get("person") != person
            or receipt.get("key") != key
            or receipt.get("attempt") != row[2]
        ):
            raise EarningsError("conflict", "receipt must match the current unknown submission")
        if receipt["outcome"] == "submitted":
            if not isinstance(receipt.get("message_id"), str) or not receipt["message_id"]:
                raise EarningsError(
                    "invalid_argument", "confirmed submission requires a message receipt"
                )
            db.execute(
                "UPDATE deliveries SET state='sent',message_id=?,sent_at=? WHERE person=? AND key=?",
                (receipt["message_id"], now(), person, key),
            )
            body = json.loads(row[1])
            if key.startswith("brief:"):
                db.execute(
                    "UPDATE people SET notify_seq=max(notify_seq,?) WHERE id=?",
                    (body["sequence"], person),
                )
                db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES(?,?)",
                    ("brief-cutoff:" + person, body["cutoff"]),
                )
        else:
            db.execute(
                "UPDATE deliveries SET state='pending' WHERE person=? AND key=?", (person, key)
            )
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES(?,?)",
            (
                "delivery-review:" + person + ":" + key + ":" + str(row[2]),
                encode(receipt | {"at": now()}),
            ),
        )
    return {"outcome": receipt["outcome"]}


def _eligible(db, person, row):
    import json

    if row["kind"] in {"results", "brief", "urgent"}:
        pref = json.loads(
            db.execute("SELECT settings FROM profiles WHERE person=?", (person,)).fetchone()[0]
        )
        keys = {
            r[0]
            for r in db.execute(
                "SELECT source_key FROM follows f WHERE person=? AND NOT EXISTS (SELECT 1 FROM mutes m WHERE m.person=f.person AND m.ticker=f.ticker AND (m.exchange='' OR m.exchange=f.exchange))",
                (person,),
            )
        }
        payload = json.loads(row["payload"])
        if row["kind"] == "brief":
            payload["events"] = [e for e in payload.get("events", []) if e["issuer_key"] in keys]
            payload["changes"] = [
                c for c in payload.get("changes", []) if c["doc"]["issuer_key"] in keys
            ]
            payload["documents"] = [
                d
                for d in payload.get("documents", [])
                if db.execute(
                    "SELECT 1 FROM events WHERE id=? AND source_key IN (SELECT source_key FROM follows f WHERE person=? AND NOT EXISTS (SELECT 1 FROM mutes m WHERE m.person=f.person AND m.ticker=f.ticker AND (m.exchange='' OR m.exchange=f.exchange)))",
                    (d["event_id"], person),
                ).fetchone()
            ]
            row["payload"] = encode(payload)
            db.execute(
                "UPDATE deliveries SET payload=? WHERE person=? AND key=? AND state='pending'",
                (row["payload"], person, row["key"]),
            )
        event_keys = {e["issuer_key"] for e in payload.get("events", [])}
        if payload.get("event"):
            event_keys.add(payload["event"]["issuer_key"])
        event_keys.update(c["doc"]["issuer_key"] for c in payload.get("changes", []))
        for document in payload.get("documents", []):
            source = db.execute(
                "SELECT source_key FROM events WHERE id=?", (document["event_id"],)
            ).fetchone()
            if source:
                event_keys.add(source[0])
        if (
            not pref.get(row["kind"], row["kind"] == "results")
            or not event_keys
            or not event_keys <= keys
        ):
            db.execute(
                "UPDATE deliveries SET state='suppressed' WHERE person=? AND key=?",
                (person, row["key"]),
            )
            return False
        if row["kind"] == "urgent":
            from .notify import urgent_transition

            current = urgent_transition(db, payload["event"]["id"])
            if current is None or current["seq"] != payload["sequence"]:
                db.execute(
                    "UPDATE deliveries SET state='superseded' WHERE person=? AND key=? AND state='pending'",
                    (person, row["key"]),
                )
                return False
    return True
