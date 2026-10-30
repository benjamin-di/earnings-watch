"""Restart-safe UTC scheduler; a failed sweep has priority over documents."""

import asyncio
from datetime import datetime, timezone
from .maintenance import Maintenance
from .settings import Settings


def sweep_due(status, now):
    previous = status.get("last_sweep")
    if (now.hour, now.minute) < (23, 10):
        # A failed prior evening sweep continues retrying after midnight.
        return bool(previous and previous["exit"] != 0)
    return not previous or previous["exit"] != 0 or previous["started"][:10] != str(now.date())


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
                    await maintenance.sweep(docs_only=True)
            except Exception:
                # Retry on the next bounded tick; never retire the daily sweep.
                pass
        await asyncio.sleep(30)
