from __future__ import annotations

import logging
import sqlite3
import threading
import time
from pathlib import Path

from .layout import state_dir

logger = logging.getLogger(__name__)

_local = threading.local()
_migration_lock = threading.Lock()

MIGRATIONS: list[tuple[int, list[str]]] = [
    (
        1,
        [
            """CREATE TABLE IF NOT EXISTS tickets (
                id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                expires_at REAL NOT NULL,
                files_left INTEGER NOT NULL,
                bytes_left INTEGER NOT NULL,
                burned INTEGER NOT NULL DEFAULT 0
            )""",
            """CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_try_at REAL NOT NULL,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                delivered_at REAL,
                payload TEXT NOT NULL,
                attachment_ids TEXT NOT NULL DEFAULT '',
                last_error TEXT,
                result TEXT
            )""",
            "CREATE INDEX IF NOT EXISTS jobs_due ON jobs (state, next_try_at)",
        ],
    ),
    (
        2,
        [
            """CREATE TABLE IF NOT EXISTS bundles (
                id TEXT PRIMARY KEY,
                created_at REAL NOT NULL,
                report_id TEXT,
                files TEXT NOT NULL
            )""",
        ],
    ),
]

SCHEMA_VERSION = MIGRATIONS[-1][0]


def db_path() -> Path:
    return state_dir() / "state.db"


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def migrate(conn: sqlite3.Connection) -> int:
    with _migration_lock:
        return _migrate_locked(conn)


def _migrate_locked(conn: sqlite3.Connection) -> int:
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"{db_path()} was written by a newer mail-report (schema v{current}, this build "
            f"understands v{SCHEMA_VERSION}). Roll the image forward again, or clear the "
            "state directory to start from a fresh database."
        )
    applied = 0
    for version, statements in MIGRATIONS:
        if version <= current:
            continue
        for attempt in range(3):
            try:
                conn.execute("BEGIN IMMEDIATE")
                if conn.execute("PRAGMA user_version").fetchone()[0] >= version:
                    conn.execute("COMMIT")
                    break
                for statement in statements:
                    conn.execute(statement)
                conn.execute(f"PRAGMA user_version = {version}")
                conn.execute("COMMIT")
                break
            except sqlite3.OperationalError:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if attempt == 2:
                    raise
                time.sleep(0.5 * (attempt + 1))
            except Exception:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
        applied += 1
        logger.info("applied state-db migration v%s", version)
    return applied


def connect() -> sqlite3.Connection:
    path = db_path()
    cached = getattr(_local, "conn", None)
    if cached is not None and getattr(_local, "path", None) == str(path):
        return cached
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    conn = sqlite3.connect(str(path), timeout=15, isolation_level=None)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    conn.execute("PRAGMA foreign_keys=ON")
    migrate(conn)
    _local.conn = conn
    _local.path = str(path)
    return conn


def describe() -> dict:
    conn = connect()
    return {
        "path": str(db_path()),
        "schema_version": conn.execute("PRAGMA user_version").fetchone()[0],
        "expected_version": SCHEMA_VERSION,
        "jobs": _columns(conn, "jobs") and sorted(_columns(conn, "jobs")) or [],
    }
