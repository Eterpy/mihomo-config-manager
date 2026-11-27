from contextlib import closing
import fcntl
import hashlib
import logging
import os
import re
import secrets
import shutil
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .database import Database
from .errors import AppError, MissingDatabase
from .settings import Settings
from .validation import display_name

log = logging.getLogger("mcm.storage")
TOKEN = re.compile(r"[0-9a-f]{64}")


def now():
    return datetime.now(timezone.utc).isoformat()


def next_update(previous):
    current = datetime.now(timezone.utc)
    last = datetime.fromisoformat(previous)
    return max(current, last + timedelta(microseconds=1)).isoformat()


def digest(path):
    with open(path, "rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def sync_dir(path: Path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_copy(source: Path, destination: Path, mode=0o600):
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=destination.parent)
    temporary = Path(name)
    try:
        with os.fdopen(fd, "wb") as out, source.open("rb") as src:
            shutil.copyfileobj(src, out, length=64 * 1024)
            out.flush()
            os.fchmod(out.fileno(), mode)
            os.fsync(out.fileno())
        os.replace(temporary, destination)
        sync_dir(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


class Store:
    """One process, one write lock; durable intents bridge SQLite and files."""
    def __init__(self, settings: Settings):
        self.settings = settings
        self.root = settings.data_dir
        self.private = self.root / "private"
        self.published = self.root / "published"
        self.incoming = self.private / ".incoming"
        self.lock = threading.RLock()
        self.snapshotting = False
        self.failed = False
        self.ready = False
        for directory, mode in [(self.root, 0o700), (self.root / "db", 0o700), (self.private, 0o700), (self.incoming, 0o700), (self.published, 0o755)]:
            directory.mkdir(parents=True, exist_ok=True, mode=mode)
            if directory.is_symlink():
                raise RuntimeError("数据目录不能是符号链接")
            os.chmod(directory, mode)
        self._lockfile = (self.root / "db" / ".writer.lock").open("a")
        try:
            fcntl.flock(self._lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
            path = self.root / "db" / "manager.sqlite3"
            marker = path.with_name(".initialized")
            if path.is_symlink() or marker.is_symlink():
                raise RuntimeError("数据库路径异常")
            version = 0
            if path.exists():
                with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
                    version = con.execute("PRAGMA user_version").fetchone()[0]
            backups = self.root / "backups"
            if not version and (marker.exists() or any(p != self.incoming for p in self.private.iterdir())
                                or any(self.published.iterdir()) or backups.is_symlink()
                                or (backups.exists() and any(backups.iterdir()))
                                or any(path.parent.glob("manager.sqlite3-*"))):
                raise MissingDatabase("数据库缺失或未初始化，数据目录中仍有记录。已停止启动，请从加密备份恢复或人工核查。")
            self.db = Database(path)
            if not marker.exists():
                with marker.open("xb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    os.fsync(stream.fileno())
                sync_dir(path.parent)
            self.recover()
        except Exception:
            self.close()
            raise

    def close(self):
        if not self._lockfile.closed:
            fcntl.flock(self._lockfile, fcntl.LOCK_UN)
            self._lockfile.close()

    def _row(self, con, config_id):
        row = con.execute("SELECT * FROM configs WHERE id = ?", (config_id,)).fetchone()
        if row is None:
            raise AppError(404, "配置不存在")
        return dict(row)

    def _view(self, row):
        row = dict(row)
        row.pop("token")
        row["enabled"] = bool(row["enabled"])
        return row

    def subscription_url(self, config_id):
        with self.lock, self.db.connect() as con:
            token = self._row(con, config_id)["token"]
            return f"{self.settings.public_base_url}/s/{token}/profile.yaml"

    def check_revision(self, row, field, expected):
        if row[field] != expected:
            raise AppError(409, "配置已在其他页面修改，请刷新后重试", code="stale", current=self._view(row))

    def list(self):
        with self.lock, self.db.connect() as con:
            return [self._view(r) for r in con.execute("SELECT * FROM configs ORDER BY created_at DESC, id")]

    def get(self, config_id):
        with self.lock, self.db.connect() as con:
            return self._view(self._row(con, config_id))

    def preview(self, config_id):
        with self.lock, self.db.connect() as con:
            row = self._row(con, config_id)
            try:
                return self._version_path(config_id, row["version"]).read_bytes()
            except OSError:
                raise AppError(503, "私有文件不可用，请检查存储") from None

    def _writable(self):
        if self.failed or not self.ready:
            raise AppError(503, "存储存在未完成操作，请检查磁盘和权限后重启管理端恢复")

    def _version_path(self, config_id, version):
        return self.private / str(uuid.UUID(config_id)) / "versions" / f"{int(version)}.yaml"

    def _token_path(self, token):
        if not TOKEN.fullmatch(token):
            raise RuntimeError("存储中的 Token 格式损坏")
        return self.published / f"{token}.yaml"

    def _new_token(self, con):
        for _ in range(8):
            token = secrets.token_hex(32)
            if not con.execute("SELECT 1 FROM configs WHERE token = ? UNION SELECT 1 FROM operations WHERE old_token = ?", (token, token)).fetchone() and not self._token_path(token).exists():
                return token
        raise AppError(503, "暂时无法生成唯一订阅凭据，请重试")

    def _store_version(self, source, config_id, version):
        destination = self._version_path(config_id, version)
        atomic_copy(source, destination)
        sync_dir(destination.parent.parent)
        sync_dir(self.private)

    def _publish(self, row):
        source = self._version_path(row["id"], row["version"])
        if source.is_symlink() or digest(source) != row["sha256"]:
            raise OSError("private version integrity failure")
        destination = self._token_path(row["token"])
        if destination.is_symlink() or not destination.exists() or digest(destination) != row["sha256"]:
            atomic_copy(source, destination, 0o644)
        else:
            os.chmod(destination, 0o644)

    def _withdraw(self, token):
        self._token_path(token).unlink(missing_ok=True)
        sync_dir(self.published)

    def _finish(self, config_id):
        with self.db.connect() as con:
            row = self._row(con, config_id)
            op = con.execute("SELECT * FROM operations WHERE config_id = ?", (config_id,)).fetchone()
        if op and op["old_token"] and op["old_token"] != row["token"]:
            self._withdraw(op["old_token"])
        if row["status"] == "deleting":
            self._withdraw(row["token"])
            directory = self.private / config_id
            if directory.exists():
                shutil.rmtree(directory)
                sync_dir(self.private)
            with self.db.connect() as con:
                con.execute("DELETE FROM configs WHERE id = ?", (config_id,))
            return
        if row["enabled"]:
            self._publish(row)
        else:
            self._withdraw(row["token"])
        with self.db.connect() as con:
            con.execute("UPDATE configs SET status = 'ready' WHERE id = ?", (config_id,))
            con.execute("DELETE FROM operations WHERE config_id = ?", (config_id,))
        self._prune(config_id)

    def _prune(self, config_id):
        with self.db.connect() as con:
            rows = con.execute("SELECT version FROM versions WHERE config_id = ? ORDER BY version DESC", (config_id,)).fetchall()
            keep = {r["version"] for r in rows[:self.settings.history_versions + 1]}
            for row in rows[self.settings.history_versions + 1:]:
                con.execute("DELETE FROM versions WHERE config_id = ? AND version = ?", (config_id, row["version"]))
        directory = self.private / config_id / "versions"
        for file in directory.iterdir():
            if file.name not in {f"{v}.yaml" for v in keep}:
                file.unlink()
        sync_dir(directory)

    def recover(self):
        with self.lock:
            self.ready = False
            with self.db.connect() as con:
                ids = [r[0] for r in con.execute("SELECT config_id FROM operations")]
            for config_id in ids:
                self._finish(config_id)
            with self.db.connect() as con:
                rows = [dict(r) for r in con.execute("SELECT * FROM configs")]
            expected = {f'{r["token"]}.yaml' for r in rows if r["enabled"]}
            # Remove revoked/orphan files before repairing active publications.
            for file in self.published.iterdir():
                if file.name not in expected or file.is_symlink():
                    file.unlink()
            sync_dir(self.published)
            for row in rows:
                if row["enabled"]:
                    self._publish(row)
                else:
                    source = self._version_path(row["id"], row["version"])
                    if source.is_symlink() or digest(source) != row["sha256"]:
                        raise OSError("private version integrity failure")
                self._prune(row["id"])
            known = {r["id"] for r in rows}
            isolated = 0
            for directory in self.private.iterdir():
                if directory.name == ".incoming":
                    for file in directory.iterdir():
                        file.unlink()
                elif directory.name not in known:
                    quarantine = self.root / "quarantine"
                    if quarantine.is_symlink():
                        raise RuntimeError("隔离目录异常")
                    quarantine.mkdir(mode=0o700, exist_ok=True)
                    dest = tempfile.mkdtemp(prefix=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-"), dir=quarantine)
                    directory.rename(Path(dest) / directory.name)
                    sync_dir(Path(dest))
                    sync_dir(quarantine)
                    sync_dir(self.private)
                    sync_dir(self.root)
                    isolated += 1
            if isolated:
                log.warning("private_entries_quarantined count=%d", isolated)
            self.failed = False
            self.ready = True

    def _complete(self, config_id):
        try:
            self._finish(config_id)
        except (OSError, sqlite3.Error):
            self.failed = True
            log.error("storage_operation_pending config_id=%s", config_id)
            raise AppError(503, "操作未完成，请检查磁盘和权限后重启管理端恢复") from None

    def create(self, source: Path, filename: str, name: str, description=""):
        name = display_name(name)
        config_id, stamp = str(uuid.uuid4()), now()
        with self.lock:
            self._writable()
            try:
                with self.db.connect() as con:
                    if con.execute("SELECT 1 FROM configs WHERE name = ?", (name,)).fetchone():
                        raise AppError(409, "显示名称已存在，请修改名称")
                    token = self._new_token(con)
                    self._store_version(source, config_id, 1)
                    size, sha = source.stat().st_size, digest(source)
                    con.execute("INSERT INTO configs VALUES (?, ?, ?, ?, ?, ?, 1, ?, 1, 'pending', ?, ?)", (config_id, name, description, filename, size, sha, token, stamp, stamp))
                    con.execute("INSERT INTO versions (config_id,version,filename,size,sha256,created_at) VALUES (?, 1, ?, ?, ?, ?)", (config_id, filename, size, sha, stamp))
                    con.execute("INSERT INTO operations VALUES (?, NULL)", (config_id,))
            except (OSError, sqlite3.Error):
                self.failed = True
                raise AppError(503, "存储写入失败，请检查磁盘和权限后重启管理端") from None
            self._complete(config_id)
            return self.get(config_id)

    def replace(self, config_id, source: Path, filename: str, source_version=None, *, expected_current_version=None):
        with self.lock:
            self._writable()
            try:
                with self.db.connect() as con:
                    row = self._row(con, config_id)
                    if expected_current_version is not None:
                        self.check_revision(row, "version", expected_current_version)
                    version, stamp = row["version"] + 1, next_update(row["updated_at"])
                    self._store_version(source, config_id, version)
                    size, sha = source.stat().st_size, digest(source)
                    con.execute("INSERT INTO versions (config_id,version,filename,size,sha256,created_at,source_version) VALUES (?, ?, ?, ?, ?, ?, ?)", (config_id, version, filename, size, sha, stamp, source_version))
                    con.execute("UPDATE configs SET filename=?, size=?, sha256=?, version=?, updated_at=?, status='pending' WHERE id=?", (filename, size, sha, version, stamp, config_id))
                    con.execute("INSERT INTO operations VALUES (?, NULL)", (config_id,))
            except (OSError, sqlite3.Error):
                self.failed = True
                raise AppError(503, "存储写入失败，请检查磁盘和权限后重启管理端") from None
            self._complete(config_id)
            return self.get(config_id)

    def change(self, config_id, action, expected_updated_at=None):
        with self.lock:
            self._writable()
            try:
                with self.db.connect() as con:
                    row = self._row(con, config_id)
                    if expected_updated_at is not None:
                        self.check_revision(row, "updated_at", expected_updated_at)
                    token = self._new_token(con) if action == "rotate-token" else row["token"]
                    enabled = action == "enable" if action in {"enable", "disable"} else row["enabled"]
                    state = "deleting" if action == "delete" else "pending"
                    con.execute("UPDATE configs SET token=?, enabled=?, status=?, updated_at=? WHERE id=?", (token, int(enabled), state, next_update(row["updated_at"]), config_id))
                    con.execute("INSERT INTO operations VALUES (?, ?)", (config_id, row["token"] if action == "rotate-token" else None))
            except sqlite3.Error:
                self.failed = True
                raise AppError(503, "数据库写入失败，请重启管理端恢复") from None
            self._complete(config_id)
            return None if action == "delete" else self.get(config_id)

    def rename(self, config_id, name=None, description=None, expected_updated_at=None):
        with self.lock:
            self._writable()
            try:
                with self.db.connect() as con:
                    row = self._row(con, config_id)
                    if expected_updated_at is not None:
                        self.check_revision(row, "updated_at", expected_updated_at)
                    con.execute("UPDATE configs SET name=?, description=?, updated_at=? WHERE id=?", (display_name(name) if name is not None else row["name"], description if description is not None else row["description"], next_update(row["updated_at"]), config_id))
            except sqlite3.IntegrityError:
                raise AppError(409, "显示名称已存在，请修改名称") from None
            except sqlite3.Error:
                self.failed = True
                raise AppError(503, "数据库写入失败，请重启管理端恢复") from None
            return self.get(config_id)

    def versions(self, config_id):
        with self.lock, self.db.connect() as con:
            current = self._row(con, config_id)
            return [dict(r) | {"is_current": r["version"] == current["version"]} for r in con.execute("SELECT * FROM versions WHERE config_id=? ORDER BY version DESC", (config_id,))]

    def historical(self, config_id, version):
        with self.lock, self.db.connect() as con:
            self._row(con, config_id)
            row = con.execute("SELECT * FROM versions WHERE config_id=? AND version=?", (config_id, version)).fetchone()
            if row is None:
                raise AppError(404, "版本不存在或已清理")
            path = self._version_path(config_id, version)
            try:
                if any(p.is_symlink() for p in (path, path.parent, path.parent.parent)) or path.stat().st_size != row["size"] or digest(path) != row["sha256"]:
                    raise OSError()
                return path, dict(row)
            except OSError:
                raise AppError(503, "历史文件完整性检查失败") from None

    def version_preview(self, config_id, version):
        with self.lock:
            path, _ = self.historical(config_id, version)
            return path.read_bytes()

    def rollback(self, config_id, version, expected):
        with self.lock:
            self._writable()
            current = self.get(config_id)
            if current["version"] != expected:
                raise AppError(409, "配置已在其他页面修改，请刷新后重试", code="stale", current=current)
            if version == current["version"]:
                raise AppError(409, "当前版本已变化或目标就是当前版本，请刷新")
            path, row = self.historical(config_id, version)
            return self.replace(config_id, path, row["filename"], version)
