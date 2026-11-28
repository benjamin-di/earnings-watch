"""Fail-first regressions for sweep reliability, repair resilience, retry scoping, circuit breaker, and stale errors."""

from __future__ import annotations

import socket
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from earnings_watch import open as open_service
from earnings_watch.llm.runner import AgentResult, AgentTask
from earnings_watch.maintenance import Maintenance
from earnings_watch.model.events import Event, EventStatus, EventType, ExtractClaim, ExtractResult
from earnings_watch.model.sources import FetchStrategy, PeriodScheme, Source
from earnings_watch.settings import Settings
from earnings_watch.store.database import Catalogue, PersonalStore, add_person, initialize
from earnings_watch.transport import Transport
from earnings_watch.worker import sweep_due


def _src(key: str, ticker: str, *, fetch: FetchStrategy = FetchStrategy.WEBSEARCH) -> Source:
    return Source(
        key=key,
        issuer=f"{ticker} Inc",
        ticker=ticker,
        name=f"{ticker} Inc",
        exchange="XNYS",
        calendar_url=f"https://ir.{key}.example/events",
        fetch=fetch,
        period_scheme=PeriodScheme.CALENDAR_Q,
        fy_end_month=12,
    )


@pytest.mark.asyncio
async def test_repair_rejected_failure_preserves_verified_events_and_skips_covered_claims(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path, max_calls_per_run=10)
    initialize(settings.db)
    cat = Catalogue(settings.db)
    src = _src("acme", "ACME", fetch=FetchStrategy.PLAIN)
    cat.put_source(src)
    add_person(settings.db, "owner")
    PersonalStore(settings.db, "owner").watch("ACME", "XNYS")

    seen_at = datetime(2026, 11, 20, 0, 0, tzinfo=timezone.utc)
    cat.commit_issuer(
        "acme",
        [
            Event(
                id="cmd:acme:2026",
                issuer_key="acme",
                ticker="ACME",
                type=EventType.CMD,
                period="2026",
                date=date(2026, 12, 8),
                status=EventStatus.IR_CONFIRMED,
                source_url="https://ir.acme.example/events",
                evidence="Investor Day December 8, 2026",
                first_seen=seen_at,
                last_verified=seen_at,
            )
        ],
        run_id="seed",
        authoritative=False,
    )

    page = "Acme Corp Investor Relations Calendar — Q4 2026 Earnings Release on 2027-01-28."
    transport = Transport(
        {"ir.acme.example"},
        resolver=lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
        mock=httpx.MockTransport(
            lambda r: httpx.Response(
                200, text=f"<html><body>{page}</body></html>", headers={"content-type": "text/html"}
            )
        ),
        pace=0,
    )

    repair_calls: list[AgentTask] = []

    class FlakyRepairRunner:
        async def run(self, task: AgentTask, **kwargs) -> AgentResult:
            if task.kind == "json":
                return AgentResult(
                    ExtractResult(
                        page_relevant=True,
                        complete=True,
                        events=[
                            ExtractClaim(
                                type=EventType.EARNINGS,
                                period="2026Q4",
                                date=date(2027, 1, 28),
                                evidence=page,
                            ),
                            # Uncovered rejected claim -> triggers repair_rejected
                            ExtractClaim(
                                type=EventType.AGM,
                                period="2027",
                                date=date(2027, 5, 10),
                                evidence="paraphrased not on page",
                            ),
                        ],
                    ),
                    "fake",
                )
            repair_calls.append(task)
            assert task.allow_repair is True
            raise RuntimeError("model output failed schema validation")

    res = await Maintenance(settings, runner=FlakyRepairRunner(), transport=transport).sweep(
        force=True
    )
    # Repair failed, but the issuer must NOT crash or discard the verified 2026Q4 earnings event.
    assert len(repair_calls) == 1
    assert repair_calls[0].schema is ExtractResult
    assert res["exit"] == 0
    assert not any(
        e.get("detail") == "model output failed schema validation" for e in res["errors"]
    )
    event_ids = {e["id"] for e in cat.events() if e["issuer_key"] == "acme"}
    assert "earnings:acme:2026Q4" in event_ids

    # Case B: only an already-covered rejection -> repair_rejected must NOT be called at all.
    repair_calls.clear()

    class CoveredOnlyRunner:
        async def run(self, task: AgentTask, **kwargs) -> AgentResult:
            if task.kind == "json":
                return AgentResult(
                    ExtractResult(
                        page_relevant=True,
                        complete=True,
                        events=[
                            ExtractClaim(
                                type=EventType.CMD,
                                period="2026",
                                date=date(2026, 12, 8),
                                evidence="not on page",
                            )
                        ],
                    ),
                    "fake",
                )
            repair_calls.append(task)
            raise AssertionError("repair_rejected should not run for already-covered claims")

    res2 = await Maintenance(settings, runner=CoveredOnlyRunner(), transport=transport).sweep(
        force=True
    )
    assert repair_calls == []
    assert res2["exit"] == 0


