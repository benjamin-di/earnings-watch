"""Answer-shaped local projections; no research or external account access."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .store.accounts import effective_follows, profile


def present(event, person, library, *, sources_by_key: dict[str, Any] | None = None):
    data = dict(event)
    certainty = {
        "ir-confirmed": "Confirmed by the issuer",
        "secondary": "Reported",
        "expected": "Expected",
    }.get(event["status"], "Past")
    tz = profile(person.path, person.person)["timezone"]
    when = datetime.fromisoformat(event["date"]).strftime("%a %d %b")
    if event.get("release_time") and event.get("release_tz") and not event.get("timing_issue"):
        dt = (
            datetime.fromisoformat(event["date"] + "T" + event["release_time"])
            .replace(tzinfo=ZoneInfo(event["release_tz"]))
            .astimezone(ZoneInfo(tz))
        )
        when = dt.strftime("%a %d %b, %H:%M") + " " + tz
    elif event.get("time_hint"):
        when += {
            "BMO": ", before market open",
            "AMC": ", after market close",
            "DURING": ", during market hours",
        }.get(event["time_hint"], "")
    followed = [
        r
        for r in effective_follows(person.path, person.person)
        if r["source_key"] == event["issuer_key"]
    ]
    if sources_by_key is None:
        sources_by_key = {s.key: s for s in library.sources()}
    source = sources_by_key.get(event["issuer_key"])
    company_name = source.name if source is not None else event["ticker"]
    data.update(
        company_name=company_name,
        title=event["ticker"]
        + " "
        + event["period"]
        + " "
        + ("results" if event["type"] == "earnings" else event["type"].replace("-", " ")),
        when=when,
        certainty=certainty,
        why_followed=sorted(
            {"holding" if r["origin"].startswith("holdings:") else "watchlist" for r in followed}
        ),
        documents=[
            {k: d.get(k) for k in ("title", "role", "url")}
            for d in library.documents()
            if d["event_id"] == event["id"] and d["verified"]
        ],
    )
    return data


def group_upcoming(
    events: list[dict[str, Any]], *, today: date | None = None
) -> dict[str, list[dict[str, Any]]]:
    """Bucket upcoming events into this_week (0-7d), next_week (8-14d), and later (15d+)."""
    ref = today or datetime.now(timezone.utc).date()
    week1 = ref + timedelta(days=7)
    week2 = ref + timedelta(days=14)
    buckets: dict[str, list[dict[str, Any]]] = {"this_week": [], "next_week": [], "later": []}
    for event in events:
        event_date = date.fromisoformat(event["date"])
        if event_date <= week1:
            buckets["this_week"].append(event)
        elif event_date <= week2:
            buckets["next_week"].append(event)
        else:
            buckets["later"].append(event)
    return buckets
