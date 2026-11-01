"""Regressions from the 0.3.0 source review; each case failed before its fix."""

import asyncio
import json
import os
import re
import socket
import sqlite3
import stat
import subprocess
import time
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from pydantic import BaseModel

from conftest import event, source
from earnings_watch import open, polling, release
from earnings_watch.cli import main
from earnings_watch.engine.verify import _time_matches, evidence_verifies, verify_claims
from earnings_watch.errors import EarningsError
from earnings_watch.interests import preview
from earnings_watch.llm.runner import AgentTask, CliRunner, NotStarted
from earnings_watch.model.events import EventType, ExtractClaim
from earnings_watch.model.sources import Source, canonical_period
from earnings_watch.store import accounts
from earnings_watch.store.database import Catalogue, PersonalStore, sync_holdings
from earnings_watch.transport import Transport


def plain_source(**changes):
    values = dict(
        key="alpha",
        issuer="Alpha",
        ticker="ALPHA",
        name="Alpha",
        calendar_url="https://issuer.example/calendar",
        fetch="plain",
        period_scheme="calendar-q",
        fy_end_month=12,
    )
    return Source.model_validate(values | changes)


# --- verification -----------------------------------------------------------


def test_numeric_date_is_not_read_as_a_publication_clock():
    page = "Financial calendar. All times are CET.\n\nQ1 2027 results published on 12.05.2027\n"
    claim = ExtractClaim(
        type=EventType.EARNINGS,
        period="Q1 2027",
        date=date(2027, 5, 12),
        release_time="12:05",
        release_tz="Europe/Berlin",
        evidence="Q1 2027 results published on 12.05.2027",
    )
    verified = verify_claims([claim], plain_source(), page, today=date(2026, 11, 1)).events[0]
    assert verified.release_time is None
    assert [m[0] for m in _time_matches("on 12.05.2027 at 07:00 CET")] == [(7, 0)]
    assert [m[0] for m in _time_matches("on 12.05. 07:00 CET")] == [(7, 0)]
    assert [m[0] for m in _time_matches("from 10.30-11.00 CET")] == [(10, 30), (11, 0)]
    assert [m[0] for m in _time_matches("7:00/8:00 CET")] == [(7, 0), (8, 0)]
    assert [m[0] for m in _time_matches("7.00/8.00 CET")] == [(7, 0), (8, 0)]
    assert [m[0] for m in _time_matches("at 10.30. The call")] == [(10, 30)]


def test_yearless_fallback_respects_an_explicit_dotted_year():
    quote = "Q2 2025 results were released on 31 Jul. 2025."
    assert not evidence_verifies(date(2026, 7, 31), quote, quote)
    assert evidence_verifies(date(2025, 7, 31), quote, quote)


def test_yearless_date_must_fall_near_its_period():
    page = "Iota Group plc will release H1 2026 Results at 7am BST on Friday 31st July."
    claim = ExtractClaim(
        type=EventType.EARNINGS, period="H1 2026", date=date(2029, 7, 31), evidence=page
    )
    result = verify_claims(
        [claim], plain_source(key="iota", ticker="IOTA"), page, today=date(2026, 7, 1)
    )
    assert not result.events and "publication window" in result.rejected[0].reason
    rolled = claim.model_copy(update={"date": date(2027, 7, 31)})
    result = verify_claims(
        [rolled], plain_source(key="iota", ticker="IOTA"), page, today=date(2026, 8, 5)
    )
    assert not result.events
    near = claim.model_copy(update={"date": date(2026, 7, 31)})
    result = verify_claims(
        [near], plain_source(key="iota", ticker="IOTA"), page, today=date(2026, 7, 1)
    )
    assert [e.date for e in result.events] == [date(2026, 7, 31)]


def test_yearless_dates_accept_fiscal_q_labels_of_calendar_q_sources():
    june = plain_source(key="june", ticker="JUNE", fy_end_month=6)
    page = "FY2026 results will be published on 20 August."
    claim = ExtractClaim(
        type=EventType.EARNINGS, period="FY2026", date=date(2026, 8, 20), evidence=page
    )
    assert verify_claims([claim], june, page, today=date(2026, 7, 1)).events
    march = plain_source(key="march", ticker="MARCH", fy_end_month=3)
    page = "FY2026 results will be published on 14 May."
    claim = ExtractClaim(
        type=EventType.EARNINGS, period="FY2026", date=date(2026, 5, 14), evidence=page
    )
    assert verify_claims([claim], march, page, today=date(2026, 4, 1)).events


