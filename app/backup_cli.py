"""Offline verifier/preparer; credentials are read only from a terminal."""
import argparse
import getpass
import json
import os
import secrets
import shutil
import sqlite3
import sys
from pathlib import Path
from .backups import unpack_verified
from .services import sync_dir
from .restore_guard import token_fingerprint


def prepare(archive, destination, password, limit):
    result = unpack_verified(archive, password, destination, limit)
    db = destination / 'db/manager.sqlite3'
    con = sqlite3.connect(db)
    try:
        con.execute("BEGIN IMMEDIATE")
        con.execute('PRAGMA trusted_schema=OFF')
        old = {r[0] for r in con.execute('SELECT token FROM configs')}
        original_tokens = set(old)
        for (ident,) in con.execute('SELECT id FROM configs').fetchall():
            token = secrets.token_hex(32)
            for attempt in range(8):
                if token not in old:
                    break
                token = secrets.token_hex(32)
            else:
                raise ValueError("token collision")
            old.add(token)
            con.execute('UPDATE configs SET token=? WHERE id=?', (token, ident))
        con.execute('DELETE FROM backup_jobs')
        fresh = {r[0] for r in con.execute('SELECT token FROM configs')}
        original = original_tokens
        if len(fresh) != result['configs'] or any(token in original for token in fresh):
            raise ValueError('token rotation')
        con.commit()
        con.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        con.execute('PRAGMA journal_mode=DELETE')
        proof = token_fingerprint(con)
    finally:
        con.close()
    (destination / '.restore-proof').write_text(proof)
    (destination / '.restore-proof').chmod(0o600)
    (destination / 'manifest.json').unlink()
    (destination / 'published').mkdir(mode=0o755)
    for path in destination.rglob('*'):
        if path.is_file():
            with path.open('rb') as stream:
                os.fsync(stream.fileno())
    for directory in sorted((p for p in destination.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        sync_dir(directory)
    sync_dir(destination)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['verify', 'prepare'])
    parser.add_argument('archive', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--max-mib', type=int, default=1024)
    args = parser.parse_args()
    os.umask(0o077)
    if not sys.stdin.isatty() or args.destination.exists() or args.archive.is_symlink() or not 1 <= args.max_mib <= 102400:
        print('需要交互终端、普通归档文件及不存在的暂存目标。', file=sys.stderr)
        return 1
    success = False
    try:
        password = getpass.getpass('备份密码：')
        if args.action == 'prepare':
            result = prepare(args.archive, args.destination, password, args.max_mib * 1048576)
        else:
            result = unpack_verified(args.archive, password, args.destination, args.max_mib * 1048576)
        password = None
        print(json.dumps(result, ensure_ascii=False))
        success = True
        return 0
    except Exception:
        print('备份校验失败：密码错误、归档损坏、格式不支持或存储不可用。', file=sys.stderr)
        return 1
    finally:
        if args.action == 'verify' or not success:
            shutil.rmtree(args.destination, ignore_errors=True)


if __name__ == '__main__':
    raise SystemExit(main())
