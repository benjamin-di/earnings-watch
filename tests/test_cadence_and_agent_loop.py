"""Fail-first tests for Slice 3: apex domains, cadence projection, IR analyst loop, and coverage watchdog."""

from __future__ import annotations

import json
import socket
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastmcp import Client

from conftest import event, source
from earnings_watch import open as open_service
from earnings_watch.llm.runner import AgentResult, AgentTask
from earnings_watch.maintenance import Maintenance
from earnings_watch.model.events import EventStatus, EventType, ExtractClaim, ExtractResult
from earnings_watch.model.sources import PeriodScheme
from earnings_watch.store.database import Catalogue
from earnings_watch.transport import Transport


def _resolver(address: str = "93.184.216.34"):
    return lambda *a, **k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443))]


# --- 1. apex_domain & same_apex ---


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("ir.acme.example", "acme.example"),
        ("NEWSROOM.ACME.EXAMPLE.", "acme.example"),
        ("acme.example", "acme.example"),
        ("ir.synthetic.co.uk", "synthetic.co.uk"),
        ("media.synthetic.com.au", "synthetic.com.au"),
        ("ri.synthetic.com.br", "synthetic.com.br"),
        ("ir.synthetic.co.jp", "synthetic.co.jp"),
        ("ir.synthetic.co.kr", "synthetic.co.kr"),
        ("ir.synthetic.com.cn", "synthetic.com.cn"),
        ("ir.synthetic.org.uk", "synthetic.org.uk"),
        ("ir.synthetic.net.au", "synthetic.net.au"),
    ],
)
def test_apex_domain_handles_common_public_suffixes(host: str, expected: str):
    from earnings_watch.model.urls import apex_domain

    assert apex_domain(host) == expected


def test_same_apex_matches_siblings_and_refuses_different_domains():
    from earnings_watch.model.urls import same_apex

    assert same_apex("ir.acme.example", "newsroom.acme.example")
    assert same_apex("ir.acme.co.uk", "investors.acme.co.uk")
    assert not same_apex("ir.acme.co.uk", "ir.other.co.uk")
    assert not same_apex("ir.acme.example", "acme.other.example")
    assert not same_apex("", "acme.example")


# --- 2. Transport apex_domains ---


async def test_transport_apex_domains_allows_sibling_and_refuses_unrelated_or_private():
    transport = Transport(
        {"ir.issuer.example"},
        apex_domains={"issuer.example"},
        resolver=_resolver("93.184.216.34"),
        mock=httpx.MockTransport(lambda r: httpx.Response(200, text="sibling ok")),
        pace=0,
    )
    resp = await transport.get("https://newsroom.issuer.example/press-release")
    assert resp.status == 200 and resp.body == b"sibling ok"

    with pytest.raises(ValueError, match="allowlist"):
        await transport.get("https://unrelated.example/page")

    private_transport = Transport(
        {"ir.issuer.example"},
        apex_domains={"issuer.example"},
        resolver=_resolver("127.0.0.1"),
        mock=httpx.MockTransport(lambda r: httpx.Response(200, text="should not reach")),
        pace=0,
    )
    with pytest.raises(ValueError, match="non-public"):
        await private_transport.get("https://newsroom.issuer.example/internal")

    strict_transport = Transport(
        {"ir.issuer.example"},
        resolver=_resolver("93.184.216.34"),
        mock=httpx.MockTransport(lambda r: httpx.Response(200, text="strict")),
        pace=0,
    )
    with pytest.raises(ValueError, match="allowlist"):
        await strict_transport.get("https://newsroom.issuer.example/press-release")


# --- 3. Pure engine cadence projection ---


