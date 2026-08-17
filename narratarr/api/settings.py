"""Application settings, the library listing, and the watch-folder scan.

APP-CONTRACT.md section 13.4 defines these four routes. Section 10.2 rule 4
carries the rule this module must obey: a secret's presence is reportable,
its value is never returned.

`GET /settings` and `PUT /settings` operate on the `settings` table (schema
section 4.7): free-form, user-editable key-value pairs, distinct from the
environment-derived `Settings` object of `narratarr/config.py`. A person
tunes an operational value, for example the sample gate toggle, without
restarting the container.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from narratarr.api.common import paginate, require_key
from narratarr.config import get_settings
from narratarr.db import connect, now, transaction
from narratarr.models import SettingRow

router = APIRouter(prefix="/api/v1", tags=["settings"], dependencies=[Depends(require_key)])


# The named secrets APP-CONTRACT.md section 10 defines. `GET /settings`
# reports whether each is present in the environment, and never its value.
_SECRET_ENV_VARS = ["NARRATARR_API_KEY", "NARRATARR_ABS_TOKEN"]


class SettingsUpdateRequest(BaseModel):
    """The body of `PUT /settings`. Each key upserts one row."""

    settings: dict = Field(default_factory=dict)


def _file_mtime_stamp(mtime: float) -> str:
    """Return a UTC stamp in the house form, for a file's modified time.

    Section 4.2 of APP-CONTRACT.md: every timestamp in this database is
    `YYYYMMDDThhmmssZ`. A file's modified time is not a database column,
    but the library listing should still read like every other timestamp
    in the product.
    """
    return datetime.fromtimestamp(mtime, tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def _read_settings_table(conn) -> dict:
    """Return every row of the `settings` table, as a plain key-value dict."""
    rows = conn.execute("SELECT * FROM settings ORDER BY key ASC").fetchall()
    result = {}
    for row in rows:
        parsed = SettingRow.from_row(row).to_dict()
        result[parsed["key"]] = parsed["value"]
    return result


def _secret_presence() -> dict:
    """Return `{env_var_name: {"present": bool}}` for every named secret.

    Refer to APP-CONTRACT.md section 10.2 rule 4: this reports presence
    only, never the value.
    """
    return {name: {"present": bool(os.environ.get(name))} for name in _SECRET_ENV_VARS}


@router.get("/settings")
def get_settings_route():
    """Return every user-editable setting, and the presence of each secret."""
    conn = connect()
    try:
        return {
            "settings": _read_settings_table(conn),
            "secrets": _secret_presence(),
        }
    finally:
        conn.close()


@router.put("/settings")
def update_settings_route(body: SettingsUpdateRequest):
    """Upsert every key of `settings`. Returns the settings as they now stand."""
    with transaction() as conn:
        stamp = now()
        for key, value in body.settings.items():
            conn.execute(
                """
                INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """,
                (key, json.dumps(value), stamp),
            )
        return {
            "settings": _read_settings_table(conn),
            "secrets": _secret_presence(),
        }


@router.get("/library")
def list_library(limit: int = 50, offset: int = 0):
    """List the files under the library directory. Refer to APP-CONTRACT.md section 7.

    A file that already backs a job carries that job's id. A file with no
    matching job has not been ingested yet.
    """
    settings = get_settings()
    library_dir = settings.library_dir

    conn = connect()
    try:
        job_rows = conn.execute("SELECT id, source_path FROM jobs").fetchall()
    finally:
        conn.close()
    job_by_path = {row["source_path"]: row["id"] for row in job_rows}

    entries = []
    if library_dir.is_dir():
        for path in sorted(library_dir.rglob("*")):
            if not path.is_file():
                continue
            stat = path.stat()
            entries.append(
                {
                    "path": str(path),
                    "name": path.name,
                    "size": stat.st_size,
                    "modified_at": _file_mtime_stamp(stat.st_mtime),
                    "job_id": job_by_path.get(str(path)),
                }
            )
    return paginate(entries, limit, offset)


@router.post("/library/scan", status_code=202)
def scan_library():
    """Ask the watch-folder poller to scan now, instead of waiting.

    APP-CONTRACT.md section 9.4's rule applies here too: the API never
    does the work itself. `narratarr/adapter/ingest.py` (owner W2) polls
    `/watch` on its own schedule; this route only records a request, so
    the poller can notice and act on its next pass, or a scheduler can
    watch this key and wake the poller early.
    """
    with transaction() as conn:
        conn.execute(
            """
            INSERT INTO settings (key, value, updated_at) VALUES ('_watch_scan_requested_at', ?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (json.dumps(now()), now()),
        )
    return {"scan_requested": True}
