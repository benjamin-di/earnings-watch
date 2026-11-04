"""Recovery failures must preserve retryability and private file permissions."""

import os
import sqlite3
from contextlib import contextmanager

import pytest

from earnings_watch.store.database import backup, connect, integrity, restore


def test_failed_backup_leaves_destination_available_for_retry(seeded, tmp_path):
    invalid = tmp_path / "invalid.db"
    invalid.write_bytes(b"not a database")
    destination = tmp_path / "backup.db"
    with pytest.raises(sqlite3.DatabaseError):
        backup(invalid, destination)
    assert not destination.exists()
    backup(seeded.db, destination)
    assert integrity(destination) == "ok"
    assert destination.stat().st_mode & 0o777 == 0o600


def test_interrupted_backup_does_not_publish_partial_destination(seeded, tmp_path, monkeypatch):
    destination = tmp_path / "backup.db"

    class Interrupted:
        def backup(self, target):
            target.execute("CREATE TABLE partial(value)")
            target.commit()
            raise KeyboardInterrupt

    @contextmanager
    def interrupted_connect(path):
        yield Interrupted()

    monkeypatch.setattr("earnings_watch.store.database.connect", interrupted_connect)
    with pytest.raises(KeyboardInterrupt):
        backup(seeded.db, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".backup-*"))


def test_backup_never_replaces_a_destination_created_during_copy(seeded, tmp_path, monkeypatch):
    destination = tmp_path / "backup.db"
    original_link = os.link

    def concurrent_link(source, target):
        destination.write_bytes(b"concurrent backup")
        original_link(source, target)

    monkeypatch.setattr("earnings_watch.store.database.os.link", concurrent_link)
    with pytest.raises(FileExistsError):
        backup(seeded.db, destination)
    assert destination.read_bytes() == b"concurrent backup"
    assert not list(tmp_path.glob(".backup-*"))


def test_restore_to_new_file_is_private_and_keeps_source_unchanged(seeded, tmp_path):
    destination = tmp_path / "restored.db"
    with connect(seeded.db) as db:
        original_id = db.execute("SELECT value FROM metadata WHERE key='database_id'").fetchone()[0]
    previous = os.umask(0o022)
    try:
        restore(destination, seeded.db)
    finally:
        os.umask(previous)
    assert destination.stat().st_mode & 0o777 == 0o600
    assert integrity(destination) == "ok"
    with connect(destination) as db:
        assert (
            db.execute("SELECT value FROM metadata WHERE key='database_id'").fetchone()[0]
            != original_id
        )
    with connect(seeded.db) as db:
        assert (
            db.execute("SELECT value FROM metadata WHERE key='database_id'").fetchone()[0]
            == original_id
        )


def test_restore_through_dangling_symlink_creates_private_target(seeded, tmp_path):
    target = tmp_path / "target.db"
    destination = tmp_path / "restored.db"
    destination.symlink_to(target)
    previous = os.umask(0o022)
    try:
        restore(destination, seeded.db)
    finally:
        os.umask(previous)
    assert destination.is_symlink()
    assert target.stat().st_mode & 0o777 == 0o600
    assert integrity(target) == "ok"
