"""Regression tests for release disclosure and cross-restore token revival."""
import os
import runpy
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

from app.backup_cli import prepare
from app.restore_guard import verify_install
from tests.test_backups import PASSWORD, finish

ROOT = Path(__file__).resolve().parents[1]


def test_restore_reference_rejects_another_valid_preparation(upload, client, application, tmp_path):
    upload()
    ident = client.post('/api/backups', json={'password': PASSWORD}).json()['id']
    assert finish(client, ident)['status'] == 'ready'
    archive = (application.state.backups.root / (ident + '.mcmbackup'))
    old, fresh = tmp_path / 'old', tmp_path / 'fresh'
    prepare(archive, old, PASSWORD, 1024**3)
    prepare(archive, fresh, PASSWORD, 1024**3)
    expected = tmp_path / 'expected-proof'
    shutil.copyfile(fresh / '.restore-proof', expected)
    verify_install(fresh, expected)
    # The old directory is valid on its own, including its original proof.
    prior = tmp_path / 'prior-proof'
    shutil.copyfile(old / '.restore-proof', prior)
    verify_install(old, prior)
    with pytest.raises(ValueError, match='identity'):
        verify_install(old, expected)
    with pytest.raises(ValueError, match='external'):
        verify_install(old, old / '.restore-proof')
    expected.unlink()
    with pytest.raises(ValueError, match='proof'):
        verify_install(fresh, expected)


@pytest.mark.parametrize('state', ['prepared', 'old_moved', 'installed'])
@pytest.mark.parametrize('matching', [False, True])
def test_resume_binds_real_guard_to_current_restore(tmp_path, settings, state, matching):
    from dataclasses import replace
    from app.services import Store
    from app.restore_guard import token_fingerprint

    script_source = ROOT / 'scripts/backup.sh'
    if not script_source.exists():
        pytest.skip('host restore controller is not in the production image')
    project = tmp_path / 'project'
    scripts = project / 'scripts'
    scripts.mkdir(parents=True)
    (scripts / 'backup.sh').write_text(script_source.read_text().replace('[ -t 0 ]', 'true'))
    (scripts / 'init-data.sh').write_text('exit 0\n')
    (project / '.env').touch()
    (scripts / 'check-compose-project.sh').write_bytes(Path('scripts/check-compose-project.sh').read_bytes())
    (project / '.restore-state').write_text(state)
    work = project / '.restore-work'
    work.mkdir()
    source = tmp_path / 'sample.yaml'
    source.write_bytes(b'password: synthetic-restore-canary\n')
    # Wrong data came from a previous restore and still has a valid self-proof.
    candidate = work / 'data' if state == 'prepared' else project / 'data'
    store = Store(replace(settings, data_dir=candidate))
    item = store.create(source, 'test.yaml', 'test')
    saved_link = store.subscription_url(item['id'])
    with store.db.connect() as con:
        (candidate / '.restore-proof').write_text(token_fingerprint(con))
    store.close()
    (work / 'expected-proof').write_text((candidate / '.restore-proof').read_text() if matching else '0' * 64)
    if state == 'prepared':
        (project / 'data/db').mkdir(parents=True)
        (project / 'data/db/.writer.lock').touch()
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    for name, body in {
        'id': '#!/bin/sh\necho 0\n',
        'flock': '#!/bin/sh\nexit 0\n',
        'sync': '#!/bin/sh\nexit 0\n',
        'docker': '''#!/bin/sh
printf '%s\\n' "$*" >> "$AUDIT_LOG"
case "$*" in
  "compose config") echo "name: test";;
  *app.restore_guard*)
    case "$*" in *'/restore-control/expected-proof') ;; *) exit 9;; esac
    "$TEST_PYTHON" -m app.restore_guard "$TEST_PROJECT/data" "$TEST_PROJECT/.restore-work/expected-proof";;
  *) exit 0;;
esac
''',
    }.items():
        path = binaries / name
        path.write_text(body)
        path.chmod(0o700)
    import sys
    log = tmp_path / 'commands'
    result = subprocess.run(['sh', str(scripts / 'backup.sh'), 'resume'], capture_output=True,
                            env=dict(os.environ, PATH=str(binaries)+':'+os.environ['PATH'],
                                     PYTHONPATH=str(ROOT), TEST_PYTHON=sys.executable,
                                     TEST_PROJECT=str(project), AUDIT_LOG=str(log)))
    assert (result.returncode == 0) == matching
    assert 'app.restore_guard' in log.read_text()
    if matching:
        assert 'compose up -d --wait admin' in log.read_text()
        assert 'compose up -d --wait public' in log.read_text()
        assert not (project / '.restore-state').exists()
        assert not work.exists()
    else:
        assert 'compose up' not in log.read_text()
    assert saved_link.encode() not in result.stdout + result.stderr