def test_as_at_periods_follow_the_fiscal_year_end():
    day = date(2026, 8, 20)
    half = plain_source(period_scheme="half-year", fy_end_month=6)
    as_at = canonical_period("half-yearly report as at 30 June 2026", half, EventType.EARNINGS, day)
    as_of = canonical_period("results as of June 30, 2026", half, EventType.EARNINGS, day)
    assert as_at == as_of
    fiscal = plain_source(period_scheme="fiscal-q", fy_end_month=9)
    assert canonical_period(
        "interim statement as at 31 March 2026", fiscal, EventType.EARNINGS, day
    ) == canonical_period("results as of March 31, 2026", fiscal, EventType.EARNINGS, day)
    with pytest.raises(ValueError):
        canonical_period("interim statement as at 15 March 2026", fiscal, EventType.EARNINGS, day)
    calendar = plain_source()
    assert (
        canonical_period("half-yearly report as at 30 June 2026", calendar, EventType.EARNINGS, day)
        == "2026H1"
    )


# --- acquisition ------------------------------------------------------------


def test_only_xhkg_listings_poll_hkexnews():
    other = plain_source(key="other", ticker="4321", exchange="XTKS", period_scheme="fiscal-q")
    assert release.adapter(other) == "page"
    assert release.adapter(plain_source(key="hk", ticker="700", exchange="XHKG")) == "hkex"
    assert release.adapter(plain_source(key="hk", ticker="700")) == "hkex"


def test_release_research_stops_once_a_release_is_verified():
    today = datetime.now(timezone.utc).date()
    row = dict(
        id="earnings:alpha:2026Q3",
        issuer_key="alpha",
        ticker="ALPHA",
        type="earnings",
        period="2026Q3",
        date=today.isoformat(),
        status="ir-confirmed",
        source_url="https://issuer.example/calendar",
        evidence="x",
        first_seen=today.isoformat(),
        last_verified=today.isoformat(),
        revision=1,
    )

    class Store:
        def __init__(self):
            self.states, self.documents = {}, []

        def events(self):
            return [row]

        def state(self, _kind, key):
            return dict(self.states.get(key, {}))

        def set_state(self, _kind, key, value):
            self.states[key] = dict(value)

        def put_document(self, document):
            self.documents.append(document)

    store, calls = Store(), []

    async def research(_source, event_row):
        calls.append(1)
        return [
            dict(
                id=f"d{len(calls)}",
                event_id=event_row["id"],
                url="https://issuer.example/q3.pdf",
                role="release",
                verified=True,
            )
        ]

    async def run():
        for _ in range(3):
            await polling.poll(store, plain_source(), transport=None, research=research)
            state = store.states[row["id"]]
            earlier = datetime.fromisoformat(state["last_attempt"]) - timedelta(hours=1)
            store.states[row["id"]] = state | {"last_attempt": earlier.isoformat()}

    asyncio.run(run())
    state = store.states[row["id"]]
    assert len(calls) == 1 and state["researched_release"] and "released_at" not in state


def test_redirect_without_location_fails_once():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return httpx.Response(302)

    transport = Transport(
        {"issuer.example"},
        pace=0,
        daily_budget=200,
        resolver=lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
        mock=httpx.MockTransport(handler),
    )
    with pytest.raises(ValueError, match="redirect without a new location"):
        asyncio.run(transport.get("https://issuer.example/calendar"))
    assert len(calls) == 1


# --- personal state ---------------------------------------------------------


def test_calendar_ack_ignores_unrelated_catalogue_changes(seeded):
    alice = open(seeded, "alice")
    alice.watch("ALPHA")
    plan = alice.calendar_plan(event_ids=[event().id])
    catalogue = Catalogue(seeded.db)
    catalogue.put_source(source(key="beta", ticker="BETA"))
    catalogue.commit_issuer(
        "beta", [event(key="beta", ticker="BETA", id="earnings:beta:2026Q4")], run_id="other"
    )
    results = [
        {"key": op["key"], "status": "applied", "provider_id": "outlook-1"}
        for op in plan["operations"]
    ]
    alice.calendar_ack(plan["handle"], results)
    assert alice.calendar_plan(event_ids=[event().id])["operations"] == []
    import base64

    payload = json.loads(base64.urlsafe_b64decode(plan["handle"].rsplit(".", 1)[0]))
    assert payload["scope"] == [event().id]


