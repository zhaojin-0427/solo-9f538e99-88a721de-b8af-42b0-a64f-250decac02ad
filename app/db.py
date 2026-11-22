"""SQLite persistence for registered handoff packages and supplement values.

The review workflow is deliberately stored in SQLite rather than in memory:

* a registered package is a *row*, not a live upload, so its samples can be
  queried one by one long after the original request has finished;
* supplements are committed atomically, so a rejected batch never leaves a
  half-written set of original values behind;
* reads run inside a single transaction.  Under WAL a reader therefore sees
  one committed snapshot for the whole sample range, even while a reviewer is
  committing supplements in another request.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

DEFAULT_DB_PATH = "data/review.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    package_no    INTEGER PRIMARY KEY AUTOINCREMENT,
    registered_at TEXT    NOT NULL,
    sample_count  INTEGER NOT NULL,
    old_lower     INTEGER NOT NULL,
    old_upper     INTEGER NOT NULL,
    eps           INTEGER NOT NULL,
    approximated  INTEGER NOT NULL,
    codec         TEXT    NOT NULL,
    byte_size     INTEGER NOT NULL,
    sha256        TEXT    NOT NULL,
    package       BLOB    NOT NULL
);

CREATE TABLE IF NOT EXISTS samples (
    package_no  INTEGER NOT NULL,
    sample_no   INTEGER NOT NULL,
    decoded     INTEGER NOT NULL,
    possible_lo INTEGER NOT NULL,
    possible_hi INTEGER NOT NULL,
    PRIMARY KEY (package_no, sample_no),
    FOREIGN KEY (package_no) REFERENCES packages(package_no) ON DELETE CASCADE
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS originals (
    package_no INTEGER NOT NULL,
    sample_no  INTEGER NOT NULL,
    value      INTEGER NOT NULL,
    updated_at TEXT    NOT NULL,
    PRIMARY KEY (package_no, sample_no),
    FOREIGN KEY (package_no, sample_no)
        REFERENCES samples(package_no, sample_no) ON DELETE CASCADE
) WITHOUT ROWID;
"""

_ready_paths: set[str] = set()
_init_lock = threading.Lock()


class DatabaseUnavailable(RuntimeError):
    """The database cannot be opened/created (usually a permissions problem)."""


def db_path() -> str:
    """Return the configured database path.

    ``GAUGE_DB_PATH`` is read on every call so tests (and operators changing
    the environment at start-up) do not have to reload this module.
    """
    return os.environ.get("GAUGE_DB_PATH") or DEFAULT_DB_PATH


def health() -> dict:
    """Best-effort readiness check used by the health endpoint."""
    path = db_path()
    try:
        ensure_ready()
        with connection() as conn:
            conn.execute("SELECT 1 FROM packages LIMIT 1").fetchall()
    except (sqlite3.Error, OSError, DatabaseUnavailable) as exc:
        return {"database": "unavailable", "detail": str(exc)}
    return {"database": "ready", "path": path}


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def ensure_ready() -> str:
    """Create the database directory and schema once per process."""
    path = db_path()
    key = path if path == ":memory:" else os.path.abspath(path)

    with _init_lock:
        if key in _ready_paths and (path == ":memory:" or os.path.exists(path)):
            return path

        try:
            if path != ":memory:":
                parent = os.path.dirname(os.path.abspath(path))
                if parent:
                    os.makedirs(parent, exist_ok=True)

            conn = _connect(path)
            try:
                # WAL lets a whole-range reader hold a stable snapshot while
                # supplements continue to commit from other connections.
                conn.execute("PRAGMA journal_mode = WAL")
                conn.executescript(_SCHEMA)
            finally:
                conn.close()
        except (sqlite3.Error, OSError) as exc:
            raise DatabaseUnavailable(
                f"cannot prepare review database at {path}: {exc}"
            ) from exc
        _ready_paths.add(key)
        return path


@contextmanager
def connection(*, write: bool = False) -> Iterator[sqlite3.Connection]:
    """Open a connection inside an explicit transaction.

    A write transaction takes the write lock immediately, so validation and
    insertion of a supplement batch cannot interleave with another writer.
    A read transaction starts its snapshot at the first SELECT and keeps it
    until the context exits.
    """
    ensure_ready()
    path = db_path()
    conn = _connect(path)
    try:
        if path == ":memory:":
            # Each in-memory connection starts empty, so the schema (created on
            # a throwaway connection by ensure_ready) has to be recreated.
            conn.executescript(_SCHEMA)
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        if conn.in_transaction:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        raise
    finally:
        conn.close()
