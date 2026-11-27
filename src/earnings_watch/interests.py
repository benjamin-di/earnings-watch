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
from .store.database import (
    MAX_DAILY_REGISTRATIONS,
    MAX_PENDING_REGISTRATIONS,
    LibraryReader,
    PersonalStore,
    connect,
    now,
)
from .symbols import (
    is_valid_exchange,
    is_valid_isin,
    is_valid_ticker,
    normalize_exchange,
    parse_symbol,
)


def _match_sources(sources, raw_value: str, raw_exchange: str):
    needle = raw_value.strip().casefold()
    explicit_exchange = normalize_exchange(raw_exchange)
    # Check exact company-name match first so names with ':' or '.CO' are not misparsed.
    if needle:
        exact = [
            s
            for s in sources
            if s.name.casefold() == needle
            and (not explicit_exchange or s.exchange == explicit_exchange)
        ]
        if exact:
            return exact[0].ticker, exact[0].exchange or "", exact
    value, exchange = parse_symbol(raw_value, raw_exchange)
    matches = [
        s
        for s in sources
        if (s.ticker == value or s.isin == value) and (not exchange or s.exchange == exchange)
    ]
    if matches:
        return value, exchange, matches
    # Only allow substring company-name fallback when the input is not a valid ticker/ISIN,
    # so short real tickers (e.g. ON, MP, T, APP) are never hijacked by existing company names.
    if needle and not is_valid_ticker(value) and not is_valid_isin(value):
        partial = [
            s
            for s in sources
            if needle in s.name.casefold()
            and (not explicit_exchange or s.exchange == explicit_exchange)
        ]
        if partial:
            return value, explicit_exchange, partial
    return value, exchange, []


def preview(settings, person: str, text: str) -> dict:
    if len(text.encode()) > 32768:
        raise EarningsError("over_cap", "Import at most 200 companies at once.")
    reader = csv.DictReader(io.StringIO(text, newline=""))
    fields = [field.strip().lower() for field in (reader.fieldnames or [])]
    if any(field in ("ticker", "isin") for field in fields):
        if any(fields.count(field) > 1 for field in ("ticker", "isin", "exchange")):
            raise EarningsError("invalid_argument", "Duplicate company identity columns in CSV.")
        reader.fieldnames = fields
        rows = list(reader)
        # Spreadsheet exports often carry blank or cash-only rows; skip rows without an identity.
        values = []
        for row in rows:
            identity = (row.get("ticker") or "").strip() or (row.get("isin") or "").strip()
            if identity:
                values.append((identity, row.get("exchange") or ""))
    else:
        values = [(v.strip(), "") for v in re.split(r"[\s,;]+", text) if v.strip()]
    if len(values) > 200:
        raise EarningsError("over_cap", "Import at most 200 companies at once.")
    sources = LibraryReader(settings.db).sources()
    prepared: set[tuple[str, str]] = set()
    unlisted_map: dict[tuple[str, str], str | None] = {}
    for raw_value, raw_exchange in values:
        value, exchange, matches = _match_sources(sources, raw_value, raw_exchange)
        if len(matches) > 1:
            raise EarningsError("ambiguous", f"Choose an exchange for {value} using MIC:TICKER.")
        if matches:
            prepared.add((matches[0].ticker, matches[0].exchange or ""))
            continue
        if not is_valid_ticker(value) or (exchange and not is_valid_exchange(exchange)):
            raise EarningsError("invalid_argument", "Use a ticker or ISIN, optionally MIC:TICKER.")
        isin = value if is_valid_isin(value) else None
        unlisted_map[(value, exchange)] = isin
    items = [dict(ticker=t, exchange=e) for t, e in sorted(prepared)]
    unlisted = [
        dict(ticker=t, exchange=e, isin=isin) for (t, e), isin in sorted(unlisted_map.items())
    ]
    all_follows = PersonalStore(settings.db, person).follows()
    current = {
        (r["ticker"], r["exchange"]) for r in all_follows if r["origin"] == "holdings:upload"
    }
    current_queued_upload = {
        (r["ticker"], r["exchange"])
        for r in all_follows
        if r["origin"] == "holdings:upload" and r["state"] == "queued"
    }
    other_follows = [r for r in all_follows if r["origin"] != "holdings:upload"]
    if len(items) + len(unlisted) + len(other_follows) > 200:
        raise EarningsError("over_cap", "Follow limit reached.")
    other_pending = sum(1 for r in other_follows if r["state"] == "queued")
    if len(unlisted) + other_pending > MAX_PENDING_REGISTRATIONS:
        raise EarningsError(
            "over_cap",
            f"At most {MAX_PENDING_REGISTRATIONS} company requests can be pending at once.",
        )
    new_queued = [u for u in unlisted if (u["ticker"], u["exchange"]) not in current_queued_upload]
    if new_queued:
        with connect(settings.db) as db:
            quota = db.execute(
                "SELECT used FROM registration_quotas WHERE person=? AND day=?",
                (person, now()[:10]),
            ).fetchone()
            used = quota[0] if quota else 0
        if used + len(new_queued) > MAX_DAILY_REGISTRATIONS:
            raise EarningsError("over_cap", "Daily registration limit reached.")
    updated = {(r["ticker"], r["exchange"]) for r in items} | {
        (r["ticker"], r["exchange"]) for r in unlisted
    }
    payload: dict = dict(
        purpose="interests_preview",
        person=person,
        revision=profile(settings.db, person)["revision"],
        exp=int(time.time()) + 900,
        items=items,
    )
    if unlisted:
        payload["unlisted"] = unlisted
    body = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True).encode()).decode()
    key = (settings.data_dir / "calendar.key").read_bytes()
    return dict(
        items=items,
        unlisted=unlisted,
        adds=len(updated - current),
        removes=len(current - updated),
        queued=len(unlisted),
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
        if (
            not isinstance(value, dict)
            or value.get("purpose") != "interests_preview"
            or value["person"] != person
            or value["exp"] < time.time()
        ):
            raise ValueError()
    except (ValueError, KeyError):
        raise EarningsError("invalid_argument", "Import preview expired. Preview again.") from None
    # Both transactions share the same connection here: replacement and revision CAS
    # must succeed together, including two concurrent submissions of the same preview.
    from .store.accounts import replace_preview

    return replace_preview(settings, person, value)
