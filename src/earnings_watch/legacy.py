"""Atomic import of a stopped snapshot, retaining original rollback projections."""

import hashlib
import json
from pathlib import Path
import yaml
from .errors import EarningsError
from .model.events import Event, ResultsLink, TimingSelection
from .model.sources import Source
from .store.database import connect, digest, encode, now, event_fingerprint


def import_snapshot(path: Path, directory: Path, person: str, sink: str = "legacy") -> dict:
    source_text = (directory / "ir-sources.yaml").read_text()
    raw_sources = yaml.safe_load(source_text)
    raw_events = yaml.safe_load((directory / "events.yaml").read_text())
    marker = directory / "earnings-watch-mail.json"
    raw_marker = json.loads(marker.read_text()) if marker.is_file() else {}
    source_rows = raw_sources.get("sources", []) if isinstance(raw_sources, dict) else raw_sources
    event_rows = raw_events.get("events", []) if isinstance(raw_events, dict) else raw_events
    override_path = directory / "events-overrides.yaml"
    overrides = yaml.safe_load(override_path.read_text()) or {} if override_path.is_file() else {}
    sources, events, by_ticker = [], [], {}
    for row in source_rows:
        # Registry comments, membership, wiki paths and arbitrary extensions
        # are personal migration evidence, never shared catalogue facts.
        clean = {
            k: v
            for k, v in row.items()
            if k in Source.model_fields and k not in {"notes", "extensions"}
        }
        source = Source.model_validate(clean)
        if source.ticker in by_ticker:
            raise EarningsError("ambiguous", "legacy ticker identities must be unique")
        sources.append(source)
        by_ticker[source.ticker] = source.key
    for row in event_rows:
        clean = {k: v for k, v in row.items() if k in Event.model_fields}
        key = clean.get("issuer_key") or by_ticker[clean["ticker"]]
        clean["issuer_key"] = key
        clean["id"] = f"{clean['type']}:{key}:{clean['period']}"
        events.append((row, Event.model_validate(clean)))
        for link in row.get("results_links", []):
            ResultsLink.model_validate(link)
    # Every write, including personal ledgers and retained original markers,
    # shares one transaction. Any constraint failure leaves the target untouched.
    with connect(path, write=True) as db:
        if (
            db.execute("SELECT count(*) FROM sources").fetchone()[0]
            or db.execute("SELECT count(*) FROM events").fetchone()[0]
        ):
            raise EarningsError("invalid_argument", "legacy import requires an empty database")
        if not db.execute("SELECT 1 FROM people WHERE id=?", (person,)).fetchone():
            raise EarningsError("not_found", "migration person not found")
        for source, raw in zip(sources, source_rows):
            db.execute(
                "INSERT INTO sources(key,ticker,exchange,doc) VALUES(?,?,?,?)",
                (source.key, source.ticker, source.exchange or "", source.model_dump_json()),
            )
            db.execute(
                "INSERT OR IGNORE INTO follows(person,source_key,ticker,exchange,origin,state,added_at) VALUES(?,?,?,?,?,?,?)",
                (
                    person,
                    source.key,
                    source.ticker,
                    source.exchange or "",
                    "manual" if raw.get("membership") == "always" else "holdings:legacy",
                    "registered",
                    now(),
                ),
            )
        for raw, event in events:
            doc = event.model_dump(mode="json")
            db.execute(
                "INSERT INTO events VALUES(?,?,?,?,?,?)",
                (
                    event.id,
                    event.issuer_key,
                    str(event.date),
                    1,
                    event_fingerprint(doc),
                    encode(doc),
                ),
            )
            db.execute(
                "INSERT INTO event_revisions(event_id,rev,change,doc,run_id,at) VALUES(?,1,'added',?,'legacy-import',?)",
                (event.id, encode(doc), now()),
            )
            polling = {
                k: v
                for k, v in raw.items()
                if k.startswith(("results_", "release_"))
                and k not in {"release_time", "release_tz", "results_links", "results_emailed_at"}
            }
            polling.update(
                attempts=raw.get("results_attempts", 0),
                last_attempt=raw.get("results_last_attempt"),
                event_date=str(event.date),
                seen=raw.get("release_baseline") or [],
                released_at=raw.get("release_at"),
            )
            db.execute("INSERT INTO release_watch VALUES(?,?)", (event.id, encode(polling)))
            for link in raw.get("results_links", []):
                document = dict(
                    {k: v for k, v in link.items() if k != "attachment_path"},
                    id=digest([event.id, link["url"]]),
                    event_id=event.id,
                    role={
                        "press-release": "release",
                        "financial-statements": "statements",
                        "presentation": "presentation",
                    }.get(link.get("kind"), "other"),
                    verified=bool(link.get("verified_at")) and not bool(link.get("unverified")),
                    unverified_reason=link.get("unverified")
                    or (None if link.get("verified_at") else "legacy evidence needs verification"),
                    sha256=link.get("content_sha256") or "",
                    first_seen=link.get("verified_at") or now(),
                )
                db.execute(
                    "INSERT INTO documents VALUES(?,?,?,?,?)",
                    (
                        document["id"],
                        event.id,
                        link["url"],
                        document["sha256"],
                        encode(document),
                    ),
                )

        # Remote ids and old fingerprints are evidence, not confirmed new
        # ledger entries. Keep them in the rollback snapshot until reconciled.
        def identity(value):
            kind, ticker, period = value.split(":")
            return f"{kind}:{by_ticker.get(ticker, ticker)}:{period}"

        for identifier in overrides.get("confirm_removals", []):
            db.execute("INSERT INTO overrides VALUES(?, 'remove', '{}')", (identity(identifier),))
        for identifier, selection in overrides.get("timing_resolutions", {}).items():
            payload = TimingSelection.model_validate(selection).model_dump(mode="json")
            db.execute(
                "INSERT INTO overrides VALUES(?, 'timing_selection', ?)",
                (identity(identifier), encode(payload)),
            )
        original = {
            "events": raw_events,
            "sources": raw_sources,
            "sources_text": source_text,
            "mail": raw_marker,
            "sink": sink,
            "overrides": overrides,
        }
        manifest = directory / "manifest.json"
        original["snapshot_sha256"] = (
            hashlib.sha256(manifest.read_bytes()).hexdigest()
            if manifest.is_file()
            else digest(original)
        )
        db.execute(
            "INSERT INTO deliveries(person,key,kind,state,payload) VALUES(?, 'legacy-state','legacy','unknown',?)",
            (person, encode(original)),
        )
        db.execute(
            "UPDATE people SET notify_seq=(SELECT coalesce(max(seq),0) FROM event_revisions) WHERE id=?",
            (person,),
        )
    return {
        "sources": len(sources),
        "events": len(events),
        "requires_delivery_reconciliation": True,
    }