def test_project_expected_earnings_quarterly_and_iso_weekday_alignment():
    from earnings_watch.engine.cadence import project_expected_earnings

    src = source("alpha", "ALPHA")
    today = date(2026, 11, 27)
    prior_year_q4 = date(2026, 1, 29)  # Thursday
    past_q3 = event(
        "alpha",
        "ALPHA",
        period="2026Q3",
        id="earnings:alpha:2026Q3",
        date=date(2026, 10, 29),
        status=EventStatus.PAST,
    )
    projected = project_expected_earnings(
        src,
        [past_q3],
        today=today,
        historical_dates=[prior_year_q4],
    )
    assert projected is not None
    assert projected.status == EventStatus.EXPECTED
    assert projected.type == EventType.EARNINGS
    assert projected.period == "2026Q4"
    assert projected.id == "earnings:alpha:2026Q4"
    assert projected.date == date(2027, 1, 28)  # Same ISO weekday (Thursday), +364d
    assert projected.date.isoweekday() == prior_year_q4.isoweekday()
    assert projected.source_url == src.calendar_url
    assert "unconfirmed" in projected.evidence.lower()


def test_project_expected_earnings_half_year_and_suppressed_by_confirmed():
    from earnings_watch.engine.cadence import project_expected_earnings

    src = source("beta", "BETA").model_copy(update={"period_scheme": PeriodScheme.HALF_YEAR})
    today = date(2026, 11, 27)
    projected = project_expected_earnings(
        src,
        [],
        today=today,
        historical_dates=[date(2026, 8, 20)],
    )
    assert projected is not None
    assert projected.status == EventStatus.EXPECTED
    assert projected.date == date(2026, 8, 20) + timedelta(days=182)
    assert projected.period == "2026H2"

    confirmed_future = event(
        "beta",
        "BETA",
        period="2026H2",
        id="earnings:beta:2026H2",
        date=date(2027, 2, 25),
        status=EventStatus.IR_CONFIRMED,
    )
    assert (
        project_expected_earnings(
            src,
            [confirmed_future],
            today=today,
            historical_dates=[date(2026, 8, 20)],
        )
        is None
    )
    assert project_expected_earnings(src, [], today=today) is None


# --- 4. Research scope, checklist, research_server, and repair_rejected ---


async def test_research_events_sets_apex_scope_max_turns_and_accepts_sibling_captures():
    from earnings_watch import research

    src = source("alpha", "ALPHA")
    captured_tasks: list[AgentTask] = []
    captured_scope: dict = {}

    async def fake_run(task: AgentTask, *, research_config: Path | None = None):
        captured_tasks.append(task)
        assert research_config is not None
        scope = json.loads(research_config.read_text())
        captured_scope.update(scope)
        cap_dir = Path(scope["directory"])
        cap_dir.mkdir(parents=True, exist_ok=True)
        (cap_dir / "sibling.json").write_text(
            json.dumps(
                {
                    "url": "https://newsroom.issuer.example/q4-release",
                    "text": "Q4 2026 results on 2027-01-28",
                    "links": [],
                }
            )
        )
        return AgentResult(
            ExtractResult(page_relevant=True, complete=True, events=[]),
            "fake",
        )

    runner = AsyncMock()
    runner.run.side_effect = fake_run

    _, docs = await research.events(
        src,
        runner,
        existing_events=[{"period": "2026Q3", "date": "2026-10-29", "status": "past"}],
    )
    assert len(captured_tasks) == 1
    assert captured_tasks[0].max_turns == 10
    assert "Newsroom" in captured_tasks[0].system or "Press Releases" in captured_tasks[0].system
    assert "www.sec.gov" in captured_scope["hosts"]
    assert "www1.hkexnews.hk" in captured_scope["hosts"]
    assert "issuer.example" in captured_scope["apex_domains"]
    assert "https://newsroom.issuer.example/q4-release" in docs


