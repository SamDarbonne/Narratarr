"""Tests for narratarr/db.py.

APP-CONTRACT.md section 14.1 defines the exact signature of every function
under test. Section 4 requires WAL mode and foreign keys on every
connection.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from narratarr import db as db_module


def test_init_db_applies_the_schema(db):
    """init_db() creates every table of APP-CONTRACT.md section 4."""
    conn = db_module.connect()
    try:
        tables = {
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    finally:
        conn.close()

    expected = {
        "meta", "jobs", "gates", "review_items", "events",
        "targets", "deliveries", "api_keys", "settings",
    }
    assert expected <= tables


def test_init_db_is_idempotent(db):
    """A second init_db() call does not raise, and does not lose data."""
    conn = db_module.connect()
    try:
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('probe', 'still here')"
        )
        conn.commit()
    finally:
        conn.close()

    db_module.init_db()  # must not raise

    conn = db_module.connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'probe'").fetchone()
    finally:
        conn.close()
    assert row["value"] == "still here"


def test_init_db_records_schema_version(db):
    """meta.schema_version holds the current version after init."""
    conn = db_module.connect()
    try:
        row = conn.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    assert int(row["value"]) == db_module.CURRENT_SCHEMA_VERSION


def test_connect_turns_on_wal(db):
    """Every connection from connect() runs in WAL mode."""
    conn = db_module.connect()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode.lower() == "wal"


def test_connect_turns_on_foreign_keys(db):
    """Every connection from connect() enforces foreign keys."""
    conn = db_module.connect()
    try:
        enabled = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    finally:
        conn.close()
    assert enabled == 1


def test_foreign_keys_are_enforced(db):
    """An orphan gate row is refused, because foreign_keys is on."""
    conn = db_module.connect()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO gates (id, job_id, kind, state, created_at) "
                "VALUES ('g1', 'no-such-job', 'sample', 'open', '20260101T000000Z')"
            )
            conn.commit()
    finally:
        conn.close()


def test_now_format(db):
    """now() returns the UTC stamp YYYYMMDDThhmmssZ."""
    stamp = db_module.now()
    assert re.fullmatch(r"\d{8}T\d{6}Z", stamp)


def test_new_id_is_uuid4_hex(db):
    """new_id() returns a 32-character hex string, and each call is unique."""
    first = db_module.new_id()
    second = db_module.new_id()
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert first != second


def test_transaction_commits_on_success(db):
    """A transaction() block that raises nothing is committed."""
    with db_module.transaction() as conn:
        conn.execute("INSERT INTO meta (key, value) VALUES ('committed', 'yes')")

    conn = db_module.connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'committed'").fetchone()
    finally:
        conn.close()
    assert row["value"] == "yes"


def test_transaction_rolls_back_on_error(db):
    """A transaction() block that raises leaves no partial write."""
    with pytest.raises(RuntimeError):
        with db_module.transaction() as conn:
            conn.execute("INSERT INTO meta (key, value) VALUES ('rolled_back', 'no')")
            raise RuntimeError("boom")

    conn = db_module.connect()
    try:
        row = conn.execute("SELECT value FROM meta WHERE key = 'rolled_back'").fetchone()
    finally:
        conn.close()
    assert row is None
