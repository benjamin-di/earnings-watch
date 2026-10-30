"""Shared library reads and personal mutations; no provider, model or network imports."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
from typing import Any

from . import calendar, ics
from .errors import EarningsError
from .store.database import LibraryReader, PersonalStore


class PersonService:
    def __init__(self, library: LibraryReader, person: PersonalStore, signing_key: bytes):
        self.library, self.person, self.signing_key = library, person, signing_key

    def _events(self, scope: str = "following") -> list[dict[str, Any]]:
        if scope not in {"following", "all"}:
            raise EarningsError("invalid_argument", "scope must be following or all")
        follows = self.person.follows()
        keys = {row["source_key"] for row in follows}
        return [
            row
            for row in self.library.events()
            if scope == "all" or not follows or row["issuer_key"] in keys
        ]

    def upcoming(
        self,
        days: int = 30,
        tickers: list[str] | None = None,
        types: list[str] | None = None,
        limit: int = 100,
        cursor: str | None = None,
        scope: str = "following",
    ) -> dict[str, Any]:
        if not 1 <= days <= 90 or not 1 <= limit <= 100 or (tickers and len(tickers) > 20):
            raise EarningsError("invalid_argument", "days, tickers or limit exceeds its budget")
        today = datetime.now(timezone.utc).date()
        rows = [
            row
            for row in self._events(scope)
            if str(today) <= row["date"] <= str(today + timedelta(days=days))
            and (not tickers or row["ticker"] in tickers)
            and (not types or row["type"] in types)
        ]
        # Stable keyset paging. Catalogue changes remain available via changes().
        if cursor:
            try:
                after = base64.urlsafe_b64decode(cursor).decode().split("|", 1)
                if len(after) != 2:
                    raise ValueError()
            except (ValueError, UnicodeError):
                raise EarningsError("invalid_argument", "invalid cursor") from None
            rows = [row for row in rows if (row["date"], row["id"]) > tuple(after)]
        selected = rows[:limit]
        next_cursor = (
            base64.urlsafe_b64encode(
                (selected[-1]["date"] + "|" + selected[-1]["id"]).encode()
            ).decode()
            if len(rows) > limit
            else None
        )
        # The compact view stays within the MCP response budget.
        fields = {
            "id",
            "ticker",
            "type",
            "period",
            "date",
            "time_hint",
            "release_time",
            "release_tz",
            "timing_issue",
            "status",
            "source_url",
            "revision",
        }
        return {
            "events": [{k: v for k, v in row.items() if k in fields} for row in selected],
            "cursor": next_cursor,
        }

    def get_events(self, ids: list[str]) -> dict[str, Any]:
        if not 1 <= len(ids) <= 20:
            raise EarningsError("invalid_argument", "provide 1 to 20 event ids")
        by_id = {row["id"]: row for row in self.library.events()}
        missing = set(ids) - by_id.keys()
        if missing:
            raise EarningsError("not_found", "event not found", ids=sorted(missing))
        return {
            "events": [by_id[key] | {"history": by_id[key].get("history", [])[-10:]} for key in ids]
        }

    def documents(
        self,
        event_ids: list[str] | None = None,
        tickers: list[str] | None = None,
        roles: list[str] | None = None,
        since: str | None = None,
        limit: int = 100,
        scope: str = "following",
    ) -> dict[str, Any]:
        if not 1 <= limit <= 100 or len(event_ids or []) > 20 or len(tickers or []) > 20:
            raise EarningsError("invalid_argument", "document query exceeds its budget")
        events = {row["id"]: row for row in self._events(scope)}
        if event_ids:
            events = {row["id"]: row for row in self.library.events() if row["id"] in event_ids}
        docs = [
            row
            for row in self.library.documents()
            if row["event_id"] in events
            and (not tickers or events[row["event_id"]]["ticker"] in tickers)
            and (not roles or row["role"] in roles)
            and (not since or row["first_seen"] >= since)
        ]
        return {"documents": docs[:limit], "truncated": len(docs) > limit}

    def changes(self, cursor: str | None = None, limit: int = 100) -> dict[str, Any]:
        try:
            seq = int(cursor or "0")
        except ValueError:
            raise EarningsError("invalid_argument", "invalid change cursor") from None
        if seq < 0 or not 1 <= limit <= 100:
            raise EarningsError("invalid_argument", "invalid change query")
        rows = self.library.changes(seq, limit)
        return {"changes": rows, "cursor": str(rows[-1]["seq"] if rows else seq)}

    def watchlist(self) -> dict[str, Any]:
        return {"follows": self.person.follows()}

    def watch(
        self,
        ticker: str,
        exchange: str | None = None,
        isin: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        return self.person.watch(ticker, exchange or "", isin=isin, name=name)

    def unwatch(self, ticker: str, exchange: str | None = None) -> dict[str, Any]:
        return self.person.unwatch(ticker, exchange or "")

    def calendar_plan(self, sink: str = "delegated", full: bool = False) -> dict[str, Any]:
        return calendar.plan(
            self.person,
            key=self.signing_key,
            sink=sink,
            full=full,
        )

    def calendar_ack(self, handle: str, results: list[dict[str, Any]]) -> dict[str, Any]:
        return calendar.ack(self.person, handle, results, key=self.signing_key)

    def calendar_ics(self) -> str:
        return ics.render(self.person.calendar_snapshot("delegated")["events"])

    def status(self) -> dict[str, Any]:
        return self.library.status()
