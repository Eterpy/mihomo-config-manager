import os
from pathlib import Path
import select
import signal
import subprocess

import pytest

from app import backup_cli
from app.backups import encrypt
from tests.test_backups import PASSWORD


@pytest.fixture
def controller(tmp_path):
    project = tmp_path / 'project'
    scripts = project / 'scripts'
    scripts.mkdir(parents=True)
    source = Path('scripts')
    (scripts / 'backup.sh').write_text((source / 'backup.sh').read_text().replace('[ -t 0 ]', 'true'))
    (scripts / 'check-compose-project.sh').write_bytes((source / 'check-compose-project.sh').read_bytes())
    (scripts / 'init-data.sh').write_text('exit 0\n')
    (project / '.env').write_text('BACKUP_MAX_MIB=2048\n')
    archive = tmp_path / 'sample.mcmbackup'
    archive.touch()
    tools = tmp_path / 'bin'
    tools.mkdir()
    for name, script in {
        'id': 'echo 0',
        'sync': 'exit 0',
        'flock': 'exit 0',
        'chown': 'exit 0',
        'docker': '''
printf '%s\\n' "$*" >> commands
case "$*" in
    "image inspect "*) [ ! -f missing-image ];;
    "compose down") [ ! -f fail-down ];;
    "ps --no-trunc --quiet") if [ -f foreign ]; then echo other; fi;;
    "inspect --format "*) cat foreign;;
    *"app.backup_cli prepare"*) mkdir -p .restore-work/data/db; echo test > .restore-work/data/.restore-proof;;
    *) exit 0;;
esac''',
    }.items():
        path = tools / name
        path.write_text('#!/bin/sh\n' + script + '\n')
        path.chmod(0o700)
    env = dict(os.environ, PATH=str(tools) + ':' + os.environ['PATH'])
    env.pop('BACKUP_MAX_MIB', None)

    def command(action):
        return ['sh', str(scripts / 'backup.sh'), action, str(archive)]
    return project, env, command


@pytest.mark.parametrize('blocker', ['env', 'image', 'port', 'mount'])
def test_restore_conflict_before_decrypt_or_downtime(controller, blocker):
    project, env, command = controller
    if blocker == 'env':
        (project / '.env').unlink()
    elif blocker == 'image':
        (project / 'missing-image').touch()
    else:
        (project / 'foreign').write_text('elsewhere\n' + ('18081' if blocker == 'port' else str(project / 'data/private')))
    result = subprocess.run(command('restore'), input='RESTORE\n', env=env, capture_output=True, text=True)
    assert result.returncode != 0
    audit = project / 'commands'
    calls = audit.read_text() if audit.exists() else ''
    assert 'compose down' not in calls and 'app.backup_cli' not in calls
    assert not (project / '.restore-work').exists()


def test_controller_preconfirmation_signal_and_no_journal(controller):
    project, env, command = controller
    with subprocess.Popen(command('restore'), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, start_new_session=True) as proc:
        try:
            assert select.select([proc.stdout], [], [], 5)[0]
            assert b'RESTORE' in proc.stdout.readline()
            proc.send_signal(signal.SIGINT)
            assert proc.wait(timeout=5) != 0
        finally:
            if proc.poll() is None:
                proc.kill()
    assert not (project / '.restore-work').exists()
    assert not (project / '.restore-state').exists()
    assert 'compose down' not in (project / 'commands').read_text()
    result = subprocess.run(command('resume'), capture_output=True, text=True, env=env)
    assert result.returncode != 0 and '没有可续作' in result.stderr


def test_controller_resume_preserves_prior_old_directories(controller):
    project, env, command = controller
    (project / 'data/db').mkdir(parents=True)
    (project / 'data/previous').touch()
    for old in ['data.before-restore', 'data.before-restore-20260101T000000Z']:
        (project / old).mkdir()
        (project / old / 'keep').touch()
    (project / 'fail-down').touch()
    result = subprocess.run(command('restore'), input='RESTORE\n', env=env, capture_output=True, text=True)
    assert result.returncode != 0 and 'resume' in result.stderr
    old_dir = (project / '.restore-work/old-dir').read_text().strip()
    assert '--max-mib 2048' in (project / 'commands').read_text()
    (project / 'fail-down').unlink()
    subprocess.run(command('resume'), env=env, check=True, capture_output=True)
    assert (project / old_dir / 'previous').is_file()
    assert (project / 'data.before-restore/keep').is_file()
    assert (project / 'data.before-restore-20260101T000000Z/keep').is_file()
    assert not (project / '.restore-work').exists() and not (project / '.restore-state').exists()


@pytest.mark.parametrize('fault,message', [('password', '密码错误'), ('tamper', '文件已被修改'),
                                           ('format', '文件头'), ('size', '超过 BACKUP_MAX_MIB')])
def test_cli_classifies_verification_failures(tmp_path, monkeypatch, capsys, fault, message):
    archive = tmp_path / 'example.mcmbackup'
    dest = tmp_path / 'out'
    if fault == 'size':
        with archive.open('wb') as stream:
            stream.truncate(18 * 1024**2)
    elif fault == 'format':
        archive.write_bytes(b'unknown')
    else:
        source = tmp_path / 'plain'
        source.write_bytes(b'fixture')
        encrypt(source, archive, PASSWORD, 1024**2)
        if fault == 'tamper':
            with archive.open('r+b') as stream:
                stream.seek(-1, 2)
                bit = stream.read(1)[0] ^ 1
                stream.seek(-1, 2)
                stream.write(bytes([bit]))
    monkeypatch.setattr(backup_cli.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(backup_cli.getpass, 'getpass', lambda _: 'other-test-password' if fault == 'password' else PASSWORD)
    monkeypatch.setattr(backup_cli.sys, 'argv', ['backup_cli', 'verify', str(archive), str(dest), '--max-mib', '1'])
    assert backup_cli.main() == 1
    output = capsys.readouterr().err
    assert message in output and PASSWORD not in output and not dest.exists()
