import sqlite3
import threading
from contextlib import contextmanager

import pytest

from app.errors import AppError
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
    monkeypatch.setattr(store, "_withdraw", lambda token: (_ for _ in ()).throw(OSError("injected")))
    with pytest.raises(AppError) as error:
        store.change(item["id"], action)
    assert error.value.status == 503
    assert store.failed
    with pytest.raises(AppError):
        store.rename(item["id"], "cannot write")
    store.close()
    recovered = Store(settings)
    assert not published(recovered, item).exists()
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
    monkeypatch.setattr(store, "_publish", lambda row: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(AppError):
        store.change(item["id"], "rotate-token")
    assert not published(store, item).exists()
    pending = store.get(item["id"])
    assert pending["status"] == "pending"
    store.close()
    recovered = Store(settings)
    assert recovered.get(item["id"])["subscription_url"] == pending["subscription_url"]
    assert published(recovered, pending).exists()
    assert not published(recovered, item).exists()
    recovered.close()


def test_replace_failure_and_orphan_cleanup(settings, source, monkeypatch):
    store = Store(settings)
    item = store.create(source, "x.yaml", "x")
    source.write_bytes(b"new: true\n")
    monkeypatch.setattr(store, "_publish", lambda row: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(AppError):
        store.replace(item["id"], source, "new.yaml")
    assert published(store, item).read_bytes() == b"proxies: []\n"
    (store.published / ("f" * 64 + ".yaml")).write_bytes(b"orphan")
    (store.published / ".pending-orphan").write_bytes(b"partial")
    store.close()
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
