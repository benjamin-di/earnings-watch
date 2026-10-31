"""The command line composes services; business rules live behind the same API as MCP."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .composition import open_service
from .errors import EarningsError
from .settings import load


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        prog="earnings-watch", description="Verified issuer earnings dates and results documents"
    )
    root.add_argument("--config", type=Path)
    root.add_argument("--person", default="local")
    sub = root.add_subparsers(dest="command", required=True)
    for name in (
        "init",
        "doctor",
        "health",
        "status",
        "preflight",
        "docs-guard",
        "watchlist",
        "review-legacy",
        "freeze",
        "unfreeze",
    ):
        p = sub.add_parser(name)
        if name == "doctor":
            p.add_argument("--live", action="store_true")
    for name in ("watch", "unwatch"):
        p = sub.add_parser(name)
        p.add_argument("ticker")
        p.add_argument("--exchange")
    p = sub.add_parser("holdings")
    p.add_argument("action", choices=["sync"])
    p.add_argument("--feed", required=True)
    p.add_argument("file", type=Path)
    p = sub.add_parser("notify")
    p.add_argument("--channel", choices=["stdout", "webhook"], default="stdout")
    p.add_argument("--url")
    p.add_argument("--preview", action="store_true")
    p.add_argument("--inspect", action="store_true")
    p.add_argument("--resolve")
    p.add_argument("--receipt", type=Path)
    p = sub.add_parser("overrides")
    p.add_argument("action", choices=["import", "set", "clear"])
    p.add_argument("value")
    p.add_argument("--kind", choices=["remove", "timing_selection"], default="remove")
    p.add_argument("--payload", default="{}")
    p = sub.add_parser("upcoming")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--limit", type=int, default=100)
    p.add_argument("--scope", choices=["following", "all"], default="following")
    p.add_argument("--format", choices=["table", "json", "ics"], default="table")
    p = sub.add_parser("show")
    p.add_argument("ids", nargs="+")
    p = sub.add_parser("documents")
    p.add_argument("--ticker", action="append")
    p.add_argument("--download", type=Path)
    p.add_argument("--allow-unverified", action="store_true")
    p = sub.add_parser("changes")
    p.add_argument("--cursor")
    p.add_argument("--limit", type=int, default=100)
    p = sub.add_parser("calendar")
    p.add_argument("action", choices=["plan", "ack", "ics"])
    p.add_argument("--sink", default="delegated")
    p.add_argument("--event-id", action="append")
    p.add_argument("--file", type=Path, help="JSON object containing handle and results")
    for name in ("backup", "restore"):
        p = sub.add_parser(name)
        p.add_argument("file", type=Path)
    p = sub.add_parser("sources")
    p.add_argument("action", choices=["list", "show", "import", "validate", "export"])
    p.add_argument("value", nargs="?")
    p = sub.add_parser("export")
    p.add_argument("kind", choices=["events", "sources", "mail"])
    p.add_argument("--legacy", action="store_true")
    p.add_argument("--format", choices=["yaml", "json", "csv"], default="yaml")
    p = sub.add_parser("import")
    p.add_argument("--legacy", type=Path, required=True)
    p.add_argument("--sink", default="legacy")
    p = sub.add_parser("archive-effects")
    p.add_argument("file", type=Path)
    p = sub.add_parser("reconcile-legacy")
    p.add_argument("file", type=Path, help="Externally reviewed JSON receipt")
    p = sub.add_parser("users")
    p.add_argument("action", choices=["add", "list", "remove", "rotate"])
    p.add_argument("id", nargs="?")
    p = sub.add_parser("sweep")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true")
    p.add_argument("--docs-only", action="store_true")
    p.add_argument("--search", default="")
    sub.add_parser("worker")
    p = sub.add_parser("serve")
    p.add_argument("--http", action="store_true")
    p.add_argument("--stdio", action="store_true")
    p.add_argument("--mode", choices=["standalone", "gateway"], default="standalone")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8787)
    return root


def execute(args, settings):
    from .store.database import (
        Catalogue,
        LibraryReader,
        SCHEMA,
        add_person,
        backup,
        connect,
        initialize,
        integrity,
        people,
    )
    from .maintenance import maintenance_lock

    command = args.command
    if command == "init":
        with maintenance_lock(settings):
            initialize(settings.db)
        if "local" not in {p["id"] for p in people(settings.db)}:
            add_person(settings.db, "local")
        return {"state": "initialized", "database": str(settings.db)}
    if command in {"doctor", "health"}:
        result = {
            "integrity": integrity(settings.db),
            "database": str(settings.db),
            "schema": SCHEMA,
            "paused": (settings.data_dir / ".paused").exists(),
            "backends": settings.backends,
        }
        if command == "doctor":
            from .llm.doctor import inspect

            async def checks():
                return [await inspect(backend, args.live) for backend in settings.backends]

            result["capabilities"] = asyncio.run(checks())
            if any(
                r["state"] not in {"ready", "disabled"} or r.get("canary") is False
                for r in result["capabilities"]
            ):
                result["exit"] = 3
        return result
    if command == "preflight":
        if (settings.data_dir / ".paused").exists():
            return {"state": "paused", "exit": 3}
        with maintenance_lock(settings):
            return {"state": "ready"}
    if command == "docs-guard":
        events = LibraryReader(settings.db).events()
        from datetime import date, datetime, timezone

        today = datetime.now(timezone.utc).date()
        due = any(-1 <= (today - date.fromisoformat(e["date"])).days <= 7 for e in events)
        return {"state": "links" if due else "none", "exit": 0 if due else 3}
    if command in {"freeze", "unfreeze"}:
        with maintenance_lock(settings):
            pause = settings.data_dir / ".paused"
            pause.touch(mode=0o600) if command == "freeze" else pause.unlink(missing_ok=True)
        return {"state": "paused" if command == "freeze" else "ready"}
    if command == "backup":
        backup(settings.db, args.file)
        return {"backup": str(args.file)}
    if command == "restore":
        # Restore to a separate DB first, validate, then exchange under the lock.
        if not (settings.data_dir / ".paused").exists():
            raise EarningsError("invalid_argument", "freeze maintenance before restoring")
        from .store.database import restore

        with maintenance_lock(settings):
            restore(settings.db, args.file)
        return {"state": "restored"}
    if command == "users":
        if args.action == "list":
            return {"people": people(settings.db)}
        if not args.id:
            raise EarningsError("invalid_argument", "person id is required")
        if args.action == "add":
            token = secrets.token_urlsafe(32)
            add_person(settings.db, args.id, token=token)
            return {"person": args.id, "token": token}
        from hashlib import sha256

        with connect(settings.db, write=True) as db:
            if args.action == "remove":
                db.execute("DELETE FROM people WHERE id=?", (args.id,))
                db.execute(
                    "INSERT OR REPLACE INTO metadata VALUES(?,?)", ("blocked:" + args.id, "removed")
                )
                for prefix in ("email:", "baseline:", "brief-cutoff:"):
                    db.execute("DELETE FROM metadata WHERE key=?", (prefix + args.id,))
                for namespace in ("holdings:", "delivery-review:"):
                    prefix = namespace + args.id + ":"
                    db.execute(
                        "DELETE FROM metadata WHERE substr(key,1,?)=?", (len(prefix), prefix)
                    )
                return {"state": "removed"}
            token = secrets.token_urlsafe(32)
            changed = db.execute(
                "UPDATE people SET token_sha256=? WHERE id=?",
                (sha256(token.encode()).hexdigest(), args.id),
            )
            if not changed.rowcount:
                raise EarningsError("not_found", "person not found")
            return {"person": args.id, "token": token}
    if command == "sources":
        from .model.sources import Source

        catalogue = Catalogue(settings.db)
        if args.action in {"import", "validate"}:
            if not args.value:
                raise EarningsError("invalid_argument", "source file is required")
            raw = yaml.safe_load(Path(args.value).read_text())
            rows = raw.get("sources", []) if isinstance(raw, dict) else raw
            sources = [Source.model_validate(row) for row in rows]
            if len({s.key for s in sources}) != len(sources) or len(
                {(s.ticker, s.exchange) for s in sources}
            ) != len(sources):
                raise EarningsError("invalid_argument", "duplicate source identity")
            if args.action == "import":
                with maintenance_lock(settings):
                    for source in sources:
                        catalogue.put_source(source)
            return {
                "sources": len(sources),
                "state": "valid" if args.action == "validate" else "imported",
            }
        rows = [
            s.model_dump(mode="json")
            for s in catalogue.sources()
            if args.action != "show" or s.key == args.value
        ]
        return {"sources": rows}
    if command == "export":
        from .exports import render

        if args.legacy:
            from .exports import legacy

            return legacy(LibraryReader(settings.db), args.kind, args.person)
        if args.kind == "mail":
            raise EarningsError("invalid_argument", "mail export requires --legacy")
        return render(LibraryReader(settings.db), args.kind, args.format)
    if command == "import":
        from .legacy import import_snapshot

        with maintenance_lock(settings):
            return import_snapshot(settings.db, args.legacy, args.person, args.sink)
    if command == "review-legacy":
        from .legacy import review

        return review(settings.db, args.person)
    if command == "archive-effects":
        from .store.accounts import archive_effects

        with maintenance_lock(settings):
            return archive_effects(settings.db, args.person, json.loads(args.file.read_text()))
    if command == "reconcile-legacy":
        from .legacy import reconcile

        with maintenance_lock(settings):
            return reconcile(settings.db, args.person, json.loads(args.file.read_text()))
    if command == "sweep":
        from .maintenance import Maintenance

        return asyncio.run(
            Maintenance(settings).sweep(
                dry_run=args.dry_run,
                force=args.force,
                docs_only=args.docs_only,
                search=set(filter(None, args.search.split(","))),
            )
        )
    if command == "worker":
        from .worker import run

        asyncio.run(run(settings))
        return None
    if command == "serve":
        if args.http:
            import uvicorn
            from .connections import create_app

            uvicorn.run(
                create_app(settings, args.mode), host=args.host, port=args.port, access_log=False
            )
        else:
            from .mcp_server import create, person_context

            person_context.set(args.person)
            create(settings).run(transport="stdio")
        return None
    if command == "holdings":
        from .store.database import sync_holdings

        rows = yaml.safe_load(args.file.read_text())
        return sync_holdings(settings.db, args.person, args.feed, rows)
    if command == "overrides":
        from .model.events import TimingSelection
        from .store.database import encode

        rows = (
            yaml.safe_load(Path(args.value).read_text())
            if args.action == "import"
            else [{"event_id": args.value, "kind": args.kind, "payload": json.loads(args.payload)}]
        )
        for row in rows:
            if row["kind"] not in {"remove", "timing_selection"}:
                raise EarningsError("invalid_argument", "unsupported override kind")
            if row["kind"] == "timing_selection" and args.action != "clear":
                TimingSelection.model_validate(row["payload"])
        with maintenance_lock(settings), connect(settings.db, write=True) as db:
            for row in rows:
                if args.action == "clear":
                    db.execute(
                        "DELETE FROM overrides WHERE event_id=? AND kind=?",
                        (row["event_id"], row["kind"]),
                    )
                else:
                    db.execute(
                        "INSERT OR REPLACE INTO overrides VALUES(?,?,?)",
                        (row["event_id"], row["kind"], encode(row["payload"])),
                    )
        return {"overrides": len(rows)}
    if command == "notify":
        from .notify import StdoutNotifier, WebhookNotifier, prepare
        from .outbox import deliver, Notifier
        from .transport import Transport

        if args.inspect:
            from .store.database import digest

            with connect(settings.db) as db:
                return {
                    "deliveries": [
                        dict(r)
                        | {
                            "person": args.person,
                            "attempt": r["attempts"],
                            "payload_sha256": digest(json.loads(r["payload"])),
                        }
                        for r in db.execute(
                            "SELECT key,kind,state,payload,message_id,attempts FROM deliveries WHERE person=? AND state IN ('unknown','pending') ORDER BY key",
                            (args.person,),
                        )
                    ]
                }
        if args.resolve:
            if not args.receipt:
                raise EarningsError("invalid_argument", "a private submission proof is required")
            from .outbox import reconcile as reconcile_delivery

            return reconcile_delivery(
                settings.db, args.person, args.resolve, json.loads(args.receipt.read_text())
            )
        if args.preview:
            from .notify import notices

            return {"notices": notices(settings, args.person)}
        notifier: Notifier = StdoutNotifier()
        if args.channel == "webhook":
            if not args.url:
                raise EarningsError("invalid_argument", "webhook requires --url")
            from .model.urls import public_url

            url = public_url(args.url)
            notifier = WebhookNotifier(url, Transport({urlsplit(url).hostname or ""}))
        with maintenance_lock(settings):
            prepare(settings)
            return asyncio.run(deliver(settings.db, args.person, notifier))
    service = open_service(settings, args.person)
    if command == "upcoming":
        return (
            service.calendar_ics()
            if args.format == "ics"
            else service.upcoming(args.days, limit=args.limit, scope=args.scope)
        )
    if command == "show":
        return service.get_events(args.ids)
    if command == "changes":
        return service.changes(args.cursor, args.limit)
    if command == "watchlist":
        return service.watchlist()
    if command in {"watch", "unwatch"}:
        return getattr(service, command)(args.ticker, args.exchange)
    if command == "calendar":
        if args.action == "ics":
            return service.calendar_ics()
        if args.action == "plan":
            return service.calendar_plan(args.sink, event_ids=args.event_id)
        if not args.file:
            raise EarningsError("invalid_argument", "ack requires --file")
        body = json.loads(args.file.read_text())
        return service.calendar_ack(body["handle"], body["results"])
    if command == "documents":
        rows = service.documents(tickers=args.ticker)
        if args.download:
            from .documents import download
            from .transport import Transport

            hosts = {urlsplit(d["url"]).hostname for d in rows["documents"]}
            transport = Transport({h for h in hosts if h})

            async def downloads():
                return [
                    str(
                        await download(
                            doc,
                            transport=transport,
                            folder=args.download,
                            root=settings.downloads_root,
                            allow_unverified=args.allow_unverified,
                            catalogue=Catalogue(settings.db),
                            person=args.person,
                            cache_dir=settings.data_dir / "cache" / "blobs",
                            cache_max_bytes=settings.cache_max_mb * 1024 * 1024,
                        )
                    )
                    for doc in rows["documents"]
                ]

            with maintenance_lock(settings):
                return {"files": asyncio.run(downloads())}
        return rows
    if command == "status":
        return service.status()
    raise EarningsError("invalid_argument", "unknown command")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    # --json works before or after subcommands.
    json_output = "--json" in raw
    raw = [arg for arg in raw if arg != "--json"]
    args = parser().parse_args(raw)
    if json_output and args.command == "upcoming":
        args.format = "json"
    try:
        result = execute(args, load(args.config))
        if args.command == "upcoming" and args.format == "table":
            columns = ("date", "ticker", "type", "period", "time_hint")
            rows = [[str(event.get(c) or "") for c in columns] for event in result["events"]]
            rows.insert(0, [c.upper().replace("_", " ") for c in columns])
            widths = [max(len(row[i]) for row in rows) for i in range(len(columns))]
            for row in rows:
                print("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())
            if result["cursor"]:
                print("More events are available; use --format json to retrieve the cursor.")
        elif isinstance(result, str):
            print(result, end="" if result.endswith("\n") else "\n")
        elif result is not None:
            print(json.dumps({"ok": True, **result}, default=str, indent=2))
        return result.get("exit", 0) if isinstance(result, dict) else 0
    except EarningsError as exc:
        print(json.dumps(exc.envelope()), file=sys.stderr)
        return 3 if exc.code == "temporarily_unavailable" else 1
    except (ValueError, OSError, KeyError) as exc:
        print(json.dumps(EarningsError("invalid_argument", str(exc)).envelope()), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
