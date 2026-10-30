"""Optional calendar delegation. This package never writes to Outlook itself."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .errors import EarningsError
from .store.database import PersonalStore, digest, encode, legacy_blocked


def desired(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    entries = []
    for event in events:
        if (
            event["type"] not in {"earnings", "earnings-call", "agm", "cmd"}
            or event["status"] == "past"
        ):
            continue
        day = event["date"]
        exact = (
            event.get("release_time") and event.get("release_tz") and not event.get("timing_issue")
        )
        title = f"{event['ticker']} {event['period']} {event['type']}"
        entry = {
            "key": "earnings-watch:" + event["id"],
            "title": title,
            "body": event["evidence"] + "\n" + event["source_url"],
            "all_day": not bool(exact),
        }
        if exact:
            start = datetime.fromisoformat(day + "T" + event["release_time"]).replace(
                tzinfo=ZoneInfo(event["release_tz"])
            )
            entry.update(start=start.isoformat(), end=(start + timedelta(minutes=30)).isoformat())
        else:
            entry.update(
                start=day, end=(datetime.fromisoformat(day) + timedelta(days=1)).date().isoformat()
            )
        entry["fingerprint"] = digest(entry)
        entry["revision"] = event["revision"]
        entries.append(entry)
    return entries


def plan(
    person: PersonalStore,
    *,
    key: bytes,
    sink: str = "delegated",
    full: bool = False,
) -> dict[str, Any]:
    if legacy_blocked(person.path, person.person):
        raise EarningsError("temporarily_unavailable", "legacy calendar reconciliation required")
    if not sink or len(sink) > 64:
        raise EarningsError("invalid_argument", "invalid calendar sink")
    snapshot = person.calendar_snapshot(sink)
    rev, ledger = snapshot["ledger_rev"], snapshot["ledger"]
    previous = {row["key"]: row for row in ledger}
    entries = {row["key"]: row for row in desired(snapshot["events"])}
    ops = []
    for identifier, entry in entries.items():
        old = previous.get(identifier)
        if full or not old or old["fingerprint"] != entry["fingerprint"]:
            ops.append(
                entry
                | {
                    "action": "update" if old else "create",
                    "provider_id": old["provider_id"] if old else None,
                }
            )
    for identifier in previous.keys() - entries.keys():
        ops.append(
            {
                "action": "delete",
                "key": identifier,
                "provider_id": previous[identifier]["provider_id"],
            }
        )
    if len(ops) > 100:
        raise EarningsError("over_cap", "calendar plan exceeds 100 operations")
    payload = {
        "person": person.person,
        "sink": sink,
        "ledger_rev": rev,
        "sequence": snapshot["sequence"],
        "follows": snapshot["follows"],
        "exp": int(time.time()) + 1800,
        "operations": ops,
    }
    data = base64.urlsafe_b64encode(encode(payload).encode()).decode()
    signature = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
    return {"handle": data + "." + signature, "operations": ops, "expires_at": payload["exp"]}


def ack(
    person: PersonalStore, handle: str, results: list[dict[str, Any]], *, key: bytes
) -> dict[str, Any]:
    if len(handle) > 262144 or len(results) > 100:
        raise EarningsError("invalid_argument", "calendar acknowledgement exceeds its budget")
    try:
        data, signature = handle.rsplit(".", 1)
        expected = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("signature")
        payload = json.loads(base64.urlsafe_b64decode(data))
        if payload["person"] != person.person or payload["exp"] < time.time():
            raise ValueError("identity or expiry")
    except (ValueError, KeyError, TypeError):
        raise EarningsError("plan_stale", "invalid or expired calendar handle") from None
    return person.acknowledge(payload, results, hashlib.sha256(handle.encode()).hexdigest())
