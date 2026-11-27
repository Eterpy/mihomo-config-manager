import sqlite3
from contextlib import contextmanager
from dataclasses import replace

import pytest

from app.backups import Backups
from app.errors import AppError, MissingDatabase
from app.services import Store
from tests.test_backups import PASSWORD, finish


@pytest.mark.parametrize('empty_db', [False, True])
def test_missing_database_preserves_all_files(settings, tmp_path, empty_db):
    source = tmp_path / 'source.yml'
    source.write_bytes(b'x: 1')
    store = Store(settings)
    item = store.create(source, 'config.yml', 'config')
    published = next(store.published.iterdir())
    private = store.private / item['id'] / 'versions/1.yaml'
    backup = store.root / 'backups/archive.mcmbackup'
    backup.parent.mkdir()
    backup.write_bytes(b'archive')
    store.close()
    store.db.path.unlink()
    if empty_db:
        store.db.path.touch()
    with pytest.raises(MissingDatabase):
        Store(settings)
    assert private.read_bytes() == published.read_bytes() == source.read_bytes()
    assert backup.read_bytes() == b'archive'


@pytest.mark.parametrize('evidence', ['marker', 'private', 'published', 'backups'])
def test_each_guard_before_database_creation(settings, evidence):
    root = settings.data_dir
    path = root / {'marker': 'db/.initialized', 'private': 'private/orphan',
                   'published': 'published/orphan', 'backups': 'backups/orphan'}[evidence]
    path.parent.mkdir(parents=True)
    path.touch()
    with pytest.raises(MissingDatabase):
        Store(settings)
    assert path.exists() and not (root / 'db/manager.sqlite3').exists()


def test_upgrade_adds_marker_and_quarantines_uncommitted_create(settings, tmp_path, monkeypatch):
    store = Store(settings)
    marker = store.db.path.with_name('.initialized')
    marker.unlink()
    source = tmp_path / 'input.yaml'
    source.write_bytes(b'x: 1')
    connect = store.db.connect

    @contextmanager
    def fail_commit():
        with connect() as con:
            yield con
            raise sqlite3.OperationalError('injected commit failure')

    monkeypatch.setattr(store.db, 'connect', fail_commit)
    with pytest.raises(AppError):
        store.create(source, 'test.yaml', 'test')
    store.close()
    reopened = Store(settings)
    try:
        assert marker.stat().st_mode & 0o777 == 0o600
        assert reopened.list() == []
        isolated, = (reopened.root / 'quarantine').rglob('1.yaml')
        assert isolated.read_bytes() == source.read_bytes()
    finally:
        reopened.close()


def test_unregistered_archive_requires_explicit_deletion(client, application):
    backups = application.state.backups
    path = backups.root / 'saved-by-operator.mcmbackup'
    path.write_bytes(b'offline copy')
    restarted = Backups(application.state.store)
    restarted.close()
    result = client.get('/api/backups').json()
    item, = result['items']
    assert item['status'] == 'unregistered' and item['filename'] == path.name
    assert result['used_bytes'] == path.stat().st_size
    assert client.delete('/api/backups/' + item['id']).status_code == 204
    assert not path.exists()


@pytest.mark.parametrize('fault,code', [('missing', 'archive_missing'), ('truncate', 'archive_corrupt'), ('change', 'archive_corrupt')])
def test_backup_integrity(client, application, upload, fault, code):
    upload()
    backup = client.post('/api/backups', json={'password': PASSWORD}).json()['id']
    assert finish(client, backup)['status'] == 'ready'
    path = application.state.backups.root / (backup + '.mcmbackup')
    if fault == 'missing':
        path.unlink()
        restarted = Backups(application.state.store)
        restarted.close()
    elif fault == 'truncate':
        path.write_bytes(b'x')
    else:
        with path.open('r+b') as stream:
            stream.write(b'X')
    api = '/api/backups/' + backup
    if fault != 'change':
        assert client.get('/api/backups').json()['items'][0]['error_code'] == code
    assert client.get(api + '/download').status_code == 409
    assert client.get(api).json()['error_code'] == code


def test_backup_quota_uses_actual_size_and_accounts_for_unregistered_files(client, application, upload):
    store = application.state.store
    limit = 1024**2
    store.settings = replace(store.settings, backup_max_bytes=limit, backup_quota_bytes=limit)
    upload()
    def create():
        return finish(client, client.post('/api/backups', json={'password': PASSWORD}).json()['id'])
    saved = create()
    assert saved['status'] == 'ready'
    list_result = client.get('/api/backups').json()
    assert list_result['quota_bytes'] == list_result['max_bytes'] == limit
    root = application.state.backups.root
    (root / 'keep.mcmbackup').write_bytes(b'0' * (limit - saved['size'] - 32))
    failed = create()
    assert failed['error_code'] == 'quota_exceeded'
    assert not (root / (failed['id'] + '.mcmbackup')).exists()
    store.settings = replace(store.settings, backup_max_bytes=1024)
    assert create()['error_code'] == 'too_large'


def test_verified_download_survives_delete(client, application, upload):
    upload()
    task = client.post('/api/backups', json={'password': PASSWORD}).json()['id']
    assert finish(client, task)['status'] == 'ready'
    backups = application.state.backups
    with backups.download(task) as archive:
        backups.delete(task)
        assert archive.read(8) == b'MCMBAK01'
    assert client.get(f'/api/backups/{task}/download').status_code == 404
