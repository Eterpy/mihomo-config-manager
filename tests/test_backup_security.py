import json
import sqlite3
import tarfile
import pytest
from app.backups import unpack_verified, encrypt
from app.backup_cli import prepare
from app.services import digest
from tests.test_backups import PASSWORD, finish


def malicious_backup(upload, client, application, tmp_path):
    upload()
    ident = client.post('/api/backups', json={'password': PASSWORD}).json()['id']
    assert finish(client, ident)['status'] == 'ready'
    extracted = tmp_path / 'source'
    unpack_verified(application.state.backups.download(ident), PASSWORD, extracted)
    db = extracted / 'db/manager.sqlite3'
    with sqlite3.connect(db) as con:
        con.execute('CREATE TRIGGER sqliteXkeep_token AFTER UPDATE OF token ON configs BEGIN UPDATE configs SET token=old.token WHERE id=old.id; END')
    # sqlite3's connection context commits but does not close/checkpoint WAL.
    # Prove the old filter hid the trigger and a token UPDATE was undone.
    assert ('trigger', 'sqliteXkeep_token') not in con.execute("SELECT type,name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'").fetchall()
    previous = con.execute('SELECT token FROM configs').fetchone()[0]
    con.execute("UPDATE configs SET token=?", ('0' * 64,))
    assert con.execute('SELECT token FROM configs').fetchone()[0] == previous
    con.rollback()
    con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    con.close()
    manifest = json.loads((extracted / 'manifest.json').read_text())
    manifest['files']['db/manager.sqlite3'] = {'size': db.stat().st_size, 'sha256': digest(db)}
    (extracted / 'manifest.json').write_text(json.dumps(manifest))
    archive = tmp_path / 'attack.tar.gz'
    with tarfile.open(archive, 'w:gz', format=tarfile.USTAR_FORMAT) as tar:
        for name in [*manifest['files'], 'manifest.json']:
            tar.add(extracted / name, arcname=name)
    encrypted = tmp_path / 'attack.mcmbackup'
    encrypt(archive, encrypted, PASSWORD, 1024**3)
    return encrypted


def test_restore_rejects_token_revival_trigger(upload, client, application, tmp_path):
    attack = malicious_backup(upload, client, application, tmp_path)
    with pytest.raises(ValueError, match='schema'):
        prepare(attack, tmp_path / 'restored', PASSWORD, 1024**3)


def test_restore_guard_rejects_old_tokens_and_publications(upload, client, application, tmp_path):
    from app.restore_guard import verify_install
    import shutil
    item = upload().json()
    ident = client.post('/api/backups', json={'password': PASSWORD}).json()['id']
    assert finish(client, ident)['status'] == 'ready'
    original = application.state.backups.download(ident)
    restored = tmp_path / 'guarded'
    prepare(original, restored, PASSWORD, 1024**3)
    expected = tmp_path / 'expected-proof'
    shutil.copyfile(restored / '.restore-proof', expected)
    verify_install(restored, expected)
    old_token = item['subscription_url'].split('/s/')[1].split('/')[0]
    forbidden = restored / 'published' / (old_token + '.yaml')
    forbidden.write_bytes(b'proxies: []\n')
    with pytest.raises(ValueError, match='publication'):
        verify_install(restored, expected)
    forbidden.unlink()
    with sqlite3.connect(restored / 'db/manager.sqlite3') as con:
        con.execute('UPDATE configs SET token=?', (old_token,))
    with pytest.raises(ValueError, match='identity'):
        verify_install(restored, expected)
    (restored / '.restore-proof').unlink()
    with pytest.raises(ValueError, match='proof'):
        verify_install(restored, expected)


@pytest.mark.parametrize('endpoint', ['versions', 'versions/1/preview', 'rollback', 'backups', 'backup', 'download', 'delete'])
def test_new_api_requires_auth(upload, client, application, endpoint):
    item = upload().json()
    base = '/api/configs/' + item['id']
    ident = client.post('/api/backups', json={'password': PASSWORD}).json()['id']
    assert finish(client, ident)['status'] == 'ready'
    paths = {'versions':('GET',base+'/versions'), 'versions/1/preview':('GET',base+'/versions/1/preview'),
             'rollback':('POST',base+'/versions/1/rollback'), 'backups':('GET','/api/backups'),
             'backup':('GET','/api/backups/'+ident), 'download':('GET','/api/backups/'+ident+'/download'),
             'delete':('DELETE','/api/backups/'+ident)}
    method, path = paths[endpoint]
    response = client.request(method, path, headers={'Cf-Access-Jwt-Assertion':''})
    assert response.status_code == 401
    assert 'proxies' not in response.text and PASSWORD not in response.text


