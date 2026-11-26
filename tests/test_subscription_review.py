"""Regressions for notification progress, current interests and account removal."""

import base64
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from conftest import event, source
from test_connections import browser, Directory
from test_subscriptions import subscribe, put_doc
from earnings_watch import open
from earnings_watch.cli import execute, parser
from earnings_watch.connections import create_app
from earnings_watch.notify import notices, prepare
from earnings_watch.outbox import deliver, reconcile
from earnings_watch.store.database import Catalogue, connect, sync_holdings, digest


class Recorder:
    def __init__(self):
        self.calls = []

    async def send(self, *args):
        self.calls.append(args)
        return "synthetic-receipt"


async def test_document_only_brief_sends_and_unfollowed_documents_are_filtered(seeded):
    subscribe(seeded, results=False, brief=True, hour=0)
    catalogue = Catalogue(seeded.db)
    catalogue.commit_issuer(
        "alpha", [event(date=datetime.now(timezone.utc).date() - timedelta(days=2))], run_id="past"
    )
    catalogue.put_source(source("beta", "BETA"))
    catalogue.commit_issuer(
        "beta",
        [event("beta", "BETA", date=datetime.now(timezone.utc).date() - timedelta(days=2))],
        run_id="beta",
    )
    open(seeded, "alice").watch("BETA")
    catalogue.put_document(
        dict(
            id="beta-statement",
            event_id=event("beta", "BETA").id,
            url="https://issuer.example/beta.pdf",
            title="Beta statement",
            role="statements",
            verified=True,
            sha256="",
            first_seen=datetime.now(timezone.utc).isoformat(),
        )
    )
    with connect(seeded.db, write=True) as db:
        db.execute(
            "UPDATE people SET notify_seq=(SELECT max(seq) FROM event_revisions) WHERE id='alice'"
        )
    put_doc(seeded, "alpha-statement", role="statements")
    prepare(seeded)
    open(seeded, "alice").unwatch("BETA")
    sender = Recorder()
    assert len((await deliver(seeded.db, "alice", sender))["sent"]) == 1
    assert not sender.calls[0][3]["events"] and not sender.calls[0][3]["changes"]
    assert [d["id"] for d in sender.calls[0][3]["documents"]] == ["alpha-statement"]


async def test_prose_and_successful_brief_cannot_consume_urgent_transition(seeded):
    subscribe(seeded, results=False, brief=True, urgent=True, hour=0)
    catalogue = Catalogue(seeded.db)
    moved = event(date=datetime.now(timezone.utc).date() + timedelta(days=3))
    catalogue.commit_issuer("alpha", [moved], run_id="move")
    at = datetime.now(timezone.utc) + timedelta(minutes=11)
    before = [n for n in notices(seeded, "alice", at) if n[1] == "urgent"]
    assert len(before) == 1
    catalogue.commit_issuer(
        "alpha", [moved.model_copy(update={"evidence": "Fresh wording"})], run_id="prose"
    )
    assert [n[0] for n in notices(seeded, "alice", at) if n[1] == "urgent"] == [before[0][0]]
    prepare(seeded)
    sender = Recorder()
    assert len((await deliver(seeded.db, "alice", sender))["sent"]) == 1
    assert sender.calls[0][2] == "brief"
    assert [n[0] for n in notices(seeded, "alice", at) if n[1] == "urgent"] == [before[0][0]]


def test_account_removal_erases_subject_namespaces_and_preserves_other_people(seeded):
    sync_holdings(seeded.db, "alice", "portfolio", [{"ticker": "ALPHA", "exchange": "XNAS"}])
    with connect(seeded.db, write=True) as db:
        for name in [
            "delivery-review:alice:results:alpha",
            "delivery-review:alice2:results:alpha",
            "holdings:bob:portfolio",
        ]:
            db.execute("INSERT INTO metadata VALUES(?,?)", (name, "synthetic-private"))
    execute(parser().parse_args(["users", "remove", "alice"]), seeded)
    with connect(seeded.db) as db:
        keys = {r[0] for r in db.execute("SELECT key FROM metadata")}
    assert "holdings:alice:portfolio" not in keys
    assert "delivery-review:alice:results:alpha" not in keys
    assert {
        "blocked:alice",
        "delivery-review:alice2:results:alpha",
        "holdings:bob:portfolio",
    } <= keys