def test_calendar_ack_still_refuses_changes_to_planned_events(seeded):
    alice = open(seeded, "alice")
    alice.watch("ALPHA")
    plan = alice.calendar_plan(event_ids=[event().id])
    moved = event(date=event().date + timedelta(days=1))
    Catalogue(seeded.db).commit_issuer("alpha", [moved], run_id="move")
    results = [{"key": op["key"], "status": "applied"} for op in plan["operations"]]
    with pytest.raises(EarningsError, match="changed"):
        alice.calendar_ack(plan["handle"], results)


async def test_calendar_ack_refuses_a_removed_planned_event(seeded):
    alice = open(seeded, "alice")
    alice.watch("ALPHA")
    plan = alice.calendar_plan(event_ids=[event().id])
    Catalogue(seeded.db).commit_issuer("alpha", [], run_id="remove", authoritative=True)
    results = [{"key": op["key"], "status": "applied"} for op in plan["operations"]]
    with pytest.raises(EarningsError, match="changed"):
        alice.calendar_ack(plan["handle"], results)


def test_calendar_handles_without_scope_use_the_global_sequence(seeded):
    alice = open(seeded, "alice")
    alice.watch("ALPHA")
    store = PersonalStore(seeded.db, "alice")
    plan = alice.calendar_plan(event_ids=[event().id])
    import base64

    payload = json.loads(base64.urlsafe_b64decode(plan["handle"].rsplit(".", 1)[0]))
    payload.pop("scope")
    catalogue = Catalogue(seeded.db)
    catalogue.put_source(source(key="beta", ticker="BETA"))
    catalogue.commit_issuer(
        "beta", [event(key="beta", ticker="BETA", id="earnings:beta:2026Q4")], run_id="other"
    )
    with pytest.raises(EarningsError, match="changed"):
        store.acknowledge(payload, [], "legacy-handle")


async def test_unmute_clears_an_all_exchange_mute(seeded):
    from test_connections import Directory, browser

    from earnings_watch.connections import create_app

    accounts.provision(seeded.db, "alice")
    sync_holdings(seeded.db, "alice", "broker", [{"ticker": "ALPHA", "exchange": "XNAS"}])
    assert open(seeded, "alice").unwatch("ALPHA")["state"] == "muted"
    assert accounts.mutes(seeded.db, "alice") == {("ALPHA", "")}
    auth = browser(seeded)
    app = create_app(seeded, auth=auth, directory=Directory())
    async with (
        app.earnings_app.router.lifespan_context(app.earnings_app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://localhost",
            cookies={auth.cookie: "a" * 43},
        ) as client,
    ):
        page = (await client.get("/connections/")).text
        form = re.search(
            r'<form method="post" action="/connections/unmute">(.*?)</form>', page, re.S
        ).group(1)
        assert "Unmute all exchanges" in form
        fields = dict(re.findall(r'name="(\w+)" value="([^"]*)"', form))
        assert fields["exchange"] == ""
        response = await client.post(
            "/connections/unmute", data=fields, headers={"Origin": "http://localhost"}
        )
        assert response.status_code == 303
    assert accounts.mutes(seeded.db, "alice") == set()
    assert open(seeded, "alice").upcoming()["events"]


def test_exact_unmute_keeps_an_all_exchange_mute(seeded):
    accounts.provision(seeded.db, "alice")
    accounts.mute(seeded.db, "alice", "ALPHA", "", True)
    accounts.mute(seeded.db, "alice", "ALPHA", "XNYS", True)
    accounts.mute(seeded.db, "alice", "ALPHA", "XNYS", False)
    assert accounts.mutes(seeded.db, "alice") == {("ALPHA", "")}


def test_removing_a_watchlist_row_keeps_the_holding_visible(seeded):
    sync_holdings(seeded.db, "alice", "broker", [{"ticker": "ALPHA", "exchange": "XNAS"}])
    store = PersonalStore(seeded.db, "alice")
    store.watch("ALPHA", "XNAS")
    assert store.unwatch("ALPHA", "XNAS") == {"state": "removed", "still_held": True}
    assert [r["origin"] for r in store.follows()] == ["holdings:broker"]
    assert accounts.mutes(seeded.db, "alice") == set()
    assert open(seeded, "alice").upcoming()["events"]
    # Without a manual row, unwatch still means "stop showing this holding".
    assert store.unwatch("ALPHA", "XNAS")["state"] == "muted"


