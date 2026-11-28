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
    *Settings.from_env*) [ ! -f invalid-config ];;
    "compose up -d --wait public") [ ! -f fail-public ];;
    "start -ai test-worker") [ ! -f detached-worker ];;
    "inspect --format {{.State.Status}}:{{.State.ExitCode}} test-worker") echo exited:0;;
    "compose down") [ ! -f fail-down ];;
    *"python -m app.restore_guard"*)
        [ ! -f fail-guard ] || exit 1
        if [ -n "$GUARD_PYTHON" ]; then "$GUARD_PYTHON" -m app.restore_guard data .restore-work/expected-proof; fi;;
    "compose ps --all --quiet") if [ -f member ]; then echo other; fi;;
    "ps --no-trunc --quiet") if [ -f foreign ] && [ ! -f stopped ]; then echo other; fi;;
    "inspect --format "*) cat foreign;;
    *"app.backup_cli prepare"*) mkdir -p .restore-work/data/db; echo test > .restore-work/data/.restore-proof; echo test-worker;;
    *"app.backup_cli verify"*) echo test-worker;;
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


@pytest.mark.parametrize('blocker', ['env', 'image', 'port', 'mount', 'app_config', 'stopped_member'])
def test_restore_conflict_before_decrypt_or_downtime(controller, blocker):
    project, env, command = controller
    if blocker == 'env':
        (project / '.env').unlink()
    elif blocker == 'app_config':
        (project / 'data').mkdir()
        (project / 'invalid-config').touch()
    elif blocker == 'stopped_member':
        (project / 'stopped').touch()
        (project / 'member').touch()
        (project / 'foreign').write_text(str(project.parent) + '\n')
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


@pytest.mark.parametrize('state,data_in_place,checked', [
    ('prepared', True, True),      # nothing stopped or moved yet: refuse before downtime
    ('prepared', False, False),    # data already moved before the journal advanced
    ('old_moved', False, False),
    ('installed', True, False),
    ('started', True, False),
])
def test_resume_checks_settings_only_before_services_stop(controller, state, data_in_place, checked):
    project, env, command = controller
    work = project / '.restore-work'
    work.mkdir()
    (work / 'old-dir').write_text('data.before-restore-20260101T000000Z\n')
    (project / '.restore-state').write_text(f'v2:{state}\n')
    if data_in_place:
        (project / 'data/db').mkdir(parents=True)
    (project / 'invalid-config').touch()
    result = subprocess.run(command('resume'), env=env, capture_output=True, text=True)
    calls = (project / 'commands').read_text()
    assert ('Settings.from_env' in calls) == checked
    assert '-v /data' not in calls
    if checked:
        assert result.returncode != 0 and '尚未停服' in result.stderr
        assert 'compose down' not in calls


@pytest.mark.parametrize('interrupt', [signal.SIGINT, signal.SIGQUIT, signal.SIGPIPE])
def test_controller_preconfirmation_signal_and_no_journal(controller, interrupt):
    project, env, command = controller
    with subprocess.Popen(command('restore'), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, env=env, start_new_session=True) as proc:
        try:
            assert select.select([proc.stdout], [], [], 5)[0]
            assert b'RESTORE' in proc.stdout.readline()
            proc.send_signal(interrupt)
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
    resumed = subprocess.run(command('resume'), env=env, check=True, capture_output=True, text=True)
    assert '恢复完成' in resumed.stdout and old_dir in resumed.stdout
    assert not resumed.stderr
    assert (project / old_dir / 'previous').is_file()
    assert (project / 'data.before-restore/keep').is_file()
    assert (project / 'data.before-restore-20260101T000000Z/keep').is_file()
    assert not (project / '.restore-work').exists() and not (project / '.restore-state').exists()


def test_success_report_and_verify_cleanup(controller):
    project, env, command = controller
    (project / 'data/db').mkdir(parents=True)
    verified = subprocess.run(command('verify'), env=env, capture_output=True)
    assert verified.returncode == 0 and not (project / '.restore-work').exists()
    restored = subprocess.run(command('restore'), input='RESTORE\n', env=env, check=True, capture_output=True, text=True)
    old_dir, = project.glob('data.before-restore-*')
    assert '恢复完成' in restored.stdout and old_dir.name in restored.stdout
    assert not restored.stderr


