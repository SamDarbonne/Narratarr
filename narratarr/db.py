"""The database connection, and the schema migration.

APP-CONTRACT.md section 4 defines the schema. APP-CONTRACT.md section 14.1
defines the exact signature of every function below. Every other backend
module calls these functions and writes none of them.

**WAL mode is mandatory.** The runner writes while the API reads, and WAL is
what lets both happen at once. Refer to APP-CONTRACT.md section 4.
"""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from narratarr.config import get_settings

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# The schema version this program writes. APP-CONTRACT.md section 4 records
# the current version as 1. A migration adds a numbered step to MIGRATIONS
# below, and bumps this constant.
CURRENT_SCHEMA_VERSION = 1

# A migration step. Each function receives an open connection at the version
# named by its key, and must leave the database at that version plus one.
# Version 1 is the schema of schema.sql, so no migration exists yet. Add a
# step here, keyed by the version it upgrades FROM, when schema_version 2
# is needed. A migration never drops a column that holds delivered data.
MIGRATIONS: dict[int, "callable"] = {}


def connect() -> sqlite3.Connection:
    """Return a connection to the Narratarr database.

    `row_factory` is `sqlite3.Row`. WAL mode and foreign keys are on for
    every connection this function returns. The caller closes the
    connection.
    """
    settings = get_settings()
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        str(settings.db_path),
        timeout=30.0,
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def transaction() -> Iterator[sqlite3.Connection]:
    """Open a connection and a transaction.

    Commit when the block finishes without an error. Roll back and re-raise
    the error when the block raises one. Close the connection either way.
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def now() -> str:
    """Return the current UTC time, in the form YYYYMMDDThhmmssZ."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def new_id() -> str:
    """Return a fresh uuid4, as a 32-character hex string."""
    return uuid.uuid4().hex


def _read_schema_version(conn: sqlite3.Connection) -> int | None:
    """Return the recorded schema version, or None when meta holds none."""
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'schema_version'"
    ).fetchone()
    if row is None:
        return None
    return int(row["value"])


def _write_schema_version(conn: sqlite3.Connection, version: int) -> None:
    """Write the schema version into meta. Overwrite an existing value."""
    conn.execute(
        """
        INSERT INTO meta (key, value) VALUES ('schema_version', ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (str(version),),
    )


def init_db() -> None:
    """Apply schema.sql, then apply every migration in order.

    A second call is safe: schema.sql uses `CREATE ... IF NOT EXISTS`, and a
    migration already recorded in `meta.schema_version` does not run again.
    """
    settings = get_settings()
    settings.config_dir.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        with open(SCHEMA_PATH, "r", encoding="utf-8") as handle:
            conn.executescript(handle.read())
        conn.commit()

        version = _read_schema_version(conn)
        if version is None:
            version = CURRENT_SCHEMA_VERSION
            _write_schema_version(conn, version)
            conn.commit()

        while version in MIGRATIONS:
            MIGRATIONS[version](conn)
            version += 1
            _write_schema_version(conn, version)
            conn.commit()
    finally:
        conn.close()
