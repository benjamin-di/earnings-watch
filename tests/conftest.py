from datetime import datetime, timedelta, timezone
import pytest
from earnings_watch.model.events import Event
from earnings_watch.model.sources import Source
from earnings_watch.settings import Settings
from earnings_watch.store.database import Catalogue, add_person, initialize


@pytest.fixture
def settings(tmp_path):
    settings = Settings(data_dir=tmp_path)
    initialize(settings.db)
    add_person(settings.db, "local")
    add_person(settings.db, "alice", token="a" * 40)
    add_person(settings.db, "bob", token="b" * 40)
    return settings


def source(key="alpha", ticker="ALPHA", exchange="XNAS"):
    return Source(
        key=key,
        ticker=ticker,
        exchange=exchange,
        issuer="Example Components",
        name="Example Components",
        calendar_url="https://issuer.example/calendar",
        fetch="plain",
        period_scheme="calendar-q",
        fy_end_month=12,
    )


def event(key="alpha", ticker="ALPHA", **changes):
    today = datetime.now(timezone.utc).date()
    values = dict(
        id=f"earnings:{key}:2026Q4",
        issuer_key=key,
        ticker=ticker,
        type="earnings",
        period="2026Q4",
        date=today + timedelta(days=1),
        status="ir-confirmed",
        source_url="https://issuer.example/calendar",
        evidence="Q4 2026 results will be released tomorrow.",
        first_seen=today,
        last_verified=today,
    )
    values.update(changes)
    return Event.model_validate(values)


@pytest.fixture
def seeded(settings):
    catalogue = Catalogue(settings.db)
    catalogue.put_source(source())
    catalogue.commit_issuer("alpha", [event()], run_id="seed")
    return settings