@pytest.mark.parametrize('setting', ['"2048"', '${ARCHIVE_LIMIT:-2048}'])
def test_resume_after_stop_needs_no_decryption_limit(controller, setting):
    project, env, command = controller
    (project / '.env').write_text(f'BACKUP_MAX_MIB={setting}\n')
    (project / 'fail-guard').touch()
    result = subprocess.run(command('restore'), input='RESTORE\n', env={**env, 'BACKUP_MAX_MIB': '4096'},
                            capture_output=True, text=True)
    assert result.returncode != 0 and 'resume' in result.stderr
    audit = project / 'commands'
    assert '--max-mib 4096' in audit.read_text() and 'compose down' in audit.read_text()
    assert (project / '.restore-state').read_text().strip() == 'v2:installed'
    (project / 'fail-guard').unlink()
    audit.write_text('')
    resumed = subprocess.run(command('resume'), env=env, check=True, capture_output=True, text=True)
    assert '恢复完成' in resumed.stdout
    assert 'app.backup_cli' not in audit.read_text() and 'compose up -d --wait public' in audit.read_text()


@pytest.mark.parametrize('line', ['BACKUP_MAX_MIB="2048"', "BACKUP_MAX_MIB='2048'",
                                  'BACKUP_MAX_MIB=2048 # comment', 'export BACKUP_MAX_MIB=2048',
                                  'BACKUP_MAX_MIB=2048\r', ' export BACKUP_MAX_MIB = "2048" # comment\r'])
def test_verify_accepts_compose_numeric_literals(controller, line):
    project, env, command = controller
    (project / '.env').write_bytes((line + '\n').encode())
    subprocess.run(command('verify'), env=env, check=True, capture_output=True)
    assert '--max-mib 2048\n' in (project / 'commands').read_text()


def test_missing_completion_marker_cannot_exit_successfully(controller):
    project, env, command = controller
    script = project / 'scripts/backup.sh'
    # Simulate a premature exit whose last command succeeded.
    script.write_text(script.read_text().replace('echo "恢复完成。', 'exit 0\necho "恢复完成。'))
    result = subprocess.run(command('restore'), input='RESTORE\n', env=env, text=True, capture_output=True)
    assert result.returncode != 0 and '恢复完成' not in result.stdout


@pytest.mark.parametrize('value', ['0', '102401', 'invalid', '2048#not-a-comment', '"2048 # inside quotes"'])
def test_invalid_restore_limit_stops_before_docker(controller, value):
    project, env, command = controller
    (project / '.env').write_text(f'BACKUP_MAX_MIB={value}\n')
    result = subprocess.run(command('verify'), env=env, capture_output=True, text=True)
    assert result.returncode != 0 and 'BACKUP_MAX_MIB 必须是 1–102400' in result.stderr
    assert not (project / 'commands').exists()


def test_compose_conflicts_compare_physical_directories(controller):
    project, env, _ = controller
    alias = project.parent / 'deployment alias'
    alias.symlink_to(project, target_is_directory=True)
    (project / 'data').mkdir()
    def check():
        return subprocess.run(['sh', 'scripts/check-compose-project.sh'], cwd=alias, env=env, capture_output=True)
    member = project / 'member'
    member.touch()
    details = project / 'foreign'
    details.write_text(str(alias) + '\n')
    assert check().returncode == 0
    details.write_text(str(project.parent) + '\n')
    assert check().returncode != 0
    details.write_text(str(project.parent / 'missing owner') + '\n')
    assert check().returncode != 0
    member.unlink()
    details.write_text('elsewhere\n' + str(alias / 'data') + '\n')
    assert check().returncode != 0
    db = project / 'data/db/manager.sqlite3'
    db.parent.mkdir()
    db.touch()
    file_alias = project.parent / 'database alias'
    file_alias.symlink_to(db)
    for mount in [alias / 'data/db/manager.sqlite3', file_alias]:
        details.write_text(f'elsewhere\n{mount}\n')
        assert check().returncode != 0
    details.write_text(f'elsewhere\n{project / ".env"}\n')
    assert check().returncode == 0


def test_unresolvable_mount_blocks_preflight(controller):
    project, env, _ = controller
    alias = project.parent / 'alias'
    alias.symlink_to(project)
    data = project / 'data'
    (data / 'db').mkdir(parents=True)
    (data / 'db/manager.sqlite3').touch()
    mount = alias / 'data/db/manager.sqlite3'
    (project / 'foreign').write_text(f'owner=/former/deployment\n{mount}\n')
    # Also exercise the lookup failure when the test runner can traverse mode 000 (root).
    realpath = project.parent / 'bin/realpath'
    realpath.write_text('#!/bin/sh\nexit 1\n')
    realpath.chmod(0o700)
    data.chmod(0)
    try:
        result = subprocess.run(['sh', 'scripts/check-compose-project.sh'], cwd=project, env=env,
                                capture_output=True, text=True)
        assert result.returncode != 0 and '无法核实容器路径' in result.stderr
    finally:
        data.chmod(0o700)


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


