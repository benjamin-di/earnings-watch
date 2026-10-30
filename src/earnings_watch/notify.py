"""Personal notification projections. Sending is an explicit, separate operation."""

import json
from datetime import date, datetime, timedelta, timezone
from .store.database import people, connect, digest, legacy_blocked
from .composition import open_service
from .outbox import enqueue


class StdoutNotifier:
    async def send(self, person, key, kind, payload):
        print(json.dumps({"person": person, "delivery_key": key, "kind": kind, "payload": payload}))
        return key


class WebhookNotifier:
    def __init__(self, url, transport):
        self.url, self.transport = url, transport

    async def send(self, person, key, kind, payload):
        await self.transport.post(
            self.url,
            {"delivery_key": key, "kind": kind, "payload": payload},
            headers={"Idempotency-Key": key},
        )
        return key


def prepare(settings) -> None:
    today = datetime.now(timezone.utc).date()
    for person in people(settings.db):
        identifier = person["id"]
        if legacy_blocked(settings.db, identifier):
            continue
        service = open_service(settings, identifier)
        follows = service.person.follows()
        keys = {row["source_key"] for row in follows}
        events = [e for e in service.library.events() if e["issuer_key"] in keys]
        with connect(settings.db) as db:
            cursor = db.execute(
                "SELECT notify_seq FROM people WHERE id=?", (identifier,)
            ).fetchone()[0]
        while True:
            changes = service.library.changes(cursor, 100)
            if not changes:
                break
            for change in changes:
                if change["doc"].get("issuer_key") in keys:
                    enqueue(settings.db, identifier, f"change:{change['seq']}", "change", change)
            cursor = changes[-1]["seq"]
        with connect(settings.db, write=True) as db:
            db.execute("UPDATE people SET notify_seq=? WHERE id=?", (cursor, identifier))
        for notice_key, kind, payload in event_notices(events, service.library.documents(), today):
            enqueue(settings.db, identifier, notice_key, kind, payload)
        with connect(settings.db) as db:
            failures = [
                dict(r)
                for r in db.execute(
                    "SELECT key,doc FROM fetch_state WHERE key NOT LIKE 'http:%' AND key NOT LIKE 'cache:%'"
                )
                if json.loads(r["doc"]).get("error")
            ]
        for failure in failures:
            if failure["key"] in keys:
                enqueue(settings.db, identifier, "degraded:" + digest(failure), "degraded", failure)
        if not follows:
            continue
        week = today.isocalendar()
        enqueue(
            settings.db,
            identifier,
            f"digest:{week.year}:{week.week}",
            "digest",
            {
                "week": str(today),
                "events": [
                    e
                    for e in events
                    if today <= date.fromisoformat(e["date"]) <= today + timedelta(days=7)
                ],
            },
        )


def event_notices(events, documents, today):
    for event in events:
        day = date.fromisoformat(event["date"])
        if today <= day <= today + timedelta(days=2):
            yield (
                f"reminder:{event['id']}:{event['date']}",
                "reminder",
                event,
            )
        docs = [d for d in documents if d["event_id"] == event["id"] and d["verified"]]
        if docs:
            signature = digest([{k: d[k] for k in ("id", "url", "sha256", "role")} for d in docs])
            yield (
                f"documents:{event['id']}:{signature}",
                "documents",
                {"event": event, "documents": docs},
            )
        if event["type"] == "earnings" and day + timedelta(days=1) == today and not docs:
            yield (
                f"release_missing:{event['id']}:{event['date']}",
                "release_missing",
                event,
            )
        if event.get("timing_issue"):
            yield (
                f"calendar_attention:{event['id']}:{event['revision']}",
                "calendar_attention",
                event,
            )