async def test_monitor_is_compact_even_with_large_run_detail(seeded, monkeypatch):
    from earnings_watch.http_server import AuthenticatedApp

    monkeypatch.setenv("BACKEND", "b" * 40)
    seeded.backend_tokens_env = ["BACKEND"]
    Catalogue(seeded.db).run("large", "sweep", {"padding": "x" * 200000}, exit_code=0)
    app = AuthenticatedApp(seeded, "gateway", directory=Directory())
    async with (
        app.app.router.lifespan_context(app.app),
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost"
        ) as client,
    ):
        response = await client.post(
            "/monitor/mcp",
            json=dict(
                jsonrpc="2.0",
                id=1,
                method="tools/call",
                params=dict(name="earnings_status", arguments={}),
            ),
            headers={
                "Authorization": "Bearer " + "b" * 40,
                "Accept": "application/json, text/event-stream",
            },
        )
    assert response.status_code == 200
    assert not response.json()["result"].get("isError")
    assert len(response.content) < 8192 and "padding" not in response.text


async def test_copy_uses_only_the_allowed_connections_script(seeded):
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
        response = await client.get("/connections/?view=calendar")
    assert 'data-copy-code="calendar-link"' in response.text
    scripts = re.findall(r"<script>(.*?)</script>", response.text, re.S)
    assert scripts
    csp = response.headers["content-security-policy"]
    for script in scripts:
        needed = "sha256-" + base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
        assert needed in csp


async def test_native_reconciliation_proof_is_person_key_and_attempt_bound(seeded):
    from earnings_watch.errors import EarningsError

    subscribe(seeded)
    put_doc(seeded, "first")
    prepare(seeded)

    class Uncertain:
        async def send(self, *args):
            raise TimeoutError()

    key = (await deliver(seeded.db, "alice", Uncertain()))["unknown"][0]
    with connect(seeded.db) as db:
        row = db.execute(
            "SELECT payload,attempts FROM deliveries WHERE person='alice' AND key=?", (key,)
        ).fetchone()
    proof = dict(
        person="alice",
        key=key,
        attempt=row[1],
        outcome="not_submitted",
        evidence="Synthetic proof",
        payload_sha256=digest(json.loads(row[0])),
    )
    with pytest.raises(EarningsError):
        reconcile(seeded.db, "alice", key, proof | dict(person="bob"))
    reconcile(seeded.db, "alice", key, proof)
    assert (await deliver(seeded.db, "alice", Uncertain()))["unknown"] == [key]
    with pytest.raises(EarningsError):
        reconcile(seeded.db, "alice", key, proof)


async def test_operator_reconciliation_refuses_an_inflight_native_send(seeded):
    from earnings_watch.outbox import delivery_lock
    from earnings_watch.errors import EarningsError

    subscribe(seeded)
    put_doc(seeded, "first")
    prepare(seeded)

    class Uncertain:
        async def send(self, *args):
            raise TimeoutError()

    key = (await deliver(seeded.db, "alice", Uncertain()))["unknown"][0]
    with delivery_lock(seeded.db, "alice"):
        with pytest.raises(EarningsError, match="still running"):
            reconcile(seeded.db, "alice", key, {})