def review(path: Path, person: str) -> dict:
    """Read-only evidence for external review while effect planning is fenced."""
    from .calendar import desired

    with connect(path) as db:
        stored = db.execute(
            "SELECT payload,state FROM deliveries WHERE person=? AND key='legacy-state'", (person,)
        ).fetchone()
        if not stored:
            raise EarningsError("not_found", "no legacy migration receipt")
        original = json.loads(stored["payload"])
        events = [
            json.loads(r["doc"]) | {"revision": r["rev"]}
            for r in db.execute(
                "SELECT doc,rev FROM events WHERE source_key IN (SELECT source_key FROM follows WHERE person=?) ORDER BY date,id",
                (person,),
            )
        ]
        raw = original.get("events", {})
        rows = raw.get("events", []) if isinstance(raw, dict) else raw
        return {
            "state": stored["state"],
            "snapshot_sha256": original.get("snapshot_sha256"),
            "sink": original.get("sink", "legacy"),
            "desired": desired(events),
            "legacy_calendar": [
                {
                    k: r.get(k)
                    for k in (
                        "id",
                        "issuer_key",
                        "ticker",
                        "type",
                        "period",
                        "calendar_linked_id",
                        "calendar_sync_fingerprint",
                    )
                }
                for r in rows
                if r.get("calendar_synced")
            ],
            "pending": raw.get("pending", {}) if isinstance(raw, dict) else {},
            "mail": original.get("mail", {}),
        }


