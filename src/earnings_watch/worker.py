"""Restart-safe UTC scheduler; a failed sweep has priority over documents."""

import asyncio
from datetime import datetime, timedelta, timezone
from .maintenance import Maintenance
from .settings import Settings

FAILED_SWEEP_COOLDOWN = timedelta(minutes=60)


def _retry_ready(previous, now):
    ended = previous.get("ended")
    if not isinstance(ended, str) or not ended:
        return True
    try:
        finished = datetime.fromisoformat(ended)
    except ValueError:
        return True
    if finished.tzinfo is None:
        finished = finished.replace(tzinfo=timezone.utc)
    return now - finished >= FAILED_SWEEP_COOLDOWN


def sweep_due(status, now):
    previous = status.get("last_sweep")
    if (now.hour, now.minute) < (23, 10):
        # A failed prior evening sweep continues retrying after midnight with bounded backoff.
        return bool(previous and previous["exit"] != 0 and _retry_ready(previous, now))
    if not previous:
        return True
    if previous["exit"] != 0:
        return _retry_ready(previous, now)
    started_raw = previous.get("started")
    if not isinstance(started_raw, str) or not started_raw:
        return True
    try:
        started = datetime.fromisoformat(started_raw)
    except ValueError:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    now_utc = now if now.tzinfo is not None else now.replace(tzinfo=timezone.utc)
    today_window = now_utc.astimezone(timezone.utc).replace(
        hour=23, minute=10, second=0, microsecond=0
    )
    return started.astimezone(timezone.utc) < today_window


async def run(settings: Settings) -> None:
    maintenance = Maintenance(settings)
    last_tick = None
    while True:
        now = datetime.now(timezone.utc)
        tick = int(now.timestamp() // 600)
        if tick != last_tick:
            last_tick = tick
            try:
                if sweep_due(maintenance.catalogue.status(), now):
                    await maintenance.sweep()
                else:
                    if maintenance.catalogue.has_pending_registrations():
                        await maintenance.register()
                    await maintenance.sweep(docs_only=True)
            except Exception:
                # Retry on the next bounded tick; never retire the daily sweep.
                pass
        await asyncio.sleep(30)
