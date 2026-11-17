"""Package source files only; never recursively archive the project root."""
import argparse
import os
import re
import stat
import tarfile
from pathlib import Path

ROOT_FILES = (
    'Dockerfile', 'compose.yaml', 'compose.test.yaml', 'pyproject.toml',
    'requirements.in', 'requirements.txt', 'requirements-dev.in', 'requirements-dev.txt',
    '.env.example', '.dockerignore', '.gitignore', 'README.md', 'SECURITY_REVIEW.md', 'TESTING.md',
    'LICENSE', 'CONTRIBUTING.md', 'SECURITY.md', 'CHANGELOG.md',
    '.github/workflows/ci.yml', '.github/dependabot.yml',
    '.github/ISSUE_TEMPLATE/bug_report.md', '.github/ISSUE_TEMPLATE/feature_request.md',
    '.github/pull_request_template.md',
)
SOURCE_TREES = {
    'app': {'.py', '.html', '.css', '.js'},
    'nginx': {'.conf'},
    'scripts': {'.py', '.sh'},
    'tests': {'.py'},
    'docs': {'.md'},
}

# High-confidence patterns only; do not print matched values into CI logs.
SECRET_PATTERNS = {
    'private key': rb'-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----',
    'GitHub token': rb'\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})\b',
    'AWS access key': rb'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b',
    'JWT': rb'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b',
    'subscription credential': rb'/s/[0-9a-fA-F]{64}/profile\.yaml',
    'URL credentials': rb'https?://[^\s/<>"\x27]+:[^\s/<>"\x27]+@',
    'personal home path': rb'/(?:Users|home)/[A-Za-z0-9_.-]+/',
}
EXAMPLE_VALUES = {
    'CF_ACCESS_TEAM_DOMAIN': 'your-team.cloudflareaccess.com',
    'CF_ACCESS_AUD': 'replace-with-your-access-application-aud',
    'ADMIN_HOSTNAME': 'admin.example.com',
    'PUBLIC_BASE_URL': 'https://subscribe.example.com',
}


def content_findings(name, data):
    findings = []
    for label, pattern in SECRET_PATTERNS.items():
        for match in re.finditer(pattern, data):
            findings.append((data[:match.start()].count(b'\n') + 1, label))
    if name == '.env.example':
        for number, line in enumerate(data.decode('utf-8').splitlines(), 1):
            key, _, value = line.partition('=')
            if key in EXAMPLE_VALUES and value != EXAMPLE_VALUES[key]:
                findings.append((number, 'non-placeholder deployment setting'))
    return findings


def source_files(root):
    root = Path(root)
    files = [root / name for name in ROOT_FILES]
    for name, suffixes in SOURCE_TREES.items():
        tree = root / name
        if tree.is_symlink() or not tree.is_dir():
            raise ValueError('source directory must be a real directory')
        for directory, dirs, names in os.walk(tree, followlinks=False):
            for item in dirs + names:
                path = Path(directory) / item
                if path.is_symlink():
                    raise ValueError('source links are not allowed')
            dirs[:] = [d for d in dirs if not d.startswith('.') and d != '__pycache__']
            files.extend(Path(directory) / n for n in names if not n.startswith('.') and Path(n).suffix in suffixes)
    for path in files:
        if any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
            raise ValueError('source links are not allowed')
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise ValueError('source must be a regular file without links')
    return sorted(files)


def build_release(root, destination):
    root, destination = Path(root), Path(destination)
    files = source_files(root)
    for path in files:
        if content_findings(path.relative_to(root).as_posix(), path.read_bytes()):
            raise ValueError('possible sensitive content; run scripts/check_public.py')
    # Exclusive creation prevents overwriting a file or following an output link.
    fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as output:
            with tarfile.open(fileobj=output, mode='w:gz', format=tarfile.PAX_FORMAT) as archive:
                for path in files:
                    with path.open('rb') as source:
                        info = archive.gettarinfo(fileobj=source, arcname=path.relative_to(root).as_posix())
                        info.uid = info.gid = 0
                        info.uname = info.gname = ''
                        info.mtime = 0
                        info.mode = 0o644
                        info.pax_headers = {}
                        archive.addfile(info, source)
            output.flush()
            os.fsync(output.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    build_release(Path(__file__).resolve().parents[1], args.destination)
    print('源码包已生成；未包含运行数据、备份或恢复目录。')
