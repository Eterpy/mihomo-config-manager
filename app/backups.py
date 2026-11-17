"""Versioned authenticated archives. Never parse unauthenticated plaintext."""
import concurrent.futures
import gzip
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import uuid
from pathlib import Path
from datetime import datetime

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from .errors import AppError
from .database import validate_schema
from .services import atomic_copy, digest, now, sync_dir

MAGIC = b'MCMBAK01'
HEADER = 36
CHUNK = 65536


def key(password, salt):
    return Scrypt(salt=salt, length=32, n=2**16, r=8, p=2).derive(password.encode('utf-8'))


def encrypt(source, destination, password, limit):
    header = MAGIC + secrets.token_bytes(16) + secrets.token_bytes(12)
    ctx = Cipher(algorithms.AES(key(password, header[8:24])), modes.GCM(header[24:])).encryptor()
    ctx.authenticate_additional_data(header)
    with open(source, 'rb') as src, open(destination, 'xb') as out:
        os.chmod(destination, 0o600)
        out.write(header)
        while block := src.read(CHUNK):
            out.write(ctx.update(block))
            if out.tell() + 16 > limit:
                raise ValueError('quota')
        out.write(ctx.finalize() + ctx.tag)
        out.flush()
        os.fsync(out.fileno())


def decrypt(source, destination, password, limit):
    with open(source, 'rb') as src, open(destination, 'xb') as out:
        os.chmod(destination, 0o600)
        size = os.fstat(src.fileno()).st_size
        if size < HEADER + 16 or size > limit + 16 * 1024 * 1024:
            raise ValueError('size')
        header = src.read(HEADER)
        if header[:8] != MAGIC:
            raise ValueError('format')
        src.seek(-16, 2)
        tag = src.read(16)
        src.seek(HEADER)
        ctx = Cipher(algorithms.AES(key(password, header[8:24])), modes.GCM(header[24:], tag)).decryptor()
        ctx.authenticate_additional_data(header)
        remaining = size - HEADER - 16
        while remaining:
            block = src.read(min(CHUNK, remaining))
            if not block:
                raise ValueError('truncated')
            remaining -= len(block)
            out.write(ctx.update(block))
        out.write(ctx.finalize())


class RegularTarInfo(tarfile.TarInfo):
    def _proc_member(self, tarfile_object):
        # Reject extension records before tarfile allocates their payload.
        if self.type not in (tarfile.REGTYPE, tarfile.AREGTYPE) or self.size < 0:
            raise ValueError('unsupported tar member')
        return super()._proc_member(tarfile_object)


class BoundedReader:
    """Bound gzip expansion and tar metadata allocation before tarfile parses it."""
    def __init__(self, stream, limit):
        self.stream, self.remaining = stream, limit

    def read(self, size=-1):
        if size < 0 or size > 16 * 1024 * 1024:
            raise ValueError('archive metadata size')
        data = self.stream.read(min(size, self.remaining + 1))
        self.remaining -= len(data)
        if self.remaining < 0:
            raise ValueError('archive expansion')
        return data


