import socket
from datetime import date
from unittest.mock import AsyncMock
import httpx
import pytest
from earnings_watch import open
from earnings_watch.documents import download, verify_link
from earnings_watch.llm.runner import AgentResult, AgentTask, command, environment
from earnings_watch.maintenance import Maintenance, maintenance_lock
from earnings_watch.model.events import ExtractResult
from earnings_watch.outbox import deliver, enqueue
from earnings_watch.store.database import Catalogue
from earnings_watch.transport import Transport
from conftest import event, source


def transport(body):
    return Transport(
        {"issuer.example"},
        resolver=lambda *a, **k: [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))
        ],
        mock=httpx.MockTransport(
            lambda r: httpx.Response(200, text=body, headers={"content-type": "text/html"})
        ),
        pace=0,
    )


async def test_maintenance_verifies_and_dry_run_does_not_write(seeded):
    service = open(seeded)
    service.watch("ALPHA")
    page = f"Q4 2026 results will be published on {date.today().isoformat()}."
    runner = AsyncMock()
    runner.run.return_value = AgentResult(
        ExtractResult(
            page_relevant=True,
            complete=True,
            events=[dict(type="earnings", period="Q4 2026", date=str(date.today()), evidence=page)],
        ),
        "fake",
    )
    extraction = runner.run.return_value

    async def run_task(task, **kwargs):
        return (
            AgentResult(task.schema.model_validate({"documents": []}), "fake")
            if task.kind == "research"
            else extraction
        )

    runner.run.side_effect = run_task
    maintenance = Maintenance(seeded, runner=runner, transport=transport(page))
    before = service.get_events(["earnings:alpha:2026Q4"])
    result = await maintenance.sweep(dry_run=True)
    assert result["exit"] == 0, result
    assert service.get_events(["earnings:alpha:2026Q4"]) == before
    result = await maintenance.sweep()
    assert result["exit"] == 0, result
    assert service.get_events(["earnings:alpha:2026Q4"])["events"][0]["date"] == str(date.today())


async def test_kill_switch_lock_and_none_backend(seeded):
    open(seeded).watch("ALPHA")
    maintenance = Maintenance(seeded, transport=transport("No verified extraction"))
    with maintenance_lock(seeded):
        with pytest.raises(Exception, match="already running"):
            await maintenance.sweep()
    (seeded.data_dir / ".paused").touch()
    assert (await maintenance.sweep())["state"] == "paused"
    (seeded.data_dir / ".paused").unlink()
    result = await maintenance.sweep()
    assert result["exit"] == 0 and not result["errors"]
    assert result["counts"]["calls"] == 0
    assert Catalogue(seeded.db).events()


async def test_documents_verify_period_and_download_idempotently(seeded, tmp_path):
    src, row = source(), event().model_dump(mode="json")
    link = {
        "url": "https://issuer.example/release.pdf",
        "title": "Q4 2026 earnings results",
        "role": "release",
        "evidence": "Q4 2026 earnings results",
    }
    doc = verify_link(src, row, link, link["evidence"])
    assert doc["verified"]
    bad = verify_link(
        src,
        row,
        link | {"title": "Q2 2026 results", "evidence": "Q2 2026 results"},
        "Q2 2026 results",
    )
    assert not bad["verified"]
    folder = tmp_path / "downloads"
    first = await download(doc, transport=transport("document"), folder=folder)
    stamp = first.stat().st_mtime_ns
    second = await download(doc, transport=transport("document"), folder=folder)
    assert first == second and second.stat().st_mtime_ns == stamp
    with pytest.raises(ValueError, match="outside"):
        await download(
            doc, transport=transport("document"), folder=folder, root=tmp_path / "elsewhere"
        )
    with pytest.raises(ValueError, match="unverified"):
        await download(bad, transport=transport("document"), folder=folder)


async def test_outbox_does_not_replay_an_unknown_effect(seeded):
    notifier = AsyncMock()
    notifier.send.side_effect = TimeoutError("outcome unknown")
    enqueue(seeded.db, "alice", "one", "change", {"event": "synthetic"})
    assert (await deliver(seeded.db, "alice", notifier))["unknown"] == ["one"]
    assert not (await deliver(seeded.db, "alice", notifier))["unknown"]
    assert notifier.send.await_count == 1
    assert not (await deliver(seeded.db, "bob", notifier))["sent"]