async def test_research_server_allows_same_apex_observations_and_render_param(tmp_path):
    from earnings_watch.llm.research_server import create

    server = create(
        {
            "directory": str(tmp_path / "captures"),
            "hosts": ["ir.issuer.example"],
            "apex_domains": ["issuer.example"],
        }
    )
    async with Client(server) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert "render" in tools["open_page"].input_schema["properties"]
        res = await client.call_tool(
            "record_observation",
            {"url": "https://newsroom.issuer.example/release", "evidence": "Q4 2026 on 2027-01-28"},
        )
        assert res.data["recorded"] is True
        with pytest.raises(Exception):
            await client.call_tool(
                "record_observation",
                {"url": "https://evil.example/release", "evidence": "bad"},
            )


# --- 5. Maintenance: 1-turn rejection repair, rolling weekly audit, cadence projection ---


async def test_maintenance_repairs_rejected_claim_in_one_turn(seeded):
    service = open_service(seeded)
    service.watch("ALPHA")
    target_date = (date.today() + timedelta(days=20)).isoformat()
    page = f"Alpha Corp Q4 2026 financial results will be published on {target_date}."
    transport = Transport(
        {"issuer.example"},
        resolver=_resolver(),
        mock=httpx.MockTransport(
            lambda r: httpx.Response(200, text=page, headers={"content-type": "text/html"})
        ),
        pace=0,
    )
    calls: list[AgentTask] = []

    async def fake_run(task: AgentTask, *, research_config: Path | None = None):
        calls.append(task)
        if task.schema is ExtractResult and task.kind == "json":
            # First pass returns a paraphrased quote that verify_claims() will reject.
            return AgentResult(
                ExtractResult(
                    page_relevant=True,
                    complete=True,
                    events=[
                        ExtractClaim(
                            type=EventType.EARNINGS,
                            period="Q4 2026",
                            date=date.fromisoformat(target_date),
                            evidence="Paraphrased non-verbatim quote",
                        )
                    ],
                ),
                "fake",
            )
        if task.schema is ExtractResult and task.kind == "research":
            # Repair turn receives the exact rejection reason and returns the verbatim quote.
            assert "rejected" in task.prompt.lower()
            return AgentResult(
                ExtractResult(
                    page_relevant=True,
                    complete=True,
                    events=[
                        ExtractClaim(
                            type=EventType.EARNINGS,
                            period="Q4 2026",
                            date=date.fromisoformat(target_date),
                            evidence=page,
                            source_url="https://issuer.example/calendar",
                        )
                    ],
                ),
                "fake",
            )
        return AgentResult(task.schema.model_validate({"documents": []}), "fake")

    runner = AsyncMock()
    runner.run.side_effect = fake_run
    result = await Maintenance(seeded, runner=runner, transport=transport).sweep(force=True)
    assert result["exit"] == 0
    assert not any(e.get("reason") == "claims_rejected" for e in result["errors"])
    assert any(t.kind == "research" and t.schema is ExtractResult for t in calls)
    stored = service.get_events(["earnings:alpha:2026Q4"])["events"][0]
    assert stored["date"] == target_date


