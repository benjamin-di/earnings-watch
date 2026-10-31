"""SQLite ownership and transactions. No other package module imports sqlite3."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from earnings_watch.errors import EarningsError
from earnings_watch.model.events import Event
from earnings_watch.model.sources import Source

Row = dict[str, Any]
SCHEMA = 3


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def encode(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(encode(value).encode()).hexdigest()


def event_fingerprint(doc: Row) -> str:
    return digest(
        {k: v for k, v in doc.items() if k not in {"first_seen", "last_verified", "history"}}
    )


@contextmanager
def connect(path: Path, *, write: bool = False) -> Iterator[sqlite3.Connection]:
    uri = path.resolve().as_uri() + ("?mode=rw" if write else "?mode=ro")
    db = sqlite3.connect(uri, uri=True, timeout=5)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA busy_timeout=5000")
    if db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA:
        db.close()
        raise EarningsError("temporarily_unavailable", "unsupported database schema")
    if not write:
        db.execute("PRAGMA query_only=ON")
    try:
        db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield db
        if write:
            db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def initialize(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.exists():
        _upgrade(path)
        with connect(path):
            return
    # Exclusive creation prevents two initializers truncating each other's DB.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript(Path(__file__).with_name("schema.sql").read_text())
        db.execute("INSERT INTO metadata VALUES('database_id',?)", (secrets.token_hex(16),))
        db.commit()
    finally:
        db.close()
    secret = path.parent / "calendar.key"
    try:
        fd = os.open(secret, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(secrets.token_bytes(32))


def _upgrade(path: Path) -> None:
    """Explicit initialization upgrades candidate databases in one transaction."""
    db = sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True, timeout=5)
    try:
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN IMMEDIATE")
        version = db.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            # A download receipt describes a past personal effect. Retiring the
            # current public document must neither erase it nor block retirement.
            db.execute("ALTER TABLE document_downloads RENAME TO document_downloads_v1")
            db.execute(
                "CREATE TABLE document_downloads (person TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE, document_id TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL, at TEXT NOT NULL, PRIMARY KEY(person,document_id))"
            )
            db.execute("INSERT INTO document_downloads SELECT * FROM document_downloads_v1")
            db.execute("DROP TABLE document_downloads_v1")
            db.execute("CREATE INDEX event_revisions_event ON event_revisions(event_id,rev)")
            db.execute("PRAGMA user_version=2")
            version = 2
        if version == 2:
            db.execute(
                "CREATE TABLE profiles (person TEXT PRIMARY KEY REFERENCES people(id) ON DELETE CASCADE, label TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 0, settings TEXT NOT NULL DEFAULT '{}', feed_nonce TEXT NOT NULL DEFAULT '', feed_sha256 TEXT UNIQUE, seen_seq INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE mutes (person TEXT NOT NULL REFERENCES people(id) ON DELETE CASCADE, ticker TEXT NOT NULL, exchange TEXT NOT NULL DEFAULT '', PRIMARY KEY(person,ticker,exchange))"
            )
            db.execute("CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            db.execute("INSERT INTO profiles(person,created_at) SELECT id,? FROM people", (now(),))
            db.execute("INSERT INTO metadata VALUES('database_id',?)", (secrets.token_hex(16),))
            db.execute("PRAGMA user_version=3")
        elif version != SCHEMA:
            raise EarningsError("temporarily_unavailable", "unsupported database schema")
        db.commit()
    except BaseException:
        db.rollback()
        raise
    finally:
        db.close()


def backup(path: Path, destination: Path) -> None:
    if destination.resolve() == path.resolve() or destination.exists():
        raise EarningsError("invalid_argument", "backup destination must be a new file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with connect(path) as db:
        target = sqlite3.connect(destination)
        try:
            db.backup(target)
        finally:
            target.close()


def integrity(path: Path) -> str:
    with connect(path) as db:
        return str(db.execute("PRAGMA integrity_check").fetchone()[0])


def _next_revision(db: sqlite3.Connection, event_id: str, current: int = 0) -> int:
    historical = db.execute(
        "SELECT coalesce(max(rev),0) FROM event_revisions WHERE event_id=?", (event_id,)
    ).fetchone()[0]
    return max(current, historical) + 1


class LibraryReader:
    def __init__(self, path: Path):
        self.path = path

    def sources(self) -> list[Source]:
        with connect(self.path) as db:
            return [
                Source.model_validate_json(row[0])
                for row in db.execute("SELECT doc FROM sources ORDER BY key")
            ]

    def events(self) -> list[Row]:
        with connect(self.path) as db:
            return [
                {**json.loads(row["doc"]), "revision": row["rev"]}
                for row in db.execute("SELECT doc,rev FROM events ORDER BY date,id")
            ]

    def documents(self) -> list[Row]:
        with connect(self.path) as db:
            return [
                json.loads(row[0]) for row in db.execute("SELECT doc FROM documents ORDER BY id")
            ]

    def changes(self, seq: int, limit: int) -> list[Row]:
        with connect(self.path) as db:
            return [
                dict(row) | {"doc": json.loads(row["doc"])}
                for row in db.execute(
                    "SELECT * FROM event_revisions WHERE seq>? ORDER BY seq LIMIT ?", (seq, limit)
                )
            ]

    def status(self) -> Row:
        with connect(self.path) as db:
            run = db.execute("SELECT * FROM runs ORDER BY started DESC LIMIT 1").fetchone()
            return {
                "sources": db.execute("SELECT count(*) FROM sources").fetchone()[0],
                "events": db.execute("SELECT count(*) FROM events").fetchone()[0],
                "documents": db.execute("SELECT count(*) FROM documents").fetchone()[0],
                "sequence": db.execute(
                    "SELECT coalesce(max(seq),0) FROM event_revisions"
                ).fetchone()[0],
                "last_run": dict(run) if run else None,
                "last_sweep": (
                    dict(sweep)
                    if (
                        sweep := db.execute(
                            "SELECT * FROM runs WHERE kind='sweep' ORDER BY started DESC LIMIT 1"
                        ).fetchone()
                    )
                    else None
                ),
            }


class Catalogue(LibraryReader):
    """Maintenance writer. Never passed to serving code."""

    def overrides(self) -> list[Row]:
        with connect(self.path) as db:
            return [
                dict(row) | {"doc": json.loads(row["doc"])}
                for row in db.execute("SELECT * FROM overrides")
            ]

    def put_source(self, source: Source) -> None:
        with connect(self.path, write=True) as db:
            db.execute(
                "INSERT INTO sources(key,ticker,exchange,doc) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET ticker=excluded.ticker,exchange=excluded.exchange,doc=excluded.doc",
                (source.key, source.ticker, source.exchange or "", source.model_dump_json()),
            )
            pending = db.execute(
                "SELECT id,person,origin FROM follows WHERE source_key IS NULL AND (ticker=? OR (? IS NOT NULL AND json_extract(hints,'$.isin')=?)) AND (exchange='' OR exchange=?)",
                (source.ticker, source.isin, source.isin, source.exchange or ""),
            ).fetchall()
            for row in pending:
                duplicate = db.execute(
                    "SELECT id FROM follows WHERE person=? AND ticker=? AND exchange=? AND origin=? AND id!=?",
                    (row["person"], source.ticker, source.exchange or "", row["origin"], row["id"]),
                ).fetchone()
                if duplicate:
                    db.execute("DELETE FROM follows WHERE id=?", (row["id"],))
                else:
                    db.execute(
                        "UPDATE follows SET source_key=?,ticker=?,exchange=?,state='registered' WHERE id=?",
                        (source.key, source.ticker, source.exchange or "", row["id"]),
                    )
                db.execute(
                    "UPDATE profiles SET revision=revision+1 WHERE person=?", (row["person"],)
                )

    def commit_issuer(
        self,
        source_key: str,
        events: list[Event],
        *,
        run_id: str,
        authoritative: bool = False,
        removal_cap: int = 3,
    ) -> None:
        with connect(self.path, write=True) as db:
            old = {
                row["id"]: row
                for row in db.execute("SELECT * FROM events WHERE source_key=?", (source_key,))
            }
            missing = set(old) - {event.id for event in events}
            if authoritative and len(missing) > removal_cap:
                raise EarningsError("temporarily_unavailable", "automatic removal cap exceeded")
            for event in events:
                if (
                    event.issuer_key != source_key
                    or event.id != f"{event.type}:{source_key}:{event.period}"
                ):
                    raise EarningsError(
                        "invalid_argument", "event identity disagrees with its source"
                    )
                doc = event.model_dump(mode="json")
                # Verification freshness must not create a revision or calendar churn.
                fingerprint = event_fingerprint(doc)
                prior = old.get(event.id)
                if prior and prior["fingerprint"] == fingerprint:
                    db.execute("UPDATE events SET doc=? WHERE id=?", (encode(doc), event.id))
                    continue
                rev = _next_revision(db, event.id, prior["rev"] if prior else 0)
                change = (
                    "added"
                    if prior is None
                    else "redated"
                    if prior["date"] != str(event.date)
                    else "updated"
                )
                db.execute(
                    "INSERT INTO events VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET date=excluded.date,rev=excluded.rev,fingerprint=excluded.fingerprint,doc=excluded.doc",
                    (event.id, source_key, str(event.date), rev, fingerprint, encode(doc)),
                )
                db.execute(
                    "INSERT INTO event_revisions(event_id,rev,change,doc,run_id,at) VALUES(?,?,?,?,?,?)",
                    (event.id, rev, change, encode(doc), run_id, now()),
                )
            if authoritative:
                for event_id in missing:
                    row = old[event_id]
                    db.execute(
                        "INSERT INTO event_revisions(event_id,rev,change,doc,run_id,at) VALUES(?,?,?,?,?,?)",
                        (
                            event_id,
                            _next_revision(db, event_id, row["rev"]),
                            "removed",
                            row["doc"],
                            run_id,
                            now(),
                        ),
                    )
                    db.execute("DELETE FROM events WHERE id=?", (event_id,))

    def state(self, table: str, key: str) -> Row:
        if table not in {"fetch_state", "heal_state", "release_watch"}:
            raise ValueError("unsupported state table")
        column = "event_id" if table == "release_watch" else "key"
        with connect(self.path) as db:
            row = db.execute(f"SELECT doc FROM {table} WHERE {column}=?", (key,)).fetchone()
            return json.loads(row[0]) if row else {}

    def set_state(self, table: str, key: str, doc: Row) -> None:
        if table not in {"fetch_state", "heal_state", "release_watch"}:
            raise ValueError("unsupported state table")
        with connect(self.path, write=True) as db:
            db.execute(f"INSERT OR REPLACE INTO {table} VALUES(?,?)", (key, encode(doc)))
            if table == "fetch_state" and key.startswith("cache:"):
                db.execute(
                    "DELETE FROM fetch_state WHERE key IN (SELECT key FROM fetch_state WHERE key LIKE 'cache:%' ORDER BY rowid DESC LIMIT -1 OFFSET 64)"
                )

    def put_cache(self, key: str, doc: Row, max_bytes: int) -> None:
        if not key.startswith("cache:"):
            raise ValueError("cache key must be namespaced")
        with connect(self.path, write=True) as db:
            db.execute("INSERT OR REPLACE INTO fetch_state VALUES(?,?)", (key, encode(doc)))
            rows = list(
                db.execute(
                    "SELECT key,length(CAST(doc AS BLOB)) AS size FROM fetch_state WHERE key LIKE 'cache:%' ORDER BY rowid DESC"
                )
            )
            used = 0
            for index, row in enumerate(rows):
                used += row["size"]
                if used > max_bytes or index >= 64:
                    db.execute("DELETE FROM fetch_state WHERE key=?", (row["key"],))

    def run(self, run_id: str, kind: str, doc: Row, *, exit_code: int | None = None) -> None:
        with connect(self.path, write=True) as db:
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET ended=excluded.ended,exit=excluded.exit,doc=excluded.doc",
                (
                    run_id,
                    kind,
                    now(),
                    now() if exit_code is not None else None,
                    exit_code,
                    encode(doc),
                ),
            )

    def active_keys(self) -> set[str]:
        with connect(self.path) as db:
            return {
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT source_key FROM follows WHERE source_key IS NOT NULL"
                )
            }

    def put_document(self, doc: Row) -> None:
        with connect(self.path, write=True) as db:
            existing = db.execute("SELECT doc FROM documents WHERE id=?", (doc["id"],)).fetchone()
            if existing:
                prior = json.loads(existing[0])
                doc = doc | {
                    "first_seen": prior["first_seen"],
                    "sha256": doc.get("sha256") or prior.get("sha256", ""),
                }
            db.execute(
                "INSERT INTO documents VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET sha256=excluded.sha256,doc=excluded.doc",
                (doc["id"], doc["event_id"], doc["url"], doc.get("sha256", ""), encode(doc)),
            )

    def record_download(self, person: str, doc: Row, path: Path, sha: str) -> None:
        with connect(self.path, write=True) as db:
            row = db.execute("SELECT doc FROM documents WHERE id=?", (doc["id"],)).fetchone()
            if row is None:
                raise EarningsError("not_found", "document is absent from the catalogue")
            updated = json.loads(row[0]) | {"sha256": sha}
            db.execute(
                "UPDATE documents SET sha256=?,doc=? WHERE id=?", (sha, encode(updated), doc["id"])
            )
            db.execute(
                "INSERT INTO document_downloads VALUES(?,?,?,?,?) ON CONFLICT(person,document_id) DO UPDATE SET path=excluded.path,sha256=excluded.sha256,at=excluded.at",
                (person, doc["id"], str(path), sha, now()),
            )

    def record_blob(self, sha: str, path: Path, size: int, max_bytes: int) -> list[Row]:
        with connect(self.path, write=True) as db:
            db.execute(
                "INSERT INTO blobs VALUES(?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET last_used=excluded.last_used",
                (sha, size, str(path), now(), now()),
            )
            removed, used = [], 0
            for row in db.execute("SELECT * FROM blobs ORDER BY last_used DESC,sha256"):
                used += row["size"]
                if used > max_bytes:
                    removed.append(dict(row))
            for row in removed:
                db.execute("DELETE FROM blobs WHERE sha256=?", (row["sha256"],))
            return removed

    def blob_keys(self) -> set[str]:
        with connect(self.path) as db:
            return {row[0] for row in db.execute("SELECT sha256 FROM blobs")}


def add_person(path: Path, person: str, *, token: str | None = None) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", person):
        raise EarningsError("invalid_argument", "invalid person id")
    if token is not None and len(token) < 32:
        raise EarningsError("invalid_argument", "tokens must contain at least 32 characters")
    with connect(path, write=True) as db:
        db.execute("DELETE FROM metadata WHERE key=?", ("blocked:" + person,))
        db.execute(
            "INSERT INTO people(id,display_name,token_sha256) VALUES(?,?,?)",
            (person, person, hashlib.sha256(token.encode()).hexdigest() if token else None),
        )
        db.execute(
            "INSERT INTO profiles(person,settings,created_at) VALUES(?,?,?)",
            (person, '{"results":true,"brief":false,"urgent":false,"hour":8}', now()),
        )


def people(path: Path) -> list[Row]:
    with connect(path) as db:
        return [
            dict(row)
            for row in db.execute("SELECT id,display_name,timezone FROM people ORDER BY id")
        ]


def token_hash(path: Path, person: str) -> str | None:
    with connect(path) as db:
        row = db.execute("SELECT token_sha256 FROM people WHERE id=?", (person,)).fetchone()
        return row[0] if row else None


class PersonalStore:
    def __init__(self, path: Path, person: str):
        self.path, self.person = path, person
        with connect(path) as db:
            if not db.execute("SELECT 1 FROM people WHERE id=?", (person,)).fetchone():
                raise EarningsError("unauthorized", "unknown person")

    def follows(self) -> list[Row]:
        with connect(self.path) as db:
            return [
                dict(row)
                for row in db.execute(
                    "SELECT source_key,ticker,exchange,origin,state,reason,hints FROM follows WHERE person=? ORDER BY ticker,exchange,origin",
                    (self.person,),
                )
            ]

    def watch(
        self,
        ticker: str,
        exchange: str = "",
        *,
        origin: str = "manual",
        isin: str | None = None,
        name: str | None = None,
        revision: int | None = None,
    ) -> Row:
        ticker = ticker.upper().strip()
        if isin and not re.fullmatch(r"[A-Z]{2}[A-Z0-9]{9}[0-9]", isin):
            raise EarningsError("invalid_argument", "invalid ISIN")
        if not re.fullmatch(r"[A-Z0-9]{1,20}", ticker) or (
            exchange and not re.fullmatch(r"[A-Z0-9]{4}", exchange)
        ):
            raise EarningsError("invalid_argument", "invalid ticker or exchange")
        with connect(self.path, write=True) as db:
            check_revision(db, self.person, revision)
            matches = list(
                db.execute(
                    "SELECT key,ticker,exchange FROM sources WHERE (ticker=? OR json_extract(doc,'$.isin')=?) AND (?='' OR exchange=?)",
                    (ticker, isin or ticker, exchange, exchange),
                )
            )
            if len(matches) > 1:
                return {"state": "ambiguous", "candidates": [dict(row) for row in matches]}
            if matches:
                ticker, exchange = matches[0]["ticker"], matches[0]["exchange"]
            existing = db.execute(
                "SELECT state FROM follows WHERE person=? AND ticker=? AND exchange=? AND origin=?",
                (self.person, ticker, exchange, origin),
            ).fetchone()
            if existing:
                return {"state": existing[0]}
            if (
                db.execute(
                    "SELECT count(*) FROM follows WHERE person=?", (self.person,)
                ).fetchone()[0]
                >= 200
            ):
                raise EarningsError("over_cap", "follow limit reached")
            if not matches:
                if (
                    db.execute(
                        "SELECT count(*) FROM follows WHERE person=? AND state='queued'",
                        (self.person,),
                    ).fetchone()[0]
                    >= 5
                ):
                    raise EarningsError("over_cap", "five company requests are already pending")
                quota = db.execute(
                    "SELECT used FROM registration_quotas WHERE person=? AND day=?",
                    (self.person, now()[:10]),
                ).fetchone()
                if quota and quota[0] >= 3:
                    raise EarningsError("over_cap", "daily registration limit reached")
                db.execute(
                    "INSERT INTO registration_quotas VALUES(?,?,1) ON CONFLICT(person,day) DO UPDATE SET used=used+1",
                    (self.person, now()[:10]),
                )
            key = matches[0]["key"] if matches else None
            state = "registered" if key else "queued"
            db.execute(
                "INSERT INTO follows(person,source_key,ticker,exchange,origin,state,hints,added_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    self.person,
                    key,
                    ticker,
                    exchange,
                    origin,
                    state,
                    encode({"isin": isin, "name": name}),
                    now(),
                ),
            )
            db.execute("UPDATE profiles SET revision=revision+1 WHERE person=?", (self.person,))
            return {"state": state, "source_key": key}

    def unwatch(self, ticker: str, exchange: str = "", *, revision: int | None = None) -> Row:
        with connect(self.path, write=True) as db:
            check_revision(db, self.person, revision)
            holding = db.execute(
                "SELECT 1 FROM follows WHERE person=? AND ticker=? AND (?='' OR exchange=?) AND origin LIKE 'holdings:%'",
                (self.person, ticker.upper(), exchange, exchange),
            ).fetchone()
            if holding:
                db.execute(
                    "INSERT OR IGNORE INTO mutes VALUES(?,?,?)",
                    (self.person, ticker.upper(), exchange),
                )
            db.execute(
                "DELETE FROM follows WHERE person=? AND ticker=? AND (?='' OR exchange=?) AND origin='manual'",
                (self.person, ticker.upper(), exchange, exchange),
            )
            db.execute("UPDATE profiles SET revision=revision+1 WHERE person=?", (self.person,))
        return {"state": "muted" if holding else "removed"}

    def ledger(self, sink: str) -> tuple[int, list[Row]]:
        with connect(self.path) as db:
            row = db.execute(
                "SELECT rev FROM calendar_versions WHERE person=? AND sink=?", (self.person, sink)
            ).fetchone()
            entries = [
                dict(r)
                for r in db.execute(
                    "SELECT key,fingerprint,provider_id FROM calendar_ledger WHERE person=? AND sink=? ORDER BY key",
                    (self.person, sink),
                )
            ]
            return row[0] if row else 0, entries

    def calendar_snapshot(self, sink: str, event_ids: list[str] | None = None) -> Row:
        with connect(self.path) as db:
            follows = [
                dict(row)
                for row in db.execute(
                    "SELECT source_key,ticker,exchange,origin,state FROM follows WHERE person=? ORDER BY ticker,exchange,origin",
                    (self.person,),
                )
            ]
            selected = set(event_ids or []) | {
                r[0].removeprefix("earnings-watch:")
                for r in db.execute(
                    "SELECT key FROM calendar_ledger WHERE person=? AND sink=?", (self.person, sink)
                )
            }
            events = [
                json.loads(row["doc"]) | {"revision": row["rev"]}
                for row in db.execute("SELECT doc,rev,source_key FROM events ORDER BY date,id")
                if json.loads(row["doc"])["id"] in selected
            ]
            version = db.execute(
                "SELECT rev FROM calendar_versions WHERE person=? AND sink=?", (self.person, sink)
            ).fetchone()
            ledger = [
                dict(row)
                for row in db.execute(
                    "SELECT key,fingerprint,provider_id FROM calendar_ledger WHERE person=? AND sink=? ORDER BY key",
                    (self.person, sink),
                )
            ]
            return {
                "events": events,
                "follows": digest(follows),
                "ledger": ledger,
                "ledger_rev": version[0] if version else 0,
                "sequence": db.execute(
                    "SELECT coalesce(max(seq),0) FROM event_revisions"
                ).fetchone()[0],
            }

    def acknowledge(self, plan: Row, results: list[Row], handle_hash: str) -> Row:
        with connect(self.path, write=True) as db:
            existing = db.execute(
                "SELECT doc FROM calendar_acks WHERE person=? AND handle_sha256=?",
                (self.person, handle_hash),
            ).fetchone()
            if existing:
                return json.loads(existing[0])
            row = db.execute(
                "SELECT rev FROM calendar_versions WHERE person=? AND sink=?",
                (self.person, plan["sink"]),
            ).fetchone()
            seq = db.execute("SELECT coalesce(max(seq),0) FROM event_revisions").fetchone()[0]
            follows = [
                dict(row)
                for row in db.execute(
                    "SELECT source_key,ticker,exchange,origin,state FROM follows WHERE person=? ORDER BY ticker,exchange,origin",
                    (self.person,),
                )
            ]
            if (
                (row[0] if row else 0) != plan["ledger_rev"]
                or seq != plan["sequence"]
                or digest(follows) != plan["follows"]
            ):
                raise EarningsError(
                    "plan_stale", "calendar or catalogue changed; request a new plan"
                )
            ops = {op["key"]: op for op in plan["operations"]}
            applied, rejected, seen = [], [], set()
            for result in results:
                key = result.get("key")
                if key not in ops or key in seen:
                    raise EarningsError("invalid_argument", "unknown or duplicate operation key")
                seen.add(key)
                op = ops[key]
                if result.get("status") != "applied":
                    rejected.append(key)
                    continue
                if op["action"] == "delete":
                    db.execute(
                        "DELETE FROM calendar_ledger WHERE person=? AND sink=? AND key=?",
                        (self.person, plan["sink"], key),
                    )
                else:
                    if not result.get("provider_id"):
                        raise EarningsError(
                            "invalid_argument", "applied writes require a provider_id"
                        )
                    db.execute(
                        "INSERT OR REPLACE INTO calendar_ledger VALUES(?,?,?,?,?,?)",
                        (
                            self.person,
                            plan["sink"],
                            key,
                            op["fingerprint"],
                            result["provider_id"],
                            now(),
                        ),
                    )
                applied.append(key)
            db.execute(
                "INSERT INTO calendar_versions VALUES(?,?,1) ON CONFLICT(person,sink) DO UPDATE SET rev=rev+1",
                (self.person, plan["sink"]),
            )
            response = {"applied": applied, "rejected": rejected}
            db.execute(
                "INSERT INTO calendar_acks VALUES(?,?,?)",
                (self.person, handle_hash, encode(response)),
            )
            return response


def restore(path: Path, source: Path) -> None:
    if integrity(source) != "ok":
        raise EarningsError("invalid_argument", "restore source failed integrity check")
    with connect(source) as origin:
        # Use the backup API so existing readers and WAL handles stay valid.
        destination = sqlite3.connect(path, timeout=5)
        try:
            origin.backup(destination)
            destination.execute(
                "UPDATE metadata SET value=? WHERE key='database_id'", (secrets.token_hex(16),)
            )
            destination.commit()
        finally:
            destination.close()


def sync_holdings(path: Path, person: str, feed: str, entries: list[Row]) -> Row:
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", feed) or len(entries) > 200:
        raise EarningsError("invalid_argument", "invalid feed or holdings budget")
    normalized = {(str(r["ticker"]).upper().strip(), r.get("exchange") or "") for r in entries}
    if any(
        not re.fullmatch(r"[A-Z0-9]{1,20}", ticker)
        or (exchange and not re.fullmatch(r"[A-Z0-9]{4}", exchange))
        for ticker, exchange in normalized
    ):
        raise EarningsError("invalid_argument", "invalid holdings identity")
    origin = "holdings:" + feed
    with connect(path, write=True) as db:
        if not db.execute("SELECT 1 FROM people WHERE id=?", (person,)).fetchone():
            raise EarningsError("not_found", "person not found")
        others = db.execute(
            "SELECT count(*) FROM follows WHERE person=? AND origin!=?", (person, origin)
        ).fetchone()[0]
        if others + len(normalized) > 200:
            raise EarningsError("over_cap", "follow limit reached")
        prepared = []
        for ticker, exchange in sorted(normalized):
            matches = list(
                db.execute(
                    "SELECT key FROM sources WHERE ticker=? AND (?='' OR exchange=?)",
                    (ticker, exchange, exchange),
                )
            )
            if len(matches) > 1:
                raise EarningsError("ambiguous", "holdings ticker requires an exchange")
            if not matches:
                raise EarningsError("not_found", "import sources before synchronizing holdings")
            prepared.append((person, matches[0][0], ticker, exchange, origin, "registered", now()))
        db.execute("DELETE FROM follows WHERE person=? AND origin=?", (person, origin))
        db.executemany(
            "INSERT INTO follows(person,source_key,ticker,exchange,origin,state,added_at) VALUES(?,?,?,?,?,?,?)",
            prepared,
        )
        db.execute("UPDATE profiles SET revision=revision+1 WHERE person=?", (person,))
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES(?,?)",
            ("holdings:" + person + ":" + feed, now()),
        )
    return {"feed": feed, "follows": len(prepared)}


def legacy_blocked(path: Path, person: str) -> bool:
    """Unreviewed legacy effects block every new external-effect projection."""
    with connect(path) as db:
        return bool(
            db.execute(
                "SELECT 1 FROM deliveries WHERE person=? AND key='legacy-state' AND state='unknown'",
                (person,),
            ).fetchone()
        )


def check_revision(db, person: str, revision: int | None) -> None:
    if revision is not None:
        row = db.execute("SELECT revision FROM profiles WHERE person=?", (person,)).fetchone()
        if row is None or row[0] != revision:
            raise EarningsError("conflict", "This page changed. Refresh before saving.")


def database_id(path: Path) -> str:
    with connect(path) as db:
        return db.execute("SELECT value FROM metadata WHERE key='database_id'").fetchone()[0]
