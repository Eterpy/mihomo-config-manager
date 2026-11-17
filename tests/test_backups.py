import json
import sqlite3
import time
from pathlib import Path

import pytest
from app.backups import unpack_verified, encrypt
from app.backup_cli import prepare
from app.services import Store
from dataclasses import replace

PASSWORD = 'test-passphrase-long-enough'


def finish(client, ident):
    for _ in range(200):
        row = client.get('/api/backups/' + ident).json()
        if row['status'] in {'ready', 'failed'}:
            return row
        time.sleep(.025)
    pytest.fail('backup timeout')


def test_history_rollback(upload, client):
    raw = b'\xef\xbb\xbfproxies: []\r\n'
    item = upload(raw).json()
    base = '/api/configs/' + item['id']
    client.put(base + '/content', files={'file': ('new.yaml', b'value: 2\n')})
    client.post(base + '/disable')
    response = client.post(base + '/versions/1/rollback', json={'expected_current_version': 2})
    assert response.status_code == 200
    assert response.json()['subscription_url'] == item['subscription_url']
    assert response.json()['version'] == 3 and not response.json()['enabled']
    assert client.get(base + '/preview').content == raw
    assert client.get(base + '/versions').json()[0]['source_version'] == 1
    assert client.post(base + '/versions/1/rollback', json={'expected_current_version': 2}).status_code == 409
    assert client.get(base + '/versions/999/preview').status_code == 404


def test_backup_restore(upload, client, application, settings, tmp_path):
    item = upload().json()
    response = client.post('/api/backups', json={'password': PASSWORD})
    assert response.status_code == 202
    ident = response.json()['id']
    row = finish(client, ident)
    assert row['status'] == 'ready', row
    path = application.state.backups.download(ident)
    download = client.get(f'/api/backups/{ident}/download')
    assert download.content == path.read_bytes()
    assert 'no-store' in download.headers['cache-control']
    assert b'proxies' not in download.content and PASSWORD.encode() not in download.content
    info = prepare(path, tmp_path / 'restored', PASSWORD, 1024**3)
    assert info['configs'] == 1
    restored = Store(replace(settings, data_dir=tmp_path / 'restored'))
    try:
        new = restored.list()[0]
        assert new['subscription_url'] != item['subscription_url']
        assert restored.preview(new['id']) == b'proxies: []\n'
        assert len(list(restored.published.iterdir())) == 1
    finally:
        restored.close()
    with pytest.raises(Exception):
        unpack_verified(path, 'wrong-password-is-long-enough', tmp_path / 'bad')
    tampered = tmp_path / 'tampered'
    data = bytearray(path.read_bytes()); data[-20] ^= 1; tampered.write_bytes(data)
    with pytest.raises(Exception):
        unpack_verified(tampered, PASSWORD, tmp_path / 'tampered-output')
    assert not (tmp_path / 'tampered-output' / 'db').exists()
    assert client.delete(f'/api/backups/{ident}').status_code == 204
    assert not path.exists()


def test_backup_auth_and_password(client):
    assert client.post('/api/backups', json={'password': 'short'}).status_code == 422
    assert client.post('/api/backups', json={'password': PASSWORD}, headers={'Origin': 'https://evil.test'}).status_code == 403
    assert client.get('/api/backups', headers={'Cf-Access-Jwt-Assertion': 'bad'}).status_code == 401


def test_snapshot_rejects_writes(upload, client, application):
    item = upload().json()
    application.state.store.snapshotting = True
    try:
        assert client.post('/api/configs/' + item['id'] + '/disable').status_code == 503
        assert client.get('/api/status').json()['snapshotting']
    finally:
        application.state.store.snapshotting = False


def test_backup_disk_failure(upload, client, application, monkeypatch):
    upload()
    from collections import namedtuple
    import app.backups as module
    monkeypatch.setattr(module.shutil, 'disk_usage', lambda _: namedtuple('Usage','total used free')(1,1,0))
    ident = client.post('/api/backups', json={'password': PASSWORD}).json()['id']
    assert finish(client, ident)['status'] == 'failed'
    assert client.get(f'/api/backups/{ident}/download').status_code == 409
    assert not application.state.store.snapshotting


def test_backup_concurrency_and_restart(upload, client, application, monkeypatch):
    import threading
    from app.backups import Backups
    import app.backups as module
    upload()
    entered, release = threading.Event(), threading.Event()
    original = module.encrypt
    def paused(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(module, 'encrypt', paused)
    ident = client.post('/api/backups', json={'password': PASSWORD}).json()['id']
    try:
        assert entered.wait(5)
        assert client.post('/api/backups', json={'password': PASSWORD}).status_code == 429
        assert upload(name='after snapshot').status_code == 201
    finally:
        release.set()
    assert finish(client, ident)['status'] == 'ready'
    store = application.state.store
    with store.db.connect() as con:
        con.execute("INSERT INTO backup_jobs(id,status,created_at) VALUES ('interrupted','encrypting','now')")
    leftover = application.state.backups.root / '.work-interrupted'
    leftover.mkdir()
    restarted = Backups(store)
    try:
        assert restarted.get('interrupted')['error_code'] == 'interrupted'
        assert not leftover.exists()
    finally:
        restarted.close()


@pytest.mark.parametrize('kind', ['traversal', 'absolute', 'symlink', 'duplicate', 'oversized'])
def test_authenticated_malicious_archive(tmp_path, kind):
    import io
    import tarfile
    tarpath = tmp_path / 'malicious.tar.gz'
    with tarfile.open(tarpath, 'w:gz') as tar:
        name = {'traversal':'../escape','absolute':'/escape'}.get(kind, 'manifest.json')
        entry = tarfile.TarInfo(name)
        if kind == 'symlink':
            entry.type = tarfile.SYMTYPE; entry.linkname = '/etc/passwd'
            tar.addfile(entry)
        else:
            content = b'x' * (2048 if kind == 'oversized' else 1)
            entry.size = len(content)
            tar.addfile(entry, io.BytesIO(content))
            if kind == 'duplicate': tar.addfile(entry, io.BytesIO(content))
    encrypted = tmp_path / 'malicious.mcmbackup'
    encrypt(tarpath, encrypted, PASSWORD, 1024**2)
    with pytest.raises(Exception):
        unpack_verified(encrypted, PASSWORD, tmp_path / 'output', 1024 if kind == 'oversized' else 1024**2)
    assert not (tmp_path / 'escape').exists()


def test_rollback_failure_recovers(settings, tmp_path, monkeypatch):
    from app.errors import AppError
    store = Store(settings)
    source = tmp_path / 'input.yaml'; source.write_bytes(b'v: 1\n')
    item = store.create(source, 'one.yaml', 'one')
    source.write_bytes(b'v: 2\n'); store.replace(item['id'], source, 'two.yaml')
    monkeypatch.setattr(store, '_publish', lambda _: (_ for _ in ()).throw(OSError()))
    with pytest.raises(AppError): store.rollback(item['id'], 1, 2)
    store.close()
    restored = Store(settings)
    try:
        assert restored.preview(item['id']) == b'v: 1\n'
        assert restored.versions(item['id'])[0]['source_version'] == 1
    finally: restored.close()


def test_schema_one_migration(settings, tmp_path):
    from app.database import SCHEMA
    settings.data_dir.mkdir(); (settings.data_dir / 'db').mkdir()
    with sqlite3.connect(settings.data_dir / 'db/manager.sqlite3') as con:
        con.executescript(SCHEMA)
    store = Store(settings)
    try:
        with store.db.connect() as con:
            assert con.execute('PRAGMA user_version').fetchone()[0] == 2
    finally: store.close()