def test_pre_cutoff_release_prevents_initial_notice_for_later_mirror_document(seeded):
    subscribe(seeded)
    cutoff = datetime.now(timezone.utc)
    with connect(seeded.db, write=True) as db:
        db.execute(
            "INSERT OR REPLACE INTO metadata VALUES(?,?)",
            ("baseline:alice", cutoff.isoformat()),
        )
    # Pre-cutoff verified release using trailing Z timestamp format.
    pre_cutoff = (cutoff - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    Catalogue(seeded.db).put_document(
        dict(
            id="alpha-sec-release",
            event_id=event().id,
            url="https://issuer.example/sec-release.htm",
            title="Alpha Q3 Release",
            role="release",
            verified=True,
            sha256="",
            first_seen=pre_cutoff,
        )
    )
    # Post-cutoff mirror release on issuer newsroom must not trigger a fresh :initial notice.
    put_doc(seeded, "alpha-ir-mirror", role="release", at=cutoff + timedelta(minutes=30))
    assert not [n for n in notices(seeded, "alice") if n[1] == "results"]

    # Another event with only a pre-cutoff non-release document still fires :initial once its release arrives.
    catalogue = Catalogue(seeded.db)
    catalogue.put_source(source("beta", "BETA"))
    beta_event = event("beta", "BETA")
    catalogue.commit_issuer("beta", [beta_event], run_id="beta")
    open(seeded, "alice").watch("BETA")
    catalogue.put_document(
        dict(
            id="beta-pre-slides",
            event_id=beta_event.id,
            url="https://issuer.example/beta-slides.pdf",
            title="Beta Slides",
            role="presentation",
            verified=True,
            sha256="",
            first_seen=pre_cutoff,
        )
    )
    catalogue.put_document(
        dict(
            id="beta-release",
            event_id=beta_event.id,
            url="https://issuer.example/beta-release.pdf",
            title="Beta Release",
            role="release",
            verified=True,
            sha256="",
            first_seen=(cutoff + timedelta(minutes=30)).isoformat(),
        )
    )
    result_keys = [n[0] for n in notices(seeded, "alice") if n[1] == "results"]
    assert result_keys == [f"results:{beta_event.id}:initial"]


async def test_archive_effects_suppresses_legacy_emailed_results_without_claiming_sent(
    tmp_path,
):
    import yaml
    from earnings_watch.legacy import import_snapshot, review
    from earnings_watch.settings import Settings
    from earnings_watch.store.accounts import archive_effects, provision
    from earnings_watch.store.database import initialize

    settings = Settings(data_dir=tmp_path / "state")
    initialize(settings.db)
    provision(settings.db, "alice", "Alice")
    legacy_dir = tmp_path / "legacy"
    legacy_dir.mkdir()
    (legacy_dir / "ir-sources.yaml").write_text(
        yaml.safe_dump({"sources": [source("alpha", "ALPHA").model_dump(mode="json")]})
    )
    emailed_event = event().model_dump(mode="json") | {
        "status": "past",
        "results_emailed_at": "2026-11-17T21:29:03Z",
        "results_links": [],
    }
    (legacy_dir / "events.yaml").write_text(yaml.safe_dump({"events": [emailed_event]}))
    import_snapshot(settings.db, legacy_dir, "alice")
    snapshot_sha = review(settings.db, "alice")["snapshot_sha256"]
    archive_effects(
        settings.db,
        "alice",
        {
            "evidence": "Legacy container stopped",
            "legacy_writer_stopped": True,
            "snapshot_sha256": snapshot_sha,
        },
    )
    key = f"results:{event().id}:initial"
    with connect(settings.db) as db:
        row = db.execute(
            "SELECT state FROM deliveries WHERE person='alice' AND key=?", (key,)
        ).fetchone()
    assert row is not None and row[0] == "suppressed_by_migration"

    # A verified release document discovered after migration must not trigger a duplicate initial email.
    put_doc(
        settings,
        "alpha-post-migration-mirror",
        role="release",
        at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    assert not [n for n in notices(settings, "alice") if n[1] == "results"]
    prepare(settings)
    sender = Recorder()
    assert (await deliver(settings.db, "alice", sender)) == {"sent": [], "unknown": []}
    assert sender.calls == []
