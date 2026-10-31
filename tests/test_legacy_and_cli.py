import json
import yaml
import pytest
from conftest import source, event
from earnings_watch.legacy import import_snapshot
from earnings_watch.exports import legacy
from earnings_watch.store.database import LibraryReader, connect, sync_holdings
from earnings_watch.errors import EarningsError
from earnings_watch.cli import main


def snapshot(directory):
    row = event().model_dump(mode="json")
    row.update(
        id="earnings:ALPHA:2026Q4",
        calendar_synced=True,
        calendar_linked_id="external-id",
        calendar_sync_fingerprint="old-fingerprint",
        results_attempts=2,
    )
    (directory / "ir-sources.yaml").write_text(
        "# Keep this note\n"
        + yaml.safe_dump({"sources": [source().model_dump(mode="json") | {"membership": "always"}]})
    )
    (directory / "events.yaml").write_text(
        yaml.safe_dump({"events": [row], "pending": {"alerts": []}})
    )
    (directory / "earnings-watch-mail.json").write_text(json.dumps({"docs_sent": ["old-key"]}))
    return row


def test_legacy_round_trip_preserves_facts_polling_ledger_and_mail(settings, tmp_path):
    directory = tmp_path / "snapshot"
    directory.mkdir()
    original = snapshot(directory)
    result = import_snapshot(settings.db, directory, "local")
    assert result["requires_delivery_reconciliation"]
    library = LibraryReader(settings.db)
    row = yaml.safe_load(legacy(library, "events", "local"))["events"][0]
    for key, value in original.items():
        assert row[key] == value, key
    assert yaml.safe_load(legacy(library, "events", "local"))["pending"] == {"alerts": []}
    assert json.loads(legacy(library, "mail", "local")) == {"docs_sent": ["old-key"]}
    assert "Keep this note" in legacy(library, "sources", "local")
    with pytest.raises(EarningsError, match="empty"):
        import_snapshot(settings.db, directory, "local")


