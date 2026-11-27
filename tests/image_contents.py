"""Check built CI images against the host release manifest, without mounting host data."""
from pathlib import Path
import subprocess
import sys

from scripts.package_release import source_files


def check(target, image):
    root = Path.cwd()
    trees = {'app'}
    expected = {'requirements.txt'}
    if target == 'test':
        trees.update(('tests', 'scripts'))
        expected.update(('requirements-dev.txt', 'pyproject.toml'))
    for path in source_files(root):
        relative = path.relative_to(root)
        if relative.parts[0] in trees:
            expected.add(relative.as_posix())
    result = subprocess.run(['docker', 'run', '--rm', '--network', 'none', '--read-only',
                             '--entrypoint', 'find', image, '.', '-type', 'f', '-print0'],
                            check=True, capture_output=True)
    actual = {entry.decode().removeprefix('./') for entry in result.stdout.split(b'\0') if entry}
    if actual != expected:
        raise SystemExit(f'{target} image differs from manifest: {len(expected - actual)} missing, '
                         f'{len(actual - expected)} unexpected files')


if __name__ == '__main__':
    check(*sys.argv[1:])
