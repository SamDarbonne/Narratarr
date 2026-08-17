"""The API key check.

APP-CONTRACT.md section 10.1 defines the rule. Every `/api/v1` route needs
the header `X-Api-Key`, except `GET /system/health` and the static files.
The check is a constant-time compare of the SHA-256 of the presented key.
**The database never holds the key itself, only its hash.**
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from typing import Optional

from narratarr.config import get_settings
from narratarr.db import new_id, now
from narratarr.models import ApiKeyRow


def hash_key(raw_key: str) -> str:
    """Return the hex SHA-256 of an API key."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def verify_key(conn: sqlite3.Connection, raw_key: str) -> Optional[ApiKeyRow]:
    """Return the matching key row, or None when no stored key matches.

    Every stored hash is compared with `hmac.compare_digest`, and the loop
    never returns early on a match. Response time then does not depend on
    which row, if any, matched, or on how many characters of a wrong key
    were correct.
    """
    if not raw_key:
        return None
    presented_hash = hash_key(raw_key)
    matched = None
    for row in conn.execute("SELECT * FROM api_keys").fetchall():
        if hmac.compare_digest(row["key_sha256"], presented_hash):
            matched = row
    if matched is None:
        return None
    conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now(), matched["id"]))
    conn.commit()
    return ApiKeyRow.from_row(matched)


def ensure_bootstrap_key(conn: sqlite3.Connection) -> Optional[str]:
    """Make the first API key when the table is empty. Return it once.

    APP-CONTRACT.md section 10.1: on first run, when no key exists and
    `NARRATARR_API_KEY` is empty, Narratarr makes one and prints it once to
    the log. When `NARRATARR_API_KEY` holds a value, that value becomes the
    stored key instead, and this function returns None: an
    operator-supplied key is never printed back.
    """
    row = conn.execute("SELECT COUNT(*) AS n FROM api_keys").fetchone()
    if row["n"] > 0:
        return None

    settings = get_settings()
    if settings.api_key:
        _insert_key(conn, "env", settings.api_key)
        conn.commit()
        return None

    raw_key = secrets.token_hex(32)
    _insert_key(conn, "bootstrap", raw_key)
    conn.commit()
    return raw_key


def _insert_key(conn: sqlite3.Connection, name: str, raw_key: str) -> None:
    """Insert one `api_keys` row for a plaintext key. Store only its hash."""
    conn.execute(
        "INSERT INTO api_keys (id, name, key_sha256, created_at) VALUES (?, ?, ?, ?)",
        (new_id(), name, hash_key(raw_key), now()),
    )