def test_legacy_constraint_failure_rolls_back_every_table(settings, tmp_path):
    directory = tmp_path / "bad"
    directory.mkdir()
    snapshot(directory)
    sources = yaml.safe_load((directory / "ir-sources.yaml").read_text())
    sources["sources"].append(source().model_dump(mode="json") | {"ticker": "OTHER"})
    (directory / "ir-sources.yaml").write_text(yaml.safe_dump(sources))
    with pytest.raises(Exception):
        import_snapshot(settings.db, directory, "local")
    with connect(settings.db) as db:
        for table in ("sources", "events", "follows", "calendar_ledger", "deliveries"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


def test_holdings_replacement_preserves_manual_and_refuses_bad_batch_atomically(seeded):
    from earnings_watch import open

    service = open(seeded)
    service.watch("ALPHA")
    assert sync_holdings(seeded.db, "local", "portfolio", [{"ticker": "ALPHA"}])["follows"] == 1
    before = service.watchlist()
    with pytest.raises(EarningsError, match="import sources"):
        sync_holdings(seeded.db, "local", "portfolio", [{"ticker": "MISSING"}])
    assert service.watchlist() == before
    sync_holdings(seeded.db, "local", "portfolio", [])
    assert len(service.watchlist()["follows"]) == 1


def test_cli_init_doctor_export_and_json_anywhere(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("EARNINGS_WATCH_DATA_DIR", str(tmp_path / "state"))
    assert main(["--json", "init"]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "initialized"
    assert main(["doctor", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["integrity"] == "ok"
    assert main(["export", "events", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"events": []}


def test_cli_upcoming_table_and_explicit_json(seeded, monkeypatch, capsys):
    from earnings_watch import open

    open(seeded).watch("ALPHA")
    monkeypatch.setenv("EARNINGS_WATCH_DATA_DIR", str(seeded.data_dir))
    assert main(["upcoming"]) == 0
    table = capsys.readouterr().out
    assert "DATE" in table and "ALPHA" in table and not table.startswith("{")
    for args in (["upcoming", "--json"], ["--json", "upcoming"], ["upcoming", "--format", "json"]):
        assert main(args) == 0
        assert json.loads(capsys.readouterr().out)["events"][0]["ticker"] == "ALPHA"


def test_notifications_only_project_followed_issuers(seeded):
    from earnings_watch import open
    from earnings_watch.notify import prepare

    prepare(seeded)
    with connect(seeded.db) as db:
        assert db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 0
        assert all(r[0] == 0 for r in db.execute("SELECT notify_seq FROM people"))
    open(seeded, "alice").watch("ALPHA")
    from earnings_watch.store.accounts import preferences, profile

    pref = profile(seeded.db, "alice")
    preferences(
        seeded.db,
        "alice",
        pref["revision"],
        dict(results=True, brief=True, urgent=False, hour=0),
        "UTC",
    )
    prepare(seeded)
    with connect(seeded.db) as db:
        rows = list(db.execute("SELECT person,kind FROM deliveries"))
    assert rows and {r[0] for r in rows} == {"alice"}
    assert "brief" in {r[1] for r in rows}


def test_migration_freezes_external_effects_and_advances_cursor(settings, tmp_path):
    from earnings_watch.notify import prepare
    from earnings_watch.outbox import enqueue, deliver
    from earnings_watch.calendar import plan
    from earnings_watch.composition import open_service
    import asyncio

    directory = tmp_path / "snapshot"
    directory.mkdir()
    snapshot(directory)
    import_snapshot(settings.db, directory, "local")
    prepare(settings)
    enqueue(settings.db, "local", "new", "reminder", {})
    with connect(settings.db) as db:
        assert (
            db.execute("SELECT notify_seq FROM people WHERE id='local'").fetchone()[0]
            == db.execute("SELECT max(seq) FROM event_revisions").fetchone()[0]
        )
        assert db.execute("SELECT count(*) FROM deliveries").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM calendar_ledger").fetchone()[0] == 0
    with pytest.raises(EarningsError, match="reconciliation"):
        plan(open_service(settings).person, key=b"key", sink="legacy")

    class NeverSend:
        async def send(self, *args):
            pytest.fail("Migration sent a notification")

    assert asyncio.run(deliver(settings.db, "local", NeverSend()))["blocked"]


def test_legacy_documents_and_polling_are_normalized_without_promoting_doubt(settings, tmp_path):
    from earnings_watch.store.database import Catalogue

    directory = tmp_path / "snapshot"
    directory.mkdir()
    snapshot(directory)
    path = directory / "events.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["events"][0].update(
        results_links=[
            {
                "title": "Results",
                "kind": "press-release",
                "url": "https://issuer.example/results.pdf",
                "unverified": "period mismatch",
                "attachment_path": "/private/download.pdf",
                "verified_at": "2026-10-01T00:00:00+00:00",
                "content_sha256": "a" * 64,
            }
        ],
        results_last_attempt="2026-10-01T00:00:00+00:00",
        release_baseline=["filing-1"],
    )
    path.write_text(yaml.safe_dump(raw))
    import_snapshot(settings.db, directory, "local")
    library = LibraryReader(settings.db)
    doc = library.documents()[0]
    assert not doc["verified"] and doc["role"] == "release" and doc["sha256"] == "a" * 64
    assert "attachment_path" not in doc
    state = Catalogue(settings.db).state("release_watch", event().id)
    assert state["attempts"] == 2 and state["last_attempt"] and state["seen"] == ["filing-1"]
    assert state["event_date"] == str(event().date)
    projected = yaml.safe_load(legacy(library, "events", "local"))["events"][0]
    assert projected["results_links"][0]["unverified"] == "period mismatch"
    assert "attempts" not in projected


def test_projections_refresh_sources_documents_and_removals(settings, tmp_path):
    from earnings_watch.store.database import Catalogue

    directory = tmp_path / "snapshot"
    directory.mkdir()
    snapshot(directory)
    import_snapshot(settings.db, directory, "local")
    catalogue = Catalogue(settings.db)
    updated = source().model_copy(
        update={
            "calendar_url": "https://issuer.example/new",
            "extensions": {"membership": "always", "wiki": "example"},
        }
    )
    catalogue.put_source(updated)
    projection = yaml.safe_load(legacy(catalogue, "sources", "local"))["sources"][0]
    assert (
        projection["calendar_url"] == updated.calendar_url and projection["membership"] == "always"
    )
    assert "extensions" not in projection and "exchange" not in projection
    catalogue.put_document(
        {
            "id": "new",
            "event_id": event().id,
            "url": "https://issuer.example/new.pdf",
            "title": "New",
            "role": "statements",
            "verified": True,
            "first_seen": "2026-10-01T00:00:00+00:00",
            "sha256": "b" * 64,
        }
    )
    projected = yaml.safe_load(legacy(catalogue, "events", "local"))["events"][0]
    assert projected["results_links"][0]["kind"] == "financial-statements"
    catalogue.commit_issuer("alpha", [], run_id="removed", authoritative=True)
    assert yaml.safe_load(legacy(catalogue, "events", "local"))["events"] == []


def test_reconciliation_requires_current_calendar_proof_and_external_review(settings, tmp_path):
    from earnings_watch.legacy import reconcile
    from earnings_watch.calendar import desired, plan
    from earnings_watch.composition import open_service

    directory = tmp_path / "snapshot"
    directory.mkdir()
    snapshot(directory)
    import_snapshot(settings.db, directory, "local")
    entry = desired(LibraryReader(settings.db).events())[0]
    receipt = {
        "notifications_reviewed": True,
        "evidence": "Reviewed synthetic pending effects",
        "calendar": [
            {
                "key": entry["key"],
                "fingerprint": "old-fingerprint",
                "provider_id": "real-provider-id",
            }
        ],
    }
    with pytest.raises(EarningsError):
        reconcile(settings.db, "local", receipt)
    with connect(settings.db) as db:
        assert db.execute("SELECT count(*) FROM calendar_ledger").fetchone()[0] == 0
    receipt["calendar"][0]["fingerprint"] = entry["fingerprint"]
    assert reconcile(settings.db, "local", receipt)["calendar_confirmed"] == 1
    assert plan(open_service(settings).person, key=b"key", sink="legacy")["operations"] == []


def test_import_baseline_does_not_create_an_unchanged_event_revision(settings, tmp_path):
    from earnings_watch.store.database import Catalogue

    directory = tmp_path / "snapshot"
    directory.mkdir()
    snapshot(directory)
    import_snapshot(settings.db, directory, "local")
    Catalogue(settings.db).commit_issuer("alpha", [event()], run_id="unchanged")
    assert LibraryReader(settings.db).events()[0]["revision"] == 1


def test_reconciliation_cannot_omit_legacy_calendar_markers(settings, tmp_path):
    from earnings_watch.legacy import reconcile
    from earnings_watch.notify import prepare

    directory = tmp_path / "snapshot"
    directory.mkdir()
    snapshot(directory)
    import_snapshot(settings.db, directory, "local")
    receipt = {
        "notifications_reviewed": True,
        "evidence": "Reviewed outside application",
        "calendar": [],
    }
    with pytest.raises(EarningsError, match="every imported calendar"):
        reconcile(settings.db, "local", receipt)
    receipt["calendar_absent"] = ["earnings-watch:" + event().id]
    reconcile(settings.db, "local", receipt)
    prepare(settings)
    with connect(settings.db) as db:
        assert not db.execute(
            "SELECT 1 FROM deliveries WHERE kind IN ('change','reminder','documents') AND state='pending'"
        ).fetchone()


def test_import_keeps_personal_registry_metadata_out_of_shared_catalogue(settings, tmp_path):
    directory = tmp_path / "snapshot"
    directory.mkdir()
    snapshot(directory)
    registry = yaml.safe_load((directory / "ir-sources.yaml").read_text())
    registry["sources"][0].update(
        wiki="/private/wiki", track_reason="personal position", extensions={"private": "data"}
    )
    (directory / "ir-sources.yaml").write_text(
        "# Personal registry comment\n" + yaml.safe_dump(registry)
    )
    import_snapshot(settings.db, directory, "local")
    shared = LibraryReader(settings.db).sources()[0]
    assert shared.notes == "" and shared.extensions == {}
    own = legacy(LibraryReader(settings.db), "sources", "local")
    assert "personal position" in own and "Personal registry comment" in own
    other = legacy(LibraryReader(settings.db), "sources", "alice")
    assert "personal position" not in other and "/private/wiki" not in other


def test_reconciliation_document_baseline_has_stable_order(settings, tmp_path):
    from earnings_watch.legacy import reconcile
    from earnings_watch.store.database import Catalogue
    from earnings_watch.notify import prepare

    directory = tmp_path / "snapshot"
    directory.mkdir()
    snapshot(directory)
    import_snapshot(settings.db, directory, "local")
    for identifier in ["z", "a"]:
        Catalogue(settings.db).put_document(
            {
                "id": identifier,
                "event_id": event().id,
                "url": f"https://issuer.example/{identifier}.pdf",
                "title": identifier,
                "role": "release",
                "verified": True,
                "first_seen": "2026-10-01T00:00:00+00:00",
                "sha256": "",
            }
        )
    reconcile(
        settings.db,
        "local",
        {
            "evidence": "External synthetic review",
            "notifications_reviewed": True,
            "calendar_absent": ["earnings-watch:" + event().id],
        },
    )
    prepare(settings)
    with connect(settings.db) as db:
        assert not db.execute(
            "SELECT 1 FROM deliveries WHERE kind='documents' AND state='pending'"
        ).fetchone()


def test_legacy_override_identity_mapping_is_transactional(settings, tmp_path):
    directory = tmp_path / "snapshot"
    directory.mkdir()
    snapshot(directory)
    (directory / "events-overrides.yaml").write_text(
        yaml.safe_dump({"confirm_removals": ["earnings:ALPHA:2026Q4"], "timing_resolutions": {}})
    )
    import_snapshot(settings.db, directory, "local")
    with connect(settings.db) as db:
        assert dict(db.execute("SELECT * FROM overrides").fetchone()) == {
            "event_id": event().id,
            "kind": "remove",
            "doc": "{}",
        }


@pytest.mark.parametrize(
    "receipt",
    [
        [],
        {"notifications_reviewed": True, "evidence": "review", "calendar": [None]},
        {"notifications_reviewed": True, "evidence": "review", "calendar": [{"key": []}]},
    ],
)
def test_invalid_reconciliation_is_a_domain_error(settings, tmp_path, receipt):
    from earnings_watch.legacy import reconcile

    directory = tmp_path / "snapshot"
    directory.mkdir()
    snapshot(directory)
    import_snapshot(settings.db, directory, "local")
    with pytest.raises(EarningsError):
        reconcile(settings.db, "local", receipt)


def test_legacy_review_exposes_current_fingerprints_without_unfencing(settings, tmp_path):
    from earnings_watch.legacy import review
    from earnings_watch.store.database import legacy_blocked

    directory = tmp_path / "snapshot"
    directory.mkdir()
    snapshot(directory)
    import_snapshot(settings.db, directory, "local")
    result = review(settings.db, "local")
    assert result["state"] == "unknown" and result["desired"][0]["fingerprint"]
    assert result["legacy_calendar"][0]["calendar_linked_id"] == "external-id"
    assert result["mail"] == {"docs_sent": ["old-key"]}
    assert legacy_blocked(settings.db, "local")
    with pytest.raises(EarningsError):
        review(settings.db, "alice")


def test_review_and_reconciliation_follow_the_personal_calendar_scope(settings, tmp_path):
    from earnings_watch.legacy import review, reconcile
    from earnings_watch.store.database import sync_holdings, PersonalStore

    directory = tmp_path / "snapshot"
    directory.mkdir()
    snapshot(directory)
    import_snapshot(settings.db, directory, "local")
    PersonalStore(settings.db, "local").unwatch("ALPHA")
    sync_holdings(settings.db, "local", "legacy", [])
    assert review(settings.db, "local")["desired"] == []
    # Historical markers still need review even after a person stops following.
    with pytest.raises(EarningsError, match="every imported calendar"):
        reconcile(settings.db, "local", {"evidence": "Reviewed", "notifications_reviewed": True})
    assert reconcile(
        settings.db,
        "local",
        {
            "evidence": "Confirmed external absence",
            "notifications_reviewed": True,
            "calendar_absent": ["earnings-watch:" + event().id],
        },
    )["reconciled"]
