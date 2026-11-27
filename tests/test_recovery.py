import sqlite3
import threading
from contextlib import closing, contextmanager

import pytest

from app.errors import AppError, MissingDatabase
from app.services import Store
from tests.test_tokens import published


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source.yaml"
    path.write_bytes(b"proxies: []\n")
    return path


@pytest.mark.parametrize("action", ["rotate-token", "disable", "delete"])
def test_revocation_failure_replays(settings, source, monkeypatch, action):
    store = Store(settings)
    item = store.create(source, "x.yaml", "x")
    old_path = published(store, item)
    monkeypatch.setattr(store, "_withdraw", lambda token: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(AppError) as error:
        store.change(item["id"], action)
    assert error.value.status == 503
    assert store.failed
    with pytest.raises(AppError):
        store.rename(item["id"], "cannot write")
    store.close()
    recovered = Store(settings)
    assert not old_path.exists()
    assert recovered.ready and not recovered.failed
    if action != "delete":
        current = recovered.get(item["id"])
        assert published(recovered, current).exists() == (action == "rotate-token")
    else:
        assert recovered.list() == []
    recovered.close()


def test_rotation_failure_never_republishes_revoked_token(settings, source, monkeypatch):
    store = Store(settings)
    item = store.create(source, "x.yaml", "x")
    old_path = published(store, item)
    monkeypatch.setattr(store, "_publish", lambda row: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(AppError):
        store.change(item["id"], "rotate-token")
    assert not old_path.exists()
    pending = store.get(item["id"])
    pending_url = store.subscription_url(item["id"])
    assert pending["status"] == "pending"
    store.close()
    recovered = Store(settings)
    assert recovered.subscription_url(item["id"]) == pending_url
    assert published(recovered, pending).exists()
    assert not old_path.exists()
    recovered.close()


def test_replace_failure_and_orphan_cleanup(settings, source, monkeypatch):
    store = Store(settings)
    item = store.create(source, "x.yaml", "x")
    source.write_bytes(b"new: true\n")
    monkeypatch.setattr(store, "_publish", lambda row: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(AppError):
        store.replace(item["id"], source, "new.yaml")
    assert published(store, item).read_bytes() == b"proxies: []\n"
    orphan = store.published / ("f" * 64 + ".yaml")
    orphan.write_bytes(b"orphan")
    (store.published / ".pending-orphan").write_bytes(b"partial")
    store.close()
    with pytest.raises(MissingDatabase):
        Store(settings)
    assert orphan.read_bytes() == b"orphan"
    assert (store.published / ".pending-orphan").exists()
    orphan.unlink()
    recovered = Store(settings)
    assert published(recovered, item).read_bytes() == b"new: true\n"
    assert len(list(recovered.published.iterdir())) == 1
    recovered.close()


def test_db_commit_failure_retains_previous_version(settings, source, monkeypatch):
    store = Store(settings)
    item = store.create(source, "x.yaml", "x")
    original = store.db.connect
    @contextmanager
    def failed_commit():
        with original() as con:
            yield con
            raise sqlite3.OperationalError("injected commit failure")
    monkeypatch.setattr(store.db, "connect", failed_commit)
    source.write_bytes(b"new: true\n")
    with pytest.raises(AppError):
        store.replace(item["id"], source, "new.yaml")
    store.close()
    recovered = Store(settings)
    assert recovered.get(item["id"])["version"] == 1
    assert published(recovered, item).read_bytes() == b"proxies: []\n"
    assert len(list((recovered.private / item["id"] / "versions").iterdir())) == 1
    isolated, = (recovered.root / "quarantine").rglob("2.yaml")
    assert isolated.read_bytes() == b"new: true\n"
    recovered.close()


def test_atomic_replace_never_partial(settings, source):
    store = Store(settings)
    old = b"value: " + b"a" * 100000
    new = b"value: " + b"b" * 200000
    source.write_bytes(old)
    item = store.create(source, "x.yaml", "x")
    observed = set()
    done = threading.Event()
    def reader():
        while not done.is_set():
            observed.add(published(store, item).read_bytes())
    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(6):
            source.write_bytes(new if index % 2 == 0 else old)
            store.replace(item["id"], source, "x.yaml")
    finally:
        done.set()
        thread.join()
        store.close()
    assert observed and observed <= {old, new}


def test_single_writer_and_corrupt_private_file_fail_closed(settings, source):
    store = Store(settings)
    item = store.create(source, "x.yaml", "x")
    with pytest.raises(BlockingIOError):
        Store(settings)
    (store.private / item["id"] / "versions" / "1.yaml").write_bytes(b"corrupt")
    store.close()
    with pytest.raises(OSError):
        Store(settings)


@pytest.mark.parametrize('change', ['versions', 'rotate'])
def test_old_database_preserves_versions_and_publications(settings, source, tmp_path, change):
    store = Store(settings)
    row = store.create(source, 'sample.yml', 'sample')
    original_path = published(store, row)
    old = tmp_path / 'backup.sqlite'
    with store.db.connect() as current:
        con = sqlite3.connect(old)
        try:
            current.backup(con)
        finally:
            con.close()
    if change == 'versions':
        for n in (2, 3):
            source.write_bytes(f'v: {n}\n'.encode())
            store.replace(row['id'], source, 'sample.yml')
    else:
        store.change(row['id'], 'rotate-token')
    current_path = published(store, row)
    history = store.private / row['id'] / 'versions'
    expected = {p.name: p.read_bytes() for p in history.iterdir()}
    expected_public = current_path.read_bytes()
    store.close()
    old.replace(store.db.path)
    with pytest.raises(MissingDatabase):
        Store(settings)
    assert {p.name: p.read_bytes() for p in history.iterdir()} == expected
    assert current_path.read_bytes() == expected_public
    if change == 'rotate':
        assert not original_path.exists()


@pytest.mark.parametrize('damage', ['missing', 'size', 'sha256', 'max_version'])
def test_inconsistent_current_history_refuses_start(settings, source, damage):
    store = Store(settings)
    row = store.create(source, 'sample.yml', 'sample')
    with store.db.connect() as con:
        if damage == 'missing':
            con.execute('DELETE FROM versions')
        elif damage == 'max_version':
            con.execute('UPDATE configs SET version=0')
        else:
            con.execute(f'UPDATE versions SET {damage}=?', (1 if damage == 'size' else 'wrong',))
    store.close()
    with pytest.raises(MissingDatabase):
        Store(settings)
    assert (store.private / row['id'] / 'versions/1.yaml').read_bytes() == source.read_bytes()


def test_snapshot_before_deletion_reports_old_database(settings, source, tmp_path):
    store = Store(settings)
    item = store.create(source, 'test.yml', 'test')
    saved = tmp_path / 'before-delete.sqlite'
    with store.db.connect() as con, closing(sqlite3.connect(saved)) as snapshot:
        con.backup(snapshot)
    store.change(item['id'], 'delete')
    store.close()
    saved.replace(store.db.path)
    with pytest.raises(MissingDatabase, match='数据库可能比磁盘.*加密备份恢复'):
        Store(settings)
    assert not (store.private / item['id']).exists()
    assert not list(store.published.iterdir())