def test_csv_import_skips_rows_without_an_identity(seeded):
    accounts.provision(seeded.db, "alice")
    for text in (
        "ticker,exchange,quantity\nALPHA,XNAS,900\n,,\n",
        "ticker,exchange,quantity\nALPHA,XNAS,900\n,,1500\n",
    ):
        assert preview(seeded, "alice", text)["items"] == [{"ticker": "ALPHA", "exchange": "XNAS"}]


def test_invalid_time_zone_is_a_plain_error(seeded):
    accounts.provision(seeded.db, "alice")
    current = accounts.profile(seeded.db, "alice")
    with pytest.raises(EarningsError, match="Choose a time zone"):
        accounts.preferences(
            seeded.db, "alice", current["revision"], current["settings"], "Mars/Base"
        )


# --- model runner -----------------------------------------------------------


class Answer(BaseModel):
    answer: str


def fake_claude(directory, body):
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "claude"
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        '  --version) echo "2.1.312 (Claude Code)"; exit 0;;\n'
        '  --help) echo "--strict-mcp-config --tools --restricted"; exit 0;;\n'
        "esac\n" + body
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return directory


def test_model_timeout_covers_an_unread_large_prompt(tmp_path, monkeypatch):
    path = fake_claude(tmp_path / "bin", "exec sleep 30\n")
    monkeypatch.setenv("PATH", f"{path}:{os.environ['PATH']}")
    task = AgentTask(system="s", prompt="x" * 300_000, schema=Answer, timeout_s=2)
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(asyncio.wait_for(CliRunner(["claude-code"]).run(task), 20))
    assert time.monotonic() - started < 10


def test_early_sign_in_refusal_falls_back_with_a_large_prompt(tmp_path, monkeypatch):
    path = fake_claude(tmp_path / "bin", 'echo "Error: not logged in" >&2\nexit 1\n')
    monkeypatch.setenv("PATH", f"{path}:{os.environ['PATH']}")
    task = AgentTask(system="s", prompt="x" * 300_000, schema=Answer, timeout_s=10)
    result = asyncio.run(CliRunner(["claude-code", "none"]).run(task))
    assert result.backend == "none"


def test_model_children_do_not_survive_the_run(tmp_path, monkeypatch):
    marker = f"sleep {300 + os.getpid() % 500}"
    path = fake_claude(
        tmp_path / "bin", f"cat >/dev/null\nnohup {marker} >&1 2>/dev/null &\nexit 0\n"
    )
    monkeypatch.setenv("PATH", f"{path}:{os.environ['PATH']}")
    task = AgentTask(system="s", prompt="p", schema=Answer, timeout_s=2)
    with pytest.raises(BaseException):
        asyncio.run(CliRunner(["claude-code"]).run(task))
    time.sleep(0.3)
    survivors = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True).stdout
    assert not survivors.strip()


@pytest.mark.parametrize(
    "message",
    [
        '{"error":{"code":"insufficient_quota","message":"You exceeded your current quota"}}',
        'API Error: 529 {"type":"error","error":{"type":"overloaded_error"}}',
        '{"type":"error","error":{"type":"rate_limit_error"}}',
        "Error: Invalid API key. Please run /login",
        "Error: This region is not supported",
        '{"error":{"code":"unsupported_country_region_territory","message":"Unsupported country, region, or territory"}}',
    ],
)
def test_account_refusals_fall_back(tmp_path, monkeypatch, message):
    body = f"cat >/dev/null\necho '{message}' >&2\nexit 1\n"
    path = fake_claude(tmp_path / "bin", body)
    monkeypatch.setenv("PATH", f"{path}:{os.environ['PATH']}")
    task = AgentTask(system="s", prompt="p", schema=Answer, timeout_s=10)
    assert asyncio.run(CliRunner(["claude-code", "none"]).run(task)).backend == "none"


