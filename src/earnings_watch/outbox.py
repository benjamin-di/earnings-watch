"""Durable personal delivery state; uncertain effects are never automatically replayed."""

from typing import Any, Protocol
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


async def deliver(path, person: str, notifier: Notifier) -> dict:
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
        sent.append(row["key"])
    return {"sent": sent, "unknown": unknown}
