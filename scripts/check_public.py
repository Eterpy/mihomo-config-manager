"""Check release inputs or Git index locally; never print matched secret values."""
import argparse
import os
from pathlib import Path
import subprocess

if __package__:
    from .package_release import content_findings, source_files
else:
    from package_release import content_findings, source_files


def git(root, *args):
    return subprocess.run(['git', '-C', str(root), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def check(root, staged=False):
    root = Path(root).resolve()
    allowed = {p.relative_to(root).as_posix() for p in source_files(root)}
    problems = []
    count = 0
    if staged:
        # Read blob object IDs from the index, not the working tree. A cleaned
        # working file must not hide a secret that is still staged for commit.
        entries = git(root, 'ls-files', '--stage', '-z').split(b'\0')
        inputs = []
        for entry in filter(None, entries):
            meta, name_bytes = entry.split(b'\t', 1)
            mode, oid, stage = meta.split()
            name = os.fsdecode(name_bytes)
            if name not in allowed or mode not in {b'100644', b'100755'} or stage != b'0':
                problems.append((name, 0, 'file is not an approved regular source file'))
                continue
            inputs.append((name, git(root, 'cat-file', 'blob', oid.decode('ascii'))))
        if not inputs and not problems:
            problems.append(('(index)', 0, 'index is empty; stage source files first'))
    else:
        # Unknown unignored files would enter `git add .`; flag them too.
        if (root / '.git').exists():
            for raw in filter(None, git(root, 'ls-files', '--cached', '--others', '--exclude-standard', '-z').split(b'\0')):
                name = os.fsdecode(raw)
                if name not in allowed:
                    problems.append((name, 0, 'file is outside the release allowlist'))
        inputs = [(name, (root / name).read_bytes()) for name in sorted(allowed)]
    for name, data in inputs:
        count += 1
        problems.extend((name, line, label) for line, label in content_findings(name, data))
    return count, problems


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--staged', action='store_true', help='inspect every file in the Git index')
    args = parser.parse_args()
    try:
        count, problems = check(Path(__file__).resolve().parents[1], args.staged)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print('检查失败：请检查 Git、源码文件和发布白名单。')
        return 1
    for name, line, label in problems:
        # repr escapes control characters in filenames supplied by contributors.
        print(f'{name!r}:{line}: {label} (value omitted)')
    if problems:
        print('发布检查未通过，请移除敏感内容或核对新增文件。')
        return 1
    print(f'已检查 {count} 个文件，未命中内置规则；仍需人工审核和历史检查。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