@pytest.mark.parametrize('action', ['verify', 'prepare'])
def test_cli_preserves_destination_created_during_password_prompt(tmp_path, monkeypatch, capsys, action):
    archive = tmp_path / 'archive.mcmbackup'
    archive.touch()
    destination = tmp_path / 'output'
    def other_operation(_):
        destination.mkdir()
        (destination / 'keep').write_bytes(b'other operation')
        return PASSWORD
    monkeypatch.setattr(backup_cli.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(backup_cli.getpass, 'getpass', other_operation)
    monkeypatch.setattr(backup_cli.sys, 'argv', ['backup_cli', action, str(archive), str(destination)])
    assert backup_cli.main() == 1 and '目标目录已存在' in capsys.readouterr().err
    assert (destination / 'keep').read_bytes() == b'other operation'


@pytest.mark.parametrize('action', ['verify', 'prepare'])
def test_offline_cli_cleans_only_verification_result(tmp_path, application, client, upload, monkeypatch, action):
    from tests.test_backups import finish
    upload()
    ident = client.post('/api/backups', json={'password': PASSWORD}).json()['id']
    assert finish(client, ident)['status'] == 'ready'
    archive = application.state.backups.root / (ident + '.mcmbackup')
    dest = tmp_path / 'result'
    monkeypatch.setattr(backup_cli.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(backup_cli.getpass, 'getpass', lambda _: PASSWORD)
    monkeypatch.setattr(backup_cli.sys, 'argv', ['backup_cli', action, str(archive), str(dest)])
    assert backup_cli.main() == 0
    if action == 'verify':
        assert not dest.exists()
    else:
        assert (dest / '.restore-proof').is_file() and (dest / 'db/manager.sqlite3').is_file()


def test_detached_worker_stopped_before_plaintext_cleanup(controller):
    project, env, command = controller
    (project / 'detached-worker').touch()
    result = subprocess.run(command('restore'), env=env, capture_output=True)
    assert result.returncode != 0
    calls = (project / 'commands').read_text()
    assert 'src=' + str(project / '.restore-work/archive.mcmbackup') + ',dst=/archive,readonly' in calls
    assert 'rm -f test-worker' in calls and 'compose down' not in calls
    assert not (project / '.restore-work').exists()


def test_cleanup_failure_reports_plaintext(controller):
    project, env, command = controller
    (project / 'detached-worker').touch()
    rm = project.parent / 'bin/rm'
    rm.write_text('#!/bin/sh\nexit 1\n')
    rm.chmod(0o700)
    result = subprocess.run(command('restore'), env=env, capture_output=True, text=True)
    assert result.returncode != 0 and '手动清理 .restore-work' in result.stderr
    assert 'rm -f test-worker' in (project / 'commands').read_text()


def test_resume_started_admin_preserves_new_changes(controller, settings):
    from dataclasses import replace
    from app.services import Store
    from app.restore_guard import token_fingerprint
    project, env, command = controller
    settings = replace(settings, data_dir=project / 'data')
    store = Store(settings)
    src = project / 'fixture.yaml'
    src.write_bytes(b'value: 1')
    item = store.create(src, 'sample.yaml', 'sample')
    with store.db.connect() as con:
        proof = token_fingerprint(con)
    store.close()
    work = project / '.restore-work'
    work.mkdir()
    (project / 'data/.restore-proof').write_text(proof)
    (work / 'expected-proof').write_text(proof)
    (work / 'old-dir').write_text('data.before-restore-20260101T000000Z')
    (project / '.restore-state').write_text('v2:installed')
    (project / 'fail-public').touch()
    # Run the real guard at the script's guard step; Docker lifecycle operations are stubbed.
    import sys
    env.update(GUARD_PYTHON=sys.executable, PYTHONPATH=str(Path.cwd()))
    from app.restore_guard import verify_install
    result = subprocess.run(command('resume'), env=env, text=True, capture_output=True)
    assert result.returncode != 0 and '管理端修改会保留' in result.stderr
    assert (project / '.restore-state').read_text().strip() == 'v2:started'
    store = Store(settings)
    store.change(item['id'], 'disable')
    store.close()
    with pytest.raises(ValueError):
        verify_install(settings.data_dir, work / 'expected-proof')
    (project / 'fail-public').unlink()
    (project / 'commands').write_text('')
    subprocess.run(command('resume'), env=env, check=True, capture_output=True)
    calls = (project / 'commands').read_text()
    assert 'compose down' not in calls and 'app.restore_guard' not in calls
    assert not (project / '.restore-state').exists() and not work.exists()
    store = Store(settings)
    try:
        assert not store.get(item['id'])['enabled']
    finally:
        store.close()
