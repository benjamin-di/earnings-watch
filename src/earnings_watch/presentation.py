"""Answer-shaped local projections; no research or external account access."""

from datetime import datetime
from zoneinfo import ZoneInfo
from .store.accounts import effective_follows, profile


def present(event, person, library):
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
    data.update(
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
