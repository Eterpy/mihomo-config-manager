"""Versioned authenticated archives. Never parse unauthenticated plaintext."""
import concurrent.futures
import errno
from collections import defaultdict
from contextlib import contextmanager
import gzip
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import stat
import tarfile
import tempfile
import threading
import uuid
from pathlib import Path
from datetime import datetime, timezone

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from .errors import AppError
from .database import validate_schema
from .services import atomic_copy, digest, now, sync_dir

MAGIC = b'MCMBAK01'
HEADER = 36
CHUNK = 65536


class ArchiveLimit(ValueError):
    pass


class ArchiveFormat(ValueError):
    pass


class SchemaMismatch(ValueError):
    pass


class BackupFailure(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def tar_bytes(sizes):
    blocks = 2 + sum(1 + (size + 511) // 512 for size in sizes)
    return ((blocks + 19) // 20) * 10240


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
                raise BackupFailure('quota_exceeded')
        out.write(ctx.finalize() + ctx.tag)
        out.flush()
        os.fsync(out.fileno())


def decrypt(source, destination, password, limit):
    with open(source, 'rb') as src, open(destination, 'xb') as out:
        os.chmod(destination, 0o600)
        size = os.fstat(src.fileno()).st_size
        if size < HEADER + 16:
            raise ArchiveFormat('format')
        if size > limit + 16 * 1024 * 1024:
            raise ArchiveLimit('size')
        header = src.read(HEADER)
        if header[:8] != MAGIC:
            raise ArchiveFormat('format')
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
            raise ArchiveLimit('archive expansion')
        return data


def unpack_verified(archive, password, destination, limit=1024**3):
    """Destination must be a fresh private directory. No extractall, links or SQL execution."""
    destination = Path(destination)
    destination.mkdir(mode=0o700, parents=True, exist_ok=False)
    return unpack_into_empty_directory(archive, password, destination, limit)


def unpack_into_empty_directory(archive, password, destination, limit):
    """Only use with a private directory exclusively created by this caller."""
    if destination.is_symlink() or any(destination.iterdir()):
        raise FileExistsError("restore destination occupied")
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
                    raise ArchiveLimit('expanded size')
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
        try:
            validate_schema(con)
        except ValueError:
            raise SchemaMismatch('schema') from None
        if con.execute('PRAGMA user_version').fetchone()[0] != 2:
            raise SchemaMismatch('schema')
        if con.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or con.execute('PRAGMA foreign_key_check').fetchone():
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


def archive_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class ArchiveVerification:
    """Share a successful digest check among requests for the same opened file."""
    def __init__(self):
        self.lock = threading.Lock()
        self.verified = None

    def matches(self, archive, expected):
        with self.lock:
            identity = archive_identity(os.fstat(archive.fileno()))
            key = (identity, expected)
            if self.verified == key:
                return True
            actual = hashlib.file_digest(archive, 'sha256').hexdigest()
            after = os.fstat(archive.fileno())
            if identity != archive_identity(after):
                # Deleting an open archive changes ctime but leaves the descriptor usable.
                if after.st_nlink:
                    raise AppError(409, '备份归档在校验期间发生变化，请重新下载')
                return actual == expected
            if actual != expected:
                return False
            self.verified = key
            return True


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
        self.archive_lock = threading.RLock()
        self.archive_checks = defaultdict(ArchiveVerification)
        for path in self.root.iterdir():
            if path.is_symlink():
                path.unlink()
            elif path.name.startswith('.work-') and path.is_dir():
                shutil.rmtree(path)
        self.list()

    @staticmethod
    def check_size(size, limit):
        if size > limit:
            raise ArchiveLimit('size')

    def close(self):
        self.pool.shutdown(wait=True)

    def archives(self):
        return [p for p in self.root.glob('*.mcmbackup') if not p.is_symlink() and p.is_file()]

    @staticmethod
    def unregistered_id(path):
        return 'file-' + hashlib.sha256(os.fsencode(path.name)).hexdigest()

    def list(self):
        with self.archive_lock, self.store.db.connect() as con:
            files = {path.name: path for path in self.archives()}
            used = sum(p.stat().st_size for p in files.values())
            items = [dict(r) for r in con.execute('SELECT * FROM backup_jobs ORDER BY created_at DESC')]
            for item in items:
                path = files.pop(item['id'] + '.mcmbackup', None)
                if item['status'] != 'ready':
                    continue
                code = None
                if path is None:
                    code = 'archive_missing'
                elif path.stat().st_size != item['size']:
                    code = 'archive_corrupt'
                if code:
                    con.execute("UPDATE backup_jobs SET status='failed', error_code=? WHERE id=?", (code, item['id']))
                    item.update(status='failed', error_code=code)
            for path in files.values():
                info = path.stat()
                items.append({'id': self.unregistered_id(path), 'filename': path.name, 'status': 'unregistered',
                              'created_at': datetime.fromtimestamp(info.st_mtime, timezone.utc).isoformat(),
                              'size': info.st_size, 'sha256': None, 'error_code': None})
            return {'items': items, 'used_bytes': used, 'quota_bytes': self.store.settings.backup_quota_bytes,
                    'max_bytes': self.store.settings.backup_max_bytes}

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
                        used = sum(p.stat().st_size for p in self.archives())
                        limit = self.store.settings.backup_max_bytes
                        quota = self.store.settings.backup_quota_bytes - used
                        if len(rows) > 50000 or tar_bytes([self.store.db.path.stat().st_size, *(r['size'] for r in rows)]) > limit:
                            raise ArchiveLimit('size')
                        if shutil.disk_usage(self.root).free < total * 3 + 16 * 1024**2:
                            raise BackupFailure('disk_space')
                        (snapshot / 'db').mkdir(mode=0o700)
                        target = sqlite3.connect(snapshot / 'db/manager.sqlite3')
                        try:
                            page_size = con.execute("PRAGMA page_size").fetchone()[0]
                            con.backup(target, pages=256, progress=lambda status, remaining, pages: self.check_size(pages * page_size, limit))
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
            self.check_size(tar_bytes(p.stat().st_size for p in snapshot.rglob('*') if p.is_file()), limit)
            archive = work / 'archive.tar.gz'
            with tarfile.open(archive, 'w:gz', format=tarfile.USTAR_FORMAT) as tar:
                for name in [*files, 'manifest.json']:
                    tar.add(snapshot / name, arcname=name, recursive=False)
            encrypted = work / 'encrypted'
            encrypt(archive, encrypted, password, quota)
            sha, size = digest(encrypted), encrypted.stat().st_size
            with self.archive_lock:
                os.replace(encrypted, output)
                sync_dir(self.root)
                self.update(ident, 'ready', size=size, sha256=sha)
        except Exception as exc:
            output.unlink(missing_ok=True)
            code = 'storage_error'
            if isinstance(exc, BackupFailure):
                code = exc.code
            elif isinstance(exc, ArchiveLimit):
                code = 'too_large'
            elif isinstance(exc, OSError) and exc.errno == errno.ENOSPC:
                code = 'disk_space'
            self.update(ident, 'failed', error_code=code)
        finally:
            password = None
            shutil.rmtree(work, ignore_errors=True)
            self.gate.release()

    @contextmanager
    def download(self, ident):
        with self.archive_lock:
            row = self.get(ident)
            if row['status'] != 'ready':
                raise AppError(409, '备份不可下载')
            path = self.root / (ident + '.mcmbackup')
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            except FileNotFoundError:
                self.update(ident, 'failed', error_code='archive_missing')
                raise AppError(409, '备份归档缺失') from None
            except OSError as exc:
                if exc.errno != errno.ELOOP:
                    raise
                self.update(ident, 'failed', error_code='archive_corrupt')
                raise AppError(409, '备份归档异常') from None
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size != row['size']:
                os.close(fd)
                self.update(ident, 'failed', error_code='archive_corrupt')
                raise AppError(409, '备份完整性校验失败')
            archive = os.fdopen(fd, 'rb')
            check = self.archive_checks[ident]
        # Verify and serve the open inode. Lists and deletions can proceed during hashing.
        with archive:
            if not check.matches(archive, row['sha256']):
                with self.archive_lock:
                    try:
                        same_file = archive_identity(path.lstat()) == archive_identity(info)
                    except FileNotFoundError:
                        same_file = False
                    if same_file:
                        self.update(ident, 'failed', error_code='archive_corrupt')
                raise AppError(409, '备份完整性校验失败')
            archive.seek(0)
            yield archive, row

    def delete(self, ident):
        with self.archive_lock, self.store.db.connect() as con:
            if ident.startswith('file-'):
                for path in self.archives():
                    if self.unregistered_id(path) == ident and not con.execute(
                        'SELECT 1 FROM backup_jobs WHERE id=?', (path.stem,)
                    ).fetchone():
                        path.unlink()
                        sync_dir(self.root)
                        return
                raise AppError(404, '未登记的备份不存在')
            row = self.get(ident)
            if row['status'] not in {'ready', 'failed'}:
                raise AppError(409, '任务进行中，不能删除')
            (self.root / (str(uuid.UUID(ident)) + '.mcmbackup')).unlink(missing_ok=True)
            sync_dir(self.root)
            con.execute('DELETE FROM backup_jobs WHERE id=?', (ident,))
            self.archive_checks.pop(ident, None)
