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
        response = await client.get("/connections/")
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