async def test_maintenance_rolling_weekly_audit_caps_per_sweep(settings):
    catalogue = Catalogue(settings.db)
    service = open_service(settings)
    stale_audit = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    future_date = date.today() + timedelta(days=60)
    page = f"Q4 2026 results will be published on {future_date.isoformat()}."
    hosts = set()
    for i in range(1, 15):
        key = f"co{i:02d}"
        ticker = f"CO{i:02d}"
        src = source(key, ticker).model_copy(
            update={"calendar_url": f"https://{key}.example/calendar"}
        )
        hosts.add(f"{key}.example")
        catalogue.put_source(src)
        catalogue.commit_issuer(
            key,
            [
                event(
                    key,
                    ticker,
                    period="2026Q4",
                    id=f"earnings:{key}:2026Q4",
                    date=future_date,
                )
            ],
            run_id=f"seed-{key}",
        )
        catalogue.set_state(
            "fetch_state",
            key,
            {"last_ok": stale_audit, "last_agent_audit": stale_audit, "rejected": []},
        )
        service.watch(ticker)

    transport = Transport(
        hosts,
        resolver=_resolver(),
        mock=httpx.MockTransport(
            lambda r: httpx.Response(200, text=page, headers={"content-type": "text/html"})
        ),
        pace=0,
    )
    research_calls = 0

    async def fake_run(task: AgentTask, *, research_config: Path | None = None):
        nonlocal research_calls
        if task.schema is ExtractResult and task.kind == "research":
            research_calls += 1
            return AgentResult(ExtractResult(page_relevant=True, complete=True, events=[]), "fake")
        if task.schema is ExtractResult:
            return AgentResult(
                ExtractResult(
                    page_relevant=True,
                    complete=True,
                    events=[
                        ExtractClaim(
                            type=EventType.EARNINGS,
                            period="Q4 2026",
                            date=future_date,
                            evidence=page,
                        )
                    ],
                ),
                "fake",
            )
        return AgentResult(task.schema.model_validate({"documents": []}), "fake")

    runner = AsyncMock()
    runner.run.side_effect = fake_run
    result = await Maintenance(settings, runner=runner, transport=transport).sweep(force=True)
    assert result["exit"] == 0
    # ceil(14 / 7) == 2 rolling weekly audits per sweep
    assert research_calls == 2


# --- 6. Coverage warnings in PersonService.status() & UI ---


def test_coverage_warnings_in_status_and_ui_rendering(settings):
    from earnings_watch.ui import hub_page, render_coverage_warnings

    catalogue = Catalogue(settings.db)
    service = open_service(settings)
    today = date.today()

    # 1. Source with 0 events
    catalogue.put_source(source("emptyco", "EMPTY"))
    service.watch("EMPTY")

    # 2. Source with expected earnings < 21d
    catalogue.put_source(source("soonco", "SOON"))
    catalogue.commit_issuer(
        "soonco",
        [
            event(
                "soonco",
                "SOON",
                period="2026Q4",
                id="earnings:soonco:2026Q4",
                date=today + timedelta(days=10),
                status=EventStatus.EXPECTED,
            )
        ],
        run_id="seed-soon",
    )
    service.watch("SOON")

    # 3. Source demoted > 48h and with recent claims_rejected
    catalogue.put_source(source("brokenco", "BRKN"))
    catalogue.commit_issuer(
        "brokenco",
        [
            event(
                "brokenco",
                "BRKN",
                period="2026Q4",
                id="earnings:brokenco:2026Q4",
                date=today + timedelta(days=40),
                status=EventStatus.IR_CONFIRMED,
            )
        ],
        run_id="seed-broken",
    )
    catalogue.set_state(
        "heal_state",
        "source:brokenco",
        {
            "state": "demoted",
            "demoted_at": (datetime.now(timezone.utc) - timedelta(hours=72)).isoformat(),
            "next_attempt": (datetime.now(timezone.utc) + timedelta(hours=12)).isoformat(),
        },
    )
    catalogue.set_state(
        "fetch_state",
        "brokenco",
        {
            "last_ok": datetime.now(timezone.utc).isoformat(),
            "rejected": ["evidence quote not found"],
            "rejected_details": [
                {
                    "type": "earnings",
                    "date": str(today + timedelta(days=40)),
                    "reason": "evidence quote not found",
                }
            ],
        },
    )
    service.watch("BRKN")

    status = service.status()
    warnings = status["coverage_warnings"]
    kinds_by_source = {(w["source_key"], w["kind"]) for w in warnings}
    assert ("emptyco", "no_events") in kinds_by_source
    assert ("soonco", "unconfirmed_soon") in kinds_by_source
    assert ("brokenco", "demoted") in kinds_by_source
    assert ("brokenco", "claims_rejected") in kinds_by_source

    html_fragment = render_coverage_warnings(warnings)
    assert "EMPTY" in html_fragment and "SOON" in html_fragment and "BRKN" in html_fragment
    full_page = hub_page("Upcoming", "<p>Body</p>", coverage_warnings=warnings)
    assert "coverage-warnings" in full_page