def unpack_verified(archive, password, destination, limit=1024**3):
    """Destination must be a fresh private directory. No extractall, links or SQL execution."""
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(dir=destination) as work:
        compressed = Path(work) / 'authenticated.tar.gz'
        decrypt(archive, compressed, password, limit)
        seen, total = set(), 0
        with gzip.open(compressed, 'rb') as zipped, tarfile.open(fileobj=BoundedReader(zipped, limit + 128 * 1024 * 1024), mode='r|', tarinfo=RegularTarInfo) as tar:
            for entry in tar:
                name = entry.name
                parts = name.split('/')
                valid = name in {'manifest.json', 'db/manager.sqlite3'}
                if len(parts) == 4 and parts[0] == 'private' and parts[2] == 'versions':
                    try:
                        valid = str(uuid.UUID(parts[1])) == parts[1] and parts[3] == f'{int(parts[3][:-5])}.yaml' and int(parts[3][:-5]) > 0
                    except (ValueError, IndexError):
                        valid = False
                if not valid or not entry.isfile() or name in seen or entry.size < 0:
                    raise ValueError('entry')
                seen.add(name)
                total += entry.size
                if total > limit or len(seen) > 100000 or (name == 'manifest.json' and entry.size > 16 * 1024 * 1024):
                    raise ValueError('expanded size')
                target = destination / name
                target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                with tar.extractfile(entry) as src, target.open('xb') as out:
                    shutil.copyfileobj(src, out, CHUNK)
                target.chmod(0o600)
    manifest = json.loads((destination / 'manifest.json').read_text())
    stamp = manifest.get('created_at')
    if not isinstance(stamp, str) or len(stamp) > 40 or datetime.fromisoformat(stamp).tzinfo is None:
        raise ValueError('timestamp')
    if manifest.get('format') != 1 or set(manifest['files']) != seen - {'manifest.json'}:
        raise ValueError('manifest')
    for name, meta in manifest['files'].items():
        path = destination / name
        if path.stat().st_size != meta['size'] or digest(path) != meta['sha256']:
            raise ValueError('digest')
    db = destination / 'db/manager.sqlite3'
    con = sqlite3.connect(f'{db.as_uri()}?mode=ro', uri=True)
    try:
        con.execute('PRAGMA trusted_schema=OFF')
        validate_schema(con)
        if con.execute('PRAGMA user_version').fetchone()[0] != 2 or con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or con.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('database')
        configs = con.execute('SELECT id,token,status,version,size,sha256,enabled FROM configs').fetchall()
        versions = con.execute('SELECT config_id,version,size,sha256 FROM versions').fetchall()
        if con.execute('SELECT COUNT(*) FROM operations').fetchone()[0] or any(r[2] != 'ready' for r in configs):
            raise ValueError('pending')
        expected = {'db/manager.sqlite3'}
        tokens = set()
        for cid, token, _, current, size, sha, enabled in configs:
            if str(uuid.UUID(cid)) != cid or len(token) != 64 or any(c not in '0123456789abcdef' for c in token) or token in tokens or enabled not in (0, 1):
                raise ValueError('config')
            tokens.add(token)
            if (cid, current, size, sha) not in versions or current != max(v[1] for v in versions if v[0] == cid):
                raise ValueError('current')
        for cid, version, size, sha in versions:
            name = f'private/{cid}/versions/{version}.yaml'
            if manifest['files'].get(name) != {'size': size, 'sha256': sha}:
                raise ValueError('version')
            expected.add(name)
        if expected != set(manifest['files']):
            raise ValueError('files')
    finally:
        con.close()
    return {'configs': len(configs), 'versions': len(versions), 'created_at': manifest['created_at']}