def reconcile(path: Path, person: str, receipt: dict) -> dict:
    """Record externally reviewed effects; never infer success from legacy flags.

    Each calendar receipt confirms the current desired fingerprint and real
    provider id. Notification review must cover pending/uncertain legacy effects.
    """
    from .calendar import desired

    if not isinstance(receipt, dict):
        raise EarningsError("invalid_argument", "reconciliation receipt must be an object")
    evidence = receipt.get("evidence")
    if (
        not isinstance(evidence, str)
        or not evidence.strip()
        or receipt.get("notifications_reviewed") is not True
    ):
        raise EarningsError("invalid_argument", "external reconciliation evidence is required")
    with connect(path, write=True) as db:
        row = db.execute(
            "SELECT payload,state FROM deliveries WHERE person=? AND key='legacy-state'", (person,)
        ).fetchone()
        if not row or row["state"] != "unknown":
            raise EarningsError("invalid_argument", "no unresolved legacy migration")
        original = json.loads(row["payload"])
        events = [
            json.loads(r["doc"]) | {"revision": r["rev"]}
            for r in db.execute(
                "SELECT doc,rev FROM events WHERE source_key IN (SELECT source_key FROM follows WHERE person=?) ORDER BY date,id",
                (person,),
            )
        ]
        entries = {entry["key"]: entry for entry in desired(events)}
        confirmations = receipt.get("calendar", [])
        if (
            not isinstance(confirmations, list)
            or any(
                not isinstance(r, dict) or not isinstance(r.get("key"), str) for r in confirmations
            )
            or len({r["key"] for r in confirmations}) != len(confirmations)
        ):
            raise EarningsError("invalid_argument", "invalid calendar confirmations")
        absent = receipt.get("calendar_absent", [])
        if not isinstance(absent, list) or any(not isinstance(k, str) for k in absent):
            raise EarningsError("invalid_argument", "invalid absent calendar keys")
        confirmed = {r["key"] for r in confirmations}
        raw_events = original.get("events", {})
        old_events = raw_events.get("events", []) if isinstance(raw_events, dict) else raw_events
        source_keys = {r["ticker"]: r["key"] for r in db.execute("SELECT ticker,key FROM sources")}
        imported_keys = {
            "earnings-watch:" + f"{r['type']}:{source_keys[r['ticker']]}:{r['period']}"
            for r in old_events
            if r.get("calendar_synced")
        }
        if confirmed & set(absent) or not imported_keys <= (confirmed | set(absent)):
            raise EarningsError(
                "invalid_argument", "every imported calendar marker requires external review"
            )
        for confirmation in confirmations:
            key = confirmation["key"]
            if (
                key not in entries
                or confirmation.get("fingerprint") != entries[key]["fingerprint"]
                or not isinstance(confirmation.get("provider_id"), str)
                or not confirmation["provider_id"].strip()
            ):
                raise EarningsError(
                    "invalid_argument", "calendar receipt must confirm current desired state"
                )
            db.execute(
                "INSERT INTO calendar_ledger VALUES(?,?,?,?,?,?)",
                (
                    person,
                    original.get("sink", "legacy"),
                    key,
                    confirmation["fingerprint"],
                    confirmation["provider_id"],
                    now(),
                ),
            )
        # Reviewed current facts become a baseline, never a fresh reminder or
        # document delivery. Future changes retain their normal delivery keys.
        from datetime import datetime, timezone
        from .notify import event_notices

        documents = [json.loads(r[0]) for r in db.execute("SELECT doc FROM documents ORDER BY id")]
        today = datetime.now(timezone.utc).date()
        for key, kind, payload in event_notices(events, documents, today):
            db.execute(
                "INSERT OR IGNORE INTO deliveries(person,key,kind,state,payload,sent_at) VALUES(?,?,?,'sent',?,?)",
                (person, key, kind, encode(payload), now()),
            )
        db.execute(
            "UPDATE people SET notify_seq=(SELECT coalesce(max(seq),0) FROM event_revisions) WHERE id=?",
            (person,),
        )
        original["reconciliation"] = receipt | {"at": now()}
        db.execute(
            "UPDATE deliveries SET state='sent',payload=? WHERE person=? AND key='legacy-state'",
            (encode(original), person),
        )
    return {"reconciled": True, "calendar_confirmed": len(confirmations)}