def test_failed_model_run_is_not_retried_for_ordinary_words(tmp_path, monkeypatch):
    count = tmp_path / "count"
    body = (
        f"cat >/dev/null\necho 1 >> {count}\n"
        'echo \'{"type":"result","subtype":"error_max_turns","is_error":true,'
        '"result":"Capacity rose 529 million after unauthorized trading losses; Region EMEA guidance not supported by the quoted page"}\'\nexit 1\n'
    )
    path = fake_claude(tmp_path / "bin", body)
    monkeypatch.setenv("PATH", f"{path}:{os.environ['PATH']}")
    task = AgentTask(system="s", prompt="p", schema=Answer, timeout_s=10)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(CliRunner(["claude-code", "none"]).run(task))
    assert not isinstance(failure.value, NotStarted)
    assert count.read_text().split() == ["1"]


# --- CLI --------------------------------------------------------------------


def test_health_fails_on_a_corrupt_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EARNINGS_WATCH_DATA_DIR", str(tmp_path))
    assert main(["init"]) == 0
    db = sqlite3.connect(tmp_path / "earnings.db")
    db.execute("INSERT INTO events VALUES('e1','k','2026-11-05',1,'f','{}')")
    db.commit()
    db.execute("PRAGMA writable_schema=ON")
    db.execute(
        "UPDATE sqlite_master SET sql=replace(sql,'(date','(fingerprint') WHERE name='events_date'"
    )
    db.commit()
    db.close()
    capsys.readouterr()
    assert main(["health"]) == 1
    assert json.loads(capsys.readouterr().out)["integrity"] != "ok"


def test_cli_upcoming_pages_with_a_cursor(seeded, monkeypatch, capsys):
    catalogue = Catalogue(seeded.db)
    catalogue.put_source(source(key="beta", ticker="BETA"))
    catalogue.commit_issuer(
        "beta", [event(key="beta", ticker="BETA", id="earnings:beta:2026Q4")], run_id="b"
    )
    service = open(seeded)
    service.watch("ALPHA")
    service.watch("BETA")
    monkeypatch.setenv("EARNINGS_WATCH_DATA_DIR", str(seeded.data_dir))
    assert main(["upcoming", "--limit", "1", "--json"]) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["cursor"] and len(first["events"]) == 1
    assert main(["upcoming", "--limit", "1", "--json", "--cursor", first["cursor"]]) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["events"] and second["events"] != first["events"]
    assert main(["upcoming", "--ticker", "BETA", "--json"]) == 0
    only = json.loads(capsys.readouterr().out)
    assert {e["ticker"] for e in only["events"]} == {"BETA"}


