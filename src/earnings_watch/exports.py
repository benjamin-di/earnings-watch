import csv
import io
import json
import yaml
from .store.database import LibraryReader
from .model.sources import Source


def render(library: LibraryReader, kind: str, format: str = "yaml") -> str:
    rows = (
        library.events()
        if kind == "events"
        else [s.model_dump(mode="json") for s in library.sources()]
    )
    if format == "json":
        return json.dumps({kind: rows}, indent=2) + "\n"
    if format == "csv":
        stream = io.StringIO()
        if rows:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        return stream.getvalue()
    return yaml.safe_dump({kind: rows}, sort_keys=False, allow_unicode=True)


def legacy(library, kind, person, sink="legacy"):
    """Rehearsal projection. Unknown deliveries still require explicit reconciliation."""
    from .store.database import connect

    with connect(library.path) as db:
        original_row = db.execute(
            "SELECT payload FROM deliveries WHERE person=? AND key='legacy-state'", (person,)
        ).fetchone()
        original = json.loads(original_row[0]) if original_row else {}
        if kind == "sources":
            raw = original.get("sources", {})
            result = dict(raw) if isinstance(raw, dict) else {}
            fields = {
                "key",
                "issuer",
                "ticker",
                "name",
                "calendar_url",
                "fetch",
                "period_scheme",
                "fy_end_month",
                "results_url",
                "announcements_url",
                "timing_url_prefixes",
                "calendar_timezone",
                "calendar_timezone_basis",
                "sec_cik",
            }
            extras = {"wiki", "membership", "track_reason", "library_entity"}
            original_sources = raw.get("sources", []) if isinstance(raw, dict) else raw
            metadata = {r["key"]: r for r in original_sources}
            result["sources"] = [
                {k: v for k, v in source.model_dump(mode="json").items() if k in fields}
                | {k: v for k, v in metadata.get(source.key, {}).items() if k in extras}
                for source in [
                    Source.model_validate_json(r[0])
                    for r in db.execute("SELECT doc FROM sources ORDER BY key")
                ]
            ]
            comments = "\n".join(
                line
                for line in original.get("sources_text", "").splitlines()
                if line.lstrip().startswith("#")
            )
            return (comments + "\n" if comments else "") + yaml.safe_dump(
                result, sort_keys=False, allow_unicode=True
            )
        if kind == "mail":
            return json.dumps(original.get("mail", {}), indent=2) + "\n"
        if kind != "events":
            raise ValueError("unknown legacy projection")
        raw = original.get("events", {})
        previous = raw.get("events", []) if isinstance(raw, dict) else raw
        old = {r["id"]: r for r in previous}
        rows = []
        for stored in db.execute("SELECT id,doc FROM events ORDER BY date,id"):
            event = json.loads(stored["doc"])
            event["id"] = f"{event['type']}:{event['ticker']}:{event['period']}"
            row = old.get(event["id"], {}) | event
            polling = db.execute(
                "SELECT doc FROM release_watch WHERE event_id=?", (stored["id"],)
            ).fetchone()
            if polling:
                state = json.loads(polling[0])
                row.update(
                    {k: v for k, v in state.items() if k.startswith(("results_", "release_"))}
                )
                for current, previous_key in {
                    "attempts": "results_attempts",
                    "last_attempt": "results_last_attempt",
                    "seen": "release_baseline",
                    "released_at": "release_at",
                }.items():
                    if current in state:
                        row[previous_key] = state[current]
            row["results_links"] = []
            for document in db.execute(
                "SELECT doc FROM documents WHERE event_id=? ORDER BY id", (stored["id"],)
            ):
                doc = json.loads(document[0])
                link_fields = {
                    "title",
                    "kind",
                    "url",
                    "evidence_url",
                    "evidence",
                    "coverage",
                    "content_sha256",
                    "attachment_path",
                    "verified_at",
                    "unverified",
                }
                link = {k: v for k, v in doc.items() if k in link_fields}
                for previous_link in old.get(event["id"], {}).get("results_links", []):
                    if (
                        previous_link["url"] == doc["url"]
                        and (previous_link.get("content_sha256") or "") == (doc.get("sha256") or "")
                        and previous_link.get("attachment_path")
                    ):
                        link["attachment_path"] = previous_link["attachment_path"]
                link.update(
                    kind={
                        "release": "press-release",
                        "presentation": "presentation",
                        "statements": "financial-statements",
                    }.get(doc["role"], doc.get("kind", "other")),
                    content_sha256=doc.get("sha256") or None,
                    verified_at=doc.get("verified_at")
                    or (doc.get("first_seen") if doc.get("verified") else None),
                    unverified=None
                    if doc.get("verified")
                    else doc.get("unverified_reason") or "verification required",
                )
                row["results_links"].append(link)
            ledger = db.execute(
                "SELECT * FROM calendar_ledger WHERE person=? AND sink=? AND key=?",
                (person, sink, "earnings-watch:" + stored["id"]),
            ).fetchone()
            if (
                original_row
                and db.execute(
                    "SELECT state FROM deliveries WHERE person=? AND key='legacy-state'", (person,)
                ).fetchone()[0]
                == "unknown"
            ):
                pass  # Retain original calendar markers as unresolved rollback evidence.
            else:
                row.update(
                    calendar_synced=bool(ledger),
                    calendar_linked_id=ledger["provider_id"] if ledger else None,
                    calendar_sync_fingerprint=ledger["fingerprint"] if ledger else None,
                )
            rows.append(row)
        result = dict(raw) if isinstance(raw, dict) else {}
        result["events"] = rows
        return yaml.safe_dump(result, sort_keys=False, allow_unicode=True)