def test_cli_runners_disable_ambient_capabilities(tmp_path, monkeypatch):
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    task = AgentTask(system="system", prompt="one issuer", schema=ExtractResult)
    cmd = command("codex", task, schema, tmp_path)
    assert "--ignore-user-config" in cmd and "--ignore-rules" in cmd
    assert "features.shell_tool=false" in cmd and "features.multi_agent=false" in cmd
    assert 'web_search="disabled"' in cmd
    cmd = command("claude-code", task, schema, tmp_path)
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd and "--setting-sources" in cmd
    monkeypatch.setenv("UNRELATED_SECRET", "private")
    assert "UNRELATED_SECRET" not in environment("codex")


async def test_download_ledger_and_bounded_blob_cache_are_personal(seeded, tmp_path):
    from earnings_watch.store.database import connect

    doc = verify_link(
        source(),
        event().model_dump(mode="json"),
        {
            "url": "https://issuer.example/results",
            "role": "release",
            "title": "Q4 2026 earnings results",
            "evidence": "Q4 2026 earnings results",
        },
        "Q4 2026 earnings results",
    )
    catalogue = Catalogue(seeded.db)
    catalogue.put_document(doc)
    first = await download(
        doc,
        transport=transport("first revision"),
        folder=tmp_path / "downloads",
        catalogue=catalogue,
        person="alice",
        cache_dir=tmp_path / "cache",
        cache_max_bytes=20,
    )
    second = await download(
        doc,
        transport=transport("second revision"),
        folder=tmp_path / "downloads",
        catalogue=catalogue,
        person="alice",
        cache_dir=tmp_path / "cache",
        cache_max_bytes=20,
    )
    assert first != second and first.exists() and second.exists()
    assert len(list((tmp_path / "cache").iterdir())) == 1
    catalogue.put_document(doc)
    assert catalogue.documents()[0]["sha256"]
    with connect(seeded.db) as db:
        rows = list(db.execute("SELECT person,path,sha256 FROM document_downloads"))
        assert len(rows) == 1 and rows[0]["person"] == "alice"
        assert rows[0]["path"] == str(second)
        assert db.execute("SELECT sum(size) FROM blobs").fetchone()[0] <= 20


@pytest.mark.parametrize("captured", [True, False])
async def test_websearch_verifies_saved_pages_and_never_claims_complete_catalogue(seeded, captured):
    import json
    from pathlib import Path
    from earnings_watch.model.sources import FetchStrategy

    open(seeded).watch("ALPHA")
    catalogue = Catalogue(seeded.db)
    catalogue.put_source(source().model_copy(update={"fetch": FetchStrategy.WEBSEARCH}))
    quote = f"Q3 2026 results will be published on {date.today().isoformat()}."
    url = "https://issuer.example/announcement"
    runner = AsyncMock()

    async def run_task(task, research_config=None):
        if task.schema is not ExtractResult:
            return AgentResult(task.schema.model_validate({"documents": []}), "fake")
        assert task.kind == "research" and research_config is not None
        if captured:
            config = json.loads(research_config.read_text())
            directory = Path(config["directory"])
            directory.mkdir()
            (directory / "page.json").write_text(json.dumps({"url": url, "text": quote}))
        return AgentResult(
            ExtractResult(
                page_relevant=True,
                complete=True,
                events=[
                    {
                        "type": "earnings",
                        "period": "Q3 2026",
                        "date": str(date.today()),
                        "evidence": quote,
                        "source_url": url,
                    }
                ],
            ),
            "fake",
        )

    runner.run.side_effect = run_task
    before = catalogue.events()[0]["date"]
    result = await Maintenance(seeded, runner=runner).sweep()
    assert bool(result["errors"]) is not captured
    assert next(e for e in catalogue.events() if e["period"] == "2026Q4")["date"] == before
    assert len(catalogue.events()) == (2 if captured else 1)
