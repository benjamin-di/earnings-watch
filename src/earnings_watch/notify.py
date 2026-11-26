"""Personal subscriptions over native facts; preparation has no external effects."""

from __future__ import annotations

import asyncio
from email.message import EmailMessage
from email.utils import make_msgid
import json
import os
from pathlib import Path
import smtplib
import ssl
import stat
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .errors import EarningsError
from .render import render_email
from .store.database import people, connect, digest, legacy_blocked
from .store.accounts import profile, effective_follows, remembered_email
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


def read_private_secret_file(path: Path) -> str:
    """Read a secret file without following symlinks and requiring 0600 (or stricter) permissions."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise EarningsError(
            "invalid_argument", "SMTP password file could not be opened safely"
        ) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise EarningsError("invalid_argument", "SMTP password file must be a regular file")
        if st.st_mode & 0o077:
            raise EarningsError(
                "invalid_argument", "SMTP password file permissions must be 0600 or stricter"
            )
        with os.fdopen(fd, "r", encoding="utf-8") as handle:
            fd = -1
            return handle.read().strip()
    finally:
        if fd >= 0:
            os.close(fd)


class SmtpNotifier:
    """TLS-only SMTP delivery channel using pure stdlib rendering."""

    def __init__(
        self,
        db_path: Path,
        *,
        host: str,
        port: int = 587,
        sender: str,
        username: str = "",
        password_file: Path | None = None,
        tls: str = "starttls",
        to_address: str = "",
        directory=None,
        smtp_factory=None,
        timeout: int = 20,
    ):
        if not host or not sender:
            raise EarningsError("invalid_argument", "SMTP requires host and from address")
        if tls not in {"starttls", "ssl"}:
            raise EarningsError("invalid_argument", "SMTP tls must be 'starttls' or 'ssl'")
        self.db_path = db_path
        self.host = host
        self.port = port
        self.sender = sender
        self.username = username
        self.password = read_private_secret_file(password_file) if password_file else ""
        self.tls = tls
        self.to_address = to_address.strip()
        self.directory = directory
        self.smtp_factory = smtp_factory
        self.timeout = timeout

    async def _recipient(self, person: str) -> str:
        if self.directory is not None:
            info = await self.directory.resolve(person)
            if not info.get("admitted") or info.get("disabled"):
                return ""
            return str(info.get("email") or "").strip()
        return remembered_email(self.db_path, person) or self.to_address

    async def ready(self, person: str, key: str, kind: str, payload: dict) -> bool:
        return bool(await self._recipient(person))

    def _deliver_sync(self, message: EmailMessage) -> None:
        context = ssl.create_default_context()
        if self.smtp_factory is not None:
            client = self.smtp_factory(self.host, self.port, timeout=self.timeout, context=context)
        elif self.tls == "ssl":
            client = smtplib.SMTP_SSL(self.host, self.port, timeout=self.timeout, context=context)
        else:
            client = smtplib.SMTP(self.host, self.port, timeout=self.timeout)
        with client as smtp:
            if self.tls == "starttls":
                smtp.ehlo()
                smtp.starttls(context=context)
                smtp.ehlo()
            if self.username:
                smtp.login(self.username, self.password)
            smtp.send_message(message)

    async def send(self, person, key, kind, payload):
        recipient = await self._recipient(person)
        if not recipient:
            raise EarningsError("invalid_argument", "recipient email is unavailable")
        pref = profile(self.db_path, person)
        rendered = render_email(kind, payload, timezone_name=pref["timezone"])
        msg = EmailMessage()
        msg_id = make_msgid(domain="earnings-watch.local")
        msg["Message-ID"] = msg_id
        msg["Subject"] = rendered.subject
        msg["From"] = self.sender
        msg["To"] = recipient
        msg.set_content(rendered.text)
        msg.add_alternative(rendered.html, subtype="html")
        await asyncio.to_thread(self._deliver_sync, msg)
        return msg_id


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def timing_signature(event):
    return tuple(
        event.get(k) for k in ("date", "release_time", "release_tz", "timing_issue", "status")
    )


def urgent_transition(db, identifier):
    """Latest material transition, stable across later evidence-only revisions."""
    rows = db.execute(
        "SELECT * FROM event_revisions WHERE event_id=? ORDER BY seq DESC", (identifier,)
    )
    latest = next(rows, None)
    if latest is None:
        return None
    material = dict(latest)
    current = json.loads(material["doc"])
    for row in rows:
        old = json.loads(row["doc"])
        if (
            material["change"] == "removed"
            or row["change"] == "removed"
            or timing_signature(old) != timing_signature(current)
        ):
            return material | {"doc": current, "previous": old}
        material = dict(row)
    return None


def notices(settings, person: str, at: datetime | None = None) -> list[tuple[str, str, dict]]:
    """Preview is nonmutating. Unknown submissions never generate replacement effects."""
    at = at or datetime.now(timezone.utc)
    pref = profile(settings.db, person)
    zone = ZoneInfo(pref["timezone"])
    local = at.astimezone(zone)
    today = local.date()
    service = open_service(settings, person)
    keys = {r["source_key"] for r in effective_follows(settings.db, person)}
    events = [e for e in service.library.events() if e["issuer_key"] in keys]
    documents = [
        d
        for d in service.library.documents()
        if d["verified"] and d["event_id"] in {e["id"] for e in events}
    ]
    with connect(settings.db) as db:
        metadata = dict(
            db.execute(
                "SELECT key,value FROM metadata WHERE key IN (?,?,?)",
                ("baseline:" + person, "brief-cutoff:" + person, "weekly-brief-cutoff:" + person),
            )
        )
        cutoff = metadata.get("baseline:" + person, pref["created_at"])
        brief_cutoff = metadata.get("brief-cutoff:" + person, cutoff)
        weekly_cutoff = metadata.get("weekly-brief-cutoff:" + person, cutoff)
        cursor = db.execute("SELECT notify_seq FROM people WHERE id=?", (person,)).fetchone()[0]
        deliveries = {
            r["key"]: dict(r)
            for r in db.execute(
                "SELECT key,state,payload,kind FROM deliveries WHERE person=?", (person,)
            )
        }
        all_changes = [
            dict(r) | {"doc": json.loads(r["doc"])}
            for r in db.execute("SELECT * FROM event_revisions WHERE seq>? ORDER BY seq", (cursor,))
        ]
        weekly_revisions = (
            [
                dict(r) | {"doc": json.loads(r["doc"])}
                for r in db.execute(
                    "SELECT * FROM event_revisions WHERE at>? ORDER BY seq", (weekly_cutoff,)
                )
            ]
            if pref["settings"]["weekly_brief"]
            else []
        )
    cutoff_at = _parse_timestamp(cutoff)
    brief_cutoff_at = _parse_timestamp(brief_cutoff)
    weekly_cutoff_at = _parse_timestamp(weekly_cutoff)
    recent_docs = [d for d in documents if _parse_timestamp(d["first_seen"]) > cutoff_at]
    pre_cutoff_released = {
        d["event_id"]
        for d in documents
        if d["role"] == "release" and _parse_timestamp(d["first_seen"]) <= cutoff_at
    }
    result: list[tuple[str, str, dict]] = []
    if pref["settings"]["results"]:
        for event in events:
            if event["id"] in pre_cutoff_released:
                continue
            docs = [d for d in recent_docs if d["event_id"] == event["id"]]
            if not any(d["role"] == "release" for d in docs):
                continue
            key = "results:" + event["id"] + ":initial"
            previous = deliveries.get(key)
            if previous:
                if previous["state"] != "sent":
                    # Pending initial payload can gather documents; uncertain sends remain immutable.
                    if previous["state"] == "pending":
                        result.append(
                            (key, "results", {"event": event, "documents": docs, "day": str(today)})
                        )
                    continue
                initial = json.loads(previous["payload"])
                old_ids = {d["id"] for d in initial["documents"]}
                if initial["day"] != str(today) or not any(d["id"] not in old_ids for d in docs):
                    continue
                key = "results:" + event["id"] + ":followup"
            result.append((key, "results", {"event": event, "documents": docs, "day": str(today)}))
    changes = [r for r in all_changes if r["doc"].get("issuer_key") in keys and r["at"] > cutoff]
    if pref["settings"]["urgent"]:
        # Brief submission must never consume changes still in urgent coalescing.
        with connect(settings.db) as db:
            identifiers = [
                r[0]
                for r in db.execute(
                    "SELECT DISTINCT event_id FROM event_revisions WHERE at>?",
                    (max(cutoff, (at - timedelta(hours=72)).isoformat()),),
                )
            ]
            transitions = [urgent_transition(db, identifier) for identifier in identifiers]
        for change in transitions:
            if (
                change is None
                or change["doc"].get("issuer_key") not in keys
                or change["at"] <= cutoff
                or (at - datetime.fromisoformat(change["at"])).total_seconds() < 600
            ):
                continue
            old, current = change["previous"], change["doc"]
            if old.get("status") != "ir-confirmed" or (
                change["change"] != "removed"
                and (
                    current.get("status") != "ir-confirmed"
                    or timing_signature(old) == timing_signature(current)
                )
            ):
                continue
            instant = datetime.fromisoformat(old["date"]).replace(tzinfo=zone)
            if old.get("release_time") and old.get("release_tz"):
                instant = datetime.fromisoformat(old["date"] + "T" + old["release_time"]).replace(
                    tzinfo=ZoneInfo(old["release_tz"])
                )
            if not at - timedelta(hours=24) <= instant <= at + timedelta(hours=48):
                continue
            result.append(
                (
                    "urgent:" + digest([change["event_id"], change["seq"]]),
                    "urgent",
                    {
                        "event": current,
                        "previous": old,
                        "change": change["change"],
                        "sequence": change["seq"],
                    },
                )
            )
    if (
        pref["settings"]["brief"]
        and local.hour >= pref["settings"]["hour"]
        and not any(r["kind"] == "brief" and r["state"] == "unknown" for r in deliveries.values())
    ):
        upcoming = [e for e in events if str(today) <= e["date"] <= str(today + timedelta(days=1))]
        brief_docs = [d for d in documents if _parse_timestamp(d["first_seen"]) > brief_cutoff_at]
        if upcoming or changes or brief_docs:
            result.append(
                (
                    "brief:" + str(today),
                    "brief",
                    {
                        "day": str(today),
                        "events": upcoming,
                        "changes": changes[-100:],
                        "documents": brief_docs,
                        "sequence": all_changes[-1]["seq"] if all_changes else cursor,
                        "cutoff": at.isoformat(),
                    },
                )
            )
    if (
        pref["settings"]["weekly_brief"]
        and local.hour >= pref["settings"]["hour"]
        and not any(
            r["kind"] == "weekly_brief" and r["state"] == "unknown" for r in deliveries.values()
        )
    ):
        iso = today.isocalendar()
        week_label = f"{iso.year}-W{iso.week:02d}"
        upcoming_week = [
            e for e in events if str(today) <= e["date"] <= str(today + timedelta(days=7))
        ]
        weekly_docs = [d for d in documents if _parse_timestamp(d["first_seen"]) > weekly_cutoff_at]
        weekly_changes = [
            r
            for r in weekly_revisions
            if r["doc"].get("issuer_key") in keys and _parse_timestamp(r["at"]) > weekly_cutoff_at
        ]
        if upcoming_week or weekly_changes or weekly_docs:
            result.append(
                (
                    "weekly_brief:" + week_label,
                    "weekly_brief",
                    {
                        "day": str(today),
                        "week": week_label,
                        "events": upcoming_week,
                        "changes": weekly_changes[-100:],
                        "documents": weekly_docs,
                        "cutoff": at.isoformat(),
                    },
                )
            )
    return [
        entry
        for entry in result
        if entry[0] not in deliveries or deliveries[entry[0]]["state"] == "pending"
    ]


def prepare(settings) -> None:
    for person in people(settings.db):
        identifier = person["id"]
        if legacy_blocked(settings.db, identifier):
            continue
        for key, kind, payload in notices(settings, identifier):
            if kind in {"brief", "weekly_brief"}:
                with connect(settings.db, write=True) as db:
                    db.execute(
                        "UPDATE deliveries SET state='superseded' WHERE person=? AND kind=? AND state='pending' AND key!=?",
                        (identifier, kind, key),
                    )
            enqueue(settings.db, identifier, key, kind, payload)


def event_notices(events, documents, today):
    """Legacy adoption baseline only; never schedules new reminder effects."""
    for e in events:
        docs = [d for d in documents if d["event_id"] == e["id"] and d["verified"]]
        if docs:
            yield "documents:" + digest([e["id"], docs]), "results", {"event": e, "documents": docs}
