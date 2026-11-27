"""Use constructed synthetic markers, never production credentials."""
import subprocess
import tarfile
from pathlib import Path

import pytest

from app.settings import Settings
from scripts.check_public import check
from scripts.package_release import EXAMPLE_VALUES, build_release, content_findings, source_files

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def deployment_env(monkeypatch):
    for key, value in {
        'APP_ENV': 'production', 'CF_ACCESS_TEAM_DOMAIN': 'test.cloudflareaccess.com',
        'CF_ACCESS_AUD': 'test-audience', 'ADMIN_HOSTNAME': 'admin.test',
        'PUBLIC_BASE_URL': 'https://subscribe.test',
    }.items():
        monkeypatch.setenv(key, value)


@pytest.mark.parametrize('key', ['ADMIN_HOSTNAME', 'PUBLIC_BASE_URL'])
def test_deployment_requires_explicit_domains(deployment_env, monkeypatch, key):
    monkeypatch.delenv(key)
    with pytest.raises(ValueError):
        Settings.from_env()


@pytest.mark.parametrize('key,value', [
    ('CF_ACCESS_TEAM_DOMAIN', 'your-team.cloudflareaccess.com'),
    ('CF_ACCESS_AUD', 'replace-with-your-access-application-aud'),
    ('ADMIN_HOSTNAME', 'admin.example.com'),
    ('PUBLIC_BASE_URL', 'https://subscribe.example.com'),
    ('PUBLIC_BASE_URL', 'https://admin.test'),
    ('ADMIN_HOSTNAME', 'admin..test'),
    ('PUBLIC_BASE_URL', 'https://subscribe.test:'),
    ('PUBLIC_BASE_URL', 'https://subscribe.test:443'),
    ('PUBLIC_BASE_URL', 'https://subscribe.test/path'),
    ('PUBLIC_BASE_URL', 'https://subscribe.test?x=1'),
])
def test_invalid_or_example_deployment_rejected(deployment_env, monkeypatch, key, value):
    monkeypatch.setenv(key, value)
    with pytest.raises(ValueError):
        Settings.from_env()


def test_custom_domains_used_in_origin_and_urls(deployment_env, monkeypatch):
    monkeypatch.setenv('ADMIN_HOSTNAME', 'ADMIN.TEST')
    monkeypatch.setenv('PUBLIC_BASE_URL', 'https://SUBSCRIBE.TEST/')
    settings = Settings.from_env()
    assert settings.origin == 'https://admin.test'
    assert settings.public_base_url == 'https://subscribe.test'


@pytest.mark.parametrize('marker', [
    b'-----BEGIN ' + b'PRIVATE KEY-----',
    b'gh' + b'p_' + b'A' * 36,
    b'AK' + b'IA' + b'X' * 16,
    b'eyJ' + b'A' * 20 + b'.' + b'B' * 20 + b'.' + b'C' * 20,
    b'/s/' + b'a' * 64 + b'/profile.yaml',
    b'https://' + b'synthetic:password@host.test',
    b'/Users/' + b'synthetic/private/',
])
def test_common_secret_markers_detected(marker):
    findings = content_findings('app/example.py', b'line one\n' + marker)
    assert findings and findings[0][0] == 2
    assert marker.decode() not in str(findings)


def test_placeholder_settings_checked():
    assert content_findings('.env.example', b'ADMIN_HOSTNAME=admin.test\n')
    sample = '\n'.join(f'{key}={value}' for key, value in EXAMPLE_VALUES.items()).encode()
    assert not content_findings('.env.example', sample)


def test_actual_release_contents_and_metadata(tmp_path):
    if not (ROOT / 'LICENSE').exists():
        pytest.skip('full source archive is checked on host')
    output = tmp_path / 'source.tar.gz'
    build_release(ROOT, output)
    with tarfile.open(output) as archive:
        names = set(archive.getnames())
        assert names == {p.relative_to(ROOT).as_posix() for p in source_files(ROOT)}
        assert {'LICENSE', '.github/workflows/ci.yml', 'docs/deployment.md',
                'app/static/brand.png', 'app/static/brand-license.txt'} <= names
        for member in archive:
            assert member.isfile() and member.uid == member.gid == member.mtime == 0
            assert member.uname == member.gname == ''
            assert not content_findings(member.name, archive.extractfile(member).read())


def test_git_index_checked_even_if_worktree_is_clean(tmp_path):
    if not (ROOT / 'LICENSE').exists():
        pytest.skip('full source archive is checked on host')
    for path in source_files(ROOT):
        dest = tmp_path / path.relative_to(ROOT)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(path.read_bytes())
    def git(*args):
        subprocess.run(['git', '-C', str(tmp_path), *args], check=True, capture_output=True)
    git('init')
    git('add', '.')
    assert check(tmp_path, staged=True)[1] == []
    source = tmp_path / 'app/__init__.py'
    original = source.read_bytes()
    source.write_bytes(b'# /s/' + b'a' * 64 + b'/profile.yaml\n')
    git('add', 'app/__init__.py')
    source.write_bytes(original)
    (tmp_path / '.env').write_text('synthetic fixture')
    git('add', '-f', '.env')
    problems = check(tmp_path, staged=True)[1]
    assert any(p[0] == '.env' for p in problems)
    assert any(p[0] == 'app/__init__.py' and p[2] == 'subscription credential' for p in problems)
