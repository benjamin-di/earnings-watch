"""Bounded, previewed company imports. Financial quantities are never persisted."""

from __future__ import annotations

import base64
import csv
import hashlib
import hmac
import io
import json
import re
import time

from .errors import EarningsError
from .store.accounts import profile
from .store.database import LibraryReader, PersonalStore


def preview(settings, person: str, text: str) -> dict:
    if len(text.encode()) > 32768:
        raise EarningsError("over_cap", "Import at most 200 companies at once.")
    if "," in text and text.splitlines()[0].lower().startswith(("ticker,", "isin,")):
        rows = list(csv.DictReader(io.StringIO(text)))
        values = [(r.get("ticker") or r.get("isin") or "", r.get("exchange") or "") for r in rows]
    else:
        values = [(v.strip(), "") for v in re.split(r"[\s,;]+", text) if v.strip()]
    if len(values) > 200:
        raise EarningsError("over_cap", "Import at most 200 companies at once.")
    sources = LibraryReader(settings.db).sources()
    prepared = set()
    missing = []
    for value, exchange in values:
        value, exchange = value.strip().upper(), exchange.strip().upper()
        # MIC:TICKER disambiguates identical exchange symbols.
        if ":" in value:
            exchange, value = value.split(":", 1)
        if not re.fullmatch(r"[A-Z0-9]{1,20}", value) or (
            exchange and not re.fullmatch(r"[A-Z0-9]{4}", exchange)
        ):
            raise EarningsError("invalid_argument", "Use a ticker or ISIN, optionally MIC:TICKER.")
        matches = [
            s
            for s in sources
            if (s.ticker == value or s.isin == value) and (not exchange or s.exchange == exchange)
        ]
        if len(matches) > 1:
            raise EarningsError("ambiguous", f"Choose an exchange for {value} using MIC:TICKER.")
        if not matches:
            missing.append(value)
        else:
            prepared.add((matches[0].ticker, matches[0].exchange or ""))
    if missing:
        raise EarningsError(
            "not_found", "Request unlisted companies before importing: " + ", ".join(missing[:10])
        )
    items = [dict(ticker=t, exchange=e) for t, e in sorted(prepared)]
    current = {
        (r["ticker"], r["exchange"])
        for r in PersonalStore(settings.db, person).follows()
        if r["origin"] == "holdings:upload"
    }
    updated = {(r["ticker"], r["exchange"]) for r in items}
    payload = dict(
        person=person,
        revision=profile(settings.db, person)["revision"],
        exp=int(time.time()) + 900,
        items=items,
    )
    body = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    key = (settings.data_dir / "calendar.key").read_bytes()
    return dict(
        items=items,
        adds=len(updated - current),
        removes=len(current - updated),
        handle=body + "." + hmac.new(key, body.encode(), hashlib.sha256).hexdigest(),
    )


def apply(settings, person: str, handle: str) -> dict:
    try:
        if len(handle) > 32768:
            raise ValueError()
        body, sig = handle.rsplit(".", 1)
        key = (settings.data_dir / "calendar.key").read_bytes()
        if not hmac.compare_digest(sig, hmac.new(key, body.encode(), hashlib.sha256).hexdigest()):
            raise ValueError()
        value = json.loads(base64.urlsafe_b64decode(body))
        if value["person"] != person or value["exp"] < time.time():
            raise ValueError()
    except (ValueError, KeyError):
        raise EarningsError("invalid_argument", "Import preview expired. Preview again.") from None
    # Both transactions share the same connection here: replacement and revision CAS
    # must succeed together, including two concurrent submissions of the same preview.
    from .store.accounts import replace_preview

    return replace_preview(settings, person, value)