def test_new_writes_require_csrf(upload, client):
    item = upload().json()
    for path, body in [('/api/backups', {'password':PASSWORD}), ('/api/configs/'+item['id']+'/versions/1/rollback', {'expected_current_version':1})]:
        assert client.post(path,json=body,headers={'X-CSRF-Token':''}).status_code == 403


def test_rollback_after_rotation_cannot_revive_old_token(upload, client, application):
    item = upload().json()
    base = '/api/configs/' + item['id']
    client.put(base+'/content', files={'file':('new.yaml', b'v: 2\n')})
    rotated = client.post(base+'/rotate-token').json()
    response = client.post(base+'/versions/1/rollback', json={'expected_current_version':2})
    assert response.status_code == 200
    assert response.json()['subscription_url'] == rotated['subscription_url']
    old = item['subscription_url'].split('/s/')[1].split('/')[0]
    assert not (application.state.store.published / (old+'.yaml')).exists()


def test_backup_errors_do_not_echo_password(upload, client, application, monkeypatch, caplog):
    import app.backups as module
    upload()
    def fail(*args):
        raise OSError('node-secret-marker ' + PASSWORD)
    monkeypatch.setattr(module, 'encrypt', fail)
    ident = client.post('/api/backups', json={'password':PASSWORD}).json()['id']
    assert finish(client, ident)['status'] == 'failed'
    rendered = client.get('/api/backups/'+ident).text + caplog.text
    assert PASSWORD not in rendered and 'node-secret-marker' not in rendered
    assert not list(application.state.backups.root.glob('.work-*'))


def test_archive_path_cannot_inject_mount_options(tmp_path):
    import os
    import subprocess
    from pathlib import Path
    project = tmp_path / 'project'; scripts = project / 'scripts'; scripts.mkdir(parents=True)
    (scripts / 'backup.sh').write_text(Path('scripts/backup.sh').read_text().replace('[ -t 0 ]', 'true'))
    # A comma would start a new --mount option, e.g. dropping "readonly".
    archive = tmp_path / 'backups,dst=work' / 'x.mcmbackup'
    archive.parent.mkdir()
    archive.write_bytes(b'MCMBAK01')
    binaries = tmp_path / 'bin'; binaries.mkdir()
    for name, body in {
        'id': '#!/bin/sh\necho 0\n',
        'flock': '#!/bin/sh\nexit 0\n',
        'chown': '#!/bin/sh\nexit 0\n',
        'docker': '#!/bin/sh\nprintf "%s\\n" "$*" >> "$AUDIT_LOG"\nexit 0\n',
    }.items():
        path = binaries / name; path.write_text(body); path.chmod(0o700)
    log = tmp_path / 'commands'
    result = subprocess.run(['sh', str(scripts / 'backup.sh'), 'verify', str(archive)], env=dict(os.environ, PATH=str(binaries)+':'+os.environ['PATH'], AUDIT_LOG=str(log)), capture_output=True)
    assert result.returncode != 0
    assert not log.exists()
    assert not (project / '.restore-work').exists()


@pytest.mark.parametrize('state', ['prepared', 'old_moved', 'installed'])
def test_resume_never_starts_containers_after_guard_failure(tmp_path, state):
    import os
    import subprocess
    from pathlib import Path
    project = tmp_path / 'project'; scripts = project / 'scripts'; scripts.mkdir(parents=True)
    script = Path('scripts/backup.sh').read_text().replace('[ -t 0 ]', 'true')
    (scripts / 'backup.sh').write_text(script)
    (project / '.restore-state').write_text(state)
    for directory in ['data/db', '.restore-work/data/db']:
        (project / directory).mkdir(parents=True)
        (project / directory / '.writer.lock').touch()
    binaries = tmp_path / 'bin'; binaries.mkdir()
    for name, body in {
        'id': '#!/bin/sh\necho 0\n',
        'flock': '#!/bin/sh\nexit 0\n',
        'sync': '#!/bin/sh\nexit 0\n',
        'docker': '#!/bin/sh\nprintf "%s\\n" "$*" >> "$AUDIT_LOG"\ncase "$*" in *app.restore_guard*) exit 1;; esac\nexit 0\n',
    }.items():
        path = binaries / name; path.write_text(body); path.chmod(0o700)
    log = tmp_path / 'commands'
    result = subprocess.run(['sh', str(scripts / 'backup.sh'), 'resume'], env=dict(os.environ, PATH=str(binaries)+':'+os.environ['PATH'], AUDIT_LOG=str(log)), capture_output=True)
    assert result.returncode != 0
    commands = log.read_text()
    assert 'app.restore_guard' in commands
    assert 'compose up' not in commands