@pytest.mark.parametrize('lock', ['missing', 'no_db', 'symlink'])
def test_resume_moves_data_admin_never_opened(tmp_path, settings, lock):
    """A new host runs init-data.sh but never starts admin, so no writer lock exists."""
    from dataclasses import replace
    from app.services import Store
    from app.restore_guard import token_fingerprint

    script_source = ROOT / 'scripts/backup.sh'
    if not script_source.exists():
        pytest.skip('host restore controller is not in the production image')
    project = tmp_path / 'project'
    scripts = project / 'scripts'
    scripts.mkdir(parents=True)
    (scripts / 'backup.sh').write_text(script_source.read_text().replace('[ -t 0 ]', 'true'))
    (scripts / 'init-data.sh').write_text('exit 0\n')
    (project / '.env').touch()
    (scripts / 'check-compose-project.sh').write_bytes(Path('scripts/check-compose-project.sh').read_bytes())
    (project / '.restore-state').write_text('prepared')
    work = project / '.restore-work'
    work.mkdir()
    source = tmp_path / 'sample.yaml'
    source.write_bytes(b'proxies: []\n')
    store = Store(replace(settings, data_dir=work / 'data'))
    store.create(source, 'test.yaml', 'test')
    with store.db.connect() as con:
        proof = token_fingerprint(con)
    store.close()
    (work / 'data/.restore-proof').write_text(proof)
    (work / 'expected-proof').write_text(proof)
    for directory in ['data/private', 'data/published'] + ([] if lock == 'no_db' else ['data/db']):
        (project / directory).mkdir(parents=True)
    outside = tmp_path / 'outside-lock'
    outside.write_bytes(b'')
    if lock == 'symlink':
        (project / 'data/db/.writer.lock').symlink_to(outside)
    binaries = tmp_path / 'bin'
    binaries.mkdir()
    for name, body in {
        'id': '#!/bin/sh\necho 0\n',
        'flock': '#!/bin/sh\nexit 0\n',
        'sync': '#!/bin/sh\nexit 0\n',
        'chown': '#!/bin/sh\nprintf "chown %s\\n" "$*" >> "$AUDIT_LOG"\n',
        'docker': '''#!/bin/sh
printf '%s\\n' "$*" >> "$AUDIT_LOG"
case "$*" in
  "compose config") echo "name: test";;
  *app.restore_guard*) "$TEST_PYTHON" -m app.restore_guard "$TEST_PROJECT/data" "$TEST_PROJECT/.restore-work/expected-proof";;
  *) exit 0;;
esac
''',
    }.items():
        path = binaries / name
        path.write_text(body)
        path.chmod(0o700)
    import sys
    log = tmp_path / 'commands'
    result = subprocess.run(['sh', str(scripts / 'backup.sh'), 'resume'], capture_output=True,
                            env=dict(os.environ, PATH=str(binaries)+':'+os.environ['PATH'],
                                     PYTHONPATH=str(ROOT), TEST_PYTHON=sys.executable,
                                     TEST_PROJECT=str(project), AUDIT_LOG=str(log)))
    if lock in {'missing', 'no_db'}:
        assert result.returncode == 0, result.stderr.decode()
        created = project / 'data.before-restore/db/.writer.lock'
        assert created.is_file() and not created.is_symlink()
        assert (project / 'data/.restore-proof').read_text() == proof
        commands = log.read_text()
        assert 'chown -h 10001:10001 data/db/.writer.lock' in commands
        assert ('chown -h 10001:10001 data/db\n' in commands) == (lock == 'no_db')
        assert 'compose up -d --wait public' in commands
    else:
        assert result.returncode != 0
        assert (project / 'data/db').is_dir() and not (project / 'data.before-restore').exists()
        assert 'compose up' not in log.read_text()
    assert outside.read_bytes() == b''


@pytest.fixture
def release_source(tmp_path):
    script = ROOT / 'scripts/package_release.py'
    if not script.exists():
        pytest.skip('host packaging tool is not in the production image')
    module = runpy.run_path(str(script))
    root = tmp_path / 'source'
    root.mkdir()
    for name in module['ROOT_FILES']:
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text('source fixture\n')
    for name in module['SOURCE_TREES']:
        (root / name).mkdir(exist_ok=True)
    (root / 'app/main.py').write_text('print("safe code")\n')
    return module['build_release'], root


def test_release_never_includes_data_or_recovery_residue(release_source, tmp_path):
    build, root = release_source
    secret = b'synthetic-private-release-canary'
    for name in ['data/private/1.yaml', 'data.before-restore/private/1.yaml',
                 'data.before-restore-20261117/db/manager.sqlite3', '.restore-work/data/private/1.yaml',
                 'backups/a.mcmbackup', '.env', '.env.production', '.restore-state',
                 'app/accidental.yaml', 'app/.env', 'app/manager.sqlite3',
                 'app/__pycache__/secret.py', 'scripts/.private/secret.py']:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(secret)
    destination = tmp_path / 'release.tar.gz'
    build(root, destination)
    assert destination.stat().st_mode & 0o777 == 0o600
    with tarfile.open(destination) as archive:
        assert 'app/main.py' in archive.getnames()
        assert 'compose.yaml' in archive.getnames()
        for entry in archive:
            assert entry.isfile()
            assert secret not in archive.extractfile(entry).read()
    original = destination.read_bytes()
    with pytest.raises(FileExistsError):
        build(root, destination)
    assert destination.read_bytes() == original


@pytest.mark.parametrize('kind', ['file', 'directory', 'hardlink'])
def test_release_rejects_source_links(release_source, tmp_path, kind):
    build, root = release_source
    private = tmp_path / 'private'
    private.mkdir()
    secret = private / 'private.yaml'
    secret.write_bytes(b'synthetic-private-release-canary')
    link = root / 'app/innocent.py'
    if kind == 'hardlink':
        os.link(secret, link)
    else:
        link.symlink_to(private if kind == 'directory' else secret)
    destination = tmp_path / 'release.tar.gz'
    with pytest.raises(ValueError):
        build(root, destination)
    assert not destination.exists()