@pytest.mark.asyncio
async def test_failed_sweep_retries_only_failed_sources_and_backs_off(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, max_calls_per_run=20)
    initialize(settings.db)
    cat = Catalogue(settings.db)
    cat.put_source(_src("alpha", "ALPH"))
    cat.put_source(_src("beta", "BETA"))
    add_person(settings.db, "owner")
    store = PersonalStore(settings.db, "owner")
    store.watch("ALPH", "XNYS")
    store.watch("BETA", "XNYS")

    seen_prompts: list[str] = []
    fail_beta = True

    class SelectiveRunner:
        async def run(self, task: AgentTask, **kwargs) -> AgentResult:
            seen_prompts.append(task.prompt)
            if '"key": "beta"' in task.prompt and fail_beta:
                raise RuntimeError("model output failed schema validation")
            return AgentResult(
                ExtractResult(page_relevant=True, complete=True, events=[]),
                "fake",
            )

    class OfflineTransport:
        async def get(self, url: str, **kwargs):
            raise RuntimeError("offline")

    maint = Maintenance(settings, runner=SelectiveRunner(), transport=OfflineTransport())
    r1 = await maint.sweep()
    assert r1["exit"] == 3
    assert len(seen_prompts) == 2

    # Second sweep (non-forced retry): should ONLY run beta, NOT alpha.
    seen_prompts.clear()
    r2 = await maint.sweep()
    assert r2["exit"] == 3
    assert len(seen_prompts) == 1
    assert '"key": "beta"' in seen_prompts[0]

    # Repeated failed sweeps back off to 4h in sweep_due()
    status = cat.status()
    ended = datetime.fromisoformat(status["last_sweep"]["ended"])
    assert not sweep_due(status, ended + timedelta(minutes=90))
    assert sweep_due(status, ended + timedelta(minutes=245))

    # When beta recovers, retry clears exit=0.
    fail_beta = False
    seen_prompts.clear()
    r3 = await maint.sweep()
    assert r3["exit"] == 0
    assert len(seen_prompts) == 1


@pytest.mark.asyncio
async def test_sweep_circuit_breaker_aborts_after_three_consecutive_transport_failures(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path, max_calls_per_run=20)
    initialize(settings.db)
    cat = Catalogue(settings.db)
    add_person(settings.db, "owner")
    store = PersonalStore(settings.db, "owner")
    for idx in range(6):
        key = f"co{idx}"
        ticker = f"CO{idx}"
        cat.put_source(_src(key, ticker))
        store.watch(ticker, "XNYS")

    calls = 0

    class DownRunner:
        async def run(self, task: AgentTask, **kwargs) -> AgentResult:
            nonlocal calls
            calls += 1
            raise RuntimeError("model run failed")

    class OfflineTransport:
        async def get(self, url: str, **kwargs):
            raise RuntimeError("offline")

    res = await Maintenance(settings, runner=DownRunner(), transport=OfflineTransport()).sweep(
        force=True
    )
    assert res["exit"] == 3
    assert res.get("aborted") is True
    assert calls == 3


@pytest.mark.asyncio
async def test_stale_error_surfaces_in_coverage_warnings_after_three_failures_and_72h(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path, max_calls_per_run=10)
    initialize(settings.db)
    cat = Catalogue(settings.db)
    cat.put_source(_src("stuck", "STCK"))
    add_person(settings.db, "owner")
    PersonalStore(settings.db, "owner").watch("STCK", "XNYS")

    stale_ok = (datetime.now(timezone.utc) - timedelta(hours=80)).isoformat()
    cat.set_state(
        "fetch_state",
        "stuck",
        {
            "last_ok": stale_ok,
            "error": "RuntimeError",
            "error_detail": "model output failed schema validation",
            "consecutive_failures": 2,
        },
    )

    class FailingRunner:
        async def run(self, task: AgentTask, **kwargs) -> AgentResult:
            raise RuntimeError("model output failed schema validation")

    class OfflineTransport:
        async def get(self, url: str, **kwargs):
            raise RuntimeError("offline")

    await Maintenance(settings, runner=FailingRunner(), transport=OfflineTransport()).sweep(
        force=True
    )
    state = cat.state("fetch_state", "stuck")
    assert state["consecutive_failures"] == 3

    svc = open_service(settings, person="owner")
    warnings = svc.status()["coverage_warnings"]
    assert any(w["source_key"] == "stuck" and w["kind"] == "stale_error" for w in warnings)


@pytest.mark.asyncio
async def test_plain_source_auto_promotes_to_websearch_after_repeated_403_waf_block(
    tmp_path: Path,
):
    settings = Settings(data_dir=tmp_path, max_calls_per_run=10)
    initialize(settings.db)
    cat = Catalogue(settings.db)
    cat.put_source(_src("wafco", "WAFC", fetch=FetchStrategy.PLAIN))
    add_person(settings.db, "owner")
    PersonalStore(settings.db, "owner").watch("WAFC", "XNYS")

    class Waf403Transport:
        async def get(self, url: str, **kwargs):
            req = httpx.Request("GET", url)
            resp = httpx.Response(403, request=req)
            raise httpx.HTTPStatusError("Forbidden", request=req, response=resp)

    class DummyRunner:
        async def run(self, task: AgentTask, **kwargs) -> AgentResult:
            return AgentResult(None, "fake")

    maint = Maintenance(settings, runner=DummyRunner(), transport=Waf403Transport())
    # First 403 marks waf_blocked; second consecutive 403 auto-promotes fetch -> websearch.
    await maint.sweep(force=True)
    assert next(s for s in cat.sources() if s.key == "wafco").fetch == FetchStrategy.PLAIN
    await maint.sweep(force=True)
    assert next(s for s in cat.sources() if s.key == "wafco").fetch == FetchStrategy.WEBSEARCH
