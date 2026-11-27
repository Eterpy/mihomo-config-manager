import sqlite3
from contextlib import contextmanager
from pathlib import Path


SCHEMA = """
CREATE TABLE configs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    version INTEGER NOT NULL,
    token TEXT NOT NULL UNIQUE,
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
    status TEXT NOT NULL CHECK(status IN ('ready', 'pending', 'deleting')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE versions (
    config_id TEXT NOT NULL REFERENCES configs(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    filename TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (config_id, version)
);
CREATE TABLE operations (
    config_id TEXT PRIMARY KEY REFERENCES configs(id) ON DELETE CASCADE,
    old_token TEXT
);
PRAGMA user_version = 1;
"""


MIGRATION_2 = (
    "ALTER TABLE versions ADD COLUMN source_version INTEGER",
    "CREATE TABLE backup_jobs (id TEXT PRIMARY KEY, status TEXT NOT NULL, created_at TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, sha256 TEXT, error_code TEXT)",
    "PRAGMA user_version=2",
)


def validate_schema(con):
    """Accept only the exact schema this release creates, including internal indexes.

    Never filter names with LIKE: '_' is a wildcard and can hide triggers.
    """
    expected = sqlite3.connect(':memory:')
    try:
        expected.executescript(SCHEMA)
        for sql in MIGRATION_2:
            expected.execute(sql)
        query = 'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name'
        if [tuple(r) for r in con.execute(query)] != expected.execute(query).fetchall():
            raise ValueError('schema')
    finally:
        expected.close()


class Database:
    def __init__(self, path: Path):
        self.path = path
        with self.connect(create=True) as con:
            con.execute("PRAGMA journal_mode=WAL")
            version = con.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                con.executescript("BEGIN IMMEDIATE;" + SCHEMA + "COMMIT;")
            if version in (0, 1):
                con.execute("BEGIN IMMEDIATE")
                for sql in MIGRATION_2:
                    con.execute(sql)
            elif version != 2:
                raise RuntimeError("数据库版本不兼容；请使用匹配的应用版本")

    @contextmanager
    def connect(self, *, create=False):
        mode = 'rwc' if create else 'rw'
        con = sqlite3.connect(f'{self.path.resolve().as_uri()}?mode={mode}', uri=True, timeout=5)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA synchronous=FULL")
        try:
            with con:
                yield con
        finally:
            con.close()

