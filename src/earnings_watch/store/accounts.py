"""Subject-owned preferences, interests and subscription secrets in the native store."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from ..errors import EarningsError
from .database import connect, encode, now, check_revision

DEFAULTS = {"results": True, "brief": False, "urgent": False, "hour": 8}


def provision(path: Path, subject: str, label: str = "") -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,199}", subject):
        raise EarningsError("unauthorized", "invalid identity subject")
    with connect(path, write=True) as db:
        if db.execute("SELECT 1 FROM metadata WHERE key=?", ("blocked:" + subject,)).fetchone():
            raise EarningsError("unauthorized", "account has been removed")
        db.execute(
            "INSERT OR IGNORE INTO people(id,display_name) VALUES(?,?)",
            (subject, label[:100] or subject),
        )
        db.execute(
            "INSERT OR IGNORE INTO profiles(person,label,settings,created_at) VALUES(?,?,?,?)",
            (subject, label[:100], encode(DEFAULTS), now()),
        )
    return subject


def profile(path: Path, person: str) -> dict:
    with connect(path) as db:
        row = db.execute(
            "SELECT p.timezone,a.* FROM people p LEFT JOIN profiles a ON a.person=p.id WHERE p.id=?",
            (person,),
        ).fetchone()
        if row is None:
            raise EarningsError("unauthorized", "unknown person")
        data = dict(row)
        data["settings"] = DEFAULTS | json.loads(data.get("settings") or "{}")
        data["revision"] = data.get("revision") or 0
        prefix = "holdings:" + person + ":"
        data["holdings_sync"] = {
            r[0][len(prefix) :]: r[1]
            for r in db.execute(
                "SELECT key,value FROM metadata WHERE substr(key,1,?)=?",
                (len(prefix), prefix),
            )
        }
        return data


def preferences(path: Path, person: str, revision: int, values: dict, tz: str) -> dict:
    ZoneInfo(tz)
    if (
        set(values) != set(DEFAULTS)
        or any(type(values[k]) is not bool for k in ("results", "brief", "urgent"))
        or type(values["hour"]) is not int
        or not 0 <= values["hour"] <= 23
    ):
        raise EarningsError("invalid_argument", "invalid notification preferences")
    with connect(path, write=True) as db:
        changed = db.execute(
            "UPDATE profiles SET settings=?,revision=revision+1 WHERE person=? AND revision=?",
            (encode(values), person, revision),
        )
        if not changed.rowcount:
            raise EarningsError("conflict", "This page changed. Refresh before saving again.")
        db.execute("UPDATE people SET timezone=? WHERE id=?", (tz, person))
    return profile(path, person)


def effective_follows(path: Path, person: str) -> list[dict]:
    with connect(path) as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT f.source_key,f.ticker,f.exchange,f.origin,f.state FROM follows f WHERE f.person=? AND NOT EXISTS (SELECT 1 FROM mutes m WHERE m.person=f.person AND m.ticker=f.ticker AND (m.exchange='' OR m.exchange=f.exchange)) ORDER BY f.ticker,f.exchange,f.origin",
                (person,),
            )
        ]


def mutes(path: Path, person: str) -> set[tuple[str, str]]:
    with connect(path) as db:
        return {
            (r[0], r[1])
            for r in db.execute("SELECT ticker,exchange FROM mutes WHERE person=?", (person,))
        }


def mute(
    path: Path,
    person: str,
    ticker: str,
    exchange: str,
    enabled: bool,
    *,
    revision: int | None = None,
) -> None:
    with connect(path, write=True) as db:
        check_revision(db, person, revision)
        if enabled:
            db.execute("INSERT OR IGNORE INTO mutes VALUES(?,?,?)", (person, ticker, exchange))
        else:
            db.execute(
                "DELETE FROM mutes WHERE person=? AND ticker=? AND exchange=?",
                (person, ticker, exchange),
            )
        db.execute("UPDATE profiles SET revision=revision+1 WHERE person=?", (person,))


def feed_token(
    path: Path, person: str, key: bytes, *, rotate: bool = False, revision: int | None = None
) -> str:
    with connect(path, write=True) as db:
        check_revision(db, person, revision)
        row = db.execute("SELECT feed_nonce FROM profiles WHERE person=?", (person,)).fetchone()
        if row is None:
            raise EarningsError("unauthorized", "unknown person")
        nonce = secrets.token_hex(32) if rotate or not row[0] else row[0]
        token = hmac.new(key, (person + ":" + nonce).encode(), hashlib.sha256).hexdigest()
        db.execute(
            "UPDATE profiles SET feed_nonce=?,feed_sha256=? WHERE person=?",
            (nonce, hashlib.sha256(token.encode()).hexdigest(), person),
        )
        if rotate:
            db.execute("UPDATE profiles SET revision=revision+1 WHERE person=?", (person,))
        return token


def feed_person(path: Path, token: str) -> str | None:
    if not re.fullmatch(r"[a-f0-9]{64}", token):
        return None
    with connect(path) as db:
        row = db.execute(
            "SELECT person FROM profiles WHERE feed_sha256=?",
            (hashlib.sha256(token.encode()).hexdigest(),),
        ).fetchone()
        return row[0] if row else None


def mark_seen(path: Path, person: str, sequence: int) -> None:
    with connect(path, write=True) as db:
        db.execute(
            "UPDATE profiles SET seen_seq=max(seen_seq,?) WHERE person=?", (sequence, person)
        )


def account_people(path: Path) -> list[dict]:
    with connect(path) as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT p.id,p.timezone,a.settings FROM people p JOIN profiles a ON a.person=p.id ORDER BY p.id"
            )
        ]


def archive_effects(path: Path, person: str, receipt: dict) -> dict:
    if (
        not isinstance(receipt, dict)
        or not isinstance(receipt.get("evidence"), str)
        or not receipt["evidence"].strip()
        or receipt.get("legacy_writer_stopped") is not True
    ):
        raise EarningsError("invalid_argument", "stopped-writer evidence is required")
    with connect(path, write=True) as db:
        row = db.execute(
            "SELECT state,payload FROM deliveries WHERE person=? AND key='legacy-state'", (person,)
        ).fetchone()
        if row is None or row[0] not in {"unknown", "archived"}:
            raise EarningsError("invalid_argument", "no archivable legacy migration")
        payload = json.loads(row[1])
        if receipt.get("snapshot_sha256") != payload.get("snapshot_sha256"):
            raise EarningsError("invalid_argument", "migration snapshot differs")
        if row[0] == "archived":
            return {"archived": True}
        payload["archive_receipt"] = receipt | {"at": now()}
        db.execute(
            "UPDATE deliveries SET state='archived',payload=? WHERE person=? AND key='legacy-state'",
            (encode(payload), person),
        )
        db.execute(
            "UPDATE deliveries SET state='suppressed_by_migration' WHERE person=? AND key!='legacy-state' AND state='pending'",
            (person,),
        )
        db.execute(
            "UPDATE people SET notify_seq=(SELECT coalesce(max(seq),0) FROM event_revisions) WHERE id=?",
            (person,),
        )
        # Archived history is a cutoff, not a claimed external delivery.
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES(?,?)",
            ("baseline:" + person, datetime.now(timezone.utc).isoformat()),
        )
    return {"archived": True}


def replace_preview(settings, person: str, value: dict) -> dict:
    with connect(settings.db, write=True) as db:
        changed = db.execute(
            "UPDATE profiles SET revision=revision+1 WHERE person=? AND revision=?",
            (person, value["revision"]),
        )
        if not changed.rowcount:
            raise EarningsError("conflict", "Import preview is stale. Preview again.")
        prepared = []
        for row in value["items"]:
            source = db.execute(
                "SELECT key FROM sources WHERE ticker=? AND exchange=?",
                (row["ticker"], row["exchange"]),
            ).fetchone()
            if not source:
                raise EarningsError("not_found", "Company catalogue changed. Preview again.")
            prepared.append((person, source[0], row["ticker"], row["exchange"], now()))
        others = db.execute(
            "SELECT count(*) FROM follows WHERE person=? AND origin!='holdings:upload'", (person,)
        ).fetchone()[0]
        if len(prepared) + others > 200:
            raise EarningsError("over_cap", "Follow limit reached.")
        db.execute("DELETE FROM follows WHERE person=? AND origin='holdings:upload'", (person,))
        db.executemany(
            "INSERT INTO follows(person,source_key,ticker,exchange,origin,state,added_at) VALUES(?,?,?,?,'holdings:upload','registered',?)",
            prepared,
        )
    return {"follows": len(prepared)}


def changed_ids(path: Path, person: str, after: int, through: int) -> set[str]:
    with connect(path) as db:
        return {
            r[0]
            for r in db.execute(
                "SELECT DISTINCT event_id FROM event_revisions WHERE seq>? AND seq<=? AND json_extract(doc,'$.issuer_key') IN (SELECT source_key FROM follows WHERE person=?)",
                (after, through, person),
            )
        }


def remember_email(path: Path, person: str, email: str) -> None:
    if not re.fullmatch(r"[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+", email):
        return
    with connect(path, write=True) as db:
        db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", ("email:" + person, email))
