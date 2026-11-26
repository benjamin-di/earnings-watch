"""Subject-owned preferences, interests and subscription secrets in the native store."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..errors import EarningsError
from ..symbols import is_valid_exchange, is_valid_ticker
from .database import connect, encode, now, check_revision

DEFAULTS: dict[str, Any] = {
    "results": True,
    "brief": False,
    "weekly_brief": False,
    "urgent": False,
    "hour": 8,
    "news_digest": False,
    "news_cadence": "twice_daily",
    "news_weekdays_only": False,
    "news_pause_until": "",
    "news_scope": "both",
    "news_mutes": [],
}

_NEWS_CADENCES = frozenset({"twice_daily", "slot_a", "slot_b"})
_NEWS_SCOPES = frozenset({"both", "holdings", "watchlist"})
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_NEWS_MUTES = 200


def _validate_news_mute_entry(item: Any) -> str:
    if type(item) is not str or ":" not in item:
        raise EarningsError("invalid_argument", "invalid news mute entry")
    ticker, exchange = item.split(":", 1)
    if not is_valid_ticker(ticker) or (exchange != "" and not is_valid_exchange(exchange)):
        raise EarningsError("invalid_argument", "invalid news mute entry")
    return f"{ticker}:{exchange}"


def _validate_preferences(values: dict[str, Any]) -> dict[str, Any]:
    if not set(values) <= set(DEFAULTS) or not {"results", "brief", "urgent", "hour"} <= set(
        values
    ):
        raise EarningsError("invalid_argument", "invalid notification preferences")
    merged = {**DEFAULTS, **values}
    for key in ("results", "brief", "weekly_brief", "urgent", "news_digest", "news_weekdays_only"):
        if type(merged[key]) is not bool:
            raise EarningsError("invalid_argument", "invalid notification preferences")
    if type(merged["hour"]) is not int or not 0 <= merged["hour"] <= 23:
        raise EarningsError("invalid_argument", "invalid notification preferences")
    if merged["news_cadence"] not in _NEWS_CADENCES:
        raise EarningsError("invalid_argument", "invalid news digest cadence")
    if merged["news_scope"] not in _NEWS_SCOPES:
        raise EarningsError("invalid_argument", "invalid news digest scope")
    pause_until = merged["news_pause_until"]
    if type(pause_until) is not str:
        raise EarningsError("invalid_argument", "invalid news digest pause date")
    if pause_until != "":
        if not _ISO_DATE_RE.fullmatch(pause_until):
            raise EarningsError("invalid_argument", "invalid news digest pause date")
        try:
            date.fromisoformat(pause_until)
        except ValueError:
            raise EarningsError("invalid_argument", "invalid news digest pause date") from None
    raw_mutes = merged["news_mutes"]
    if type(raw_mutes) is not list or len(raw_mutes) > MAX_NEWS_MUTES:
        raise EarningsError("invalid_argument", "invalid news mutes")
    cleaned_mutes = sorted({_validate_news_mute_entry(item) for item in raw_mutes})
    return merged | {"news_mutes": cleaned_mutes}


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
        merged = {**DEFAULTS, **json.loads(data.get("settings") or "{}")}
        merged["news_mutes"] = list(merged.get("news_mutes") or [])
        data["settings"] = merged
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
    try:
        ZoneInfo(tz)
    except (ValueError, ZoneInfoNotFoundError):
        raise EarningsError(
            "invalid_argument", "Choose a time zone such as Europe/Berlin."
        ) from None
    cleaned = _validate_preferences(values)
    with connect(path, write=True) as db:
        changed = db.execute(
            "UPDATE profiles SET settings=?,revision=revision+1 WHERE person=? AND revision=?",
            (encode(cleaned), person, revision),
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
        raw_events = payload.get("events", {})
        legacy_events = raw_events.get("events", []) if isinstance(raw_events, dict) else raw_events
        source_keys = {r["ticker"]: r["key"] for r in db.execute("SELECT ticker,key FROM sources")}
        for legacy_event in legacy_events if isinstance(legacy_events, list) else []:
            if not isinstance(legacy_event, dict) or not legacy_event.get("results_emailed_at"):
                continue
            event_type = legacy_event.get("type")
            period = legacy_event.get("period")
            issuer_key = legacy_event.get("issuer_key") or source_keys.get(
                legacy_event.get("ticker", "")
            )
            if (
                isinstance(event_type, str)
                and isinstance(issuer_key, str)
                and isinstance(period, str)
                and event_type
                and issuer_key
                and period
            ):
                db.execute(
                    "INSERT OR IGNORE INTO deliveries(person,key,kind,state,payload) VALUES(?,?,'results','suppressed_by_migration','{}')",
                    (person, f"results:{event_type}:{issuer_key}:{period}:initial"),
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
    from .database import MAX_DAILY_REGISTRATIONS, MAX_PENDING_REGISTRATIONS

    with connect(settings.db, write=True) as db:
        changed = db.execute(
            "UPDATE profiles SET revision=revision+1 WHERE person=? AND revision=?",
            (person, value["revision"]),
        )
        if not changed.rowcount:
            raise EarningsError("conflict", "Import preview is stale. Preview again.")
        ts = now()
        prepared_map: dict[tuple[str, str], tuple[str, str, str, str, str]] = {}
        for row in value["items"]:
            source = db.execute(
                "SELECT key FROM sources WHERE ticker=? AND exchange=?",
                (row["ticker"], row["exchange"]),
            ).fetchone()
            if not source:
                raise EarningsError("not_found", "Company catalogue changed. Preview again.")
            prepared_map[(row["ticker"], row["exchange"])] = (
                person,
                source[0],
                row["ticker"],
                row["exchange"],
                ts,
            )
        queued = []
        for row in value.get("unlisted") or []:
            row_isin = row.get("isin")
            matches = db.execute(
                "SELECT key,ticker,exchange FROM sources "
                "WHERE (ticker=? OR (? IS NOT NULL AND json_extract(doc,'$.isin')=?)) "
                "AND (?='' OR exchange=?)",
                (row["ticker"], row_isin, row_isin, row["exchange"], row["exchange"]),
            ).fetchall()
            if len(matches) > 1:
                raise EarningsError(
                    "ambiguous", "Company catalogue changed with multiple exchanges. Preview again."
                )
            if matches:
                m = matches[0]
                prepared_map[(m["ticker"], m["exchange"])] = (
                    person,
                    m["key"],
                    m["ticker"],
                    m["exchange"],
                    ts,
                )
            else:
                queued.append(
                    (
                        person,
                        row["ticker"],
                        row["exchange"],
                        encode({"isin": row_isin, "name": None}),
                        ts,
                    )
                )
        prepared = list(prepared_map.values())
        others = db.execute(
            "SELECT count(*) FROM follows WHERE person=? AND origin!='holdings:upload'", (person,)
        ).fetchone()[0]
        if len(prepared) + len(queued) + others > 200:
            raise EarningsError("over_cap", "Follow limit reached.")
        if queued:
            other_pending = db.execute(
                "SELECT count(*) FROM follows WHERE person=? AND origin!='holdings:upload' AND state='queued'",
                (person,),
            ).fetchone()[0]
            if len(queued) + other_pending > MAX_PENDING_REGISTRATIONS:
                raise EarningsError(
                    "over_cap",
                    f"At most {MAX_PENDING_REGISTRATIONS} company requests can be pending at once.",
                )
            existing_queued_upload = {
                (r[0], r[1])
                for r in db.execute(
                    "SELECT ticker,exchange FROM follows WHERE person=? AND origin='holdings:upload' AND state='queued'",
                    (person,),
                )
            }
            new_count = sum(1 for row in queued if (row[1], row[2]) not in existing_queued_upload)
            if new_count:
                day = ts[:10]
                quota = db.execute(
                    "SELECT used FROM registration_quotas WHERE person=? AND day=?",
                    (person, day),
                ).fetchone()
                used = quota[0] if quota else 0
                if used + new_count > MAX_DAILY_REGISTRATIONS:
                    raise EarningsError("over_cap", "Daily registration limit reached.")
                db.execute(
                    "INSERT INTO registration_quotas VALUES(?,?,?) ON CONFLICT(person,day) DO UPDATE SET used=used+excluded.used",
                    (person, day, new_count),
                )
        db.execute("DELETE FROM follows WHERE person=? AND origin='holdings:upload'", (person,))
        db.executemany(
            "INSERT OR IGNORE INTO follows(person,source_key,ticker,exchange,origin,state,added_at) VALUES(?,?,?,?,'holdings:upload','registered',?)",
            prepared,
        )
        db.executemany(
            "INSERT OR IGNORE INTO follows(person,source_key,ticker,exchange,origin,state,hints,added_at) VALUES(?,NULL,?,?,'holdings:upload','queued',?,?)",
            queued,
        )
    return {"follows": len(prepared), "queued": len(queued)}


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


def remembered_email(path: Path, person: str) -> str:
    with connect(path) as db:
        row = db.execute("SELECT value FROM metadata WHERE key=?", ("email:" + person,)).fetchone()
        return str(row[0]) if row else ""


def recent_deliveries(path: Path, person: str, *, limit: int = 20) -> list[dict[str, Any]]:
    bounded = max(1, min(limit, 50))
    with connect(path) as db:
        return [
            dict(r)
            for r in db.execute(
                "SELECT key,kind,state,sent_at,attempts FROM deliveries "
                "WHERE person=? AND key!='legacy-state' AND state!='suppressed_by_migration' "
                "ORDER BY (sent_at IS NULL AND state IN ('pending','unknown')) DESC, "
                "COALESCE(sent_at,'') DESC, rowid DESC LIMIT ?",
                (person, bounded),
            )
        ]


def news_mute(
    path: Path,
    person: str,
    ticker: str,
    exchange: str,
    enabled: bool,
    *,
    revision: int | None = None,
) -> dict[str, Any]:
    norm_ticker = ticker.strip().upper()
    norm_exchange = exchange.strip().upper()
    entry = _validate_news_mute_entry(f"{norm_ticker}:{norm_exchange}")
    with connect(path, write=True) as db:
        check_revision(db, person, revision)
        row = db.execute("SELECT settings FROM profiles WHERE person=?", (person,)).fetchone()
        if row is None:
            raise EarningsError("unauthorized", "unknown person")
        merged = {**DEFAULTS, **json.loads(row[0] or "{}")}
        current_mutes = set(merged.get("news_mutes") or [])
        if enabled:
            if entry not in current_mutes and len(current_mutes) >= MAX_NEWS_MUTES:
                raise EarningsError("over_cap", "News mute limit reached.")
            current_mutes.add(entry)
        else:
            current_mutes.discard(entry)
        merged["news_mutes"] = sorted(current_mutes)
        cleaned = _validate_preferences(merged)
        db.execute(
            "UPDATE profiles SET settings=?,revision=revision+1 WHERE person=?",
            (encode(cleaned), person),
        )
    return profile(path, person)