class Backups:
    def __init__(self, store):
        self.store = store
        self.root = store.root / 'backups'
        if self.root.is_symlink():
            raise ValueError('backup directory')
        self.root.mkdir(mode=0o700, exist_ok=True)
        self.root.chmod(0o700)
        self.gate = threading.Lock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        with store.db.connect() as con:
            con.execute("UPDATE backup_jobs SET status='failed',error_code='interrupted' WHERE status NOT IN ('ready','failed')")
            ready = {r[0] + '.mcmbackup' for r in con.execute("SELECT id FROM backup_jobs WHERE status='ready'")}
        for path in self.root.iterdir():
            if path.name not in ready or path.is_symlink():
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()

    @staticmethod
    def check_size(size, limit):
        if size > limit:
            raise ValueError("size")

    def close(self):
        self.pool.shutdown(wait=True)

    def list(self):
        with self.store.db.connect() as con:
            return [dict(r) for r in con.execute('SELECT * FROM backup_jobs ORDER BY created_at DESC')]

    def get(self, ident):
        with self.store.db.connect() as con:
            row = con.execute('SELECT * FROM backup_jobs WHERE id=?', (ident,)).fetchone()
        if row is None:
            raise AppError(404, '备份不存在')
        return dict(row)

    def update(self, ident, state, **values):
        with self.store.db.connect() as con:
            con.execute('UPDATE backup_jobs SET status=?' + ''.join(f', {k}=?' for k in values) + ' WHERE id=?', (state, *values.values(), ident))

    def create(self, password):
        self.store._writable()
        if not 16 <= len(password) <= 256:
            raise AppError(422, '密码长度必须为 16–256 个字符')
        if not self.gate.acquire(blocking=False):
            raise AppError(429, '已有备份任务正在执行')
        ident = str(uuid.uuid4())
        try:
            with self.store.db.connect() as con:
                con.execute("INSERT INTO backup_jobs(id,status,created_at) VALUES (?,'queued',?)", (ident, now()))
            self.pool.submit(self.run, ident, password)
            return self.get(ident)
        except Exception:
            self.gate.release()
            raise

    def run(self, ident, password):
        work = self.root / ('.work-' + ident)
        output = self.root / (ident + '.mcmbackup')
        try:
            work.mkdir(mode=0o700)
            snapshot = work / 'snapshot'
            snapshot.mkdir(mode=0o700)
            self.update(ident, 'snapshotting')
            self.store.snapshotting = True
            try:
                with self.store.lock:
                    self.store._writable()
                    with self.store.db.connect() as con:
                        rows = con.execute('SELECT * FROM versions').fetchall()
                        total = sum(r['size'] for r in rows) + self.store.db.path.stat().st_size
                        used = sum(p.stat().st_size for p in self.root.glob('*.mcmbackup'))
                        limit = self.store.settings.backup_max_bytes
                        quota = self.store.settings.backup_quota_bytes - used
                        if len(rows) > 50000 or total > limit or quota < total + 1024*1024 or shutil.disk_usage(self.root).free < total * 3 + 64*1024*1024:
                            raise ValueError('space')
                        (snapshot / 'db').mkdir(mode=0o700)
                        target = sqlite3.connect(snapshot / 'db/manager.sqlite3')
                        try:
                            con.backup(target, pages=256, progress=lambda status, remaining, pages: self.check_size(pages * 4096, limit))
                        finally:
                            target.close()
                        (snapshot / 'db/manager.sqlite3').chmod(0o600)
                        for row in rows:
                            source, _ = self.store.historical(row['config_id'], row['version'])
                            dest = snapshot / source.relative_to(self.store.root)
                            atomic_copy(source, dest)
            finally:
                self.store.snapshotting = False
            self.update(ident, 'encrypting')
            files = {p.relative_to(snapshot).as_posix(): {'size': p.stat().st_size, 'sha256': digest(p)} for p in snapshot.rglob('*') if p.is_file()}
            manifest = {'format': 1, 'created_at': now(), 'files': files}
            (snapshot / 'manifest.json').write_text(json.dumps(manifest))
            if sum(p.stat().st_size for p in snapshot.rglob('*') if p.is_file()) > limit:
                raise ValueError('size')
            archive = work / 'archive.tar.gz'
            with tarfile.open(archive, 'w:gz', format=tarfile.USTAR_FORMAT) as tar:
                for name in [*files, 'manifest.json']:
                    tar.add(snapshot / name, arcname=name, recursive=False)
            encrypted = work / 'encrypted'
            encrypt(archive, encrypted, password, quota)
            sha, size = digest(encrypted), encrypted.stat().st_size
            os.replace(encrypted, output)
            sync_dir(self.root)
            self.update(ident, 'ready', size=size, sha256=sha)
        except Exception:
            output.unlink(missing_ok=True)
            self.update(ident, 'failed', error_code='backup_failed')
        finally:
            password = None
            shutil.rmtree(work, ignore_errors=True)
            self.gate.release()

    def download(self, ident):
        row = self.get(ident)
        if row['status'] != 'ready':
            raise AppError(409, '备份尚未完成')
        path = self.root / (ident + '.mcmbackup')
        if path.is_symlink() or not path.is_file():
            raise AppError(503, '备份文件不可用')
        return path

    def delete(self, ident):
        row = self.get(ident)
        if row['status'] not in {'ready', 'failed'}:
            raise AppError(409, '任务进行中，不能删除')
        (self.root / (ident + '.mcmbackup')).unlink(missing_ok=True)
        sync_dir(self.root)
        with self.store.db.connect() as con:
            con.execute('DELETE FROM backup_jobs WHERE id=?', (ident,))
