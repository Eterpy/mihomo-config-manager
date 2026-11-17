"""Fail closed before publishing a resumed restore. No secrets in output."""
import hashlib
import hmac
import json
import re
import sqlite3
import sys
from pathlib import Path
from .database import validate_schema


def token_fingerprint(con):
    rows = [tuple(r) for r in con.execute('SELECT id,token,enabled FROM configs ORDER BY id')]
    return hashlib.sha256(json.dumps(rows, separators=(',', ':')).encode()).hexdigest()


def read_proof(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size != 64:
        raise ValueError('restore proof')
    value = path.read_text(encoding='ascii')
    if not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('restore proof')
    return value


def verify_install(root, expected_proof):
    root = Path(root)
    expected_proof = Path(expected_proof)
    proof, database = root / '.restore-proof', root / 'db/manager.sqlite3'
    if any(p.is_symlink() for p in (root, proof, database, database.parent, expected_proof, expected_proof.parent)):
        raise ValueError('restore path')
    # The controller pins this value BEFORE moving either data directory.
    # A previous restore can carry a perfectly valid proof of its own.
    if expected_proof.resolve().is_relative_to(root.resolve()):
        raise ValueError('restore reference must be external')
    expected = read_proof(expected_proof)
    if not hmac.compare_digest(read_proof(proof), expected):
        raise ValueError('restore identity')
    # Prepared database is checkpointed before installation. Never ignore a WAL.
    wal = database.with_name(database.name + '-wal')
    if wal.exists() and wal.stat().st_size:
        raise ValueError('uncheckpointed restore')
    con = sqlite3.connect(database.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    try:
        con.execute('PRAGMA trusted_schema=OFF')
        validate_schema(con)
        if not hmac.compare_digest(expected, token_fingerprint(con)):
            raise ValueError('restore identity')
        if con.execute('SELECT COUNT(*) FROM operations').fetchone()[0]:
            raise ValueError('pending restore')
        allowed = {r[0] + '.yaml' for r in con.execute('SELECT token FROM configs WHERE enabled=1')}
        published = root / 'published'
        if published.is_symlink() or any(p.is_symlink() or not p.is_file() or p.name not in allowed for p in published.iterdir()):
            raise ValueError('unexpected publication')
    finally:
        con.close()


if __name__ == '__main__':
    try:
        verify_install(Path(sys.argv[1]), Path(sys.argv[2]))
    except Exception:
        print('恢复状态核验失败，保持公开服务停止；请检查恢复记录和数据目录。', file=sys.stderr)
        raise SystemExit(1)
