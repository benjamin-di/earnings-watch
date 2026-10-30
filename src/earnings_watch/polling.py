"""Persistent, bounded release polling for official providers and issuer links."""

from datetime import datetime, timedelta, timezone
from . import release
from .documents import verify_link
from .model.events import Event
from .providers.generic_ir import capture
from .store.database import digest

# Four days at ten-minute cadence, one at thirty minutes, two at two hours,
# and two at one day. Cover the entire -1..+7 day window.
MAX_POLL_ATTEMPTS = 4 * 144 + 48 + 2 * 12 + 2


def due(event, state, now):
    if state.get("event_date", str(event.date)) != str(event.date):
        state = {}
    offset = (now.date() - event.date).days
    if not -1 <= offset <= 7 or state.get("attempts", 0) >= MAX_POLL_ATTEMPTS:
        return False
    interval = 600 if offset <= 2 else 1800 if offset <= 3 else 7200 if offset <= 5 else 86400
    previous = state.get("last_attempt")
    return not previous or now - datetime.fromisoformat(previous) >= timedelta(seconds=interval)


async def poll(catalogue, source, transport, *, dry_run=False, research=None, providers=None):
    enabled = set(providers) if providers is not None else {"generic_ir", "sec_edgar", "hkexnews"}
    provider = release.adapter(source)
    official = {"sec": "sec_edgar", "hkex": "hkexnews", "page": "generic_ir"}[provider]
    if official not in enabled and "generic_ir" not in enabled:
        return 0
    now = datetime.now(timezone.utc)
    count = 0
    for row in catalogue.events():
        if row["issuer_key"] != source.key or row["type"] != "earnings":
            continue
        event = Event.model_validate({k: v for k, v in row.items() if k != "revision"})
        state = catalogue.state("release_watch", event.id)
        if not due(event, state, now):
            continue
        if state.get("event_date", str(event.date)) != str(event.date):
            state = {k: v for k, v in state.items() if k not in {"attempts", "last_attempt"}}
        state.update(
            attempts=state.get("attempts", 0) + 1,
            last_attempt=now.isoformat(),
            event_date=str(event.date),
        )
        # Reserve attempts before I/O so crashes cannot create unbounded retries.
        if not dry_run:
            catalogue.set_state("release_watch", event.id, state)
        found = []
        try:
            if provider in {"sec", "hkex"} and official in enabled:
                signal, learned = await (release._sec if provider == "sec" else release._hkex)(
                    source, event, now, set(state.get("seen", [])), transport
                )
                state["seen"] = sorted(set(state.get("seen", [])) | set(learned))[-200:]
                if signal and signal.link:
                    link = signal.link
                    found.append(
                        {
                            "id": digest([event.id, link.url]),
                            "event_id": event.id,
                            "url": link.url,
                            "title": link.title,
                            "role": "release",
                            "evidence": link.evidence,
                            "evidence_url": link.evidence_url,
                            "verified": True,
                            "unverified_reason": None,
                            "first_seen": now.isoformat(),
                            "sha256": "",
                        }
                    )
                    state["released_at"] = (signal.published_at or now).isoformat()
            if source.results_url and "generic_ir" in enabled:
                page = await capture(transport, source.results_url)
                for page_link in page.links:
                    for role in ("release", "presentation", "statements"):
                        doc = verify_link(
                            source,
                            row,
                            page_link | {"role": role, "evidence": page_link["title"]},
                            page.text,
                        )
                        if doc["verified"]:
                            found.append(doc)
                            break
            if not found and research is not None and "generic_ir" in enabled:
                found.extend(await research(source, row))
        finally:
            if not dry_run:
                for doc in found:
                    catalogue.put_document(doc)
                catalogue.set_state("release_watch", event.id, state)
        count += len(found)
    return count