async def test_maintenance_projects_expected_and_triggers_layer2_within_45d(settings):
    catalogue = Catalogue(settings.db)
    service = open_service(settings)
    src = source("cadenceco", "CADC")
    catalogue.put_source(src)
    # Past earnings 65 days ago -> next quarterly expected in ~26 days (<= 45d)
    past_date = date.today() - timedelta(days=65)
    catalogue.commit_issuer(
        "cadenceco",
        [
            event(
                "cadenceco",
                "CADC",
                period="2026Q3",
                id="earnings:cadenceco:2026Q3",
                date=past_date,
                status=EventStatus.PAST,
            )
        ],
        run_id="seed-past",
    )
    service.watch("CADC")

    transport = Transport(
        {"issuer.example"},
        resolver=_resolver(),
        mock=httpx.MockTransport(
            lambda r: httpx.Response(
                200, text="No upcoming dates announced yet.", headers={"content-type": "text/html"}
            )
        ),
        pace=0,
    )
    research_triggered = False

    async def fake_run(task: AgentTask, *, research_config: Path | None = None):
        nonlocal research_triggered
        if task.schema is ExtractResult and task.kind == "research":
            research_triggered = True
            return AgentResult(ExtractResult(page_relevant=True, complete=True, events=[]), "fake")
        if task.schema is ExtractResult:
            return AgentResult(ExtractResult(page_relevant=True, complete=True, events=[]), "fake")
        return AgentResult(task.schema.model_validate({"documents": []}), "fake")

    runner = AsyncMock()
    runner.run.side_effect = fake_run
    result = await Maintenance(settings, runner=runner, transport=transport).sweep(force=True)
    assert result["exit"] == 0
    assert research_triggered is True
    events = [ev for ev in catalogue.events() if ev["issuer_key"] == "cadenceco"]
    expected_events = [ev for ev in events if ev["status"] == "expected"]
    assert len(expected_events) == 1
    assert expected_events[0]["date"] == (past_date + timedelta(days=91)).isoformat()


async def test_maintenance_caps_rejected_details_at_five(seeded):
    service = open_service(seeded)
    service.watch("ALPHA")
    transport = Transport(
        {"issuer.example"},
        resolver=_resolver(),
        mock=httpx.MockTransport(
            lambda r: httpx.Response(200, text="Empty page", headers={"content-type": "text/html"})
        ),
        pace=0,
    )
    bad_claims = [
        ExtractClaim(
            type=EventType.EARNINGS,
            period=f"Q{(i % 4) + 1} 2027",
            date=date.today() + timedelta(days=30 + i),
            evidence=f"Unverified quote {i}",
        )
        for i in range(7)
    ]

    async def fake_run(task: AgentTask, *, research_config: Path | None = None):
        if task.schema is ExtractResult:
            return AgentResult(
                ExtractResult(page_relevant=True, complete=True, events=bad_claims),
                "fake",
            )
        return AgentResult(task.schema.model_validate({"documents": []}), "fake")

    runner = AsyncMock()
    runner.run.side_effect = fake_run
    await Maintenance(seeded, runner=runner, transport=transport).sweep(force=True)
    fstate = Catalogue(seeded.db).state("fetch_state", "alpha")
    assert len(fstate["rejected_details"]) == 5
    assert all({"type", "date", "reason"} <= set(d) for d in fstate["rejected_details"])


async def test_heal_records_demoted_at_timestamp(seeded):
    from earnings_watch.heal import repair

    catalogue = Catalogue(seeded.db)
    src = source("alpha", "ALPHA")
    runner = AsyncMock()
    runner.run.return_value = AgentResult(None, "fake")
    ok = await repair(src, catalogue, runner)
    assert ok is False
    hstate = catalogue.state("heal_state", "source:alpha")
    assert hstate["state"] == "demoted"
    assert "demoted_at" in hstate