def test_cli_reports_missing_database_and_bad_source_files(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EARNINGS_WATCH_DATA_DIR", str(tmp_path / "state"))
    assert main(["status"]) == 3
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "temporarily_unavailable"
    for name, text in (("empty.yaml", ""), ("bad.yaml", "sources: [\n")):
        path = tmp_path / name
        path.write_text(text)
        assert main(["sources", "validate", str(path)]) == 1
        error = json.loads(capsys.readouterr().err)["error"]
        assert error["code"] == "invalid_argument" and not re.search("Traceback", error["message"])


# --- model endpoints ----------------------------------------------------------


def test_claude_retry_bound_reaches_the_model_process(monkeypatch):
    from earnings_watch.llm.runner import environment

    monkeypatch.setenv("CLAUDE_CODE_MAX_RETRIES", "1")
    monkeypatch.setenv("UNRELATED_SECRET", "x")
    env = environment("claude-code")
    assert env["CLAUDE_CODE_MAX_RETRIES"] == "1" and "UNRELATED_SECRET" not in env


def test_codex_endpoint_keeps_isolation_and_passes_only_its_key(tmp_path, monkeypatch):
    from earnings_watch.llm.runner import command, environment

    monkeypatch.setenv("HUB_KEY", "k")
    monkeypatch.setenv("OTHER_KEY", "x")
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    task = AgentTask(system="s", prompt="p", schema=Answer)
    endpoint = ("https://hub.example/v1", "HUB_KEY")
    cmd = command("codex", task, schema, tmp_path, None, endpoint)
    assert "--ignore-user-config" in cmd and "--strict-config" in cmd
    assert 'model_provider="earnings_endpoint"' in cmd
    provider = next(c for c in cmd if c.startswith("model_providers.earnings_endpoint="))
    assert 'base_url="https://hub.example/v1"' in provider and 'env_key="HUB_KEY"' in provider
    assert "request_max_retries=2" in provider
    assert environment("codex", endpoint)["HUB_KEY"] == "k"
    assert "HUB_KEY" not in environment("codex") and "OTHER_KEY" not in environment(
        "codex", endpoint
    )
    assert not any("model_provider" in c for c in command("codex", task, schema, tmp_path))


def test_codex_endpoint_settings_are_validated(tmp_path):
    from earnings_watch.settings import Settings

    settings = Settings(
        data_dir=tmp_path, codex_base_url="https://hub.example/v1/", codex_key_env="HUB_KEY"
    )
    assert settings.codex_endpoint == ("https://hub.example/v1", "HUB_KEY")
    assert Settings(data_dir=tmp_path).codex_endpoint is None
    for values in (
        {"codex_base_url": "https://hub.example/v1"},
        {"codex_key_env": "HUB_KEY"},
        {"codex_base_url": "file:///etc/passwd", "codex_key_env": "HUB_KEY"},
        {"codex_base_url": "https:///", "codex_key_env": "HUB_KEY"},
        {"codex_base_url": "https://hub.example/v1", "codex_key_env": "bad-name"},
    ):
        with pytest.raises(ValueError):
            Settings(data_dir=tmp_path, **values)


def fake_codex(directory, events, then="exit 1"):
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "codex"
    lines = "".join(f"echo '{json.dumps(event)}'\n" for event in events)
    script.write_text(
        "#!/bin/sh\n"
        'case "$1" in --version) echo "codex-cli 0.166.0"; exit 0;; esac\n'
        'if [ "$2" = "--help" ]; then '
        'echo "--ignore-user-config --ignore-rules --strict-config --output-schema"; exit 0; fi\n'
        "cat >/dev/null\n" + lines + then + "\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return directory


NOTICE = {"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": "notice"}}
WAITING = {
    "type": "error",
    "message": "Reconnecting... waiting for network (Connection failed: error sending request)",
}
FAILED_503 = {
    "type": "turn.failed",
    "error": {"message": "unexpected status 503 Service Unavailable: Unknown error"},
}


@pytest.mark.parametrize(
    "events, then",
    [
        ([NOTICE, {"type": "turn.started"}, WAITING, WAITING], "exec sleep 30"),
        ([NOTICE, {"type": "turn.started"}, FAILED_503], "exit 1"),
    ],
)
def test_unreachable_codex_endpoint_falls_back_promptly(tmp_path, monkeypatch, events, then):
    path = fake_codex(tmp_path / "bin", events, then)
    monkeypatch.setenv("PATH", f"{path}:{os.environ['PATH']}")
    monkeypatch.setenv("HUB_KEY", "k")
    task = AgentTask(system="s", prompt="p", schema=Answer, timeout_s=20)
    runner = CliRunner(["codex", "none"], ("https://hub.example/v1", "HUB_KEY"))
    started = time.monotonic()
    assert asyncio.run(runner.run(task)).backend == "none"
    assert time.monotonic() - started < 10


RETRY_503 = {"type": "error", "message": "Reconnecting... 1/2 (unexpected status 503 Unknown)"}


@pytest.mark.parametrize("retry", [[], [RETRY_503]])
def test_codex_failure_after_output_is_not_repeated(tmp_path, monkeypatch, retry):
    output = {"type": "item.completed", "item": {"id": "item_1", "type": "agent_message"}}
    events = [{"type": "turn.started"}, *retry, output, FAILED_503]
    path = fake_codex(tmp_path / "bin", events)
    monkeypatch.setenv("PATH", f"{path}:{os.environ['PATH']}")
    task = AgentTask(system="s", prompt="p", schema=Answer, timeout_s=10)
    with pytest.raises(RuntimeError) as failure:
        asyncio.run(CliRunner(["codex", "none"]).run(task))
    assert not isinstance(failure.value, NotStarted)


def test_codex_watch_counts_long_turn_output_lines():
    from earnings_watch.llm.runner import _CodexWatch

    class Process:
        pid, returncode = -1, 0

    watch = _CodexWatch(Process())  # type: ignore[arg-type]
    watch.feed(b'{"type":"turn.started"}\n')
    for _ in range(5):
        watch.feed(b'{"type":"item.completed","item":{"text":"' + b"x" * 65536)
    watch.feed(b'"}}\n{"type":"turn.failed","error":{"message":"unexpected status 503"}}\n')
    assert watch.started and not watch.not_started
